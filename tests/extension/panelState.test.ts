import assert from "node:assert/strict";
import { test } from "node:test";

import {
  LEGACY_RUN_CURSORS_KEY,
  PANEL_STATE_KEY,
  PanelStateStore,
  RECENTLY_CLOSED_LIMIT,
  WorkspaceMemento,
} from "../../extension/src/panelState";

/** In-memory memento that clones writes and deletes keys when updated with undefined. */
class MemoryWorkspace implements WorkspaceMemento {
  readonly values = new Map<string, unknown>();
  failures = 0;

  get<T>(key: string, defaultValue?: T): T | undefined {
    return (this.values.has(key) ? this.values.get(key) : defaultValue) as T | undefined;
  }

  update(key: string, value: unknown): PromiseLike<void> {
    if (this.failures > 0) {
      this.failures -= 1;
      return Promise.reject(new Error("transient workspace update failure"));
    }
    if (value === undefined) this.values.delete(key);
    else this.values.set(key, structuredClone(value));
    return Promise.resolve();
  }
}

test("transient persistence failures do not strand initialization or later writes", async () => {
  const workspace = new MemoryWorkspace();
  workspace.failures = 1;
  const store = new PanelStateStore(workspace, () => "conversation-retry");
  await assert.rejects(store.initialize(), /transient workspace update failure/);
  await assert.doesNotReject(store.initialize());

  workspace.failures = 1;
  store.create("first");
  await assert.rejects(store.flush(), /transient workspace update failure/);
  store.create("second");
  await assert.doesNotReject(store.flush());
  const persisted = workspace.values.get(PANEL_STATE_KEY) as { panels: Array<{ conversationId: string }> };
  assert.deepEqual(persisted.panels.map(({ conversationId }) => conversationId), ["first", "second"]);
});

test("legacy singleton migration is deterministic, one-time, and leaves kaizen.bindings untouched", async () => {
  const first = new MemoryWorkspace();
  first.values.set(LEGACY_RUN_CURSORS_KEY, {
    session_id: "session-1",
    agent_run_id: "run-1",
    profile_hash: "profile-1",
  });
  first.values.set("kaizen.bindings", { sessions: ["keep"], cursors: { keep: 4 } });
  const store = new PanelStateStore(first, () => "unused");
  await store.initialize();
  await store.flush();
  const migrated = store.openRecords();
  assert.equal(migrated.length, 1);
  assert.match(migrated[0].conversationId, /^legacy-[0-9a-f]{16}$/);
  assert.deepEqual(
    { sessionId: migrated[0].sessionId, agentRunId: migrated[0].agentRunId, profileHash: migrated[0].profileHash },
    { sessionId: "session-1", agentRunId: "run-1", profileHash: "profile-1" },
  );
  assert.equal(first.values.has(LEGACY_RUN_CURSORS_KEY), false);
  assert.deepEqual(first.values.get("kaizen.bindings"), { sessions: ["keep"], cursors: { keep: 4 } });

  const second = new MemoryWorkspace();
  second.values.set(LEGACY_RUN_CURSORS_KEY, { session_id: "session-1", agent_run_id: "run-1", profile_hash: "profile-1" });
  const secondStore = new PanelStateStore(second, () => "unused");
  await secondStore.initialize();
  assert.equal(secondStore.openRecords()[0].conversationId, migrated[0].conversationId);

  // Any legacy blob is ignored once durable v2 exists; its contents are intentionally irrelevant.
  first.values.set(LEGACY_RUN_CURSORS_KEY, {});
  const reloaded = new PanelStateStore(first, () => "unused");
  await reloaded.initialize();
  assert.equal(reloaded.records().length, 1, "durable v2 prevents duplicate legacy migration");
  assert.equal(first.values.has(LEGACY_RUN_CURSORS_KEY), false);
});

test("scoped controller adapter persists only run metadata and monotonic cursor", async () => {
  const workspace = new MemoryWorkspace();
  const store = new PanelStateStore(workspace, () => "conversation-1");
  await store.initialize();
  const record = store.create();
  const scoped = store.scopedMemento(record.conversationId);
  await scoped.update(LEGACY_RUN_CURSORS_KEY, {
    session_id: "session-1",
    agent_run_id: "run-1",
    profile_hash: "hash-1",
    prompt: "must not persist",
    queue: ["must not persist"],
  });
  scoped.updateCursor(8);
  scoped.updateCursor(3);
  store.reconcileSessionList({ status: "OK", sessions: [{ session_id: "session-1", title: "First prompt" }] });
  await store.flush();

  assert.deepEqual(store.get(record.conversationId), {
    conversationId: "conversation-1",
    sessionId: "session-1",
    agentRunId: "run-1",
    profileHash: "hash-1",
    title: "First prompt",
    cursor: 8,
    order: 1,
  });
  const persisted = workspace.values.get(PANEL_STATE_KEY) as { panels: Array<Record<string, unknown>> };
  assert.deepEqual(Object.keys(persisted.panels[0]).sort(), [
    "agentRunId", "conversationId", "cursor", "order", "profileHash", "sessionId", "title",
  ]);
  const serialized = JSON.stringify(persisted);
  for (const forbidden of ["must not persist", "\"draft\"", "\"queue\"", "\"attachments\"", "\"context_refs\""]) {
    assert.ok(!serialized.includes(forbidden), `forbidden persisted field: ${forbidden}`);
  }

  await scoped.update(LEGACY_RUN_CURSORS_KEY, { session_id: "session-1", agent_run_id: "run-2" });
  assert.equal(store.get(record.conversationId)?.cursor, 0, "cursor resets when the linked run changes");
  await store.flush();
  const reset = workspace.values.get(PANEL_STATE_KEY) as { panels: Array<{ cursor: number }> };
  assert.equal(reset.panels[0].cursor, 0, "the cursor reset is durable");
});

test("recently closed is newest-first, capped at ten, and never evicts open panels", async () => {
  const workspace = new MemoryWorkspace();
  let id = 0;
  let now = 100;
  const store = new PanelStateStore(workspace, () => `conversation-${++id}`, () => ++now);
  await store.initialize();
  const open = store.create();
  const closed: string[] = [];
  for (let index = 0; index < RECENTLY_CLOSED_LIMIT + 3; index += 1) {
    const record = store.create();
    closed.push(record.conversationId);
    store.close(record.conversationId);
  }
  assert.equal(store.openRecords().some((entry) => entry.conversationId === open.conversationId), true);
  assert.equal(store.recentlyClosed().length, RECENTLY_CLOSED_LIMIT);
  assert.equal(store.recentlyClosed()[0].conversationId, closed.at(-1));
  assert.equal(store.get(closed[0]), undefined, "oldest closed metadata is evicted");

  const latest = store.recentlyClosed()[0];
  const reopened = store.reopen(latest.conversationId)!;
  assert.equal(reopened.closedAt, undefined);
  assert.equal(store.openRecords()[0].conversationId, latest.conversationId);
});

test("malformed v2 rows are rejected without leaking unexpected fields", async () => {
  const workspace = new MemoryWorkspace();
  workspace.values.set(PANEL_STATE_KEY, {
    version: 2,
    migrationComplete: true,
    nextOrder: -5,
    panels: [
      { conversationId: "valid", cursor: 2, order: 4, prompt: "drop" },
      { conversationId: "", cursor: 0, order: 1 },
      { conversationId: "valid", cursor: 9, order: 5 },
    ],
  });
  const store = new PanelStateStore(workspace, () => "new");
  await store.initialize();
  assert.deepEqual(store.records(), [{ conversationId: "valid", cursor: 2, order: 4 }]);
  await store.flush();
  assert.deepEqual(workspace.values.get(PANEL_STATE_KEY), {
    version: 2,
    migrationComplete: true,
    nextOrder: 5,
    panels: [{ conversationId: "valid", cursor: 2, order: 4 }],
  });
  assert.equal(store.create().order, 5);
});

test("equal close timestamps use newest panel order as a deterministic tie-break", async () => {
  const workspace = new MemoryWorkspace();
  const store = new PanelStateStore(workspace, () => "unused", () => 100);
  await store.initialize();
  const first = store.create("first");
  const second = store.create("second");
  store.close(first.conversationId);
  store.close(second.conversationId);
  assert.deepEqual(store.recentlyClosed().map(({ conversationId }) => conversationId), ["second", "first"]);
});
