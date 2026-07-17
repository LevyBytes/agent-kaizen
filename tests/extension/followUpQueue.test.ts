import assert from "node:assert/strict";
import { test } from "node:test";

import { FOLLOW_UP_QUEUE_LIMIT, FollowUpQueue } from "../../extension/src/followUpQueue";

test("FIFO accepts exactly ten prompts and rejects the eleventh without changing the head", () => {
  const queue = new FollowUpQueue();
  for (let index = 0; index < FOLLOW_UP_QUEUE_LIMIT; index += 1) {
    assert.equal(queue.enqueue(`prompt ${index + 1}`, `token-${index + 1}`).accepted, true);
  }
  assert.deepEqual(queue.enqueue("overflow", "overflow-token"), { accepted: false, reason: "full" });
  assert.equal(queue.size, 10);
  assert.equal(queue.view().items[0].preview, "prompt 1");
});

test("only an explicit normal-completion dispatch can expose the head", () => {
  const queue = new FollowUpQueue();
  const first = queue.enqueue("first", "one");
  queue.enqueue("second", "two");
  assert.equal(first.accepted, true);
  const head = queue.beginDispatchAfterNormalCompletion();
  assert.equal(head?.prompt, "first");
  assert.equal(queue.view().items[0].dispatching, true);
  assert.equal(queue.beginDispatchAfterNormalCompletion(), undefined, "one dispatch at a time");
  assert.equal(queue.acknowledgeDurable("wrong-id"), false);
  assert.equal(queue.size, 2);
  assert.equal(queue.acknowledgeDurable(head!.id), true);
  assert.equal(queue.size, 1);
  assert.equal(queue.beginDispatchAfterNormalCompletion()?.prompt, "second");
});

test("durable acknowledgement requires the dispatch id to remain the queue head", () => {
  const queue = new FollowUpQueue();
  const first = queue.enqueue("first");
  queue.enqueue("second");
  assert.equal(first.accepted, true);
  const dispatched = queue.beginDispatchAfterNormalCompletion()!;
  const internals = queue as unknown as { items: Array<{ id: string }> };
  internals.items.shift();
  assert.equal(queue.acknowledgeDurable(dispatched.id), false);
  assert.equal(queue.size, 1);
});

test("denial and every abnormal pause preserve the exact head", () => {
  for (const reason of ["failure", "interrupt", "kill", "approval_timeout", "lease_conflict", "uncertain_transport"] as const) {
    const queue = new FollowUpQueue();
    const accepted = queue.enqueue("  preserve internal   spacing  ", "token");
    assert.equal(accepted.accepted, true);
    const head = queue.beginDispatchAfterNormalCompletion()!;
    queue.rejectDispatch(head.id);
    queue.pause(reason);
    assert.equal(queue.paused, true);
    assert.equal(queue.pauseReason, reason);
    assert.equal(queue.beginDispatchAfterNormalCompletion(), undefined);
    assert.equal(queue.size, 1);
    assert.equal(queue.view().items[0].preview, "preserve internal spacing");
    assert.equal(queue.view().items[0].dispatching, false);
    assert.equal(queue.view().pauseReason, reason);
  }
});

test("queue state is explicit and clear is a complete in-memory reset", () => {
  const queue = new FollowUpQueue();
  assert.deepEqual(queue.enqueue(" "), { accepted: false, reason: "empty" });
  queue.enqueue(`${"word   ".repeat(30)}tail`);
  assert.equal(queue.view().items[0].dispatching, false);
  const dispatch = queue.beginDispatchAfterNormalCompletion();
  assert.ok(dispatch);
  queue.pause("failure");
  const view = queue.view();
  assert.equal(view.items[0].preview.length, 80);
  assert.match(view.items[0].preview, /\u2026$/);
  assert.doesNotMatch(view.items[0].preview, /\s{2}/);
  assert.equal(view.pauseReason, "failure");
  queue.clear();
  assert.deepEqual(queue.view(), { items: [], paused: false });
  assert.equal(queue.enqueue("fresh").accepted, true);
  assert.equal(queue.beginDispatchAfterNormalCompletion()?.prompt, "fresh");
});
