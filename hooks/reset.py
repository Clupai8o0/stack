#!/usr/bin/env python3
"""Agent-requested context resets for Claude Code sessions running in kitty (main and cx accounts).

Claude Code gives the model no way to run /clear or /compact itself. This script lets the agent ask for one at a
good point in the task (instructions written down, a milestone done). When the turn ends, the Stop hook types the
command into the agent's own kitty window, and the SessionStart hook loads the handoff into the fresh context.

  reset.py request clear   [--handoff PATH]           agent: /clear when this turn ends, continue from the handoff
  reset.py request compact [--focus TEXT] [--handoff PATH]   agent: /compact TEXT when this turn ends
  reset.py cancel          agent: drop this session's queued reset
  reset.py context         agent: how full its own context is, and how often it was compacted
  reset.py stop            Stop hook: starts a queued reset, detached, and returns at once
  reset.py session-start   SessionStart hook (clear|compact): loads the handoff after a reset this script started
  reset.py fire FILE       internal: waits for the turn to settle, types the command, then the continue prompt
  reset.py status          queued and recent resets
  reset.py off | on        kill switch for every session

Only kitty remote-control text input is used (send-text), never window creation: kitty @ launch --type=os-window
crashed kitty 0.48.2 on macOS 27 and took every session down.

Safety, in order: a request is refused outside kitty, without a fresh handoff (clear), past MAX_PER_HOUR resets an
hour per window, or with the kill switch on. The Stop hook only acts on a request the main agent made in the turn
that just ended (not a subagent, not a turn the user interrupted). Before typing, the turn must really be over and
the input box empty; after typing, the box must hold exactly the command or it is erased again. Jobs are keyed by
kitty socket plus window id, since every kitty instance numbers its windows from 1. The handoff is copied at request
time, so another session rewriting the shared handoff.md cannot leak into this one.
"""
import glob, hashlib, json, os, re, subprocess, sys, time
from datetime import datetime

STATE = os.path.expanduser('~/.local/state/claude-reset')
OFF = os.path.join(STATE, 'off')
LOG = os.path.join(STATE, 'log.jsonl')
KITTY = '/Applications/kitty.app/Contents/MacOS/kitty'
MAX_PER_HOUR = 3
REQUEST_TTL = 10 * 60      # a request whose turn never ended within this is dropped
HANDOFF_FRESH = 15 * 60    # a /clear needs a handoff written this recently
HANDOFF_INJECT = 9000      # characters of handoff put into the fresh context; the rest is read from the file
SETTLE = 2.0               # seconds between the Stop hook and typing, so Claude is back at its prompt
WAIT_DONE = {'clear': 90, 'compact': 600}   # how long to wait for SessionStart before giving up
TAIL = 8 << 20             # bytes of transcript read to find the turn that made the request


def say(msg, code=0):
    print(msg)
    sys.exit(code)


def log(**row):
    try:
        os.makedirs(STATE, exist_ok=True)
        row['at'] = time.time()
        with open(LOG, 'a') as f:
            f.write(json.dumps(row) + '\n')
    except OSError:
        pass


def read_log():
    try:
        return [json.loads(l) for l in open(LOG) if l.strip()]
    except (OSError, ValueError):
        return []


def write_json(path, data):
    os.makedirs(STATE, exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, path)


def read_json(path):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return None


def remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def term_key(listen, window):
    """One kitty window across all kitty instances: the socket names the instance, the id the window in it."""
    return f'{hashlib.sha1(str(listen).encode()).hexdigest()[:8]}-{re.sub(r"[^0-9]", "", str(window))}'


def req_path(sid):
    return os.path.join(STATE, f'req-{re.sub(r"[^A-Za-z0-9_-]", "", sid)}.json')


def win_path(key):
    return os.path.join(STATE, f'win-{key}.json')


def project_root(cwd):
    r = subprocess.run(['git', '-C', cwd, 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else cwd


def fires_last_hour(key):
    now = time.time()
    return sum(1 for r in read_log() if r.get('event') == 'fired' and r.get('key') == key and now - r.get('at', 0) < 3600)


def kitty(listen, *args, stdin=None):
    return subprocess.run([KITTY, '@', '--to', listen, *args], input=stdin, capture_output=True, text=True,
                          timeout=10)


# ---- reading the input box -------------------------------------------------------------------------------------

SGR = re.compile(r'\x1b\[([0-9;:]*)m')
OTHER_ESC = re.compile(r'\x1b(?:\[[0-9;?]*[A-Za-ln-z]|\][^\x07\x1b]*(?:\x07|\x1b\\))')
# A running turn's spinner line: a glyph, a verb with an ellipsis, then "(15s · still thinking)". A finished turn
# reads "✻ Worked for 20s · done 11:37 pm", with no ellipsis.
SPINNER = re.compile(r'^\s*[^\w\s❯─│]\s+\S+…\s*(\(|$)')


def sgr_dim(params, dim):
    """Walk one SGR code's parameters and return the new dim state. Colour codes (38/48/58 with 5;n or 2;r;g;b)
    carry sub-values that must be skipped: the 2 in 38;2;r;g;b is not 'dim'."""
    ps, i = params.replace(':', ';').split(';') if params else ['0'], 0
    while i < len(ps):
        p = ps[i]
        if p in ('38', '48', '58'):
            i += 3 if ps[i + 1:i + 2] == ['5'] else 5 if ps[i + 1:i + 2] == ['2'] else 1
            continue
        if p in ('', '0', '22'):
            dim = False
        elif p == '2':
            dim = True
        i += 1
    return dim


def visible_text(line):
    """The characters of one screen line a person would read as typed text: escape codes removed, and dim runs
    (Claude Code's greyed-out prompt suggestion) dropped."""
    out, dim, pos = [], False, 0
    line = OTHER_ESC.sub('', line)
    for m in SGR.finditer(line):
        if not dim:
            out.append(line[pos:m.start()])
        dim = sgr_dim(m.group(1), dim)
        pos = m.end()
    if not dim:
        out.append(line[pos:])
    return ''.join(out)


def read_box(job):
    """('busy'|'unknown'|'ok', typed text). 'busy' while a turn runs anywhere on screen, 'unknown' when no prompt
    is visible (a dialog, a menu, the window gone)."""
    r = kitty(job['listen'], 'get-text', '--match', f'id:{job["window"]}', '--ansi')
    if r.returncode != 0 or not r.stdout:
        return 'unknown', ''
    lines = r.stdout.splitlines()
    plain = [OTHER_ESC.sub('', SGR.sub('', l)) for l in lines]
    if any('esc to interrupt' in l or SPINNER.match(l) for l in plain):
        return 'busy', ''
    prompts = [i for i, l in enumerate(plain) if l.lstrip().startswith('❯')]
    if not prompts:
        return 'unknown', ''
    i = prompts[-1]
    if not (i > 0 and plain[i - 1].strip().startswith('─')):   # the input box sits right under a rule line;
        return 'unknown', ''                                    # a dialog's "❯ 1. Yes" does not
    typed = visible_text(lines[i]).replace('\xa0', ' ').lstrip().lstrip('❯').strip()
    j = i + 1
    while j < len(plain) and not plain[j].lstrip().startswith('─'):   # a draft can wrap onto more lines
        typed += visible_text(lines[j]).replace('\xa0', ' ').strip()
        j += 1
    return 'ok', typed


def wait_for_empty(job, tries=5):
    state = 'unknown'
    for _ in range(tries):
        state, typed = read_box(job)
        if state == 'ok' and not typed:
            return 'empty'
        state = 'draft' if state == 'ok' else state
        time.sleep(2)
    return state


def type_line(job, text):
    """Type one line into the agent's window and press Enter only if the box then holds exactly that line: the user
    may have started typing in the moment between the check and the send. Otherwise erase what was typed, but only
    when the box ends with it, so a user's own text is never touched."""
    match = ('--match', f'id:{job["window"]}')
    try:
        kitty(job['listen'], 'send-text', *match, '--stdin', stdin=text)
        time.sleep(0.4)
        state, typed = read_box(job)
        if state == 'ok' and typed.replace(' ', '') == text.replace(' ', ''):
            kitty(job['listen'], 'send-key', *match, 'enter')
            return True
        if state == 'ok' and typed.replace(' ', '').endswith(text.replace(' ', '')):
            kitty(job['listen'], 'send-text', *match, '--stdin', stdin='\x7f' * len(text))   # one call, fast
        else:
            log(event='left in box', text=text, key=job['key'], state=state)
    except subprocess.TimeoutExpired:
        log(event='kitty timeout', text=text, key=job['key'])
    return False


# ---- reading the transcript ------------------------------------------------------------------------------------

def tail_records(tp, start=None):
    """Transcript records from byte `start`, or from the last TAIL bytes."""
    try:
        with open(tp, 'rb') as f:
            size = f.seek(0, 2)
            at = start if start is not None else max(0, size - TAIL)
            f.seek(at)
            chunk = f.read()
    except OSError:
        return []
    out = []
    for line in chunk.splitlines()[1 if start is None and at > 0 else 0:]:   # skip a line cut in half
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def human_prompt(r):
    if r.get('type') != 'user' or r.get('isMeta') or r.get('isCompactSummary') or r.get('isSidechain'):
        return False
    c = (r.get('message') or {}).get('content')
    if isinstance(c, str):
        return bool(c.strip())
    return isinstance(c, list) and any(isinstance(b, dict) and b.get('type') == 'text' for b in c) \
        and not any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in c)


def requested_this_turn(tp):
    """The kind ('clear'|'compact') the main agent last asked for with `reset.py request` after the last human
    prompt, or None. A subagent's calls live in its
    own transcript, and a request from a turn the user interrupted (no Stop hook) is followed by a new prompt."""
    recs = tail_records(tp)
    last = max((i for i, r in enumerate(recs) if human_prompt(r)), default=-1)
    kind = None
    for r in recs[last + 1:]:
        if r.get('type') != 'assistant' or r.get('isSidechain'):
            continue
        for b in (r.get('message') or {}).get('content') or []:
            if isinstance(b, dict) and b.get('type') == 'tool_use' and b.get('name') == 'Bash':
                cmd = str((b.get('input') or {}).get('command', ''))
                m = re.findall(r'\brequest\s+(clear|compact)\b', cmd)   # the req file proves it was reset.py
                kind = m[-1] if m else kind
    return kind


def wrote_file(tp, path):
    """True when this session's main agent wrote `path` (Edit/Write, or a Bash command naming it), so a fresh
    handoff.md another session shares in the same project is not taken for ours."""
    for r in tail_records(tp):
        if r.get('type') != 'assistant' or r.get('isSidechain'):
            continue
        for b in (r.get('message') or {}).get('content') or []:
            if not (isinstance(b, dict) and b.get('type') == 'tool_use'):
                continue
            inp = b.get('input') or {}
            if b.get('name') in ('Edit', 'Write', 'MultiEdit') and os.path.abspath(str(inp.get('file_path', ''))) == path:
                return True
            if b.get('name') == 'Bash' and path in str(inp.get('command', '')):
                return True
    return False


def session_transcript(sid):
    home = os.environ.get('CLAUDE_CONFIG_DIR') or os.path.expanduser('~/.claude')
    found = glob.glob(os.path.join(home, 'projects', '*', f'{sid}.jsonl')) if sid else []
    return found[0] if found else ''


def turn_continued(job):
    """True when a user or assistant message reached the transcript after the Stop hook: a queued message, or a
    Stop hook that kept Claude going. Claude appends bookkeeping lines (turn_duration, ai-title, mode) after every
    turn, and the turn's own last reply can land just after the Stop hook reads the file, so an assistant line
    counts only when it is stamped more than a second after the Stop."""
    tp, start = job.get('transcript') or '', job.get('transcript_size', -1)
    if not tp or start < 0 or not os.path.isfile(tp):
        return False
    for r in tail_records(tp, start):
        if r.get('isSidechain') or r.get('type') not in ('user', 'assistant'):
            continue
        if r['type'] == 'user':
            return True
        try:
            at = datetime.fromisoformat(r.get('timestamp', '').replace('Z', '+00:00')).timestamp()
        except ValueError:
            continue
        if at > job.get('stopped_at', 0) + 1:
            return True
    return False


# ---- commands --------------------------------------------------------------------------------------------------

def request(args):
    kind = args[0] if args and args[0] in ('clear', 'compact') else None
    if not kind:
        say('usage: reset.py request clear|compact [--focus TEXT] [--handoff PATH]', 2)
    opts = dict(zip(args[1::2], args[2::2]))
    sid, window, listen = (os.environ.get(k, '') for k in ('CLAUDE_CODE_SESSION_ID', 'KITTY_WINDOW_ID',
                                                           'KITTY_LISTEN_ON'))
    cwd = os.getcwd()
    handoff = os.path.abspath(opts.get('--handoff') or os.path.join(project_root(cwd), '.claude', 'handoff.md'))
    if os.path.exists(OFF):
        say(f'Refused: agent resets are switched off (reset.py on). Ask the user to run /{kind}.', 1)
    if not sid:
        say('Refused: no CLAUDE_CODE_SESSION_ID, so this is not a Claude Code session.', 1)
    if not (window and listen.startswith('unix:')):
        say(f'Refused: not running in kitty with remote control. Ask the user to run /{kind}; handoff: {handoff}', 1)
    key = term_key(listen, window)
    if fires_last_hour(key) >= MAX_PER_HOUR:
        say(f'Refused: {MAX_PER_HOUR} resets in this window in the last hour. Carry on, or ask the user.', 1)
    text = ''
    if os.path.isfile(handoff) and time.time() - os.path.getmtime(handoff) < HANDOFF_FRESH:
        try:
            text = open(handoff, errors='replace').read()
        except OSError:
            text = ''
    if text and not wrote_file(session_transcript(sid), handoff):
        text = ''   # fresh, but written by another session
    if kind == 'clear' and len(text) < 200:
        say(f'Refused: /clear needs a handoff this session wrote in the last {HANDOFF_FRESH // 60} min: {handoff}. '
            'Write what is done, what is left in order, decisions, files, commands and open risks, then ask again.', 1)
    focus = re.sub(r'\s+', ' ', re.sub(r'[\x00-\x1f\x7f]', ' ', opts.get('--focus', ''))).strip()[:300]
    write_json(req_path(sid), dict(kind=kind, focus=focus, handoff=handoff if text else '', handoff_text=text,
                                   cwd=cwd, window=window, listen=listen, key=key, sid=sid,
                                   requested_at=time.time()))
    log(event='requested', kind=kind, key=key, sid=sid, cwd=cwd)
    what = f'/compact {focus}'.strip() if kind == 'compact' else '/clear'
    say(f'Queued: {what} runs when this turn ends, then a prompt to continue from {handoff if text else "the summary"}'
        ' (the handoff as it is now). End the turn now, with a reply that needs no reading: the screen is wiped. '
        'No background tasks or agents may still be running. Your file claims and status note are dropped by the '
        'reset and come back as you edit. `reset.py cancel` undoes this.')


def cancel():
    path = req_path(os.environ.get('CLAUDE_CODE_SESSION_ID', ''))
    if os.path.exists(path):
        remove(path)
        say('Cancelled the queued reset.')
    say('No reset queued for this session.')


def stop():
    data = json.load(sys.stdin)
    if not os.path.isdir(STATE):
        return
    now = time.time()
    for old in glob.glob(os.path.join(STATE, 'req-*.json')):   # a session that died with a request queued
        try:
            if now - os.path.getmtime(old) > REQUEST_TTL:
                remove(old)
        except OSError:
            pass
    path = req_path(str(data.get('session_id', '')))
    job = read_json(path)
    if not job:
        return
    remove(path)
    tp = data.get('transcript_path') or ''
    why = ('off' if os.path.exists(OFF) else
           'stale' if now - job.get('requested_at', 0) > REQUEST_TTL else
           'window changed' if term_key(os.environ.get('KITTY_LISTEN_ON', ''),
                                        os.environ.get('KITTY_WINDOW_ID', '')) != job.get('key') else
           'not requested by the main agent this turn' if requested_this_turn(tp) != job.get('kind') else '')
    if why:
        return log(event='dropped', why=why, key=job.get('key'))
    wp = win_path(job['key'])
    if os.path.exists(wp) and now - os.path.getmtime(wp) < 900:   # older: its fire process was killed
        return log(event='dropped', why='another reset in flight', key=job['key'])
    job.update(transcript=tp, transcript_size=os.path.getsize(tp) if os.path.isfile(tp) else -1,
               stopped_at=now, session_cwd=data.get('cwd') or job['cwd'])
    write_json(wp, job)
    subprocess.Popen([sys.executable, os.path.abspath(__file__), 'fire', wp], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def fire(wp):
    job = read_json(wp)
    if not job:
        return
    try:
        run_job(wp, job)
    finally:
        remove(wp)


def run_job(wp, job):
    time.sleep(SETTLE)
    state = wait_for_empty(job)
    if state != 'empty' or turn_continued(job):   # never type over a draft, into a dialog, or mid-turn
        return log(event='dropped', why=f'input {state}' if state != 'empty' else 'turn continued', key=job['key'])
    what = f'/compact {job["focus"]}'.strip() if job['kind'] == 'compact' else '/clear'
    job['fired_at'] = time.time()
    write_json(wp, job)
    if not type_line(job, what):
        return log(event='dropped', why='box changed while typing', key=job['key'])
    log(event='fired', kind=job['kind'], key=job['key'], sid=job['sid'], cwd=job['cwd'])
    deadline = time.time() + WAIT_DONE[job['kind']]
    while time.time() < deadline:
        time.sleep(1)
        job = read_json(wp) or job
        if job.get('done_at'):
            break
    else:
        return log(event='no session-start', kind=job['kind'], key=job['key'])
    if wait_for_empty(job, tries=10) != 'empty':
        return log(event='not continued', why='input not empty', kind=job['kind'], key=job['key'])
    where = f'the handoff loaded above ({job["handoff"]})' if job.get('handoff') else 'the summary above'
    if type_line(job, f'Context was reset on purpose (reset.py). Continue the task from {where}.'):
        log(event='continued', kind=job['kind'], key=job['key'])
    else:
        log(event='not continued', why='box changed while typing', kind=job['kind'], key=job['key'])


def session_start():
    data = json.load(sys.stdin)
    if data.get('source') not in ('clear', 'compact') or not os.path.isdir(STATE):
        return
    listen = os.environ.get('KITTY_LISTEN_ON', '')
    wp = win_path(term_key(listen, os.environ.get('KITTY_WINDOW_ID', '')))
    job = read_json(wp)
    if not job or not job.get('fired_at') or job.get('done_at') or time.time() - job['fired_at'] > 900:
        return
    if job.get('listen') != listen or (data['source'] == 'compact' and data.get('session_id') != job.get('sid')):
        return   # a /clear gets a new session id and may start in another cwd; the window key identifies it
    job['done_at'] = time.time()
    write_json(wp, job)
    text = ('This context was reset on purpose by reset.py, at your own request, to keep it small. Your file claims '
            'and status note were dropped; they come back as you edit (add a claims.py note).')
    body = job.get('handoff_text') or ''
    if body:
        cut = len(body) > HANDOFF_INJECT
        text += f' Continue the task from this handoff ({job["handoff"]}, as it was when you asked for the reset):\n\n'
        text += body[:HANDOFF_INJECT] + (f'\n\n[cut here: read the rest in {job["handoff"]}]' if cut else '')
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': text}}))


def context():
    """How full this session's context is, for the agent itself: Claude sees no statusline. Read from the last main
    assistant turn's usage in its own transcript, found by CLAUDE_CODE_SESSION_ID."""
    tp = session_transcript(os.environ.get('CLAUDE_CODE_SESSION_ID', ''))
    if not tp:
        say('context: unknown (no transcript for this session)', 1)
    tokens, compacts = 0, 0
    with open(tp, 'rb') as f:
        for line in f:
            if b'"subtype":"compact_boundary"' in line:
                compacts += 1
            if b'"type":"assistant"' in line and b'"usage"' in line:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                u = (r.get('message') or {}).get('usage') or {}
                n = sum(u.get(k) or 0 for k in ('input_tokens', 'cache_read_input_tokens',
                                                'cache_creation_input_tokens'))
                if n and not r.get('isSidechain'):
                    tokens = n
    window = int(os.environ.get('CLAUDE_CODE_AUTO_COMPACT_WINDOW') or 0) // 1000
    print(f'context: {tokens // 1000}k tokens, {compacts} compaction(s) this session '
          f'(auto-compact at {window or "?"}k)')


def status():
    print('agent resets:', 'OFF' if os.path.exists(OFF) else 'on')
    for name in sorted(os.listdir(STATE)) if os.path.isdir(STATE) else []:
        if name.startswith(('req-', 'win-')):
            j = read_json(os.path.join(STATE, name)) or {}
            print(f'  {name}: {j.get("kind")} window {j.get("window")} {j.get("cwd")}')
    for r in read_log()[-10:]:
        print(time.strftime('  %d %b %H:%M:%S', time.localtime(r['at'])), r.get('event'), r.get('kind', ''),
              r.get('key', r.get('window', '')), r.get('why', ''))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'
    try:
        if cmd == 'request':
            request(sys.argv[2:])
        elif cmd == 'cancel':
            cancel()
        elif cmd == 'stop':
            stop()
        elif cmd == 'session-start':
            session_start()
        elif cmd == 'fire':
            fire(sys.argv[2])
        elif cmd == 'context':
            context()
        elif cmd == 'off':
            os.makedirs(STATE, exist_ok=True)
            open(OFF, 'w').close()
            print('agent resets off')
        elif cmd == 'on':
            remove(OFF)
            print('agent resets on')
        else:
            status()
    except SystemExit:
        raise
    except Exception as e:    # a hook must never break the session
        log(event='error', cmd=cmd, error=repr(e))
        if cmd == 'request':
            print(f'reset.py failed: {e!r}')
            sys.exit(1)


if __name__ == '__main__':
    main()
