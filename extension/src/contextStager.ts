/** Host-only governed-context validation and content-addressed dirty-selection staging. */

import { createHash } from "node:crypto";
import * as fs from "node:fs/promises";
import * as path from "node:path";

import type { ContextRange, ContextRef } from "./sessionClient";
import { ArtifactStoreError, SecureArtifactStore } from "./artifactStore";

export const CONTEXT_REF_LIMIT = 8;
export const CONTEXT_ITEM_BYTES = 256 * 1024;
export const CONTEXT_TOTAL_BYTES = 1024 * 1024;
const CONTEXT_ITEM_KIB = CONTEXT_ITEM_BYTES / 1024;

export interface StagedContext {
  ref: ContextRef;
  label: string;
  bytes: number;
}

/** Validates governed workspace context and stages content-addressed selections. */
export class ContextStager {
  private readonly store: SecureArtifactStore;

  constructor(private readonly workspaceRoot: string) {
    this.store = new SecureArtifactStore(workspaceRoot, "context");
  }

  /** Validate a workspace file reference; bytes are re-statted, not snapshotted. */
  async file(id: string, absolutePath: string): Promise<StagedContext> {
    const source = await this.safeSource(absolutePath);
    const stat = await fs.stat(source.realPath);
    if (!stat.isFile()) throw new ContextStageError("DENIED_CONTEXT_PATH_INVALID", "Context source must be a file");
    this.assertSize(stat.size);
    return {
      ref: { id: validId(id), kind: "file", source_path: source.relativePath },
      label: path.basename(source.relativePath),
      bytes: stat.size,
    };
  }

  /** Stage exact selection text and return an end-exclusive content-addressed reference. */
  async selection(
    id: string,
    absolutePath: string,
    range: ContextRange,
    exactText: string,
  ): Promise<StagedContext> {
    const source = await this.safeSource(absolutePath);
    assertRange(range);
    const bytes = Buffer.from(exactText, "utf8");
    this.assertSize(bytes.length);
    const digest = createHash("sha256").update(bytes).digest("hex");
    let staged: Awaited<ReturnType<SecureArtifactStore["store"]>>;
    try {
      staged = await this.store.store(bytes, { origin: "selection" });
    } catch (error) {
      if (error instanceof ArtifactStoreError) {
        const pathFailure = error.code === "DENIED_ARTIFACT_PATH";
        throw new ContextStageError(
          pathFailure ? "DENIED_CONTEXT_REPARSE_ESCAPE" : "DENIED_CONTEXT_SNAPSHOT_INVALID",
          pathFailure ? "Context snapshot cache contains a reparse or non-file target" : "Context snapshot could not be staged safely",
        );
      }
      throw error;
    }
    if (staged.sha256 !== digest) throw new ContextStageError("DENIED_CONTEXT_SNAPSHOT_INVALID", "Selection snapshot hash mismatch");
    return {
      ref: {
        id: validId(id),
        kind: "selection",
        source_path: source.relativePath,
        range: structuredClone(range),
        snapshot_ref: `sha256:${digest}`,
        sha256: digest,
        bytes: bytes.length,
        encoding: "utf-8",
      },
      label: `${path.basename(source.relativePath)}:${range.start.line + 1}-${range.end.line + 1}`,
      bytes: bytes.length,
    };
  }

  /** Enforce lexical and realpath containment and return a POSIX workspace path. */
  private async safeSource(candidate: string): Promise<{ realPath: string; relativePath: string }> {
    const root = path.resolve(this.workspaceRoot);
    const lexical = path.resolve(candidate);
    if (!isContained(root, lexical)) {
      throw new ContextStageError("DENIED_CONTEXT_PATH_INVALID", "Context source is outside the workspace");
    }
    let realRoot: string;
    let realSource: string;
    try {
      [realRoot, realSource] = await Promise.all([fs.realpath(root), fs.realpath(lexical)]);
    } catch {
      throw new ContextStageError("DENIED_CONTEXT_PATH_INVALID", "Context source is unavailable");
    }
    if (!isContained(realRoot, realSource)) {
      throw new ContextStageError("DENIED_CONTEXT_REPARSE_ESCAPE", "Context source escapes the workspace through a reparse point");
    }
    const relativePath = path.relative(root, lexical).replace(/\\/g, "/");
    if (!relativePath || relativePath.split("/").some((part) => !part || part === "..")) {
      throw new ContextStageError("DENIED_CONTEXT_PATH_INVALID", "Context source path is invalid");
    }
    if (relativePath.toLocaleLowerCase().startsWith("ai/work/orchestration/ui-cache/")) {
      throw new ContextStageError("DENIED_CONTEXT_PATH_INVALID", "Context sources cannot point into the UI cache");
    }
    return { realPath: realSource, relativePath };
  }

  private assertSize(bytes: number): void {
    if (!Number.isSafeInteger(bytes) || bytes < 0 || bytes > CONTEXT_ITEM_BYTES) {
      throw new ContextStageError("DENIED_CONTEXT_TOO_LARGE", `Each context item is limited to ${CONTEXT_ITEM_KIB} KiB`);
    }
  }
}

/** Enforce at most eight 256-KiB references and a 1-MiB aggregate envelope. */
export function validateContextEnvelope(items: readonly StagedContext[]): void {
  if (items.length > CONTEXT_REF_LIMIT) {
    throw new ContextStageError("DENIED_CONTEXT_COUNT", "At most 8 context references are allowed");
  }
  let total = 0;
  for (const item of items) {
    if (!Number.isSafeInteger(item.bytes) || item.bytes < 0 || item.bytes > CONTEXT_ITEM_BYTES) {
      throw new ContextStageError("DENIED_CONTEXT_TOO_LARGE", `Each context item is limited to ${CONTEXT_ITEM_KIB} KiB`);
    }
    total += item.bytes;
  }
  if (total > CONTEXT_TOTAL_BYTES) {
    throw new ContextStageError("DENIED_CONTEXT_TOTAL_TOO_LARGE", "Context references are limited to 1 MiB total");
  }
}

export class ContextStageError extends Error {
  constructor(readonly code: string, message: string) {
    super(message);
    this.name = "ContextStageError";
  }
}

/** Trim and validate a bounded control-free context identifier. */
function validId(value: string): string {
  const id = value.trim();
  if (!id || id.length > 128 || /[\u0000-\u001f]/.test(id)) {
    throw new ContextStageError("DENIED_CONTEXT_ID_INVALID", "Context id is invalid");
  }
  return id;
}

/** Validate non-negative safe-integer coordinates and end-exclusive ordering. */
function assertRange(range: ContextRange): void {
  const values = [range.start.line, range.start.character, range.end.line, range.end.character];
  if (!values.every((value) => Number.isSafeInteger(value) && value >= 0)) {
    throw new ContextStageError("DENIED_CONTEXT_RANGE_INVALID", "Selection range is invalid");
  }
  if (range.end.line < range.start.line || (range.end.line === range.start.line && range.end.character < range.start.character)) {
    throw new ContextStageError("DENIED_CONTEXT_RANGE_INVALID", "Selection end must be end-exclusive and after its start");
  }
}

/** Return true only when candidate is a strict descendant of root. */
function isContained(root: string, candidate: string): boolean {
  const relative = path.relative(root, candidate);
  return relative !== "" && relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);
}
