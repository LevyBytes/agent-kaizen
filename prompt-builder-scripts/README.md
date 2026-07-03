# Prompt Builder Scripts

This folder holds small Python scripts that ask a few terminal questions and
turn the answers into paste-ready prompts. They are meant for day-to-day use in
VS Code with Claude, Codex, Copilot, or any other LLM the user chooses.

The scripts do not call AI APIs and do not generate images or code by
themselves. They only build clear prompts, copy the prompt to the clipboard
when supported, and print the prompt in the terminal so it can always be copied
manually.

## Quick Start

From the repo root:

```powershell
python .\prompt-builder-scripts\<category>\<script-name>.py
```

Scripts that live at the folder root (no category) are invoked without one, for example
`python .\prompt-builder-scripts\generate-prompt-for-new-script.py`.

Press Enter to accept the default shown for a question. Most runs should take
less than a minute once the settings are established.

## Finding A Script

Scripts are organized into category subfolders (for example, an
image-generation category). To see what a script does, open it and read its
first line — every script starts with a one-line docstring describing its
purpose. This README deliberately does not list scripts, so it stays accurate
no matter how many are added.

## How The Scripts Behave

- **Sibling log:** each script creates a `.log` file beside itself with the same
  base name. The log stores current settings and compact run history.
- **Prompts are not logged:** generated prompt text is printed and may be copied
  or backed up, but it is not written into the sibling log. The history stores
  the answers instead — the same answers reproduce the same prompt, and typing
  `last` at the first question reprints the previous prompt.
- **Clipboard first, terminal always:** scripts try to copy the paste-ready
  prompt to the clipboard. They still print the full prompt between clear
  markers so the user can copy it manually.
- **One-click launch protection:** scripts that may be launched outside the VS
  Code terminal keep the terminal open at the end when needed, so the prompt is
  not lost.
- **Optional backups:** scripts may ask whether to save a Markdown copy of the
  generated prompt under the ignored `user/` area. These backups are local user
  artifacts, not files intended for the online repo.

## Where Will You Run The Prompt? (extension vs online)

Some scripts ask whether the generated prompt will run in a **VS Code AI
extension** (Claude or Codex-like, which can read your repo files) or an
**online external LLM** (ChatGPT, Claude web, etc., which cannot). The choice
shapes the output: the extension version may reference repo files such as
`CLAUDE.md` or `AGENTS.md` (by absolute path) and the `.agents` folder; the
online version is fully self-contained and never references repo files, since
the online LLM can't open them. For an extension you also pick which one
(Claude follows `CLAUDE.md`; Codex-like follows `AGENTS.md` + `.agents`).

Every question explains itself and shows its default. At any question you can
press **Enter** to keep the default, **type** a new value, **`m`** to enter a
multi-line answer (paste, then a line containing only `END`), **`b`** to go back
to the previous question, or **`q`** to quit. Pasting several lines directly will
spill into later questions — use `m` for multi-line answers.

## Sessions (Continue / New)

Some scripts open with a **Continue / New** choice. Starting **New** asks for a
short session name and records a short _root hash_ in the log's `SESSIONS` zone.
**Continue** lists your named sessions so you can resume one — its previous
answers come back as editable defaults, and new runs are tagged under the same
root hash. This keeps separate projects distinct and lets you pick up where you
left off without retyping long answers.

## Optional LLM Assist (copy-paste)

Some scripts offer an optional, default-on assist loop that works with **any**
LLM — ChatGPT, Claude, Codex, Copilot — with no API key and no cost. The script
copies a request to your clipboard; you paste it into your LLM, then paste the
reply back. Suggestions become **editable defaults** for your answers, so the
final prompt is still assembled deterministically and `last` still reproduces it
exactly. End a pasted reply with a line containing only `<<END>>`; press `s` to
skip the loop and generate immediately.

Reply parsing is more robust with one optional, pure-Python package:

```powershell
pip install json-repair
```

The script still runs without it (it falls back to standard-library parsing and,
when a reply can't be parsed, shows it as advice you apply by hand). This is the
only optional dependency in the folder; scripts remain runnable stdlib-only.

A script that uses the assist loop also keeps a third `DEVELOPMENT LOG` zone in
its sibling `.log` recording the decisions made and work done per run (it
references the prompt archive path and never stores prompt text).

## Conventions For New Scripts

New scripts in this folder should follow the same pattern:

- Python 3.10+ and standard library only.
- **Fully self-contained:** one file per script, with all prompt templates
  embedded as string constants. Scripts never read template text from other
  files at runtime and never import from sibling scripts, so any single script
  can be copied out of the repo and still work.
- UTF-8 file I/O and `pathlib` for paths.
- Interactive terminal questions with visible defaults.
- Single number or letter shortcuts for controls (modes and commands);
  free-form typing only for descriptive values.
- A sibling `.log` with a `SETTINGS` zone and append-only `RUN HISTORY` zone.
- No generated prompt text in sibling logs.
- Clear terminal markers around the generated prompt.
- Clipboard copy when supported, with a printed fallback.
- Function and class docstrings so people learning Python can understand the
  code, starting with a one-line module docstring that names the script's
  purpose (this is how scripts are discovered).
- Verification tests are local working artifacts, standard library only. They
  live under the repo's gitignored `AI/work/` area (for example
  `AI/work/tests/`); only create new repo folders when the user explicitly
  asks for them.

## Verification

Local verification tests live under the gitignored `AI/work/tests/` area when
present. Run them from the repo root:

```powershell
python -m unittest discover -s AI/work/tests
```

The tests use sandbox copies where needed so the user's real sibling logs are
not changed during verification. Because `AI/work/` is gitignored, these tests
are local working artifacts and are not part of the published repo.
