/**
 * Shared fake daemon for the extension client tests (node:test). Speaks the exact loopback JSON-lines
 * protocol ({op,args,token}\n -> one line -> close, one exchange per connection) on the real per-OS
 * transport, so both protocol.test.ts (round-trips) and sessionClient.test.ts (start + long-poll pump)
 * exercise the true socket path, not a stub. Extends the original protocol.test fake with:
 *   - per-op RESPONDERS (a function of the request args, so session/events can advance a cursor and go
 *     terminal on a scripted schedule), alongside plain canned objects, and
 *   - a recorder of received requests (assert the exact approve wire shape).
 */

import * as fs from "node:fs";
import * as net from "node:net";
import * as path from "node:path";

import { pipeName, runtimeDir } from "../../extension/src/protocol";
import { makeSocketTestRepoRoot } from "./tempRoot";

export const TOKEN = "test-token-123";
export const WIN = process.platform === "win32";

/** A per-op reply: a fixed object, or a function of the request args (dynamic session/events scripts). */
export type OpResponder = Record<string, unknown> | ((args: Record<string, unknown>, req: Record<string, unknown>) => Record<string, unknown>);

export interface FakeDaemon {
  server: net.Server;
  /** Every request the daemon received, in arrival order (assert wire shapes off this). */
  requests: Array<{ op: string; args: Record<string, unknown>; token: unknown }>;
  /** Stop accepting connections; one-shot tests close each live exchange before calling this. */
  close: () => void;
}

/** Create a temp repo root with a valid control.token so protocol.request authenticates. */
export function fakeRepoRoot(): string {
  const root = makeSocketTestRepoRoot();
  fs.mkdirSync(runtimeDir(root), { recursive: true });
  fs.writeFileSync(path.join(runtimeDir(root), "control.token"), TOKEN, "utf-8");
  return root;
}

/** The default listen path for a root: named pipe on Windows, UDS on POSIX (the daemon's first choice). */
export function listenPathFor(root: string): string {
  return WIN ? pipeName(root) : path.join(runtimeDir(root), "control.sock");
}

/**
 * Start a fake daemon on `listenPath` (a pipe/UDS path, or 0 for an ephemeral TCP port — the Windows
 * fallback leg). Token is checked first (DENIED_LOOPBACK_AUTH on mismatch); then the matching responder
 * runs, else DENIED_UNKNOWN_OP echoing the request. Each connection supports exactly one exchange;
 * malformed JSON and socket/listen errors intentionally surface to the test process.
 */
export function startFakeDaemon(listenPath: string | number, ops: Record<string, OpResponder>): Promise<FakeDaemon> {
  const requests: FakeDaemon["requests"] = [];
  const server = net.createServer((conn) => {
    let buf = "";
    let responded = false;
    conn.on("error", () => conn.destroy());
    conn.on("data", (chunk) => {
      if (responded) return;
      buf += chunk.toString("utf-8");
      const nl = buf.indexOf("\n");
      if (nl < 0) return;
      responded = true;
      let req: { op: string; args?: Record<string, unknown>; token?: unknown };
      try {
        req = JSON.parse(buf.slice(0, nl)) as typeof req;
      } catch {
        conn.end(JSON.stringify({ status: "DENIED", code: "DENIED_BAD_JSON" }) + "\n");
        return;
      }
      const args = req.args ?? {};
      requests.push({ op: req.op, args, token: req.token });
      let reply: Record<string, unknown>;
      if (req.token !== TOKEN) {
        reply = { status: "DENIED", code: "DENIED_LOOPBACK_AUTH" };
      } else {
        const responder = ops[req.op];
        reply =
          responder === undefined
            ? { status: "DENIED", code: "DENIED_UNKNOWN_OP", echo: req }
            : typeof responder === "function"
              ? responder(args, req as Record<string, unknown>)
              : responder;
      }
      conn.end(JSON.stringify(reply) + "\n");
    });
  });
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(listenPath, () => {
      server.off("error", reject);
      server.on("error", () => server.close());
      resolve({ server, requests, close: () => server.close() });
    });
  });
}
