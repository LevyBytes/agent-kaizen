import assert from "node:assert/strict";
import { test } from "node:test";

import { SessionIndex, SessionListClient } from "../../extension/src/sessionIndex";

class Client implements SessionListClient {
  calls: Array<Record<string, unknown> | undefined> = [];
  disposed = false;
  gate: Promise<Record<string, unknown>> | undefined;
  responses: Array<Record<string, unknown> | Error> = [];

  async list(args?: { controller?: "driven" | "observed"; limit?: number }): Promise<Record<string, unknown>> {
    this.calls.push(args);
    if (this.gate) return this.gate;
    const response = this.responses.shift() ?? { status: "OK", sessions: [] };
    if (response instanceof Error) throw response;
    return response;
  }

  dispose(): void {
    this.disposed = true;
  }
}

test("concurrent callers coalesce into one unfiltered session/list request", async () => {
  const client = new Client();
  let release!: (value: Record<string, unknown>) => void;
  client.gate = new Promise((resolve) => { release = resolve; });
  const index = new SessionIndex(client);
  const updates: number[] = [];
  index.subscribe((update) => updates.push(update.revision));
  const first = index.refresh();
  const second = index.refresh();
  const third = index.refresh();
  assert.equal(client.calls.length, 1);
  assert.deepEqual(client.calls[0], { limit: 1_000 });
  release({
    status: "OK",
    sessions: [
      { session_id: "driven", controller: "driven" },
      { session_id: "observed", controller: "observed" },
    ],
  });
  const [a, b, c] = await Promise.all([first, second, third]);
  assert.equal(a.revision, 1);
  assert.equal(a, b);
  assert.equal(b, c);
  assert.deepEqual(updates, [1]);
  assert.equal(Array.isArray(index.current()?.sessions), true);
});

test("failure fans out degraded state and recovery replaces it with current truth", async () => {
  const client = new Client();
  client.responses.push(new Error("daemon down"), { status: "OK", sessions: [{ session_id: "recovered" }] });
  const index = new SessionIndex(client);
  const updates: Array<{ revision: number; error?: string; sessions?: unknown }> = [];
  index.subscribe((update) => updates.push({
    revision: update.revision,
    ...(update.error ? { error: update.error.message } : {}),
    ...(update.response ? { sessions: update.response.sessions } : {}),
  }));
  const failed = await index.refresh();
  assert.match(failed.error?.message ?? "", /daemon down/);
  assert.equal(index.current(), undefined, "stale data is not authoritative during outage");
  const recovered = await index.refresh();
  assert.equal(recovered.error, undefined);
  assert.deepEqual(index.current()?.sessions, [{ session_id: "recovered" }]);
  assert.deepEqual(updates, [
    { revision: 1, error: "daemon down" },
    { revision: 2, sessions: [{ session_id: "recovered" }] },
  ]);
});

test("structured non-OK list response is a shared error and dispose owns the one client", async () => {
  const client = new Client();
  client.responses.push({ status: "DENIED", code: "DENIED_LIST" });
  const index = new SessionIndex(client);
  const broadcasts: number[] = [];
  index.subscribe((update) => broadcasts.push(update.revision));
  const first = index.refresh();
  const second = index.refresh();
  assert.equal(client.calls.length, 1);
  const [a, b] = await Promise.all([first, second]);
  assert.equal(a.error?.name, "SessionIndexError");
  assert.match(a.error?.message ?? "", /DENIED_LIST/);
  assert.equal(a.error, b.error, "coalesced callers receive the same error object");
  index.dispose();
  assert.equal(client.disposed, true);
  client.responses.push({ status: "OK", sessions: [] });
  await index.refresh();
  assert.deepEqual(broadcasts, [1], "dispose removes every listener");
});
