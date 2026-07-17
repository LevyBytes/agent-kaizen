/** Chat-asset contract tests; run-tests.mjs pins cwd to extension/ before resolving production assets. */
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";
import * as vm from "node:vm";

const root = path.resolve(process.cwd());
const repoRoot = path.resolve(root, "..");
const read = (...parts: string[]) => fs.readFileSync(path.join(root, ...parts), "utf-8");

test("chat remains editor-panel-only and all three sidebar trees are unchanged", () => {
  const pkg = JSON.parse(read("package.json")) as {
    contributes: { views: Record<string, Array<{ id: string }>>; commands: Array<{ command: string }> };
  };
  assert.deepEqual(pkg.contributes.views.kaizen.map((entry) => entry.id), [
    "kaizenApprovals",
    "kaizenSessions",
    "kaizenFleet",
  ]);
  assert.ok(pkg.contributes.commands.some((entry) => entry.command === "kaizen.openChat"));
  assert.ok(!pkg.contributes.commands.some((entry) => entry.command === "kaizen.popOutChat"));
  const extension = read("src", "extension.ts");
  assert.match(extension, /registerCommand\("kaizen\.openChat", \(\) => panels\.openOrFocus\(\)/);
  assert.doesNotMatch(extension, /registerWebviewViewProvider/);
  assert.doesNotMatch(read("src", "chatPanel.ts"), /static current|Reveal-if-open \(singleton\)/);
});

test("native welcome/chat body is accessible, scriptless, styleless, and has no side card pane", () => {
  const html = read("media", "webview", "chat.html");
  for (const id of [
    "welcome", "chatScreen", "transcript", "engine", "model", "permission", "effort", "auth",
    "maxTurns", "maxTurnsField", "runtimeStatus",
    "overflow", "queueChips", "lossWarning", "prompt", "send", "turnStop", "startDaemon",
    "historyScreen", "historyList", "historyDriven", "historyObserved", "contextChips", "contextActions",
    "addFile", "addSelection", "mentionResults", "continuationBanner",
  ]) {
    assert.match(html, new RegExp(`id=["']${id}["']`), `missing #${id}`);
  }
  assert.doesNotMatch(html, /<script\b|<style\b/i);
  assert.doesNotMatch(html, /\sstyle=|\son[a-z]+=/i);
  assert.doesNotMatch(html, /id=["'](?:cards|approvals|side)["']/i);
  assert.match(html, /id="transcript"[^>]*role="log"[^>]*tabindex="0"/i);
  assert.match(html, /<label[^>]*for="prompt"/i);
  assert.match(html, /Start Daemon in Visible Terminal/);
  assert.match(html, /<h1 id="welcomeTitle">Kaizen<\/h1>/);
  assert.match(html, /<h2 id="historyTitle">Conversation history<\/h2>/);
  assert.equal((html.match(/<h1\b/gi) ?? []).length, 1, "the static template has one document-level heading");
});

test("composer uses compact engine/model/permission pills and the exact overflow split", () => {
  const html = read("media", "webview", "chat.html");
  const pills = /id="profile"[\s\S]*?id="engine"[\s\S]*?id="model"[\s\S]*?id="permission"[\s\S]*?<\/div>/.exec(html)?.[0] ?? "";
  assert.ok(pills);
  assert.doesNotMatch(pills, /id="(?:effort|auth)"/);
  const menu = /class="overflow-menu"[\s\S]*?<\/div>/.exec(html)?.[0] ?? "";
  for (const id of ["effort", "auth", "refreshCapabilities", "stopDaemon", "kill"]) {
    assert.match(menu, new RegExp(`id="${id}"`));
  }
  assert.equal((html.match(/id="turnStop"/g) ?? []).length, 1, "exactly one running-turn Stop action");
  assert.doesNotMatch(html, /id="(?:interrupt|close)"/);
});

test("Claude workload and authentication UI is dynamic, bounded, immutable, and credential-free", () => {
  const html = read("media", "webview", "chat.html");
  const main = read("src", "webview", "main.ts");
  const controller = read("src", "conversationController.ts");
  assert.match(html, /id="maxTurns"[^>]*type="number"/);
  assert.match(main, /const bounds = capability\?\.id === "claude" \? capability\.maxTurns : undefined/);
  assert.match(main, /maxTurns\.min = String\(bounds\.min\)/);
  assert.match(main, /maxTurns\.max = String\(bounds\.max\)/);
  assert.match(main, /auth\.disabled = locked \|\| capability\?\.id === "claude"/);
  assert.match(controller, /capability\.models\.find\(\(model\) => model\.id === profile\.model\)/);
  assert.match(controller, /advertisedEfforts\.length > 0/);
  assert.match(main, /effortUsable/);
  assert.match(controller, /profile\.auth_mode !== "subscription"/);
  assert.match(controller, /this\.active !== null \|\| this\.pendingPrompt/);
  assert.doesNotMatch(html, /password|token|credential path|api key/i);
});

test("Claude model UI preserves exact stale ids and never substitutes the first catalog item", () => {
  const main = read("src", "webview", "main.ts");
  const controller = read("src", "conversationController.ts");
  assert.match(main, /models\.find\(\(entry\) => entry\.id === model\.value\.trim\(\)\)/);
  assert.match(main, /model\.value = \(next\.selectedProfile\.model \?\? ""\)\.trim\(\)/);
  assert.doesNotMatch(controller, /capability\.models\[0\]/);
  assert.match(controller, /DENIED_MODEL_UNAVAILABLE/);
});

test("runtime status and process cards expose only sanitized readiness and the application boundary", () => {
  const main = read("src", "webview", "main.ts");
  const css = read("media", "webview", "chat.css");
  assert.match(main, /Anthropic-owned Claude application outside Kaizen/);
  assert.match(main, /Claude subscription runtime ready/);
  assert.match(main, /Application-level permission mediation only; no OS containment/);
  for (const label of ["Executable", "Arguments", "Working directory", "Timeout", "Decision", "Exit", "Truncated"]) {
    assert.match(main, new RegExp(`\\["${label}"`));
  }
  assert.match(css, /\.process-grid/);
  assert.match(css, /\.process-boundary/);
  assert.match(main, /typeof card\.process\.truncated === "boolean"/);
  assert.doesNotMatch(main, /card\.process\.truncated === true \? "yes" : "no"/);
  assert.doesNotMatch(main, /credential[_ ]path|account identity|raw[_ ]error/i);
});

test("process-card DOM reports only attested truncation truth", () => {
  /** Minimal DOM element surface used by vm-sandboxed renderToolCard assertions. */
  class FakeElement {
    readonly children: FakeElement[] = [];
    readonly dataset: Record<string, string> = {};
    className = "";
    textContent = "";
    value = "";
    hidden = false;
    disabled = false;
    open = false;
    scrollHeight = 0;
    scrollTop = 0;
    clientHeight = 0;

    append(...nodes: FakeElement[]): void { this.children.push(...nodes); }
    replaceChildren(...nodes: FakeElement[]): void { this.children.splice(0, this.children.length, ...nodes); }
    addEventListener(): void {}
    setAttribute(): void {}
    removeAttribute(): void {}
    remove(): void {}
    focus(): void {}
    querySelector(): null { return null; }
    closest(): null { return null; }
  }

  const elements = new Map<string, FakeElement>();
  const document = {
    activeElement: null,
    createElement: () => new FakeElement(),
    getElementById: (id: string) => {
      let element = elements.get(id);
      if (!element) {
        element = new FakeElement();
        elements.set(id, element);
      }
      return element;
    },
  };
  const compiledMain = read("out", "webview", "main.js");
  const safeMarkdownImport = 'import { renderSafeMarkdown } from "./safeMarkdown.js";';
  assert.equal(compiledMain.split(safeMarkdownImport).length - 1, 1, "sandbox shim must replace exactly one import");
  const source = compiledMain
    .replace(safeMarkdownImport, "const renderSafeMarkdown = (text) => text;")
    .concat("\nglobalThis.renderToolCardForTest = renderToolCard;\n");
  const sandbox: Record<string, unknown> = {
    acquireVsCodeApi: () => ({ postMessage: () => undefined, setState: () => undefined }),
    clearTimeout,
    document,
    queueMicrotask,
    setTimeout,
    window: { addEventListener: () => undefined, clearTimeout, queueMicrotask, setTimeout },
    navigator: { clipboard: { writeText: () => Promise.resolve() } },
  };
  vm.runInNewContext(source, sandbox, { filename: "out/webview/main.js" });
  const renderToolCard = sandbox.renderToolCardForTest as (node: FakeElement, card: unknown) => void;

  const truncationLabel = (status: string, truncated?: boolean): string => {
    const node = new FakeElement();
    renderToolCard(node, {
      tool: "kaizen_run_process",
      status,
      summary: "process",
      process: {
        executable: "tool.exe",
        argv: [],
        cwd: ".",
        timeoutMs: 1000,
        decision: "pending",
        effectsUnknown: true,
        ...(truncated === undefined ? {} : { truncated }),
      },
    });
    const grid = node.children.find((child) => child.className === "process-grid");
    assert.ok(grid, "process grid was not rendered");
    const labelIndex = grid.children.findIndex((child) => child.textContent === "Truncated");
    assert.ok(labelIndex >= 0, "Truncated row was not rendered");
    return grid.children.find((child, index) => index > labelIndex && child.className === "process-value")?.textContent ?? "";
  };

  assert.equal(truncationLabel("running"), "pending");
  assert.equal(truncationLabel("error"), "unknown");
  assert.equal(truncationLabel("ok", true), "yes");
  assert.equal(truncationLabel("ok", false), "no");
});

test("free-text approval DOM posts exact textarea answers only after local validity succeeds", () => {
  class FakeElement {
    readonly children: FakeElement[] = [];
    readonly dataset: Record<string, string> = {};
    readonly listeners = new Map<string, () => void>();
    className = "";
    textContent = "";
    value = "";
    hidden = false;
    disabled = false;
    open = false;
    scrollHeight = 0;
    scrollTop = 0;
    clientHeight = 0;
    validityMessage = "";
    validityReports = 0;

    append(...nodes: FakeElement[]): void { this.children.push(...nodes); }
    replaceChildren(...nodes: FakeElement[]): void { this.children.splice(0, this.children.length, ...nodes); }
    addEventListener(type: string, listener: () => void): void { this.listeners.set(type, listener); }
    click(): void { this.listeners.get("click")?.(); }
    setCustomValidity(message: string): void { this.validityMessage = message; }
    reportValidity(): boolean { this.validityReports += 1; return !this.validityMessage; }
    setAttribute(): void {}
    removeAttribute(): void {}
    remove(): void {}
    focus(): void {}
    querySelector(): null { return null; }
    closest(): null { return null; }
  }

  const elements = new Map<string, FakeElement>();
  const document = {
    activeElement: null,
    createElement: () => new FakeElement(),
    getElementById: (id: string) => {
      let element = elements.get(id);
      if (!element) {
        element = new FakeElement();
        elements.set(id, element);
      }
      return element;
    },
  };
  const posted: unknown[] = [];
  const compiledMain = read("out", "webview", "main.js");
  const safeMarkdownImport = 'import { renderSafeMarkdown } from "./safeMarkdown.js";';
  assert.equal(compiledMain.split(safeMarkdownImport).length - 1, 1, "sandbox shim must replace exactly one import");
  const source = compiledMain
    .replace(safeMarkdownImport, "const renderSafeMarkdown = (text) => text;")
    .concat("\nglobalThis.renderApprovalCardForTest = renderApprovalCard;\n");
  const sandbox: Record<string, unknown> = {
    acquireVsCodeApi: () => ({ postMessage: (message: unknown) => posted.push(message), setState: () => undefined }),
    clearTimeout,
    document,
    queueMicrotask,
    setTimeout,
    window: { addEventListener: () => undefined, clearTimeout, queueMicrotask, setTimeout },
    navigator: { clipboard: { writeText: () => Promise.resolve() } },
  };
  vm.runInNewContext(source, sandbox, { filename: "out/webview/main.js" });
  posted.length = 0;
  const renderApprovalCard = sandbox.renderApprovalCardForTest as (node: FakeElement, approval: unknown) => void;
  const descendants = (node: FakeElement): FakeElement[] => [node, ...node.children.flatMap(descendants)];
  const approval = {
    timelineKey: "run:1",
    agentRunId: "run",
    sequenceNo: 1,
    correlationId: "input-1",
    summary: "Answer required",
    status: "pending",
    displayOnly: false,
    pending: false,
    questions: [{ id: "reason", header: "Decision", question: "Why continue?", options: [] }],
  };
  const node = new FakeElement();
  renderApprovalCard(node, approval);
  const answer = descendants(node).find((element) => element.className === "approval-answer");
  const approve = descendants(node).find((element) => element.className === "approve-button");
  assert.ok(answer);
  assert.ok(approve);

  answer.value = "   ";
  approve.click();
  assert.equal(posted.length, 0);
  assert.equal(answer.validityReports, 1);
  assert.match(answer.validityMessage, /explicit answer/i);

  answer.value = "  exact answer  ";
  approve.click();
  assert.deepEqual(JSON.parse(JSON.stringify(posted)), [{
    type: "approve",
    correlationId: "input-1",
    agentRunId: "run",
    answers: { reason: "  exact answer  " },
  }]);
});

test("timeline renderer keys by agent_run_id:sequence_no and mutates existing inline nodes", () => {
  const reducer = read("src", "eventReducer.ts");
  const main = read("src", "webview", "main.ts");
  assert.match(reducer, /return `\$\{agentRunId\}:\$\{sequenceNo\}`/);
  assert.match(reducer, /scopedCorrelation\(agentRunId, correlationId\)/);
  assert.match(main, /const timelineNodes = new Map<string, HTMLElement>/);
  assert.match(main, /timelineNodes\.get\(item\.key\)/);
  assert.match(main, /transcript\.replaceChildren\(\.\.\.orderedNodes\)/);
  assert.match(main, /node\.dataset\.timelineKey = item\.key/);
  assert.match(main, /renderToolCard\(node, item\.card\)/);
  assert.match(main, /renderApprovalCard\(node, item\.approval\)/);
});

test("users take the textContent path; only assistant/system enter the safe Markdown renderer", () => {
  const main = read("src", "webview", "main.ts");
  assert.match(main, /if \(role === "user"\) \{[^}]*body\.textContent = text;/);
  assert.match(main, /\} else \{[^}]*body\.innerHTML = renderSafeMarkdown\(text\);/);
  assert.match(main, /import \{ renderSafeMarkdown \} from "\.\/safeMarkdown\.js"/);
  assert.doesNotMatch(main, /body\.innerHTML\s*=\s*text/);
});

test("queue/draft loss is persistent and destructive actions carry draft state", () => {
  const main = read("src", "webview", "main.ts");
  const controller = read("src", "conversationController.ts");
  const extension = read("src", "extension.ts");
  assert.doesNotMatch(main, /window\.addEventListener\("beforeunload"|hasEphemeralState\(\)/);
  assert.match(main, /type: "newConversation", hasDraft: prompt\.value\.length > 0/);
  assert.match(main, /temporary and will be lost if this tab reloads or closes/);
  assert.match(controller, /confirmEphemeralLoss\(hasDraft\)/);
  assert.match(extension, /confirmEphemeralLoss: async/);
  assert.match(extension, /\{ modal: true \}/);
});

test("running input queues follow-ups while a single Stop interrupts and Kill stays in overflow", () => {
  const main = read("src", "webview", "main.ts");
  assert.match(main, /const queueMode = running \|\| next\.pendingPrompt/);
  assert.match(main, /send\.textContent = queueMode \? "Queue" : "Send"/);
  assert.match(main, /turnStop\.addEventListener\("click", \(\) => post\(\{ type: "interrupt" \}\)\)/);
  assert.doesNotMatch(main, /type: "steer"/);
  assert.match(main, /kill\.addEventListener\("click"/);
});

test("daemon controls are explicit, visible-terminal only, and reachable in all existing surfaces", () => {
  const pkg = JSON.parse(read("package.json")) as {
    contributes: { commands: Array<{ command: string }>; menus: Record<string, Array<{ command: string; when?: string }>> };
  };
  assert.ok(pkg.contributes.commands.some((entry) => entry.command === "kaizen.startDaemon"));
  assert.ok(pkg.contributes.commands.some((entry) => entry.command === "kaizen.stopDaemon"));
  assert.ok(pkg.contributes.menus["view/item/context"].some((entry) => entry.command === "kaizen.startDaemon" && /daemon-down/.test(entry.when ?? "")));
  assert.ok(pkg.contributes.menus["view/item/context"].some((entry) => entry.command === "kaizen.stopDaemon" && /daemon-up/.test(entry.when ?? "")));
  const extension = read("src", "extension.ts");
  assert.match(extension, /startDaemonTerminal\(/);
  assert.match(extension, /registerCommand\("kaizen\.stopDaemon"[\s\S]*?showWarningMessage[\s\S]*?control\("shutdown", \{\}\)/);
  const renderer = read("src", "chatRenderer.ts");
  assert.match(renderer, /executeCommand\("kaizen\.startDaemon"\)/);
  assert.match(renderer, /executeCommand\("kaizen\.stopDaemon"\)/);
});

test("CSP and assets use local roots, nonce ESM, explicit .js import, and no data image source", () => {
  const html = read("src", "chatHtml.ts");
  const renderer = read("src", "chatRenderer.ts");
  const main = read("src", "webview", "main.ts");
  assert.match(html, /default-src 'none'/);
  assert.match(html, /script-src 'nonce-\$\{nonce\}' \$\{webview\.cspSource\}/);
  assert.match(html, /<script type="module" nonce="\$\{nonce\}" src="\$\{scriptUri\}">/);
  assert.match(html, /media", "kaizen\.svg"/);
  assert.doesNotMatch(html, /img-src[^`\n]*data:/);
  assert.match(renderer, /joinPath\(extensionUri, "media"\)/);
  assert.match(renderer, /joinPath\(extensionUri, "out", "webview"\)/);
  assert.match(main, /from "\.\/safeMarkdown\.js"/);
});

test("layout uses VS Code theme tokens and a full-height transcript", () => {
  const css = read("media", "webview", "chat.css");
  assert.match(css, /height:\s*100vh/);
  assert.match(css, /\.content[\s\S]*?flex:\s*1/);
  assert.match(css, /\.transcript[\s\S]*?overflow-y:\s*auto/);
  assert.match(css, /var\(--vscode-/);
  assert.doesNotMatch(css, /:\s*#(?:[0-9a-f]{3}|[0-9a-f]{4}|[0-9a-f]{6}|[0-9a-f]{8})\b|\brgba?\(/i);
  assert.doesNotMatch(css, /body\[data-surface="sidebar"\]/);
});

test("test command compiles both TS targets and enumerates U1 through U4 suites", () => {
  const pkg = JSON.parse(read("package.json")) as { scripts: { test: string } };
  const runner = fs.readFileSync(path.join(repoRoot, "tests", "extension", "run-tests.mjs"), "utf-8");
  assert.equal(pkg.scripts.test, "node ../tests/extension/run-tests.mjs");
  assert.match(runner, /path\.join\(EXTENSION_ROOT, "tsconfig\.json"\)/);
  assert.match(runner, /path\.join\(EXTENSION_ROOT, "src", "webview"\)/);
  assert.match(runner, /path\.join\(TEST_ROOT, "tsconfig\.json"\)/);
  assert.match(runner, /"--outDir", compiledRoot/);
  assert.match(runner, /followUpQueue\.test\.js/);
  assert.match(runner, /safeMarkdown\.test\.js/);
  assert.match(runner, /sessionIndex\.test\.js/);
  assert.match(runner, /panelState\.test\.js/);
  assert.match(runner, /chatPanelManager\.test\.js/);
  assert.match(runner, /historyModel\.test\.js/);
  assert.match(runner, /mentionIndex\.test\.js/);
  assert.match(runner, /contextStager\.test\.js/);
  assert.match(runner, /imageStager\.test\.js/);
  assert.match(runner, /streamReducer\.test\.js/);
  assert.match(runner, /diffModel\.test\.js/);
  assert.match(runner, /tempRoot\.test\.js/);
});

test("extension polling continues to refresh the unchanged sidebar and observed data", () => {
  const extension = read("src", "extension.ts");
  assert.match(extension, /async function refresh\(\)[\s\S]*await panels\.refreshSessions\(\)/);
  assert.match(extension, /let refreshInFlight: Promise<void> \| undefined/);
  assert.match(extension, /if \(refreshInFlight\) return refreshInFlight/);
  assert.match(extension, /finally \{\s*refreshInFlight = undefined/);
  assert.match(extension, /providers\.forEach\(\(p\) => p\.refresh\(\)\)/);
});

test("diff documents encode complete identity and evict stale registered keys", () => {
  const provider = read("src", "diffProvider.ts");
  assert.match(provider, /const prefix = identityPrefix\(approval\.agentRunId, approval\.correlationId, approval\.diff\.revision\)/);
  assert.match(provider, /path: `\/\$\{prefix\}\/\$\{encodeURIComponent\(change\.changeId\)\}\/\$\{sideName\}`/);
  assert.match(provider, /return \[encodeURIComponent\(agentRunId\), encodeURIComponent\(correlationId\), revision\]\.join\("\/"\)/);
  assert.match(provider, /if \(document\.isCurrent\(\)\) continue;[\s\S]*this\.documents\.delete\(key\);[\s\S]*this\.changed\.fire\(vscode\.Uri\.parse\(key\)\)/);
});

test("serializer persists only conversation identity and revives through the panel manager", () => {
  const extension = read("src", "extension.ts");
  const panel = read("src", "chatPanel.ts");
  const main = read("src", "webview", "main.ts");
  assert.match(extension, /registerWebviewPanelSerializer\(ChatPanelManager\.viewType, panels\)/);
  assert.match(panel, /deserializeWebviewPanel[\s\S]*core\.revive/);
  assert.match(main, /vscode\.setState\(\{ conversationId: message\.snapshot\.conversationId \}\)/);
  assert.equal((main.match(/\.setState\(/g) ?? []).length, 1);
  assert.doesNotMatch(main, /setState\([^)]*(?:prompt|draft|queue|attachment|context)/i);
});

test("locked commands, exact keybindings, and only locked chat settings are contributed", () => {
  const pkg = JSON.parse(read("package.json")) as {
    contributes: {
      commands: Array<{ command: string }>;
      keybindings: Array<{ command: string; key: string }>;
      configuration: { properties: Record<string, unknown> };
      menus: Record<string, Array<{ command: string }>>;
    };
  };
  const commands = new Set(pkg.contributes.commands.map((entry) => entry.command));
  for (const command of [
    "kaizen.newConversation", "kaizen.history", "kaizen.focusInput", "kaizen.reopenClosed",
    "kaizen.addFile", "kaizen.addSelection", "kaizen.acceptDiff", "kaizen.rejectDiff", "kaizen.showLogs", "kaizen.testExtension.open",
  ]) assert.ok(commands.has(command), `missing ${command}`);
  assert.deepEqual(pkg.contributes.keybindings, [
    { command: "kaizen.openChat", key: "ctrl+alt+j" },
    { command: "kaizen.newConversation", key: "ctrl+alt+n" },
    { command: "kaizen.addSelection", key: "ctrl+alt+a" },
  ]);
  assert.deepEqual(
    Object.keys(pkg.contributes.configuration.properties).filter((key) => key.startsWith("kaizen.chat.")).sort(),
    [
      "kaizen.chat.autosaveBeforeTurn",
      "kaizen.chat.enterBehavior",
      "kaizen.chat.followUpQueueMode",
      "kaizen.chat.renderMarkdown",
    ],
  );
  const menuCommands = Object.values(pkg.contributes.menus).flat().map((entry) => entry.command);
  assert.ok(!menuCommands.includes("kaizen.acceptDiff") && !menuCommands.includes("kaizen.rejectDiff"), "dark diff placeholders have no visible menu");
});

test("new conversation command and webview action both route to a fresh manager tab", () => {
  const extension = read("src", "extension.ts");
  const renderer = read("src", "chatRenderer.ts");
  const manager = read("src", "chatPanelManager.ts");
  assert.match(extension, /registerCommand\("kaizen\.newConversation", \(\) => panels\.newConversation\(\)/);
  assert.match(renderer, /message\.type === "newConversation"[\s\S]*?actions\.newConversation\(\)/);
  assert.match(manager, /async newConversation\(\)[\s\S]*?state\.create\(\)/);
  assert.match(read("src", "webview", "main.ts"), /newConversation\.disabled = false/);
  assert.doesNotMatch(renderer, /controller\.handle\(message[^)]*newConversation/);
});

test("tail-session compatibility command uses capability-driven editor chat", () => {
  const pkg = JSON.parse(read("package.json")) as {
    contributes: {
      commands: Array<{ command: string; title: string }>;
      viewsWelcome: Array<{ view: string; contents: string }>;
    };
  };
  const command = pkg.contributes.commands.find((entry) => entry.command === "kaizen.tailSession");
  assert.equal(command?.title, "Kaizen: New Conversation (Compatibility)");
  const extension = read("src", "extension.ts");
  assert.match(extension, /registerCommand\("kaizen\.tailSession", \(\) => panels\.newConversation\(\)\)/);
  assert.doesNotMatch(extension, /only 'local_llm' is wired|codex\/claude are denied|Prompt for the .* run/);
  const welcome = pkg.contributes.viewsWelcome.find((entry) => entry.view === "kaizenSessions")?.contents ?? "";
  assert.match(welcome, /Start a governed agent turn/);
  assert.doesNotMatch(welcome, /local[-_ ]LLM/i);
});

test("history is a real newest-first driven/observed screen and selection uses daemon ids", () => {
  const main = read("src", "webview", "main.ts");
  const model = read("src", "historyModel.ts");
  const manager = read("src", "chatPanelManager.ts");
  const extension = read("src", "extension.ts");
  assert.match(main, /historyController: "driven" \| "observed"/);
  assert.match(main, /type: "historySelect"[\s\S]*sessionId: entry\.sessionId[\s\S]*agentRunId: entry\.agentRunId/);
  assert.match(model, /sort\(\(left, right\) => compareNewest/);
  assert.match(model, /clean\(value\.title\) \?\? titleFallback\(snippet\)/);
  assert.match(manager, /!active\.hasDraft && active\.controller\.isGenuinelyEmpty\(\)/);
  assert.match(manager, /this\.entries\.get\(await this\.newConversation\(\)\)/);
  assert.match(extension, /registerCommand\("kaizen\.history", \(\) => panels\.showHistory\(\)\)/);
});

test("governed context is host-staged, capability-dark, metadata-only, and never persists prompt bytes", () => {
  const panel = read("src", "chatPanel.ts");
  const stager = read("src", "contextStager.ts");
  const artifactStore = read("src", "artifactStore.ts");
  const mentions = read("src", "mentionIndex.ts");
  const controller = read("src", "conversationController.ts");
  const main = read("src", "webview", "main.ts");
  assert.match(panel, /editor\.document\.getText\(selection\)/);
  assert.match(stager, /Buffer\.from\(exactText, "utf8"\)/);
  assert.match(stager, /snapshot_ref: `sha256:\$\{digest\}`/);
  assert.match(stager, /new SecureArtifactStore\(workspaceRoot, "context"\)/);
  assert.match(artifactStore, /\.part`/);
  assert.match(mentions, /spawn\("git", \["ls-files", "-co", "--exclude-standard", "-z"\]/);
  assert.match(mentions, /shell: false/);
  assert.match(controller, /this\.client\.turn\(text, composerEnvelope\(submittedContext, submittedImages\)\)/);
  assert.match(controller, /context_refs: context\.map/);
  assert.match(main, /features\.governed_context === true/);
  assert.match(main, /contextActions\.hidden = \(!governedContext && !imageAttachments\)/);
  assert.match(main, /addFile\.hidden = !governedContext/);
  assert.equal((main.match(/\.setState\(/g) ?? []).length, 1, "context and prompt remain out of webview persistence");
});

test("reduced fidelity and omission/expiry metadata are visible without replaying historical bytes", () => {
  const main = read("src", "webview", "main.ts");
  const controller = read("src", "conversationController.ts");
  assert.match(controller, /response\.transcript_seeded === true/);
  assert.match(controller, /event\.body_omitted !== true/);
  assert.match(main, /next\.conversation\?\.continuationNotice/);
  assert.match(main, /bytes expired/);
  assert.doesNotMatch(main, /readFile|snapshot_ref/);
});
