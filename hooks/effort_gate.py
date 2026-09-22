#!/usr/bin/env python3
"""UserPromptSubmit hook: make Claude state the effort a task needs before it starts work.

Early in an interactive session (small context, so switching effort is cheap), Claude stops and asks the user to
run /effort when the task needs more effort than the session has, or much less on a multi-step task. Later in a
session, switching would drop the prompt cache, so Claude only states the line and carries on. Headless runs never stop.
Short follow-ups ("go", "continue", "status?") and background notifications are skipped. Any failure lets the prompt
through untouched.

The session effort comes from the hook input when Claude Code sends it (newer versions), else from the last assistant
entry in the transcript, else from a --effort flag on the parent process, else from settings effortLevel.
"""
import json, os, re, subprocess, sys

LADDER = ['low', 'medium', 'high', 'xhigh', 'max']
SKIP = {'go', 'continue', 'yes', 'y', 'ok', 'okay', 'status', 'status?', 'thanks', 'thank you', 'proceed', 'carry on',
        'next', 'done', 'retry', 'try again', 'keep going', 'sounds good', 'do it', 'ship it', 'looks good'}
NOTIFICATION = re.compile(r'<task-notification>|\[SYSTEM NOTIFICATION|^<system-reminder>|^<local-command', re.I)
EARLY_TRANSCRIPT_BYTES = 400_000   # roughly the first ~100k tokens of a session
# Either an assistant entry's effort field or the output of a /effort command, whichever comes last.
EFFORT_IN_TRANSCRIPT = re.compile(rb'"effort":"(low|medium|high|xhigh|max)"|Set effort level to (low|medium|high|xhigh|max|ultracode)\b')
ALIASES = {'ultracode': 'xhigh'}


def effort_from_transcript(path):
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 300_000))
            last = None
            for m in EFFORT_IN_TRANSCRIPT.finditer(f.read()):
                last = (m.group(1) or m.group(2)).decode()
        return ALIASES.get(last, last)
    except OSError:
        return None


def effort_from_parent():
    """Look for --effort on the nearest claude ancestor (the hook may run directly under claude or under a shell)."""
    try:
        pid = os.getppid()
        for _ in range(4):
            out = subprocess.run(['/bin/ps', '-ww', '-o', 'ppid=,args=', '-p', str(pid)], capture_output=True, text=True, timeout=1).stdout.strip()
            if not out:
                return None
            ppid, _, args = out.partition(' ')
            if re.search(r'(^|/)claude(\s|$)', args.split(' --')[0]):
                m = re.search(r'--effort[= ](low|medium|high|xhigh|max)\b', args)
                return m.group(1) if m else None
            pid = int(ppid.strip() or 0)
            if pid <= 1:
                return None
        return None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def effort_from_settings(cwd):
    config_dir = os.environ.get('CLAUDE_CONFIG_DIR') or os.path.expanduser('~/.claude')
    for p in (os.path.join(cwd, '.claude', 'settings.local.json'), os.path.join(cwd, '.claude', 'settings.json'),
              os.path.join(config_dir, 'settings.json')):
        try:
            level = json.load(open(p)).get('effortLevel')
            if level in LADDER:
                return level
        except (OSError, ValueError, AttributeError):
            continue
    return None


def main():
    data = json.load(sys.stdin)
    if not isinstance(data, dict) or data.get('agent_id'):
        return 0
    text = (data.get('prompt') if isinstance(data.get('prompt'), str) else '').strip()
    if (not text or NOTIFICATION.search(text[:400]) or text.startswith('/')
            or text.lower().rstrip('.!') in SKIP or len(text.split()) < 3):
        return 0
    effort = data.get('effort') if isinstance(data.get('effort'), dict) else {}
    path = data.get('transcript_path') if isinstance(data.get('transcript_path'), str) else ''
    cwd = data.get('cwd') if isinstance(data.get('cwd'), str) else os.getcwd()
    level = (effort.get('level') if effort.get('level') in LADDER else None) \
        or effort_from_transcript(path) or effort_from_parent() or effort_from_settings(cwd) or 'unknown'
    try:
        early = os.path.getsize(path) < EARLY_TRANSCRIPT_BYTES
    except FileNotFoundError:
        early = True           # the transcript is created after the first prompt, so a missing file means a new session
    except OSError:
        early = False
    attended = os.environ.get('CLAUDE_CODE_SESSION_ATTENDED') == '1' or os.environ.get('EFFORT_GATE_FORCE_ATTENDED') == '1'

    msg = [
        f'REQUIRED FIRST LINE. Session effort is {level}. Your reply must begin with exactly this line before anything else, '
        f'including before any tool call: "Effort: {level} now, <needed> needed (<reason in 6 words or fewer>)."',
        'Pick <needed>: low = lookup, status, one-line edit; medium = routine edit or small script; high = normal feature, '
        'bug fix, review or research; xhigh = unclear scope, planning, architecture, hard debugging, or text a reviewer, '
        'client or mentor reads; max = almost never.',
    ]
    if not attended:
        msg.append('This is a headless run: never stop. State the line and continue.')
    elif early:
        msg.append(f'The context is still small, so switching is cheap. If <needed> is higher than {level}, or at least two '
                   'steps lower on a task that will take many steps, stop right after that line and add one more line: '
                   '"Run /effort <needed>, then send: go". Do no other work and ask nothing else. Otherwise continue.')
    else:
        msg.append('The context is already large, and switching effort would drop the prompt cache. Do not stop. Continue, '
                   'give each subagent the effort its step needs, and if follow-up work belongs at a different effort, say so '
                   'in the end recap (start it in a fresh session).')
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': ' '.join(msg)}}))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:  # never block a prompt because the gate failed
        print(f'effort_gate: {type(e).__name__}: {e}', file=sys.stderr)
        sys.exit(0)
