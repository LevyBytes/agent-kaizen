/**
 * The three tree views (v8 M8, plan §D): Approvals queue, Sessions & timelines, Fleet & engines.
 * Renderers only — every fact comes from the shared Store the poll loop fills (daemon status over
 * loopback + R0/C5 reads); the providers hold no authoritative state.
 */

import * as vscode from "vscode";
import { Bindings, isReadOnly, laneItems, SessionInfo } from "./state";
import { LoopbackResponse } from "./protocol";

/** Poll-owned state: status is loopback, digest is R0, timelines are C5, and providers read only. */
export interface Store {
  status: LoopbackResponse | null; // null = daemon unreachable
  digest: Record<string, any> | null; // R0
  timelines: Map<string, Record<string, any>>; // bound sessionId -> C5 result
  bindings: Bindings;
  newEvents: Map<string, number>;
}

/** Build a tree item from optional display, identity, and command properties. */
function item(
  label: string,
  opts: {
    description?: string;
    context?: string;
    icon?: string;
    collapsible?: vscode.TreeItemCollapsibleState;
    tooltip?: string;
    id?: string;
    command?: vscode.Command;
  } = {},
): vscode.TreeItem {
  const it = new vscode.TreeItem(label, opts.collapsible ?? vscode.TreeItemCollapsibleState.None);
  if (opts.description) it.description = opts.description;
  if (opts.context) it.contextValue = opts.context;
  if (opts.icon) it.iconPath = new vscode.ThemeIcon(opts.icon);
  if (opts.tooltip) it.tooltip = opts.tooltip;
  if (opts.id) it.id = opts.id;
  if (opts.command) it.command = opts.command;
  return it;
}

/** Shared read-only store and refresh emitter for pre-built tree items. */
abstract class BaseProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
  protected readonly emitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.emitter.event;
  constructor(protected store: Store) {}
  refresh(): void {
    this.emitter.fire();
  }
  getTreeItem(e: vscode.TreeItem): vscode.TreeItem {
    return e;
  }
  abstract getChildren(e?: vscode.TreeItem): vscode.TreeItem[];
}

/** Open C4 approvals from every bound session's timeline (actionable) + R0's runs-awaiting list. */
export class ApprovalsProvider extends BaseProvider {
  /** Return approval roots; approval items have no children. */
  getChildren(parent?: vscode.TreeItem): vscode.TreeItem[] {
    if (parent) return [];
    const out: vscode.TreeItem[] = [];
    for (const [sessionId, timeline] of this.store.timelines) {
      for (const a of (timeline.approvals as any[]) ?? []) {
        if (a.state !== "open") continue;
        const it = item(a.summary || a.id, {
          description: `${a.request_type ?? "approval"} · ${sessionId}`,
          context: "approval",
          icon: "question",
          id: `approval:${a.id}`,
          tooltip: `approval_id: ${a.id}`,
        });
        (it as any).approvalId = a.id;
        out.push(it);
      }
    }
    for (const r of (this.store.digest?.waiting_approvals as any[]) ?? []) {
      out.push(
        item(r.id, {
          description: `run awaiting ${r.unresolved_approvals} approval(s) — bind its session for actions`,
          icon: "watch",
        }),
      );
    }
    return out.length ? out : [item("No approvals waiting", { icon: "check-all" })];
  }
}

/** Bound session panes (owned vs read-only) with their C5 timelines, plus R0's active runs. */
export class SessionsProvider extends BaseProvider {
  /** Dispatch roots, active-run leaves, or one bound session timeline. */
  getChildren(parent?: vscode.TreeItem): vscode.TreeItem[] {
    if (!parent) {
      const roots: vscode.TreeItem[] = [];
      for (const sessionId of this.store.bindings.sessions) {
        const timeline = this.store.timelines.get(sessionId);
        const session = (timeline?.session ?? { id: sessionId }) as SessionInfo;
        const readonly = isReadOnly(session, (this.store.status?.node_id as string | null) ?? null);
        const fresh = this.store.newEvents.get(sessionId) ?? 0;
        const parts = [session.state ?? "", readonly ? "read-only" : "owned", fresh ? `+${fresh} new` : ""];
        const it = item(sessionId, {
          description: parts.filter(Boolean).join(" · "),
          context: readonly ? "session-readonly" : "session-owned",
          icon: readonly ? "lock" : "comment-discussion",
          collapsible: vscode.TreeItemCollapsibleState.Expanded,
          id: `session:${sessionId}`,
          tooltip: timeline ? `owning_node: ${session.owning_node ?? "—"} · epoch: ${session.node_epoch ?? "—"}` : "timeline not loaded yet",
        });
        (it as any).sessionId = sessionId;
        roots.push(it);
      }
      const runs = (this.store.digest?.active_agent_runs as any[]) ?? [];
      if (runs.length) {
        roots.push(item("Active runs", { icon: "pulse", collapsible: vscode.TreeItemCollapsibleState.Collapsed, id: "runs" }));
      }
      // Empty -> [] so the package.json viewsWelcome (with its Open Chat button) renders instead of a
      // dead placeholder row.
      return roots;
    }
    if (parent.id === "runs") {
      return ((this.store.digest?.active_agent_runs as any[]) ?? []).map((r) =>
        item(r.id, {
          description: `${r.agent_type} · ${r.open_children} child(ren) · ${r.unresolved_approvals} approval(s)`,
          icon: "play",
          id: `run:${r.id}`,
        }),
      );
    }
    const sessionId = (parent as any).sessionId as string | undefined;
    const timeline = sessionId ? this.store.timelines.get(sessionId) : undefined;
    if (!timeline) return [];
    const entries = [
      ...((timeline.instructions as any[]) ?? []).map((e) => ({ at: e.created_at, icon: "comment", label: e.instruction ?? e.summary, desc: "instruction" })),
      ...((timeline.goals as any[]) ?? []).map((e) => ({ at: e.created_at, icon: "milestone", label: e.title ?? e.summary, desc: `goal · ${e.state}` })),
      ...((timeline.approvals as any[]) ?? []).map((e) => ({ at: e.created_at, icon: "question", label: e.summary ?? e.id, desc: `approval · ${e.state}` })),
    ].sort((a, b) => String(a.at ?? "").localeCompare(String(b.at ?? "")));
    return entries.map((e, index) => item(String(e.label ?? "(no summary)"), {
      description: e.desc,
      icon: e.icon,
      id: `timeline:${sessionId}:${e.at ?? "undated"}:${index}`,
    }));
  }
}

/** Daemon liveness, dynamic engine lanes (absent reserved lane greyed), fleet digest + metrics. */
export class FleetProvider extends BaseProvider {
  /** Dispatch fleet roots and their current daemon, engine, or metrics leaves. */
  getChildren(parent?: vscode.TreeItem): vscode.TreeItem[] {
    if (!parent) {
      return [
        item("Daemon", { icon: "server-process", collapsible: vscode.TreeItemCollapsibleState.Expanded, id: "daemon" }),
        item("Engines", { icon: "circuit-board", collapsible: vscode.TreeItemCollapsibleState.Expanded, id: "engines" }),
        item("Fleet", { icon: "globe", collapsible: vscode.TreeItemCollapsibleState.Collapsed, id: "fleet" }),
      ];
    }
    const s = this.store.status;
    if (parent.id === "daemon") {
      if (!s)
        return [
          item("not running", {
            icon: "circle-slash",
            description: "click to start (visible terminal)",
            tooltip: "Runs `python kaizen.py daemon run` in a terminal you own — no background service.",
            command: { command: "kaizen.startDaemon", title: "Start Daemon" },
            context: "daemon-down", // surfaces the inline ▶ button (view/item/context menu)
          }),
        ];
      return [
        item("running", {
          icon: "check",
          description: `pid ${s.pid} · ${s.transport}`,
          tooltip: "Stop with the inline button (graceful shutdown: children reaped, runs finalized).",
          context: "daemon-up", // surfaces the inline ■ stop button (view/item/context menu)
        }),
        item(`node: ${s.node_id ?? "single-node (dist off)"}`, { icon: "vm" }),
      ];
    }
    if (parent.id === "engines") {
      const engines = Array.isArray(s?.engines) ? s.engines.filter((engine): engine is string => typeof engine === "string") : [];
      const lanes = laneItems(engines);
      if (!lanes.length) return [item("unknown (daemon not running)", { icon: "question" })];
      return lanes.map((l) =>
        item(l.engine, {
          icon: l.present ? "pass" : "circle-slash",
          description: l.present ? "registered" : "not registered — greyed out",
        }),
      );
    }
    const fleet = this.store.digest?.fleet as Record<string, any> | undefined;
    if (!fleet) return [item("distribution off", { icon: "circle-slash" })];
    const metrics = fleet.metrics ?? {};
    const conflicts = metrics.lease_conflicts ?? {};
    return [
      item(`nodes: ${(fleet.nodes as any[])?.length ?? 0}`, { icon: "vm", id: "fleet:nodes" }),
      item(`coordinator: ${fleet.coordinator?.holder ?? "—"}`, {
        icon: "star",
        description: fleet.coordinator?.iso ? String(fleet.coordinator.iso) : "",
        id: "fleet:coordinator",
      }),
      item(`open conflicts: ${conflicts.open_count ?? 0}`, { icon: "warning", id: "fleet:conflicts" }),
      item(`orphan reclaims: ${metrics.orphan_sweeps?.reclaimed_total ?? 0}`, { icon: "trash", id: "fleet:reclaims" }),
      item(`dispatch latency avg: ${metrics.dispatch_latency?.avg_s ?? "—"} s`, { icon: "dashboard", id: "fleet:latency" }),
    ];
  }
}
