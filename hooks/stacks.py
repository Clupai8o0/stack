#!/usr/bin/env python3
"""Shared dev stacks with leases, so parallel sessions reuse one Docker stack per project and nothing runs for days.

Stacks are defined in ../stacks.json (compose dir, files, project, services, after_up). Leases live in
~/.local/state/claude-claims/stacks.json and belong to a Claude process: a lease lasts while that process runs
(through /clear), until down, or lease_max_h hours for a lease taken outside a session. Machine-wide, shared by
all accounts and agents.

  stacks.py up NAME ["why"]      take a lease; start the stack (and the Docker runtime) if it is not running,
                                 wait for healthy, run after_up. Already running: just lease it and print ports.
  stacks.py down NAME            drop your lease; the last lease out stops the stack (docker compose stop, data kept)
                                 if stacks.py started it
  stacks.py list                 stacks, whether running, who holds them, and containers no stack owns
  stacks.py add NAME --dir DIR [-f FILE ...] [--project P] [--service S ...] [--after-up CMD] [--note TEXT]
  stacks.py reap [--dry-run]     launchd, every 10 min (com.example.stacks-reaper):
                                 - stops a stack that `up` started and nobody leases, after grace_min minutes idle
                                 - stops any other container after unmanaged_h hours up (a configured stack started
                                   by hand counts as "other"; names in keep, restart policies, dev containers and
                                   Kubernetes containers are left alone)
                                 - kills orphaned dev servers (ng serve, metro, firebase emulators, vite, next dev ...)
                                   whose parent is gone, after orphan_min minutes, unless a live session launched it
                                   (claims.py records dev-server commands) or works in the same repo
                                 - on colima, stops the VM once nothing has run for grace_min minutes
Stopping keeps containers and volumes, so the next up is quick. Log: ~/.local/state/claude-claims/stacks.log
"""
import argparse, contextlib, fcntl, glob, json, os, re, shlex, signal, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claims  # noqa: E402  session lookup, liveness and locking are shared with the claims hook

CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'stacks.json')
STATE = os.path.join(claims.INDEX_DIR, 'stacks.json')
LAUNCHES = os.path.join(claims.INDEX_DIR, 'dev-launches.json')
LOG = os.path.join(claims.INDEX_DIR, 'stacks.log')
DOCKER = os.environ.get('STACKS_DOCKER', 'docker')  # tests point this at a stub
CONTEXTS = {'docker-desktop': 'desktop-linux', 'orbstack': 'orbstack', 'colima': 'colima'}
CTX = []  # ['--context', NAME] for the configured runtime, set by load_config
DEV_SERVER = re.compile(r'firebase(\.js)? emulators:(start|exec)|\bng serve\b|react-native start|expo start|'
                        r'\bvite\b|next dev|nodemon|webpack(-dev-server| serve)|rails s(erver)?\b|\bpuma\b|'
                        r'agent-browser-darwin')  # agent-browser's daemon + its headless Chrome, left open
# commands that start a dev server through a script: recorded as launches too
LAUNCH = re.compile(DEV_SERVER.pattern + r'|\b(npm|pnpm|yarn|bun)( run)? (dev|start|serve|functions|emulators?)\b')
WRAPPER = re.compile(r'(\S*/)?(sh|bash|zsh|dash|env|nohup|node|npm|npx|yarn|pnpm|bun|java|ruby|bundle)(\s|$)')
SCRIPT = re.compile(r'(\S*/)?(sh|bash|zsh)\s+(?!-)\S')  # a shell running a script file, not a `-c` wrapper
HEALTH_WAIT = 240


class DockerError(OSError):
    """A docker call failed, so we do not know; never read it as "not running"."""


class State(claims.Store):
    KEYS = ()
    LOCK_WAIT = 15  # docker calls never run under this lock, so a wait means a slow disk, not a busy reaper

    def __init__(self, readonly=False):
        os.makedirs(claims.INDEX_DIR, exist_ok=True)
        super().__init__(STATE)
        self.readonly = readonly

    def __exit__(self, exc_type, *exc):
        return super().__exit__(RuntimeError if self.readonly else exc_type, *exc)  # readonly: write nothing back

    def __enter__(self):
        super().__enter__()
        for key in ('leases', 'idle_since', 'started', 'device_seen'):
            if not isinstance(self.data.get(key), dict):
                self.data[key] = {}
        return self

    def prune(self, cfg):
        cutoff = time.time() - cfg.get('lease_max_h', 8) * 3600
        live = claims.live_sessions()
        for name in list(self.data['leases']):
            ls = [l for l in self.data['leases'][name] if isinstance(l, dict) and alive(l, live, cutoff)]
            if ls:
                self.data['leases'][name] = ls
            else:
                del self.data['leases'][name]


class Launches(claims.Store):
    KEYS = ('launches',)
    LOCK_WAIT = 1  # written from a PreToolUse hook: never hold up a tool call

    def __init__(self):
        os.makedirs(claims.INDEX_DIR, exist_ok=True)
        super().__init__(LAUNCHES)


_SAME = {}  # (pid, procStart) -> still that Claude process; one ps per process per run


def alive(owner, live, cutoff):
    """A lease or launch record still has an owner: its Claude process runs (same pid and start time, so /clear
    keeps it), or its session is live; a manual one lasts until cutoff."""
    if owner.get('session') == 'manual':
        return owner.get('at', 0) > cutoff
    if isinstance(owner.get('pid'), int):
        key = (owner['pid'], owner.get('procStart'))
        if key not in _SAME:
            _SAME[key] = claims.is_same_claude(owner)
        if _SAME[key]:
            return True
    return owner.get('session') in live


def load_config():
    try:
        with open(CONFIG) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {'runtime': 'docker-desktop', 'stacks': {}}
    if 'STACKS_DOCKER' not in os.environ:
        ctx = cfg.get('context') or CONTEXTS.get(cfg.get('runtime', 'docker-desktop'))
        CTX[:] = ['--context', ctx] if ctx else []
    return cfg


def save_config(cfg):
    tmp = CONFIG + f'.{os.getpid()}.tmp'
    with open(tmp, 'w') as f:
        json.dump(cfg, f, indent=1)
        f.write('\n')
    os.replace(tmp, CONFIG)


def log(text):
    with open(LOG, 'a') as f:
        f.write(f'{claims.stamp(time.time())} {text}\n')


def run(args, cwd=None, timeout=600):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, errors='replace', timeout=timeout)


def docker(*args, timeout=600, cwd=None):
    return run([DOCKER, *CTX, *args], cwd=cwd, timeout=timeout)


def compose(st, *args, timeout=600):
    files = [x for f in st['files'] for x in ('-f', f)]
    return docker('compose', '-p', st['project'], *files, *args, cwd=st['dir'], timeout=timeout)


@contextlib.contextmanager
def stack_lock(name, wait):
    """One start or stop at a time per stack, across sessions and the reaper. Yields False if busy after wait s."""
    os.makedirs(claims.INDEX_DIR, exist_ok=True)
    with open(os.path.join(claims.INDEX_DIR, f'stacks-{re.sub(r"[^A-Za-z0-9_.-]", "_", name)}.lock'), 'a') as f:
        deadline = time.time() + wait
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    yield False
                    return
                time.sleep(1)
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def daemon_up():
    try:
        return docker('info', '--format', '{{.ServerVersion}}', timeout=15).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def start_runtime(cfg):
    if daemon_up():
        return True
    rt = cfg.get('runtime', 'docker-desktop')
    start = {'docker-desktop': ['open', '-g', '-a', 'Docker'], 'orbstack': ['orb', 'start'],
             'colima': ['colima', 'start', *cfg.get('colima_args', [])]}[rt]
    print(f'starting {rt} ...', flush=True)
    subprocess.Popen(start, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 180
    while time.time() < deadline:
        if daemon_up():
            return True
        time.sleep(3)
    return False


def containers(project=None):
    """[(name, id, status, compose project)] of running containers."""
    args = ['ps', '--format', '{{.Names}}\t{{.ID}}\t{{.Status}}\t{{.Label "com.docker.compose.project"}}']
    if project:
        args += ['--filter', f'label=com.docker.compose.project={project}']
    r = docker(*args, timeout=20)
    if r.returncode != 0:
        raise DockerError(f'docker ps failed: {r.stderr.strip()[:200]}')
    return [tuple(line.split('\t')) for line in r.stdout.splitlines() if line.count('\t') == 3]


def stack_running(st):
    wanted = set(st.get('services') or [])
    if not wanted:
        return bool(containers(st['project']))
    r = compose(st, 'ps', '--services', '--status', 'running', timeout=30)
    if r.returncode != 0:
        raise DockerError(f'compose ps failed: {r.stderr.strip()[:200]}')
    return bool(wanted & set(r.stdout.split()))


def wait_healthy(st):
    """Every service running (and healthy if it has a check), or exited 0 (a one-shot job). A crash fails at once."""
    deadline, rows = time.time() + HEALTH_WAIT, []
    while time.time() < deadline:
        r = compose(st, 'ps', '-a', '--format', '{{.Service}}\t{{.State}}\t{{.Health}}\t{{.ExitCode}}',
                    *(st.get('services') or []), timeout=30)
        rows = [line.split('\t') for line in r.stdout.splitlines() if line.count('\t') == 3]
        if any(s == 'dead' or (s == 'exited' and c != '0') for _, s, _, c in rows):
            return False, rows
        if any(s == 'running' for _, s, _, _ in rows) and all(
                (s == 'running' and h in ('', 'healthy')) or (s == 'exited' and c == '0') for _, s, h, c in rows):
            return True, rows
        time.sleep(3)
    return False, rows


def ports(st):
    r = compose(st, 'ps', '--format', '{{.Service}}: {{.Ports}}', *(st.get('services') or []), timeout=30)
    return r.stdout.strip()


def owner():
    """This command's owner: the Claude process's registry record (pid, procStart, session, name), or manual."""
    sid = claims.current_session()
    if sid:
        for reg in claims.REGISTRIES:
            for f in glob.glob(os.path.join(reg, '*.json')):
                try:
                    d = json.load(open(f))
                except (OSError, ValueError):
                    continue
                if isinstance(d, dict) and d.get('sessionId') == sid and isinstance(d.get('pid'), int):
                    return {'session': sid, 'pid': d['pid'], 'procStart': d.get('procStart'),
                            'name': d.get('name') or str(d['pid'])}
        return {'session': sid, 'name': sid[:8]}
    return {'session': 'manual', 'name': 'manual'}


def same_owner(a, b):
    if a.get('pid') and b.get('pid'):
        return a['pid'] == b['pid'] and a.get('procStart') == b.get('procStart')
    return a.get('session') == b.get('session')


def up(name, why):
    cfg = load_config()
    if name in cfg.get('devices', {}):
        try:
            return device_up(cfg, name, why)
        except DeviceMissing as e:
            print(e)
            return 1
    st = cfg['stacks'].get(name)
    if not st:
        print(f'no stack {name}. Known: {", ".join(cfg["stacks"])}. Add one with: stacks.py add')
        return 2
    me = owner()
    with State() as s:
        s.prune(cfg)
        ls = [l for l in s.data['leases'].get(name, []) if not same_owner(l, me)]
        s.data['leases'][name] = ls + [{**me, 'why': (why or '')[:200], 'at': time.time()}]
        s.data['idle_since'].pop(name, None)
    if not start_runtime(cfg):
        print(f'{cfg.get("runtime")} did not start within 3 minutes. Lease kept; run up again once docker info works.')
        return 1
    with stack_lock(name, wait=1200) as got:  # another session may be starting it: wait, then reuse its copy
        if not got:
            print(f'{name}: another start has held the lock for 20 min. Lease kept; check stacks.py list.')
            return 1
        if stack_running(st):
            with State() as s:
                managed = name in s.data['started']
            others = ', '.join(l['name'] for l in ls) or 'nobody else'
            print(f'{name} is already running (shared with {others}). Leased; do not start another copy.')
            if not managed:
                print('It was started outside stacks.py, so down will not stop it.')
            print(ports(st))
            print(st.get('note', ''))
            return 0
        print(f'starting {name} ...', flush=True)
        r = compose(st, 'up', '-d', *(st.get('services') or []))
        if r.returncode != 0:
            print(r.stderr[-2000:])
            return 1
        with State() as s:
            s.data['started'][name] = {'by': me['name'], 'at': time.time()}
        ok, rows = wait_healthy(st)
        log(f'up {name} by {me["name"]}: {"healthy" if ok else "NOT healthy"}')
        if not ok:
            print(f'{name} not healthy after {HEALTH_WAIT}s: {rows}. Check: docker compose -p {st["project"]} logs')
            return 1
        if st.get('after_up'):
            a = subprocess.run(st['after_up'], shell=True, cwd=st['dir'], capture_output=True, text=True,
                               errors='replace', timeout=900)
            print(f'after_up ({st["after_up"]}): {"ok" if a.returncode == 0 else "FAILED"}')
            if a.returncode != 0:
                print((a.stdout + a.stderr)[-1500:])
    print(ports(st))
    print(st.get('note', ''))
    print(f'When done: python3 {os.path.abspath(__file__)} down {name}')
    return 0


def stop_stack(name, st, reason):
    r = compose(st, 'stop', *(st.get('services') or []), timeout=300)
    log(f'stop {name}: {reason}' + ('' if r.returncode == 0 else f' FAILED {r.stderr[-300:]}'))
    if r.returncode == 0:
        with State() as s:
            s.data['started'].pop(name, None)
            s.data['idle_since'].pop(name, None)
    return r.returncode == 0


def down(name):
    cfg = load_config()
    if name in cfg.get('devices', {}):
        try:
            return device_down(cfg, name)
        except DeviceMissing as e:
            print(e)
            return 1
    st = cfg['stacks'].get(name)
    if not st:
        print(f'no stack {name}')
        return 2
    me = owner()
    with State() as s:
        s.prune(cfg)
        ls = [l for l in s.data['leases'].get(name, []) if not same_owner(l, me)]
        if ls:
            s.data['leases'][name] = ls
        else:
            s.data['leases'].pop(name, None)
        managed = name in s.data['started']
    if ls:
        print(f'lease dropped; {name} keeps running for {", ".join(l["name"] for l in ls)}')
        return 0
    if not (daemon_up() and stack_running(st)):
        print(f'lease dropped; {name} was not running')
        return 0
    if not managed:
        print(f'lease dropped; {name} was started outside stacks.py, so it is left running')
        return 0
    with stack_lock(name, wait=600) as got:
        with State() as s:
            s.prune(cfg)
            if s.data['leases'].get(name):  # someone leased it while we waited
                print(f'lease dropped; {name} was leased again, so it keeps running')
                return 0
        if got and stop_stack(name, st, f'last lease dropped by {me["name"]}'):
            print(f'{name} stopped (containers and data kept)')
        else:
            print(f'{name}: stop failed or lock busy; the reaper will retry')
    return 0


def managed_projects(cfg, started):
    return {cfg['stacks'][n]['project'] for n in started if n in cfg['stacks']}


def show():
    cfg = load_config()
    with State() as s:
        s.prune(cfg)
        leases, idle, started = s.data['leases'], s.data['idle_since'], s.data['started']
    up_ = daemon_up()
    print(f'runtime: {cfg.get("runtime")} ({"up" if up_ else "down"}), context {CTX[1] if CTX else "default"}')
    for name, st in cfg['stacks'].items():
        try:
            state = ('running' if stack_running(st) else 'stopped') if up_ else 'stopped'
        except (DockerError, subprocess.TimeoutExpired):
            state = 'unknown (docker call failed)'
        if state == 'running' and name not in started:
            state = 'running (started by hand: stopped only after unmanaged_h)'
        held = ', '.join(f'{l["name"]} ({l["why"][:40]})' if l.get('why') else l['name'] for l in leases.get(name, []))
        extra = f', idle {int((time.time() - idle[name]) / 60)} min' if name in idle and state == 'running' else ''
        print(f'  {name:22} {state:8} {("held by " + held) if held else "no lease"}{extra}')
    for name, dev in cfg.get('devices', {}).items():
        try:
            state = 'booted' if device_running(dev) else 'off'
        except (subprocess.SubprocessError, OSError, ValueError, DeviceMissing):
            state = 'unknown'
        if state == 'booted' and name not in started:
            state = 'booted (by hand)'
        held = ', '.join(l['name'] for l in leases.get(name, []))
        print(f'  {name:22} {state:8} {("held by " + held) if held else "no lease"}  [{dev["kind"]} device]')
    if up_:
        own = {st['project'] for st in cfg['stacks'].values()}  # listed above already
        for n, _, status, proj in [c for c in containers() if c[3] not in own]:
            print(f'  (no stack) {n:30} {status}  project={proj or "-"}')
    return 0


def add(a):
    cfg = load_config()
    d = os.path.abspath(os.path.expanduser(a.dir))
    cfg['stacks'][a.name] = {'dir': d, 'files': a.file or ['docker-compose.yml'],
                             'project': a.project or os.path.basename(d), 'services': a.service or [],
                             'after_up': a.after_up or '', 'note': a.note or ''}
    save_config(cfg)
    print(f'added {a.name}: {cfg["stacks"][a.name]}')
    return 0


def up_minutes(status):
    """Minutes up from a docker ps status like 'Up 4 days (healthy)'; None when not up."""
    m = re.match(r'Up (About )?(an?|\d+) (second|minute|hour|day|week|month)s?', status)
    if not m:
        return 0 if status.startswith('Up Less than') else None
    n = 1 if m.group(2) in ('a', 'an') else int(m.group(2))
    return n * {'second': 1 / 60, 'minute': 1, 'hour': 60, 'day': 1440, 'week': 10080, 'month': 43200}[m.group(3)]


def repo_root(path):
    try:
        return claims.git(path, 'rev-parse', '--show-toplevel') if path and os.path.isdir(path) else None
    except (subprocess.SubprocessError, OSError):
        return None


def launch_dirs(command, cwd):
    """The folder a dev-server command runs in: its cwd and any `cd DIR` / `--prefix DIR` / `--dir DIR` in it."""
    dirs = [cwd]
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        words = command.split()
    for i, w in enumerate(words[:-1]):
        if w in ('cd', 'pushd', '--prefix', '--dir', '--cwd', '-C'):
            d = os.path.expanduser(words[i + 1])
            dirs.append(os.path.normpath(os.path.join(cwd, d)))
    return [d for d in dict.fromkeys(dirs) if d and os.path.isdir(d)]


def note_launch(sid, command, cwd):
    """claims.py's PreToolUse hook calls this for Bash commands that look like a dev server, so the reaper knows a
    live session owns what it started, wherever it started it. Fast and silent: never blocks a tool call."""
    if not LAUNCH.search(command) or re.match(r'\s*(grep|rg|git|cat|echo|sed|ls|find|ps|pgrep|pkill|kill|lsof)\b', command):
        return
    me = owner()
    if me.get('session') != sid:
        me = {'session': sid, 'name': sid[:8]}
    rec = {**me, 'dirs': launch_dirs(command, cwd), 'at': time.time()}
    live = claims.live_sessions()
    with Launches() as s:
        keep = [l for l in s.data['launches'] if isinstance(l, dict) and alive(l, live, 0)
                and not (same_owner(l, rec) and l.get('dirs') == rec['dirs'])]
        s.data['launches'] = keep[-100:] + [rec]


def owned_dirs(live_sessions):
    """Folders live sessions work in: where they launched dev servers, the repos they edit, and where they started."""
    dirs = []
    try:
        Launches.LOCK_WAIT = 15  # the reaper can wait; the hook cannot
        with Launches() as s:
            ls = [l for l in s.data['launches'] if isinstance(l, dict) and alive(l, live_sessions, 0)]
            s.data['launches'] = ls
        dirs += [d for l in ls for d in l.get('dirs', [])]
    except TimeoutError:
        return None
    for sid in live_sessions:
        dirs += list(claims.session_stores(sid))  # the git dirs of repos this session edited
    for reg in claims.REGISTRIES:
        for f in glob.glob(os.path.join(reg, '*.json')):
            try:
                d = json.load(open(f))
                if d.get('sessionId') in live_sessions and d.get('cwd'):
                    dirs.append(d['cwd'])
            except (OSError, ValueError, AttributeError):
                continue
    out, home = set(), os.path.expanduser('~')
    for d in dirs:
        if not d or home.startswith(d.rstrip('/') + '/') or d.rstrip('/') == home:
            continue  # a session started in ~ owns nothing in particular
        d = os.path.dirname(d) if d.endswith('.json') else d  # a claims store: <git common dir>/claude-claims.json
        d = d[:-len('/.git')] if d.endswith('/.git') else d
        out.add(repo_root(d) or d)
    return out


def orphans(cfg):
    """Dev servers left behind by a finished session: the process and every ancestor up to launchd is a dev server,
    shell or package runner (so no terminal, editor or agent owns it), older than orphan_min, its folder is known,
    and no live session launched it or works in the same repo. Returns only the top of each such tree."""
    out = run(['/bin/ps', '-axo', 'pid=,ppid=,etime=,command='], timeout=10).stdout
    procs = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4:
            procs[int(parts[0])] = (int(parts[1]), parts[2], parts[3])
    jobs = launchd_jobs()
    roots = {}
    for pid, (_, _, cmd) in procs.items():
        if not DEV_SERVER.search(cmd):
            continue
        chain, p = [], pid
        while p in procs and p != 1:
            chain.append(p)
            p = procs[p][0]
        if p != 1 or not all(DEV_SERVER.search(procs[c][2]) or WRAPPER.match(procs[c][2]) for c in chain):
            continue
        if chain[-1] in jobs or SCRIPT.match(procs[chain[-1]][2]) or any(k in procs[c][2] for c in chain for k in cfg.get('protect', [])):
            continue  # a live script (a launchd job, a nightly run) still owns it, or it is protected in stacks.json
        roots[chain[-1]] = pid
    roots = {r: p for r, p in roots.items() if etime_min(procs[r][1]) >= cfg.get('orphan_min', 60)}
    if not roots:
        return []
    owned = owned_dirs(claims.live_sessions())
    if owned is None:
        return []  # cannot tell who owns what right now: kill nothing
    found = []
    for root, server in roots.items():
        cwd = proc_cwd(root)
        if not cwd or cwd == '/':
            continue  # unknown folder: cannot rule out a live owner
        repo = repo_root(cwd) or cwd
        if any(repo == r or repo.startswith(r + '/') or r.startswith(repo + '/') for r in owned if r and r != '/'):
            continue
        found.append((root, etime_min(procs[root][1]), cwd, procs[server][2][:120]))
    return found


def launchd_jobs():
    """pids launchd runs as jobs (their parent is pid 1 too, but they are not orphans)."""
    out = run(['launchctl', 'list'], timeout=10).stdout
    return {int(l.split()[0]) for l in out.splitlines()[1:] if l.split() and l.split()[0].isdigit()}


def etime_min(e):
    days, _, rest = e.rpartition('-')
    bits = [int(x) for x in rest.split(':')]
    while len(bits) < 3:
        bits.insert(0, 0)
    return (int(days or 0) * 1440) + bits[0] * 60 + bits[1] + bits[2] / 60


def proc_cwd(pid):
    try:
        r = run(['lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'], timeout=10)
    except subprocess.TimeoutExpired:
        return None
    return next((l[1:] for l in r.stdout.splitlines() if l.startswith('n')), None)


def descendants(pid):
    out = run(['/bin/ps', '-axo', 'pid=,ppid='], timeout=10).stdout
    kids = {}
    for line in out.splitlines():
        p, pp = (int(x) for x in line.split())
        kids.setdefault(pp, []).append(p)
    todo, seen = [pid], []
    while todo:
        p = todo.pop()
        seen.append(p)
        todo += kids.get(p, [])
    return seen


def protected_container(cid):
    """Restart policies, dev containers and Kubernetes containers are meant to stay up."""
    r = docker('inspect', '--format', '{{.HostConfig.RestartPolicy.Name}}\t{{json .Config.Labels}}', cid, timeout=20)
    policy, _, labels = r.stdout.strip().partition('\t')
    if r.returncode != 0:
        return True
    try:
        keys = list(json.loads(labels or '{}') or {})
    except ValueError:
        keys = []
    return policy not in ('', 'no') or any(k.startswith(('devcontainer.', 'io.kubernetes.', 'dev.containers.'))
                                           for k in keys)


def reap(dry):
    cfg = load_config()
    grace, acted = cfg.get('grace_min', 45) * 60, []

    def attempt(what, fn):
        try:
            fn()
        except (subprocess.TimeoutExpired, TimeoutError, OSError) as e:
            acted.append(f'{what}: skipped this round ({type(e).__name__})')

    def kill_orphans():
        for pid, age, cwd, cmd in orphans(cfg):
            acted.append(f'kill orphan dev server {pid} ({int(age)} min, {cwd}): {cmd}')
            now_ = run(['/bin/ps', '-o', 'etime=', '-p', str(pid)], timeout=5).stdout.strip()
            if not now_ or etime_min(now_) < age:
                acted[-1] += ' (gone or pid reused; left alone)'
                continue
            if not dry:
                for p in reversed(descendants(pid)):
                    try:
                        os.kill(p, signal.SIGTERM)
                    except OSError:
                        pass
    attempt('orphan check', kill_orphans)
    try:
        reap_devices(cfg, dry, grace, acted, attempt)
    except (subprocess.SubprocessError, OSError, ValueError, DeviceMissing) as e:
        acted.append(f'devices: skipped this round ({type(e).__name__})')
    if not daemon_up():
        return finish(acted, dry, 'runtime down; nothing else to reap')

    now = time.time()
    running = {}
    for name, st in cfg['stacks'].items():  # docker calls first, with no lock held
        attempt(f'check {name}', lambda: running.__setitem__(name, stack_running(st)))
    with State(readonly=dry) as s:  # decide under the lock
        s.prune(cfg)
        started = dict(s.data['started'])
        leased = {n for n, ls in s.data['leases'].items() if ls}
        any_lease = bool(leased - set(cfg.get('devices', {})))
        due, gone = [], []
        for name in cfg['stacks']:
            if name not in running:
                continue
            if not running[name] and name in started:
                gone.append(name)
            if s.data['leases'].get(name) or not running[name] or name not in started:
                s.data['idle_since'].pop(name, None)
                continue
            since = s.data['idle_since'].setdefault(name, now)
            if now - since >= grace:
                due.append((name, int((now - since) / 60)))
    for name in [] if dry else gone:  # stopped some other way: forget we started it, unless an up is under way
        def forget(name=name):
            with stack_lock(name, wait=0) as got:
                if got and not stack_running(cfg['stacks'][name]):
                    with State() as s:
                        s.data['started'].pop(name, None)
        attempt(f'forget {name}', forget)
    for name, idle_min in due:  # act outside it, holding only that stack's start lock
        acted.append(f'stop stack {name}: no lease for {idle_min} min')
        if dry:
            continue

        def stop(name=name):
            with stack_lock(name, wait=0) as got:
                if not got:
                    acted.append(f'{name}: being started right now, left alone')
                    return
                with State() as s:
                    s.prune(cfg)
                    if s.data['leases'].get(name):
                        acted.append(f'{name}: leased again, left running')
                        return
                stop_stack(name, cfg['stacks'][name], 'reaper: no lease')
        attempt(f'stop {name}', stop)

    def stop_unmanaged():
        own, keep = managed_projects(cfg, set(started) | leased), set(cfg.get('keep', []))
        for n, cid, status, proj in containers():
            mins = up_minutes(status)
            if proj in own or n in keep or proj in keep or mins is None or mins < cfg.get('unmanaged_h', 12) * 60:
                continue
            if protected_container(cid):
                continue
            acted.append(f'stop container {n} (project {proj or "-"}): up {int(mins / 60)} h, no stack owns it')
            if not dry:
                r = docker('stop', cid, timeout=120)
                log(f'stop container {n}: reaper, unmanaged' + ('' if r.returncode == 0 else ' FAILED'))
    attempt('unmanaged containers', stop_unmanaged)

    def colima_idle():
        empty = cfg.get('runtime') == 'colima' and not any_lease and not containers()
        for name in cfg['stacks'] if empty else []:
            with stack_lock(name, wait=0) as got:
                if not got:
                    empty = False  # an up is pulling images or starting containers
        with State(readonly=dry) as s:
            if not empty:
                s.data['idle_since'].pop('_runtime', None)
                return
            since = s.data['idle_since'].setdefault('_runtime', now)
        if now - since >= grace:
            acted.append('stop colima: nothing running')
            if not dry:
                run(['colima', 'stop'], timeout=180)
                log('stop colima: reaper, nothing running')
                with State() as s:
                    s.data['idle_since'].pop('_runtime', None)
    attempt('colima', colima_idle)
    return finish(acted, dry, 'nothing to reap')

# ---- simulators and emulators: leased like stacks, booted headless, shut down when nobody holds them ----
SDK = os.path.expanduser(os.environ.get('ANDROID_HOME') or '~/Library/Android/sdk')
ADB, EMULATOR = os.path.join(SDK, 'platform-tools', 'adb'), os.path.join(SDK, 'emulator', 'emulator')
BOOT_WAIT = 300


def sims():
    """[{udid, name, state, runtime}] of available iOS simulators."""
    r = run(['xcrun', 'simctl', 'list', 'devices', 'available', '-j'], timeout=30)
    out = []
    for rt, devs in json.loads(r.stdout or '{}').get('devices', {}).items():
        out += [{**d, 'runtime': rt} for d in devs]
    return out


class DeviceMissing(LookupError):
    """A configured simulator does not exist (e.g. its iOS runtime was removed)."""


def sim_udid(dev, all_=None):
    if dev.get('udid'):
        return dev['udid']
    want = [d for d in (all_ if all_ is not None else sims())
            if d['name'] == dev['device'] and dev.get('runtime', '') in d['runtime']]
    if not want:
        raise DeviceMissing(f'no simulator named {dev["device"]} {dev.get("runtime", "")}; see xcrun simctl list devices')
    # a booted one first, so a newly installed runtime never swaps the udid under a running lease
    version = lambda d: [int(x) for x in re.findall(r'\d+', d['runtime'])]
    return max(want, key=lambda d: (d['state'] == 'Booted', version(d)))['udid']


def emulators():
    """avd name -> adb serial for every running Android emulator."""
    if not os.path.exists(ADB):
        return {}
    out = {}
    for line in run([ADB, 'devices'], timeout=15).stdout.splitlines()[1:]:
        serial = line.split('\t')[0]
        if serial.startswith('emulator-'):
            name = run([ADB, '-s', serial, 'emu', 'avd', 'name'], timeout=10).stdout.split()
            if name:
                out[name[0]] = serial
    return out


def device_running(dev):
    if dev['kind'] == 'ios':
        u = sim_udid(dev)
        return any(d['udid'] == u and d['state'] == 'Booted' for d in sims())
    return dev['avd'] in emulators()


def device_info(dev):
    if dev['kind'] == 'ios':
        return f'iOS simulator {dev["device"]}, udid {sim_udid(dev)} (headless; open DeviceHub only to watch)'
    return f'Android emulator {dev["avd"]}, serial {emulators().get(dev["avd"], "?")} (headless)'


def device_start(dev):
    if dev['kind'] == 'ios':
        u = sim_udid(dev)
        run(['xcrun', 'simctl', 'boot', u], timeout=120)
        return device_wait(dev)
    ports = {int(s.split('-')[1]) for s in emulators().values()}
    port = next(p for p in range(5554, 5700, 2) if p not in ports)
    args = [EMULATOR, '-avd', dev['avd'], '-port', str(port), '-no-window', '-no-audio', '-no-boot-anim',
            '-no-snapshot-save', '-gpu', dev.get('gpu', 'host'), '-memory', str(dev.get('memory_mb', 2048))]
    with open(os.path.join(claims.INDEX_DIR, f'emulator-{dev["avd"]}.log'), 'w') as f:
        subprocess.Popen(args, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    return device_wait(dev, f'emulator-{port}')


def device_wait(dev, serial=None):
    """True once the device has finished booting (a shared device may still be mid-boot)."""
    if dev['kind'] == 'ios':
        return run(['xcrun', 'simctl', 'bootstatus', sim_udid(dev), '-b'], timeout=BOOT_WAIT).returncode == 0
    deadline = time.time() + BOOT_WAIT
    while time.time() < deadline:
        serial = serial or emulators().get(dev['avd'])
        if serial and run([ADB, '-s', serial, 'shell', 'getprop', 'sys.boot_completed'], timeout=15).stdout.strip() == '1':
            return True
        time.sleep(3)
    return False


def device_stop(name, dev, reason):
    if dev['kind'] == 'ios':
        r = run(['xcrun', 'simctl', 'shutdown', sim_udid(dev)], timeout=120)
    else:
        serial = emulators().get(dev['avd'])
        r = run([ADB, '-s', serial, 'emu', 'kill'], timeout=60) if serial else None
    ok = r is None or r.returncode == 0
    log(f'stop device {name}: {reason}' + ('' if ok else ' FAILED'))
    if ok:
        with State() as s:
            s.data['started'].pop(name, None)
            s.data['idle_since'].pop(name, None)
    if dev['kind'] == 'ios':
        try:
            kill_stale_idb()
        except (subprocess.SubprocessError, OSError, ValueError):
            pass
    return ok


def kill_stale_idb(dry=False):
    """idb_companion processes left for simulators that are no longer booted."""
    off = {d['udid'] for d in sims() if d['state'] == 'Shutdown'}  # never a real iPhone or a sim mid-boot
    acted = []
    for line in run(['/bin/ps', '-axo', 'pid=,command='], timeout=10).stdout.splitlines():
        m = re.match(r'\s*(\d+) .*idb_companion .*--udid ([0-9A-F-]{36})(\s|$)', line)
        if m and m.group(2) in off:
            acted.append(f'kill idb_companion {m.group(1)} (simulator {m.group(2)[:8]} not booted)')
            if not dry:
                try:
                    os.kill(int(m.group(1)), signal.SIGTERM)
                except OSError:
                    pass
    return acted


def device_up(cfg, name, why):
    dev = cfg['devices'][name]
    me = owner()
    with State() as s:
        s.prune(cfg)
        ls = [l for l in s.data['leases'].get(name, []) if not same_owner(l, me)]
        s.data['leases'][name] = ls + [{**me, 'why': (why or '')[:200], 'at': time.time()}]
        s.data['idle_since'].pop(name, None)
    with stack_lock(name, wait=900) as got:
        if not got:
            print(f'{name}: another boot has held the lock for 15 min. Lease kept; check stacks.py list.')
            return 1
        if device_running(dev):
            print(f'{name} is already booted (shared with {", ".join(l["name"] for l in ls) or "nobody else"}). '
                  'Leased; do not boot another device.')
            ok = device_wait(dev)
        else:
            print(f'booting {name} headless ...', flush=True)
            with State() as s:  # recorded first, so a boot that times out is still shut down by down or the reaper
                s.data['started'][name] = {'by': me['name'], 'at': time.time()}
            log(f'up device {name} by {me["name"]}')
            ok = device_start(dev)
        if not ok:
            print(f'{name} did not finish booting in {BOOT_WAIT}s. Log: {claims.INDEX_DIR}/emulator-*.log')
            return 1
    print(device_info(dev))
    print(dev.get('note', ''))
    print(f'Drive it with Maestro (maestro test flow.yaml, maestro hierarchy); when done: '
          f'python3 {os.path.abspath(__file__)} down {name}')
    return 0


def device_down(cfg, name):
    dev = cfg['devices'][name]
    me = owner()
    with State() as s:
        s.prune(cfg)
        ls = [l for l in s.data['leases'].get(name, []) if not same_owner(l, me)]
        if ls:
            s.data['leases'][name] = ls
        else:
            s.data['leases'].pop(name, None)
        managed = name in s.data['started']
    if ls:
        print(f'lease dropped; {name} stays booted for {", ".join(l["name"] for l in ls)}')
    elif not managed or not device_running(dev):
        print(f'lease dropped; {name} was not booted by stacks.py, so it is left as is')
    else:
        with stack_lock(name, wait=300) as got:
            with State() as s:
                s.prune(cfg)
                if s.data['leases'].get(name):
                    print(f'lease dropped; {name} was leased again, so it stays booted')
                    return 0
            if not got:
                print(f'lease dropped; {name} is being booted or stopped by another session, so it is left to the reaper')
            elif device_stop(name, dev, f'last lease dropped by {me["name"]}'):
                print(f'{name} shut down')
            else:
                print(f'lease dropped; shutting {name} down FAILED, the reaper will retry')
    return 0


def reap_devices(cfg, dry, grace, acted, attempt):
    devs, now = cfg.get('devices', {}), time.time()
    all_sims = sims()
    ids, running = {}, {}  # every simctl/adb call happens here, before the State lock
    for name, dev in devs.items():
        try:
            ids[name] = sim_udid(dev, all_sims) if dev['kind'] == 'ios' else dev['avd']
            running[name] = device_running(dev)
        except (DeviceMissing, subprocess.SubprocessError, OSError, ValueError) as e:
            acted.append(f'check {name}: skipped this round ({type(e).__name__})')
    booted = [('ios', d['udid'], d['name']) for d in all_sims if d['state'] == 'Booted'] + \
             [('android', a, a) for a in emulators()]
    held, due = set(), []  # held: booted devices a lease or `up` accounts for, so the 12 h rule skips them
    with State(readonly=dry) as s:
        s.prune(cfg)
        for name in running:
            if not running[name]:
                s.data['started'].pop(name, None)
                s.data['idle_since'].pop(name, None)
                continue
            leased, started = bool(s.data['leases'].get(name)), name in s.data['started']
            if leased or started:
                held.add(ids[name])
            if leased or not started:
                s.data['idle_since'].pop(name, None)
                continue
            since = s.data['idle_since'].setdefault(name, now)
            if now - since >= grace:
                due.append((name, int((now - since) / 60)))
        # booted outside stacks.py (Xcode, run-ios, a bare emulator): shut down after unmanaged_h
        seen = s.data['device_seen']
        for key in [k for k in seen if k not in {i for _, i, _ in booted} or k in held]:
            del seen[key]
        old = [(kind, i, label, int((now - seen.setdefault(i, now)) / 3600)) for kind, i, label in booted
               if i not in held and now - seen.setdefault(i, now) >= cfg.get('unmanaged_h', 12) * 3600]
    for name, idle_min in due:
        acted.append(f'shut down device {name}: no lease for {idle_min} min')
        if not dry:
            def stop(name=name):
                with stack_lock(name, wait=0) as got:
                    if not got:
                        return
                    with State() as s:
                        s.prune(cfg)
                        if s.data['leases'].get(name):
                            return
                    device_stop(name, devs[name], 'reaper: no lease')
            attempt(f'stop {name}', stop)
    for kind, i, label, hours in old:
        acted.append(f'shut down {kind} device {label} ({i[:8]}): booted {hours} h, nobody leases it')
        if not dry:
            def stop_loose(kind=kind, i=i):
                names = [n for n in ids if ids[n] == i]  # a configured device booted by hand: take its lock too
                with contextlib.ExitStack() as locks:
                    if not all(locks.enter_context(stack_lock(n, wait=0)) for n in names):
                        return
                    with State() as s:  # a session may have leased it since we decided
                        s.prune(cfg)
                        if any(s.data['leases'].get(n) or n in s.data['started'] for n in names):
                            return
                    if kind == 'ios':
                        run(['xcrun', 'simctl', 'shutdown', i], timeout=120)
                    elif emulators().get(i):
                        run([ADB, '-s', emulators()[i], 'emu', 'kill'], timeout=60)
                    log(f'stop loose {kind} device {label}: booted {hours} h, no lease')
            attempt(f'shutdown {label}', stop_loose)
    attempt('idb cleanup', lambda: acted.extend(kill_stale_idb(dry)))

def finish(acted, dry, empty):
    for a in acted:
        if dry or not a.startswith(('stop stack', 'stop container', 'stop colima')):
            log(('[dry] ' if dry else '') + a)
    print('\n'.join(acted) or empty)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd')
    p = sub.add_parser('up')
    p.add_argument('name')
    p.add_argument('why', nargs='?', default='')
    sub.add_parser('down').add_argument('name')
    sub.add_parser('list')
    p = sub.add_parser('reap')
    p.add_argument('--dry-run', action='store_true')
    p = sub.add_parser('add')
    p.add_argument('name')
    p.add_argument('--dir', required=True)
    p.add_argument('-f', '--file', action='append')
    p.add_argument('--project')
    p.add_argument('--service', action='append')
    p.add_argument('--after-up')
    p.add_argument('--note')
    a = ap.parse_args()
    if a.cmd == 'up':
        return up(a.name, a.why)
    if a.cmd == 'down':
        return down(a.name)
    if a.cmd == 'reap':
        return reap(a.dry_run)
    if a.cmd == 'add':
        return add(a)
    return show()


if __name__ == '__main__':
    sys.exit(main())
