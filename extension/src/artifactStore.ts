/** Hardened workspace-local content-addressed artifact IO shared by U4 host surfaces. */

import { createHash, randomBytes } from "node:crypto";
import type { Stats } from "node:fs";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { isDeepStrictEqual } from "node:util";

const DIGEST = /^[0-9a-f]{64}$/;
const RESERVED_METADATA = new Set(["version", "kind", "sha256", "bytes", "created_at"]);

export class ArtifactStoreError extends Error {
  constructor(readonly code: string, message: string) {
    super(message);
    this.name = "ArtifactStoreError";
  }
}

/**
 * Workspace-confined content-addressed store that rejects links/non-files and verifies fixed names,
 * file identity, byte counts, and hashes around every read.
 */
export class SecureArtifactStore {
  readonly root: string;

  constructor(private readonly workspaceRoot: string, readonly kind: "images" | "context" | "diffs") {
    this.root = artifactRoot(workspaceRoot, kind);
  }

  /** Idempotently stage content and merge non-reserved JSON metadata, or throw DENIED_ARTIFACT_*. */
  async store(
    content: Buffer,
    metadata: Record<string, unknown>,
  ): Promise<{ artifactRef: string; sha256: string; bytes: number; target: string }> {
    const digest = createHash("sha256").update(content).digest("hex");
    const root = await this.ensureRoot(true);
    const target = path.join(root, digest);
    if (!(await this.verifyObject(target, root, digest, content.length))) {
      await this.atomic(target, content, root, digest);
      if (!(await this.verifyObject(target, root, digest, content.length))) {
        throw new ArtifactStoreError("DENIED_ARTIFACT_INVALID", "Artifact disappeared after staging");
      }
    }
    await this.ensureMetadata(digest, content.length, metadata, root);
    return { artifactRef: `sha256:${digest}`, sha256: digest, bytes: content.length, target };
  }

  /** Re-verify the caller-declared digest/size and stored bytes, or throw DENIED_ARTIFACT_*. */
  async read(artifactRef: string, expectedSha256: string, expectedBytes: number): Promise<Buffer> {
    const digest = artifactDigest(artifactRef);
    if (digest !== expectedSha256 || !Number.isSafeInteger(expectedBytes) || expectedBytes < 0) {
      throw new ArtifactStoreError("DENIED_ARTIFACT_INVALID", "Artifact declaration is invalid");
    }
    const root = await this.ensureRoot(false);
    const target = path.join(root, digest);
    const content = await this.readVerified(target, root, digest);
    if (content.length !== expectedBytes || createHash("sha256").update(content).digest("hex") !== digest) {
      throw new ArtifactStoreError("DENIED_ARTIFACT_CORRUPT", "Artifact size or hash mismatch");
    }
    return content;
  }

  private async ensureRoot(create: boolean): Promise<string> {
    const workspace = path.resolve(this.workspaceRoot);
    const components = ["AI", "work", "orchestration", "ui-cache", this.kind, "sha256"];
    let current = workspace;
    await assertDirectory(current, workspace);
    for (const component of components) {
      current = path.join(current, component);
      if (create) {
        try {
          await fs.mkdir(current);
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code !== "EEXIST") {
            throw new ArtifactStoreError("DENIED_ARTIFACT_WRITE", "Artifact cache creation failed");
          }
        }
      } else {
        try {
          await fs.lstat(current);
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code === "ENOENT") {
            throw new ArtifactStoreError("DENIED_ARTIFACT_MISSING", "Artifact cache does not exist");
          }
          throw error;
        }
      }
      await assertDirectory(current, workspace);
    }
    return fs.realpath(current);
  }

  private async atomic(target: string, content: Buffer, root: string, digest: string): Promise<void> {
    const partial = path.join(root, `.${digest}.${randomBytes(8).toString("hex")}.part`);
    try {
      await fs.writeFile(partial, content, { flag: "wx" });
      try {
        await fs.rename(partial, target);
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "EEXIST" && (error as NodeJS.ErrnoException).code !== "EPERM") throw error;
      }
    } finally {
      await fs.rm(partial, { force: true });
    }
  }

  private async ensureMetadata(
    digest: string,
    bytes: number,
    supplied: Record<string, unknown>,
    root: string,
  ): Promise<void> {
    const custom = Object.fromEntries(Object.entries(supplied).filter(([key]) => !RESERVED_METADATA.has(key)));
    const target = path.join(root, `${digest}.meta.json`);
    const existing = await this.readOptionalVerified(target, root, `${digest}.meta.json`);
    let previous: Record<string, unknown> = {};
    if (existing) {
      try {
        const parsed = JSON.parse(existing.toString("utf8")) as Record<string, unknown>;
        if (parsed.sha256 !== digest || parsed.bytes !== bytes || parsed.kind !== this.kind) throw new Error("metadata mismatch");
        previous = parsed;
        if (Object.entries(custom).every(([key, value]) => isDeepStrictEqual(parsed[key], value))) return;
      } catch {
        throw new ArtifactStoreError("DENIED_ARTIFACT_METADATA", "Artifact metadata is corrupt");
      }
    }
    const encoded = Buffer.from(JSON.stringify({
      ...previous,
      ...custom,
      version: 1,
      kind: this.kind,
      sha256: digest,
      bytes,
      created_at: previous.created_at ?? new Date().toISOString(),
    }), "utf8");
    const partial = path.join(root, `.${digest}.meta.${randomBytes(8).toString("hex")}.part`);
    try {
      await fs.writeFile(partial, encoded, { flag: "wx" });
      try {
        await fs.rename(partial, target);
      } catch (error) {
        // A first-writer race may have landed an equivalent sidecar. Existing metadata updates must
        // never silently lose required origin/media fields.
        if (existing || ((error as NodeJS.ErrnoException).code !== "EEXIST" && (error as NodeJS.ErrnoException).code !== "EPERM")) throw error;
      }
      const landed = await this.readOptionalVerified(target, root, `${digest}.meta.json`);
      if (!landed) throw new ArtifactStoreError("DENIED_ARTIFACT_METADATA", "Artifact metadata disappeared");
      const parsed = JSON.parse(landed.toString("utf8")) as Record<string, unknown>;
      if (parsed.sha256 !== digest || parsed.bytes !== bytes || parsed.kind !== this.kind) {
        throw new ArtifactStoreError("DENIED_ARTIFACT_METADATA", "Artifact metadata race mismatch");
      }
      if (!Object.entries(custom).every(([key, value]) => isDeepStrictEqual(parsed[key], value))) {
        throw new ArtifactStoreError("DENIED_ARTIFACT_METADATA", "Artifact metadata fields were not staged");
      }
    } finally {
      await fs.rm(partial, { force: true });
    }
  }

  private async verifyObject(
    target: string,
    root: string,
    digest: string,
    expectedBytes: number,
  ): Promise<boolean> {
    try {
      const content = await this.readVerified(target, root, digest);
      if (content.length !== expectedBytes || createHash("sha256").update(content).digest("hex") !== digest) {
        throw new ArtifactStoreError("DENIED_ARTIFACT_CORRUPT", "Existing artifact is corrupt");
      }
      return true;
    } catch (error) {
      if (error instanceof ArtifactStoreError && error.code === "DENIED_ARTIFACT_MISSING") return false;
      throw error;
    }
  }

  private async readOptionalVerified(target: string, root: string, expectedName: string): Promise<Buffer | undefined> {
    try {
      return await this.readVerified(target, root, expectedName);
    } catch (error) {
      if (error instanceof ArtifactStoreError && error.code === "DENIED_ARTIFACT_MISSING") return undefined;
      throw error;
    }
  }

  private async readVerified(target: string, root: string, expectedName: string): Promise<Buffer> {
    let initial: Stats;
    try {
      initial = await fs.lstat(target);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") {
        throw new ArtifactStoreError("DENIED_ARTIFACT_MISSING", "Artifact is missing");
      }
      throw error;
    }
    assertPlain(initial);
    assertExact(await fs.realpath(target), target, root, expectedName);
    const handle = await fs.open(target, "r");
    try {
      const [opened, current, currentReal] = await Promise.all([handle.stat(), fs.lstat(target), fs.realpath(target)]);
      assertPlain(current);
      assertExact(currentReal, target, root, expectedName);
      if (!sameFile(initial, opened) || !sameFile(opened, current)) throw new ArtifactStoreError("DENIED_ARTIFACT_PATH", "Artifact target changed");
      const content = await handle.readFile();
      const [after, afterPath, afterReal] = await Promise.all([handle.stat(), fs.lstat(target), fs.realpath(target)]);
      assertPlain(afterPath);
      assertExact(afterReal, target, root, expectedName);
      if (!sameFile(opened, after) || !sameFile(after, afterPath)) throw new ArtifactStoreError("DENIED_ARTIFACT_PATH", "Artifact changed during read");
      return content;
    } catch (error) {
      if (error instanceof ArtifactStoreError) throw error;
      throw new ArtifactStoreError("DENIED_ARTIFACT_PATH", "Artifact verification failed");
    } finally {
      await handle.close();
    }
  }
}

/** Normalize a bare or `sha256:`-prefixed digest and reject invalid values. */
export function artifactDigest(value: string): string {
  const digest = value.startsWith("sha256:") ? value.slice(7) : value;
  if (!DIGEST.test(digest)) throw new ArtifactStoreError("DENIED_ARTIFACT_INVALID", "Invalid artifact reference");
  return digest;
}

/** Return the declared cache root; callers must use SecureArtifactStore for verified IO. */
export function artifactRoot(workspaceRoot: string, kind: "images" | "context" | "diffs"): string {
  return path.join(workspaceRoot, "AI", "work", "orchestration", "ui-cache", kind, "sha256");
}

async function assertDirectory(candidate: string, workspace: string): Promise<void> {
  const stat = await fs.lstat(candidate);
  if (stat.isSymbolicLink() || !stat.isDirectory()) throw new ArtifactStoreError("DENIED_ARTIFACT_PATH", "Cache component is not a plain directory");
  const real = await fs.realpath(candidate);
  const relative = path.relative(workspace, real);
  if (relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw new ArtifactStoreError("DENIED_ARTIFACT_PATH", "Cache component escapes workspace");
  }
}

function assertPlain(stat: Stats): void {
  if (stat.isSymbolicLink() || !stat.isFile()) throw new ArtifactStoreError("DENIED_ARTIFACT_PATH", "Artifact target is not a plain file");
}

function assertExact(resolved: string, target: string, root: string, expectedName: string): void {
  if (!samePath(resolved, target) || !samePath(path.dirname(resolved), root) || path.basename(resolved) !== expectedName) {
    throw new ArtifactStoreError("DENIED_ARTIFACT_PATH", "Artifact target escapes its fixed cache name");
  }
}

function samePath(left: string, right: string): boolean {
  const a = path.resolve(left);
  const b = path.resolve(right);
  return process.platform === "win32" ? a.toLowerCase() === b.toLowerCase() : a === b;
}

function sameFile(left: Stats, right: Stats): boolean {
  return left.dev === right.dev && left.ino === right.ino;
}
