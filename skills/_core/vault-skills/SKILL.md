---
name: vault-skills
description: Find and load a skill that is not installed. Use when a task needs specialised know-how that no installed skill covers, such as UI or UX design, animation or GSAP, brand and visual design, pptx or xlsx files, evaluation or context engineering, TDD or debugging method, production-readiness or launch audits ("is X production ready?"), or Obsidian formats. Looks it up in the skill library ($CLUPAI_HOME/skills) instead of keeping every skill installed.
---

# Vault skills

Most skills live in the skill library, not in `~/.claude/skills`. That keeps every session and every subagent small. This skill is how to reach them.

## Steps

1. Read `$CLUPAI_HOME/skills/INDEX.md`. One line per skill: name, category, when to use.
2. Pick at most two skills that clearly match the task. If nothing matches, carry on without one.
3. Read `$CLUPAI_HOME/skills/<category>/<name>/SKILL.md` in full and follow it as if it had been invoked.
4. Resolve any relative path in that file (scripts, references, data) against its own folder.
5. Read reference files only when the SKILL.md tells you to. Never read a whole category.

## Keep the library honest

- A skill used often in one project belongs in that project: link it into `<project>/.claude/skills/<name>`. Ask first, in the decisions checklist.
- A skill used often everywhere belongs in `skills/_core/` and gets linked into both accounts. Ask first.
- After adding, moving or editing a skill, regenerate the index in the same step:
  `python3 $CLUPAI_HOME/scripts/build_index.py`
