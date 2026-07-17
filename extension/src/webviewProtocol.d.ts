/** Types-only contract shared by the extension host and both chat renderers. */

export type CorrelationId = string;
export type PermissionMode = "plan" | "ask" | "agent" | "full";
export type AuthMode = "none" | "subscription" | "api-key";

export interface ProfileSelection {
  model?: string;
  reasoning_effort?: string;
  max_turns?: number;
  permission_mode: PermissionMode;
  auth_mode: AuthMode;
}

export interface CapabilityModelView {
  id: string;
  label: string;
  description?: string;
  reasoning_efforts: string[];
  default_effort?: string;
  supports_adaptive_thinking: boolean;
  supports_fast_mode: boolean;
}

export interface EngineCapabilityView {
  id: string;
  label: string;
  drivable: boolean;
  availability: { state: string; code?: string; message?: string };
  models: CapabilityModelView[];
  default_model?: string;
  default_reasoning_effort?: string;
  auth_modes: AuthMode[];
  permission_modes: PermissionMode[];
  warnings: string[];
  /** Governed context, leases, tools, and test_extension are daemon-enforced capability gates. */
  features: {
    streaming: boolean;
    image_attachments: boolean;
    governed_context: boolean;
    diff_snapshots: boolean;
    writer_leasing: boolean;
    subscription_auth: boolean;
    controlled_tools: boolean;
    process_execution: boolean;
    test_extension: boolean;
  };
  runtime?: {
    kind: string;
    version?: string;
    status: "ready" | "auth_required" | "unavailable";
  };
  maxTurns?: { min: number; default: number; max: number };
}

export interface TranscriptMessageView {
  role: "user" | "assistant" | "system";
  text: string;
  source: "driven" | "observed";
  turnId?: string;
}

export interface ToolCardView {
  timelineKey: string;
  agentRunId: string;
  sequenceNo: number;
  correlationId: string;
  tool: string;
  status: "running" | "ok" | "error" | "blocked";
  summary: string;
  code?: string;
  process?: {
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
    /** Always true because process side effects are never introspected by the UI. */
    effectsUnknown: true;
  };
}

export interface ApprovalQuestionView {
  id: string;
  question: string;
  header?: string;
  options: Array<{ label: string; description?: string }>;
}

export interface ApprovalView {
  timelineKey: string;
  agentRunId: string;
  sequenceNo: number;
  correlationId: string;
  summary: string;
  /** Overall approval lifecycle. */
  status: "pending" | "approved" | "denied" | "timed_out";
  code?: string;
  displayOnly: boolean;
  pending: boolean;
  /** Sanitized requestUserInput questions; questions without options require explicit answers. */
  questions?: ApprovalQuestionView[];
  diff?: {
    /** Snapshot-preview validity while the overall approval is still pending. */
    status: "pending" | "corrupt";
    revision: number;
    snapshotSetSha256: string;
    metadataConfirmationRequired: boolean;
    files: Array<{
      changeId: string;
      path: string;
      oldPath: string | null;
      kind: "create" | "modify" | "delete" | "rename";
      previewMode: "text" | "metadata";
      previewReason: "binary" | "unsupported_encoding" | "oversize" | null;
      beforeSha256: string | null;
      proposedSha256: string | null;
    }>;
  };
  diffError?: { code: string; message: string };
}

export interface TimelineMessageView {
  kind: "message";
  key: string;
  agentRunId: string;
  sequenceNo: number;
  message: TranscriptMessageView;
}

export interface TimelineToolView {
  kind: "tool";
  key: string;
  agentRunId: string;
  sequenceNo: number;
  card: ToolCardView;
}

export interface TimelineApprovalView {
  kind: "approval";
  key: string;
  agentRunId: string;
  sequenceNo: number;
  approval: ApprovalView;
}

export type TimelineItemView = TimelineMessageView | TimelineToolView | TimelineApprovalView;

export interface FollowUpQueueSnapshot {
  items: Array<{ id: string; preview: string; dispatching: boolean }>;
  paused: boolean;
  pauseReason?: "failure" | "interrupt" | "kill" | "approval_timeout" | "lease_conflict" | "uncertain_transport";
}

export interface ConversationView {
  sessionId: string;
  agentRunId: string;
  engine: string;
  profileHash?: string;
  controller: "driven" | "observed";
  /** Live-leg state; terminal may remain resumable and therefore not read-only. */
  state: "running" | "idle" | "terminal";
  terminalState?: string;
  readOnly: boolean;
  profileLegacy?: boolean;
  /** Daemon-restart continuation: the leg is terminal but the conversation continues on send. */
  resumable?: boolean;
  continuationNotice?: ContinuationNoticeView;
}

export interface ContinuationNoticeView {
  mode: "reduced";
  message: string;
  omittedMessages?: number;
  expiredArtifacts?: ArtifactMetadataView[];
}

export interface ArtifactMetadataView {
  kind: string;
  name?: string;
  sha256?: string;
  bytes?: number;
}

export interface HistoryEntryView {
  sessionId: string;
  agentRunId: string;
  controller: "driven" | "observed";
  title: string;
  snippet?: string;
  createdAt?: string;
  engine?: string;
  /** History presentation state; resumable/read-only duplicate booleans for direct consumers. */
  state: "running" | "idle" | "resumable" | "read-only";
  resumable: boolean;
  readOnly: boolean;
  terminalState?: string;
  fidelity?: { mode: "full" | "reduced"; message: string };
  omittedMessages?: number;
  runsOmitted?: number;
  expiredArtifacts?: ArtifactMetadataView[];
}

export interface HistorySnapshot {
  entries: HistoryEntryView[];
  sessionsOmitted?: number;
  truncated: boolean;
}

export interface ContextChipView {
  id: string;
  kind: "file" | "selection";
  label: string;
  sourcePath: string;
  bytes?: number;
}

export interface ImageChipView {
  id: string;
  label: string;
  bytes: number;
  mediaType: "image/png" | "image/jpeg" | "image/webp" | "image/gif";
  sha256: string;
  previewUri?: string;
}

export interface MentionCandidateView {
  path: string;
  label: string;
}

export interface ObservedSessionView {
  sessionId: string;
  label: string;
}

/** A prior driven conversation offered for continuation (owner 2026-07-11: no dead-ends). */
export interface ConversationListEntry {
  sessionId: string;
  agentRunId: string;
  label: string;
  active: boolean;
}

export interface ConversationSnapshot {
  /** Serializer identity only. No draft, prompt, attachment, or queue bytes are persisted. */
  conversationId?: string;
  capabilities: EngineCapabilityView[];
  selectedEngine: string;
  selectedProfile: ProfileSelection;
  selectorsLocked: boolean;
  conversation: ConversationView | null;
  conversations: ConversationListEntry[];
  transcript: TranscriptMessageView[];
  timeline: TimelineItemView[];
  cards: ToolCardView[];
  approvals: ApprovalView[];
  followUpQueue: FollowUpQueueSnapshot;
  observedSessions: ObservedSessionView[];
  history: HistorySnapshot;
  contextChips: ContextChipView[];
  imageChips: ImageChipView[];
  streamingBubble?: { turnId: string; text: string };
  pendingPrompt: boolean;
  pendingPromptToken?: string;
  acceptedPromptToken?: string;
  queuedPromptToken?: string;
  settledPromptToken?: string;
  banner?: { message: string; code?: string };
}

export interface WvReady {
  type: "ready";
}

export interface WvEphemeralState {
  type: "ephemeralState";
  hasDraft: boolean;
}

export interface WvProfileChange {
  type: "profileChange";
  engine: string;
  profile: ProfileSelection;
}

export interface WvUserPrompt {
  type: "userPrompt";
  prompt: string;
  requestToken?: string;
}

export interface WvSteer {
  type: "steer";
  instruction: string;
}

export interface WvInterrupt {
  type: "interrupt";
}

export interface WvKill {
  type: "kill";
}

export interface WvClose {
  type: "close";
  hasDraft?: boolean;
}

export interface WvNewConversation {
  type: "newConversation";
  hasDraft?: boolean;
}

export interface WvApprove {
  type: "approve";
  correlationId: CorrelationId;
  /** Optional for legacy rows; the host falls back to the active or legacy run identity. */
  agentRunId?: string;
  /** Exact explicit answers for every displayed free-text question. */
  answers?: Record<string, string>;
}

export interface WvDeny {
  type: "deny";
  correlationId: CorrelationId;
  /** Optional for legacy rows; the host falls back to the active or legacy run identity. */
  agentRunId?: string;
}

export interface WvPreviewDiff {
  type: "previewDiff";
  correlationId: CorrelationId;
  agentRunId: string;
}

export interface WvAcceptDiff {
  type: "acceptDiff";
  correlationId: CorrelationId;
  agentRunId: string;
}

export interface WvRejectDiff {
  type: "rejectDiff";
  correlationId: CorrelationId;
  agentRunId: string;
}

export interface WvObservedSelect {
  type: "observedSelect";
  sessionId: string;
  hasDraft?: boolean;
}

export interface WvConversationSelect {
  type: "conversationSelect";
  sessionId: string;
  agentRunId: string;
  hasDraft?: boolean;
}

export interface WvRefreshCapabilities {
  type: "refreshCapabilities";
}

export interface WvShowHistory {
  type: "showHistory";
}

export interface WvHistorySelect {
  type: "historySelect";
  sessionId: string;
  agentRunId: string;
}

export interface WvMentionQuery {
  type: "mentionQuery";
  query: string;
}

export interface WvMentionSelect {
  type: "mentionSelect";
  path: string;
}

export interface WvAddFile {
  type: "addFile";
}

export interface WvAddSelection {
  type: "addSelection";
}

export interface WvContextRemove {
  type: "contextRemove";
  id: string;
}

export interface WvAddImage {
  type: "addImage";
}

export interface WvImageRemove {
  type: "imageRemove";
  id: string;
}

export interface WvImagePasteStart {
  type: "imagePasteStart";
  uploadId: string;
  name: string;
  mediaType: ImageChipView["mediaType"];
  bytes: number;
}

export interface WvImagePasteChunk {
  type: "imagePasteChunk";
  uploadId: string;
  index: number;
  data: string;
}

export interface WvImagePasteEnd {
  type: "imagePasteEnd";
  uploadId: string;
}

export interface WvImagePasteCancel {
  type: "imagePasteCancel";
  uploadId: string;
}

/** Handled by the RENDERER layer (vscode command), never by the controller. */
export interface WvStartDaemon {
  type: "startDaemon";
}

/** Handled by the RENDERER layer (vscode command + host confirm dialog), never by the controller. */
export interface WvStopDaemon {
  type: "stopDaemon";
}

export type WebviewToHost =
  | WvReady
  | WvEphemeralState
  | WvProfileChange
  | WvUserPrompt
  | WvSteer
  | WvInterrupt
  | WvKill
  | WvClose
  | WvNewConversation
  | WvApprove
  | WvDeny
  | WvPreviewDiff
  | WvAcceptDiff
  | WvRejectDiff
  | WvObservedSelect
  | WvConversationSelect
  | WvRefreshCapabilities
  | WvShowHistory
  | WvHistorySelect
  | WvMentionQuery
  | WvMentionSelect
  | WvAddFile
  | WvAddSelection
  | WvContextRemove
  | WvAddImage
  | WvImageRemove
  | WvImagePasteStart
  | WvImagePasteChunk
  | WvImagePasteEnd
  | WvImagePasteCancel
  | WvStartDaemon
  | WvStopDaemon;

export interface HvSnapshot {
  type: "snapshot";
  snapshot: ConversationSnapshot;
}

export interface HvFocusInput {
  type: "focusInput";
}

export interface HvShowHistory {
  type: "showHistory";
}

export interface HvShowChat {
  type: "showChat";
}

export interface HvMentionResults {
  type: "mentionResults";
  query: string;
  items: MentionCandidateView[];
}

export type HostToWebview = HvSnapshot | HvFocusInput | HvShowHistory | HvShowChat | HvMentionResults;

/** Additive daemon event shape; the open index preserves forward-compatible producer fields. */
export interface WireEvent {
  sequence_no: number;
  event_kind: string;
  marker: string;
  summary?: string;
  correlation_id?: string | null;
  code?: string | null;
  body?: string | null;
  agent_run_id?: string;
  turn_id?: string;
  turn_state?: string;
  [key: string]: unknown;
}
