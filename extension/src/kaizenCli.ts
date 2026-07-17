/**
 * Read-side bridge (v8 M8, plan §D: the extension "renders C5/R0/D fleet reads"): spawns
 * `python kaizen.py <op> ... --json` in the workspace and parses the JSON reply. Reads only —
 * every CONTROL mutation goes through the loopback (protocol.ts), never the CLI.
 */

import { execFile } from "child_process";
import * as fs from "fs";
import * as path from "path";

/** Prefer an explicit interpreter, then the DEVROOT shared venv, then PATH python. */
export function resolvePython(repoRoot: string, configured: string): string {
  if (configured) return configured;
  const devroot = process.env.DEVROOT;
  if (devroot) {
    const venv =
      process.platform === "win32"
        ? path.join(devroot, "Python", "venvs", "kaizen", "Scripts", "python.exe")
        : path.join(devroot, "Python", "venvs", "kaizen", "bin", "python");
    if (fs.existsSync(venv)) return venv;
  }
  return "python";
}

/** Parse structured JSON even on nonzero exits; reject only when the bounded reply has no JSON. */
export function kaizenRead(
  repoRoot: string,
  python: string,
  args: string[],
  timeoutMs = 30000,
): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    execFile(
      python,
      [path.join(repoRoot, "kaizen.py"), ...args, "--json"],
      { cwd: repoRoot, timeout: timeoutMs, maxBuffer: 8 * 1024 * 1024, windowsHide: true },
      (err, stdout, stderr) => {
        // kaizen.py emits structured JSON on DENIED paths too (nonzero exit) — parse before failing.
        const text = String(stdout ?? "");
        const parsed = parseJsonReply(text);
        if (parsed) return resolve(parsed);
        const operation = args[0] ?? "<op>";
        const detail = String(stderr ?? "").trim().slice(-512);
        const overflow = err && "code" in err && err.code === "ERR_CHILD_PROCESS_STDIO_MAXBUFFER";
        reject(new Error(
          overflow
            ? `kaizen.py ${operation} output exceeded 8 MiB`
            : `no JSON from kaizen.py ${operation}${detail ? `: ${detail}` : ""}`,
        ));
      },
    );
  });
}

function parseJsonReply(output: string): Record<string, unknown> | undefined {
  const candidates = [output.trim(), ...output.split(/\r?\n/).reverse().map((line) => line.trim())];
  for (const candidate of candidates) {
    if (!candidate.startsWith("{") || !candidate.endsWith("}")) continue;
    try {
      const parsed: unknown = JSON.parse(candidate);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed as Record<string, unknown>;
    } catch {
      // Try the next clean JSON candidate.
    }
  }
  return undefined;
}
