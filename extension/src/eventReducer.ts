/** Durable-event reducer; cards and approvals are scoped by run plus NUL-delimited correlation id. */

import type { ApprovalQuestionView, CorrelationId, WireEvent } from "./webviewProtocol";

export const LEGACY_RUN_ID = "legacy";

export type ToolCardStatus = "running" | "ok" | "error" | "blocked";
export type ApprovalCardStatus = "pending" | "approved" | "denied" | "timed_out";

export interface ToolCard {
  timelineKey: string;
  agentRunId: string;
  sequenceNo: number;
  correlationId: CorrelationId;
  tool: string;
  status: ToolCardStatus;
  summary: string;
  code?: string;
  process?: ProcessCard;
}

export interface ProcessCard {
  executable: string;
  argv: string[];
  cwd: string;
  timeoutMs: number;
  decision: string;
  exitCode?: number;
  timedOut?: boolean;
  truncated?: boolean;
  stdoutSha256?: string;
  stdoutBytes?: number;
  effectsUnknown: true;
}

export interface ApprovalCard {
  timelineKey: string;
  agentRunId: string;
  sequenceNo: number;
  correlationId: CorrelationId;
  status: ApprovalCardStatus;
  summary: string;
  code?: string;
  questions?: ApprovalQuestionView[];
}

export interface TranscriptMessage {
  role: "user" | "assistant" | "system";
  text: string;
  source: "driven" | "observed";
  turnId?: string;
}

interface TimelineBase {
  key: string;
  agentRunId: string;
  sequenceNo: number;
}

export interface TimelineMessage extends TimelineBase {
  kind: "message";
  message: TranscriptMessage;
}

export interface TimelineTool extends TimelineBase {
  kind: "tool";
  card: ToolCard;
}

export interface TimelineApproval extends TimelineBase {
  kind: "approval";
  approval: ApprovalCard;
}

export type TimelineItem = TimelineMessage | TimelineTool | TimelineApproval;

export interface ReducerState {
  /** Maps are scoped by agent_run_id + correlation_id, never by a global correlation id. */
  cards: Map<string, ToolCard>;
  approvals: Map<string, ApprovalCard>;
  transcript: TranscriptMessage[];
  timeline: TimelineItem[];
  /** Constant-time lookup of an event's stable position in the timeline. */
  timelineIndex: Map<string, number>;
  seenEventKeys: Set<string>;
  turnState: "idle" | "running";
  terminal: boolean;
  terminalState?: string;
  sawChatMessages: boolean;
}

export function emptyState(): ReducerState {
  return {
    cards: new Map(),
    approvals: new Map(),
    transcript: [],
    timeline: [],
    timelineIndex: new Map(),
    seenEventKeys: new Set(),
    turnState: "idle",
    terminal: false,
    sawChatMessages: false,
  };
}

export interface ToolCardOp {
  kind: "toolCard";
  card: ToolCard;
}

export interface ApprovalRequestOp {
  kind: "approvalRequest";
  approval: ApprovalCard;
}

export interface ApprovalUpdateOp {
  kind: "approvalUpdate";
  approval: ApprovalCard;
}

export interface BlockedToolOp {
  kind: "blockedTool";
  correlationId: CorrelationId;
  summary: string;
  code?: string;
}

export interface TurnDoneOp {
  kind: "turnDone";
  terminalState?: string;
}

export interface RunDoneOp {
  kind: "runDone";
  terminalState?: string;
}

export interface ChatMessageOp {
  kind: "chatMessage";
  message: TranscriptMessage;
}

/** Legacy compatibility output for hosts that still consume finalization text directly. */
export interface AssistantTextOp {
  kind: "assistantText";
  text: string;
}

export type ReducerOp =
  | ToolCardOp
  | ApprovalRequestOp
  | ApprovalUpdateOp
  | BlockedToolOp
  | TurnDoneOp
  | RunDoneOp
  | ChatMessageOp
  | AssistantTextOp;

const TOOL_ERROR = "TOOL_ERROR";
const TURN_CLOSE_MARKERS = new Set(["close_ok", "close_fail", "close_canceled"]);

export function timelineIdentity(agentRunId: string, sequenceNo: number): string {
  return `${agentRunId}:${sequenceNo}`;
}

/** Scope a correlation id to its run with an unambiguous NUL delimiter. */
export function scopedCorrelation(agentRunId: string, correlationId: string): string {
  return `${agentRunId}\u0000${correlationId}`;
}

function decodeBody(ev: WireEvent): Record<string, unknown> {
  if (typeof ev.body !== "string" || ev.body.length === 0) return {};
  try {
    const parsed: unknown = JSON.parse(ev.body);
    return parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

function approvalQuestionsOf(body: Record<string, unknown>): ApprovalQuestionView[] | undefined {
  if (!Array.isArray(body.questions) || body.questions.length === 0) return undefined;
  const questions: ApprovalQuestionView[] = [];
  const ids = new Set<string>();
  for (const value of body.questions) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
    const question = value as Record<string, unknown>;
    if (typeof question.id !== "string" || !question.id || typeof question.question !== "string" || !question.question) return undefined;
    if (ids.has(question.id)) return undefined;
    ids.add(question.id);
    if (question.header !== undefined && typeof question.header !== "string") return undefined;
    const options: ApprovalQuestionView["options"] = [];
    if (question.options !== undefined) {
      if (!Array.isArray(question.options)) return undefined;
      for (const optionValue of question.options) {
        if (!optionValue || typeof optionValue !== "object" || Array.isArray(optionValue)) return undefined;
        const option = optionValue as Record<string, unknown>;
        if (typeof option.label !== "string" || !option.label ||
            (option.description !== undefined && typeof option.description !== "string")) return undefined;
        options.push({ label: option.label, ...(typeof option.description === "string" ? { description: option.description } : {}) });
      }
    }
    questions.push({
      id: question.id,
      question: question.question,
      ...(typeof question.header === "string" ? { header: question.header } : {}),
      options,
    });
  }
  return questions;
}

function toolNameOf(ev: WireEvent, body: Record<string, unknown>): string {
  const tool = body.tool ?? body.name;
  if (typeof tool === "string" && tool) return tool;
  const match = /tool call (\S+)/.exec(ev.summary ?? "");
  return match ? match[1] : "tool";
}

function chatMessageOf(body: Record<string, unknown>): TranscriptMessage | null {
  const role = body.role;
  const text = body.text;
  const source = body.source;
  if ((role !== "user" && role !== "assistant" && role !== "system") || typeof text !== "string") return null;
  if (source !== "driven" && source !== "observed") return null;
  const turnId = typeof body.turn_id === "string" && body.turn_id ? body.turn_id : undefined;
  return { role, text, source, ...(turnId ? { turnId } : {}) };
}

/** Mutate reducer state in place and return operations for unseen run/sequence events. */
export function reduceEvents(
  state: ReducerState,
  events: readonly WireEvent[],
  defaultRunId = LEGACY_RUN_ID,
): ReducerOp[] {
  const ops: ReducerOp[] = [];
  for (const ev of events) {
    const agentRunId = cleanRunId(ev.agent_run_id) ?? defaultRunId;
    const eventKey = timelineIdentity(agentRunId, ev.sequence_no);
    if (state.seenEventKeys.has(eventKey)) continue;
    state.seenEventKeys.add(eventKey);
    const body = decodeBody(ev);
    switch (ev.event_kind) {
      case "chat_message": {
        if (ev.marker !== "point") break;
        const message = chatMessageOf(body);
        if (!message) break;
        state.sawChatMessages = true;
        state.transcript.push(message);
        appendTimeline(state, { kind: "message", key: eventKey, agentRunId, sequenceNo: ev.sequence_no, message });
        ops.push({ kind: "chatMessage", message });
        break;
      }
      case "tool_call": {
        const correlationId = ev.correlation_id ?? eventKey;
        const correlationKey = scopedCorrelation(agentRunId, correlationId);
        if (ev.marker === "open") {
          const existing = state.cards.get(correlationKey);
          if (existing) break;
          const tool = toolNameOf(ev, body);
          const card: ToolCard = {
            timelineKey: eventKey,
            agentRunId,
            sequenceNo: ev.sequence_no,
            correlationId,
            tool,
            status: "running",
            summary: ev.summary ?? "",
            ...processCardOf(tool, body, undefined, "running"),
          };
          state.cards.set(correlationKey, card);
          appendTimeline(state, { kind: "tool", key: eventKey, agentRunId, sequenceNo: ev.sequence_no, card });
          ops.push({ kind: "toolCard", card });
        } else if (ev.marker === "close_ok") {
          const card = updateCard(state, agentRunId, correlationId, "ok", ev, body, eventKey);
          ops.push({ kind: "toolCard", card });
        } else if (ev.marker === "close_fail" || ev.marker === "close_canceled") {
          const policyBlocked = ev.marker === "close_fail" && !!ev.code && ev.code !== TOOL_ERROR;
          const card = updateCard(state, agentRunId, correlationId, policyBlocked ? "blocked" : "error", ev, body, eventKey);
          ops.push({ kind: "toolCard", card });
          if (policyBlocked) {
            ops.push({ kind: "blockedTool", correlationId, summary: card.summary, code: ev.code ?? undefined });
          }
        }
        break;
      }
      case "approval": {
        const correlationId = ev.correlation_id ?? eventKey;
        const correlationKey = scopedCorrelation(agentRunId, correlationId);
        if (ev.marker === "open") {
          const prior = state.approvals.get(correlationKey);
          const questions = approvalQuestionsOf(body);
          const approval: ApprovalCard = {
            timelineKey: prior?.timelineKey ?? eventKey,
            agentRunId,
            sequenceNo: prior?.sequenceNo ?? ev.sequence_no,
            correlationId,
            status: "pending",
            summary: ev.summary ?? "tool requires approval",
            ...(questions ? { questions } : {}),
          };
          state.approvals.set(correlationKey, approval);
          if (prior) replaceApproval(state, prior.timelineKey, approval);
          else appendTimeline(state, { kind: "approval", key: eventKey, agentRunId, sequenceNo: ev.sequence_no, approval });
          ops.push({ kind: "approvalRequest", approval });
        } else if (ev.marker === "declined" || ev.marker === "resolved" || ev.marker === "timed_out") {
          const prior = state.approvals.get(correlationKey);
          const decision = body.decision;
          const status: ApprovalCardStatus =
            ev.marker === "timed_out" ? "timed_out" : ev.marker === "declined" || decision === "denied" ? "denied" : "approved";
          const approval: ApprovalCard = {
            timelineKey: prior?.timelineKey ?? eventKey,
            agentRunId,
            sequenceNo: prior?.sequenceNo ?? ev.sequence_no,
            correlationId,
            status,
            summary: ev.summary ?? prior?.summary ?? "approval resolved",
            code: ev.code ?? undefined,
            ...(prior?.questions ? { questions: prior.questions } : {}),
          };
          state.approvals.set(correlationKey, approval);
          if (prior) replaceApproval(state, prior.timelineKey, approval);
          else appendTimeline(state, { kind: "approval", key: eventKey, agentRunId, sequenceNo: ev.sequence_no, approval });
          ops.push({ kind: "approvalUpdate", approval });
          if (status === "denied") {
            ops.push({ kind: "blockedTool", correlationId, summary: approval.summary, code: ev.code ?? undefined });
          }
        }
        break;
      }
      case "turn": {
        if (ev.marker === "open") state.turnState = "running";
        if (TURN_CLOSE_MARKERS.has(ev.marker)) {
          state.turnState = "idle";
          ops.push({ kind: "turnDone", terminalState: turnTerminalState(ev, body) });
        }
        break;
      }
      case "finalization": {
        state.terminal = true;
        state.turnState = "idle";
        state.terminalState = finalTerminalState(ev, body);
        const legacyText = ev.marker === "close_ok" && !state.sawChatMessages
          ? legacyFinalizationText(ev, body)
          : undefined;
        if (legacyText) {
          const message: TranscriptMessage = { role: "assistant", text: legacyText, source: "driven" };
          const key = timelineIdentity(agentRunId, ev.sequence_no);
          if (!state.timelineIndex.has(key)) {
            state.transcript.push(message);
            appendTimeline(state, { kind: "message", key, agentRunId, sequenceNo: ev.sequence_no, message });
          }
          ops.push({ kind: "assistantText", text: legacyText });
        }
        ops.push({ kind: "runDone", terminalState: state.terminalState });
        break;
      }
      case "subagent": {
        const correlationId = ev.correlation_id ?? eventKey;
        if (ev.marker === "open") {
          const correlationKey = scopedCorrelation(agentRunId, correlationId);
          if (state.cards.has(correlationKey)) break;
          const card: ToolCard = {
            timelineKey: eventKey,
            agentRunId,
            sequenceNo: ev.sequence_no,
            correlationId,
            tool: "subagent",
            status: "running",
            summary: ev.summary ?? "subagent started",
          };
          state.cards.set(correlationKey, card);
          appendTimeline(state, { kind: "tool", key: eventKey, agentRunId, sequenceNo: ev.sequence_no, card });
          ops.push({ kind: "toolCard", card });
        } else if (TURN_CLOSE_MARKERS.has(ev.marker)) {
          const status = ev.marker === "close_ok" ? "ok" : "error";
          ops.push({ kind: "toolCard", card: updateCard(state, agentRunId, correlationId, status, ev, body, eventKey) });
        }
        break;
      }
      default:
        break;
    }
  }
  return ops;
}

function updateCard(
  state: ReducerState,
  agentRunId: string,
  correlationId: CorrelationId,
  status: ToolCardStatus,
  ev: WireEvent,
  body: Record<string, unknown>,
  eventKey: string,
): ToolCard {
  const correlationKey = scopedCorrelation(agentRunId, correlationId);
  const prior = state.cards.get(correlationKey);
  const tool = prior?.tool ?? toolNameOf(ev, body);
  const card: ToolCard = {
    timelineKey: prior?.timelineKey ?? eventKey,
    agentRunId,
    sequenceNo: prior?.sequenceNo ?? ev.sequence_no,
    correlationId,
    tool,
    status,
    summary: ev.summary ?? prior?.summary ?? "",
    code: ev.code ?? undefined,
    ...processCardOf(tool, body, prior?.process, status),
  };
  state.cards.set(correlationKey, card);
  if (prior) replaceCard(state, prior.timelineKey, card);
  else appendTimeline(state, { kind: "tool", key: eventKey, agentRunId, sequenceNo: ev.sequence_no, card });
  return card;
}

/** Return process details only for kaizen_run_process cards or an existing process card. */
function processCardOf(
  tool: string,
  body: Record<string, unknown>,
  prior: ProcessCard | undefined,
  status: ToolCardStatus,
): { process: ProcessCard } | Record<string, never> {
  if (tool !== "kaizen_run_process" && !prior) return {};
  const request = objectOf(body.request ?? body.input ?? body.arguments);
  const result = objectOf(body.result);
  const field = (name: string): unknown => body[name] ?? request[name] ?? result[name];
  const executable = processText(field("executable"), 2_048) ?? prior?.executable;
  if (!executable) return {};
  const rawArgv = field("argv");
  const argv = Array.isArray(rawArgv) && rawArgv.length <= 256 && rawArgv.every(processArgument)
    ? rawArgv
    : prior?.argv ?? [];
  const cwd = processText(field("cwd"), 2_048) ?? prior?.cwd ?? ".";
  const timeout = safeProcessInteger(field("timeout_ms"), 1, 1_800_000) ?? prior?.timeoutMs ?? 120_000;
  const reportedDecision = processDecision(field("decision") ?? field("policy_decision"));
  const decision = reportedDecision
    ?? (prior?.decision && prior.decision !== "pending" ? prior.decision : statusDecision(status));
  const exitCode = safeProcessInteger(field("exit_code"), -2_147_483_648, 2_147_483_647) ?? prior?.exitCode;
  const timedOut = booleanField(field("timed_out"), prior?.timedOut);
  const truncated = booleanField(field("truncated") ?? field("output_truncated"), prior?.truncated);
  const stdoutSha256 = processDigest(field("stdout_sha256")) ?? prior?.stdoutSha256;
  const stdoutBytes = safeProcessInteger(field("stdout_bytes"), 0, 64 * 1024 * 1024) ?? prior?.stdoutBytes;
  return {
    process: {
      executable,
      argv,
      cwd,
      timeoutMs: timeout,
      decision,
      ...(exitCode !== undefined ? { exitCode } : {}),
      ...(timedOut !== undefined ? { timedOut } : {}),
      ...(truncated !== undefined ? { truncated } : {}),
      ...(stdoutSha256 !== undefined ? { stdoutSha256 } : {}),
      ...(stdoutBytes !== undefined ? { stdoutBytes } : {}),
      effectsUnknown: true,
    },
  };
}

function processDigest(value: unknown): string | undefined {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value) ? value : undefined;
}

function objectOf(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function processText(value: unknown, max: number): string | undefined {
  return typeof value === "string" && value.length > 0 && value.length <= max && !value.includes("\u0000") ? value : undefined;
}

function processArgument(value: unknown): value is string {
  return typeof value === "string" && value.length <= 8_192 && !value.includes("\u0000");
}

function safeProcessInteger(value: unknown, min: number, max: number): number | undefined {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= min && value <= max ? value : undefined;
}

/** Normalize an engine-reported process decision through a closed allowlist. */
function processDecision(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const decision = value.trim().toLowerCase();
  return ["pending", "ask", "approve", "approved", "allow", "allowed", "deny", "denied", "blocked", "error"]
    .includes(decision) ? decision : undefined;
}

function statusDecision(status: ToolCardStatus): string {
  return status === "running" ? "pending" : status === "blocked" ? "denied" : status === "ok" ? "allowed" : "error";
}

function booleanField(value: unknown, prior: boolean | undefined): boolean | undefined {
  return typeof value === "boolean" ? value : prior;
}

function replaceCard(state: ReducerState, key: string, card: ToolCard): void {
  const position = state.timelineIndex.get(key);
  if (position !== undefined && state.timeline[position]?.kind === "tool") state.timeline[position] = { kind: "tool", key, agentRunId: card.agentRunId, sequenceNo: card.sequenceNo, card };
}

function replaceApproval(state: ReducerState, key: string, approval: ApprovalCard): void {
  const position = state.timelineIndex.get(key);
  if (position !== undefined && state.timeline[position]?.kind === "approval") state.timeline[position] = { kind: "approval", key, agentRunId: approval.agentRunId, sequenceNo: approval.sequenceNo, approval };
}

function appendTimeline(state: ReducerState, item: TimelineItem): void {
  state.timelineIndex.set(item.key, state.timeline.length);
  state.timeline.push(item);
}

function turnTerminalState(ev: WireEvent, body: Record<string, unknown>): string {
  const status = body.status;
  if (typeof status === "string" && status) return status;
  if (ev.marker === "close_ok") return "completed";
  if (ev.marker === "close_canceled") return "canceled";
  return ev.code?.trim() || "failed";
}

function finalTerminalState(ev: WireEvent, body: Record<string, unknown>): string {
  const state = body.terminal_state;
  if (typeof state === "string" && state) return state;
  if (ev.marker === "close_ok") return "success";
  if (ev.marker === "close_canceled") return "canceled";
  return ev.code?.trim() || "failed";
}

function legacyFinalizationText(ev: WireEvent, body: Record<string, unknown>): string | undefined {
  for (const key of ["assistant_text", "final_text", "text", "message"]) {
    const value = body[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  if (typeof ev.body !== "string" || !ev.body.trim()) return undefined;
  try {
    const parsed: unknown = JSON.parse(ev.body);
    return typeof parsed === "string" && parsed.trim() ? parsed : undefined;
  } catch {
    return ev.body;
  }
}

function cleanRunId(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}
