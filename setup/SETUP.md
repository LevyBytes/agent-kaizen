# SETUP.md - Agent Operating Manual For Agent Kaizen

This is the agent-facing operating manual for this repo - the agent counterpart to the human-facing [`README.md`](../README.md). It lives in `setup/`, alongside the bootstrap and helper scripts. The public system concept lives in [`Kaizen_System.md`](../Kaizen_System.md). `AGENTS.md` and `CLAUDE.md` (repo root) are the compact host instructions that point here.

## 1. What This Repo Is

Agent Kaizen is a local implementation of the Kaizen System for improving AI coding-agent work in VS Code projects.

The system loop is:

```text
SAVMI = Scope -> Adapt -> Verify -> Manage -> Improve
```

The local harness is:

- `kaizen.py`;
- the local DB under `AI/db/`;
- project and skill `evals/` surfaces;
- structured reports, proof records, hashes, source locks, packets, and policy context.

## 2. First-Time Setup

The one-file installers and their helper scripts all live in this `setup/` folder:

- Windows: `setup/Install-Agent-Kaizen.cmd` - a thin launcher that self-elevates via UAC and relaunches the installer in an **elevated PowerShell window** (automation flags like `-SelfTest` / `-PlanOnly` / `-ListSteps` / `-NoPrompt` run inline instead). That PowerShell window chooses `DEVROOT`, installs git and Python into `DEVROOT\Python\Python312`, offers the developer-tool menu below, clones the repo into `DEVROOT\agent-kaizen`, builds the shared venv, installs dependencies, generates the VS Code workspace and launcher, scaffolds an empty sibling `SKILLS` store, and initializes the DB.
- Windows package bootstrap: git uses winget when available and otherwise downloads from git-scm.com. The winget/appx bootstrap is registration-first; see the Windows Sandbox notes below for the exact fallback sequence and sandbox exception.
- Developer tools + Turso: `pyturso` (the DB binding) has no prebuilt Windows wheel, so pip compiles it from source, which needs **Rust + Visual Studio Build Tools**. The installer's tool menu offers Rust + Build Tools up front (default yes) and .NET SDKs / CMake / Node.js / VS Code as optionals installed at the end. Flags: `-WithRust -WithBuildTools -WithDotNet -WithCMake -WithNode -WithVSCode -NoDevTools`. To skip the multi-GB toolchain, provide a prebuilt wheel: drop `pyturso-*.whl` in `DEVROOT\wheels` (or the repo's `wheels\`), or pass `-PyTursoWheelUrl <url>`; the installer uses it (wheel-first) and skips compiling. A successful source build is cached back to `DEVROOT\wheels`. When Build Tools are installed, an `open-agent-kaizen-devshell.cmd` launcher (VS dev env + venv) is written to the repo.
- Linux/macOS: `setup/install-agent-kaizen.sh` - installs git + Python via the system package manager, clones the repo, then runs `setup/setup.sh` (which also runs standalone in an already-cloned repo).
- Claude subscription runtime: explicitly select `-WithClaudeRuntime` on Windows or `--with-claude-runtime` on Linux/macOS; automation may instead set `AK_WITH_CLAUDE_RUNTIME`. The CLI selection overrides the environment, the default is off, and accepted environment values are `1` / `true` / `enabled` and `0` / `false` / `disabled`. This provider selection is independent of `-NoDevTools` / `--no-dev-tools`, requires the exact managed Node/npm pair under `DEVROOT`, and performs no login or credential setup.
- Skills are optional and ship empty: `setup/link-skills.ps1` / `setup/link-skills.sh` clone a skills store you choose and link it into `.agents/skills` + `.claude/skills`.
- `setup/SetDevRoot.cmd` sets only the `DEVROOT` user environment variable.

Installer UX contract:

- Every step must pre-flight validate (the Verify pillar applied to setup): detect an already-present, valid result and skip its download/install work, so a warm re-run is cheap and side-effect-free. Downloads reuse an already-downloaded valid file; installs check "already installed" before running; generated files are written only when their content changes; env writes only when the value differs. Do work only when validation fails, and re-validate after doing it.
- Choose or validate `DEVROOT` before tool bootstrap. Do not silently fall back to a system-drive dev root in automation; pass the intended root explicitly.
- Logs and setup state live under `DEVROOT/agent-kaizen-setup/` (`DEVROOT\agent-kaizen-setup\` on Windows). Native command logs are under `logs/`.
- Long-running native commands must run through the installer wrappers so users see step `N/M`, overall percent ranges, elapsed time, recent output, and command log paths.
- Download helpers show bytes downloaded, total bytes and ETA when `Content-Length` exists, and an unknown-total progress line otherwise.
- A user-skipped step (e.g. a deselected optional tool) must report distinctly as skipped, never as `OK`, so the transcript makes clear what was and was not installed.
- Any pinned progress banner must be self-cleaning (only the current activity) and non-flashing: reserve a fixed block, redraw it in one pass with full-width line fills (no partial clears), throttle live updates, and degrade gracefully to plain scrolling output on a host without cursor support or under `-NoProgressHeader`.
- Selectable toolchain installs persist their user `PATH` (and the matching root env var) on both the fresh-install and already-present paths, so an interrupted prior run is repaired on re-run; Node additionally sets an npm global prefix under `DEVROOT` and puts it on `PATH`.
- Non-live safety modes must not run downloads, package managers, external tools, GUI launches, or user environment writes.

Windows flags:

```powershell
setup\Install-Agent-Kaizen.cmd X:\dev -ListSteps -NoPause
setup\Install-Agent-Kaizen.cmd X:\dev -PlanOnly -NoNetwork -NoExternalActions -NoUserEnvWrites -EmitPlanJson X:\dev\agent-kaizen\AI\work\installer-plan.json -NoPause
setup\Install-Agent-Kaizen.cmd X:\dev -SelfTest -NoNetwork -NoExternalActions -NoUserEnvWrites -NoPause
setup\Install-Agent-Kaizen.cmd X:\dev -WithClaudeRuntime
```

Linux/macOS flags:

```sh
bash setup/install-agent-kaizen.sh "$HOME/dev" --list-steps
bash setup/install-agent-kaizen.sh "$HOME/dev" --plan-only --no-network --no-external-actions --no-user-env-writes --emit-plan-json "$HOME/dev/agent-kaizen/AI/work/installer-plan.json"
bash setup/install-agent-kaizen.sh "$HOME/dev" --self-test --no-network --no-external-actions --no-user-env-writes --no-input
bash setup/install-agent-kaizen.sh "$HOME/dev" --with-claude-runtime
```

Optional installers (`install-pytorch`, `install-ollama`, `install-comfyui`, and `link-skills`) follow the same plan/list/self-test/safety flag pattern. Python-based optional installers should prefer the shared venv at `DEVROOT/Python/venvs/kaizen` unless the caller passes an explicit Python override.

Windows Sandbox notes:

- Windows Sandbox is an edge-case test harness. A ready-made, generic template lives at [`tests/windows-sandbox-template.wsb`](../tests/windows-sandbox-template.wsb) - launch it, or adapt its commented `MappedFolder` to test local installer/repo changes.
- **Disabling Smart App Control in the `.wsb` `LogonCommand` is the key** that lets the installer succeed: recent Windows 11 base images ship SAC in Enforce mode, which silently blocks the per-user Python MSI child and unsigned native modules. The template sets `HKLM\SYSTEM\CurrentControlSet\Control\CI\Policy\VerifiedAndReputablePolicyState=0` + `CiTool.exe -r` (and disables Defender realtime) at logon. If SAC still shows enabled, close and relaunch the `.wsb` once.
- Keep sandbox mappings **minimal** — map only a small staging folder (never a whole git repo; junctions and size destabilize the sandbox). The installer clones the repo fresh, so it does not need the repo mapped.
- Use an explicit writable `DEVROOT`.
- It is fine for the installer source or repo source to be a read-only mapped folder; writes should go under `DEVROOT`.
- The winget bootstrap runs in the elevated PowerShell payload session, registration-first: it runs `Add-AppxPackage -RegisterByFamilyName -MainPackage Microsoft.DesktopAppInstaller_8wekyb3d8bbwe` before any download, which enables winget when the App Installer's framework dependencies are already staged (a warm sandbox). In a truly fresh sandbox that registration fails (`0x80073CF9`) because the bundle's frameworks (VCLibs `14.0.33519.0`, UI.Xaml, WindowsAppRuntime 1.8) are not staged; the fallback then installs the Windows App Runtime 1.8 redist (`windowsappruntimeinstall`), retries the same registration, and finally downloads Desktop VCLibs + UI.Xaml + the App Installer bundle (`Repair-WinGetPackageManager` is skipped in Sandbox where it fails `0x80070005`). winget is optional: Git and Python have direct-download paths.
- Python is installed from the python.org Windows installer into `DEVROOT\Python\Python312` with `/quiet InstallAllUsers=0 TargetDir=... PrependPath=0`. Its WiX-burn bootstrapper is run through the job-based runner (so it returns a real exit code, not a 1-second no-op) with `/log`; the diagnostic (`python-install-*.log`) is written under the setup `logs/` folder and mirrored by `AK_LOG_MIRROR`. The shared venv is built from it at `DEVROOT\Python\venvs\kaizen`.
- `pyturso` has no prebuilt Windows wheel, so pip compiles it from source (needs Rust + Build Tools; see the developer-tool note above). Smart App Control Enforce may block unsigned binaries; the installer detects and warns about SAC in preflight but does not halt.
- Set `AK_LOG_MIRROR` (or map a writable `C:\ak-logs` in the `.wsb`) to copy the setup logs out of the ephemeral sandbox before it closes.

Human-facing install steps are in the repo [`README.md`](../README.md).

## 3. Session Start

For non-trivial work, run or request the current private policy context first - the same command for Claude Code and Codex:

```powershell
python kaizen.py X5 --json
```

The policy DB ships empty; it returns only the rules you have added with `X1`. Reload this context after conversation compaction. Before every major task, remind the user to compact or start a continuation when the context window is getting heavy.

Then check the DB and load the session digest:

```powershell
python kaizen.py K1 --json
python kaizen.py R0 --json
```

`R0` is the read-back half of Manage: one small JSON payload with active policy, open GOTCHAs, blocking verification conclusions (`VERIFICATION_FAILED`, `NEEDS_HUMAN_DECISION`), recent LEARNED lessons, and active tasks — so a session starts from records instead of chat memory. Reload `X5` and `R0` after compaction.

## 4. Core Rule

```text
Scripts control structure and repetitive work.
Agents control meaning, synthesis, and adaptation.
Verification controls acceptance.
Management controls durable memory and policy.
The user controls priorities and approvals.
```

Move deterministic mechanics into scripts. Keep agent context for judgment, tradeoffs, and synthesis.

## 5. Kaizen CLI

Use:

```powershell
python kaizen.py <operation> --json
```

Use the shared venv at `$DEVROOT/Python/venvs/kaizen` (created by setup). A repo-local `.venv` is only a rarely present fallback. Dependency pins are in `requirements-kaizen.txt`.

Command families:

- `K*` DB/core
- `W*` work/tasks/plans/packets
- `G*` GOTCHA
- `L*` LEARNING/LEARNED
- `Q*` quality/evals/verification/proof (incl. `Q8` output-validate against record schemas)
- `M*` migration
- `R*` reports
- `S*` sources
- `I*` IRL Review
- `A*` artifacts
- `X*` private policy/session context
- `T*` traces/scores
- `E*` evidence ingestion
- `O*` improvement lab
- `Y*` generative runs (ComfyUI)
- `B*` model/embedding backends (Ollama)
- `SK*` skill inventory, validation, reconciliation, policy, and project context

Use `--help` for the approved operation list. Short codes and named aliases are equivalent. Do not invent operation codes or flags during task work.

JSON-valued args and shell quoting differ per shell. The file fallback always works; inline forms vary:

| Shell                  | Inline JSON that survives argv                          | Safest form   |
| ---------------------- | ------------------------------------------------------- | ------------- |
| Windows PowerShell 5.1 | `--payload-json "{\"task\":\"summarize\"}"` (escaped)   | `*-file` flag |
| PowerShell 7+          | `--payload-json '{"task":"summarize"}'` (single-quoted) | either        |
| bash / zsh             | `--payload-json '{"task":"summarize"}'` (single-quoted) | either        |
| cmd.exe                | `--payload-json "{\"task\":\"summarize\"}"` (escaped)   | `*-file` flag |

JSON flags provide file-based forms for quoting-sensitive input; examples include `--payload-json-file`, `--summary-file`, `--body-file`, `--evidence-file`, `--findings-file`, `--remedies-file`, `--artifact-ids-file`, and `--expected-json-file`. Use `--help` for the current operation-specific set.

### Supervisor conversations and controller UI

The workspace supervisor owns driven/observed conversation state and exposes it only through the authenticated local control transport. Start it with the shared venv in a dedicated visible terminal (or the extension's `Kaizen: Start Daemon` action, which opens exactly this terminal for you — by design there is no machine-level autostart: no scheduled task, no service):

```powershell
python kaizen.py daemon run
```

Inspect the installed engine boundary before attempting a session:

```powershell
python kaizen.py daemon session capabilities --json
python kaizen.py daemon session list --json
```

Only `drivable: true` engines may start. A visible-but-disabled vendor lane is a safety result, not a setup error: the installed version failed or has not yet passed its sandbox, approval, hook, credential, and profile probes. Never bypass the gate or substitute another permission mode silently.

Claude subscription support uses a pinned official SDK runtime outside the VSIX. See the selection and precedence contract in section 2. Daemon start and extension activation never install or update the runtime. The direct helper remains available for diagnosis or deliberate repair:

```powershell
python setup/claude_runtime_setup.py check
python setup/claude_runtime_setup.py install
```

`check` is offline and returns a sanitized, path-free readiness result. A selected warm runtime therefore succeeds even with `-NoNetwork` / `--no-network`. A selected cold setup under either no-network flag fails before npm. `install` is enabled only when the repository contains an exact audited lock and requires the setup-managed Node/npm under `DEVROOT`; arbitrary `PATH` or system Node/npm does not satisfy it. The installer performs an exact local install with lifecycle scripts disabled, verifies the native dependency and installed tree, and skips package-manager work on a valid warm rerun. It never uses a global package, global npm prefix, `PATH` fallback, login flow, credential prompt, or credential-file access. Missing lock, managed Node/npm, native dependency, or integrity evidence fails closed. There is no Claude API-key fallback.

The VSIX intentionally excludes the SDK, native runtime, worker source, `node_modules`, caches, and runtime pointers. Kaizen mediates model-visible tools and owns launched process lifecycles at the application layer; this is not OS, filesystem, network, kernel, or hostile-native-code containment.

Existing-target proposal modify, delete, and rename operations use the verified bounded crash-recovery path on Windows. A platform without equivalent proven primitives denies those operations before mutation; retained recovery artifacts support exact restart reconciliation, not filesystem transactionality, rollback, or broad OS containment.

One driven conversation keeps one C1/T5 open across multiple turns. `session/turn` is idle-only, `session/close` is idle-only and writes the sole successful T8, and `kill` remains a separate explicit non-success path. The event pump remains attached while idle; `session/events --since 0` is the replay source after a UI reload.

The VS Code extension under `extension/` is an in-tree development foundation, not a release-ready public surface; its Node/VSIX lane is intentionally absent from public GitHub Actions. It provides one isolated `ConversationController` per editor chat tab and one shared session-list poll (owner design 2026-07-10: the sidebar keeps its three sections — Approvals, Sessions & Timeline, Fleet & Engines — and chat opens in the center like a document). Each controller persists only its active identifiers/profile hash. Webviews never own transcript or policy state, and disposing a renderer never closes the run. Developers can build and test it locally with:

```powershell
cd extension
npm test
npm run compile
python build_vsix.py
```

`Kaizen: Open Test Extension` (`kaizen.testExtension.open`) opens the approved Test Extension editor tab. Its Start action opens a visible terminal runner that owns a fresh isolated daemon and a visible Extension Development Host; nothing autostarts or runs as a hidden service. The real authenticated provider leg is initiated by the user outside agent sandboxes, uses a bounded provider-call ceiling, and records sanitized evidence only. Ollama is a separate baseline and cannot substitute for Claude.

Claude observed capture is post-install only; it never imports transcript JSONL. The daemon's `hooks install` verb is the sole sanctioned writer of the workspace-local `.claude/settings.local.json` hook entries and must preserve user-authored entries on the same events. Existing installations must reinstall their marker-owned hooks with the same mode they already use; mode switching is outside this relocation:

If the existing mode is strict, substitute `hooked-strict` for `hooked-observe` in the install command.

```powershell
python kaizen.py daemon hooks install --mode hooked-observe --json
python kaizen.py daemon hooks verify --json
```

Observed sessions are audit-only. Their linked runs may be listed/replayed, but turn, close, steer, interrupt, kill, and approval controls must deny as read-only. Hook recording failures must never change strict/observe policy decisions.

## 6. Work Locations

- Durable human-facing deliverables: `user/`.
- Generated content: `AI/generation/`.
- Task scratch: `AI/work/<task-slug>/`.
- Kaizen DB and exports: `AI/db/`.
- Setup/bootstrap scripts: `setup/`.
- Test, benchmark, verification, and acceptance sources: `tests/`.
- Auxiliary-only helper scripts: `support_scripts/`.
- Helper scripts scratch: `AI/support_scripts_work/`.
- Root eval fixtures and learning command stubs: `evals/`.

For public repositories, keep `AI/db/` contents private/local unless explicitly sanitized. Private repositories may choose to track DB data deliberately.

`AI/work/build-ledger.md` remains a local continuity export during the transition. Update it after major milestones until DB-backed ledger reporting fully replaces it.

## 7. SAVMI Workflow

For substantial work use [`Kaizen_System.md`](../Kaizen_System.md):

1. **Scope**: research the system, interview the user when needed, define assumptions, scope, and acceptance criteria.
2. **Adapt**: create the execution contract, use allowed capabilities, and make bounded changes.
3. **Verify**: run deterministic checks first; use structured synthesis only where judgment is needed.
4. **Manage**: record tasks, plans, proof, artifacts, source locks, learning records, ingested evidence, activity traces, eval scores, and reports.
5. **Improve**: review managed evidence and feed better priorities into the next Scope cycle.

Use `W1`/`W3` for tasks and plans when work is substantial. Use `Q*` and `A*` for proof, verifier findings, eval runs, anti-patterns, and artifacts. Use `G*` and `L*` for the GOTCHA -> LEARNING -> LEARNED lifecycle. Use `E*` to ingest external evidence, `T*` to record traces and eval scores, and `O*` for the improvement lab.

Read records back, not just in: `R0` (session digest), `L10` (LEARNED lessons with their GOTCHA -> LEARNING chain), `Q9` (verification conclusions by task/conclusion/severity), and `T4` (eval scores with aggregates) pull past work into the current session cheaply.

### Durability triage

Not every observation deserves a durable record. Default rubric:

| Situation                                                  | Action                    |
| ---------------------------------------------------------- | ------------------------- |
| Changes scope, risk, acceptance criteria, or a decision    | `W1`/`W2` (task + ledger) |
| A failure, pitfall, or suspected issue with evidence       | `G1`                      |
| A validated root cause + correction for an existing GOTCHA | `L2` (promote)            |
| Proof that work passed or failed a check                   | `Q1`/`Q2`                 |
| Minor mechanics, typos, or chat-level back-and-forth       | chat only — no record     |

### Evidence Ingestion Walkthrough

Managed ingestion instead of pasting raw text into context:

```powershell
python kaizen.py E1 --path docs/spec.md --summary "Ingest the widget spec." --json
python kaizen.py E3 --id DOC_ID_FROM_E1 --json
python kaizen.py E4 --query "retry budget" --json
python kaizen.py E5 --id DOC_ID_FROM_E1 --json
```

`E3` chunks deterministically (add `--chunker semantic` with an embedding backend); `E4` is lexical by default and vector-ranked with `--semantic` once chunks are embedded (`B3` backfills). Lock external sources with `S1` first when provenance matters.

### Private Policy Walkthrough

The policy DB ships empty by design. A realistic first rule and its round-trip:

```powershell
python kaizen.py X1 --title "User-owned commits" --trigger session-start --priority high --summary "Agents prepare changes; the user commits." --body "Do not commit, push, or rewrite history unless explicitly authorized for that exact action." --json
python kaizen.py X5 --json
```

`X5` (and `R0`) return active rules sorted by priority; `--trigger` labels when a rule applies (`session-start` rules always load).

## 8. Skills

Skills live in the external skills store. `.agents/skills` and `.claude/skills` remain optional host-native surfaces, but project routing no longer depends on a hard-coded list in `AGENTS.md` or `CLAUDE.md`. Edit the canonical skill store, not a copied mirror.

Load relevant context at session start and whenever task intent materially changes:

```powershell
python kaizen.py SK7 --query "<current task intent>" --host "<codex|claude>" --json
```

`SK7` requires one explicit `codex` or `claude` host, searches the latest validated project snapshot, rechecks the live `SKILL.md` and whole-package hashes, and returns full instructions without writing telemetry only when the selected-host surface is correct and its policy is `on`. Claude project policy may also be `name-only`, `user-invocable-only`, or `off`, which SK7 excludes with a portable reason; Codex policy is currently audit-only/default-on because no supported project-local writer exists. Missing and wrong surfaces are also excluded with a portable reason. Before bootstrap SK7 returns an explicit unavailable result rather than forcing a write. For a fresh database, run `K1`; when upgrading an existing schema-v1 database, run `K1`, inspect `K2`, and use the owner-approved `K1 --restamp-manifest` only when `schema_ok` is true and `manifest_match` is false from this known additive update. Then run `SK6 --action plan` and apply only after reviewing that plan. Missing, invalid, stale, or integrity-failed packages remain unavailable. `SK8` uses `current` for snapshot, inventory, surface, and policy freshness and reports package validity, publication, and policy health separately. `SK6 --action apply` writes Turso only after the recomputed plan matches `--confirm-plan`.

Publication, host policy, and surface validation are independent. A package is `published` only when a configured Git remote validates as GitHub and is otherwise `staged`; this local classification neither publishes nor fetches the repository. Claude project policy supports `on`, `name-only`, `user-invocable-only`, and `off`; Codex policy remains audit-only/default-on. Surface validation independently checks the selected host link. A staged package is not automatically disabled, and no management operation automatically publishes, links, or enables it.

The management surface is explicit:

- `SK1` inventories packages, local GitHub publication classification, and host surfaces without writing.
- `SK2` validates package, link, index, and freshness state without writing.
- `SK3` inspects, plans, or applies project link reconciliation.
- `SK4` inspects, plans, or applies the surfaced-skill index.
- `SK5` inspects Claude or Codex policy and plans, applies, or restores Claude project policy; Codex is audit-only.
- `SK6` plans or applies the Turso skill-context snapshot.
- `SK7` queries task-relevant context for one explicit host.
- `SK8` reports DB-versus-live freshness separately from package and policy health.

Status and plan actions emit data only. Filesystem, settings, and database mutations require an explicit `apply` or `restore` action, owner approval, and the matching `plan_sha256`. Turso stores routing metadata and validated observations; Python tooling remains authoritative for packages, Git-remote classification, links, indexes, and host settings.

Skills and scripts can support all SAVMI layers:

- Scope research and routing;
- Adapt execution and deterministic mechanics;
- Verify proof and review;
- Manage records and generated views;
- Improve evals, anti-patterns, and correction patterns.

Every serious skill should have:

- `SKILL.md`;
- `evals/GOTCHA.md`;
- `evals/LEARNING.md`;
- `evals/LEARNED.md`;
- `evals/` fixtures for executable or behavioral evals when needed.

The Markdown learning files are stubs or generated views. Durable learning records live in the DB.

## 9. Verification

Before reporting success:

- re-read the user request and the active Scope;
- check each acceptance criterion;
- run relevant tests, formatters, linters, validators, or smoke commands;
- record proof artifacts and hashes where practical;
- say what passed, failed, and was not run.

Known caveat: running a repo-wide formatter can hit unrelated local scratch. Prefer targeted checks on the Markdown you changed.

## 10. Public Surfaces

Tracked public docs should explain the portable system and local harness, not private machine policy. Private policy context belongs in the local data plane and is loaded through `X5`.

Before preparing public output, inspect:

- tracked files;
- ignored/generated files intended for export;
- local DB exports and reports;
- personal paths;
- secret-like strings.
