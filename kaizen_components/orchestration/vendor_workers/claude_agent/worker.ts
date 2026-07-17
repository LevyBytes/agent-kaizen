/** Fail-closed line-delimited JSON protocol bridge between the host adapter and one Claude Agent SDK session; stdout carries protocol frames only. */

import { createHash, randomUUID } from "node:crypto";
import { lstat, mkdir, readFile, realpath, rename, stat, unlink, writeFile } from "node:fs/promises";
import { dirname, isAbsolute, join, parse, relative, resolve, sep } from "node:path";
import { createInterface } from "node:readline";
import { TextDecoder } from "node:util";

import {
  createSdkMcpServer,
  query,
  tool,
  type Query,
  type SDKMessage,
  type SDKUserMessage,
} from "@anthropic-ai/claude-agent-sdk";
import { z } from "zod";

declare global { type HeadersInit = NonNullable<RequestInit["headers"]>; }

type ToolCallResult = Awaited<ReturnType<Parameters<typeof tool>[3]>>;
type UserMessageContent = Exclude<SDKUserMessage["message"]["content"], string>;

const PROTOCOL_VERSION = 1;
const SDK_VERSION = "0.3.207";
const MAX_FRAME_BYTES = 1_048_576;
const MAX_DELTA_BYTES = 32_768;
const MAX_CONTEXT_BYTES = 256 * 1024;
const MAX_CONTEXT_TOTAL_BYTES = 1_048_576;
const MAX_IMAGE_BYTES = 4 * 1024 * 1024;
const MAX_PROPOSAL_FILE_BYTES = 8 * 1024 * 1024;
const MAX_PROPOSAL_TOTAL_BYTES = 64 * 1024 * 1024;
const TOOL_RESULT_INLINE_BYTES = 256 * 1024;
const MAX_TOOL_PATH_CHARS = 1_024;
const MAX_GLOB_CHARS = 1_024;
const MAX_SEARCH_QUERY_BYTES = 4_096;
const MAX_PROCESS_ARGS = 64;
const MAX_PROCESS_ARG_BYTES = 4_096;
const MAX_PROPOSAL_SUMMARY_CHARS = 256;
const IDENTIFIER = /^[A-Za-z0-9._:-]{1,128}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const EFFORTS = new Set(["low", "medium", "high", "xhigh", "max"]);
const TEXT_CONTROL_OK = new Set([9, 10, 13]);
const TOOL_NAMES = [
  "kaizen_read_file",
  "kaizen_list_files",
  "kaizen_search_text",
  "kaizen_run_process",
  "kaizen_propose_changes",
] as const;
const CAPABILITY_PROBE_VERSION = 1;
const CAPABILITY_PROBE_FEATURES = [
  "streaming",
  "image_attachments",
  "governed_context",
  "diff_snapshots",
  "controlled_tools",
  "process_execution",
] as const;
type CapabilityProbeFeature = typeof CAPABILITY_PROBE_FEATURES[number];
const CAPABILITY_PROBE_EVIDENCE: Record<CapabilityProbeFeature, string> = {
  streaming: "sdk-delta-parser-and-32k-fragmentation",
  image_attachments: "verified-reference-to-sdk-image-block",
  governed_context: "verified-reference-to-governed-prompt",
  diff_snapshots: "proposal-outbox-reference-roundtrip",
  controlled_tools: "exact-kaizen-tool-schema-set",
  process_execution: "direct-argv-process-schema",
};
const MCP_TOOL_NAMES = new Set(TOOL_NAMES.map((name) => `mcp__kaizen__${name}`));
const HOST_OPERATIONS = new Set([
  "initialize",
  "capability.probe",
  "turn.start",
  "turn.steer",
  "turn.interrupt",
  "session.close",
  "tool.result",
]);
const TURN_EVENTS = new Set([
  "turn.open", "delta", "status", "rate_limit", "tool.invoke", "tool.open", "tool.close", "turn.result",
]);
const SYSTEM_PROMPT = [
  "You are an AI coding agent operating inside the Agent Kaizen SAVMI harness.",
  "Use only the five provided kaizen tools for workspace reads, search, process requests, and change proposals.",
  "Never imply that a proposed change was applied until its tool result confirms a verified apply.",
  "Do not use vendor-native file, shell, web, subagent, skill, plugin, MCP discovery, or @file expansion surfaces.",
  "A rejected tool request is authoritative; explain or choose a non-mutating alternative.",
].join(" ");

type JsonObject = Record<string, unknown>;
type Effort = "low" | "medium" | "high" | "xhigh" | "max";

interface RequestFrame {
  v: 1;
  type: "request";
  id: string;
  op: string;
  session_id?: string;
  turn_id?: string;
  body: JsonObject;
}

interface LocalReference {
  root: "runtime" | "cache";
  path: string;
  sha256: string;
  bytes: number;
  encoding?: "utf-8";
  media_type?: "image/png" | "image/jpeg" | "image/webp" | "image/gif";
}

interface ModelEntry {
  id: string;
  label: string;
  description: string;
  reasoning_efforts: Effort[];
  supports_adaptive_thinking: boolean;
  supports_fast_mode: boolean;
}

interface ToolResolution {
  ok: boolean;
  body: JsonObject;
}

class ProtocolFailure extends Error {
  constructor(readonly code = "DENIED_WORKER_PROTOCOL", readonly field = "frame") {
    super(`${code}: ${field}`);
  }
}

/** Single-consumer asynchronous SDK input queue that rejects pushes after closure. */
class AsyncInput implements AsyncIterable<SDKUserMessage> {
  private readonly items: SDKUserMessage[] = [];
  private readonly waiters: Array<(value: IteratorResult<SDKUserMessage>) => void> = [];
  private closed = false;

  push(message: SDKUserMessage): void {
    if (this.closed) throw new ProtocolFailure("WORKER_DIED", "input_closed");
    const waiter = this.waiters.shift();
    if (waiter) waiter({ value: message, done: false });
    else this.items.push(message);
  }

  close(): void {
    this.closed = true;
    while (this.waiters.length) this.waiters.shift()?.({ value: undefined, done: true });
  }

  [Symbol.asyncIterator](): AsyncIterator<SDKUserMessage> {
    return {
      next: async () => {
        const item = this.items.shift();
        if (item) return { value: item, done: false };
        if (this.closed) return { value: undefined, done: true };
        return await new Promise<IteratorResult<SDKUserMessage>>((resolveNext) => this.waiters.push(resolveNext));
      },
    };
  }
}

const protocolWrite = process.stdout.write.bind(process.stdout);
let stderrSuppressed = false;

function suppressProviderStderr(): void {
  if (stderrSuppressed) return;
  stderrSuppressed = true;
  process.stderr.write("[claude-agent-sdk stderr suppressed]\n");
}

console.log = () => undefined;
console.info = () => undefined;
console.debug = () => undefined;

function objectValue(value: unknown, field: string): JsonObject {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", field);
  }
  return value as JsonObject;
}

function stringValue(value: unknown, field: string, max = 4096): string {
  if (typeof value !== "string" || value.length < 1 || value.length > max) {
    throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", field);
  }
  return value;
}

function idValue(value: unknown, field: string): string {
  const result = stringValue(value, field, 128);
  if (!IDENTIFIER.test(result)) throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", field);
  return result;
}

function integerValue(
  value: unknown,
  field: string,
  minimum: number,
  maximum: number,
  code = "DENIED_WORKER_PROTOCOL",
): number {
  if (!Number.isInteger(value) || (value as number) < minimum || (value as number) > maximum) {
    throw new ProtocolFailure(code, field);
  }
  return value as number;
}

function decodeDeclaredUtf8(content: Buffer): string {
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(content);
  } catch {
    throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "reference.encoding");
  }
  for (const point of text) {
    const codepoint = point.codePointAt(0)!;
    if (codepoint < 32 && !TEXT_CONTROL_OK.has(codepoint)) {
      throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "reference.encoding");
    }
  }
  return text;
}

function assertDisabledSurface(value: unknown, field: string): void {
  if (value === undefined) return;
  if (!Array.isArray(value) || value.length !== 0) {
    throw new ProtocolFailure("DENIED_TOOL_UNSUPPORTED", field);
  }
}

function validateRequest(raw: string): RequestFrame {
  if (Buffer.byteLength(raw, "utf8") + 1 > MAX_FRAME_BYTES) {
    throw new ProtocolFailure("DENIED_WORKER_OVERSIZE", "frame");
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new ProtocolFailure();
  }
  const frame = objectValue(parsed, "frame");
  const allowed = new Set(["v", "type", "id", "op", "session_id", "turn_id", "body"]);
  if (Object.keys(frame).some((key) => !allowed.has(key)) || frame.v !== PROTOCOL_VERSION || frame.type !== "request") {
    throw new ProtocolFailure();
  }
  const id = idValue(frame.id, "id");
  const op = stringValue(frame.op, "op", 64);
  if (!HOST_OPERATIONS.has(op)) throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "op");
  const request: RequestFrame = { v: 1, type: "request", id, op, body: objectValue(frame.body, "body") };
  if (frame.session_id !== undefined) request.session_id = idValue(frame.session_id, "session_id");
  if (frame.turn_id !== undefined) request.turn_id = idValue(frame.turn_id, "turn_id");
  if (!request.session_id) throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "session_id");
  if (["turn.start", "turn.steer", "turn.interrupt", "tool.result"].includes(op)) {
    if (!request.turn_id) throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "turn_id");
  } else if (request.turn_id) {
    throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "turn_id");
  }
  return request;
}

function sendFrame(frame: JsonObject): void {
  const encoded = `${JSON.stringify(frame)}\n`;
  if (Buffer.byteLength(encoded, "utf8") > MAX_FRAME_BYTES) {
    throw new ProtocolFailure("DENIED_WORKER_OVERSIZE", "output_frame");
  }
  protocolWrite(encoded);
}

function splitUtf8(text: string, maximum: number): string[] {
  const chunks: string[] = [];
  let current = "";
  let currentBytes = 0;
  for (const point of text) {
    const pointBytes = Buffer.byteLength(point, "utf8");
    if (current && currentBytes + pointBytes > maximum) {
      chunks.push(current);
      current = "";
      currentBytes = 0;
    }
    current += point;
    currentBytes += pointBytes;
  }
  if (current) chunks.push(current);
  return chunks;
}

function capabilityProbeDigest(feature: CapabilityProbeFeature, challenge: string): string {
  const material = `kaizen-capability-probe-v${CAPABILITY_PROBE_VERSION}\0${feature}\0${challenge}\0${CAPABILITY_PROBE_EVIDENCE[feature]}`;
  return createHash("sha256").update(material, "utf8").digest("hex");
}

function sanitizeCatalog(value: unknown): ModelEntry[] {
  if (!Array.isArray(value) || value.length < 1) throw new ProtocolFailure("DENIED_MODEL_UNAVAILABLE", "models");
  const seen = new Set<string>();
  return value.map((raw, index) => {
    const entry = objectValue(raw, `models[${index}]`);
    const id = stringValue(entry.value, `models[${index}].value`, 256);
    const label = stringValue(entry.displayName, `models[${index}].displayName`, 256);
    const description = typeof entry.description === "string" ? entry.description.slice(0, 1024) : "";
    if (seen.has(id)) throw new ProtocolFailure("DENIED_MODEL_UNAVAILABLE", `models[${index}].value`);
    seen.add(id);
    const rawEfforts = entry.supportedEffortLevels ?? [];
    if (!Array.isArray(rawEfforts) || rawEfforts.some((item) => typeof item !== "string" || !EFFORTS.has(item))) {
      throw new ProtocolFailure("DENIED_MODEL_UNAVAILABLE", `models[${index}].supportedEffortLevels`);
    }
    const efforts = [...new Set(rawEfforts)] as Effort[];
    if (efforts.length !== rawEfforts.length) {
      throw new ProtocolFailure("DENIED_MODEL_UNAVAILABLE", `models[${index}].supportedEffortLevels`);
    }
    return {
      id,
      label,
      description,
      reasoning_efforts: efforts,
      supports_adaptive_thinking: entry.supportsAdaptiveThinking === true,
      supports_fast_mode: entry.supportsFastMode === true,
    };
  });
}

function containedPath(candidate: string, root: string): boolean {
  const rel = relative(root, candidate);
  return rel === "" || (!rel.startsWith(`..${sep}`) && rel !== ".." && !isAbsolute(rel));
}

function samePath(left: string, right: string): boolean {
  return containedPath(left, right) && containedPath(right, left);
}

function errorCode(error: unknown): string | undefined {
  return (error as NodeJS.ErrnoException).code;
}

/** Validate a plain directory against the single-user pre-existing-swap threat model; this is not a handle-relative hostile-concurrency guarantee. */
async function assertPlainDirectory(path: string, field: string, containingRoot?: string): Promise<string> {
  const lexical = resolve(path);
  let info;
  try {
    info = await lstat(lexical);
  } catch {
    throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", field);
  }
  if (!info.isDirectory() || info.isSymbolicLink()) {
    throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", field);
  }
  const actual = await realpath(lexical);
  if (!samePath(actual, lexical) || (containingRoot !== undefined && !containedPath(actual, containingRoot))) {
    throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", field);
  }
  return actual;
}

// These checks enforce the local single-user contract against pre-existing reparse replacements.
// They are not handle-relative OS containment and do not claim safety against a concurrent hostile swap.
/** Create or validate one plain child directory under the pre-existing-reparse-replacement threat model. */
async function ensurePlainChildDirectory(parent: string, parentReal: string, name: string,
                                         field: string): Promise<{ path: string; real: string }> {
  if (!/^[A-Za-z0-9._-]{1,64}$/.test(name)) throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", field);
  const path = resolve(parent, name);
  if (!samePath(dirname(path), resolve(parent))) throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", field);
  try {
    await mkdir(path);
  } catch (error) {
    if (errorCode(error) !== "EEXIST") throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", field);
  }
  return { path, real: await assertPlainDirectory(path, field, parentReal) };
}

/** Read a contained plain file under the single-user, non-hostile-concurrent-swap threat model. */
async function readPlainFile(path: string, containingRoot: string, field: string): Promise<Buffer | undefined> {
  let info;
  try {
    info = await lstat(path);
  } catch (error) {
    if (errorCode(error) === "ENOENT") return undefined;
    throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", field);
  }
  if (!info.isFile() || info.isSymbolicLink()) throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", field);
  const actual = await realpath(path);
  if (!samePath(actual, resolve(path)) || !containedPath(actual, containingRoot)) {
    throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", field);
  }
  return await readFile(actual);
}

function assertNonSystemAbsolute(path: string, field: string): string {
  if (!isAbsolute(path)) throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", field);
  if (process.platform === "win32") {
    const systemDrive = (process.env.SystemDrive ?? "").replace(/[\\/]+$/, "").toLowerCase();
    const drive = parse(path).root.replace(/[\\/]+$/, "").toLowerCase();
    if (!systemDrive || !drive || drive === systemDrive) {
      throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", field);
    }
  }
  return resolve(path);
}

function rootsFromEnvironment(): { workspace: string; runtime: string; cache: string } {
  return {
    workspace: assertNonSystemAbsolute(stringValue(process.env.KAIZEN_WORKSPACE_ROOT, "workspace_root", 4096), "workspace_root"),
    runtime: assertNonSystemAbsolute(stringValue(process.env.KAIZEN_CLAUDE_SESSION_ROOT, "runtime_root", 4096), "runtime_root"),
    cache: assertNonSystemAbsolute(stringValue(process.env.KAIZEN_CLAUDE_CACHE_ROOT, "cache_root", 4096), "cache_root"),
  };
}

function sdkEnvironment(roots: { runtime: string; cache: string }): Record<string, string | undefined> {
  const blocked = /(?:CREDENTIAL|_API_KEY$|_AUTH_TOKEN$|_ACCESS_TOKEN$|_SESSION_TOKEN$|_BEARER_TOKEN$|_PASSWORD$|_SECRET$|_SECRET_KEY$|_TOKEN$)/i;
  const env: Record<string, string | undefined> = {};
  for (const [key, value] of Object.entries(process.env)) {
    const upper = key.toUpperCase();
    if (!blocked.test(key) && !upper.startsWith("OTEL_") && upper !== "SENTRY_DSN"
        && upper !== "NODE_OPTIONS" && upper !== "NODE_PATH"
        && upper !== "CLAUDE_CODE_MAX_RETRIES" && upper !== "KAIZEN_TEST_EXTENSION_PROVIDER_RETRIES") {
      env[key] = value;
    }
  }
  const temporary = join(roots.runtime, "temp");
  env.TEMP = temporary;
  env.TMP = temporary;
  env.TMPDIR = temporary;
  env.XDG_CACHE_HOME = roots.cache;
  env.CLAUDE_AGENT_SDK_CLIENT_APP = "agent-kaizen";
  env.CLAUDE_CODE_ENABLE_TELEMETRY = "0";
  env.DISABLE_TELEMETRY = "1";
  env.DISABLE_AUTOUPDATER = "1";
  env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "1";
  env.CLAUDE_CODE_DISABLE_AUTO_MEMORY = "1";
  env.CLAUDE_CODE_IDE_SKIP_AUTO_INSTALL = "1";
  if (process.env.KAIZEN_TEST_EXTENSION_PROVIDER_RETRIES === "0") env.CLAUDE_CODE_MAX_RETRIES = "0";
  return env;
}

/** Reject every symlink or reparse component and confirm final realpath containment under root. */
async function rejectReparseChain(root: string, relativePath: string): Promise<string> {
  const pieces = relativePath.split("/");
  let cursor = root;
  const rootInfo = await lstat(root);
  if (rootInfo.isSymbolicLink()) throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "reference.root");
  for (const piece of pieces) {
    cursor = join(cursor, piece);
    const info = await lstat(cursor);
    if (info.isSymbolicLink()) throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "reference.path");
  }
  const actualRoot = await realpath(root);
  const actual = await realpath(cursor);
  if (!containedPath(actual, actualRoot)) throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "reference.path");
  return actual;
}

function referenceValue(value: unknown): LocalReference {
  const raw = objectValue(value, "reference");
  const allowed = new Set(["root", "path", "sha256", "bytes", "encoding", "media_type"]);
  if (Object.keys(raw).some((key) => !allowed.has(key)) || (raw.root !== "runtime" && raw.root !== "cache")) {
    throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "reference");
  }
  const path = stringValue(raw.path, "reference.path", 4096);
  if (path.includes("\\") || path.includes(":") || path.startsWith("/") || path.split("/").some((part) => !part || part === "." || part === "..")) {
    throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "reference.path");
  }
  const sha256 = stringValue(raw.sha256, "reference.sha256", 64);
  if (!SHA256.test(sha256)) throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "reference.sha256");
  const bytes = integerValue(raw.bytes, "reference.bytes", 0, MAX_PROPOSAL_TOTAL_BYTES);
  if (raw.encoding !== undefined && raw.encoding !== "utf-8") throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "reference.encoding");
  const media = raw.media_type;
  if (media !== undefined && !["image/png", "image/jpeg", "image/webp", "image/gif"].includes(String(media))) {
    throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "reference.media_type");
  }
  return { root: raw.root, path, sha256, bytes, ...(raw.encoding ? { encoding: "utf-8" as const } : {}),
    ...(media ? { media_type: media as LocalReference["media_type"] } : {}) };
}

function validImageMagic(content: Buffer, mediaType: LocalReference["media_type"]): boolean {
  if (mediaType === "image/png") return content.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
  if (mediaType === "image/jpeg") return content.length >= 3 && content[0] === 0xff && content[1] === 0xd8 && content[2] === 0xff;
  if (mediaType === "image/gif") return content.subarray(0, 6).toString("ascii") === "GIF87a" || content.subarray(0, 6).toString("ascii") === "GIF89a";
  if (mediaType === "image/webp") return content.subarray(0, 4).toString("ascii") === "RIFF" && content.subarray(8, 12).toString("ascii") === "WEBP";
  return false;
}

/** Resolve and verify a bounded runtime/cache reference, including containment, digest, encoding, and image magic. */
async function readReference(value: unknown, roots: { runtime: string; cache: string }, maximum: number,
                             requireEncoding?: "utf-8", requireImage = false): Promise<{ ref: LocalReference; content: Buffer }> {
  const ref = referenceValue(value);
  if (ref.bytes > maximum) throw new ProtocolFailure("DENIED_WORKER_OVERSIZE", "reference.bytes");
  if (requireEncoding && ref.encoding !== requireEncoding) throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "reference.encoding");
  if (requireImage && !ref.media_type) throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "reference.media_type");
  const base = ref.root === "runtime" ? roots.runtime : roots.cache;
  const path = await rejectReparseChain(base, ref.path);
  const info = await stat(path);
  if (!info.isFile() || info.size !== ref.bytes) throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "reference.bytes");
  const content = await readFile(path);
  if (createHash("sha256").update(content).digest("hex") !== ref.sha256) {
    throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "reference.sha256");
  }
  if (requireEncoding === "utf-8") decodeDeclaredUtf8(content);
  if (requireImage && !validImageMagic(content, ref.media_type)) {
    throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "reference.media_type");
  }
  return { ref, content };
}

/** Atomically stage content-addressed text in the runtime outbox and return its local reference. */
async function stageText(content: string, runtimeRoot: string): Promise<LocalReference> {
  const bytes = Buffer.from(content, "utf8");
  const sha256 = createHash("sha256").update(bytes).digest("hex");
  const relativePath = `outbox/sha256/${sha256.slice(0, 2)}/${sha256}.utf8`;
  const runtimeReal = await assertPlainDirectory(runtimeRoot, "outbox.root");
  const outbox = await ensurePlainChildDirectory(runtimeRoot, runtimeReal, "outbox", "outbox");
  const shaRoot = await ensurePlainChildDirectory(outbox.path, outbox.real, "sha256", "outbox.sha256");
  const prefix = await ensurePlainChildDirectory(shaRoot.path, shaRoot.real, sha256.slice(0, 2), "outbox.prefix");
  const target = resolve(prefix.path, `${sha256}.utf8`);
  if (!samePath(dirname(target), prefix.path)) throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "outbox.path");
  const existing = await readPlainFile(target, prefix.real, "outbox.target");
  if (existing !== undefined && !existing.equals(bytes)) {
    throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "outbox_collision");
  }
  if (existing === undefined) {
    const temporary = resolve(outbox.path, `.stage-${process.pid}-${randomUUID()}`);
    if (!samePath(dirname(temporary), outbox.path)) {
      throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "outbox.stage");
    }
    const writeRuntime = await assertPlainDirectory(runtimeRoot, "outbox.root");
    const writeOutbox = await assertPlainDirectory(outbox.path, "outbox", writeRuntime);
    if (!samePath(writeRuntime, runtimeReal) || !samePath(writeOutbox, outbox.real)) {
      throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "outbox.changed");
    }
    await writeFile(temporary, bytes, { flag: "wx" });
    let primaryError: unknown;
    try {
      const staged = await readPlainFile(temporary, outbox.real, "outbox.stage");
      if (staged === undefined || !staged.equals(bytes)) {
        throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "outbox.stage");
      }
      const freshRuntime = await assertPlainDirectory(runtimeRoot, "outbox.root");
      const freshOutbox = await assertPlainDirectory(outbox.path, "outbox", freshRuntime);
      const freshShaRoot = await assertPlainDirectory(shaRoot.path, "outbox.sha256", freshOutbox);
      const freshPrefix = await assertPlainDirectory(prefix.path, "outbox.prefix", freshShaRoot);
      if (!samePath(freshOutbox, outbox.real) || !samePath(freshPrefix, prefix.real)) {
        throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "outbox.changed");
      }
      // Hermetic worker-test seam. The adapter admits KAIZEN_FAKE_CLAUDE_* through the subprocess
      // environment it constructs at spawn, never from provider stream identity state.
      const stagedRace = process.env.KAIZEN_FAKE_CLAUDE_STAGE_RENAME_RACE;
      if (stagedRace === "same" || stagedRace === "collision") {
        await writeFile(target, stagedRace === "same" ? bytes : Buffer.from("collision", "utf8"), { flag: "wx" });
        const injected = new Error("injected staged rename race") as NodeJS.ErrnoException;
        injected.code = "EEXIST";
        throw injected;
      }
      await rename(temporary, target);
    } catch (renameError) {
      primaryError = renameError;
      if (new Set(["EEXIST", "EPERM"]).has(errorCode(renameError) ?? "")) {
        try {
          const existing = await readPlainFile(target, prefix.real, "outbox.target");
          if (existing !== undefined) {
            primaryError = existing.equals(bytes)
              ? undefined
              : new ProtocolFailure("DENIED_WORKER_PROTOCOL", "outbox_collision");
          }
        } catch (collisionError) {
          primaryError = collisionError;
        }
      }
    } finally {
      try {
        const freshOutbox = await assertPlainDirectory(outbox.path, "outbox", runtimeReal);
        const staged = await readPlainFile(temporary, freshOutbox, "outbox.stage");
        if (staged !== undefined) await unlink(temporary);
        if (process.env.KAIZEN_FAKE_CLAUDE_STAGE_CLEANUP_ERROR === "1") {
          throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "outbox.cleanup");
        }
      } catch (cleanupError) {
        if (errorCode(cleanupError) !== "ENOENT" && primaryError === undefined) primaryError = cleanupError;
      }
    }
    if (primaryError !== undefined) throw primaryError;
  }
  const landed = await readPlainFile(target, prefix.real, "outbox.target");
  if (landed === undefined || !landed.equals(bytes)) {
    throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "outbox.target");
  }
  return { root: "runtime", path: relativePath, sha256, bytes: bytes.length, encoding: "utf-8" };
}

const toolPathString = () => z.string().min(1).max(MAX_TOOL_PATH_CHARS);
const utf8BoundedString = (maximum: number, requireNonEmpty = false) => {
  const value = requireNonEmpty ? z.string().min(1) : z.string();
  return value.refine((text) => Buffer.byteLength(text, "utf8") <= maximum, {
    message: `UTF-8 value exceeds ${maximum} bytes`,
  });
};

const readFileShape = {
  path: toolPathString(),
  start_line: z.number().int().min(1).optional(),
  end_line: z.number().int().min(1).optional(),
  max_bytes: z.number().int().min(1).max(262_144).optional(),
};
const listFilesShape = {
  path: toolPathString().default("."),
  glob: z.string().min(1).max(MAX_GLOB_CHARS).optional(),
  max_depth: z.number().int().min(0).max(8).default(8),
  max_entries: z.number().int().min(1).max(500).default(500),
};
const searchTextShape = {
  query: utf8BoundedString(MAX_SEARCH_QUERY_BYTES, true),
  path: toolPathString().default("."),
  mode: z.enum(["literal", "regex"]).default("literal"),
  glob: z.string().min(1).max(MAX_GLOB_CHARS).optional(),
  case_sensitive: z.boolean().default(false),
  max_results: z.number().int().min(1).max(200).default(200),
};
const runProcessShape = {
  executable: utf8BoundedString(MAX_PROCESS_ARG_BYTES, true),
  argv: z.array(utf8BoundedString(MAX_PROCESS_ARG_BYTES)).max(MAX_PROCESS_ARGS).default([]),
  cwd: toolPathString().optional(),
  timeout_ms: z.number().int().min(1_000).max(1_800_000).default(120_000),
};
const changeSchema = z.object({
  kind: z.enum(["create", "modify", "delete", "rename"]),
  path: toolPathString(),
  old_path: toolPathString().optional(),
  content: utf8BoundedString(MAX_PROPOSAL_FILE_BYTES).optional(),
}).strict().superRefine((change, context) => {
  if ((change.kind === "create" || change.kind === "modify") && change.content === undefined) {
    context.addIssue({ code: "custom", message: "create/modify requires complete final content" });
  }
  if (change.kind === "rename" && change.old_path === undefined) {
    context.addIssue({ code: "custom", message: "rename requires old_path" });
  }
  if (change.kind !== "rename" && change.old_path !== undefined) {
    context.addIssue({ code: "custom", message: "old_path is rename-only" });
  }
  if (change.kind === "delete" && change.content !== undefined) {
    context.addIssue({ code: "custom", message: "delete cannot carry content" });
  }
});
const proposeChangesShape = {
  summary: z.string().min(1).max(MAX_PROPOSAL_SUMMARY_CHARS)
    .refine((text) => text.trim().length > 0, { message: "summary must contain non-whitespace text" }).optional(),
  changes: z.array(changeSchema).min(1).max(64),
};

/** Own one SDK query session, turn lifecycle, tool mediation, model attestation, and fail-closed teardown. */
class Worker {
  private sessionId: string | undefined;
  private activeTurnId: string | undefined;
  private queryHandle: Query | undefined;
  private readonly input = new AsyncInput();
  private readonly sequences = new Map<string, number>();
  private readonly pendingTools = new Map<string, { resolve: (value: ToolResolution) => void; reject: (reason: Error) => void }>();
  private readonly usedToolCallIds = new Set<string>();
  private readonly settledToolCallIds = new Set<string>();
  private roots: { workspace: string; runtime: string; cache: string } | undefined;
  private selectedModel: string | undefined;
  private selectedEffort: Effort | undefined;
  private selectedModelEfforts: Effort[] | undefined;
  private maxTurns: number | undefined;
  private turnModelEvidence = 0;

  constructor(private readonly terminateRequests: () => void) {}
  private initMessageResolve: ((value: JsonObject) => void) | undefined;
  private initMessageReject: ((reason: Error) => void) | undefined;
  private initMessagePromise: Promise<JsonObject> | undefined;
  private closing = false;

  private sequence(turnId?: string): number {
    const key = turnId ?? "session";
    const next = (this.sequences.get(key) ?? 0) + 1;
    this.sequences.set(key, next);
    return next;
  }

  private event(event: string, body: JsonObject, turnId?: string): void {
    if (!this.sessionId) throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "session_id");
    if (TURN_EVENTS.has(event) && (!this.activeTurnId || turnId !== this.activeTurnId)) {
      throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "turn_id");
    }
    if (event === "initialized" && (turnId !== undefined || this.activeTurnId !== undefined)) {
      throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "turn_id");
    }
    if (event === "fatal" && ((this.activeTurnId && turnId !== this.activeTurnId)
        || (!this.activeTurnId && turnId !== undefined))) {
      throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "turn_id");
    }
    sendFrame({
      v: PROTOCOL_VERSION,
      type: "event",
      event,
      session_id: this.sessionId ?? "worker",
      ...(turnId ? { turn_id: turnId } : {}),
      seq: this.sequence(turnId),
      body,
    });
  }

  private respond(request: RequestFrame, ok: boolean, body: JsonObject): void {
    sendFrame({
      v: PROTOCOL_VERSION,
      type: "response",
      id: request.id,
      ok,
      ...(ok ? { body } : { error: body }),
    });
  }

  private requireSession(request: RequestFrame): void {
    if (!this.sessionId || request.session_id !== this.sessionId) {
      throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "session_id");
    }
  }

  private async invokeTool(name: typeof TOOL_NAMES[number], input: JsonObject): Promise<ToolCallResult> {
    const turnId = this.activeTurnId;
    if (!turnId) return { content: [{ type: "text", text: '{"ok":false,"code":"DENIED_TOOL_UNSUPPORTED"}' }], isError: true };
    if (this.pendingTools.size > 0) {
      return { content: [{ type: "text", text: '{"ok":false,"code":"DENIED_TOOL_CONCURRENCY"}' }], isError: true };
    }
    let forwarded = input;
    if (name === "kaizen_propose_changes") {
      const changes = input.changes as JsonObject[];
      let total = 0;
      const staged: JsonObject[] = [];
      for (const change of changes) {
        const copy = { ...change };
        if (typeof copy.content === "string") {
          const size = Buffer.byteLength(copy.content, "utf8");
          total += size;
          if (size > MAX_PROPOSAL_FILE_BYTES || total > MAX_PROPOSAL_TOTAL_BYTES) {
            throw new ProtocolFailure("DENIED_WORKER_OVERSIZE", "proposal.content");
          }
          copy.content_ref = await stageText(copy.content, this.roots!.runtime);
          delete copy.content;
        }
        staged.push(copy);
      }
      forwarded = { ...input, changes: staged };
    }
    if (Buffer.byteLength(JSON.stringify(forwarded), "utf8") > MAX_FRAME_BYTES - 4096) {
      throw new ProtocolFailure("DENIED_WORKER_OVERSIZE", "tool_input");
    }
    let callId: string;
    do callId = `tool-${randomUUID()}`;
    while (this.usedToolCallIds.has(callId));
    this.usedToolCallIds.add(callId);
    const resolution = new Promise<ToolResolution>((resolveTool, rejectTool) => {
      this.pendingTools.set(callId, { resolve: resolveTool, reject: rejectTool });
    });
    this.event("tool.open", { call_id: callId, name, input: forwarded }, turnId);
    this.event("tool.invoke", { call_id: callId, name, input: forwarded }, turnId);
    try {
      const outcome = await resolution;
      const text = JSON.stringify(outcome.body);
      this.event("tool.close", { call_id: callId, name, ok: outcome.ok }, turnId);
      return { content: [{ type: "text", text }], ...(outcome.ok ? {} : { isError: true }) };
    } finally {
      this.pendingTools.delete(callId);
    }
  }

  private createTools() {
    return [
      tool("kaizen_read_file", "Read a bounded UTF-8 workspace file range.", readFileShape,
        async (args) => await this.invokeTool("kaizen_read_file", args)),
      tool("kaizen_list_files", "List sorted workspace-relative entries without following reparse points.", listFilesShape,
        async (args) => await this.invokeTool("kaizen_list_files", args)),
      tool("kaizen_search_text", "Search workspace text with bounded sorted results.", searchTextShape,
        async (args) => await this.invokeTool("kaizen_search_text", args)),
      tool("kaizen_run_process", "Request a permission-mediated direct-argv process; effects are not exhaustively knowable.", runProcessShape,
        async (args) => await this.invokeTool("kaizen_run_process", args)),
      tool("kaizen_propose_changes", "Propose one whole create/modify/delete/rename request for user review; this tool never applies files.", proposeChangesShape,
        async (args) => await this.invokeTool("kaizen_propose_changes", args)),
    ];
  }

  private assertAccount(accountValue: unknown): void {
    const account = objectValue(accountValue, "account");
    const sources = [account.apiKeySource, account.tokenSource].filter((value) => value !== undefined && value !== null);
    if (sources.length === 0) throw new ProtocolFailure("DENIED_AUTH_UNAVAILABLE", "auth_source");
    for (const source of sources) this.assertOAuthSource(source, "auth_source");
  }

  private assertOAuthSource(value: unknown, field: string): void {
    if (value === undefined || value === null || value === "") {
      throw new ProtocolFailure("DENIED_AUTH_UNAVAILABLE", field);
    }
    if (typeof value !== "string" || value.trim().toLowerCase() !== "oauth") {
      throw new ProtocolFailure("DENIED_AUTH_MODE_MISMATCH", field);
    }
  }

  private assertProfile(models: ModelEntry[]): Effort[] | undefined {
    if (!this.selectedModel) {
      if (this.selectedEffort) throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "profile");
      return undefined;
    }
    const selected = models.find((model) => model.id === this.selectedModel);
    if (!selected) throw new ProtocolFailure("DENIED_MODEL_UNAVAILABLE", "model");
    if (selected.reasoning_efforts.length === 0) {
      if (this.selectedEffort) {
        throw new ProtocolFailure("DENIED_EFFORT_UNSUPPORTED", "reasoning_effort");
      }
      return [];
    }
    if (!this.selectedEffort || !selected.reasoning_efforts.includes(this.selectedEffort)) {
      throw new ProtocolFailure("DENIED_EFFORT_UNSUPPORTED", "reasoning_effort");
    }
    return [...selected.reasoning_efforts];
  }

  private assertTurnProfile(): void {
    if (!this.selectedModel || this.selectedModelEfforts === undefined) {
      throw new ProtocolFailure("DENIED_MODEL_UNAVAILABLE", "profile");
    }
    if (this.selectedModelEfforts.length === 0) {
      if (this.selectedEffort !== undefined) {
        throw new ProtocolFailure("DENIED_EFFORT_UNSUPPORTED", "reasoning_effort");
      }
      return;
    }
    if (!this.selectedEffort || !this.selectedModelEfforts.includes(this.selectedEffort)) {
      throw new ProtocolFailure("DENIED_EFFORT_UNSUPPORTED", "reasoning_effort");
    }
  }

  private async assertInitSurface(init: JsonObject): Promise<void> {
    this.assertOAuthSource(init.apiKeySource, "auth_source");
    if (init.tokenSource !== undefined) this.assertOAuthSource(init.tokenSource, "auth_source");
    if (init.permissionMode !== "dontAsk") throw new ProtocolFailure("DENIED_TOOL_UNSUPPORTED", "permission_mode");
    if (this.selectedModel && init.model !== this.selectedModel) {
      throw new ProtocolFailure("DENIED_MODEL_UNAVAILABLE", "effective_profile");
    }
    const tools = Array.isArray(init.tools) ? init.tools : [];
    if (tools.length !== MCP_TOOL_NAMES.size || new Set(tools).size !== tools.length
      || tools.some((name) => typeof name !== "string" || !MCP_TOOL_NAMES.has(name))) {
      throw new ProtocolFailure("DENIED_TOOL_UNSUPPORTED", "reported_tools");
    }
    for (const field of ["agents", "skills", "plugins"] as const) {
      assertDisabledSurface(init[field], `reported_${field}`);
    }
    const servers = Array.isArray(init.mcp_servers) ? init.mcp_servers : [];
    if (servers.length !== 1 || objectValue(servers[0], "mcp_server").name !== "kaizen") {
      throw new ProtocolFailure("DENIED_TOOL_UNSUPPORTED", "reported_mcp_servers");
    }
    if (this.selectedModel && (typeof init.model !== "string" || init.model !== this.selectedModel)) {
      throw new ProtocolFailure("DENIED_MODEL_UNAVAILABLE", "effective_profile");
    }
    const statuses = await this.queryHandle!.mcpServerStatus();
    if (statuses.length !== 1 || statuses[0]?.name !== "kaizen" || statuses[0]?.status !== "connected") {
      throw new ProtocolFailure("DENIED_TOOL_UNSUPPORTED", "mcp_server_status");
    }
  }

  private async initialize(request: RequestFrame): Promise<void> {
    if (this.queryHandle) {
      this.requireSession(request);
      if (Object.keys(request.body).length !== 0) {
        throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "refresh.body");
      }
      const models = sanitizeCatalog(await this.queryHandle.supportedModels());
      const selectedModelEfforts = this.assertProfile(models);
      this.selectedModelEfforts = selectedModelEfforts;
      const body = this.initializedBody(models);
      this.respond(request, true, body);
      this.event("initialized", body);
      return;
    }
    if (!request.session_id) throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "session_id");
    this.sessionId = request.session_id;
    if (request.body.auth_mode !== "subscription") {
      throw new ProtocolFailure("DENIED_AUTH_MODE_MISMATCH", "auth_mode");
    }
    const hasModel = request.body.model !== undefined;
    const hasEffort = request.body.reasoning_effort !== undefined;
    if (hasEffort && !hasModel) throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "profile");
    if (hasModel) {
      this.selectedModel = stringValue(request.body.model, "model", 256);
    }
    if (hasEffort) {
      const effort = stringValue(request.body.reasoning_effort, "reasoning_effort", 16);
      if (!EFFORTS.has(effort)) throw new ProtocolFailure("DENIED_EFFORT_UNSUPPORTED", "reasoning_effort");
      this.selectedEffort = effort as Effort;
    }
    this.maxTurns = integerValue(request.body.max_turns ?? 8, "max_turns", 1, 32);
    this.roots = rootsFromEnvironment();
    const [, runtimeReal] = await Promise.all([
      assertPlainDirectory(this.roots.workspace, "workspace_root"),
      assertPlainDirectory(this.roots.runtime, "runtime_root"),
      assertPlainDirectory(this.roots.cache, "cache_root"),
    ]);
    await Promise.all([
      ensurePlainChildDirectory(this.roots.runtime, runtimeReal, "temp", "runtime.temp"),
      ensurePlainChildDirectory(this.roots.runtime, runtimeReal, "outbox", "outbox"),
    ]);
    const mcp = createSdkMcpServer({ name: "kaizen", version: "1.0.0", tools: this.createTools() });
    this.initMessagePromise = new Promise<JsonObject>((resolveInit, rejectInit) => {
      this.initMessageResolve = resolveInit;
      this.initMessageReject = rejectInit;
    });
    this.queryHandle = query({
      prompt: this.input,
      options: {
        abortController: new AbortController(),
        additionalDirectories: [],
        agents: {},
        allowedTools: [...MCP_TOOL_NAMES],
        betas: [],
        canUseTool: async (toolName, input, options) => MCP_TOOL_NAMES.has(toolName)
          ? { behavior: "allow", updatedInput: input, toolUseID: options.toolUseID }
          : { behavior: "deny", message: "DENIED_TOOL_UNSUPPORTED", interrupt: false, toolUseID: options.toolUseID },
        cwd: this.roots.workspace,
        ...(this.selectedEffort ? { effort: this.selectedEffort } : {}),
        enableFileCheckpointing: false,
        env: sdkEnvironment(this.roots),
        hooks: {
          PreToolUse: [{
            hooks: [async (input) => {
              const toolName = (input as unknown as JsonObject).tool_name;
              const allowed = typeof toolName === "string" && MCP_TOOL_NAMES.has(toolName);
              return {
                hookSpecificOutput: {
                  hookEventName: "PreToolUse" as const,
                  permissionDecision: allowed ? "allow" as const : "deny" as const,
                  permissionDecisionReason: allowed ? "Kaizen ToolGateway" : "DENIED_TOOL_UNSUPPORTED",
                },
              };
            }],
          }],
        },
        includePartialMessages: true,
        maxTurns: this.maxTurns,
        mcpServers: { kaizen: mcp },
        ...(this.selectedModel ? { model: this.selectedModel } : {}),
        permissionMode: "dontAsk",
        persistSession: false,
        plugins: [],
        promptSuggestions: false,
        settingSources: [],
        skills: [],
        stderr: () => suppressProviderStderr(),
        strictMcpConfig: true,
        systemPrompt: SYSTEM_PROMPT,
        tools: [],
      },
    });
    void this.consumeMessages().catch((error: unknown) => this.fail(error));
    let timeoutHandle: NodeJS.Timeout;
    const timeout = new Promise<never>((_, rejectTimeout) => {
      timeoutHandle = setTimeout(
        () => rejectTimeout(new ProtocolFailure("DENIED_SDK_UNAVAILABLE", "initialize_timeout")), 120_000);
      timeoutHandle.unref();
    });
    let control: Awaited<ReturnType<Query["initializationResult"]>>;
    let initMessage: JsonObject;
    try {
      [control, initMessage] = await Promise.all([
        Promise.race([this.queryHandle.initializationResult(), timeout]),
        Promise.race([this.initMessagePromise, timeout]),
      ]);
    } finally {
      clearTimeout(timeoutHandle!);
    }
    this.assertAccount(control.account);
    const controlSurface = control as unknown as JsonObject;
    for (const field of ["agents", "skills", "plugins"] as const) {
      assertDisabledSurface(controlSurface[field], `reported_${field}`);
    }
    await this.assertInitSurface(initMessage);
    const models = sanitizeCatalog(control.models);
    const selectedModelEfforts = this.assertProfile(models);
    this.selectedModelEfforts = selectedModelEfforts;
    const body = this.initializedBody(models);
    this.respond(request, true, body);
    this.event("initialized", body);
  }

  private initializedBody(models: ModelEntry[]): JsonObject {
    return {
      status: "ready",
      auth_mode: "subscription",
      auth_source: "oauth",
      runtime_version: SDK_VERSION,
      tools: [...TOOL_NAMES],
      models,
      ...(this.selectedModel ? { selected_model: this.selectedModel } : {}),
      ...(this.selectedEffort ? { reasoning_effort: this.selectedEffort } : {}),
      max_turns: this.maxTurns!,
    };
  }

  /** Emit challenge-bound self-attestation for a supported worker enforcement capability. */
  private async probeCapability(request: RequestFrame): Promise<void> {
    this.requireSession(request);
    if (!this.queryHandle || !this.roots || this.activeTurnId) {
      throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "capability.state");
    }
    const featureValue = stringValue(request.body.feature, "capability.feature", 64);
    if (!(CAPABILITY_PROBE_FEATURES as readonly string[]).includes(featureValue)) {
      throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "capability.feature");
    }
    const feature = featureValue as CapabilityProbeFeature;
    const challenge = idValue(request.body.challenge, "capability.challenge");
    const expectedKeys = new Set(["feature", "challenge"]);
    if (feature === "image_attachments") {
      expectedKeys.add("prompt_ref");
      expectedKeys.add("image_ref");
    } else if (feature === "governed_context") {
      expectedKeys.add("prompt_ref");
      expectedKeys.add("context_ref");
    }
    if (Object.keys(request.body).length !== expectedKeys.size
        || Object.keys(request.body).some((key) => !expectedKeys.has(key))) {
      throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "capability.body");
    }

    let artifactRef: LocalReference | undefined;
    if (feature === "streaming") {
      const sample = `KAIZEN_CAPABILITY_STREAM:${challenge}:` + "\u20ac".repeat(12_000);
      const extracted = this.textDelta({
        type: "stream_event",
        event: { type: "content_block_delta", delta: { type: "text_delta", text: sample } },
      });
      const fragments = extracted ? splitUtf8(extracted, MAX_DELTA_BYTES) : [];
      if (fragments.length < 2 || fragments.join("") !== sample
          || fragments.some((item) => Buffer.byteLength(item, "utf8") > MAX_DELTA_BYTES)) {
        throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "capability.streaming");
      }
    } else if (feature === "image_attachments") {
      const message = await this.buildMessage({ prompt_ref: request.body.prompt_ref, images: [request.body.image_ref] });
      const envelope = objectValue(message.message, "capability.image.message");
      if (!Array.isArray(envelope.content) || envelope.content.length !== 2) {
        throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "capability.image.content");
      }
      const image = objectValue(envelope.content[1], "capability.image.block");
      const source = objectValue(image.source, "capability.image.source");
      const requested = referenceValue(request.body.image_ref);
      if (image.type !== "image" || source.type !== "base64" || source.media_type !== requested.media_type
          || typeof source.data !== "string"
          || createHash("sha256").update(Buffer.from(source.data, "base64")).digest("hex") !== requested.sha256) {
        throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "capability.image.block");
      }
    } else if (feature === "governed_context") {
      const message = await this.buildMessage({
        prompt_ref: request.body.prompt_ref, context_refs: [request.body.context_ref],
      });
      const envelope = objectValue(message.message, "capability.context.message");
      const expected = "KAIZEN_CAPABILITY_PROBE_PROMPT\n\n<kaizen_context index=\"1\">\nKAIZEN_CAPABILITY_CONTEXT\n</kaizen_context>";
      if (envelope.content !== expected) {
        throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "capability.context.content");
      }
    } else if (feature === "diff_snapshots") {
      const content = `KAIZEN_CAPABILITY_DIFF:${challenge}`;
      artifactRef = await stageText(content, this.roots.runtime);
      const verified = await readReference(artifactRef, this.roots, 1024, "utf-8");
      if (verified.content.toString("utf8") !== content) {
        throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "capability.diff_snapshots");
      }
    } else if (feature === "controlled_tools") {
      const definitions = this.createTools() as unknown[];
      const names = definitions.map((item, index) => objectValue(item, `capability.tools[${index}]`).name);
      const schemasPass = [
        z.object(readFileShape).strict().safeParse({ path: "probe.txt" }).success,
        z.object(listFilesShape).strict().safeParse({ path: "." }).success,
        z.object(searchTextShape).strict().safeParse({ query: "probe", path: "." }).success,
        z.object(proposeChangesShape).strict().safeParse({
          changes: [{ kind: "modify", path: "probe.txt", content: "replacement" }],
        }).success,
      ];
      if (definitions.length !== TOOL_NAMES.length || names.some((name, index) => name !== TOOL_NAMES[index])
          || schemasPass.some((value) => !value)) {
        throw new ProtocolFailure("DENIED_TOOL_UNSUPPORTED", "capability.controlled_tools");
      }
    } else if (feature === "process_execution") {
      const schema = z.object(runProcessShape).strict();
      const valid = schema.safeParse({ executable: "probe.exe", argv: ["--version"], cwd: ".", timeout_ms: 1_000 });
      const shellInjection = schema.safeParse({ executable: "probe.exe", argv: [], shell: true });
      const badTimeout = schema.safeParse({ executable: "probe.exe", timeout_ms: 999 });
      if (!valid.success || shellInjection.success || badTimeout.success) {
        throw new ProtocolFailure("DENIED_TOOL_UNSUPPORTED", "capability.process_execution");
      }
    }
    this.respond(request, true, {
      probe_version: CAPABILITY_PROBE_VERSION,
      feature,
      challenge,
      status: "proven",
      evidence_sha256: capabilityProbeDigest(feature, challenge),
      ...(artifactRef ? { artifact_ref: artifactRef } : {}),
    });
  }

  /** Assemble one bounded prompt, governed context set, and image set, optionally neutralizing @file mentions. */
  private async buildMessage(body: JsonObject, neutralizeMentions = false): Promise<SDKUserMessage> {
    const roots = this.roots!;
    let prompt: string;
    if (typeof body.prompt === "string") {
      if (Buffer.byteLength(body.prompt, "utf8") > MAX_FRAME_BYTES / 2) {
        throw new ProtocolFailure("DENIED_WORKER_OVERSIZE", "prompt");
      }
      prompt = body.prompt;
    } else if (body.prompt_ref !== undefined) {
      prompt = (await readReference(body.prompt_ref, roots, MAX_FRAME_BYTES / 2, "utf-8")).content.toString("utf8");
    } else {
      throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "prompt");
    }
    const contextValues = body.context_refs ?? [];
    if (!Array.isArray(contextValues) || contextValues.length > 8) {
      throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "context_refs");
    }
    let contextTotal = 0;
    for (let index = 0; index < contextValues.length; index += 1) {
      const context = await readReference(contextValues[index], roots, MAX_CONTEXT_BYTES, "utf-8");
      contextTotal += context.content.length;
      if (contextTotal > MAX_CONTEXT_TOTAL_BYTES) throw new ProtocolFailure("DENIED_WORKER_OVERSIZE", "context_refs");
      prompt += `\n\n<kaizen_context index="${index + 1}">\n${context.content.toString("utf8")}\n</kaizen_context>`;
    }
    if (neutralizeMentions) prompt = prompt.replace(/@/g, "＠");
    const imageValues = body.images ?? body.attachments ?? [];
    if (!Array.isArray(imageValues) || imageValues.length > 4) {
      throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "images");
    }
    if (imageValues.length === 0) {
      return { type: "user", message: { role: "user", content: prompt }, parent_tool_use_id: null,
        origin: { kind: "human" } };
    }
    const content: UserMessageContent = [{ type: "text", text: prompt }];
    for (const imageValue of imageValues) {
      const image = await readReference(imageValue, roots, MAX_IMAGE_BYTES, undefined, true);
      content.push({
        type: "image",
        source: { type: "base64", media_type: image.ref.media_type!, data: image.content.toString("base64") },
      });
    }
    return { type: "user", message: { role: "user", content }, parent_tool_use_id: null,
      origin: { kind: "human" } };
  }

  private async startTurn(request: RequestFrame): Promise<void> {
    this.requireSession(request);
    const turnId = idValue(request.turn_id, "turn_id");
    if (this.activeTurnId) throw new ProtocolFailure("DENIED_PROVIDER_CAPACITY", "turn_id");
    this.assertTurnProfile();
    const message = await this.buildMessage(request.body, true);
    this.activeTurnId = turnId;
    this.turnModelEvidence = 0;
    this.event("turn.open", {
      model: this.selectedModel!,
      ...(this.selectedEffort ? { reasoning_effort: this.selectedEffort } : {}),
      max_turns: this.maxTurns!,
    }, turnId);
    this.input.push(message);
    this.respond(request, true, { accepted: true });
  }

  private async steer(request: RequestFrame): Promise<void> {
    this.requireSession(request);
    if (!this.activeTurnId || request.turn_id !== this.activeTurnId) {
      throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "turn_id");
    }
    this.input.push(await this.buildMessage(request.body, true));
    this.respond(request, true, { accepted: true });
  }

  private async interrupt(request: RequestFrame): Promise<void> {
    this.requireSession(request);
    if (!this.queryHandle || !this.activeTurnId || request.turn_id !== this.activeTurnId) {
      throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "turn_id");
    }
    await this.queryHandle.interrupt();
    this.respond(request, true, { interrupted: true });
  }

  private async toolResult(request: RequestFrame): Promise<void> {
    this.requireSession(request);
    if (!this.activeTurnId || request.turn_id !== this.activeTurnId) {
      throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "turn_id");
    }
    const callId = idValue(request.body.call_id, "call_id");
    const pending = this.pendingTools.get(callId);
    if (!pending || this.settledToolCallIds.has(callId) || typeof request.body.ok !== "boolean") {
      throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "call_id");
    }
    this.settledToolCallIds.add(callId);
    try {
      const rawResult = request.body.ok
        ? objectValue(request.body.result ?? {}, "result")
        : objectValue(request.body.error ?? { code: "DENIED_TOOL_UNSUPPORTED" }, "error");
      const result = await this.resolveToolResult(rawResult, request.body.ok);
      pending.resolve({ ok: request.body.ok, body: result });
      this.respond(request, true, { accepted: true });
    } catch (error) {
      const failure = error instanceof ProtocolFailure ? error : new ProtocolFailure("DENIED_WORKER_PROTOCOL", "result_ref");
      pending.reject(failure);
      throw failure;
    }
  }

  private async resolveToolResult(value: JsonObject, ok: boolean): Promise<JsonObject> {
    if (value.result_ref === undefined) return value;
    const allowed = new Set(["status", "result_ref", "fatal"]);
    if (Object.keys(value).some((key) => !allowed.has(key))) {
      throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "result_ref");
    }
    const resolved = await readReference(value.result_ref, this.roots!, MAX_PROPOSAL_TOTAL_BYTES, "utf-8");
    if (resolved.ref.bytes <= TOOL_RESULT_INLINE_BYTES) {
      throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "result_ref.bytes");
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(resolved.content.toString("utf8"));
    } catch {
      throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "result_ref.json");
    }
    const result = objectValue(parsed, "result_ref.json");
    const status = typeof result.status === "string" ? result.status.toUpperCase() : "";
    if ((ok && status !== "OK") || (!ok && status === "OK")) {
      throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "result_ref.status");
    }
    if (value.status !== undefined && value.status !== result.status) {
      throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "result_ref.status");
    }
    return result;
  }

  private close(request: RequestFrame): void {
    this.requireSession(request);
    this.closing = true;
    this.input.close();
    for (const pending of this.pendingTools.values()) pending.reject(new ProtocolFailure("WORKER_DIED", "session_close"));
    this.pendingTools.clear();
    this.queryHandle?.close();
    this.respond(request, true, { closed: true });
  }

  async handle(request: RequestFrame): Promise<void> {
    try {
      if (request.op === "initialize") await this.initialize(request);
      else if (request.op === "capability.probe") await this.probeCapability(request);
      else if (request.op === "turn.start") await this.startTurn(request);
      else if (request.op === "turn.steer") await this.steer(request);
      else if (request.op === "turn.interrupt") await this.interrupt(request);
      else if (request.op === "tool.result") await this.toolResult(request);
      else if (request.op === "session.close") this.close(request);
      else throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "op");
    } catch (error) {
      const failure = error instanceof ProtocolFailure ? error : new ProtocolFailure("DENIED_SDK_UNAVAILABLE", "operation");
      this.respond(request, false, { code: failure.code, field: failure.field });
      if (request.op === "initialize") this.fail(failure);
    }
  }

  private textDelta(message: JsonObject): string | undefined {
    if (message.type !== "stream_event") return undefined;
    const event = objectValue(message.event, "stream_event");
    if (event.type !== "content_block_delta") return undefined;
    const delta = objectValue(event.delta, "stream_event.delta");
    return delta.type === "text_delta" && typeof delta.text === "string" ? delta.text : undefined;
  }

  private async emitResult(message: JsonObject): Promise<void> {
    const turnId = this.activeTurnId;
    if (!turnId) throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "unexpected_result");
    if (message.modelUsage !== undefined) this.attestModelUsage(message.modelUsage, "modelUsage");
    if (message.model_usage !== undefined) this.attestModelUsage(message.model_usage, "model_usage");
    if (this.turnModelEvidence < 1) throw new ProtocolFailure("DENIED_MODEL_UNAVAILABLE", "effective_model");
    const subtype = typeof message.subtype === "string" ? message.subtype : "error_during_execution";
    const result = typeof message.result === "string" ? message.result : "";
    const numTurns = integerValue(
      message.num_turns ?? 0,
      "num_turns",
      0,
      this.maxTurns!,
      "MODEL_CALL_BUDGET_EXHAUSTED",
    );
    const budgetExhausted = subtype === "error_max_turns";
    const body: JsonObject = {
      ok: subtype === "success" && message.is_error !== true && !budgetExhausted,
      subtype,
      num_turns: numTurns,
      stop_reason: typeof message.stop_reason === "string" ? message.stop_reason : null,
      terminal_reason: typeof message.terminal_reason === "string" ? message.terminal_reason : null,
      model: this.selectedModel!,
      ...(budgetExhausted ? { code: "MODEL_CALL_BUDGET_EXHAUSTED", fatal: true } : {}),
    };
    if (result) {
      if (Buffer.byteLength(result, "utf8") > MAX_FRAME_BYTES / 2) body.result_ref = await stageText(result, this.roots!.runtime);
      else body.result = result;
    }
    for (const pending of this.pendingTools.values()) {
      pending.reject(new ProtocolFailure("DENIED_WORKER_PROTOCOL", "terminal_result_with_pending_tool"));
    }
    this.pendingTools.clear();
    this.event("turn.result", body, turnId);
    this.sequences.delete(turnId);
    this.usedToolCallIds.clear();
    this.settledToolCallIds.clear();
    this.activeTurnId = undefined;
  }

  /** Require provider-reported model identity to equal the selected model and count evidence. */
  private attestModel(value: unknown, field: string): void {
    if (typeof value !== "string" || value !== this.selectedModel) {
      throw new ProtocolFailure("DENIED_MODEL_UNAVAILABLE", field);
    }
    this.turnModelEvidence += 1;
  }

  /** Validate each usage record's model identity and count turn-level model evidence. */
  private attestModelUsage(value: unknown, field: string): void {
    const usage = objectValue(value, field);
    const models = Object.keys(usage);
    if (models.length < 1) throw new ProtocolFailure("DENIED_MODEL_UNAVAILABLE", field);
    for (const model of models) this.attestModel(model, field);
  }

  private async consumeMessages(): Promise<void> {
    for await (const sdkMessage of this.queryHandle!) {
      const message = sdkMessage as SDKMessage & JsonObject;
      if (message.type === "system" && message.subtype === "init") {
        const init = message as unknown as JsonObject;
        this.initMessageResolve?.(init);
        this.initMessageResolve = undefined;
        this.initMessageReject = undefined;
        continue;
      }
      if (message.type === "assistant") {
        if (!this.activeTurnId) throw new ProtocolFailure("DENIED_WORKER_PROTOCOL", "unexpected_assistant");
        const assistant = objectValue(message.message, "assistant.message");
        this.attestModel(assistant.model, "assistant.model");
        if (message.error && this.activeTurnId) this.event("status", { status: String(message.error) }, this.activeTurnId);
        continue;
      }
      const delta = this.textDelta(message as unknown as JsonObject);
      if (delta && this.activeTurnId) {
        for (const text of splitUtf8(delta, MAX_DELTA_BYTES)) this.event("delta", { text }, this.activeTurnId);
        continue;
      }
      if (message.type === "rate_limit_event" && this.activeTurnId) {
        this.event("rate_limit", { status: "rate_limited" }, this.activeTurnId);
        continue;
      }
      if (message.type === "system" && message.subtype === "informational" && this.activeTurnId) {
        this.event("status", { status: "provider_notice", level: message.level ?? "info" }, this.activeTurnId);
        continue;
      }
      if (message.type === "result") await this.emitResult(message as unknown as JsonObject);
    }
    if (!this.closing) throw new ProtocolFailure("WORKER_DIED", "provider_stream");
  }

  fail(error: unknown): void {
    if (this.closing) return;
    this.closing = true;
    const failure = error instanceof ProtocolFailure ? error : new ProtocolFailure("WORKER_DIED", "worker");
    this.initMessageReject?.(failure);
    try {
      this.event("fatal", { code: failure.code, field: failure.field }, this.activeTurnId);
    } catch {
      // The host will independently classify a corrupt or closed stdout channel as fatal.
    }
    this.input.close();
    for (const pending of this.pendingTools.values()) pending.reject(failure);
    this.pendingTools.clear();
    this.queryHandle?.close();
    process.exitCode = 1;
    try {
      this.terminateRequests();
    } catch {
      // The nonzero exit code and closed provider/input state remain authoritative.
    }
  }
}

const lines = createInterface({ input: process.stdin, crlfDelay: Infinity, terminal: false });
const worker = new Worker(() => {
  lines.close();
  process.stdin.destroy();
});
let explicitlyClosed = false;

try {
  for await (const line of lines) {
    if (!line.trim()) continue;
    let request: RequestFrame;
    try {
      request = validateRequest(line);
    } catch (error) {
      worker.fail(error);
      break;
    }
    await worker.handle(request);
    if (request.op === "session.close") {
      explicitlyClosed = true;
      lines.close();
      break;
    }
  }
  if (!explicitlyClosed) worker.fail(new ProtocolFailure("WORKER_DIED", "stdin"));
} catch (error) {
  worker.fail(error);
}
