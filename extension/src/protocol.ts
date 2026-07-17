/**
 * Loopback client for the Kaizen supervisor daemon (v8 M8, plan §D — mirrors
 * kaizen_components/orchestration/loopback.py byte-for-byte).
 *
 * Wire: ONE JSON object `{op, args, token}` + "\n" per connection; epoch is handler-optional and
 * this client omits it. The server replies with one JSON line and closes. Transports, in daemon preference order: Windows named pipe
 * `\\.\pipe\kaizen-<sha256(lowercased resolved repo root)[:16]>`, TCP fallback advertised in the
 * owner-only `<runtime>/control.addr`, POSIX UDS `<runtime>/control.sock`. The shared token is minted
 * at daemon start into `<runtime>/control.token`; the server refuses DENIED_LOOPBACK_AUTH without it.
 *
 * Pure Node (no vscode import) so the whole module is unit-testable under node:test.
 */

import * as crypto from "crypto";
import * as fs from "fs";
import * as net from "net";
import * as path from "path";

export const MAX_LINE_BYTES = 1 << 20; // matches loopback.py _MAX_LINE_BYTES
const LOOPBACK_TIMEOUT = "loopback timeout";

/** Decoded reply-line shape with optional status/code and pass-through fields. */
export interface LoopbackResponse {
  status?: string;
  code?: string;
  [key: string]: unknown;
}

/** Thrown when no daemon transport answers — the UI's clean "daemon not running" state. */
export class DaemonUnreachable extends Error {}

/** Internal transport error; fallbackSafe means no request bytes crossed the failed transport. */
class LoopbackAttemptError extends DaemonUnreachable {
  constructor(message: string, readonly fallbackSafe: boolean) {
    super(message);
  }
}

/** Canonical outbound-or-inbound PAYLOAD_TOO_LARGE response matching the Python client. */
export function payloadTooLarge(): LoopbackResponse {
  return { status: "DENIED", code: "PAYLOAD_TOO_LARGE", retryable: false, limit_bytes: MAX_LINE_BYTES };
}

/** Hash the resolved root to 16 hex characters, lowercasing only on Windows for parity. */
export function workspaceHash(repoRoot: string, platform: string = process.platform): string {
  // loopback.workspace_hash: sha256 over the RESOLVED root, lowercased on Windows only.
  const resolved = path.resolve(repoRoot);
  const text = platform === "win32" ? resolved.toLowerCase() : resolved;
  return crypto.createHash("sha256").update(text, "utf-8").digest("hex").slice(0, 16);
}

/** Return the Windows named-pipe transport name for a workspace. */
export function pipeName(repoRoot: string): string {
  return `\\\\.\\pipe\\kaizen-${workspaceHash(repoRoot)}`;
}

/** Return the fixed workspace-local orchestration runtime directory. */
export function runtimeDir(repoRoot: string): string {
  return path.join(repoRoot, "AI", "work", "orchestration", "runtime");
}

/** Read the trimmed control token, or null when the daemon is unavailable. */
export function readToken(repoRoot: string): string | null {
  try {
    return fs.readFileSync(path.join(runtimeDir(repoRoot), "control.token"), "utf-8").trim();
  } catch {
    return null;
  }
}

/** Perform one JSON-line exchange; every rejection is a LoopbackAttemptError. */
function exchange(connectOpts: net.NetConnectOpts, payload: string, timeoutMs: number): Promise<LoopbackResponse> {
  return new Promise((resolve, reject) => {
    const sock = net.connect(connectOpts);
    let buf = Buffer.alloc(0);
    let settled = false;
    let connected = false;
    const fail = (err: Error) => {
      if (!settled) {
        settled = true;
        sock.destroy();
        reject(err);
      }
    };
    sock.setTimeout(timeoutMs, () => fail(new LoopbackAttemptError(LOOPBACK_TIMEOUT, false)));
    sock.on("error", (err) => fail(new LoopbackAttemptError(String(err), !connected)));
    sock.on("connect", () => {
      connected = true;
      sock.write(payload);
    });
    sock.on("data", (chunk) => {
      buf = Buffer.concat([buf, chunk]);
      if (buf.byteLength > MAX_LINE_BYTES && !settled) {
        settled = true;
        sock.destroy();
        resolve(payloadTooLarge());
        return;
      }
      const nl = buf.indexOf(0x0a);
      if (nl >= 0 && !settled) {
        settled = true;
        sock.destroy();
        try {
          const reply: unknown = JSON.parse(buf.subarray(0, nl).toString("utf-8"));
          if (!reply || typeof reply !== "object" || Array.isArray(reply)) throw new Error("reply is not an object");
          resolve(reply as LoopbackResponse);
        } catch (err) {
          reject(new LoopbackAttemptError(`malformed reply: ${err}`, false));
        }
      }
    });
    sock.on("close", () => fail(new LoopbackAttemptError("daemon closed without a reply", !connected)));
  });
}

/**
 * One request/response round-trip. DENIED/ERROR payloads RESOLVE (they are structured answers the UI
 * renders — e.g. DENIED_STALE_FENCE on attach); only transport failures reject (DaemonUnreachable).
 * On Windows, timeoutMs bounds the combined pipe attempt and safe TCP fallback.
 */
export async function request(
  repoRoot: string,
  op: string,
  args: Record<string, unknown> = {},
  timeoutMs = 5000,
): Promise<LoopbackResponse> {
  const token = readToken(repoRoot);
  if (token === null) throw new DaemonUnreachable("no control.token (daemon not running?)");
  const payload = JSON.stringify({ op, args, token }) + "\n";
  if (Buffer.byteLength(payload, "utf-8") > MAX_LINE_BYTES) return payloadTooLarge();

  if (process.platform === "win32") {
    const deadline = Date.now() + timeoutMs;
    try {
      return await exchange({ path: pipeName(repoRoot) }, payload, timeoutMs);
    } catch (error) {
      // Only a pre-connect failure proves no request bytes crossed the pipe. A timeout, malformed
      // response, close, or post-connect error may follow an accepted request and must never replay it.
      if (!(error instanceof LoopbackAttemptError) || !error.fallbackSafe) throw error;
    }
    let addr: string;
    try {
      addr = fs.readFileSync(path.join(runtimeDir(repoRoot), "control.addr"), "utf-8").trim();
    } catch {
      throw new DaemonUnreachable("no loopback transport (pipe/addr) present");
    }
    const i = addr.lastIndexOf(":");
    const port = Number(addr.slice(i + 1));
    if (i < 1 || !Number.isInteger(port) || port < 1 || port > 65_535) {
      throw new DaemonUnreachable("malformed control.addr");
    }
    const remainingMs = deadline - Date.now();
    if (remainingMs <= 0) throw new DaemonUnreachable(LOOPBACK_TIMEOUT);
    return exchange({ host: addr.slice(0, i), port }, payload, remainingMs);
  }
  return exchange({ path: path.join(runtimeDir(repoRoot), "control.sock") }, payload, timeoutMs);
}
