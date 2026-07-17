import assert from "node:assert/strict";
import { test } from "node:test";

import {
  ConversationController,
  ControllerPrompts,
  MementoPort,
  RendererPort,
  RUN_CURSORS_KEY,
  SessionPort,
} from "../../extension/src/conversationController";
import type { ConversationSnapshot, HostToWebview, WireEvent } from "../../extension/src/webviewProtocol";
import { SessionResponseError } from "../../extension/src/sessionClient";
import type { CapabilitiesResult, EngineCapability, EngineFeatures, SessionClientCallbacks, StartRequest, StartResult, TurnEnvelope } from "../../extension/src/sessionClient";
import type { StagedContext } from "../../extension/src/contextStager";
import type { StagedImage } from "../../extension/src/imageStager";
import { canonicalSnapshotSetSha256, NegotiatedDiff } from "../../extension/src/diffModel";

class MemoryMemento implements MementoPort {
  readonly values = new Map<string, unknown>();

  get<T>(key: string, defaultValue?: T): T | undefined {
    return (this.values.has(key) ? this.values.get(key) : defaultValue) as T | undefined;
  }

  update(key: string, value: unknown): PromiseLike<void> {
    if (value === undefined) this.values.delete(key);
    else this.values.set(key, value);
    return Promise.resolve();
  }
}

class Renderer implements RendererPort {
  readonly messages: HostToWebview[] = [];

  constructor(readonly id: string) {}

  postMessage(message: HostToWebview): void {
    this.messages.push(message);
  }

  get snapshot(): ConversationSnapshot | undefined {
    const message = [...this.messages].reverse().find((entry) => entry.type === "snapshot");
    return message?.type === "snapshot" ? message.snapshot : undefined;
  }
}

function engineFeatures(overrides: Partial<EngineFeatures> = {}): EngineFeatures {
  return {
    streaming: false,
    image_attachments: false,
    governed_context: false,
    diff_snapshots: false,
    writer_leasing: false,
    subscription_auth: false,
    controlled_tools: false,
    process_execution: false,
    test_extension: false,
    ...overrides,
  };
}

type CapabilityOverrides = Partial<Omit<EngineCapability, "features">> & { features?: Partial<EngineFeatures> };

function localCapability(overrides: CapabilityOverrides = {}): EngineCapability {
  const { features, ...rest } = overrides;
  return {
    id: "local_llm",
    label: "Local LLM",
    drivable: true,
    availability: { state: "available" },
    models: [{ id: "qwen", label: "Qwen", reasoning_efforts: [] }],
    default_model: "qwen",
    auth_modes: ["none"],
    permission_modes: ["plan", "ask", "agent", "full"],
    warnings: [],
    features: engineFeatures(features),
    ...rest,
  };
}

function claudeCapability(overrides: CapabilityOverrides = {}): EngineCapability {
  const { features, ...rest } = overrides;
  return {
    id: "claude",
    label: "Claude",
    drivable: true,
    availability: { state: "available" },
    models: [
      { id: "account-model-a", label: "Account A", reasoning_efforts: ["low", "high"] },
      { id: "account-model-b", label: "Account B", reasoning_efforts: ["medium", "max"] },
    ],
    auth_modes: ["subscription"],
    permission_modes: ["plan", "ask", "agent", "full"],
    warnings: [],
    runtime: { kind: "claude-agent-sdk", version: "0.3.207", status: "ready" },
    max_turns: { min: 1, default: 8, max: 32 },
    features: engineFeatures({
      streaming: true,
      image_attachments: true,
      governed_context: true,
      diff_snapshots: true,
      writer_leasing: true,
      subscription_auth: true,
      controlled_tools: true,
      process_execution: true,
      test_extension: false,
      ...features,
    }),
    ...rest,
  };
}

/** Scriptable in-memory daemon that records controller calls and emits deterministic pump events. */
class Backend {
  callbacks: SessionClientCallbacks[] = [];
  clients = 0;
  starts: StartRequest[] = [];
  turns: string[] = [];
  turnEnvelopes: TurnEnvelope[] = [];
  streamingSettings: boolean[] = [];
  startResponse: StartResult | undefined;
  turnResponse: Record<string, unknown> = { status: "OK" };
  closes = 0;
  resumes: Array<[string, string | null | undefined]> = [];
  approvals: Array<[string, string]> = [];
  approvalRequests: Array<{ correlationId: string; decision: "approve" | "deny"; sessionId?: string; confirmation?: { expected_revision: number; snapshot_set_sha256: string; metadata_confirmed: boolean }; answers?: Record<string, string> }> = [];
  listResponse: Record<string, unknown> = { status: "OK", sessions: [] };
  listResponses = new Map<string, Record<string, unknown>>();
  events = new Map<string, WireEvent[]>();
  eventTerminal = new Map<string, { terminal: boolean; terminalState?: string; turnState?: string }>();
  eventResponses = new Map<string, Record<string, unknown>>();
  eventCalls: Array<{ runId: string; since: number; limit: number | undefined; deltaSince: number | undefined }> = [];
  approveError: Error | undefined;
  closeError: Error | undefined;
  controlError: Error | undefined;
  resumeError: Error | undefined;
  capabilitiesResponse: CapabilitiesResult | Error = { status: "OK", engines: [localCapability()] };
  capabilityRequests: boolean[] = [];

  factory = (callbacks: SessionClientCallbacks): SessionPort => {
    this.clients += 1;
    this.callbacks.push(callbacks);
    return new FakeSession(this, callbacks);
  };

  /** Deliver one synchronous event batch and a nonterminal turn-state transition to the latest client. */
  emit(events: WireEvent[], turnState = "idle"): void {
    const callbacks = this.callbacks.at(-1);
    assert.ok(callbacks, "Backend.emit called before a session client registered");
    callbacks.onEvents?.(events, events.at(-1)?.sequence_no ?? 0);
    callbacks.onTurnState?.(turnState, false);
  }
}

class FakeSession implements SessionPort {
  runId: string | null = null;
  session: string | null = null;

  constructor(
    private readonly backend: Backend,
    private readonly callbacks: SessionClientCallbacks,
  ) {}

  capabilities(refresh = false): Promise<CapabilitiesResult> {
    this.backend.capabilityRequests.push(refresh);
    return this.backend.capabilitiesResponse instanceof Error
      ? Promise.reject(this.backend.capabilitiesResponse)
      : Promise.resolve(this.backend.capabilitiesResponse);
  }

  list(args: { controller?: "driven" | "observed" } = {}): Promise<Record<string, unknown>> {
    return Promise.resolve(this.backend.listResponses.get(args.controller ?? "") ?? this.backend.listResponse);
  }

  eventsOnce(agentRunId: string, since = 0, limit?: number, deltaSince?: number): Promise<Record<string, unknown>> {
    this.backend.eventCalls.push({ runId: agentRunId, since, limit, deltaSince });
    const override = this.backend.eventResponses.get(agentRunId);
    if (override) return Promise.resolve(override);
    const events = (this.backend.events.get(agentRunId) ?? [])
      .filter((event) => event.sequence_no > since)
      .slice(0, limit);
    const terminal = this.backend.eventTerminal.get(agentRunId) ?? { terminal: true, terminalState: "success", turnState: "terminal" };
    return Promise.resolve({
      status: "OK",
      events,
      cursor: events.at(-1)?.sequence_no ?? since,
      terminal: terminal.terminal,
      ...(terminal.terminalState ? { terminal_state: terminal.terminalState } : {}),
      turn_state: terminal.turnState ?? (terminal.terminal ? "terminal" : "open"),
    });
  }

  start(request: StartRequest) {
    this.backend.starts.push(request);
    if (this.backend.startResponse) return Promise.resolve(this.backend.startResponse);
    this.runId = "run-1";
    this.session = "session-1";
    return Promise.resolve({
      status: "OK",
      session_id: this.session,
      agent_run_id: this.runId,
      engine: request.engine,
      profile: request.profile,
      profile_hash: "hash-1",
    });
  }

  resume(agentRunId: string, sessionId?: string | null): void {
    this.runId = agentRunId;
    this.session = sessionId ?? null;
    this.backend.resumes.push([agentRunId, sessionId]);
    if (this.backend.resumeError) this.callbacks.onError?.(this.backend.resumeError);
  }

  setStreaming(enabled: boolean): void {
    this.backend.streamingSettings.push(enabled);
  }

  turn(prompt: string, envelope: TurnEnvelope = {}): Promise<Record<string, unknown>> {
    this.backend.turns.push(prompt);
    this.backend.turnEnvelopes.push(structuredClone(envelope));
    return Promise.resolve(this.backend.turnResponse);
  }

  steer(): Promise<Record<string, unknown>> {
    if (this.backend.controlError) return Promise.reject(this.backend.controlError);
    return Promise.resolve({ status: "OK" });
  }

  interrupt(): Promise<Record<string, unknown>> {
    if (this.backend.controlError) return Promise.reject(this.backend.controlError);
    return Promise.resolve({ status: "OK" });
  }

  close(): Promise<Record<string, unknown>> {
    this.backend.closes += 1;
    if (this.backend.closeError) return Promise.reject(this.backend.closeError);
    return Promise.resolve({ status: "OK" });
  }

  kill(): Promise<Record<string, unknown>> {
    return Promise.resolve({ status: "OK" });
  }

  approve(
    correlationId: string,
    decision: "approve" | "deny",
    sessionId?: string,
    confirmation?: { expected_revision: number; snapshot_set_sha256: string; metadata_confirmed: boolean },
    answers?: Record<string, string>,
  ): Promise<Record<string, unknown>> {
    this.backend.approvals.push([correlationId, decision]);
    this.backend.approvalRequests.push({ correlationId, decision, ...(sessionId ? { sessionId } : {}), ...(confirmation ? { confirmation } : {}), ...(answers ? { answers } : {}) });
    if (this.backend.approveError) return Promise.reject(this.backend.approveError);
    return Promise.resolve({ status: "OK" });
  }

  dispose(): void {
    void this.callbacks;
  }
}

/** Build a wire approval body with its matching canonical snapshot digest for the selected revision. */
function diffApprovalBody(revision: number, metadata = false): string {
  const sha256 = "a".repeat(64);
  const side = { artifactRef: `sha256:${sha256}`, sha256, bytes: 4, encoding: metadata ? null : ("utf-8" as const), mediaType: metadata ? "application/octet-stream" : null };
  const diff: NegotiatedDiff = {
    revision,
    expiresAt: "2026-07-12T12:00:00Z",
    snapshotSetSha256: "",
    fileChanges: [{
      changeId: "change-1", path: "src/app.ts", kind: "create", oldPath: null,
      previewMode: metadata ? "metadata" : "text", previewReason: metadata ? "binary" : null,
      before: null, proposed: side,
    }],
    metadataConfirmationRequired: metadata,
  };
  diff.snapshotSetSha256 = canonicalSnapshotSetSha256(diff);
  return JSON.stringify({
    approval_revision: revision,
    expires_at: diff.expiresAt,
    snapshot_set_sha256: diff.snapshotSetSha256,
    file_changes: [{
      change_id: "change-1", path: "src/app.ts", kind: "create", old_path: null,
      preview_mode: metadata ? "metadata" : "text", preview_reason: metadata ? "binary" : null, before: null,
      proposed: { artifact_ref: side.artifactRef, sha256, bytes: 4, encoding: side.encoding, media_type: side.mediaType },
    }],
  });
}

function prompts(overrides: Partial<ControllerPrompts> = {}): ControllerPrompts {
  return {
    confirmFull: async () => true,
    confirmNewConversation: async () => true,
    ...overrides,
  };
}

async function settleUntil(predicate: () => boolean): Promise<void> {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (predicate()) return;
    await new Promise<void>((resolve) => setImmediate(resolve));
  }
  assert.fail("controller effect did not settle");
}

function chat(sequence_no: number, role: "user" | "assistant", text: string, source: "driven" | "observed" = "driven"): WireEvent {
  return {
    sequence_no,
    event_kind: "chat_message",
    marker: "point",
    body: JSON.stringify({ role, text, source }),
  };
}

function fileContext(id = "ctx-file"): StagedContext {
  return { ref: { id, kind: "file", source_path: "src/app.ts" }, label: "app.ts", bytes: 12 };
}

function selectionContext(id = "ctx-selection"): StagedContext {
  return {
    ref: {
      id,
      kind: "selection",
      source_path: "src/app.ts",
      range: { start: { line: 1, character: 2 }, end: { line: 1, character: 5 } },
      snapshot_ref: `sha256:${"a".repeat(64)}`,
      sha256: "a".repeat(64),
      bytes: 3,
      encoding: "utf-8",
    },
    label: "app.ts:2-2",
    bytes: 3,
  };
}

function stagedImage(id = "image-1"): StagedImage {
  return {
    ref: {
      id, kind: "image", artifact_ref: `sha256:${"b".repeat(64)}`, sha256: "b".repeat(64), bytes: 12,
      media_type: "image/png", name: "capture.png",
    },
    label: "capture.png",
  };
}

test("sidebar and popout share one client, reducer, and identical snapshots; detaching one leaves the run alive", async () => {
  const backend = new Backend();
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  await controller.initialize();
  const sidebar = new Renderer("sidebar");
  const popout = new Renderer("popout");
  controller.attach(sidebar);
  controller.attach(popout);

  await controller.handle({ type: "userPrompt", prompt: "first" });
  assert.equal(backend.starts.length, 1);
  assert.equal(backend.clients, 1);
  assert.deepEqual(sidebar.snapshot, popout.snapshot);

  backend.emit([chat(1, "user", "first"), chat(2, "assistant", "answer")]);
  assert.deepEqual(sidebar.snapshot?.transcript, popout.snapshot?.transcript);
  assert.equal(sidebar.snapshot?.conversation?.state, "idle");

  const sidebarCount = sidebar.messages.length;
  controller.detach("sidebar");
  await controller.handle({ type: "userPrompt", prompt: "second" });
  assert.deepEqual(backend.turns, ["second"]);
  assert.equal(sidebar.messages.length, sidebarCount);
  assert.ok(popout.messages.length > sidebarCount);
});

test("reload persists only identifiers/profile hash and replays through one resumed pump", async () => {
  const memory = new MemoryMemento();
  memory.values.set(RUN_CURSORS_KEY, {
    session_id: "session-kept",
    agent_run_id: "run-kept",
    profile_hash: "hash-kept",
  });
  const backend = new Backend();
  const controller = new ConversationController(memory, backend.factory, prompts());
  await controller.initialize();
  const renderer = new Renderer("sidebar");
  controller.attach(renderer);

  assert.deepEqual(backend.resumes, [["run-kept", "session-kept"]]);
  assert.equal(renderer.snapshot?.selectorsLocked, true);
  assert.deepEqual(memory.values.get(RUN_CURSORS_KEY), {
    session_id: "session-kept",
    agent_run_id: "run-kept",
    profile_hash: "hash-kept",
  });
  const persisted = memory.values.get(RUN_CURSORS_KEY) as Record<string, unknown>;
  assert.equal(Object.hasOwn(persisted, "transcript"), false);
});

test("New Conversation is denied while running and closes an idle run before resetting", async () => {
  const backend = new Backend();
  let confirmations = 0;
  const controller = new ConversationController(
    new MemoryMemento(),
    backend.factory,
    prompts({ confirmNewConversation: async () => (confirmations += 1) > 0 }),
  );
  await controller.initialize();
  await controller.handle({ type: "userPrompt", prompt: "first" });
  await controller.handle({ type: "newConversation" });
  assert.equal(backend.closes, 0);
  assert.equal(confirmations, 0);

  backend.emit([chat(1, "user", "first")]);
  await controller.handle({ type: "newConversation" });
  assert.equal(backend.closes, 1);
  assert.equal(confirmations, 1);
  assert.equal(controller.snapshot().conversation, null);
  assert.equal(backend.clients, 2);
});

test("Full mode gets a fresh host confirmation and is never persisted", async () => {
  const memory = new MemoryMemento();
  const backend = new Backend();
  let confirmations = 0;
  const controller = new ConversationController(
    memory,
    backend.factory,
    prompts({ confirmFull: async () => (confirmations += 1) > 0 }),
  );
  await controller.initialize();
  await controller.handle({
    type: "profileChange",
    engine: "local_llm",
    profile: { model: "qwen", permission_mode: "full", auth_mode: "none" },
  });
  await controller.handle({ type: "userPrompt", prompt: "go" });

  assert.equal(confirmations, 1);
  assert.equal(backend.starts[0].full_opt_in, true);
  const persisted = memory.values.get(RUN_CURSORS_KEY) as Record<string, unknown>;
  assert.equal(Object.hasOwn(persisted, "full"), false);

  backend.emit([chat(1, "user", "go")]);
  await controller.handle({ type: "newConversation" });
  await controller.handle({
    type: "profileChange",
    engine: "local_llm",
    profile: { model: "qwen", permission_mode: "full", auth_mode: "none" },
  });
  await controller.handle({ type: "userPrompt", prompt: "go again" });
  assert.equal(confirmations, 2);
  assert.equal(backend.starts.length, 2);
  assert.equal(backend.starts[1].full_opt_in, true);
});

test("Claude uses only the dynamic catalog, requires subscription, and locks model effort and max turns", async () => {
  const backend = new Backend();
  backend.capabilitiesResponse = { status: "OK", engines: [claudeCapability()] };
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  await controller.initialize();
  assert.deepEqual(controller.snapshot().selectedProfile, {
    max_turns: 8,
    permission_mode: "plan",
    auth_mode: "subscription",
  });

  await controller.handle({
    type: "profileChange",
    engine: "claude",
    profile: {
      model: "account-model-b", reasoning_effort: "max", max_turns: 32,
      permission_mode: "ask", auth_mode: "api-key",
    },
  });
  assert.equal(controller.snapshot().selectedProfile.auth_mode, "subscription");
  await controller.handle({ type: "userPrompt", prompt: "bounded" });
  assert.deepEqual(backend.starts[0], {
    engine: "claude",
    prompt: "bounded",
    profile: {
      model: "account-model-b", reasoning_effort: "max", permission_mode: "ask", auth_mode: "subscription",
    },
    max_turns: 32,
    client_features: { diff_snapshots: true },
  });
  assert.equal(controller.snapshot().selectorsLocked, true);
  await controller.handle({
    type: "profileChange",
    engine: "claude",
    profile: {
      model: "account-model-a", reasoning_effort: "high", max_turns: 1,
      permission_mode: "plan", auth_mode: "subscription",
    },
  });
  assert.equal(controller.snapshot().selectedProfile.model, "account-model-b");
  assert.equal(controller.snapshot().selectedProfile.max_turns, 32);
});

test("Claude models with no advertised effort choices start without an invented effort", async () => {
  const backend = new Backend();
  backend.capabilitiesResponse = {
    status: "OK",
    engines: [claudeCapability({
      models: [{ id: "account-model-no-effort", label: "Account Model", reasoning_efforts: [] }],
      default_model: "account-model-no-effort",
      default_reasoning_effort: undefined,
    })],
  };
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  await controller.initialize();

  assert.deepEqual(controller.snapshot().selectedProfile, {
    model: "account-model-no-effort",
    max_turns: 8,
    permission_mode: "plan",
    auth_mode: "subscription",
  });
  assert.equal(controller.snapshot().banner, undefined);

  await controller.handle({ type: "userPrompt", prompt: "no effort profile" });
  assert.deepEqual(backend.starts[0], {
    engine: "claude",
    prompt: "no effort profile",
    profile: {
      model: "account-model-no-effort", permission_mode: "plan", auth_mode: "subscription",
    },
    max_turns: 8,
    client_features: { diff_snapshots: true },
  });
});

test("Claude capability refresh preserves an unavailable exact model and surfaces its denial", async () => {
  const backend = new Backend();
  backend.capabilitiesResponse = { status: "OK", engines: [claudeCapability()] };
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  await controller.initialize();
  await controller.handle({
    type: "profileChange",
    engine: "claude",
    profile: {
      model: "account-model-b", reasoning_effort: "max", max_turns: 12,
      permission_mode: "ask", auth_mode: "subscription",
    },
  });

  backend.capabilitiesResponse = {
    status: "OK",
    engines: [claudeCapability({
      models: [{ id: "account-model-a", label: "Account A", reasoning_efforts: ["low", "high"] }],
    })],
  };
  await controller.handle({ type: "refreshCapabilities" });
  assert.deepEqual(controller.snapshot().selectedProfile, {
    model: "account-model-b", reasoning_effort: "max", max_turns: 12,
    permission_mode: "ask", auth_mode: "subscription",
  });
  assert.equal(controller.snapshot().banner?.code, "DENIED_MODEL_UNAVAILABLE");

  await controller.handle({ type: "userPrompt", prompt: "must not silently switch models" });
  assert.equal(backend.starts.length, 0);
  assert.equal(controller.snapshot().banner?.code, "DENIED_MODEL_UNAVAILABLE");

  backend.capabilitiesResponse = { status: "OK", engines: [claudeCapability()] };
  await controller.handle({ type: "refreshCapabilities" });
  assert.equal(controller.snapshot().selectedProfile.model, "account-model-b");
  assert.equal(controller.snapshot().selectedProfile.reasoning_effort, "max");
  assert.equal(controller.snapshot().banner, undefined);

  await controller.handle({
    type: "profileChange",
    engine: "claude",
    profile: {
      model: "unknown-account-model", reasoning_effort: "high", max_turns: 12,
      permission_mode: "ask", auth_mode: "subscription",
    },
  });
  assert.equal(controller.snapshot().selectedProfile.model, "unknown-account-model");
  assert.equal(controller.snapshot().banner?.code, "DENIED_MODEL_UNAVAILABLE");
});

test("explicit capability refresh keeps structured and transport failures visible until a successful refresh", async () => {
  const backend = new Backend();
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  await controller.initialize();
  assert.equal(controller.snapshot().capabilities.length, 1);

  backend.capabilitiesResponse = {
    status: "DENIED", code: "DENIED_PROVIDER_CAPACITY", message: "bounded provider refresh refused", engines: [],
  };
  await controller.handle({ type: "refreshCapabilities" });
  assert.equal(controller.snapshot().banner?.code, "DENIED_PROVIDER_CAPACITY");
  assert.equal(controller.snapshot().capabilities.length, 1, "last good catalog remains displayable");

  backend.capabilitiesResponse = new Error("loopback timeout");
  await controller.handle({ type: "refreshCapabilities" });
  assert.equal(controller.snapshot().banner?.code, "DAEMON_UNREACHABLE");
  assert.match(controller.snapshot().banner?.message ?? "", /loopback timeout/);
  assert.equal(controller.snapshot().capabilities.length, 1);

  backend.capabilitiesResponse = { status: "OK", engines: [localCapability()] };
  await controller.handle({ type: "refreshCapabilities" });
  assert.equal(controller.snapshot().banner, undefined, "success clears the prior capability outage");
  assert.equal(controller.snapshot().capabilities[0].id, "local_llm");
  assert.deepEqual(backend.capabilityRequests, [false, true, true, true]);
});

test("observed linked runs replay in order and remain strictly read-only", async () => {
  const backend = new Backend();
  backend.listResponse = {
    status: "OK",
    sessions: [
      {
        session: { id: "observed-1", summary: "Claude observed" },
        runs: [{ id: "run-a" }, { id: "run-b" }],
      },
    ],
  };
  backend.events.set("run-a", [chat(1, "user", "one", "observed")]);
  backend.events.set("run-b", [chat(1, "assistant", "two", "observed")]);
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  await controller.initialize();
  await controller.handle({ type: "observedSelect", sessionId: "observed-1" });

  const snapshot = controller.snapshot();
  assert.equal(snapshot.conversation?.readOnly, true);
  assert.deepEqual(snapshot.transcript.map((message) => message.text), ["one", "two"]);
  await controller.handle({ type: "userPrompt", prompt: "must not send" });
  await controller.handle({ type: "approve", correlationId: "display-only" });
  assert.equal(backend.starts.length, 0);
  assert.equal(backend.turns.length, 0);
  assert.equal(backend.approvals.length, 0);
});

test("prior-leg replay drains more than one 500-event page", async () => {
  const backend = new Backend();
  backend.listResponses.set("driven", {
    status: "OK",
    sessions: [{
      session_id: "driven-many",
      controller: "driven",
      latest_run_id: "run-current",
      runs: [{ id: "run-many" }, { id: "run-current" }],
    }],
  });
  const all = Array.from({ length: 501 }, (_, index) => chat(index + 1, "assistant", `message-${index + 1}`));
  backend.events.set("run-many", all);
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  await controller.initialize();
  await controller.openHistoryConversation("driven-many", "run-current", "driven");
  assert.equal(controller.snapshot().transcript.length, all.length);
  assert.deepEqual(
    backend.eventCalls.filter(({ runId }) => runId === "run-many").map(({ since, limit }) => [since, limit]),
    [[0, 500], [500, 500], [501, 500]],
  );
  controller.dispose();
});

test("approval cards remain visible and pending until a durable resolved event arrives", async () => {
  const backend = new Backend();
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  await controller.initialize();
  await controller.handle({ type: "userPrompt", prompt: "write" });
  backend.emit([
    chat(1, "user", "write"),
    { sequence_no: 2, event_kind: "approval", marker: "open", correlation_id: "corr-1", summary: "write needs approval" },
  ], "running");
  await controller.handle({ type: "approve", correlationId: "corr-1" });
  const pending = controller.snapshot().approvals[0];
  assert.deepEqual(
    { run: pending.agentRunId, sequence: pending.sequenceNo, id: pending.correlationId, status: pending.status, pending: pending.pending },
    { run: "run-1", sequence: 2, id: "corr-1", status: "pending", pending: true },
  );
  const timelineKey = pending.timelineKey;
  backend.emit([
    { sequence_no: 3, event_kind: "approval", marker: "resolved", correlation_id: "corr-1", summary: "approved" },
  ], "running");
  const resolved = controller.snapshot().approvals[0];
  assert.equal(controller.snapshot().approvals.length, 1);
  assert.equal(resolved.timelineKey, timelineKey);
  assert.equal(resolved.status, "approved");
  assert.equal(resolved.pending, false);
});

test("free-text approval questions reach the view and exact validated answers reach the daemon", async () => {
  const backend = new Backend();
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  await controller.initialize();
  await controller.handle({ type: "userPrompt", prompt: "ask" });
  backend.emit([{
    sequence_no: 1,
    event_kind: "approval",
    marker: "open",
    correlation_id: "input-1",
    summary: "Answer required",
    body: JSON.stringify({
      questions: [{ id: "reason", header: "Decision", question: "Why continue?", options: [], ignored: "drop" }],
      ignored: "drop",
    }),
  }], "running");
  assert.deepEqual(controller.snapshot().approvals[0]?.questions, [
    { id: "reason", header: "Decision", question: "Why continue?", options: [] },
  ]);

  await controller.handle({ type: "approve", correlationId: "input-1", agentRunId: "run-1", answers: { reason: "  exact answer  " } });
  assert.deepEqual(backend.approvalRequests.at(-1), {
    correlationId: "input-1",
    decision: "approve",
    sessionId: "session-1",
    answers: { reason: "  exact answer  " },
  });
});

test("malformed explicit approval-answer maps are denied locally", async () => {
  const invalidAnswers: Array<[string, unknown]> = [
    ["empty object", {}],
    ["array", []],
    ["blank answer", { reason: "   " }],
    ["NUL", { reason: "contains\0nul" }],
    ["oversize", { reason: "x".repeat(64 * 1024 + 1) }],
    ["unpaired surrogate", { reason: "\ud800" }],
    ["non-string", { reason: 7 }],
  ];
  for (const [name, answers] of invalidAnswers) {
    const backend = new Backend();
    const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
    await controller.initialize();
    await controller.handle({ type: "userPrompt", prompt: "ask" });
    await controller.handle({ type: "approve", correlationId: "input-1", answers } as never);
    assert.equal(backend.approvalRequests.length, 0, name);
    assert.equal(controller.snapshot().banner?.code, "DENIED_USER_INPUT_ANSWER_INVALID", name);
    controller.dispose();
  }
});

test("negotiated diffs track only the latest revision and Accept sends the exact confirmation tuple", async () => {
  const backend = new Backend();
  backend.capabilitiesResponse = { status: "OK", engines: [localCapability({
    features: { streaming: false, image_attachments: false, governed_context: false, diff_snapshots: true, writer_leasing: false },
  })] };
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  await controller.initialize();
  await controller.handle({ type: "userPrompt", prompt: "write" });
  backend.emit([chat(1, "user", "write"), {
    sequence_no: 2, event_kind: "approval", marker: "open", correlation_id: "corr-diff", body: diffApprovalBody(1),
  }], "running");
  const first = controller.snapshot().approvals[0];
  assert.equal(first.diff?.revision, 1);
  assert.equal(first.diff?.files[0].beforeSha256, null);
  assert.equal(first.diff?.files[0].proposedSha256, "a".repeat(64));
  const timelineKey = first.timelineKey;
  await controller.handle({ type: "approve", correlationId: "corr-diff", agentRunId: "run-1" });
  assert.equal(backend.approvalRequests.length, 0, "generic Approve cannot bypass negotiated diff confirmation");

  backend.emit([{
    sequence_no: 3, event_kind: "approval", marker: "open", correlation_id: "corr-diff", body: diffApprovalBody(2),
  }], "running");
  const refreshed = controller.snapshot().approvals[0];
  assert.equal(refreshed.timelineKey, timelineKey, "revision refresh mutates the inline card");
  assert.equal(refreshed.diff?.revision, 2);
  assert.equal((await controller.resolveDiff("run-1", "corr-diff", "approve", false)), true);
  assert.deepEqual(backend.approvalRequests.at(-1), {
    correlationId: "corr-diff",
    decision: "approve",
    sessionId: "session-1",
    confirmation: {
      expected_revision: 2,
      snapshot_set_sha256: refreshed.diff?.snapshotSetSha256,
      metadata_confirmed: false,
    },
  });
});

test("Test Extension profile attestation becomes true only after a durable effective-profile event", async () => {
  const backend = new Backend();
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  await controller.initialize();
  assert.equal(controller.testExtensionObservation().profileAttested, false);
  backend.emit([{
    sequence_no: 1,
    event_kind: "profile",
    marker: "point",
    body: JSON.stringify({
      effective: { model: "attested-model", reasoning_effort: "high", permission_mode: "ask", auth_mode: "subscription" },
      profile_hash: "a".repeat(64),
    }),
  }], "running");
  const observation = controller.testExtensionObservation();
  assert.equal(observation.profileAttested, true);
  assert.equal(observation.profile.model, "attested-model");
  assert.equal(observation.profile.reasoning_effort, "high");
});

test("metadata diff acceptance requires host confirmation while Reject carries no tuple", async () => {
  const backend = new Backend();
  backend.capabilitiesResponse = { status: "OK", engines: [localCapability({
    features: { streaming: false, image_attachments: false, governed_context: false, diff_snapshots: true, writer_leasing: false },
  })] };
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  await controller.initialize();
  await controller.handle({ type: "userPrompt", prompt: "binary" });
  backend.emit([{
    sequence_no: 1, event_kind: "approval", marker: "open", correlation_id: "meta", body: diffApprovalBody(1, true),
  }], "running");
  assert.equal(await controller.resolveDiff("run-1", "meta", "approve", false), false);
  assert.equal(backend.approvalRequests.length, 0);
  assert.equal(await controller.resolveDiff("run-1", "meta", "approve", true), true);
  assert.equal(backend.approvalRequests.at(-1)?.confirmation?.metadata_confirmed, true);

  backend.emit([{
    sequence_no: 2, event_kind: "approval", marker: "open", correlation_id: "reject", body: diffApprovalBody(1),
  }], "running");
  assert.equal(await controller.resolveDiff("run-1", "reject", "deny"), true);
  assert.equal(backend.approvalRequests.at(-1)?.decision, "deny");
  assert.equal(backend.approvalRequests.at(-1)?.confirmation, undefined);
});

test("malformed negotiated approval metadata is rendered fail-closed", async () => {
  const backend = new Backend();
  backend.capabilitiesResponse = { status: "OK", engines: [localCapability({
    features: { streaming: false, image_attachments: false, governed_context: false, diff_snapshots: true, writer_leasing: false },
  })] };
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  await controller.initialize();
  await controller.handle({ type: "userPrompt", prompt: "bad diff" });
  backend.emit([{
    sequence_no: 1, event_kind: "approval", marker: "open", correlation_id: "bad", body: JSON.stringify({ approval_revision: 1, snapshot_set_sha256: "bad", file_changes: [] }),
  }], "running");
  assert.equal(controller.snapshot().approvals[0]?.diffError?.code, "DENIED_APPROVAL_BODY_INVALID");
  await controller.handle({ type: "approve", correlationId: "bad", agentRunId: "run-1" });
  assert.equal(backend.approvalRequests.length, 0);
});

test("orphan-sweep finalization renders the daemon-restart RESUMABLE state (owner continuation decision)", async () => {
  const backend = new Backend();
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  await controller.initialize();
  await controller.handle({ type: "userPrompt", prompt: "wait" });
  backend.emit([
    {
      sequence_no: 1,
      event_kind: "finalization",
      marker: "close_fail",
      code: "ORPHAN_SWEEP_FINALIZED",
      summary: "orphan-sweep force-finalize: owning daemon dead",
    },
  ], "idle");
  backend.callbacks.at(-1)?.onTerminal?.("failure");
  const snapshot = controller.snapshot();
  assert.equal(snapshot.banner?.code, "DAEMON_RESTART");
  assert.match(snapshot.banner?.message ?? "", /send a message to continue/i);
  assert.equal(snapshot.conversation?.state, "terminal");
  assert.equal(snapshot.conversation?.resumable, true);
  assert.equal(snapshot.conversation?.readOnly, false);
});

test("a rejected approval transport clears pending state and remains retryable", async () => {
  const backend = new Backend();
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  await controller.initialize();
  await controller.handle({ type: "userPrompt", prompt: "write", requestToken: "prompt-1" });
  backend.emit([
    chat(1, "user", "write"),
    { sequence_no: 2, event_kind: "approval", marker: "open", correlation_id: "corr-1", summary: "write needs approval" },
  ], "running");
  backend.approveError = new Error("transport down");
  await controller.handle({ type: "approve", correlationId: "corr-1" });
  assert.equal(controller.snapshot().approvals[0]?.pending, false);
  assert.equal(controller.snapshot().banner?.code, "DAEMON_UNREACHABLE");
  backend.approveError = undefined;
  await controller.handle({ type: "approve", correlationId: "corr-1" });
  assert.deepEqual(backend.approvals.map(([id]) => id), ["corr-1", "corr-1"]);
  assert.equal(controller.snapshot().approvals[0]?.pending, true);
});

test("close transport errors are contained and leave the idle conversation recoverable", async () => {
  const backend = new Backend();
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  await controller.initialize();
  await controller.handle({ type: "userPrompt", prompt: "hello" });
  backend.emit([chat(1, "user", "hello")]);
  backend.closeError = new Error("close transport down");
  await controller.handle({ type: "close" });
  assert.equal(controller.snapshot().conversation?.state, "idle");
  assert.equal(controller.snapshot().conversation?.readOnly, false);
  assert.equal(controller.snapshot().banner?.code, "DAEMON_UNREACHABLE");
});

test("steer and interrupt transport errors are contained by the shared control path", async () => {
  const backend = new Backend();
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  await controller.initialize();
  await controller.handle({ type: "userPrompt", prompt: "hello" });
  backend.controlError = new Error("control transport down");
  await controller.handle({ type: "steer", instruction: "adjust" });
  assert.equal(controller.snapshot().banner?.code, "DAEMON_UNREACHABLE");
  await controller.handle({ type: "interrupt" });
  assert.match(controller.snapshot().banner?.message ?? "", /interrupt failed/);
});

test("structured stale-run errors clear persisted reattachment and recover to a new conversation", async () => {
  const memory = new MemoryMemento();
  memory.values.set(RUN_CURSORS_KEY, { session_id: "gone-session", agent_run_id: "gone-run", profile_hash: "old" });
  const backend = new Backend();
  backend.resumeError = new SessionResponseError({ status: "DENIED", code: "DENIED_AGENT_RUN_NOT_DRIVEN" });
  const controller = new ConversationController(memory, backend.factory, prompts());
  await controller.initialize();
  assert.equal(controller.snapshot().conversation, null);
  assert.equal(controller.snapshot().selectorsLocked, false);
  assert.equal(memory.values.has(RUN_CURSORS_KEY), false);
  assert.equal(backend.clients, 2);
  assert.equal(controller.snapshot().banner?.code, "DENIED_AGENT_RUN_NOT_DRIVEN");
});

test("observed refresh re-lists linked runs, incrementally replays them, and stays display-only while open", async () => {
  const backend = new Backend();
  backend.listResponses.set("observed", {
    status: "OK",
    sessions: [{
      session_id: "observed-1",
      summary: "Observed Claude",
      runs: [{ id: "run-a", terminal: false, turn_state: "open" }],
    }],
  });
  backend.events.set("run-a", [
    chat(1, "user", "one", "observed"),
    { sequence_no: 2, event_kind: "approval", marker: "open", correlation_id: "obs-a", summary: "observed write" },
  ]);
  backend.eventTerminal.set("run-a", { terminal: false, turnState: "open" });
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  await controller.initialize();
  await controller.handle({ type: "observedSelect", sessionId: "observed-1" });
  assert.equal(controller.snapshot().conversation?.state, "running");
  assert.equal(controller.snapshot().conversation?.readOnly, true);
  assert.deepEqual(controller.snapshot().approvals.map(({ agentRunId, correlationId, status, displayOnly, pending }) => ({ agentRunId, correlationId, status, displayOnly, pending })), [
    { agentRunId: "run-a", correlationId: "obs-a", status: "pending", displayOnly: true, pending: false },
  ]);

  backend.events.set("run-a", [
    ...(backend.events.get("run-a") ?? []),
    chat(3, "assistant", "answer one", "observed"),
    { sequence_no: 4, event_kind: "approval", marker: "resolved", correlation_id: "obs-a" },
  ]);
  backend.eventTerminal.set("run-a", { terminal: true, terminalState: "success", turnState: "terminal" });
  backend.events.set("run-b", [
    chat(1, "user", "two", "observed"),
    { sequence_no: 2, event_kind: "approval", marker: "open", correlation_id: "obs-b", summary: "observed execute" },
  ]);
  backend.eventTerminal.set("run-b", { terminal: false, turnState: "open" });
  backend.listResponses.set("observed", {
    status: "OK",
    sessions: [{
      session_id: "observed-1",
      summary: "Observed Claude",
      runs: [
        { id: "run-a", terminal: true, terminal_state: "success", turn_state: "terminal" },
        { id: "run-b", terminal: false, turn_state: "open" },
      ],
    }],
  });
  await controller.refreshObserved();
  assert.deepEqual(controller.snapshot().transcript.map((message) => message.text), ["one", "answer one", "two"]);
  assert.equal(controller.snapshot().conversation?.agentRunId, "run-b");
  assert.equal(controller.snapshot().conversation?.state, "running");
  assert.deepEqual(controller.snapshot().approvals.map(({ agentRunId, correlationId, status, displayOnly, pending }) => ({ agentRunId, correlationId, status, displayOnly, pending })), [
    { agentRunId: "run-a", correlationId: "obs-a", status: "approved", displayOnly: true, pending: false },
    { agentRunId: "run-b", correlationId: "obs-b", status: "pending", displayOnly: true, pending: false },
  ]);
  assert.ok(backend.eventCalls.some((call) => call.runId === "run-a" && call.since === 2));
  await controller.handle({ type: "approve", correlationId: "obs-b" });
  assert.equal(backend.approvals.length, 0);
});

test("observed replay validates non-OK responses without rejecting the webview handler", async () => {
  const backend = new Backend();
  backend.listResponses.set("observed", {
    status: "OK",
    sessions: [{ session_id: "observed-1", runs: [{ id: "run-bad", terminal: false }] }],
  });
  backend.eventResponses.set("run-bad", { status: "DENIED", code: "DENIED_AGENT_RUN_NOT_DRIVEN" });
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  await controller.initialize();
  await controller.handle({ type: "observedSelect", sessionId: "observed-1" });
  assert.equal(controller.snapshot().conversation?.readOnly, true);
  assert.equal(controller.snapshot().banner?.code, "DENIED_AGENT_RUN_NOT_DRIVEN");
});

test("capabilities canonicalize the claude_cli alias and prefer the canonical descriptor", async () => {
  const backend = new Backend();
  backend.capabilitiesResponse = {
    status: "OK",
    engines: [
      localCapability(),
      localCapability({ id: "claude_cli", label: "Legacy Claude", auth_modes: ["subscription"] }),
      localCapability({ id: "claude", label: "Claude", auth_modes: ["subscription"] }),
    ],
  };
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  await controller.initialize();
  assert.deepEqual(controller.snapshot().capabilities.map(({ id }) => id), ["local_llm", "claude"]);
  assert.equal(controller.snapshot().capabilities.find(({ id }) => id === "claude")?.label, "Claude");
});

test("model changes replace an invalid stale effort with the selected model default or clear it", async () => {
  const backend = new Backend();
  backend.capabilitiesResponse = {
    status: "OK",
    engines: [localCapability({
      models: [
        { id: "model-a", label: "A", reasoning_efforts: ["high"], default_effort: "high" },
        { id: "model-b", label: "B", reasoning_efforts: ["low"], default_effort: "low" },
        { id: "model-c", label: "C", reasoning_efforts: [] },
      ],
      default_model: "model-a",
      default_reasoning_effort: "high",
    })],
  };
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  await controller.initialize();
  assert.equal(controller.snapshot().selectedProfile.reasoning_effort, "high");
  await controller.handle({
    type: "profileChange",
    engine: "local_llm",
    profile: { model: "model-b", reasoning_effort: "high", permission_mode: "plan", auth_mode: "none" },
  });
  assert.equal(controller.snapshot().selectedProfile.reasoning_effort, "low");
  await controller.handle({
    type: "profileChange",
    engine: "local_llm",
    profile: { model: "model-c", reasoning_effort: "low", permission_mode: "plan", auth_mode: "none" },
  });
  assert.equal(controller.snapshot().selectedProfile.reasoning_effort, undefined);
});

test("prompt tokens settle on rejection and acknowledge only after the durable user event", async () => {
  const backend = new Backend();
  let confirmations = 0;
  const controller = new ConversationController(
    new MemoryMemento(),
    backend.factory,
    prompts({ confirmFull: async () => (++confirmations === 1 ? false : true) }),
  );
  await controller.initialize();
  await controller.handle({
    type: "profileChange",
    engine: "local_llm",
    profile: { model: "qwen", permission_mode: "full", auth_mode: "none" },
  });
  await controller.handle({ type: "userPrompt", prompt: "first", requestToken: "rejected-token" });
  assert.equal(controller.snapshot().settledPromptToken, "rejected-token");
  assert.equal(controller.snapshot().acceptedPromptToken, undefined);
  assert.equal(controller.snapshot().pendingPrompt, false);
  assert.equal(backend.starts.length, 0);

  await controller.handle({ type: "userPrompt", prompt: "second", requestToken: "accepted-token" });
  assert.equal(controller.snapshot().pendingPromptToken, "accepted-token");
  assert.equal(controller.snapshot().acceptedPromptToken, undefined);
  backend.emit([chat(1, "user", "second")], "running");
  assert.equal(controller.snapshot().settledPromptToken, "accepted-token");
  assert.equal(controller.snapshot().acceptedPromptToken, "accepted-token");
  assert.equal(controller.snapshot().pendingPrompt, false);
  assert.equal(confirmations, 2);
});

test("a second prompt racing the pending request is accepted into the ephemeral FIFO", async () => {
  const backend = new Backend();
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  await controller.initialize();
  await controller.handle({ type: "userPrompt", prompt: "first", requestToken: "sidebar-1" });
  await controller.handle({ type: "userPrompt", prompt: "duplicate", requestToken: "popout-1" });
  assert.equal(controller.snapshot().pendingPromptToken, "sidebar-1");
  assert.equal(controller.snapshot().settledPromptToken, "popout-1");
  assert.equal(controller.snapshot().queuedPromptToken, "popout-1");
  assert.deepEqual(controller.snapshot().followUpQueue.items.map((item) => item.preview), ["duplicate"]);
  assert.equal(backend.starts.length, 1);
  backend.emit([chat(1, "user", "first")], "running");
  assert.equal(controller.snapshot().acceptedPromptToken, "sidebar-1");
  assert.equal(controller.snapshot().settledPromptToken, "sidebar-1");
  assert.equal(controller.snapshot().followUpQueue.items.length, 1);
});

test("queued follow-ups drain FIFO one-at-a-time only after durable normal completion", async () => {
  const backend = new Backend();
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  await controller.initialize();
  await controller.handle({ type: "userPrompt", prompt: "first", requestToken: "first-token" });
  backend.emit([chat(1, "user", "first")], "running");

  await controller.handle({ type: "userPrompt", prompt: "second", requestToken: "second-token" });
  await controller.handle({ type: "userPrompt", prompt: "third", requestToken: "third-token" });
  assert.deepEqual(controller.snapshot().followUpQueue.items.map((item) => item.preview), ["second", "third"]);
  assert.deepEqual(backend.turns, []);
  backend.emit([chat(1, "user", "first")], "running");
  assert.equal(controller.snapshot().followUpQueue.items.length, 2, "replayed user events never acknowledge a queued head");

  backend.emit([
    chat(2, "assistant", "first answer"),
    { sequence_no: 3, event_kind: "turn", marker: "close_ok", body: JSON.stringify({ status: "completed" }) },
  ], "idle");
  await settleUntil(() => backend.turns.length === 1);
  assert.deepEqual(backend.turns, ["second"]);
  assert.equal(controller.snapshot().followUpQueue.items[0].dispatching, true);
  assert.equal(controller.snapshot().followUpQueue.items.length, 2, "head stays until its durable user event");

  backend.emit([chat(4, "user", "second")], "running");
  assert.deepEqual(controller.snapshot().followUpQueue.items.map((item) => item.preview), ["third"]);
  backend.emit([
    chat(5, "assistant", "second answer"),
    { sequence_no: 6, event_kind: "turn", marker: "close_ok", body: JSON.stringify({ status: "completed" }) },
  ], "idle");
  await settleUntil(() => backend.turns.length === 2);
  assert.deepEqual(backend.turns, ["second", "third"]);
  backend.emit([chat(7, "user", "third")], "running");
  assert.equal(controller.snapshot().followUpQueue.items.length, 0);
});

test("failure, approval timeout, writer conflict, interrupt, kill, and uncertain transport pause with head intact", async () => {
  async function queuedController() {
    const backend = new Backend();
    const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
    await controller.initialize();
    await controller.handle({ type: "userPrompt", prompt: "first" });
    backend.emit([chat(1, "user", "first")], "running");
    await controller.handle({ type: "userPrompt", prompt: "keep me" });
    return { backend, controller };
  }

  {
    const { backend, controller } = await queuedController();
    backend.emit([{ sequence_no: 2, event_kind: "turn", marker: "close_fail", code: "BACKEND_ERROR" }], "idle");
    assert.deepEqual({ reason: controller.snapshot().followUpQueue.pauseReason, size: controller.snapshot().followUpQueue.items.length }, { reason: "failure", size: 1 });
  }
  {
    const { backend, controller } = await queuedController();
    backend.emit([{ sequence_no: 2, event_kind: "approval", marker: "timed_out", correlation_id: "a", code: "DENIED_APPROVAL_TIMEOUT" }], "running");
    assert.deepEqual({ reason: controller.snapshot().followUpQueue.pauseReason, size: controller.snapshot().followUpQueue.items.length }, { reason: "approval_timeout", size: 1 });
  }
  {
    const { backend, controller } = await queuedController();
    backend.emit([{ sequence_no: 2, event_kind: "tool_call", marker: "close_fail", correlation_id: "w", code: "DENIED_WORKSPACE_WRITER_BUSY" }], "idle");
    assert.deepEqual({ reason: controller.snapshot().followUpQueue.pauseReason, size: controller.snapshot().followUpQueue.items.length }, { reason: "lease_conflict", size: 1 });
  }
  {
    const { controller } = await queuedController();
    await controller.handle({ type: "interrupt" });
    assert.deepEqual({ reason: controller.snapshot().followUpQueue.pauseReason, size: controller.snapshot().followUpQueue.items.length }, { reason: "interrupt", size: 1 });
  }
  {
    const { controller } = await queuedController();
    await controller.handle({ type: "kill" });
    assert.deepEqual({ reason: controller.snapshot().followUpQueue.pauseReason, size: controller.snapshot().followUpQueue.items.length }, { reason: "kill", size: 1 });
  }
  {
    const { backend, controller } = await queuedController();
    backend.callbacks.at(-1)?.onError?.(new Error("transport uncertain"));
    assert.deepEqual({ reason: controller.snapshot().followUpQueue.pauseReason, size: controller.snapshot().followUpQueue.items.length }, { reason: "uncertain_transport", size: 1 });
  }
});

test("controller enforces the queue cap and keeps the eleventh composer token rejected", async () => {
  const backend = new Backend();
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  await controller.initialize();
  await controller.handle({ type: "userPrompt", prompt: "first" });
  backend.emit([chat(1, "user", "first")], "running");
  for (let index = 1; index <= 10; index += 1) {
    await controller.handle({ type: "userPrompt", prompt: `queued ${index}`, requestToken: `queue-${index}` });
  }
  await controller.handle({ type: "userPrompt", prompt: "must remain in composer", requestToken: "queue-11" });
  const snapshot = controller.snapshot();
  assert.equal(snapshot.followUpQueue.items.length, 10);
  assert.equal(snapshot.banner?.code, "DENIED_FOLLOW_UP_QUEUE_FULL");
  assert.equal(snapshot.settledPromptToken, "queue-11");
  assert.notEqual(snapshot.queuedPromptToken, "queue-11");
});

test("canceling the ephemeral-loss modal preserves the paused queue and conversation", async () => {
  const backend = new Backend();
  let allowLoss = false;
  const confirmations: Array<{ hasDraft: boolean; queuedCount: number; contextCount: number; imageCount: number }> = [];
  const controller = new ConversationController(
    new MemoryMemento(),
    backend.factory,
    prompts({
      confirmEphemeralLoss: async (state) => {
        confirmations.push(state);
        return allowLoss;
      },
    }),
  );
  await controller.initialize();
  await controller.handle({ type: "userPrompt", prompt: "first" });
  backend.emit([chat(1, "user", "first")], "running");
  await controller.handle({ type: "userPrompt", prompt: "preserve" });
  backend.emit([{ sequence_no: 2, event_kind: "turn", marker: "close_fail", code: "BACKEND_ERROR" }], "idle");
  await controller.handle({ type: "newConversation", hasDraft: true });
  assert.deepEqual(confirmations, [{ hasDraft: true, queuedCount: 1, contextCount: 0, imageCount: 0 }]);
  assert.equal(controller.snapshot().conversation?.sessionId, "session-1");
  assert.equal(controller.snapshot().followUpQueue.items.length, 1);

  allowLoss = true;
  await controller.handle({ type: "newConversation", hasDraft: true });
  assert.equal(controller.snapshot().conversation, null);
  assert.equal(controller.snapshot().followUpQueue.items.length, 0);
});

test("legacy/null H2 metadata is labeled unknown and terminal driven replay becomes read-only", async () => {
  const memory = new MemoryMemento();
  memory.values.set(RUN_CURSORS_KEY, { session_id: "legacy-session", agent_run_id: "legacy-run" });
  const backend = new Backend();
  backend.listResponses.set("driven", {
    status: "OK",
    sessions: [{
      session_id: "legacy-session",
      engine: "local_llm",
      profile: { permission_mode: null, auth_mode: null },
      latest_run_state: "terminal",
      latest_terminal_state: "success",
      runs: [{
        id: "legacy-run",
        engine: "local_llm",
        terminal: true,
        terminal_state: "success",
        turn_state: "terminal",
        profile: { permission_mode: null, auth_mode: null },
      }],
    }],
  });
  const controller = new ConversationController(memory, backend.factory, prompts());
  await controller.initialize();
  assert.equal(controller.snapshot().conversation?.profileLegacy, true);
  assert.equal(controller.snapshot().conversation?.state, "terminal");
  assert.equal(controller.snapshot().conversation?.readOnly, true);
  assert.equal(controller.snapshot().conversation?.terminalState, "success");
});

test("daemon coming up auto-reloads capabilities on the observed poll (no manual refresh)", async () => {
  const backend = new Backend();
  let daemonDown = true;
  const factory = (callbacks: SessionClientCallbacks): SessionPort => {
    const session = backend.factory(callbacks);
    const live = session.capabilities.bind(session);
    session.capabilities = () =>
      daemonDown ? Promise.reject(new Error("no control.token (daemon not running?)")) : live();
    return session;
  };
  const controller = new ConversationController(new MemoryMemento(), factory, prompts());
  await controller.initialize();
  const renderer = new Renderer("a");
  controller.attach(renderer);
  assert.equal(renderer.snapshot?.banner?.code, "DAEMON_UNREACHABLE");
  assert.equal(renderer.snapshot?.capabilities.length, 0);

  daemonDown = false; // the user clicked Start Daemon; the daemon now answers
  await controller.refreshObserved(); // the extension's poll tick
  assert.equal(renderer.snapshot?.banner, undefined);
  assert.ok((renderer.snapshot?.capabilities.length ?? 0) > 0, "selectors fill themselves after recovery");
  controller.dispose();
});

test("daemon-restart continuation: banner + composer stay usable, the turn rebinds to the new linked leg", async () => {
  const backend = new Backend();
  let turnResponse: Record<string, unknown> = { status: "OK" };
  const factory = (callbacks: SessionClientCallbacks): SessionPort => {
    const session = backend.factory(callbacks);
    session.turn = (prompt: string) => {
      backend.turns.push(prompt);
      return Promise.resolve(turnResponse);
    };
    return session;
  };
  const controller = new ConversationController(new MemoryMemento(), factory, prompts());
  await controller.initialize();
  const renderer = new Renderer("a");
  controller.attach(renderer);
  await controller.handle({ type: "userPrompt", prompt: "first" }, "a");
  backend.emit([chat(1, "user", "first"), chat(2, "assistant", "one")], "idle");

  // The daemon dies; on reconnect the pump replays the orphan-sweep finalization.
  backend.emit(
    [{ sequence_no: 3, event_kind: "finalization", marker: "close_fail",
       code: "ORPHAN_SWEEP_FINALIZED", summary: "unrelated terminal summary" }],
    "terminal",
  );
  const restarted = renderer.snapshot!;
  assert.equal(restarted.banner?.code, "DAEMON_RESTART");
  assert.match(restarted.banner?.message ?? "", /send a message to continue/i);
  assert.equal(restarted.conversation?.state, "terminal");
  assert.equal(restarted.conversation?.resumable, true);
  assert.equal(restarted.conversation?.readOnly, false, "composer must stay usable");
  const oldRunId = restarted.conversation!.agentRunId;
  const clientsBefore = backend.clients;

  // The next send resumes: the daemon answers with a NEW linked leg id; the controller rebinds.
  turnResponse = { status: "OK", agent_run_id: "run-2-resumed", resumed_from: oldRunId, turn_state: "running" };
  await controller.handle({ type: "userPrompt", prompt: "again" }, "a");
  const resumed = renderer.snapshot!;
  assert.equal(resumed.conversation?.agentRunId, "run-2-resumed");
  assert.equal(resumed.conversation?.state, "running");
  assert.ok(!resumed.conversation?.resumable);
  assert.notEqual(resumed.banner?.code, "DAEMON_RESTART");
  assert.equal(backend.clients, clientsBefore + 1, "a fresh client binds the new leg");
  assert.deepEqual(backend.resumes.at(-1), ["run-2-resumed", "session-1"]);
  // Leg-1 transcript is retained in the live view across the rebind.
  assert.deepEqual(
    resumed.transcript.map((entry) => [entry.role, entry.text]),
    [["user", "first"], ["assistant", "one"]],
  );
  controller.dispose();
});

test("legacy row: the daemon's resumable=false verdict beats the event heuristic (no false continue invite)", async () => {
  const backend = new Backend();
  backend.listResponses.set("driven", {
    status: "OK",
    sessions: [{
      session_id: "session-legacy",
      engine: "claude",
      controller: "driven",
      resumable: false,
      latest_run_state: "terminal",
      latest_terminal_state: "failure",
      runs: [{
        id: "run-legacy", terminal: true, terminal_state: "failure", turn_state: "terminal",
        profile: { model: "opus", permission_mode: "ask", auth_mode: "subscription" },
      }],
    }],
  });
  const memento = new MemoryMemento();
  await memento.update(RUN_CURSORS_KEY, { session_id: "session-legacy", agent_run_id: "run-legacy" });
  const controller = new ConversationController(memento, backend.factory, prompts());
  await controller.initialize();
  const renderer = new Renderer("a");
  controller.attach(renderer);
  const restored = renderer.snapshot!;
  assert.equal(restored.conversation?.engine, "claude", "engine restores from daemon metadata");
  assert.equal(restored.conversation?.resumable, false);
  assert.equal(restored.conversation?.readOnly, true);
  // Legacy prose alone is not an orphan-sweep signal and must not flip resumability back on.
  backend.emit(
    [{ sequence_no: 1, event_kind: "finalization", marker: "close_fail",
       summary: "orphan-sweep force-finalize: owning daemon dead" }],
    "terminal",
  );
  const after = renderer.snapshot!;
  assert.equal(after.conversation?.resumable, false);
  assert.equal(after.conversation?.readOnly, true);
  assert.notEqual(after.banner?.code, "DAEMON_RESTART");
  controller.dispose();
});

test("governed context crosses start and turn envelopes exactly, clears only on durable acceptance, and survives denial", async () => {
  const backend = new Backend();
  backend.capabilitiesResponse = {
    status: "OK",
    engines: [localCapability({ features: {
      streaming: false, image_attachments: false, governed_context: true, diff_snapshots: false, writer_leasing: false,
    } })],
  };
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  const renderer = new Renderer("context");
  await controller.initialize();
  controller.attach(renderer);
  assert.equal(controller.addContext(fileContext()), true);
  await controller.handle({ type: "userPrompt", prompt: "first", requestToken: "p1" });
  assert.deepEqual(backend.starts[0].context_refs, [fileContext().ref]);
  assert.equal(renderer.snapshot?.contextChips.length, 1, "request acknowledgement alone does not discard context");
  backend.emit([chat(1, "user", "first")], "idle");
  assert.equal(renderer.snapshot?.contextChips.length, 0, "durable user event clears the submitted context");

  assert.equal(controller.addContext(selectionContext()), true);
  backend.turnResponse = { status: "DENIED", code: "DENIED_CONTEXT_INVALID", message: "context denied" };
  await controller.handle({ type: "userPrompt", prompt: "second", requestToken: "p2" });
  assert.deepEqual(backend.turnEnvelopes.at(-1)?.context_refs, [selectionContext().ref]);
  assert.equal(renderer.snapshot?.contextChips.length, 1, "daemon denial preserves the complete composer context envelope");
  assert.equal(renderer.snapshot?.banner?.code, "DENIED_CONTEXT_INVALID");

  backend.turnResponse = { status: "OK" };
  await controller.handle({ type: "userPrompt", prompt: "second", requestToken: "p3" });
  backend.emit([chat(2, "user", "second")], "idle");
  assert.equal(renderer.snapshot?.contextChips.length, 0);
  controller.dispose();
});

test("Test Extension traversal reference reaches the normal SessionClient start envelope and preserves zero-session denial", async () => {
  const backend = new Backend();
  backend.capabilitiesResponse = { status: "OK", engines: [claudeCapability()] };
  backend.startResponse = { status: "DENIED", code: "DENIED_CONTEXT_INVALID", message: "context denied" };
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  const renderer = new Renderer("traversal");
  await controller.initialize();
  controller.attach(renderer);
  await controller.handle({
    type: "profileChange", engine: "claude",
    profile: {
      model: "account-model-a", reasoning_effort: "low", max_turns: 8,
      permission_mode: "plan", auth_mode: "subscription",
    },
  });
  const invalid: StagedContext = {
    ref: { id: "te-traversal", kind: "file", source_path: "../outside.txt" },
    label: "outside.txt",
    bytes: 0,
  };
  assert.equal(controller.addContext(invalid), true, "host envelope accepts the bounded opaque reference");
  await controller.handle({ type: "userPrompt", prompt: "bounded traversal probe", requestToken: "te-traversal" });
  assert.deepEqual(backend.starts[0].context_refs, [invalid.ref]);
  assert.equal(renderer.snapshot?.banner?.code, "DENIED_CONTEXT_INVALID");
  assert.equal(renderer.snapshot?.conversation, null, "daemon denial creates no accepted session");
  assert.deepEqual(renderer.snapshot?.contextChips.map((item) => item.sourcePath), ["../outside.txt"]);
  controller.dispose();
});

test("capability-absent old daemon keeps basic chat usable while governed context stays dark", async () => {
  const backend = new Backend();
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  const renderer = new Renderer("dark");
  await controller.initialize();
  controller.attach(renderer);
  assert.equal(controller.supportsGovernedContext(), false);
  assert.equal(controller.addContext(fileContext()), false);
  assert.equal(renderer.snapshot?.contextChips.length, 0);
  assert.equal(renderer.snapshot?.banner?.code, "DENIED_CONTEXT_UNSUPPORTED");
  await controller.handle({ type: "userPrompt", prompt: "plain chat still works" });
  assert.equal(backend.starts.length, 1);
  assert.equal(backend.starts[0].context_refs, undefined);
  controller.dispose();
});

test("image attachments cross start/turn envelopes, queue ownership, and denial preservation without raw bytes", async () => {
  const backend = new Backend();
  backend.capabilitiesResponse = {
    status: "OK",
    engines: [localCapability({ features: {
      streaming: false, image_attachments: true, governed_context: false, diff_snapshots: false, writer_leasing: false,
    } })],
  };
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  const renderer = new Renderer("images");
  await controller.initialize();
  controller.attach(renderer);
  assert.equal(controller.addImage(stagedImage()), true);
  await controller.handle({ type: "userPrompt", prompt: "inspect", requestToken: "image-start" });
  assert.deepEqual(backend.starts[0].attachments, [stagedImage().ref]);
  for (const attachment of backend.starts[0].attachments ?? []) {
    assert.equal("data" in attachment || "base64" in attachment || "bytes_base64" in attachment, false);
  }
  assert.equal(renderer.snapshot?.imageChips.length, 1);
  backend.emit([chat(1, "user", "inspect")], "idle");
  assert.equal(renderer.snapshot?.imageChips.length, 0);

  assert.equal(controller.addImage(stagedImage("image-2")), true);
  backend.turnResponse = { status: "DENIED", code: "DENIED_ATTACHMENT_INVALID" };
  await controller.handle({ type: "userPrompt", prompt: "again", requestToken: "image-denied" });
  assert.deepEqual(backend.turnEnvelopes.at(-1)?.attachments, [stagedImage("image-2").ref]);
  assert.equal(renderer.snapshot?.imageChips.length, 1, "denial preserves staged image metadata");
  controller.dispose();
});

test("transcript-seeded continuation shows reduced fidelity without replaying old artifact bytes", async () => {
  const memory = new MemoryMemento();
  await memory.update(RUN_CURSORS_KEY, { session_id: "session-1", agent_run_id: "run-old" });
  const backend = new Backend();
  backend.listResponse = {
    status: "OK",
    sessions: [{
      session_id: "session-1", controller: "driven", engine: "claude", latest_run_id: "run-old",
      latest_run_state: "terminal", latest_terminal_state: "failure", resumable: true,
      runs: [{ id: "run-old", terminal: true, terminal_state: "failure", turn_state: "terminal",
        profile: { model: "opus", permission_mode: "plan", auth_mode: "subscription" } }],
    }],
  };
  backend.capabilitiesResponse = { status: "OK", engines: [localCapability({ id: "claude", auth_modes: ["subscription"] })] };
  backend.turnResponse = { status: "OK", agent_run_id: "run-new", transcript_seeded: true };
  const controller = new ConversationController(memory, backend.factory, prompts());
  const renderer = new Renderer("fidelity");
  await controller.initialize();
  controller.attach(renderer);
  await controller.handle({ type: "userPrompt", prompt: "continue" });
  assert.equal(renderer.snapshot?.conversation?.continuationNotice?.mode, "reduced");
  assert.match(renderer.snapshot?.conversation?.continuationNotice?.message ?? "", /old artifact bytes were not replayed/i);
  const notice = renderer.snapshot?.conversation?.continuationNotice as unknown as Record<string, unknown> | undefined;
  assert.equal(notice ? Object.hasOwn(notice, "snapshot_ref") : false, false);
  for (const entry of renderer.snapshot?.history.entries ?? []) {
    assert.equal(Object.hasOwn(entry, "snapshot_ref"), false);
  }
  controller.dispose();
});

test("degraded restoration (daemon down at activation) heals on the first successful poll", async () => {
  for (const healedState of ["idle", "running"] as const) {
    const backend = new Backend();
    let daemonDown = true;
    backend.listResponses.set("driven", {
      status: "OK",
      sessions: [{
        session_id: `session-${healedState}`,
        engine: "claude",
        controller: "driven",
        resumable: true,
        latest_run_state: healedState,
        runs: [{
          id: `run-${healedState}`, terminal: false, turn_state: healedState,
          profile: { model: "opus", permission_mode: "ask", auth_mode: "subscription" },
        }],
      }],
    });
    const factory = (callbacks: SessionClientCallbacks): SessionPort => {
      const session = backend.factory(callbacks);
      const live = session.list.bind(session);
      session.list = (args?: { controller?: "driven" | "observed"; limit?: number }) =>
        daemonDown ? Promise.reject(new Error("no control.token (daemon not running?)")) : live(args);
      return session;
    };
    const memento = new MemoryMemento();
    await memento.update(RUN_CURSORS_KEY, { session_id: `session-${healedState}`, agent_run_id: `run-${healedState}` });
    const controller = new ConversationController(memento, factory, prompts());
    await controller.initialize();
    const renderer = new Renderer(healedState);
    controller.attach(renderer);
    assert.notEqual(renderer.snapshot?.conversation?.engine, "claude");
    assert.equal(renderer.snapshot?.conversation?.state, "idle", "degraded restore must not claim a running turn");
    assert.equal(renderer.snapshot?.conversation?.readOnly, true, "degraded restore is fail-closed until metadata heals");

    daemonDown = false;
    await controller.refreshObserved();
    const healed = renderer.snapshot!;
    assert.equal(healed.conversation?.engine, "claude");
    assert.equal(healed.selectedEngine, "claude");
    assert.equal(healed.selectedProfile.model, "opus");
    assert.equal(healed.conversation?.state, healedState);
    assert.equal(healed.conversation?.resumable, true);
    assert.equal(healed.conversation?.readOnly, false);
    controller.dispose();
  }
});

test("continue-previous dropdown lists driven conversations and attaches the selected one", async () => {
  const backend = new Backend();
  backend.listResponses.set("driven", {
    status: "OK",
    sessions: [
      {
        session_id: "session-new", latest_run_id: "run-new", engine: "local_llm", controller: "driven",
        created_at: "2026-07-11T10:00:00+00:00", resumable: true,
        profile: { requested_model: "qwen", permission_mode: "plan", auth_mode: "none" },
        latest_run_state: "terminal", latest_terminal_state: "success",
        runs: [{ id: "run-new", terminal: true, terminal_state: "success", turn_state: "terminal",
                 profile: { model: "qwen", permission_mode: "plan", auth_mode: "none" } }],
      },
      {
        session_id: "session-old", latest_run_id: "run-old", engine: "claude", controller: "driven",
        created_at: "2026-07-11T02:31:00+00:00", resumable: true,
        profile: { requested_model: "opus", permission_mode: "ask", auth_mode: "subscription" },
        latest_run_state: "terminal", latest_terminal_state: "failure",
        runs: [{ id: "run-old", terminal: true, terminal_state: "failure", turn_state: "terminal",
                 profile: { model: "opus", permission_mode: "ask", auth_mode: "subscription" } }],
      },
    ],
  });
  const controller = new ConversationController(new MemoryMemento(), backend.factory, prompts());
  await controller.initialize();
  const renderer = new Renderer("a");
  controller.attach(renderer);
  await controller.refreshObserved();
  const listed = renderer.snapshot!.conversations;
  assert.equal(listed.length, 2);
  assert.match(listed[1].label, /claude · opus · 07-11 02:31/);

  await controller.handle({ type: "conversationSelect", sessionId: "session-old", agentRunId: "run-old" }, "a");
  const attached = renderer.snapshot!;
  assert.equal(attached.conversation?.sessionId, "session-old");
  assert.equal(attached.conversation?.agentRunId, "run-old");
  assert.equal(attached.conversation?.engine, "claude");
  assert.equal(attached.selectedProfile.model, "opus");
  assert.equal(attached.conversation?.resumable, true);
  assert.equal(attached.conversation?.readOnly, false);
  assert.deepEqual(backend.resumes.at(-1), ["run-old", "session-old"]);
  assert.ok(attached.conversations.find((entry) => entry.sessionId === "session-old")?.active);
  controller.dispose();
});
