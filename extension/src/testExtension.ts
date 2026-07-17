/** Acceptance controller for isolated Kaizen sessions and the bound outer-runner file protocol. */

import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";

import { ContextStager, StagedContext } from "./contextStager";
import { ImageStager, StagedImage } from "./imageStager";
import { ChatPanelManager } from "./chatPanel";
import {
  TEST_EXTENSION_SCENARIOS,
  canonicalRequestSha256,
  createTestExtensionReadyGate,
  externalBlockRequest,
  externalScenarioBlockCode,
  installWebviewHtml,
  TestExtensionControlRequest,
  TestExtensionLedgerEntry,
  TestExtensionManifestSnapshot,
  TestExtensionPrivateManifest,
  TestExtensionScenario,
  TestExtensionSelection,
  parseEvidenceJsonl,
  planProcessControlProof,
  preApprovalStopRequest,
  processRequestSha256,
  providerEngine,
  restartContinuationPrompt,
  scenarioCallMultiplier,
  scenarioPrompt,
  runBounded,
  testExtensionControlState,
  testExtensionCapabilityCleanupNotice,
  testExtensionCapabilityRefreshOutcome,
  testExtensionPlaneClosed,
  testExtensionProviderReadiness,
  pinTestExtensionManifest,
  validatePhysicalManifestFile,
  validatePhysicalManifestWorkspace,
  validateSelection,
  verifyPinnedTestExtensionManifest,
  writerLoserPrompt,
} from "./testExtensionCore";
import {
  TestExtensionTerminalPort,
  startTestExtensionTerminal,
  testExtensionLauncherControlState,
} from "./testExtensionTerminal";
import { EngineCapability } from "./sessionClient";

const SCENARIO_TIMEOUT_MS = 10 * 60 * 1000;
const OUTER_APPROVAL_WAIT_MS = 15_000;
const EVIDENCE_BYTES_LIMIT = 8 * 1024 * 1024;

type TestExtensionManifest = TestExtensionPrivateManifest;

/** Common scenario result plus kind-specific proof fields serialized to client evidence JSONL. */
interface ScenarioResult {
  scenario: string;
  provider: "claude" | "ollama";
  status: "PASS" | "FAIL" | "NOT_RUN";
  code?: string;
  session_id?: string;
  agent_run_id?: string;
  correlation_id?: string;
  previous_agent_run_id?: string;
  calls_reserved: number;
  delta_count: number;
  ordered: boolean;
  codeword_seen: boolean;
  approval_ui: boolean;
  decision?: "approve" | "deny";
  profile_attested?: boolean;
  attested_model?: string;
  attested_effort?: string;
  durable_replacements?: number;
  stream_suppressed?: boolean;
  image_refs?: number;
  context_refs?: number;
  selection_exact?: boolean;
  approval_status?: string;
  diff_card_count?: number;
  diff_path_matches?: boolean;
  diff_before_matches?: boolean;
  diff_proposed_hashed?: boolean;
  diff_final_matches?: boolean;
  tool_card_count?: number;
  tool_status?: string;
  tool_request_matches?: boolean;
  tool_result_matches?: boolean;
  tool_zero_execution?: boolean;
  tool_zero_stdout?: boolean;
  process_exit_code?: number;
  process_stdout_bytes?: number;
  process_request_sha256?: string;
  process_stdout_sha256?: string;
  tool_decision_approved?: boolean;
  tool_output_complete?: boolean;
  denial_code?: string;
  invariant_denial_code?: string;
  zero_records?: boolean;
  conversation_idle?: boolean;
  pending_approvals?: number;
  running_cards?: number;
  diff_revision_refreshed?: boolean;
  diff_corrupt?: boolean;
  diff_second_accept?: boolean;
  diff_authoritative_denial?: boolean;
  diff_path?: string;
  diff_revision?: number;
  diff_refreshed_revision?: number;
  diff_snapshot_set_sha256?: string;
  diff_refreshed_snapshot_set_sha256?: string;
  diff_before_sha256?: string;
  diff_mutated_base_sha256?: string;
  diff_refreshed_before_sha256?: string;
  diff_proposed_sha256?: string;
  diff_final_sha256?: string;
  diff_corrupted_artifact_sha256?: string;
  writer_conflict_seen?: boolean;
  writer_loser_zero_records?: boolean;
  writer_lease_retained?: boolean;
  writer_lease_released?: boolean;
  interrupted?: boolean;
  restart_resumable?: boolean;
  restart_same_session?: boolean;
  restart_new_run?: boolean;
  restart_reduced_fidelity?: boolean;
  queue_empty?: boolean;
  runtime_children_clean?: boolean;
}

type ScenarioProof = Omit<ScenarioResult,
  "scenario" | "provider" | "status" | "code" | "calls_reserved" | "delta_count" | "ordered" |
  "codeword_seen" | "approval_ui" | "decision">;

interface ScenarioLocatorProof {
  passed: boolean;
  fields: Pick<ScenarioResult, "session_id" | "agent_run_id" | "correlation_id">;
}

interface DiffProof {
  path: string;
  beforeSha256: string | null;
  proposedSha256: string | null;
}

interface ExactDiffBinding {
  conversationId: string;
  sessionId: string;
  agentRunId: string;
  correlationId: string;
  revision: number;
  snapshotSetSha256: string;
  workspacePath: string;
  beforeSha256: string;
  proposedSha256: string;
  openSequenceNo: number;
}

interface HarnessMessage {
  type?: unknown;
  selection?: unknown;
  provider?: unknown;
  code?: unknown;
  scenarios?: unknown;
}

type TestExtensionCleanupState = "idle" | "active" | "verifying" | "verified" | "unproven";

const ACTION_PROTOCOL = "test-extension-action-v2";
type OuterActionPhase = "stale.mutate_base" | "corrupt.snapshot" | "writer.holder_open" |
  "writer.loser_arm" | "writer.loser_verify" | "writer.holder_release" |
  "restart.daemon" | "restart.cleanup" | "traversal.arm_zero_records" |
  "traversal.verify_zero_records" | "suite.stop";

interface SuiteActionBinding {
  protocol: typeof ACTION_PROTOCOL;
  suite_nonce: string;
  request_sha256: string;
}

/** Receipt that must echo every request field before its phase-specific proof is trusted. */
interface OuterActionReceipt extends SuiteActionBinding, Record<string, unknown> {
  sequence: number;
  action_id: string;
  scenario_id: string;
  phase: OuterActionPhase;
  status: "OK" | "FAILED";
  code?: string;
  proof: Record<string, unknown>;
}

/** Editor-tab acceptance controller over the normal Kaizen session protocol. */
export class TestExtensionPanel implements vscode.Disposable {
  static readonly viewType = "kaizen.testExtension.panel";

  private panel: vscode.WebviewPanel | undefined;
  private readonly sourceRoot: string;
  private readonly runRoot: string | undefined;
  private readonly evidencePath: string | undefined;
  private readonly clientEvidencePath: string | undefined;
  private readonly controlPath: string | undefined;
  private readonly actionRoot: string | undefined;
  private readonly edhReadyPath: string | undefined;
  private manifestSnapshot: TestExtensionManifestSnapshot | undefined;
  private actionBinding: SuiteActionBinding | undefined;
  private actionSequence = 0;
  private capabilities: EngineCapability[] = [];
  private ledger: TestExtensionLedgerEntry[] = [];
  private poller: NodeJS.Timeout | undefined;
  private terminal: TestExtensionTerminalPort | undefined;
  private launcherRunRoot: string | undefined;
  private launcherEvidencePath: string | undefined;
  private launcherStopRequested = false;
  private launcherClosedProven = false;
  private launcherPoller: NodeJS.Timeout | undefined;
  private readonly terminalCloseListener: vscode.Disposable;
  private panelReady: import("./testExtensionCore").TestExtensionReadyGate | undefined;
  private pendingRequest: TestExtensionControlRequest | undefined;
  private cancelledRequestSha256: string | undefined;
  private running = false;
  private stopping = false;
  private stopRequested = false;
  private capabilityRefreshFailed = false;
  private notice: string | undefined;
  private cleanupState: TestExtensionCleanupState = "idle";

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly repoRoot: string,
    private readonly python: () => string,
    private readonly chats: ChatPanelManager,
  ) {
    this.runRoot = safeRunRoot(process.env.KAIZEN_TEST_EXTENSION_RUN_ROOT);
    if (this.runRoot) {
      this.cleanupState = "active";
      validatePhysicalManifestWorkspace(this.runRoot, this.repoRoot);
      validatePhysicalManifestFile(this.runRoot, path.join(this.runRoot, "manifest.json"));
    }
    const workspaceSource = path.join(repoRoot, "tests", "run_test_extension.py");
    this.sourceRoot = fs.existsSync(workspaceSource) ? repoRoot : path.resolve(context.extensionUri.fsPath, "..");
    this.evidencePath = this.runRoot
      ? safeChildPath(this.runRoot, process.env.KAIZEN_TEST_EXTENSION_EVIDENCE_PATH, "evidence.jsonl")
      : undefined;
    this.clientEvidencePath = this.runRoot
      ? safeChildPath(this.runRoot, process.env.KAIZEN_TEST_EXTENSION_CLIENT_EVIDENCE_PATH, "client-evidence.jsonl")
      : undefined;
    this.controlPath = this.runRoot
      ? safeChildPath(this.runRoot, process.env.KAIZEN_TEST_EXTENSION_CONTROL_PATH, "control.json")
      : undefined;
    this.actionRoot = this.runRoot
      ? safeChildPath(this.runRoot, process.env.KAIZEN_TEST_EXTENSION_ACTION_ROOT, "actions")
      : undefined;
    this.edhReadyPath = this.runRoot
      ? safeChildPath(this.runRoot, process.env.KAIZEN_TEST_EXTENSION_EDH_READY_PATH, "edh-ready.json")
      : undefined;
    this.terminalCloseListener = vscode.window.onDidCloseTerminal((terminal) => {
      if (terminal !== this.terminal) return;
      this.finishLauncherTerminal();
    });
  }

  async open(): Promise<void> {
    if (this.panel) {
      this.panel.reveal(this.panel.viewColumn ?? vscode.ViewColumn.Active, true);
      return;
    }
    const panel = vscode.window.createWebviewPanel(
      TestExtensionPanel.viewType,
      "Kaizen Test Extension",
      vscode.ViewColumn.Active,
      { enableScripts: true, retainContextWhenHidden: true },
    );
    this.panel = panel;
    const readiness = createTestExtensionReadyGate();
    this.panelReady = readiness;
    let listener: { dispose(): unknown } | undefined;
    panel.onDidDispose(() => {
      listener?.dispose();
      readiness.settle(false);
      if (this.panel !== panel) return;
      this.panel = undefined;
      this.panelReady = undefined;
      this.stopPolling();
      this.stopLauncherPolling();
    });
    listener = installWebviewHtml<HarnessMessage>(
      (handler) => panel.webview.onDidReceiveMessage(handler),
      (html) => { panel.webview.html = html; },
      panelHtml(panel.webview),
      (message) => void this.onMessage(message),
    );
    if (this.panel !== panel) {
      listener.dispose();
      return;
    }
    if (this.runRoot) {
      await this.refreshCapabilities(false);
      this.startPolling();
    } else if (this.terminal && this.terminal.exitStatus === undefined) {
      this.startLauncherPolling();
    }
    this.postSnapshot();
  }

  async autoOpenInPlane(): Promise<void> {
    if (!this.runRoot) return;
    await this.open();
    const readiness = this.panelReady;
    if (!readiness) throw new Error("TEST_EXTENSION_PANEL_READY_MISSING");
    if (!(await readiness.promise)) throw new Error("TEST_EXTENSION_PANEL_DISPOSED_BEFORE_READY");
    this.writeEdhReady();
  }

  dispose(): void {
    this.stopPolling();
    this.stopLauncherPolling();
    this.terminalCloseListener.dispose();
    this.panel?.dispose();
  }

  private async onMessage(message: HarnessMessage): Promise<void> {
    try {
      switch (message.type) {
        case "ready":
          this.panelReady?.settle(true);
          this.postSnapshot();
          break;
        case "launch": this.launch(); break;
        case "refresh": await this.refreshCapabilities(true); break;
        case "start": await this.startSuite(asSelection(message.selection)); break;
        case "external-block": this.recordExternalNotRun(message.provider, message.code, message.scenarios); break;
        case "stop": await this.stop(); break;
      }
    } catch (error) {
      this.notice = error instanceof Error ? error.message : String(error);
      this.postSnapshot();
    }
  }

  private launch(): void {
    if (this.runRoot) return;
    if (this.cleanupState === "unproven") {
      throw new Error("TEST_EXTENSION_CLEANUP_NOT_VERIFIED");
    }
    this.ledger = [];
    this.launcherEvidencePath = undefined;
    const runner = path.join(this.sourceRoot, "tests", "run_test_extension.py");
    const planeBase = testExtensionLauncherPlaneBase(this.sourceRoot);
    const requestedRunId = createTestExtensionRunId();
    const result = startTestExtensionTerminal(
      vscode.window, this.python(), this.sourceRoot, process.execPath, runner, planeBase, requestedRunId,
    );
    this.terminal = result.terminal;
    this.launcherRunRoot = result.runId ? path.join(planeBase, "runs", result.runId) : undefined;
    this.launcherEvidencePath = this.launcherRunRoot
      ? path.join(this.launcherRunRoot, "evidence.jsonl")
      : undefined;
    this.launcherStopRequested = false;
    this.launcherClosedProven = false;
    this.cleanupState = "active";
    this.notice = result.created
      ? "The isolated Extension Development Host will open its Test Extension editor tab after preflight."
      : result.runId
        ? "The existing visible Test Extension terminal was revealed."
        : "The legacy Test Extension terminal was revealed; its exact run binding is unavailable to this launcher.";
    this.startLauncherPolling();
    this.postSnapshot();
  }

  private async refreshCapabilities(refresh: boolean): Promise<void> {
    if (!this.runRoot) return;
    let conversationId: string | undefined;
    try {
      conversationId = await this.chats.newConversation();
      const snapshot = refresh
        ? await this.chats.testExtensionRefreshCapabilities(conversationId)
        : this.chats.testExtensionSnapshot(conversationId);
      const outcome = testExtensionCapabilityRefreshOutcome(snapshot, this.capabilities);
      this.capabilities = outcome.capabilities;
      this.capabilityRefreshFailed = outcome.failed;
      this.notice = outcome.notice;
    } catch (error) {
      this.capabilityRefreshFailed = true;
      this.notice = `Capability refresh failed: ${error instanceof Error ? error.message : String(error)}`;
    } finally {
      if (conversationId) {
        try {
          if (!(await this.chats.testExtensionClose(conversationId))) {
            throw new Error("TEST_EXTENSION_CAPABILITY_CONTROLLER_CLOSE_FAILED");
          }
        } catch (error) {
          this.capabilityRefreshFailed = true;
          this.notice = testExtensionCapabilityCleanupNotice(this.notice, error);
        }
      }
    }
    this.postSnapshot();
  }

  private startPolling(): void {
    if (this.poller) return;
    const tick = () => {
      if (!this.evidencePath) return;
      try {
        const data = fs.existsSync(this.evidencePath) ? readBoundedUtf8(this.evidencePath, EVIDENCE_BYTES_LIMIT) : "";
        this.ledger = parseEvidenceJsonl(data);
        this.postSnapshot();
      } catch {
        this.notice = "Sanitized evidence is temporarily unavailable";
      }
    };
    tick();
    this.poller = setInterval(tick, 1000);
  }

  private stopPolling(): void {
    if (this.poller) clearInterval(this.poller);
    this.poller = undefined;
  }

  private async startSuite(selection: TestExtensionSelection): Promise<void> {
    if (this.running) throw new Error("An acceptance suite is already running");
    if (this.stopping) throw new Error("The isolated test plane is stopping");
    if (this.stopRequested) throw new Error("The isolated test plane is closing");
    if (this.capabilityRefreshFailed) throw new Error("Capability catalog is unavailable; Refresh catalog first");
    if (!this.runRoot || !this.controlPath || !this.clientEvidencePath) {
      throw new Error("Launch the isolated visible-terminal runner first");
    }
    const manifest = this.readManifest();
    const request = validateSelection(selection, this.capabilities, manifest.suite_nonce);
    this.actionBinding = undefined;
    this.pendingRequest = request;
    this.cancelledRequestSha256 = undefined;
    writeAtomicJson(this.controlPath, request);
    this.running = true;
    this.notice = undefined;
    this.postSnapshot();
    try {
      const binding = await this.waitForOuterApproval(request);
      if (!binding) return;
      this.actionBinding = binding;
      this.actionSequence = 0;
      this.appendClient({
        event: "suite.started", provider: request.provider, status: "START",
        calls_reserved: request.scenarios.reduce(
          (total, id) => total + scenarioCallMultiplier(id) * request.max_turns, 0,
        ),
      });
      const definitions = request.scenarios
        .map((id) => TEST_EXTENSION_SCENARIOS.find((scenario) => scenario.id === id))
        .filter((scenario): scenario is TestExtensionScenario => !!scenario);
      const results = await runBounded(definitions, (scenario) => this.runScenario(request, scenario), 1);
      for (const result of results) {
        this.appendClient({ event: result.status === "NOT_RUN" ? "scenario.not_run" : "scenario.complete", ...result });
      }
      this.appendClient({
        event: "suite.complete",
        passed: results.filter((result) => result.status === "PASS").length,
        failed: results.filter((result) => result.status === "FAIL").length,
        not_run: results.filter((result) => result.status === "NOT_RUN").length,
      });
    } finally {
      if (this.pendingRequest?.request_sha256 === request.request_sha256) this.pendingRequest = undefined;
      this.running = false;
      this.postSnapshot();
    }
  }

  private recordExternalNotRun(providerValue: unknown, codeValue: unknown, scenariosValue: unknown): void {
    if (this.running) throw new Error("An acceptance suite is already running");
    if (this.stopping) throw new Error("The isolated test plane is stopping");
    if (this.stopRequested) throw new Error("The isolated test plane is closing");
    if (this.capabilityRefreshFailed) throw new Error("Capability catalog is unavailable; Refresh catalog first");
    if (!this.runRoot || !this.controlPath) throw new Error("Launch the isolated visible-terminal runner first");
    if (providerValue !== "claude" && providerValue !== "ollama") {
      throw new Error("Select Claude or Ollama");
    }
    if (typeof codeValue !== "string" || !Array.isArray(scenariosValue)) {
      throw new Error("Invalid external NOT RUN request");
    }
    const scenarios = scenariosValue.filter((value): value is string => typeof value === "string");
    if (scenarios.length !== scenariosValue.length) throw new Error("Invalid external NOT RUN scenarios");
    const manifest = this.readManifest();
    const request = externalBlockRequest(
      manifest.suite_nonce,
      crypto.randomBytes(32).toString("hex"),
      providerValue,
      codeValue,
      scenarios,
      this.capabilities,
      !this.capabilityRefreshFailed,
    );
    writeAtomicJson(this.controlPath, request);
    this.running = false;
    this.stopRequested = true;
    this.actionBinding = undefined;
    this.pendingRequest = undefined;
    this.actionSequence = 0;
    this.notice = `External ${providerValue} condition recorded as NOT RUN. The outer runner owns final cleanup proof and will close this isolated editor host.`;
    this.postSnapshot();
  }

  /** Wait 15 seconds for approval; cancellation returns undefined, rejection and timeout throw. */
  private async waitForOuterApproval(request: TestExtensionControlRequest): Promise<SuiteActionBinding | undefined> {
    const deadline = Date.now() + OUTER_APPROVAL_WAIT_MS;
    while (Date.now() < deadline) {
      if (this.cancelledRequestSha256 === request.request_sha256) return undefined;
      if (this.evidencePath && fs.existsSync(this.evidencePath)) {
        const ledger = parseEvidenceJsonl(readBoundedUtf8(this.evidencePath, EVIDENCE_BYTES_LIMIT));
        if (ledger.some((entry) => entry.event === "run.failed")) throw new Error("Outer runner rejected the suite");
        if (ledger.some((entry) => entry.event === "suite.approved")) {
          const binding = this.readSuiteActionBinding(request);
          if (binding) return binding;
        }
      }
      await delay(200);
    }
    throw new Error("Outer runner did not approve the suite within 15 seconds");
  }

  private readSuiteActionBinding(request: TestExtensionControlRequest): SuiteActionBinding | undefined {
    const target = this.actionRoot ? path.join(this.actionRoot, "suite.json") : undefined;
    if (!target || !fs.existsSync(target)) return undefined;
    const binding = readBoundedJson(target, 16 * 1024);
    const expected = canonicalSuiteRequestSha256(request);
    if (binding.protocol !== ACTION_PROTOCOL || binding.suite_nonce !== request.suite_nonce ||
        !safeNonce(binding.suite_nonce) || binding.request_sha256 !== expected) {
      throw new Error("TEST_EXTENSION_SUITE_BINDING_INVALID");
    }
    return {
      protocol: ACTION_PROTOCOL,
      suite_nonce: binding.suite_nonce,
      request_sha256: binding.request_sha256,
    };
  }

  /** Atomically request one sequenced outer action and verify its fully echoed receipt. */
  private async requestOuterAction(
    scenarioId: string,
    phase: OuterActionPhase,
    bindings: Record<string, unknown>,
  ): Promise<OuterActionReceipt> {
    if (!this.actionRoot || !this.actionBinding) throw new Error("TEST_EXTENSION_ACTION_BINDING_MISSING");
    const sequence = ++this.actionSequence;
    const actionId = `te-${crypto.randomUUID()}`;
    const request = {
      ...this.actionBinding,
      sequence,
      action_id: actionId,
      scenario_id: scenarioId,
      phase,
      ...bindings,
    };
    if (containsUndefined(request)) {
      throw new Error("TEST_EXTENSION_ACTION_BINDING_INCOMPLETE");
    }
    const basename = `${String(sequence).padStart(8, "0")}-${actionId}.json`;
    const requestPath = safeChildPath(this.actionRoot, path.join(this.actionRoot, "requests", basename), basename);
    const receiptPath = safeChildPath(this.actionRoot, path.join(this.actionRoot, "receipts", basename), basename);
    if (!fs.existsSync(path.dirname(requestPath)) || !fs.existsSync(path.dirname(receiptPath))) {
      throw new Error("TEST_EXTENSION_ACTION_CHANNEL_MISSING");
    }
    writeAtomicJson(requestPath, request);
    const deadline = Date.now() + 30_000;
    while (Date.now() < deadline) {
      if (fs.existsSync(receiptPath)) {
        const raw = readBoundedJson(receiptPath, 64 * 1024);
        for (const [key, value] of Object.entries(request)) {
          if (canonicalJsonValue(raw[key]) !== canonicalJsonValue(value)) {
            throw new Error("TEST_EXTENSION_ACTION_RECEIPT_BINDING_MISMATCH");
          }
        }
        if (raw.status !== "OK" && raw.status !== "FAILED") throw new Error("TEST_EXTENSION_ACTION_RECEIPT_INVALID");
        if (!raw.proof || typeof raw.proof !== "object" || Array.isArray(raw.proof)) {
          throw new Error("TEST_EXTENSION_ACTION_RECEIPT_INVALID");
        }
        const receipt = raw as unknown as OuterActionReceipt;
        if (receipt.status === "FAILED") throw new Error(boundedCode(receipt.code ?? "TEST_EXTENSION_ACTION_FAILED"));
        return receipt;
      }
      await delay(100);
    }
    throw new Error("TEST_EXTENSION_ACTION_RECEIPT_TIMEOUT");
  }

  private async runScenario(
    request: TestExtensionControlRequest,
    scenario: TestExtensionScenario,
  ): Promise<ScenarioResult> {
    if (scenario.kind === "stale") return this.runStaleScenario(request, scenario);
    if (scenario.kind === "corrupt") return this.runCorruptScenario(request, scenario);
    if (scenario.kind === "writer") return this.runWriterScenario(request, scenario);
    if (scenario.kind === "interrupt") return this.runInterruptRestartScenario(request, scenario);
    if (scenario.kind === "traversal") return this.runTraversalScenario(request, scenario);
    const manifest = this.readManifest();
    const expected = expectedCodewords(manifest, scenario);
    const seen = new Set<string>();
    let streamSequences: number[] = [];
    let streamOrdered = true;
    let durableReplacements = 0;
    let streamSuppressed = false;
    let attestedModel: string | undefined;
    let attestedEffort: string | undefined;
    let profileAttested = false;
    let approvalUi = false;
    let approvalDecision: "approve" | "deny" | undefined;
    let approvalStatus: string | undefined;
    let latestSnapshot: import("./webviewProtocol").ConversationSnapshot | undefined;
    let diffProof: DiffProof | undefined;
    let imageRefs = 0;
    let contextRefs = 0;
    let selectionExact = false;
    const observe = (text: string) => expected.forEach((word) => { if (text.includes(word)) seen.add(word); });
    let conversationId: string | undefined;
    try {
      const diffTarget = path.join(this.repoRoot, manifest.fixtures.diff_target);
      const baselineSha256 = scenario.kind === "diff" || scenario.kind === "timeout" ? sha256File(diffTarget) : undefined;
      const staged = await this.stageEnvelope(scenario, manifest);
      imageRefs = staged.images.length;
      contextRefs = staged.contexts.length;
      selectionExact = staged.contexts.some((item) => item.ref.kind === "selection" &&
        typeof item.ref.snapshot_ref === "string" && /^sha256:[0-9a-f]{64}$/.test(item.ref.snapshot_ref));
      if (scenario.expectedDecision) {
        const action = scenario.kind === "process" ? "Approve" : scenario.expectedDecision === "approve" ? "Accept" : "Reject";
        this.notice = `Scenario ${scenario.id} is waiting for ${action} in its normal Kaizen chat tab.`;
        this.postSnapshot();
      }
      conversationId = await this.chats.testExtensionDrive(
        providerEngine(request.provider),
        {
          model: request.model,
          ...(request.effort ? { reasoning_effort: request.effort } : {}),
          max_turns: request.max_turns,
          permission_mode: scenario.permission,
          auth_mode: request.provider === "claude" ? "subscription" : "none",
        },
        scenarioPrompt(scenario, manifest, this.python()),
        staged.contexts,
        staged.images,
      );
      const deadline = Date.now() + SCENARIO_TIMEOUT_MS;
      let terminal = false;
      let externalCode: string | undefined;
      while (Date.now() < deadline) {
        const snapshot = this.chats.testExtensionSnapshot(conversationId);
        if (!snapshot) throw new Error("TEST_EXTENSION_CHAT_SNAPSHOT_MISSING");
        latestSnapshot = snapshot;
        const observation = this.chats.testExtensionObservation(conversationId);
        if (observation) {
          streamSequences = observation.stream.sequences;
          streamOrdered = observation.stream.ordered;
          durableReplacements = observation.stream.durableReplacements;
          streamSuppressed = observation.stream.suppressed;
          attestedModel = observation.profile.model;
          attestedEffort = observation.profile.reasoning_effort;
          profileAttested = observation.profileAttested;
        }
        if (snapshot.streamingBubble?.text) {
          observe(snapshot.streamingBubble.text);
        }
        snapshot.transcript.filter((message) => message.role === "assistant").forEach((message) => observe(message.text));
        approvalUi = approvalUi || snapshot.approvals.some((approval) => approval.pending);
        for (const approval of snapshot.approvals) {
          approvalStatus = approval.status;
          if (approval.status === "approved") approvalDecision = "approve";
          if (approval.status === "denied") approvalDecision = "deny";
          const target = approval.diff?.files.find((change) => change.path === manifest.fixtures.diff_target);
          if (target) {
            diffProof = {
              path: target.path,
              beforeSha256: target.beforeSha256,
              proposedSha256: target.proposedSha256,
            };
          }
        }
        externalCode = externalCode ?? scenarioExternalCode(snapshot, observation?.turnEvents);
        if (externalCode) break;
        terminal = snapshot.conversation?.state === "idle" || snapshot.conversation?.state === "terminal";
        if (terminal) break;
        await delay(200);
      }
      if (externalCode) {
        return resultOf(scenario, request, "NOT_RUN", externalCode, streamSequences.length, streamOrdered, false, approvalUi);
      }
      if (!terminal) throw new Error("TEST_EXTENSION_SCENARIO_TIMEOUT");
      const codewordSeen = expected.every((word) => seen.has(word));
      const exactSequence = streamSequences.every((sequence, index) =>
        Number.isSafeInteger(sequence) && sequence > 0 && (index === 0 || sequence === streamSequences[index - 1] + 1));
      const streamingPassed = scenario.kind !== "text" || (
        streamSequences.length > 0 && streamOrdered && exactSequence && durableReplacements > 0 && !streamSuppressed
      );
      const profilePassed = profileAttested && attestedModel === request.model && (
        request.effort ? attestedEffort === request.effort : attestedEffort === undefined
      );
      const approvalPassed = !scenario.expectedDecision || (
        approvalUi && approvalDecision === scenario.expectedDecision
      );
      const special = specialProof(
        scenario, latestSnapshot, manifest, this.repoRoot, baselineSha256, diffProof, this.python(), approvalUi,
        approvalStatus, imageRefs, contextRefs, selectionExact,
      );
      const locator = scenarioLocatorProof(scenario, latestSnapshot, manifest, this.python());
      const passed = codewordSeen && streamingPassed && profilePassed && approvalPassed && special.passed && locator.passed;
      const proof: ScenarioProof = {
        ...(passed ? locator.fields : {}),
        profile_attested: profileAttested,
        attested_model: attestedModel,
        ...(attestedEffort ? { attested_effort: attestedEffort } : {}),
        ...(scenario.kind === "text" ? { durable_replacements: durableReplacements, stream_suppressed: streamSuppressed } : {}),
        ...(scenario.kind === "image" ? { image_refs: imageRefs } : {}),
        ...(scenario.kind === "context" ? { context_refs: contextRefs, selection_exact: selectionExact } : {}),
        ...special.fields,
      };
      return resultOf(
        scenario, request, passed ? "PASS" : "FAIL", passed ? undefined : "TEST_EXTENSION_ASSERTION_FAILED",
        streamSequences.length, streamOrdered && exactSequence, codewordSeen, approvalUi, approvalDecision, proof,
      );
    } catch (error) {
      return resultOf(
        scenario, request, "FAIL", error instanceof Error ? boundedCode(error.message) : "TEST_EXTENSION_SCENARIO_FAILED",
        streamSequences.length, streamOrdered, expected.every((word) => seen.has(word)), approvalUi, approvalDecision,
      );
    } finally {
      if (conversationId) {
        // The visible outer runner owns final whole-plane cleanup proof for these streaming scenarios.
        try { await this.chats.testExtensionClose(conversationId); } catch { /* outer runner proves final cleanup */ }
      }
      this.postSnapshot();
    }
  }

  private async runTraversalScenario(
    request: TestExtensionControlRequest,
    scenario: TestExtensionScenario,
  ): Promise<ScenarioResult> {
    let conversationId: string | undefined;
    let result: ScenarioResult;
    try {
      const manifest = this.readManifest();
      const armed = await this.requestOuterAction(
        scenario.id, "traversal.arm_zero_records", {},
      );
      requireProof(armed, { zero_record_baseline_armed: true });
      const invalidContext: StagedContext = {
        ref: { id: "te-traversal", kind: "file", source_path: "../outside.txt" },
        label: "outside.txt",
        bytes: 0,
      };
      conversationId = await this.chats.testExtensionDrive(
        providerEngine(request.provider), profileFor(request, scenario),
        scenarioPrompt(scenario, manifest, this.python()), [invalidContext],
      );
      const snapshot = this.chats.testExtensionSnapshot(conversationId);
      const denialCode = snapshot?.banner?.code;
      if (denialCode !== "DENIED_CONTEXT_INVALID" || snapshot?.conversation !== null) {
        throw new Error("TEST_EXTENSION_TRAVERSAL_CONTROLLER_DENIAL_MISSING");
      }
      const receipt = await this.requestOuterAction(
        scenario.id, "traversal.verify_zero_records", { denial_code: denialCode },
      );
      requireProof(receipt, { denial_code: denialCode, zero_records: true });
      result = resultOf(
        scenario, request, "PASS", undefined, 0, true, true, false, undefined,
        { denial_code: denialCode, zero_records: true },
      );
    } catch (error) {
      result = this.errorResult(scenario, request, error);
    }
    return this.closeAndFinalize([conversationId], result);
  }

  private async runStaleScenario(
    request: TestExtensionControlRequest,
    scenario: TestExtensionScenario,
  ): Promise<ScenarioResult> {
    const manifest = this.readManifest();
    const target = path.join(this.repoRoot, manifest.fixtures.diff_target);
    let conversationId: string | undefined;
    let result: ScenarioResult;
    try {
      conversationId = await this.chats.testExtensionDrive(
        providerEngine(request.provider), profileFor(request, scenario), scenarioPrompt(scenario, manifest, this.python()),
      );
      this.notice = "Stale diff: wait while the controlled base mutation is staged; then use the normal inline Accept twice, once per revision.";
      this.postSnapshot();
      const initial = await this.waitForExactDiff(conversationId, manifest.fixtures.diff_target);
      const receipt = await this.requestOuterAction(scenario.id, "stale.mutate_base", approvalBindings(initial));
      const mutated = proofDigest(receipt, "mutated_sha256");
      this.notice = "Stale diff revision 1 is ready. Click Accept in the normal Kaizen chat tab; wait for the refreshed revision.";
      this.postSnapshot();
      const refreshed = await this.waitForExactDiff(conversationId, manifest.fixtures.diff_target, initial);
      if (refreshed.revision !== initial.revision + 1 || refreshed.beforeSha256 !== mutated) {
        throw new Error("TEST_EXTENSION_STALE_REVISION_CHAIN_INVALID");
      }
      this.notice = `Stale diff revision ${refreshed.revision} is ready. Click Accept again in the normal Kaizen chat tab.`;
      this.postSnapshot();
      await this.waitForApprovalResolution(conversationId, refreshed, "resolved", "approved");
      const finalSnapshot = await this.waitForConversation(conversationId, (snapshot) => snapshot.conversation?.state === "idle");
      const finalSha = sha256File(target);
      const profile = this.profileProof(conversationId, request);
      const cards = finalSnapshot.cards.filter((card) => card.tool === "kaizen_propose_changes").length;
      const locator = durableLocatorProof(refreshed.sessionId, refreshed.agentRunId, refreshed.correlationId, true);
      const passed = profile.passed && cards === 1 && finalSha === refreshed.proposedSha256 && locator.passed;
      result = resultOf(
        scenario, request, passed ? "PASS" : "FAIL", passed ? undefined : "TEST_EXTENSION_STALE_ASSERTION_FAILED",
        0, true, true, true, "approve", {
          ...(passed ? locator.fields : {}),
          ...profile.fields,
          approval_status: "approved", diff_card_count: cards,
          diff_revision_refreshed: true, diff_second_accept: true,
          diff_path: refreshed.workspacePath, diff_revision: initial.revision,
          diff_refreshed_revision: refreshed.revision,
          diff_snapshot_set_sha256: initial.snapshotSetSha256,
          diff_refreshed_snapshot_set_sha256: refreshed.snapshotSetSha256,
          diff_before_sha256: initial.beforeSha256, diff_mutated_base_sha256: mutated,
          diff_refreshed_before_sha256: refreshed.beforeSha256,
          diff_proposed_sha256: refreshed.proposedSha256, diff_final_sha256: finalSha,
          diff_path_matches: refreshed.workspacePath === manifest.fixtures.diff_target,
          diff_before_matches: initial.beforeSha256 === receipt.before_sha256,
          diff_proposed_hashed: true, diff_final_matches: finalSha === refreshed.proposedSha256,
        },
      );
    } catch (error) {
      result = this.errorResult(scenario, request, error);
    }
    return this.closeAndFinalize([conversationId], result);
  }

  private async runCorruptScenario(
    request: TestExtensionControlRequest,
    scenario: TestExtensionScenario,
  ): Promise<ScenarioResult> {
    const manifest = this.readManifest();
    const target = path.join(this.repoRoot, manifest.fixtures.diff_target);
    let conversationId: string | undefined;
    let result: ScenarioResult;
    try {
      conversationId = await this.chats.testExtensionDrive(
        providerEngine(request.provider), profileFor(request, scenario), scenarioPrompt(scenario, manifest, this.python()),
      );
      const opened = await this.waitForExactDiff(conversationId, manifest.fixtures.diff_target);
      const receipt = await this.requestOuterAction(scenario.id, "corrupt.snapshot", approvalBindings(opened));
      const corrupted = proofDigest(receipt, "corrupted_sha256");
      this.notice = "Corrupt diff snapshot staged. Click Accept in the normal Kaizen chat tab; it must fail closed.";
      this.postSnapshot();
      await this.waitForApprovalResolution(
        conversationId, opened, "declined", "denied", "DENIED_APPROVAL_SNAPSHOT_INVALID",
      );
      const finalSnapshot = await this.waitForConversation(
        conversationId, (snapshot) => snapshot.conversation?.state !== "running",
      );
      const finalSha = sha256File(target);
      const profile = this.profileProof(conversationId, request);
      const cards = finalSnapshot.cards.filter((card) => card.tool === "kaizen_propose_changes").length;
      const locator = durableLocatorProof(opened.sessionId, opened.agentRunId, opened.correlationId, true);
      const corruptionChanged = corrupted !== opened.proposedSha256;
      const passed = profile.passed && cards === 1 && finalSha === opened.beforeSha256 &&
        corruptionChanged && locator.passed;
      result = resultOf(
        scenario, request, passed ? "PASS" : "FAIL", passed ? undefined : "TEST_EXTENSION_CORRUPT_ASSERTION_FAILED",
        0, true, true, true, "approve", {
          ...(passed ? locator.fields : {}),
          ...profile.fields,
          approval_status: "denied", denial_code: "DENIED_APPROVAL_SNAPSHOT_INVALID",
          diff_card_count: cards, diff_corrupt: true, diff_authoritative_denial: true,
          diff_path: opened.workspacePath, diff_revision: opened.revision,
          diff_snapshot_set_sha256: opened.snapshotSetSha256,
          diff_before_sha256: opened.beforeSha256, diff_proposed_sha256: opened.proposedSha256,
          diff_final_sha256: finalSha, diff_corrupted_artifact_sha256: corrupted,
          diff_path_matches: true, diff_before_matches: true, diff_proposed_hashed: true,
          diff_final_matches: finalSha === opened.beforeSha256,
        },
      );
    } catch (error) {
      result = this.errorResult(scenario, request, error);
    }
    return this.closeAndFinalize([conversationId], result);
  }

  private async runWriterScenario(
    request: TestExtensionControlRequest,
    scenario: TestExtensionScenario,
  ): Promise<ScenarioResult> {
    const manifest = this.readManifest();
    let holderId: string | undefined;
    let loserId: string | undefined;
    let result: ScenarioResult;
    try {
      holderId = await this.chats.testExtensionDrive(
        providerEngine(request.provider), profileFor(request, scenario), scenarioPrompt(scenario, manifest, this.python()),
      );
      const holder = await this.waitForExactDiff(holderId, manifest.fixtures.diff_target);
      const holderFields = approvalBindings(holder);
      requireProof(await this.requestOuterAction(scenario.id, "writer.holder_open", holderFields), {
        writer_lease_active: true, isolated_parked: true, holder_agent_run_id: holder.agentRunId,
        holder_session_id: holder.sessionId,
      });
      loserId = await this.chats.testExtensionPrepare(providerEngine(request.provider), profileFor(request, scenario));
      const loserToken = crypto.randomBytes(32).toString("hex");
      const loserFields = { ...holderFields, loser_conversation_id: loserId, loser_request_token: loserToken };
      requireProof(await this.requestOuterAction(scenario.id, "writer.loser_arm", loserFields), {
        holder_agent_run_id: holder.agentRunId, isolated_parked: true,
      });
      await this.chats.testExtensionPrompt(
        loserId,
        writerLoserPrompt(manifest),
        loserToken,
      );
      const loserSnapshot = await this.waitForConversation(
        loserId, (snapshot) => snapshot.banner?.code === "DENIED_WORKSPACE_WRITER_BUSY",
      );
      const verifyFields = { ...loserFields, denial_code: "DENIED_WORKSPACE_WRITER_BUSY" };
      requireProof(await this.requestOuterAction(scenario.id, "writer.loser_verify", verifyFields), {
        zero_record_delta: true, same_holder_retained: true,
      });
      this.notice = "Writer conflict proved. Click Reject on the holder's normal inline diff card to release its lease.";
      this.postSnapshot();
      await this.waitForApprovalResolution(holderId, holder, "declined", "denied");
      await this.waitForConversation(holderId, (snapshot) => snapshot.conversation?.state !== "running");
      requireProof(await this.requestOuterAction(scenario.id, "writer.holder_release", holderFields), {
        writer_lease_released: true,
      });
      const profile = this.profileProof(holderId, request);
      const conflict = loserSnapshot.banner?.code === "DENIED_WORKSPACE_WRITER_BUSY";
      const locator = durableLocatorProof(holder.sessionId, holder.agentRunId, holder.correlationId, true);
      const passed = profile.passed && conflict && locator.passed;
      result = resultOf(
        scenario, request, passed ? "PASS" : "FAIL", passed ? undefined : "TEST_EXTENSION_WRITER_ASSERTION_FAILED",
        0, true, true, true, "deny", {
          ...(passed ? locator.fields : {}),
          ...profile.fields, approval_status: "denied", denial_code: "DENIED_WORKSPACE_WRITER_BUSY",
          zero_records: true, writer_conflict_seen: conflict, writer_loser_zero_records: true,
          writer_lease_retained: true, writer_lease_released: true,
        },
      );
    } catch (error) {
      result = this.errorResult(scenario, request, error);
    }
    return this.closeAndFinalize([loserId, holderId], result);
  }

  private async runInterruptRestartScenario(
    request: TestExtensionControlRequest,
    scenario: TestExtensionScenario,
  ): Promise<ScenarioResult> {
    const manifest = this.readManifest();
    let conversationId: string | undefined;
    try {
      conversationId = await this.chats.testExtensionDrive(
        providerEngine(request.provider), profileFor(request, scenario), scenarioPrompt(scenario, manifest, this.python()),
      );
      const running = await this.waitForConversation(conversationId, (snapshot) => snapshot.conversation?.state === "running");
      const old = running.conversation;
      if (!old) throw new Error("TEST_EXTENSION_RESTART_IDENTITY_MISSING");
      this.notice = "Interrupt/restart: click the visible Stop button in this normal Kaizen chat tab now.";
      this.postSnapshot();
      await this.waitForTurnCancellation(conversationId, old.agentRunId);
      const restartReceipt = await this.requestOuterAction(scenario.id, "restart.daemon", {
        conversation_id: conversationId, session_id: old.sessionId, agent_run_id: old.agentRunId,
      });
      requireProof(restartReceipt, { old_termination_proven: true, new_boot_proven: true });
      const resumable = await this.waitForConversation(conversationId, (snapshot) =>
        snapshot.banner?.code === "DAEMON_RESTART" && snapshot.conversation?.resumable === true);
      if (resumable.conversation?.sessionId !== old.sessionId) throw new Error("TEST_EXTENSION_RESTART_SESSION_CHANGED");
      await this.chats.testExtensionPrompt(
        conversationId,
        restartContinuationPrompt(manifest),
        `te-restart-${crypto.randomUUID()}`,
      );
      const resumed = await this.waitForConversation(conversationId, (snapshot) =>
        snapshot.conversation?.sessionId === old.sessionId && snapshot.conversation.agentRunId !== old.agentRunId &&
        snapshot.conversation.state === "idle" && snapshot.conversation.continuationNotice?.mode === "reduced");
      const next = resumed.conversation;
      if (!next) throw new Error("TEST_EXTENSION_RESTART_REBIND_MISSING");
      const queueEmpty = resumed.followUpQueue.items.length === 0 && !resumed.followUpQueue.paused;
      const profile = this.profileProof(conversationId, request);
      const closedConversationId = conversationId;
      const closed = await this.chats.testExtensionClose(conversationId);
      if (!closed) throw new Error("TEST_EXTENSION_RESTART_CLOSE_UNPROVEN");
      conversationId = undefined;
      const cleanupReceipt = await this.requestOuterAction(scenario.id, "restart.cleanup", {
        conversation_id: closedConversationId,
        session_id: old.sessionId, agent_run_id: next.agentRunId, previous_agent_run_id: old.agentRunId,
      });
      requireProof(cleanupReceipt, {
        runtime_children_clean: true, zero_pending_approvals: true,
        zero_active_durable_runs: true, writer_lease_released: true,
      });
      const locator = durableLocatorProof(old.sessionId, next.agentRunId);
      const previousAgentRunId = safeOpaqueLocator(old.agentRunId);
      const passed = profile.passed && queueEmpty && locator.passed && !!previousAgentRunId;
      return resultOf(
        scenario, request, passed ? "PASS" : "FAIL", passed ? undefined : "TEST_EXTENSION_RESTART_ASSERTION_FAILED",
        0, true, true, false, undefined, {
          ...(passed ? { ...locator.fields, previous_agent_run_id: previousAgentRunId } : {}),
          ...profile.fields, interrupted: true, restart_resumable: true,
          restart_same_session: next.sessionId === old.sessionId, restart_new_run: next.agentRunId !== old.agentRunId,
          restart_reduced_fidelity: next.continuationNotice?.mode === "reduced",
          queue_empty: queueEmpty, runtime_children_clean: true, conversation_idle: true,
          pending_approvals: 0, running_cards: 0,
        },
      );
    } catch (error) {
      return this.closeAndFinalize([conversationId], this.errorResult(scenario, request, error));
    }
  }

  /** Wait for exactly one traced diff, optionally requiring a newer revision than prior. */
  private async waitForExactDiff(
    conversationId: string,
    workspacePath: string,
    prior?: ExactDiffBinding,
  ): Promise<ExactDiffBinding> {
    const deadline = Date.now() + SCENARIO_TIMEOUT_MS;
    while (Date.now() < deadline) {
      const snapshot = this.chats.testExtensionSnapshot(conversationId);
      if (!snapshot) throw new Error("TEST_EXTENSION_CHAT_SNAPSHOT_MISSING");
      throwIfExternal(snapshot, this.chats.testExtensionObservation(conversationId)?.turnEvents);
      const pending = this.chats.testExtensionPendingDiff(conversationId);
      const conversation = snapshot.conversation;
      const change = pending?.diff.fileChanges.find((item) => item.path === workspacePath);
      const before = change?.before?.sha256;
      const proposed = change?.proposed?.sha256;
      if (pending && conversation && change && before && proposed && pending.diff.fileChanges.length === 1) {
        const trace = this.chats.testExtensionObservation(conversationId)?.approvalEvents.find((event) =>
          event.marker === "open" && event.agentRunId === pending.agentRunId &&
          event.correlationId === pending.correlationId && event.revision === pending.diff.revision &&
          event.snapshotSetSha256 === pending.diff.snapshotSetSha256 && event.files.length === 1 &&
          event.files[0].path === workspacePath && event.files[0].beforeSha256 === before &&
          event.files[0].proposedSha256 === proposed);
        if (trace && (!prior || (
          pending.agentRunId === prior.agentRunId && pending.correlationId === prior.correlationId &&
          pending.diff.revision > prior.revision && trace.sequenceNo > prior.openSequenceNo
        ))) {
          return {
            conversationId, sessionId: conversation.sessionId, agentRunId: pending.agentRunId,
            correlationId: pending.correlationId, revision: pending.diff.revision,
            snapshotSetSha256: pending.diff.snapshotSetSha256, workspacePath,
            beforeSha256: before, proposedSha256: proposed, openSequenceNo: trace.sequenceNo,
          };
        }
      }
      await delay(200);
    }
    throw new Error("TEST_EXTENSION_DIFF_OPEN_TIMEOUT");
  }

  private async waitForApprovalResolution(
    conversationId: string,
    binding: ExactDiffBinding,
    marker: "resolved" | "declined",
    status: "approved" | "denied",
    code?: string,
  ): Promise<void> {
    await this.waitForConversation(conversationId, (snapshot) => {
      const approval = snapshot.approvals.find((item) =>
        item.agentRunId === binding.agentRunId && item.correlationId === binding.correlationId);
      const trace = this.chats.testExtensionObservation(conversationId)?.approvalEvents.find((event) =>
        event.agentRunId === binding.agentRunId && event.correlationId === binding.correlationId &&
        event.marker === marker && event.sequenceNo > binding.openSequenceNo && (!code || event.code === code));
      return approval?.status === status && !!trace;
    });
  }

  private async waitForTurnCancellation(conversationId: string, agentRunId: string): Promise<void> {
    await this.waitForConversation(conversationId, (snapshot) => {
      const events = this.chats.testExtensionObservation(conversationId)?.turnEvents ?? [];
      const opened = events.find((event) => event.agentRunId === agentRunId && event.marker === "open");
      const canceled = events.find((event) =>
        event.agentRunId === agentRunId && event.marker === "close_canceled" &&
        !!opened && event.sequenceNo > opened.sequenceNo);
      return snapshot.conversation?.state !== "running" && !!canceled;
    });
  }

  private async waitForConversation(
    conversationId: string,
    predicate: (snapshot: import("./webviewProtocol").ConversationSnapshot) => boolean,
  ): Promise<import("./webviewProtocol").ConversationSnapshot> {
    const deadline = Date.now() + SCENARIO_TIMEOUT_MS;
    while (Date.now() < deadline) {
      const snapshot = this.chats.testExtensionSnapshot(conversationId);
      if (!snapshot) throw new Error("TEST_EXTENSION_CHAT_SNAPSHOT_MISSING");
      throwIfExternal(snapshot, this.chats.testExtensionObservation(conversationId)?.turnEvents);
      if (predicate(snapshot)) return snapshot;
      await delay(200);
    }
    throw new Error("TEST_EXTENSION_SCENARIO_TIMEOUT");
  }

  private profileProof(conversationId: string, request: TestExtensionControlRequest): { passed: boolean; fields: ScenarioProof } {
    const observation = this.chats.testExtensionObservation(conversationId);
    const passed = observation?.profileAttested === true && observation.profile.model === request.model &&
      (request.effort ? observation.profile.reasoning_effort === request.effort : observation.profile.reasoning_effort === undefined);
    return {
      passed,
      fields: {
        profile_attested: observation?.profileAttested === true,
        attested_model: observation?.profile.model,
        ...(observation?.profile.reasoning_effort ? { attested_effort: observation.profile.reasoning_effort } : {}),
      },
    };
  }

  private errorResult(
    scenario: TestExtensionScenario,
    request: TestExtensionControlRequest,
    error: unknown,
  ): ScenarioResult {
    const rawCode = error && typeof error === "object" && "code" in error
      ? (error as { code?: unknown }).code
      : error instanceof Error ? error.message : undefined;
    const external = error instanceof ExternalScenarioBlock ? error.code : externalScenarioBlockCode(rawCode);
    return resultOf(
      scenario, request, external ? "NOT_RUN" : "FAIL",
      external ?? (error instanceof Error ? boundedCode(error.message) : "TEST_EXTENSION_SCENARIO_FAILED"),
      0, true, false, false,
    );
  }

  /** Close all conversations and demote PASS when any close cannot be proven. */
  private async closeAndFinalize(
    conversationIds: Array<string | undefined>,
    result: ScenarioResult,
  ): Promise<ScenarioResult> {
    let closed = true;
    for (const conversationId of conversationIds) {
      if (!conversationId) continue;
      try { closed = await this.chats.testExtensionClose(conversationId) && closed; } catch { closed = false; }
    }
    return closed || result.status !== "PASS"
      ? result
      : { ...result, status: "FAIL", code: "TEST_EXTENSION_CLOSE_UNPROVEN" };
  }

  private async stageEnvelope(
    scenario: TestExtensionScenario,
    manifest: TestExtensionManifest,
  ): Promise<{ contexts: StagedContext[]; images: StagedImage[] }> {
    if (scenario.kind === "image") {
      const staged = await new ImageStager(this.repoRoot).file(
        `te-${crypto.randomUUID()}`, path.join(this.repoRoot, manifest.fixtures.image),
      );
      return { contexts: [], images: [staged] };
    }
    if (scenario.kind === "context") {
      const stager = new ContextStager(this.repoRoot);
      const dirtySelection = `unsaved dirty selection codeword ${manifest.codewords.selection}`;
      const [file, selection] = await Promise.all([
        stager.file(`te-file-${crypto.randomUUID()}`, path.join(this.repoRoot, manifest.fixtures.context)),
        stager.selection(
          `te-selection-${crypto.randomUUID()}`,
          path.join(this.repoRoot, manifest.fixtures.selection),
          { start: { line: 0, character: 0 }, end: { line: 0, character: dirtySelection.length } },
          dirtySelection,
        ),
      ]);
      return { contexts: [file, selection], images: [] };
    }
    return { contexts: [], images: [] };
  }

  private async stop(): Promise<void> {
    if (this.runRoot) {
      if (this.stopping) return;
      if (this.stopRequested) return;
      if (testExtensionPlaneClosed(this.readOuterLedger())) {
        this.finishPlaneClosed();
        this.postSnapshot();
        return;
      }
      this.stopping = true;
      this.notice = "STOPPING: waiting for the bound outer runner to terminate and prove the complete isolated plane.";
      this.postSnapshot();
      try {
        const pending = this.pendingRequest;
        if (pending) this.cancelledRequestSha256 = pending.request_sha256;
        let binding = this.actionBinding;
        if (!binding && pending) {
          try { binding = this.readSuiteActionBinding(pending); } catch { /* runner will fail closed on control Stop */ }
        }
        if (binding) {
          this.actionBinding = binding;
          requireProof(await this.requestOuterAction("suite", "suite.stop", { reason: "user" }), {
            stop_accepted: true,
          });
        } else {
          if (!this.controlPath) throw new Error("TEST_EXTENSION_STOP_CONTROL_MISSING");
          const manifest = this.readManifest();
          writeAtomicJson(
            this.controlPath,
            preApprovalStopRequest(
              manifest.suite_nonce,
              crypto.randomBytes(32).toString("hex"),
              pending?.request_sha256,
            ),
          );
        }
        this.finishPlaneStopRequested();
      } finally {
        this.stopping = false;
        this.postSnapshot();
      }
    } else if (this.terminal && this.terminal.exitStatus === undefined) {
      await this.stopLauncherPlane();
    }
    this.postSnapshot();
  }

  private async stopLauncherPlane(): Promise<void> {
    if (this.stopping || this.launcherStopRequested) return;
    const runRoot = this.launcherRunRoot;
    if (!runRoot) throw new Error("TEST_EXTENSION_LAUNCHER_STOP_BINDING_UNAVAILABLE");
    this.stopping = true;
    this.notice = "STOPPING: binding the launcher request to the exact isolated run.";
    this.postSnapshot();
    try {
      const manifest = await this.waitForLauncherManifest(runRoot);
      if (this.refreshLauncherLedger(runRoot)) {
        throw new Error("TEST_EXTENSION_LAUNCHER_RUN_ALREADY_CLOSED");
      }
      const controlPath = safeChildPath(runRoot, undefined, "launcher-stop.json");
      writeExclusiveJson(
        controlPath,
        preApprovalStopRequest(manifest.suite_nonce, crypto.randomBytes(32).toString("hex")),
      );
      this.launcherStopRequested = true;
      this.cleanupState = "verifying";
      this.notice = "Stop requested for the exact isolated run. The outer runner is proving complete cleanup.";
    } finally {
      this.stopping = false;
      this.postSnapshot();
    }
  }

  private async waitForLauncherManifest(runRoot: string): Promise<TestExtensionManifest> {
    const manifestPath = safeChildPath(runRoot, undefined, "manifest.json");
    for (let attempt = 0; attempt < 100; attempt += 1) {
      if (this.terminal?.exitStatus !== undefined) {
        throw new Error("TEST_EXTENSION_LAUNCHER_TERMINAL_CLOSED");
      }
      if (fs.existsSync(manifestPath)) {
        try {
          const workspace = path.join(runRoot, "fixture-workspace");
          validatePhysicalManifestWorkspace(runRoot, workspace);
          validatePhysicalManifestFile(runRoot, manifestPath);
          return pinTestExtensionManifest(fs.readFileSync(manifestPath), runRoot, workspace).manifest;
        } catch {
          // The outer runner publishes asynchronously; retry a partial or not-yet-valid manifest.
        }
      }
      await delay(100);
    }
    throw new Error("TEST_EXTENSION_LAUNCHER_MANIFEST_TIMEOUT");
  }

  private refreshLauncherLedger(runRoot: string): boolean {
    const evidencePath = safeChildPath(runRoot, undefined, "evidence.jsonl");
    if (!fs.existsSync(evidencePath)) {
      this.ledger = [];
      return false;
    }
    try {
      if (fs.statSync(evidencePath).size > EVIDENCE_BYTES_LIMIT) throw new Error("TEST_EXTENSION_EVIDENCE_TOO_LARGE");
      this.ledger = parseEvidenceJsonl(fs.readFileSync(evidencePath, "utf-8"));
      return testExtensionPlaneClosed(this.ledger);
    } catch {
      this.notice = "Sanitized launcher evidence is temporarily unavailable";
      return false;
    }
  }

  private startLauncherPolling(): void {
    if (this.runRoot || this.launcherPoller) return;
    const tick = () => {
      const terminal = this.terminal;
      if (!terminal) {
        this.stopLauncherPolling();
        return;
      }
      if (this.launcherRunRoot && this.refreshLauncherLedger(this.launcherRunRoot)) {
        this.launcherClosedProven = true;
        this.cleanupState = "verified";
        this.notice = this.launcherStopRequested
          ? "The outer runner proved the stopped isolated test plane closed."
          : "The outer runner proved the isolated test plane closed.";
      }
      if (terminal.exitStatus !== undefined) {
        this.finishLauncherTerminal();
        return;
      }
      this.postSnapshot();
    };
    tick();
    if (this.terminal) this.launcherPoller = setInterval(tick, 500);
  }

  private stopLauncherPolling(): void {
    if (this.launcherPoller) clearInterval(this.launcherPoller);
    this.launcherPoller = undefined;
  }

  private finishLauncherTerminal(): void {
    const stopped = this.launcherStopRequested;
    const closed = this.launcherClosedProven || (
      this.launcherRunRoot ? this.refreshLauncherLedger(this.launcherRunRoot) : false
    );
    this.stopLauncherPolling();
    this.terminal = undefined;
    this.launcherRunRoot = undefined;
    this.launcherStopRequested = false;
    this.launcherClosedProven = false;
    this.cleanupState = closed ? "verified" : "unproven";
    this.notice = closed
      ? stopped
        ? "The outer runner proved the stopped isolated test plane closed."
        : "The outer runner proved the isolated test plane closed."
      : "The Test Extension terminal exited without complete-plane cleanup proof.";
    this.postSnapshot();
  }

  private readOuterLedger(): TestExtensionLedgerEntry[] {
    if (!this.evidencePath || !fs.existsSync(this.evidencePath)) return [];
    this.ledger = parseEvidenceJsonl(readBoundedUtf8(this.evidencePath, EVIDENCE_BYTES_LIMIT));
    return this.ledger;
  }

  private finishPlaneStopRequested(): void {
    this.running = false;
    this.stopRequested = true;
    this.actionBinding = undefined;
    this.pendingRequest = undefined;
    this.actionSequence = 0;
    this.cleanupState = "verifying";
    this.notice = "Stop requested. The visible outer runner owns final cleanup proof and will close this isolated editor host.";
  }

  private finishPlaneClosed(): void {
    this.running = false;
    this.stopRequested = true;
    this.actionBinding = undefined;
    this.pendingRequest = undefined;
    this.cancelledRequestSha256 = undefined;
    this.actionSequence = 0;
    this.cleanupState = "verified";
    this.notice = "The outer runner proved the isolated plane closed.";
  }

  private cleanupSnapshot(): { state: TestExtensionCleanupState; message: string } {
    const state: TestExtensionCleanupState = this.stopping ? "verifying" : this.cleanupState;
    const message = state === "active"
      ? "TEST PLANE RUNNING — Stop and cleanup verification remain available from this tab."
      : state === "verifying"
        ? "STOPPING — verifying Extension Development Host, daemon, and owned-worker cleanup."
        : state === "verified"
          ? "CLEANUP VERIFIED — the isolated EDH and daemon termination proofs passed; their owned process trees are closed."
          : state === "unproven"
            ? "CLEANUP NOT VERIFIED — do not start another test plane until the remaining processes are audited."
            : "";
    return { state, message };
  }

  private readManifest(): TestExtensionManifest {
    if (!this.runRoot) throw new Error("Missing isolated run root");
    validatePhysicalManifestWorkspace(this.runRoot, this.repoRoot);
    const manifestPath = path.join(this.runRoot, "manifest.json");
    validatePhysicalManifestFile(this.runRoot, manifestPath);
    const bytes = fs.readFileSync(manifestPath);
    if (!this.manifestSnapshot) {
      this.manifestSnapshot = pinTestExtensionManifest(bytes, this.runRoot, this.repoRoot);
    }
    return verifyPinnedTestExtensionManifest(this.manifestSnapshot, bytes);
  }

  private writeEdhReady(): void {
    if (!this.runRoot || !this.edhReadyPath) throw new Error("Missing isolated EDH ready path");
    if (fs.existsSync(this.edhReadyPath)) throw new Error("TEST_EXTENSION_EDH_READY_STALE");
    const manifest = this.readManifest();
    const snapshot = this.manifestSnapshot;
    if (!snapshot) throw new Error("TEST_EXTENSION_MANIFEST_SNAPSHOT_MISSING");
    writeAtomicJson(this.edhReadyPath, {
      v: 1,
      run_id: manifest.run_id,
      suite_nonce: manifest.suite_nonce,
      manifest_sha256: snapshot.sha256,
    });
  }

  private appendClient(value: Record<string, unknown>): void {
    if (!this.clientEvidencePath) throw new Error("Missing client evidence path");
    const allowed = new Set([
      "event", "scenario", "provider", "status", "code", "session_id", "agent_run_id", "correlation_id",
      "previous_agent_run_id", "calls_reserved", "delta_count", "ordered",
      "codeword_seen", "approval_ui", "decision", "passed", "failed", "not_run",
      "profile_attested", "attested_model", "attested_effort", "durable_replacements", "stream_suppressed",
      "image_refs", "context_refs", "selection_exact", "approval_status", "diff_card_count",
      "diff_path_matches", "diff_before_matches", "diff_proposed_hashed", "diff_final_matches",
      "tool_card_count", "tool_status", "tool_request_matches", "tool_result_matches",
      "tool_zero_execution", "tool_zero_stdout", "process_exit_code", "process_stdout_bytes",
      "process_request_sha256", "process_stdout_sha256", "tool_decision_approved", "tool_output_complete",
      "denial_code", "invariant_denial_code", "zero_records", "conversation_idle", "pending_approvals", "running_cards",
      "diff_revision_refreshed", "diff_corrupt", "diff_second_accept", "diff_authoritative_denial",
      "diff_path", "diff_revision", "diff_refreshed_revision", "diff_snapshot_set_sha256",
      "diff_refreshed_snapshot_set_sha256", "diff_before_sha256", "diff_mutated_base_sha256",
      "diff_refreshed_before_sha256", "diff_proposed_sha256", "diff_final_sha256",
      "diff_corrupted_artifact_sha256", "writer_conflict_seen", "writer_loser_zero_records",
      "writer_lease_retained", "writer_lease_released", "interrupted", "restart_resumable",
      "restart_same_session", "restart_new_run", "restart_reduced_fidelity", "queue_empty",
      "runtime_children_clean",
    ]);
    // The evidence schema allowlists top-level keys; current values are flat JSON scalars.
    if (Object.keys(value).some((key) => !allowed.has(key))) throw new Error("Unsafe client evidence field");
    fs.appendFileSync(this.clientEvidencePath, JSON.stringify({ v: 1, ...value }) + "\n", { encoding: "utf-8" });
  }

  private postSnapshot(): void {
    const launcherActive = !this.runRoot && !!this.terminal && this.terminal.exitStatus === undefined;
    void this.panel?.webview.postMessage({
      type: "snapshot",
      connected: !!this.runRoot,
      launcher: testExtensionLauncherControlState(
        launcherActive,
        !!this.launcherRunRoot && !this.launcherClosedProven,
        this.launcherStopRequested,
        !this.runRoot && (this.stopping || this.launcherClosedProven),
      ),
      running: this.running,
      stopping: this.stopping,
      capabilities: this.capabilities,
      readiness: {
        claude: testExtensionProviderReadiness("claude", this.capabilities, !this.capabilityRefreshFailed),
        ollama: testExtensionProviderReadiness("ollama", this.capabilities, !this.capabilityRefreshFailed),
      },
      controls: testExtensionControlState(this.running, this.stopping, this.stopRequested),
      cleanup: this.cleanupSnapshot(),
      scenarios: TEST_EXTENSION_SCENARIOS,
      ledger: this.ledger,
      evidencePath: this.evidencePath ?? this.launcherEvidencePath,
      notice: this.notice,
      limits: { maxSimultaneousTurns: 2, maxWallMinutes: 30, providerRetries: 0 },
    });
  }
}

function asSelection(value: unknown): TestExtensionSelection {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid Test Extension selection");
  const raw = value as Record<string, unknown>;
  return {
    provider: raw.provider === "ollama" ? "ollama" : "claude",
    model: typeof raw.model === "string" ? raw.model : "",
    ...(typeof raw.effort === "string" && raw.effort ? { effort: raw.effort } : {}),
    maxTurns: Number(raw.maxTurns),
    callCeiling: Number(raw.callCeiling),
    scenarios: Array.isArray(raw.scenarios) ? raw.scenarios.filter((item): item is string => typeof item === "string") : [],
  };
}

/** Resolve an absolute run root, rejecting only the local Windows system-drive root; UNC roots remain valid. */
function safeRunRoot(value: string | undefined): string | undefined {
  if (!value || !path.isAbsolute(value)) return undefined;
  const resolved = path.resolve(value);
  if (process.platform === "win32") {
    const systemDrive = (process.env.SystemDrive ?? "C:").replace(/[\\/]+$/, "").toLowerCase();
    if (path.parse(resolved).root.replace(/[\\/]+$/, "").toLowerCase() === systemDrive) return undefined;
  }
  return resolved;
}

/** Derive the isolated test-extension plane and reject only a local Windows system-drive root, not UNC roots. */
function testExtensionLauncherPlaneBase(sourceRoot: string): string {
  const parent = process.env.DEVROOT ? path.resolve(process.env.DEVROOT) : path.dirname(path.resolve(sourceRoot));
  const base = path.join(parent, "test-extension");
  if (process.platform === "win32") {
    const systemDrive = (process.env.SystemDrive ?? "C:").replace(/[\\/]+$/, "").toLowerCase();
    if (path.parse(base).root.replace(/[\\/]+$/, "").toLowerCase() === systemDrive) {
      throw new Error("TEST_EXTENSION_LAUNCHER_SYSTEM_DRIVE");
    }
  }
  return base;
}

function createTestExtensionRunId(): string {
  const stamp = `${new Date().toISOString().slice(0, 19).replace(/[-:]/g, "")}Z`;
  return `te-${stamp}-${crypto.randomBytes(4).toString("hex")}`;
}

/** Resolve an absolute child and reject every escape from the supplied root. */
function safeChildPath(root: string, supplied: string | undefined, fallback: string): string {
  const candidate = path.resolve(supplied ?? path.join(root, fallback));
  const relative = path.relative(root, candidate);
  if (relative.startsWith("..") || path.isAbsolute(relative)) throw new Error("Test Extension path escaped its run root");
  return candidate;
}

/** Publish JSON by exclusive temp write and atomic rename, cleaning failures best-effort. */
function writeAtomicJson(target: string, value: unknown): void {
  const temporary = `${target}.${process.pid}.${crypto.randomBytes(6).toString("hex")}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(value) + "\n", { encoding: "utf-8", flag: "wx" });
  try {
    fs.renameSync(temporary, target);
  } catch (error) {
    try { fs.unlinkSync(temporary); } catch { /* best effort for a disposable run root */ }
    throw error;
  }
}

/** Exclusively create, fsync, and clean a partial JSON file on failure. */
function writeExclusiveJson(target: string, value: unknown): void {
  const bytes = Buffer.from(JSON.stringify(value) + "\n", "utf-8");
  let descriptor: number | undefined;
  try {
    descriptor = fs.openSync(target, "wx", 0o600);
    fs.writeFileSync(descriptor, bytes);
    fs.fsyncSync(descriptor);
  } catch (error) {
    if (descriptor !== undefined) {
      try { fs.closeSync(descriptor); } catch { /* best effort before removing a partial exclusive file */ }
      descriptor = undefined;
      try { fs.unlinkSync(target); } catch { /* the runner may already own the path */ }
    }
    throw error;
  } finally {
    if (descriptor !== undefined) fs.closeSync(descriptor);
  }
}

function readBoundedJson(target: string, maximum: number): Record<string, unknown> {
  const bytes = fs.readFileSync(target);
  if (bytes.length < 3 || bytes.length > maximum) throw new Error("TEST_EXTENSION_CONTROL_JSON_INVALID");
  const value = JSON.parse(bytes.toString("utf-8")) as unknown;
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("TEST_EXTENSION_CONTROL_JSON_INVALID");
  return value as Record<string, unknown>;
}

function readBoundedUtf8(target: string, maximum: number): string {
  const bytes = fs.readFileSync(target);
  if (bytes.length > maximum) throw new Error("TEST_EXTENSION_EVIDENCE_TOO_LARGE");
  return bytes.toString("utf-8");
}

function safeNonce(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

function canonicalSuiteRequestSha256(request: TestExtensionControlRequest): string {
  const { request_sha256: _omitted, ...body } = request;
  return canonicalRequestSha256(body);
}

/** Encode deterministic sorted-key JSON, spelling undefined as the literal `undefined`. */
function canonicalJsonValue(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJsonValue).join(",")}]`;
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonicalJsonValue(record[key])}`).join(",")}}`;
  }
  return value === undefined ? "undefined" : JSON.stringify(value);
}

function containsUndefined(value: unknown, ancestors = new Set<object>()): boolean {
  if (value === undefined) return true;
  if (!value || typeof value !== "object") return false;
  if (ancestors.has(value)) return true;
  ancestors.add(value);
  const nested = (Array.isArray(value) ? value : Object.values(value)).some((item) => containsUndefined(item, ancestors));
  ancestors.delete(value);
  return nested;
}

function expectedCodewords(manifest: TestExtensionManifest, scenario: TestExtensionScenario): string[] {
  if (scenario.kind === "text") return [manifest.codewords.text];
  if (scenario.kind === "image") return [manifest.codewords.image];
  if (scenario.kind === "context") return [manifest.codewords.context, manifest.codewords.selection];
  return [];
}

function specialProof(
  scenario: TestExtensionScenario,
  snapshot: import("./webviewProtocol").ConversationSnapshot | undefined,
  manifest: TestExtensionManifest,
  repoRoot: string,
  baselineSha256: string | undefined,
  diffProof: DiffProof | undefined,
  python: string,
  approvalUi: boolean,
  approvalStatus: string | undefined,
  imageRefs: number,
  contextRefs: number,
  selectionExact: boolean,
): { passed: boolean; fields: ScenarioProof } {
  if (!snapshot) return { passed: false, fields: {} };
  switch (scenario.kind) {
    case "image":
      return { passed: imageRefs >= 1, fields: {} };
    case "context":
      return { passed: contextRefs >= 2 && selectionExact, fields: {} };
    case "control": {
      const requireInvariant = scenario.id === "claude-plan-controls";
      const fields = planProcessControlProof(snapshot.cards, python, requireInvariant);
      return {
        passed: fields.tool_card_count === (requireInvariant ? 2 : 1) && fields.tool_status === "blocked" &&
          fields.tool_request_matches && fields.tool_zero_execution && fields.tool_zero_stdout &&
          fields.process_stdout_bytes === 0 && fields.denial_code === "MODE_CEILING:exec" &&
          (!requireInvariant || fields.invariant_denial_code === "INV_GIT_PUSH"),
        fields,
      };
    }
    case "diff": {
      const cards = snapshot.cards.filter((card) => card.tool === "kaizen_propose_changes");
      const pathMatches = diffProof?.path === manifest.fixtures.diff_target;
      const beforeMatches = !!baselineSha256 && diffProof?.beforeSha256 === baselineSha256;
      const proposedHashed = typeof diffProof?.proposedSha256 === "string" && /^[0-9a-f]{64}$/.test(diffProof.proposedSha256);
      const finalSha256 = baselineSha256 ? sha256File(path.join(repoRoot, manifest.fixtures.diff_target)) : undefined;
      const finalMatches = scenario.expectedDecision === "approve"
        ? proposedHashed && finalSha256 === diffProof?.proposedSha256
        : !!baselineSha256 && finalSha256 === baselineSha256;
      const fields: ScenarioProof = {
        approval_status: approvalStatus,
        diff_card_count: cards.length,
        diff_path_matches: pathMatches,
        diff_before_matches: beforeMatches,
        diff_proposed_hashed: proposedHashed,
        diff_final_matches: finalMatches,
      };
      return {
        passed: cards.length === 1 && approvalUi && pathMatches && beforeMatches && proposedHashed && finalMatches,
        fields,
      };
    }
    case "process": {
      const cards = snapshot.cards.filter((card) => card.tool === "kaizen_run_process");
      const card = cards.length === 1 ? cards[0] : undefined;
      const process = card?.process;
      const requestMatches = !!process && path.resolve(process.executable) === path.resolve(python) &&
        process.argv.length === 2 && process.argv[0] === "-c" && process.argv[1] === "print('TE_PROCESS_OK')" &&
        process.cwd === "." && process.timeoutMs === 5000;
      const resultMatches = process?.exitCode === 0 && process.stdoutBytes === Buffer.byteLength("TE_PROCESS_OK\n") &&
        process.stdoutSha256 === crypto.createHash("sha256").update("TE_PROCESS_OK\n").digest("hex");
      const decisionApproved = process?.decision === "approved";
      const outputComplete = process?.truncated === false;
      const fields: ScenarioProof = {
        approval_status: approvalStatus,
        tool_card_count: cards.length,
        tool_status: card?.status ?? "missing",
        tool_request_matches: requestMatches,
        tool_result_matches: resultMatches,
        tool_decision_approved: decisionApproved,
        tool_output_complete: outputComplete,
        ...(process?.exitCode !== undefined ? { process_exit_code: process.exitCode } : {}),
        process_stdout_bytes: process?.stdoutBytes ?? 0,
        process_request_sha256: process
          ? processRequestSha256(process.executable, process.argv, process.cwd, process.timeoutMs)
          : processRequestSha256(".", [], ".", 1),
        ...(process?.stdoutSha256 ? { process_stdout_sha256: process.stdoutSha256 } : {}),
      };
      return {
        passed: approvalUi && card?.status === "ok" && requestMatches && resultMatches && decisionApproved && outputComplete,
        fields,
      };
    }
    case "timeout": {
      const cards = snapshot.cards.filter((card) => card.tool === "kaizen_propose_changes");
      const pathMatches = diffProof?.path === manifest.fixtures.diff_target;
      const beforeMatches = !!baselineSha256 && diffProof?.beforeSha256 === baselineSha256;
      const proposedHashed = typeof diffProof?.proposedSha256 === "string" && /^[0-9a-f]{64}$/.test(diffProof.proposedSha256);
      const finalMatches = !!baselineSha256 && sha256File(path.join(repoRoot, manifest.fixtures.diff_target)) === baselineSha256;
      const timedOut = snapshot.approvals.some((approval) => approval.status === "timed_out");
      const fields: ScenarioProof = {
        approval_status: timedOut ? "timed_out" : approvalStatus,
        diff_card_count: cards.length,
        diff_path_matches: pathMatches,
        diff_before_matches: beforeMatches,
        diff_proposed_hashed: proposedHashed,
        diff_final_matches: finalMatches,
      };
      return { passed: cards.length === 1 && approvalUi && pathMatches && beforeMatches && proposedHashed && finalMatches && timedOut, fields };
    }
    case "cleanup": {
      const pendingApprovals = snapshot.approvals.filter((approval) => approval.pending).length;
      const runningCards = snapshot.cards.filter((card) => card.status === "running").length;
      const idle = snapshot.conversation?.state === "idle";
      return {
        passed: idle && pendingApprovals === 0 && runningCards === 0,
        fields: { conversation_idle: idle, pending_approvals: pendingApprovals, running_cards: runningCards },
      };
    }
    default: return { passed: true, fields: {} };
  }
}

function profileFor(
  request: TestExtensionControlRequest,
  scenario: TestExtensionScenario,
): import("./webviewProtocol").ProfileSelection {
  return {
    model: request.model,
    ...(request.effort ? { reasoning_effort: request.effort } : {}),
    max_turns: request.max_turns,
    permission_mode: scenario.permission,
    auth_mode: request.provider === "claude" ? "subscription" : "none",
  };
}

function approvalBindings(binding: ExactDiffBinding): Record<string, unknown> {
  return {
    conversation_id: binding.conversationId,
    session_id: binding.sessionId,
    agent_run_id: binding.agentRunId,
    correlation_id: binding.correlationId,
    revision: binding.revision,
    snapshot_set_sha256: binding.snapshotSetSha256,
    workspace_path: binding.workspacePath,
    before_sha256: binding.beforeSha256,
    proposed_sha256: binding.proposedSha256,
  };
}

function scenarioLocatorProof(
  scenario: TestExtensionScenario,
  snapshot: import("./webviewProtocol").ConversationSnapshot | undefined,
  manifest: TestExtensionManifest,
  python: string,
): ScenarioLocatorProof {
  const conversation = snapshot?.conversation;
  if (!snapshot || !conversation) return { passed: false, fields: {} };
  if (scenario.kind === "diff" || scenario.kind === "timeout") {
    const expectedStatus = scenario.kind === "timeout"
      ? "timed_out"
      : scenario.expectedDecision === "approve" ? "approved" : "denied";
    const approvals = snapshot.approvals.filter((approval) =>
      approval.agentRunId === conversation.agentRunId && approval.status === expectedStatus &&
      approval.diff?.files.some((file) => file.path === manifest.fixtures.diff_target));
    if (approvals.length !== 1) return { passed: false, fields: {} };
    const approval = approvals[0];
    const cards = snapshot.cards.filter((card) =>
      card.tool === "kaizen_propose_changes" && card.agentRunId === approval.agentRunId);
    return cards.length === 1
      ? durableLocatorProof(conversation.sessionId, approval.agentRunId, approval.correlationId, true)
      : { passed: false, fields: {} };
  }
  if (scenario.kind === "process") {
    const cards = snapshot.cards.filter((card) => {
      const process = card.process;
      return card.tool === "kaizen_run_process" && card.agentRunId === conversation.agentRunId && !!process &&
        path.resolve(process.executable) === path.resolve(python) && process.argv.length === 2 &&
        process.argv[0] === "-c" && process.argv[1] === "print('TE_PROCESS_OK')" &&
        process.cwd === "." && process.timeoutMs === 5000;
    });
    if (cards.length !== 1) return { passed: false, fields: {} };
    const card = cards[0];
    const approvals = snapshot.approvals.filter((approval) =>
      approval.agentRunId === card.agentRunId && approval.status === "approved");
    return approvals.length === 1
      ? durableLocatorProof(conversation.sessionId, card.agentRunId, approvals[0].correlationId, true)
      : { passed: false, fields: {} };
  }
  if (scenario.kind === "control") {
    const cards = snapshot.cards.filter((card) => {
      const process = card.process;
      return card.tool === "kaizen_run_process" && card.agentRunId === conversation.agentRunId && !!process &&
        path.resolve(process.executable) === path.resolve(python) && process.argv.length === 2 &&
        process.argv[0] === "-c" && process.argv[1] === "print('MUST_NOT_RUN')" &&
        process.cwd === "." && process.timeoutMs === 5000;
    });
    return cards.length === 1
      ? durableLocatorProof(conversation.sessionId, cards[0].agentRunId, cards[0].correlationId, true)
      : { passed: false, fields: {} };
  }
  return durableLocatorProof(conversation.sessionId, conversation.agentRunId);
}

/** Validate durable session/run locators and an optional or required correlation locator. */
function durableLocatorProof(
  sessionId: unknown,
  agentRunId: unknown,
  correlationId?: unknown,
  correlationRequired = false,
): ScenarioLocatorProof {
  const session = safeOpaqueLocator(sessionId);
  const run = safeOpaqueLocator(agentRunId);
  const correlation = correlationId === undefined ? undefined : safeOpaqueLocator(correlationId);
  const correlationValid = correlationId === undefined || !!correlation;
  const passed = !!session && !!run && correlationValid && (!correlationRequired || !!correlation);
  return {
    passed,
    fields: passed ? {
      session_id: session,
      agent_run_id: run,
      ...(correlation ? { correlation_id: correlation } : {}),
    } : {},
  };
}

/** Return a bounded opaque locator containing only the wire-safe character set. */
function safeOpaqueLocator(value: unknown): string | undefined {
  return typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/.test(value) ? value : undefined;
}

function proofDigest(receipt: OuterActionReceipt, key: string): string {
  const value = receipt.proof[key];
  if (typeof value !== "string" || !/^[0-9a-f]{64}$/.test(value)) {
    throw new Error("TEST_EXTENSION_ACTION_PROOF_INVALID");
  }
  return value;
}

function requireProof(receipt: OuterActionReceipt, expected: Record<string, unknown>): void {
  for (const [key, value] of Object.entries(expected)) {
    if (canonicalJsonValue(receipt.proof[key]) !== canonicalJsonValue(value)) {
      throw new Error("TEST_EXTENSION_ACTION_PROOF_INVALID");
    }
  }
}

class ExternalScenarioBlock extends Error {
  constructor(readonly code: string) {
    super(code);
    this.name = "ExternalScenarioBlock";
  }
}

function scenarioExternalCode(
  snapshot: import("./webviewProtocol").ConversationSnapshot,
  turnEvents: readonly { code?: string; status?: string }[] = [],
): string | undefined {
  for (const value of [
    snapshot.banner?.code,
    snapshot.conversation?.terminalState,
    ...turnEvents.flatMap((event) => [event.code, event.status]),
  ]) {
    const code = externalScenarioBlockCode(value);
    if (code) return code;
  }
  return undefined;
}

function throwIfExternal(
  snapshot: import("./webviewProtocol").ConversationSnapshot,
  turnEvents: readonly { code?: string; status?: string }[] = [],
): void {
  const code = scenarioExternalCode(snapshot, turnEvents);
  if (code) throw new ExternalScenarioBlock(code);
}

function sha256File(target: string): string {
  return crypto.createHash("sha256").update(fs.readFileSync(target)).digest("hex");
}

function resultOf(
  scenario: TestExtensionScenario,
  request: TestExtensionControlRequest,
  status: ScenarioResult["status"],
  code: string | undefined,
  deltaCount: number,
  ordered: boolean,
  codewordSeen: boolean,
  approvalUi: boolean,
  decision?: "approve" | "deny",
  proof: ScenarioProof = {},
): ScenarioResult {
  return {
    scenario: scenario.id,
    provider: request.provider,
    status,
    ...(code ? { code: boundedCode(code) } : {}),
    calls_reserved: request.max_turns * scenarioCallMultiplier(scenario.id),
    delta_count: deltaCount,
    ordered,
    codeword_seen: codewordSeen,
    approval_ui: approvalUi,
    ...(decision ? { decision } : {}),
    ...proof,
  };
}

function boundedCode(value: string): string {
  const normalized = value.toUpperCase().replace(/[^A-Z0-9_]+/g, "_").replace(/^_+|_+$/g, "");
  return normalized.slice(0, 128) || "TEST_EXTENSION_SCENARIO_FAILED";
}

const delay = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

function panelHtml(webview: vscode.Webview): string {
  const nonce = crypto.randomBytes(16).toString("hex");
  const csp = `default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}'`;
  return `<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="${csp}"><title>Kaizen Test Extension</title>
<style nonce="${nonce}">[hidden]{display:none!important}body{font-family:var(--vscode-font-family);color:var(--vscode-foreground);background:var(--vscode-editor-background);margin:0;padding:20px;max-width:1100px}h1{font-size:20px}h2{font-size:14px;margin-top:22px}button,select,input{font:inherit;color:var(--vscode-input-foreground);background:var(--vscode-input-background);border:1px solid var(--vscode-input-border);padding:6px 9px}button{background:var(--vscode-button-background);color:var(--vscode-button-foreground);border:0;cursor:pointer}button.secondary{background:var(--vscode-button-secondaryBackground);color:var(--vscode-button-secondaryForeground)}button.stop{background:var(--vscode-statusBarItem-errorBackground,#a1260d);color:var(--vscode-statusBarItem-errorForeground,#fff);border:1px solid var(--vscode-inputValidation-errorBorder,#be1100);font-weight:600}button:disabled{opacity:.5}.grid{display:grid;grid-template-columns:repeat(4,minmax(130px,1fr));gap:10px}.field{display:flex;flex-direction:column;gap:4px}.scenarios{display:grid;grid-template-columns:repeat(2,minmax(240px,1fr));gap:5px}.scenario{display:flex;gap:7px}.limits,.notice{color:var(--vscode-descriptionForeground);font-size:12px}.warning{color:var(--vscode-errorForeground);white-space:pre-wrap}.actions{display:flex;gap:8px;margin-top:16px}.cleanup{border:1px solid var(--vscode-panel-border);padding:9px;margin:12px 0;font-size:12px}.cleanup.verified{border-color:var(--vscode-testing-iconPassed);color:var(--vscode-testing-iconPassed)}.cleanup.unproven{border-color:var(--vscode-testing-iconFailed);color:var(--vscode-testing-iconFailed)}.cleanup.verifying{border-color:var(--vscode-focusBorder)}.ledger{font-family:var(--vscode-editor-font-family);font-size:12px;border:1px solid var(--vscode-panel-border);padding:10px;min-height:120px;max-height:280px;overflow:auto;white-space:pre-wrap}.approval{border:1px solid var(--vscode-focusBorder);padding:10px;margin:8px 0}@media(max-width:700px){.grid,.scenarios{grid-template-columns:1fr}}</style></head><body>
<h1>Kaizen Test Extension</h1><p class="notice" id="mode"></p><p class="limits" id="limits"></p><p class="warning" id="notice"></p><p class="cleanup" id="cleanup-status" hidden></p><section id="launcher" class="actions"><button id="launch">Start isolated test plane in a visible terminal</button><button id="launch-stop" class="stop" hidden>Stop and verify cleanup</button></section><section id="controls" hidden><div class="actions"><button id="stop" class="stop">Stop and verify cleanup</button></div><p class="notice" id="readiness"></p><div class="grid"><label class="field">Provider<select id="provider"><option value="claude">Claude subscription</option><option value="ollama">Ollama baseline</option></select></label><label class="field">Discovered model<select id="model"></select></label><label class="field">Effort<select id="effort"></select></label><label class="field">Max turns per scenario<input id="turns" type="number" min="1" max="32" value="8"></label><label class="field">Suite call ceiling<input id="ceiling" type="number" min="1" max="256" value="256"></label></div><h2>Scenarios</h2><div class="scenarios" id="scenarios"></div><div class="actions"><button id="start">Start selected suite</button><button id="notrun" class="secondary" hidden>Record selected provider NOT RUN</button><button id="refresh" class="secondary">Refresh catalog</button></div></section><p class="notice">Use Accept/Reject for diff approvals and Approve for the harmless process card in the normal Kaizen chat tabs.</p><h2>Sanitized evidence</h2><p class="limits" id="evidence"></p><div class="ledger" id="ledger"></div>
<script nonce="${nonce}">
const vscode=acquireVsCodeApi();
let snapshot={capabilities:[],scenarios:[],ledger:[],readiness:{},controls:{}};
const selectedScenarios=new Map();
const selectedModels=new Map();
let renderedScenarioProvider;
let renderedModelProvider;
const $=id=>document.getElementById(id);
function engine(){const id=$("provider").value==="claude"?"claude":"local_llm";return snapshot.capabilities.find(item=>item.id===id)}
function reconcileModelSelection(available,previous,defaultModel){
  const ids=new Set(available);
  if(previous)return {selected:previous,unavailable:!ids.has(previous)};
  if(defaultModel&&ids.has(defaultModel))return {selected:defaultModel,unavailable:false};
  return {selected:"",unavailable:false};
}
function rememberModelSelection(){
  if(renderedModelProvider)selectedModels.set(renderedModelProvider,$("model").value);
}
function renderModels(){
  rememberModelSelection();
  const provider=$("provider").value,capability=engine(),box=$("model"),models=capability?.models??[];
  const choice=reconcileModelSelection(models.map(model=>model.id),selectedModels.get(provider),capability?.default_model);
  const options=[new Option(models.length?"Select a model":"No models discovered","")].concat(models.map(model=>new Option(model.label,model.id)));
  if(choice.unavailable){const stale=new Option("Unavailable: "+choice.selected,choice.selected,true,true);stale.disabled=true;options.push(stale)}
  box.replaceChildren(...options);box.value=choice.selected;
  selectedModels.set(provider,choice.selected);renderedModelProvider=provider;
  renderEfforts();
}
function renderEfforts(){
  const capability=engine(),selected=capability?.models?.find(model=>model.id===$("model").value),box=$("effort"),values=selected?.reasoning_efforts??[],prior=box.value;
  box.replaceChildren(...(values.length?values.map(value=>new Option(value,value)):[new Option(selected?"Not available":"Select a model","")]));
  if(values.includes(prior))box.value=prior;
  else if(selected?.default_effort&&values.includes(selected.default_effort))box.value=selected.default_effort;
}
function reconcileScenarioSelection(available,previous){
  const selected=new Set(previous??available);
  return [...new Set(available)].filter(scenario=>selected.has(scenario));
}
function checkedScenarioIds(){return [...document.querySelectorAll("#scenarios input:checked")].map(input=>input.value)}
function rememberScenarioSelection(){
  if(renderedScenarioProvider)selectedScenarios.set(renderedScenarioProvider,checkedScenarioIds());
}
function renderScenarios(){
  rememberScenarioSelection();
  const provider=$("provider").value,box=$("scenarios"),definitions=snapshot.scenarios.filter(scenario=>scenario.provider===provider),available=definitions.map(scenario=>scenario.id);
  const selected=new Set(reconcileScenarioSelection(available,selectedScenarios.get(provider)));
  selectedScenarios.set(provider,[...selected]);
  box.replaceChildren(...definitions.map(scenario=>{
    const label=document.createElement("label"),input=document.createElement("input");
    label.className="scenario";input.type="checkbox";input.value=scenario.id;input.checked=selected.has(scenario.id);
    input.onchange=()=>{rememberScenarioSelection();applyControlState()};
    label.append(input,document.createTextNode(scenario.label));return label;
  }));
  renderedScenarioProvider=provider;
}
function currentReadiness(){return snapshot.readiness?.[$("provider").value]}
function renderReadiness(){
  const readiness=currentReadiness(),box=$("readiness");
  box.textContent=readiness?.message??"Provider readiness is unavailable. Refresh catalog.";
  box.className=readiness?.state==="ready"?"notice":"warning";
  box.dataset.state=readiness?.state??"unavailable";
}
function selectionReady(){
  if(currentReadiness()?.canStart!==true)return false;
  const capability=engine(),model=capability?.models?.find(item=>item.id===$("model").value);
  if(!model)return false;
  const efforts=model.reasoning_efforts??[];
  if(efforts.length>0&&!efforts.includes($("effort").value))return false;
  const turns=Number($("turns").value),ceiling=Number($("ceiling").value);
  return Number.isSafeInteger(turns)&&turns>=1&&turns<=32&&Number.isSafeInteger(ceiling)&&ceiling>=1&&ceiling<=256&&checkedScenarioIds().length>0;
}
function applyControlState(){
  const locked=!!snapshot.controls?.workloadLocked;
  const externalCode=currentReadiness()?.externalCode;
  for(const id of ["provider","turns","ceiling","refresh"])$(id).disabled=locked;
  const capability=engine(),model=capability?.models?.find(item=>item.id===$("model").value);
  $("model").disabled=locked||(capability?.models?.length??0)===0;
  $("effort").disabled=locked||(model?.reasoning_efforts?.length??0)===0;
  for(const input of document.querySelectorAll("#scenarios input"))input.disabled=locked;
  $("start").disabled=!!snapshot.controls?.startLocked||!selectionReady();
  $("notrun").hidden=!externalCode;
  $("notrun").disabled=locked||!externalCode||checkedScenarioIds().length===0;
  $("stop").disabled=!!snapshot.controls?.stopLocked;
}
function render(){
  const connected=!!snapshot.connected;
  $("launcher").hidden=connected;$("controls").hidden=!connected;
  $("launch").hidden=!!snapshot.launcher?.active;
  $("launch").disabled=snapshot.cleanup?.state==="unproven";
  $("launch-stop").hidden=!snapshot.launcher?.active;
  $("launch-stop").disabled=!snapshot.launcher?.stopAvailable;
  const cleanup=$("cleanup-status"),cleanupState=snapshot.cleanup?.state??"idle";
  cleanup.hidden=cleanupState==="idle";cleanup.className="cleanup "+cleanupState;cleanup.textContent=snapshot.cleanup?.message??"";
  const stopping=cleanupState==="verifying";
  $("launch-stop").textContent=stopping?"Stopping and verifying cleanup…":"Stop and verify cleanup";
  $("stop").textContent=stopping?"Stopping and verifying cleanup…":"Stop and verify cleanup";
  $("mode").textContent=connected?"Isolated child EDH: this tab drives the normal Kaizen chat controller and protocol.":"Launcher tab: Start creates a fresh non-system-drive daemon and Extension Development Host.";
  $("limits").textContent="Maximum "+(snapshot.limits?.maxSimultaneousTurns??2)+" simultaneous provider turns; "+(snapshot.limits?.maxWallMinutes??30)+"-minute wall clock; provider retries "+(snapshot.limits?.providerRetries??0)+".";
  $("notice").textContent=snapshot.notice??"";
  $("evidence").textContent=snapshot.evidencePath?"Evidence: "+snapshot.evidencePath:"Evidence appears in the child EDH after preflight.";
  renderModels();renderScenarios();renderReadiness();applyControlState();
  $("ledger").textContent=(snapshot.ledger??[]).map(entry=>"#"+entry.seq+" "+entry.status+" "+entry.event+(entry.scenario?" "+entry.scenario:"")+(entry.code?" ["+entry.code+"]":"")).join("\\n")||"No evidence yet.";
}
window.addEventListener("message",event=>{if(event.data?.type==="snapshot"){snapshot=event.data;render()}});
$("provider").onchange=()=>{renderModels();renderScenarios();renderReadiness();applyControlState()};
$("model").onchange=()=>{selectedModels.set($("provider").value,$("model").value);renderEfforts();applyControlState()};
$("effort").onchange=applyControlState;$("turns").oninput=applyControlState;$("ceiling").oninput=applyControlState;
$("launch").onclick=()=>vscode.postMessage({type:"launch"});
$("launch-stop").onclick=()=>vscode.postMessage({type:"stop"});
$("refresh").onclick=()=>vscode.postMessage({type:"refresh"});
$("stop").onclick=()=>vscode.postMessage({type:"stop"});
$("notrun").onclick=()=>vscode.postMessage({type:"external-block",provider:$("provider").value,code:currentReadiness()?.externalCode,scenarios:checkedScenarioIds()});
$("start").onclick=()=>vscode.postMessage({type:"start",selection:{provider:$("provider").value,model:$("model").value,effort:$("effort").value,maxTurns:Number($("turns").value),callCeiling:Number($("ceiling").value),scenarios:checkedScenarioIds()}});
vscode.postMessage({type:"ready"});
</script></body></html>`;
}
