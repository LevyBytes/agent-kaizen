/** Pure Node client for the frozen Harness UI session wire. */

import { DaemonUnreachable, LoopbackResponse, request } from "./protocol";
import type { WireEvent } from "./webviewProtocol";

const POLL_WAIT_SECONDS = 25;
const POLL_TIMEOUT_MS = 30_000;
const BACKOFF_START_MS = 500;
const BACKOFF_CAP_MS = 5_000;
const CONTROL_TIMEOUT_MS = 10_000;
// Worst bounded refresh: Ollama 2s + Codex 30s + Claude open 30s + catalog 30s + six 5s feature
// probes + 15s fail-closed teardown = 137s. Keep one 210s transport envelope for startup overhead.
const CAPABILITY_REFRESH_TIMEOUT_MS = 210_000;
const MAX_TURNS_CEILING = 32;

export type PermissionMode = "plan" | "ask" | "agent" | "full";
export type AuthMode = "none" | "subscription" | "api-key";

export interface EngineFeatures {
  streaming: boolean;
  image_attachments: boolean;
  governed_context: boolean;
  diff_snapshots: boolean;
  writer_leasing: boolean;
  subscription_auth: boolean;
  controlled_tools: boolean;
  process_execution: boolean;
  test_extension: boolean;
}

/** Fail closed by materializing every known feature as a strict boolean. */
export function normalizeFeatures(value: unknown): EngineFeatures {
  const source = typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
  return {
    streaming: source.streaming === true,
    image_attachments: source.image_attachments === true,
    governed_context: source.governed_context === true,
    diff_snapshots: source.diff_snapshots === true,
    writer_leasing: source.writer_leasing === true,
    subscription_auth: source.subscription_auth === true,
    controlled_tools: source.controlled_tools === true,
    process_execution: source.process_execution === true,
    test_extension: source.test_extension === true,
  };
}

export interface ImageRef {
  id: string;
  kind: "image";
  artifact_ref: string;
  sha256: string;
  bytes: number;
  media_type: "image/png" | "image/jpeg" | "image/webp" | "image/gif";
  name?: string;
}

export interface ContextRange {
  start: { line: number; character: number };
  end: { line: number; character: number };
}

export type ContextRef =
  | { id: string; kind: "file"; source_path: string }
  | {
      id: string;
      kind: "selection";
      source_path: string;
      range: ContextRange;
      snapshot_ref: string;
      sha256: string;
      bytes: number;
      encoding: "utf-8";
    };

export interface ConversationProfile {
  model?: string;
  reasoning_effort?: string;
  max_turns?: number;
  permission_mode: PermissionMode;
  auth_mode: AuthMode;
}

export interface StartRequest {
  engine: string;
  prompt: string;
  profile: ConversationProfile;
  full_opt_in?: boolean;
  model?: string;
  max_turns?: number;
  title?: string;
  attachments?: ImageRef[];
  context_refs?: ContextRef[];
  client_features?: { diff_snapshots?: boolean };
}

export interface TurnEnvelope {
  attachments?: ImageRef[];
  context_refs?: ContextRef[];
}

export interface StartResult extends LoopbackResponse {
  status: string;
  session_id?: string;
  agent_run_id?: string;
  engine?: string;
  profile?: ConversationProfile;
  profile_hash?: string;
}

export interface CapabilityModel {
  id: string;
  label: string;
  description?: string;
  reasoning_efforts?: string[];
  default_effort?: string;
  supports_adaptive_thinking?: boolean;
  supports_fast_mode?: boolean;
}

export interface RuntimeCapability {
  kind: string;
  version?: string;
  status: "ready" | "auth_required" | "unavailable";
}

export interface MaxTurnsBounds {
  min: number;
  default: number;
  max: number;
}

export interface EngineCapability {
  id: string;
  label: string;
  drivable: boolean;
  availability: { state: string; code?: string; message?: string };
  models: CapabilityModel[];
  default_model?: string;
  default_reasoning_effort?: string;
  auth_modes: AuthMode[];
  permission_modes: PermissionMode[];
  warnings: string[];
  features: EngineFeatures;
  runtime?: RuntimeCapability;
  max_turns?: MaxTurnsBounds;
}

export interface CapabilitiesResult extends LoopbackResponse {
  engines?: EngineCapability[];
}

export interface SessionDelta {
  seq: number;
  turn_id: string;
  text: string;
}

export interface SessionEventsResult extends LoopbackResponse {
  events?: WireEvent[];
  cursor?: number;
  terminal?: boolean;
  terminal_state?: string;
  turn_state?: string;
  deltas?: SessionDelta[];
  delta_cursor?: number;
  delta_dropped?: boolean;
}

export interface SessionListEntry {
  session_id: string;
  title: string | null;
  snippet: string | null;
  [key: string]: unknown;
}

export interface SessionListResult extends LoopbackResponse {
  sessions?: SessionListEntry[];
}

export type Decision = "approve" | "deny";

/** A structured non-OK daemon answer. Unlike a transport outage, retrying the same cursor is unsafe. */
export class SessionResponseError extends Error {
  readonly code?: string;

  constructor(readonly response: LoopbackResponse) {
    const code = typeof response.code === "string" ? response.code : undefined;
    const detail =
      typeof response.message === "string"
        ? response.message
        : typeof response.required_action === "string"
          ? response.required_action
          : code ?? String(response.status ?? "session request failed");
    super(detail);
    this.name = "SessionResponseError";
    this.code = code;
  }
}

export interface SessionClientCallbacks {
  onEvents?: (events: WireEvent[], cursor: number) => void;
  onDeltas?: (deltas: SessionDelta[], deltaCursor: number, dropped: boolean) => void;
  onTurnState?: (turnState: string, terminal: boolean) => void;
  onTerminal?: (terminalState: string | undefined) => void;
  onError?: (err: Error) => void;
}

export type RequestFn = (op: string, args: Record<string, unknown>, timeoutMs?: number) => Promise<LoopbackResponse>;

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

/** Own one active run and one generation-scoped event pump; replace the client to re-drive. */
export class SessionClient {
  private readonly req: RequestFn;
  private readonly cb: SessionClientCallbacks;
  private agentRunId: string | null = null;
  private sessionId: string | null = null;
  private cursor = 0;
  private deltaCursor = 0;
  private streamingEnabled = false;
  private disposed = false;
  private runGeneration = 0;
  private pumpingGeneration: number | undefined;

  constructor(repoRootOrReq: string | RequestFn, callbacks: SessionClientCallbacks = {}) {
    this.req =
      typeof repoRootOrReq === "string"
        ? (op, args, timeoutMs) => request(repoRootOrReq, op, args, timeoutMs)
        : repoRootOrReq;
    this.cb = callbacks;
  }

  get runId(): string | null {
    return this.agentRunId;
  }

  get session(): string | null {
    return this.sessionId;
  }

  /** Use the 210-second probe envelope only for an explicit heavy refresh. */
  async capabilities(refresh = false): Promise<CapabilitiesResult> {
    const timeout = refresh ? CAPABILITY_REFRESH_TIMEOUT_MS : CONTROL_TIMEOUT_MS;
    const result = (await this.req("session/capabilities", { refresh }, timeout)) as CapabilitiesResult;
    if (Array.isArray(result.engines)) {
      result.engines = result.engines
        .flatMap((engine) => normalizeEngineCapability(engine));
    } else {
      result.engines = [];
    }
    return result;
  }

  async list(args: { controller?: "driven" | "observed"; limit?: number } = {}): Promise<SessionListResult> {
    const result = await this.req("session/list", args, CONTROL_TIMEOUT_MS) as SessionListResult;
    if (Array.isArray(result.sessions)) {
      result.sessions = result.sessions
        .filter((session): session is SessionListEntry => typeof session === "object" && session !== null)
        .map((session) => ({
          ...session,
          title: typeof session.title === "string" ? session.title : null,
          snippet: typeof session.snippet === "string" ? session.snippet : null,
        }));
    } else {
      result.sessions = [];
    }
    return result;
  }

  async eventsOnce(agentRunId: string, since = 0, limit?: number, deltaSince?: number): Promise<SessionEventsResult> {
    const args: Record<string, unknown> = { agent_run_id: agentRunId, since, wait: 0 };
    if (limit !== undefined) args.limit = limit;
    if (deltaSince !== undefined) args.delta_since = deltaSince;
    return this.req("session/events", args, CONTROL_TIMEOUT_MS) as Promise<SessionEventsResult>;
  }

  /** Start a run, reset cursors, and defer pumping until the owner can persist identifiers. */
  async start(reqBody: StartRequest): Promise<StartResult> {
    const resp = (await this.req("session/start", { ...reqBody }, CONTROL_TIMEOUT_MS)) as StartResult;
    if (resp.status === "OK" && typeof resp.agent_run_id === "string") {
      this.agentRunId = resp.agent_run_id;
      this.sessionId = typeof resp.session_id === "string" ? resp.session_id : null;
      this.cursor = 0;
      this.deltaCursor = 0;
      const generation = ++this.runGeneration;
      // Let the owner bind/persist the returned identifiers before callbacks can arrive from a very
      // fast local daemon. resume() is already called only after the controller has restored its state.
      if (!this.disposed) setTimeout(() => void this.pump(generation), 0);
    }
    return resp;
  }

  resume(agentRunId: string, sessionId: string | null = null): void {
    this.agentRunId = agentRunId;
    this.sessionId = sessionId;
    this.cursor = 0;
    this.deltaCursor = 0;
    const generation = ++this.runGeneration;
    if (!this.disposed) void this.pump(generation);
  }

  setStreaming(enabled: boolean): void {
    this.streamingEnabled = enabled === true;
  }

  turn(prompt: string, envelope: TurnEnvelope = {}): Promise<LoopbackResponse> {
    return this.control("session/turn", { prompt, ...envelope });
  }

  steer(instruction: string): Promise<LoopbackResponse> {
    return this.control("session/steer", { instruction });
  }

  interrupt(): Promise<LoopbackResponse> {
    return this.control("session/interrupt", {});
  }

  close(): Promise<LoopbackResponse> {
    return this.control("session/close", {});
  }

  async kill(): Promise<LoopbackResponse> {
    const resp = await this.control("session/kill", {});
    if (resp.status === "OK") this.disposed = true;
    return resp;
  }

  /** Send confirmation only for approval; omit an unavailable session id. */
  approve(
    correlationId: string,
    decision: Decision,
    sessionId?: string,
    confirmation?: { expected_revision: number; snapshot_set_sha256: string; metadata_confirmed: boolean },
    answers?: Record<string, string>,
  ): Promise<LoopbackResponse> {
    const sid = sessionId ?? this.sessionId;
    return this.req(
      "approve",
      {
        correlation_id: correlationId,
        ...(sid ? { session_id: sid } : {}),
        decision,
        ...(decision === "approve" ? confirmation : undefined),
        ...(decision === "approve" && answers ? { answers } : {}),
      },
      CONTROL_TIMEOUT_MS,
    );
  }

  dispose(): void {
    this.disposed = true;
    this.runGeneration += 1;
  }

  /** Run one generation-scoped long poll with unreachable-only backoff and durable event authority. */
  private async pump(generation: number): Promise<void> {
    if (this.pumpingGeneration === generation || generation !== this.runGeneration || this.agentRunId === null) return;
    this.pumpingGeneration = generation;
    const agentRunId = this.agentRunId;
    let backoff = BACKOFF_START_MS;
    try {
      while (!this.disposed && generation === this.runGeneration) {
        let resp: LoopbackResponse;
        try {
          const args: Record<string, unknown> = {
            agent_run_id: agentRunId,
            since: this.cursor,
            wait: POLL_WAIT_SECONDS,
          };
          if (this.streamingEnabled) args.delta_since = this.deltaCursor;
          resp = await this.req("session/events", args, POLL_TIMEOUT_MS);
          if (this.disposed || generation !== this.runGeneration) break;
          if (resp.status !== "OK") {
            this.cb.onError?.(new SessionResponseError(resp));
            break;
          }
          backoff = BACKOFF_START_MS;
        } catch (error) {
          if (this.disposed || generation !== this.runGeneration) break;
          if (error instanceof DaemonUnreachable) {
            this.cb.onError?.(error);
            await sleep(backoff);
            backoff = Math.min(backoff * 2, BACKOFF_CAP_MS);
            continue;
          }
          this.cb.onError?.(error instanceof Error ? error : new Error(String(error)));
          break;
        }
        if (this.disposed || generation !== this.runGeneration) break;

        const priorCursor = this.cursor;
        const events = ((resp.events as WireEvent[] | undefined) ?? []).filter(
          (event) => event.sequence_no > priorCursor,
        );
        this.cursor = events.reduce(
          (cursor, event) => Number.isSafeInteger(event.sequence_no) ? Math.max(cursor, event.sequence_no) : cursor,
          this.cursor,
        );
        const deltas = ((resp.deltas as SessionDelta[] | undefined) ?? []).filter(
          (delta) => typeof delta === "object" && delta !== null && Number.isSafeInteger(delta.seq) && delta.seq > 0 &&
            typeof delta.turn_id === "string" && typeof delta.text === "string",
        );
        const validDeltaCursor = typeof resp.delta_cursor === "number" && Number.isSafeInteger(resp.delta_cursor) && resp.delta_cursor >= 0;
        const hasDeltaState = this.streamingEnabled && (Array.isArray(resp.deltas) || validDeltaCursor || resp.delta_dropped === true);
        if (this.streamingEnabled && validDeltaCursor) this.deltaCursor = Math.max(this.deltaCursor, resp.delta_cursor as number);
        if (hasDeltaState) this.cb.onDeltas?.(deltas, this.deltaCursor, resp.delta_dropped === true);
        // Durable events are authoritative and reconcile after same-response ephemeral chunks.
        if (events.length > 0) this.cb.onEvents?.(events, this.cursor);
        if (typeof resp.turn_state === "string") {
          this.cb.onTurnState?.(resp.turn_state, resp.terminal === true);
        }
        if (resp.terminal === true && events.length === 0) {
          this.cb.onTerminal?.(typeof resp.terminal_state === "string" ? resp.terminal_state : undefined);
          break;
        }
      }
    } finally {
      if (this.pumpingGeneration === generation) this.pumpingGeneration = undefined;
    }
  }

  /** Return NO_ACTIVE_RUN when unbound; otherwise inject the active run id. */
  private control(op: string, extra: Record<string, unknown>): Promise<LoopbackResponse> {
    if (this.agentRunId === null) {
      return Promise.resolve({ status: "ERROR", code: "NO_ACTIVE_RUN" });
    }
    return this.req(op, { agent_run_id: this.agentRunId, ...extra }, CONTROL_TIMEOUT_MS);
  }
}

const PERMISSION_MODES = new Set<PermissionMode>(["plan", "ask", "agent", "full"]);
const AUTH_MODES = new Set<AuthMode>(["none", "subscription", "api-key"]);
const RUNTIME_STATES = new Set<RuntimeCapability["status"]>(["ready", "auth_required", "unavailable"]);

/** Fail-closed normalization for additive capability fields; malformed optional fields disappear. */
export function normalizeEngineCapability(value: unknown): EngineCapability[] {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  const source = value as Record<string, unknown>;
  const id = boundedText(source.id, 128);
  const label = boundedText(source.label, 256);
  if (!id || !label || typeof source.drivable !== "boolean") return [];
  const availabilitySource = recordOf(source.availability);
  const state = boundedText(availabilitySource.state, 64) ?? "unavailable";
  const code = boundedText(availabilitySource.code, 128);
  const message = boundedText(availabilitySource.message, 512);
  const models = Array.isArray(source.models) ? source.models.flatMap(normalizeModel) : [];
  const defaultModel = boundedText(source.default_model, 256);
  const defaultEffort = boundedText(source.default_reasoning_effort, 64);
  const normalizedRuntime = normalizeRuntime(source.runtime);
  const runtime = normalizedRuntime && state === "auth_required"
    ? { ...normalizedRuntime, status: "auth_required" as const }
    : normalizedRuntime;
  const maxTurns = normalizeMaxTurns(source.max_turns);
  return [{
    id,
    label,
    drivable: source.drivable,
    availability: { state, ...(code ? { code } : {}), ...(message ? { message } : {}) },
    models,
    ...(defaultModel && models.some((model) => model.id === defaultModel) ? { default_model: defaultModel } : {}),
    ...(defaultEffort ? { default_reasoning_effort: defaultEffort } : {}),
    auth_modes: arrayOf(source.auth_modes, AUTH_MODES),
    permission_modes: arrayOf(source.permission_modes, PERMISSION_MODES),
    warnings: Array.isArray(source.warnings)
      ? source.warnings.flatMap((warning) => boundedText(warning, 512) ?? []).slice(0, 16)
      : [],
    features: normalizeFeatures(source.features),
    ...(runtime ? { runtime } : {}),
    ...(maxTurns ? { max_turns: maxTurns } : {}),
  }];
}

function normalizeModel(value: unknown): CapabilityModel[] {
  const source = recordOf(value);
  const id = boundedText(source.id, 256);
  const label = boundedText(source.label, 256);
  if (!id || !label) return [];
  const reasoningEfforts = Array.isArray(source.reasoning_efforts)
    ? [...new Set(source.reasoning_efforts.flatMap((effort) => boundedText(effort, 64) ?? []))]
    : [];
  const defaultEffort = boundedText(source.default_effort, 64);
  const description = boundedText(source.description, 1024);
  return [{
    id,
    label,
    ...(description ? { description } : {}),
    reasoning_efforts: reasoningEfforts,
    ...(defaultEffort && reasoningEfforts.includes(defaultEffort) ? { default_effort: defaultEffort } : {}),
    supports_adaptive_thinking: source.supports_adaptive_thinking === true,
    supports_fast_mode: source.supports_fast_mode === true,
  }];
}

function normalizeRuntime(value: unknown): RuntimeCapability | undefined {
  const source = recordOf(value);
  const kind = boundedText(source.kind, 128);
  const status = boundedText(source.status, 32);
  if (!kind || !status || !RUNTIME_STATES.has(status as RuntimeCapability["status"])) return undefined;
  const version = boundedText(source.version, 128);
  return { kind, status: status as RuntimeCapability["status"], ...(version ? { version } : {}) };
}

function normalizeMaxTurns(value: unknown): MaxTurnsBounds | undefined {
  const source = recordOf(value);
  const minimum = safeInteger(source.min);
  const fallback = safeInteger(source.default);
  const maximum = safeInteger(source.max);
  if (minimum === undefined || fallback === undefined || maximum === undefined) return undefined;
  if (minimum < 1 || minimum > fallback || fallback > maximum || maximum > MAX_TURNS_CEILING) return undefined;
  return { min: minimum, default: fallback, max: maximum };
}

function recordOf(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function boundedText(value: unknown, max: number): string | undefined {
  if (typeof value !== "string") return undefined;
  const text = value.trim();
  return text && text.length <= max ? text : undefined;
}

function safeInteger(value: unknown): number | undefined {
  return typeof value === "number" && Number.isSafeInteger(value) ? value : undefined;
}

function arrayOf<T extends string>(value: unknown, allowed: ReadonlySet<T>): T[] {
  return Array.isArray(value)
    ? [...new Set(value.filter((entry): entry is T => typeof entry === "string" && allowed.has(entry as T)))]
    : [];
}
