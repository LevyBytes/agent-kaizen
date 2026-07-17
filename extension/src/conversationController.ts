/** Workspace-scoped owner of the single active Kaizen conversation and all renderer snapshots. */

import { emptyState, reduceEvents, ReducerState, scopedCorrelation, ToolCard } from "./eventReducer";
import { FollowUpQueue, QueuePauseReason, QueuedFollowUp } from "./followUpQueue";
import { StagedContext, validateContextEnvelope } from "./contextStager";
import { StagedImage, validateImageEnvelope } from "./imageStager";
import { historyFromSessionList } from "./historyModel";
import { StreamReducer } from "./streamReducer";
import { DiffModelError, NegotiatedDiff, parseNegotiatedDiff } from "./diffModel";
import {
  CapabilitiesResult,
  ConversationProfile,
  EngineCapability,
  SessionClient,
  SessionClientCallbacks,
  SessionDelta,
  SessionResponseError,
  StartRequest,
  StartResult,
  TurnEnvelope,
} from "./sessionClient";
import type {
  ConversationSnapshot,
  EngineCapabilityView,
  HostToWebview,
  ObservedSessionView,
  ProfileSelection,
  WebviewToHost,
  WireEvent,
} from "./webviewProtocol";

export const RUN_CURSORS_KEY = "kaizen.runCursors";

export interface PersistedRun {
  session_id: string;
  agent_run_id: string;
  profile_hash?: string;
}

export interface MementoPort {
  get<T>(key: string, defaultValue?: T): T | undefined;
  update(key: string, value: unknown): PromiseLike<void>;
  updateCursor?(cursor: number): void;
}

export interface SessionIndexPort {
  current(): Record<string, unknown> | undefined;
}

export interface RendererPort {
  id: string;
  postMessage(message: HostToWebview): PromiseLike<boolean> | void;
}

export interface SessionPort {
  readonly runId: string | null;
  readonly session: string | null;
  capabilities(refresh?: boolean): Promise<CapabilitiesResult>;
  list(args?: { controller?: "driven" | "observed"; limit?: number }): Promise<Record<string, unknown>>;
  eventsOnce(agentRunId: string, since?: number, limit?: number): Promise<Record<string, unknown>>;
  start(request: StartRequest): Promise<StartResult>;
  resume(agentRunId: string, sessionId?: string | null): void;
  setStreaming(enabled: boolean): void;
  turn(prompt: string, envelope?: TurnEnvelope): Promise<Record<string, unknown>>;
  steer(instruction: string): Promise<Record<string, unknown>>;
  interrupt(): Promise<Record<string, unknown>>;
  close(): Promise<Record<string, unknown>>;
  kill(): Promise<Record<string, unknown>>;
  approve(
    correlationId: string,
    decision: "approve" | "deny",
    sessionId?: string,
    confirmation?: { expected_revision: number; snapshot_set_sha256: string; metadata_confirmed: boolean },
    answers?: Record<string, string>,
  ): Promise<Record<string, unknown>>;
  dispose(): void;
}

export type SessionFactory = (callbacks: SessionClientCallbacks) => SessionPort;

export interface ControllerPrompts {
  confirmFull(): Promise<boolean>;
  confirmNewConversation(): Promise<boolean>;
  confirmEphemeralLoss?(state: { hasDraft: boolean; queuedCount: number; contextCount: number; imageCount: number }): Promise<boolean>;
}

interface ActiveConversation {
  sessionId: string;
  agentRunId: string;
  engine: string;
  profileHash?: string;
  controller: "driven" | "observed";
  state: "running" | "idle" | "terminal";
  terminalState?: string;
  readOnly: boolean;
  profileLegacy?: boolean;
  /** Daemon-restart continuation: terminal leg, but the conversation continues on the next send. */
  resumable?: boolean;
  continuationNotice?: {
    mode: "reduced";
    message: string;
    omittedMessages?: number;
    expiredArtifacts?: Array<{ kind: string; name?: string; sha256?: string; bytes?: number }>;
  };
}

interface ObservedRunRecord {
  id: string;
  terminal: boolean;
  terminalState?: string;
  turnState?: string;
}

interface ObservedRecord {
  sessionId: string;
  label: string;
  runs: ObservedRunRecord[];
}

interface ReplayStatus {
  cursor: number;
  terminal?: boolean;
  terminalState?: string;
  turnState?: string;
}

export interface PendingDiffApproval {
  agentRunId: string;
  correlationId: string;
  sequenceNo: number;
  diff: NegotiatedDiff;
  corrupt: boolean;
}

export interface TestExtensionApprovalEvent {
  agentRunId: string;
  sequenceNo: number;
  correlationId: string;
  marker: string;
  code?: string;
  revision?: number;
  snapshotSetSha256?: string;
  files: Array<{ path: string; beforeSha256: string | null; proposedSha256: string | null }>;
}

export interface TestExtensionTurnEvent {
  agentRunId: string;
  sequenceNo: number;
  marker: string;
  code?: string;
  status?: string;
}

const DEFAULT_PROFILE: ConversationProfile = {
  permission_mode: "plan",
  auth_mode: "none",
};

/** Owns one workspace conversation; broadcast is the sole renderer synchronization point. */
export class ConversationController {
  private readonly renderers = new Map<string, RendererPort>();
  private client: SessionPort;
  private reducer: ReducerState = emptyState();
  private capabilities: EngineCapability[] = [];
  private observed = new Map<string, ObservedRecord>();
  private drivenConversations: Array<{ sessionId: string; agentRunId: string; label: string }> = [];
  private readonly pendingApprovals = new Set<string>();
  private readonly diffApprovals = new Map<string, PendingDiffApproval>();
  private readonly invalidDiffApprovals = new Map<string, { code: string; message: string }>();
  private readonly followUps = new FollowUpQueue();
  private contextItems: StagedContext[] = [];
  private readonly queuedContexts = new Map<string, StagedContext[]>();
  private imageItems: StagedImage[] = [];
  private readonly queuedImages = new Map<string, StagedImage[]>();
  private readonly omittedEventKeys = new Set<string>();
  private readonly stream = new StreamReducer();
  private readonly observedCursors = new Map<string, number>();
  private observedReplayRunIds: string[] = [];
  private observedRefreshPromise: Promise<void> | null = null;
  private selectedEngine = "local_llm";
  private selectedProfile: ConversationProfile = { ...DEFAULT_PROFILE };
  private durableProfileAttested = false;
  private active: ActiveConversation | null = null;
  private pendingPrompt = false;
  private pendingPromptToken: string | undefined;
  private acceptedPromptToken: string | undefined;
  private queuedPromptToken: string | undefined;
  private settledPromptToken: string | undefined;
  private queuedDispatchId: string | undefined;
  private promptSequence = 0;
  private lifecyclePending = false;
  private banner: { message: string; code?: string } | undefined;
  private capabilityDiscoveryFailed = false;
  private initialized = false;
  /** Reattach ran while the daemon was down (labels/resumability unverified) — heal on recovery. */
  private restorationDegraded = false;
  private readonly testExtensionApprovalEvents: TestExtensionApprovalEvent[] = [];
  private readonly testExtensionTurnEvents: TestExtensionTurnEvent[] = [];

  constructor(
    private readonly workspaceState: MementoPort,
    private readonly factory: SessionFactory,
    private readonly prompts: ControllerPrompts,
    private readonly sessionIndex?: SessionIndexPort,
  ) {
    this.client = this.makeClient();
  }

  static forRepo(
    repoRoot: string,
    workspaceState: MementoPort,
    prompts: ControllerPrompts,
    sessionIndex?: SessionIndexPort,
  ): ConversationController {
    return new ConversationController(
      workspaceState,
      (callbacks) => new SessionClient(repoRoot, callbacks),
      prompts,
      sessionIndex,
    );
  }

  async initialize(): Promise<void> {
    if (this.initialized) return;
    this.initialized = true;
    await Promise.all([
      this.loadCapabilities(false),
      this.sessionIndex ? Promise.resolve() : this.refreshObserved(),
    ]);
    const persisted = this.workspaceState.get<PersistedRun>(RUN_CURSORS_KEY);
    if (!this.active && persisted?.agent_run_id) {
      await this.attachConversation(persisted.session_id, persisted.agent_run_id, persisted.profile_hash);
    }
    this.broadcast();
  }

  /** Attach a driven conversation (reload restore OR the continue-previous dropdown): load metadata,
   * backfill prior linked legs' durable events, and bind the pump to the latest leg. */
  private async attachConversation(sessionId: string, agentRunId: string, profileHash?: string): Promise<void> {
    const loaded = await this.loadDrivenMetadata(sessionId, agentRunId);
    // Transport failure (daemon not up yet) => DEGRADED restoration; the observed-poll recovery
    // path re-runs the metadata load once the daemon answers, healing labels/profile/resumability.
    this.restorationDegraded = loaded === null;
    const metadata = loaded ?? {};
    if (metadata.engine) this.selectedEngine = metadata.engine;
    if (metadata.profile) this.selectedProfile = metadata.profile;
    if (this.active) {
      // Switching away from another conversation: a fresh client drops its pump/cursor cleanly.
      this.replaceClient();
      this.pendingApprovals.clear();
      this.diffApprovals.clear();
      this.invalidDiffApprovals.clear();
      this.banner = undefined;
    }
    this.active = {
      sessionId,
      agentRunId,
      engine: metadata.engine ?? this.selectedEngine,
      profileHash,
      controller: "driven",
      state: loaded === null ? "idle" : metadata.terminal ? "terminal" : metadata.turnState === "idle" ? "idle" : "running",
      ...(metadata.terminalState ? { terminalState: metadata.terminalState } : {}),
      readOnly: loaded === null || (metadata.terminal === true && metadata.resumable !== true),
      ...(metadata.legacy && loaded !== null ? { profileLegacy: true } : {}),
      ...(metadata.resumable !== undefined ? { resumable: metadata.resumable } : {}),
      ...(metadata.continuationNotice ? { continuationNotice: metadata.continuationNotice } : {}),
    };
    this.reducer = emptyState();
    this.diffApprovals.clear();
    this.invalidDiffApprovals.clear();
    this.omittedEventKeys.clear();
    this.stream.reset();
    // Continuation backfill: a resumed conversation spans LINKED legs; replay prior legs' durable
    // events first so the transcript is complete after a reload, then attach the pump to the
    // latest leg. Prior-leg finalizations latch the reducer terminal -- clear the latch; the live
    // leg's own replay re-establishes terminal truth.
    // session/list guarantees linkedRunIds oldest-first; replay order is therefore durable chronology.
    const priorLegs = (metadata.linkedRunIds ?? []).filter((id) => id !== agentRunId);
    for (const prior of priorLegs) await this.replayDrivenLeg(prior);
    if (priorLegs.length) {
      this.reducer.terminal = false;
      this.reducer.terminalState = undefined;
    }
    this.syncStreamingCapability();
    this.client.resume(agentRunId, sessionId);
    this.persistActive();
    this.broadcast();
  }

  private async selectConversation(sessionId: string, agentRunId: string, hasDraft = false): Promise<void> {
    if (this.active?.sessionId === sessionId) return;
    if (this.pendingPrompt || this.lifecyclePending) {
      return this.denyLocal("A conversation action is in progress", "DENIED_SESSION_NOT_IDLE");
    }
    if (this.active?.state === "running") {
      return this.denyLocal("Interrupt or close the running turn before switching", "DENIED_TURN_IN_PROGRESS");
    }
    if (!(await this.confirmEphemeralLoss(hasDraft))) return;
    this.followUps.clear();
    this.queuedContexts.clear();
    this.queuedImages.clear();
    this.contextItems = [];
    this.imageItems = [];
    this.queuedDispatchId = undefined;
    this.queuedPromptToken = undefined;
    await this.attachConversation(sessionId, agentRunId);
  }

  /** Refresh observed session metadata and incrementally replay the selected linked runs. */
  refreshObserved(): Promise<void> {
    if (this.observedRefreshPromise) return this.observedRefreshPromise;
    const operation = this.refreshObservedNow().finally(() => {
      if (this.observedRefreshPromise === operation) this.observedRefreshPromise = null;
    });
    this.observedRefreshPromise = operation;
    return operation;
  }

  sessionIndexFailed(error: Error): void {
    this.banner = { message: `Session index unavailable: ${error.message}`, code: "DAEMON_UNREACHABLE" };
    this.broadcast();
  }

  attach(renderer: RendererPort): void {
    this.renderers.set(renderer.id, renderer);
    this.send(renderer);
  }

  detach(rendererId: string): void {
    this.renderers.delete(rendererId);
  }

  supportsGovernedContext(): boolean {
    return this.capabilities.find((entry) => entry.id === this.selectedEngine)?.features.governed_context === true;
  }

  addContext(item: StagedContext): boolean {
    if (!this.supportsGovernedContext()) {
      this.denyLocal("Governed context is unavailable for the selected engine", "DENIED_CONTEXT_UNSUPPORTED");
      return false;
    }
    if (this.pendingPrompt || this.lifecyclePending) {
      this.denyLocal("Wait for the pending conversation action before changing context", "DENIED_SESSION_NOT_IDLE");
      return false;
    }
    try {
      const next = [...this.contextItems.filter((entry) => entry.ref.id !== item.ref.id), cloneContext(item)];
      validateContextEnvelope(next);
      this.contextItems = next;
      if (this.banner?.code?.startsWith("DENIED_CONTEXT_")) this.banner = undefined;
      this.broadcast();
      return true;
    } catch (error) {
      const code = error && typeof error === "object" && "code" in error && typeof error.code === "string"
        ? error.code
        : "DENIED_CONTEXT_INVALID";
      this.denyLocal(errorText(error), code);
      return false;
    }
  }

  removeContext(id: string): void {
    if (this.pendingPrompt || this.lifecyclePending) return;
    this.contextItems = this.contextItems.filter((entry) => entry.ref.id !== id);
    this.broadcast();
  }

  contextCount(): number {
    return this.contextItems.length;
  }

  supportsImages(): boolean {
    return this.capabilities.find((entry) => entry.id === this.selectedEngine)?.features.image_attachments === true;
  }

  addImage(item: StagedImage): boolean {
    if (!this.supportsImages()) {
      this.denyLocal("Image attachments are unavailable for the selected engine", "DENIED_ATTACHMENT_UNSUPPORTED");
      return false;
    }
    if (this.pendingPrompt || this.lifecyclePending) {
      this.denyLocal("Wait for the pending conversation action before changing images", "DENIED_SESSION_NOT_IDLE");
      return false;
    }
    try {
      const next = [...this.imageItems.filter((entry) => entry.ref.id !== item.ref.id), cloneImage(item)];
      validateImageEnvelope(next);
      this.imageItems = next;
      if (this.banner?.code?.startsWith("DENIED_ATTACHMENT_")) this.banner = undefined;
      this.broadcast();
      return true;
    } catch (error) {
      const code = error && typeof error === "object" && "code" in error && typeof error.code === "string"
        ? error.code
        : "DENIED_ATTACHMENT_INVALID";
      this.denyLocal(errorText(error), code);
      return false;
    }
  }

  removeImage(id: string): void {
    if (this.pendingPrompt || this.lifecyclePending) return;
    this.imageItems = this.imageItems.filter((entry) => entry.ref.id !== id);
    this.broadcast();
  }

  imageCount(): number {
    return this.imageItems.length;
  }

  /** Return the newest pending diff for a writable run, including legacy unscoped rows. */
  pendingDiff(agentRunId?: string, correlationId?: string): PendingDiffApproval | undefined {
    const runId = agentRunId ?? this.active?.agentRunId ?? "legacy";
    if (this.active?.readOnly || this.active?.state === "terminal") return undefined;
    const candidates = [...this.diffApprovals.values()].filter((approval) => {
      if (approval.agentRunId !== runId || (correlationId && approval.correlationId !== correlationId)) return false;
      return this.reducer.approvals.get(scopedCorrelation(runId, approval.correlationId))?.status === "pending";
    });
    return candidates.sort((left, right) => right.sequenceNo - left.sequenceNo)[0];
  }

  markDiffCorrupt(agentRunId: string, correlationId: string, revision: number): void {
    const key = scopedCorrelation(agentRunId, correlationId);
    const current = this.diffApprovals.get(key);
    if (!current || current.diff.revision !== revision) return;
    current.corrupt = true;
    this.banner = { message: "Diff snapshot is missing or corrupt; reject it or wait for a fresh revision", code: "DENIED_APPROVAL_SNAPSHOT_INVALID" };
    this.broadcast();
  }

  async resolveDiff(
    agentRunId: string,
    correlationId: string,
    decision: "approve" | "deny",
    metadataConfirmed = false,
  ): Promise<boolean> {
    const approval = this.pendingDiff(agentRunId, correlationId);
    if (!approval || (decision === "approve" && approval.corrupt)) return false;
    if (decision === "approve" && approval.diff.metadataConfirmationRequired && !metadataConfirmed) return false;
    await this.resolveApproval(correlationId, decision, agentRunId, decision === "approve" ? {
      expected_revision: approval.diff.revision,
      snapshot_set_sha256: approval.diff.snapshotSetSha256,
      metadata_confirmed: metadataConfirmed,
    } : undefined);
    return true;
  }

  isGenuinelyEmpty(): boolean {
    return this.active === null && !this.pendingPrompt && !this.lifecyclePending && this.contextItems.length === 0 &&
      this.imageItems.length === 0 && this.followUps.size === 0;
  }

  async openHistoryConversation(
    sessionId: string,
    agentRunId: string,
    controller: "driven" | "observed",
  ): Promise<void> {
    if (controller === "observed") await this.openObserved(sessionId, false);
    else await this.selectConversation(sessionId, agentRunId, false);
  }

  async handle(message: WebviewToHost, rendererId?: string): Promise<void> {
    switch (message.type) {
      case "ready":
        if (rendererId) {
          const renderer = this.renderers.get(rendererId);
          if (renderer) this.send(renderer);
        } else {
          this.broadcast();
        }
        return;
      case "profileChange":
        this.changeProfile(message.engine, message.profile);
        return;
      case "userPrompt":
        await this.submitPrompt(message.prompt, message.requestToken);
        return;
      case "steer":
        await this.control("steer", () => this.client.steer(message.instruction));
        return;
      case "interrupt":
        this.pauseFollowUps("interrupt");
        this.stream.clearFailure();
        await this.control("interrupt", () => this.client.interrupt());
        return;
      case "kill":
        await this.killConversation();
        return;
      case "close":
        await this.closeConversation(false, message.hasDraft ?? false);
        return;
      case "newConversation":
        await this.newConversation(message.hasDraft ?? false);
        return;
      case "approve": {
        if (this.pendingDiff(message.agentRunId, message.correlationId) ||
            this.invalidDiffApprovals.has(scopedCorrelation(message.agentRunId ?? this.active?.agentRunId ?? "legacy", message.correlationId))) {
          this.denyLocal("Preview and Accept the active diff revision", "DENIED_APPROVAL_CONFIRMATION_REQUIRED");
          return;
        }
        const answers = explicitAnswerMap(message.answers);
        if (message.answers !== undefined && answers === undefined) {
          this.denyLocal("Explicit approval answers are malformed", "DENIED_USER_INPUT_ANSWER_INVALID");
          return;
        }
        await this.resolveApproval(
          message.correlationId,
          "approve",
          message.agentRunId,
          undefined,
          answers,
        );
        return;
      }
      case "deny":
        await this.resolveApproval(message.correlationId, "deny", message.agentRunId);
        return;
      case "observedSelect":
        await this.openObserved(message.sessionId, message.hasDraft ?? false);
        return;
      case "conversationSelect":
        await this.selectConversation(message.sessionId, message.agentRunId, message.hasDraft ?? false);
        return;
      case "refreshCapabilities":
        if (this.pendingPrompt || this.active) return;
        await this.loadCapabilities(true);
        this.broadcast();
        return;
      default:
        return;
    }
  }

  snapshot(): ConversationSnapshot {
    const approvals = [...this.reducer.approvals.values()].map((approval) => ({
      ...approval,
      displayOnly: this.active?.readOnly ?? false,
      pending:
        approval.status === "pending" &&
        this.pendingApprovals.has(scopedCorrelation(approval.agentRunId, approval.correlationId)),
      ...diffView(this.diffApprovals.get(scopedCorrelation(approval.agentRunId, approval.correlationId))),
      ...diffErrorView(this.invalidDiffApprovals.get(scopedCorrelation(approval.agentRunId, approval.correlationId))),
    }));
    const approvalByKey = new Map(approvals.map((approval) => [approval.timelineKey, approval]));
    return {
      capabilities: this.capabilities.map(capabilityView),
      selectedEngine: this.selectedEngine,
      selectedProfile: { ...this.selectedProfile } as ProfileSelection,
      selectorsLocked: this.active !== null || this.pendingPrompt,
      conversation: this.active ? { ...this.active } : null,
      transcript: this.reducer.transcript.map((message) => ({ ...message })),
      timeline: this.reducer.timeline.map((item) => {
        if (item.kind === "message") return { ...item, message: { ...item.message } };
        if (item.kind === "tool") return { ...item, card: cloneToolCard(item.card) };
        return { ...item, approval: approvalByKey.get(item.key) ?? {
          ...item.approval,
          displayOnly: this.active?.readOnly ?? false,
          pending: false,
        } };
      }),
      cards: [...this.reducer.cards.values()].map(cloneToolCard),
      approvals,
      followUpQueue: this.followUps.view(),
      observedSessions: [...this.observed.values()].map(({ sessionId, label }) => ({ sessionId, label })),
      history: historyFromSessionList(this.sessionIndex?.current()),
      contextChips: this.contextItems.map((item) => ({
        id: item.ref.id,
        kind: item.ref.kind,
        label: item.label,
        sourcePath: item.ref.source_path,
        ...(item.bytes ? { bytes: item.bytes } : {}),
      })),
      imageChips: this.imageItems.map((item) => ({
        id: item.ref.id,
        label: item.label,
        bytes: item.ref.bytes,
        mediaType: item.ref.media_type,
        sha256: item.ref.sha256,
      })),
      ...(this.stream.snapshot() ? { streamingBubble: this.stream.snapshot() } : {}),
      conversations: this.drivenConversations.map((entry) => ({
        ...entry,
        active: this.active?.sessionId === entry.sessionId,
      })),
      pendingPrompt: this.pendingPrompt,
      ...(this.pendingPromptToken ? { pendingPromptToken: this.pendingPromptToken } : {}),
      ...(this.acceptedPromptToken ? { acceptedPromptToken: this.acceptedPromptToken } : {}),
      ...(this.queuedPromptToken ? { queuedPromptToken: this.queuedPromptToken } : {}),
      ...(this.settledPromptToken ? { settledPromptToken: this.settledPromptToken } : {}),
      ...(this.banner ? { banner: { ...this.banner } } : {}),
    };
  }

  /** Read-only acceptance telemetry; it never exposes prompt, context, image, or provider bytes. */
  testExtensionObservation(): {
    profile: ConversationProfile;
    profileAttested: boolean;
    stream: import("./streamReducer").StreamAcceptanceState;
    approvalEvents: TestExtensionApprovalEvent[];
    turnEvents: TestExtensionTurnEvent[];
  } {
    return {
      profile: { ...this.selectedProfile },
      profileAttested: this.durableProfileAttested,
      stream: this.stream.acceptanceState(),
      approvalEvents: this.testExtensionApprovalEvents.map((event) => ({
        ...event, files: event.files.map((file) => ({ ...file })),
      })),
      turnEvents: this.testExtensionTurnEvents.map((event) => ({ ...event })),
    };
  }

  dispose(): void {
    this.client.dispose();
    this.renderers.clear();
  }

  private makeClient(): SessionPort {
    return this.factory({
      onEvents: (events, cursor) => {
        this.workspaceState.updateCursor?.(cursor);
        this.onEvents(events);
      },
      onDeltas: (deltas, cursor, dropped) => this.onDeltas(deltas, cursor, dropped),
      onTurnState: (state, terminal) => this.onTurnState(state, terminal),
      onTerminal: (state) => this.onTerminal(state),
      onError: (error) => this.onClientError(error),
    });
  }

  private replaceClient(): void {
    this.client.dispose();
    this.client = this.makeClient();
    this.syncStreamingCapability();
  }

  /** Refresh engine capabilities while preserving valid selections and surfacing catalog drift. */
  private async loadCapabilities(refresh: boolean): Promise<void> {
    try {
      const response = await this.client.capabilities(refresh);
      if (response.status !== "OK") {
        this.capabilityDiscoveryFailed = true;
        this.banner = responseBanner(response, "Capability discovery failed");
        return;
      }
      const recoveringCapabilityDiscovery = this.capabilityDiscoveryFailed;
      this.capabilityDiscoveryFailed = false;
      const previousCapabilities = this.capabilities;
      const previousEngine = canonicalEngine(this.selectedEngine);
      const previousProfile = { ...this.selectedProfile };
      this.capabilities = canonicalCapabilities(Array.isArray(response.engines) ? response.engines : []);
      this.selectedEngine = previousEngine;
      const engineStillPresent = this.capabilities.some((entry) => entry.id === this.selectedEngine);
      if (!engineStillPresent) {
        this.selectedEngine = this.capabilities[0]?.id ?? "local_llm";
      }
      if (!this.active && !this.pendingPrompt) {
        const capability = this.capabilities.find((entry) => entry.id === this.selectedEngine);
        if (capability) {
          this.selectedProfile = engineStillPresent && previousCapabilities.length > 0
            ? normalizeProfile(capability, previousProfile)
            : defaultProfile(capability);
          const unavailableModel = capability.id === "claude" && previousProfile.model
            && !capability.models.some((entry) => entry.id === previousProfile.model);
          if (unavailableModel) {
            this.banner = {
              message: "The selected Claude model is no longer available; choose a model from the refreshed catalog",
              code: "DENIED_MODEL_UNAVAILABLE",
            };
          } else if (recoveringCapabilityDiscovery || this.banner?.code === "DENIED_MODEL_UNAVAILABLE") {
            this.banner = undefined;
          }
        }
      }
      this.syncStreamingCapability();
    } catch (error) {
      this.capabilityDiscoveryFailed = true;
      this.banner = { message: `Capability discovery unavailable: ${errorText(error)}`, code: "DAEMON_UNREACHABLE" };
    }
  }

  private async refreshObservedNow(): Promise<void> {
    try {
      const response = this.sessionIndex?.current() ?? await this.client.list({ controller: "observed", limit: 100 });
      assertOk(response, "Observed session refresh failed");
      // Auto-recover (owner UX ask 2026-07-10): the daemon just answered, so if capability discovery
      // previously failed (empty selectors / DAEMON_UNREACHABLE banner), reload it now — the chat
      // heals itself on the poll after the daemon starts, no manual Refresh needed.
      if (this.capabilities.length === 0 || this.banner?.code === "DAEMON_UNREACHABLE") {
        this.banner = undefined;
        await this.loadCapabilities(false);
      }
      // Restoration healing (2026-07-11): a reattach that raced the daemon boot rendered default/wrong
      // labels and guessed resumability — re-load the authoritative metadata now that the daemon answers.
      if (this.restorationDegraded && this.active?.controller === "driven") {
        const healed = await this.loadDrivenMetadata(this.active.sessionId, this.active.agentRunId);
        if (healed !== null) {
          this.restorationDegraded = false;
          this.applyDrivenMetadata(healed);
        }
      }
      this.observed = observedRecords(response);
      // Continue-previous dropdown (owner 2026-07-11): every driven conversation is offered for
      // continuation, newest first; the daemon's turn path decides fidelity (full vendor resume or
      // transcript-seeded).
      try {
        const drivenList = this.sessionIndex?.current() ?? await this.client.list({ controller: "driven", limit: 20 });
        if (drivenList.status === "OK" && Array.isArray(drivenList.sessions)) {
          this.drivenConversations = (drivenList.sessions as Array<Record<string, unknown>>)
            .map(conversationEntry)
            .filter((entry): entry is { sessionId: string; agentRunId: string; label: string } => entry !== null);
        }
      } catch {
        // The dropdown is a convenience surface; a failed refresh keeps the last known list.
      }
      if (this.active?.controller === "observed") {
        const record = this.observed.get(this.active.sessionId);
        if (!record) {
          this.active.state = "terminal";
          this.active.readOnly = true;
          this.active.terminalState = "unavailable";
          this.banner = { message: "Observed conversation is no longer available", code: "OBSERVED_SESSION_UNAVAILABLE" };
        } else {
          await this.syncObservedRecord(record);
        }
      }
    } catch (error) {
      this.banner = errorBanner(error, "Observed session refresh failed");
    }
    this.broadcast();
  }

  private async loadDrivenMetadata(
    sessionId: string,
    agentRunId: string,
  ): Promise<{
    engine?: string;
    profile?: ConversationProfile;
    legacy?: boolean;
    terminal?: boolean;
    terminalState?: string;
    turnState?: string;
    linkedRunIds?: string[];
    resumable?: boolean;
    continuationNotice?: ActiveConversation["continuationNotice"];
  } | null> {
    // null = TRANSPORT failure (daemon down): restoration is DEGRADED and must be retried when the
    // daemon answers again -- otherwise a reattached conversation renders default/wrong labels forever
    // (seen live 2026-07-11: extension host activated 20s before the daemon booted).
    try {
      const shared = this.sessionIndex?.current();
      if (this.sessionIndex && !shared) return null;
      const response = shared ?? await this.client.list({ controller: "driven", limit: 1_000 });
      assertOk(response, "Driven session metadata failed");
      return drivenMetadata(response, sessionId, agentRunId);
    } catch (error) {
      this.banner = errorBanner(error, "Driven session metadata failed");
      return null;
    }
  }

  private applyDrivenMetadata(metadata: NonNullable<Awaited<ReturnType<ConversationController["loadDrivenMetadata"]>>>): void {
    if (!this.active) return;
    if (metadata.engine) {
      this.selectedEngine = metadata.engine;
      this.active.engine = metadata.engine;
    }
    if (metadata.profile) {
      this.selectedProfile = metadata.profile;
      this.active.profileLegacy = false;
    } else if (metadata.legacy) {
      this.active.profileLegacy = true;
    }
    // The daemon is AUTHORITATIVE on continuation eligibility (legacy pre-continuation rows cannot
    // resume no matter what the event heuristic spotted).
    if (metadata.resumable !== undefined) this.active.resumable = metadata.resumable;
    if (metadata.continuationNotice) this.active.continuationNotice = metadata.continuationNotice;
    if (metadata.terminal) {
      this.active.state = "terminal";
      if (metadata.terminalState) this.active.terminalState = metadata.terminalState;
      this.active.readOnly = !this.active.resumable;
      if (this.banner?.code === "DAEMON_RESTART" && !this.active.resumable) {
        this.banner = { message: "Conversation ended by daemon restart — start a new conversation", code: "DAEMON_RESTART" };
      }
    } else {
      this.active.state = metadata.turnState === "idle" ? "idle" : "running";
      this.active.readOnly = false;
      delete this.active.terminalState;
    }
  }

  private changeProfile(engine: string, profile: ProfileSelection): void {
    if (this.active || this.pendingPrompt || this.lifecyclePending) return;
    const normalizedEngine = canonicalEngine(engine);
    const capability = this.capabilities.find((entry) => entry.id === normalizedEngine);
    if (!capability) return;
    this.selectedEngine = normalizedEngine;
    this.selectedProfile = normalizeProfile(capability, profile);
    const unavailableModel = capability.id === "claude" && this.selectedProfile.model
      && !capability.models.some((entry) => entry.id === this.selectedProfile.model);
    if (unavailableModel) {
      this.banner = {
        message: "The selected Claude model is unavailable; choose an exact model from the current catalog",
        code: "DENIED_MODEL_UNAVAILABLE",
      };
    } else if (this.banner?.code === "DENIED_MODEL_UNAVAILABLE") {
      this.banner = undefined;
    }
    this.syncStreamingCapability();
    this.broadcast();
  }

  private resetProfileForEngine(engine: string): void {
    if (this.active || this.pendingPrompt || this.lifecyclePending) return;
    const capability = this.capabilities.find((entry) => entry.id === engine);
    if (!capability) return;
    this.selectedProfile = defaultProfile(capability);
    this.syncStreamingCapability();
  }

  private syncStreamingCapability(): void {
    const capability = this.capabilities.find((entry) => entry.id === (this.active?.engine ?? this.selectedEngine));
    this.client.setStreaming(capability?.features.streaming === true);
  }

  /** Submit a direct or queued prompt; durable user-event folding settles accepted state. */
  private async submitPrompt(prompt: string, requestToken?: string, queuedItem?: QueuedFollowUp): Promise<void> {
    const text = prompt.trim();
    const token = cleanOptional(requestToken);
    const submittedContext = queuedItem
      ? (this.queuedContexts.get(queuedItem.id) ?? []).map(cloneContext)
      : this.contextItems.map(cloneContext);
    const submittedImages = queuedItem
      ? (this.queuedImages.get(queuedItem.id) ?? []).map(cloneImage)
      : this.imageItems.map(cloneImage);
    if (!text) return this.settleRejectedPrompt(token);
    if (!queuedItem && (this.pendingPrompt || this.active?.state === "running")) {
      this.enqueueFollowUp(text, token);
      return;
    }
    if (this.pendingPrompt) {
      if (queuedItem) this.rejectQueuedDispatch(queuedItem.id, "uncertain_transport");
      return this.settleRejectedPrompt(token);
    }
    if (this.lifecyclePending) {
      if (queuedItem) this.rejectQueuedDispatch(queuedItem.id, "failure");
      this.settleRejectedPrompt(token);
      return this.denyLocal("A conversation lifecycle action is in progress", "DENIED_SESSION_NOT_IDLE");
    }
    if (this.active?.readOnly) {
      if (queuedItem) this.rejectQueuedDispatch(queuedItem.id, "failure");
      this.settleRejectedPrompt(token);
      return this.denyLocal("Observed conversations are read-only", "DENIED_SESSION_READ_ONLY");
    }
    if (this.active?.state === "running") {
      if (queuedItem) this.rejectQueuedDispatch(queuedItem.id, "failure");
      this.settleRejectedPrompt(token);
      return this.denyLocal("A turn is already running", "DENIED_TURN_IN_PROGRESS");
    }
    if (this.active?.state === "terminal" && !this.active.resumable) {
      if (queuedItem) this.rejectQueuedDispatch(queuedItem.id, "failure");
      this.settleRejectedPrompt(token);
      return this.denyLocal("Start a new conversation before sending", "DENIED_SESSION_TERMINAL");
    }
    try {
      validateContextEnvelope(submittedContext);
      validateImageEnvelope(submittedImages);
      if (submittedContext.length > 0 && !this.supportsGovernedContext()) {
        throw Object.assign(new Error("Governed context is unavailable for the selected engine"), {
          code: "DENIED_CONTEXT_UNSUPPORTED",
        });
      }
      if (submittedImages.length > 0 && !this.supportsImages()) {
        throw Object.assign(new Error("Image attachments are unavailable for the selected engine"), {
          code: "DENIED_ATTACHMENT_UNSUPPORTED",
        });
      }
    } catch (error) {
      if (queuedItem) this.rejectQueuedDispatch(queuedItem.id, "failure");
      this.settleRejectedPrompt(token);
      const code = error && typeof error === "object" && "code" in error && typeof error.code === "string"
        ? error.code
        : "DENIED_CONTEXT_INVALID";
      return this.denyLocal(errorText(error), code);
    }

    this.pendingPrompt = true;
    this.pendingPromptToken = token ?? `host-${++this.promptSequence}`;
    this.banner = undefined;
    this.broadcast();
    try {
      if (this.active) {
        const response = await this.client.turn(text, composerEnvelope(submittedContext, submittedImages));
        if (response.status !== "OK") {
          this.banner = responseBanner(response, "Turn denied");
        } else if (
          typeof response.agent_run_id === "string" &&
          response.agent_run_id &&
          response.agent_run_id !== this.active.agentRunId
        ) {
          // The daemon rehydrated the conversation as a NEW linked leg (resumed_from = the old run):
          // rebind the pump and persisted identifiers to the new run; the transcript continues.
          this.rebindResumedLeg(response.agent_run_id);
          if (response.transcript_seeded === true && this.active) {
            this.active.continuationNotice = reducedFidelityNotice();
          }
        }
      } else {
        await this.startConversation(text, submittedContext, submittedImages);
      }
    } catch (error) {
      this.banner = { message: `Daemon unreachable: ${errorText(error)}`, code: "DAEMON_UNREACHABLE" };
    } finally {
      if (!this.active || this.banner) {
        this.settledPromptToken = this.pendingPromptToken;
        this.pendingPrompt = false;
        this.pendingPromptToken = undefined;
        if (queuedItem) {
          const reason = this.banner?.code === "DENIED_WORKSPACE_WRITER_BUSY" ? "lease_conflict" :
            this.banner?.code === "DAEMON_UNREACHABLE" ? "uncertain_transport" : "failure";
          this.rejectQueuedDispatch(queuedItem.id, reason);
        }
      }
      this.broadcast();
    }
  }

  private enqueueFollowUp(prompt: string, requestToken?: string): void {
    const result = this.followUps.enqueue(prompt, requestToken);
    if (!result.accepted) {
      this.settledPromptToken = requestToken;
      this.banner = {
        message: result.reason === "full" ? "Follow-up queue is full (10 prompts)" : "Prompt is empty",
        code: result.reason === "full" ? "DENIED_FOLLOW_UP_QUEUE_FULL" : "DENIED_PROMPT_EMPTY",
      };
      this.broadcast();
      return;
    }
    this.queuedPromptToken = requestToken;
    this.settledPromptToken = requestToken;
    this.queuedContexts.set(result.item.id, this.contextItems.map(cloneContext));
    this.queuedImages.set(result.item.id, this.imageItems.map(cloneImage));
    this.contextItems = [];
    this.imageItems = [];
    if (this.banner?.code === "DENIED_FOLLOW_UP_QUEUE_FULL") this.banner = undefined;
    this.broadcast();
  }

  private drainFollowUpAfterNormalCompletion(): void {
    if (this.pendingPrompt || !this.active || this.active.readOnly || this.active.state !== "idle") return;
    const item = this.followUps.beginDispatchAfterNormalCompletion();
    if (!item) return;
    this.queuedDispatchId = item.id;
    void this.submitPrompt(item.prompt, item.requestToken, item);
  }

  private rejectQueuedDispatch(itemId: string, reason: QueuePauseReason): void {
    this.followUps.rejectDispatch(itemId);
    this.queuedDispatchId = undefined;
    this.pauseFollowUps(reason);
  }

  private pauseFollowUps(reason: QueuePauseReason): void {
    this.followUps.pause(reason);
  }

  /** Rebind a resumed conversation to its daemon-minted leg and reset leg-local state. */
  private rebindResumedLeg(newRunId: string): void {
    if (!this.active) return;
    this.active.agentRunId = newRunId;
    this.active.state = "running";
    this.active.resumable = false;
    this.active.readOnly = false;
    delete this.active.terminalState;
    // The old leg's finalization latched the reducer terminal; the conversation continues live.
    this.reducer.terminal = false;
    this.reducer.terminalState = undefined;
    this.reducer.turnState = "running";
    this.pendingApprovals.clear();
    this.diffApprovals.clear();
    this.invalidDiffApprovals.clear();
    this.stream.reset();
    if (this.banner?.code === "DAEMON_RESTART") this.banner = undefined;
    this.persistActive();
    this.replaceClient();
    this.client.resume(newRunId, this.active.sessionId);
  }

  private async startConversation(
    prompt: string,
    context: readonly StagedContext[],
    images: readonly StagedImage[],
  ): Promise<void> {
    this.durableProfileAttested = false;
    const capability = this.capabilities.find((entry) => entry.id === this.selectedEngine);
    if (!capability?.drivable) {
      this.banner = {
        message: capability?.id === "claude"
          ? capability.availability.state === "auth_required"
            ? "Claude subscription authentication is required outside Kaizen"
            : "Claude subscription runtime is unavailable"
          : capability?.availability.message || "Selected engine is not drivable",
        code: capability?.availability.code || "DENIED_ENGINE_UNAVAILABLE",
      };
      return;
    }
    const claudeDenial = claudeProfileDenial(capability, this.selectedProfile);
    if (claudeDenial) {
      this.banner = claudeDenial;
      return;
    }
    let fullOptIn = false;
    if (this.selectedProfile.permission_mode === "full") {
      fullOptIn = await this.prompts.confirmFull();
      if (!fullOptIn) {
        this.banner = { message: "Full mode requires a fresh confirmation", code: "DENIED_FULL_CONFIRMATION_REQUIRED" };
        return;
      }
    }
    const { max_turns: selectedMaxTurns, ...wireProfile } = this.selectedProfile;
    const response = await this.client.start({
      engine: this.selectedEngine,
      prompt,
      profile: wireProfile,
      ...(this.selectedEngine === "claude" && selectedMaxTurns !== undefined
        ? { max_turns: selectedMaxTurns }
        : {}),
      ...(fullOptIn ? { full_opt_in: true } : {}),
      ...composerEnvelope(context, images),
      ...(capability.features.diff_snapshots ? { client_features: { diff_snapshots: true } } : {}),
    });
    if (response.status !== "OK" || !response.session_id || !response.agent_run_id) {
      this.banner = this.selectedEngine === "claude"
        ? sanitizedClaudeStartBanner(response)
        : responseBanner(response, "Session start denied");
      return;
    }
    this.active = {
      sessionId: response.session_id,
      agentRunId: response.agent_run_id,
      engine: response.engine ?? this.selectedEngine,
      profileHash: response.profile_hash,
      controller: "driven",
      state: this.reducer.transcript.length > 0 && this.reducer.turnState === "idle" ? "idle" : "running",
      readOnly: false,
    };
    if (response.profile) {
      this.selectedProfile = normalizeProfile(capability, {
        ...response.profile,
        ...(selectedMaxTurns !== undefined && response.profile.max_turns === undefined
          ? { max_turns: selectedMaxTurns }
          : {}),
      });
    }
    this.persistActive();
  }

  /** Backfill one prior leg by cursor, degrading fidelity instead of failing restoration. */
  private async replayDrivenLeg(runId: string): Promise<void> {
    let cursor = 0;
    try {
      for (let page = 0; page < 1_000; page += 1) {
        const replay = await this.client.eventsOnce(runId, cursor, 500);
        assertOk(replay, `Driven replay failed for ${runId}`);
        const events = Array.isArray(replay.events) ? replay.events as WireEvent[] : [];
        this.captureOmittedMessages(events, runId);
        reduceEvents(this.reducer, events, runId);
        const next = typeof replay.cursor === "number" ? replay.cursor : cursor;
        if (events.length === 0 || next <= cursor) return;
        cursor = next;
      }
      this.mergeContinuationNotice({ omittedMessages: 1 }, "Prior conversation replay exceeded its safety bound.");
    } catch {
      this.mergeContinuationNotice({ omittedMessages: 1 }, "Some prior durable messages could not be replayed.");
    }
  }

  /** Fold durable events, then apply side effects and broadcast in a fixed order. */
  private onEvents(events: WireEvent[]): void {
    this.stream.reconcileDurable(events);
    this.captureTestExtensionApprovalEvents(events, this.active?.agentRunId ?? "legacy");
    this.captureTestExtensionTurnEvents(events, this.active?.agentRunId ?? "legacy");
    this.captureOmittedMessages(events, this.active?.agentRunId ?? "legacy");
    this.captureDiffApprovals(events, this.active?.agentRunId ?? "legacy");
    for (const event of events) {
      if (event.event_kind !== "profile" || event.marker !== "point" || typeof event.body !== "string") continue;
      try {
        const body = JSON.parse(event.body) as Record<string, unknown>;
        const effective = conversationProfile(body.effective);
        if (effective) {
          this.selectedProfile = effective;
          this.durableProfileAttested = true;
          if (this.active) this.active.profileLegacy = false;
        }
        if (this.active && typeof body.profile_hash === "string") this.active.profileHash = body.profile_hash;
        if (body.resume_fidelity === "transcript_seeded" || body.resume_fidelity === "reduced") {
          this.mergeContinuationNotice(
            { omittedMessages: nonNegativeInteger(body.omitted_message_count) },
            reducedFidelityNotice().message,
          );
        }
        const expiredArtifacts = metadataArtifacts(body.expired_artifacts);
        if (expiredArtifacts.length) this.mergeContinuationNotice({ expiredArtifacts });
      } catch {
        // A malformed profile event does not break transcript replay; the server remains authoritative.
      }
    }
    const defaultRunId = this.active?.agentRunId ?? "legacy";
    const ops = reduceEvents(this.reducer, events, defaultRunId);
    const acceptedUser = ops.some(
      (op) => op.kind === "chatMessage" && op.message.role === "user",
    );
    if (events.some((event) => event.event_kind === "finalization" && event.code === "ORPHAN_SWEEP_FINALIZED")) {
      // Continuation (owner decision 2026-07-10): a restart-orphaned DRIVEN conversation is a durable
      // asset -- the daemon rehydrates it as a new linked leg on the next turn, so the composer stays
      // usable instead of locking read-only. The daemon's session/list `resumable` verdict is
      // AUTHORITATIVE: when it said false (e.g. a legacy pre-continuation row without a stored
      // snapshot), the event heuristic must not override it.
      if (this.active?.controller === "driven" && this.active.resumable !== false) {
        this.active.resumable = true;
        this.banner = { message: "Daemon restarted — send a message to continue this conversation", code: "DAEMON_RESTART" };
      } else {
        this.banner = { message: "Conversation ended by daemon restart", code: "DAEMON_RESTART" };
      }
    }
    for (const event of events) {
      if (event.event_kind === "approval" && event.marker !== "open" && event.correlation_id) {
        const runId = typeof event.agent_run_id === "string" && event.agent_run_id ? event.agent_run_id : defaultRunId;
        const key = scopedCorrelation(runId, event.correlation_id);
        this.pendingApprovals.delete(key);
        this.diffApprovals.delete(key);
        this.invalidDiffApprovals.delete(key);
      }
    }
    if (acceptedUser) {
      if (this.queuedDispatchId) {
        this.followUps.acknowledgeDurable(this.queuedDispatchId);
        this.queuedContexts.delete(this.queuedDispatchId);
        this.queuedImages.delete(this.queuedDispatchId);
        this.queuedDispatchId = undefined;
      } else if (this.pendingPrompt) {
        this.contextItems = [];
        this.imageItems = [];
      }
      this.acceptedPromptToken = this.pendingPromptToken;
      this.settledPromptToken = this.pendingPromptToken;
      this.pendingPromptToken = undefined;
      this.pendingPrompt = false;
    }
    if (this.banner?.code === "DAEMON_UNREACHABLE") this.banner = undefined;
    if (this.active && !this.active.readOnly && this.reducer.turnState === "idle" && !this.reducer.terminal) this.active.state = "idle";
    if (this.active && !this.active.readOnly && this.reducer.turnState === "running") this.active.state = "running";
    if (this.active && this.reducer.terminal) {
      this.active.state = "terminal";
      this.active.terminalState = this.reducer.terminalState;
      this.active.readOnly = !this.active.resumable;
    }
    const approvalTimedOut = events.some(
      (event) => event.event_kind === "approval" && event.marker === "timed_out",
    );
    const writerConflict = events.some((event) => event.code === "DENIED_WORKSPACE_WRITER_BUSY");
    const abnormalTurn = ops.some(
      (op) => op.kind === "turnDone" && op.terminalState !== "completed",
    );
    const normalTurn = ops.some(
      (op) => op.kind === "turnDone" && op.terminalState === "completed",
    );
    if (approvalTimedOut) this.pauseFollowUps("approval_timeout");
    else if (writerConflict) this.pauseFollowUps("lease_conflict");
    else if (abnormalTurn) this.pauseFollowUps("failure");
    else if (normalTurn && this.active?.state === "terminal") this.pauseFollowUps("failure");
    this.broadcast();
    if (normalTurn && this.active?.state !== "terminal" && !approvalTimedOut && !writerConflict && !abnormalTurn) {
      this.drainFollowUpAfterNormalCompletion();
    }
  }

  private captureTestExtensionApprovalEvents(events: readonly WireEvent[], fallbackRunId: string): void {
    for (const event of events) {
      if (event.event_kind !== "approval" || !event.correlation_id) continue;
      const agentRunId = cleanOptional(event.agent_run_id) ?? fallbackRunId;
      let revision: number | undefined;
      let snapshotSetSha256: string | undefined;
      let files: TestExtensionApprovalEvent["files"] = [];
      if (event.marker === "open") {
        try {
          const diff = parseNegotiatedDiff(event.body);
          if (diff) {
            revision = diff.revision;
            snapshotSetSha256 = diff.snapshotSetSha256;
            files = diff.fileChanges.map((change) => ({
              path: change.path,
              beforeSha256: change.before?.sha256 ?? null,
              proposedSha256: change.proposed?.sha256 ?? null,
            }));
          }
        } catch {
          // The normal invalid-diff path remains authoritative; telemetry records only the event identity.
        }
      }
      const entry: TestExtensionApprovalEvent = {
        agentRunId, sequenceNo: event.sequence_no, correlationId: event.correlation_id,
        marker: event.marker, files,
        ...(cleanOptional(event.code) ? { code: cleanOptional(event.code) } : {}),
        ...(revision !== undefined ? { revision } : {}),
        ...(snapshotSetSha256 ? { snapshotSetSha256 } : {}),
      };
      const prior = this.testExtensionApprovalEvents.some((item) =>
        item.agentRunId === entry.agentRunId && item.sequenceNo === entry.sequenceNo);
      if (!prior) this.testExtensionApprovalEvents.push(entry);
    }
    if (this.testExtensionApprovalEvents.length > 128) {
      this.testExtensionApprovalEvents.splice(0, this.testExtensionApprovalEvents.length - 128);
    }
  }

  private captureTestExtensionTurnEvents(events: readonly WireEvent[], fallbackRunId: string): void {
    for (const event of events) {
      if (event.event_kind !== "turn") continue;
      let status: string | undefined;
      if (typeof event.body === "string") {
        try {
          const body = JSON.parse(event.body) as Record<string, unknown>;
          status = cleanOptional(body.status);
        } catch { /* identity and marker remain useful */ }
      }
      const entry: TestExtensionTurnEvent = {
        agentRunId: cleanOptional(event.agent_run_id) ?? fallbackRunId,
        sequenceNo: event.sequence_no,
        marker: event.marker,
        ...(cleanOptional(event.code) ? { code: cleanOptional(event.code) } : {}),
        ...(status ? { status } : {}),
      };
      if (!this.testExtensionTurnEvents.some((item) =>
        item.agentRunId === entry.agentRunId && item.sequenceNo === entry.sequenceNo)) {
        this.testExtensionTurnEvents.push(entry);
      }
    }
    if (this.testExtensionTurnEvents.length > 128) {
      this.testExtensionTurnEvents.splice(0, this.testExtensionTurnEvents.length - 128);
    }
  }

  private onDeltas(deltas: SessionDelta[], cursor: number, dropped: boolean): void {
    const capability = this.capabilities.find((entry) => entry.id === this.active?.engine);
    if (!this.active || this.active.controller !== "driven" || capability?.features.streaming !== true) return;
    this.stream.apply(deltas, cursor, dropped);
    this.broadcast();
  }

  private captureDiffApprovals(events: readonly WireEvent[], fallbackRunId: string): void {
    const capability = this.capabilities.find((entry) => entry.id === (this.active?.engine ?? this.selectedEngine));
    if (capability?.features.diff_snapshots !== true) return;
    for (const event of events) {
      if (event.event_kind !== "approval" || event.marker !== "open" || !event.correlation_id) continue;
      const agentRunId = cleanOptional(event.agent_run_id) ?? fallbackRunId;
      const key = scopedCorrelation(agentRunId, event.correlation_id);
      try {
        const diff = parseNegotiatedDiff(event.body);
        if (!diff) continue;
        this.diffApprovals.set(key, {
          agentRunId,
          correlationId: event.correlation_id,
          sequenceNo: event.sequence_no,
          diff,
          corrupt: false,
        });
        this.invalidDiffApprovals.delete(key);
      } catch (error) {
        if (error instanceof DiffModelError) {
          this.diffApprovals.delete(key);
          this.invalidDiffApprovals.set(key, { code: error.code, message: error.message });
          this.banner = { message: error.message, code: error.code };
        }
      }
    }
  }

  private captureOmittedMessages(events: readonly WireEvent[], fallbackRunId: string): void {
    for (const event of events) {
      if (event.event_kind !== "chat_message" || event.body_omitted !== true) continue;
      const runId = cleanOptional(event.agent_run_id) ?? fallbackRunId;
      const key = `${runId}:${event.sequence_no}`;
      if (this.omittedEventKeys.has(key)) continue;
      this.omittedEventKeys.add(key);
      this.mergeContinuationNotice({ omittedMessages: (this.active?.continuationNotice?.omittedMessages ?? 0) + 1 });
    }
  }

  private mergeContinuationNotice(
    metadata: {
      omittedMessages?: number;
      expiredArtifacts?: Array<{ kind: string; name?: string; sha256?: string; bytes?: number }>;
    },
    message?: string,
  ): void {
    if (!this.active) return;
    const previous = this.active.continuationNotice;
    const omittedMessages = Math.max(previous?.omittedMessages ?? 0, metadata.omittedMessages ?? 0);
    const expiredArtifacts = uniqueArtifactMetadata([
      ...(previous?.expiredArtifacts ?? []),
      ...(metadata.expiredArtifacts ?? []),
    ]);
    this.active.continuationNotice = {
      mode: "reduced",
      message: message ?? previous?.message ?? "Some durable conversation metadata is unavailable; old bytes were not replayed.",
      ...(omittedMessages ? { omittedMessages } : {}),
      ...(expiredArtifacts.length ? { expiredArtifacts } : {}),
    };
  }

  private onTurnState(state: string, terminal: boolean): void {
    if (!this.active) return;
    if (this.active.readOnly && this.active.state === "terminal" && !terminal) return;
    if (this.banner?.code === "DAEMON_UNREACHABLE") this.banner = undefined;
    if (terminal) {
      this.stream.clearFailure();
      this.active.state = "terminal";
      this.active.readOnly = !this.active.resumable;
      this.pauseFollowUps("failure");
    } else if (state === "idle") {
      this.active.state = "idle";
    } else if (state === "running") {
      this.active.state = "running";
    }
    this.broadcast();
  }

  private onTerminal(state: string | undefined): void {
    if (!this.active) return;
    this.active.state = "terminal";
    this.stream.clearFailure();
    this.active.terminalState = state;
    this.active.readOnly = !this.active.resumable;
    this.pendingPrompt = false;
    this.settledPromptToken = this.pendingPromptToken;
    this.pendingPromptToken = undefined;
    if (this.followUps.size > 0) this.pauseFollowUps("failure");
    if (state && state !== "success" && this.banner?.code !== "DAEMON_RESTART") {
      this.banner = {
        message: state === "orphaned" ? "Conversation ended by daemon restart" : `Conversation ended: ${state}`,
        code: state,
      };
    }
    this.persistActive();
    this.broadcast();
  }

  private onClientError(error: Error): void {
    this.stream.clearFailure();
    if (!(error instanceof SessionResponseError)) {
      this.pauseFollowUps("uncertain_transport");
      this.banner = { message: `Daemon unreachable, retrying: ${error.message}`, code: "DAEMON_UNREACHABLE" };
      this.broadcast();
      return;
    }

    this.pendingPrompt = false;
    this.settledPromptToken = this.pendingPromptToken;
    this.pendingPromptToken = undefined;
    this.pendingApprovals.clear();
    this.diffApprovals.clear();
    this.invalidDiffApprovals.clear();
    this.pauseFollowUps(error.code === "DENIED_WORKSPACE_WRITER_BUSY" ? "lease_conflict" : "failure");
    if (error.code === "DENIED_AGENT_RUN_NOT_DRIVEN" || error.code === "DENIED_AGENT_RUN_REQUIRED") {
      this.replaceClient();
      this.reducer = emptyState();
      this.omittedEventKeys.clear();
      this.stream.reset();
      this.active = null;
      this.resetObservedReplay();
      void this.workspaceState.update(RUN_CURSORS_KEY, undefined);
      this.resetProfileForEngine(this.selectedEngine);
      this.banner = {
        message: "Saved conversation is no longer available; start a new conversation",
        code: error.code ?? "STALE_PERSISTED_RUN",
      };
    } else {
      if (this.active) {
        this.active.state = "terminal";
        this.active.terminalState = error.code ?? "event-stream-error";
        this.active.readOnly = true;
      }
      this.banner = errorBanner(error, "Conversation event stream stopped");
    }
    this.broadcast();
  }

  private async resolveApproval(
    correlationId: string,
    decision: "approve" | "deny",
    agentRunId?: string,
    confirmation?: { expected_revision: number; snapshot_set_sha256: string; metadata_confirmed: boolean },
    answers?: Record<string, string>,
  ): Promise<void> {
    if (!this.active || this.active.readOnly || this.active.state === "terminal" || this.lifecyclePending) return;
    const runId = agentRunId || this.active.agentRunId;
    if (runId !== this.active.agentRunId) return;
    const correlationKey = scopedCorrelation(runId, correlationId);
    if (this.pendingApprovals.has(correlationKey)) return;
    this.pendingApprovals.add(correlationKey);
    this.broadcast();
    try {
      const response = await this.client.approve(correlationId, decision, this.active.sessionId, confirmation, answers);
      if (response.status === "OK") return;
      this.pendingApprovals.delete(correlationKey);
      this.banner = responseBanner(response, "Approval decision failed");
      if (response.code === "DENIED_APPROVAL_TIMEOUT") this.pauseFollowUps("approval_timeout");
      this.broadcast();
    } catch (error) {
      this.pendingApprovals.delete(correlationKey);
      this.banner = errorBanner(error, "Approval decision failed");
      this.pauseFollowUps("uncertain_transport");
      this.broadcast();
    }
  }

  private async killConversation(): Promise<void> {
    if (!this.active || this.active.readOnly || this.active.state === "terminal" || this.lifecyclePending) return;
    this.pauseFollowUps("kill");
    this.lifecyclePending = true;
    try {
      const response = await this.client.kill();
      if (response.status !== "OK") {
        this.banner = responseBanner(response, "Kill denied");
      } else {
        this.stream.clearFailure();
        this.active.state = "terminal";
        this.active.terminalState = "canceled";
        this.active.readOnly = true;
        this.pendingPrompt = false;
        this.settledPromptToken = this.pendingPromptToken;
        this.pendingPromptToken = undefined;
        this.persistActive();
      }
    } catch (error) {
      this.banner = { message: `Kill failed: ${errorText(error)}`, code: "DAEMON_UNREACHABLE" };
    } finally {
      this.lifecyclePending = false;
    }
    this.broadcast();
  }

  private async closeConversation(lockHeld = false, hasDraft = false): Promise<boolean> {
    if (!this.active || this.active.readOnly || this.active.state === "terminal") return false;
    if (this.lifecyclePending && !lockHeld) return false;
    if (this.active.state !== "idle") {
      this.denyLocal("Interrupt or kill the active turn before closing", "DENIED_SESSION_NOT_IDLE");
      return false;
    }
    if (!lockHeld && !(await this.confirmEphemeralLoss(hasDraft))) return false;
    if (!lockHeld) this.lifecyclePending = true;
    try {
      const response = await this.client.close();
      if (response.status !== "OK") {
        this.banner = responseBanner(response, "Session close denied");
        this.broadcast();
        return false;
      }
      this.active.state = "terminal";
      this.stream.clearFailure();
      this.active.terminalState = typeof response.terminal_state === "string" ? response.terminal_state : "success";
      this.active.readOnly = true;
      this.pendingPrompt = false;
      this.settledPromptToken = this.pendingPromptToken;
      this.pendingPromptToken = undefined;
      this.followUps.clear();
      this.queuedContexts.clear();
      this.queuedImages.clear();
      this.contextItems = [];
      this.imageItems = [];
      this.queuedDispatchId = undefined;
      this.persistActive();
      this.broadcast();
      return true;
    } catch (error) {
      this.banner = errorBanner(error, "Session close failed");
      this.broadcast();
      return false;
    } finally {
      if (!lockHeld) this.lifecyclePending = false;
    }
  }

  private async newConversation(hasDraft = false): Promise<void> {
    if (this.lifecyclePending) return;
    if (this.pendingPrompt) {
      this.denyLocal("New Conversation is disabled while a prompt is pending", "DENIED_TURN_IN_PROGRESS");
      return;
    }
    if (this.active?.state === "running" && !this.active.readOnly) {
      this.denyLocal("New Conversation is disabled while a turn is running", "DENIED_TURN_IN_PROGRESS");
      return;
    }
    if (!(await this.confirmEphemeralLoss(hasDraft))) return;
    this.lifecyclePending = true;
    try {
      if (this.active && this.active.state !== "terminal" && !this.active.readOnly) {
        if (!(await this.prompts.confirmNewConversation())) return;
        if (!(await this.closeConversation(true))) return;
      }
      this.replaceClient();
      this.reducer = emptyState();
      this.omittedEventKeys.clear();
      this.stream.reset();
      this.pendingApprovals.clear();
      this.diffApprovals.clear();
      this.invalidDiffApprovals.clear();
      this.active = null;
      this.pendingPrompt = false;
      this.pendingPromptToken = undefined;
      this.acceptedPromptToken = undefined;
      this.queuedPromptToken = undefined;
      this.settledPromptToken = undefined;
      this.followUps.clear();
      this.queuedContexts.clear();
      this.queuedImages.clear();
      this.contextItems = [];
      this.imageItems = [];
      this.queuedDispatchId = undefined;
      this.banner = undefined;
      this.resetObservedReplay();
      void this.workspaceState.update(RUN_CURSORS_KEY, undefined);
      this.resetProfileForEngine(this.selectedEngine);
    } finally {
      this.lifecyclePending = false;
    }
    this.broadcast();
  }

  private async openObserved(sessionId: string, hasDraft = false): Promise<void> {
    if (this.lifecyclePending) return;
    if (this.pendingPrompt) {
      this.denyLocal("Wait for the pending prompt before opening an observed conversation", "DENIED_TURN_IN_PROGRESS");
      return;
    }
    await this.refreshObserved();
    const record = this.observed.get(sessionId);
    if (!record) return;
    if (this.active?.state === "running" && !this.active.readOnly) {
      this.denyLocal("Interrupt or kill the active turn before opening an observed conversation", "DENIED_TURN_IN_PROGRESS");
      return;
    }
    if (!(await this.confirmEphemeralLoss(hasDraft))) return;
    this.lifecyclePending = true;
    try {
      if (this.active && this.active.controller === "driven" && this.active.state !== "terminal") {
        if (!(await this.prompts.confirmNewConversation())) return;
        if (!(await this.closeConversation(true))) return;
      }
      this.replaceClient();
      this.reducer = emptyState();
      this.omittedEventKeys.clear();
      this.stream.reset();
      this.pendingApprovals.clear();
      this.diffApprovals.clear();
      this.invalidDiffApprovals.clear();
      this.pendingPrompt = false;
      this.pendingPromptToken = undefined;
      this.acceptedPromptToken = undefined;
      this.queuedPromptToken = undefined;
      this.settledPromptToken = undefined;
      this.followUps.clear();
      this.queuedContexts.clear();
      this.queuedImages.clear();
      this.contextItems = [];
      this.imageItems = [];
      this.queuedDispatchId = undefined;
      this.resetObservedReplay();
      this.active = observedConversation(record);
      void this.workspaceState.update(RUN_CURSORS_KEY, undefined);
    } finally {
      this.lifecyclePending = false;
    }
    await this.refreshObserved();
    this.broadcast();
  }

  private async syncObservedRecord(record: ObservedRecord): Promise<void> {
    const runIds = record.runs.map((run) => run.id);
    const prefixStable = this.observedReplayRunIds.every((id, index) => runIds[index] === id);
    if (!prefixStable) {
      this.reducer = emptyState();
      this.diffApprovals.clear();
      this.invalidDiffApprovals.clear();
      this.resetObservedReplay();
    }
    let latestStatus: ReplayStatus | undefined;
    for (const run of record.runs) {
      const status = await this.replayObservedRun(run.id);
      if (run.id === runIds.at(-1)) latestStatus = status;
      if (!this.observedReplayRunIds.includes(run.id)) this.observedReplayRunIds.push(run.id);
    }
    if (!this.active || this.active.controller !== "observed" || this.active.sessionId !== record.sessionId) return;
    const latest = record.runs.at(-1);
    const terminal = latestStatus?.terminal ?? latest?.terminal ?? true;
    this.active.agentRunId = latest?.id ?? "";
    this.active.state = terminal ? "terminal" : "running";
    this.active.terminalState = terminal ? latestStatus?.terminalState ?? latest?.terminalState : undefined;
    this.active.readOnly = true;
  }

  private async replayObservedRun(runId: string): Promise<ReplayStatus> {
    let cursor = this.observedCursors.get(runId) ?? 0;
    let status: ReplayStatus = { cursor };
    for (let page = 0; page < 1_000; page += 1) {
      const response = await this.client.eventsOnce(runId, cursor, 1_000);
      assertOk(response, `Observed replay failed for ${runId}`);
      const events = Array.isArray(response.events) ? (response.events as WireEvent[]) : [];
      reduceEvents(this.reducer, events, runId);
      const next = typeof response.cursor === "number" ? response.cursor : cursor;
      status = {
        cursor: next,
        ...(typeof response.terminal === "boolean" ? { terminal: response.terminal } : {}),
        ...(typeof response.terminal_state === "string" ? { terminalState: response.terminal_state } : {}),
        ...(typeof response.turn_state === "string" ? { turnState: response.turn_state } : {}),
      };
      this.observedCursors.set(runId, next);
      if (events.length === 0 || next <= cursor) return status;
      cursor = next;
    }
    throw new SessionResponseError({
      status: "ERROR",
      code: "ERROR_REPLAY_PAGE_BOUND",
      message: `Observed replay exceeded the page bound for ${runId}`,
    });
  }

  private async control(label: string, action: () => Promise<Record<string, unknown>>): Promise<void> {
    if (!this.active || this.active.readOnly || this.active.state === "terminal" || this.lifecyclePending) return;
    try {
      const response = await action();
      if (response.status !== "OK") this.banner = responseBanner(response, `${label} denied`);
    } catch (error) {
      this.banner = { message: `${label} failed: ${errorText(error)}`, code: "DAEMON_UNREACHABLE" };
    }
    this.broadcast();
  }

  private async confirmEphemeralLoss(hasDraft: boolean): Promise<boolean> {
    const queuedCount = this.followUps.size;
    const contextCount = this.contextItems.length;
    const imageCount = this.imageItems.length;
    if (!hasDraft && queuedCount === 0 && contextCount === 0 && imageCount === 0) return true;
    return this.prompts.confirmEphemeralLoss?.({ hasDraft, queuedCount, contextCount, imageCount }) ?? true;
  }

  private denyLocal(message: string, code: string): void {
    this.banner = { message, code };
    this.broadcast();
  }

  private settleRejectedPrompt(token: string | undefined): void {
    if (!token) return;
    this.settledPromptToken = token;
    this.broadcast();
  }

  private resetObservedReplay(): void {
    this.observedCursors.clear();
    this.observedReplayRunIds = [];
  }

  private persistActive(): void {
    if (!this.active || this.active.controller !== "driven") return;
    const persisted: PersistedRun = {
      session_id: this.active.sessionId,
      agent_run_id: this.active.agentRunId,
      ...(this.active.profileHash ? { profile_hash: this.active.profileHash } : {}),
    };
    void this.workspaceState.update(RUN_CURSORS_KEY, persisted);
  }

  private send(renderer: RendererPort): void {
    void renderer.postMessage({ type: "snapshot", snapshot: this.snapshot() });
  }

  private broadcast(): void {
    for (const renderer of this.renderers.values()) this.send(renderer);
  }
}

function capabilityView(capability: EngineCapability): EngineCapabilityView {
  return {
    id: capability.id,
    label: capability.label,
    drivable: capability.drivable,
    availability: { ...capability.availability },
    models: (capability.models ?? []).map((model) => ({
      id: model.id,
      label: model.label,
      ...(model.description ? { description: model.description } : {}),
      reasoning_efforts: model.reasoning_efforts ?? [],
      ...(model.default_effort ? { default_effort: model.default_effort } : {}),
      supports_adaptive_thinking: model.supports_adaptive_thinking === true,
      supports_fast_mode: model.supports_fast_mode === true,
    })),
    ...(capability.default_model ? { default_model: capability.default_model } : {}),
    ...(capability.default_reasoning_effort
      ? { default_reasoning_effort: capability.default_reasoning_effort }
      : {}),
    auth_modes: capability.auth_modes ?? [],
    permission_modes: capability.permission_modes ?? [],
    warnings: capability.warnings ?? [],
    features: {
      streaming: capability.features.streaming === true,
      image_attachments: capability.features.image_attachments === true,
      governed_context: capability.features.governed_context === true,
      diff_snapshots: capability.features.diff_snapshots === true,
      writer_leasing: capability.features.writer_leasing === true,
      subscription_auth: capability.features.subscription_auth === true,
      controlled_tools: capability.features.controlled_tools === true,
      process_execution: capability.features.process_execution === true,
      test_extension: capability.features.test_extension === true,
    },
    ...(capability.runtime ? { runtime: { ...capability.runtime } } : {}),
    ...(capability.max_turns ? { maxTurns: { ...capability.max_turns } } : {}),
  };
}

function diffView(approval: PendingDiffApproval | undefined): { diff: NonNullable<import("./webviewProtocol").ApprovalView["diff"]> } | Record<string, never> {
  if (!approval) return {};
  return {
    diff: {
      status: approval.corrupt ? "corrupt" : "pending",
      revision: approval.diff.revision,
      snapshotSetSha256: approval.diff.snapshotSetSha256,
      metadataConfirmationRequired: approval.diff.metadataConfirmationRequired,
      files: approval.diff.fileChanges.map((change) => ({
        changeId: change.changeId,
        path: change.path,
        oldPath: change.oldPath,
        kind: change.kind,
        previewMode: change.previewMode,
        previewReason: change.previewReason,
        beforeSha256: change.before?.sha256 ?? null,
        proposedSha256: change.proposed?.sha256 ?? null,
      })),
    },
  };
}

function diffErrorView(error: { code: string; message: string } | undefined): { diffError: { code: string; message: string } } | Record<string, never> {
  return error ? { diffError: { ...error } } : {};
}

function explicitAnswerMap(value: unknown): Record<string, string> | undefined {
  if (value === undefined) return undefined;
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const entries = Object.entries(value as Record<string, unknown>);
  if (entries.length === 0 || entries.length > 32) return undefined;
  let totalBytes = 0;
  const answers: Array<[string, string]> = [];
  for (const [id, answer] of entries) {
    if (!id || Buffer.byteLength(id, "utf8") > 256 || typeof answer !== "string" || !answer.trim() ||
        Buffer.byteLength(answer, "utf8") > 64 * 1024 || answer.includes("\0") || hasUnpairedSurrogate(answer)) return undefined;
    totalBytes += Buffer.byteLength(id, "utf8") + Buffer.byteLength(answer, "utf8");
    if (totalBytes > 256 * 1024) return undefined;
    answers.push([id, answer]);
  }
  return Object.fromEntries(answers);
}

function hasUnpairedSurrogate(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code >= 0xd800 && code <= 0xdbff) {
      if (index + 1 >= value.length) return true;
      const next = value.charCodeAt(index + 1);
      if (next < 0xdc00 || next > 0xdfff) return true;
      index += 1;
    } else if (code >= 0xdc00 && code <= 0xdfff) {
      return true;
    }
  }
  return false;
}

function responseBanner(response: Record<string, unknown>, fallback: string): { message: string; code?: string } {
  const code = typeof response.code === "string" ? response.code : undefined;
  const message = typeof response.message === "string" && response.message ? response.message : fallback;
  return { message, ...(code ? { code } : {}) };
}

function sanitizedClaudeStartBanner(response: Record<string, unknown>): { message: string; code?: string } {
  const code = typeof response.code === "string" ? response.code : undefined;
  const message = code === "DENIED_AUTH_UNAVAILABLE" || code === "DENIED_AUTH_MODE_MISMATCH"
    ? "Claude subscription authentication is unavailable"
    : code === "DENIED_MODEL_UNAVAILABLE"
      ? "The selected Claude model is no longer available; refresh capabilities"
      : code === "DENIED_EFFORT_UNSUPPORTED"
        ? "The selected effort is no longer available for this Claude model; refresh capabilities"
        : code === "MODEL_CALL_BUDGET_EXHAUSTED"
          ? "The Claude round-trip ceiling is invalid or exhausted"
          : code === "DENIED_SDK_UNAVAILABLE"
            ? "Claude subscription runtime is unavailable"
            : "Claude session start denied";
  return { message, ...(code ? { code } : {}) };
}

function cleanOptional(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function cloneContext(item: StagedContext): StagedContext {
  return { ref: structuredClone(item.ref), label: item.label, bytes: item.bytes };
}

function cloneImage(item: StagedImage): StagedImage {
  return { ref: structuredClone(item.ref), label: item.label };
}

function cloneToolCard(card: ToolCard): ToolCard {
  return {
    ...card,
    ...(card.process ? { process: { ...card.process, argv: [...card.process.argv] } } : {}),
  };
}

function composerEnvelope(context: readonly StagedContext[], images: readonly StagedImage[]): TurnEnvelope {
  return {
    ...(context.length ? { context_refs: context.map((item) => structuredClone(item.ref)) } : {}),
    ...(images.length ? { attachments: images.map((item) => structuredClone(item.ref)) } : {}),
  };
}

function reducedFidelityNotice(): NonNullable<ActiveConversation["continuationNotice"]> {
  return {
    mode: "reduced",
    message: "This continuation was seeded from durable transcript text; original vendor context and old artifact bytes were not replayed.",
  };
}

function nonNegativeInteger(value: unknown): number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : 0;
}

function metadataArtifacts(value: unknown): Array<{ kind: string; name?: string; sha256?: string; bytes?: number }> {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry) => {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) return [];
    const row = entry as Record<string, unknown>;
    const kind = cleanOptional(row.kind ?? row.media_type) ?? "artifact";
    const name = cleanOptional(row.name);
    const sha256 = /^[0-9a-f]{64}$/i.test(cleanOptional(row.sha256) ?? "") ? cleanOptional(row.sha256) : undefined;
    const bytes = nonNegativeInteger(row.bytes);
    return [{ kind, ...(name ? { name } : {}), ...(sha256 ? { sha256 } : {}), ...(bytes ? { bytes } : {}) }];
  });
}

function uniqueArtifactMetadata(
  values: Array<{ kind: string; name?: string; sha256?: string; bytes?: number }>,
): Array<{ kind: string; name?: string; sha256?: string; bytes?: number }> {
  const seen = new Set<string>();
  return values.filter((value) => {
    const key = `${value.kind}\u0000${value.sha256 ?? ""}\u0000${value.name ?? ""}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function errorBanner(error: unknown, fallback: string): { message: string; code?: string } {
  if (error instanceof SessionResponseError) {
    return { message: error.message || fallback, ...(error.code ? { code: error.code } : {}) };
  }
  return { message: `${fallback}: ${errorText(error)}`, code: "DAEMON_UNREACHABLE" };
}

function assertOk(response: Record<string, unknown>, fallback: string): void {
  if (response.status !== "OK") throw new SessionResponseError({ ...response, message: response.message ?? fallback });
}

function canonicalEngine(engine: string): string {
  return engine === "claude_cli" ? "claude" : engine;
}

function canonicalCapabilities(capabilities: EngineCapability[]): EngineCapability[] {
  const aliases = new Map<string, EngineCapability>();
  const canonical = new Map<string, EngineCapability>();
  for (const capability of capabilities) {
    const id = canonicalEngine(capability.id);
    const normalized = { ...capability, id };
    if (capability.id === id) canonical.set(id, normalized);
    else if (!aliases.has(id)) aliases.set(id, normalized);
  }
  for (const [id, capability] of aliases) {
    if (!canonical.has(id)) canonical.set(id, capability);
  }
  return [...canonical.values()];
}

function normalizeProfile(capability: EngineCapability, profile: ProfileSelection): ConversationProfile {
  const model = cleanOptional(profile.model);
  const isClaude = capability.id === "claude";
  const selectedModel = (capability.models ?? []).find((entry) => entry.id === (model ?? capability.default_model));
  const requestedEffort = cleanOptional(profile.reasoning_effort);
  const allowedEfforts = selectedModel?.reasoning_efforts ?? [];
  const defaultEffort = cleanOptional(selectedModel?.default_effort ?? capability.default_reasoning_effort);
  const effort =
    isClaude && model && !selectedModel
      ? requestedEffort
      : requestedEffort && allowedEfforts.includes(requestedEffort)
      ? requestedEffort
      : defaultEffort && allowedEfforts.includes(defaultEffort)
        ? defaultEffort
        : isClaude ? allowedEfforts[0] : undefined;
  const permissionMode = capability.permission_modes.includes(profile.permission_mode)
    ? profile.permission_mode
    : capability.permission_modes.includes("plan")
      ? "plan"
      : capability.permission_modes[0] ?? "plan";
  const authMode = isClaude
    ? capability.auth_modes.includes("subscription") ? "subscription" : "none"
    : capability.auth_modes.includes(profile.auth_mode)
      ? profile.auth_mode
      : capability.auth_modes[0] ?? "none";
  const bounds = capability.max_turns;
  const requestedMaxTurns = profile.max_turns;
  const maxTurns = bounds && typeof requestedMaxTurns === "number" && Number.isSafeInteger(requestedMaxTurns)
    && requestedMaxTurns >= bounds.min && requestedMaxTurns <= bounds.max
    ? requestedMaxTurns
    : bounds?.default;
  return {
    ...(model ? { model } : {}),
    ...(effort ? { reasoning_effort: effort } : {}),
    ...(isClaude && maxTurns !== undefined ? { max_turns: maxTurns } : {}),
    permission_mode: permissionMode,
    auth_mode: authMode,
  };
}

function defaultProfile(capability: EngineCapability): ConversationProfile {
  return normalizeProfile(capability, {
    ...(capability.default_model ? { model: capability.default_model } : {}),
    permission_mode: capability.permission_modes.includes("plan") ? "plan" : capability.permission_modes[0] ?? "plan",
    auth_mode: capability.auth_modes[0] ?? "none",
  });
}

function claudeProfileDenial(
  capability: EngineCapability,
  profile: ConversationProfile,
): { message: string; code: string } | undefined {
  if (capability.id !== "claude") return undefined;
  if (capability.runtime && capability.runtime.status !== "ready") {
    return capability.runtime.status === "auth_required"
      ? { message: "Claude subscription authentication is required outside Kaizen", code: "DENIED_AUTH_UNAVAILABLE" }
      : { message: "Claude subscription runtime is unavailable", code: "DENIED_SDK_UNAVAILABLE" };
  }
  const selectedModel = capability.models.find((model) => model.id === profile.model);
  if (!selectedModel) return { message: "Select an available Claude model", code: "DENIED_MODEL_UNAVAILABLE" };
  const advertisedEfforts = selectedModel.reasoning_efforts ?? [];
  const effortInvalid = advertisedEfforts.length > 0
    ? !profile.reasoning_effort || !advertisedEfforts.includes(profile.reasoning_effort)
    : profile.reasoning_effort !== undefined;
  if (effortInvalid) {
    return { message: "Select an effort advertised for this Claude model", code: "DENIED_EFFORT_UNSUPPORTED" };
  }
  if (profile.auth_mode !== "subscription" || !capability.auth_modes.includes("subscription")) {
    return { message: "Claude requires existing subscription authentication", code: "DENIED_AUTH_MODE_MISMATCH" };
  }
  if (capability.runtime && (!capability.max_turns || profile.max_turns === undefined)) {
    return { message: "Claude workload bounds are unavailable", code: "MODEL_CALL_BUDGET_EXHAUSTED" };
  }
  return undefined;
}

function observedConversation(record: ObservedRecord): ActiveConversation {
  const latest = record.runs.at(-1);
  return {
    sessionId: record.sessionId,
    agentRunId: latest?.id ?? "",
    engine: "claude",
    controller: "observed",
    state: latest?.terminal === false ? "running" : "terminal",
    ...(latest?.terminalState ? { terminalState: latest.terminalState } : {}),
    readOnly: true,
  };
}

function observedRecords(response: Record<string, unknown>): Map<string, ObservedRecord> {
  const out = new Map<string, ObservedRecord>();
  const rows = Array.isArray(response.sessions) ? response.sessions : Array.isArray(response.records) ? response.records : [];
  for (const raw of rows) {
    if (!raw || typeof raw !== "object") continue;
    const row = raw as Record<string, unknown>;
    if (row.controller !== undefined && row.controller !== "observed") continue;
    const session = row.session && typeof row.session === "object" ? (row.session as Record<string, unknown>) : row;
    const sessionId = cleanOptional(session.id ?? row.session_id);
    if (!sessionId) continue;
    const runs = Array.isArray(row.runs) ? row.runs : [];
    const runRecords = runs
      .map((run): ObservedRunRecord | undefined => {
        if (!run || typeof run !== "object") return undefined;
        const data = run as Record<string, unknown>;
        const id = cleanOptional(data.id ?? data.agent_run_id);
        if (!id) return undefined;
        const terminalState = cleanOptional(data.terminal_state);
        const turnState = cleanOptional(data.turn_state);
        return {
          id,
          terminal: data.terminal === true || turnState === "terminal" || terminalState !== undefined,
          ...(terminalState ? { terminalState } : {}),
          ...(turnState ? { turnState } : {}),
        };
      })
      .filter((run): run is ObservedRunRecord => !!run);
    const label = cleanOptional(session.summary) ?? `Observed Claude ${sessionId.slice(0, 8)}`;
    out.set(sessionId, { sessionId, label, runs: runRecords });
  }
  return out;
}

function drivenMetadata(
  response: Record<string, unknown>,
  sessionId: string,
  agentRunId: string,
): {
  engine?: string;
  profile?: ConversationProfile;
  legacy?: boolean;
  terminal?: boolean;
  terminalState?: string;
  turnState?: string;
  linkedRunIds?: string[];
  resumable?: boolean;
  continuationNotice?: ActiveConversation["continuationNotice"];
} {
  const rows = Array.isArray(response.sessions) ? response.sessions : Array.isArray(response.records) ? response.records : [];
  for (const raw of rows) {
    if (!raw || typeof raw !== "object") continue;
    const row = raw as Record<string, unknown>;
    const session = row.session && typeof row.session === "object" ? (row.session as Record<string, unknown>) : row;
    const id = cleanOptional(session.id ?? row.session_id);
    const runs = Array.isArray(row.runs) ? (row.runs as Array<Record<string, unknown>>) : [];
    const run = runs.find((entry) => entry && (entry.id === agentRunId || entry.agent_run_id === agentRunId));
    if (id !== sessionId && !run) continue;
    const engine = cleanOptional(row.engine ?? session.engine ?? run?.engine);
    const runProfile = run?.profile;
    const profile = conversationProfile(runProfile) ?? conversationProfile(row.profile ?? row.profile_summary ?? session);
    const terminalState = cleanOptional(run?.terminal_state ?? row.latest_terminal_state);
    const turnState = cleanOptional(run?.turn_state ?? row.latest_run_state);
    const terminal = run?.terminal === true || row.latest_run_state === "terminal" || terminalState !== undefined;
    const linkedRunIds = runs
      .map((entry) => cleanOptional(entry.id ?? entry.agent_run_id))
      .filter((value): value is string => !!value);
    const history = historyFromSessionList(response).entries.find((entry) => entry.sessionId === sessionId);
    const continuationNotice = history && (
      history.fidelity?.mode === "reduced" || history.omittedMessages || history.expiredArtifacts?.length
    ) ? {
      mode: "reduced" as const,
      message: history.fidelity?.message ?? "Some durable conversation metadata is unavailable; old bytes were not replayed.",
      ...(history.omittedMessages ? { omittedMessages: history.omittedMessages } : {}),
      ...(history.expiredArtifacts?.length ? { expiredArtifacts: history.expiredArtifacts } : {}),
    } : undefined;
    return {
      ...(engine ? { engine } : {}),
      ...(profile ? { profile } : { legacy: true }),
      ...(terminal ? { terminal: true } : {}),
      ...(terminalState ? { terminalState } : {}),
      ...(turnState ? { turnState } : {}),
      ...(linkedRunIds.length ? { linkedRunIds } : {}),
      ...(typeof row.resumable === "boolean" ? { resumable: row.resumable } : {}),
      ...(continuationNotice ? { continuationNotice } : {}),
    };
  }
  return {};
}

function conversationEntry(row: Record<string, unknown>): { sessionId: string; agentRunId: string; label: string } | null {
  if (row.controller !== undefined && row.controller !== "driven") return null;
  const sessionId = cleanOptional(row.session_id);
  const agentRunId = cleanOptional(row.latest_run_id);
  if (!sessionId || !agentRunId) return null;
  const profile = row.profile && typeof row.profile === "object" ? (row.profile as Record<string, unknown>) : {};
  const model = cleanOptional(profile.requested_model);
  const engine = cleanOptional(row.engine) ?? "engine?";
  const created = cleanOptional(row.created_at) ?? "";
  const stamp = created.length >= 16 ? `${created.slice(5, 10)} ${created.slice(11, 16)}` : created;
  return { sessionId, agentRunId, label: `${engine} · ${model ?? "default"}${stamp ? ` · ${stamp}` : ""}` };
}

function conversationProfile(value: unknown): ConversationProfile | undefined {
  if (!value || typeof value !== "object") return undefined;
  const row = value as Record<string, unknown>;
  const permissionMode = row.permission_mode;
  const authMode = row.auth_mode;
  if (!isPermissionMode(permissionMode) || !isAuthMode(authMode)) return undefined;
  const model = cleanOptional(row.model ?? row.requested_model);
  const effort = cleanOptional(row.reasoning_effort ?? row.requested_reasoning_effort);
  const maxTurns = typeof row.max_turns === "number" && Number.isSafeInteger(row.max_turns) && row.max_turns >= 1
    ? row.max_turns
    : undefined;
  return {
    ...(model ? { model } : {}),
    ...(effort ? { reasoning_effort: effort } : {}),
    ...(maxTurns ? { max_turns: maxTurns } : {}),
    permission_mode: permissionMode,
    auth_mode: authMode,
  };
}

function isPermissionMode(value: unknown): value is ConversationProfile["permission_mode"] {
  return value === "plan" || value === "ask" || value === "agent" || value === "full";
}

function isAuthMode(value: unknown): value is ConversationProfile["auth_mode"] {
  return value === "none" || value === "subscription" || value === "api-key";
}
