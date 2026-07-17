import assert from "node:assert/strict";
import { test } from "node:test";

import {
  ChatPanelManagerCore,
  EditorPanelPort,
  ManagedController,
  ManagedRenderer,
  PanelManagerDependencies,
  RendererActions,
} from "../../extension/src/chatPanelManager";
import { PanelMemento, PanelStateStore, WorkspaceMemento } from "../../extension/src/panelState";
import { SessionIndex, SessionListClient } from "../../extension/src/sessionIndex";
import type { ConversationSnapshot, HostToWebview } from "../../extension/src/webviewProtocol";
import type { StagedContext } from "../../extension/src/contextStager";
import type { StagedImage } from "../../extension/src/imageStager";

class MemoryWorkspace implements WorkspaceMemento {
  readonly values = new Map<string, unknown>();
  get<T>(key: string, defaultValue?: T): T | undefined {
    const value = this.values.has(key) ? this.values.get(key) : defaultValue;
    return value === undefined ? undefined : structuredClone(value) as T;
  }
  update(key: string, value: unknown): PromiseLike<void> {
    if (value === undefined) this.values.delete(key);
    else this.values.set(key, structuredClone(value));
    return Promise.resolve();
  }
}

class ListClient implements SessionListClient {
  calls = 0;
  response: Record<string, unknown> | Error = { status: "OK", sessions: [] };
  async list(): Promise<Record<string, unknown>> {
    this.calls += 1;
    if (this.response instanceof Error) throw this.response;
    return this.response;
  }
}

class FakePanel implements EditorPanelPort {
  title = "";
  reveals = 0;
  disposed = false;
  private readonly disposeListeners = new Set<() => void>();
  private readonly activeListeners = new Set<() => void>();

  reveal(): void { this.reveals += 1; }
  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    for (const listener of this.disposeListeners) listener();
  }
  setTitle(title: string): void { this.title = title; }
  onDidDispose(listener: () => void) {
    this.disposeListeners.add(listener);
    return { dispose: () => this.disposeListeners.delete(listener) };
  }
  onDidActivate(listener: () => void) {
    this.activeListeners.add(listener);
    return { dispose: () => this.activeListeners.delete(listener) };
  }
  activate(): void { for (const listener of this.activeListeners) listener(); }
}

class FakeController implements ManagedController {
  initialized = 0;
  refreshed = 0;
  failures: string[] = [];
  disposed = false;
  queued: string[] = [];
  banner: { message: string; code?: string } | undefined;
  refreshBanner: { message: string; code?: string } | undefined;
  composerEnvelope: string | undefined;
  contexts: StagedContext[] = [];
  images: StagedImage[] = [];
  activeSession = false;
  historyOpens: Array<[string, string, "driven" | "observed"]> = [];
  handled: import("../../extension/src/webviewProtocol").WebviewToHost[] = [];
  envelopeOrder: string[] = [];
  selectedEngine = "local_llm";

  constructor(
    readonly conversationId: string,
    readonly memento: PanelMemento,
    private readonly claudeOnlyAttachments = false,
  ) {}

  async initialize(): Promise<void> { this.initialized += 1; }
  async refreshObserved(): Promise<void> { this.refreshed += 1; }
  sessionIndexFailed(error: Error): void { this.failures.push(error.message); }
  dispose(): void { this.disposed = true; }
  supportsGovernedContext(): boolean { return !this.claudeOnlyAttachments || this.selectedEngine === "claude"; }
  addContext(item: StagedContext): boolean {
    this.envelopeOrder.push("context");
    if (!this.supportsGovernedContext()) return false;
    this.contexts.push(item);
    return true;
  }
  removeContext(id: string): void { this.contexts = this.contexts.filter((item) => item.ref.id !== id); }
  contextCount(): number { return this.contexts.length; }
  supportsImages(): boolean { return !this.claudeOnlyAttachments || this.selectedEngine === "claude"; }
  addImage(item: StagedImage): boolean {
    this.envelopeOrder.push("image");
    if (!this.supportsImages()) return false;
    this.images.push(item);
    return true;
  }
  removeImage(id: string): void { this.images = this.images.filter((item) => item.ref.id !== id); }
  imageCount(): number { return this.images.length; }
  pendingDiff(): undefined { return undefined; }
  markDiffCorrupt(): void {}
  async resolveDiff(): Promise<boolean> { return false; }
  isGenuinelyEmpty(): boolean { return !this.activeSession && this.contexts.length === 0 && this.images.length === 0 && this.queued.length === 0; }
  async openHistoryConversation(sessionId: string, agentRunId: string, controller: "driven" | "observed"): Promise<void> {
    this.activeSession = true;
    this.historyOpens.push([sessionId, agentRunId, controller]);
  }
  async handle(message: import("../../extension/src/webviewProtocol").WebviewToHost): Promise<void> {
    this.handled.push(message);
    if (message.type === "refreshCapabilities") this.banner = this.refreshBanner;
    if (message.type === "profileChange") {
      this.envelopeOrder.push("profileChange");
      this.selectedEngine = message.engine;
    }
  }
  /** Interface-conformance stub; observation data is not exercised by this suite. */
  testExtensionObservation() {
    return {
      profile: { permission_mode: "plan" as const, auth_mode: "none" as const },
      profileAttested: false,
      stream: { sequences: [], ordered: true, durableReplacements: 0, suppressed: false },
      approvalEvents: [],
      turnEvents: [],
    };
  }
  writerConflict(envelope: string): void {
    this.composerEnvelope = envelope;
    this.banner = { message: "Workspace writer is busy", code: "DENIED_WORKSPACE_WRITER_BUSY" };
  }
  snapshot(): ConversationSnapshot {
    return {
      capabilities: [],
      selectedEngine: "local_llm",
      selectedProfile: { permission_mode: "plan", auth_mode: "none" },
      selectorsLocked: false,
      conversation: null,
      conversations: [],
      transcript: [],
      timeline: [],
      cards: [],
      approvals: [],
      followUpQueue: {
        items: this.queued.map((preview, index) => ({ id: String(index), preview, dispatching: false })),
        paused: false,
      },
      observedSessions: [],
      history: { entries: [], truncated: false },
      contextChips: this.contexts.map((item) => ({
        id: item.ref.id, kind: item.ref.kind, label: item.label, sourcePath: item.ref.source_path, bytes: item.bytes,
      })),
      imageChips: this.images.map((item) => ({
        id: item.ref.id, label: item.label, bytes: item.ref.bytes, mediaType: item.ref.media_type, sha256: item.ref.sha256,
      })),
      pendingPrompt: false,
      ...(this.banner ? { banner: this.banner } : {}),
    };
  }
}

class FakeRenderer implements ManagedRenderer {
  disposed = false;
  focusCount = 0;
  readonly messages: HostToWebview[] = [];

  constructor(
    readonly id: string,
    readonly conversationId: string,
    readonly controller: FakeController,
    readonly actions: RendererActions,
  ) {}

  focusInput(): void { this.focusCount += 1; }
  postMessage(message: HostToWebview): void { this.messages.push(message); }
  dispose(): void { this.disposed = true; }
  newConversation(): void { this.actions.newConversation(); }
  draft(hasDraft: boolean): void { this.actions.draftChanged(hasDraft); }
  writerConflict(envelope: string): void { this.controller.writerConflict(envelope); }
}

interface Harness {
  workspace: MemoryWorkspace;
  state: PanelStateStore;
  list: ListClient;
  index: SessionIndex;
  manager: ChatPanelManagerCore;
  panels: FakePanel[];
  controllers: FakeController[];
  renderers: FakeRenderer[];
  setAllowLoss(value: boolean): void;
}

function harness(options: {
  listResponse?: Record<string, unknown> | Error;
  claudeOnlyAttachments?: boolean;
} = {}): Harness {
  const workspace = new MemoryWorkspace();
  let id = 0;
  let clock = 1_000;
  const state = new PanelStateStore(workspace, () => `conversation-${++id}`, () => ++clock);
  const list = new ListClient();
  if (options.listResponse) list.response = options.listResponse;
  const index = new SessionIndex(list);
  const panels: FakePanel[] = [];
  const controllers: FakeController[] = [];
  const renderers: FakeRenderer[] = [];
  let allowLoss = true;
  const dependencies: PanelManagerDependencies = {
    createPanel: () => {
      const panel = new FakePanel();
      panels.push(panel);
      return panel;
    },
    createController: (conversationId, memento) => {
      const controller = new FakeController(conversationId, memento, options.claudeOnlyAttachments === true);
      controllers.push(controller);
      return controller;
    },
    createRenderer: (rendererId, conversationId, _panel, controller, actions) => {
      const renderer = new FakeRenderer(rendererId, conversationId, controller as FakeController, actions);
      renderers.push(renderer);
      return renderer;
    },
    confirmLoss: async () => allowLoss,
  };
  return {
    workspace,
    state,
    list,
    index,
    manager: new ChatPanelManagerCore(state, index, dependencies),
    panels,
    controllers,
    renderers,
    setAllowLoss: (value) => { allowLoss = value; },
  };
}

/** Await a fire-and-forget manager effect by its observable postcondition. */
async function settleUntil(predicate: () => boolean): Promise<void> {
  for (let index = 0; index < 100; index += 1) {
    if (predicate()) return;
    await Promise.resolve();
  }
  assert.fail("manager effect did not settle");
}

test("every new conversation gets an isolated panel/controller/client-facing renderer", async () => {
  const h = harness();
  const first = await h.manager.newConversation();
  const second = await h.manager.newConversation();
  assert.notStrictEqual(first, second);
  assert.deepEqual(h.manager.openConversationIds(), [first, second]);
  assert.equal(h.controllers.length, 2);
  assert.notStrictEqual(h.controllers[0], h.controllers[1]);
  assert.notStrictEqual(h.renderers[0].id, h.renderers[1].id);
  h.controllers[0].queued.push("first-only");
  assert.deepEqual(h.controllers[0].snapshot().followUpQueue.items.map((item) => item.preview), ["first-only"]);
  assert.deepEqual(h.controllers[1].snapshot().followUpQueue.items, []);

  h.renderers[0].newConversation();
  await settleUntil(() => h.manager.openConversationIds().length === 3);
  assert.equal(h.manager.openConversationIds().length, 3, "in-webview New always creates another tab");
  assert.equal(h.panels[0].disposed, false, "origin remains open");
});

test("open/focus uses MRU while explicit new never reuses an empty panel", async () => {
  const h = harness();
  const first = await h.manager.newConversation();
  const second = await h.manager.newConversation();
  h.panels[0].activate();
  assert.equal(h.manager.activeConversationId(), first);
  assert.equal(await h.manager.openOrFocus(), first);
  assert.ok(h.renderers[0].focusCount > 0);
  const third = await h.manager.newConversation();
  assert.notEqual(third, first);
  assert.notEqual(third, second);
});

test("writer conflict and complete composer envelope remain isolated to the originating panel", async () => {
  const h = harness();
  await h.manager.newConversation();
  await h.manager.newConversation();
  const envelope = "exact prompt\nwith spacing  and metadata-token";
  h.renderers[0].writerConflict(envelope);
  assert.equal(h.controllers[0].snapshot().banner?.code, "DENIED_WORKSPACE_WRITER_BUSY");
  assert.equal(h.controllers[0].composerEnvelope, envelope);
  assert.equal(h.controllers[1].snapshot().banner, undefined);
  assert.equal(h.controllers[1].composerEnvelope, undefined);
});

test("one shared poll fans out to all panels and panel creation adds no list request", async () => {
  const h = harness({ listResponse: { status: "OK", sessions: [{ session_id: "s", title: "Title" }] } });
  await h.manager.initialize();
  assert.equal(h.list.calls, 1);
  await h.manager.newConversation();
  await h.manager.newConversation();
  await h.manager.newConversation();
  assert.equal(h.list.calls, 1);
  const before = h.controllers.map((controller) => controller.refreshed);
  await h.manager.refreshSessions();
  assert.equal(h.list.calls, 2);
  assert.deepEqual(h.controllers.map((controller, index) => controller.refreshed - before[index]), [1, 1, 1]);
});

test("a transient manager initialization failure remains retryable", async () => {
  const h = harness();
  const initializeState = h.state.initialize.bind(h.state);
  let attempts = 0;
  h.state.initialize = async () => {
    attempts += 1;
    if (attempts === 1) throw new Error("transient workspace state failure");
    await initializeState();
  };
  await assert.rejects(h.manager.initialize(), /transient workspace state failure/);
  await assert.doesNotReject(h.manager.initialize());
  assert.equal(attempts, 2);
  assert.equal(h.list.calls, 1);
});

test("serializer revival restores metadata during daemon-down degradation", async () => {
  const h = harness({ listResponse: new Error("daemon down") });
  await h.state.initialize();
  const record = h.state.create("persisted-panel");
  await h.state.scopedMemento(record.conversationId).update("kaizen.runCursors", {
    session_id: "session-1",
    agent_run_id: "run-1",
    profile_hash: "hash-1",
  });
  const supplied = new FakePanel();
  const revived = await h.manager.revive(supplied, { conversationId: record.conversationId });
  assert.equal(revived, record.conversationId);
  assert.equal(h.manager.openConversationIds()[0], record.conversationId);
  assert.match(h.controllers[0].failures[0], /daemon down/);
  assert.deepEqual(h.controllers[0].memento.get("kaizen.runCursors"), {
    session_id: "session-1",
    agent_run_id: "run-1",
    profile_hash: "hash-1",
  });
});

test("dispose records recently closed; reopen creates a fresh controller for the same metadata", async () => {
  const h = harness();
  const conversationId = await h.manager.newConversation();
  const firstController = h.controllers[0];
  const firstRenderer = h.renderers[0];
  h.panels[0].dispose();
  assert.deepEqual(h.manager.openConversationIds(), []);
  assert.equal(firstController.disposed, true);
  assert.equal(firstRenderer.disposed, true);
  assert.equal(h.state.recentlyClosed()[0].conversationId, conversationId);
  assert.equal(await h.manager.reopenClosed(), conversationId);
  assert.equal(h.controllers.length, 2);
  assert.notEqual(h.controllers[1], firstController);
  assert.equal(h.state.get(conversationId)?.closedAt, undefined);
});

test("controlled close cancellation preserves draft, queue, panel, and controller", async () => {
  const h = harness();
  const conversationId = await h.manager.newConversation();
  h.renderers[0].draft(true);
  h.controllers[0].queued.push("keep queued");
  h.setAllowLoss(false);
  assert.equal(await h.manager.closePanel(conversationId), false);
  assert.equal(h.panels[0].disposed, false);
  assert.equal(h.controllers[0].disposed, false);
  assert.equal(h.controllers[0].queued.length, 1);
  h.setAllowLoss(true);
  assert.equal(await h.manager.closePanel(conversationId), true);
  assert.equal(h.panels[0].disposed, true);
});

test("manager shutdown disposes runtime objects without falsely marking tabs recently closed", async () => {
  const h = harness();
  const conversationId = await h.manager.newConversation();
  h.manager.dispose();
  assert.equal(h.controllers[0].disposed, true);
  assert.equal(h.renderers[0].disposed, true);
  assert.equal(h.state.get(conversationId)?.closedAt, undefined);
});

test("history selection reuses only a genuinely empty active panel and otherwise opens a new tab", async () => {
  const h = harness({ listResponse: {
    status: "OK",
    sessions: [
      { session_id: "driven-1", controller: "driven", latest_run_id: "run-1", latest_run_state: "terminal", resumable: true,
        created_at: "2026-07-11T10:00:00Z", runs: [{ id: "run-1", terminal: true }] },
      { session_id: "observed-1", controller: "observed", latest_run_id: "run-o", latest_run_state: "terminal",
        created_at: "2026-07-10T10:00:00Z", runs: [{ id: "run-o", terminal: true }] },
    ],
  } });
  const empty = await h.manager.newConversation();
  await h.manager.refreshSessions();
  assert.equal(await h.manager.openHistory("driven-1", "run-1"), empty);
  assert.deepEqual(h.controllers[0].historyOpens, [["driven-1", "run-1", "driven"]]);
  assert.ok(h.renderers[0].messages.some((message) => message.type === "showChat"));

  const second = await h.manager.openHistory("observed-1", "run-o");
  assert.notEqual(second, empty, "a panel with a daemon session is never reused");
  assert.equal(h.controllers.length, 2);
  assert.deepEqual(h.controllers[1].historyOpens, [["observed-1", "run-o", "observed"]]);
});

test("draft, context chips, or queued prompts each prevent history reuse while New remains always-new", async () => {
  const h = harness({ listResponse: {
    status: "OK",
    sessions: [{ session_id: "s", controller: "driven", latest_run_id: "r", latest_run_state: "idle",
      created_at: "2026-07-11T10:00:00Z", runs: [{ id: "r", terminal: false }] }],
  } });
  await h.manager.newConversation();
  await h.manager.refreshSessions();
  h.renderers[0].draft(true);
  const fromDraft = await h.manager.openHistory("s", "r");
  assert.equal(h.controllers.length, 2);
  assert.notEqual(fromDraft, h.controllers[0].conversationId);

  const fresh = await h.manager.newConversation();
  h.controllers.at(-1)!.contexts.push(fileContextForManager("context"));
  const fromContext = await h.manager.openHistory("s", "r");
  assert.notEqual(fromContext, fresh);
  assert.equal(h.controllers.length, 4);

  const queued = await h.manager.newConversation();
  h.controllers.at(-1)!.queued.push("keep me");
  const fromQueue = await h.manager.openHistory("s", "r");
  assert.notEqual(fromQueue, queued);
  assert.equal(h.controllers.length, 6);
});

test("Test Extension driver uses the normal manager controller messages and a read-only snapshot sink", async () => {
  const h = harness();
  const context = fileContextForManager("te-context");
  const image: StagedImage = {
    ref: {
      id: "te-image", kind: "image", artifact_ref: "sha256/aa", sha256: "a".repeat(64),
      bytes: 8, media_type: "image/png", name: "te.png",
    },
    label: "te.png",
  };
  const conversationId = await h.manager.testExtensionDrive(
    "claude",
    { model: "discovered", reasoning_effort: "high", max_turns: 4, permission_mode: "ask", auth_mode: "subscription" },
    "bounded acceptance prompt",
    [context],
    [image],
  );
  assert.deepEqual(h.controllers[0].handled, [
    {
      type: "profileChange", engine: "claude",
      profile: { model: "discovered", reasoning_effort: "high", max_turns: 4, permission_mode: "ask", auth_mode: "subscription" },
    },
    { type: "userPrompt", prompt: "bounded acceptance prompt", requestToken: `te-${conversationId}` },
  ]);
  assert.deepEqual(h.controllers[0].contexts, [context]);
  assert.deepEqual(h.controllers[0].images, [image]);
  const stableControllerSnapshot = h.controllers[0].snapshot();
  h.controllers[0].snapshot = () => stableControllerSnapshot;
  const first = h.manager.testExtensionSnapshot(conversationId);
  const second = h.manager.testExtensionSnapshot(conversationId);
  assert.notStrictEqual(first, second, "the manager must clone even when the controller returns one stable object");
  assert.equal(await h.manager.testExtensionClose(conversationId), true);
  assert.equal(h.controllers[0].handled.at(-1)?.type, "userPrompt", "no live daemon session was fabricated");
  assert.equal(h.panels[0].disposed, true);
  assert.equal(h.controllers[0].disposed, true);
  assert.equal(h.manager.testExtensionSnapshot(conversationId), undefined, "closed Test Extension tabs cannot leak");
});

test("Test Extension catalog refresh traverses the normal controller capability request", async () => {
  const h = harness();
  const conversationId = await h.manager.newConversation();
  h.controllers[0].refreshBanner = { message: "Capability discovery failed", code: "DENIED_PROVIDER_CAPACITY" };
  const nonOk = await h.manager.testExtensionRefreshCapabilities(conversationId);
  assert.deepEqual(h.controllers[0].handled, [{ type: "refreshCapabilities" }]);
  assert.deepEqual(nonOk.banner, { message: "Capability discovery failed", code: "DENIED_PROVIDER_CAPACITY" });

  h.controllers[0].refreshBanner = { message: "Capability discovery unavailable", code: "DAEMON_UNREACHABLE" };
  const transport = await h.manager.testExtensionRefreshCapabilities(conversationId);
  assert.equal(transport.banner?.code, "DAEMON_UNREACHABLE");

  h.controllers[0].refreshBanner = undefined;
  const recovered = await h.manager.testExtensionRefreshCapabilities(conversationId);
  assert.equal(recovered.banner, undefined);
  assert.equal(await h.manager.testExtensionClose(conversationId), true);
  await assert.rejects(
    h.manager.testExtensionRefreshCapabilities(conversationId),
    /Test Extension conversation is unavailable/,
  );
});

test("Test Extension selects Claude before staging envelopes when the default local engine is dark", async () => {
  const h = harness({ claudeOnlyAttachments: true });
  const context = fileContextForManager("claude-context");
  const image: StagedImage = {
    ref: {
      id: "claude-image", kind: "image", artifact_ref: "sha256/aa", sha256: "a".repeat(64),
      bytes: 8, media_type: "image/png", name: "claude.png",
    },
    label: "claude.png",
  };
  const conversationId = await h.manager.testExtensionPrepare(
    "claude",
    { model: "discovered", reasoning_effort: "low", max_turns: 2, permission_mode: "plan", auth_mode: "subscription" },
    [context],
    [image],
  );
  assert.deepEqual(h.controllers[0].envelopeOrder, ["profileChange", "context", "image"]);
  assert.deepEqual(h.controllers[0].contexts, [context]);
  assert.deepEqual(h.controllers[0].images, [image]);
  assert.equal(await h.manager.testExtensionClose(conversationId), true);
});

function fileContextForManager(id: string): StagedContext {
  return { ref: { id, kind: "file", source_path: "README.md" }, label: "README.md", bytes: 10 };
}
