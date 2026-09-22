#!/usr/bin/env python3
"""Guardrails that sessions learn from failures and every later session gets, in both accounts.

Rules live in guardrails.json next to the hooks folder, so they are durable, reviewable and shared. The claims
PreToolUse hook checks every Bash command (and, for --path rules, every Edit/Write) against them before it runs.
A command rule matches where a command starts (after && || ; | & ( and newlines, outside quotes, with or without
leading VAR=value), only when that command runs under the rule's folder. Heredoc bodies and comments never match.

  guardrails.py learn (--match REGEX | --path REGEX) --why "what breaks; what to do instead" [--dir FOLDER]
                      [--action warn|stop|queue|deny] [--queue NAME] [--user-approved]
        warn  (default) it runs, and the session is told why first
        stop  it is blocked before it runs: the session is told what could break and the recommended fix, checks
              whether the edge case applies, then either fixes it, tells the user, or runs `ack` and retries
        queue it waits its turn in queue NAME (see queues.py), for merges and deploys to shared targets
        deny  blocked outright; only with --user-approved, when the user asked for a hard block
  guardrails.py ack ID "what I checked"      after a stop: confirm the edge case does not apply (30 min, this session)
  guardrails.py test "COMMAND" [--cwd DIR]   which rules a command would hit, before you learn a new one
  guardrails.py list [--dir FOLDER] [--all]  active rules (with --all, retired ones too)
  guardrails.py retire ID "reason"           turn a rule off, keeping it on record

Refused as unsafe: patterns that match everyday commands (ls, git status, cd, cat, echo, grep, tests) and patterns
with nested repeats like (a+)+ or (a|aa)+ that can hang the hook; matching also stops after 2 seconds. Any failure
here lets the command through. Known limits: a command in a branch that never runs (false && git push) still matches,
and a command needing two queues can in rare orders wait on a session that waits on it, until the stale check fires.
"""
import json, os, re, shlex, signal, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claims  # noqa: E402  locking, sessions and time stamps are shared with the claims hook

RULES = os.environ.get('CLAIMS_RULES') or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                       'guardrails.json')
ACKS = os.path.join(claims.INDEX_DIR, 'guardrail-acks.json')
ACK_MIN = 30
ME = os.path.abspath(__file__)
ACTIONS = ('warn', 'stop', 'queue', 'deny')
EVERYDAY = ['ls', 'ls -la', 'pwd', 'git status', 'git diff', 'git log --oneline -5', 'cd src', 'cat README.md',
            'echo hi', 'grep -rn foo .', 'python3 -m pytest', 'npm test', 'make test', 'git add file.py']
RISKY = re.compile(r'(?:git\s+(?:push|merge|rebase|reset|checkout\s+--|clean)|gh\s+(?:pr\s+merge|release)|\S*deploy|'
                   r'make\s+\S*(?:deploy|release|migrat)|gcloud|firebase|terraform|kubectl|helm|docker\s+push|'
                   r'npm\s+publish|\S*migrat|alembic|psql|flyway|rm\s+-rf)', re.I)
ASSIGN = re.compile(r'(?:[A-Za-z_]\w*=\S*|env|command|nohup|time|sudo|exec)\s+')
NESTED_REPEAT = re.compile(r'\((?:[^()\\]|\\.)*[+*}|](?:[^()\\]|\\.)*\)\s*[+*{]')  # (a+)+ and (a|aa)+
MATCH_SECONDS = 2
MAX_PATTERN, MAX_SEGMENT = 200, 400


class Rules(claims.Store):
    KEYS = ('rules',)

    def __init__(self):
        super().__init__(RULES)


class Acks(claims.Store):
    KEYS = ()

    def __init__(self):
        os.makedirs(claims.INDEX_DIR, exist_ok=True)
        super().__init__(ACKS)


def strip_assign(text):
    while ASSIGN.match(text):
        text = text[ASSIGN.match(text).end():]
    return text


def command_starts(command, cwd=None):
    """[(segment, cwd it runs in)] for every place a command starts. Quote-aware, skips comments and heredoc bodies.
    The cwd follows plain `cd DIR`; it becomes None when it cannot be known. Unbalanced quotes return [] (fail open)."""
    segs, cur, quote, heredocs = [], [], None, []
    here, stack, state = cwd, [], {'start': ''}
    n, i = len(command), 0

    def flush(end):
        nonlocal here
        text = ''.join(cur).strip()
        cur.clear()
        began, state['start'] = state['start'], end
        if not text:
            return
        segs.append((text, here))
        bare = strip_assign(text)
        if not re.match(r'(?:cd|pushd)(?:\s|$)', bare) or end in ('|', '&'):
            return  # not a cd, or a cd in a pipeline or in the background, which does not move this shell
        try:
            words = shlex.split(bare)
        except ValueError:
            words = []
        dest = words[1] if len(words) == 2 else ('~' if len(words) == 1 else None)
        if dest is None or began in ('&&', '||') or end == '||' or dest == '-' or '$' in dest or '`' in dest:
            here = None  # it may not run, or goes somewhere we cannot resolve
        elif here is not None or dest.startswith(('/', '~')):
            here = os.path.normpath(os.path.join(here or '/', os.path.expanduser(dest)))

    while i < n:
        c = command[i]
        if quote:
            if c == '\\' and quote == '"' and i + 1 < n:
                cur.append(command[i:i + 2])
                i += 2
                continue
            if c == quote:
                quote = None
            cur.append(c)
            i += 1
            continue
        if c in '\'"':
            quote = c
        elif c == '\\' and i + 1 < n:
            cur.append(command[i:i + 2])
            i += 2
            continue
        elif c == '#' and (not cur or cur[-1][-1:].isspace()):
            j = command.find('\n', i)
            i = n if j < 0 else j
            continue
        elif command.startswith('<<', i) and not command.startswith('<<<', i):
            m = re.match(r'<<(-?)\s*([\'"]?)([A-Za-z_][\w.-]*)\2', command[i:])
            if m:
                heredocs.append((m.group(3), bool(m.group(1))))
                cur.append(m.group(0))
                i += m.end()
                continue
        elif c == '\n':
            flush('\n')
            for tag, dash in heredocs:  # bodies follow on the next lines, one after another
                while i < n:
                    j = command.find('\n', i + 1)
                    line, i = command[i + 1:j if j >= 0 else n], (j if j >= 0 else n)
                    if (line.lstrip('\t') if dash else line).rstrip('\r') == tag:  # <<- allows leading tabs only
                        break
            heredocs = []
            i += 1
            continue
        else:
            sep = next((s for s in ('&&', '||', '$(', ';', '|', '`', '(', ')') if command.startswith(s, i)), None)
            if sep is None and c == '&' and not (cur and cur[-1][-1:] in '<>') and not command.startswith('&>', i):
                sep = '&'
            if sep:
                flush(sep)
                if sep in ('(', '$('):
                    stack.append(here)
                elif sep == ')' and stack:
                    here = stack.pop()  # leaving a subshell undoes its cd
                i += len(sep)
                continue
        cur.append(c)
        i += 1
    if quote:
        return []
    flush(';')
    return segs


def unsafe_pattern(pattern):
    if len(pattern) > MAX_PATTERN:
        return f'longer than {MAX_PATTERN} characters'
    if NESTED_REPEAT.search(pattern):
        return 'nested repeats like (a+)+ can hang the hook'
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f'bad regex: {e}'
    if rx.match(''):
        return 'matches an empty command'
    return None


def load():
    try:
        rules = json.load(open(RULES)).get('rules', [])
    except (OSError, ValueError, AttributeError):
        return []
    return [r for r in rules if isinstance(r, dict) and not r.get('retired') and r.get('action', 'warn') in ACTIONS
            and any(isinstance(r.get(k), str) and not unsafe_pattern(r[k]) for k in ('match', 'path'))]


def under(folder, path):
    if not folder:
        return True
    if not path:
        return False
    return path == folder or path.startswith(folder.rstrip('/') + '/')


def matching(command, cwd, rules=None):
    rules = [r for r in (load() if rules is None else rules) if isinstance(r.get('match'), str)]
    if not rules:
        return []
    segs = command_starts(command, os.path.realpath(cwd) if cwd else None)
    hits = []
    for r in rules:
        for text, here in segs:
            if r.get('dir') and not under(r['dir'], here):
                continue
            if any(re.match(r['match'], v[:MAX_SEGMENT]) for v in dict.fromkeys((text, strip_assign(text)))):
                hits.append(r)
                break
    return hits


def path_matching(path, rules=None):
    real = os.path.realpath(path)
    return [r for r in (load() if rules is None else rules) if isinstance(r.get('path'), str)
            and under(r.get('dir'), real) and re.search(r['path'], real[:MAX_SEGMENT])]


def record_hits(ids):
    if ids and os.path.exists(RULES):
        with Rules() as s:
            for r in s.data['rules']:
                if isinstance(r, dict) and r.get('id') in ids:
                    r['hits'] = int(r.get('hits', 0) or 0) + 1
                    r['last_hit'] = claims.stamp(time.time())


def acked(sid):
    try:
        mine = json.load(open(ACKS)).get(sid, {})
        return {k for k, v in mine.items() if isinstance(v, dict) and time.time() - v.get('at', 0) < ACK_MIN * 60}
    except (OSError, ValueError, AttributeError):
        return set()


def label(r):
    return f'{r.get("id")} (learned {r.get("learned", "?")} by {r.get("by", "?")})'


def decide(hits, command, sid, target):
    """Turn matched rules into (kind, text, granted queues) for the hook, or None."""
    if not hits:
        return None
    try:
        record_hits({r.get('id') for r in hits})
    except Exception:
        pass  # counting hits is bookkeeping, never a reason to block
    denies = [r for r in hits if r.get('action') == 'deny']
    if denies:
        return ('deny', ' '.join(f'Guardrail {label(r)}: {r.get("why")}' for r in denies)
                + ' The user asked for this block. Do not work around it; tell the user if you need to do this.', [])
    done = acked(sid)
    stops = [r for r in hits if r.get('action') == 'stop' and r.get('id') not in done]
    if stops:
        return ('deny', f'STOP before {target}. Known edge case: ' + ' | '.join(f'{label(r)}: {r.get("why")}' for r in stops)
                + '. Check now whether it applies here. If it does, follow the recommendation, or tell the user in one '
                'line what could break and what you propose. If you checked and it is safe, run: '
                + ' ; '.join(f'python3 {ME} ack {r.get("id")} "<what you checked>"' for r in stops)
                + ' and retry. Do not work around this check.', [])
    ctx, granted = [], []
    queued = [r.get('queue') for r in hits if r.get('action') == 'queue' and isinstance(r.get('queue'), str)]
    if queued:
        import queues
        verdict = queues.check(sorted(set(queued)), command, sid)
        if verdict and verdict[0] == 'deny':
            return verdict[0], verdict[1], []
        if verdict:
            ctx.append(verdict[1])
            granted = verdict[2]
    notes = [r for r in hits if r.get('action', 'warn') == 'warn' or (r.get('action') == 'stop' and r.get('id') in done)]
    if notes:
        ctx.append('Guardrails learned from earlier failures apply: ' + ' | '.join(f'{label(r)}: {r.get("why")}' for r in notes)
                   + f'. If one is wrong or out of date, retire it: python3 {ME} retire ID "reason".')
    return ('context', ' '.join(ctx), granted) if ctx else None


class TooSlow(Exception):
    pass


def bounded(fn, *args):
    """Run a match with a hard time limit, so a pathological rule can never hang every Bash call."""
    def stop(*_):
        raise TooSlow()
    old = signal.signal(signal.SIGALRM, stop)
    signal.setitimer(signal.ITIMER_REAL, MATCH_SECONDS)
    try:
        return fn(*args)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def evaluate(command, cwd, sid):
    return decide(bounded(matching, command, cwd), command, sid, 'running this command')


def evaluate_path(path, sid):
    return decide(bounded(path_matching, path), f'edit {path}', sid, f'editing {os.path.basename(path)}')


def failure_nudge(command, cwd):
    """For PostToolUseFailure: invite a lesson when a risky command failed and no rule covers it yet."""
    if not command or bounded(matching, command, cwd):
        return None
    if not any(RISKY.match(strip_assign(text)) for text, _ in command_starts(command)):
        return None
    return (f'That command failed. If the cause is an edge case another session could hit again (not a typo, not a '
            f'normal red test), record it once so later sessions are stopped before it: python3 {ME} test "<command>" '
            f'to see what already matches, then python3 {ME} learn --action stop --match \'^<start of the command>\' '
            f'--dir <project folder> --why "could break: <what>; instead: <fix>". Use --action warn for a softer '
            f'note, or --action queue --queue <project>:<action> if it failed because another session was doing the '
            f'same thing. Skip this if it was a one-off.')


def session_start_summary(cwd, limit=6):
    here = os.path.realpath(cwd)
    rules = [r for r in load() if under(r.get('dir'), here) or (r.get('dir') and os.path.dirname(r['dir']) == here)]
    if not rules:
        return None
    order = {'deny': 0, 'stop': 1, 'queue': 2, 'warn': 3}
    rules.sort(key=lambda r: order.get(r.get('action', 'warn'), 3))
    shown = ' | '.join(f'{r.get("id")} [{r.get("action", "warn")}] {r.get("why", "")[:140]}' for r in rules[:limit])
    more = f' ({len(rules) - limit} more: python3 {ME} list --dir .)' if len(rules) > limit else ''
    return (f'{len(rules)} guardrails learned from earlier failures apply here: {shown}{more}. Before you execute a '
            f'plan, say which of these it could hit, what could break, and how you will avoid it.')


def opts(args, flags=('--user-approved', '--all')):
    out, rest, i = {}, [], 0
    while i < len(args):
        if args[i] in flags:
            out[args[i]] = True
        elif args[i].startswith('--') and i + 1 < len(args):
            out[args[i]], i = args[i + 1], i + 1
        else:
            rest.append(args[i])
        i += 1
    return out, rest


def learn(args):
    o, _ = opts(args)
    match, path, why, action = o.get('--match'), o.get('--path'), o.get('--why'), o.get('--action', 'warn')
    if not (match or path) or (match and path) or not why or action not in ACTIONS:
        print('usage: learn (--match REGEX | --path REGEX) --why TEXT [--dir FOLDER] [--action warn|stop|queue|deny] '
              '[--queue NAME] [--user-approved]')
        return 2
    problem = unsafe_pattern(match or path)
    if problem:
        print(f'refused: {problem}')
        return 2
    if match:
        broad = [c for c in EVERYDAY if re.match(match, c)]
        if broad:
            print(f'refused: the pattern also matches everyday commands ({", ".join(broad)}). Make it narrower.')
            return 2
    if action == 'queue' and (path or not re.match(r'^[A-Za-z0-9_.:/-]{1,80}$', o.get('--queue', ''))):
        print('--action queue needs --match and --queue NAME, like myapp:deploy-staging')
        return 2
    if action == 'deny' and not o.get('--user-approved'):
        print('refused: deny blocks every session outright. Use stop (blocked until checked), warn or queue, '
              'or ask the user and add --user-approved.')
        return 2
    folder = os.path.realpath(os.path.expanduser(o['--dir'])) if o.get('--dir') else None
    sid = claims.current_session()
    name = claims.live_sessions().get(sid, 'user') if sid else 'user'
    key = 'match' if match else 'path'
    with Rules() as s:
        same = [r for r in s.data['rules'] if isinstance(r, dict) and not r.get('retired')
                and r.get(key) == (match or path) and r.get('dir') == folder and r.get('action', 'warn') == action
                and (action != 'queue' or r.get('queue') == o.get('--queue'))]
        if same:
            print(f'already covered by {same[0].get("id")} [{same[0].get("action", "warn")}]: {same[0].get("why")}')
            return 0
        n = 1 + max((int(r['id'][1:]) for r in s.data['rules'] if isinstance(r, dict)
                     and re.match(r'^g\d+$', str(r.get('id', '')))), default=0)
        rule = {'id': f'g{n}', key: match or path, 'dir': folder, 'action': action, 'why': why, 'by': name,
                'learned': claims.stamp(time.time()), 'hits': 0}
        if action == 'queue':
            rule['queue'] = o['--queue']
        s.data['rules'].append(rule)
    print(f'learned g{n} [{action}] under {folder or "everywhere"}: {why}')
    return 0


def ack(args):
    sid = claims.current_session()
    if len(args) < 2 or not sid:
        print('usage, from inside a session: ack ID "what you checked"')
        return 2
    if not any(r.get('id') == args[0] for r in load()):
        print(f'no active rule {args[0]}')
        return 3
    with Acks() as s:
        now = time.time()
        for k in list(s.data):  # forget old acks
            if not isinstance(s.data[k], dict):
                del s.data[k]
                continue
            s.data[k] = {i: v for i, v in s.data[k].items() if isinstance(v, dict) and now - v.get('at', 0) < ACK_MIN * 60}
            if not s.data[k]:
                del s.data[k]
        s.data.setdefault(sid, {})[args[0]] = {'at': now, 'note': ' '.join(args[1:])}
    with Rules() as s:
        for r in s.data['rules']:
            if isinstance(r, dict) and r.get('id') == args[0]:
                r.setdefault('acks', []).append({'time': claims.stamp(now), 'by': claims.live_sessions().get(sid, '?'),
                                                 'note': ' '.join(args[1:])[:300]})
                r['acks'] = r['acks'][-20:]
    print(f'acknowledged {args[0]} for {ACK_MIN} min. Retry the command.')
    return 0


def test(args):
    o, rest = opts(args)
    if not rest:
        print('usage: test "COMMAND" [--cwd DIR]')
        return 2
    hits = matching(' '.join(rest), o.get('--cwd', os.getcwd()))
    print('\n'.join(f'{r.get("id")} [{r.get("action", "warn")}] {r.get("match")}: {r.get("why")}' for r in hits)
          or 'no guardrail matches')
    return 0


def show(args):
    o, _ = opts(args)
    try:
        rules = json.load(open(RULES)).get('rules', [])
    except (OSError, ValueError, AttributeError):
        rules = []
    cwd = os.path.realpath(o['--dir']) if '--dir' in o else None
    rules = [r for r in rules if isinstance(r, dict) and (o.get('--all') or not r.get('retired'))
             and (cwd is None or under(r.get('dir'), cwd) or str(r.get('dir') or '').startswith(cwd.rstrip('/') + '/'))]
    if not rules:
        print('no guardrails')
    for r in rules:
        state = f' RETIRED {r.get("retired")}' if r.get('retired') else ''
        q = f' -> {r.get("queue")}' if r.get('action') == 'queue' else ''
        what = f'path {r.get("path")}' if r.get('path') else r.get('match')
        print(f'{r.get("id")} [{r.get("action", "warn")}{q}]{state} {what}  under {r.get("dir") or "everywhere"}\n'
              f'    {r.get("why")}  (learned {r.get("learned")} by {r.get("by")}, {r.get("hits", 0)} hits'
              f'{", last " + r["last_hit"] if r.get("last_hit") else ""}, {len(r.get("acks", []))} acks)')
    return 0


def retire(args):
    if len(args) < 2:
        print('usage: retire ID "reason"')
        return 2
    with Rules() as s:
        for r in s.data['rules']:
            if isinstance(r, dict) and r.get('id') == args[0] and not r.get('retired'):
                r['retired'] = f'{claims.stamp(time.time())}: {" ".join(args[1:])}'
                print(f'retired {args[0]}')
                return 0
    print(f'no active rule {args[0]}')
    return 3


if __name__ == '__main__':
    cmd, rest = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ('list', [])
    commands = {'learn': learn, 'ack': ack, 'test': test, 'list': show, 'retire': retire}
    if cmd not in commands:
        print(__doc__)
        sys.exit(2)
    sys.exit(commands[cmd](rest))
