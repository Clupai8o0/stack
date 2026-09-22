#!/usr/bin/env python3
"""Code quality beyond "the tests pass": a blind Astra review of each contestant's passing diff on the long tasks.

For each (task, contestant) with a PASS in runs-r2/, build a fresh two-commit repo (the task's start tree plus its test
files, then the contestant's diff) and ask Astra, read-only, for defects with a severity. Astra never sees who wrote
the change, and Astra's own diffs are not scored (a model reviewing itself).

    python3 quality.py run [--tasks h1,h3,h4] [--authors kimi,ds-flash,opus,fable,gemini]
    python3 quality.py score
"""
import argparse, json, os, re, shutil, subprocess, threading, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS, OUT, WORK = HERE / 'runs-r2', HERE / 'quality-runs', HERE / 'work'
TASKS = {t['id']: dict(t, repo=cfg['repo']) for f in ('tasks-r2-fn.json', 'tasks-r2-app.json')
         for cfg in [json.loads((HERE / f).read_text())] for t in cfg['tasks']}
LOCK = threading.Lock()

PROMPT = """You are reviewing a change in a disposable copy of a repository. The change is the HEAD commit (read it with
`git show HEAD`); the tests it was written against pass. What it was asked to do:

{task}

Find concrete defects in HEAD that would make it behave wrongly or insecurely, or that miss part of what was asked:
logic errors, bypasses, races, wrong data written, security holes, unhandled failure paths. Skip style and naming.
List each on ONE line exactly like this, most severe first, at most 12:

P1|file:line|what goes wrong, with a concrete trigger
P2|file:line|...
P3|file:line|...

P1 = wrong or insecure in normal use, P2 = wrong in a realistic edge case, P3 = minor. If there is nothing real, reply
NONE. Read-only and headless: do not edit, do not ask questions, work alone."""


def sh(cmd, cwd=None, timeout=600, stdin=None):
    p = subprocess.Popen(cmd, shell=True, cwd=cwd, text=True, start_new_session=True,
                         stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        out, _ = p.communicate(stdin, timeout=timeout)
        return p.returncode, out
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, 9)
        return 124, '[timeout]'


def build(task, author, dest):
    src = Path(os.path.expanduser(task['repo']))
    start = task.get('start') or f'{task["commit"]}^'
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    env_git = 'git -c user.name=q -c user.email=q@local'
    steps = ['git init -q', f'git -C "{src}" archive {start} | tar -x']
    steps += [f'mkdir -p "$(dirname "{p}")" && git -C "{src}" show {task["commit"]}:{p} > "{p}"' for p in task['tests']]
    steps += ['git add -A', f'{env_git} commit -qm base']
    for s in steps:
        code, out = sh(s, cwd=dest)
        if code:
            raise RuntimeError(f'{s}: {out[-300:]}')
    diff = (RUNS / f'{task["id"]}__{author}.diff').read_text()
    code, out = sh('git apply --whitespace=nowarn -', cwd=dest, stdin=diff)
    if code:
        raise RuntimeError(f'apply failed: {out[-300:]}')
    sh('git add -A', cwd=dest)
    sh(f'{env_git} commit -qm "implement the task"', cwd=dest)


def one(task, author):
    out = OUT / f'{task["id"]}__{author}.json'
    if out.exists():
        return
    rec = json.loads((RUNS / f'{task["id"]}__{author}.json').read_text())
    if not rec.get('passed'):
        return
    wt = WORK / f'quality-{task["id"]}-{author}'
    build(task, author, wt)
    last = WORK / f'quality-{task["id"]}-{author}.txt'
    try:
        t0 = time.time()
        sh(f'codex exec --skip-git-repo-check -m gpt-6-astra -c model_reasoning_effort=high -s read-only '
           f'-C "{wt}" -o "{last}" -', cwd=wt, timeout=2400, stdin=PROMPT.format(task=task['task']))
        answer = last.read_text() if last.exists() else ''
        found = re.findall(r'^\s*[-*]?\s*(P[123])\s*\|', answer, re.M)
        out.write_text(json.dumps({'task': task['id'], 'author': author, 'secs': round(time.time() - t0),
                                   'P1': found.count('P1'), 'P2': found.count('P2'), 'P3': found.count('P3'),
                                   'answer': answer}, indent=1))
        with LOCK:
            print(f'{time.strftime("%H:%M:%S")} {task["id"]} {author}: P1={found.count("P1")} P2={found.count("P2")} '
                  f'P3={found.count("P3")}', flush=True)
    finally:
        last.unlink(missing_ok=True)
        shutil.rmtree(wt, ignore_errors=True)


def cmd_run(a):
    tasks = [t for tid, t in TASKS.items() if tid.split('-')[0] in a.tasks.split(',')]
    jobs = [(t, n) for t in tasks for n in a.authors.split(',')]
    lock = threading.Semaphore(2)  # two Astra reviews at a time
    def go(t, n):
        with lock:
            try:
                one(t, n)
            except Exception as e:
                with LOCK:
                    print(f'ERROR {t["id"]} {n}: {e!r}'[:300], flush=True)
    ths = [threading.Thread(target=go, args=j) for j in jobs]
    for th in ths:
        th.start()
    for th in ths:
        th.join()


def cmd_score(a):
    recs = [json.loads(p.read_text()) for p in sorted(OUT.glob('*.json'))]
    by = {}
    for r in recs:
        s = by.setdefault(r['author'], {'P1': 0, 'P2': 0, 'P3': 0, 'n': 0})
        for k in ('P1', 'P2', 'P3'):
            s[k] += r[k]
        s['n'] += 1
    print('| author | tasks reviewed | P1 | P2 | P3 | weighted (3/2/1) |\n|---|---|---|---|---|---|')
    for n, s in sorted(by.items(), key=lambda kv: 3 * kv[1]['P1'] + 2 * kv[1]['P2'] + kv[1]['P3']):
        print(f'| {n} | {s["n"]} | {s["P1"]} | {s["P2"]} | {s["P3"]} | {3 * s["P1"] + 2 * s["P2"] + s["P3"]} |')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['run', 'score'])
    ap.add_argument('--tasks', default='h1,h3,h4')
    ap.add_argument('--authors', default='kimi,ds-flash,opus,fable')
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    {'run': cmd_run, 'score': cmd_score}[a.cmd](a)


if __name__ == '__main__':
    main()
