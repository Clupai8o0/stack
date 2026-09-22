#!/usr/bin/env python3
"""Turn-taking queues for actions only one Claude session may do at a time: merging to a shared branch, deploying to
a shared staging or production environment. Machine-wide, shared by both accounts (~/.local/state/claude-claims/).
Name a queue <project>:<action>, for example myapp:deploy-staging.

  queues.py join NAME "what I will do"   join the end of the line and print your position (1 = your turn now)
  queues.py wait NAME [--stale-after MIN] [--timeout MIN]
                                         block until it is your turn, then exit 0. Run it in the background so the
                                         session is woken when its turn comes. Exit 4: the holder has held the turn for
                                         MIN minutes (default 60) since you started waiting, ask them and wait again.
                                         Exit 3: you are no longer in the queue. Exit 2: timeout.
  queues.py done NAME ["result"]         finish your turn; the next session in line gets it
  queues.py leave NAME                   leave the line without taking a turn
  queues.py list [NAME]                  who holds each queue and who is waiting
  queues.py history [NAME] [-n N]        finished turns, with times

To make a command wait its turn automatically, add a queue guardrail:
  guardrails.py learn --action queue --queue NAME --match REGEX --dir FOLDER --why "..."
A session that runs a matching command out of turn is blocked and put in the line.

Sessions that end or die leave every queue. Any failure in the guard lets the command through.
"""
import json, os, re, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claims  # noqa: E402  session lookup, liveness and locking are shared with the claims hook

QUEUE = os.path.join(claims.INDEX_DIR, 'queue.json')
HISTORY = os.path.join(claims.INDEX_DIR, 'queue-history.jsonl')
NAME_RE = re.compile(r'^[A-Za-z0-9_.:/-]{1,80}$')
POLL = 5
HOLD_NUDGE_MIN = 30
ME = os.path.abspath(__file__)


class Queues(claims.Store):
    KEYS = ()

    def __init__(self):
        os.makedirs(claims.INDEX_DIR, exist_ok=True)
        super().__init__(QUEUE)

    def __enter__(self):
        super().__enter__()
        if not isinstance(self.data.get('queues'), dict):
            self.data['queues'] = {}
        return self

    def prune(self, live):
        for name in list(self.data['queues']):
            q = self.data['queues'][name]
            q = [e for e in q if isinstance(e, dict) and e.get('session') in live] if isinstance(q, list) else []
            if q:
                self.data['queues'][name] = promote(q)
            else:
                del self.data['queues'][name]


def promote(q):
    if q and not isinstance(q[0].get('turn_at'), (int, float)):
        q[0]['turn_at'] = time.time()
    return q


def hhmm(t):
    return time.strftime('%H:%M', time.localtime(t))


def mins(t):
    return int((time.time() - t) / 60)


def log(rec):
    rec = dict(rec, time=claims.stamp(time.time()), at=time.time())
    with open(HISTORY, 'a') as f:
        f.write(json.dumps(rec) + '\n')


def enqueue(s, name, sid, live, text, auto=False):
    """Put sid in line for name (no-op if already there). Returns (position, queue)."""
    q = s.data['queues'].setdefault(name, [])
    if not any(e['session'] == sid for e in q):
        q.append({'session': sid, 'name': live.get(sid, '?'), 'text': text[:300], 'joined_at': time.time(), 'auto': auto})
    promote(q)
    return next(i for i, e in enumerate(q) if e['session'] == sid) + 1, q


def turn_text(name):
    return (f'It is your turn on {name}. Do the action, check it worked, then run: '
            f'python3 {ME} done {name} "<result>"')


def wait_text(name, pos, q):
    h = q[0]
    return (f'You are number {pos} in line for {name}, behind {h["name"]} (holding since {hhmm(h["turn_at"])}: '
            f'{h["text"][:120]}). Do not run that action yet. Wait in the background so you are woken on your turn: '
            f'python3 {ME} wait {name} (Bash with run_in_background). Do other work meanwhile, or run leave {name} if '
            f'you no longer need it.')


def need_session():
    sid = claims.current_session()
    if not sid:
        print('run this from a Bash call inside a Claude session')
    return sid


def join(name, text):
    sid = need_session()
    if not sid:
        return 2
    with Queues() as s:
        live = claims.live_sessions()
        s.prune(live)
        pos, q = enqueue(s, name, sid, live, text or '(no description)')
    print(turn_text(name) if pos == 1 else wait_text(name, pos, q))
    return 0


def wait(name, stale_after=60, timeout=None):
    sid = need_session()
    if not sid:
        return 2
    started = time.time()
    while True:
        with Queues() as s:
            live = claims.live_sessions()
            s.prune(live)
            q = s.data['queues'].get(name, [])
        pos = next((i for i, e in enumerate(q) if e['session'] == sid), -1) + 1
        if not pos:
            print(f'You are not in line for {name} any more. Run join again if you still need it.')
            return 3
        if pos == 1:
            print(f'{turn_text(name)} (waited {mins(started)} min)')
            return 0
        h = q[0]
        if time.time() - max(h['turn_at'], started) > stale_after * 60:
            print(f'{h["name"]} has held {name} for {mins(h["turn_at"])} min ({h["text"][:120]}). You are number {pos}. '
                  f'Ask it with SendMessage to {h["name"]} whether it is still using the turn, or tell the user. '
                  f'Then wait again. Do not run the action yourself.')
            return 4
        if timeout and time.time() - started > timeout * 60:
            print(f'Still number {pos} for {name} after {timeout} min.')
            return 2
        time.sleep(POLL)


def done(name, result):
    sid = need_session()
    if not sid:
        return 2
    with Queues() as s:
        live = claims.live_sessions()
        s.prune(live)
        q = s.data['queues'].get(name, [])
        if not q or q[0]['session'] != sid:
            print(f'You do not hold {name}. ' + ('Use leave to drop your place.' if any(e['session'] == sid for e in q)
                                                  else 'You are not in that line.'))
            return 3
        h = q.pop(0)
        log({'queue': name, 'event': 'done', 'session': sid, 'name': h['name'], 'text': h['text'], 'result': result,
             'joined': claims.stamp(h['joined_at']), 'turn': claims.stamp(h['turn_at']),
             'waited_min': int((h['turn_at'] - h['joined_at']) / 60), 'held_min': mins(h['turn_at'])})
        if q:
            promote(q)
        else:
            del s.data['queues'][name]
    print(f'Finished your turn on {name}.' + (f' Next: {q[0]["name"]} ({q[0]["text"][:120]}).' if q else ' Nobody waiting.'))
    return 0


def leave(name, sid=None, quiet=False):
    sid = sid or need_session()
    if not sid:
        return 2
    with Queues() as s:
        live = claims.live_sessions()
        s.prune(live)
        q = s.data['queues'].get(name, [])
        mine = [e for e in q if e['session'] == sid]
        q[:] = [e for e in q if e['session'] != sid]
        if mine and not q:
            del s.data['queues'][name]
        elif q:
            promote(q)
    if mine:
        log({'queue': name, 'event': 'left', 'session': sid, 'name': mine[0]['name'], 'text': mine[0]['text']})
    if not quiet:
        print(f'Left {name}.' if mine else f'You were not in line for {name}.')
    return 0


def show(name=None):
    with Queues() as s:
        live = claims.live_sessions()
        s.prune(live)
        queues = {k: v for k, v in s.data['queues'].items() if name in (None, k)}
    if not queues:
        print('no queues' if name is None else f'nobody in line for {name}')
    for k, q in sorted(queues.items()):
        h = q[0]
        print(f'{k}: {h["name"]} holds it since {hhmm(h["turn_at"])} ({mins(h["turn_at"])} min): {h["text"][:150]}')
        for i, e in enumerate(q[1:], 2):
            print(f'  {i}. {e["name"]} waiting since {hhmm(e["joined_at"])}: {e["text"][:150]}')
    return 0


def history(args):
    name, n, i = None, 20, 0
    while i < len(args):
        if args[i] == '-n' and i + 1 < len(args) and args[i + 1].isdigit():
            n, i = int(args[i + 1]), i + 1
        else:
            name = args[i]
        i += 1
    try:
        recs = [json.loads(line) for line in open(HISTORY) if line.strip()]
    except (OSError, ValueError):
        recs = []
    recs = [r for r in recs if isinstance(r, dict) and name in (None, r.get('queue'))]
    if not recs:
        print('no queue history')
    for r in recs[-n:]:
        extra = f' | result: {r.get("result")}' if r.get('result') else ''
        timing = f' (waited {r.get("waited_min")} min, held {r.get("held_min")} min)' if r.get('event') == 'done' else ''
        print(f'{r.get("time")} {r.get("queue")} {r.get("event")} by {r.get("name")}{timing}: {r.get("text", "")[:150]}{extra}')
    return 0


def check(names, command, sid):
    """For guardrails: returns None, ('context', text, granted) or ('deny', text, []).
    All or nothing: a session takes a free turn only when it can take every queue the command needs, so two sessions
    never each hold one queue while waiting for the other's."""
    first_line = command.strip().splitlines()[0] if command.strip() else '(command)'
    with Queues() as s:
        live = claims.live_sessions()
        s.prune(live)
        busy = [n for n in names if s.data['queues'].get(n) and s.data['queues'][n][0]['session'] != sid]
        if busy:
            waits = []
            for name in busy:
                pos, q = enqueue(s, name, sid, live, first_line, auto=True)
                waits.append(wait_text(name, pos, q))
            return ('deny', 'This command is queued: only one session at a time may run it. ' + ' '.join(waits), [])
        granted = [n for n in names if not s.data['queues'].get(n)]
        for name in granted:
            enqueue(s, name, sid, live, first_line, auto=True)
    if granted:
        return ('context', 'Nobody else was waiting, so you now hold ' + ', '.join(granted) + '. Other sessions queue '
                'behind you until you finish. When the whole action is done and checked, run: '
                + ' ; '.join(f'python3 {ME} done {n} "<result>"' for n in granted), granted)
    return None


def release(names, sid):
    """Undo turns check() just granted, when the tool call is denied for another reason."""
    with Queues() as s:
        for name in names:
            q = s.data['queues'].get(name, [])
            if q and q[0]['session'] == sid:
                q.pop(0)
                if q:
                    promote(q)
                else:
                    del s.data['queues'][name]


def drop_session(sid):
    """SessionEnd: leave every queue."""
    if not os.path.exists(QUEUE):
        return
    try:
        names = [k for k, q in json.load(open(QUEUE)).get('queues', {}).items() if any(e.get('session') == sid for e in q)]
    except (OSError, ValueError, AttributeError):
        return
    for name in names:
        leave(name, sid=sid, quiet=True)


def holder_nudge(sid):
    """For the prompt hook: a reminder when this session has held a queue for a while."""
    try:
        queues = json.load(open(QUEUE)).get('queues', {})
    except (OSError, ValueError, AttributeError):
        return None
    held = [(k, q[0]) for k, q in queues.items() if isinstance(q, list) and q and isinstance(q[0], dict)
            and q[0].get('session') == sid and isinstance(q[0].get('turn_at'), (int, float))
            and time.time() - q[0]['turn_at'] > HOLD_NUDGE_MIN * 60]
    if not held:
        return None
    return ' '.join(f'You have held the {k} queue for {mins(e["turn_at"])} min ({len(queues[k]) - 1} waiting). If that '
                    f'action is finished, run: python3 {ME} done {k} "<result>". If not, keep going.' for k, e in held)


if __name__ == '__main__':
    cmd, rest = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ('list', [])
    try:
        if cmd in ('join', 'wait', 'done', 'leave') and (not rest or not NAME_RE.match(rest[0])):
            print(f'usage: {cmd} NAME ... (letters, digits and _ . : / - only)')
            sys.exit(2)
        if cmd == 'join':
            sys.exit(join(rest[0], ' '.join(rest[1:])))
        if cmd == 'wait':
            opts = dict(zip(rest[1::2], rest[2::2]))
            sys.exit(wait(rest[0], float(opts.get('--stale-after', 60)),
                          float(opts['--timeout']) if '--timeout' in opts else None))
        if cmd == 'done':
            sys.exit(done(rest[0], ' '.join(rest[1:])))
        if cmd == 'leave':
            sys.exit(leave(rest[0]))
        if cmd == 'list':
            sys.exit(show(rest[0] if rest else None))
        if cmd == 'history':
            sys.exit(history(rest))
        print(__doc__)
    except KeyboardInterrupt:
        sys.exit(130)
