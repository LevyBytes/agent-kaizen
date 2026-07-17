/** Versioned metadata-only persistence for editor chat panels. */

import * as crypto from "node:crypto";

import type { MementoPort, PersistedRun } from "./conversationController";

export const PANEL_STATE_KEY = "kaizen.chat.panels.v2";
export const LEGACY_RUN_CURSORS_KEY = "kaizen.runCursors";
export const RECENTLY_CLOSED_LIMIT = 10;

export interface PanelMetadata {
  conversationId: string;
  sessionId?: string;
  agentRunId?: string;
  profileHash?: string;
  title?: string;
  cursor: number;
  order: number;
  closedAt?: number;
}

interface PanelStateV2 {
  version: 2;
  migrationComplete: true;
  nextOrder: number;
  panels: PanelMetadata[];
}

export interface WorkspaceMemento {
  get<T>(key: string, defaultValue?: T): T | undefined;
  update(key: string, value: unknown): PromiseLike<void>;
}

export type PanelStateListener = (records: readonly PanelMetadata[]) => void;

export class PanelStateStore {
  private state: PanelStateV2 = { version: 2, migrationComplete: true, nextOrder: 1, panels: [] };
  private initialized = false;
  private writeChain: Promise<void> = Promise.resolve();
  private readonly listeners = new Set<PanelStateListener>();

  constructor(
    private readonly workspace: WorkspaceMemento,
    private readonly makeId: () => string = () => crypto.randomUUID(),
    private readonly now: () => number = () => Date.now(),
  ) {}

  /** Idempotently load, sanitize, persist v2 state, and retire the legacy cursor key. */
  async initialize(): Promise<void> {
    if (this.initialized) return;
    this.initialized = true;
    try {
      const loaded = sanitizeState(this.workspace.get<unknown>(PANEL_STATE_KEY));
      if (loaded) {
        this.state = loaded;
      } else {
        const legacy = sanitizeLegacy(this.workspace.get<unknown>(LEGACY_RUN_CURSORS_KEY));
        const panels = legacy ? [legacyPanel(legacy)] : [];
        this.state = {
          version: 2,
          migrationComplete: true,
          nextOrder: panels.length + 1,
          panels,
        };
      }
      // Rewrite the sanitized allow-list even when v2 already existed; unexpected legacy fields vanish.
      await this.persist();
      // Once v2 is durable, the singleton cursor must never be read again. Bindings are untouched.
      if (this.workspace.get<unknown>(LEGACY_RUN_CURSORS_KEY) !== undefined) {
        await this.workspace.update(LEGACY_RUN_CURSORS_KEY, undefined);
      }
      this.emit();
    } catch (error) {
      this.initialized = false;
      throw error;
    }
  }

  subscribe(listener: PanelStateListener): { dispose(): void } {
    this.listeners.add(listener);
    listener(this.records());
    return { dispose: () => this.listeners.delete(listener) };
  }

  records(): PanelMetadata[] {
    return this.state.panels.map(copyRecord);
  }

  get(conversationId: string): PanelMetadata | undefined {
    const record = this.state.panels.find((entry) => entry.conversationId === conversationId);
    return record ? copyRecord(record) : undefined;
  }

  openRecords(): PanelMetadata[] {
    return this.state.panels.filter((entry) => entry.closedAt === undefined).sort(byOrderNewest).map(copyRecord);
  }

  recentlyClosed(): PanelMetadata[] {
    return this.state.panels.filter((entry) => entry.closedAt !== undefined).sort(byClosedNewest).map(copyRecord);
  }

  /** Create a panel with a trimmed, bounded, collision-free conversation id. */
  create(conversationId = this.makeId()): PanelMetadata {
    const uniqueId = this.uniqueId(cleanId(conversationId));
    const record: PanelMetadata = {
      conversationId: uniqueId,
      cursor: 0,
      order: this.state.nextOrder++,
    };
    this.state.panels.push(record);
    this.commit();
    return copyRecord(record);
  }

  reopen(conversationId: string): PanelMetadata | undefined {
    const record = this.find(conversationId);
    if (!record) return undefined;
    delete record.closedAt;
    record.order = this.state.nextOrder++;
    this.commit();
    return copyRecord(record);
  }

  touch(conversationId: string): void {
    const record = this.find(conversationId);
    if (!record || record.closedAt !== undefined) return;
    record.order = this.state.nextOrder++;
    this.commit();
  }

  close(conversationId: string): void {
    const record = this.find(conversationId);
    if (!record) return;
    record.closedAt = this.now();
    this.commit();
  }

  updateRun(conversationId: string, run: PersistedRun | undefined): void {
    const record = this.find(conversationId);
    if (!record) return;
    if (!run) {
      delete record.sessionId;
      delete record.agentRunId;
      delete record.profileHash;
      record.cursor = 0;
    } else {
      const runChanged = record.agentRunId !== run.agent_run_id;
      record.sessionId = run.session_id;
      record.agentRunId = run.agent_run_id;
      if (runChanged) {
        record.cursor = 0;
        delete record.title;
      }
      if (run.profile_hash) record.profileHash = run.profile_hash;
      else delete record.profileHash;
    }
    this.commit();
  }

  /** Advance the cursor monotonically; ignore negative or unsafe values. */
  updateCursor(conversationId: string, cursor: number): void {
    const record = this.find(conversationId);
    if (!record || !Number.isSafeInteger(cursor) || cursor < 0) return;
    record.cursor = Math.max(record.cursor, cursor);
    this.commit();
  }

  reconcileSessionList(response: Record<string, unknown>): void {
    const sessions = Array.isArray(response.sessions) ? response.sessions : [];
    const titles = new Map<string, string | undefined>();
    for (const raw of sessions) {
      if (!raw || typeof raw !== "object") continue;
      const row = raw as Record<string, unknown>;
      const sessionId = cleanOptional(row.session_id);
      const title = cleanOptional(row.title);
      if (sessionId) titles.set(sessionId, title);
    }
    let changed = false;
    for (const record of this.state.panels) {
      if (!record.sessionId || !titles.has(record.sessionId)) continue;
      const title = titles.get(record.sessionId);
      if (title === record.title) continue;
      if (title) record.title = title;
      else delete record.title;
      changed = true;
    }
    if (changed) this.commit();
  }

  scopedMemento(conversationId: string): PanelMemento {
    return new PanelMemento(this, conversationId);
  }

  async flush(): Promise<void> {
    await this.writeChain;
  }

  private uniqueId(candidate: string): string {
    let value = candidate || "conversation";
    let suffix = 1;
    while (this.find(value)) value = `${candidate || "conversation"}-${suffix++}`;
    return value;
  }

  private find(conversationId: string): PanelMetadata | undefined {
    return this.state.panels.find((entry) => entry.conversationId === conversationId);
  }

  private trimClosed(): void {
    const keep = new Set(this.state.panels.filter((entry) => entry.closedAt !== undefined).sort(byClosedNewest).slice(0, RECENTLY_CLOSED_LIMIT));
    this.state.panels = this.state.panels.filter((entry) => entry.closedAt === undefined || keep.has(entry));
  }

  private commit(): void {
    this.trimClosed();
    void this.persist().catch(() => undefined);
    this.emit();
  }

  private persist(): Promise<void> {
    const snapshot = cloneState(this.state);
    this.writeChain = this.writeChain.catch(() => undefined).then(async () => {
      await this.workspace.update(PANEL_STATE_KEY, snapshot);
    });
    return this.writeChain;
  }

  private emit(): void {
    const records = this.records();
    for (const listener of this.listeners) listener(records);
  }
}

export class PanelMemento implements MementoPort {
  constructor(
    private readonly store: PanelStateStore,
    private readonly conversationId: string,
  ) {}

  get<T>(key: string, defaultValue?: T): T | undefined {
    if (key !== LEGACY_RUN_CURSORS_KEY) return defaultValue;
    const record = this.store.get(this.conversationId);
    if (!record?.sessionId || !record.agentRunId) return defaultValue;
    return {
      session_id: record.sessionId,
      agent_run_id: record.agentRunId,
      ...(record.profileHash ? { profile_hash: record.profileHash } : {}),
    } as T;
  }

  update(key: string, value: unknown): PromiseLike<void> {
    if (key === LEGACY_RUN_CURSORS_KEY) this.store.updateRun(this.conversationId, sanitizeLegacy(value));
    return Promise.resolve();
  }

  updateCursor(cursor: number): void {
    this.store.updateCursor(this.conversationId, cursor);
  }
}

function sanitizeState(value: unknown): PanelStateV2 | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const row = value as Record<string, unknown>;
  if (row.version !== 2 || row.migrationComplete !== true || !Array.isArray(row.panels)) return undefined;
  const seen = new Set<string>();
  const panels: PanelMetadata[] = [];
  for (const raw of row.panels) {
    const record = sanitizeRecord(raw);
    if (!record || seen.has(record.conversationId)) continue;
    seen.add(record.conversationId);
    panels.push(record);
  }
  const maxOrder = panels.reduce((maximum, record) => Math.max(maximum, record.order), 0);
  const nextOrder = Number.isSafeInteger(row.nextOrder) && Number(row.nextOrder) > maxOrder
    ? Number(row.nextOrder)
    : maxOrder + 1;
  const closed = panels.filter((entry) => entry.closedAt !== undefined).sort(byClosedNewest).slice(0, RECENTLY_CLOSED_LIMIT);
  const keepClosed = new Set(closed);
  return {
    version: 2,
    migrationComplete: true,
    nextOrder,
    panels: panels.filter((entry) => entry.closedAt === undefined || keepClosed.has(entry)),
  };
}

function sanitizeRecord(value: unknown): PanelMetadata | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const row = value as Record<string, unknown>;
  const conversationId = cleanOptional(row.conversationId);
  if (!conversationId || !Number.isSafeInteger(row.order) || Number(row.order) < 0) return undefined;
  const cursor = Number.isSafeInteger(row.cursor) && Number(row.cursor) >= 0 ? Number(row.cursor) : 0;
  const closedAt = typeof row.closedAt === "number" && Number.isFinite(row.closedAt) && row.closedAt >= 0 ? row.closedAt : undefined;
  return {
    conversationId,
    ...(cleanOptional(row.sessionId) ? { sessionId: cleanOptional(row.sessionId) } : {}),
    ...(cleanOptional(row.agentRunId) ? { agentRunId: cleanOptional(row.agentRunId) } : {}),
    ...(cleanOptional(row.profileHash) ? { profileHash: cleanOptional(row.profileHash) } : {}),
    ...(cleanOptional(row.title) ? { title: cleanOptional(row.title) } : {}),
    cursor,
    order: Number(row.order),
    ...(closedAt !== undefined ? { closedAt } : {}),
  };
}

function sanitizeLegacy(value: unknown): PersistedRun | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const row = value as Record<string, unknown>;
  const sessionId = cleanOptional(row.session_id);
  const runId = cleanOptional(row.agent_run_id);
  if (!sessionId || !runId) return undefined;
  const profileHash = cleanOptional(row.profile_hash);
  return {
    session_id: sessionId,
    agent_run_id: runId,
    ...(profileHash ? { profile_hash: profileHash } : {}),
  };
}

function legacyPanel(run: PersistedRun): PanelMetadata {
  const digest = crypto.createHash("sha256").update(`${run.session_id}\u0000${run.agent_run_id}`).digest("hex").slice(0, 16);
  return {
    conversationId: `legacy-${digest}`,
    sessionId: run.session_id,
    agentRunId: run.agent_run_id,
    ...(run.profile_hash ? { profileHash: run.profile_hash } : {}),
    cursor: 0,
    order: 1,
  };
}

function cleanId(value: string): string {
  return value.trim().slice(0, 128);
}

function cleanOptional(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function copyRecord(record: PanelMetadata): PanelMetadata {
  return { ...record };
}

function cloneState(state: PanelStateV2): PanelStateV2 {
  return { ...state, panels: state.panels.map(copyRecord) };
}

function byOrderNewest(left: PanelMetadata, right: PanelMetadata): number {
  return right.order - left.order || left.conversationId.localeCompare(right.conversationId);
}

function byClosedNewest(left: PanelMetadata, right: PanelMetadata): number {
  return (right.closedAt ?? 0) - (left.closedAt ?? 0) || byOrderNewest(left, right);
}
