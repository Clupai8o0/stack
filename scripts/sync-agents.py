#!/usr/bin/env python3
"""Install the shared Claude layer into Codex, OpenCode, dsh, Kimi Code (kimi) and Grok Build (grok), so each starts
with the same rules, hooks, skills, commands and memory recall.

    python3 sync-agents.py [--check] [--only codex|opencode|dsh|kimi|grok|projects|ui] [--repos DIR ...]

Everything is a symlink back to this shared folder ($CLUPAI_HOME), so there is one source:

  rules     <agent home>/AGENTS.md  <- <agent>/AGENTS.md, generated from CLAUDE.md + <agent>/DELTA.md
            ~/AGENTS.md             <- the Codex one (Codex also reads it outside a project)
  hooks     ~/.codex/hooks.json     <- codex/hooks.json            (needs one interactive approval in codex)
            ~/.config/opencode/plugins/shared-agent-layer.js <- opencode/shared-agent-layer.js
            ~/.dsh/profiles/*/cordis.patch.yml  gets an insert row for dsh-hooks-claude-code -> dsh/hooks.json
            ~/.kimi-code/config.toml  gets a managed block of [[hooks]] rows calling agent_hook.py --agent kimi
            ~/.grok/config.toml  gets a managed block: [compat.claude] hooks = false (grok would otherwise run the
                                 Claude hooks as if it were Claude) and [[hooks.<Event>]] groups calling
                                 agent_hook.py --agent grok
  grok      reads ~/.claude/CLAUDE.md and ~/.claude/skills itself (Claude compatibility), so it gets no generated
            AGENTS.md and no skill links: ~/.grok/rules/shared-agent-layer.md <- grok/DELTA.md only
  skills    <agent skills dir>/<name> <- the same skill folders the Claude accounts have
  commands  ~/.codex/prompts, ~/.config/opencode/commands <- the same slash commands
  projects  <repo>/AGENTS.md        <- that repo's CLAUDE.md, git-ignored locally
  ui        ui/palette.json  regenerated from the kitty accent (ui/palette.py)
            <each Claude config dir>/statusline.py <- ui/statusline.py, and settings.json statusLine, theme,
                                                  the Codex MCP allow rule and env CLAUDE_ENV (auto-compact window),
                                                  and the CLAUDE_HOOKS rows (agent-requested resets, hooks/reset.py)
            (OpenCode keeps its own default theme on purpose: the owner prefers it, 2026-09-21)
            ~/.codex/config.toml [tui]  status_line, terminal_title and theme set to the shared list

--check reports what is missing or stale and changes nothing.
--ui opts in to the ui step (Claude settings.json statusLine and theme, Codex [tui]); off by default.
--repos opts in to the projects step; without it no repo is touched.
A folder holding a .sync-agents-skip file never gets an AGENTS.md link.
"""
import argparse, filecmp, glob, json, os, re, shutil, subprocess, sys, time
CLUPAI_HOME = os.path.expanduser(os.environ.get('CLUPAI_HOME') or (
    '~/clupai' if os.path.isdir(os.path.expanduser('~/clupai'))
    else os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))  # where this repo is installed

SHARED = os.path.abspath(CLUPAI_HOME)   # the repo root holds rules/, hooks/, agents/, skills/, ui/
HERE = os.path.join(SHARED, 'agents', 'codex')
VAULT = SHARED   # skills/_core lives in the same folder
CODEX_HOME = os.environ.get('CODEX_HOME') or os.path.expanduser('~/.codex')
HOME = os.path.expanduser('~')

CLAUDE_MD = os.path.join(SHARED, 'rules', 'CLAUDE.md')
OPENCODE_HOME = os.path.join(HOME, '.config', 'opencode')
DSH_HOME = os.environ.get('DSH_HOME') or os.path.join(HOME, '.dsh')
KIMI_HOME = os.environ.get('KIMI_CODE_HOME') or os.path.join(HOME, '.kimi-code')
GROK_HOME = os.environ.get('GROK_HOME') or os.path.join(HOME, '.grok')
TARGETS = {   # agent -> generated rules file, where it must appear, its skills and commands dirs
    'codex': dict(rules=os.path.join(SHARED, 'agents', 'codex', 'AGENTS.md'), delta=os.path.join(SHARED, 'agents', 'codex', 'DELTA.md'),
                  homes=[os.path.join(CODEX_HOME, 'AGENTS.md'), os.path.join(HOME, 'AGENTS.md')],
                  skills=os.path.join(CODEX_HOME, 'skills'), commands=os.path.join(CODEX_HOME, 'prompts')),
    'opencode': dict(rules=os.path.join(SHARED, 'agents', 'opencode', 'AGENTS.md'),
                     delta=os.path.join(SHARED, 'agents', 'opencode', 'DELTA.md'),
                     homes=[os.path.join(OPENCODE_HOME, 'AGENTS.md')],
                     skills=os.path.join(OPENCODE_HOME, 'skills'), commands=os.path.join(OPENCODE_HOME, 'commands')),
    'dsh': dict(rules=os.path.join(SHARED, 'agents', 'dsh', 'AGENTS.md'), delta=os.path.join(SHARED, 'agents', 'dsh', 'DELTA.md'),
                homes=[os.path.join(DSH_HOME, 'AGENTS.md')],
                skills=os.path.join(DSH_HOME, 'skills'), commands=None),
    'kimi': dict(rules=os.path.join(SHARED, 'agents', 'kimi', 'AGENTS.md'), delta=os.path.join(SHARED, 'agents', 'kimi', 'DELTA.md'),
                 homes=[os.path.join(KIMI_HOME, 'AGENTS.md')],
                 skills=os.path.join(KIMI_HOME, 'skills'), commands=None),
    # delta_only: the home gets a link to DELTA.md itself; the shared rules already load via ~/.claude/CLAUDE.md
    'grok': dict(rules=None, delta=os.path.join(SHARED, 'agents', 'grok', 'DELTA.md'), delta_only=True,
                 homes=[os.path.join(GROK_HOME, 'rules', 'shared-agent-layer.md')], skills=None, commands=None),
}
DSH_MARK = '# shared-agent-layer: Claude hooks bridge (managed by sync-agents.py)'
KIMI_BEGIN = '# >>> shared-agent-layer hooks (managed by sync-agents.py; edit there, not here)'
KIMI_END = '# <<< shared-agent-layer hooks'
# (event, matcher, agent_hook event, timeout). kimi drops SessionStart output, so agent_hook.py sends the
# start-of-session context with the first prompt; SessionStart here only registers the session early.
KIMI_HOOKS = [('SessionStart', None, 'session-start', 20), ('UserPromptSubmit', None, 'prompt', 20),
              ('PreToolUse', '^(Bash|Write|Edit|Agent|AgentSwarm)$', 'pretool', 20), ('Stop', None, 'stop', 10),
              ('SessionEnd', None, 'session-end', 10)]
GROK_BEGIN = KIMI_BEGIN
GROK_END = KIMI_END
# grok runs PreToolUse on every tool (no matcher) so the context it holds back arrives with the first call; the
# default hook timeout is 5s (SessionEnd 1.5s), too short for the claim scripts, so each sets its own.
GROK_HOOKS = [('SessionStart', 'session-start', 20), ('UserPromptSubmit', 'prompt', 20),
              ('PreToolUse', 'pretool', 20), ('Stop', 'stop', 10), ('SessionEnd', 'session-end', 10)]

CORE_SKILLS = ['token-economy', 'tool-scoping', 'vault-skills']          # from the vault, in every agent
SHARED_SKILLS = [s for s in os.environ.get('CLUPAI_SHARED_SKILLS', '').split(',') if s]  # extra skills from ~/.claude/skills
COMMANDS = [c for c in os.environ.get('CLUPAI_COMMANDS', '').split(',') if c]  # slash commands from ~/.claude/commands, e.g. review.md

# Repos whose CLAUDE.md should also load in Codex. Anything under here with a CLAUDE.md and a .git.
REPO_ROOTS = [os.path.expanduser(p) for p in os.environ.get('CLUPAI_REPOS', '~/projects').split(os.pathsep)]

CLAUDE_HOMES = [os.path.join(HOME, d) for d in ('.claude', '.claude-exec')]  # main, cx
CLAUDE_ALLOW = ['mcp__codex__codex', 'mcp__codex__codex-reply']  # the Codex review gate runs without a prompt
# settings.json env for every Claude account. 23 Sep 2026: about 60% of Claude spend went on calls with more than 200k
# tokens of context (usage_report.py), so sessions auto-compact at a 250k window instead of near 1M. /autocompact
# changes it for one session.
CLAUDE_ENV = {'CLAUDE_CODE_AUTO_COMPACT_WINDOW': '250000'}
# Hooks every Claude account must have, as (event, matcher or None, command, timeout). Added when missing, never
# removed or reordered; the older hooks in settings.json were added by hand and stay as they are.
RESET = os.path.join(SHARED, 'hooks', 'reset.py')
CLAUDE_HOOKS = [('Stop', None, f'python3 {RESET} stop', 10),
                ('SessionStart', 'clear|compact', f'python3 {RESET} session-start', 10)]
CLAUDE_THEME = 'dark-ansi'   # Claude's theme that draws in the terminal's own 16 colours, i.e. kitty's Catppuccin
CODEX_TUI = {   # Codex has no custom statusline command; these are its closest built-in items
    'status_line': ['model-with-reasoning', 'current-dir', 'git-branch', 'context-used', 'five-hour-limit',
                    'weekly-limit'],
    'status_line_use_colors': True,
    'terminal_title': ['app-name', 'project-name', 'git-branch'],
    'theme': 'catppuccin-mocha',
}

changes = []


def git(cwd, *args):
    try:
        r = subprocess.run(['git', '-C', cwd, *args], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def say(kind, what):
    changes.append((kind, what))
    print(f'{kind:8} {what}')


def link(src, dst, check, pending=False):
    """Make dst a symlink to src, replacing a wrong link or a plain file that is just a stale copy.

    pending=True means --check has not written src yet, so a missing source is expected, not a fault."""
    if not os.path.exists(src):
        return say('would', f'link {dst} -> {src}') if (check and pending) \
            else say('missing', f'{src} (source does not exist)')
    if os.path.islink(dst) and os.path.realpath(dst) == os.path.realpath(src):
        return
    if os.path.exists(dst) and not os.path.islink(dst):
        if os.path.isfile(dst) and os.path.isfile(src) and filecmp.cmp(dst, src, shallow=False):
            pass                                   # identical copy, safe to replace with the link
        else:
            return say('KEPT', f'{dst} exists and differs from {src} — move it aside yourself')
    if check:
        return say('would', f'link {dst} -> {src}')
    if os.path.islink(dst):
        say('replaced', f'{dst} used to point at {os.readlink(dst)}')
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.lexists(dst):
        if os.path.isdir(dst) and not os.path.islink(dst):
            return say('KEPT', f'{dst} is a real directory — move it aside yourself')  # never delete a tree
        os.remove(dst)                             # a symlink, or a file we already proved is an identical copy
    os.symlink(src, dst)
    say('linked', f'{dst} -> {src}')


def build_agents_md(agent, check):
    """<agent>/AGENTS.md = that agent's delta, then the shared CLAUDE.md verbatim. One source, no drift."""
    t = TARGETS[agent]
    if not os.path.exists(CLAUDE_MD):
        return say('missing', CLAUDE_MD)
    delta = open(t['delta']).read().rstrip().rstrip('-').rstrip() if os.path.exists(t['delta']) else ''
    body = open(CLAUDE_MD).read().strip()
    text = (f'{delta}\n\n---\n\n'
            '# The shared rules (generated from rules/CLAUDE.md — edit that file, then rerun '
            'sync-agents.py)\n\n'
            'Everything below is written for the Claude Code accounts. It applies to you too, except where the\n'
            'section above says otherwise. Read `claude` as "any agent on this Mac", including yourself.\n\n'
            f'{body}\n')
    old = open(t['rules']).read() if os.path.exists(t['rules']) else None
    if old == text:
        return
    if check:
        return say('would', f'regenerate {t["rules"]}')
    open(t['rules'], 'w').write(text)
    say('wrote', t['rules'])


def install_hooks(check):
    """Link ~/.codex/hooks.json. A hooks.json that was already there is kept as codex/hooks.pre-existing.json,
    then replaced — leaving it in place would silently install no hooks at all."""
    src = os.path.join(HERE, 'hooks.json')
    dst = os.path.join(CODEX_HOME, 'hooks.json')
    if os.path.lexists(dst) and not os.path.islink(dst):
        keep = os.path.join(HERE, 'hooks.pre-existing.json')
        while os.path.lexists(keep):               # never overwrite an earlier backup, dangling link included
            keep = keep[:-5] + '.1.json'
        if check:
            return say('would', f'save {dst} to {keep}, then link it')
        shutil.copy2(dst, keep)
        os.remove(dst)
        say('saved', f'{dst} -> {os.path.relpath(keep, HOME)} (fold anything you still want into codex/hooks.json)')
    link(src, dst, check)


def install_skills(agent, check):
    dest = TARGETS[agent]['skills']
    if not dest:
        return
    for name in CORE_SKILLS:
        link(os.path.join(VAULT, 'skills', '_core', name), os.path.join(dest, name), check)
    for name in SHARED_SKILLS:
        src = os.path.join(HOME, '.claude', 'skills', name)
        if os.path.exists(src):
            link(os.path.realpath(src), os.path.join(dest, name), check)


def install_commands(agent, check):
    dest = TARGETS[agent]['commands']
    if not dest:
        return
    for name in COMMANDS:
        src = os.path.join(HOME, '.claude', 'commands', name)
        if os.path.exists(src):
            link(os.path.realpath(src), os.path.join(dest, name), check)


def install_opencode_plugin(check):
    link(os.path.join(SHARED, 'agents', 'opencode', 'shared-agent-layer.js'),
         os.path.join(OPENCODE_HOME, 'plugins', 'shared-agent-layer.js'), check)


def install_dsh_hooks(check):
    """Every dsh profile gets one insert row mounting the Claude hooks bridge on dsh/hooks.json."""
    row = (f'\n{DSH_MARK}\n'
           '- insert:\n'
           '    - id: shared-agent-layer-hooks\n'
           "      name: '@deepseek-ai/dsh-hooks-claude-code'\n"
           '      config:\n'
           f"        configPath: '{os.path.join(SHARED, 'agents', 'dsh', 'hooks.json')}'\n"
           '        defaultTimeoutMs: 20000\n')
    for patch in sorted(glob.glob(os.path.join(DSH_HOME, 'profiles', '*', 'cordis.patch.yml'))):
        text = open(patch).read()
        if DSH_MARK in text:
            continue
        if check:
            say('would', f'mount the hooks bridge in {patch}')
            continue
        backup(patch, '.bak-shared-agent-layer')
        open(patch, 'a').write(row)
        say('mounted', f'hooks bridge in {os.path.relpath(patch, HOME)}')


def install_kimi_hooks(check):
    """Keep one managed block of [[hooks]] rows at the end of ~/.kimi-code/config.toml. [[hooks]] is an array of
    tables, so rows appended after any other table still belong to the top level."""
    path = os.path.join(KIMI_HOME, 'config.toml')
    if not os.path.exists(path):
        return say('missing', f'{path} (run kimi once, or write its provider config, first)')
    script = os.path.join(SHARED, 'hooks', 'agent_hook.py')
    rows = [KIMI_BEGIN]
    for event, matcher, name, timeout in KIMI_HOOKS:
        rows += ['[[hooks]]', f'event = "{event}"']
        if matcher:
            rows.append(f'matcher = {json.dumps(matcher)}')
        rows += [f'command = {json.dumps(f"python3 {script} --agent kimi {name}")}', f'timeout = {timeout}', '']
    rows[-1] = KIMI_END
    block = '\n'.join(rows)
    text = open(path).read()
    if KIMI_BEGIN in text and KIMI_END in text:
        head, _, rest = text.partition(KIMI_BEGIN)
        _, _, tail = rest.partition(KIMI_END)
        new = head + block + tail
    else:
        new = text.rstrip('\n') + '\n\n' + block + '\n'
    if new == text:
        return
    if check:
        return say('would', f'write the hooks block in {path}')
    backup(path, '.bak-shared-agent-layer')
    open(path, 'w').write(new)
    say('set', f'hooks block in {os.path.relpath(path, HOME)}')


def install_grok_hooks(check):
    """Keep one managed block at the end of ~/.grok/config.toml: Claude-compat hooks off, and native hook groups.
    [[hooks.X]] is an array of tables, so it may follow any other table. If [compat.claude] already exists outside
    the block, hooks = false is set inside that table instead, since a table may not be declared twice."""
    try:
        import tomllib                             # Python 3.11+
    except ImportError:
        tomllib = None                             # older Python: skip the parse check below
    path = os.path.join(GROK_HOME, 'config.toml')
    if not os.path.exists(path):
        return say('missing', f'{path} (run grok once first)')
    text = open(path).read()
    if GROK_BEGIN in text and GROK_END in text:
        head, _, rest = text.partition(GROK_BEGIN)
        _, _, tail = rest.partition(GROK_END)
    else:
        head, tail = text.rstrip('\n') + '\n\n', '\n'
    compat = re.compile(r'\s*\[\s*compat\s*\.\s*(claude|"claude")\s*\]\s*(#.*)?$')

    def hooks_off(lines):
        """Set hooks = false in a [compat.claude] table found in lines (edited in place); False if there is none."""
        at = next((i for i, l in enumerate(lines) if compat.match(l)), None)
        if at is None:
            return False
        end = next((i for i in range(at + 1, len(lines)) if lines[i].lstrip().startswith('[')), len(lines))
        key = next((i for i in range(at + 1, end) if re.match(r'\s*hooks\s*=', lines[i])), None)
        if key is None:
            lines.insert(at + 1, 'hooks = false')
        else:
            lines[key] = 'hooks = false'
        return True
    head_l, tail_l = head.split('\n'), tail.split('\n')
    theirs = hooks_off(head_l) or hooks_off(tail_l)   # the user's own [compat.claude], outside the block
    head, tail = '\n'.join(head_l), '\n'.join(tail_l)
    script = os.path.join(SHARED, 'hooks', 'agent_hook.py')
    rows = [GROK_BEGIN]
    if not theirs:
        rows += ['[compat.claude]', 'hooks = false', '']
    for event, name, timeout in GROK_HOOKS:
        cmd = json.dumps(f'python3 {script} --agent grok {name}')
        rows += [f'[[hooks.{event}]]', f'hooks = [{{ type = "command", command = {cmd}, timeout = {timeout} }}]', '']
    rows[-1] = GROK_END
    new = head + '\n'.join(rows) + tail
    if new == text:
        return
    try:
        cfg = tomllib.loads(new) if tomllib else {'compat': {'claude': {'hooks': False}}}
        assert cfg.get('compat', {}).get('claude', {}).get('hooks') is False
    except (tomllib.TOMLDecodeError, AssertionError, AttributeError) as e:
        return say('KEPT', f'{path}: the managed block would not parse ({e}) — fix the file by hand')
    if check:
        return say('would', f'write the hooks block in {path}')
    backup(path, '.bak-shared-agent-layer')
    open(path, 'w').write(new)
    say('set', f'hooks block in {os.path.relpath(path, HOME)}')


def edit_json(path, change, check, what):
    """Apply change(dict) to the JSON object in path, keeping key order and 2-space indent. change returns True
    when it altered the dict."""
    try:
        data = json.load(open(path)) if os.path.exists(path) else {}
    except ValueError as e:
        return say('KEPT', f'{path} is not valid JSON ({e}) — fix it by hand')
    if not isinstance(data, dict) or not change(data):
        return
    if check:
        return say('would', f'{what} in {path}')
    if os.path.exists(path):
        backup(path, '.bak-ui')
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write(json.dumps(data, indent=2, ensure_ascii=False) + '\n')
    os.replace(tmp, path)
    say('set', f'{what} in {path}')


def toml_value(v):
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, list):
        return '[' + ', '.join(json.dumps(x) for x in v) + ']'
    return json.dumps(v)


def set_codex_tui(check):
    """Set the CODEX_TUI keys inside the [tui] table of ~/.codex/config.toml, line by line, touching nothing else."""
    path = os.path.join(CODEX_HOME, 'config.toml')
    try:
        lines = open(path).read().split('\n')
    except OSError:
        return say('missing', f'{path} (run codex once first)')
    start = next((i for i, l in enumerate(lines) if re.match(r'\s*\[\s*(tui|"tui"|\'tui\')\s*\]\s*(#.*)?$', l)), None)
    if start is None:
        lines += ['', '[tui]']
        start = len(lines) - 1
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith('[')), len(lines))
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1                                   # insert above the blank lines that end the table
    new = list(lines)
    for key, value in CODEX_TUI.items():
        want = f'{key} = {toml_value(value)}'
        at = next((i for i in range(start + 1, end) if re.match(rf'\s*{key}\s*=', new[i])), None)
        if at is None:
            new.insert(end, want)
            end += 1
        elif new[at].strip() != want:
            new[at] = want
    if new == lines:
        return
    if check:
        return say('would', f'set [tui] {", ".join(CODEX_TUI)} in {path}')
    backup(path, '.bak-ui')
    open(path, 'w').write('\n'.join(new))
    say('set', f'[tui] {", ".join(CODEX_TUI)} in {path}')


def install_ui(check):
    """One palette and statusline for every agent. See ui/palette.py and ui/statusline.py."""
    ui = os.path.join(SHARED, 'ui')
    r = subprocess.run([sys.executable, os.path.join(ui, 'palette.py')] + (['--check'] if check else []),
                       capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if line.startswith(('wrote', 'would')):
            say('palette', line)
    for home in CLAUDE_HOMES:
        if not os.path.isdir(home):
            continue
        dst = os.path.join(home, 'statusline.py')
        link(os.path.join(ui, 'statusline.py'), dst, check)
        command = '~/' + os.path.relpath(dst, HOME)

        def claude_settings(d, command=command):
            want = {'type': 'command', 'command': command, 'padding': 0}
            changed = d.get('statusLine') != want or d.get('theme') != CLAUDE_THEME
            d['statusLine'], d['theme'] = want, CLAUDE_THEME
            perms = d.get('permissions')
            if not isinstance(perms, dict):
                perms = d['permissions'] = {}
            allow = perms.get('allow')
            if not isinstance(allow, list):
                allow = perms['allow'] = []
            for rule in CLAUDE_ALLOW:
                if rule not in allow:
                    allow.append(rule)
                    changed = True
            env = d.get('env')
            if not isinstance(env, dict):
                env = d['env'] = {}
            for key, value in CLAUDE_ENV.items():
                if env.get(key) != value:
                    env[key] = value
                    changed = True
            hooks = d.get('hooks')
            if not isinstance(hooks, dict):
                hooks = d['hooks'] = {}
            for event, matcher, cmd, timeout in CLAUDE_HOOKS:
                groups = hooks.get(event)
                if not isinstance(groups, list):
                    groups = hooks[event] = []
                if any(isinstance(h, dict) and h.get('command') == cmd
                       for g in groups if isinstance(g, dict) for h in g.get('hooks') or []):
                    continue
                group = {'matcher': matcher} if matcher else {}
                group['hooks'] = [{'type': 'command', 'command': cmd, 'timeout': timeout}]
                groups.append(group)
                changed = True
            return changed
        edit_json(os.path.join(home, 'settings.json'), claude_settings, check,
                  'statusLine, theme, Codex MCP allow, auto-compact env + reset hooks')
    set_codex_tui(check)


def git_ignore_locally(repo, name, check):
    """Add /name to the exclude file git reads, so the AGENTS.md link never shows in git status or a commit.

    The pattern is anchored (/AGENTS.md) so a real AGENTS.md deeper in the tree stays visible, and the path
    comes from git itself: a linked worktree's own info/exclude is NOT read, only the common dir's."""
    common = git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    if not common:
        return
    exclude, name = os.path.join(common, 'info', 'exclude'), '/' + name.lstrip('/')
    try:
        current = open(exclude).read() if os.path.exists(exclude) else ''
    except OSError:
        return
    lines, legacy = current.splitlines(), name.lstrip('/')
    if legacy in (l.strip() for l in lines):       # unanchored: it also hides nested AGENTS.md files
        if check:
            return say('would', f'anchor {legacy} as {name} in {exclude}')
        kept, seen = [], name in (l.strip() for l in lines)
        for l in lines:                            # rewrite the legacy line, or drop it if /AGENTS.md is already there
            if l.strip() != legacy:
                kept.append(l)
            elif not seen:
                kept.append(name)
                seen = True
        open(exclude, 'w').write('\n'.join(kept) + '\n')
        return say('anchored', f'{name} in {os.path.relpath(exclude, HOME)}')
    if name in (l.strip() for l in lines):
        return
    if check:
        return say('would', f'ignore {name} in {exclude}')
    os.makedirs(os.path.dirname(exclude), exist_ok=True)
    with open(exclude, 'a') as f:
        f.write(('' if current.endswith('\n') or not current else '\n') + f'{name}\n')
    say('ignored', f'{name} in {os.path.relpath(exclude, HOME)}')



def backup(path, suffix):
    """Copy path to path+suffix once; a later run keeps that first copy and adds a timestamped one beside it."""
    dest = path + suffix
    if os.path.exists(dest):
        dest += time.strftime('-%Y%m%d-%H%M%S')
    shutil.copy2(path, dest)


IMPORT_AGENTS = re.compile(r'(?:^|\s)@AGENTS\.md\b')


def claude_only_lines(claude_md):
    """How many lines CLAUDE.md adds beside an @AGENTS.md import, or None when it does not import AGENTS.md.

    create-next-app (and some umbrella folders) keep the briefing in a real AGENTS.md and import it from CLAUDE.md.
    Codex already reads that AGENTS.md, and a link back to CLAUDE.md would loop, so only the added lines are missed."""
    try:
        text = open(claude_md).read()
    except OSError:
        return None
    if not IMPORT_AGENTS.search(text):
        return None
    return sum(1 for l in text.splitlines() if l.strip() and l.strip() != '@AGENTS.md')


def install_project_rules(roots, check):
    """Every repo with a CLAUDE.md gets an AGENTS.md link to it, so Codex loads the same project briefing."""
    for root in roots:
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.scandir(root), key=lambda e: e.name):
            if entry.name.startswith('.') or not entry.is_dir():
                continue
            here = [entry.path] + [d.path for d in os.scandir(entry.path)
                                   if d.is_dir() and not d.name.startswith('.')]
            for repo in here:
                claude_md = os.path.join(repo, 'CLAUDE.md')
                # .sync-agents-skip opts a folder out, e.g. a CLAUDE.md that is a template, not a project briefing
                if not os.path.exists(claude_md) or os.path.exists(os.path.join(repo, '.sync-agents-skip')):
                    continue
                agents_md = os.path.join(repo, 'AGENTS.md')
                extra = claude_only_lines(claude_md)
                if extra is not None and os.path.isfile(agents_md) and not os.path.islink(agents_md):
                    if extra:              # a skill list is fine here; project rules belong in AGENTS.md
                        say('imports', f'{claude_md} imports AGENTS.md and adds {extra} lines outside models never read')
                    continue               # a real briefing, not our link: nothing to link, nothing to git-ignore
                top = git(repo, 'rev-parse', '--show-toplevel')
                if not top or os.path.realpath(top) != os.path.realpath(repo):
                    continue                       # only a repo's own top level, never a subfolder or umbrella
                link(claude_md, agents_md, check)
                git_ignore_locally(repo, 'AGENTS.md', check)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--check', action='store_true', help='report what would change, change nothing')
    ap.add_argument('--only', choices=[*TARGETS, 'projects', 'ui'],
                    help='sync one agent (or only the project links, or only the shared UI)')
    ap.add_argument('--repos', nargs='*', default=None,
                    help='folders of repos to give an AGENTS.md link (none listed: $CLUPAI_REPOS, else ~/projects). '
                         'Off unless given.')
    ap.add_argument('--ui', action='store_true',
                    help='also set the statusline and theme in Claude settings.json and Codex [tui] (off by default)')
    a = ap.parse_args()
    for agent in TARGETS:
        if a.only and a.only != agent:
            continue
        if TARGETS[agent].get('delta_only'):
            for home in TARGETS[agent]['homes']:
                link(TARGETS[agent]['delta'], home, a.check)
        else:
            build_agents_md(agent, a.check)
            for home in TARGETS[agent]['homes']:
                link(TARGETS[agent]['rules'], home, a.check, pending=True)
        install_skills(agent, a.check)
        install_commands(agent, a.check)
        {'codex': install_hooks, 'opencode': install_opencode_plugin, 'dsh': install_dsh_hooks,
         'kimi': install_kimi_hooks, 'grok': install_grok_hooks}[agent](a.check)
    if a.repos is not None and (not a.only or a.only == 'projects'):
        install_project_rules(a.repos or REPO_ROOTS, a.check)
    if a.ui or a.only == 'ui':
        install_ui(a.check)
    kept = [c for c in changes if c[0] == 'KEPT']
    imports = [c for c in changes if c[0] == 'imports']
    if len(kept) + len(imports) == len(changes):
        print('every agent is in sync with the shared layer'
              + (f', except {len(kept)} file(s) KEPT above because they differ — review those by hand' if kept else '')
              + (f'; {len(imports)} CLAUDE.md file(s) add lines beside their @AGENTS.md import (fine for a Claude '
                 f'skill list; move project rules into AGENTS.md)' if imports else ''))
    elif not a.check and any('hooks.json' in w and k == 'linked' for k, w in changes):
        print('\nNow launch `codex` once interactively and approve the hooks when it asks. '
              'Until you do, codex exec hangs on an untrusted hooks.json.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
