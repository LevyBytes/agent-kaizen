/** CSP-safe editor-panel renderer. Durable state is supplied by the extension host. */

import type {
  ApprovalView,
  ConversationSnapshot,
  EngineCapabilityView,
  HistoryEntryView,
  HostToWebview,
  ImageChipView,
  MentionCandidateView,
  ProfileSelection,
  TimelineItemView,
  ToolCardView,
  WebviewToHost,
} from "../webviewProtocol.js";
import { renderSafeMarkdown } from "./safeMarkdown.js";

declare function acquireVsCodeApi(): {
  postMessage(message: unknown): void;
  setState(state: { conversationId: string }): void;
};
const vscode = acquireVsCodeApi();
const MAX_IMAGES = 4;
const MAX_IMAGE_BYTES = 4 * 1024 * 1024;
const IMAGE_CHUNK_BYTES = 64 * 1024;
type PastedImage = File & { type: ImageChipView["mediaType"] };

const $ = <T extends HTMLElement>(id: string): T => document.getElementById(id) as T;
const welcome = $<HTMLElement>("welcome");
const chatScreen = $<HTMLElement>("chatScreen");
const historyScreen = $<HTMLElement>("historyScreen");
const historyList = $<HTMLElement>("historyList");
const historyMeta = $<HTMLElement>("historyMeta");
const historyDriven = $<HTMLButtonElement>("historyDriven");
const historyObserved = $<HTMLButtonElement>("historyObserved");
const transcript = $<HTMLElement>("transcript");
const warnings = $<HTMLElement>("warnings");
const runtimeStatus = $<HTMLElement>("runtimeStatus");
const banner = $<HTMLElement>("banner");
const continuationBanner = $<HTMLElement>("continuationBanner");
const lossWarning = $<HTMLElement>("lossWarning");
const status = $<HTMLElement>("status");
const queueChips = $<HTMLElement>("queueChips");
const contextChips = $<HTMLElement>("contextChips");
const imageChips = $<HTMLElement>("imageChips");
const contextActions = $<HTMLElement>("contextActions");
const mentionResults = $<HTMLElement>("mentionResults");
const engine = $<HTMLSelectElement>("engine");
const model = $<HTMLInputElement>("model");
const models = $<HTMLDataListElement>("models");
const effort = $<HTMLSelectElement>("effort");
const permission = $<HTMLSelectElement>("permission");
const auth = $<HTMLSelectElement>("auth");
const authField = $<HTMLElement>("authField");
const maxTurns = $<HTMLInputElement>("maxTurns");
const maxTurnsField = $<HTMLElement>("maxTurnsField");
const refreshCapabilities = $<HTMLButtonElement>("refreshCapabilities");
const startDaemon = $<HTMLButtonElement>("startDaemon");
const stopDaemon = $<HTMLButtonElement>("stopDaemon");
const kill = $<HTMLButtonElement>("kill");
const prompt = $<HTMLTextAreaElement>("prompt");
const send = $<HTMLButtonElement>("send");
const turnStop = $<HTMLButtonElement>("turnStop");
const newConversation = $<HTMLButtonElement>("newSession");
const historyButton = $<HTMLButtonElement>("historyButton");
const addFile = $<HTMLButtonElement>("addFile");
const addSelection = $<HTMLButtonElement>("addSelection");
const addImage = $<HTMLButtonElement>("addImage");
const overflow = $<HTMLDetailsElement>("overflow");

let snapshot: ConversationSnapshot | null = null;
let submittedPromptToken: string | undefined;
let promptTokenSequence = 0;
let profileRenderKey = "";
let activeScreen: "conversation" | "history" = "conversation";
let historyController: "driven" | "observed" = "driven";
const timelineNodes = new Map<string, HTMLElement>();
const streamingNode = document.createElement("article");

function post(message: WebviewToHost): void {
  vscode.postMessage(message);
}

window.addEventListener("message", (event: MessageEvent) => {
  const message = event.data as HostToWebview;
  if (message.type === "snapshot") {
    if (message.snapshot.conversationId) vscode.setState({ conversationId: message.snapshot.conversationId });
    render(message.snapshot);
  } else if (message.type === "focusInput") {
    activeScreen = "conversation";
    renderScreen();
    prompt.focus();
  } else if (message.type === "showHistory") {
    activeScreen = "history";
    renderHistory();
    renderScreen();
  } else if (message.type === "showChat") {
    activeScreen = "conversation";
    renderScreen();
    prompt.focus();
  } else if (message.type === "mentionResults") {
    renderMentionResults(message.items);
  }
});

function render(next: ConversationSnapshot): void {
  const wasNearBottom = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 80;
  snapshot = next;
  reconcilePrompt(next);
  renderProfile(next);
  renderTimeline(next);
  renderQueue(next);
  renderContext(next);
  renderImages(next);
  renderHistory();
  renderWarnings(next);
  renderRuntimeStatus(next);
  renderBanner(next);
  renderContinuation(next);
  renderState(next);
  renderLossWarning();
  renderScreen();
  if (wasNearBottom) transcript.scrollTop = transcript.scrollHeight;
}

function renderProfile(next: ConversationSnapshot): void {
  const nextKey = JSON.stringify({
    capabilities: next.capabilities,
    selectedEngine: next.selectedEngine,
    selectedProfile: next.selectedProfile,
    selectorsLocked: next.selectorsLocked,
    profileLegacy: next.conversation?.profileLegacy,
  });
  if (nextKey === profileRenderKey) return;
  profileRenderKey = nextKey;
  replaceOptions(
    engine,
    next.capabilities.map((entry) => ({
      value: entry.id,
      label: entry.drivable ? entry.label : `${entry.label} - ${entry.availability.state}`,
      disabled: !entry.drivable,
    })),
    next.selectedEngine,
  );
  if (next.conversation?.profileLegacy) {
    models.replaceChildren();
    model.value = "legacy/unknown";
    replaceOptions(effort, [{ value: "", label: "Legacy / unknown" }], "");
    replaceOptions(permission, [{ value: "", label: "Legacy / unknown" }], "");
    replaceOptions(auth, [{ value: "", label: "Legacy / unknown" }], "");
    maxTurnsField.hidden = true;
    authField.hidden = false;
    for (const control of [engine, model, effort, permission, auth, maxTurns, refreshCapabilities]) control.disabled = true;
    return;
  }
  const capability = selectedCapability(next);
  models.replaceChildren(
    ...(capability?.models ?? []).map((entry) => {
      const option = document.createElement("option");
      option.value = entry.id;
      option.label = entry.label;
      return option;
    }),
  );
  if (document.activeElement !== model || next.selectorsLocked) model.value = (next.selectedProfile.model ?? "").trim();
  const selectedModel = capability?.models.find((entry) => entry.id === model.value.trim());
  const efforts = selectedModel?.reasoning_efforts ?? [];
  replaceOptions(
    effort,
    [{ value: "", label: "Not available" }, ...efforts.map((value) => ({ value, label: value }))],
    next.selectedProfile.reasoning_effort ?? "",
  );
  replaceOptions(
    permission,
    (capability?.permission_modes ?? []).map((value) => ({ value, label: title(value) })),
    next.selectedProfile.permission_mode,
  );
  replaceOptions(
    auth,
    (capability?.auth_modes ?? []).map((value) => ({ value, label: title(value) })),
    next.selectedProfile.auth_mode,
  );
  authField.hidden = (capability?.auth_modes.length ?? 0) === 0;
  const bounds = capability?.id === "claude" ? capability.maxTurns : undefined;
  maxTurnsField.hidden = bounds === undefined;
  if (bounds) {
    maxTurns.min = String(bounds.min);
    maxTurns.max = String(bounds.max);
    maxTurns.step = "1";
    maxTurns.value = String(next.selectedProfile.max_turns ?? bounds.default);
    maxTurns.title = `Hard round-trip ceiling (${bounds.min}-${bounds.max})`;
  } else {
    maxTurns.removeAttribute("min");
    maxTurns.removeAttribute("max");
    maxTurns.value = "";
  }
  const locked = next.selectorsLocked;
  engine.disabled = locked;
  model.disabled = locked || (capability?.id === "claude" && capability.models.length === 0);
  effort.disabled = locked || efforts.length === 0;
  permission.disabled = locked;
  auth.disabled = locked || capability?.id === "claude";
  maxTurns.disabled = locked || bounds === undefined;
  refreshCapabilities.disabled = locked;
}

function renderTimeline(next: ConversationSnapshot): void {
  const liveKeys = new Set(next.timeline.map((item) => item.key));
  for (const [key, node] of timelineNodes) {
    if (liveKeys.has(key)) continue;
    node.remove();
    timelineNodes.delete(key);
  }

  const orderedNodes = next.timeline.map((item) => {
    let node = timelineNodes.get(item.key);
    if (!node) {
      node = document.createElement("article");
      node.className = "timeline-item";
      node.dataset.timelineKey = item.key;
      timelineNodes.set(item.key, node);
    }
    updateTimelineNode(node, item);
    return node;
  });
  if (next.streamingBubble) {
    streamingNode.className = "timeline-item message streaming-message";
    streamingNode.dataset.role = "assistant";
    streamingNode.dataset.turnId = next.streamingBubble.turnId;
    streamingNode.replaceChildren(textDiv("message-role", "assistant"), textDiv("message-body plain-text", next.streamingBubble.text));
    transcript.replaceChildren(...orderedNodes, streamingNode);
  } else {
    transcript.replaceChildren(...orderedNodes);
  }
}

function updateTimelineNode(node: HTMLElement, item: TimelineItemView): void {
  node.dataset.agentRunId = item.agentRunId;
  node.dataset.sequenceNo = String(item.sequenceNo);
  if (item.kind === "message") {
    renderMessage(node, item.message.role, item.message.text);
  } else if (item.kind === "tool") {
    renderToolCard(node, item.card);
  } else {
    renderApprovalCard(node, item.approval);
  }
}

function renderMessage(node: HTMLElement, role: "user" | "assistant" | "system", text: string): void {
  node.className = "timeline-item message";
  node.dataset.role = role;
  let roleNode = node.querySelector<HTMLElement>(".message-role");
  let body = node.querySelector<HTMLElement>(".message-body");
  if (!roleNode || !body) {
    roleNode = textDiv("message-role", role);
    body = document.createElement("div");
    body.className = "message-body";
    node.replaceChildren(roleNode, body);
  }
  roleNode.textContent = role;
  if (role === "user") {
    body.className = "message-body plain-text";
    body.textContent = text;
  } else {
    body.className = "message-body markdown";
    body.innerHTML = renderSafeMarkdown(text);
  }
}

function renderToolCard(node: HTMLElement, card: ToolCardView): void {
  node.className = "timeline-item tool-card";
  node.dataset.status = card.status;
  const head = document.createElement("div");
  head.className = "card-head";
  head.append(textDiv("card-title", card.tool), textDiv("card-badge", card.status === "blocked" ? "blocked by policy" : card.status));
  const children: Node[] = [head, textDiv("card-summary", card.summary)];
  if (card.code) children.push(textDiv("card-code", card.code));
  if (card.process) {
    const process = document.createElement("div");
    process.className = "process-grid";
    const rows: Array<[string, string]> = [
      ["Executable", card.process.executable],
      ["Arguments", card.process.argv.length ? JSON.stringify(card.process.argv) : "[]"],
      ["Working directory", card.process.cwd],
      ["Timeout", `${card.process.timeoutMs} ms`],
      ["Decision", card.process.decision],
      ["Exit", card.process.timedOut === true
        ? "timed out"
        : card.process.exitCode === undefined ? (card.status === "running" ? "pending" : card.status) : String(card.process.exitCode)],
      ["Truncated", typeof card.process.truncated === "boolean" ? (card.process.truncated ? "yes" : "no") : card.status === "running" ? "pending" : "unknown"],
    ];
    for (const [label, value] of rows) {
      process.append(textDiv("process-label", label), textDiv("process-value", value));
    }
    children.push(process, textDiv(
      "process-boundary",
      "Application-level permission mediation only; no OS containment. Process effects may be unknown.",
    ));
  }
  node.replaceChildren(...children);
}

function renderApprovalCard(node: HTMLElement, approval: ApprovalView): void {
  node.className = "timeline-item approval-card";
  node.dataset.status = approval.status;
  const head = document.createElement("div");
  head.className = "card-head";
  head.append(textDiv("card-title", "Approval"), textDiv("card-badge", approval.status));
  const children: Node[] = [head, textDiv("card-summary", approval.summary)];
  const answerInputs = new Map<string, HTMLTextAreaElement>();
  if (approval.code) children.push(textDiv("card-code", approval.code));
  if (approval.diff) {
    const textCount = approval.diff.files.filter((file) => file.previewMode === "text").length;
    const metadataCount = approval.diff.files.length - textCount;
    children.push(textDiv(
      "approval-note diff-summary",
      `Revision ${approval.diff.revision} · ${approval.diff.files.length} file${approval.diff.files.length === 1 ? "" : "s"} · ${textCount} text preview${metadataCount ? ` · ${metadataCount} metadata-only` : ""}`,
    ));
    if (approval.diff.status === "corrupt") {
      children.push(textDiv("approval-note diff-corrupt", "Snapshot bytes are missing or corrupt. Accept is disabled."));
    }
  }
  if (approval.diffError) {
    children.push(textDiv("approval-note diff-corrupt", `${approval.diffError.message} [${approval.diffError.code}]`));
  }
  if (approval.questions?.length) {
    const questions = document.createElement("div");
    questions.className = "approval-questions";
    for (const item of approval.questions) {
      const group = document.createElement("div");
      group.className = "approval-question";
      if (item.header) group.append(textDiv("approval-question-header", item.header));
      group.append(textDiv("approval-question-text", item.question));
      if (item.options.length) {
        const options = document.createElement("ul");
        options.className = "approval-question-options";
        for (const option of item.options) {
          const entry = document.createElement("li");
          entry.textContent = option.description ? `${option.label} — ${option.description}` : option.label;
          options.append(entry);
        }
        group.append(options);
      } else if (approval.status === "pending" && !approval.displayOnly && !approval.pending) {
        const input = document.createElement("textarea");
        input.className = "approval-answer";
        input.rows = 3;
        input.required = true;
        input.placeholder = "Enter an explicit answer";
        input.setAttribute("aria-label", item.header || item.question);
        answerInputs.set(item.id, input);
        group.append(input);
      } else {
        group.append(textDiv("approval-note", "An explicit free-text answer is required."));
      }
      questions.append(group);
    }
    children.push(questions);
  }
  if (approval.status === "pending") {
    if (approval.displayOnly) {
      children.push(textDiv("approval-note", "Display only - this conversation is not controlled here."));
    } else if (approval.pending) {
      children.push(textDiv("approval-note", "Waiting for daemon acknowledgement..."));
    } else {
      const actions = document.createElement("div");
      actions.className = "approval-actions";
      if (approval.diff) {
        if (approval.diff.status !== "corrupt") {
          actions.append(
            actionButton("Preview", "preview-button", () => decideDiff(approval, "previewDiff")),
            actionButton("Accept", "approve-button", () => decideDiff(approval, "acceptDiff")),
          );
        }
        actions.append(actionButton("Reject", "deny-button", () => decideDiff(approval, "rejectDiff")));
      } else if (approval.diffError) {
        actions.append(actionButton("Reject", "deny-button", () => decide(approval, "deny")));
      } else {
        actions.append(
          actionButton("Approve", "approve-button", () => decide(approval, "approve", answerInputs)),
          actionButton("Deny", "deny-button", () => decide(approval, "deny")),
        );
      }
      children.push(actions);
    }
  }
  node.replaceChildren(...children);
}

function renderQueue(next: ConversationSnapshot): void {
  queueChips.replaceChildren(
    ...next.followUpQueue.items.map((item, index) => {
      const chip = document.createElement("span");
      chip.className = "queue-chip";
      chip.dataset.dispatching = String(item.dispatching);
      chip.title = item.preview;
      chip.append(
        textDiv("queue-chip-index", String(index + 1)),
        textDiv("queue-chip-text", item.dispatching ? `Sending: ${item.preview}` : item.preview),
      );
      return chip;
    }),
  );
}

function renderContext(next: ConversationSnapshot): void {
  contextChips.replaceChildren(...next.contextChips.map((item) => {
    const chip = document.createElement("span");
    chip.className = "context-chip";
    chip.title = item.sourcePath;
    const remove = actionButton("\u00d7", "context-remove", () => post({ type: "contextRemove", id: item.id }));
    remove.title = `Remove ${item.label}`;
    remove.setAttribute("aria-label", `Remove ${item.label}`);
    chip.append(textDiv("context-kind", item.kind === "selection" ? "selection" : "file"), textDiv("context-label", item.label), remove);
    return chip;
  }));
}

function renderImages(next: ConversationSnapshot): void {
  imageChips.replaceChildren(...next.imageChips.map((item) => {
    const chip = document.createElement("span");
    chip.className = "image-chip";
    if (item.previewUri) {
      const preview = document.createElement("img");
      preview.className = "image-preview";
      preview.src = item.previewUri;
      preview.alt = "";
      chip.append(preview);
    }
    const remove = actionButton("\u00d7", "context-remove", () => post({ type: "imageRemove", id: item.id }));
    remove.title = `Remove ${item.label}`;
    remove.setAttribute("aria-label", `Remove ${item.label}`);
    chip.append(textDiv("context-label", `${item.label} (${formatBytes(item.bytes)})`), remove);
    return chip;
  }));
}

function renderHistory(): void {
  if (!snapshot) return;
  historyDriven.setAttribute("aria-pressed", String(historyController === "driven"));
  historyObserved.setAttribute("aria-pressed", String(historyController === "observed"));
  const entries = snapshot.history.entries.filter((entry) => entry.controller === historyController);
  historyMeta.textContent = snapshot.history.truncated
    ? `${entries.length} shown; ${snapshot.history.sessionsOmitted ?? "additional"} conversation(s) omitted by the response limit.`
    : `${entries.length} conversation${entries.length === 1 ? "" : "s"}`;
  historyList.replaceChildren(...(entries.length ? entries.map(historyRow) : [textDiv("history-empty", `No ${historyController} conversations.`)]));
}

function historyRow(entry: HistoryEntryView): HTMLElement {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "history-row";
  button.setAttribute("role", "listitem");
  const meta = [entry.engine, entry.state, displayDate(entry.createdAt)].filter((value): value is string => !!value).join(" \u00b7 ");
  const notes = [
    ...(entry.fidelity?.mode === "reduced" ? ["Reduced-fidelity continuation"] : []),
    ...(entry.omittedMessages ? [`${entry.omittedMessages} omitted message${entry.omittedMessages === 1 ? "" : "s"}`] : []),
    ...(entry.runsOmitted ? ["Some linked runs omitted"] : []),
    ...(entry.expiredArtifacts?.length ? [`${entry.expiredArtifacts.length} expired artifact${entry.expiredArtifacts.length === 1 ? "" : "s"}`] : []),
  ];
  button.append(
    textDiv("history-row-title", entry.title),
    textDiv("history-row-snippet", entry.snippet ?? "No preview available"),
    textDiv("history-row-meta", meta),
    ...(notes.length ? [textDiv("history-row-note", notes.join(" \u00b7 "))] : []),
  );
  button.addEventListener("click", () => post({
    type: "historySelect",
    sessionId: entry.sessionId,
    agentRunId: entry.agentRunId,
  }));
  return button;
}

function renderMentionResults(items: MentionCandidateView[]): void {
  const governed = snapshot ? selectedCapability(snapshot)?.features.governed_context === true : false;
  if (!governed || items.length === 0) {
    mentionResults.replaceChildren();
    setMentionResultsVisible(false);
    return;
  }
  mentionResults.replaceChildren(...items.map((item, index) => {
    const option = actionButton(item.label, "mention-result", () => {
      prompt.value = prompt.value.replace(/(^|\s)@[^\s]*$/, "$1");
      post({ type: "mentionSelect", path: item.path });
      post({ type: "ephemeralState", hasDraft: prompt.value.length > 0 });
      setMentionResultsVisible(false);
    });
    option.id = `mention-option-${index}`;
    option.setAttribute("role", "option");
    return option;
  }));
  setMentionResultsVisible(true);
}

function setMentionResultsVisible(visible: boolean): void {
  mentionResults.hidden = !visible;
  prompt.setAttribute("aria-expanded", String(visible));
}

function renderContinuation(next: ConversationSnapshot): void {
  const notice = next.conversation?.continuationNotice;
  if (!notice) {
    continuationBanner.replaceChildren();
    continuationBanner.hidden = true;
    return;
  }
  const details = [
    ...(notice.omittedMessages ? [`${notice.omittedMessages} durable message${notice.omittedMessages === 1 ? "" : "s"} omitted`] : []),
    ...(notice.expiredArtifacts?.map((item) => `${item.name ?? item.kind}${item.bytes ? ` (${formatBytes(item.bytes)})` : ""}: bytes expired`) ?? []),
  ];
  continuationBanner.textContent = `${notice.message}${details.length ? ` ${details.join("; ")}.` : ""}`;
  continuationBanner.hidden = false;
}

function renderScreen(): void {
  const showHistory = activeScreen === "history";
  const showWelcome = !showHistory && snapshot?.conversation === null && (snapshot?.timeline.length ?? 0) === 0;
  historyScreen.hidden = !showHistory;
  welcome.hidden = !showWelcome;
  chatScreen.hidden = showHistory || showWelcome;
  $("composer").hidden = showHistory;
}

function renderWarnings(next: ConversationSnapshot): void {
  const capability = selectedCapability(next);
  warnings.replaceChildren(
    ...(capability?.warnings ?? []).map((warning) => textDiv("warning", warning)),
  );
}

function renderRuntimeStatus(next: ConversationSnapshot): void {
  const capability = selectedCapability(next);
  const runtime = capability?.id === "claude" ? capability.runtime : undefined;
  if (!runtime) {
    runtimeStatus.replaceChildren();
    runtimeStatus.hidden = true;
    delete runtimeStatus.dataset.state;
    return;
  }
  runtimeStatus.dataset.state = runtime.status;
  if (runtime.status === "ready") {
    runtimeStatus.textContent = `Claude subscription runtime ready${runtime.version ? ` (${runtime.version})` : ""}.`;
  } else if (runtime.status === "auth_required") {
    runtimeStatus.textContent = "Claude subscription authentication is required. Complete sign-in in an Anthropic-owned Claude application outside Kaizen, then Refresh capabilities.";
  } else {
    runtimeStatus.textContent = "Claude subscription runtime is unavailable. Repair or install the Kaizen-managed runtime, then Refresh capabilities.";
  }
  runtimeStatus.hidden = false;
}

function renderBanner(next: ConversationSnapshot): void {
  if (!next.banner) {
    banner.replaceChildren();
    banner.hidden = true;
    return;
  }
  const label = `${next.banner.message}${next.banner.code ? ` [${next.banner.code}]` : ""}`;
  const content: Node[] = [document.createTextNode(label)];
  if (next.banner.code === "DAEMON_UNREACHABLE") {
    content.push(document.createTextNode(" "), actionButton("Start Daemon in Visible Terminal", "banner-action", () => post({ type: "startDaemon" })));
  }
  banner.replaceChildren(...content);
  banner.hidden = false;
}

function renderState(next: ConversationSnapshot): void {
  const conversation = next.conversation;
  const readOnly = conversation?.readOnly ?? false;
  const running = conversation?.state === "running";
  const resumable = conversation?.resumable === true;
  const terminal = conversation?.state === "terminal" && !resumable;
  const queueMode = running || next.pendingPrompt;
  const governedContext = selectedCapability(next)?.features.governed_context === true;
  const imageAttachments = selectedCapability(next)?.features.image_attachments === true;
  const profileUsable = claudeProfileUsable(next);
  status.textContent = conversation
    ? `${conversation.controller} - ${conversation.engine} - ${resumable ? "resumable" : conversation.state}${conversation.terminalState && !resumable ? ` - ${conversation.terminalState}` : ""}`
    : next.pendingPrompt ? "Starting..." : "Ready";
  status.dataset.state = queueMode ? "running" : resumable ? "idle" : conversation?.state ?? "idle";

  prompt.disabled = readOnly || terminal || submittedPromptToken !== undefined;
  send.disabled = prompt.disabled || prompt.value.trim().length === 0 || !profileUsable;
  send.textContent = queueMode ? "Queue" : "Send";
  prompt.placeholder = readOnly
    ? "Observed conversations are read-only"
    : resumable
      ? "Send to continue this conversation..."
      : queueMode
        ? "Add a follow-up (runs only after normal completion)..."
        : "Message the agent...";
  turnStop.hidden = !running || readOnly;
  turnStop.disabled = !running || readOnly;
  kill.disabled = !conversation || conversation.state === "terminal" || readOnly;
  // U2 opens a separate controller/tab, so the origin can keep running without losing state.
  newConversation.disabled = false;
  contextActions.hidden = (!governedContext && !imageAttachments) || readOnly || terminal;
  addImage.hidden = !imageAttachments;
  addImage.disabled = !imageAttachments || next.pendingPrompt || next.imageChips.length >= MAX_IMAGES;
  addFile.hidden = !governedContext;
  addSelection.hidden = !governedContext;
  addFile.disabled = !governedContext || next.pendingPrompt;
  addSelection.disabled = !governedContext || next.pendingPrompt;
  if (!governedContext) {
    mentionResults.replaceChildren();
    setMentionResultsVisible(false);
  }
  const daemonUnreachable = next.banner?.code === "DAEMON_UNREACHABLE" || next.capabilities.length === 0;
  stopDaemon.hidden = daemonUnreachable;
  startDaemon.textContent = daemonUnreachable ? "Start Daemon in Visible Terminal" : "Show Daemon Terminal";
}

function renderLossWarning(): void {
  const draft = prompt.value.length > 0;
  const queued = snapshot?.followUpQueue.items.length ?? 0;
  const context = snapshot?.contextChips.length ?? 0;
  const images = snapshot?.imageChips.length ?? 0;
  if (!draft && queued === 0 && context === 0 && images === 0) {
    lossWarning.hidden = true;
    lossWarning.textContent = "";
    return;
  }
  const parts = [
    ...(draft ? ["draft"] : []),
    ...(queued ? [`${queued} queued follow-up${queued === 1 ? "" : "s"}`] : []),
    ...(context ? [`${context} context chip${context === 1 ? "" : "s"}`] : []),
    ...(images ? [`${images} image${images === 1 ? "" : "s"}`] : []),
  ];
  const pause = snapshot?.followUpQueue.paused
    ? ` Queue paused: ${pauseLabel(snapshot.followUpQueue.pauseReason)}. The head was preserved.`
    : "";
  lossWarning.textContent = `${parts.join(" and ")} ${parts.length === 1 ? "is" : "are"} temporary and will be lost if this tab reloads or closes.${pause}`;
  lossWarning.hidden = false;
}

function submit(): void {
  const text = prompt.value.trim();
  if (!text || !snapshot || submittedPromptToken || !claudeProfileUsable(snapshot)) return;
  submittedPromptToken = `popout-${Date.now()}-${++promptTokenSequence}`;
  prompt.disabled = true;
  send.disabled = true;
  post({ type: "userPrompt", prompt: text, requestToken: submittedPromptToken });
}

function reconcilePrompt(next: ConversationSnapshot): void {
  if (!submittedPromptToken || next.settledPromptToken !== submittedPromptToken) return;
  if (next.acceptedPromptToken === submittedPromptToken || next.queuedPromptToken === submittedPromptToken) {
    prompt.value = "";
  }
  submittedPromptToken = undefined;
  post({ type: "ephemeralState", hasDraft: prompt.value.length > 0 });
}

function sendProfile(): void {
  if (!snapshot || snapshot.selectorsLocked) return;
  const profile: ProfileSelection = {
    ...(model.value.trim() ? { model: model.value.trim() } : {}),
    ...(effort.value ? { reasoning_effort: effort.value } : {}),
    ...(!maxTurnsField.hidden && Number.isSafeInteger(maxTurns.valueAsNumber)
      ? { max_turns: maxTurns.valueAsNumber }
      : {}),
    permission_mode: permission.value as ProfileSelection["permission_mode"],
    auth_mode: auth.value as ProfileSelection["auth_mode"],
  };
  post({ type: "profileChange", engine: engine.value, profile });
}

engine.addEventListener("change", () => {
  const capability = snapshot?.capabilities.find((entry) => entry.id === engine.value);
  if (!capability) return;
  post({
    type: "profileChange",
    engine: capability.id,
    profile: {
      ...(capability.default_model ? { model: capability.default_model } : {}),
      ...(capability.default_reasoning_effort ? { reasoning_effort: capability.default_reasoning_effort } : {}),
      ...(capability.maxTurns ? { max_turns: capability.maxTurns.default } : {}),
      permission_mode: capability.permission_modes.includes("plan") ? "plan" : capability.permission_modes[0] ?? "plan",
      auth_mode: capability.auth_modes[0] ?? "none",
    },
  });
});
model.addEventListener("change", sendProfile);
model.addEventListener("input", () => {
  if (!snapshot || snapshot.selectorsLocked) return;
  profileRenderKey = "";
  const capability = snapshot.capabilities.find((entry) => entry.id === engine.value);
  const selectedModel = capability?.models.find((entry) => entry.id === model.value.trim());
  const efforts = selectedModel?.reasoning_efforts ?? [];
  replaceOptions(
    effort,
    [{ value: "", label: "Not available" }, ...efforts.map((value) => ({ value, label: value }))],
    effort.value,
  );
  effort.disabled = efforts.length === 0;
});
effort.addEventListener("change", sendProfile);
permission.addEventListener("change", sendProfile);
auth.addEventListener("change", sendProfile);
maxTurns.addEventListener("change", sendProfile);
refreshCapabilities.addEventListener("click", () => {
  overflow.open = false;
  post({ type: "refreshCapabilities" });
});
startDaemon.addEventListener("click", () => post({ type: "startDaemon" }));
stopDaemon.addEventListener("click", () => {
  overflow.open = false;
  post({ type: "stopDaemon" });
});
kill.addEventListener("click", () => {
  overflow.open = false;
  post({ type: "kill" });
});
turnStop.addEventListener("click", () => post({ type: "interrupt" }));
send.addEventListener("click", submit);
prompt.addEventListener("input", () => {
  if (snapshot) send.disabled = prompt.disabled || prompt.value.trim().length === 0 || !claudeProfileUsable(snapshot);
  renderLossWarning();
  post({ type: "ephemeralState", hasDraft: prompt.value.length > 0 });
  const query = mentionQuery(prompt.value);
  if (query !== undefined && snapshot && selectedCapability(snapshot)?.features.governed_context === true) {
    post({ type: "mentionQuery", query });
  } else {
    setMentionResultsVisible(false);
  }
});
prompt.addEventListener("keydown", (event: KeyboardEvent) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    submit();
  }
});
newConversation.addEventListener("click", () => post({ type: "newConversation", hasDraft: prompt.value.length > 0 }));
historyButton.addEventListener("click", () => {
  if (activeScreen === "history") {
    activeScreen = "conversation";
    renderScreen();
    prompt.focus();
  } else {
    post({ type: "showHistory" });
  }
});
historyDriven.addEventListener("click", () => {
  historyController = "driven";
  renderHistory();
});
historyObserved.addEventListener("click", () => {
  historyController = "observed";
  renderHistory();
});
addFile.addEventListener("click", () => post({ type: "addFile" }));
addSelection.addEventListener("click", () => post({ type: "addSelection" }));
addImage.addEventListener("click", () => post({ type: "addImage" }));
prompt.addEventListener("paste", (event: ClipboardEvent) => {
  if (!snapshot || selectedCapability(snapshot)?.features.image_attachments !== true) return;
  const files = Array.from(event.clipboardData?.files ?? []).filter(
    (file): file is PastedImage => isImageMediaType(file.type),
  );
  const remaining = Math.max(0, MAX_IMAGES - snapshot.imageChips.length);
  if (!files.length || remaining === 0) return;
  event.preventDefault();
  void stagePastedImages(files.slice(0, remaining));
});
transcript.addEventListener("click", (event) => {
  const button = (event.target as HTMLElement).closest<HTMLButtonElement>(".copy-code");
  const code = button?.closest<HTMLElement>(".code-block")?.querySelector<HTMLElement>("code");
  if (!button || !code) return;
  void navigator.clipboard.writeText(code.textContent ?? "").then(() => {
    button.textContent = "Copied";
    window.setTimeout(() => { button.textContent = "Copy"; }, 1200);
  });
});

post({ type: "ready" });
post({ type: "ephemeralState", hasDraft: false });

function selectedCapability(next: ConversationSnapshot): EngineCapabilityView | undefined {
  return next.capabilities.find((entry) => entry.id === next.selectedEngine);
}

function claudeProfileUsable(next: ConversationSnapshot): boolean {
  const capability = selectedCapability(next);
  if (capability?.id !== "claude") return true;
  // Old daemons have no runtime envelope; keep their already-supported basic chat path functional.
  if (!capability.runtime) return capability.drivable;
  const selectedModel = capability.models.find((entry) => entry.id === next.selectedProfile.model);
  const turns = next.selectedProfile.max_turns;
  const advertisedEfforts = selectedModel?.reasoning_efforts ?? [];
  const effortUsable = advertisedEfforts.length > 0
    ? !!next.selectedProfile.reasoning_effort && advertisedEfforts.includes(next.selectedProfile.reasoning_effort)
    : next.selectedProfile.reasoning_effort === undefined;
  return capability.runtime.status === "ready"
    && capability.drivable
    && capability.auth_modes.includes("subscription")
    && next.selectedProfile.auth_mode === "subscription"
    && !!selectedModel
    && effortUsable
    && !!capability.maxTurns
    && typeof turns === "number"
    && Number.isSafeInteger(turns)
    && turns >= capability.maxTurns.min
    && turns <= capability.maxTurns.max;
}

function replaceOptions(
  select: HTMLSelectElement,
  items: Array<{ value: string; label: string; disabled?: boolean }>,
  selected: string,
): void {
  select.replaceChildren(
    ...items.map((item) => {
      const option = document.createElement("option");
      option.value = item.value;
      option.textContent = item.label;
      option.disabled = item.disabled ?? false;
      option.selected = item.value === selected;
      return option;
    }),
  );
}

function decide(
  approval: ApprovalView,
  decision: "approve" | "deny",
  answerInputs: ReadonlyMap<string, HTMLTextAreaElement> = new Map(),
): void {
  const answers: Record<string, string> = {};
  if (decision === "approve") {
    for (const [id, input] of answerInputs) {
      if (!input.value.trim()) {
        input.setCustomValidity("Enter an explicit answer before approving.");
        input.reportValidity();
        return;
      }
      input.setCustomValidity("");
      answers[id] = input.value;
    }
  }
  post({
    type: decision,
    correlationId: approval.correlationId,
    agentRunId: approval.agentRunId,
    ...(decision === "approve" && answerInputs.size ? { answers } : {}),
  });
}

function decideDiff(approval: ApprovalView, type: "previewDiff" | "acceptDiff" | "rejectDiff"): void {
  post({ type, correlationId: approval.correlationId, agentRunId: approval.agentRunId });
}

function textDiv(className: string, text: string): HTMLDivElement {
  const div = document.createElement("div");
  div.className = className;
  div.textContent = text;
  return div;
}

function actionButton(label: string, className: string, action: () => void): HTMLButtonElement {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", action);
  return button;
}

async function stagePastedImages(files: PastedImage[]): Promise<void> {
  for (const file of files) {
    const uploadId = `paste-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    try {
      if (file.size < 1 || file.size > MAX_IMAGE_BYTES) throw new Error("image paste exceeds 4 MiB");
      const content = new Uint8Array(await file.arrayBuffer());
      post({ type: "imagePasteStart", uploadId, name: file.name || "pasted-image", mediaType: file.type, bytes: content.byteLength });
      for (let offset = 0, index = 0; offset < content.length; offset += IMAGE_CHUNK_BYTES, index += 1) {
        post({ type: "imagePasteChunk", uploadId, index, data: base64(content.subarray(offset, offset + IMAGE_CHUNK_BYTES)) });
      }
      post({ type: "imagePasteEnd", uploadId });
    } catch {
      post({ type: "imagePasteCancel", uploadId });
    }
  }
}

function base64(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function isImageMediaType(value: string): value is ImageChipView["mediaType"] {
  return value === "image/png" || value === "image/jpeg" || value === "image/webp" || value === "image/gif";
}

function mentionQuery(value: string): string | undefined {
  const match = /(?:^|\s)@([^\s]*)$/.exec(value);
  return match?.[1];
}

function displayDate(value: string | undefined): string | undefined {
  if (!value) return undefined;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function formatBytes(bytes: number): string {
  return bytes < 1024 ? `${bytes} B` : `${Math.ceil(bytes / 1024)} KiB`;
}

function pauseLabel(reason: ConversationSnapshot["followUpQueue"]["pauseReason"]): string {
  switch (reason) {
    case "approval_timeout": return "approval timed out";
    case "lease_conflict": return "workspace writer is busy";
    case "uncertain_transport": return "transport state is uncertain";
    case "interrupt": return "turn stopped";
    case "kill": return "conversation killed";
    default: return "turn ended abnormally";
  }
}

function title(value: string): string {
  return value.replace(/(^|-)([a-z])/g, (_, prefix: string, letter: string) => `${prefix ? " " : ""}${letter.toUpperCase()}`);
}
