#!/usr/bin/env python3
"""Hand a task to the cx account (~/.claude-exec) as a headless Claude run, so it bills cx's usage, not main's.

Usage:
  cx_run.py PROMPT_FILE|- [--cwd DIR] [--model M] [--effort E] [--read-only] [--timeout SECS] [--max-budget-usd N]

  PROMPT_FILE     the task; '-' reads it from stdin. Write it like a subagent brief: goal, files, done-when.
  --cwd           where cx works (default: current dir). Use a worktree when main is editing the same repo.
  --model/effort  default claude-opus-5-5 at low, the token-economy default worker.
  --read-only     cx may read and run read-only tools but not edit (permission mode plan).

cx runs with the shared hooks, so its edits are claimed in claims.py like any session: do not edit the
same files from main while it runs. Run it as a background Bash task; it prints cx's final reply,
then one line with cost, turns and session id (resume with: cx --resume ID). Full JSON goes to a log.
The run is reachable by SendMessage (session_mirror.py), but a message ends its turn early: don't message it.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time

CX_DIR = os.path.expanduser('~/.claude-exec')
LOG_DIR = os.path.join(tempfile.gettempdir(), 'cx-run')
# the calling session's identity (session id, messaging socket, effort, config dir): a cx run inheriting
# them would be logged by the hooks as main's session. Keep only settings that are the same in every account.
KEEP_ENV = {'CLAUDE_CODE_AUTO_COMPACT_WINDOW'}


def drop_env(key):
    return key.startswith(('CLAUDE', 'ANTHROPIC_API_KEY')) and key not in KEEP_ENV


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('prompt_file')
    ap.add_argument('--cwd', default=os.getcwd())
    ap.add_argument('--model', default='claude-opus-5-5')
    ap.add_argument('--effort', default='low', choices=['low', 'medium', 'high', 'xhigh', 'max'])
    ap.add_argument('--read-only', action='store_true')
    ap.add_argument('--timeout', type=int, default=3600)
    ap.add_argument('--max-budget-usd', type=float)
    a = ap.parse_args()

    prompt = sys.stdin.read() if a.prompt_file == '-' else open(a.prompt_file).read()
    if not prompt.strip():
        raise SystemExit('empty prompt')
    cwd = os.path.abspath(os.path.expanduser(a.cwd))
    if not os.path.isdir(cwd):
        raise SystemExit(f'no such dir: {cwd}')

    cmd = ['claude', '-p', '--model', a.model, '--effort', a.effort, '--output-format', 'json',
           '--permission-mode', 'plan' if a.read_only else 'bypassPermissions']
    if a.max_budget_usd:
        cmd += ['--max-budget-usd', str(a.max_budget_usd)]
    env = {k: v for k, v in os.environ.items() if not drop_env(k)}
    env['CLAUDE_CONFIG_DIR'] = CX_DIR

    os.makedirs(LOG_DIR, exist_ok=True)
    log = os.path.join(LOG_DIR, time.strftime('%Y%m%d-%H%M%S') + f'-{os.getpid()}.json')
    # own process group, so a timeout also kills the tools cx started
    p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = p.communicate(prompt, timeout=a.timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.communicate()
        raise SystemExit(f'cx timed out after {a.timeout}s')
    with open(log, 'w') as f:
        f.write(out)

    try:
        res = json.loads(out)
    except json.JSONDecodeError:
        sys.stderr.write(err[-2000:] or out[-2000:])
        raise SystemExit(f'cx exited {p.returncode} without JSON (log: {log})')
    print(res.get('result', ''))
    print(f"\n[cx] {res.get('subtype', '?')}, ${res.get('total_cost_usd', 0):.2f}, "
          f"{res.get('num_turns', '?')} turns, session {res.get('session_id', '?')}, log {log}")
    sys.exit(1 if res.get('is_error') else 0)


if __name__ == '__main__':
    main()
