#!/usr/bin/env node

/** Compile and run extension, webview, Node, and VSIX tests in an AI/work-confined temporary root, then clean it. */
import * as fs from "node:fs";
import * as path from "node:path";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";
import pythonSelection from "./pythonSelection.cjs";

const { sharedPython } = pythonSelection;

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");
const EXTENSION_ROOT = path.join(REPO_ROOT, "extension");
const TEST_ROOT = path.join(REPO_ROOT, "tests", "extension");
const WORK_ROOT = path.join(REPO_ROOT, "AI", "work");
const DEFAULT_TEMP_BASE = path.join(REPO_ROOT, "AI", "work", "test-temp");
for (const required of [path.join(REPO_ROOT, "kaizen.py"), EXTENSION_ROOT, TEST_ROOT, WORK_ROOT]) {
  if (!fs.existsSync(required)) throw new Error(`extension test repository sentinel is missing: ${required}`);
}

function samePath(left, right) {
  // Windows development volumes are case-insensitive here; ASCII folding matches their path contract.
  const normalize = process.platform === "win32" ? (value) => path.resolve(value).toLowerCase() : (value) => path.resolve(value);
  return normalize(left) === normalize(right);
}

/** Return true only for strict same-drive descendants; equality and upward escapes are false. */
function isDescendant(parent, child) {
  const relative = path.relative(parent, child);
  return relative !== "" && !relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative);
}

/** Reject Windows candidates on the system drive; this is a no-op elsewhere. */
function rejectSystemDrive(candidate) {
  if (process.platform !== "win32") return;
  const systemDrive = (process.env.SystemDrive || "C:").replace(/[\\/]+$/, "").toLowerCase();
  const candidateDrive = path.parse(candidate).root.replace(/[\\/]+$/, "").toLowerCase();
  if (candidateDrive === systemDrive) throw new Error(`test temp root must not use the Windows system drive: ${candidate}`);
}

/** Require a physical directory; the isolated temp plane intentionally rejects links in any ancestor. */
function assertPlainDirectory(candidate, label) {
  const stat = fs.lstatSync(candidate);
  if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error(`${label} must be a physical directory: ${candidate}`);
  const physical = fs.realpathSync.native(candidate);
  if (!samePath(physical, candidate)) throw new Error(`${label} must not traverse a link or junction: ${candidate}`);
}

function assertNearestExistingAncestor(candidate, label) {
  let probe = candidate;
  while (!fs.existsSync(probe)) {
    const parent = path.dirname(probe);
    if (parent === probe) throw new Error(`no existing ancestor for ${label}: ${candidate}`);
    probe = parent;
  }
  assertPlainDirectory(probe, `${label} ancestor`);
}

/** Resolve and create an absolute, physical temp base confined beneath AI/work. */
function resolveTempBase() {
  const configured = process.env.KAIZEN_TEST_TEMP_ROOT?.trim();
  if (configured && !path.isAbsolute(configured)) throw new Error("KAIZEN_TEST_TEMP_ROOT must be absolute");
  const candidate = path.resolve(configured || DEFAULT_TEMP_BASE);
  rejectSystemDrive(candidate);
  if (!isDescendant(WORK_ROOT, candidate)) throw new Error(`test temp root escaped AI/work: ${candidate}`);
  assertNearestExistingAncestor(candidate, "test temp base");
  fs.mkdirSync(candidate, { recursive: true });
  assertPlainDirectory(candidate, "test temp base");
  return candidate;
}

/** Replace any stale per-process root and return a fresh physical child of base. */
function prepareRunRoot(base) {
  const candidate = path.join(base, `extension-npm-${process.pid}-${randomUUID()}`);
  if (!isDescendant(base, candidate)) throw new Error(`test run root escaped its base: ${candidate}`);
  if (fs.existsSync(candidate)) {
    assertPlainDirectory(candidate, "existing test run root");
    fs.rmSync(candidate, { recursive: true, force: true, maxRetries: 3, retryDelay: 50 });
  }
  fs.mkdirSync(candidate, { recursive: false });
  assertPlainDirectory(candidate, "test run root");
  return candidate;
}

/** Best-effort remove an existing physical run root only when it remains confined beneath base. */
function removeRunRoot(base, runRoot) {
  if (!isDescendant(base, runRoot) || !fs.existsSync(runRoot)) return;
  assertPlainDirectory(runRoot, "test run root cleanup target");
  fs.rmSync(runRoot, { recursive: true, force: true, maxRetries: 3, retryDelay: 50 });
}

function run(command, args, env) {
  const result = spawnSync(command, args, { cwd: EXTENSION_ROOT, env, stdio: "inherit", shell: false });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`${path.basename(command)} exited with ${result.status ?? result.signal ?? "no status"}`);
}

const TEST_FILES = [
  "protocol.test.js",
  "state.test.js",
  "eventReducer.test.js",
  "followUpQueue.test.js",
  "safeMarkdown.test.js",
  "sessionClient.test.js",
  "sessionIndex.test.js",
  "panelState.test.js",
  "historyModel.test.js",
  "mentionIndex.test.js",
  "contextStager.test.js",
  "imageStager.test.js",
  "streamReducer.test.js",
  "diffModel.test.js",
  "conversationController.test.js",
  "chatPanelManager.test.js",
  "chatAssets.test.js",
  "startDaemon.test.js",
  "testExtensionCore.test.js",
  "testExtensionTerminal.test.js",
  "tempRoot.test.js",
];

const tempBase = resolveTempBase();
const runRoot = prepareRunRoot(tempBase);
const compiledRoot = path.join(runRoot, "compiled");
const planeRoot = path.join(runRoot, "plane");
fs.mkdirSync(planeRoot, { recursive: true });
if (!isDescendant(runRoot, planeRoot)) throw new Error("test plane root escaped its validated run root");
assertPlainDirectory(planeRoot, "test plane root");
const env = {
  ...process.env,
  KAIZEN_EXTENSION_ROOT: EXTENSION_ROOT,
  KAIZEN_REPO_ROOT: planeRoot,
  KAIZEN_TEST_TEMP_ROOT: runRoot,
  TEMP: runRoot,
  TMP: runRoot,
  TMPDIR: runRoot,
  PYTHONPYCACHEPREFIX: path.join(runRoot, "pycache"),
  NODE_COMPILE_CACHE: path.join(runRoot, "node-compile-cache"),
};

try {
  const tsc = path.join(EXTENSION_ROOT, "node_modules", "typescript", "bin", "tsc");
  run(process.execPath, [tsc, "-p", path.join(EXTENSION_ROOT, "tsconfig.json")], env);
  run(process.execPath, [tsc, "-p", path.join(EXTENSION_ROOT, "src", "webview")], env);
  // tests/extension/tsconfig.json rootDir ../.. preserves tests/extension/*.js beneath this isolated outDir.
  run(process.execPath, [
    tsc, "-p", path.join(TEST_ROOT, "tsconfig.json"), "--outDir", compiledRoot,
  ], env);
  const compiledTests = TEST_FILES.map((name) => path.join(compiledRoot, "tests", "extension", name));
  for (const compiledTest of compiledTests) {
    if (!fs.existsSync(compiledTest)) throw new Error(`compiled extension test is missing: ${compiledTest}`);
  }
  run(process.execPath, ["--test", ...compiledTests], env);
  run(sharedPython({ repoRoot: REPO_ROOT, env }), [
    "-B", "-m", "unittest", "discover", "-s", TEST_ROOT, "-p", "test_build_vsix.py",
  ], env);
} finally {
  removeRunRoot(tempBase, runRoot);
}
