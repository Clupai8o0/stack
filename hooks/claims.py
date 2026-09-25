#!/usr/bin/env python3
"""Shared file claims for parallel agent sessions on one repo (all worktrees, all Claude accounts and Codex).

Claims live in <git common dir>/claude-claims.json, which every worktree of the repo shares and git never commits.
Key = path relative to the worktree root, so the same file in two worktrees counts as the same claim.

  claims.py pretool            PreToolUse hook for Edit|Write|MultiEdit|NotebookEdit|Bash. Auto-claims the file for this
                               session. Denies if a live session edits the same file in the SAME worktree; warns if a
                               live session edits it in another worktree (merge conflict ahead). For Bash only the
                               obvious in-place edits count: sed -i, gsed -i, perl -i on literal paths.
  claims.py session-start      SessionStart hook: shows other live sessions' notes and recent history for the cwd's repo,
                               or for every repo one level down when the cwd is a folder of repos.
  claims.py prompt             UserPromptSubmit hook: after NOTE_EVERY turns with edits in a repo and no fresh note, asks
                               the session to update its note during this turn (a nudge, not a forced extra call).
  claims.py session-end        SessionEnd hook: logs and drops this session's claims and notes in every repo it touched.
  claims.py list [DIR]         show live notes and claims for the repo at DIR (default cwd)
  claims.py note TEXT          from inside a session: status for the cwd's repo, "doing now; done/tried: ..."
  claims.py note --clear       remove that note
  claims.py release [DIR]      from inside a session: drop its claims and note in the repo at DIR
  claims.py failure            PostToolUseFailure hook for Bash: after a risky command fails, suggests a guardrail
  (queues.py and guardrails.py in this folder are checked from the same PreToolUse hook)
  claims.py closeout           from inside a session: what it still holds (claims, notes, queue places) before closing
  claims.py history [-n N] [--grep TEXT] [DIR]
                               past notes, with exact times and the files edited in between

Every note and clear is appended to <git common dir>/claude-claims-history.jsonl, and so are release and session end
when files were edited since the last record, so replaced notes stay as a record of what was done or tried.

A claim or note expires after EXPIRE_H hours without an edit, or as soon as its session is no longer running
(registry pid alive and still the same agent process). Any failure lets the edit through.
Codex sessions register themselves through codex_hook.py and are treated exactly like Claude ones.
"""
import fcntl, glob, json, os, re, shlex, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from memory_guard import is_same_claude  # pid-reuse check, shared with the memory guard
except Exception:  # without it, fall back to a bare pid check rather than break every edit
    def is_same_claude(record):
        return True

EXPIRE_H = 4
NOTE_EVERY = 3  # turns with edits a session may take before the Stop hook asks for a fresh note
REGISTRIES = [os.path.expanduser('~/.claude/sessions'), os.path.expanduser('~/.claude-exec/sessions'),  # main, cx
              os.path.expanduser('~/.codex/agent-sessions'),  # codex   } none of these four writes a registry,
              os.path.expanduser('~/.local/state/opencode/agent-sessions'),  # opencode } so agent_hook.py writes
              os.path.expanduser('~/.dsh/agent-sessions'),  # dsh     } one for them
              os.path.expanduser('~/.kimi-code/agent-sessions'),  # kimi    }
              os.path.expanduser('~/.grok/agent-sessions')]  # grok    }
INDEX_DIR = os.environ.get('CLAIMS_STATE_DIR') or os.path.expanduser('~/.local/state/claude-claims')  # <sessionId>.stores: every store a session wrote to
INPLACE_HINT = re.compile(r'\b(?:g?sed|perl)\b[\s\S]*?(?:\s-[A-Za-z]*[iI]|--in-place)')


def git(cwd, *args):
    r = subprocess.run(['git', '-C', cwd, *args], capture_output=True, text=True, timeout=3)
    return r.stdout.strip() if r.returncode == 0 else None


def repo_of(path):
    d = path if os.path.isdir(path) else os.path.dirname(path)
    while d and not os.path.isdir(d):
        d = os.path.dirname(d)
    top = git(d, 'rev-parse', '--show-toplevel')
    common = git(d, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    if not top or not common:
        return None
    return os.path.realpath(top), os.path.join(common, 'claude-claims.json')


def live_sessions():
    """sessionId -> name for every session whose process is still running and was not replaced by a reused pid."""
    live = {}
    for reg in REGISTRIES:
        for f in glob.glob(os.path.join(reg, '*.json')):
            try:
                d = json.load(open(f))
                os.kill(int(d['pid']), 0)
                if 'procStart' in d and not is_same_claude(d):
                    continue
                live[d['sessionId']] = d.get('name') or str(d['pid'])
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                continue
    return live


def current_session():
    """sessionId of the Claude session this command runs under (for the CLI), or None."""
    def registered(pid):
        for reg in REGISTRIES:
            # agent_hook.py keeps one file per session (<pid>-<key>.json): take that process's newest session.
            # Checked before <pid>.json, which in an agent registry is a stale record from the first adapter.
            recs = []
            for f in glob.glob(os.path.join(reg, f'{pid}-*.json')):
                try:
                    d = json.load(open(f))
                    recs.append((d.get('updatedAt') or 0, d['sessionId']))
                except (OSError, ValueError, KeyError, TypeError):
                    continue
            if recs:
                return max(recs)[1]
            try:
                return json.load(open(os.path.join(reg, f'{pid}.json')))['sessionId']  # follows /clear, the env var may not
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return None

    pid = os.environ.get('CLAUDE_PID', '')
    if pid.isdigit() and registered(int(pid)):
        return registered(int(pid))
    p = os.getppid()
    for _ in range(20):  # no CLAUDE_PID: walk up to the Claude process
        if p <= 1:
            break
        if registered(p):
            return registered(p)
        out = subprocess.run(['/bin/ps', '-o', 'ppid=', '-p', str(p)], capture_output=True, text=True, timeout=3).stdout
        p = int(out.strip() or 0)
    return os.environ.get('CLAUDE_CODE_SESSION_ID') or None


class Store:
    """Lock a sidecar lockfile (fail open after LOCK_WAIT seconds), then replace the JSON atomically."""
    LOCK_WAIT = 3
    KEYS = ('claims', 'notes', 'logged')  # list fields every load guarantees

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.lock = open(self.path + '.lock', 'a')
        deadline = time.time() + self.LOCK_WAIT
        while True:
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() > deadline:
                    self.lock.close()
                    raise TimeoutError('claims lock busy')
                time.sleep(0.05)
        try:
            self.data = json.load(open(self.path))
            if not isinstance(self.data, dict):
                self.data = {}
        except (OSError, ValueError):
            self.data = {}
        for key in self.KEYS:
            if not isinstance(self.data.get(key), list):
                self.data[key] = []
        return self

    def prune(self, live):
        cutoff = time.time() - EXPIRE_H * 3600
        for key in ('claims', 'notes', 'logged'):
            self.data[key] = [c for c in self.data[key]
                              if isinstance(c, dict) and c.get('session') in live and c.get('at', 0) > cutoff]

    def drop(self, sid, worktree=None):
        for key in ('claims', 'notes', 'logged'):
            self.data[key] = [c for c in self.data[key] if isinstance(c, dict)
                              and not (c.get('session') == sid and (worktree is None or c.get('worktree') == worktree))]

    def __exit__(self, exc_type, *exc):
        try:
            if exc_type is None:
                tmp = self.path + f'.{os.getpid()}.tmp'
                with open(tmp, 'w') as f:
                    json.dump(self.data, f, indent=1)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
        finally:
            fcntl.flock(self.lock, fcntl.LOCK_UN)
            self.lock.close()


def remember_store(sid, store):
    """Record that this session wrote to store, so SessionEnd can find every repo it touched."""
    os.makedirs(INDEX_DIR, exist_ok=True)
    index = os.path.join(INDEX_DIR, f'{os.path.basename(sid)}.stores')
    try:
        known = open(index).read().splitlines()
    except OSError:
        known = []
    if store not in known:
        with open(index, 'a') as f:
            f.write(store + '\n')


def sed_files(args, bsd=True):
    """Files `sed -i` edits. BSD sed (/usr/bin/sed) always takes the word after a bare -i as the backup suffix."""
    files, inplace, script_given, i = [], False, False, 0
    while i < len(args):
        a = args[i]
        if a == '--':
            files += args[i + 1:]
            break
        if a.startswith('--'):
            inplace |= a.startswith('--in-place')
            if a in ('--expression', '--file'):
                script_given, i = True, i + 1
            script_given |= a.startswith(('--expression=', '--file='))
        elif a.startswith('-') and len(a) > 1:
            flags = a[1:]
            for j, ch in enumerate(flags):
                if ch in 'iI':
                    inplace = True
                    if bsd and j == len(flags) - 1:
                        i += 1
                    break  # anything after i in the same word is the suffix
                if ch in 'ef':
                    script_given = True
                    if j == len(flags) - 1:
                        i += 1
                    break
        else:
            files.append(a)
        i += 1
    if not script_given:
        files = files[1:]
    return files if inplace else []


def perl_files(args):
    """Files `perl -i` edits: the words after the switches, minus the script file when there is no -e."""
    inplace, script_given, i = False, False, 0
    while i < len(args) and args[i].startswith('-') and args[i] != '-':
        if args[i] == '--':
            i += 1
            break
        flags = args[i][1:]
        for j, ch in enumerate(flags):
            if ch == 'i':
                inplace = True
                break
            if ch in 'eE':
                script_given = True
                if j == len(flags) - 1:
                    i += 1
                break
            if ch in 'MmI0lFxCdD':  # these take the rest of the word as their value
                break
        i += 1
    rest = args[i:]
    if not script_given:
        rest = rest[1:]
    return rest if inplace else []


def bash_targets(command, cwd):
    """Existing files that obvious in-place edits in command write to. Anything unclear is left out (fail open)."""
    if not INPLACE_HINT.search(command) or '<<' in command:  # a heredoc body would parse as commands
        return []
    lex = shlex.shlex(command, posix=True, punctuation_chars='();<>|&\n')
    lex.whitespace, lex.whitespace_split = ' \t\r', True
    try:
        tokens = list(lex)
    except ValueError:  # unbalanced quotes, usually a heredoc body
        return []
    targets, here, seg, skip, outer, prev = [], cwd, [], False, [], ''
    for tok in tokens + [';']:
        if skip:
            skip = False
            continue
        if tok and set(tok) <= set('();<>|&\n'):
            if '<' in tok or '>' in tok:  # redirect: drop its fd number and its target, the command goes on
                if seg and seg[-1].isdigit():
                    seg.pop()
                skip = True
                continue
            moved = run_segment(seg, here, targets)
            op = tok.replace('(', '').replace(')', '')
            if moved != here:
                if prev.startswith(('&&', '||')) or op.startswith('||'):
                    here = None  # the cd may or may not have run
                elif not (op.startswith('|') or op.startswith('&') and not op.startswith('&&')):
                    here = moved  # a cd in a pipeline or in the background does not change this shell
            prev = op
            for ch in tok:
                if ch == '(':
                    outer.append(here)
                elif ch == ')' and outer:
                    here = outer.pop()  # leaving a subshell undoes its cd
            seg = []
        else:
            seg.append(tok)
    return targets


def run_segment(words, here, targets):
    """Follow `cd`, collect sed/perl in-place targets. Returns the directory later segments run in (None = unknown)."""
    while words and (re.match(r'^[A-Za-z_]\w*=', words[0]) or words[0] in ('command', 'env', 'nohup', 'time')):
        words = words[1:]
    if not words:
        return here
    prog, args = os.path.basename(words[0]), words[1:]
    if prog in ('cd', 'pushd'):
        dest = args[0] if args else '~'
        if dest == '-' or '$' in dest or '`' in dest or here is None and not dest.startswith(('/', '~')):
            return None
        return os.path.normpath(os.path.join(here or '/', os.path.expanduser(dest)))
    files = sed_files(args, bsd=prog == 'sed') if prog in ('sed', 'gsed') else perl_files(args) if prog == 'perl' else []
    for f in files:
        if '$' in f or '`' in f:
            continue
        f = os.path.expanduser(f)
        if not os.path.isabs(f):
            if here is None:
                continue
            f = os.path.join(here, f)
        targets += [p for p in (glob.glob(f) if re.search(r'[*?[]', f) else [f]) if os.path.isfile(p)]
    return here


def claim(target, sid, commit=True):
    """Claim target for sid (commit=False only checks). Returns (rel, same-worktree conflicts, other-worktree claims,
    other sessions' notes, live, whether the claim is new)."""
    target = os.path.realpath(target)
    repo = repo_of(target)
    if not repo:
        return None
    top, store = repo
    rel = os.path.relpath(target, top)
    same, other = [], []
    with Store(store) as s:
        live = live_sessions()  # read inside the lock so a slow hook can't prune a newer session's claim
        s.prune(live)
        first_touch = not any(c['session'] == sid for c in s.data['claims'] + s.data['notes'])
        notes = [n for n in s.data['notes'] if n['session'] != sid] if first_touch else []
        for c in s.data['claims']:
            if c['file'] == rel and c['session'] != sid:
                (same if c['worktree'] == top else other).append(c)
        mine = [c for c in s.data['claims'] if c['file'] == rel and c['worktree'] == top and c['session'] == sid]
        added = commit and not same and not mine
        if commit and not same:
            now = time.time()
            s.data['claims'] = [c for c in s.data['claims'] if c not in mine]
            s.data['claims'].append({'file': rel, 'worktree': top, 'session': sid, 'at': now,
                                     'branch': git(top, 'branch', '--show-current') or ''})
            for n in s.data['notes']:
                if n['session'] == sid:
                    n['at'] = now  # an active session keeps its note alive
    if commit:
        remember_store(sid, store)
    return rel, same, other, notes, live, added


def unclaim(target, sid):
    target = os.path.realpath(target)
    repo = repo_of(target)
    if repo:
        rel = os.path.relpath(target, repo[0])
        with Store(repo[1]) as s:
            s.data['claims'] = [c for c in s.data['claims'] if not (isinstance(c, dict) and c.get('session') == sid
                                and c.get('file') == rel and c.get('worktree') == repo[0])]


def sibling(module):
    """queues.py or guardrails.py from this folder, or None. Imported late so a broken one never breaks claims."""
    try:
        return __import__(module)
    except Exception:
        return None


def pretool():
    data = json.load(sys.stdin)
    if not isinstance(data, dict):
        return 0
    ti = data.get('tool_input') if isinstance(data.get('tool_input'), dict) else {}
    sid = data.get('session_id')
    if not isinstance(sid, str) or not sid:
        return 0  # subagents carry the parent's session_id, so they claim and check as the parent
    g = sibling('guardrails')
    if data.get('tool_name') == 'Bash':
        cwd = data.get('cwd') if isinstance(data.get('cwd'), str) else os.getcwd()
        command = ti.get('command') if isinstance(ti.get('command'), str) else ''
        check = lambda: g.evaluate(command, cwd, sid)
        targets = lambda: bash_targets(command, cwd)
        if re.search(r'serve|vite|next dev|emulators|expo|react-native|nodemon|webpack|rails|puma|run dev|start', command):
            try:  # tell the stacks reaper this live session owns the dev server it is starting
                st = sibling('stacks')
                if st:
                    st.note_launch(sid, command, cwd)
            except Exception:
                pass
    else:
        target = ti.get('file_path') or ti.get('notebook_path')
        check = lambda: g.evaluate_path(target, sid) if isinstance(target, str) else None
        targets = lambda: [target] if isinstance(target, str) else []
    try:
        verdict = check() if g else None
    except Exception:
        verdict = None  # a broken guardrail lets the call through
    if verdict and verdict[0] == 'deny':
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                                 'permissionDecisionReason': verdict[1]}}))
        return 0
    rail_ctx, granted = (verdict[1], verdict[2]) if verdict else (None, [])
    targets = targets()
    if not targets:
        if rail_ctx:
            print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'additionalContext': rail_ctx}}))
        return 0
    targets = list(dict.fromkeys(targets))
    live, denied, warned, noted = {}, [], [], {}
    for t in targets if len(targets) > 1 else []:  # check them all first: a denied call must claim nothing
        r = claim(t, sid, commit=False)
        if r and r[1]:
            live.update(r[4])
            denied.append((r[0], r[1][0]))
            break
    added = []
    for t in targets if not denied else []:
        r = claim(t, sid)
        if not r:
            continue
        rel, same, other, notes, now_live, new = r
        live.update(now_live)
        if new:
            added.append(t)
        if same:
            denied.append((rel, same[0]))
            for a in added:  # another session claimed a file after the check: undo this call's new claims
                unclaim(a, sid)
            break
        if other:
            warned.append((rel, other))
        for n in notes:
            noted[(n['session'], n['worktree'])] = n
    ctx = [f'Heads-up: {rel} is also being edited in another worktree by '
           + ', '.join(f'{live.get(c["session"], "?")} on {c["branch"] or c["worktree"]}' for c in other)
           + '. Expect a merge conflict; keep the change small and mention it in the end recap.' for rel, other in warned]
    if noted:
        ctx.append('Other live sessions in this repo say they are working on: '
                   + '; '.join(f'{live.get(n["session"], "?")} ({n["branch"] or n["worktree"]}, '
                               f'{stamp(n.get("noted_at", n["at"]))}): {n["text"]}' for n in noted.values())
                   + '. Stay out of their way or mention the overlap in the end recap.')
    if denied and granted:
        q = sibling('queues')
        try:
            q.release(granted, sid)  # the call will not run, so give back the turns it just took
        except Exception:
            pass
    if denied:
        rel, c = denied[0]
        reason = (f'{rel} is being edited by another live session ({live.get(c["session"], c["session"])}) in this same '
                  f'worktree. Do not overwrite it. Work on something else, or tell the user in the end recap. '
                  f'See: python3 {os.path.abspath(__file__)} list')
        out = {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny', 'permissionDecisionReason': reason}
    elif ctx or rail_ctx:
        out = {'hookEventName': 'PreToolUse', 'additionalContext': ' '.join(ctx + ([rail_ctx] if rail_ctx else []))}
    else:
        return 0
    print(json.dumps({'hookSpecificOutput': out}))
    return 0


def stamp(t):
    return time.strftime('%Y-%m-%d %H:%M:%S %z', time.localtime(t))


def history_path(store):
    return os.path.join(os.path.dirname(store), 'claude-claims-history.jsonl')


def log_entry(s, sid, top, event, text, name, force=False):
    """Append what a session did in worktree top since its last note to the repo history. Call inside the Store lock."""
    cursor = [c for c in s.data['logged'] if c.get('session') == sid and c.get('worktree') == top]
    since = max((c.get('at', 0) for c in cursor), default=0)  # survives note --clear, so files are never logged twice
    files = sorted({c['file'] for c in s.data['claims']
                    if c.get('session') == sid and c.get('worktree') == top and c.get('at', 0) > since})
    if not (text or files or force):
        return
    now = time.time()
    s.data['logged'] = [c for c in s.data['logged'] if c not in cursor] + [{'session': sid, 'worktree': top, 'at': now}]
    rec = {'time': stamp(now), 'at': now, 'event': event, 'session': sid, 'name': name, 'worktree': top,
           'branch': git(top, 'branch', '--show-current') or '', 'text': text, 'files': files}
    with open(history_path(s.path), 'a') as f:
        f.write(json.dumps(rec) + '\n')


def read_history(store, tail_bytes=None):
    offset = 0
    try:
        with open(history_path(store), 'rb') as f:
            if tail_bytes:
                offset = max(0, os.fstat(f.fileno()).st_size - tail_bytes)
                f.seek(offset)
            lines = f.read().decode('utf-8', 'replace').splitlines()
    except OSError:
        return []
    out = []
    for line in lines[1:] if offset else lines:  # a read that starts mid-file starts mid-line
        try:
            rec = json.loads(line)
            if isinstance(rec, dict):
                out.append(rec)
        except ValueError:
            continue
    return out


def fmt_history(h, width=None):
    text = h.get('text') or f'({h.get("event")})'
    if width and len(text) > width:
        text = text[:width - 1] + '…'
    files = h.get('files') or []
    shown = ', '.join(files[:8]) + (f' +{len(files) - 8}' if len(files) > 8 else '')
    return f'{h.get("time", "?")} {h.get("name", "?")} ({h.get("branch") or "-"}): {text}' + (f' [files: {shown}]' if files else '')


def session_stores(sid):
    try:
        return set(open(os.path.join(INDEX_DIR, f'{os.path.basename(sid)}.stores')).read().splitlines())
    except OSError:
        return set()


def turns_path(sid):
    return os.path.join(INDEX_DIR, f'{os.path.basename(sid)}.turns.json')


class Turns(Store):
    """Per-session turn counters, same lock-then-atomic-replace as the claims store."""
    KEYS = ()

    def __init__(self, sid):
        os.makedirs(INDEX_DIR, exist_ok=True)
        super().__init__(turns_path(sid))

    def __enter__(self):
        super().__enter__()
        if not isinstance(self.data.get('stores'), dict):
            self.data['stores'] = {}
        if not isinstance(self.data.get('last_prompt'), (int, float)):
            self.data['last_prompt'] = 0
        if not isinstance(self.data.get('noted'), dict):
            self.data['noted'] = {}
        return self


def start_stores(cwd, max_repos=60, max_entries=2000):
    """Stores to brief a new session on: the cwd's repo, or when cwd is a folder of repos, every repo one level down."""
    repo = repo_of(cwd)
    if repo:
        return {repo[1]: os.path.basename(repo[0])}
    stores, found = {}, []
    try:
        with os.scandir(cwd) as it:
            for n, e in enumerate(it):
                if n >= max_entries or len(found) >= max_repos:
                    break
                if not e.name.startswith('.') and e.is_dir() and os.path.exists(os.path.join(e.path, '.git')):
                    found.append(e.path)  # a repo or a worktree; worktrees of one repo share a store
    except OSError:
        return stores
    for d in sorted(found):
        r = repo_of(d)
        if r and r[1] not in stores:
            stores[r[1]] = os.path.basename(os.path.dirname(os.path.dirname(r[1]))) or os.path.basename(r[0])
    return stores


def session_start():
    data = json.load(sys.stdin)
    if not isinstance(data, dict):
        return 0
    sid = data.get('session_id')
    cwd = data.get('cwd') if isinstance(data.get('cwd'), str) else os.getcwd()
    g = sibling('guardrails')
    try:
        rails = g.session_start_summary(cwd) if g else None
    except Exception:
        rails = None
    stores = {k: v for k, v in start_stores(os.path.realpath(cwd)).items()
              if os.path.exists(k) or os.path.exists(history_path(k))}
    if not stores and not rails:
        return 0
    many = len(stores) > 1
    notes, recent, live, cutoff = [], [], live_sessions(), time.time() - EXPIRE_H * 3600
    for store, label in stores.items():
        try:  # read only: no lock, no prune, so a slow start never holds up or rewrites anyone's claims
            data = json.load(open(store))
            notes += [(label, n) for n in data.get('notes', []) if isinstance(n, dict) and n.get('session') != sid
                      and n.get('session') in live and isinstance(n.get('at'), (int, float)) and n['at'] > cutoff
                      and isinstance(n.get('text'), str)]
        except (OSError, ValueError, AttributeError):
            pass
        recent += [(label, h) for h in read_history(store, tail_bytes=65536)[-5:]]
    recent = sorted(recent, key=lambda x: x[1].get('at', 0) if isinstance(x[1].get('at'), (int, float)) else 0)[-5:]
    if not notes and not recent and not rails:
        return 0
    where = 'the repos under this folder' if many else 'this repo'
    tag = lambda label: f'[{label}] ' if many else ''
    msg = [rails] if rails else []
    if notes:
        msg.append(f'Other live sessions in {where} are working on: ' + '; '.join(
            f'{tag(label)}{live.get(n["session"], "?")} ({n.get("branch") or n.get("worktree", "?")}, '
            f'{stamp(n.get("noted_at", n["at"]))}): {n["text"][:200]}' for label, n in notes[:12]) + '.')
    if recent:
        msg.append(f'Recent work logged in {where}, newest last: '
                   + ' | '.join(tag(label) + fmt_history(h, 160) for label, h in recent) + '.')
    if notes or recent:
        msg.append(f'Before starting a piece of work, check it was not already done or tried: '
                   f'python3 {os.path.abspath(__file__)} history --grep WORD <repo dir>. Once you edit, keep a status '
                   f'note with claims.py note "doing now; done/tried: ..." run inside that repo.')
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': ' '.join(msg)}}))
    return 0


def failure():
    """PostToolUseFailure hook for Bash: invite a guardrail when a risky command failed."""
    data = json.load(sys.stdin)
    if not isinstance(data, dict) or data.get('tool_name') != 'Bash':
        return 0
    ti = data.get('tool_input') if isinstance(data.get('tool_input'), dict) else {}
    cwd = data.get('cwd') if isinstance(data.get('cwd'), str) else os.getcwd()
    g = sibling('guardrails')
    try:
        text = g.failure_nudge(ti.get('command') if isinstance(ti.get('command'), str) else '', cwd) if g else None
    except Exception:
        text = None
    if text:
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PostToolUseFailure', 'additionalContext': text}}))
    return 0


def prompt():
    data = json.load(sys.stdin)
    sid = data.get('session_id') if isinstance(data, dict) else None
    if not isinstance(sid, str) or not sid:
        return 0
    due = []
    with Turns(sid) as t:
        last, counts, noted = t.data['last_prompt'], t.data['stores'], t.data['noted']
        for store in sorted(session_stores(sid)):
            try:
                d = json.load(open(store))
                mine = [c for c in d.get('claims', []) if isinstance(c, dict) and c.get('session') == sid]
            except (OSError, ValueError, AttributeError):
                continue
            since = max(last, noted.get(store, 0) if isinstance(noted.get(store), (int, float)) else 0)
            if any(isinstance(c.get('at'), (int, float)) and c['at'] > since for c in mine):  # the last turn edited here
                counts[store] = int(counts.get(store, 0) or 0) + 1
            if mine and counts.get(store, 0) >= NOTE_EVERY:
                due += sorted({c['worktree'] for c in mine if isinstance(c.get('worktree'), str)})
        t.data['last_prompt'] = time.time()
    msgs = []
    q = sibling('queues')
    try:
        nudge = q.holder_nudge(sid) if q else None
    except Exception:
        nudge = None
    if nudge:
        msgs.append(nudge)
    if due:
        me = os.path.abspath(__file__)
        cmds = ' ; '.join(f'cd {shlex.quote(w)} && python3 {me} note "<doing now>; done/tried: <results since the last note>"'
                          for w in dict.fromkeys(due))
        msgs.append(f'Status note due: this session has edited for {NOTE_EVERY}+ turns without updating its note. During '
                    f'this turn, run: {cmds} One short line each, name what worked and what did not. Do not mention it '
                    f'to the user.')
    if msgs:
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': ' '.join(msgs)}}))
    return 0


def session_end():
    data = json.load(sys.stdin)
    sid = data.get('session_id') if isinstance(data, dict) else None
    if not isinstance(sid, str) or not sid:
        return 0
    q = sibling('queues')
    try:
        if q:
            q.drop_session(sid)
    except Exception:
        pass
    name = live_sessions().get(sid, '?')
    for store in session_stores(sid):
        if os.path.exists(store):
            with Store(store) as s:
                for top in {c.get('worktree') for c in s.data['claims'] + s.data['notes']
                            if isinstance(c, dict) and c.get('session') == sid}:
                    log_entry(s, sid, top, 'ended', '', name)
                s.drop(sid)
    for path in (os.path.join(INDEX_DIR, f'{os.path.basename(sid)}.stores'), turns_path(sid), turns_path(sid) + '.lock'):
        try:
            os.remove(path)
        except OSError:
            pass
    for old in glob.glob(os.path.join(INDEX_DIR, '*.stores')) + glob.glob(os.path.join(INDEX_DIR, '*.turns.json')):
        try:  # sessions that crashed never reach SessionEnd; a lock goes only with its stale state file
            if time.time() - os.path.getmtime(old) > 7 * 86400:
                os.remove(old)
                if old.endswith('.turns.json') and os.path.exists(old + '.lock'):
                    os.remove(old + '.lock')
        except OSError:
            pass
    return 0


def show(cwd):
    repo = repo_of(os.path.realpath(cwd))
    if not repo:
        print('not a git repo')
        return 1
    with Store(repo[1]) as s:
        live = live_sessions()
        s.prune(live)
        notes = sorted(s.data['notes'], key=lambda n: n['worktree'])
        claims = sorted(s.data['claims'], key=lambda c: (c['worktree'], c['file']))
    if not claims and not notes:
        print('no live claims')
    for n in notes:
        print(f'{live.get(n["session"], "?"):20} {n["branch"] or "-":30} note ({stamp(n.get("noted_at", n["at"]))}): '
              f'{n["text"]}  ({n["worktree"]})')
    for c in claims:
        age = int((time.time() - c['at']) / 60)
        print(f'{live.get(c["session"], "?"):20} {c["branch"] or "-":30} {c["file"]}  ({age}m ago, {c["worktree"]})')
    return 0


def note(words):
    sid = current_session()
    repo = repo_of(os.path.realpath(os.getcwd()))
    if not sid or not repo or not words or not ' '.join(words).strip():
        print('usage: run inside a Claude session, in a git repo: claims.py note "doing now; done/tried: ..." | note --clear')
        return 2
    top, store = repo
    clear = words == ['--clear']
    with Store(store) as s:
        now = time.time()  # taken under the lock: any later edit is stamped after it and counts toward the next note
        live = live_sessions()
        s.prune(live)
        text = '' if clear else ' '.join(words)
        log_entry(s, sid, top, 'cleared' if clear else 'note', text, live.get(sid, '?'), force=True)
        s.data['notes'] = [n for n in s.data['notes'] if not (n['session'] == sid and n['worktree'] == top)]
        if not clear:
            s.data['notes'].append({'text': text, 'worktree': top, 'session': sid, 'at': now, 'noted_at': now,
                                    'branch': git(top, 'branch', '--show-current') or ''})
    remember_store(sid, store)
    with Turns(sid) as t:
        t.data['stores'][store] = 0
        t.data['noted'][store] = now  # edits in this repo made before the note are covered by it
    print('note cleared' if clear else f'note set for {top} at {stamp(now)}')
    return 0


def release(cwd):
    sid = current_session()
    repo = repo_of(os.path.realpath(cwd))
    if not sid or not repo:
        print('usage: run inside a Claude session: claims.py release [DIR in a git repo]')
        return 2
    with Store(repo[1]) as s:
        log_entry(s, sid, repo[0], 'released', '', live_sessions().get(sid, '?'))
        s.drop(sid, repo[0])
    print(f'released this session\'s claims and note in {repo[0]}')
    return 0


def closeout():
    """What this session still holds, so it can say whether it is safe to close. Exit 0 when nothing is held."""
    sid = current_session()
    if not sid:
        print('run this from a Bash call inside a Claude session')
        return 2
    live, items, me = live_sessions(), [], os.path.abspath(__file__)
    for store in sorted(session_stores(sid)):
        if not os.path.exists(store):
            continue  # the repo is gone, so nothing is held there
        try:
            data = json.load(open(store))
            if not isinstance(data, dict):
                raise ValueError('not an object')
            mine = [c for c in data.get('claims') or [] if isinstance(c, dict) and c.get('session') == sid]
            notes = [n for n in data.get('notes') or [] if isinstance(n, dict) and n.get('session') == sid]
        except (OSError, ValueError, TypeError, AttributeError) as e:
            items.append(f'could not read {store} ({e}); check it by hand with python3 {me} list <that repo>')
            continue
        for top in sorted({c.get('worktree') for c in mine + notes if isinstance(c.get('worktree'), str)}):
            files = sorted({c.get('file') for c in mine if c.get('worktree') == top})
            note = next((n.get('text') for n in notes if n.get('worktree') == top), None)
            items.append(f'{top}: {len(files)} claimed files' + (f' ({", ".join(files[:5])}{" ..." if len(files) > 5 else ""})' if files else '')
                         + (f'; note: {note[:120]}' if note else '; no note')
                         + f'. When the handover is written: cd {shlex.quote(top)} && python3 {me} note "handed over: <handover path>" '
                           f'&& python3 {me} release')
    q = sibling('queues')
    try:
        queues = json.load(open(q.QUEUE)).get('queues', {}) if q and os.path.exists(q.QUEUE) else {}
        if not isinstance(queues, dict):
            raise ValueError('queues is not an object')
    except (OSError, ValueError, AttributeError) as e:
        queues = {}
        items.append(f'could not read the queue file ({e}); check with python3 {q.ME if q else "queues.py"} list')
    for name, line in sorted(queues.items()):
        pos = next((i for i, e in enumerate(line if isinstance(line, list) else []) if isinstance(e, dict)
                    and e.get('session') == sid), -1) + 1
        if pos == 1:
            items.append(f'holds the {name} queue turn. Finish and check the action, then: python3 {q.ME} done {name} "<result>"')
        elif pos:
            items.append(f'number {pos} in line for {name}. If you will not do it: python3 {q.ME} leave {name}; '
                         f'if you will, put it in the handover and leave the line')
    if not items:
        print('clear: this session holds no claims, notes or queue places')
        return 0
    print('still held by this session:\n- ' + '\n- '.join(items))
    return 1


def history(args):
    n, pattern, cwd, i = 20, None, os.getcwd(), 0
    while i < len(args):
        if args[i] == '-n' and i + 1 < len(args) and args[i + 1].isdigit():
            n, i = int(args[i + 1]), i + 1
        elif args[i] == '--grep' and i + 1 < len(args):
            pattern, i = args[i + 1].lower(), i + 1
        else:
            cwd = args[i]
        i += 1
    repo = repo_of(os.path.realpath(cwd))
    if not repo:
        print('not a git repo')
        return 1
    recs = read_history(repo[1])
    if pattern:
        recs = [h for h in recs if pattern in json.dumps([h.get('text'), h.get('files'), h.get('branch'), h.get('name')]).lower()]
    if not recs:
        print('no history' + (f' matching {pattern!r}' if pattern else ''))
    for h in recs[-n:]:
        print(fmt_history(h))
    return 0


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'list'
    arg = sys.argv[2] if len(sys.argv) > 2 else os.getcwd()
    hooks = {'pretool': pretool, 'session-start': session_start, 'prompt': prompt, 'failure': failure,
             'session-end': session_end}
    try:
        if cmd in hooks:
            sys.exit(hooks[cmd]())
        if cmd == 'list':
            sys.exit(show(arg))
        if cmd == 'note':
            sys.exit(note(sys.argv[2:]))
        if cmd == 'release':
            sys.exit(release(arg))
        if cmd == 'history':
            sys.exit(history(sys.argv[2:]))
        if cmd == 'closeout':
            try:
                sys.exit(closeout())
            except (SystemExit, KeyboardInterrupt):
                raise
            except Exception as e:  # unlike the hooks, a closeout that cannot check must not look clear
                print(f'could not check what this session holds: {type(e).__name__}: {e}')
                sys.exit(2)
        print(__doc__)
    except Exception as e:  # never block an edit because the claims check failed
        print(f'claims: {type(e).__name__}: {e}', file=sys.stderr)
        sys.exit(0)
