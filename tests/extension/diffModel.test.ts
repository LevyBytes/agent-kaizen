import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { test } from "node:test";

import {
  canonicalSnapshotSetSha256,
  DiffArtifactReader,
  DiffModelError,
  NegotiatedDiff,
  parseNegotiatedDiff,
} from "../../extension/src/diffModel";
import { SecureArtifactStore } from "../../extension/src/artifactStore";

/** Return matched wire and parsed diff fixtures with a canonical digest for round-trip assertions. */
function negotiated(text = "proposed", revision = 1): { body: Record<string, unknown>; diff: NegotiatedDiff } {
  const bytes = Buffer.from(text);
  const sha256 = createHash("sha256").update(bytes).digest("hex");
  const diff: NegotiatedDiff = {
    revision,
    expiresAt: "2026-07-12T12:00:00Z",
    snapshotSetSha256: "",
    fileChanges: [{
      changeId: "change-1",
      path: "src/example.ts",
      kind: "create",
      oldPath: null,
      previewMode: "text",
      previewReason: null,
      before: null,
      proposed: { artifactRef: `sha256:${sha256}`, sha256, bytes: bytes.length, encoding: "utf-8", mediaType: null },
    }],
    metadataConfirmationRequired: false,
  };
  diff.snapshotSetSha256 = canonicalSnapshotSetSha256(diff);
  return {
    diff,
    body: {
      approval_revision: revision,
      expires_at: diff.expiresAt,
      snapshot_set_sha256: diff.snapshotSetSha256,
      file_changes: [{
        change_id: "change-1", path: "src/example.ts", kind: "create", old_path: null,
        preview_mode: "text", preview_reason: null, before: null,
        proposed: { artifact_ref: `sha256:${sha256}`, sha256, bytes: bytes.length, encoding: "utf-8", media_type: null },
      }],
    },
  };
}

test("strict negotiated approval bodies verify the canonical snapshot-set hash", () => {
  const { body, diff } = negotiated();
  assert.deepEqual(parseNegotiatedDiff(JSON.stringify(body)), diff);
  assert.equal(parseNegotiatedDiff(JSON.stringify({ decision: "approve" })), undefined, "legacy approvals remain additive");
  assert.throws(() => parseNegotiatedDiff({ ...body, unexpected: true }), (error: unknown) =>
    error instanceof DiffModelError && error.code === "DENIED_APPROVAL_BODY_INVALID");
  assert.throws(() => parseNegotiatedDiff({ ...body, snapshot_set_sha256: "0".repeat(64) }), (error: unknown) =>
    error instanceof DiffModelError && error.code === "DENIED_APPROVAL_SNAPSHOT_INVALID");
});

test("metadata-only changes require whole-request confirmation", () => {
  const { diff } = negotiated();
  const metadata: NegotiatedDiff = {
    ...diff,
    fileChanges: [{
      ...diff.fileChanges[0], previewMode: "metadata", previewReason: "binary",
      proposed: { ...diff.fileChanges[0].proposed!, encoding: null, mediaType: "application/octet-stream" },
    }],
    metadataConfirmationRequired: true,
  };
  metadata.snapshotSetSha256 = canonicalSnapshotSetSha256(metadata);
  const side = metadata.fileChanges[0].proposed!;
  const parsed = parseNegotiatedDiff({
    approval_revision: metadata.revision,
    expires_at: metadata.expiresAt,
    snapshot_set_sha256: metadata.snapshotSetSha256,
    file_changes: [{
      change_id: "change-1", path: "src/example.ts", kind: "create", old_path: null,
      preview_mode: "metadata", preview_reason: "binary", before: null,
      proposed: { artifact_ref: side.artifactRef, sha256: side.sha256, bytes: side.bytes, encoding: null, media_type: side.mediaType },
    }],
  });
  assert.equal(parsed?.metadataConfirmationRequired, true);
});

test("diff reads only verified fixed-digest cache bytes and rejects corruption", async (t) => {
  const repo = process.env.KAIZEN_REPO_ROOT;
  assert.ok(repo && path.isAbsolute(repo), "run-tests.mjs must pin an absolute KAIZEN_REPO_ROOT plane");
  const content = Buffer.from(`diff-${randomUUID()}`);
  const store = new SecureArtifactStore(repo, "diffs");
  let stored: Awaited<ReturnType<SecureArtifactStore["store"]>> | undefined;
  t.after(async () => {
    if (!stored) return;
    await fs.rm(stored.target, { force: true });
    await fs.rm(path.join(store.root, `${stored.sha256}.meta.json`), { force: true });
  });
  stored = await store.store(content, { origin: "test" });
  const side = { artifactRef: stored.artifactRef, sha256: stored.sha256, bytes: content.length, encoding: "utf-8" as const, mediaType: null };
  assert.equal(await new DiffArtifactReader(repo).text(side), content.toString("utf8"));
  await fs.writeFile(stored.target, "corrupt");
  await assert.rejects(new DiffArtifactReader(repo).text(side), (error: unknown) =>
    error instanceof DiffModelError && error.code === "DENIED_APPROVAL_SNAPSHOT_INVALID");
  await fs.writeFile(stored.target, Buffer.alloc(content.length));
  await assert.rejects(new DiffArtifactReader(repo).text(side), (error: unknown) =>
    error instanceof DiffModelError && error.code === "DENIED_APPROVAL_SNAPSHOT_INVALID");
});
