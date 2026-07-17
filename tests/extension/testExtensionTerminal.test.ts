import assert from "node:assert/strict";
import { test } from "node:test";

import {
  TEST_EXTENSION_TERMINAL_NAME,
  TestExtensionTerminalOptions,
  TestExtensionTerminalPort,
  TestExtensionWindowPort,
  startTestExtensionTerminal,
  testExtensionLauncherControlState,
  testExtensionTerminalOptions,
  testExtensionTerminalRunId,
} from "../../extension/src/testExtensionTerminal";

/** In-memory terminal port that counts show and dispose calls; undefined exit status means live. */
class FakeTerminal implements TestExtensionTerminalPort {
  shown = 0;
  disposed = 0;
  lastPreserveFocus: boolean | undefined;
  constructor(readonly name: string, public exitStatus: unknown | undefined = undefined) {}
  show(preserveFocus?: boolean): void { this.shown += 1; this.lastPreserveFocus = preserveFocus; }
  dispose(): void { this.disposed += 1; }
}

/** In-memory window port whose created list records every terminal option payload. */
class FakeWindow implements TestExtensionWindowPort<FakeTerminal> {
  created: TestExtensionTerminalOptions[] = [];
  constructor(public terminals: FakeTerminal[] = []) {}
  createTerminal(options: TestExtensionTerminalOptions): FakeTerminal {
    this.created.push(options);
    const terminal = new FakeTerminal(options.name);
    this.terminals.push(terminal);
    return terminal;
  }
}

test("visible terminal invokes the runner directly with separate argv", () => {
  assert.deepEqual(
    testExtensionTerminalOptions(
      "D:/python.exe", "D:/source", "D:/Code.exe", "D:/source/tests/run_test_extension.py",
      "D:/test-extension", "te-20260714T120000Z-a1b2c3d4",
    ),
    {
      name: `${TEST_EXTENSION_TERMINAL_NAME} (te-20260714T120000Z-a1b2c3d4)`,
      cwd: "D:/source",
      shellPath: "D:/python.exe",
      shellArgs: [
        "-B", "D:/source/tests/run_test_extension.py", "--source-root", "D:/source",
        "--plane-base", "D:/test-extension", "--run-id", "te-20260714T120000Z-a1b2c3d4",
        "--python", "D:/python.exe", "--code-path", "D:/Code.exe",
      ],
    },
  );
});

test("one live Test Extension terminal is reused", () => {
  const live = new FakeTerminal(`${TEST_EXTENSION_TERMINAL_NAME} (te-existing)`);
  const window = new FakeWindow([live]);
  const result = startTestExtensionTerminal(
    window, "py", "D:/source", "code", "runner.py", "D:/test-extension", "te-new",
  );
  assert.equal(result.created, false);
  assert.equal(result.terminal, live);
  assert.equal(result.runId, "te-existing");
  assert.equal(live.shown, 1);
  assert.equal(live.lastPreserveFocus, true);
  assert.equal(window.created.length, 0);
});

test("a live unbound Test Extension terminal is reused without inventing a run id", () => {
  const live = new FakeTerminal(TEST_EXTENSION_TERMINAL_NAME);
  const result = startTestExtensionTerminal(
    new FakeWindow([live]), "py", "D:/source", "code", "runner.py", "D:/test-extension", "te-new",
  );
  assert.equal(result.created, false);
  assert.equal(result.runId, undefined);
  assert.equal(result.terminal, live);
});

test("an exited terminal permits a fresh explicit run", () => {
  const dead = new FakeTerminal(`${TEST_EXTENSION_TERMINAL_NAME} (te-dead)`, { code: 0 });
  const window = new FakeWindow([dead]);
  const result = startTestExtensionTerminal(
    window, "py", "D:/source", "code", "runner.py", "D:/test-extension", "te-fresh",
  );
  assert.equal(result.created, true);
  assert.equal(result.runId, "te-fresh");
  assert.equal(window.created.length, 1);
});

test("a failed terminal also permits a fresh explicit run", () => {
  const dead = new FakeTerminal(`${TEST_EXTENSION_TERMINAL_NAME} (te-failed)`, { code: 1 });
  const result = startTestExtensionTerminal(
    new FakeWindow([dead]), "py", "D:/source", "code", "runner.py", "D:/test-extension", "te-fresh",
  );
  assert.equal(result.created, true);
  assert.equal(result.runId, "te-fresh");
});

test("a forged traversal-named terminal is never reused", () => {
  const forged = new FakeTerminal(`${TEST_EXTENSION_TERMINAL_NAME} (../escape)`);
  const window = new FakeWindow([forged]);
  const result = startTestExtensionTerminal(
    window, "py", "D:/source", "code", "runner.py", "D:/test-extension", "te-safe",
  );
  assert.equal(result.created, true);
  assert.equal(result.runId, "te-safe");
  assert.equal(forged.shown, 0);
  assert.equal(window.created.length, 1);
});

test("launcher control exposes Stop only for an exact live bound run", () => {
  assert.deepEqual(testExtensionLauncherControlState(false, false, false, false), {
    active: false, stopAvailable: false, stopRequested: false, stopping: false,
  });
  assert.deepEqual(testExtensionLauncherControlState(true, true, false, false), {
    active: true, stopAvailable: true, stopRequested: false, stopping: false,
  });
  assert.deepEqual(testExtensionLauncherControlState(true, true, true, false), {
    active: true, stopAvailable: false, stopRequested: true, stopping: false,
  });
  assert.deepEqual(testExtensionLauncherControlState(true, true, false, true), {
    active: true, stopAvailable: false, stopRequested: false, stopping: true,
  });
  assert.equal(testExtensionLauncherControlState(true, true, true, false).stopAvailable, false);
  assert.equal(testExtensionLauncherControlState(true, true, false, true).stopAvailable, false);
  assert.equal(testExtensionLauncherControlState(true, false, false, false).stopAvailable, false);
});

test("terminal run binding accepts exact names and rejects legacy or forged names", () => {
  assert.equal(testExtensionTerminalRunId(`${TEST_EXTENSION_TERMINAL_NAME} (te-bound_1)`), "te-bound_1");
  assert.equal(testExtensionTerminalRunId(TEST_EXTENSION_TERMINAL_NAME), undefined);
  assert.equal(testExtensionTerminalRunId(`${TEST_EXTENSION_TERMINAL_NAME} (../escape)`), undefined);
  assert.throws(
    () => testExtensionTerminalOptions("py", "D:/source", "code", "runner.py", "D:/plane", "../escape"),
    /TEST_EXTENSION_RUN_ID_INVALID/,
  );
});
