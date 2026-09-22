#!/usr/bin/env bash
# Link this repo into Claude Code (and optionally Codex, OpenCode, dsh, kimi).
#
#   ./install.sh                 print what would happen, change nothing
#   ./install.sh --yes           do it
#   ./install.sh --yes --settings   also merge the hooks into each settings.json (backup first)
#   ./install.sh --yes --agents     also run scripts/sync-agents.py --check (read-only) and print the next step
#   ./install.sh --yes --no-rules   leave your CLAUDE.md alone (add @$CLUPAI_HOME/rules/CLAUDE.md to it yourself)
#
# Without --no-rules, <config dir>/CLAUDE.md is REPLACED by a link to rules/CLAUDE.md (the old one is backed up).
#
# Env:
#   CLUPAI_HOME   where the shared layer lives (default ~/clupai; linked to this repo if missing)
#   CLAUDE_DIRS   Claude config dirs, space-separated (default ~/.claude)
#
# Never overwrites a file: anything in the way is moved to <name>.bak-clupai-<time> first.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUPAI_HOME="${CLUPAI_HOME:-$HOME/clupai}"
CLAUDE_DIRS="${CLAUDE_DIRS:-$HOME/.claude}"
STAMP="$(date +%Y%m%d-%H%M%S)"
CORE_SKILLS="token-economy tool-scoping vault-skills"

YES=0
SETTINGS=0
AGENTS=0
RULES=1
for arg in "$@"; do
    case "$arg" in
        --yes) YES=1 ;;
        --settings) SETTINGS=1 ;;
        --agents) AGENTS=1 ;;
        --no-rules) RULES=0 ;;
        -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

PLAN=()
plan()
{
    PLAN+=("$1")
}

# link SRC DST: plan a symlink, with a backup if something else is there
link()
{
    local src="$1" dst="$2"
    if [ -L "$dst" ] && [ "$(readlink "$dst")" = "$src" ]; then
        return
    fi
    if [ -e "$dst" ] || [ -L "$dst" ]; then
        plan "backup  $dst -> $dst.bak-clupai-$STAMP"
    fi
    plan "link    $dst -> $src"
}

do_link()
{
    local src="$1" dst="$2"
    if [ -L "$dst" ] && [ "$(readlink "$dst")" = "$src" ]; then
        return
    fi
    mkdir -p "$(dirname "$dst")"
    if [ -e "$dst" ] || [ -L "$dst" ]; then
        mv "$dst" "$dst.bak-clupai-$STAMP"
        echo "backup  $dst -> $dst.bak-clupai-$STAMP"
    fi
    ln -s "$src" "$dst"
    echo "linked  $dst -> $src"
}

# 1. where the shared layer lives
HOME_LINK=0
if [ "$(cd "$CLUPAI_HOME" 2>/dev/null && pwd -P)" != "$(cd "$REPO" && pwd -P)" ]; then
    if [ -e "$CLUPAI_HOME" ] || [ -L "$CLUPAI_HOME" ]; then
        echo "CLUPAI_HOME=$CLUPAI_HOME exists and is not this repo. Set CLUPAI_HOME=$REPO or move it aside." >&2
        exit 1
    fi
    HOME_LINK=1
    plan "link    $CLUPAI_HOME -> $REPO"
fi

# 2. your own config, from the examples
[ -e "$REPO/model-policy.json" ] || plan "copy    examples/model-policy.example.json -> model-policy.json (edit it)"
[ -e "$REPO/guardrails.json" ] || plan "copy    examples/guardrails.example.json -> guardrails.json (~ expanded)"

# 3. each Claude config dir
for dir in $CLAUDE_DIRS; do
    if [ "$RULES" = 1 ]; then
        link "$CLUPAI_HOME/rules/CLAUDE.md" "$dir/CLAUDE.md"
    fi
    link "$CLUPAI_HOME/output-styles/on-the-go.md" "$dir/output-styles/on-the-go.md"
    for s in $CORE_SKILLS; do
        link "$CLUPAI_HOME/skills/_core/$s" "$dir/skills/$s"
    done
    if [ "$SETTINGS" = 1 ]; then
        plan "merge   hooks and the Codex MCP allow rule into $dir/settings.json (edits Claude settings; backup first)"
    fi
done

plan "run     scripts/build_index.py (writes skills/INDEX.md)"
if [ "$AGENTS" = 1 ]; then
    plan "run     scripts/sync-agents.py --check (read-only). The real run, which you start yourself, edits"
    plan "        Codex hooks.json/config.toml, kimi and grok config.toml, dsh profiles; with --ui also Claude"
    plan "        settings.json (statusLine, theme) and Codex [tui]"
fi

echo "CLUPAI_HOME=$CLUPAI_HOME"
echo "Claude config dirs: $CLAUDE_DIRS"
echo
if [ ${#PLAN[@]} -eq 0 ]; then
    echo "Nothing to do."
    exit 0
fi
printf '  %s\n' "${PLAN[@]}"
echo
if [ "$YES" != 1 ]; then
    echo "Dry run. Re-run with --yes to do this."
    exit 0
fi

if [ "$HOME_LINK" = 1 ]; then
    do_link "$REPO" "$CLUPAI_HOME"
fi

if [ ! -e "$REPO/model-policy.json" ]; then
    cp "$REPO/examples/model-policy.example.json" "$REPO/model-policy.json"
    echo "copied  model-policy.json"
fi
if [ ! -e "$REPO/guardrails.json" ]; then
    # guardrails.py compares absolute folders, so expand ~ once here
    sed "s#\"~/#\"$HOME/#g" "$REPO/examples/guardrails.example.json" > "$REPO/guardrails.json"
    echo "copied  guardrails.json"
fi

for dir in $CLAUDE_DIRS; do
    if [ "$RULES" = 1 ]; then
        do_link "$CLUPAI_HOME/rules/CLAUDE.md" "$dir/CLAUDE.md"
    fi
    do_link "$CLUPAI_HOME/output-styles/on-the-go.md" "$dir/output-styles/on-the-go.md"
    for s in $CORE_SKILLS; do
        do_link "$CLUPAI_HOME/skills/_core/$s" "$dir/skills/$s"
    done
    if [ "$SETTINGS" = 1 ]; then
        python3 - "$dir/settings.json" "$REPO/examples/claude-settings.example.json" "$STAMP" <<'PY'
import json, os, shutil, sys
path, example, stamp = sys.argv[1:4]
want = json.load(open(example))
data = json.load(open(path)) if os.path.exists(path) else {}
if os.path.exists(path):
    shutil.copy2(path, f'{path}.bak-clupai-{stamp}')
hooks = data.setdefault('hooks', {})
added = 0
for event, groups in want['hooks'].items():
    have = json.dumps(hooks.get(event, []))
    for g in groups:
        cmd = g['hooks'][0]['command']
        if json.dumps(cmd) not in have:
            hooks.setdefault(event, []).append(g)
            added += 1
allow = data.setdefault('permissions', {}).setdefault('allow', [])
for rule in want['permissions']['allow']:
    if rule not in allow:
        allow.append(rule)
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, 'w') as f:
    f.write(json.dumps(data, indent=2) + '\n')
print(f'merged  {added} hook(s) into {path}')
PY
    fi
done

CLUPAI_HOME="$CLUPAI_HOME" python3 "$REPO/scripts/build_index.py"

if [ "$AGENTS" = 1 ]; then
    CLUPAI_HOME="$CLUPAI_HOME" python3 "$REPO/scripts/sync-agents.py" --check
    echo
    echo "Read the list above. To apply it (backups are timestamped):"
    echo "  CLUPAI_HOME=\"$CLUPAI_HOME\" python3 \"$REPO/scripts/sync-agents.py\"            # agents only"
    echo "  add --ui for the shared statusline and theme, --repos DIR for project AGENTS.md links"
fi

echo
echo "Done. Add this to your shell profile: export CLUPAI_HOME=\"$CLUPAI_HOME\""
