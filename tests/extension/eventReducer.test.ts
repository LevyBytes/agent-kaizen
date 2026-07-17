import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";

import {
  emptyState,
  reduceEvents,
  scopedCorrelation,
  timelineIdentity,
  ToolCard,
} from "../../extension/src/eventReducer";
import type { WireEvent } from "../../extension/src/webviewProtocol";

let seq = 0;
beforeEach(() => { seq = 0; });
function ev(partial: Partial<WireEvent> & Pick<WireEvent, "event_kind" | "marker">): WireEvent {
  return { sequence_no: ++seq, ...partial };
}

// Explicit literals pin replay/cross-run wire identities; ev() is reserved for tests where only order matters.

test("timeline identity is agent_run_id:sequence_no and cards use run-scoped correlations", () => {
  const state = emptyState();
  const open = ev({
    agent_run_id: "run-a",
    event_kind: "tool_call",
    marker: "open",
    correlation_id: "same",
    summary: "tool call read_file",
    body: JSON.stringify({ tool: "read_file" }),
  });
  const ops = reduceEvents(state, [open]);
  const card = (ops[0] as { card: ToolCard }).card;
  assert.equal(card.timelineKey, timelineIdentity("run-a", open.sequence_no));
  assert.equal(state.cards.get(scopedCorrelation("run-a", "same"))?.status, "running");
  // Pin the public wire-format identity as well as the helper result.
  assert.equal(state.timeline[0].key, `run-a:${open.sequence_no}`);
});

test("the same correlation id in two runs creates two independent cards", () => {
  const state = emptyState();
  reduceEvents(state, [
    { sequence_no: 1, agent_run_id: "run-a", event_kind: "tool_call", marker: "open", correlation_id: "same", body: JSON.stringify({ tool: "read" }) },
    { sequence_no: 1, agent_run_id: "run-b", event_kind: "tool_call", marker: "open", correlation_id: "same", body: JSON.stringify({ tool: "write" }) },
    { sequence_no: 2, agent_run_id: "run-a", event_kind: "tool_call", marker: "close_ok", correlation_id: "same", summary: "read done" },
  ]);
  assert.equal(state.cards.size, 2);
  assert.equal(state.cards.get(scopedCorrelation("run-a", "same"))?.status, "ok");
  assert.equal(state.cards.get(scopedCorrelation("run-b", "same"))?.status, "running");
});

test("tool close mutates the card at its first-event position", () => {
  const state = emptyState();
  reduceEvents(state, [
    { sequence_no: 1, agent_run_id: "run", event_kind: "tool_call", marker: "open", correlation_id: "tool", body: JSON.stringify({ tool: "read" }) },
    { sequence_no: 2, agent_run_id: "run", event_kind: "chat_message", marker: "point", body: JSON.stringify({ role: "assistant", text: "between", source: "driven" }) },
  ]);
  const originalKey = state.timeline[0].key;
  reduceEvents(state, [
    { sequence_no: 3, agent_run_id: "run", event_kind: "tool_call", marker: "close_ok", correlation_id: "tool", summary: "done" },
  ]);
  assert.equal(state.timeline.length, 2);
  assert.equal(state.timeline[0].key, originalKey);
  assert.equal(state.timeline[0].kind, "tool");
  if (state.timeline[0].kind === "tool") assert.equal(state.timeline[0].card.status, "ok");
});

test("process cards merge bounded argv, policy decision, exit, and truncation metadata inline", () => {
  const state = emptyState();
  reduceEvents(state, [{
    sequence_no: 1,
    agent_run_id: "run",
    event_kind: "tool_call",
    marker: "open",
    correlation_id: "process",
    body: JSON.stringify({
      name: "kaizen_run_process",
      executable: "tool.exe",
      argv: ["--check", "space value"],
      cwd: "workspace/subdir",
      timeout_ms: 45000,
      decision: "approved",
    }),
  }]);
  reduceEvents(state, [{
    sequence_no: 2,
    agent_run_id: "run",
    event_kind: "tool_call",
    marker: "close_ok",
    correlation_id: "process",
    body: JSON.stringify({ result: { exit_code: 0, truncated: true, effects_unknown: true, stdout_sha256: "a".repeat(64), stdout_bytes: 14 } }),
  }]);
  const card = state.cards.get(scopedCorrelation("run", "process"));
  assert.deepEqual(card?.process, {
    executable: "tool.exe",
    argv: ["--check", "space value"],
    cwd: "workspace/subdir",
    timeoutMs: 45000,
    decision: "approved",
    exitCode: 0,
    truncated: true,
    stdoutSha256: "a".repeat(64),
    stdoutBytes: 14,
    effectsUnknown: true,
  });
  assert.equal(state.timeline.length, 1);
  assert.equal(state.timeline[0].key, "run:1");
});

test("process-card sanitizer drops unsafe executable, argv, digest, and integer fields", () => {
  const unsafe = emptyState();
  reduceEvents(unsafe, [{
    sequence_no: 1, agent_run_id: "run", event_kind: "tool_call", marker: "open", correlation_id: "unsafe",
    body: JSON.stringify({ name: "kaizen_run_process", executable: "bad\0.exe" }),
  }]);
  assert.equal(unsafe.cards.get(scopedCorrelation("run", "unsafe"))?.process, undefined);

  const bounded = emptyState();
  reduceEvents(bounded, [{
    sequence_no: 1, agent_run_id: "run", event_kind: "tool_call", marker: "open", correlation_id: "bounded",
    body: JSON.stringify({
      name: "kaizen_run_process", executable: "tool.exe", argv: Array.from({ length: 257 }, () => "x"),
      timeout_ms: 0, exit_code: 3_000_000_000, stdout_sha256: "zz", stdout_bytes: -1,
    }),
  }]);
  assert.deepEqual(bounded.cards.get(scopedCorrelation("run", "bounded"))?.process, {
    executable: "tool.exe", argv: [], cwd: ".", timeoutMs: 120000, decision: "pending", effectsUnknown: true,
  });
});

test("runner failures are errors while coded policy denials are blocked", () => {
  const plain = emptyState();
  reduceEvents(plain, [
    { sequence_no: 1, agent_run_id: "r", event_kind: "tool_call", marker: "open", correlation_id: "c", body: JSON.stringify({ tool: "write" }) },
  ]);
  const errorOps = reduceEvents(plain, [
    { sequence_no: 2, agent_run_id: "r", event_kind: "tool_call", marker: "close_fail", correlation_id: "c", code: "TOOL_ERROR", summary: "runner error" },
  ]);
  assert.equal((errorOps[0] as { card: ToolCard }).card.status, "error");
  assert.equal(errorOps.some((op) => op.kind === "blockedTool"), false);

  const canceledOps = reduceEvents(plain, [
    { sequence_no: 3, agent_run_id: "r", event_kind: "tool_call", marker: "close_canceled", correlation_id: "c" },
  ]);
  assert.equal((canceledOps[0] as { card: ToolCard }).card.status, "error");
  assert.equal(canceledOps.some((op) => op.kind === "blockedTool"), false);

  const noCode = emptyState();
  reduceEvents(noCode, [
    { sequence_no: 1, agent_run_id: "r", event_kind: "tool_call", marker: "open", correlation_id: "c", body: JSON.stringify({ tool: "write" }) },
  ]);
  const noCodeOps = reduceEvents(noCode, [
    { sequence_no: 2, agent_run_id: "r", event_kind: "tool_call", marker: "close_fail", correlation_id: "c" },
  ]);
  assert.equal((noCodeOps[0] as { card: ToolCard }).card.status, "error");
  assert.equal(noCodeOps.some((op) => op.kind === "blockedTool"), false);

  const blocked = emptyState();
  reduceEvents(blocked, [
    { sequence_no: 1, agent_run_id: "r", event_kind: "tool_call", marker: "open", correlation_id: "c", body: JSON.stringify({ tool: "git_push" }) },
  ]);
  const blockedOps = reduceEvents(blocked, [
    { sequence_no: 2, agent_run_id: "r", event_kind: "tool_call", marker: "close_fail", correlation_id: "c", code: "INV_NO_GIT_PUSH", summary: "denied" },
  ]);
  assert.equal((blockedOps[0] as { card: ToolCard }).card.status, "blocked");
  assert.deepEqual(blockedOps.find((op) => op.kind === "blockedTool"), {
    kind: "blockedTool", correlationId: "c", summary: "denied", code: "INV_NO_GIT_PUSH",
  });
});

test("approval resolution remains inline and mutates the open card in place", () => {
  const state = emptyState();
  reduceEvents(state, [
    { sequence_no: 4, agent_run_id: "run", event_kind: "approval", marker: "open", correlation_id: "approval", summary: "review write" },
    { sequence_no: 5, agent_run_id: "run", event_kind: "chat_message", marker: "point", body: JSON.stringify({ role: "assistant", text: "waiting", source: "driven" }) },
  ]);
  const openKey = state.timeline[0].key;
  const ops = reduceEvents(state, [
    { sequence_no: 6, agent_run_id: "run", event_kind: "approval", marker: "resolved", correlation_id: "approval", summary: "approved", body: JSON.stringify({ decision: "approved" }) },
  ]);
  assert.equal(ops[0].kind, "approvalUpdate");
  assert.equal(state.timeline.length, 2);
  assert.equal(state.timeline[0].key, openKey);
  assert.equal(state.timeline[0].kind, "approval");
  if (state.timeline[0].kind === "approval") assert.equal(state.timeline[0].approval.status, "approved");
});

test("requestUserInput approval questions are sanitized onto the approval card", () => {
  const state = emptyState();
  const ops = reduceEvents(state, [{
    sequence_no: 1,
    agent_run_id: "run",
    event_kind: "approval",
    marker: "open",
    correlation_id: "input-1",
    summary: "Answer required",
    body: JSON.stringify({
      request_type: "requestUserInput",
      questions: [
        { id: "reason", header: "Decision", question: "Why continue?", options: [], ignored: "drop" },
        { id: "mode", question: "Choose mode", options: [{ label: "Safe", description: "Use checks", ignored: true }] },
      ],
      secret: "drop",
    }),
  }]);
  const approval = state.approvals.get(scopedCorrelation("run", "input-1"));
  assert.deepEqual(approval?.questions, [
    { id: "reason", header: "Decision", question: "Why continue?", options: [] },
    { id: "mode", question: "Choose mode", options: [{ label: "Safe", description: "Use checks" }] },
  ]);
  assert.deepEqual(ops, [{ kind: "approvalRequest", approval }]);
});

test("declined and timed-out approvals remain terminal cards", () => {
  for (const [marker, expected] of [["declined", "denied"], ["timed_out", "timed_out"]] as const) {
    const state = emptyState();
    reduceEvents(state, [
      { sequence_no: 1, agent_run_id: "run", event_kind: "approval", marker: "open", correlation_id: "a", summary: "ask" },
      { sequence_no: 2, agent_run_id: "run", event_kind: "approval", marker, correlation_id: "a", summary: marker, code: marker === "timed_out" ? "DENIED_APPROVAL_TIMEOUT" : "DENIED" },
    ]);
    assert.equal(state.approvals.get(scopedCorrelation("run", "a"))?.status, expected);
    assert.equal(state.timeline.length, 1);
  }
});

test("messages, tools, and approvals retain durable first-event order", () => {
  const state = emptyState();
  reduceEvents(state, [
    { sequence_no: 1, agent_run_id: "run", event_kind: "chat_message", marker: "point", body: JSON.stringify({ role: "user", text: "one", source: "driven" }) },
    { sequence_no: 2, agent_run_id: "run", event_kind: "tool_call", marker: "open", correlation_id: "tool", body: JSON.stringify({ tool: "read" }) },
    { sequence_no: 3, agent_run_id: "run", event_kind: "approval", marker: "open", correlation_id: "approval", summary: "approve" },
    { sequence_no: 4, agent_run_id: "run", event_kind: "chat_message", marker: "point", body: JSON.stringify({ role: "assistant", text: "four", source: "driven" }) },
  ]);
  assert.deepEqual(state.timeline.map((item) => [item.key, item.kind]), [
    ["run:1", "message"],
    ["run:2", "tool"],
    ["run:3", "approval"],
    ["run:4", "message"],
  ]);
});

test("timeline index stays aligned across appends and inline replacements", () => {
  const state = emptyState();
  reduceEvents(state, [
    { sequence_no: 1, agent_run_id: "run", event_kind: "chat_message", marker: "point", body: JSON.stringify({ role: "user", text: "one", source: "driven" }) },
    { sequence_no: 2, agent_run_id: "run", event_kind: "tool_call", marker: "open", correlation_id: "tool", body: JSON.stringify({ tool: "read" }) },
    { sequence_no: 3, agent_run_id: "run", event_kind: "approval", marker: "open", correlation_id: "approval", summary: "approve" },
  ]);
  assert.deepEqual([...state.timelineIndex], [["run:1", 0], ["run:2", 1], ["run:3", 2]]);
  reduceEvents(state, [
    { sequence_no: 4, agent_run_id: "run", event_kind: "tool_call", marker: "close_ok", correlation_id: "tool", summary: "done" },
    { sequence_no: 5, agent_run_id: "run", event_kind: "approval", marker: "resolved", correlation_id: "approval", body: JSON.stringify({ decision: "approved" }) },
  ]);
  assert.deepEqual([...state.timelineIndex], [["run:1", 0], ["run:2", 1], ["run:3", 2]]);
  assert.equal(state.timeline[1].kind, "tool");
  assert.equal(state.timeline[2].kind, "approval");
});

test("duplicate replay on the same reducer is idempotent", () => {
  const state = emptyState();
  const batch: WireEvent[] = [
    { sequence_no: 1, agent_run_id: "run", event_kind: "tool_call", marker: "open", correlation_id: "tc", body: JSON.stringify({ tool: "read" }) },
    { sequence_no: 2, agent_run_id: "run", event_kind: "tool_call", marker: "close_ok", correlation_id: "tc", summary: "done" },
  ];
  assert.equal(reduceEvents(state, batch).length, 2);
  assert.deepEqual(reduceEvents(state, batch), []);
  assert.equal(state.timeline.length, 1);
  assert.equal(state.cards.get(scopedCorrelation("run", "tc"))?.status, "ok");
});

test("fresh replay produces the same final timeline", () => {
  const batch: WireEvent[] = [
    { sequence_no: 1, agent_run_id: "run", event_kind: "tool_call", marker: "open", correlation_id: "tc", body: JSON.stringify({ tool: "read" }) },
    { sequence_no: 2, agent_run_id: "run", event_kind: "tool_call", marker: "close_ok", correlation_id: "tc", summary: "done" },
    { sequence_no: 3, agent_run_id: "run", event_kind: "turn", marker: "close_ok", body: JSON.stringify({ status: "completed" }) },
  ];
  const first = emptyState();
  const second = emptyState();
  reduceEvents(first, batch);
  reduceEvents(second, batch);
  assert.deepEqual(second.timeline, first.timeline);
  assert.equal(second.turnState, "idle");
});

test("turn close emits authoritative normal/abnormal terminal states", () => {
  const normal = reduceEvents(emptyState(), [
    ev({ event_kind: "turn", marker: "close_ok", body: JSON.stringify({ status: "completed" }) }),
  ]);
  assert.deepEqual(normal, [{ kind: "turnDone", terminalState: "completed" }]);
  const abnormal = reduceEvents(emptyState(), [
    ev({ event_kind: "turn", marker: "close_fail", code: "MAX_TURNS_EXHAUSTED", body: JSON.stringify({ status: "max_turns" }) }),
  ]);
  assert.deepEqual(abnormal, [{ kind: "turnDone", terminalState: "max_turns" }]);
  assert.deepEqual(reduceEvents(emptyState(), [
    ev({ event_kind: "turn", marker: "close_canceled" }),
  ]), [{ kind: "turnDone", terminalState: "canceled" }]);
  assert.deepEqual(reduceEvents(emptyState(), [
    ev({ event_kind: "turn", marker: "close_fail", code: "BACKEND_ERROR" }),
  ]), [{ kind: "turnDone", terminalState: "BACKEND_ERROR" }]);
});

test("subagent cards share the same ordered, run-scoped mutation model", () => {
  const state = emptyState();
  reduceEvents(state, [
    { sequence_no: 1, agent_run_id: "run", event_kind: "subagent", marker: "open", correlation_id: "sub", summary: "started" },
    { sequence_no: 2, agent_run_id: "run", event_kind: "subagent", marker: "close_ok", correlation_id: "sub", summary: "done" },
  ]);
  assert.equal(state.timeline.length, 1);
  assert.equal(state.cards.get(scopedCorrelation("run", "sub"))?.status, "ok");
});

test("malformed bodies and unrelated point kinds never break the fold", () => {
  const state = emptyState();
  const ops = reduceEvents(state, [
    ev({ event_kind: "tool_call", marker: "open", correlation_id: "tc", summary: "tool call grep", body: "{not json" }),
    ev({ event_kind: "transport", marker: "point", summary: "reconnect" }),
  ]);
  assert.equal((ops[0] as { card: ToolCard }).card.tool, "grep");
  assert.equal(ops.length, 1);
});

test("chat messages accept user/assistant/system while preserving source text", () => {
  const state = emptyState();
  reduceEvents(state, [
    { sequence_no: 1, agent_run_id: "run", event_kind: "chat_message", marker: "point", body: JSON.stringify({ role: "user", text: "<b>plain</b>", source: "driven" }) },
    { sequence_no: 2, agent_run_id: "run", event_kind: "chat_message", marker: "point", body: JSON.stringify({ role: "assistant", text: "**markdown**", source: "driven" }) },
    { sequence_no: 3, agent_run_id: "run", event_kind: "chat_message", marker: "point", body: JSON.stringify({ role: "system", text: "notice", source: "observed" }) },
  ]);
  assert.deepEqual(state.transcript.map(({ role, text }) => ({ role, text })), [
    { role: "user", text: "<b>plain</b>" },
    { role: "assistant", text: "**markdown**" },
    { role: "system", text: "notice" },
  ]);
});

test("legacy finalization body is used only when no canonical chat message exists", () => {
  const legacy = emptyState();
  const ops = reduceEvents(legacy, [
    { sequence_no: 1, agent_run_id: "run", event_kind: "finalization", marker: "close_ok", body: "answer" },
  ]);
  assert.deepEqual(ops.map((op) => op.kind), ["assistantText", "runDone"]);
  assert.deepEqual(ops.at(-1), { kind: "runDone", terminalState: "success" });
  assert.equal(legacy.timeline[0].key, "run:1");

  const canonical = emptyState();
  reduceEvents(canonical, [
    { sequence_no: 1, agent_run_id: "run", event_kind: "chat_message", marker: "point", body: JSON.stringify({ role: "assistant", text: "answer", source: "driven" }) },
    { sequence_no: 2, agent_run_id: "run", event_kind: "finalization", marker: "close_ok", body: "answer" },
  ]);
  assert.equal(canonical.transcript.length, 1);

  const explicit = reduceEvents(emptyState(), [
    { sequence_no: 1, agent_run_id: "run", event_kind: "finalization", marker: "close_fail", body: JSON.stringify({ terminal_state: "custom" }) },
  ]);
  assert.deepEqual(explicit, [{ kind: "runDone", terminalState: "custom" }]);

  const blank = emptyState();
  assert.deepEqual(reduceEvents(blank, [
    { sequence_no: 1, event_kind: "finalization", marker: "close_ok", body: "   " },
  ]), [{ kind: "runDone", terminalState: "success" }]);
  assert.equal(blank.timeline.length, 0);
});

test("missing or blank run ids use the legacy timeline namespace", () => {
  const state = emptyState();
  reduceEvents(state, [
    { sequence_no: 1, event_kind: "chat_message", marker: "point", body: JSON.stringify({ role: "user", text: "one", source: "driven" }) },
    { sequence_no: 2, agent_run_id: " ", event_kind: "chat_message", marker: "point", body: JSON.stringify({ role: "assistant", text: "two", source: "driven" }) },
  ]);
  assert.deepEqual(state.timeline.map((item) => item.key), ["legacy:1", "legacy:2"]);
});
