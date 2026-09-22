#!/usr/bin/env python3
"""Non-Claude side of the shared agent layer: one adapter so Codex, OpenCode and DeepSeek's dsh run the same
hooks as Claude Code. Kimi Code CLI (kimi) and Grok Build (grok) are wired the same way.

    agent_hook.py --agent codex|opencode|dsh|kimi|grok <event>

None of them writes a session registry, and their hook payload field names differ from Claude's. This adapter
sits between them and the shared scripts:

  session-start   registers the session in the agent's registry dir, then runs memory_guard session-start,
                  claims.py session-start and memory_recall.py (project memories for the cwd)
  prompt          marks the session busy, then runs effort_gate (Codex only), claims.py prompt and memory_recall.py
  pretool         runs memory_guard pretool (heavy tools only) and claims.py pretool per file touched
                  (claims, queues, guardrails)
  stop            marks the session idle
  session-end     runs claims.py session-end and drops the registry entry

Everything fails open: a broken adapter must never block a turn. The delegated scripts are the same files Claude
Code uses, so a rule learned in one agent applies in every other with no copy.
"""
import glob, hashlib, json, os, re, shlex, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
CODEX_HOME = os.environ.get('CODEX_HOME') or os.path.expanduser('~/.codex')
# Per agent: where its registry lives (inside a dir second_opinion.py's sandbox lets it write), the word that
# identifies its process in `ps -o args`, the rules file it was given, and whether the effort gate applies.
AGENTS = {
    'codex': dict(registry=os.path.join(CODEX_HOME, 'agent-sessions'),  # not sessions/: Codex's own rollout store
                  proc='codex', rules='~/.codex/AGENTS.md', effort=True),
    'opencode': dict(registry=os.path.expanduser('~/.local/state/opencode/agent-sessions'),
                     proc='opencode', rules='~/.config/opencode/AGENTS.md', effort=False),
    # dsh maps UserPromptSubmit onto every model step, and context added after the first step starts an extra
    # turn whose reply replaces the real answer in headless output. So dsh takes prompt context once per session.
    'dsh': dict(registry=os.path.expanduser('~/.dsh/agent-sessions'),
                proc='dsh', rules='~/.dsh/AGENTS.md', effort=False, prompt_once=True),
    # kimi treats SessionStart as observe-only and drops its output, so the start-of-session context rides on
    # the first UserPromptSubmit instead; later prompts get the normal per-prompt context. It pastes a hook's stdout
    # into the context verbatim, so it gets plain text, not the JSON envelope.
    'kimi': dict(registry=os.path.expanduser('~/.kimi-code/agent-sessions'),
                 proc='kimi', rules='~/.kimi-code/AGENTS.md', effort=False, start_on_prompt=True, plain_context=True),
    # grok ignores SessionStart stdout and discards an allowing UserPromptSubmit hook's stdout, so both are kept
    # in a pending file and delivered with the next PreToolUse, whose additionalContext grok does pass on. Its
    # executable is ~/.grok/downloads/grok-macos-aarch64, which is what libproc reports where ps is blocked.
    'grok': dict(registry=os.path.expanduser('~/.grok/agent-sessions'),
                 proc='grok', proc_re=r'grok(?:-macos-[A-Za-z0-9_]+)?', rules='~/.grok/rules/shared-agent-layer.md',
                 effort=False, deferred_context=True),
}
AGENT = 'codex'           # set from --agent in main()
REGISTRY = AGENTS[AGENT]['registry']
LADDER = ['low', 'medium', 'high', 'xhigh', 'max']
# Claude writes lower_snake; Codex may write either. Map every alias onto the Claude name the scripts expect.
ALIASES = {
    'toolName': 'tool_name', 'tool': 'tool_name',
    'toolInput': 'tool_input', 'input': 'tool_input', 'arguments': 'tool_input',
    'sessionId': 'session_id', 'threadId': 'session_id', 'thread_id': 'session_id',
    'transcriptPath': 'transcript_path', 'rolloutPath': 'transcript_path', 'rollout_path': 'transcript_path',
    'workingDirectory': 'cwd', 'working_directory': 'cwd', 'workdir': 'cwd',
    'userPrompt': 'prompt', 'user_prompt': 'prompt', 'message': 'prompt', 'text': 'prompt',
    'agentId': 'agent_id',
}
# Codex names its shell tool differently from Claude's Bash, and its edit tool is apply_patch.
HEAVY_TOOLS = {'Bash', 'Agent', 'Task', 'Workflow'}  # what memory_guard.py pretool is wired for in Claude Code
EFFORT_MAP = {'minimal': 'low', 'none': 'low', 'xhigh': 'xhigh', 'max': 'max'}  # Codex ladder -> effort_gate ladder
PATCH_FILE = re.compile(r'^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+?)\s*$', re.M)  # apply_patch envelope
MAX_PATCH_FILES = 64   # above this the call is refused rather than partly checked
TOOL_NAMES = {'shell': 'Bash', 'local_shell': 'Bash', 'exec_command': 'Bash', 'bash': 'Bash',
              'apply_patch': 'Edit', 'edit_file': 'Edit', 'write_file': 'Write', 'create_file': 'Write',
              'edit': 'Edit', 'multiedit': 'Edit', 'multi_edit': 'Edit', 'patch': 'Edit', 'write': 'Write',
              'task': 'Task', 'subagent': 'Task',   # OpenCode and dsh name their tools in lower case
              'AgentSwarm': 'Agent',                 # kimi's fan-out tool
              'run_terminal_command': 'Bash', 'run_terminal_cmd': 'Bash', 'search_replace': 'Edit',  # grok
              'spawn_subagent': 'Task'}
# The tools the claim, queue and guardrail checks are for; grok also sends every other tool (for its context).
GATED_TOOLS = {'Bash', 'Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'Agent', 'Task', 'Workflow'}


def read_payload():
    raw = sys.stdin.read() if not sys.stdin.isatty() else ''
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


CODEX_EFFORTS = ('minimal', 'none', 'low', 'medium', 'high', 'xhigh', 'max')


def _effort(v):
    """A Codex effort mapped onto the gate's ladder, or None when it is not a known level."""
    v = v.strip().lower() if isinstance(v, str) else ''
    return EFFORT_MAP.get(v, v) if v in CODEX_EFFORTS else None


def codex_effort(payload):
    """Codex's model_reasoning_effort, mapped onto the effort gate's ladder: from the payload, else the rollout's
    latest turn_context (last 256 KB only), else config.toml."""
    for key in ('model_reasoning_effort', 'reasoningEffort', 'reasoning_effort', 'effort'):
        v = payload.get(key)
        if isinstance(v, dict):
            v = v.get('level')
        if _effort(v):
            return _effort(v)
    path = next((payload.get(k) for k in ('transcript_path', 'rolloutPath', 'rollout_path')
                 if isinstance(payload.get(k), str) and payload.get(k)), None)
    if path:
        # the rollout's latest turn_context records the effort the session really runs at, including a
        # `-c model_reasoning_effort=...` override that config.toml never sees; read only the tail
        try:
            with open(path, 'rb') as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 262144))
                lines = f.read().decode('utf-8', 'replace').splitlines()
            for line in reversed(lines):
                if '"turn_context"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue   # a line cut by the tail read, or partly written
                if not isinstance(d, dict) or d.get('type') != 'turn_context':
                    continue
                body = d.get('payload')
                level = body.get('effort') if isinstance(body, dict) else None
                if _effort(level):
                    return _effort(level)
                # no effort on this turn_context: keep looking at earlier ones, then the config
        except OSError:
            pass
    try:  # fall back to the config the session was started with
        for line in open(os.path.join(CODEX_HOME, 'config.toml')):
            m = re.match(r'\s*model_reasoning_effort\s*=\s*["\']([A-Za-z]+)["\']', line)
            if m and _effort(m.group(1)):
                return _effort(m.group(1))
    except OSError:
        pass
    return None


def normalise(data):
    """Rename Codex field spellings onto the ones claims.py / memory_guard.py / effort_gate.py read."""
    out = dict(data)
    for src, dst in ALIASES.items():
        if src in out and dst not in out:
            out[dst] = out[src]
    ti = out.get('tool_input')
    if isinstance(ti, str):                       # some payloads pass the arguments as a JSON string
        try:
            ti = json.loads(ti)
        except ValueError:
            ti = {'command': ti}
        out['tool_input'] = ti
    if isinstance(ti, dict):
        for src, dst in (('cmd', 'command'), ('path', 'file_path'), ('filePath', 'file_path'),
                         ('file', 'file_path'), ('filename', 'file_path'), ('target_file', 'file_path'),
                         ('targetFile', 'file_path')):
            if src in ti and dst not in ti:
                ti[dst] = ti[src]
        if isinstance(ti.get('command'), list):   # Codex shell tools pass argv; keep the quoting a parser needs
            ti['command'] = shlex.join(str(x) for x in ti['command'])
    name = out.get('tool_name')
    if isinstance(name, str):
        out['tool_name'] = TOOL_NAMES.get(name, TOOL_NAMES.get(name.lower(), name))
    out.setdefault('cwd', os.getcwd())
    out['files'] = patch_files(out)   # every path the call touches, for the claim check
    if out['files'] and not (isinstance(ti, dict) and ti.get('file_path')):
        out.setdefault('tool_input', {})['file_path'] = out['files'][0]
    out['agent'] = AGENT
    level = codex_effort(data) if AGENTS[AGENT]['effort'] else None
    if level:  # the effort gate reads data['effort']['level']; Codex's own setting is the truth here
        out['effort'] = {'level': level}
    out.pop('transcript_path', None)  # a Codex rollout is not a Claude transcript; let the gate use 'effort'
    return out


def patch_files(payload):
    """Absolute paths an edit touches. apply_patch carries them inside the patch text, not as a field, so a
    claim check that only reads file_path would let a Codex patch overwrite another session's file."""
    ti = payload.get('tool_input') if isinstance(payload.get('tool_input'), dict) else {}
    cwd = payload.get('cwd') or os.getcwd()
    found = []
    for value in (ti.get('file_path'), ti.get('input'), ti.get('patch'), ti.get('patchText'), ti.get('command'),
                  ti.get('content') if payload.get('tool_name') != 'Write' else None):
        if not isinstance(value, str):
            continue
        hits = PATCH_FILE.findall(value)
        if hits:
            found += hits
        elif value is ti.get('file_path'):
            found.append(value)
    out = []
    for f in found:
        f = os.path.normpath(f if os.path.isabs(f) else os.path.join(cwd, f))
        if f not in out:
            out.append(f)
    return out


def held_claims(sid, paths):
    """The subset of paths this session already claimed before this call."""
    import claims
    held = set()
    for p in paths:
        r = claims.repo_of(p)
        if not r:
            continue
        top, store = r
        rel = os.path.relpath(os.path.realpath(p), top)
        try:
            d = json.load(open(store))
        except (OSError, ValueError):
            continue
        if any(isinstance(c, dict) and c.get('session') == sid and c.get('file') == rel and c.get('worktree') == top
               for c in d.get('claims', [])):
            held.add(p)
    return held


def give_back(sid, paths):
    """Drop this session's claims on paths it claimed for a call that was then denied and never ran."""
    import claims
    for p in paths:
        r = claims.repo_of(p)
        if not r:
            continue
        top, store = r
        rel = os.path.relpath(os.path.realpath(p), top)
        try:
            with claims.Store(store) as st:
                st.data['claims'] = [c for c in st.data['claims'] if not (
                    c.get('session') == sid and c.get('file') == rel and c.get('worktree') == top)]
        except Exception:                   # fail open: a claim left behind only expires later
            continue


def delegate(script, argv, payload):
    """Run one of the shared hook scripts with the normalised payload. Returns (exit_code, stdout, stderr)."""
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, script), *argv],
                           input=json.dumps(payload), capture_output=True, text=True, timeout=20)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return 0, '', f'agent_hook: {script}: {type(e).__name__}: {e}'


def context_of(stdout):
    """Pull the text a shared hook wanted to add to the model's context, whatever shape it used."""
    stdout = (stdout or '').strip()
    if not stdout:
        return ''
    try:
        d = json.loads(stdout)
    except ValueError:
        return stdout                              # plain text: SessionStart hooks print it directly
    if not isinstance(d, dict):
        return stdout
    hso = d.get('hookSpecificOutput')
    if isinstance(hso, dict) and isinstance(hso.get('additionalContext'), str):
        return hso['additionalContext']
    for key in ('additionalContext', 'systemMessage', 'reason'):
        if isinstance(d.get(key), str):
            return d[key]
    return ''


def decision_of(stdout):
    """(deny_reason, ask_reason) if a shared PreToolUse hook wanted to stop or question the call."""
    try:
        d = json.loads((stdout or '').strip() or '{}')
    except ValueError:
        return None, None
    if not isinstance(d, dict):
        return None, None
    hso = d.get('hookSpecificOutput') if isinstance(d.get('hookSpecificOutput'), dict) else {}
    dec = (hso.get('permissionDecision') or d.get('permissionDecision') or d.get('decision') or '').lower()
    why = hso.get('permissionDecisionReason') or d.get('permissionDecisionReason') or d.get('reason') or ''
    if dec in ('deny', 'block'):
        return why or 'blocked by the shared guardrails', None
    if dec == 'ask':
        return None, why or 'the shared guardrails want this checked'
    return None, None


# ---------------------------------------------------------------- session registry

SHORT = {'codex': 'cx', 'opencode': 'oc', 'dsh': 'ds', 'kimi': 'km', 'grok': 'gk'}


def name_for(pid, cwd):
    """<project>-<cx|oc|ds|km|gk><2 hex of pid>, close to how Claude Code derives its session names."""
    base = os.path.basename(os.path.realpath(cwd)) or AGENT
    base = re.sub(r'[^A-Za-z0-9._-]+', '-', base)[:24]
    return f'{base}-{SHORT.get(AGENT, AGENT[:2])}{pid % 256:02x}'


def bsd_info(pid):
    """(ppid, start epoch, name) from libproc; lives in memory_guard.py so claims liveness checks share it."""
    import memory_guard
    return memory_guard.bsd_info(pid)


def agent_pid():
    """The agent process this hook runs under, so the registry entry dies with it rather than with the hook.

    Matched on the whole command line, not the process name: dsh runs as `node .../bin/dsh`."""
    word = re.compile(r'(?:^|[/\s])' + (AGENTS[AGENT].get('proc_re') or re.escape(AGENTS[AGENT]['proc']))
                      + r'(?:\s|$)', re.I)
    pid = os.getppid()
    for _ in range(12):
        if pid <= 1:
            break
        try:
            out = subprocess.run(['/bin/ps', '-ww', '-o', 'ppid=,args=', '-p', str(pid)],
                                 capture_output=True, text=True, timeout=3).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            out = ''
        if not out:
            # ps blocked (sandbox-exec refuses setuid) or silent: walk with libproc on the executable's name
            # (kimi matches; dsh runs as node and won't, so it keeps the old parent-pid fallback)
            info = bsd_info(pid)
            if not info:
                break
            if word.search(info[2]):
                return pid
            pid = info[0]
            continue
        ppid, _, args = out.partition(' ')
        if word.search(args.strip()):
            return pid
        pid = int(ppid.strip() or 0)
    return os.getppid()


def proc_start_utc(pid):
    """Process start time in the '%a %b %d %H:%M:%S %Y' UTC form memory_guard.is_same_claude parses."""
    info = bsd_info(pid)
    if info and info[1]:                    # exact, and works where /bin/ps is blocked
        return time.strftime('%a %b %e %H:%M:%S %Y', time.gmtime(info[1])).replace('  ', ' ')
    try:
        r = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'etime='],
                           capture_output=True, text=True, timeout=3).stdout.split()
        if not r:
            return None
        days, _, clock = r[0].rpartition('-')
        parts = [int(x) for x in clock.split(':')]
        while len(parts) < 3:
            parts.insert(0, 0)
        secs = (int(days) if days else 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
        return time.strftime('%a %b %e %H:%M:%S %Y', time.gmtime(time.time() - secs)).replace('  ', ' ')
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def same_start(a, b):
    """Whether two procStart stamps describe the same process start.

    Both are rebuilt from ps etime, which has one-second granularity, so the same process can stamp a second
    apart between two hooks. Comparing the strings exactly would rebuild the registry record — and with it the
    session id every claim is keyed on — for no reason. memory_guard.is_same_claude uses the same 5s window.

    The window is also the limit of this identification: a pid reused within 5 seconds by a new Codex process
    whose payload carries no session id is indistinguishable, and inherits the old record. That is the same
    residual the three Claude accounts already live with, and the worst case is a dead session's claims
    staying held for their 4-hour expiry."""
    absent = lambda v: v is None or v == ''        # nothing to contradict
    if absent(a) or absent(b):
        return True
    if not isinstance(a, str) or not isinstance(b, str):
        return False                               # a corrupt stamp rebuilds the record rather than being trusted
    if a == b:
        return True
    try:
        import calendar
        return abs(calendar.timegm(time.strptime(a, '%a %b %d %H:%M:%S %Y'))
                   - calendar.timegm(time.strptime(b, '%a %b %d %H:%M:%S %Y'))) <= 5
    except (ValueError, TypeError):   # never let a malformed stamp escape and skip the whole hook
        return False


def registry_path(pid, sid):
    """One file per session, not per process: an OpenCode or Codex server can host several sessions, and one
    session's hook must never overwrite another's record (claims.py would then think the other one dead)."""
    key = hashlib.sha1(str(sid).encode()).hexdigest()[:12]
    return os.path.join(REGISTRY, f'{pid}-{key}.json')


REG_FILE = re.compile(r'^(\d+)-[0-9a-f]{12}\.json$')
LEGACY_FILE = re.compile(r'^\d+\.json$')   # the first adapter's one-file-per-process records
FLAG_FILE = re.compile(r'^\.[a-z]+-(\d+)-.*\.(?:prompted|pending)$')


def prune_dead():
    """Drop registry and flag files whose process is gone: dsh has no SessionEnd, so its entries would pile up."""
    for f in os.listdir(REGISTRY):
        if LEGACY_FILE.match(f):
            try:
                os.unlink(os.path.join(REGISTRY, f))
            except OSError:
                pass
            continue
        m = REG_FILE.match(f) or FLAG_FILE.match(f)
        if not m:
            continue
        try:
            os.kill(int(m.group(1)), 0)
        except ProcessLookupError:
            try:
                os.unlink(os.path.join(REGISTRY, f))
            except OSError:
                pass
        except (ValueError, OSError):
            continue


def records_of(pid):
    """(path, record) for every registry record of this process, newest first."""
    out = []
    for f in glob.glob(os.path.join(REGISTRY, f'{pid}-*.json')):
        try:
            rec = json.load(open(f))
            if isinstance(rec, dict) and rec.get('pid') == pid:
                out.append((f, rec))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda x: -(x[1].get('updatedAt') or 0))


def register(payload, status):
    """Write or refresh this session's registry entry so claims.py and memory_guard.py can see it."""
    try:
        os.makedirs(REGISTRY, exist_ok=True)
        pid = agent_pid()
        now = int(time.time() * 1000)
        sid, started = payload.get('session_id'), proc_start_utc(pid)
        rec = None
        for path, old in records_of(pid):
            # A reused pid must not inherit a dead process's records: claims are keyed on sessionId, so a stale
            # one makes a live session look dead, or a dead one look alive.
            if not same_start(old.get('procStart'), started):
                try:
                    os.unlink(path)
                except OSError:
                    pass
                continue
            if rec is None and (old.get('sessionId') == sid or not sid):
                rec = old                           # a payload without a session id keeps the newest identity
        if rec is None:
            cwd = payload.get('cwd') or os.getcwd()
            rec = {'pid': pid, 'sessionId': sid or f'{AGENT}-{pid}-{now}', 'cwd': cwd,
                   'startedAt': now, 'procStart': started, 'agent': AGENT,
                   'kind': 'interactive', 'entrypoint': AGENT, 'name': name_for(pid, cwd),
                   'nameSource': 'derived', 'nameSince': now}
        if started and not rec.get('procStart'):
            rec['procStart'] = started              # fill in what an earlier ps failure left blank
        if payload.get('cwd'):
            rec['cwd'] = payload['cwd']
        rec['status'], rec['updatedAt'], rec['statusUpdatedAt'] = status, now, now
        path = registry_path(pid, rec['sessionId'])
        tmp = f'{path}.{os.getpid()}.tmp'
        with open(tmp, 'w') as f:
            json.dump(rec, f)
        os.replace(tmp, path)
        os.environ['CLAUDE_CODE_SESSION_ID'] = rec['sessionId']   # what current_session() falls back to
        return rec
    except OSError:
        return {}


def first_prompt(rec, check_only=False):
    """True exactly once per session, and only if that could be recorded somewhere durable.

    Uses an O_EXCL flag file, first in the registry dir, else in the temp dir (dsh's read-only sandbox may allow
    only that). If neither can be written, say False: repeating the context on every step would start extra turns,
    and giving none is harmless because the rules still load through AGENTS.md."""
    import tempfile
    pid = rec.get('pid') or agent_pid()
    sid = re.sub(r'[^A-Za-z0-9._-]', '_', str(rec.get('sessionId') or pid))[:80]
    for d in (REGISTRY, tempfile.gettempdir()):
        flag = os.path.join(d, f'.{AGENT}-{pid}-{sid}.prompted')
        if check_only:
            if os.path.exists(flag):
                return False
            continue
        try:
            os.close(os.open(flag, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            return True
        except FileExistsError:
            return False
        except OSError:
            continue
    return None if check_only else False


def _safe(sid):
    return re.sub(r'[^A-Za-z0-9._-]', '_', str(sid))[:80]


def stash(rec, kind, parts):
    """Keep context for the next PreToolUse (grok: see AGENTS). kind is 'start' (once) or 'prompt' (replaced every
    turn, so a turn with no tool call never delivers an old turn's recall late)."""
    parts = [p for p in parts if p and p.strip()]
    pid = rec.get('pid') or agent_pid()
    path = os.path.join(REGISTRY, f'.{AGENT}-{pid}-{_safe(rec.get("sessionId") or pid)}.{kind}.pending')
    try:
        if not parts:
            if kind == 'prompt' and os.path.exists(path):
                os.unlink(path)
            return
        os.makedirs(REGISTRY, exist_ok=True)
        tmp = f'{path}.{os.getpid()}.tmp'
        with open(tmp, 'w') as f:
            f.write('\n'.join(parts))
        os.replace(tmp, path)
    except OSError:
        pass


def take_pending(sid):
    """Claim and return this session's pending context. A rename claims it, so of two parallel tool calls only one
    delivers it."""
    out = []
    if not sid:
        return out
    for kind in ('start', 'prompt'):
        pattern = os.path.join(glob.escape(REGISTRY), f'.{AGENT}-*-{glob.escape(_safe(sid))}.{kind}.pending')
        for f in glob.glob(pattern):
            grab = f'{f}.{os.getpid()}.taken'
            try:
                os.rename(f, grab)
            except OSError:
                continue                    # another tool call of the same turn took it first
            try:
                out.append(open(grab).read())
            except OSError:
                pass
            finally:
                try:
                    os.unlink(grab)
                except OSError:
                    pass
    return [p for p in out if p.strip()]


def unregister(rec):
    """Drop this session's record only; other sessions of the same process keep theirs."""
    try:
        if rec.get('pid') and rec.get('sessionId'):
            os.unlink(registry_path(rec['pid'], rec['sessionId']))
    except OSError:
        pass


def session_env(rec, payload=None):
    """Make the delegated scripts resolve this session the same way they do under Claude Code.

    claims.py returns early on a payload with no session_id, so the id the registry settled on has to go
    back into the payload as well as into the environment."""
    if rec.get('sessionId'):
        os.environ['CLAUDE_CODE_SESSION_ID'] = rec['sessionId']
        if payload is not None:
            payload['session_id'] = rec['sessionId']
    if rec.get('pid'):
        os.environ['CLAUDE_PID'] = str(rec['pid'])


# ---------------------------------------------------------------- events

FRAME = ('[Background from the shared agent layer, added by a hook. It is NOT a request and needs no reply. '
         'Do the task you were given; use this only where it applies.]')


def emit_context(event, parts):
    parts = [p for p in parts if p and p.strip()]
    if not parts:
        return 0
    # Some harnesses (dsh) place hook context after the user's message, and a model then answers the context
    # instead of the task. The frame says what it is.
    text = FRAME + '\n' + '\n'.join(parts) if event in ('SessionStart', 'UserPromptSubmit') else '\n'.join(parts)
    if AGENTS[AGENT].get('plain_context'):   # kimi pastes hook stdout into the context as is
        print(text)
        return 0
    print(json.dumps({'hookSpecificOutput': {'hookEventName': event, 'additionalContext': text}}))
    return 0


def start_parts(payload):
    """What a new session is told: other sessions' notes, memory state, recalled memories, where its rules are."""
    parts = []
    calls = [('memory_guard.py', ['session-start']), ('claims.py', ['session-start'])]
    if not AGENTS[AGENT].get('prompt_once'):   # dsh gets start and prompt context together; one recall is enough
        calls.append(('memory_recall.py', ['session-start', '--agent', AGENT]))
    for script, argv in calls:
        _, out, err = delegate(script, argv, payload)
        if err:
            print(err, file=sys.stderr)
        parts.append(context_of(out))
    parts.append(f'You are {AGENT}, sharing this Mac with Claude Code sessions and other agents. The same claims, '
                 f'queue and guardrail rules apply to you: follow {AGENTS[AGENT]["rules"]}.')
    return parts


def session_start():
    payload = normalise(read_payload())
    try:
        os.makedirs(REGISTRY, exist_ok=True)
        prune_dead()
    except OSError:
        pass
    rec = register(payload, 'busy')
    session_env(rec, payload)
    if AGENTS[AGENT].get('deferred_context'):   # grok ignores SessionStart output: deliver it with the first tool
        stash(rec, 'start', start_parts(payload))
        return 0
    return emit_context('SessionStart', start_parts(payload))


def prompt():
    payload = normalise(read_payload())
    rec = register(payload, 'busy')
    session_env(rec, payload)
    parts = []
    once = AGENTS[AGENT].get('prompt_once')
    if once:
        if first_prompt(rec, check_only=True) is False:
            return 0
        try:
            prune_dead()
        except OSError:
            pass
        parts += start_parts(payload)  # dsh runs SessionStart detached, so it lands late; the first prompt is awaited
    elif AGENTS[AGENT].get('start_on_prompt') and first_prompt(rec):
        try:
            prune_dead()
        except OSError:
            pass
        parts += start_parts(payload)  # kimi drops SessionStart output; the first prompt carries it
    out, err = '', ''
    if AGENTS[AGENT]['effort']:        # only Codex has a per-session effort to gate on
        _, out, err = delegate('effort_gate.py', [], payload)
    if err:
        print(err, file=sys.stderr)
    gate = context_of(out)
    if gate:   # Codex has no /effort command; point at the switch it does have
        gate = gate.replace('Run /effort <needed>, then send: go',
                            'Say which effort it needs and stop; the user restarts you with '
                            '`codex -c model_reasoning_effort=<needed>` or switches it with /model')
        parts.append(gate)
    recall = ['prompt', '--agent', AGENT] + (['--stateless'] if once else [])  # one emission: no seen-state needed
    for script, argv in (('claims.py', ['prompt']), ('memory_recall.py', recall)):
        _, out, err = delegate(script, argv, payload)
        if err:
            print(err, file=sys.stderr)
        parts.append(context_of(out))
    if once and not first_prompt(rec):   # flag taken only now: a crash above leaves the next step free to retry
        return 0
    if AGENTS[AGENT].get('deferred_context'):   # grok discards this event's stdout: deliver it with the next tool
        stash(rec, 'prompt', parts)
        return 0
    return emit_context('UserPromptSubmit', parts)


def pretool():
    payload = normalise(read_payload())
    deferred = AGENTS[AGENT].get('deferred_context')
    if deferred and payload.get('tool_name') not in GATED_TOOLS:
        # grok runs this hook on every tool so pending context arrives early; a read needs no claim check
        pend = take_pending(payload.get('session_id'))
        return emit_context('PreToolUse', [FRAME] + pend if pend else [])
    session_env(register(payload, 'busy'), payload)
    notes, deny, ask = [], None, None
    files = payload.get('files') or []
    if len(files) > MAX_PATCH_FILES:
        why = (f'This call touches {len(files)} files. The shared claim check runs per file and would time out, '
               f'so it cannot prove none of them is being edited by another live session. Split it into patches '
               f'of at most {MAX_PATCH_FILES} files.')
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                                 'permissionDecisionReason': why}}))
        print(why, file=sys.stderr)
        return 2
    calls = []
    if payload.get('tool_name') in HEAVY_TOOLS:    # the guard denies on low memory; edits must not go through it
        calls.append(('memory_guard.py', ['pretool'], payload))
    sid = payload.get('session_id')
    multi = bool(sid) and len(files) > 1
    created = []                                    # files this very call newly claimed, in order
    for path in (files or [None]):                  # one claim check per file the call touches
        per = payload
        if path:
            per = dict(payload, tool_input=dict(payload.get('tool_input') or {}, file_path=path))
        calls.append(('claims.py', ['pretool'], per))
    for script, argv, data in calls:
        path = (data.get('tool_input') or {}).get('file_path') if script == 'claims.py' else None
        had = multi and path and path in held_claims(sid, [path])
        code, out, err = delegate(script, argv, data)
        if multi and path and not had and path in held_claims(sid, [path]):
            created.append(path)
        if err:
            print(err, file=sys.stderr)
        d, a = decision_of(out)
        deny, ask = deny or d, ask or a
        if code == 2:                      # Claude's "block, and tell the model why" exit code
            deny = deny or (err.strip() or out.strip() or 'blocked by the shared guardrails')
        notes.append(context_of(out))
        if deny:
            break                          # the call will not run, so do not claim the files after this one
    if deny:
        if created:                         # the call will not run: give back what it claimed on the way
            give_back(sid, created)
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                                 'permissionDecisionReason': deny}}))
        print(deny, file=sys.stderr)
        return 2
    if ask:
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'ask',
                                                 'permissionDecisionReason': ask}}))
        return 0
    pend = take_pending(payload.get('session_id')) if deferred else []   # only on an allowed call: a deny drops it
    return emit_context('PreToolUse', ([FRAME] + pend if pend else []) + notes)


def stop():
    payload = normalise(read_payload())
    if payload.get('reason') in ('shutdown', 'channel_closed'):
        # grok's extra session-end Stop runs beside SessionEnd; registering here would re-create the record
        # SessionEnd just dropped
        return 0
    register(payload, 'idle')
    return 0


def session_end():
    payload = normalise(read_payload())
    rec = register(payload, 'idle')
    session_env(rec, payload)
    _, _, err = delegate('claims.py', ['session-end'], payload)
    if err:
        print(err, file=sys.stderr)
    unregister(rec)
    return 0


EVENTS = {'session-start': session_start, 'prompt': prompt, 'pretool': pretool,
          'stop': stop, 'session-end': session_end}


def main(default_agent='codex'):
    global AGENT, REGISTRY
    argv = sys.argv[1:]
    AGENT = default_agent
    if len(argv) >= 2 and argv[0] == '--agent':
        AGENT, argv = argv[1], argv[2:]
    if AGENT not in AGENTS or not argv or argv[0] not in EVENTS:
        print(__doc__.strip(), file=sys.stderr)
        return 0
    REGISTRY = AGENTS[AGENT]['registry']
    return EVENTS[argv[0]]()


def run(default_agent='codex'):
    try:
        sys.exit(main(default_agent))
    except Exception as e:      # never break a turn because the adapter failed
        print(f'agent_hook: {type(e).__name__}: {e}', file=sys.stderr)
        sys.exit(0)


if __name__ == '__main__':
    run()
