#!/usr/bin/env python3
"""Refresh this public repo from the private working copy of the system, then scan it for leaks.

    python3 tools/export.py            copy the allowlist, apply rewrites, then run the leak scan
    python3 tools/export.py --check    show what would change, write nothing, then run the leak scan
    python3 tools/export.py --scan     only run the leak scan

Three steps, in order:
  1. allowlist  ALLOWLIST below maps a source file to a path in this repo. Nothing else is ever copied.
  2. rewrites   private rewrites from tools/export.local.json (paths, names), then the generic code rewrites
                below (base dir from $CLUPAI_HOME, agent folders under agents/).
  3. leak scan  every file git would publish is checked against tools/denylist.local.txt plus built-in secret
                patterns. Any hit prints file:line and the exit code is 1.

Private inputs stay out of git (see .gitignore):
  tools/export.local.json   where the sources live, and the rewrites that name private things.
                            Format: tools/export.local.example.json
  tools/denylist.local.txt  terms that must never appear. Format: tools/denylist.txt

Hand-written files (README, docs/, rules/, examples/, install.sh, bakeoff/tasks.example.json) are never touched.
"""
import argparse, json, os, re, subprocess, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_CONFIG = os.path.join(REPO, 'tools', 'export.local.json')
DENYLIST = os.path.join(REPO, 'tools', 'denylist.local.txt')
MANIFEST = os.path.join(REPO, 'tools', 'exported.txt')   # what the last export wrote, so stale files get removed
LOCAL_FILES = {'tools/export.local.json', 'tools/denylist.local.txt'}

# (source root key, path under that root) -> path in this repo. Source roots come from export.local.json.
SHARED = 'shared'     # the shared agent layer: rules, hooks, ui, per-agent deltas
SKILLS = 'skills'     # the skill library
BAKEOFF = 'bakeoff'   # the bake-off runner
ALLOWLIST = [
    (SHARED, 'hooks/agent_hook.py', 'hooks/agent_hook.py'),
    (SHARED, 'hooks/claims.py', 'hooks/claims.py'),
    (SHARED, 'hooks/queues.py', 'hooks/queues.py'),
    (SHARED, 'hooks/guardrails.py', 'hooks/guardrails.py'),
    (SHARED, 'hooks/memory_guard.py', 'hooks/memory_guard.py'),
    (SHARED, 'hooks/memory_recall.py', 'hooks/memory_recall.py'),
    (SHARED, 'hooks/effort_gate.py', 'hooks/effort_gate.py'),
    (SHARED, 'hooks/codex_hook.py', 'hooks/codex_hook.py'),
    (SHARED, 'hooks/stacks.py', 'hooks/stacks.py'),
    (SHARED, 'hooks/reset.py', 'hooks/reset.py'),
    (SHARED, 'hooks/session_mirror.py', 'hooks/session_mirror.py'),
    (SHARED, 'ui/statusline.py', 'ui/statusline.py'),
    (SHARED, 'ui/palette.py', 'ui/palette.py'),
    (SHARED, 'ui/palette.json', 'ui/palette.json'),
    (SHARED, 'output-styles/on-the-go.md', 'output-styles/on-the-go.md'),
    (SHARED, 'codex/DELTA.md', 'agents/codex/DELTA.md'),
    (SHARED, 'codex/hooks.json', 'agents/codex/hooks.json'),
    (SHARED, 'dsh/DELTA.md', 'agents/dsh/DELTA.md'),
    (SHARED, 'dsh/hooks.json', 'agents/dsh/hooks.json'),
    (SHARED, 'kimi/DELTA.md', 'agents/kimi/DELTA.md'),
    (SHARED, 'opencode/DELTA.md', 'agents/opencode/DELTA.md'),
    (SHARED, 'grok/DELTA.md', 'agents/grok/DELTA.md'),
    (SHARED, 'opencode/shared-agent-layer.js', 'agents/opencode/shared-agent-layer.js'),
    (SHARED, 'sync-agents.py', 'scripts/sync-agents.py'),
    (SKILLS, '_core/token-economy/SKILL.md', 'skills/_core/token-economy/SKILL.md'),
    (SKILLS, '_core/token-economy/scripts/second_opinion.py', 'scripts/second_opinion.py'),
    (SKILLS, '_core/token-economy/scripts/cx_run.py', 'skills/_core/token-economy/scripts/cx_run.py'),
    (SHARED, 'usage_report.py', 'scripts/usage_report.py'),
    (SKILLS, 'engineering/production-ready/SKILL.md', 'skills/production-ready/SKILL.md'),
    (SKILLS, 'engineering/production-ready/checklist.md', 'skills/production-ready/checklist.md'),
    (SKILLS, '_core/tool-scoping/SKILL.md', 'skills/_core/tool-scoping/SKILL.md'),
    (SKILLS, '_core/vault-skills/SKILL.md', 'skills/_core/vault-skills/SKILL.md'),
    (SKILLS, '_tools/build_index.py', 'scripts/build_index.py'),
    (BAKEOFF, 'bakeoff.py', 'bakeoff/bakeoff.py'),
    (BAKEOFF, 'review.py', 'bakeoff/review.py'),
    (BAKEOFF, 'quality.py', 'bakeoff/quality.py'),
]

HOME_DEFAULT = '~/clupai'
HOME_LINE = ("CLUPAI_HOME = os.path.expanduser(os.environ.get('CLUPAI_HOME') or (\n"
             "    '~/clupai' if os.path.isdir(os.path.expanduser('~/clupai'))\n"
             "    else os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))  # where this repo is installed")
HOOK_CMD = 'python3 "${CLUPAI_HOME:-$HOME/clupai}"/'

# Generic rewrites, applied after the private ones. By then every private base path reads $CLUPAI_HOME/...
# Each entry: dest glob-ish prefix ('' = every file), pattern (re), replacement.
CODE_REWRITES = [
    # python: a path under the base dir comes from the env var, not a home path
    ('.py', r"os\.path\.expanduser\('\$CLUPAI_HOME/([^']*)'\)", r"os.path.join(CLUPAI_HOME, '\1')"),
    ('.py', r"os\.path\.expanduser\('\$CLUPAI_HOME'\)", 'CLUPAI_HOME'),
    # sync-agents.py: the repo root is the shared folder; agent folders live under agents/, the rules under rules/
    ('scripts/sync-agents.py', r"^SHARED = os\.path\.dirname\(os\.path\.abspath\(__file__\)\)$",
     'SHARED = os.path.abspath(CLUPAI_HOME)   # the repo root holds rules/, hooks/, agents/, skills/, ui/'),
    ('scripts/sync-agents.py', r"^VAULT = .*$", 'VAULT = SHARED   # skills/_core lives in the same folder'),
    ('scripts/sync-agents.py', r"os\.path\.join\(SHARED, '(codex|opencode|dsh|kimi|grok)'", r"os.path.join(SHARED, 'agents', '\1'"),
    ('scripts/sync-agents.py', r"os\.path\.join\(SHARED, 'AGENTS\.md'\)", "os.path.join(SHARED, 'agents', 'codex', 'AGENTS.md')"),
    ('scripts/sync-agents.py', r"os\.path\.join\(SHARED, 'CLAUDE\.md'\)", "os.path.join(SHARED, 'rules', 'CLAUDE.md')"),
    ('scripts/sync-agents.py', r"^SHARED_SKILLS = .*$",
     "SHARED_SKILLS = [s for s in os.environ.get('CLUPAI_SHARED_SKILLS', '').split(',') if s]"
     "  # extra skills from ~/.claude/skills"),
    ('scripts/sync-agents.py', r"^COMMANDS = .*$",
     "COMMANDS = [c for c in os.environ.get('CLUPAI_COMMANDS', '').split(',') if c]"
     "  # slash commands from ~/.claude/commands, e.g. review.md"),
    ('scripts/sync-agents.py', r"^REPO_ROOTS = .*$",
     "REPO_ROOTS = [os.path.expanduser(p) for p in os.environ.get('CLUPAI_REPOS', '~/projects').split(os.pathsep)]"),
    ('scripts/sync-agents.py', r'this vault folder', 'this shared folder ($CLUPAI_HOME)'),
    # hook configs and the OpenCode plugin find the scripts through $CLUPAI_HOME, with a default
    ('hooks.json', r'python3 \$CLUPAI_HOME/', HOOK_CMD.replace('"', '\\"')),
    ('agents/', r'Its source is the vault: ', 'Its source is this repo: '),
    ('agents/', r'Source: vault `', 'Source: `'),
    ('agents/', r'Source: vault ', 'Source: '),
    ('agents/opencode/shared-agent-layer.js', r'const HOOK = "\$CLUPAI_HOME/hooks/agent_hook\.py";',
     'const HOOK = (process.env.CLUPAI_HOME || (process.env.HOME + "/clupai")) + "/hooks/agent_hook.py";'),
    # Codex delta: drop the owner's terminal notes
    ('agents/codex/DELTA.md', r'(?s)\n## Codex-only environment notes\n.*?(?=\n## )', ''),
    # tool-scoping: the change log lives wherever the user keeps notes
    ('skills/_core/tool-scoping/SKILL.md', r'`30-resources/skills/index\.md` in the vault', '`skills/CHANGES.md`'),
    # bake-off runner: optional node path and Claude config dir from the environment
    ('bakeoff/', r"^os\.environ\['PATH'\] = os\.path\.expanduser\('[^']*'\) \+ ':' \+ os\.environ\['PATH'\].*$",
     "if os.environ.get('BAKEOFF_NODE_BIN'):   # e.g. a pinned Node version's bin dir\n"
     "    os.environ['PATH'] = os.environ['BAKEOFF_NODE_BIN'] + ':' + os.environ['PATH']"),
    ('bakeoff/', r"'https://api\.moonshot\.ai/v1/[u]sers/me/balance'",
     "'/'.join(['https://api.moonshot.ai/v1', 'users', 'me', 'balance'])"),
    ('bakeoff/', r"os\.path\.expanduser\('~/\.claude-alt'\)",
     "os.path.expanduser(os.environ.get('BAKEOFF_CLAUDE_CONFIG_DIR', '~/.claude'))"),
]

CODE_REWRITES += [
    # plan names that describe one person's situation (more live in export.local.json)
    ('', r'SuperGrok Lite plan', 'Grok plan quota'),
    ('', r'SuperGrok plan pool', 'Grok plan quota'),
    ('', r'your SuperGrok plan', 'your Grok plan'),
    ('.md', r'\bno gain\b', 'no meaningful gain'),
    ('scripts/build_index.py', r'skills/_tools/build_index\.py', 'scripts/build_index.py'),
    ('.md', r'claude-shared/usage_report\.py', 'scripts/usage_report.py'),
    ('', r'the three Claude accounts', 'the Claude accounts'),
    ('hooks/reset.py', r' \(guardrail g25\)', ''),
]

# Safety patches for the public copy of sync-agents.py. Each is an exact literal that must appear exactly `count`
# times after the rewrites above. If the source changed so a patch no longer applies, the export FAILS (exit 3).
SYNC = 'scripts/sync-agents.py'
PATCHES = [
    # tomllib is Python 3.11+; stock macOS python3 is 3.9, so load it lazily and skip the model read without it
    ('scripts/second_opinion.py', 1, 'signal, subprocess, sys, tempfile, time, tomllib\n', 'signal, subprocess, sys, tempfile, time\n'),
    ('scripts/second_opinion.py', 1, "    try:\n        with open(os.path.join(HOME, '.codex', 'config.toml'), 'rb') as f:\n",
     "    try:\n        import tomllib                             # Python 3.11+\n"
     "        with open(os.path.join(HOME, '.codex', 'config.toml'), 'rb') as f:\n"),
    ('scripts/second_opinion.py', 1, "    except (OSError, ValueError):\n        return None\n",
     "    except (ImportError, OSError, ValueError):\n        return None\n"),
    # usage_report.py: the report note lives in the repo root, not a vault three folders up
    ('scripts/usage_report.py', 1,
     "VAULT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..'))\n",
     "VAULT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))   # the repo root\n"),
    ('scripts/usage_report.py', 1, "NOTE = os.path.join(VAULT, '30-resources', 'claude-usage.md')\n",
     "NOTE = os.path.join(VAULT, 'claude-usage.md')\n"),
    ('scripts/usage_report.py', 1, 'What the three Claude Code accounts spent', 'What your Claude Code accounts spent'),
    # no stacks.json yet: start empty instead of crashing (`stacks.py add` creates it)
    ('hooks/stacks.py', 1, "def load_config():\n    with open(CONFIG) as f:\n        cfg = json.load(f)\n",
     "def load_config():\n    try:\n        with open(CONFIG) as f:\n            cfg = json.load(f)\n"
     "    except FileNotFoundError:\n        cfg = {'runtime': 'docker-desktop', 'stacks': {}}\n"),
    # the Obsidian graph maps are private to the vault; the public index builder skips them
    ('scripts/build_index.py', 1,
     "# Keep the Obsidian graph maps in step with the index.\n"
     "import subprocess, sys\n"
     "subprocess.run([sys.executable, os.path.join(LIB, '_tools', 'build_graph_maps.py')], check=False)\n", ''),
    # tomllib is Python 3.11+; older Pythons skip the parse check instead of crashing
    (SYNC, 1, '    import tomllib\n',
     '    try:\n'
     '        import tomllib                             # Python 3.11+\n'
     '    except ImportError:\n'
     '        tomllib = None                             # older Python: skip the parse check below\n'),
    (SYNC, 1, '        cfg = tomllib.loads(new)\n',
     "        cfg = tomllib.loads(new) if tomllib else {'compat': {'claude': {'hooks': False}}}\n"),
    # project links only when asked (--repos), and only at a git repo's own top level
    (SYNC, 1,
     "    ap.add_argument('--repos', nargs='*', default=REPO_ROOTS, help='folders of repos to give an AGENTS.md link')\n",
     "    ap.add_argument('--repos', nargs='*', default=None,\n"
     "                    help='folders of repos to give an AGENTS.md link (none listed: $CLUPAI_REPOS, else ~/projects). '\n"
     "                         'Off unless given.')\n"
     "    ap.add_argument('--ui', action='store_true',\n"
     "                    help='also set the statusline and theme in Claude settings.json and Codex [tui] (off by default)')\n"),
    (SYNC, 1, "    if not a.only or a.only == 'projects':\n        install_project_rules(a.repos, a.check)\n",
     "    if a.repos is not None and (not a.only or a.only == 'projects'):\n"
     "        install_project_rules(a.repos or REPO_ROOTS, a.check)\n"),
    (SYNC, 1,
     "                link(claude_md, agents_md, check)\n"
     "                if os.path.exists(os.path.join(repo, '.git')):   # an umbrella folder is not a repo\n"
     "                    git_ignore_locally(repo, 'AGENTS.md', check)\n",
     "                top = git(repo, 'rev-parse', '--show-toplevel')\n"
     "                if not top or os.path.realpath(top) != os.path.realpath(repo):\n"
     "                    continue                       # only a repo's own top level, never a subfolder or umbrella\n"
     "                link(claude_md, agents_md, check)\n"
     "                git_ignore_locally(repo, 'AGENTS.md', check)\n"),
    # UI changes are opt-in
    (SYNC, 1, "    if not a.only or a.only == 'ui':\n        install_ui(a.check)\n",
     "    if a.ui or a.only == 'ui':\n        install_ui(a.check)\n"),
    (SYNC, 1, '--check reports what is missing or stale and changes nothing.\n',
     '--check reports what is missing or stale and changes nothing.\n'
     '--ui opts in to the ui step (Claude settings.json statusLine and theme, Codex [tui]); off by default.\n'
     '--repos opts in to the projects step; without it no repo is touched.\n'
     'A folder holding a .sync-agents-skip file never gets an AGENTS.md link.\n'),
]


class PatchError(Exception):
    pass


# Secrets that must never ship, whatever the denylist says.
BUILTIN_DENY = [
    (r'sk-[A-Za-z0-9_-]{20,}', 'API key (sk-)'),
    (r'ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}', 'GitHub token'),
    (r'AIza[0-9A-Za-z_-]{30,}', 'Google API key'),
    (r'xox[abprs]-[A-Za-z0-9-]{10,}', 'Slack token'),
    (r'(?<![0-9a-fA-F])[0-9a-fA-F]{40,}(?![0-9a-fA-F])', 'long hex string'),
    (r'-----BEGIN [A-Z ]*PRIVATE KEY-----', 'private key'),
    (r'/[U]sers/[A-Za-z]', 'macOS home path'),   # [U] so this line never matches itself
    (r'/home/[a-z][a-z0-9_-]+/', 'Linux home path'),
    (r'[A-Za-z0-9._%+-]+@(?:g[m]ail|outlook|hotmail|icloud|yahoo)\.[a-z]{2,}', 'personal email'),
]


def fail(msg):
    print(f'export: {msg}', file=sys.stderr)
    sys.exit(2)


def load_local():
    if not os.path.exists(LOCAL_CONFIG):
        fail(f'{os.path.relpath(LOCAL_CONFIG, REPO)} is missing. Copy tools/export.local.example.json and fill it in.')
    cfg = json.load(open(LOCAL_CONFIG))
    for key in (SHARED, SKILLS):
        if key not in cfg.get('sources', {}):
            fail(f'sources.{key} is not set in {os.path.relpath(LOCAL_CONFIG, REPO)}')
    return cfg


def apply(text, rules):
    """rules: [[from, to], ...]. A 'from' starting with 're:' is a regex (multiline), else a literal."""
    for frm, to in rules:
        if frm.startswith('re:'):
            text = re.sub(frm[3:], to, text, flags=re.M)   # add (?s) to the pattern to let . match newlines
        else:
            text = text.replace(frm, to)
    return text


def matches(dest, prefix):
    return prefix == '' or dest == prefix or dest.endswith('/' + prefix) \
        or (prefix.endswith('/') and dest.startswith(prefix)) \
        or (prefix.startswith('.') and dest.endswith(prefix))


def ensure_home_line(text):
    """A .py file that uses CLUPAI_HOME gets it defined right after its first import line."""
    if 'CLUPAI_HOME' not in re.sub(r'#.*|"""[\s\S]*?"""', '', text) or re.search(r'^CLUPAI_HOME = ', text, re.M):
        return text
    lines = text.split('\n')
    first = next((i for i, l in enumerate(lines) if re.match(r'(?:import|from) ', l)), None)
    if first is None:
        return text
    last = first
    while last + 1 < len(lines) and re.match(r'(?:import|from) ', lines[last + 1]):
        last += 1                                  # after the whole first block of imports
    return '\n'.join(lines[:last + 1] + [HOME_LINE] + lines[last + 1:])


def drop_hooks(text, needle):
    """JSON hook config: remove every hook whose command mentions needle, and any group left empty."""
    data = json.loads(text)
    for event, groups in list(data.get('hooks', {}).items()):
        kept = []
        for g in groups:
            g['hooks'] = [h for h in g.get('hooks', []) if needle not in json.dumps(h)]
            if g['hooks']:
                kept.append(g)
        if kept:
            data['hooks'][event] = kept
        else:
            del data['hooks'][event]
    return json.dumps(data, indent=2) + '\n'


def transform(dest, text, cfg):
    text = apply(text, cfg.get('rewrites', []))
    text = apply(text, cfg.get('file_rewrites', {}).get(dest, []))
    for prefix, pat, repl in CODE_REWRITES:
        if matches(dest, prefix):
            text, n = re.subn(pat, repl, text, flags=re.M)
            if n == 0 and prefix == dest:          # a rewrite aimed at one file must still apply
                raise PatchError(f'{dest}: rewrite {pat!r} no longer matches')
    for target, count, old, new in PATCHES:
        if target == dest:
            found = text.count(old)
            if found != count:
                raise PatchError(f'{dest}: patch expected {count} match(es), found {found}: {old[:70]!r}')
            text = text.replace(old, new)
    if dest.endswith('.py'):
        text = ensure_home_line(text)
    if dest.endswith('hooks.json'):
        for needle in cfg.get('drop_hooks_matching', []):
            text = drop_hooks(text, needle)
    return text


def export(check):
    cfg = load_local()
    roots = {k: os.path.expanduser(v) for k, v in cfg['sources'].items()}
    old = set(open(MANIFEST).read().split()) if os.path.exists(MANIFEST) else set()
    written, changed = [], 0
    for root, src, dest in ALLOWLIST:
        if root not in roots:
            print(f'skip     {dest} (sources.{root} not set)')
            continue
        path = os.path.join(roots[root], src)
        if not os.path.isfile(path):
            print(f'missing  {path}')
            continue
        try:
            text = transform(dest, open(path, encoding='utf-8').read(), cfg)
        except PatchError as e:
            print(f'export: PATCH FAILED, stopping. {e}', file=sys.stderr)
            sys.exit(3)
        out = os.path.join(REPO, dest)
        written.append(dest)
        if os.path.exists(out) and open(out, encoding='utf-8').read() == text:
            continue
        changed += 1
        print(f'{"would" if check else "wrote"}    {dest}')
        if not check:
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, 'w', encoding='utf-8') as f:
                f.write(text)
            if os.access(path, os.X_OK):
                os.chmod(out, 0o755)
    for stale in sorted(old - set(written)):
        print(f'{"would remove" if check else "removed"}  {stale} (no longer in the allowlist)')
        if not check and os.path.exists(os.path.join(REPO, stale)):
            os.remove(os.path.join(REPO, stale))
    if not check:
        with open(MANIFEST, 'w') as f:
            f.write('\n'.join(sorted(written)) + '\n')
    print(f'{len(written)} files exported, {changed} changed')


def load_denylist():
    rules = [(re.compile(p), why) for p, why in BUILTIN_DENY]
    if not os.path.exists(DENYLIST):
        print('scan: tools/denylist.local.txt is missing, so only built-in secret patterns are checked', file=sys.stderr)
        return rules, False
    for line in open(DENYLIST, encoding='utf-8'):
        line = line.rstrip('\n')
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if line.startswith('re:'):
            rules.append((re.compile(line[3:], re.I), 'denylist'))
        else:
            rules.append((re.compile(re.escape(line.strip()), re.I), 'denylist'))
    return rules, True


def published_files():
    """Every file git would publish: tracked plus untracked-but-not-ignored. Falls back to a plain walk."""
    try:
        out = subprocess.run(['git', '-C', REPO, 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
                             capture_output=True, text=True, check=True).stdout
        files = [f for f in out.split('\0') if f]
    except (OSError, subprocess.CalledProcessError):
        files = []
        for d, dirs, names in os.walk(REPO):
            dirs[:] = [x for x in dirs if x != '.git']
            files += [os.path.relpath(os.path.join(d, n), REPO) for n in names]
    return sorted(f for f in files if f not in LOCAL_FILES and os.path.isfile(os.path.join(REPO, f)))


def scan():
    rules, full = load_denylist()
    hits = 0
    for rel in published_files():
        try:
            lines = open(os.path.join(REPO, rel), encoding='utf-8').read().splitlines()
        except UnicodeDecodeError:
            print(f'{rel}: binary file, not scanned; remove it or check it by hand')
            hits += 1
            continue
        for rel_check in (rel,):   # the path itself can leak too
            for rx, why in rules:
                if rx.search(rel_check):
                    print(f'{rel}:0: {why}: path matches {rx.pattern!r}')
                    hits += 1
        for n, line in enumerate(lines, 1):
            for rx, why in rules:
                if rx.search(line):
                    # never echo the matched term itself: the scan output may be pasted somewhere public
                    print(f'{rel}:{n}: {why} ({"pattern #" + str(rules.index((rx, why)))})')
                    hits += 1
    print(f'leak scan: {hits} hit(s) in {len(published_files())} files'
          + ('' if full else ' (built-in patterns only)'))
    return 1 if hits else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--check', action='store_true', help='show what would change, write nothing')
    ap.add_argument('--scan', action='store_true', help='only run the leak scan')
    a = ap.parse_args()
    if not a.scan:
        export(a.check)
    return scan()


if __name__ == '__main__':
    sys.exit(main())
