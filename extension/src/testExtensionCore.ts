/** Pure Test Extension core: manifest attestation, readiness, request builders, and bounded pools. */

import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";

import type { EngineCapability } from "./sessionClient";
import type { ConversationSnapshot, ToolCardView } from "./webviewProtocol";

export interface TestExtensionReadyGate {
  readonly promise: Promise<boolean>;
  settle(ready: boolean): void;
}

export function createTestExtensionReadyGate(): TestExtensionReadyGate {
  let resolve!: (ready: boolean) => void;
  let settled = false;
  const promise = new Promise<boolean>((done) => { resolve = done; });
  return {
    promise,
    settle: (ready) => {
      if (settled) return;
      settled = true;
      resolve(ready);
    },
  };
}

export function installWebviewHtml<T>(
  subscribe: (listener: (message: T) => unknown) => { dispose(): unknown },
  assignHtml: (html: string) => void,
  html: string,
  listener: (message: T) => unknown,
): { dispose(): unknown } {
  const disposable = subscribe(listener);
  assignHtml(html);
  return disposable;
}

export type TestExtensionProvider = "claude" | "ollama";

export interface TestExtensionScenario {
  id: string;
  provider: TestExtensionProvider;
  label: string;
  permission: "plan" | "ask";
  expectedDecision?: "approve" | "deny";
  kind: "text" | "image" | "context" | "diff" | "process" | "control" | "traversal" |
    "stale" | "corrupt" | "timeout" | "writer" | "interrupt" | "cleanup";
}

export const TEST_EXTENSION_SCENARIOS: readonly TestExtensionScenario[] = [
  { id: "claude-text-stream", provider: "claude", label: "Streaming + durable text", permission: "plan", kind: "text" },
  { id: "claude-image-codeword", provider: "claude", label: "Image codeword", permission: "plan", kind: "image" },
  { id: "claude-governed-context", provider: "claude", label: "Governed context", permission: "plan", kind: "context" },
  { id: "claude-diff-accept", provider: "claude", label: "Whole-request diff Accept", permission: "ask", expectedDecision: "approve", kind: "diff" },
  { id: "claude-diff-reject", provider: "claude", label: "Whole-request diff Reject", permission: "ask", expectedDecision: "deny", kind: "diff" },
  { id: "claude-process-approval", provider: "claude", label: "Harmless process approval", permission: "ask", expectedDecision: "approve", kind: "process" },
  { id: "claude-plan-controls", provider: "claude", label: "Plan/invariant process denial", permission: "plan", kind: "control" },
  { id: "claude-traversal-zero-record", provider: "claude", label: "Traversal denial", permission: "plan", kind: "traversal" },
  { id: "claude-diff-stale", provider: "claude", label: "Stale diff refresh", permission: "ask", expectedDecision: "approve", kind: "stale" },
  { id: "claude-diff-corrupt", provider: "claude", label: "Corrupt diff fails closed", permission: "ask", kind: "corrupt" },
  { id: "claude-diff-timeout", provider: "claude", label: "Diff approval timeout", permission: "ask", kind: "timeout" },
  { id: "claude-writer-conflict", provider: "claude", label: "Workspace writer conflict", permission: "ask", kind: "writer" },
  { id: "claude-interrupt-restart", provider: "claude", label: "Interrupt/restart recovery", permission: "plan", kind: "interrupt" },
  { id: "cleanup-leak-state", provider: "claude", label: "Cleanup and leak state", permission: "plan", kind: "cleanup" },
  { id: "ollama-text-stream", provider: "ollama", label: "Ollama streaming text", permission: "plan", kind: "text" },
  { id: "ollama-governed-context", provider: "ollama", label: "Ollama governed context", permission: "plan", kind: "context" },
  { id: "ollama-tool-policy", provider: "ollama", label: "Ollama tool-policy denial", permission: "plan", kind: "control" },
] as const;

export interface TestExtensionPromptManifest {
  run_id: string;
  workspace_path: string;
  codewords: { text: string; context: string; selection: string; image: string };
  fixtures: { context: string; selection: string; image: string; diff_target: string };
}

export interface TestExtensionPrivateManifest extends TestExtensionPromptManifest {
  readonly v: 1;
  readonly suite_nonce: string;
}

export interface TestExtensionManifestSnapshot {
  readonly sha256: string;
  readonly manifest: TestExtensionPrivateManifest;
}

export const TEST_EXTENSION_FIXED_FIXTURES = Object.freeze({
  context: "te-context.txt",
  selection: "te-selection.txt",
  image: "te-image.png",
  diff_target: "te-diff-target.txt",
}) satisfies Readonly<TestExtensionPromptManifest["fixtures"]>;

interface PhysicalPathStat {
  isDirectory(): boolean;
  isFile(): boolean;
  isSymbolicLink(): boolean;
}

export interface TestExtensionPhysicalPathOps {
  lstat(target: string): PhysicalPathStat;
  realpath(target: string): string;
}

const NODE_PHYSICAL_PATH_OPS: TestExtensionPhysicalPathOps = {
  lstat: (target) => fs.lstatSync(target),
  realpath: (target) => fs.realpathSync.native(target),
};

/** Compare resolved paths, normalizing extended Windows prefixes and case. */
function sameResolvedPath(left: string, right: string): boolean {
  const normalize = (value: string) => process.platform === "win32"
    ? path.resolve(stripExtendedPathPrefix(value)).toLowerCase()
    : path.resolve(value);
  return normalize(left) === normalize(right);
}

function stripExtendedPathPrefix(value: string): string {
  if (value.startsWith("\\\\?\\UNC\\")) return `\\\\${value.slice(8)}`;
  return value.startsWith("\\\\?\\") ? value.slice(4) : value;
}

/** Return true only for a strict lexical descendant of root. */
function isResolvedChild(root: string, candidate: string): boolean {
  const relative = path.relative(path.resolve(root), path.resolve(candidate));
  return !!relative && !relative.startsWith("..") && !path.isAbsolute(relative);
}

/** Prove that the isolated workspace is a plain physical child and the VS Code root is that directory. */
export function validatePhysicalManifestWorkspace(
  runRoot: string,
  repoRoot: string,
  operations: TestExtensionPhysicalPathOps = NODE_PHYSICAL_PATH_OPS,
): string {
  const lexicalRunRoot = path.resolve(runRoot);
  const lexicalWorkspace = path.resolve(lexicalRunRoot, "fixture-workspace");
  try {
    const runStat = operations.lstat(lexicalRunRoot);
    const workspaceStat = operations.lstat(lexicalWorkspace);
    const repoStat = operations.lstat(path.resolve(repoRoot));
    if (runStat.isSymbolicLink() || workspaceStat.isSymbolicLink() || repoStat.isSymbolicLink() ||
        !runStat.isDirectory() || !workspaceStat.isDirectory() || !repoStat.isDirectory()) {
      throw new Error("TEST_EXTENSION_MANIFEST_WORKSPACE_INVALID");
    }
    const physicalRunRoot = operations.realpath(lexicalRunRoot);
    const physicalWorkspace = operations.realpath(lexicalWorkspace);
    const physicalRepoRoot = operations.realpath(path.resolve(repoRoot));
    if (!sameResolvedPath(physicalRunRoot, lexicalRunRoot) ||
        !sameResolvedPath(physicalWorkspace, lexicalWorkspace) ||
        !sameResolvedPath(physicalRepoRoot, lexicalWorkspace) ||
        !isResolvedChild(physicalRunRoot, physicalWorkspace)) {
      throw new Error("TEST_EXTENSION_MANIFEST_WORKSPACE_INVALID");
    }
  } catch (error) {
    if (error instanceof Error && error.message === "TEST_EXTENSION_MANIFEST_WORKSPACE_INVALID") throw error;
    throw new Error("TEST_EXTENSION_MANIFEST_WORKSPACE_INVALID");
  }
  return lexicalWorkspace;
}

/** Prove the private manifest itself is a plain file physically inside the unchanged run root. */
export function validatePhysicalManifestFile(
  runRoot: string,
  manifestPath: string,
  operations: TestExtensionPhysicalPathOps = NODE_PHYSICAL_PATH_OPS,
): void {
  const lexicalRunRoot = path.resolve(runRoot);
  const lexicalManifest = path.resolve(manifestPath);
  if (!isResolvedChild(lexicalRunRoot, lexicalManifest)) {
    throw new Error("TEST_EXTENSION_MANIFEST_INVALID");
  }
  try {
    const stat = operations.lstat(lexicalManifest);
    if (stat.isSymbolicLink() || !stat.isFile()) throw new Error("TEST_EXTENSION_MANIFEST_INVALID");
    const physicalRunRoot = operations.realpath(lexicalRunRoot);
    const physicalManifest = operations.realpath(lexicalManifest);
    if (!sameResolvedPath(physicalRunRoot, lexicalRunRoot) ||
        !sameResolvedPath(physicalManifest, lexicalManifest) ||
        !isResolvedChild(physicalRunRoot, physicalManifest)) {
      throw new Error("TEST_EXTENSION_MANIFEST_INVALID");
    }
  } catch (error) {
    if (error instanceof Error && error.message === "TEST_EXTENSION_MANIFEST_INVALID") throw error;
    throw new Error("TEST_EXTENSION_MANIFEST_INVALID");
  }
}

/** Bind the private manifest to the one fixed workspace owned by its isolated run. */
export function validateManifestWorkspace(runRoot: string, repoRoot: string, supplied: unknown): string {
  const expected = path.resolve(runRoot, "fixture-workspace");
  if (typeof supplied !== "string" || !path.isAbsolute(supplied) || supplied !== expected) {
    throw new Error("TEST_EXTENSION_MANIFEST_WORKSPACE_INVALID");
  }
  const relative = path.relative(path.resolve(runRoot), expected);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative) || !sameResolvedPath(repoRoot, expected)) {
    throw new Error("TEST_EXTENSION_MANIFEST_WORKSPACE_INVALID");
  }
  return expected;
}

function exactObjectKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  return actual.length === wanted.length && actual.every((key, index) => key === wanted[index]);
}

/** Parse, validate, deep-freeze, and hash the one private manifest byte snapshot used by the EDH. */
export function pinTestExtensionManifest(
  bytes: Buffer,
  runRoot: string,
  repoRoot: string,
): TestExtensionManifestSnapshot {
  if (bytes.length < 2 || bytes.length > 64 * 1024) throw new Error("TEST_EXTENSION_MANIFEST_INVALID");
  let raw: unknown;
  try { raw = JSON.parse(bytes.toString("utf-8")); } catch { throw new Error("TEST_EXTENSION_MANIFEST_INVALID"); }
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("TEST_EXTENSION_MANIFEST_INVALID");
  const value = raw as Record<string, unknown>;
  const expectedRunId = path.basename(path.resolve(runRoot));
  const codewords = value.codewords;
  const fixtures = value.fixtures;
  if (!/^te-[A-Za-z0-9_-]{1,120}$/.test(expectedRunId) || value.v !== 1 || value.run_id !== expectedRunId ||
      typeof value.suite_nonce !== "string" || !/^[0-9a-f]{64}$/.test(value.suite_nonce) ||
      !codewords || typeof codewords !== "object" || Array.isArray(codewords) ||
      !fixtures || typeof fixtures !== "object" || Array.isArray(fixtures)) {
    throw new Error("TEST_EXTENSION_MANIFEST_INVALID");
  }
  const codewordRecord = codewords as Record<string, unknown>;
  const fixtureRecord = fixtures as Record<string, unknown>;
  const suffix = codewordSuffix(expectedRunId);
  const expectedCodewords = {
    text: `TETEXT_${suffix}`,
    context: `TECTX_${suffix}`,
    selection: `TESEL_${suffix}`,
    image: `TEIMG_${suffix}`,
  } as const;
  if (!exactObjectKeys(codewordRecord, Object.keys(expectedCodewords)) ||
      !exactObjectKeys(fixtureRecord, Object.keys(TEST_EXTENSION_FIXED_FIXTURES)) ||
      Object.entries(expectedCodewords).some(([key, expected]) => codewordRecord[key] !== expected) ||
      Object.entries(TEST_EXTENSION_FIXED_FIXTURES).some(([key, expected]) => fixtureRecord[key] !== expected)) {
    throw new Error("TEST_EXTENSION_MANIFEST_INVALID");
  }
  const workspace = validateManifestWorkspace(runRoot, repoRoot, value.workspace_path);
  const manifest: TestExtensionPrivateManifest = Object.freeze({
    v: 1,
    run_id: expectedRunId,
    suite_nonce: value.suite_nonce,
    workspace_path: workspace,
    codewords: Object.freeze({ ...expectedCodewords }),
    fixtures: Object.freeze({ ...TEST_EXTENSION_FIXED_FIXTURES }),
  });
  return Object.freeze({
    sha256: crypto.createHash("sha256").update(bytes).digest("hex"),
    manifest,
  });
}

/** Reject any post-attestation byte change before returning the pinned immutable manifest. */
export function verifyPinnedTestExtensionManifest(
  snapshot: TestExtensionManifestSnapshot,
  currentBytes: Buffer,
): TestExtensionPrivateManifest {
  const current = crypto.createHash("sha256").update(currentBytes).digest("hex");
  if (current !== snapshot.sha256) throw new Error("TEST_EXTENSION_MANIFEST_CHANGED");
  return snapshot.manifest;
}

function codewordSuffix(runId: string): string {
  return runId.slice(runId.lastIndexOf("-") + 1).toUpperCase();
}

/** Resolve and validate one safe workspace-relative fixture location. */
function fixtureLocation(manifest: TestExtensionPromptManifest, fixture: keyof TestExtensionPromptManifest["fixtures"]): {
  absolute: string;
  relative: string;
} {
  const relative = manifest.fixtures[fixture];
  const segments = relative.split(/[\\/]+/);
  const absolute = path.resolve(manifest.workspace_path, relative);
  const fromWorkspace = path.relative(manifest.workspace_path, absolute);
  if (!relative || path.isAbsolute(relative) || segments.includes("..") || segments.includes(".") ||
      fromWorkspace.startsWith("..") || path.isAbsolute(fromWorkspace)) {
    throw new Error("TEST_EXTENSION_MANIFEST_FIXTURE_INVALID");
  }
  return { absolute, relative };
}

function workspaceLocation(manifest: TestExtensionPromptManifest): string {
  return `Absolute isolated workspace/cwd: ${JSON.stringify(manifest.workspace_path)}.`;
}

function targetLocation(
  manifest: TestExtensionPromptManifest,
  fixture: keyof TestExtensionPromptManifest["fixtures"],
): string {
  const target = fixtureLocation(manifest, fixture);
  return `Absolute target: ${JSON.stringify(target.absolute)}. Tool path (workspace-relative): ${JSON.stringify(target.relative)}.`;
}

export function scenarioPrompt(
  scenario: TestExtensionScenario,
  manifest: TestExtensionPromptManifest,
  python: string,
): string {
  const workspace = workspaceLocation(manifest);
  switch (scenario.kind) {
    case "text": return `${workspace} Reply once and include the exact codeword ${manifest.codewords.text}.`;
    case "image": {
      const image = fixtureLocation(manifest, "image");
      return `${workspace} Absolute attachment source: ${JSON.stringify(image.absolute)}. Read the attached image and include its visible TEIMG codeword in your reply.`;
    }
    case "context": {
      const context = fixtureLocation(manifest, "context");
      const selection = fixtureLocation(manifest, "selection");
      return `${workspace} Absolute context sources: ${JSON.stringify(context.absolute)} and ${JSON.stringify(selection.absolute)}. Context-ref paths (workspace-relative): ${JSON.stringify(context.relative)} and ${JSON.stringify(selection.relative)}. Use both governed context references and include their exact TECTX and TESEL codewords.`;
    }
    case "diff":
      return `${workspace} ${targetLocation(manifest, "diff_target")} Call kaizen_propose_changes once with that tool path and complete final UTF-8 text containing TE_DIFF_${codewordSuffix(manifest.run_id)}. Do not use another tool.`;
    case "process":
      return `${workspace} Absolute cwd: ${JSON.stringify(manifest.workspace_path)}. Tool cwd (workspace-relative): ".". Call kaizen_run_process once with executable ${JSON.stringify(python)}, argv ["-c","print('TE_PROCESS_OK')"], cwd ".", and timeout_ms 5000.`;
    case "control":
      return scenario.id === "claude-plan-controls"
        ? `${workspace} Absolute cwd: ${JSON.stringify(manifest.workspace_path)}. Tool cwd (workspace-relative): ".". Make exactly two kaizen_run_process calls in order. First use executable ${JSON.stringify(python)}, argv ["-c","print('MUST_NOT_RUN')"], cwd ".", and timeout_ms 5000. After its structured denial, use executable "git", argv ["push","origin","main"], cwd ".", and timeout_ms 5000. Report both structured denials.`
        : `${workspace} Absolute cwd: ${JSON.stringify(manifest.workspace_path)}. Tool cwd (workspace-relative): ".". Attempt kaizen_run_process once with executable ${JSON.stringify(python)}, argv ["-c","print('MUST_NOT_RUN')"], cwd ".", and timeout_ms 5000. Report the structured denial.`;
    case "traversal":
      return `${workspace} Call kaizen_read_file once with path ../outside.txt. Do not call another tool. Report the structured traversal denial.`;
    case "stale":
      return `${workspace} ${targetLocation(manifest, "diff_target")} Propose a whole-file modification using that tool path. Wait for the normal chat diff UI; the user will change the base before Accept and then decide the refreshed revision.`;
    case "corrupt":
      return `${workspace} ${targetLocation(manifest, "diff_target")} Propose a whole-file modification using that tool path. Wait while the normal chat diff preview is deliberately made unavailable; do not retry another tool.`;
    case "timeout":
      return `${workspace} ${targetLocation(manifest, "diff_target")} Propose a whole-file modification using that tool path. Wait without deciding the normal inline approval so the daemon timeout is authoritative.`;
    case "writer":
      return `${workspace} ${targetLocation(manifest, "diff_target")} Propose a whole-file modification using that tool path while another selected source-writing scenario holds the workspace lease.`;
    case "interrupt":
      return `${workspace} Begin a multi-step read-only analysis in this isolated workspace and keep working until the user uses Stop in this normal Kaizen chat tab; recovery must remain resumable after daemon restart.`;
    case "cleanup":
      return `${workspace} ${targetLocation(manifest, "context")} Read that tool path once and finish normally so the controller can close this final conversation before outer cleanup.`;
  }
}

export function writerLoserPrompt(manifest: TestExtensionPromptManifest): string {
  return `${workspaceLocation(manifest)} ${targetLocation(manifest, "diff_target")} Propose one modification using that tool path; do not call another tool.`;
}

export function restartContinuationPrompt(manifest: TestExtensionPromptManifest): string {
  return `${workspaceLocation(manifest)} Reply once with exact codeword ${manifest.codewords.text} after reduced-fidelity continuation.`;
}

export interface TestExtensionSelection {
  provider: TestExtensionProvider;
  model: string;
  effort?: string;
  maxTurns: number;
  callCeiling: number;
  scenarios: string[];
}

export interface TestExtensionControlRequest {
  v: 1;
  action: "start";
  suite_nonce: string;
  request_sha256: string;
  provider: TestExtensionProvider;
  model: string;
  effort: string | null;
  max_turns: number;
  call_ceiling: number;
  scenarios: string[];
  provider_retries: 0;
}

export interface TestExtensionPreApprovalStopRequest {
  v: 1;
  action: "stop";
  suite_nonce: string;
  stop_id: string;
  reason: "user";
  request_sha256?: string;
}

export interface TestExtensionExternalBlockRequest {
  v: 1;
  action: "external_block";
  suite_nonce: string;
  block_id: string;
  provider: TestExtensionProvider;
  code: string;
  scenarios: string[];
}

export class TestExtensionSelectionError extends Error {
  constructor(readonly code: string, message: string) {
    super(message);
    this.name = "TestExtensionSelectionError";
  }
}

export function providerEngine(provider: TestExtensionProvider): string {
  return provider === "claude" ? "claude" : "local_llm";
}

export function providerScenarios(provider: TestExtensionProvider): TestExtensionScenario[] {
  return TEST_EXTENSION_SCENARIOS.filter((scenario) => scenario.provider === provider);
}

export type TestExtensionReadinessState = "ready" | "auth_required" | "unavailable" | "unsupported";

export interface TestExtensionProviderReadiness {
  state: TestExtensionReadinessState;
  canStart: boolean;
  message: string;
  externalCode?: string;
}

export interface TestExtensionCapabilityRefreshOutcome {
  capabilities: EngineCapability[];
  failed: boolean;
  notice?: string;
}

export function testExtensionCapabilityCleanupNotice(existing: string | undefined, error: unknown): string {
  const detail = error instanceof Error ? error.message : String(error);
  const notice = `Capability refresh cleanup failed: ${detail}`;
  return existing ? `${existing}\n${notice}` : notice;
}

/** Convert View maxTurns to Client max_turns without hiding refresh failure. */
export function testExtensionCapabilityRefreshOutcome(
  snapshot: Pick<ConversationSnapshot, "capabilities" | "banner"> | undefined,
  previous: readonly EngineCapability[],
): TestExtensionCapabilityRefreshOutcome {
  if (!snapshot || snapshot.banner) {
    const message = snapshot?.banner?.message ?? "Capability discovery unavailable";
    const code = snapshot?.banner?.code ?? "TEST_EXTENSION_CAPABILITIES_UNAVAILABLE";
    return {
      capabilities: previous.map((capability) => ({ ...capability })),
      failed: true,
      notice: `Capability refresh failed: ${message} [${code}]`,
    };
  }
  return {
    capabilities: snapshot.capabilities.map((capability) => {
      const { maxTurns, ...rest } = capability;
      return { ...rest, ...(maxTurns ? { max_turns: maxTurns } : {}) };
    }),
    failed: false,
  };
}

/** Fail-closed, content-free readiness for the Test Extension surface. */
export function testExtensionProviderReadiness(
  provider: TestExtensionProvider,
  capabilities: readonly EngineCapability[],
  catalogCurrent = true,
): TestExtensionProviderReadiness {
  if (!catalogCurrent) {
    return {
      state: "unavailable",
      canStart: false,
      message: "Capability catalog refresh failed. Resolve the visible error and Refresh catalog.",
    };
  }
  const engine = capabilities.find((item) => item.id === providerEngine(provider));
  const label = provider === "claude" ? "Claude subscription" : "Ollama";
  if (!engine) {
    return { state: "unavailable", canStart: false, message: `${label} catalog is unavailable. Refresh catalog.` };
  }
  if (engine.features.test_extension !== true) {
    return {
      state: "unsupported",
      canStart: false,
      message: "Test Extension support is unavailable from this daemon. Update and restart the isolated test plane.",
    };
  }
  if (engine.availability.state === "auth_required" || engine.runtime?.status === "auth_required") {
    return {
      state: "auth_required",
      canStart: false,
      externalCode: externalScenarioBlockCode(engine.availability.code) ?? "DENIED_AUTH_UNAVAILABLE",
      message: provider === "claude"
        ? "Claude subscription authentication is required. Complete sign-in in an Anthropic-owned application outside Kaizen, then Refresh catalog."
        : "Provider authentication is required outside Kaizen, then Refresh catalog.",
    };
  }
  const runtimeUnavailable = engine.runtime?.status === "unavailable";
  if (runtimeUnavailable) {
    const initializationTimedOut = provider === "claude" &&
      engine.availability.message === "Claude subscription initialization timed out before account and model discovery.";
    return {
      state: "unavailable",
      canStart: false,
      externalCode: externalScenarioBlockCode(engine.availability.code) ?? "DENIED_SDK_UNAVAILABLE",
      message: initializationTimedOut
        ? "Claude subscription initialization timed out before account and model discovery. Wait for network access to settle, then Refresh catalog."
        : `${label} runtime is unavailable. Repair or start it outside Kaizen, then Refresh catalog.`,
    };
  }
  const runtimeNotReady = provider === "claude" && engine.runtime?.status !== "ready";
  const exactExternalCode = engine.drivable && engine.availability.state !== "available"
    ? externalScenarioBlockCode(engine.availability.code)
    : undefined;
  if (!engine.drivable || engine.availability.state !== "available" || runtimeNotReady) {
    return {
      state: "unavailable",
      canStart: false,
      ...(exactExternalCode === undefined ? {} : { externalCode: exactExternalCode }),
      message: `${label} runtime is unavailable. Repair or start it outside Kaizen, then Refresh catalog.`,
    };
  }
  if (engine.models.length < 1) {
    return {
      state: "unavailable",
      canStart: false,
      externalCode: externalScenarioBlockCode(engine.availability.code) ?? "DENIED_MODEL_UNAVAILABLE",
      message: `${label} reported no available models. Refresh catalog.`,
    };
  }
  return { state: "ready", canStart: true, message: `${label} is ready for a bounded Test Extension suite.` };
}

export interface TestExtensionControlState {
  workloadLocked: boolean;
  startLocked: boolean;
  stopLocked: boolean;
}

export function testExtensionControlState(
  running: boolean,
  stopping: boolean,
  stopRequested = false,
): TestExtensionControlState {
  const workloadLocked = running || stopping || stopRequested;
  return { workloadLocked, startLocked: workloadLocked, stopLocked: stopping || stopRequested };
}

/** Exact one-shot control used only before the outer runner has issued an action binding. */
export function preApprovalStopRequest(
  suiteNonce: string,
  stopId: string,
  requestSha256?: string,
): TestExtensionPreApprovalStopRequest {
  if (!/^[0-9a-f]{64}$/.test(suiteNonce)) {
    throw new TestExtensionSelectionError("DENIED_TEST_EXTENSION_SUITE_NONCE", "Suite nonce is invalid");
  }
  if (!/^[0-9a-f]{64}$/.test(stopId)) {
    throw new TestExtensionSelectionError("DENIED_TEST_EXTENSION_STOP_ID", "Stop id is invalid");
  }
  if (requestSha256 !== undefined && !/^[0-9a-f]{64}$/.test(requestSha256)) {
    throw new TestExtensionSelectionError("DENIED_TEST_EXTENSION_REQUEST_DIGEST", "Suite request digest is invalid");
  }
  return {
    v: 1, action: "stop", suite_nonce: suiteNonce, stop_id: stopId, reason: "user",
    ...(requestSha256 === undefined ? {} : { request_sha256: requestSha256 }),
  };
}

/** Require one final run.closed after proven, non-preserved EDH and daemon cleanup. */
export function testExtensionPlaneClosed(entries: readonly TestExtensionLedgerEntry[]): boolean {
  let closedIndex = -1;
  let closedCount = 0;
  for (let index = 0; index < entries.length; index += 1) {
    if (entries[index].event === "run.closed") {
      closedIndex = index;
      closedCount += 1;
    }
  }
  if (closedCount !== 1 || closedIndex !== entries.length - 1) return false;
  const closed = entries[closedIndex];
  if (entries.slice(0, closedIndex).some((entry) => entry.seq >= closed.seq)) return false;
  if (entries.slice(0, closedIndex).some(
    (entry) => entry.event.startsWith("cleanup.") && entry.preserved_for_audit === true,
  )) return false;
  return ["cleanup.edh", "cleanup.daemon"].every((event) => {
    let terminal: TestExtensionLedgerEntry | undefined;
    for (let index = 0; index < closedIndex; index += 1) {
      if (entries[index].event === event) terminal = entries[index];
    }
    return terminal?.status === "PASS" && terminal.termination_proven === true &&
      terminal.preserved_for_audit !== true && terminal.seq < closed.seq;
  });
}

export function reconcileModelSelection(
  available: readonly string[],
  previous: string | undefined,
  defaultModel?: string | null,
): { selected: string; unavailable: boolean } {
  const ids = new Set(available);
  if (previous) return { selected: previous, unavailable: !ids.has(previous) };
  if (defaultModel && ids.has(defaultModel)) return { selected: defaultModel, unavailable: false };
  return { selected: "", unavailable: false };
}

/** Preserve explicit choices, including select-none, while filtering catalog drift deterministically. */
export function reconcileScenarioSelection(
  available: readonly string[],
  previous: readonly string[] | undefined,
): string[] {
  const selected = new Set(previous ?? available);
  return [...new Set(available)].filter((scenario) => selected.has(scenario));
}

const EXTERNAL_SCENARIO_BLOCK_CODES = new Set([
  "DENIED_AUTH_UNAVAILABLE",
  "DENIED_AUTH_MODE_MISMATCH",
  "DENIED_MODEL_UNAVAILABLE",
  "DENIED_SDK_UNAVAILABLE",
  "DENIED_PROVIDER_CAPACITY",
  "RATE_LIMIT",
  "RATE_LIMITED",
  "RATE_LIMIT_EXHAUSTED",
  "DENIED_RATE_LIMIT",
  "DENIED_RATE_LIMITED",
  "DENIED_RATE_LIMIT_EXHAUSTED",
  "QUOTA_EXHAUSTED",
  "DENIED_QUOTA_EXHAUSTED",
  "SUBSCRIPTION_RATE_LIMIT_EXHAUSTED",
  "SUBSCRIPTION_QUOTA_EXHAUSTED",
]);

/** Exact sanitized provider/external blocks only; workload, policy, and internal failures stay FAIL. */
export function externalScenarioBlockCode(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const normalized = value.trim().toUpperCase();
  if (!/^[A-Z0-9_]{1,128}$/.test(normalized)) return undefined;
  return EXTERNAL_SCENARIO_BLOCK_CODES.has(normalized) ? normalized : undefined;
}

/** Build an exact replay-safe external NOT_RUN control only when current capabilities justify it. */
export function externalBlockRequest(
  suiteNonce: string,
  blockId: string,
  provider: TestExtensionProvider,
  code: string,
  scenarios: readonly string[],
  capabilities: readonly EngineCapability[],
  catalogCurrent = true,
): TestExtensionExternalBlockRequest {
  if (!/^[0-9a-f]{64}$/.test(suiteNonce)) {
    throw new TestExtensionSelectionError("DENIED_TEST_EXTENSION_SUITE_NONCE", "Suite nonce is invalid");
  }
  if (!/^[0-9a-f]{64}$/.test(blockId)) {
    throw new TestExtensionSelectionError("DENIED_TEST_EXTENSION_BLOCK_ID", "External block id is invalid");
  }
  if (provider !== "claude" && provider !== "ollama") {
    throw new TestExtensionSelectionError("DENIED_TEST_EXTENSION_PROVIDER", "Select Claude or Ollama");
  }
  const normalizedCode = externalScenarioBlockCode(code);
  const readiness = testExtensionProviderReadiness(provider, capabilities, catalogCurrent);
  if (!normalizedCode || !readiness.externalCode || normalizedCode !== readiness.externalCode) {
    throw new TestExtensionSelectionError(
      "DENIED_TEST_EXTENSION_EXTERNAL_BLOCK",
      "Current provider capabilities do not justify this external block",
    );
  }
  if (!Array.isArray(scenarios) || scenarios.length < 1 || new Set(scenarios).size !== scenarios.length) {
    throw new TestExtensionSelectionError("DENIED_TEST_EXTENSION_SCENARIOS", "Select unique scenarios");
  }
  const allowed = new Set(providerScenarios(provider).map((scenario) => scenario.id));
  if (scenarios.some((scenario) => typeof scenario !== "string" || !allowed.has(scenario))) {
    throw new TestExtensionSelectionError(
      "DENIED_TEST_EXTENSION_PROVIDER_SCENARIO",
      "Scenario belongs to another provider",
    );
  }
  return {
    v: 1,
    action: "external_block",
    suite_nonce: suiteNonce,
    block_id: blockId,
    provider,
    code: normalizedCode,
    scenarios: [...scenarios],
  };
}

export function scenarioCallMultiplier(scenario: string): number {
  return scenario === "claude-interrupt-restart" ? 2 : 1;
}

export function reservedProviderCalls(scenarios: readonly string[], maxTurns: number): number {
  return scenarios.reduce((total, scenario) => total + scenarioCallMultiplier(scenario), 0) * maxTurns;
}

export interface ProcessCardProof {
  tool_card_count: number;
  tool_status: string;
  tool_request_matches: boolean;
  tool_zero_execution: boolean;
  tool_zero_stdout: boolean;
  process_stdout_bytes: number;
  process_request_sha256: string;
  denial_code?: string;
  invariant_denial_code?: string;
}

/** Hash the OS-local resolved executable and request fields exactly as the daemon does. */
export function processRequestSha256(executable: string, argv: readonly string[], cwd: string, timeoutMs: number): string {
  return crypto.createHash("sha256").update([path.resolve(executable), ...argv, cwd, String(timeoutMs)].join("\n")).digest("hex");
}

/** Exact, content-free proof that Plan and, when requested, git-push invariant controls did not execute. */
export function planProcessControlProof(
  cards: readonly ToolCardView[], executable: string, requireGitPushInvariant = true,
): ProcessCardProof {
  const expectedArgv = ["-c", "print('MUST_NOT_RUN')"];
  const processCards = cards.filter((card) => card.tool === "kaizen_run_process");
  const planCards = processCards.filter((card) => {
    const process = card.process;
    return !!process && path.resolve(process.executable) === path.resolve(executable) &&
      process.argv.length === expectedArgv.length && process.argv.every((arg, index) => arg === expectedArgv[index]) &&
      process.cwd === "." && process.timeoutMs === 5000;
  });
  const invariantCards = processCards.filter((card) => {
    const process = card.process;
    return !!process && process.executable === "git" &&
      process.argv.length === 3 && process.argv[0] === "push" && process.argv[1] === "origin" &&
      process.argv[2] === "main" && process.cwd === "." && process.timeoutMs === 5000;
  });
  const planCard = planCards.length === 1 ? planCards[0] : undefined;
  const invariantCard = requireGitPushInvariant && invariantCards.length === 1 ? invariantCards[0] : undefined;
  const expectedCount = requireGitPushInvariant ? 2 : 1;
  const selected = requireGitPushInvariant ? [planCard, invariantCard] : [planCard];
  const requestMatches = processCards.length === expectedCount && selected.every((card) => !!card);
  const zeroExecution = selected.every((card) => card?.status === "blocked" && card.process?.decision === "denied" &&
    card.process.exitCode === undefined && card.process.timedOut !== true);
  const zeroOutput = selected.every((card) => !!card?.process && card.process.truncated !== true &&
    (card.process.stdoutBytes ?? 0) === 0 && card.process.stdoutSha256 === undefined);
  const denialCode = planCard?.code;
  const invariantCode = invariantCard?.code;
  return {
    tool_card_count: processCards.length,
    tool_status: selected.every((card) => card?.status === "blocked") ? "blocked" : "missing",
    tool_request_matches: requestMatches,
    tool_zero_execution: zeroExecution,
    tool_zero_stdout: zeroOutput,
    process_stdout_bytes: selected.reduce((total, card) => total + (card?.process?.stdoutBytes ?? 0), 0),
    process_request_sha256: processRequestSha256(executable, expectedArgv, ".", 5000),
    ...(typeof denialCode === "string" && /^(?:DENIED_[A-Z0-9_]{1,120}|INV_[A-Z0-9_]{1,120}|MODE_CEILING:[a-z_]{1,64})$/.test(denialCode)
      ? { denial_code: denialCode }
      : {}),
    ...(typeof invariantCode === "string" && /^INV_[A-Z0-9_]{1,120}$/.test(invariantCode)
      ? { invariant_denial_code: invariantCode }
      : {}),
  };
}

export function validateSelection(
  value: TestExtensionSelection,
  capabilities: readonly EngineCapability[],
  suiteNonce: string,
): TestExtensionControlRequest {
  if (!/^[0-9a-f]{64}$/.test(suiteNonce)) {
    throw new TestExtensionSelectionError("DENIED_TEST_EXTENSION_SUITE_NONCE", "Suite nonce is invalid");
  }
  if (value.provider !== "claude" && value.provider !== "ollama") {
    throw new TestExtensionSelectionError("DENIED_TEST_EXTENSION_PROVIDER", "Select Claude or Ollama");
  }
  const readiness = testExtensionProviderReadiness(value.provider, capabilities);
  if (!readiness.canStart) {
    throw new TestExtensionSelectionError("DENIED_TEST_EXTENSION_PROVIDER_UNAVAILABLE", readiness.message);
  }
  const engine = capabilities.find((item) => item.id === providerEngine(value.provider));
  if (!engine) throw new TestExtensionSelectionError("DENIED_TEST_EXTENSION_PROVIDER_UNAVAILABLE", readiness.message);
  const model = engine.models.find((item) => item.id === value.model);
  if (!model) throw new TestExtensionSelectionError("DENIED_TEST_EXTENSION_MODEL", "Select a discovered model");
  if ((model.reasoning_efforts ?? []).length > 0 && !value.effort) {
    throw new TestExtensionSelectionError("DENIED_TEST_EXTENSION_EFFORT_REQUIRED", "Select a discovered effort for this model");
  }
  if (value.effort && !(model.reasoning_efforts ?? []).includes(value.effort)) {
    throw new TestExtensionSelectionError("DENIED_TEST_EXTENSION_EFFORT", "Effort is unsupported by this model");
  }
  if (!Number.isSafeInteger(value.maxTurns) || value.maxTurns < 1 || value.maxTurns > 32) {
    throw new TestExtensionSelectionError("DENIED_TEST_EXTENSION_MAX_TURNS", "Max turns must be 1..32");
  }
  if (!Number.isSafeInteger(value.callCeiling) || value.callCeiling < 1 || value.callCeiling > 256) {
    throw new TestExtensionSelectionError("DENIED_TEST_EXTENSION_CALL_CEILING", "Call ceiling must be 1..256");
  }
  if (!Array.isArray(value.scenarios) || value.scenarios.length < 1 || new Set(value.scenarios).size !== value.scenarios.length) {
    throw new TestExtensionSelectionError("DENIED_TEST_EXTENSION_SCENARIOS", "Select unique scenarios");
  }
  const allowed = new Set(providerScenarios(value.provider).map((item) => item.id));
  if (value.scenarios.some((scenario) => !allowed.has(scenario))) {
    throw new TestExtensionSelectionError("DENIED_TEST_EXTENSION_PROVIDER_SCENARIO", "Scenario belongs to another provider");
  }
  const reservedCalls = reservedProviderCalls(value.scenarios, value.maxTurns);
  if (reservedCalls > value.callCeiling) {
    throw new TestExtensionSelectionError(
      "DENIED_TEST_EXTENSION_CALL_BUDGET",
      `${reservedCalls} conservatively reserved calls exceed the suite ceiling`,
    );
  }
  const request: Omit<TestExtensionControlRequest, "request_sha256"> = {
    v: 1,
    action: "start",
    suite_nonce: suiteNonce,
    provider: value.provider,
    model: value.model,
    effort: value.effort ?? null,
    max_turns: value.maxTurns,
    call_ceiling: value.callCeiling,
    scenarios: [...value.scenarios],
    provider_retries: 0,
  };
  return { ...request, request_sha256: canonicalRequestSha256(request) };
}

/** Python-compatible recursive key sort + compact UTF-8 JSON. */
export function canonicalRequestSha256(value: Record<string, unknown>): string {
  return crypto.createHash("sha256").update(canonicalJson(value), "utf8").digest("hex");
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

/** Clamp concurrency to 1..2 and preserve input order with a race-free shared cursor. */
export async function runBounded<T, R>(
  values: readonly T[],
  worker: (value: T, index: number) => Promise<R>,
  concurrency = 2,
): Promise<R[]> {
  if (!Number.isSafeInteger(concurrency) || concurrency < 1 || concurrency > 2) {
    throw new TestExtensionSelectionError("DENIED_TEST_EXTENSION_CONCURRENCY", "Concurrency must be 1 or 2");
  }
  const results = new Array<R>(values.length);
  let cursor = 0;
  const lane = async () => {
    while (cursor < values.length) {
      const index = cursor++;
      if (index >= values.length) return;
      results[index] = await worker(values[index], index);
    }
  };
  await Promise.all(Array.from({ length: Math.min(concurrency, values.length) }, lane));
  return results;
}

export interface TestExtensionLedgerEntry {
  seq: number;
  event: string;
  status: string;
  scenario?: string;
  code?: string;
  termination_proven?: boolean;
  not_started?: boolean;
  preserved_for_audit?: boolean;
}

/** Tolerantly parse bounded v1 NDJSON fields and retain the final 500 valid rows. */
export function parseEvidenceJsonl(text: string): TestExtensionLedgerEntry[] {
  const output: TestExtensionLedgerEntry[] = [];
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim()) continue;
    let value: unknown;
    try { value = JSON.parse(line); } catch { continue; }
    if (!value || typeof value !== "object" || Array.isArray(value)) continue;
    const raw = value as Record<string, unknown>;
    if (raw.v !== 1 || !Number.isSafeInteger(raw.seq) || typeof raw.event !== "string" || typeof raw.status !== "string") continue;
    output.push({
      seq: raw.seq as number,
      event: raw.event.slice(0, 128),
      status: raw.status.slice(0, 32),
      ...(typeof raw.scenario === "string" ? { scenario: raw.scenario.slice(0, 128) } : {}),
      ...(typeof raw.code === "string" ? { code: raw.code.slice(0, 128) } : {}),
      ...(typeof raw.termination_proven === "boolean" ? { termination_proven: raw.termination_proven } : {}),
      ...(typeof raw.not_started === "boolean" ? { not_started: raw.not_started } : {}),
      ...(typeof raw.preserved_for_audit === "boolean" ? { preserved_for_audit: raw.preserved_for_audit } : {}),
    });
  }
  return output.slice(-500);
}
