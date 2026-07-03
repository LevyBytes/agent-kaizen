# Skills — Master Index

This public release ships with **no bundled skills**.

Skill packages are maintained in a separate store and surfaced here through per-skill directory junctions
(Windows) or symlinks (Linux/macOS): `.agents/skills/<name>` and `.claude/skills/<name>` point into a sibling
skills store at `$DEVROOT/SKILLS/skills/<name>`. Only this `INDEX.md` is tracked in the repo; the junctions
themselves are local and gitignored.

## Adding skills

- Run `setup/link-skills.ps1` (Windows) or `setup/link-skills.sh` (Linux/macOS) to clone a skills store you
  choose and link its skills here.
- Or drop a skill folder directly into this directory. Each skill needs a `SKILL.md` plus an `evals/` surface —
  see [`Kaizen_System.md`](../../Kaizen_System.md) §7.
- Regenerate this index once skills exist with `skill-drafting`'s `skill_builder.py index`.

(No skills present.)
