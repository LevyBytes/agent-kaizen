/** CSP-safe HTML generator for the editor chat panel. */

import * as crypto from "crypto";
import * as fs from "fs";
import * as vscode from "vscode";

/** Return a nonce-bearing CSP document wired to the packaged webview script, stylesheet, and body. */
export function chatHtml(
  webview: vscode.Webview,
  extensionUri: vscode.Uri,
): string {
  const nonce = crypto.randomBytes(16).toString("hex");
  const scriptUri = webview.asWebviewUri(vscode.Uri.joinPath(extensionUri, "out", "webview", "main.js"));
  const styleUri = webview.asWebviewUri(vscode.Uri.joinPath(extensionUri, "media", "webview", "chat.css"));
  const csp = [
    "default-src 'none'",
    `style-src ${webview.cspSource}`,
    `script-src 'nonce-${nonce}' ${webview.cspSource}`,
    `img-src ${webview.cspSource}`,
    `font-src ${webview.cspSource}`,
  ].join("; ");
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<meta http-equiv="Content-Security-Policy" content="${csp}" />
<link rel="stylesheet" href="${styleUri}" />
<title>Kaizen Chat</title>
</head>
<body>
${readBody(webview, extensionUri)}
<script type="module" nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
}

/** Extract the packaged body and safely substitute its logo URI; fail closed to an empty app shell. */
function readBody(webview: vscode.Webview, extensionUri: vscode.Uri): string {
  const path = vscode.Uri.joinPath(extensionUri, "media", "webview", "chat.html").fsPath;
  try {
    const text = fs.readFileSync(path, "utf-8");
    const body = /<body[^>]*>([\s\S]*)<\/body>/i.exec(text);
    if (!body) return '<div id="app"></div>';
    const logoUri = webview.asWebviewUri(vscode.Uri.joinPath(extensionUri, "media", "kaizen.svg")).toString();
    return body[1].replace(/\{\{KAIZEN_LOGO_URI\}\}/g, () => escapeAttribute(logoUri));
  } catch {
    return '<div id="app"></div>';
  }
}

/** Escape a value for either single- or double-quoted HTML attributes. */
function escapeAttribute(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/'/g, "&#39;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
