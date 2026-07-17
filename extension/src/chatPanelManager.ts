/** Pure registry/lifecycle manager; VS Code panel construction is injected by chatPanel.ts. */

import type { ConversationSnapshot, HostToWebview, ProfileSelection, WebviewToHost } from "./webviewProtocol";
import type { StagedContext } from "./contextStager";
import type { PasteStart, StagedImage } from "./imageStager";
import type { PendingDiffApproval } from "./conversationController";
import { historyFromSessionList } from "./historyModel";
import { PanelMetadata, PanelMemento, PanelStateStore } from "./panelState";
import { SessionIndex, SessionIndexUpdate } from "./sessionIndex";

export interface DisposablePort {
  dispose(): void;
}

export interface EditorPanelPort {
  reveal(): void;
  dispose(): void;
  setTitle(title: string): void;
  onDidDispose(listener: () => void): DisposablePort;
  onDidActivate(listener: () => void): DisposablePort;
}

export interface ManagedController {
  initialize(): Promise<void>;
  refreshObserved(): Promise<void>;
  sessionIndexFailed(error: Error): void;
  snapshot(): ConversationSnapshot;
  supportsGovernedContext(): boolean;
  addContext(item: StagedContext): boolean;
  removeContext(id: string): void;
  contextCount(): number;
  supportsImages(): boolean;
  addImage(item: StagedImage): boolean;
  removeImage(id: string): void;
  imageCount(): number;
  pendingDiff(agentRunId?: string, correlationId?: string): PendingDiffApproval | undefined;
  markDiffCorrupt(agentRunId: string, correlationId: string, revision: number): void;
  resolveDiff(agentRunId: string, correlationId: string, decision: "approve" | "deny", metadataConfirmed?: boolean): Promise<boolean>;
  isGenuinelyEmpty(): boolean;
  openHistoryConversation(sessionId: string, agentRunId: string, controller: "driven" | "observed"): Promise<void>;
  handle(message: WebviewToHost, rendererId?: string): Promise<void>;
  testExtensionObservation(): {
    profile: import("./sessionClient").ConversationProfile;
    profileAttested: boolean;
    stream: import("./streamReducer").StreamAcceptanceState;
    approvalEvents: import("./conversationController").TestExtensionApprovalEvent[];
    turnEvents: import("./conversationController").TestExtensionTurnEvent[];
  };
  dispose(): void;
}

export interface ManagedRenderer extends DisposablePort {
  focusInput(): void;
  postMessage(message: HostToWebview): PromiseLike<boolean> | void;
}

export interface RendererActions {
  newConversation(): void;
  draftChanged(hasDraft: boolean): void;
  showHistory(): void;
  historySelect(sessionId: string, agentRunId: string): void;
  mentionQuery(query: string): void;
  mentionSelect(path: string): void;
  addFile(): void;
  addSelection(): void;
  contextRemove(id: string): void;
  addImage(): void;
  imageRemove(id: string): void;
  imagePasteStart(start: PasteStart): void;
  imagePasteChunk(uploadId: string, index: number, bytes: number[]): void;
  imagePasteEnd(uploadId: string): void;
  imagePasteCancel(uploadId: string): void;
  previewDiff(agentRunId: string, correlationId: string): void;
  acceptDiff(agentRunId: string, correlationId: string): void;
  rejectDiff(agentRunId: string, correlationId: string): void;
  diffSnapshotChanged(): void;
}

/** Host construction ports plus optional hooks whose presence enables context, image, and diff features. */
export interface PanelManagerDependencies {
  createPanel(title: string): EditorPanelPort;
  createController(conversationId: string, memento: PanelMemento): ManagedController;
  createRenderer(
    rendererId: string,
    conversationId: string,
    panel: EditorPanelPort,
    controller: ManagedController,
    actions: RendererActions,
  ): ManagedRenderer;
  confirmLoss(state: { hasDraft: boolean; queuedCount: number; contextCount: number; imageCount: number }): Promise<boolean>;
  findMentions?(query: string): Promise<string[]>;
  stageMention?(path: string): Promise<StagedContext>;
  pickFile?(): Promise<StagedContext | undefined>;
  pickSelection?(): Promise<StagedContext | undefined>;
  contextError?(error: unknown): void;
  pickImages?(remaining: number): Promise<StagedImage[]>;
  pasteStart?(start: PasteStart): void;
  pasteChunk?(uploadId: string, index: number, bytes: number[]): void;
  pasteFinish?(uploadId: string): Promise<StagedImage>;
  pasteCancel?(uploadId: string): void;
  previewDiff?(approval: PendingDiffApproval, isCurrent: () => boolean): Promise<boolean>;
  confirmMetadataDiff?(approval: PendingDiffApproval): Promise<boolean>;
  diffError?(error: unknown): void;
  refreshDiffDocuments?(): void;
}

interface PanelEntry {
  conversationId: string;
  panel: EditorPanelPort;
  controller: ManagedController;
  renderer: ManagedRenderer;
  disposables: DisposablePort[];
  hasDraft: boolean;
}

/** Injected conversationId-to-panel registry owning focus, lifecycle, history, and diff routing. */
export class ChatPanelManagerCore implements DisposablePort {
  private static readonly MAX_IMAGES = 4;
  private readonly entries = new Map<string, PanelEntry>();
  private readonly subscriptions: DisposablePort[] = [];
  private initializePromise: Promise<void> | undefined;
  private activeId: string | undefined;
  private rendererSequence = 0;
  private shuttingDown = false;

  constructor(
    private readonly state: PanelStateStore,
    private readonly sessionIndex: SessionIndex,
    private readonly dependencies: PanelManagerDependencies,
  ) {}

  /** Coalesce initialization callers; clear a rejected attempt so a later call can retry. */
  initialize(): Promise<void> {
    if (this.initializePromise) return this.initializePromise;
    this.initializePromise = this.initializeNow().catch((error: unknown) => {
      this.initializePromise = undefined;
      throw error;
    });
    return this.initializePromise;
  }

  /** Focus the active tab, newest open tab, or persisted tab in order; create only as a last resort. */
  async openOrFocus(): Promise<string> {
    await this.initialize();
    const current = this.activeId ? this.entries.get(this.activeId) : undefined;
    if (current) {
      current.panel.reveal();
      current.renderer.focusInput();
      return current.conversationId;
    }
    let existing: PanelEntry | undefined;
    for (const entry of this.entries.values()) {
      const order = this.state.get(entry.conversationId)?.order ?? 0;
      const newest = existing ? this.state.get(existing.conversationId)?.order ?? 0 : -1;
      if (!existing || order > newest) existing = entry;
    }
    if (existing) {
      this.activate(existing);
      existing.panel.reveal();
      existing.renderer.focusInput();
      return existing.conversationId;
    }
    const persisted = this.state.openRecords()[0];
    if (persisted) return this.openRecord(persisted, undefined, true);
    return this.newConversation();
  }

  async newConversation(): Promise<string> {
    await this.initialize();
    const record = this.state.create();
    return this.openRecord(record, undefined, true);
  }

  /** Narrow acceptance driver: normal panel, controller.handle, renderer, and protocol only. */
  async testExtensionDrive(
    engine: string,
    profile: ProfileSelection,
    prompt: string,
    contexts: readonly StagedContext[] = [],
    images: readonly StagedImage[] = [],
  ): Promise<string> {
    const conversationId = await this.testExtensionPrepare(engine, profile, contexts, images);
    try {
      await this.testExtensionPrompt(conversationId, prompt, `te-${conversationId}`);
      return conversationId;
    } catch (error) {
      await this.testExtensionClose(conversationId);
      throw error;
    }
  }

  /** Prepare a normal chat tab without starting its turn, allowing outer proof to arm first. */
  async testExtensionPrepare(
    engine: string,
    profile: ProfileSelection,
    contexts: readonly StagedContext[] = [],
    images: readonly StagedImage[] = [],
  ): Promise<string> {
    const conversationId = await this.newConversation();
    const entry = this.entries.get(conversationId);
    if (!entry) throw new Error("Test Extension conversation was not created");
    try {
      await entry.controller.handle({ type: "profileChange", engine, profile });
      if (contexts.some((item) => !entry.controller.addContext(item)) || images.some((item) => !entry.controller.addImage(item))) {
        throw new Error("Test Extension context or image envelope was refused");
      }
      return conversationId;
    } catch (error) {
      entry.panel.dispose();
      throw error;
    }
  }

  /** Read-only observation sink; returns the controller's defensive snapshot clone. */
  testExtensionSnapshot(conversationId: string): ConversationSnapshot | undefined {
    const snapshot = this.entries.get(conversationId)?.controller.snapshot();
    return snapshot === undefined ? undefined : structuredClone(snapshot);
  }

  /** Explicit catalog refresh through the normal controller/client boundary. */
  async testExtensionRefreshCapabilities(conversationId: string): Promise<ConversationSnapshot> {
    const entry = this.entries.get(conversationId);
    if (!entry) throw new Error("Test Extension conversation is unavailable");
    await entry.controller.handle({ type: "refreshCapabilities" });
    return entry.controller.snapshot();
  }

  testExtensionObservation(conversationId: string): ReturnType<ManagedController["testExtensionObservation"]> | undefined {
    return this.entries.get(conversationId)?.controller.testExtensionObservation();
  }

  /** Test Extension control still traverses the normal controller message boundary. */
  async testExtensionInterrupt(conversationId: string): Promise<void> {
    const entry = this.entries.get(conversationId);
    if (!entry) throw new Error("Test Extension conversation is unavailable");
    await entry.controller.handle({ type: "interrupt" });
  }

  async testExtensionPrompt(conversationId: string, prompt: string, requestToken?: string): Promise<void> {
    const entry = this.entries.get(conversationId);
    if (!entry) throw new Error("Test Extension conversation is unavailable");
    await entry.controller.handle({
      type: "userPrompt", prompt, requestToken: requestToken ?? `te-followup-${conversationId}-${Date.now()}`,
    });
  }

  /** Read-only negotiated metadata for an outer-runner corruption or stale-base action. */
  testExtensionPendingDiff(conversationId: string): PendingDiffApproval | undefined {
    const pending = this.entries.get(conversationId)?.controller.pendingDiff();
    if (!pending) return undefined;
    return {
      ...pending,
      diff: {
        ...pending.diff,
        fileChanges: pending.diff.fileChanges.map((change) => ({
          ...change,
          before: change.before ? { ...change.before } : null,
          proposed: change.proposed ? { ...change.proposed } : null,
        })),
      },
    };
  }

  async testExtensionClose(conversationId: string): Promise<boolean> {
    const entry = this.entries.get(conversationId);
    if (!entry) return true;
    const before = entry.controller.snapshot().conversation;
    if (before?.state === "running") return false;
    if (before?.state === "idle") await entry.controller.handle({ type: "close", hasDraft: false });
    const closed = entry.controller.snapshot().conversation;
    const quiescent = before === null || (closed?.state === "terminal" && closed.readOnly === true);
    if (!quiescent) return false;
    entry.panel.dispose();
    return !this.entries.has(conversationId);
  }

  async showHistory(conversationId?: string): Promise<string> {
    await this.initialize();
    const id = conversationId ?? await this.openOrFocus();
    const entry = this.entries.get(id);
    if (entry) {
      this.activate(entry);
      entry.panel.reveal();
      void entry.renderer.postMessage({ type: "showHistory" });
    }
    return id;
  }

  async openHistory(sessionId: string, agentRunId: string): Promise<string | undefined> {
    await this.initialize();
    const history = historyFromSessionList(this.sessionIndex.current());
    const selected = history.entries.find((entry) => entry.sessionId === sessionId && entry.agentRunId === agentRunId);
    if (!selected) return undefined;
    const active = this.activeId ? this.entries.get(this.activeId) : undefined;
    const reusable = active && !active.hasDraft && active.controller.isGenuinelyEmpty() ? active : undefined;
    const target = reusable
      ? reusable
      : this.entries.get(await this.newConversation());
    if (!target) return undefined;
    try {
      await target.controller.openHistoryConversation(selected.sessionId, selected.agentRunId, selected.controller);
    } catch (error) {
      this.dependencies.contextError?.(error);
      if (!reusable) target.panel.dispose();
      return undefined;
    }
    this.activate(target);
    target.panel.reveal();
    void target.renderer.postMessage({ type: "showChat" });
    return target.conversationId;
  }

  async addFile(): Promise<boolean> {
    const id = await this.openOrFocus();
    return this.addPickedContext(id, this.dependencies.pickFile);
  }

  async addSelection(): Promise<boolean> {
    const id = await this.openOrFocus();
    return this.addPickedContext(id, this.dependencies.pickSelection);
  }

  async addImages(): Promise<boolean> {
    const id = await this.openOrFocus();
    return this.addPickedImages(id);
  }

  previewDiff(conversationId?: string, agentRunId?: string, correlationId?: string): Promise<boolean> {
    return this.withDiff(conversationId, agentRunId, correlationId, async (entry, approval) => {
      if (approval.corrupt || !this.dependencies.previewDiff) return false;
      try {
        return await this.dependencies.previewDiff(approval, () => {
          const current = entry.controller.pendingDiff(approval.agentRunId, approval.correlationId);
          return current?.diff.revision === approval.diff.revision &&
            current.diff.snapshotSetSha256 === approval.diff.snapshotSetSha256 && !current.corrupt;
        });
      } catch (error) {
        entry.controller.markDiffCorrupt(approval.agentRunId, approval.correlationId, approval.diff.revision);
        this.dependencies.diffError?.(error);
        return false;
      }
    });
  }

  acceptDiff(conversationId?: string, agentRunId?: string, correlationId?: string): Promise<boolean> {
    return this.withDiff(conversationId, agentRunId, correlationId, async (entry, approval) => {
      if (approval.corrupt) return false;
      const confirmed = approval.diff.metadataConfirmationRequired
        ? await this.dependencies.confirmMetadataDiff?.(approval) === true
        : false;
      if (approval.diff.metadataConfirmationRequired && !confirmed) return false;
      const current = entry.controller.pendingDiff(approval.agentRunId, approval.correlationId);
      if (!current || current.corrupt || current.diff.revision !== approval.diff.revision ||
          current.diff.snapshotSetSha256 !== approval.diff.snapshotSetSha256) return false;
      return entry.controller.resolveDiff(approval.agentRunId, approval.correlationId, "approve", confirmed);
    });
  }

  rejectDiff(conversationId?: string, agentRunId?: string, correlationId?: string): Promise<boolean> {
    return this.withDiff(conversationId, agentRunId, correlationId, (entry, approval) =>
      entry.controller.resolveDiff(approval.agentRunId, approval.correlationId, "deny"));
  }

  /** Revive a validated free id, reopen closed metadata, or allocate a fresh unique record. */
  async revive(panel: EditorPanelPort, serializedState: unknown): Promise<string> {
    await this.initialize();
    const requestedId = serializedConversationId(serializedState);
    let record = requestedId ? this.state.get(requestedId) : undefined;
    if (!record || this.entries.has(record.conversationId)) {
      record = this.state.create(requestedId);
    } else if (record.closedAt !== undefined) {
      record = this.state.reopen(record.conversationId) ?? record;
    }
    return this.openRecord(record, panel, false);
  }

  async reopenClosed(): Promise<string | undefined> {
    await this.initialize();
    const record = this.state.recentlyClosed()[0];
    if (!record) return undefined;
    const reopened = this.state.reopen(record.conversationId);
    return reopened ? this.openRecord(reopened, undefined, true) : undefined;
  }

  async focusInput(): Promise<string> {
    const conversationId = await this.openOrFocus();
    this.entries.get(conversationId)?.renderer.focusInput();
    return conversationId;
  }

  async closePanel(conversationId: string): Promise<boolean> {
    const entry = this.entries.get(conversationId);
    if (!entry) return false;
    const queuedCount = entry.controller.snapshot().followUpQueue.items.length;
    const contextCount = entry.controller.contextCount();
    const imageCount = entry.controller.imageCount();
    if ((entry.hasDraft || queuedCount > 0 || contextCount > 0 || imageCount > 0) &&
        !(await this.dependencies.confirmLoss({ hasDraft: entry.hasDraft, queuedCount, contextCount, imageCount }))) {
      return false;
    }
    entry.panel.dispose();
    return true;
  }

  refreshSessions(): Promise<SessionIndexUpdate> {
    return this.sessionIndex.refresh();
  }

  openConversationIds(): string[] {
    return [...this.entries.keys()];
  }

  activeConversationId(): string | undefined {
    return this.activeId;
  }

  controllerFor(conversationId: string): ManagedController | undefined {
    return this.entries.get(conversationId)?.controller;
  }

  dispose(): void {
    this.shuttingDown = true;
    while (this.subscriptions.length) this.subscriptions.pop()?.dispose();
    for (const entry of this.entries.values()) {
      this.disposeEntry(entry, false);
      entry.panel.dispose();
    }
    this.entries.clear();
    this.activeId = undefined;
  }

  private async initializeNow(): Promise<void> {
    await this.state.initialize();
    this.subscriptions.push(
      this.state.subscribe((records) => this.syncTitles(records)),
      this.sessionIndex.subscribe((update) => this.applySessionIndex(update)),
    );
    // Do not hold serializer revival behind a daemon timeout; the poll fans in when it resolves.
    void this.sessionIndex.refresh();
  }

  private async openRecord(
    record: PanelMetadata,
    suppliedPanel: EditorPanelPort | undefined,
    reveal: boolean,
  ): Promise<string> {
    const panel = suppliedPanel ?? this.dependencies.createPanel(panelTitle(record));
    const controller = this.dependencies.createController(record.conversationId, this.state.scopedMemento(record.conversationId));
    const rendererId = `renderer-${++this.rendererSequence}-${record.conversationId}`;
    const renderer = this.dependencies.createRenderer(
      rendererId,
      record.conversationId,
      panel,
      controller,
      {
        newConversation: () => { void this.newConversation(); },
        draftChanged: (hasDraft) => {
          const entry = this.entries.get(record.conversationId);
          if (entry) entry.hasDraft = hasDraft;
        },
        showHistory: () => { void this.showHistory(record.conversationId); },
        historySelect: (sessionId, agentRunId) => { void this.openHistory(sessionId, agentRunId); },
        mentionQuery: (query) => { void this.sendMentionResults(record.conversationId, query); },
        mentionSelect: (relativePath) => { void this.stageMention(record.conversationId, relativePath); },
        addFile: () => { void this.addPickedContext(record.conversationId, this.dependencies.pickFile); },
        addSelection: () => { void this.addPickedContext(record.conversationId, this.dependencies.pickSelection); },
        contextRemove: (id) => this.entries.get(record.conversationId)?.controller.removeContext(id),
        addImage: () => { void this.addPickedImages(record.conversationId); },
        imageRemove: (id) => this.entries.get(record.conversationId)?.controller.removeImage(id),
        imagePasteStart: (start) => this.startPaste(record.conversationId, start),
        imagePasteChunk: (uploadId, index, bytes) => this.runPaste(() => this.dependencies.pasteChunk?.(uploadId, index, bytes)),
        imagePasteEnd: (uploadId) => { void this.finishPaste(record.conversationId, uploadId); },
        imagePasteCancel: (uploadId) => this.dependencies.pasteCancel?.(uploadId),
        previewDiff: (agentRunId, correlationId) => { void this.previewDiff(record.conversationId, agentRunId, correlationId); },
        acceptDiff: (agentRunId, correlationId) => { void this.acceptDiff(record.conversationId, agentRunId, correlationId); },
        rejectDiff: (agentRunId, correlationId) => { void this.rejectDiff(record.conversationId, agentRunId, correlationId); },
        diffSnapshotChanged: () => this.dependencies.refreshDiffDocuments?.(),
      },
    );
    const entry: PanelEntry = {
      conversationId: record.conversationId,
      panel,
      controller,
      renderer,
      disposables: [],
      hasDraft: false,
    };
    entry.disposables.push(
      panel.onDidDispose(() => this.panelDisposed(record.conversationId)),
      panel.onDidActivate(() => this.activate(entry)),
    );
    this.entries.set(record.conversationId, entry);
    this.activate(entry);
    panel.setTitle(panelTitle(record));
    if (reveal) panel.reveal();
    await controller.initialize();
    const index = this.sessionIndex.snapshot();
    if (index.response) await controller.refreshObserved();
    else if (index.error) controller.sessionIndexFailed(index.error);
    return record.conversationId;
  }

  private activate(entry: PanelEntry): void {
    if (!this.entries.has(entry.conversationId)) return;
    this.activeId = entry.conversationId;
    this.state.touch(entry.conversationId);
  }

  private panelDisposed(conversationId: string): void {
    const entry = this.entries.get(conversationId);
    if (!entry) return;
    this.entries.delete(conversationId);
    if (this.activeId === conversationId) this.activeId = undefined;
    this.disposeEntry(entry, !this.shuttingDown);
  }

  private disposeEntry(entry: PanelEntry, markClosed: boolean): void {
    while (entry.disposables.length) entry.disposables.pop()?.dispose();
    entry.renderer.dispose();
    entry.controller.dispose();
    if (markClosed) this.state.close(entry.conversationId);
  }

  private applySessionIndex(update: SessionIndexUpdate): void {
    if (update.response) {
      this.state.reconcileSessionList(update.response);
      for (const entry of this.entries.values()) void entry.controller.refreshObserved();
    } else if (update.error) {
      for (const entry of this.entries.values()) entry.controller.sessionIndexFailed(update.error);
    }
  }

  private syncTitles(records: readonly PanelMetadata[]): void {
    const byId = new Map(records.map((record) => [record.conversationId, record]));
    for (const entry of this.entries.values()) {
      const record = byId.get(entry.conversationId);
      if (record) entry.panel.setTitle(panelTitle(record));
    }
  }

  private async sendMentionResults(conversationId: string, query: string): Promise<void> {
    const entry = this.entries.get(conversationId);
    if (!entry || !entry.controller.supportsGovernedContext() || !this.dependencies.findMentions) return;
    try {
      const paths = await this.dependencies.findMentions(query);
      if (!this.entries.has(conversationId)) return;
      void entry.renderer.postMessage({
        type: "mentionResults",
        query,
        items: paths.slice(0, 50).map((path) => ({ path, label: path })),
      });
    } catch (error) {
      this.dependencies.contextError?.(error);
    }
  }

  private async stageMention(conversationId: string, relativePath: string): Promise<boolean> {
    if (!this.dependencies.stageMention) return false;
    try {
      return this.entries.get(conversationId)?.controller.addContext(await this.dependencies.stageMention(relativePath)) ?? false;
    } catch (error) {
      this.dependencies.contextError?.(error);
      return false;
    }
  }

  private async addPickedContext(
    conversationId: string,
    picker: (() => Promise<StagedContext | undefined>) | undefined,
  ): Promise<boolean> {
    const entry = this.entries.get(conversationId);
    if (!entry || !picker) return false;
    if (!entry.controller.supportsGovernedContext()) {
      this.dependencies.contextError?.(Object.assign(
        new Error("Governed context is unavailable for the selected engine"),
        { code: "DENIED_CONTEXT_UNSUPPORTED" },
      ));
      return false;
    }
    try {
      const item = await picker();
      return item ? entry.controller.addContext(item) : false;
    } catch (error) {
      this.dependencies.contextError?.(error);
      return false;
    }
  }

  private async addPickedImages(conversationId: string): Promise<boolean> {
    const entry = this.entries.get(conversationId);
    if (!entry || !this.dependencies.pickImages) return false;
    if (!entry.controller.supportsImages()) {
      this.dependencies.contextError?.(Object.assign(new Error("Image attachments are unavailable for the selected engine"), {
        code: "DENIED_ATTACHMENT_UNSUPPORTED",
      }));
      return false;
    }
    try {
      const images = await this.dependencies.pickImages(Math.max(0, ChatPanelManagerCore.MAX_IMAGES - entry.controller.imageCount()));
      return images.reduce((accepted, image) => entry.controller.addImage(image) || accepted, false);
    } catch (error) {
      this.dependencies.contextError?.(error);
      return false;
    }
  }

  private runPaste(action: () => void): void {
    try {
      action();
    } catch (error) {
      this.dependencies.contextError?.(error);
    }
  }

  private startPaste(conversationId: string, start: PasteStart): void {
    const controller = this.entries.get(conversationId)?.controller;
    if (!controller?.supportsImages() || controller.imageCount() >= ChatPanelManagerCore.MAX_IMAGES) {
      this.dependencies.pasteCancel?.(start.uploadId);
      return;
    }
    this.runPaste(() => this.dependencies.pasteStart?.(start));
  }

  private async finishPaste(conversationId: string, uploadId: string): Promise<void> {
    try {
      const image = await this.dependencies.pasteFinish?.(uploadId);
      const controller = this.entries.get(conversationId)?.controller;
      if (image && controller?.supportsImages() && controller.imageCount() < ChatPanelManagerCore.MAX_IMAGES) controller.addImage(image);
    } catch (error) {
      this.dependencies.contextError?.(error);
    }
  }

  /** Resolve a requested or active panel's pending diff before invoking a shared diff action. */
  private async withDiff(
    conversationId: string | undefined,
    agentRunId: string | undefined,
    correlationId: string | undefined,
    action: (entry: PanelEntry, approval: PendingDiffApproval) => Promise<boolean>,
  ): Promise<boolean> {
    const id = conversationId ?? this.activeId;
    const entry = id ? this.entries.get(id) : undefined;
    const approval = entry?.controller.pendingDiff(agentRunId, correlationId);
    if (!entry || !approval) {
      this.dependencies.diffError?.(new Error("No active pending negotiated diff"));
      return false;
    }
    return action(entry, approval);
  }
}

/** Shape-check, trim, and bound untrusted serializer state before using it as a conversation id. */
function serializedConversationId(value: unknown): string | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const id = (value as Record<string, unknown>).conversationId;
  return typeof id === "string" && id.trim() ? id.trim().slice(0, 128) : undefined;
}

function panelTitle(record: PanelMetadata): string {
  return record.title || "Kaizen Chat";
}
