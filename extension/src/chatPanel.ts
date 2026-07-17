/** VS Code adapter for the pure multi-panel registry. */

import * as vscode from "vscode";
import * as path from "node:path";
import { randomUUID } from "node:crypto";

import {
  ChatPanelManagerCore,
  DisposablePort,
  EditorPanelPort,
  PanelManagerDependencies,
} from "./chatPanelManager";
import { ChatRenderer } from "./chatRenderer";
import { artifactRoot } from "./artifactStore";
import { ControllerPrompts, ConversationController } from "./conversationController";
import { ContextStageError, ContextStager, StagedContext } from "./contextStager";
import { ImageChunkAssembler, ImageStager } from "./imageStager";
import { KaizenDiffProvider } from "./diffProvider";
import { MentionIndex, normalizeRelativePath } from "./mentionIndex";
import { PanelStateStore } from "./panelState";
import { SessionClient } from "./sessionClient";
import { SessionIndex } from "./sessionIndex";
import type { ProfileSelection } from "./webviewProtocol";

export class ChatPanelManager implements vscode.Disposable, vscode.WebviewPanelSerializer {
  static readonly viewType = "kaizenChat.popout";

  private readonly core: ChatPanelManagerCore;
  private lastTextEditor: vscode.TextEditor | undefined;

  constructor(
    context: vscode.ExtensionContext,
    repoRoot: string,
    sessionIndex: SessionIndex,
    prompts: ControllerPrompts,
  ) {
    const state = new PanelStateStore(context.workspaceState);
    const stager = new ContextStager(repoRoot);
    const imageStager = new ImageStager(repoRoot);
    const imageAssembler = new ImageChunkAssembler(imageStager);
    const diffProvider = new KaizenDiffProvider(repoRoot);
    context.subscriptions.push(
      diffProvider,
      vscode.workspace.registerTextDocumentContentProvider(KaizenDiffProvider.scheme, diffProvider),
    );
    const imageCacheRoot = vscode.Uri.file(artifactRoot(repoRoot, "images"));
    const mentions = new MentionIndex(repoRoot, async () => {
      const uris = await vscode.workspace.findFiles("**/*", undefined, 50);
      return uris.flatMap((uri) => {
        const relative = normalizeRelativePath(path.relative(repoRoot, uri.fsPath));
        return relative ? [relative] : [];
      });
    });
    this.lastTextEditor = vscode.window.activeTextEditor;
    context.subscriptions.push(vscode.window.onDidChangeActiveTextEditor((editor) => {
      if (editor?.document.uri.scheme === "file") this.lastTextEditor = editor;
    }));
    const stageRelative = (relativePath: string) => stager.file(
      `context-${randomUUID()}`,
      path.resolve(repoRoot, relativePath),
    );
    const pickFile = async (): Promise<StagedContext | undefined> => {
      const selected = await vscode.window.showQuickPick(await mentions.search(), {
        title: "Add governed file context",
        placeHolder: "Workspace file",
      });
      return selected ? stageRelative(selected) : undefined;
    };
    const pickSelection = async (): Promise<StagedContext | undefined> => {
      const editor = vscode.window.activeTextEditor?.document.uri.scheme === "file"
        ? vscode.window.activeTextEditor
        : this.lastTextEditor;
      if (!editor || editor.document.uri.scheme !== "file") {
        throw new ContextStageError("DENIED_CONTEXT_SELECTION_REQUIRED", "Open a workspace text editor and select text first");
      }
      const selection = editor.selection;
      if (selection.isEmpty) {
        throw new ContextStageError("DENIED_CONTEXT_SELECTION_REQUIRED", "Select text before adding selection context");
      }
      return stager.selection(
        `context-${randomUUID()}`,
        editor.document.uri.fsPath,
        {
          start: { line: selection.start.line, character: selection.start.character },
          end: { line: selection.end.line, character: selection.end.character },
        },
        editor.document.getText(selection),
      );
    };
    const dependencies: PanelManagerDependencies = {
      createPanel: (title) => new VsCodePanel(vscode.window.createWebviewPanel(
        ChatPanelManager.viewType,
        title,
        vscode.ViewColumn.Active,
        { enableScripts: true },
      )),
      createController: (_conversationId, memento) => new ConversationController(
        memento,
        (callbacks) => new SessionClient(repoRoot, callbacks),
        prompts,
        sessionIndex,
      ),
      createRenderer: (rendererId, conversationId, panel, controller, actions) => new ChatRenderer(
        rendererId,
        conversationId,
        asVsCodePanel(panel).webview,
        context.extensionUri,
        controller as ConversationController,
        actions,
        imageCacheRoot,
      ),
      confirmLoss: (loss) => prompts.confirmEphemeralLoss?.(loss) ?? Promise.resolve(true),
      findMentions: (query) => mentions.search(query),
      stageMention: stageRelative,
      pickFile,
      pickSelection,
      contextError: (error) => {
        const code = error && typeof error === "object" && "code" in error && typeof error.code === "string"
          ? ` [${error.code}]`
          : "";
        void vscode.window.showWarningMessage(`${error instanceof Error ? error.message : String(error)}${code}`);
      },
      pickImages: async (remaining) => {
        if (remaining < 1) return [];
        const selected = await vscode.window.showOpenDialog({
          canSelectFiles: true,
          canSelectFolders: false,
          canSelectMany: remaining > 1,
          defaultUri: vscode.Uri.file(repoRoot),
          filters: { Images: ["png", "jpg", "jpeg", "webp", "gif"] },
          title: `Add up to ${remaining} image${remaining === 1 ? "" : "s"}`,
        });
        return Promise.all((selected ?? []).slice(0, remaining).map((uri) =>
          imageStager.file(`image-${randomUUID()}`, uri.fsPath)));
      },
      pasteStart: (start) => imageAssembler.begin(start),
      pasteChunk: (uploadId, index, bytes) => imageAssembler.chunk(uploadId, index, bytes),
      pasteFinish: (uploadId) => imageAssembler.finish(uploadId, `image-${randomUUID()}`),
      pasteCancel: (uploadId) => imageAssembler.cancel(uploadId),
      previewDiff: (approval, isCurrent) => diffProvider.preview({ ...approval, isCurrent }),
      confirmMetadataDiff: async (approval) => {
        const count = approval.diff.fileChanges.filter((change) => change.previewMode === "metadata").length;
        const choice = await vscode.window.showWarningMessage(
          `Accept revision ${approval.diff.revision}? ${count} change${count === 1 ? "" : "s"} can only be confirmed by metadata. This accepts the whole request.`,
          { modal: true },
          "Accept whole request",
        );
        return choice === "Accept whole request";
      },
      diffError: (error) => {
        void vscode.window.showWarningMessage(error instanceof Error ? error.message : String(error));
      },
      refreshDiffDocuments: () => diffProvider.refreshLifecycle(),
    };
    this.core = new ChatPanelManagerCore(state, sessionIndex, dependencies);
  }

  initialize(): Promise<void> {
    return this.core.initialize();
  }

  openOrFocus(): Promise<string> {
    return this.core.openOrFocus();
  }

  newConversation(): Promise<string> {
    return this.core.newConversation();
  }

  showHistory(): Promise<string> {
    return this.core.showHistory();
  }

  addFile(): Promise<boolean> {
    return this.core.addFile();
  }

  addSelection(): Promise<boolean> {
    return this.core.addSelection();
  }

  addImages(): Promise<boolean> {
    return this.core.addImages();
  }

  previewDiff(): Promise<boolean> {
    return this.core.previewDiff();
  }

  acceptDiff(): Promise<boolean> {
    return this.core.acceptDiff();
  }

  rejectDiff(): Promise<boolean> {
    return this.core.rejectDiff();
  }

  reopenClosed(): Promise<string | undefined> {
    return this.core.reopenClosed();
  }

  focusInput(): Promise<string> {
    return this.core.focusInput();
  }

  testExtensionDrive(
    engine: string,
    profile: ProfileSelection,
    prompt: string,
    contexts: readonly StagedContext[] = [],
    images: readonly import("./imageStager").StagedImage[] = [],
  ): Promise<string> {
    return this.core.testExtensionDrive(engine, profile, prompt, contexts, images);
  }

  testExtensionPrepare(
    engine: string,
    profile: ProfileSelection,
    contexts: readonly StagedContext[] = [],
    images: readonly import("./imageStager").StagedImage[] = [],
  ): Promise<string> {
    return this.core.testExtensionPrepare(engine, profile, contexts, images);
  }

  testExtensionSnapshot(conversationId: string): import("./webviewProtocol").ConversationSnapshot | undefined {
    return this.core.testExtensionSnapshot(conversationId);
  }

  testExtensionRefreshCapabilities(conversationId: string): Promise<import("./webviewProtocol").ConversationSnapshot> {
    return this.core.testExtensionRefreshCapabilities(conversationId);
  }

  testExtensionObservation(conversationId: string):
    | {
      profile: import("./sessionClient").ConversationProfile;
      profileAttested: boolean;
      stream: import("./streamReducer").StreamAcceptanceState;
      approvalEvents: import("./conversationController").TestExtensionApprovalEvent[];
      turnEvents: import("./conversationController").TestExtensionTurnEvent[];
    }
    | undefined {
    return this.core.testExtensionObservation(conversationId);
  }

  testExtensionInterrupt(conversationId: string): Promise<void> {
    return this.core.testExtensionInterrupt(conversationId);
  }

  testExtensionPrompt(conversationId: string, prompt: string, requestToken?: string): Promise<void> {
    return this.core.testExtensionPrompt(conversationId, prompt, requestToken);
  }

  testExtensionPendingDiff(conversationId: string): import("./conversationController").PendingDiffApproval | undefined {
    return this.core.testExtensionPendingDiff(conversationId);
  }

  testExtensionClose(conversationId: string): Promise<boolean> {
    return this.core.testExtensionClose(conversationId);
  }

  refreshSessions(): Promise<unknown> {
    return this.core.refreshSessions();
  }

  deserializeWebviewPanel(webviewPanel: vscode.WebviewPanel, state: unknown): Promise<void> {
    return this.core.revive(new VsCodePanel(webviewPanel), state).then(() => undefined);
  }

  dispose(): void {
    this.core.dispose();
  }
}

class VsCodePanel implements EditorPanelPort {
  constructor(readonly panel: vscode.WebviewPanel) {}

  reveal(): void {
    this.panel.reveal(this.panel.viewColumn ?? vscode.ViewColumn.Active, true);
  }

  dispose(): void {
    this.panel.dispose();
  }

  setTitle(title: string): void {
    this.panel.title = title;
  }

  onDidDispose(listener: () => void): DisposablePort {
    return this.panel.onDidDispose(listener);
  }

  onDidActivate(listener: () => void): DisposablePort {
    return this.panel.onDidChangeViewState((event) => {
      if (event.webviewPanel.active) listener();
    });
  }
}

function asVsCodePanel(panel: EditorPanelPort): vscode.WebviewPanel {
  if (!(panel instanceof VsCodePanel)) throw new Error("Chat renderer requires a VS Code panel adapter");
  return panel.panel;
}
