# Skills — Master Index

Catalog of the skill packages currently surfaced under `.claude/skills/`, with their covered entities and overlaps. Package validation and host policy remain authoritative; this file is a generated discovery view.

## Skills

| Skill | Covers (top) | Trigger |
| --- | --- | --- |
| [ableton-live](ableton-live/SKILL.md) | reference, live-concepts-part-1, live-concepts-part-2, arrangement-view-part-1, arrangement-view-part-2, session-view, clip-view-part-1, clip-view-part-2 … | Use when working with Ableton Live 12 (the DAW): the Arrangement and Session views; clips and the Clip View; audio warping and tempo; MIDI and MPE editing; MIDI tools; converting audio to MIDI; compin |
| [adobe-products](adobe-products/SKILL.md) | acrobat, after-effects, audition, common-platform, firefly, illustrator, incopy, indesign … | Use when Codex or Claude needs Adobe product documentation, workflow, scripting, plugin, SDK, or service API facts; routes Acrobat, Photoshop, Illustrator, InDesign, InCopy, Premiere Pro, After Effect |
| [blender](blender/SKILL.md) | actions-fcurves, addons-panels, cameras-lights, linked-libraries, material-nodes, python-api | Use when work depends on Blender Manual or Blender Python API facts: bpy scripting, operators, add-ons and extensions; actions, F-curves, keyframes and animation; materials, shader nodes, textures and |
| [chrome-extensions](chrome-extensions/SKILL.md) | permissions, optional-permissions, service worker, alarms, oninstalled, onmessage, offscreen, scripting … | Use when building, scaffolding, or debugging Chrome, Chromium, or Edge Manifest V3 browser extensions: service workers, content scripts, popups, options pages, side panels, omnibox, context menus, mes |
| [cli-design](cli-design/SKILL.md) | stdout, stderr, secrets, --no-input, no-color, tty, arguments, flags … | Use when designing, building, reviewing, testing, or improving how a terminal program interacts with people - command and subcommand naming, arguments, flags, defaults, prompts and confirmations, help |
| [davinci-resolve](davinci-resolve/SKILL.md) | reference, getting-started, davinci-resolve-interface, setup-and-workflows, davinci-control-panels-setup, project-settings-part-1, project-settings-part-2, project-settings-part-3 … | Use when working in DaVinci Resolve (the Blackmagic Design editing, color, VFX, and audio app): the Media, Cut, Edit, Fusion, Color, Fairlight, and Deliver pages; importing and managing media, proxies |
| [discord-developers](discord-developers/SKILL.md) | design-patterns, local-development-mobile, user-actions, how-activities-work-overview, bots-overview, change-log, components-overview, components-reference … | Use when building on the Discord developer platform: bots and apps, slash/application commands and interactions (buttons, select menus, modals, message components), the HTTP/REST API and Gateway (WebS |
| [gimp](gimp/SKILL.md) | recipe, procedure browser, selection, feather, save to channel, paths, levels, curves … | Use when working with GIMP, the GNU Image Manipulation Program (especially GIMP 3.2) - editing or generating raster images; working with layers, layer/blend modes, masks, channels, selections, or Bézi |
| [git](git/SKILL.md) | reflog, refspec, init, clone, config, gitignore, user.name, scopes … | Use when working with Git version control - running or explaining git commands, subcommands, options/flags, refspecs, or configuration keys; staging and committing; branching, switching, merging, reba |
| [github](github/SKILL.md) | gh pr, runs-on, permissions, github-token, gh repo, branch protection, rulesets, required reviews … | Use when working with GitHub (the platform on top of git) - creating or managing repositories, branches, and branch protection / rulesets; opening, reviewing, or merging pull requests (merge vs squash |
| [lumberyard](lumberyard/SKILL.md) | animation, audio, entities, materials, overview, vfx-rendering | Use when work depends on AWS Lumberyard or CryEngine-family facts: the editor, asset pipeline, Cloud Canvas and AWS gems, and Lua scripting; entities, components, EBus, slices and prefabs; CGA/CAF/CHR |
| [powershell-vsdevshell](powershell-vsdevshell/SKILL.md) | artifact routing, powershell scripts, repo setup scripts, generated scripts, windows compatibility reviews, native executable invocation, quoted tool paths, argument arrays … | Windows PowerShell 5.1 and Visual Studio Developer Shell guidance for any Windows VS Code repository. |
| [pymeasure](pymeasure/SKILL.md) | reference, latest, reporting-errors, properties, advanced-communication-tests, channels, instrument-solutions, coding-standards-contribute … | Use when working with PyMeasure - the Python library for scientific measurement, instrument control, and experiment automation (built on PyVISA): the Instrument/Channel base classes and dynamic proper |
| [pyvisa](pyvisa/SKILL.md) | reference, highlevel-part-2, constants-part-2, constants-resourcemanager, visalibrarybase-part-1, resources-part-1, messagebased-part-2, latest … | Use when working with PyVISA - the Python library for controlling measurement instruments over VISA (GPIB, USB, serial/RS-232, TCPIP/Ethernet): ResourceManager, listing/opening resources, read/write/q |
| [skill-drafting](skill-drafting/SKILL.md) | references, skill, skill.md, frontmatter, description, scripts, assets, progressive disclosure … | Use when creating, building, updating, reviewing, validating, or publishing Codex or Claude skills (skill-building): drafting SKILL.md files, trigger descriptions, references/scripts/assets, gotchas, |
| [tauri-develop](tauri-develop/SKILL.md) | configuration-files, calling-rust, calling-frontend, resources-sidecar, vscode, plugins, develop-mobile, mocking … | Use when developing a Tauri v2 desktop or mobile app: project configuration (tauri.conf.json), calling Rust commands from the frontend and calling the frontend from Rust (commands, events, channels), |
| [turso-db](turso-db/SKILL.md) | agentfs, cli, database-core, platform-api, sdks, sql-reference, turso-cloud | Use when working with Turso Database, Turso Cloud, libSQL, AgentFS, Turso CLI, Turso Platform API, SDKs, SQL reference, sync, migrations, auth tokens, groups, organizations, vector search, embedded re |

## Related skills

Skills that share covered entities:

- **ableton-live** ↔ davinci-resolve (1), discord-developers (1), pymeasure (1), pyvisa (1), tauri-develop (1)
- **chrome-extensions** ↔ github (3), skill-drafting (2), cli-design (1), discord-developers (1), gimp (1), lumberyard (1)
- **cli-design** ↔ skill-drafting (4), git (2), chrome-extensions (1), gimp (1), github (1)
- **davinci-resolve** ↔ discord-developers (2), ableton-live (1), pymeasure (1), pyvisa (1), tauri-develop (1)
- **discord-developers** ↔ davinci-resolve (2), github (2), ableton-live (1), chrome-extensions (1), pymeasure (1), pyvisa (1)
- **gimp** ↔ chrome-extensions (1), cli-design (1), git (1), pymeasure (1), skill-drafting (1)
- **git** ↔ cli-design (2), gimp (1), github (1), skill-drafting (1)
- **github** ↔ chrome-extensions (3), discord-developers (2), cli-design (1), git (1), skill-drafting (1)
- **lumberyard** ↔ chrome-extensions (1)
- **pymeasure** ↔ pyvisa (2), ableton-live (1), chrome-extensions (1), davinci-resolve (1), discord-developers (1), gimp (1)
- **pyvisa** ↔ pymeasure (2), ableton-live (1), davinci-resolve (1), discord-developers (1), tauri-develop (1)
- **skill-drafting** ↔ cli-design (4), chrome-extensions (2), discord-developers (1), gimp (1), git (1), github (1)
- **tauri-develop** ↔ ableton-live (1), davinci-resolve (1), discord-developers (1), pymeasure (1), pyvisa (1)

## Entity → skill map (shared)

Entities documented by more than one skill (where to look, and overlaps):

| Entity | Skills |
| --- | --- |
| audio | chrome-extensions, lumberyard |
| batch | gimp, skill-drafting |
| channels | gimp, pymeasure |
| clone | git, github |
| config | cli-design, git |
| events | chrome-extensions, github |
| filter | gimp, git |
| frontmatter | cli-design, skill-drafting |
| getting-started | davinci-resolve, discord-developers |
| glossary | git, skill-drafting |
| i18n | chrome-extensions, cli-design |
| interactive | cli-design, git |
| latest | pymeasure, pyvisa |
| naming | cli-design, gimp |
| permissions | chrome-extensions, discord-developers, github, skill-drafting |
| query | chrome-extensions, github |
| reference | ableton-live, davinci-resolve, discord-developers, pymeasure, pyvisa, tauri-develop |
| review | cli-design, github |
| scripting | chrome-extensions, gimp |
| skill | cli-design, skill-drafting |
| skill.md | cli-design, skill-drafting |
| teams | discord-developers, github |
| usage | cli-design, skill-drafting |
| windows | chrome-extensions, pymeasure, skill-drafting |
