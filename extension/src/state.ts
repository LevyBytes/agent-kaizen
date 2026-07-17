/**
 * Pure UI-state logic (v8 M8, plan §D) — no vscode/net imports so every rule here is unit-testable:
 * unowned-session read-only gating, the dynamic engine-lane list (absent reserved lane greys out,
 * never a hard-coded engine set), and the persisted pane↔session bindings + per-session replay
 * cursors that make panes survive a VS Code reload.
 */

/** Session fence metadata; node_epoch is authoritative only at the server attach boundary. */
export interface SessionInfo {
  id: string;
  owning_node?: string | null;
  node_epoch?: number | null;
  state?: string;
  summary?: string;
  engine?: string;
}

/** §D: unowned sessions are READ-ONLY. A session is owned here iff it carries no fence at all
 * (single-node plane) or its owning_node IS this daemon's fleet node. */
export function isReadOnly(session: SessionInfo, myNodeId: string | null): boolean {
  const owner = session.owning_node ?? null;
  if (owner === null) return false;
  return owner !== myNodeId;
}

export interface Lane {
  engine: string;
  present: boolean;
}

/** The reserved-but-not-yet-registered lane set (§2 reserved Claude value space; M-CLAUDE registers
 * the adapter, at which point the daemon reports it and the lane un-greys with ZERO UI change). */
export const RESERVED_LANES = ["claude"];

/** Engine lanes from the daemon's ADAPTER REGISTRATION (status.engines), plus reserved lanes greyed
 * when absent. Never hard-codes the registered set. */
export function laneItems(engines: string[], reserved: string[] = RESERVED_LANES): Lane[] {
  const present = new Set(engines);
  const all = [...new Set([...engines, ...reserved])];
  return all.map((engine) => ({ engine, present: present.has(engine) }));
}

/** Persisted pane bindings; each cursor is the high-water rendered timeline length. */
export interface Bindings {
  sessions: string[];
  cursors: Record<string, number>;
}

/** Empty persisted bindings and cursor watermarks. */
export const EMPTY_BINDINGS: Bindings = { sessions: [], cursors: {} };

/** Append a session binding idempotently. */
export function bindSession(b: Bindings, sessionId: string): Bindings {
  return b.sessions.includes(sessionId) ? b : { ...b, sessions: [...b.sessions, sessionId] };
}

/** Remove a session binding and its replay cursor. */
export function unbindSession(b: Bindings, sessionId: string): Bindings {
  const cursors = { ...b.cursors };
  delete cursors[sessionId];
  return { sessions: b.sessions.filter((s) => s !== sessionId), cursors };
}

/** Advance a session's replay cursor to the rendered timeline length; returns how many events were
 * NEW since the last render (the reload-survival contract: cursors persist, so a reloaded pane knows
 * what it has already shown). */
export function advanceCursor(b: Bindings, sessionId: string, timelineLength: number): { bindings: Bindings; newEvents: number } {
  const seen = b.cursors[sessionId] ?? 0;
  const highWater = Math.max(seen, timelineLength);
  return {
    bindings: { ...b, cursors: { ...b.cursors, [sessionId]: highWater } },
    newEvents: Math.max(0, timelineLength - seen),
  };
}
