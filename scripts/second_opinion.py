#!/usr/bin/env python3
"""Ask a non-Claude model, falling back down a ladder when one is out of quota or fails.

Usage:
  second_opinion.py PROMPT_FILE [--cwd DIR] [--network] [--confidential] [--write] [--ladder NAME,NAME,...] [--timeout SECS]

Prints the model's final answer on stdout. Prints one JSON line on stderr per attempt:
{"provider":..., "model":..., "ok":..., "secs":..., "usage":..., "quota":..., "error":...}

Ladder entries (default order is LADDER below, from the 17 Sep 2026 benchmark):
  codex                         Codex CLI, your ChatGPT plan
  agy:<model>                   Google Antigravity CLI, e.g. agy:gemini-3.8-flash-medium
  dsh:<model>                   DeepSeek's own CLI on the direct API (own key), e.g. dsh:deepseek-flash, dsh:deepseek-v4-pro
  kimi:<model>                  Kimi Code CLI on the Moonshot platform API (own key, keychain kimi-api), e.g. kimi:kimi-k3
  grok:<model>                  Grok Build CLI on your Grok plan (grok login), e.g. grok:grok-4.7, grok:grok-4.7-build-fast
  opencode:<provider/model>     OpenCode Go, e.g. opencode:opencode-go/glm-5.3 (GLM, Grok, Qwen, MiniMax; DeepSeek goes
                                through dsh and Kimi through kimi)

Safety:
  --write         lets the model edit files inside --cwd only (use a disposable worktree).
  without --write no route may write to your files: codex runs read-only (or in an empty temp dir when
                  --network is set, with /tmp excluded), and agy, dsh, kimi and opencode run inside a macOS sandbox that
                  blocks writes under $HOME and inside --cwd (kimi, grok and dsh --write: everywhere but their own
                  state and a private temp dir, because they run tools without asking).
  policy          read from model-policy.json by --cwd: confidential = codex only, open = + dsh, kimi, opencode,
                  personal = + agy (Gemini), grok (Grok Build on a consumer plan). Override with --policy or --confidential.
"""
import argparse, glob, json, os, re, signal, subprocess, sys, tempfile, time
CLUPAI_HOME = os.path.expanduser(os.environ.get('CLUPAI_HOME') or (
    '~/clupai' if os.path.isdir(os.path.expanduser('~/clupai'))
    else os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))  # where this repo is installed

LADDER = ['codex', 'dsh:deepseek-flash', 'agy:gemini-3.8-flash-high', 'kimi:kimi-k3']  # app bake-off, 22 Sep 2026
# OpenCode's own privacy policy allows using prompts to improve its services, several upstream
# providers may train on inputs, and Antigravity's free tier trains on inputs.
POLICY_FILE = os.path.join(CLUPAI_HOME, 'model-policy.json')
RANK = {'confidential': 0, 'open': 1, 'personal': 2}
MAX_ROUTES = {'confidential': {'codex'}, 'open': {'codex', 'dsh', 'kimi', 'opencode'},
              'personal': {'codex', 'dsh', 'kimi', 'opencode', 'agy', 'grok'}}


def _ancestors(path):
    path = os.path.abspath(path)
    while True:
        yield path
        parent = os.path.dirname(path)
        if parent == path:
            return
        path = parent


def _policy_of_one(path, rules):
    """Most specific rule whose folder is the same directory (by file identity, so case and symlinks can't dodge it)."""
    best, best_len = 'confidential', -1
    for rule in rules:
        root = os.path.expanduser(str(rule.get('path', '')))
        if not root or not os.path.isdir(root):
            continue
        for anc in _ancestors(path):
            try:
                if os.path.exists(anc) and os.path.samefile(anc, root):
                    if len(os.path.abspath(root)) > best_len:
                        best, best_len = str(rule.get('policy')), len(os.path.abspath(root))
                    break
            except OSError:
                continue
    return best if best in RANK else 'confidential'


def policy_for(path):
    """Strictest policy of the path as given and as resolved. Unlisted, unknown or unreadable means confidential."""
    try:
        cfg = json.load(open(POLICY_FILE))
        rules = [r for r in cfg.get('rules', []) if isinstance(r, dict)]
    except (OSError, ValueError, AttributeError):
        return 'confidential', MAX_ROUTES['confidential']
    lexical = os.path.normpath(os.path.abspath(os.path.expanduser(path)))
    candidates = [_policy_of_one(lexical, rules), _policy_of_one(os.path.realpath(lexical), rules)]
    policy = min(candidates, key=lambda x: RANK[x])
    configured = set((cfg.get('routes') or {}).get(policy, []))
    return policy, (configured & MAX_ROUTES[policy]) or MAX_ROUTES['confidential']


QUOTA = re.compile(r'usage limit|rate.?limit|quota|429|exceeded|insufficient|out of credits|resource.?exhausted|try again later', re.I)
HOME = os.path.expanduser('~')
# Where each CLI keeps its own state; it must stay writable inside the sandbox.
STATE_DIRS = {
    'opencode': ['.local/share/opencode', '.local/state/opencode', '.cache/opencode', '.config/opencode', '.cache', '.npm', '.bun'],
    'agy': ['.gemini', '.cache', '.config'],
    'dsh': ['.dsh', '.cache', '.npm', '.local/share/pnpm', 'Library/pnpm'],
    'kimi': ['.kimi-code', '.cache'],
    'grok': ['.grok', '.cache'],
}


def sandbox_profile(kind, project_dir, write, scratch=None):
    """Block writes under $HOME and inside the project; re-allow the CLI's own state dirs, and the project only with --write."""
    project = os.path.realpath(project_dir)
    allow = [f'(subpath "{os.path.join(HOME, d)}")' for d in STATE_DIRS[kind]]
    if write:
        allow.append(f'(subpath "{project}")')
    if (kind == 'dsh' and write) or kind in ('kimi', 'grok'):
        # dsh runs with approvals off under --write, and kimi's prompt mode never asks, so deny writes
        # everywhere but the project (only with --write), their own state dirs and scratch space
        # its own temp dir only, not every other run's worktree under /tmp
        allow += [f'(subpath "{os.path.realpath(scratch)}")', '(subpath "/dev")']
        profile = f'(version 1)(allow default)(deny file-write* (subpath "/"))(allow file-write* {" ".join(allow)})'
        if not write:
            profile += f'(deny file-write* (subpath "{project}"))'  # last rule wins, even for a project under /tmp
        return profile
    return (f'(version 1)(allow default)(deny file-write* (subpath "{HOME}") (subpath "{project}"))'
            f'(allow file-write* {" ".join(allow)})')


def run(cmd, cwd=None, stdin_text=None, timeout=1200, env=None):
    """Run a command in its own process group so a timeout kills everything it spawned."""
    p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True, env=env)
    try:
        out, err = p.communicate(stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.communicate()
        raise
    return p.returncode, out, err


def kimi_usage(session):
    """Token totals for a kimi session, summed from the per-turn usage records in its wire logs."""
    tot = {'input': 0, 'output': 0, 'cache_read': 0}
    if not session:
        return tot
    for path in glob.glob(os.path.join(HOME, '.kimi-code', 'sessions', '*', session, 'agents', '*', 'wire.jsonl')):
        try:
            for line in open(path):
                if '"usage.record"' not in line:
                    continue
                d = json.loads(line)
                if d.get('usageScope', 'turn') != 'turn':
                    continue
                u = d.get('usage') or {}
                tot['input'] += u.get('inputOther', 0) + u.get('inputCacheCreation', 0)
                tot['output'] += u.get('output', 0)
                tot['cache_read'] += u.get('inputCacheRead', 0)
        except (OSError, ValueError):
            continue
    return tot


def attempt(entry, prompt, cwd, network, timeout, write):
    t0 = time.time()
    kind, _, model = entry.partition(':')
    usage, text, err, ok = {}, '', '', False
    if kind == 'codex':
        fd, last_path = tempfile.mkstemp(suffix='.txt')
        os.close(fd)
        scratch = None
        try:
            if write:
                sandbox, run_dir = 'workspace-write', cwd
            elif network:
                # Codex needs workspace-write for network access; point it at an empty temp dir so it
                # cannot modify the project. It can still read the project by absolute path.
                scratch = tempfile.mkdtemp(prefix='second-opinion-')
                sandbox, run_dir = 'workspace-write', scratch
                prompt = f'The project to look at is {cwd} (read it by absolute path; do not modify it).\n\n' + prompt
            else:
                sandbox, run_dir = 'read-only', cwd
            cmd = ['codex', 'exec', '--skip-git-repo-check', '-C', run_dir, '-o', last_path, '-s', sandbox]
            if network:
                cmd += ['-c', 'sandbox_workspace_write.network_access=true']
            if not write:
                # without --write, only the empty scratch dir may be written: no /tmp, no $TMPDIR
                cmd += ['-c', 'sandbox_workspace_write.exclude_slash_tmp=true', '-c', 'sandbox_workspace_write.exclude_tmpdir_env_var=true']
            cmd.append('-')  # prompt on stdin, so it can never be parsed as a flag
            code, out, stderr = run(cmd, stdin_text=prompt, timeout=timeout)
            text = open(last_path).read().strip()
        finally:
            os.remove(last_path)
            if scratch:
                subprocess.run(['rm', '-rf', scratch])
        ok = code == 0 and bool(text)
        m = re.search(r'tokens used\s+([\d,]+)', stderr)
        usage = {'total': int(m.group(1).replace(',', ''))} if m else {}
        err = '' if ok else (stderr + out)[-2000:]
    elif kind == 'agy':
        if network:
            return False, '', dict(provider=kind, model=model, ok=False, secs=0, usage={}, quota=False,
                                   error='skipped: agy cannot combine --network with its sandbox')
        # -p takes the prompt as a flag value, so a prompt starting with '--' stays a prompt.
        cmd = ['sandbox-exec', '-p', sandbox_profile('agy', cwd, write),
               'agy', '-p', prompt, '--model', model, '--output-format', 'json', '--add-dir', cwd,
               '--sandbox', '--dangerously-skip-permissions']
        code, out, stderr = run(cmd, cwd=cwd, timeout=timeout)
        try:
            d = json.loads(out)
            text, usage = (d.get('response') or '').strip(), d.get('usage', {})
            ok = d.get('status') == 'SUCCESS' and bool(text)
        except ValueError:
            ok = False
        err = '' if ok else (stderr + out)[-2000:]
    elif kind == 'dsh':
        # dsh reads the key from DEEPSEEK_API_KEY; fall back to the keychain when the caller's shell
        # did not load ~/.zprofile. The model comes from DSH_MODEL, read by ~/.dsh/profiles/headless/cordis.patch.yml.
        env = dict(os.environ, DSH_MODEL=model or 'deepseek-v4-pro', DSH_TELEMETRY_DISABLED='1',
                   # dsh's own workspace-write sandbox cannot nest inside sandbox-exec (its shell tool fails), so
                   # --write runs dsh unrestricted and the macOS sandbox below is the only write guard
                   DSH_PERMISSION_MODE='danger-full-access' if write else 'read-only')
        if not env.get('DEEPSEEK_API_KEY'):
            k = subprocess.run(['security', 'find-generic-password', '-s', 'deepseek-api', '-w'],
                               capture_output=True, text=True).stdout.strip()
            if k:
                env['DEEPSEEK_API_KEY'] = k
        task = prompt if not prompt.lstrip().startswith('-') else 'Task:\n' + prompt  # never parsed as a flag
        scratch = tempfile.mkdtemp(prefix='dsh-') if write else None  # --write: a private temp dir, not all of /tmp
        if scratch:
            env['TMPDIR'] = scratch
        cmd = ['sandbox-exec', '-p', sandbox_profile('dsh', cwd, write, scratch), 'dsh', '--profile', 'headless', task]
        try:
            code, out, stderr = run(cmd, cwd=cwd, timeout=timeout, env=env)
        finally:
            if scratch:
                subprocess.run(['rm', '-rf', scratch])
        text = out.strip()
        ok = code == 0 and bool(text)
        err = '' if ok else (stderr + out)[-2000:]
    elif kind == 'kimi':
        # The config (~/.kimi-code/config.toml) reads the key from MOONSHOT_API_KEY; fall back to the
        # keychain when the caller's shell did not load ~/.zprofile. Prompt mode runs every tool without
        # asking (and refuses --auto), so the sandbox profile is the only write guard.
        # a private temp dir even with --write, so it cannot touch other runs' worktrees under /tmp
        scratch = tempfile.mkdtemp(prefix='kimi-')
        env = dict(os.environ, TMPDIR=scratch)
        if not env.get('MOONSHOT_API_KEY'):
            k = subprocess.run(['security', 'find-generic-password', '-s', 'kimi-api', '-w'],
                               capture_output=True, text=True).stdout.strip()
            if k:
                env['MOONSHOT_API_KEY'] = k
        task = prompt if not prompt.lstrip().startswith('-') else 'Task:\n' + prompt  # never parsed as a flag
        # --skills-dir: only the core and shared skills sync-agents.py links in, not the ~100 in ~/.agents/skills
        cmd = ['sandbox-exec', '-p', sandbox_profile('kimi', cwd, write, scratch), 'kimi', '-p', task,
               '-m', model or 'kimi-k3', '--output-format', 'stream-json',
               '--skills-dir', os.path.join(HOME, '.kimi-code', 'skills')]
        try:
            code, out, stderr = run(cmd, cwd=cwd, timeout=timeout, env=env)
        finally:
            subprocess.run(['rm', '-rf', scratch])
        session = None
        for line in out.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get('role') == 'assistant' and isinstance(d.get('content'), str) and d['content'].strip():
                text = d['content'].strip()  # the last assistant message with text is the answer
            if d.get('type') == 'session.resume_hint':
                session = d.get('session_id')
        usage = kimi_usage(session)
        ok = code == 0 and bool(text)
        err = '' if ok else (stderr + out)[-2000:]
    elif kind == 'grok':
        # Headless grok needs --always-approve to use tools at all, so like kimi the sandbox is the only write
        # guard: everywhere is denied but ~/.grok, the project (only with --write) and a private scratch dir.
        # a private temp dir even with --write, so it cannot touch other runs' worktrees under /tmp
        scratch = tempfile.mkdtemp(prefix='grok-')
        env = dict(os.environ, TMPDIR=scratch)
        task = prompt if not prompt.lstrip().startswith('-') else 'Task:\n' + prompt  # never parsed as a flag
        cmd = ['sandbox-exec', '-p', sandbox_profile('grok', cwd, write, scratch), 'grok', '-p', task,
               '-m', model or 'grok-4.7', '--always-approve', '--output-format', 'json', '--cwd', cwd]
        try:
            code, out, stderr = run(cmd, cwd=cwd, timeout=timeout, env=env)
        finally:
            subprocess.run(['rm', '-rf', scratch])
        try:
            d = json.loads(out)
            text = (d.get('text') or '').strip()
            u = d.get('usage') or {}
            usage = {'input': u.get('input_tokens', 0), 'output': u.get('output_tokens', 0),
                     'cache_read': u.get('cache_read_input_tokens', 0), 'turns': d.get('num_turns'),
                     'cost': d.get('total_cost_usd')}  # notional on the plan; it counts against the weekly pool
            ok = code == 0 and bool(text) and d.get('stopReason') != 'error'
        except ValueError:
            ok = False
        err = '' if ok else (stderr + out)[-2000:]
    elif kind == 'opencode':
        cmd = ['sandbox-exec', '-p', sandbox_profile('opencode', cwd, write),
               'opencode', 'run', '--dir', cwd, '--format', 'json', '-m', model] + (['--auto'] if write else [])
        code, out, stderr = run(cmd, stdin_text=prompt, timeout=timeout)  # prompt on stdin
        texts, last_msg, tot = {}, None, {'input': 0, 'output': 0, 'cache_read': 0, 'cost': 0.0}
        for line in out.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            part = d.get('part') or {}
            if d.get('type') == 'text':
                texts.setdefault(part.get('messageID'), []).append(part.get('text', ''))
                last_msg = part.get('messageID')
            if d.get('type') == 'step_finish':
                tk = part.get('tokens') or {}
                tot['input'] += tk.get('input', 0)
                tot['output'] += tk.get('output', 0)
                tot['cache_read'] += (tk.get('cache') or {}).get('read', 0)
                tot['cost'] += part.get('cost') or 0
            if d.get('type') == 'error':
                err += json.dumps(d)[:500]
        text, usage = ''.join(texts.get(last_msg, [])).strip(), tot
        ok = code == 0 and bool(text)
        err = '' if ok else (err + stderr)[-2000:]
    else:
        raise SystemExit(f'unknown ladder entry: {entry}')
    return ok, text, dict(provider=kind, model=model or 'default', ok=ok, secs=round(time.time() - t0),
                          usage=usage, quota=bool(QUOTA.search(err)), error=err[-300:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('prompt_file')
    ap.add_argument('--cwd', default=os.getcwd())
    ap.add_argument('--network', action='store_true', help='allow network, e.g. for gh')
    ap.add_argument('--ladder', default=','.join(LADDER))
    ap.add_argument('--timeout', type=int, default=1200)
    ap.add_argument('--policy', choices=['auto', 'confidential', 'open', 'personal'], default='auto',
                    help='auto reads model-policy.json for --cwd (default); unmatched folders are confidential')
    ap.add_argument('--confidential', action='store_true', help='same as --policy confidential')
    ap.add_argument('--write', action='store_true', help='allow edits inside --cwd only (use a disposable worktree)')
    a = ap.parse_args()
    prompt = open(a.prompt_file).read()
    given_cwd = a.cwd
    cwd = os.path.realpath(given_cwd)
    if '"' in cwd or '\\' in cwd:  # would break out of the quoted paths in the sandbox profile
        raise SystemExit('--cwd must not contain a double quote or a backslash')
    if a.write and os.path.realpath(HOME) == cwd:
        raise SystemExit('--write with --cwd set to your home folder is not allowed')
    ladder = [e.strip() for e in a.ladder.split(',') if e.strip()]
    policy, allowed = policy_for(given_cwd)            # folder policy is the ceiling
    requested = 'confidential' if a.confidential else a.policy
    if requested != 'auto' and RANK[requested] < RANK[policy]:  # overrides may only restrict
        policy, allowed = requested, MAX_ROUTES[requested]
    ladder = [e for e in ladder if e.partition(':')[0] in allowed]
    print(json.dumps({'policy': policy, 'allowed_routes': sorted(allowed), 'ladder': ladder}), file=sys.stderr)
    for entry in ladder:
        try:
            ok, text, info = attempt(entry, prompt, cwd, a.network, a.timeout, a.write)
        except (subprocess.TimeoutExpired, OSError) as e:
            ok, text, info = False, '', dict(provider=entry, ok=False, error=str(e)[:300])
        print(json.dumps(info), file=sys.stderr)
        if ok:
            print(text)
            return 0
    return 1


if __name__ == '__main__':
    sys.exit(main())
