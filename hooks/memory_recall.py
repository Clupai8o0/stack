#!/usr/bin/env python3
"""Recall only the memories that matter for this prompt, instead of loading every memory every session.

Claude Code loads a project's whole MEMORY.md into every session. This keeps MEMORY.md down to the few memories
that apply to all work (feedback and user notes) and recalls the rest per prompt, by relevance:

  score  = keyword overlap between the prompt (plus repo and branch) and each memory's name, description and body,
           weighted by how rare each word is across all memories
  graph  = a memory linked by [[name]] from a strong match gets part of that match's score, so related memories
           come along even when they share no words with the prompt
  scope  = memories are gathered from every account (main, cx) for this folder and each folder above it, so a
           memory one account wrote reaches the others, and Codex, OpenCode and dsh too

  memory_recall.py prompt [--agent A]         UserPromptSubmit hook: adds the top matches as context (each at most
                                              once per session)
  memory_recall.py session-start [--agent A]  SessionStart hook: folds this session's MEMORY.md (Claude only) and,
                                              for other agents, recalls on the folder alone
  memory_recall.py search WORDS [--dir D]     CLI: rank memories for WORDS from folder D (default cwd)
  memory_recall.py share [--check]            CLI: make every cx memory dir a link to main's store for the same
                                              folder, merging what is in it (session-start does this per session)
  memory_recall.py fold [--all|--dir D] [--check]
                                              CLI: shrink MEMORY.md to the pinned memories and write the full
                                              catalogue to MEMORY.full.md (never loaded automatically)

Shared store: main's ~/.claude/projects/<folder>/memory is the one memory dir per folder. Every other account's dir
for that folder is a symlink to it, so both accounts read and write the same files. A merge never overwrites: when
two different files share a name, the newer one wins and the other is kept in the store's .merged/ folder.

Pinned = type feedback or user, or a MEMORY.md line containing "(pinned)" (remembered in .pins). Everything fails open.

A fold races with a session appending to MEMORY.md at the same moment, and can drop that appended line. That is
harmless by design: the memory file itself is untouched, an unpinned memory is recalled from its file anyway, and
the next fold re-adds a pointer for any pinned memory that has none. One narrow case is accepted: a "(pinned)" line
for a memory of another type, appended in the moment between the fold's last read and its write, is lost with its
pin; pin it again (or give the memory type feedback/user).
"""
import contextlib, fcntl, filecmp, glob, hashlib, json, math, os, re, shutil, subprocess, sys, time
CLUPAI_HOME = os.path.expanduser(os.environ.get('CLUPAI_HOME') or (
    '~/clupai' if os.path.isdir(os.path.expanduser('~/clupai'))
    else os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))  # where this repo is installed

HOME = os.path.expanduser('~')
CONFIG_DIRS = [os.path.join(HOME, d) for d in ('.claude', '.claude-exec')]
STATE = os.path.join(HOME, '.local', 'state', 'memory-recall')
TOP_K = 5              # memories per prompt at most
MIN_SCORE = 6.0        # below this a match is noise (real matches score 11-32; stray words 3-5)
REL_CUT = 0.4          # and a match must reach this share of the best one, so one strong hit brings no tail
BODY_TOP = 1           # how many of the best matches also get their body inlined
BODY_MAX = 900         # characters of body inlined
PIN_TYPES = {'feedback', 'user'}
STOP = set('''the and for are but not you your with this that from have has had was were will would can could should
into onto over under then than them they their there here what when where which while who whom why how all any
each few more most other some such only own same too very just also make made does did doing done get got going
use used using want wants need needs like please lets let our ours out off now new one two per via its it's i'm
about again after before being both between during until upon yes okay still ever even much many'''.split())
WORD = re.compile(r'[A-Za-z][A-Za-z0-9]+')
LINK = re.compile(r'\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]|\]\(([^)\s]+\.md)\)')


# ---------------------------------------------------------------- parsing

def tokens(text):
    out = []
    for w in WORD.findall(text or ''):
        parts = [w] + re.findall(r'[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+', w)   # MyCoolApp -> mycoolapp, my, cool, app
        for p in parts:
            p = p.lower()
            if len(p) >= 3 and p not in STOP:
                out.append(p)
    return out


def parse(path):
    """One memory file -> dict(name, description, type, body, links). None if unreadable."""
    try:
        text = open(path, encoding='utf-8', errors='replace').read()
    except OSError:
        return None
    meta, body = {}, text
    if text.startswith('---'):
        end = text.find('\n---', 3)
        if end != -1:
            for line in text[3:end].splitlines():
                m = re.match(r'\s*(name|description|type)\s*:\s*(.+?)\s*$', line)
                if m and m.group(1) not in meta:
                    meta[m.group(1)] = m.group(2).strip('\'"')
            body = text[end + 4:]
    slug = os.path.splitext(os.path.basename(path))[0]
    links = set()
    for a, b in LINK.findall(body):
        links.add((a or os.path.splitext(os.path.basename(b))[0]).strip())
    return dict(path=path, slug=slug, name=meta.get('name', slug), description=meta.get('description', ''),
                type=meta.get('type', ''), body=body.strip(), links=links)


def slug_of(path):
    return re.sub(r'[^A-Za-z0-9]', '-', path)


POLICY_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'model-policy.json')
OUTSIDE = {'dsh', 'kimi', 'opencode', 'agy', 'grok'}       # agents model-policy.json restricts by folder (Claude is never restricted)


def allowed(folder, agent):
    """model-policy.json: may this agent see what was written about this folder? Longest rule wins; unlisted
    folders are confidential. Fails closed: an unreadable policy lets no outside model see anything."""
    if agent not in OUTSIDE and agent != 'codex':
        return True
    try:
        pol = json.load(open(POLICY_FILE))
    except (OSError, ValueError):
        return agent == 'codex'          # Codex is allowed everywhere; nobody else without a readable policy
    best, policy = -1, pol.get('default', 'confidential')
    real = os.path.realpath(folder)
    for r in pol.get('rules', []):
        root = os.path.realpath(os.path.expanduser(r.get('path', '')))
        if (real == root or real.startswith(root + os.sep)) and len(root) > best:
            best, policy = len(root), r.get('policy', 'confidential')
    return agent in pol.get('routes', {}).get(policy, [])


CWD_IN_LOG = re.compile(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')


def origins_allowed(memdir, agent):
    """The slug maps several paths to one folder (/a/b-c and /a/b/c both give -a-b-c), so a memory dir cannot
    prove which folder wrote it. The session logs beside it can: every cwd they record must be one the agent may
    see. A dir with no readable log is refused for outside models."""
    if agent not in OUTSIDE:
        return True
    slug = os.path.basename(os.path.dirname(os.path.abspath(memdir)))
    logs = []                                   # the dir is shared, so every account's sessions may have written it
    for cfg in CONFIG_DIRS:
        logs += sorted(glob.glob(os.path.join(cfg, 'projects', slug, '*.jsonl')), key=os.path.getmtime)[-200:]
    seen = set()
    for f in logs:
        try:
            with open(f, 'rb') as fh:
                head = fh.read(65536).decode('utf-8', 'replace')
        except OSError:
            continue
        for c in CWD_IN_LOG.findall(head):
            seen.add(json.loads(f'"{c}"'))
    return bool(seen) and all(allowed(c, agent) for c in seen)


def memory_dirs(cwd, own=None, agent='claude'):
    """Every account's memory dir for cwd and each folder above it (up to $HOME), own session's dir first.
    For an outside model, only folders model-policy.json lets it see: a personal project's memories reach dsh,
    but the memories of the folder above it (unlisted, so confidential) do not."""
    dirs, d = [], os.path.realpath(cwd or os.getcwd())
    chain = []
    while d.startswith(HOME) and d != HOME:
        chain.append(d)
        d = os.path.dirname(d)
    for folder in chain:
        if not allowed(folder, agent):
            continue
        for cfg in CONFIG_DIRS:
            m = os.path.join(cfg, 'projects', slug_of(folder), 'memory')
            if os.path.isdir(m) and origins_allowed(m, agent) and \
                    os.path.realpath(m) not in {os.path.realpath(x) for x in dirs}:   # a shared store counts once
                dirs.append(m)
    if own and os.path.isdir(own):
        dirs = [own] + [x for x in dirs if os.path.realpath(x) != os.path.realpath(own)]
    return dirs


def load(dirs):
    nodes, seen = [], set()
    for d in dirs:
        for f in sorted(glob.glob(os.path.join(d, '*.md'))):
            base = os.path.basename(f)
            if base in ('MEMORY.md', 'MEMORY.full.md'):
                continue
            n = parse(f)
            if not n:
                continue
            key = hashlib.sha1(n['body'].encode()).hexdigest()   # same memory copied into two accounts
            if key in seen:
                continue
            seen.add(key)
            n['dir'] = d
            nodes.append(n)
    return nodes


# ---------------------------------------------------------------- ranking

def rank(nodes, query, context=''):
    """Score every node against the query; context words (repo, branch) count half."""
    if not nodes:
        return []
    q = tokens(query)
    c = [t for t in tokens(context) if t not in q]
    if not q and not c:
        return []
    fields = []
    df = {}
    for n in nodes:
        f = dict(name=set(tokens(n['name'] + ' ' + n['slug'])), desc=set(tokens(n['description'])),
                 body=tokens(n['body']))
        f['bodyset'] = set(f['body'])
        fields.append(f)
        for t in f['name'] | f['desc'] | f['bodyset']:
            df[t] = df.get(t, 0) + 1
    N = len(nodes)
    idf = lambda t: math.log(1 + (N - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5))

    def score(f, words, weight):
        s = 0.0
        for t in set(words):
            if t not in df:
                continue
            tf = f['body'].count(t) if t in f['bodyset'] else 0
            s += weight * idf(t) * (3.0 * (t in f['name']) + 2.0 * (t in f['desc']) + min(tf, 3) / 3.0)
        return s

    base = [score(f, q, 1.0) + score(f, c, 0.5) for f in fields]
    by_slug = {}
    for i, n in enumerate(nodes):
        by_slug.setdefault(n['slug'], []).append(i)
        by_slug.setdefault(n['name'], []).append(i)
    final = list(base)
    for i, n in enumerate(nodes):          # spread along [[links]] in both directions, one hop
        for l in n['links']:
            for j in by_slug.get(l, []):
                if j != i:
                    final[j] = max(final[j], base[j] + 0.35 * base[i])
                    final[i] = max(final[i], base[i] + 0.35 * base[j])
    order = sorted(range(N), key=lambda i: -final[i])
    return [(final[i], nodes[i]) for i in order if final[i] > 0]


# ---------------------------------------------------------------- MEMORY.md folding

def pinned_slugs(memdir):
    """Slugs that stay in MEMORY.md: feedback/user types, plus any line the user marked (pinned)."""
    pins = set()
    for f in glob.glob(os.path.join(memdir, '*.md')):
        n = parse(f)
        if n and n['type'] in PIN_TYPES:
            pins.add(n['slug'])
    manual = set()
    try:
        for line in open(os.path.join(memdir, 'MEMORY.md')):
            if '(pinned)' in line.lower():
                m = re.search(r'\(([^)]+\.md)\)', line)
                if m:
                    manual.add(os.path.splitext(os.path.basename(m.group(1)))[0])
    except OSError:
        pass
    pin_file = os.path.join(memdir, '.pins')     # manual pins also live here, so a line lost to a race is not
    try:                                         # a pin lost for good
        manual |= {l.strip() for l in open(pin_file) if l.strip()}
    except OSError:
        pass
    manual = {m for m in manual if os.path.exists(os.path.join(memdir, m + '.md'))}
    try:
        if manual:
            atomic_write(pin_file, '\n'.join(sorted(manual)) + '\n')
    except OSError:
        pass
    return pins | manual, manual


HUB = ('<!-- memory_recall.py keeps this file short. Only pinned memories live here; every other memory is recalled '
       'per prompt when relevant. Full catalogue: MEMORY.full.md. Search: python3 '
       '$CLUPAI_HOME/hooks/memory_recall.py search WORDS -->')


def fold(memdir, check=False):
    """MEMORY.md -> pinned lines only; MEMORY.full.md -> every memory. Returns (lines_before, lines_after) or None."""
    path = os.path.join(memdir, 'MEMORY.md')
    try:
        before = open(path).read()
    except OSError:
        return None
    lines = before.splitlines()
    pins, manual = pinned_slugs(memdir)
    keep, moved = [], []
    for line in lines:
        if line.strip() == HUB or not line.strip():
            continue
        m = re.search(r'\]\(([^)]+\.md)\)', line)
        slug = os.path.splitext(os.path.basename(m.group(1)))[0] if m else None
        if slug is None or slug in pins:
            keep.append(line)                 # headings, notes, pinned pointers stay
        else:
            moved.append(line)
    listed = {os.path.splitext(os.path.basename(m.group(1)))[0]
              for m in (re.search(r'\]\(([^)]+\.md)\)', l) for l in keep) if m}
    missing = []
    for slug in sorted(pins - listed):         # e.g. a line another session appended while an earlier fold ran
        n = parse(os.path.join(memdir, slug + '.md'))
        if n:
            mark = ' (pinned)' if slug in manual else ''
            missing.append(f'- [{n["name"]}]({slug}.md) — {n["description"]}{mark}')
    if not moved and not missing:
        return None
    keep += missing
    after = HUB + '\n' + '\n'.join(keep) + ('\n' if keep else '')
    if check:
        return len(lines), len(keep) + 1
    # full catalogue first: regenerated from the files themselves, so nothing is ever lost
    cat = ['# All memories in this folder (not loaded automatically; recalled per prompt by memory_recall.py)', '']
    for n in sorted(load([memdir]), key=lambda n: n['slug']):
        cat.append(f'- [{n["name"]}]({os.path.basename(n["path"])}) — {n["description"]}')
    atomic_write(os.path.join(memdir, 'MEMORY.full.md'), '\n'.join(cat) + '\n')
    if open(path).read() != before:           # another session appended meanwhile; next session folds it
        return None
    atomic_write(path, after)
    return len(lines), len(keep) + 1


def atomic_write(path, text):
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w') as f:
        f.write(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------- one shared store per folder

def shared_memdir(memdir):
    """main's memory dir for the same folder: the store every account's dir links to."""
    return os.path.join(CONFIG_DIRS[0], 'projects', os.path.basename(os.path.dirname(os.path.abspath(memdir))), 'memory')


def catalogue(memdir):
    cat = ['# All memories in this folder (not loaded automatically; recalled per prompt by memory_recall.py)', '']
    for n in sorted(load([memdir]), key=lambda n: n['slug']):
        cat.append(f'- [{n["name"]}]({os.path.basename(n["path"])}) — {n["description"]}')
    atomic_write(os.path.join(memdir, 'MEMORY.full.md'), '\n'.join(cat) + '\n')


def merge_dir(src, dst, label):
    """Move everything in src into dst. Never overwrites: the older of two different files goes to dst/.merged/."""
    kept = os.path.join(dst, '.merged')
    changed = 0

    def aside(path, who, name):
        base = f'{who}-{time.strftime("%Y%m%d-%H%M%S")}-{os.getpid()}'
        n, out = 0, os.path.join(kept, f'{base}-{name}')
        while os.path.lexists(out):
            n += 1
            out = os.path.join(kept, f'{base}-{n}-{name}')
        shutil.move(path, out)

    for name in sorted(os.listdir(src)):
        s, d = os.path.join(src, name), os.path.join(dst, name)
        if name == 'MEMORY.full.md':
            os.remove(s)                        # regenerated from the files below
        elif name in ('MEMORY.md', '.pins') and os.path.isfile(d) and os.path.isfile(s):
            have = open(d, encoding='utf-8', errors='replace').read().splitlines()
            links = {m.group(1) for m in (re.search(r'\]\(([^)]+\.md)\)', l) for l in have) if m}
            add = [l for l in open(s, encoding='utf-8', errors='replace').read().splitlines()
                   if l.strip() and l not in have and l.strip() != HUB
                   and not ((m := re.search(r'\]\(([^)]+\.md)\)', l)) and m.group(1) in links)]
            if add:
                atomic_write(d, '\n'.join(have + add) + '\n')
                changed += 1
            os.remove(s)
        elif not os.path.lexists(d):
            shutil.move(s, d)
            changed += 1
        elif os.path.isfile(s) and os.path.isfile(d) and filecmp.cmp(s, d, shallow=False):
            os.remove(s)
        else:
            os.makedirs(kept, exist_ok=True)
            if os.path.isfile(s) and os.path.isfile(d) and os.path.getmtime(s) > os.path.getmtime(d):
                aside(d, 'store', name)
                shutil.move(s, d)
            else:
                aside(s, label, name)
            changed += 1
    return changed


def share(memdir, check=False):
    """Make memdir (a cx dir) a symlink to the shared store, merging its files in. Idempotent; returns what it did
    or None. Leftovers of an interrupted merge (memory.merging-*) are merged on the next call."""
    memdir = os.path.abspath(memdir)
    target = shared_memdir(memdir)
    if not any(memdir.startswith(os.path.join(c, 'projects') + os.sep) for c in CONFIG_DIRS[1:]):
        return None                             # main's own store, or a config dir that is not one of ours
    stranded = glob.glob(glob.escape(memdir) + '.merging-*')
    if os.path.islink(memdir) and os.readlink(memdir) == target and not stranded:
        return None
    label = os.path.basename(memdir.split('/projects/')[0]).lstrip('.') or 'acct'
    if check:
        return 'would merge and link' if os.path.isdir(memdir) and not os.path.islink(memdir) else 'would link'
    os.makedirs(STATE, exist_ok=True)
    lock = os.path.join(STATE, 'share-' + hashlib.sha1(target.encode()).hexdigest()[:12] + '.lock')
    with open(lock, 'a') as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)          # two sessions starting in one folder at once
        if os.path.islink(target) or (os.path.lexists(memdir) and
                                      os.path.realpath(memdir) == os.path.realpath(target) and
                                      not os.path.islink(memdir)):
            raise OSError(f'refusing to share {memdir}: the store {target} is a link or the same dir')
        os.makedirs(target, exist_ok=True)
        done = []
        if os.path.islink(memdir) and os.readlink(memdir) != target:
            os.unlink(memdir)
        if not os.path.lexists(memdir):
            os.symlink(target, memdir)
            done.append('linked')
        elif not os.path.islink(memdir):
            old = f'{memdir}.merging-{os.getpid()}-{int(time.time())}'
            os.rename(memdir, old)
            os.symlink(target, memdir)          # writers land in the shared store from here on
            stranded.append(old)
            done.append('linked')
        n, left = 0, []
        for old in stranded:
            n += merge_dir(old, target, label)
            if os.listdir(old):
                left.append(old)
            else:
                os.rmdir(old)
        if stranded:
            catalogue(target)
            done.insert(0, f'merged {n}')
        return ' and '.join(done) + (f' ({len(left)} dirs left: {", ".join(left)})' if left else '')


def share_all(check=False):
    for cfg in CONFIG_DIRS[1:]:
        for m in sorted(glob.glob(os.path.join(cfg, 'projects', '*', 'memory'))):
            try:
                r = share(m, check)
            except OSError as e:
                r = f'FAILED {e}'
            if r:
                print(f'{r:28} {m.replace(HOME, "~")}')


# ---------------------------------------------------------------- hooks

def git(cwd, *args):
    try:
        r = subprocess.run(['git', '-C', cwd, *args], capture_output=True, text=True, timeout=2)
        return r.stdout.strip() if r.returncode == 0 else ''
    except (OSError, subprocess.SubprocessError):
        return ''


def own_memdir(data):
    t = data.get('transcript_path')
    if isinstance(t, str) and t:
        return os.path.join(os.path.dirname(t), 'memory')
    return None


def already_loaded(memdir):
    """Memories this Claude session already has through its MEMORY.md, so recall does not repeat them."""
    try:
        return {os.path.splitext(os.path.basename(m))[0]
                for m in re.findall(r'\]\(([^)]+\.md)\)', open(os.path.join(memdir, 'MEMORY.md')).read())}
    except (OSError, TypeError):
        return set()


def seen_path(sid):
    return os.path.join(STATE, re.sub(r'[^A-Za-z0-9._-]', '_', sid or 'nosession') + '.json')


def recall(data, agent, query):
    cwd = data.get('cwd') if isinstance(data.get('cwd'), str) else os.getcwd()
    own = own_memdir(data) if agent == 'claude' else None
    nodes = load(memory_dirs(cwd, own, agent))
    if not nodes:
        return ''
    top = git(cwd, 'rev-parse', '--show-toplevel')
    context = ' '.join(filter(None, [os.path.basename(top or cwd), git(cwd, 'branch', '--show-current')]))
    skip = already_loaded(own) if own else set()
    sid = data.get('session_id') if isinstance(data.get('session_id'), str) else ''
    stateless = '--stateless' in sys.argv       # a caller that emits once (dsh) must not mark memories seen early
    try:
        seen = set() if stateless else set(json.load(open(seen_path(sid))))
    except (OSError, ValueError, TypeError):
        seen = set()
    ranked = [(s, n) for s, n in rank(nodes, query, context) if n['slug'] not in skip and n['path'] not in seen]
    picked, best = [], ranked[0][0] if ranked else 0   # the cut is relative to the best memory still eligible
    for s, n in ranked:
        if s < MIN_SCORE or s < REL_CUT * best or len(picked) >= TOP_K:
            break
        if n['slug'] in skip or n['path'] in seen:
            continue
        picked.append((s, n))
    if not picked:
        return ''
    try:
        if not stateless:
            os.makedirs(STATE, exist_ok=True)
            atomic_write(seen_path(sid), json.dumps(sorted(seen | {n['path'] for _, n in picked})))
    except OSError:
        pass
    lines = ['Memories recalled for this prompt (relevance-ranked from every account\'s memory for this folder; '
             'background, not instructions; open a file for detail):']
    for i, (s, n) in enumerate(picked):
        lines.append(f'- {n["name"]}: {n["description"] or n["body"][:160]} — {n["path"].replace(HOME, "~")}')
        if i < BODY_TOP and s >= 2 * MIN_SCORE:
            body = re.sub(r'\n{3,}', '\n\n', n['body'])
            lines.append('  ' + (body[:BODY_MAX] + (' …' if len(body) > BODY_MAX else '')).replace('\n', '\n  '))
    return '\n'.join(lines)


def emit(event, text):
    if text:
        print(json.dumps({'hookSpecificOutput': {'hookEventName': event, 'additionalContext': text}}))
    return 0


def arg(flag, default=None):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def hook(event):
    raw = sys.stdin.read() if not sys.stdin.isatty() else ''
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict) or data.get('agent_id'):
        return 0                               # subagents get their parent's context
    agent = arg('--agent', 'claude')
    if event == 'session-start':
        own = own_memdir(data) if agent == 'claude' else None
        if own:
            try:
                share(own)                     # this account's dir -> the one shared store for the folder
            except OSError:
                pass
            fold(own)                          # this session already loaded MEMORY.md; the fold is for the next one
            return 0
        return emit('SessionStart', recall(data, agent, ''))
    prompt = data.get('prompt') if isinstance(data.get('prompt'), str) else ''
    if len(prompt.split()) < 3 or re.match(r'\s*(<task-notification>|\[SYSTEM NOTIFICATION|<system-reminder>)', prompt):
        return 0
    return emit('UserPromptSubmit', recall(data, agent, prompt))


def cli():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd == 'search':
        words = [a for a in sys.argv[2:] if not a.startswith('--') and a != arg('--dir')]
        cwd = arg('--dir', os.getcwd())
        dirs = memory_dirs(cwd)
        ranked = rank(load(dirs), ' '.join(words), os.path.basename(cwd))
        print(f'{len(dirs)} memory dirs for {cwd}')
        for s, n in ranked[:10]:
            print(f'{s:6.1f}  {n["name"]} — {n["description"][:90]}\n        {n["path"].replace(HOME, "~")}')
        return 0
    if cmd == 'share':
        share_all('--check' in sys.argv)
        return 0
    if cmd == 'fold':
        check = '--check' in sys.argv
        targets = (sorted({os.path.realpath(d) for d in glob.glob(os.path.join(HOME, '.claude*', 'projects', '*', 'memory'))})
                   if '--all' in sys.argv else [arg('--dir', os.getcwd())])
        for d in targets:
            r = fold(d, check)
            if r:
                print(f'{"would fold" if check else "folded"}  {r[0]:3} -> {r[1]:3} lines  {d.replace(HOME, "~")}')
        return 0
    print(__doc__.strip())
    return 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ('prompt', 'session-start'):
        try:
            return hook(sys.argv[1])
        except Exception as e:                 # never block a prompt because recall failed
            print(f'memory_recall: {type(e).__name__}: {e}', file=sys.stderr)
            return 0
    return cli()


if __name__ == '__main__':
    sys.exit(main())
