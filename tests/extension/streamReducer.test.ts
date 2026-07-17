import assert from "node:assert/strict";
import { test } from "node:test";

import { StreamReducer } from "../../extension/src/streamReducer";
import type { WireEvent } from "../../extension/src/webviewProtocol";

const delta = (seq: number, text: string, turn_id = "turn-1"): { seq: number; text: string; turn_id: string } => ({
  seq,
  text,
  turn_id,
});
const assistant = (sequence_no: number, text: string, turn_id = "turn-1"): WireEvent => ({
  sequence_no,
  event_kind: "chat_message",
  marker: "point",
  body: JSON.stringify({ role: "assistant", text, source: "driven", turn_id }),
});

test("out-of-order repeated non-consuming pages reduce to one ordered bubble", () => {
  const stream = new StreamReducer();
  stream.apply([delta(2, "B"), delta(1, "A"), delta(2, "B")], 2, false);
  assert.deepEqual(stream.snapshot(), { turnId: "turn-1", text: "AB" });
  stream.apply([delta(1, "A"), delta(2, "B"), delta(3, "C")], 3, false);
  assert.deepEqual(stream.snapshot(), { turnId: "turn-1", text: "ABC" });
});

test("dropped or gapped tails are suppressed until authoritative durable completion", () => {
  const dropped = new StreamReducer();
  dropped.apply([delta(4, "tail")], 4, true);
  assert.equal(dropped.snapshot(), undefined);
  dropped.apply([delta(5, "still hidden")], 5, false);
  assert.equal(dropped.snapshot(), undefined);
  dropped.reconcileDurable([assistant(10, "complete")]);
  assert.equal(dropped.acceptanceState().suppressed, false);
  dropped.apply([delta(6, "next", "turn-2")], 6, false);
  assert.deepEqual(dropped.snapshot(), { turnId: "turn-2", text: "next" });

  const gap = new StreamReducer();
  gap.apply([delta(1, "A"), delta(3, "C")], 3, false);
  assert.equal(gap.snapshot(), undefined);
  assert.deepEqual(gap.acceptanceState(), {
    sequences: [1],
    ordered: false,
    durableReplacements: 0,
    suppressed: true,
  });
});

test("durable assistant replaces the bubble and post-final chunks never reappear", () => {
  const stream = new StreamReducer();
  stream.apply([delta(1, "partial")], 1, false);
  stream.reconcileDurable([assistant(2, "authoritative")]);
  assert.equal(stream.snapshot(), undefined);
  assert.deepEqual(stream.acceptanceState(), {
    sequences: [1],
    ordered: true,
    durableReplacements: 1,
    suppressed: false,
  });
  stream.apply([delta(2, "late")], 2, false);
  assert.equal(stream.snapshot(), undefined);
});

test("a stale turn close cannot clear the active turn tail", () => {
  const stream = new StreamReducer();
  stream.apply([delta(1, "old", "turn-1")], 1, false);
  stream.apply([delta(2, "active", "turn-2")], 2, false);
  assert.deepEqual(stream.snapshot(), { turnId: "turn-2", text: "active" });
  stream.reconcileDurable([{ sequence_no: 3, event_kind: "turn", marker: "close_ok", turn_id: "turn-1" }]);
  assert.deepEqual(stream.snapshot(), { turnId: "turn-2", text: "active" });
  stream.reconcileDurable([{ sequence_no: 4, event_kind: "turn", marker: "close_ok", turn_id: "turn-2" }]);
  assert.equal(stream.snapshot(), undefined);
});

test("failure, terminal close, and restart reset clear all ephemeral state", () => {
  const stream = new StreamReducer();
  stream.apply([delta(1, "partial")], 1, false);
  stream.reconcileDurable([{ sequence_no: 2, event_kind: "turn", marker: "close_fail", turn_id: "turn-1" }]);
  assert.equal(stream.snapshot(), undefined);
  stream.reset();
  assert.deepEqual(stream.acceptanceState(), {
    sequences: [],
    ordered: true,
    durableReplacements: 0,
    suppressed: false,
  });
  stream.apply([delta(1, "fresh", "turn-new")], 1, false);
  assert.deepEqual(stream.snapshot(), { turnId: "turn-new", text: "fresh" });
  stream.clearFailure();
  assert.equal(stream.snapshot(), undefined);
});
