#!/usr/bin/env python3
"""Make SendMessage / ListAgents work across the Claude accounts.

Claude Code finds peer sessions through its own <config dir>/sessions/ folder: one <pid>.json (name, socket path,
status) and one <pid>.<hash>.key per live session. The sockets already sit in a shared /tmp/cc-socks, so a session
is reachable from another account once both files are in that account's folder too. Symlinks are ignored; real
copies work (tested 2026-09-23). This script copies every live session's pair into the other accounts' folders,
refreshes them when the original changes, and removes copies whose session has ended.

Run by launchd every 15s (com.example.session-mirror). Manual: session_mirror.py [sync|status|clean]
"""
import fcntl
import json
import os
import re
import shutil
import sys

HOME = os.path.expanduser('~')
ACCOUNTS = ['.claude', '.claude-exec']
DIRS = [os.path.join(HOME, a, 'sessions') for a in ACCOUNTS]
STATE = os.path.join(HOME, '.local/state/session-mirror.json')  # {copy path: original path}
ENTRY = re.compile(r'^(\d+)\.(json|[0-9a-f]+\.key)$')


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(copies):
    tmp = STATE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(copies, f, indent=0, sort_keys=True)
    os.replace(tmp, STATE)


def remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def sync(copies):
    # drop copies whose original is gone or whose session ended
    for dst, src in list(copies.items()):
        m = ENTRY.match(os.path.basename(dst))
        if not os.path.exists(src) or not m or not alive(int(m.group(1))):
            remove(dst)
            del copies[dst]
    for d in DIRS:
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            m = ENTRY.match(name)
            src = os.path.join(d, name)
            if not m or src in copies or os.path.islink(src) or not alive(int(m.group(1))):
                continue
            for other in DIRS:
                dst = os.path.join(other, name)
                if other == d or not os.path.isdir(other):
                    continue
                if os.path.exists(dst) and dst not in copies:
                    continue  # a real session of that account; never overwrite
                if dst in copies and os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
                    continue
                tmp = dst + '.mirror-tmp'
                shutil.copy2(src, tmp)  # keeps the key's 0600 mode
                os.replace(tmp, dst)
                copies[dst] = src


def clean(copies):
    for dst in copies:
        remove(dst)
    copies.clear()


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'sync'
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE + '.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        copies = load()
        if cmd == 'sync':
            sync(copies)
        elif cmd == 'clean':
            clean(copies)
        elif cmd == 'status':
            for dst, src in sorted(copies.items()):
                if dst.endswith('.json'):
                    print(f'{src.split(HOME + "/")[1]} -> {dst.split(HOME + "/")[1]}')
            return
        else:
            raise SystemExit(__doc__)
        save(copies)


if __name__ == '__main__':
    main()
