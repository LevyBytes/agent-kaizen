/** Core test-extension contracts; the sanctioned runner pins source and compiled extension roots. */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { test } from "node:test";

import {
  TEST_EXTENSION_SCENARIOS,
  TestExtensionSelectionError,
  createTestExtensionReadyGate,
  externalBlockRequest,
  externalScenarioBlockCode,
  installWebviewHtml,
  planProcessControlProof,
  parseEvidenceJsonl,
  preApprovalStopRequest,
  pinTestExtensionManifest,
  providerScenarios,
  reconcileModelSelection,
  reconcileScenarioSelection,
  reservedProviderCalls,
  restartContinuationPrompt,
  runBounded,
  scenarioPrompt,
  testExtensionControlState,
  testExtensionCapabilityCleanupNotice,
  testExtensionCapabilityRefreshOutcome,
  testExtensionPlaneClosed,
  testExtensionProviderReadiness,
  validateManifestWorkspace,
  validatePhysicalManifestFile,
  validatePhysicalManifestWorkspace,
  validateSelection,
  verifyPinnedTestExtensionManifest,
  writerLoserPrompt,
} from "../../extension/src/testExtensionCore";
import { normalizeEngineCapability } from "../../extension/src/sessionClient";
import type { EngineCapability } from "../../extension/src/sessionClient";
import type { ConversationSnapshot, ToolCardView } from "../../extension/src/webviewProtocol";
import { testTempRoot } from "./tempRoot";

const SUITE_NONCE = "a".repeat(64);
const configuredExtensionRoot = process.env.KAIZEN_EXTENSION_ROOT;
assert.ok(configuredExtensionRoot && path.isAbsolute(configuredExtensionRoot), "run through run-tests.mjs so KAIZEN_EXTENSION_ROOT is pinned");
const EXTENSION_ROOT = path.resolve(configuredExtensionRoot);
const TEST_DEVROOT = path.join(testTempRoot(), "devroot");
const TEST_PYTHON = process.platform === "win32"
  ? path.join(TEST_DEVROOT, "Python", "venvs", "kaizen", "Scripts", "python.exe")
  : path.join(TEST_DEVROOT, "Python", "venvs", "kaizen", "bin", "python");

/** Read a deliberate structural pin for non-exported VS Code wiring with a precise fixture diagnostic. */
function structuralSource(...relative: string[]): string {
  const sourcePath = path.join(EXTENSION_ROOT, ...relative);
  assert.ok(fs.existsSync(sourcePath), `structural contract source is missing: ${sourcePath}`);
  return fs.readFileSync(sourcePath, "utf-8");
}

test("private manifest binds prompts to the exact isolated workspace and rejects forged paths", () => {
  const runRoot = path.join(testTempRoot(), "test-extension-manifest-run");
  const workspace = path.resolve(runRoot, "fixture-workspace");
  const manifest = {
    run_id: "te-functional-prompt-test",
    workspace_path: workspace,
    codewords: { text: "TETEXT_TEST", context: "TECTX_TEST", selection: "TESEL_TEST", image: "TEIMG_TEST" },
    fixtures: {
      context: "te-context.txt", selection: "te-selection.txt", image: "te-image.png",
      diff_target: "te-diff-target.txt",
    },
  };
  assert.equal(validateManifestWorkspace(runRoot, workspace, workspace), workspace);
  for (const forged of [
    undefined,
    "fixture-workspace",
    path.resolve(runRoot, "..", "fixture-workspace"),
    path.resolve(runRoot, "other-workspace"),
    `${runRoot}${path.sep}nested${path.sep}..${path.sep}fixture-workspace`,
  ]) {
    assert.throws(
      () => validateManifestWorkspace(runRoot, workspace, forged),
      /TEST_EXTENSION_MANIFEST_WORKSPACE_INVALID/,
    );
  }
  assert.throws(
    () => validateManifestWorkspace(runRoot, path.resolve(runRoot, "other-workspace"), workspace),
    /TEST_EXTENSION_MANIFEST_WORKSPACE_INVALID/,
  );

  for (const scenario of TEST_EXTENSION_SCENARIOS) {
    const prompt = scenarioPrompt(scenario, manifest, path.resolve(workspace, "python.exe"));
    assert.match(prompt, /Absolute isolated workspace\/cwd:/, scenario.id);
    assert.ok(prompt.includes(JSON.stringify(workspace)), scenario.id);
  }
  const diffTarget = path.resolve(workspace, manifest.fixtures.diff_target);
  for (const kind of ["diff", "stale", "corrupt", "timeout", "writer"] as const) {
    const scenario = TEST_EXTENSION_SCENARIOS.find((item) => item.kind === kind)!;
    const prompt = scenarioPrompt(scenario, manifest, path.resolve(workspace, "python.exe"));
    assert.ok(prompt.includes(`Absolute target: ${JSON.stringify(diffTarget)}`), kind);
    assert.ok(prompt.includes(`Tool path (workspace-relative): ${JSON.stringify(manifest.fixtures.diff_target)}`), kind);
  }
  const loser = writerLoserPrompt(manifest);
  assert.ok(loser.includes(`Absolute target: ${JSON.stringify(diffTarget)}`));
  assert.ok(loser.includes(`Tool path (workspace-relative): ${JSON.stringify(manifest.fixtures.diff_target)}`));
  assert.ok(restartContinuationPrompt(manifest).includes(JSON.stringify(workspace)));
  const traversal = scenarioPrompt(
    TEST_EXTENSION_SCENARIOS.find((item) => item.kind === "traversal")!, manifest, "python",
  );
  assert.ok(traversal.includes("path ../outside.txt"), "intentional traversal tool path remains unchanged");
  assert.throws(
    () => scenarioPrompt(
      TEST_EXTENSION_SCENARIOS.find((item) => item.kind === "diff")!,
      { ...manifest, fixtures: { ...manifest.fixtures, diff_target: "../escape.txt" } },
      "python",
    ),
    /TEST_EXTENSION_MANIFEST_FIXTURE_INVALID/,
  );
});

test("private manifest is fully validated and pinned before fixture consumption", () => {
  const runRoot = path.join(testTempRoot(), "te-pinned-manifest");
  const workspace = path.resolve(runRoot, "fixture-workspace");
  const body = {
    v: 1,
    run_id: "te-pinned-manifest",
    suite_nonce: SUITE_NONCE,
    workspace_path: workspace,
    codewords: {
      text: "TETEXT_MANIFEST", context: "TECTX_MANIFEST",
      selection: "TESEL_MANIFEST", image: "TEIMG_MANIFEST",
    },
    fixtures: {
      context: "te-context.txt", selection: "te-selection.txt", image: "te-image.png",
      diff_target: "te-diff-target.txt",
    },
  };
  const bytes = Buffer.from(JSON.stringify(body));
  const snapshot = pinTestExtensionManifest(bytes, runRoot, workspace);
  assert.ok(Object.isFrozen(snapshot));
  assert.ok(Object.isFrozen(snapshot.manifest));
  assert.ok(Object.isFrozen(snapshot.manifest.codewords));
  assert.ok(Object.isFrozen(snapshot.manifest.fixtures));
  assert.equal(verifyPinnedTestExtensionManifest(snapshot, bytes), snapshot.manifest);
  assert.throws(
    () => verifyPinnedTestExtensionManifest(snapshot, Buffer.from(JSON.stringify({ ...body, created_at: "rewritten" }))),
    /TEST_EXTENSION_MANIFEST_CHANGED/,
  );

  let consumed = false;
  for (const forged of [
    { ...body, fixtures: { ...body.fixtures, diff_target: "../escape.txt" } },
    { ...body, fixtures: { ...body.fixtures, image: "subdir/te-image.png" } },
    { ...body, codewords: { ...body.codewords, text: "FORGED" } },
  ]) {
    assert.throws(() => {
      const validated = pinTestExtensionManifest(Buffer.from(JSON.stringify(forged)), runRoot, workspace);
      consumed = validated.manifest.fixtures.diff_target.length > 0;
    }, /TEST_EXTENSION_MANIFEST_INVALID/);
  }
  assert.equal(consumed, false, "forged fixture metadata must be rejected before a consumer can observe it");
});

test("physical manifest binding rejects workspace replacement and redirected manifest files", () => {
  const runRoot = path.join(testTempRoot(), "te-physical-binding");
  const workspace = path.resolve(runRoot, "fixture-workspace");
  const manifestPath = path.resolve(runRoot, "manifest.json");
  const key = (value: string) => process.platform === "win32" ? path.resolve(value).toLowerCase() : path.resolve(value);
  const directories = new Set([key(runRoot), key(workspace)]);
  const files = new Set([key(manifestPath)]);
  const normal = {
    lstat: (target: string) => ({
      isDirectory: () => directories.has(key(target)),
      isFile: () => files.has(key(target)),
      isSymbolicLink: () => false,
    }),
    realpath: (target: string) => path.resolve(target),
  };
  const caseVariant = process.platform === "win32" ? workspace.toUpperCase() : workspace;
  assert.equal(validatePhysicalManifestWorkspace(runRoot, caseVariant, normal), workspace);
  validatePhysicalManifestFile(runRoot, manifestPath, normal);

  const replacedWorkspace = {
    ...normal,
    lstat: (target: string) => ({
      isDirectory: () => directories.has(key(target)),
      isFile: () => files.has(key(target)),
      isSymbolicLink: () => key(target) === key(workspace),
    }),
  };
  assert.throws(
    () => validatePhysicalManifestWorkspace(runRoot, workspace, replacedWorkspace),
    /TEST_EXTENSION_MANIFEST_WORKSPACE_INVALID/,
  );
  const redirectedWorkspace = {
    ...normal,
    realpath: (target: string) => key(target) === key(workspace)
      ? path.resolve(runRoot, "..", "outside-workspace")
      : path.resolve(target),
  };
  assert.throws(
    () => validatePhysicalManifestWorkspace(runRoot, workspace, redirectedWorkspace),
    /TEST_EXTENSION_MANIFEST_WORKSPACE_INVALID/,
  );
  const redirectedManifest = {
    ...normal,
    lstat: (target: string) => ({
      isDirectory: () => directories.has(key(target)),
      isFile: () => files.has(key(target)),
      isSymbolicLink: () => key(target) === key(manifestPath),
    }),
  };
  assert.throws(
    () => validatePhysicalManifestFile(runRoot, manifestPath, redirectedManifest),
    /TEST_EXTENSION_MANIFEST_INVALID/,
  );
});

test("Windows extended-length realpaths compare equal to their lexical paths", { skip: process.platform !== "win32" }, () => {
  const runRoot = path.join(testTempRoot(), "te-extended-path-binding");
  const workspace = path.resolve(runRoot, "fixture-workspace");
  const manifestPath = path.resolve(runRoot, "manifest.json");
  const key = (value: string) => path.resolve(value).toLowerCase();
  const directories = new Set([key(runRoot), key(workspace)]);
  const files = new Set([key(manifestPath)]);
  const extended = (value: string) => value.startsWith("\\\\")
    ? `\\\\?\\UNC\\${value.slice(2)}`
    : `\\\\?\\${value}`;
  const operations = {
    lstat: (target: string) => ({
      isDirectory: () => directories.has(key(target)),
      isFile: () => files.has(key(target)),
      isSymbolicLink: () => false,
    }),
    realpath: (target: string) => extended(path.resolve(target)),
  };
  assert.equal(validatePhysicalManifestWorkspace(runRoot, workspace, operations), workspace);
  assert.doesNotThrow(() => validatePhysicalManifestFile(runRoot, manifestPath, operations));
});

const capabilities: EngineCapability[] = [
  {
    id: "claude", label: "Claude", drivable: true, availability: { state: "available" },
    models: [{ id: "new-model", label: "New model", reasoning_efforts: ["low", "high"] }],
    auth_modes: ["subscription"], permission_modes: ["plan", "ask"], warnings: [],
    runtime: { kind: "claude-sdk", status: "ready" },
    features: {
      streaming: true, image_attachments: true, governed_context: true, diff_snapshots: true,
      writer_leasing: true, subscription_auth: true, controlled_tools: true, process_execution: true,
      test_extension: true,
    },
  },
  {
    id: "local_llm", label: "Ollama", drivable: true, availability: { state: "available" },
    models: [{ id: "local-model", label: "Local model", reasoning_efforts: [] }],
    auth_modes: ["none"], permission_modes: ["plan"], warnings: [],
    features: {
      streaming: true, image_attachments: false, governed_context: true, diff_snapshots: false,
      writer_leasing: true, subscription_auth: false, controlled_tools: false, process_execution: false,
      test_extension: true,
    },
  },
];

test("selection uses discovered models and reserves max-turns per scenario", () => {
  const request = validateSelection(
    {
      provider: "claude", model: "new-model", effort: "high", maxTurns: 4, callCeiling: 8,
      scenarios: ["claude-text-stream", "claude-governed-context"],
    },
    capabilities,
    SUITE_NONCE,
  );
  assert.deepEqual(
    { v: request.v, action: request.action, provider: request.provider, effort: request.effort, call_ceiling: request.call_ceiling },
    { v: 1, action: "start", provider: "claude", effort: "high", call_ceiling: 8 },
  );
  assert.equal(request.model, "new-model");
  assert.equal(request.provider_retries, 0);
  assert.equal(reservedProviderCalls(request.scenarios, request.max_turns), request.call_ceiling);
  assert.equal(request.suite_nonce, SUITE_NONCE);
  assert.match(request.request_sha256, /^[0-9a-f]{64}$/);
});

test("restart recovery reserves two provider turns and suite nonce fails closed", () => {
  const selection = {
    provider: "claude" as const, model: "new-model", effort: "high", maxTurns: 4, callCeiling: 8,
    scenarios: ["claude-interrupt-restart"],
  };
  assert.equal(validateSelection(selection, capabilities, SUITE_NONCE).call_ceiling, 8);
  assert.throws(
    () => validateSelection({ ...selection, callCeiling: 7 }, capabilities, SUITE_NONCE),
    (error) => error instanceof TestExtensionSelectionError && error.code === "DENIED_TEST_EXTENSION_CALL_BUDGET",
  );
  assert.throws(
    () => validateSelection(selection, capabilities, "bad"),
    (error) => error instanceof TestExtensionSelectionError && error.code === "DENIED_TEST_EXTENSION_SUITE_NONCE",
  );
});

test("feature absence, unknown profile, cross-provider scenario, and budget fail closed", () => {
  const base = {
    provider: "claude" as const, model: "new-model", effort: "high", maxTurns: 4, callCeiling: 8,
    scenarios: ["claude-text-stream", "claude-governed-context"],
  };
  const cases: Array<[Partial<typeof base>, EngineCapability[], string]> = [
    [{ model: "hard-coded" }, capabilities, "DENIED_TEST_EXTENSION_MODEL"],
    [{ effort: undefined }, capabilities, "DENIED_TEST_EXTENSION_EFFORT_REQUIRED"],
    [{ effort: "unknown" }, capabilities, "DENIED_TEST_EXTENSION_EFFORT"],
    [{ scenarios: ["ollama-text-stream"] }, capabilities, "DENIED_TEST_EXTENSION_PROVIDER_SCENARIO"],
    [{ callCeiling: 7 }, capabilities, "DENIED_TEST_EXTENSION_CALL_BUDGET"],
    [{}, [{ ...capabilities[0], features: { ...capabilities[0].features, test_extension: false } }], "DENIED_TEST_EXTENSION_PROVIDER_UNAVAILABLE"],
  ];
  for (const [change, caps, code] of cases) {
    assert.throws(
      () => validateSelection({ ...base, ...change }, caps, SUITE_NONCE),
      (error) => error instanceof TestExtensionSelectionError && error.code === code,
    );
  }
});

test("Claude and Ollama scenario sets remain disjoint", () => {
  const claude = new Set(providerScenarios("claude").map((item) => item.id));
  const ollama = new Set(providerScenarios("ollama").map((item) => item.id));
  assert.equal([...claude].some((item) => ollama.has(item)), false);
});

test("scenario choices survive repeated snapshots, select-none, provider switches, and catalog drift", () => {
  const claude = providerScenarios("claude").map((scenario) => scenario.id);
  const ollama = providerScenarios("ollama").map((scenario) => scenario.id);
  assert.deepEqual(reconcileScenarioSelection(claude, undefined), claude, "first render selects provider defaults");
  assert.deepEqual(
    reconcileScenarioSelection(claude, [claude[0], claude[2]]),
    [claude[0], claude[2]],
    "repeat render preserves explicit selections",
  );
  assert.deepEqual(reconcileScenarioSelection(claude, []), [], "select-none is not mistaken for first render");
  assert.deepEqual(reconcileScenarioSelection([...claude, "new-scenario"], [claude[0]]), [claude[0]]);
  assert.deepEqual(reconcileScenarioSelection(claude.slice(1), [claude[0], claude[1]]), [claude[1]]);
  assert.deepEqual(reconcileScenarioSelection(ollama, undefined), ollama, "a newly visited provider gets its defaults");
});

test("model choices never silently substitute the first catalog entry", () => {
  assert.deepEqual(reconcileModelSelection(["new-a", "new-b"], undefined), {
    selected: "", unavailable: false,
  });
  assert.deepEqual(reconcileModelSelection(["new-a", "new-b"], undefined, "new-b"), {
    selected: "new-b", unavailable: false,
  });
  assert.deepEqual(reconcileModelSelection(["new-a", "new-b"], "new-a"), {
    selected: "new-a", unavailable: false,
  });
  assert.deepEqual(reconcileModelSelection(["new-a", "new-b"], "removed"), {
    selected: "removed", unavailable: true,
  });
});

test("workload controls lock for both running and stopping states", () => {
  assert.deepEqual(testExtensionControlState(false, false), {
    workloadLocked: false, startLocked: false, stopLocked: false,
  });
  assert.deepEqual(testExtensionControlState(true, false), {
    workloadLocked: true, startLocked: true, stopLocked: false,
  });
  assert.deepEqual(testExtensionControlState(false, true), {
    workloadLocked: true, startLocked: true, stopLocked: true,
  });
  assert.deepEqual(testExtensionControlState(true, true), {
    workloadLocked: true, startLocked: true, stopLocked: true,
  });
  assert.deepEqual(testExtensionControlState(false, false, true), {
    workloadLocked: true, startLocked: true, stopLocked: true,
  });
});

test("pre-approval Stop is exact, plane-bound, optionally pending-suite-bound, and locally idempotent", () => {
  const stopId = "b".repeat(64);
  assert.deepEqual(preApprovalStopRequest(SUITE_NONCE, stopId), {
    v: 1, action: "stop", suite_nonce: SUITE_NONCE, stop_id: stopId, reason: "user",
  });
  assert.deepEqual(preApprovalStopRequest(SUITE_NONCE, stopId, "c".repeat(64)), {
    v: 1, action: "stop", suite_nonce: SUITE_NONCE, stop_id: stopId, reason: "user",
    request_sha256: "c".repeat(64),
  });
  for (const invoke of [
    () => preApprovalStopRequest("bad", stopId),
    () => preApprovalStopRequest(SUITE_NONCE, "bad"),
    () => preApprovalStopRequest(SUITE_NONCE, stopId, "bad"),
  ]) assert.throws(invoke, TestExtensionSelectionError);
  const closed = [
    { seq: 1, event: "cleanup.edh", status: "PASS", termination_proven: true },
    { seq: 2, event: "cleanup.daemon", status: "PASS", termination_proven: true },
    { seq: 3, event: "run.closed", status: "FAIL" },
  ];
  assert.equal(testExtensionPlaneClosed(closed), true);
  assert.equal(testExtensionPlaneClosed([
    { seq: 1, event: "cleanup.edh", status: "PASS", termination_proven: true, not_started: true },
    { seq: 2, event: "cleanup.daemon", status: "PASS", termination_proven: true, not_started: true },
    { seq: 3, event: "run.closed", status: "STOP" },
  ]), true);
  assert.equal(testExtensionPlaneClosed([{ seq: 1, event: "run.closed", status: "FAIL" }]), false);
  assert.equal(testExtensionPlaneClosed([
    { seq: 1, event: "cleanup.edh", status: "PASS", termination_proven: true },
    { seq: 2, event: "cleanup.daemon", status: "FAIL", termination_proven: false, preserved_for_audit: true },
    { seq: 3, event: "run.closed", status: "FAIL" },
  ]), false);
  assert.equal(testExtensionPlaneClosed([...closed, { seq: 4, event: "run.closed", status: "FAIL" }]), false);
  assert.equal(testExtensionPlaneClosed([...closed, { seq: 4, event: "late", status: "FAIL" }]), false);
  assert.equal(testExtensionPlaneClosed([
    { seq: 1, event: "cleanup.edh", status: "FAIL", termination_proven: false, preserved_for_audit: true },
    { seq: 2, event: "cleanup.edh", status: "PASS", termination_proven: true },
    { seq: 3, event: "cleanup.daemon", status: "PASS", termination_proven: true },
    { seq: 4, event: "run.closed", status: "FAIL" },
  ]), false);
  assert.equal(testExtensionPlaneClosed([{ seq: 1, event: "suite.stop_requested", status: "STOP" }]), false);
});

test("Test Extension Stop cancels approval wait and never cross-controls a normal conversation", () => {
  const source = structuralSource("src", "testExtension.ts");
  assert.match(source, /this\.cancelledRequestSha256 === request\.request_sha256\) return undefined/);
  assert.match(source, /preApprovalStopRequest\([\s\S]*pending\?\.request_sha256/);
  assert.match(source, /this\.requestOuterAction\("suite", "suite\.stop", \{ reason: "user" \}\)/);
  assert.match(source, /if \(this\.stopRequested\) return/);
  assert.match(source, /visible outer runner owns final cleanup proof/);
  assert.doesNotMatch(source, /TEST_EXTENSION_STOP_REQUIRES_APPROVED_SUITE/);
  const stopBody = source.slice(source.indexOf("private async stop()"), source.indexOf("private readOuterLedger()"));
  assert.doesNotMatch(stopBody, /this\.chats\./);
});

test("launcher and child expose exact-run Stop with persistent cleanup proof and never hard-dispose the terminal", () => {
  const source = structuralSource("src", "testExtension.ts");
  const terminal = structuralSource("src", "testExtensionTerminal.ts");
  assert.match(source, /const launcherActive = !this\.runRoot && !!this\.terminal && this\.terminal\.exitStatus === undefined/);
  assert.doesNotMatch(source, /this\.terminal\?\.exitStatus === undefined/);
  assert.match(source, /\[hidden\]\{display:none!important\}/);
  assert.match(source, /<button id="launch-stop" class="stop" hidden>Stop and verify cleanup<\/button>/);
  assert.match(source, /<button id="stop" class="stop">Stop and verify cleanup<\/button>/);
  assert.match(source, /\$\("launch"\)\.hidden=!!snapshot\.launcher\?\.active/);
  assert.match(source, /\$\("launch-stop"\)\.hidden=!snapshot\.launcher\?\.active/);
  assert.match(source, /\$\("launch-stop"\)\.disabled=!snapshot\.launcher\?\.stopAvailable/);
  assert.match(source, /safeChildPath\(runRoot, undefined, "launcher-stop\.json"\)/);
  assert.match(source, /writeExclusiveJson\([\s\S]*preApprovalStopRequest\(manifest\.suite_nonce/);
  assert.match(source, /validatePhysicalManifestWorkspace\(runRoot, workspace\)/);
  assert.match(source, /validatePhysicalManifestFile\(runRoot, manifestPath\)/);
  assert.match(source, /pinTestExtensionManifest\(fs\.readFileSync\(manifestPath\), runRoot, workspace\)/);
  assert.doesNotMatch(source, /TEST_EXTENSION_LAUNCHER_STOP_REQUIRES_CHILD_EDH/);
  assert.doesNotMatch(source, /this\.terminal\.dispose\(\)/);
  assert.match(source, /this\.launcherPoller = setInterval\(tick, 500\)/);
  assert.match(source, /this\.ledger = \[\];\s*this\.launcherEvidencePath = undefined/);
  assert.match(source, /this\.ledger = parseEvidenceJsonl\(fs\.readFileSync\(evidencePath, "utf-8"\)\)/);
  assert.match(source, /return testExtensionPlaneClosed\(this\.ledger\)/);
  assert.match(source, /evidencePath: this\.evidencePath \?\? this\.launcherEvidencePath/);
  assert.match(source, /if \(terminal\.exitStatus !== undefined\) \{\s*this\.finishLauncherTerminal\(\)/);
  assert.match(source, /this\.launcherClosedProven = true/);
  assert.match(source, /CLEANUP VERIFIED — the isolated EDH and daemon termination proofs passed/);
  assert.match(source, /CLEANUP NOT VERIFIED — do not start another test plane/);
  assert.match(terminal, /"--plane-base", planeBase,[\s\S]*"--run-id", runId/);
  assert.match(terminal, /TEST_EXTENSION_RUN_ID_INVALID/);
});

test("provider readiness is sanitized and fails closed for auth, old daemon, and unavailable runtime", () => {
  assert.deepEqual(testExtensionProviderReadiness("claude", capabilities), {
    state: "ready", canStart: true, message: "Claude subscription is ready for a bounded Test Extension suite.",
  });
  assert.equal(testExtensionProviderReadiness("ollama", capabilities).canStart, true);

  const [normalized] = normalizeEngineCapability({
    id: "claude", label: "Claude", drivable: true, availability: { state: "available" },
    models: [{ id: "account-model", label: "Account model", reasoning_efforts: ["low", "high"] }],
    auth_modes: ["subscription"], permission_modes: ["plan", "ask"], warnings: [],
    runtime: { kind: "claude-agent-sdk", status: "ready" },
    features: { test_extension: true },
  });
  assert.ok(normalized);
  assert.equal(testExtensionProviderReadiness("claude", [normalized]).canStart, true);
  const [normalizedOllama] = normalizeEngineCapability({
    id: "local_llm", label: "Ollama", drivable: true, availability: { state: "available" },
    models: [{ id: "local-model", label: "Local model", reasoning_efforts: [] }],
    auth_modes: ["none"], permission_modes: ["plan"], warnings: [],
    features: { test_extension: true },
  });
  assert.ok(normalizedOllama);
  assert.equal(testExtensionProviderReadiness("ollama", [normalizedOllama]).canStart, true);
  for (const state of ["ready", "degraded", "policy_gate_unavailable", "unavailable"]) {
    assert.equal(testExtensionProviderReadiness("claude", [{
      ...normalized, availability: { state },
    }]).canStart, false, `wire availability ${state} must fail closed`);
  }

  const authRequired: EngineCapability[] = [{
    ...capabilities[0],
    availability: { state: "auth_required", code: "DENIED_AUTH_UNAVAILABLE", message: "raw external detail" },
    runtime: { kind: "claude-sdk", status: "auth_required" },
  }];
  const auth = testExtensionProviderReadiness("claude", authRequired);
  assert.equal(auth.state, "auth_required");
  assert.equal(auth.canStart, false);
  assert.equal(auth.externalCode, "DENIED_AUTH_UNAVAILABLE");
  assert.match(auth.message, /Anthropic-owned application outside Kaizen/);
  assert.doesNotMatch(auth.message, /raw external detail|DENIED_AUTH_UNAVAILABLE/);

  const unsupported = testExtensionProviderReadiness("claude", [{
    ...capabilities[0], features: { ...capabilities[0].features, test_extension: false },
  }]);
  assert.equal(unsupported.state, "unsupported");
  assert.equal(unsupported.canStart, false);
  assert.equal(unsupported.externalCode, undefined);

  const unavailable = testExtensionProviderReadiness("claude", [{
    ...capabilities[0], runtime: { kind: "claude-sdk", status: "unavailable" },
  }]);
  assert.equal(unavailable.state, "unavailable");
  assert.equal(unavailable.canStart, false);
  assert.equal(unavailable.externalCode, "DENIED_SDK_UNAVAILABLE");

  const initializationTimeout = testExtensionProviderReadiness("claude", [{
    ...capabilities[0],
    availability: {
      state: "unavailable", code: "DENIED_SDK_UNAVAILABLE",
      message: "Claude subscription initialization timed out before account and model discovery.",
    },
    runtime: { kind: "claude-sdk", status: "unavailable" },
  }]);
  assert.match(initializationTimeout.message, /timed out before account and model discovery/);

  const noRuntime = testExtensionProviderReadiness("claude", [{ ...capabilities[0], runtime: undefined }]);
  assert.equal(noRuntime.canStart, false, "Claude runtime readiness is mandatory");
  assert.equal(noRuntime.externalCode, undefined, "missing runtime metadata is an internal catalog failure");
});

test("provider readiness exposes only capability-justified external NOT_RUN codes", () => {
  const noModels = testExtensionProviderReadiness("claude", [{ ...capabilities[0], models: [] }]);
  assert.equal(noModels.externalCode, "DENIED_MODEL_UNAVAILABLE");

  const capacity = testExtensionProviderReadiness("claude", [{
    ...capabilities[0],
    availability: { state: "degraded", code: "DENIED_PROVIDER_CAPACITY", message: "private detail" },
  }]);
  assert.equal(capacity.externalCode, "DENIED_PROVIDER_CAPACITY");
  assert.doesNotMatch(capacity.message, /private detail|DENIED_PROVIDER_CAPACITY/);

  const unrecognized = testExtensionProviderReadiness("claude", [{
    ...capabilities[0], availability: { state: "degraded", code: "INTERNAL_PROVIDER_FAILURE" },
  }]);
  assert.equal(unrecognized.externalCode, undefined);

  const notRegistered = testExtensionProviderReadiness("claude", [{
    ...capabilities[0], drivable: false, runtime: undefined,
    availability: { state: "unavailable", code: "DENIED_SDK_UNAVAILABLE" },
  }]);
  assert.equal(notRegistered.externalCode, undefined, "not-registered state stays an internal failure");
  assert.equal(testExtensionProviderReadiness("claude", [], true).externalCode, undefined);
  assert.equal(testExtensionProviderReadiness("claude", capabilities, false).externalCode, undefined);
});

test("external NOT_RUN request is nonce-bound, capability-bound, provider-owned, and replay-identifiable", () => {
  const blockId = "b".repeat(64);
  const authRequired: EngineCapability[] = [{
    ...capabilities[0],
    availability: { state: "auth_required", code: "DENIED_AUTH_MODE_MISMATCH" },
    runtime: { kind: "claude-sdk", status: "auth_required" },
  }];
  assert.deepEqual(externalBlockRequest(
    SUITE_NONCE,
    blockId,
    "claude",
    "denied_auth_mode_mismatch",
    ["claude-text-stream", "claude-governed-context"],
    authRequired,
  ), {
    v: 1,
    action: "external_block",
    suite_nonce: SUITE_NONCE,
    block_id: blockId,
    provider: "claude",
    code: "DENIED_AUTH_MODE_MISMATCH",
    scenarios: ["claude-text-stream", "claude-governed-context"],
  });

  const cases: Array<[() => unknown, string]> = [
    [() => externalBlockRequest("bad", blockId, "claude", "DENIED_AUTH_MODE_MISMATCH", ["claude-text-stream"], authRequired),
      "DENIED_TEST_EXTENSION_SUITE_NONCE"],
    [() => externalBlockRequest(SUITE_NONCE, "bad", "claude", "DENIED_AUTH_MODE_MISMATCH", ["claude-text-stream"], authRequired),
      "DENIED_TEST_EXTENSION_BLOCK_ID"],
    [() => externalBlockRequest(SUITE_NONCE, blockId, "codex" as never, "DENIED_AUTH_MODE_MISMATCH", ["claude-text-stream"], authRequired),
      "DENIED_TEST_EXTENSION_PROVIDER"],
    [() => externalBlockRequest(SUITE_NONCE, blockId, "claude", "DENIED_MODEL_UNAVAILABLE", ["claude-text-stream"], authRequired),
      "DENIED_TEST_EXTENSION_EXTERNAL_BLOCK"],
    [() => externalBlockRequest(SUITE_NONCE, blockId, "claude", "DENIED_AUTH_MODE_MISMATCH", ["claude-text-stream", "claude-text-stream"], authRequired),
      "DENIED_TEST_EXTENSION_SCENARIOS"],
    [() => externalBlockRequest(SUITE_NONCE, blockId, "claude", "DENIED_AUTH_MODE_MISMATCH", ["ollama-text-stream"], authRequired),
      "DENIED_TEST_EXTENSION_PROVIDER_SCENARIO"],
    [() => externalBlockRequest(SUITE_NONCE, blockId, "claude", "DENIED_AUTH_MODE_MISMATCH", ["claude-text-stream"], capabilities),
      "DENIED_TEST_EXTENSION_EXTERNAL_BLOCK"],
    [() => externalBlockRequest(SUITE_NONCE, blockId, "claude", "DENIED_AUTH_MODE_MISMATCH", ["claude-text-stream"], authRequired, false),
      "DENIED_TEST_EXTENSION_EXTERNAL_BLOCK"],
  ];
  for (const [invoke, code] of cases) {
    assert.throws(
      invoke,
      (error) => error instanceof TestExtensionSelectionError && error.code === code,
      code,
    );
  }
});

test("Test Extension panel keeps refresh failures visible and fail-closed until exact recovery", () => {
  const views = capabilities.map((capability) => ({
    ...capability,
    features: {
      streaming: capability.features.streaming,
      image_attachments: capability.features.image_attachments,
      governed_context: capability.features.governed_context,
      diff_snapshots: capability.features.diff_snapshots,
      writer_leasing: capability.features.writer_leasing,
      subscription_auth: capability.features.subscription_auth === true,
      controlled_tools: capability.features.controlled_tools === true,
      process_execution: capability.features.process_execution === true,
      test_extension: capability.features.test_extension === true,
    },
    ...(capability.max_turns ? { maxTurns: capability.max_turns } : {}),
  })) as ConversationSnapshot["capabilities"];

  const nonOk = testExtensionCapabilityRefreshOutcome({
    capabilities: views,
    banner: { message: "Capability discovery failed", code: "DENIED_PROVIDER_CAPACITY" },
  }, capabilities);
  assert.equal(nonOk.failed, true);
  assert.equal(nonOk.capabilities.length, capabilities.length, "last good catalog is retained for display");
  assert.equal(nonOk.notice, "Capability refresh failed: Capability discovery failed [DENIED_PROVIDER_CAPACITY]");
  assert.equal(testExtensionProviderReadiness("claude", nonOk.capabilities, !nonOk.failed).canStart, false);

  const transport = testExtensionCapabilityRefreshOutcome({
    capabilities: views,
    banner: { message: "Capability discovery unavailable", code: "DAEMON_UNREACHABLE" },
  }, nonOk.capabilities);
  assert.equal(transport.notice, "Capability refresh failed: Capability discovery unavailable [DAEMON_UNREACHABLE]");
  assert.equal(testExtensionProviderReadiness("ollama", transport.capabilities, !transport.failed).state, "unavailable");

  const recovered = testExtensionCapabilityRefreshOutcome({ capabilities: views }, transport.capabilities);
  assert.equal(recovered.failed, false);
  assert.equal(recovered.notice, undefined, "success clears the prior outage notice");
  assert.equal(testExtensionProviderReadiness("claude", recovered.capabilities, !recovered.failed).canStart, true);

  assert.equal(
    testExtensionCapabilityCleanupNotice(undefined, new Error("TEST_EXTENSION_CAPABILITY_CONTROLLER_CLOSE_FAILED")),
    "Capability refresh cleanup failed: TEST_EXTENSION_CAPABILITY_CONTROLLER_CLOSE_FAILED",
  );
  assert.equal(
    testExtensionCapabilityCleanupNotice(nonOk.notice, new Error("close transport failed")),
    `${nonOk.notice}\nCapability refresh cleanup failed: close transport failed`,
    "cleanup failure appends without erasing the authoritative refresh failure",
  );
});

test("only exact external provider and rate/quota codes become NOT_RUN candidates", () => {
  for (const code of [
    "DENIED_AUTH_UNAVAILABLE", "DENIED_AUTH_MODE_MISMATCH", "DENIED_MODEL_UNAVAILABLE",
    "DENIED_SDK_UNAVAILABLE", "DENIED_PROVIDER_CAPACITY", "rate_limit", "rate_limited",
    "RATE_LIMIT_EXHAUSTED", "QUOTA_EXHAUSTED", "SUBSCRIPTION_QUOTA_EXHAUSTED",
  ]) {
    assert.equal(externalScenarioBlockCode(code), code.toUpperCase(), code);
  }
  for (const code of [
    "MODEL_CALL_BUDGET_EXHAUSTED", "MAX_TURNS_EXHAUSTED", "DENIED_TEST_EXTENSION_CALL_BUDGET",
    "DENIED_POLICY_RATE_LIMIT", "INTERNAL_AUTH_FAILURE", "DAEMON_UNREACHABLE", "rate limited by prose",
  ]) {
    assert.equal(externalScenarioBlockCode(code), undefined, code);
  }
});

test("Test Extension panel wires repeat-safe scenario state, readiness, and complete workload locks", () => {
  const source = structuralSource("src", "testExtension.ts");
  assert.match(source, /readiness:\s*\{\s*claude: testExtensionProviderReadiness/);
  assert.match(source, /controls: testExtensionControlState\(this\.running, this\.stopping, this\.stopRequested\)/);
  assert.match(source, /const selectedScenarios=new Map\(\)/);
  assert.match(source, /const selectedModels=new Map\(\)/);
  assert.match(source, /new Option\(models\.length\?"Select a model":"No models discovered",""\)/);
  assert.match(source, /"Unavailable: "\+choice\.selected/);
  assert.match(source, /this\.chats\.testExtensionRefreshCapabilities\(conversationId\)/);
  assert.match(source, /await this\.refreshCapabilities\(false\)/);
  assert.match(source, /case "refresh": await this\.refreshCapabilities\(true\)/);
  assert.match(source, /testExtensionCapabilityRefreshOutcome\(snapshot, this\.capabilities\)/);
  assert.match(source, /testExtensionProviderReadiness\("claude", this\.capabilities, !this\.capabilityRefreshFailed\)/);
  assert.match(source, /if \(this\.capabilityRefreshFailed\) throw new Error\("Capability catalog is unavailable/);
  assert.match(source, /if \(!\(await this\.chats\.testExtensionClose\(conversationId\)\)\) \{\s*throw new Error\("TEST_EXTENSION_CAPABILITY_CONTROLLER_CLOSE_FAILED"\)/);
  assert.match(source, /this\.capabilityRefreshFailed = true;\s*this\.notice = testExtensionCapabilityCleanupNotice\(this\.notice, error\)/);
  assert.match(source, /rememberScenarioSelection\(\);\s*const provider=/);
  assert.match(source, /for\(const id of \["provider","turns","ceiling","refresh"\]\)/);
  assert.match(source, /\$\("model"\)\.disabled=locked\|\|\(capability\?\.models\?\.length\?\?0\)===0/);
  assert.match(source, /\$\("effort"\)\.disabled=locked\|\|\(model\?\.reasoning_efforts\?\.length\?\?0\)===0/);
  assert.match(source, /document\.querySelectorAll\("#scenarios input"\).*input\.disabled=locked/);
  assert.doesNotMatch(source, /AUTH\|MODEL\|SDK\|PROVIDER_CAPACITY/);
});

test("Test Extension panel records capability-bound external NOT_RUN without starting a provider turn", () => {
  const source = structuralSource("src", "testExtension.ts");
  assert.match(source, /case "external-block": this\.recordExternalNotRun/);
  assert.match(source, /externalBlockRequest\([\s\S]*manifest\.suite_nonce[\s\S]*this\.capabilities/);
  assert.match(source, /<button id="notrun"[^>]*hidden>Record selected provider NOT RUN<\/button>/);
  assert.match(source, /\$\("notrun"\)\.hidden=!externalCode/);
  assert.match(source, /type:"external-block",provider:\$\("provider"\)\.value,code:currentReadiness\(\)\?\.externalCode,scenarios:checkedScenarioIds\(\)/);
  assert.match(source, /writeAtomicJson\(this\.controlPath, request\);[\s\S]*this\.stopRequested = true/);
});

test("required control scenarios are explicit and unsupported outcomes cannot disappear", () => {
  const ids = new Set(TEST_EXTENSION_SCENARIOS.map((scenario) => scenario.id));
  for (const id of [
    "claude-traversal-zero-record", "claude-diff-stale", "claude-diff-corrupt", "claude-diff-timeout",
    "claude-writer-conflict", "claude-interrupt-restart", "cleanup-leak-state",
  ]) assert.equal(ids.has(id), true, id);
});

test("Test Extension notices use the approval verb rendered by each normal chat card", () => {
  const source = structuralSource("src", "testExtension.ts");
  assert.match(source, /scenario\.kind === "process" \? "Approve"/);
  assert.match(source, /scenario\.expectedDecision === "approve" \? "Accept" : "Reject"/);
  assert.match(source, /Use Accept\/Reject for diff approvals and Approve for the harmless process card/);
  assert.doesNotMatch(source, /Approval scenarios must be decided with the normal inline Accept\/Reject controls/);
  assert.doesNotMatch(source, /waiting for \$\{scenario\.expectedDecision === "approve"/);
});

test("Test Extension delegates turns and approvals to the normal chat manager", () => {
  const source = structuralSource("src", "testExtension.ts");
  assert.match(source, /this\.chats\.testExtensionDrive/);
  assert.match(source, /normal Kaizen chat tabs/);
  for (const phase of [
    "traversal.arm_zero_records",
    "traversal.verify_zero_records",
    "stale.mutate_base", "corrupt.snapshot", "writer.holder_open", "writer.loser_arm",
    "writer.loser_verify", "writer.holder_release", "restart.daemon", "restart.cleanup",
  ]) assert.match(source, new RegExp(phase.replace(".", "\\.")), phase);
  assert.match(source, /click the visible Stop button/);
  assert.match(source, /waitForTurnCancellation/);
  assert.doesNotMatch(source, /TEST_EXTENSION_[A-Z0-9_]*_NOT_RUN/);
  assert.doesNotMatch(source, /new SessionClient|\.approve\(|type:\s*["']approval["']/);
  assert.match(source, /source_path:\s*["']\.\.\/outside\.txt["']/);
  assert.doesNotMatch(source, /readTraversalProof|traversal-proof\.json/);
});

test("EDH readiness is written atomically only after panel/controller initialization and binds the manifest", () => {
  const source = structuralSource("src", "testExtension.ts");
  const activation = structuralSource("src", "extension.ts");
  assert.match(source, /await this\.open\(\);[\s\S]*await readiness\.promise[\s\S]*this\.writeEdhReady\(\)/);
  assert.match(source, /case "ready":\s*this\.panelReady\?\.settle\(true\)/);
  assert.match(source, /KAIZEN_TEST_EXTENSION_EDH_READY_PATH/);
  assert.match(source, /suite_nonce:\s*manifest\.suite_nonce/);
  assert.match(source, /const manifest = this\.readManifest\(\);[\s\S]*const snapshot = this\.manifestSnapshot/);
  assert.match(source, /manifest_sha256:\s*snapshot\.sha256/);
  assert.match(source, /verifyPinnedTestExtensionManifest\(this\.manifestSnapshot, bytes\)/);
  assert.match(source, /if \(fs\.existsSync\(this\.edhReadyPath\)\) throw new Error\("TEST_EXTENSION_EDH_READY_STALE"\)/);
  assert.match(activation, /await panels\.initialize\(\);\s*await testExtension\.autoOpenInPlane\(\)/);
  const disposeRegistration = source.indexOf("panel.onDidDispose(() => {");
  const htmlInstallation = source.indexOf("listener = installWebviewHtml<HarnessMessage>(");
  assert.ok(disposeRegistration >= 0 && disposeRegistration < htmlInstallation,
    "dispose settlement must be installed before any webview HTML can execute");
});

test("EDH ready gate cannot be overwritten and HTML subscribes before a synchronous ready event", async () => {
  const gate = createTestExtensionReadyGate();
  const order: string[] = [];
  let receiver: ((message: { type: string }) => unknown) | undefined;
  const disposable = installWebviewHtml<{ type: string }>(
    (listener) => {
      order.push("subscribe");
      receiver = listener;
      return { dispose: () => { order.push("dispose"); } };
    },
    () => {
      order.push("html");
      receiver?.({ type: "ready" });
    },
    "<html></html>",
    (message) => {
      order.push(message.type);
      gate.settle(message.type === "ready");
    },
  );
  gate.settle(false);
  assert.equal(await gate.promise, true, "first ready settlement wins over a later dispose settlement");
  assert.deepEqual(order, ["subscribe", "html", "ready"]);
  disposable.dispose();
  assert.deepEqual(order, ["subscribe", "html", "ready", "dispose"]);

  const disposed = createTestExtensionReadyGate();
  disposed.settle(false);
  disposed.settle(true);
  assert.equal(await disposed.promise, false, "dispose-before-ready settles without creating readiness");
});

test("compiled Test Extension webview script parses before posting EDH readiness", () => {
  const runtimePath = path.join(EXTENSION_ROOT, "out", "src", "testExtension.js");
  assert.ok(fs.existsSync(runtimePath), "compiled Test Extension artifact is missing; run the extension build first");
  const runtime = fs.readFileSync(runtimePath, "utf-8");
  const template = runtime.match(/function panelHtml\(webview\) \{[\s\S]*?return `([\s\S]*?)`;\s*\}/)?.[1];
  assert.ok(template, "compiled panelHtml template was not found");
  const html = Function("nonce", "csp", `return \`${template}\`;`)("a".repeat(32), "default-src 'none'") as string;
  const script = html.match(/<script nonce="[^"]+">([\s\S]*?)<\/script>/)?.[1];
  assert.ok(script, "generated Test Extension script was not found");
  assert.doesNotThrow(() => Function(script), "generated Test Extension script must be valid JavaScript");
  assert.match(script, /vscode\.postMessage\(\{type:"ready"\}\)/);
});

test("PASS evidence exposes only scenario-local durable locators for outer reconciliation", () => {
  const source = structuralSource("src", "testExtension.ts");
  for (const field of ["session_id", "agent_run_id", "correlation_id", "previous_agent_run_id"]) {
    assert.match(source, new RegExp(`"${field}"`), `${field} is client-evidence allowlisted`);
  }
  assert.match(source, /const locator = scenarioLocatorProof\(scenario, latestSnapshot/);
  assert.match(source, /passed \? locator\.fields : \{\}/, "generic FAIL and NOT_RUN results do not inherit locators");
  assert.match(source, /durableLocatorProof\(refreshed\.sessionId, refreshed\.agentRunId, refreshed\.correlationId, true\)/);
  assert.match(source, /durableLocatorProof\(opened\.sessionId, opened\.agentRunId, opened\.correlationId, true\)/);
  assert.match(source, /durableLocatorProof\(holder\.sessionId, holder\.agentRunId, holder\.correlationId, true\)/);
  assert.match(source, /durableLocatorProof\(old\.sessionId, next\.agentRunId\)/);
  assert.match(source, /previous_agent_run_id: previousAgentRunId/);
  assert.match(source, /approval\.agentRunId === conversation\.agentRunId/);
  assert.match(source, /card\.tool === "kaizen_propose_changes" && card\.agentRunId === approval\.agentRunId/);
  assert.match(source, /approval\.agentRunId === card\.agentRunId && approval\.status === "approved"/);
  assert.match(source, /durableLocatorProof\(conversation\.sessionId, card\.agentRunId, approvals\[0\]\.correlationId, true\)/);
  assert.doesNotMatch(source, /approval\.correlationId === card\.correlationId/);
  assert.doesNotMatch(source, /card\.correlationId === approval\.correlationId/);
  assert.doesNotMatch(source, /restart_agent_run_id|restart_correlation_id/);
});

test("dirty governed selection range covers the exact staged snapshot", () => {
  const source = structuralSource("src", "testExtension.ts");
  assert.match(source, /const dirtySelection = `unsaved dirty selection codeword \$\{manifest\.codewords\.selection\}`/);
  assert.match(source, /end: \{ line: 0, character: dirtySelection\.length \}/);
  assert.match(source, /character: dirtySelection\.length \} \},\s*dirtySelection,/);
  assert.doesNotMatch(source, /character: manifest\.codewords\.selection\.length/);
});

test("corrupt-diff evidence reports and gates on the authenticated corrupted digest", () => {
  const source = structuralSource("src", "testExtension.ts");
  assert.match(source, /const corrupted = proofDigest\(receipt, "corrupted_sha256"\)/);
  assert.match(source, /const corruptionChanged = corrupted !== opened\.proposedSha256/);
  assert.match(source, /corruptionChanged && locator\.passed/);
  assert.match(source, /diff_proposed_sha256: opened\.proposedSha256/);
  assert.match(source, /diff_corrupted_artifact_sha256: corrupted/);
  assert.doesNotMatch(source, /diff_corrupted_artifact_sha256: opened\.proposedSha256/);
});

test("writer loser token matches the authenticated action-v2 digest contract", () => {
  const source = structuralSource("src", "testExtension.ts");
  assert.match(source, /const loserToken = crypto\.randomBytes\(32\)\.toString\("hex"\)/);
  assert.match(source, /loser_request_token: loserToken/);
  assert.doesNotMatch(source, /te-writer-loser-\$\{crypto\.randomUUID\(\)\}/);
});

test("bounded pool never exceeds two simultaneous turns and preserves order", async () => {
  let active = 0;
  let peak = 0;
  const gates: Array<() => void> = [];
  const promise = runBounded([1, 2, 3, 4], async (value) => {
    active += 1; peak = Math.max(peak, active);
    await new Promise<void>((resolve) => gates.push(resolve));
    active -= 1; return value * 2;
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(peak, 2);
  while (gates.length) { gates.shift()?.(); await new Promise((resolve) => setImmediate(resolve)); }
  assert.deepEqual(await promise, [2, 4, 6, 8]);
});

test("concurrency above two is denied", async () => {
  await assert.rejects(
    runBounded([1], async (value) => value, 3),
    (error) => error instanceof TestExtensionSelectionError && error.code === "DENIED_TEST_EXTENSION_CONCURRENCY",
  );
});

function planProcessCards(python: string): [ToolCardView, ToolCardView] {
  return [
    {
      timelineKey: "run:1", agentRunId: "run", sequenceNo: 1, correlationId: "call",
      tool: "kaizen_run_process", status: "blocked", summary: "denied", code: "MODE_CEILING:exec",
      process: {
        executable: python, argv: ["-c", "print('MUST_NOT_RUN')"], cwd: ".", timeoutMs: 5000,
        decision: "denied", effectsUnknown: true,
      },
    },
    {
      timelineKey: "run:2", agentRunId: "run", sequenceNo: 2, correlationId: "call-2",
      tool: "kaizen_run_process", status: "blocked", summary: "denied", code: "INV_GIT_PUSH",
      process: {
        executable: "git", argv: ["push", "origin", "main"], cwd: ".", timeoutMs: 5000,
        decision: "denied", effectsUnknown: true,
      },
    },
  ];
}

test("Plan process proof requires exact Plan and git-push invariant denials with no execution or output", () => {
  const python = TEST_PYTHON;
  const [card, invariant] = planProcessCards(python);
  const proof = planProcessControlProof([card, invariant], python);
  assert.equal(proof.tool_card_count, 2);
  assert.equal(proof.tool_status, "blocked");
  assert.equal(proof.tool_request_matches, true);
  assert.equal(proof.tool_zero_execution, true);
  assert.equal(proof.tool_zero_stdout, true);
  assert.equal(proof.denial_code, "MODE_CEILING:exec");
  assert.equal(proof.invariant_denial_code, "INV_GIT_PUSH");

  for (const cards of [
    [{ ...card, status: "ok" as const }, invariant],
    [{ ...card, process: { ...card.process!, argv: ["-c", "print('OTHER')"] } }, invariant],
    [{ ...card, process: { ...card.process!, exitCode: 0 } }, invariant],
    [{ ...card, process: { ...card.process!, stdoutBytes: 1, stdoutSha256: "a".repeat(64) } }, invariant],
    [card],
    [card, { ...invariant, process: { ...invariant.process!, argv: ["status"] } }],
  ]) {
    const rejected = planProcessControlProof(cards, python);
    assert.equal(
      rejected.tool_card_count === 2 && rejected.tool_status === "blocked" && rejected.tool_request_matches &&
      rejected.tool_zero_execution && rejected.tool_zero_stdout && !!rejected.denial_code &&
      rejected.invariant_denial_code === "INV_GIT_PUSH",
      false,
    );
  }
});

test("Plan process proof rejects an approved decision and truncated output", () => {
  const python = TEST_PYTHON;
  const [card, invariant] = planProcessCards(python);

  const approved = planProcessControlProof([
    { ...card, process: { ...card.process!, decision: "approved" } }, invariant,
  ], python);
  assert.equal(approved.tool_zero_execution, false);

  const truncated = planProcessControlProof([
    { ...card, process: { ...card.process!, truncated: true } }, invariant,
  ], python);
  assert.equal(truncated.tool_zero_stdout, false);
});

test("evidence parser drops malformed and undeclared fields", () => {
  const entries = parseEvidenceJsonl([
    "not json",
    JSON.stringify({
      v: 1, seq: 1, event: "cleanup.edh", status: "PASS", termination_proven: true,
      preserved_for_audit: false, raw_output: "private prose",
    }),
    JSON.stringify({ v: 2, seq: 2, event: "bad", status: "FAIL" }),
  ].join("\n"));
  assert.deepEqual(entries, [{
    seq: 1, event: "cleanup.edh", status: "PASS", termination_proven: true, preserved_for_audit: false,
  }]);
  assert.equal(JSON.stringify(entries).includes("private prose"), false);
});

test("diff preview cancellation is success while stale approvals and stale documents fail closed", () => {
  const source = structuralSource("src", "diffProvider.ts");
  assert.match(source, /if \(!picked\) return true;/, "cancelling a multi-change picker leaves the pending approval valid");
  assert.ok((source.match(/if \(!approval\.isCurrent\(\)\) return false;/g) ?? []).length >= 2, "stale approvals fail before and after selection");
  assert.match(source, /if \(!document\) throw vscode\.FileSystemError\.FileNotFound\(uri\)/);
  assert.match(source, /if \(!document\.isCurrent\(\)\) throw vscode\.FileSystemError\.Unavailable\(uri\)/);
  assert.match(source, /this\.documents\.delete\(key\);[\s\S]*this\.changed\.fire\(vscode\.Uri\.parse\(key\)\)/);
  assert.match(source, /!change\.before \? " \[new file\]" : !change\.proposed \? " \[deleted file\]"/);
});

test("daemon start and stop schedule one recovery retry when periodic polling is disabled", () => {
  const source = structuralSource("src", "extension.ts");
  assert.match(source, /scheduleRefresh\(2000, pollSeconds <= 0 \? 1 : 0\)/);
  assert.match(source, /scheduleRefresh\(1500, pollSeconds <= 0 \? 1 : 0\)/);
  assert.match(source, /if \(retries > 0\) scheduleRefresh\(delayMs, retries - 1\)/);
});
