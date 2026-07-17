export const TEST_EXTENSION_TERMINAL_NAME = "Kaizen Test Extension";

const TEST_EXTENSION_RUN_ID = /^te-[A-Za-z0-9_-]{1,120}$/;

export interface TestExtensionTerminalOptions {
  name: string;
  cwd: string;
  shellPath: string;
  shellArgs: string[];
}

export interface TestExtensionTerminalPort {
  readonly name: string;
  readonly exitStatus: unknown | undefined;
  show(preserveFocus?: boolean): void;
  dispose(): void;
}

export interface TestExtensionWindowPort<T extends TestExtensionTerminalPort> {
  readonly terminals: readonly T[];
  createTerminal(options: TestExtensionTerminalOptions): T;
}

export interface TestExtensionLauncherControlState {
  active: boolean;
  stopAvailable: boolean;
  stopRequested: boolean;
  stopping: boolean;
}

/** Derive controls from positional active, bound, stop-requested, and stopping flags. */
export function testExtensionLauncherControlState(
  active: boolean,
  bound: boolean,
  stopRequested: boolean,
  stopping: boolean,
): TestExtensionLauncherControlState {
  return {
    active,
    stopAvailable: active && bound && !stopping && !stopRequested,
    stopRequested,
    stopping,
  };
}

/** Build direct argv terminal options; throw when the run id or required paths are invalid. */
export function testExtensionTerminalOptions(
  python: string,
  sourceRoot: string,
  codePath: string,
  runnerPath: string,
  planeBase: string,
  runId: string,
): TestExtensionTerminalOptions {
  if (!TEST_EXTENSION_RUN_ID.test(runId)) throw new Error("TEST_EXTENSION_RUN_ID_INVALID");
  if ([python, sourceRoot, codePath, runnerPath, planeBase].some((value) => !value.trim())) {
    throw new Error("TEST_EXTENSION_TERMINAL_PATH_INVALID");
  }
  return {
    name: `${TEST_EXTENSION_TERMINAL_NAME} (${runId})`,
    cwd: sourceRoot,
    shellPath: python,
    shellArgs: [
      "-B", runnerPath, "--source-root", sourceRoot, "--plane-base", planeBase,
      "--run-id", runId, "--python", python, "--code-path", codePath,
    ],
  };
}

export function testExtensionTerminalRunId(name: string): string | undefined {
  const match = name.match(/^Kaizen Test Extension \((te-[A-Za-z0-9_-]{1,120})\)$/);
  return match?.[1];
}

function isTestExtensionTerminal(name: string): boolean {
  return name === TEST_EXTENSION_TERMINAL_NAME || testExtensionTerminalRunId(name) !== undefined;
}

export function startTestExtensionTerminal<T extends TestExtensionTerminalPort>(
  window: TestExtensionWindowPort<T>,
  python: string,
  sourceRoot: string,
  codePath: string,
  runnerPath: string,
  planeBase: string,
  runId: string,
): { terminal: T; created: boolean; runId?: string } {
  const existing = window.terminals.find(
    (terminal) => isTestExtensionTerminal(terminal.name) && terminal.exitStatus === undefined,
  );
  if (existing) {
    existing.show(true);
    return { terminal: existing, created: false, runId: testExtensionTerminalRunId(existing.name) };
  }
  const terminal = window.createTerminal(
    testExtensionTerminalOptions(python, sourceRoot, codePath, runnerPath, planeBase, runId),
  );
  terminal.show(true);
  return { terminal, created: true, runId };
}
