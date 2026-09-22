#!/usr/bin/env python3
"""Rebuild skills/INDEX.md from every SKILL.md in the vault skill library."""
import os, re
CLUPAI_HOME = os.path.expanduser(os.environ.get('CLUPAI_HOME') or (
    '~/clupai' if os.path.isdir(os.path.expanduser('~/clupai'))
    else os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))  # where this repo is installed
LIB = os.path.join(CLUPAI_HOME, 'skills')
rows = []
for cat in sorted(os.listdir(LIB)):
    cdir = os.path.join(LIB, cat)
    if cat.startswith(('.', '_tools')) or not os.path.isdir(cdir):
        continue
    for name in sorted(os.listdir(cdir)):
        f = os.path.join(cdir, name, 'SKILL.md')
        if not os.path.isfile(f):
            continue
        text = open(f, errors='ignore').read()
        desc = ''
        fm = re.match(r'\A---\s*\n(.*?)\n---\s*(\n|\Z)', text, re.S)
        if fm:
            try:
                import yaml
                meta = yaml.safe_load(fm.group(1)) or {}
                desc = str(meta.get('description') or '') if isinstance(meta, dict) else ''
            except Exception:
                m = re.search(r'^description:\s*(.+)$', fm.group(1), re.M)
                desc = m.group(1) if m else ''
        desc = ' '.join(desc.split()).strip('"\'').replace('|', '/')
        first = re.split(r'(?<=[.!?])\s', desc, maxsplit=1)[0]
        rows.append((cat, name, first[:160]))
out = ['# Skill library index', '',
       'One line per skill. `_core` skills are installed in both Claude accounts. Everything else loads on demand through the `vault-skills` skill: read `<category>/<name>/SKILL.md`.',
       '', 'Regenerate with `python3 scripts/build_index.py`. Do not edit by hand.', '',
       '| Skill | Category | Use when |', '|---|---|---|']
out += [f'| `{n}` | {c} | {d} |' for c, n, d in rows]
open(os.path.join(LIB, 'INDEX.md'), 'w').write('\n'.join(out) + '\n')
print(len(rows), 'skills indexed')
