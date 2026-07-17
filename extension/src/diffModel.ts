/** Negotiated-diff parser: body errors and snapshot errors are distinct; expiry is not hash-anchored. */

import { createHash } from "node:crypto";
import { SecureArtifactStore } from "./artifactStore";

const SHA = /^[0-9a-f]{64}$/;
const UTC_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z$/;
const BODY_KEYS = ["approval_revision", "expires_at", "file_changes", "snapshot_set_sha256"];
const CHANGE_KEYS = ["before", "change_id", "kind", "old_path", "path", "preview_mode", "preview_reason", "proposed"];
const SIDE_KEYS = ["artifact_ref", "bytes", "encoding", "media_type", "sha256"];

export interface DiffSideRef {
  artifactRef: string;
  sha256: string;
  bytes: number;
  encoding: "utf-8" | null;
  mediaType: string | null;
}

export interface DiffFileChange {
  changeId: string;
  path: string;
  kind: "create" | "modify" | "delete" | "rename";
  oldPath: string | null;
  previewMode: "text" | "metadata";
  previewReason: "binary" | "unsupported_encoding" | "oversize" | null;
  before: DiffSideRef | null;
  proposed: DiffSideRef | null;
}

export interface NegotiatedDiff {
  revision: number;
  expiresAt: string;
  snapshotSetSha256: string;
  fileChanges: DiffFileChange[];
  metadataConfirmationRequired: boolean;
}

export class DiffModelError extends Error {
  constructor(readonly code: string, message: string) {
    super(message);
    this.name = "DiffModelError";
  }
}

export class DiffArtifactReader {
  private readonly store: SecureArtifactStore;

  constructor(workspaceRoot: string) {
    this.store = new SecureArtifactStore(workspaceRoot, "diffs");
  }

  /** Read a verified native UTF-8 side, mapping artifact failures to snapshot-invalid. */
  async text(side: DiffSideRef | null): Promise<string> {
    if (side === null) return "";
    if (!isNativeTextSide(side)) {
      throw new DiffModelError("DENIED_APPROVAL_SNAPSHOT_INVALID", "Diff side is not a native text preview");
    }
    try {
      const content = await this.store.read(side.artifactRef, side.sha256, side.bytes);
      return new TextDecoder("utf-8", { fatal: true }).decode(content);
    } catch (error) {
      if (error instanceof DiffModelError) throw error;
      throw new DiffModelError("DENIED_APPROVAL_SNAPSHOT_INVALID", "Diff artifact is missing or corrupt");
    }
  }
}

/** Validate negotiated metadata and its canonical hash; legacy bodies return undefined. */
export function parseNegotiatedDiff(value: unknown): NegotiatedDiff | undefined {
  let body: unknown = value;
  if (typeof value === "string") {
    try { body = JSON.parse(value); } catch { return undefined; }
  }
  if (!record(body) || !("snapshot_set_sha256" in body)) return undefined;
  exactKeys(body, BODY_KEYS);
  const revision = body.approval_revision;
  if (!Number.isSafeInteger(revision) || (revision as number) < 1) invalid();
  const expiresAt = text(body.expires_at, 64);
  if (!UTC_TIMESTAMP.test(expiresAt) || Number.isNaN(Date.parse(expiresAt))) invalid();
  const snapshotSetSha256 = digest(body.snapshot_set_sha256, true);
  if (!Array.isArray(body.file_changes) || body.file_changes.length < 1 || body.file_changes.length > 64) invalid();
  let total = 0;
  const ids = new Set<string>();
  const fileChanges = body.file_changes.map((raw): DiffFileChange => {
    if (!record(raw)) invalid();
    exactKeys(raw, CHANGE_KEYS);
    const changeId = text(raw.change_id, 256);
    if (ids.has(changeId)) invalid();
    ids.add(changeId);
    const targetPath = relativePath(raw.path);
    const kind = raw.kind;
    if (kind !== "create" && kind !== "modify" && kind !== "delete" && kind !== "rename") invalid();
    const oldPath = raw.old_path === null ? null : relativePath(raw.old_path);
    const previewMode = raw.preview_mode;
    if (previewMode !== "text" && previewMode !== "metadata") invalid();
    const previewReason = raw.preview_reason;
    if (previewReason !== null && previewReason !== "binary" && previewReason !== "unsupported_encoding" && previewReason !== "oversize") invalid();
    if ((previewMode === "text") !== (previewReason === null)) invalid();
    const before = side(raw.before);
    const proposed = side(raw.proposed);
    if (kind === "create" && (before !== null || proposed === null || oldPath !== null)) invalid();
    if (kind === "modify" && (before === null || proposed === null || oldPath !== null)) invalid();
    if (kind === "delete" && (before === null || proposed !== null || oldPath !== null)) invalid();
    if (kind === "rename" && (!oldPath || oldPath === targetPath || before === null || proposed === null)) invalid();
    const sides = [before, proposed].filter((entry): entry is DiffSideRef => entry !== null);
    if (previewMode === "text" && sides.some((entry) => !isNativeTextSide(entry))) snapshotInvalid();
    total += sides.reduce((sum, entry) => sum + entry.bytes, 0);
    return { changeId, path: targetPath, kind, oldPath, previewMode, previewReason, before, proposed };
  });
  if (total > 64 * 1024 * 1024) snapshotInvalid();
  const normalized = { revision: revision as number, expiresAt, snapshotSetSha256, fileChanges,
    metadataConfirmationRequired: fileChanges.some((change) => change.previewMode === "metadata") };
  if (canonicalSnapshotSetSha256(normalized) !== snapshotSetSha256) {
    throw new DiffModelError("DENIED_APPROVAL_SNAPSHOT_INVALID", "Diff manifest hash mismatch");
  }
  return normalized;
}

/** Produce the daemon-compatible canonical snapshot-manifest SHA-256 trust anchor. */
export function canonicalSnapshotSetSha256(diff: Pick<NegotiatedDiff, "revision" | "fileChanges">): string {
  const changes = diff.fileChanges.map((change) => ({
    change_id: change.changeId,
    path: change.path,
    kind: change.kind,
    old_path: change.oldPath,
    preview_mode: change.previewMode,
    preview_reason: change.previewReason,
    before: manifestSide(change.before),
    proposed: manifestSide(change.proposed),
  // change_id uniqueness makes this final tie-breaker a total order.
  })).sort((left, right) =>
    compareCodePoints(left.path, right.path) || compareCodePoints(left.old_path ?? "", right.old_path ?? "") ||
    compareCodePoints(left.kind, right.kind) || compareCodePoints(left.change_id, right.change_id));
  const canonical = canonicalJson({ approval_revision: diff.revision, file_changes: changes });
  return createHash("sha256").update(Buffer.from(canonical, "utf8")).digest("hex");
}

/** Parse one side and enforce the artifact, encoding/media, digest, and byte invariants. */
function side(value: unknown): DiffSideRef | null {
  if (value === null) return null;
  if (!record(value)) invalid();
  exactKeys(value, SIDE_KEYS);
  const sha256 = digest(value.sha256, true);
  if (value.artifact_ref !== `sha256:${sha256}` || !Number.isSafeInteger(value.bytes) || (value.bytes as number) < 0) snapshotInvalid();
  const encoding = value.encoding;
  const mediaType = value.media_type;
  if ((encoding === null) === (mediaType === null)) invalid();
  if (encoding !== null && encoding !== "utf-8") invalid();
  if (mediaType !== null) text(mediaType, 128);
  return {
    artifactRef: `sha256:${sha256}`,
    sha256,
    bytes: value.bytes as number,
    encoding: encoding as "utf-8" | null,
    mediaType: mediaType as string | null,
  };
}

/** Project a side to the exact daemon-hashed manifest fields. */
function manifestSide(value: DiffSideRef | null): Record<string, unknown> | null {
  return value ? { sha256: value.sha256, bytes: value.bytes, encoding: value.encoding, media_type: value.mediaType } : null;
}

/** Serialize recursively with sorted object keys; manifest keys are fixed ASCII. */
function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (record(value)) return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  return JSON.stringify(value);
}

/** Match the daemon's locale-independent Unicode scalar ordering. */
function compareCodePoints(left: string, right: string): number {
  const a = [...left];
  const b = [...right];
  for (let index = 0; index < Math.min(a.length, b.length); index += 1) {
    const delta = (a[index].codePointAt(0) ?? 0) - (b[index].codePointAt(0) ?? 0);
    if (delta) return delta;
  }
  return a.length - b.length;
}

/** Validate a portable relative path without dot segments, drives, or backslashes. */
function relativePath(value: unknown): string {
  const candidate = text(value, 1024);
  if (candidate.includes("\\") || candidate.includes(":") || candidate.startsWith("/") ||
      candidate.split("/").some((part) => !part || part === "." || part === "..")) snapshotInvalid();
  return candidate;
}

/** Require an exact sorted-key match. */
function exactKeys(value: Record<string, unknown>, expected: string[]): void {
  const keys = Object.keys(value).sort();
  if (keys.length !== expected.length || keys.some((key, index) => key !== expected[index])) invalid();
}

/** Validate a lowercase SHA-256 digest, selecting the requested error family. */
function digest(value: unknown, snapshot = false): string {
  if (typeof value !== "string" || !SHA.test(value)) {
    if (snapshot) snapshotInvalid();
    invalid();
  }
  return value;
}

/** Validate a bounded non-empty UI-safe string. */
function text(value: unknown, max: number): string {
  if (typeof value !== "string" || !value || value.length > max || /[\u0000-\u001f\u007f-\u009f\u2028\u2029]/.test(value)) invalid();
  return value;
}

/** Narrow non-null, non-array objects. */
function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Throw a negotiated-body validation error. */
function invalid(): never {
  throw new DiffModelError("DENIED_APPROVAL_BODY_INVALID", "Negotiated diff metadata is invalid");
}

/** Throw a negotiated-snapshot validation error. */
function snapshotInvalid(): never {
  throw new DiffModelError("DENIED_APPROVAL_SNAPSHOT_INVALID", "Negotiated diff snapshot metadata is invalid");
}

function isNativeTextSide(side: DiffSideRef): boolean {
  return side.encoding === "utf-8" && side.mediaType === null && side.bytes <= 8 * 1024 * 1024;
}
