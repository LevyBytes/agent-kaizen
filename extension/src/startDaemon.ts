/**
 * Start Daemon action (owner ask 2026-07-10): a button that does exactly what the manual flow does —
 * run `python kaizen.py daemon run` in a VISIBLE terminal the user owns and can Ctrl+C. By owner
 * policy there is NO machine-level persistence (no scheduled task, no service) and no detached or
 * hidden process: the terminal IS the daemon process (shellPath = python), so closing it stops the
 * daemon and a second click reuses the live terminal. The daemon's own single-instance guard makes
 * an accidental duplicate refuse loudly on-screen. Port interfaces keep the helper vscode-free and testable.
 */

/** Stable terminal-name key used to find and reveal the live daemon terminal. */
export const DAEMON_TERMINAL_NAME = "Kaizen Daemon";

/** Local subset of vscode.TerminalOptions used without importing vscode. */
export interface TerminalOptionsShape {
  name: string;
  cwd: string;
  shellPath: string;
  shellArgs: string[];
}

/** The terminal runs the daemon directly — no shell, no quoting hazards across pwsh/cmd/bash. */
export function daemonTerminalOptions(python: string, repoRoot: string, kaizenPy: string): TerminalOptionsShape {
  return {
    name: DAEMON_TERMINAL_NAME,
    cwd: repoRoot,
    shellPath: python,
    shellArgs: [kaizenPy, "daemon", "run"],
  };
}

export interface TerminalPort {
  readonly name: string;
  readonly exitStatus: unknown; // undefined while the process is alive
  show(preserveFocus?: boolean): void;
}

/** Minimal vscode.window surface retained as a testable port. */
export interface WindowPort<T extends TerminalPort> {
  readonly terminals: readonly T[];
  createTerminal(options: TerminalOptionsShape): T;
}

/** VS Code reports an undefined exitStatus only while the terminal process is alive. */
function isAlive(terminal: TerminalPort): boolean {
  return terminal.exitStatus === undefined;
}

/** Reveal the live daemon terminal if one exists; otherwise create (and reveal) a fresh one. */
export function startDaemonTerminal<T extends TerminalPort>(
  window: WindowPort<T>,
  python: string,
  repoRoot: string,
  kaizenPy: string,
): { terminal: T; created: boolean } {
  const existing = window.terminals.find(
    (terminal) => terminal.name === DAEMON_TERMINAL_NAME && isAlive(terminal),
  );
  if (existing) {
    existing.show(true);
    return { terminal: existing, created: false };
  }
  const terminal = window.createTerminal(daemonTerminalOptions(python, repoRoot, kaizenPy));
  terminal.show(true);
  return { terminal, created: true };
}
