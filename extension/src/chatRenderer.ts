/** Thin webview endpoint; conversation ownership stays in ConversationController. */

import * as vscode from "vscode";
import { chatHtml } from "./chatHtml";
import { ConversationController, RendererPort } from "./conversationController";
import type { RendererActions } from "./chatPanelManager";
import type { HostToWebview, WebviewToHost } from "./webviewProtocol";

export class ChatRenderer implements RendererPort, vscode.Disposable {
  private readonly disposables: vscode.Disposable[] = [];
  private readonly pasteUploads = new Set<string>();

  constructor(
    public readonly id: string,
    private readonly conversationId: string,
    private readonly webview: vscode.Webview,
    extensionUri: vscode.Uri,
    private readonly controller: ConversationController,
    private readonly actions: RendererActions,
    private readonly imageCacheRoot: vscode.Uri,
  ) {
    this.webview.options = {
      enableScripts: true,
      localResourceRoots: [
        vscode.Uri.joinPath(extensionUri, "media"),
        vscode.Uri.joinPath(extensionUri, "out", "webview"),
        imageCacheRoot,
      ],
    };
    this.webview.html = chatHtml(this.webview, extensionUri);
    this.disposables.push(
      this.webview.onDidReceiveMessage((message: WebviewToHost) => {
        // start/stopDaemon are HOST/vscode concerns (visible terminal; confirm dialog + loopback
        // shutdown); the controller stays vscode-free, so the renderer routes them to the commands.
        if (message.type === "startDaemon") {
          void vscode.commands.executeCommand("kaizen.startDaemon");
          return;
        }
        if (message.type === "stopDaemon") {
          void vscode.commands.executeCommand("kaizen.stopDaemon");
          return;
        }
        if (message.type === "newConversation") {
          this.actions.newConversation();
          return;
        }
        if (message.type === "ephemeralState") {
          this.actions.draftChanged(message.hasDraft);
          return;
        }
        if (message.type === "showHistory") {
          this.actions.showHistory();
          return;
        }
        if (message.type === "historySelect") {
          this.actions.historySelect(message.sessionId, message.agentRunId);
          return;
        }
        if (message.type === "mentionQuery") {
          this.actions.mentionQuery(message.query);
          return;
        }
        if (message.type === "mentionSelect") {
          this.actions.mentionSelect(message.path);
          return;
        }
        if (message.type === "addFile") {
          this.actions.addFile();
          return;
        }
        if (message.type === "addSelection") {
          this.actions.addSelection();
          return;
        }
        if (message.type === "contextRemove") {
          this.actions.contextRemove(message.id);
          return;
        }
        if (message.type === "addImage") {
          this.actions.addImage();
          return;
        }
        if (message.type === "imageRemove") {
          this.actions.imageRemove(message.id);
          return;
        }
        if (message.type === "imagePasteStart") {
          this.pasteUploads.add(message.uploadId);
          this.actions.imagePasteStart(message);
          return;
        }
        if (message.type === "imagePasteChunk") {
          this.actions.imagePasteChunk(message.uploadId, message.index, base64Bytes(message.data));
          return;
        }
        if (message.type === "imagePasteEnd") {
          this.pasteUploads.delete(message.uploadId);
          this.actions.imagePasteEnd(message.uploadId);
          return;
        }
        if (message.type === "imagePasteCancel") {
          this.pasteUploads.delete(message.uploadId);
          this.actions.imagePasteCancel(message.uploadId);
          return;
        }
        if (message.type === "previewDiff") {
          this.actions.previewDiff(message.agentRunId, message.correlationId);
          return;
        }
        if (message.type === "acceptDiff") {
          this.actions.acceptDiff(message.agentRunId, message.correlationId);
          return;
        }
        if (message.type === "rejectDiff") {
          this.actions.rejectDiff(message.agentRunId, message.correlationId);
          return;
        }
        void this.controller.handle(message, this.id);
      }),
    );
    this.controller.attach(this);
  }

  /** Stamp panel identity and derive image preview URIs after validating each digest as a path atom. */
  postMessage(message: HostToWebview): PromiseLike<boolean> {
    if (message.type === "snapshot") {
      this.actions.diffSnapshotChanged();
      return this.webview.postMessage({
        ...message,
        snapshot: {
          ...message.snapshot,
          conversationId: this.conversationId,
          imageChips: message.snapshot.imageChips.map((chip) => ({
            ...chip,
            // Fixed-width lowercase hex prevents traversal before the digest joins the cache root.
            ...(/^[0-9a-f]{64}$/.test(chip.sha256) ? {
              previewUri: this.webview.asWebviewUri(vscode.Uri.joinPath(this.imageCacheRoot, chip.sha256)).toString(),
            } : {}),
          })),
        },
      } satisfies HostToWebview);
    }
    return this.webview.postMessage(message);
  }

  focusInput(): void {
    void this.webview.postMessage({ type: "focusInput" } satisfies HostToWebview);
  }

  dispose(): void {
    for (const uploadId of this.pasteUploads) this.actions.imagePasteCancel(uploadId);
    this.pasteUploads.clear();
    this.controller.detach(this.id);
    while (this.disposables.length) this.disposables.pop()?.dispose();
  }
}

function base64Bytes(value: string): number[] {
  if (typeof value !== "string" || value.length > 90_000 || !/^[A-Za-z0-9+/]*={0,2}$/.test(value)) return [];
  const bytes = Buffer.from(value, "base64");
  return bytes.toString("base64") === value ? [...bytes] : [];
}
