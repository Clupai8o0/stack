#!/usr/bin/env python3
"""Memory guard for Claude Code sessions on this Mac (both accounts).

  memory_guard.py pretool        PreToolUse hook for Agent|Workflow|Bash: blocks new heavy work when memory is low
  memory_guard.py session-start  SessionStart hook: tells Claude about low memory and idle sessions
  memory_guard.py list           table of every Claude session: status, idle hours, memory
  memory_guard.py close-idle H   ask idle sessions untouched for H+ hours to exit (SIGTERM). Run by a person, never by a hook.
"""
import glob, json, os, re, signal, subprocess, sys, time

REGISTRIES = [os.path.expanduser('~/.claude/sessions'), os.path.expanduser('~/.claude-exec/sessions'),
              os.path.expanduser('~/.claude-alt/sessions'),  # main, cx, cm
              os.path.expanduser('~/.codex/agent-sessions'),  # codex   } none of these four writes a registry,
              os.path.expanduser('~/.local/state/opencode/agent-sessions'),  # opencode } so agent_hook.py writes
              os.path.expanduser('~/.dsh/agent-sessions'),  # dsh     } one for them
              os.path.expanduser('~/.kimi-code/agent-sessions'),  # kimi    }
              os.path.expanduser('~/.grok/agent-sessions')]  # grok    }
# Only match a heavy program where a command starts (line start, or after ; & | ( $( ), optionally behind
# env assignments or a timeout wrapper, so text inside heredocs, echo strings or notes does not trip it.
HEAVY_BASH = re.compile(r"(?m)(?:^|[;&|(]|\$\()\s*(?:(?:[A-Za-z_][A-Za-z0-9_]*=\S*|env|nohup|time|command|exec|timeout\s+\S+|perl\s+-e\s+'[^']*'|(?:ba|z)?sh\s+-c\s+['\"]?)\s*)*"
                        r"(?:\S*/)?(opencode\s+run|agy\s+(?:-p|--print)|kimi\s+(?:-p|--prompt)|grok\s+(?:-p|--print)|codex\s+exec|claude\s+(?:-p|--print)|xcodebuild|gradlew|pod\s+install|"
                        r"xcrun\s+simctl\s+boot|emulator\s+-avd|docker\s+(?:compose\s+up|run|build)|npm\s+run\s+(?:ios|android|build)|repowise\s+init)\b")
SYSCTL, PS = '/usr/sbin/sysctl', '/bin/ps'
AGENT_PROC = re.compile(r'(?:^|[/\s])(?:claude|codex|opencode|dsh|kimi|grok(?:-macos-[A-Za-z0-9_]+)?)(?:\s|$)',
                        re.I)  # every agent with a registry; grok's binary is grok-macos-aarch64 behind a symlink


def sysctl(name):
    return subprocess.run([SYSCTL, '-n', name], capture_output=True, text=True, timeout=3).stdout.strip()


def memory_state():
    level = int(sysctl('kern.memorystatus_vm_pressure_level') or 1)   # 1 normal, 2 warn, 4 critical
    m = re.search(r'total = ([\d.]+)M\s+used = ([\d.]+)M', sysctl('vm.swapusage'))
    swap_pct = (float(m.group(2)) / float(m.group(1)) * 100) if m and float(m.group(1)) else 0.0
    low = level >= 4 or swap_pct >= 95          # block new heavy work
    tight = low or (level >= 2 and swap_pct >= 80)  # warn at session start
    return dict(level=level, swap_pct=round(swap_pct), low=low, tight=tight)


def bsd_info(pid):
    """(ppid, start epoch, name) from macOS libproc, or None. Used when /bin/ps cannot run: sandbox-exec refuses
    to exec it (setuid), which is how second_opinion.py runs kimi and dsh."""
    try:
        import ctypes
        u32 = ctypes.c_uint32

        class BSDInfo(ctypes.Structure):   # struct proc_bsdinfo, <sys/proc_info.h>
            _fields_ = [(n, u32) for n in ('flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid', 'rgid',
                                           'svuid', 'svgid', 'rfu')] + \
                       [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)] + \
                       [(n, u32) for n in ('nfiles', 'pgid', 'pjobc', 'tdev', 'tpgid')] + \
                       [('nice', ctypes.c_int32), ('start_sec', ctypes.c_uint64), ('start_usec', ctypes.c_uint64)]
        lib = ctypes.CDLL('/usr/lib/libproc.dylib')
        info = BSDInfo()
        if lib.proc_pidinfo(int(pid), 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
            return None                     # 3 = PROC_PIDTBSDINFO
        path = ctypes.create_string_buffer(4096)
        lib.proc_pidpath(int(pid), path, 4096)
        name = os.path.basename(path.value.decode(errors='replace')) or info.comm.decode(errors='replace')
        return info.ppid, int(info.start_sec), name
    except Exception:
        return None


def proc_start_epoch(pid):
    """Process start time as a Unix timestamp, from ps etime ([[dd-]hh:]mm:ss), independent of locale and time zone."""
    try:
        r = subprocess.run([PS, '-ww', '-p', str(pid), '-o', 'etime=,args='], capture_output=True, text=True, timeout=3).stdout.split()
    except (OSError, subprocess.SubprocessError):
        r = []
    if not r:
        # sandbox-exec (kimi, dsh under second_opinion.py) refuses to run the setuid ps, or ps printed nothing.
        # libproc gives the exact start time (None for a dead pid); without a command line the name check is
        # skipped, and the caller's 5s start match still catches a reused pid.
        info = bsd_info(pid)
        return float(info[1]) if info and info[1] else None
    # the whole command line, not the name: dsh runs as `node .../bin/dsh`
    if len(r) < 2 or not AGENT_PROC.search(' '.join(r[1:])):
        return None
    days, _, clock = r[0].rpartition('-')
    parts = [int(x) for x in clock.split(':')]
    while len(parts) < 3:
        parts.insert(0, 0)
    secs = (int(days) if days else 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
    return time.time() - secs


def is_same_claude(record):
    """True only if the registry's pid still belongs to the Claude process that wrote the record."""
    pid, started = record.get('pid'), record.get('procStart')
    if not isinstance(pid, int) or not isinstance(started, str):
        return False
    try:
        import calendar
        registry_epoch = calendar.timegm(time.strptime(started, '%a %b %d %H:%M:%S %Y'))  # procStart is written in UTC
    except ValueError:
        return False
    actual = proc_start_epoch(pid)
    return actual is not None and abs(actual - registry_epoch) <= 5


def sessions(verify=False):
    ps = {}
    for line in subprocess.run([PS, '-Ao', 'pid=,ppid=,rss='], capture_output=True, text=True, timeout=5).stdout.splitlines():
        try:
            pid, ppid, rss = map(int, line.split())
        except ValueError:
            continue
        ps[pid] = (ppid, rss)
    kids = {}
    for pid, (ppid, _) in ps.items():
        kids.setdefault(ppid, []).append(pid)

    def tree_rss(pid, seen):
        if pid in seen:
            return 0
        seen.add(pid)
        return ps.get(pid, (0, 0))[1] + sum(tree_rss(k, seen) for k in kids.get(pid, []))

    out, now = [], time.time()
    for reg in REGISTRIES:
        acct = ('codex' if '.codex' in reg else 'opencode' if 'opencode' in reg else 'dsh' if '.dsh' in reg
                else 'kimi' if '.kimi-code' in reg else 'grok' if '.grok' in reg
                else 'cx' if 'claude-exec' in reg else 'cm' if 'claude-alt' in reg else 'main')
        for f in glob.glob(reg + '/*.json'):
            try:
                s = json.load(open(f))
                pid = s.get('pid')
                if not isinstance(s, dict) or pid not in ps or (verify and not is_same_claude(s)):
                    continue
                stamp = float(s.get('statusUpdatedAt') or s.get('updatedAt') or 0)
                out.append(dict(account=acct, pid=pid, name=str(s.get('name', '')), status=str(s.get('status', '?')),
                                cwd=str(s.get('cwd', '')), file=f, idle_h=round((now - stamp / 1000) / 3600, 1),
                                idle_exact_h=(now - stamp / 1000) / 3600,  # for decisions; idle_h is for display
                                gb=round(tree_rss(pid, set()) / 1048576, 2)))
            except (ValueError, OSError, TypeError, AttributeError):
                continue
    return sorted(out, key=lambda r: -r['idle_h'])


def pretool():
    data = json.load(sys.stdin)
    if not isinstance(data, dict):
        return 0
    tool = data.get('tool_name', '')
    tool_input = data.get('tool_input') if isinstance(data.get('tool_input'), dict) else {}
    command = tool_input.get('command') if isinstance(tool_input.get('command'), str) else ''
    if tool == 'Bash' and not HEAVY_BASH.search(command):
        return 0
    mem = memory_state()
    if not mem['low']:
        return 0
    ss = sessions()
    idle = [s for s in ss if s['status'] == 'idle' and s['idle_h'] >= 6]
    reason = (f"Memory is low (pressure level {mem['level']}, swap {mem['swap_pct']}% used, {len(ss)} Claude sessions open, "
              f"{len(idle)} idle 6h+ holding {sum(s['gb'] for s in idle):.1f} GB). Do not start new agents, workflows or heavy commands now. "
              "Finish the current step inline, save progress to the project handoff file, and put this line in your end recap: "
              "'Memory low: close idle sessions with `claude-sessions close-idle 6`'.")
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny', 'permissionDecisionReason': reason}}))
    return 0


def session_start():
    mem = memory_state()
    idle = [s for s in sessions() if s['status'] == 'idle' and s['idle_h'] >= 6]
    if not mem['tight'] and len(idle) < 3:
        return 0
    msg = (f"Machine memory: pressure level {mem['level']}, swap {mem['swap_pct']}% used. {len(idle)} other Claude sessions have been idle 6h+ "
           f"({sum(s['gb'] for s in idle):.1f} GB). Keep agent fan-out small. If this is an interactive session with the user, mention `claude-sessions close-idle 6` in the end recap; in a headless run, ignore this.")
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': msg}}))
    return 0


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'list'
    if cmd in ('pretool', 'session-start'):
        try:  # a hook must never block work because the guard itself failed
            return pretool() if cmd == 'pretool' else session_start()
        except Exception as e:
            print(f'memory_guard: {type(e).__name__}: {e}', file=sys.stderr)
            return 0
    if cmd == 'list':
        mem = memory_state()
        print(f"pressure level {mem['level']} (1 ok, 2 warn, 4 critical), swap {mem['swap_pct']}% used, low={mem['low']}")
        for s in sessions():
            print(f"{s['account']:4} {s['pid']:>6} {s['status']:8} idle {s['idle_h']:5.1f}h {s['gb']:5.2f} GB  {s['name']:16} {s['cwd']}")
        return 0
    if cmd == 'close-idle':
        try:
            hours = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
        except ValueError:
            hours = -1
        if not (0 < hours < 10000):
            print('usage: claude-sessions close-idle HOURS   (HOURS must be a positive number)')
            return 2
        closed, every = 0, sessions(verify=True)
        # An OpenCode or Codex process can host several sessions, each with its own record. Signalling the pid
        # ends all of them, so a pid is closed only when every session it hosts is idle long enough.
        busy_pids = {s['pid'] for s in every if s['status'] != 'idle' or s['idle_exact_h'] < hours}
        done = set()
        for s in every:
            if s['status'] != 'idle' or s['idle_exact_h'] < hours or s['pid'] in busy_pids or s['pid'] in done:
                continue
            done.add(s['pid'])
            try:  # re-read the record and re-check identity immediately before signalling
                rec = json.load(open(s['file']))
                fresh_idle_h = (time.time() - float(rec.get('statusUpdatedAt') or rec.get('updatedAt') or 0) / 1000) / 3600
                if rec.get('status') != 'idle' or fresh_idle_h < hours or not is_same_claude(rec):
                    continue
                # re-check every session sharing this pid right before signalling, not the snapshot above
                if any(o['pid'] == s['pid'] and (o['status'] != 'idle' or o['idle_exact_h'] < hours) for o in sessions()):
                    continue
                print(f"closing {s['account']} {s['pid']} {s['name']} (idle {s['idle_h']}h, {s['gb']} GB)")
                os.kill(s['pid'], signal.SIGTERM)
                closed += 1
            except (OSError, ValueError, TypeError) as e:
                print('  skipped:', e)
        print(f"{closed} idle sessions asked to exit")
        return 0
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main())
