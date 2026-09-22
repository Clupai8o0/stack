#!/usr/bin/env python3
"""Claude Code statusline shared by all three accounts (main, cx, cm). Session JSON on stdin, one line out.

Shows: account · model · effort │ folder · branch │ context │ 5h · week │ cost · lines │ claims · memories
Colours come from palette.json (run palette.py, or kitty-accent, to change them). It runs on every render, so it
must stay fast: no network, git capped at 0.5 s, the transcript read only from its tail.

The last input is saved to ~/.local/state/statusline/last-<account>.json so new fields can be checked by hand.
"""
import json, os, re, subprocess, sys, time

HERE = os.path.dirname(os.path.realpath(__file__))
STATE = os.path.expanduser('~/.local/state')
CLAIMS_INDEX = os.environ.get('CLAIMS_STATE_DIR') or os.path.join(STATE, 'claude-claims')
RECALL_DIR = os.path.join(STATE, 'memory-recall')
EFFORT_RE = re.compile(rb'"effort":"(low|medium|high|xhigh|max)"|Set effort level to (low|medium|high|xhigh|max|ultracode)\b')
FALLBACK = {'colors': {'accent': '#cba6f7', 'crust': '#11111b', 'text': '#cdd6f4'}, 'roles': {}, 'accounts': {}}


def load_palette():
    try:
        return json.load(open(os.path.join(HERE, 'palette.json')))
    except (OSError, ValueError):
        return FALLBACK


PAL = load_palette()
RESET, BOLD = '\033[0m', '\033[1m'


def rgb(name):
    """Hex for a palette colour or role name."""
    colours = PAL.get('colors', {})
    name = PAL.get('roles', {}).get(name, name)
    h = colours.get(name) or colours.get('text') or '#cdd6f4'
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def fg(name, text, bold=False):
    r, g, b = rgb(name)
    return f'{BOLD if bold else ""}\033[38;2;{r};{g};{b}m{text}{RESET}'


def pill(bg, fgc, text):
    r, g, b = rgb(bg)
    r2, g2, b2 = rgb(fgc)
    return f'{BOLD}\033[48;2;{r};{g};{b}m\033[38;2;{r2};{g2};{b2}m {text} {RESET}'


def num(v):
    """A number from JSON that may arrive as a string or null; None when it is not one."""
    try:
        return float(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


def level(pct):
    return 'ok' if pct < 50 else ('warn' if pct < 80 else 'bad')


def account():
    d = os.path.basename((os.environ.get('CLAUDE_CONFIG_DIR') or '~/.claude').rstrip('/'))
    return {'.claude': 'main', '.claude-exec': 'cx', '.claude-alt': 'cm'}.get(d, d.lstrip('.') or 'main')


def tail(path, size):
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - size))
            return f.read()
    except OSError:
        return b''


def effort(data):
    e = data.get('effort')
    if isinstance(e, dict) and e.get('level'):
        return str(e['level'])
    if isinstance(e, str) and e:
        return e
    last = None
    for m in EFFORT_RE.finditer(tail(data.get('transcript_path') or '', 300_000)):
        last = (m.group(1) or m.group(2)).decode()
    if last:
        return 'xhigh' if last == 'ultracode' else last
    try:
        cfg = os.environ.get('CLAUDE_CONFIG_DIR') or os.path.expanduser('~/.claude')
        return json.load(open(os.path.join(cfg, 'settings.json'))).get('effortLevel')
    except (OSError, ValueError):
        return None


def context(data):
    """(tokens, percent) from context_window when Claude Code sends it, else from the transcript's last usage."""
    cw = data.get('context_window') if isinstance(data.get('context_window'), dict) else {}
    size = cw.get('context_window_size') or (1_000_000 if '1m' in str((data.get('model') or {}).get('id', '')).lower()
                                             else 200_000)
    pct = num(cw.get('used_percentage'))
    usage = cw.get('current_usage') if isinstance(cw.get('current_usage'), dict) else None
    tokens = 0
    if usage:
        tokens = sum(usage.get(k) or 0 for k in ('input_tokens', 'cache_read_input_tokens',
                                                  'cache_creation_input_tokens'))
    if not tokens:
        for line in reversed(tail(data.get('transcript_path') or '', 400_000).splitlines()):
            if b'"usage"' not in line:
                continue
            try:
                u = json.loads(line).get('message', {}).get('usage')
            except ValueError:
                continue
            if u:
                tokens = sum(u.get(k) or 0 for k in ('input_tokens', 'cache_read_input_tokens',
                                                      'cache_creation_input_tokens'))
                break
    if pct is None and tokens:
        pct = tokens / size * 100
    return tokens, (min(100, round(pct)) if pct is not None else None)


def reset_in(ts):
    try:
        secs = float(ts) - time.time()
    except (TypeError, ValueError):
        return ''
    if secs <= 0:
        return ''
    d, h, m = int(secs // 86400), int(secs % 86400 // 3600), int(secs % 3600 // 60)
    return f'{d}d{h}h' if d else (f'{h}h{m:02d}' if h else f'{m}m')


def claims_held(sid):
    """Files this session has claimed across every repo it touched (claims.py's per-session store index)."""
    if not sid:
        return 0
    try:
        stores = open(os.path.join(CLAIMS_INDEX, f'{os.path.basename(sid)}.stores')).read().splitlines()
    except OSError:
        return 0
    n = 0
    for store in stores:
        try:
            n += sum(1 for c in json.load(open(store)).get('claims') or []
                     if isinstance(c, dict) and c.get('session') == sid)
        except (OSError, ValueError, AttributeError):
            continue
    return n


def memories_recalled(sid):
    try:
        got = json.load(open(os.path.join(RECALL_DIR, f'{os.path.basename(sid)}.json')))
        return len(got) if isinstance(got, list) else 0
    except (OSError, ValueError, TypeError):
        return 0


def git(cwd, *args):
    try:
        r = subprocess.run(['git', '-C', cwd, *args], capture_output=True, text=True, timeout=0.5)
        return r.stdout.strip() if r.returncode == 0 else ''
    except (OSError, subprocess.SubprocessError):
        return ''


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except ValueError:
        data = {}
    acct = account()
    try:
        os.makedirs(os.path.join(STATE, 'statusline'), exist_ok=True)
        with open(os.path.join(STATE, 'statusline', f'last-{acct}.json'), 'w') as f:
            f.write(raw)
    except OSError:
        pass
    sid = data.get('session_id') or ''
    sep = fg('sep', ' │ ')
    groups = []

    # account · model · effort
    model = (data.get('model') or {}).get('display_name') or 'Claude'
    head = [pill(PAL.get('accounts', {}).get(acct, 'accent'), 'badge_fg', acct), fg('model', f'󰚩 {model}', True)]
    lvl = effort(data)
    if lvl:
        head.append(fg('effort', f'󱐋 {lvl}'))
    groups.append(' '.join(head))

    # folder · branch
    cwd = (data.get('workspace') or {}).get('current_dir') or data.get('cwd') or os.getcwd()
    where = [fg('dir', ' ' + ('~' if cwd == os.path.expanduser('~') else os.path.basename(cwd.rstrip('/')) or cwd))]
    branch = git(cwd, 'rev-parse', '--abbrev-ref', 'HEAD')
    if branch:
        dirty = git(cwd, 'status', '--porcelain', '--untracked-files=no')
        where.append(fg('branch', f' {branch}') + (fg('dirty', ' ●') if dirty else fg('clean', ' ✓')))
    groups.append(' '.join(where))

    # context
    tokens, pct = context(data)
    if pct is not None:
        k = f'{tokens / 1000:.0f}k ' if tokens >= 1000 else ''
        groups.append(fg(level(pct), f'󰘦 {k}{pct}%'))

    # 5-hour and weekly subscription limits
    limits = []
    for key, label in (('five_hour', '󱎫 5h'), ('seven_day', '󰃭 wk')):
        lim = (data.get('rate_limits') or {}).get(key) or {}
        used = num(lim.get('used_percentage'))
        if used is None:
            continue
        when = reset_in(lim.get('resets_at'))
        limits.append(fg(level(used), f'{label} {round(used)}%') + (fg('dim', f' {when}') if when else ''))
    if limits:
        groups.append(' '.join(limits))

    # cost · lines
    cost = data.get('cost') or {}
    spend = []
    usd = num(cost.get('total_cost_usd'))
    if usd is not None:
        spend.append(fg('cost', f'${usd:.2f}'))
    add, rem = int(num(cost.get('total_lines_added')) or 0), int(num(cost.get('total_lines_removed')) or 0)
    if add or rem:
        spend.append(fg('ok', f'+{add}') + fg('dim', '/') + fg('bad', f'-{rem}'))
    if spend:
        groups.append(' '.join(spend))

    # shared layer: claims held, memories recalled
    layer = []
    n = claims_held(sid)
    if n:
        layer.append(fg('claims', f'󰌾 {n}'))
    m = memories_recalled(sid)
    if m:
        layer.append(fg('memory', f'󰧑 {m}'))
    if layer:
        groups.append(' '.join(layer))

    sys.stdout.write(sep.join(groups))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:  # a broken statusline must never break the session; show why instead
        sys.stdout.write(f'statusline: {type(e).__name__}: {e}')
