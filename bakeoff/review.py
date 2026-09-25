#!/usr/bin/env python3
"""Review bake-off: can a model find the real bugs a later review caught, in a change that passed its tests?

Each case is a commit whose defects a later commit fixed. A reviewer gets a fresh two-commit repo (the parent
tree, then the buggy commit with its own message) so it cannot read the fix from history, and is asked to review
HEAD. Answers are saved under review-runs/ with random ids so they can be graded blind against review-cases.json.

    python3 review.py run [--cases c1,..] [--reviewers fable,astra,kimi,ds-flash]
    python3 review.py blind        # print answers under their random ids, for grading
    python3 review.py score        # grades.json (id -> list of bug keys found) -> table per reviewer
"""
import argparse, json, os, random, shutil, subprocess, sys, threading, time
from pathlib import Path
CLUPAI_HOME = os.path.expanduser(os.environ.get('CLUPAI_HOME') or (
    '~/clupai' if os.path.isdir(os.path.expanduser('~/clupai'))
    else os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))  # where this repo is installed

HERE = Path(__file__).resolve().parent
CASES = json.loads((HERE / 'review-cases.json').read_text())
OUT, WORK = HERE / 'review-runs', HERE / 'work'
SO = os.path.join(CLUPAI_HOME, 'scripts/second_opinion.py')
TIMEOUT = 40 * 60
REVIEWERS = {
    'fable': ('claude', 'claude-fable-5-1'),
    'opus': ('claude', 'claude-opus-5'),
    'opus55': ('claude', 'claude-opus-5-5'),
    'opus55-r2': ('claude', 'claude-opus-5-5'),   # second run, same setup
    'fable-r2': ('claude', 'claude-fable-5-1'),
    'astra': ('codex', 'gpt-6-astra'),
    'kimi': ('so', 'kimi:kimi-k3'),
    'ds-flash': ('so', 'dsh:deepseek-flash'),
    'gemini': ('so', 'agy:gemini-3.8-flash-high'),
    'ds41-or': ('so', 'opencode:openrouter/deepseek/deepseek-v4.1-flash'),
    'mimo-or': ('so', 'opencode:openrouter/xiaomi/mimo-v2.6-pro'),
    'hy4-or': ('so', 'opencode:openrouter/tencent/hy4-preview'),
    'grok47-or': ('so', 'opencode:openrouter/x-ai/grok-4.7'),
    'qwenmax-or': ('so', 'opencode:openrouter/qwen/qwen3.8-max-0902'),
    'fugu-or': ('so', 'opencode:openrouter/sakana/fugu-max'),
    'glmflash-or': ('so', 'opencode:openrouter/z-ai/glm-5.3-flash'),
    'grok-build': ('so', 'grok:grok-4.7'),
    'muse-or': ('so', 'opencode:openrouter/meta/muse-spark-1.3'),
    'glm-or': ('so', 'opencode:openrouter/z-ai/glm-5.3'),
    'grok-or': ('so', 'opencode:openrouter/x-ai/grok-4.6'),
    'minimax-or': ('so', 'opencode:openrouter/minimax/minimax-m3'),
}
LOCK = threading.Lock()
if os.environ.get('BAKEOFF_NODE_BIN'):   # e.g. a pinned Node version's bin dir
    os.environ['PATH'] = os.environ['BAKEOFF_NODE_BIN'] + ':' + os.environ['PATH']

PROMPT = """You are reviewing a change in a disposable copy of a repository ({what}). The change under review is the
HEAD commit: read it with `git show HEAD` (and `git show HEAD --stat`), and read any surrounding code you need.
Its tests passed when it was written.

Find concrete defects that would make it behave wrongly or insecurely: logic errors, ways a check can be bypassed,
races, wrong data written, security holes. For each one give: the file and line, what goes wrong, and a concrete
scenario that triggers it. Most severe first, at most 10. Skip style, naming and missing-docs remarks. If you find
nothing real, say NONE.

This is a read-only, headless review: do not edit files, do not run git commands that change anything, do not use the
network, and do not ask questions. Work alone (no subagents, no other models, no MCP tools)."""


def sh(cmd, cwd=None, timeout=600, env=None, stdin=None):
    p = subprocess.Popen(cmd, shell=True, cwd=cwd, env=env, text=True, start_new_session=True,
                         stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        out, _ = p.communicate(stdin, timeout=timeout)
        return p.returncode, out
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, 9)
        out, _ = p.communicate()
        return 124, (out or '') + '\n[timeout]'


def fresh_repo(case, dest):
    """A new repo holding only the buggy commit's parent tree and the buggy commit, so the fix is not in history."""
    src = Path(os.path.expanduser(case['repo']))
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    env = dict(os.environ, GIT_AUTHOR_NAME='review', GIT_AUTHOR_EMAIL='review@local',
               GIT_COMMITTER_NAME='review', GIT_COMMITTER_EMAIL='review@local')
    msg = subprocess.run(['git', '-C', str(src), 'log', '-1', '--format=%B', case['commit']],
                         capture_output=True, text=True).stdout
    steps = [f'git init -q', f'git -C "{src}" archive {case["commit"]}^ | tar -x', 'git add -A',
             'git commit -qm base', f'git ls-files -z | xargs -0 rm -f',
             f'git -C "{src}" archive {case["commit"]} | tar -x', 'git add -A']
    for s in steps:
        code, out = sh(s, cwd=dest, env=env)
        if code:
            raise RuntimeError(f'{s}: {out[-300:]}')
    subprocess.run(['git', 'commit', '-qF', '-'], cwd=dest, env=env, input=msg, text=True, check=True)


def review(case, name):
    out_json = OUT / f'{case["id"]}__{name}.json'
    if out_json.exists():
        return
    kind, model = REVIEWERS[name]
    if name.endswith('-or'):
        sys.path.insert(0, str(HERE))
        import bakeoff
        if bakeoff.openrouter_balance() < bakeoff.OR_FLOOR:
            return
    wt = WORK / f'review-{case["id"]}-{name}'
    fresh_repo(case, wt)
    prompt = PROMPT.format(what=case['what'])
    t0 = time.time()
    cost = None
    try:
        if kind == 'claude':
            env = dict(os.environ, CLAUDE_CONFIG_DIR=os.path.expanduser(os.environ.get('BAKEOFF_CLAUDE_CONFIG_DIR', '~/.claude')))
            cmd = (f'claude -p --model {model} --effort high --output-format json --permission-mode bypassPermissions '
                   f'--strict-mcp-config --disallowedTools Agent,Edit,Write --max-budget-usd 10')
            for _ in range(6):
                code, out = sh(cmd, cwd=wt, timeout=TIMEOUT, env=env, stdin=prompt)
                if 'hit your session limit' not in out:
                    break
                time.sleep(1800)
            try:
                d = json.loads(out[out.index('{'):])
                answer, cost = str(d.get('result')), d.get('total_cost_usd')
            except ValueError:
                answer = out
        elif kind == 'codex':
            last = WORK / f'review-{case["id"]}-{name}.txt'
            code, out = sh(f'codex exec --skip-git-repo-check -m {model} -c model_reasoning_effort=high -s read-only '
                           f'-C "{wt}" -o "{last}" -', cwd=wt, timeout=TIMEOUT, stdin=prompt)
            answer = last.read_text() if last.exists() else out
            last.unlink(missing_ok=True)
        else:
            pf = WORK / f'review-{case["id"]}-{name}.prompt'
            pf.write_text(prompt)
            code, out = sh(f'python3 "{SO}" "{pf}" --cwd "{wt}" --ladder {model} --timeout {TIMEOUT}',
                           cwd=wt, timeout=TIMEOUT + 120)
            lines = out.splitlines()
            answer = '\n'.join(l for l in lines if not l.startswith('{"p'))
            pf.unlink(missing_ok=True)
        if name.endswith('-or'):  # an OpenRouter reviewer stops when the key is nearly spent
            pass
        out_json.write_text(json.dumps({'case': case['id'], 'reviewer': name, 'secs': round(time.time() - t0),
                                        'cost_usd': cost, 'exit': code, 'answer': answer,
                                        'blind_id': f'{random.randrange(16**6):06x}'}, indent=1))
        with LOCK:
            print(f'{time.strftime("%H:%M:%S")} {case["id"]:14} {name:9} {round(time.time() - t0)}s', flush=True)
    finally:
        shutil.rmtree(wt, ignore_errors=True)


def cmd_run(a):
    cases = [c for c in CASES if not a.cases or c['id'] in a.cases.split(',')]
    names = a.reviewers.split(',')
    lanes = {}
    for n in names:   # one lane per reviewer, cases in order; kimi and dsh stay serial for clean balances
        lanes[n] = [(c, n) for c in cases]
    def lane(jobs):
        for c, n in jobs:
            try:
                review(c, n)
            except Exception as e:
                with LOCK:
                    print(f'ERROR {c["id"]} {n}: {e!r}'[:300], flush=True)
    ths = [threading.Thread(target=lane, args=(j,)) for j in lanes.values()]
    for t in ths:
        t.start()
    for t in ths:
        t.join()


def cmd_blind(a):
    recs = [json.loads(p.read_text()) for p in sorted(OUT.glob('*__*.json'))]
    random.shuffle(recs)
    for r in sorted(recs, key=lambda r: r['case']):
        print(f'\n===== case {r["case"]}  id {r["blind_id"]} =====\n{r["answer"].strip()[:6000]}')


def cmd_score(a):
    grades = json.loads((HERE / 'review-grades.json').read_text())   # blind_id -> [bug keys]
    recs = [json.loads(p.read_text()) for p in sorted(OUT.glob('*__*.json'))]
    keys = {c['id']: [b['key'] for b in c['bugs']] for c in CASES}
    total = sum(len(v) for v in keys.values())
    found = {}
    for r in recs:
        found.setdefault(r['reviewer'], set()).update(f'{r["case"]}:{k}' for k in grades.get(r['blind_id'], []))
    everyone = set().union(*found.values()) if found else set()
    print(f'| reviewer | bugs found (of {total}) | found by no other reviewer |')
    print('|---|---|---|')
    for n, s in sorted(found.items(), key=lambda kv: -len(kv[1])):
        others = set().union(*(v for m, v in found.items() if m != n))
        print(f'| {n} | {len(s)} | {len(s - others)}: {", ".join(sorted(s - others)) or "-"} |')
    missed = sorted(f'{c}:{k}' for c, ks in keys.items() for k in ks if f'{c}:{k}' not in everyone)
    print(f'\nfound by nobody: {", ".join(missed) or "-"}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['run', 'blind', 'score'])
    ap.add_argument('--cases')
    ap.add_argument('--reviewers', default='fable,astra,kimi,ds-flash')
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    WORK.mkdir(exist_ok=True)
    {'run': cmd_run, 'blind': cmd_blind, 'score': cmd_score}[a.cmd](a)


if __name__ == '__main__':
    main()
