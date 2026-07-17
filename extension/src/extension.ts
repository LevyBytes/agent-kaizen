/**
 * Activation + command wiring (v8 M8, plan §D). The extension is a thin controller UI over the
 * supervisor daemon: CONTROL goes through the owner-only loopback ({op,args,token} JSON-lines —
 * approve/steer/attach/orchestrate/status), READS go through kaizen.py --json (R0/C5), and every
 * pane↔session binding + replay cursor persists in workspaceState so panes survive a reload.
 */

import * as fs from "fs";
import * as vscode from "vscode";
import { ChatPanelManager } from "./chatPanel";
import type { ControllerPrompts } from "./conversationController";
import * as cli from "./kaizenCli";
import * as path from "path";
import * as protocol from "./protocol";
import { SessionClient } from "./sessionClient";
import { SessionIndex } from "./sessionIndex";
import { startDaemonTerminal } from "./startDaemon";
import { advanceCursor, Bindings, bindSession, EMPTY_BINDINGS, unbindSession } from "./state";
import { TestExtensionPanel } from "./testExtension";
import { ApprovalsProvider, FleetProvider, SessionsProvider, Store } from "./views";

const BINDINGS_KEY = "kaizen.bindings";

export function activate(context: vscode.ExtensionContext): void {
  // Multi-root workspaces (e.g. a *.code-workspace bundling several folders): the Kaizen repo is the
  // folder that contains kaizen.py, not necessarily folders[0]. Fallback keeps single-folder behavior.
  const folders = vscode.workspace.workspaceFolders ?? [];
  const repoRoot =
    folders.find((f) => fs.existsSync(path.join(f.uri.fsPath, "kaizen.py")))?.uri.fsPath ??
    folders[0]?.uri.fsPath;
  if (!repoRoot) return;
  const root = repoRoot;

  const store: Store = {
    status: null,
    digest: null,
    timelines: new Map(),
    bindings: context.workspaceState.get<Bindings>(BINDINGS_KEY, EMPTY_BINDINGS),
    newEvents: new Map(),
  };
  const prompts: ControllerPrompts = {
    confirmFull: async () =>
      (await vscode.window.showWarningMessage(
        "Full mode relaxes vendor sandboxes within Kaizen's invariant floor for this conversation only.",
        { modal: true },
        "Start Full conversation",
      )) === "Start Full conversation",
    confirmNewConversation: async () =>
      (await vscode.window.showWarningMessage(
        "Close the idle conversation and start a new one?",
        { modal: true },
        "Close and start new",
      )) === "Close and start new",
    confirmEphemeralLoss: async ({ hasDraft, queuedCount, contextCount, imageCount }) => {
      const parts = [
        ...(hasDraft ? ["the unsent draft"] : []),
        ...(queuedCount ? [`${queuedCount} queued follow-up${queuedCount === 1 ? "" : "s"}`] : []),
        ...(contextCount ? [`${contextCount} context chip${contextCount === 1 ? "" : "s"}`] : []),
        ...(imageCount ? [`${imageCount} image${imageCount === 1 ? "" : "s"}`] : []),
      ];
      if (parts.length === 0) return true;
      return (await vscode.window.showWarningMessage(
        `${parts.join(" and ")} ${parts.length === 1 ? "is" : "are"} temporary and will be lost. Continue?`,
        { modal: true },
        "Discard and continue",
      )) === "Discard and continue";
    },
  };
  const sessionIndex = new SessionIndex(new SessionClient(root));
  const panels = new ChatPanelManager(context, root, sessionIndex, prompts);
  const logs = vscode.window.createOutputChannel("Kaizen");
  context.subscriptions.push(
    logs,
    sessionIndex,
    panels,
    vscode.window.registerWebviewPanelSerializer(ChatPanelManager.viewType, panels),
  );
  const providers = [new ApprovalsProvider(store), new SessionsProvider(store), new FleetProvider(store)];
  const [approvals, sessions, fleet] = providers;
  context.subscriptions.push(
    vscode.window.registerTreeDataProvider("kaizenApprovals", approvals),
    vscode.window.registerTreeDataProvider("kaizenSessions", sessions),
    vscode.window.registerTreeDataProvider("kaizenFleet", fleet),
  );

  const python = () =>
    cli.resolvePython(root, vscode.workspace.getConfiguration("kaizen").get<string>("pythonPath", ""));
  const testExtension = new TestExtensionPanel(context, root, python, panels);
  context.subscriptions.push(testExtension);

  const saveBindings = (b: Bindings) => {
    store.bindings = b;
    void context.workspaceState.update(BINDINGS_KEY, b);
  };

  /** Return null when unreachable; otherwise surface DENIED/ERROR without throwing. */
  async function control(op: string, args: Record<string, unknown>): Promise<protocol.LoopbackResponse | null> {
    try {
      const resp = await protocol.request(root, op, args);
      if (resp.status !== "OK") {
        void vscode.window.showWarningMessage(`Kaizen ${op}: ${resp.code ?? resp.status}`);
      }
      return resp;
    } catch (err) {
      void vscode.window.showWarningMessage(`Kaizen daemon unreachable: ${err instanceof Error ? err.message : err}`);
      return null;
    }
  }

  let refreshInFlight: Promise<void> | undefined;
  /** Refresh all panes, swallowing per-source failures and persisting replay cursors. */
  async function refresh(): Promise<void> {
    if (refreshInFlight) return refreshInFlight;
    refreshInFlight = (async () => {
      await panels.refreshSessions();
      try {
        store.status = await protocol.request(root, "status", {});
      } catch {
        store.status = null;
      }
      try {
        store.digest = await cli.kaizenRead(root, python(), ["R0"]);
      } catch {
        store.digest = null;
      }
      for (const sessionId of store.bindings.sessions) {
        try {
          const timeline = await cli.kaizenRead(root, python(), ["C5", "--session-id", sessionId]);
          store.timelines.set(sessionId, timeline);
          const length =
            ((timeline.instructions as unknown[])?.length ?? 0) +
            ((timeline.goals as unknown[])?.length ?? 0) +
            ((timeline.approvals as unknown[])?.length ?? 0);
          const { bindings, newEvents } = advanceCursor(store.bindings, sessionId, length);
          saveBindings(bindings);
          store.newEvents.set(sessionId, newEvents);
        } catch {
          store.timelines.delete(sessionId); // session unreadable -> pane shows "not loaded", stays bound
          store.newEvents.delete(sessionId);
        }
      }
      providers.forEach((p) => p.refresh());
    })();
    try {
      await refreshInFlight;
    } finally {
      refreshInFlight = undefined;
    }
  }

  const sessionIdOf = (target: unknown): string | undefined =>
    (target as { sessionId?: string } | undefined)?.sessionId;

  const pollSeconds = vscode.workspace.getConfiguration("kaizen").get<number>("pollSeconds", 5);
  const refreshTimers = new Set<ReturnType<typeof setTimeout>>();
  const scheduleRefresh = (delayMs: number, retries = 0): void => {
    const timer = setTimeout(() => {
      refreshTimers.delete(timer);
      void refresh().finally(() => {
        if (retries > 0) scheduleRefresh(delayMs, retries - 1);
      });
    }, delayMs);
    refreshTimers.add(timer);
  };
  context.subscriptions.push({
    dispose: () => {
      for (const timer of refreshTimers) clearTimeout(timer);
      refreshTimers.clear();
    },
  });

  context.subscriptions.push(
    vscode.commands.registerCommand("kaizen.refresh", refresh),

    // Owner policy 2026-07-10: no machine-level persistence, no hidden processes. The button starts
    // the daemon in a VISIBLE terminal the user owns (the terminal IS the python process); a second
    // click reveals the live terminal instead of spawning again.
    vscode.commands.registerCommand("kaizen.startDaemon", () => {
      startDaemonTerminal(vscode.window, python(), root, path.join(root, "kaizen.py"));
      scheduleRefresh(2000, pollSeconds <= 0 ? 1 : 0); // retry once when periodic polling is disabled
    }),

    // Owner ask 2026-07-10: the user can stop the daemon AT ANY TIME. Graceful loopback `shutdown`
    // (the daemon reaps children, finalizes, releases the pidfile) — works no matter which terminal
    // started it. Confirmed first: stopping force-finalizes any active conversation.
    vscode.commands.registerCommand("kaizen.stopDaemon", async () => {
      const choice = await vscode.window.showWarningMessage(
        "Stop the Kaizen daemon? Active conversations and runs will be finalized.",
        { modal: true },
        "Stop Daemon",
      );
      if (choice !== "Stop Daemon") return;
      await control("shutdown", {});
      scheduleRefresh(1500, pollSeconds <= 0 ? 1 : 0); // retry once when periodic polling is disabled
    }),

    vscode.commands.registerCommand("kaizen.approve", async (target: unknown) => {
      const approvalId = (target as { approvalId?: string } | undefined)?.approvalId;
      if (approvalId) {
        await control("approve", { approval_id: approvalId, decision: "approve" });
        await refresh();
      }
    }),

    vscode.commands.registerCommand("kaizen.deny", async (target: unknown) => {
      const approvalId = (target as { approvalId?: string } | undefined)?.approvalId;
      if (approvalId) {
        await control("approve", { approval_id: approvalId, decision: "deny" });
        await refresh();
      }
    }),

    vscode.commands.registerCommand("kaizen.steer", async (target: unknown) => {
      const sessionId = sessionIdOf(target) ?? (await vscode.window.showInputBox({ prompt: "Session id to steer" }));
      if (!sessionId) return;
      const instruction = await vscode.window.showInputBox({ prompt: `Steer ${sessionId}` });
      if (!instruction) return;
      await control("steer", { session_id: sessionId, instruction });
      await refresh();
    }),

    vscode.commands.registerCommand("kaizen.setGoal", async (target: unknown) => {
      const sessionId = sessionIdOf(target);
      if (!sessionId) return;
      const title = await vscode.window.showInputBox({ prompt: `Goal title for ${sessionId}` });
      if (!title) return;
      try {
        await cli.kaizenRead(root, python(), ["C3", "--session-id", sessionId, "--title", title, "--summary", title]);
      } catch (err) {
        void vscode.window.showWarningMessage(`Kaizen C3 failed: ${err instanceof Error ? err.message : err}`);
      }
      await refresh();
    }),

    vscode.commands.registerCommand("kaizen.bindSession", async () => {
      const sessionId = await vscode.window.showInputBox({ prompt: "Session id to bind as a pane (from R0/C1)" });
      if (!sessionId) return;
      saveBindings(bindSession(store.bindings, sessionId));
      await refresh();
    }),

    vscode.commands.registerCommand("kaizen.unbindSession", (target: unknown) => {
      const sessionId = sessionIdOf(target);
      if (!sessionId) return;
      saveBindings(unbindSession(store.bindings, sessionId));
      store.timelines.delete(sessionId);
      store.newEvents.delete(sessionId);
      providers.forEach((p) => p.refresh());
    }),

    vscode.commands.registerCommand("kaizen.attach", async (target: unknown) => {
      const sessionId = sessionIdOf(target);
      if (!sessionId) return;
      const session = store.timelines.get(sessionId)?.session as Record<string, unknown> | undefined;
      if (!session) {
        void vscode.window.showWarningMessage("Timeline not loaded yet — refresh first.");
        return;
      }
      // §B.5/§D: attach asserts the epoch the UI SAW; a concurrent take elsewhere means the daemon
      // refuses DENIED_STALE_FENCE and the pane stays read-only after the refresh (stale-epoch reject).
      const resp = await control("attach", {
        session_id: sessionId,
        expected_owning_node: session.owning_node,
        expected_node_epoch: session.node_epoch,
      });
      if (resp?.status === "OK") void vscode.window.showInformationMessage(`Attached ${sessionId} to this node.`);
      await refresh();
    }),

    vscode.commands.registerCommand("kaizen.orchestrate", async () => {
      const engines = Array.isArray(store.status?.engines)
        ? store.status.engines.filter((engine): engine is string => typeof engine === "string")
        : [];
      if (engines.length) {
        // The dynamic lane selector: PRESENT lanes only (absent reserved lanes stay greyed in the
        // Fleet view); informational here — the dispatch executor picks its configured runner.
        await vscode.window.showQuickPick(engines, { title: "Registered engine lanes (dispatch runner is daemon-configured)" });
      }
      const scope = await vscode.window.showInputBox({ prompt: "Lease scope key (must be granted to this node)" });
      if (!scope) return;
      const task = await vscode.window.showInputBox({ prompt: "Task" });
      if (!task) return;
      await control("orchestrate", { scope, task });
      await refresh();
    }),

    // Owner design decision 2026-07-10 (post-H2 visual review): the chat control surface is the
    // EDITOR panel (center, like a document), not a sidebar view — the sidebar keeps its three
    // sections (Approvals, Sessions & Timeline, Fleet & Engines). Reveal-if-open (singleton).
    vscode.commands.registerCommand("kaizen.openChat", () => panels.openOrFocus()),
    vscode.commands.registerCommand("kaizen.newConversation", () => panels.newConversation()),
    vscode.commands.registerCommand("kaizen.focusInput", () => panels.focusInput()),
    vscode.commands.registerCommand("kaizen.reopenClosed", async () => {
      if (!(await panels.reopenClosed())) void vscode.window.showInformationMessage("No recently closed Kaizen conversation.");
    }),
    vscode.commands.registerCommand("kaizen.history", () => panels.showHistory()),
    vscode.commands.registerCommand("kaizen.addFile", () => panels.addFile()),
    vscode.commands.registerCommand("kaizen.addSelection", () => panels.addSelection()),
    vscode.commands.registerCommand("kaizen.addImage", () => panels.addImages()),
    vscode.commands.registerCommand("kaizen.acceptDiff", async () => {
      if (!(await panels.acceptDiff())) void vscode.window.showInformationMessage("No active acceptable negotiated diff revision.");
    }),
    vscode.commands.registerCommand("kaizen.rejectDiff", async () => {
      if (!(await panels.rejectDiff())) void vscode.window.showInformationMessage("No active pending negotiated diff revision.");
    }),
    vscode.commands.registerCommand("kaizen.showLogs", () => logs.show(true)),
    vscode.commands.registerCommand("kaizen.testExtension.open", () => testExtension.open()),

    // Preserve the legacy command ID while using the capability-driven editor-tab conversation path.
    vscode.commands.registerCommand("kaizen.tailSession", () => panels.newConversation()),
  );

  // Always-visible chat launcher: the view/title icon is hover-only, so the status bar carries a
  // persistent affordance (owner UX ask, 2026-07-10).
  const chatLauncher = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  chatLauncher.text = "$(comment-discussion) Kaizen";
  chatLauncher.tooltip = "Open the Kaizen chat (governed driven session)";
  chatLauncher.command = "kaizen.openChat";
  chatLauncher.show();
  context.subscriptions.push(chatLauncher);

  if (pollSeconds > 0) {
    const timer = setInterval(() => void refresh(), pollSeconds * 1000);
    context.subscriptions.push({ dispose: () => clearInterval(timer) });
  }
  void (async () => {
    await panels.initialize();
    await testExtension.autoOpenInPlane();
    await refresh();
  })();
}

export function deactivate(): void {}
