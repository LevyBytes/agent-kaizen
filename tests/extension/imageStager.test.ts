import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { test } from "node:test";

import {
  detectImageMediaType,
  IMAGE_BYTES_LIMIT,
  ImageChunkAssembler,
  ImageStageError,
  ImageStager,
  StagedImage,
  validateImageEnvelope,
} from "../../extension/src/imageStager";

/** Build minimal unique PNG bytes so content-addressed fixtures do not deduplicate across tests. */
const png = (suffix = "x") => Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), Buffer.from(suffix)]);

/** Create an auto-cleaned fixture in the runner-pinned isolated repository plane. */
async function workspace(t: { after(fn: () => Promise<void>): void }): Promise<{ repo: string; root: string }> {
  const repo = process.env.KAIZEN_REPO_ROOT;
  assert.ok(repo && path.isAbsolute(repo), "run-tests.mjs must provide an absolute KAIZEN_REPO_ROOT");
  const root = path.join(repo, "AI", "work", "tmp", `extension-u4-image-${randomUUID()}`);
  await fs.mkdir(root, { recursive: true });
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  return { repo, root };
}

/** Register cleanup for the shared cache blob and metadata sidecar addressed by content digest. */
function cleanupArtifact(t: { after(fn: () => Promise<void>): void }, repo: string, content: Buffer): void {
  const digest = createHash("sha256").update(content).digest("hex");
  const root = path.join(repo, "AI", "work", "orchestration", "ui-cache", "images", "sha256");
  t.after(async () => {
    await fs.rm(path.join(root, digest), { force: true });
    await fs.rm(path.join(root, `${digest}.meta.json`), { force: true });
  });
}

test("PNG/JPEG/WebP/GIF magic is exact and mismatched declarations fail closed", () => {
  assert.equal(detectImageMediaType(png()), "image/png");
  assert.equal(detectImageMediaType(Buffer.from([0xff, 0xd8, 0xff, 0x00])), "image/jpeg");
  assert.equal(detectImageMediaType(Buffer.from("RIFFxxxxWEBP", "ascii")), "image/webp");
  assert.equal(detectImageMediaType(Buffer.from("GIF89a", "ascii")), "image/gif");
  assert.equal(detectImageMediaType(Buffer.from("not-image")), undefined);
});

test("image bytes stage once with opaque hash metadata, sidecar, and safe deduplication", async (t) => {
  const { repo } = await workspace(t);
  const content = png(`dedupe-${randomUUID()}`);
  cleanupArtifact(t, repo, content);
  const stager = new ImageStager(repo);
  const first = await stager.bytes("image-1", content, "capture.png", "image/png");
  const second = await stager.bytes("image-2", content, "same.png", "image/png");
  const digest = createHash("sha256").update(content).digest("hex");
  assert.equal(first.ref.artifact_ref, `sha256:${digest}`);
  assert.equal(first.ref.sha256, digest);
  assert.equal(first.ref.bytes, content.length);
  assert.equal(second.ref.sha256, digest);
  const root = path.join(repo, "AI", "work", "orchestration", "ui-cache", "images", "sha256");
  assert.deepEqual(await fs.readFile(path.join(root, digest)), content);
  const metadata = JSON.parse(await fs.readFile(path.join(root, `${digest}.meta.json`), "utf8"));
  assert.equal(metadata.kind, "images");
  assert.equal(metadata.media_type, "image/png");
  assert.equal(metadata.origin, "host_image");
});

test("invalid magic, declaration mismatch, and size overflow are denied", async (t) => {
  const { repo } = await workspace(t);
  const stager = new ImageStager(repo);
  await assert.rejects(stager.bytes("bad", Buffer.from("x"), "bad.png"), (error: unknown) =>
    error instanceof ImageStageError && error.code === "DENIED_ATTACHMENT_INVALID");
  await assert.rejects(stager.bytes("bad-mime", png(), "bad.jpg", "image/jpeg"), (error: unknown) =>
    error instanceof ImageStageError && error.code === "DENIED_ATTACHMENT_INVALID");
  await assert.rejects(stager.bytes("large", Buffer.concat([png(), Buffer.alloc(IMAGE_BYTES_LIMIT)]), "large.png"),
    (error: unknown) => error instanceof ImageStageError && error.code === "DENIED_ATTACHMENT_TOO_LARGE");
  await assert.rejects(stager.file("relative", "relative.png"), (error: unknown) =>
    error instanceof ImageStageError && error.code === "DENIED_ATTACHMENT_PATH");
});

test("an explicitly selected external D-temp image is staged while symlink and junction redirects fail closed", async (t) => {
  const { root } = await workspace(t);
  const workspaceRoot = path.join(root, "selected-workspace");
  await fs.mkdir(workspaceRoot);
  const external = path.join(root, "picker-external.png");
  await fs.writeFile(external, png(`external-${randomUUID()}`));
  const stager = new ImageStager(workspaceRoot);
  const selected = await stager.file("external", external);
  assert.equal(selected.ref.name, "picker-external.png");
  assert.equal(selected.ref.media_type, "image/png");
  assert.equal("source_path" in selected.ref, false, "the selected host path never crosses the daemon wire");

  const link = path.join(root, "link.png");
  let redirectKind: "symlink" | "junction";
  try {
    await fs.symlink(external, link, "file");
    redirectKind = "symlink";
    await assert.rejects(stager.file("link", link), (error: unknown) =>
      error instanceof ImageStageError && error.code === "DENIED_ATTACHMENT_PATH");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EPERM") throw error;
    redirectKind = "junction";
    const realDirectory = path.join(root, "real-directory");
    const junction = path.join(root, "redirect-directory");
    await fs.mkdir(realDirectory);
    await fs.writeFile(path.join(realDirectory, "image.png"), png());
    await fs.symlink(realDirectory, junction, "junction");
    await assert.rejects(stager.file("junction", path.join(junction, "image.png")), (nested: unknown) =>
      nested instanceof ImageStageError && nested.code === "DENIED_ATTACHMENT_PATH");
  }
  assert.ok(redirectKind === "symlink" || redirectKind === "junction");
});

test("ImageRef names are sanitized and truncated once to 128 Unicode code points", async (t) => {
  const { root } = await workspace(t);
  const workspaceRoot = path.join(root, "name-workspace");
  await fs.mkdir(workspaceRoot);
  const stager = new ImageStager(workspaceRoot);
  const longPath = path.join(root, `${"x".repeat(140)}.png`);
  await fs.writeFile(longPath, png(`name-${randomUUID()}`));
  const derived = await stager.file("derived", longPath);
  assert.equal([...(derived.ref.name ?? "")].length, 128, "picker basenames truncate without splitting a code point");
  assert.equal(derived.ref.name, "x".repeat(128), "the extension is intentionally dropped beyond the 128-code-point boundary");

  const unicodePath = path.join(root, `${"y".repeat(127)}${"\u{1f600}"}tail.png`);
  await fs.writeFile(unicodePath, png(`unicode-${randomUUID()}`));
  const unicodeDerived = await stager.file("unicode-derived", unicodePath);
  assert.equal([...(unicodeDerived.ref.name ?? "")].length, 128);
  assert.equal([...(unicodeDerived.ref.name ?? "")].at(-1), "\u{1f600}", "the derived-name boundary preserves the complete surrogate pair");

  // The name is independent of the unique PNG fixture content supplied to bytes().
  const exact = await stager.bytes("exact", png(`exact-${randomUUID()}`), "😀".repeat(128));
  assert.equal([...(exact.ref.name ?? "")].length, 128);
  const truncated = await stager.bytes("too-long", png(`truncated-${randomUUID()}`), "😀".repeat(129));
  assert.equal([...(truncated.ref.name ?? "")].length, 128);
});

test("precreated image-cache reparse target is rejected before bytes are read", async (t) => {
  const { repo, root } = await workspace(t);
  const content = png(`redirect-${randomUUID()}`);
  const digest = createHash("sha256").update(content).digest("hex");
  const cache = path.join(repo, "AI", "work", "orchestration", "ui-cache", "images", "sha256");
  const target = path.join(cache, digest);
  await fs.mkdir(cache, { recursive: true });
  const outside = path.join(root, "outside.png");
  await fs.writeFile(outside, content);
  t.after(() => fs.rm(target, { recursive: true, force: true }).catch(() => undefined));
  try {
    await fs.symlink(outside, target, "file");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EPERM") throw error;
    const directory = path.join(root, "outside-directory");
    await fs.mkdir(directory);
    await fs.symlink(directory, target, "junction");
  }
  await assert.rejects(new ImageStager(repo).bytes("redirect", content, "redirect.png"), (error: unknown) =>
    error instanceof ImageStageError && error.code === "DENIED_ARTIFACT_PATH");
});

test("paste assembler enforces ordered bounded chunks and produces a Claude-ready ImageRef", async (t) => {
  const { repo } = await workspace(t);
  const content = png(`paste-${randomUUID()}`);
  cleanupArtifact(t, repo, content);
  const assembler = new ImageChunkAssembler(new ImageStager(repo));
  assembler.begin({ uploadId: "upload-1", name: "paste.png", mediaType: "image/png", bytes: content.length });
  assembler.chunk("upload-1", 0, Array.from(content.subarray(0, 5)));
  assembler.chunk("upload-1", 1, Array.from(content.subarray(5)));
  const staged = await assembler.finish("upload-1", "image-paste");
  assert.equal(staged.ref.kind, "image");
  assert.equal(staged.ref.media_type, "image/png");
  assert.match(staged.ref.artifact_ref, /^sha256:[0-9a-f]{64}$/);

  assembler.begin({ uploadId: "upload-bad", name: "paste.png", mediaType: "image/png", bytes: content.length });
  assert.throws(() => assembler.chunk("upload-bad", 1, [1]), (error: unknown) =>
    error instanceof ImageStageError && error.code === "DENIED_ATTACHMENT_INVALID");
});

test("image envelope accepts four and rejects five", () => {
  const item = (index: number): StagedImage => ({
    ref: {
      id: `image-${index}`, kind: "image", artifact_ref: `sha256:${String(index).padStart(64, "a")}`,
      sha256: String(index).padStart(64, "a"), bytes: 10, media_type: "image/png", name: `${index}.png`,
    },
    label: `${index}.png`,
  });
  assert.doesNotThrow(() => validateImageEnvelope(Array.from({ length: 4 }, (_, index) => item(index))));
  assert.throws(() => validateImageEnvelope(Array.from({ length: 5 }, (_, index) => item(index))),
    (error: unknown) => error instanceof ImageStageError && error.code === "DENIED_ATTACHMENT_COUNT");
});
