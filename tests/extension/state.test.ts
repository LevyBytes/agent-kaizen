/**
 * Pure UI-state rules (node:test): the M8 exit-criteria logic legs — unowned sessions read-only,
 * dynamic engine lanes with the absent reserved lane greyed (never hard-coded), and pane↔session
 * bindings + replay cursors surviving a "reload" (state reconstruction from the persisted shape).
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { advanceCursor, bindSession, EMPTY_BINDINGS, isReadOnly, laneItems, unbindSession } from "../../extension/src/state";

test("unfenced session (single-node plane) is owned/writable", () => {
  assert.equal(isReadOnly({ id: "s1", owning_node: null }, null), false);
  assert.equal(isReadOnly({ id: "s1" }, "nMe"), false);
});

test("session fenced to THIS node is writable; fenced elsewhere is read-only", () => {
  assert.equal(isReadOnly({ id: "s1", owning_node: "nMe" }, "nMe"), false);
  assert.equal(isReadOnly({ id: "s1", owning_node: "nOther" }, "nMe"), true);
  // Fenced session on a dist-off daemon (no node id) is read-only too — we provably do not own it.
  assert.equal(isReadOnly({ id: "s1", owning_node: "nOther" }, null), true);
  assert.equal(isReadOnly({ id: "s1", owning_node: "" }, "nMe"), true);
});

test("lanes come from adapter registration; absent claude lane is present-but-greyed", () => {
  assert.deepEqual(laneItems(["codex", "local_llm"]), [
    { engine: "codex", present: true },
    { engine: "local_llm", present: true },
    { engine: "claude", present: false },
  ]);
});

test("a registered claude lane un-greys with no UI change (deferral insurance)", () => {
  const lanes = laneItems(["claude", "codex"]);
  assert.deepEqual(lanes.find((l) => l.engine === "claude"), { engine: "claude", present: true });
  assert.equal(lanes.length, 2);
  assert.deepEqual(laneItems(["codex"], ["codex", "future"]), [
    { engine: "codex", present: true },
    { engine: "future", present: false },
  ]);
});

test("an empty registration list renders only the reserved grey lane", () => {
  assert.deepEqual(laneItems([]), [{ engine: "claude", present: false }]);
});

test("no hard-coded engine set: unknown future lanes render as reported", () => {
  assert.deepEqual(laneItems(["gemini_cli"]), [
    { engine: "gemini_cli", present: true },
    { engine: "claude", present: false },
  ]);
});

test("bind/unbind is idempotent and drops the cursor with the pane", () => {
  let b = bindSession(EMPTY_BINDINGS, "s1");
  b = bindSession(b, "s1");
  assert.deepEqual(b.sessions, ["s1"]);
  b = advanceCursor(b, "s1", 5).bindings;
  b = unbindSession(b, "s1");
  assert.deepEqual(b, { sessions: [], cursors: {} });
  assert.deepEqual(EMPTY_BINDINGS, { sessions: [], cursors: {} });
  assert.deepEqual(unbindSession(EMPTY_BINDINGS, "missing"), { sessions: [], cursors: {} });
});

test("replay cursor survives reload: reconstructed state knows what was already rendered", () => {
  const before = advanceCursor(bindSession(EMPTY_BINDINGS, "s1"), "s1", 5);
  assert.equal(before.newEvents, 5);
  // "Reload": the persisted shape round-trips through JSON (workspaceState semantics).
  const restored = JSON.parse(JSON.stringify(before.bindings));
  const after = advanceCursor(restored, "s1", 7);
  assert.equal(after.newEvents, 2); // only the two events appended since the pre-reload render
  const idle = advanceCursor(after.bindings, "s1", 7);
  assert.equal(idle.newEvents, 0);
  const rewind = advanceCursor(idle.bindings, "s1", 3);
  assert.equal(rewind.newEvents, 0);
  assert.equal(rewind.bindings.cursors.s1, 7);
});

test("multi-pane: two bound sessions keep independent cursors", () => {
  let b = bindSession(bindSession(EMPTY_BINDINGS, "s1"), "s2");
  b = advanceCursor(b, "s1", 3).bindings;
  b = advanceCursor(b, "s2", 10).bindings;
  assert.equal(advanceCursor(b, "s1", 4).newEvents, 1);
  assert.equal(advanceCursor(b, "s2", 10).newEvents, 0);
});
