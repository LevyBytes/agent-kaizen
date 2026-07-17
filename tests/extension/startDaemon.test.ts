import assert from "node:assert/strict";
import { test } from "node:test";

import {
  DAEMON_TERMINAL_NAME,
  daemonTerminalOptions,
  startDaemonTerminal,
  TerminalOptionsShape,
  TerminalPort,
  WindowPort,
} from "../../extension/src/startDaemon";

class FakeTerminal implements TerminalPort {
  shown = 0;
  preserveFocus: boolean | undefined;

  constructor(
    readonly name: string,
    public exitStatus: unknown = undefined,
  ) {}

  show(preserveFocus?: boolean): void {
    this.shown += 1;
    this.preserveFocus = preserveFocus;
  }
}

class FakeWindow implements WindowPort<FakeTerminal> {
  readonly created: TerminalOptionsShape[] = [];

  constructor(public terminals: FakeTerminal[] = []) {}

  createTerminal(options: TerminalOptionsShape): FakeTerminal {
    this.created.push(options);
    const terminal = new FakeTerminal(options.name);
    this.terminals = [...this.terminals, terminal];
    return terminal;
  }
}

test("terminal options: the terminal IS the daemon process — python + kaizen.py daemon run, cwd workspace", () => {
  const options = daemonTerminalOptions("D:/venv/python.exe", "D:/repo", "D:/repo/kaizen.py");
  assert.deepEqual(options, {
    name: DAEMON_TERMINAL_NAME,
    cwd: "D:/repo",
    shellPath: "D:/venv/python.exe",
    shellArgs: ["D:/repo/kaizen.py", "daemon", "run"],
  });
});

test("no live daemon terminal: creates one and reveals it", () => {
  const window = new FakeWindow();
  const { terminal, created } = startDaemonTerminal(window, "py", "D:/repo", "D:/repo/kaizen.py");
  assert.equal(created, true);
  assert.equal(window.created.length, 1);
  assert.deepEqual(window.created[0], daemonTerminalOptions("py", "D:/repo", "D:/repo/kaizen.py"));
  assert.equal(terminal.name, DAEMON_TERMINAL_NAME);
  assert.equal((terminal as FakeTerminal).shown, 1);
  assert.equal((terminal as FakeTerminal).preserveFocus, true);
});

test("live daemon terminal exists: reveals it, never spawns a second daemon", () => {
  const live = new FakeTerminal(DAEMON_TERMINAL_NAME);
  const window = new FakeWindow([live]);
  const { terminal, created } = startDaemonTerminal(window, "py", "D:/repo", "D:/repo/kaizen.py");
  assert.equal(created, false);
  assert.equal(terminal, live);
  assert.equal(window.created.length, 0);
  assert.equal(live.shown, 1);
  assert.equal(live.preserveFocus, true);
});

test("exited daemon terminal is dead: a fresh one is created", () => {
  const dead = new FakeTerminal(DAEMON_TERMINAL_NAME, { code: 0 });
  const window = new FakeWindow([dead]);
  const { created } = startDaemonTerminal(window, "py", "D:/repo", "D:/repo/kaizen.py");
  assert.equal(created, true);
  assert.equal(window.created.length, 1);
  assert.equal(dead.shown, 0);
});

test("unrelated terminals are ignored", () => {
  const other = new FakeTerminal("build");
  const window = new FakeWindow([other]);
  const { created } = startDaemonTerminal(window, "py", "D:/repo", "D:/repo/kaizen.py");
  assert.equal(created, true);
  assert.equal(window.created.length, 1);
  assert.equal(other.shown, 0);
});
