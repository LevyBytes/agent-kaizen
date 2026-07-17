/** Native, immutable VS Code diff documents backed by verified daemon snapshots. */

import * as vscode from "vscode";

import { DiffArtifactReader, DiffFileChange, DiffSideRef, NegotiatedDiff } from "./diffModel";

export interface DiffApprovalIdentity {
  agentRunId: string;
  correlationId: string;
  diff: NegotiatedDiff;
  /** Rechecked when VS Code asks for either side; stale documents never read cache bytes. */
  isCurrent(): boolean;
}

interface RegisteredDocument {
  content: string;
  isCurrent(): boolean;
}

/** Provides digest-verified, approval-current virtual documents for VS Code diffs. */
export class KaizenDiffProvider implements vscode.TextDocumentContentProvider, vscode.Disposable {
  static readonly scheme = "kaizen-diff";

  private readonly reader: DiffArtifactReader;
  private readonly changed = new vscode.EventEmitter<vscode.Uri>();
  private readonly documents = new Map<string, RegisteredDocument>();
  readonly onDidChange = this.changed.event;

  constructor(workspaceRoot: string) {
    this.reader = new DiffArtifactReader(workspaceRoot);
  }

  /** Open a current text diff; false means stale, denied, or user-cancelled. */
  async preview(approval: DiffApprovalIdentity): Promise<boolean> {
    if (!approval.isCurrent()) return false;
    const textChanges = approval.diff.fileChanges.filter((change) => change.previewMode === "text");
    if (!textChanges.length) {
      void vscode.window.showInformationMessage(metadataSummary(approval.diff));
      return true;
    }
    let change = textChanges[0];
    if (textChanges.length > 1) {
      const picked = await vscode.window.showQuickPick(textChanges.map((item) => ({
          label: item.path,
          description: item.kind === "rename" ? `from ${item.oldPath}` : item.kind,
          item,
        })), { title: "Preview proposed Kaizen change" });
      if (!picked) return true;
      change = picked.item;
    }
    if (!approval.isCurrent()) return false;

    // Read and verify both fixed digest artifacts before registering either virtual document.
    const [before, proposed] = await Promise.all([
      this.reader.text(change.before),
      this.reader.text(change.proposed),
    ]);
    if (!approval.isCurrent()) return false;
    const beforeUri = this.register(approval, change, "before", change.before, before);
    const proposedUri = this.register(approval, change, "proposed", change.proposed, proposed);
    await vscode.commands.executeCommand(
      "vscode.diff",
      beforeUri,
      proposedUri,
      `${change.oldPath ?? change.path} <-> ${change.path}${!change.before ? " [new file]" : !change.proposed ? " [deleted file]" : ""} (Kaizen revision ${approval.diff.revision})`,
      { preview: true },
    );
    if (approval.diff.metadataConfirmationRequired) {
      void vscode.window.showInformationMessage(metadataSummary(approval.diff));
    }
    return true;
  }

  provideTextDocumentContent(uri: vscode.Uri): string {
    const document = this.documents.get(uri.toString());
    if (!document) throw vscode.FileSystemError.FileNotFound(uri);
    if (!document.isCurrent()) throw vscode.FileSystemError.Unavailable(uri);
    return document.content;
  }

  /** Notify and evict documents whose approval is no longer current. */
  refreshLifecycle(): void {
    for (const [key, document] of this.documents) {
      if (document.isCurrent()) continue;
      this.documents.delete(key);
      this.changed.fire(vscode.Uri.parse(key));
    }
  }

  dispose(): void {
    this.documents.clear();
    this.changed.dispose();
  }

  /** Register one immutable side under its approval identity and digest. */
  private register(
    approval: DiffApprovalIdentity,
    change: DiffFileChange,
    sideName: "before" | "proposed",
    side: DiffSideRef | null,
    content: string,
  ): vscode.Uri {
    const digest = side?.sha256 ?? "empty";
    const prefix = identityPrefix(approval.agentRunId, approval.correlationId, approval.diff.revision);
    const uri = vscode.Uri.from({
      scheme: KaizenDiffProvider.scheme,
      path: `/${prefix}/${encodeURIComponent(change.changeId)}/${sideName}`,
      query: `snapshot=${approval.diff.snapshotSetSha256}&sha256=${digest}`,
    });
    this.documents.set(uri.toString(), { content, isCurrent: approval.isCurrent });
    return uri;
  }
}

/** Build the encoded run/correlation identity, optionally scoped to a revision. */
function identityPrefix(agentRunId: string, correlationId: string, revision: number): string {
  return [encodeURIComponent(agentRunId), encodeURIComponent(correlationId), revision].join("/");
}

/** Build the user-facing metadata-only confirmation summary. */
function metadataSummary(diff: NegotiatedDiff): string {
  const metadata = diff.fileChanges.filter((change) => change.previewMode === "metadata");
  return metadata.length
    ? `${metadata.length} change${metadata.length === 1 ? "" : "s"} require metadata-only confirmation before Accept.`
    : "No native text preview is available for this request.";
}
