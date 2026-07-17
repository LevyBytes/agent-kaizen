/**
 * SessionClient tests (node:test, no vscode). Two layers:
 *   REAL TRANSPORT (fakeDaemon over the true pipe/UDS): session/start binds the run then the long-poll
 *     pump streams scripted session/events batches, advances the gapless cursor, stops on terminal, and
 *     approve() sends the exact {correlation_id, session_id, decision} wire shape (asserted off the
 *     daemon's request recorder).
 *   INJECTED RequestFn (deterministic, no sockets): DaemonUnreachable -> backoff+resume from the same
 *     cursor (no skips/dupes), and dispose() stops polling.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { DaemonUnreachable, LoopbackResponse } from "../../extension/src/protocol";
import { RequestFn, SessionClient, SessionResponseError, normalizeEngineCapability, normalizeFeatures } from "../../extension/src/sessionClient";
import type { WireEvent } from "../../extension/src/webviewProtocol";
import { fakeRepoRoot, listenPathFor, startFakeDaemon } from "./fakeDaemon";

const startRequest = (engine = "local_llm", prompt = "hi") => ({
  engine,
  prompt,
  profile: {
    permission_mode: "plan" as const,
    auth_mode: engine === "local_llm" ? ("none" as const) : ("subscription" as const),
  },
});

/** Flush one macrotask turn so tests can prove a deferred pump has not started. */
const tick = (): Promise<void> => new Promise((r) => setImmediate(r));
/** Await a predicate with a hard cap so a stuck pump fails the test instead of hanging. */
async function until(pred: () => boolean, label: string, capMs = 3000): Promise<void> {
  const start = Date.now();
  while (!pred()) {
    if (Date.now() - start > capMs) throw new Error(`until() timed out waiting for ${label}`);
    await new Promise((r) => setTimeout(r, 5));
  }
}

function evt(sequence_no: number, kind: string, marker: string): WireEvent {
  return { sequence_no, event_kind: kind, marker, summary: `${kind}/${marker}` };
}

test("start binds the run, then the pump streams a batch and STOPS on terminal", async () => {
  const root = fakeRepoRoot();
  // Two session/events responses: a live batch (terminal:false), then the terminal close.
  let poll = 0;
  const daemon = await startFakeDaemon(listenPathFor(root), {
    "session/start": { status: "OK", session_id: "s1", agent_run_id: "run1" },
    "session/events": (args) => {
      poll += 1;
      if (poll === 1) {
        assert.equal(args.since, 0); // first poll replays from 0
        return { status: "OK", events: [evt(1, "turn", "open"), evt(2, "tool_call", "open")], cursor: 2, terminal: false };
      }
      if (poll === 2) {
        assert.equal(args.since, 2); // cursor advanced to 2
        return { status: "OK", events: [evt(3, "turn", "close_ok")], cursor: 3, terminal: true, terminal_state: "completed" };
      }
      assert.equal(args.since, 3); // terminal drain check after the last delivered event
      return { status: "OK", events: [], cursor: 3, terminal: true, terminal_state: "completed" };
    },
  });
  const seen: WireEvent[] = [];
  let terminalState: string | undefined;
  let sawTerminal = false;
  const client = new SessionClient(root, {
    onEvents: (events) => seen.push(...events),
    onTerminal: (ts) => { terminalState = ts; sawTerminal = true; },
  });
  try {
    const res = await client.start(startRequest());
    assert.equal(res.status, "OK");
    assert.equal(client.runId, "run1");
    await until(() => sawTerminal, "terminal callback");
    assert.deepEqual(seen.map((e) => e.sequence_no), [1, 2, 3]);
    assert.equal(terminalState, "completed");
    assert.equal(poll, 3, "pump should stop immediately after the terminal drain poll");
  } finally {
    client.dispose();
    daemon.close();
  }
});

test("a denied engine RESOLVES structurally and never starts the pump", async () => {
  const root = fakeRepoRoot();
  let polled = false;
  const daemon = await startFakeDaemon(listenPathFor(root), {
    "session/start": { status: "DENIED", code: "DENIED_ENGINE_NOT_WIRED" },
    "session/events": () => {
      polled = true;
      return { status: "OK", events: [], cursor: 0, terminal: true };
    },
  });
  const client = new SessionClient(root, {});
  try {
    const res = await client.start(startRequest("codex", "x"));
    assert.equal(res.code, "DENIED_ENGINE_NOT_WIRED");
    assert.equal(client.runId, null);
    await tick();
    assert.equal(polled, false); // no run id -> the pump never polled
  } finally {
    client.dispose();
    daemon.close();
  }
});

test("approve() sends the exact {correlation_id, session_id, decision} wire shape", async () => {
  const root = fakeRepoRoot();
  const daemon = await startFakeDaemon(listenPathFor(root), {
    "session/start": { status: "OK", session_id: "s1", agent_run_id: "run1" },
    "session/events": { status: "OK", events: [], cursor: 0, terminal: true }, // end immediately
    approve: { status: "OK", state: "approved" },
  });
  const client = new SessionClient(root, {});
  try {
    await client.start(startRequest());
    await client.approve("corr-hash-9", "approve");
    const approveReq = daemon.requests.find((r) => r.op === "approve");
    assert.ok(approveReq, "approve reached the daemon");
    assert.deepEqual(approveReq!.args, { correlation_id: "corr-hash-9", session_id: "s1", decision: "approve" });
  } finally {
    client.dispose();
    daemon.close();
  }
});

test("steer/interrupt/kill carry agent_run_id; kill halts the pump locally", async () => {
  const root = fakeRepoRoot();
  const daemon = await startFakeDaemon(listenPathFor(root), {
    "session/start": { status: "OK", session_id: "s1", agent_run_id: "run1" },
    "session/events": { status: "OK", events: [], cursor: 0, terminal: false }, // would poll forever
    "session/steer": { status: "OK", queued: true },
    "session/interrupt": { status: "OK", interrupted: true },
    "session/kill": { status: "OK", killed: true },
  });
  const client = new SessionClient(root, {});
  try {
    await client.start(startRequest());
    await client.steer("go left");
    await client.interrupt();
    const killResp = await client.kill();
    assert.equal(killResp.status, "OK");
    for (const op of ["session/steer", "session/interrupt", "session/kill"]) {
      const r = daemon.requests.find((x) => x.op === op);
      assert.ok(r, `${op} reached the daemon`);
      assert.equal(r.args.agent_run_id, "run1", `${op} carries agent_run_id`);
    }
  } finally {
    client.dispose();
    daemon.close();
  }
});

test("capabilities, turn, and close use the frozen H2 wire without opening a second client", async () => {
  const calls: Array<{ op: string; args: Record<string, unknown>; timeoutMs?: number }> = [];
  const req: RequestFn = (op, args, timeoutMs) => {
    calls.push({ op, args, timeoutMs });
    if (op === "session/capabilities") return Promise.resolve({ status: "OK", engines: [] });
    if (op === "session/start") return Promise.resolve({ status: "OK", session_id: "s1", agent_run_id: "run1" });
    if (op === "session/events") return Promise.resolve({ status: "OK", events: [], cursor: 0, terminal: true, terminal_state: "success" });
    return Promise.resolve({ status: "OK" });
  };
  const client = new SessionClient(req, {});
  await client.capabilities(false);
  await client.capabilities(true);
  await client.start(startRequest());
  await client.turn("second");
  await client.close();
  assert.deepEqual(calls.filter((call) => call.op === "session/capabilities"), [
    { op: "session/capabilities", args: { refresh: false }, timeoutMs: 10_000 },
    { op: "session/capabilities", args: { refresh: true }, timeoutMs: 210_000 },
  ]);
  assert.deepEqual(calls.find((call) => call.op === "session/turn")?.args, { agent_run_id: "run1", prompt: "second" });
  assert.deepEqual(calls.find((call) => call.op === "session/close")?.args, { agent_run_id: "run1" });
});

test("old or malformed feature maps fail closed", () => {
  const values = [undefined, null, [], { streaming: 1, image_attachments: "true", governed_context: true }];
  const dark = {
    streaming: false, image_attachments: false, governed_context: false, diff_snapshots: false,
    writer_leasing: false, subscription_auth: false, controlled_tools: false,
    process_execution: false, test_extension: false,
  };
  assert.deepEqual(values.map(normalizeFeatures), [
    dark,
    dark,
    dark,
    { ...dark, governed_context: true },
  ]);
});

test("Claude runtime, dynamic models, and workload bounds normalize fail closed", () => {
  const [capability] = normalizeEngineCapability({
    id: "claude", label: "Claude", drivable: true, availability: { state: "available", raw_error: "secret" },
    models: [
      { id: "account-model-new", label: "New", description: "Dynamic", reasoning_efforts: ["low", "high"], supports_adaptive_thinking: true },
      { id: "bad", label: "Bad", reasoning_efforts: "high" },
    ],
    default_model: "removed-account-model",
    auth_modes: ["subscription", "unknown"], permission_modes: ["plan", "ask"], warnings: [],
    runtime: { kind: "claude-agent-sdk", version: "0.3.207", status: "ready", account: "must-not-cross" },
    max_turns: { min: 1, default: 8, max: 32 },
    features: { subscription_auth: true, controlled_tools: true, process_execution: true },
  });
  assert.equal(capability.models[0].id, "account-model-new");
  assert.equal(capability.models.length, 2);
  assert.deepEqual(capability.models[1].reasoning_efforts, []);
  assert.equal(capability.default_model, undefined, "a disappeared default never becomes an implicit selection");
  assert.deepEqual(capability.auth_modes, ["subscription"]);
  assert.deepEqual(capability.runtime, { kind: "claude-agent-sdk", version: "0.3.207", status: "ready" });
  assert.deepEqual(capability.max_turns, { min: 1, default: 8, max: 32 });
  assert.equal(capability.features.subscription_auth, true);
  assert.equal("account" in (capability.runtime as unknown as Record<string, unknown>), false);
  assert.equal(normalizeEngineCapability({ ...capability, max_turns: { min: 0, default: 8, max: 99 } })[0].max_turns, undefined);
});

test("malformed capability entries are isolated without breaking legacy descriptors", async () => {
  const client = new SessionClient(() => Promise.resolve({
    status: "OK",
    engines: [
      null,
      {
        id: "local_llm",
        label: "Local LLM",
        drivable: true,
        availability: { state: "available" },
        models: [],
        auth_modes: ["none"],
        permission_modes: ["plan"],
        warnings: [],
      },
    ],
  }) as Promise<LoopbackResponse>);
  const result = await client.capabilities();
  assert.equal(result.engines?.length, 1);
  assert.deepEqual(result.engines?.[0]?.features, {
    streaming: false,
    image_attachments: false,
    governed_context: false,
    diff_snapshots: false,
    writer_leasing: false,
    subscription_auth: false,
    controlled_tools: false,
    process_execution: false,
    test_extension: false,
  });
});

test("start and turn preserve negotiated attachment/context envelopes", async () => {
  const calls: Array<{ op: string; args: Record<string, unknown> }> = [];
  const client = new SessionClient((op, args) => {
    calls.push({ op, args });
    if (op === "session/start") return Promise.resolve({ status: "OK", session_id: "s1", agent_run_id: "run1" });
    if (op === "session/events") return Promise.resolve({ status: "OK", events: [], cursor: 0, terminal: true });
    return Promise.resolve({ status: "OK" });
  });
  const image = {
    id: "img-1",
    kind: "image" as const,
    artifact_ref: "sha256/aa",
    sha256: "a".repeat(64),
    bytes: 3,
    media_type: "image/png" as const,
  };
  const context = { id: "ctx-1", kind: "file" as const, source_path: "README.md" };
  await client.start({ ...startRequest(), title: "A title", attachments: [image], context_refs: [context] });
  await client.turn("next", { attachments: [image], context_refs: [context] });
  assert.deepEqual(calls.find(({ op }) => op === "session/start")?.args, {
    ...startRequest(),
    title: "A title",
    attachments: [image],
    context_refs: [context],
  });
  assert.deepEqual(calls.find(({ op }) => op === "session/turn")?.args, {
    agent_run_id: "run1",
    prompt: "next",
    attachments: [image],
    context_refs: [context],
  });
  client.dispose();
});

test("approval confirmation and explicit answers are approve-only while legacy calls remain unchanged", async () => {
  const calls: Array<{ op: string; args: Record<string, unknown> }> = [];
  const client = new SessionClient((op, args) => {
    calls.push({ op, args });
    return Promise.resolve({ status: "OK" });
  });
  await client.approve("c1", "approve", "s1");
  await client.approve("c2", "approve", "s1", {
    expected_revision: 2,
    snapshot_set_sha256: "b".repeat(64),
    metadata_confirmed: true,
  });
  await client.approve("c3", "deny", "s1", {
    expected_revision: 3,
    snapshot_set_sha256: "c".repeat(64),
    metadata_confirmed: true,
  });
  await client.approve("c4", "approve", "s1", undefined, { reason: "exact answer" });
  await client.approve("c5", "deny", "s1", undefined, { reason: "must not cross the wire" });
  assert.deepEqual(calls.map(({ args }) => args), [
    { correlation_id: "c1", session_id: "s1", decision: "approve" },
    { correlation_id: "c2", session_id: "s1", decision: "approve", expected_revision: 2, snapshot_set_sha256: "b".repeat(64), metadata_confirmed: true },
    { correlation_id: "c3", session_id: "s1", decision: "deny" },
    { correlation_id: "c4", session_id: "s1", decision: "approve", answers: { reason: "exact answer" } },
    { correlation_id: "c5", session_id: "s1", decision: "deny" },
  ]);
});

test("events delta cursor and old-daemon list metadata stay additive", async () => {
  const calls: Array<{ op: string; args: Record<string, unknown> }> = [];
  const client = new SessionClient((op, args) => {
    calls.push({ op, args });
    if (op === "session/list") {
      return Promise.resolve({ status: "OK", sessions: [{ session_id: "legacy" }] });
    }
    return Promise.resolve({ status: "OK", events: [], deltas: [], delta_cursor: 7, delta_dropped: false });
  });
  const listed = await client.list();
  const events = await client.eventsOnce("run-1", 2, 10, 6);
  assert.deepEqual(listed.sessions, [{ session_id: "legacy", title: null, snippet: null }]);
  assert.equal(events.delta_cursor, 7);
  assert.deepEqual(calls.find(({ op }) => op === "session/events")?.args, {
    agent_run_id: "run-1",
    since: 2,
    wait: 0,
    limit: 10,
    delta_since: 6,
  });
});

test("the streaming pump filters deltas and advances a monotone delta cursor", async () => {
  let polls = 0;
  let done = false;
  const requests: Array<Record<string, unknown>> = [];
  const seen: Array<{ deltas: unknown[]; cursor: number; dropped: boolean }> = [];
  const client = new SessionClient(
    (op, args) => {
      if (op === "session/start") return Promise.resolve({ status: "OK", session_id: "s1", agent_run_id: "run1" });
      requests.push(args);
      polls += 1;
      if (polls === 1) {
        return Promise.resolve({
          status: "OK",
          events: [evt(1, "turn", "open")],
          cursor: 1,
          terminal: false,
          deltas: [
            { seq: 1, turn_id: "turn-1", text: "one" },
            { seq: 0, turn_id: "bad", text: "drop" },
            { seq: 2, turn_id: 7, text: "drop" },
          ],
          delta_cursor: 2,
          delta_dropped: true,
        });
      }
      return Promise.resolve({
        status: "OK",
        events: [],
        cursor: 1,
        terminal: true,
        terminal_state: "success",
        deltas: [{ seq: 3, turn_id: "turn-1", text: "three" }],
        delta_cursor: 1,
      });
    },
    {
      onDeltas: (deltas, cursor, dropped) => seen.push({ deltas, cursor, dropped }),
      onTerminal: () => { done = true; },
    },
  );
  client.setStreaming(true);
  await client.start(startRequest());
  await until(() => done, "streaming terminal drain");
  assert.deepEqual(requests.map((args) => args.delta_since), [0, 2]);
  assert.deepEqual(seen, [
    { deltas: [{ seq: 1, turn_id: "turn-1", text: "one" }], cursor: 2, dropped: true },
    { deltas: [{ seq: 3, turn_id: "turn-1", text: "three" }], cursor: 2, dropped: false },
  ]);
  client.dispose();
});

test("turn controls fail locally before a run is bound", async () => {
  const client = new SessionClient(() => Promise.reject(new Error("must not reach transport")), {});
  for (const response of [
    await client.turn("x"),
    await client.steer("x"),
    await client.interrupt(),
    await client.close(),
  ]) {
    assert.deepEqual(response, { status: "ERROR", code: "NO_ACTIVE_RUN" });
  }
  client.dispose();
});

test("the event pump remains attached while a conversation is idle and stops only at run terminal", async () => {
  let polls = 0;
  const states: Array<[string, boolean]> = [];
  let done = false;
  const client = new SessionClient(
    (op) => {
      if (op === "session/start") return Promise.resolve({ status: "OK", session_id: "s1", agent_run_id: "run1" });
      polls += 1;
      if (polls === 1) {
        return Promise.resolve({
          status: "OK",
          events: [evt(1, "turn", "close_ok")],
          cursor: 1,
          terminal: false,
          turn_state: "idle",
        });
      }
      return Promise.resolve({
        status: "OK",
        events: [evt(2, "finalization", "close_ok")],
        cursor: 2,
        terminal: true,
        terminal_state: "success",
        turn_state: "idle",
      });
    },
    {
      onTurnState: (state, terminal) => states.push([state, terminal]),
      onTerminal: () => {
        done = true;
      },
    },
  );
  await client.start(startRequest());
  await until(() => done, "idle conversation terminal drain");
  assert.equal(polls, 3);
  assert.deepEqual(states, [["idle", false], ["idle", true], ["idle", true]]);
});

test("a structured non-OK events response surfaces once and stops instead of tight-looping", async () => {
  let polls = 0;
  const errors: Error[] = [];
  const client = new SessionClient(
    (op) => {
      if (op === "session/start") return Promise.resolve({ status: "OK", session_id: "s1", agent_run_id: "missing" });
      polls += 1;
      return Promise.resolve({ status: "DENIED", code: "DENIED_AGENT_RUN_NOT_DRIVEN" });
    },
    { onError: (error) => errors.push(error) },
  );
  await client.start(startRequest());
  await until(() => errors.length === 1, "structured pump error");
  await tick();
  assert.equal(polls, 1);
  assert.ok(errors[0] instanceof SessionResponseError);
  assert.equal((errors[0] as SessionResponseError).code, "DENIED_AGENT_RUN_NOT_DRIVEN");
  client.dispose();
});

test("start returns identifiers before a fast event-pump callback can fire", async () => {
  let ownerBound = false;
  let callbackBeforeBind = false;
  let errorSeen = false;
  const client = new SessionClient(
    (op) => op === "session/start"
      ? Promise.resolve({ status: "OK", session_id: "s1", agent_run_id: "run1" })
      : Promise.resolve({ status: "DENIED", code: "DENIED_AGENT_RUN_NOT_DRIVEN" }),
    {
      onError: () => {
        callbackBeforeBind = !ownerBound;
        errorSeen = true;
      },
    },
  );
  const response = await client.start(startRequest());
  assert.equal(response.agent_run_id, "run1");
  ownerBound = true;
  await until(() => errorSeen, "fast pump error callback");
  assert.equal(callbackBeforeBind, false);
});

test("terminal replay drains every default-size page before stopping", async () => {
  const all = Array.from({ length: 601 }, (_, index) => evt(index + 1, "chat_message", "point"));
  let polls = 0;
  const seen: number[] = [];
  let done = false;
  const client = new SessionClient(
    (op, args) => {
      if (op === "session/start") return Promise.resolve({ status: "OK", session_id: "s1", agent_run_id: "run1" });
      polls += 1;
      const since = Number(args.since ?? 0);
      const page = all.filter((event) => event.sequence_no > since).slice(0, 500);
      const cursor = page.at(-1)?.sequence_no ?? since;
      return Promise.resolve({
        status: "OK",
        events: page,
        cursor,
        terminal: true,
        terminal_state: "success",
        turn_state: "terminal",
      });
    },
    {
      onEvents: (events) => seen.push(...events.map((event) => event.sequence_no)),
      onTerminal: () => {
        done = true;
      },
    },
  );
  await client.start(startRequest());
  await until(() => done, "terminal pagination drain");
  assert.deepEqual(seen, all.map((event) => event.sequence_no));
  assert.equal(polls, 3);
});

test("rebinding starts a new pump generation while the prior long-poll is still pending", async () => {
  const requests: Array<[string, number]> = [];
  const seen: number[] = [];
  let releaseOld!: (response: LoopbackResponse) => void;
  const oldResponse = new Promise<LoopbackResponse>((resolve) => { releaseOld = resolve; });
  let run2Polls = 0;
  let run2Done = false;
  const client = new SessionClient(
    (op, args) => {
      if (op === "session/start") return Promise.resolve({ status: "OK", session_id: "s1", agent_run_id: "run1" });
      const runId = String(args.agent_run_id);
      requests.push([runId, Number(args.since)]);
      if (runId === "run1") return oldResponse;
      run2Polls += 1;
      return Promise.resolve(run2Polls === 1
        ? { status: "OK", events: [evt(1, "chat_message", "point")], cursor: 1, terminal: true }
        : { status: "OK", events: [], cursor: 1, terminal: true, terminal_state: "success" });
    },
    {
      onEvents: (events) => seen.push(...events.map(({ sequence_no }) => sequence_no)),
      onTerminal: () => { run2Done = true; },
    },
  );
  await client.start(startRequest());
  await until(() => requests.some(([runId]) => runId === "run1"), "first resume generation poll");
  client.resume("run2", "s2");
  await until(() => run2Done, "rebound run terminal drain");
  releaseOld({ status: "OK", events: [evt(99, "chat_message", "point")], cursor: 99, terminal: true });
  await tick();
  assert.ok(requests.some(([runId]) => runId === "run2"), "new generation polls without waiting for the stale request");
  assert.equal(requests.find(([runId]) => runId === "run2")?.[1], 0, "resume resets the durable cursor");
  assert.deepEqual(seen, [1], "the stale generation cannot publish events into the rebound run");
  client.dispose();
  const requestCount = requests.length;
  client.resume("run3", "s3");
  await tick();
  assert.equal(requests.length, requestCount, "resume after dispose cannot restart the pump");
});

// --- deterministic pump behavior via an injected RequestFn (no sockets) ------------------------

test("DaemonUnreachable -> onError + backoff, then RESUMES from the same cursor (no skip, no dupe)", async () => {
  const errors: Error[] = [];
  let call = 0;
  const sinceSeen: unknown[] = [];
  const req: RequestFn = (op, args) => {
    if (op === "session/start") return Promise.resolve({ status: "OK", session_id: "s1", agent_run_id: "run1" });
    // session/events: batch, then a transport blip, then a batch from the SAME cursor, then terminal.
    call += 1;
    sinceSeen.push(args.since);
    if (call === 1) return Promise.resolve({ status: "OK", events: [evt(1, "turn", "open")], cursor: 1, terminal: false });
    if (call === 2) return Promise.reject(new DaemonUnreachable("blip"));
    if (call === 3) return Promise.resolve({ status: "OK", events: [evt(2, "tool_call", "open")], cursor: 2, terminal: false });
    return Promise.resolve({ status: "OK", events: [evt(3, "turn", "close_ok")], cursor: 3, terminal: true, terminal_state: "completed" });
  };
  const seen: number[] = [];
  let done = false;
  const client = new SessionClient(req, {
    onEvents: (events) => seen.push(...events.map((e) => e.sequence_no)),
    onError: (e) => errors.push(e),
    onTerminal: () => (done = true),
  });
  await client.start(startRequest());
  await until(() => done, "post-blip terminal drain");
  assert.deepEqual(seen, [1, 2, 3]); // no event lost across the blip
  assert.equal(errors.length, 1);
  assert.ok(errors[0] instanceof DaemonUnreachable);
  // The failed cursor is retried; the final poll re-serves seq=3 and client-side filtering proves terminal drain.
  assert.deepEqual(sinceSeen, [0, 1, 1, 2, 3]);
});

test("dispose() stops the pump — no further polls after it is called", async () => {
  let polls = 0;
  let disposed = false;
  const client: SessionClient = new SessionClient(
    (op) => {
      if (op === "session/start") return Promise.resolve({ status: "OK", session_id: "s1", agent_run_id: "run1" });
      polls += 1;
      if (polls >= 2) {
        client.dispose(); // dispose mid-stream after the 2nd poll
        disposed = true;
      }
      return Promise.resolve({ status: "OK", events: [], cursor: polls, terminal: false } as LoopbackResponse);
    },
    {},
  );
  await client.start(startRequest());
  await until(() => disposed, "mid-pump dispose");
  const at = polls;
  await tick();
  assert.equal(polls, at, "no polls after dispose()");
});
