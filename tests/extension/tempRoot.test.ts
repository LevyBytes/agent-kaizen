import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";

import { assertAmbientTempIsPinned, makeTestTempDir, testTempRoot } from "./tempRoot";

test("test runner pins ambient temp variables to its guarded D-workspace root", () => {
  assertAmbientTempIsPinned();
  const root = testTempRoot();
  const child = makeTestTempDir("temp-root-proof-");
  assert.equal(path.dirname(child), root);
  fs.rmSync(child, { recursive: true, force: true });
});

test("test entrypoint routes npm and every child beneath guarded AI/work scratch", () => {
  const extensionRoot = path.resolve(process.cwd());
  const repoRoot = path.resolve(extensionRoot, "..");
  const runner = fs.readFileSync(path.join(repoRoot, "tests", "extension", "run-tests.mjs"), "utf-8");
  const npmrc = fs.readFileSync(path.join(extensionRoot, ".npmrc"), "utf-8");
  // Deliberate source-level tamper guard: update these wiring assertions when refactoring the runner.
  assert.match(runner, /if \(!isDescendant\(WORK_ROOT, candidate\)\)/);
  assert.match(runner, /KAIZEN_TEST_TEMP_ROOT: runRoot/);
  assert.match(runner, /TEMP: runRoot[\s\S]*TMP: runRoot[\s\S]*TMPDIR: runRoot/);
  assert.match(runner, /PYTHONPYCACHEPREFIX: path\.join\(runRoot, "pycache"\)/);
  assert.match(runner, /NODE_COMPILE_CACHE: path\.join\(runRoot, "node-compile-cache"\)/);
  assert.match(runner, /const compiledRoot = path\.join\(runRoot, "compiled"\)/);
  assert.match(runner, /sharedPython\(\{ repoRoot: REPO_ROOT, env \}\)/);
  assert.match(npmrc, /cache=\.\.\/AI\/work\/test-temp\/npm-cache/);
  assert.match(npmrc, /logs-dir=\.\.\/AI\/work\/test-temp\/npm-logs/);
});

test("unset Python selectors discover the shared sibling venv without a nonexistent fs API", () => {
  const extensionRoot = path.resolve(process.cwd());
  const repoRoot = path.resolve(extensionRoot, "..");
  const selector = require(path.join(repoRoot, "tests", "extension", "pythonSelection.cjs")) as {
    sharedPython(options: { env: NodeJS.ProcessEnv; platform: NodeJS.Platform; repoRoot: string }): string;
  };
  const selected = selector.sharedPython({ env: {}, platform: process.platform, repoRoot });
  const expected =
    process.platform === "win32"
      ? path.join(path.dirname(repoRoot), "Python", "venvs", "kaizen", "Scripts", "python.exe")
      : path.join(path.dirname(repoRoot), "Python", "venvs", "kaizen", "bin", "python");
  if (fs.existsSync(expected) && fs.statSync(expected).isFile()) {
    assert.equal(path.resolve(selected), path.resolve(expected));
  } else {
    assert.equal(selected, process.platform === "win32" ? "python.exe" : "python3");
  }
});

test("Python selector precedence and DEVROOT override are explicit", () => {
  const extensionRoot = path.resolve(process.cwd());
  const repoRoot = path.resolve(extensionRoot, "..");
  const selector = require(path.join(repoRoot, "tests", "extension", "pythonSelection.cjs")) as {
    sharedPython(options: { env: NodeJS.ProcessEnv; platform: NodeJS.Platform; repoRoot: string }): string;
  };
  assert.equal(selector.sharedPython({ env: { KAIZEN_PYTHON: "  kaizen-override  ", PYTHON: "python-override" }, platform: process.platform, repoRoot }), "kaizen-override");
  assert.equal(selector.sharedPython({ env: { PYTHON: "  python-override  " }, platform: process.platform, repoRoot }), "python-override");

  const devRoot = makeTestTempDir("python-devroot-");
  const candidate = process.platform === "win32"
    ? path.join(devRoot, "Python", "venvs", "kaizen", "Scripts", "python.exe")
    : path.join(devRoot, "Python", "venvs", "kaizen", "bin", "python");
  fs.mkdirSync(path.dirname(candidate), { recursive: true });
  fs.writeFileSync(candidate, "fixture");
  assert.equal(selector.sharedPython({ env: { DEVROOT: `${devRoot}${path.sep}` }, platform: process.platform, repoRoot: `${repoRoot}${path.sep}` }), candidate);
  fs.rmSync(devRoot, { recursive: true, force: true });
});
