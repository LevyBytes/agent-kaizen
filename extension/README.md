# Kaizen Controller (VS Code extension)

> Development status: the controller foundation remains in the repository for active development, but it is not release-ready or supported for public use. Its Node and VSIX tests are intentionally excluded from the public GitHub Actions gate until the owner promotes it.

The controlling-harness UI for the Kaizen supervisor daemon: isolated per-tab conversations rendered as editor chat panels (opened from the sidebar, the status bar, or `Kaizen: Open Chat`), plus the sidebar's approvals, session timeline, fleet, attach, and orchestration views. The extension holds no authoritative transcript or policy state: control goes through the daemon's owner-only loopback channel, while durable events replay from the daemon after a renderer or extension-host reload.

## Developer install

For local extension development only, install a package built from the current working tree.

```text
cd extension
npm install
npm run package
code --install-extension kaizen-controller-0.1.0.vsix
```

Then open the agent-kaizen workspace and start the daemon (`python kaizen.py daemon run`); the Kaizen icon appears in the activity bar. Upgrading later = re-run `npm run package` and `code --install-extension` with the new file; uninstall from the Extensions panel like any other extension.

The dependency posture matches the rest of the project: the extension has zero runtime dependencies, the dev tree is exactly typescript + type stubs (all actively maintained), and the VSIX is produced by `build_vsix.py` — Python stdlib only, deterministic output — rather than a packager with its own dependency tree.

Claude is provider-disabled until its separately managed official SDK runtime passes daemon capability checks. The extension never installs that runtime, accepts credentials, reads vendor identity files, or falls back to a Claude API key. From the repository root, the user explicitly manages it with:

```text
python setup/claude_runtime_setup.py check
python setup/claude_runtime_setup.py install
```

`check` is offline and path-free. `install` is enabled only when the repository contains an exact audited lock, consumes that lock with the setup-managed Node/npm under `DEVROOT`, reuses a valid warm runtime, and performs no login or credential handling. The VSIX excludes the SDK, native runtime, worker source, `node_modules`, caches, and runtime pointers.

## Test

```text
npm test        # tsc + node --test (protocol client against a fake daemon, state rules)
npm run compile # host + webview TypeScript
```

## Test Extension

Run `Kaizen: Open Test Extension` (`kaizen.testExtension.open`) to open the approved Test Extension editor tab. Starting its suite explicitly opens a visible terminal runner, then a fresh isolated daemon and visible Extension Development Host. The user selects the discovered provider model/effort, scenario set, maximum turns, and suite-wide provider-call ceiling before starting. Stop terminates the complete test plane; uncertain cleanup fails the run and preserves its sanitized evidence.

The real authenticated Claude leg is always user-launched and consumes subscription capacity. Ollama is a separately reported baseline, never a fallback or substitute for Claude. The surface tests the real editor-tab/controller/protocol path; it is not part of ordinary chat and does not provide OS containment.

## How it maps to the daemon

| UI | Wire |
| --- | --- |
| Chat editor panels | one isolated `ConversationController` per tab plus one shared session-list poll; `session/capabilities`, `session/start`, `session/turn`, `session/events`, `session/close`, `session/steer`, `session/interrupt`, and `session/kill` |
| Engine/model/effort/permission/auth controls | capability-advertised values; controls lock after start; Full requires a fresh host confirmation; the UI never accepts an API key |
| Driven transcript | complete redacted `chat_message/point` events; the accepted user message is not rendered optimistically |
| Observed Claude transcript | `session/list {controller: observed}` plus ordered linked-run replay; all controls and approval records are display-only |
| Chat approvals | loopback `approve {correlation_id, session_id, decision}`; cards remain until the daemon emits resolved/declined |
| Approvals queue (approve/deny) | loopback `approve {approval_id, decision}`; open items come from bound sessions' `C5` timelines |
| Sessions & Timeline panes | `C5 --session-id` reads; bindings + replay cursors persist in `workspaceState` (panes survive reload) |
| Attach Session Here | loopback `attach {session_id, expected_owning_node, expected_node_epoch}` — a concurrent take elsewhere returns `DENIED_STALE_FENCE` and the pane stays read-only |
| Steer / Set Goal | loopback `steer`; `kaizen.py C3` |
| Orchestrate | loopback `orchestrate {scope, task}` (lease-gated by the daemon) |
| Fleet & Engines | daemon `status` (engine lanes come from adapter registration — an unregistered lane renders greyed) + the `R0` fleet section |

Sessions owned by another fleet node render read-only (lock icon); write commands are hidden for them.

The conversation controller persists only `session_id`, `agent_run_id`, and `profile_hash` in `workspaceState`. Transcript text, credentials, approval secrets, and Full-mode confirmation are never persisted there. Closing either renderer only detaches that surface; it does not stop the daemon run.

## Compatibility and safety

The extension consumes canonical engine IDs (`local_llm`, `codex`, `claude`) and trusts each engine's capability response. An installed vendor build that cannot provide the selected permission boundary stays visible but disabled with a structured reason; the UI never silently weakens the requested profile. The daemon provides application-layer mediation plus each vendor's supported sandbox/approval controls. OS-level containment is outside this release.

Existing-target proposal modify, delete, and rename operations use the verified bounded crash-recovery path on Windows. A platform without equivalent proven primitives denies those operations before mutation; retained recovery artifacts support exact restart reconciliation, not filesystem transactionality, rollback, or broad OS containment.

## Settings

- `kaizen.pythonPath` — interpreter for `kaizen.py` reads (default: the `$DEVROOT` kaizen venv, then `python`).
- `kaizen.pollSeconds` — refresh interval (0 disables polling; there is no push channel — the loopback is request/response by design).
- `kaizen.chat.enterBehavior` — choose whether Enter sends or inserts a newline.
- `kaizen.chat.followUpQueueMode` — queue follow-ups during a running turn or disable follow-ups.
- `kaizen.chat.autosaveBeforeTurn` — save dirty files before a governed turn when a context feature requests it.
- `kaizen.chat.renderMarkdown` — render the safe assistant/system Markdown subset.
