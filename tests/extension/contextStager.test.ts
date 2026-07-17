/** Context-stager tests; run-tests.mjs pins cwd to extension/ so cwd/.. is the repository root. */
import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { test } from "node:test";

import {
  CONTEXT_ITEM_BYTES,
  ContextStageError,
  ContextStager,
  StagedContext,
  validateContextEnvelope,
} from "../../extension/src/contextStager";
import { SecureArtifactStore } from "../../extension/src/artifactStore";

/** Create an auto-cleaned, UUID-scoped AI/work fixture and return both its repository and scratch roots. */
async function workspace(t: { after(fn: () => Promise<void>): void }): Promise<{ repo: string; root: string }> {
  const configuredRepo = process.env.KAIZEN_REPO_ROOT;
  assert.ok(configuredRepo && path.isAbsolute(configuredRepo), "run through run-tests.mjs so KAIZEN_REPO_ROOT is pinned");
  const repo = path.resolve(configuredRepo);
  const root = path.join(repo, "AI", "work", "tmp", `extension-u3-${randomUUID()}`);
  await fs.mkdir(root, { recursive: true });
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  return { repo, root };
}

test("dirty Unicode selection is staged as exact UTF-8 end-exclusive bytes with opaque hash reference", async (t) => {
  const { repo, root } = await workspace(t);
  const source = path.join(root, "dirty.ts");
  await fs.writeFile(source, "saved text", "utf8");
  const exact = "A\ud83d\ude42e\u0301\r\n\u7d42";
  const digest = createHash("sha256").update(Buffer.from(exact, "utf8")).digest("hex");
  const range = { start: { line: 2, character: 3 }, end: { line: 4, character: 1 } };
  const staged = await new ContextStager(repo).selection("selection-1", source, range, exact);
  assert.equal(staged.ref.kind, "selection");
  if (staged.ref.kind !== "selection") throw new Error("selection expected");
  assert.equal(staged.ref.snapshot_ref, `sha256:${digest}`);
  assert.equal(staged.ref.sha256, digest);
  assert.equal(staged.ref.bytes, Buffer.byteLength(exact, "utf8"));
  assert.equal(staged.ref.encoding, "utf-8");
  assert.equal(staged.ref.id, "selection-1");
  assert.equal(staged.bytes, Buffer.byteLength(exact, "utf8"));
  assert.equal(staged.label, "dirty.ts:3-5");
  assert.deepEqual(staged.ref.range, range);
  const cached = path.join(repo, "AI", "work", "orchestration", "ui-cache", "context", "sha256", digest);
  t.after(() => fs.rm(cached, { force: true }));
  const sidecar = `${cached}.meta.json`;
  t.after(() => fs.rm(sidecar, { force: true }));
  assert.deepEqual(await fs.readFile(cached), Buffer.from(exact, "utf8"));
  assert.equal(JSON.parse(await fs.readFile(sidecar, "utf8")).origin, "selection");
});

test("invalid ids, ranges, non-files, and mismatched snapshot digests fail closed", async (t) => {
  const { repo, root } = await workspace(t);
  const source = path.join(root, "source.txt");
  await fs.writeFile(source, "source", "utf8");
  const stager = new ContextStager(repo);
  const validRange = { start: { line: 0, character: 0 }, end: { line: 0, character: 1 } };
  for (const id of ["", "x".repeat(129), "bad\ncontrol"]) {
    await assert.rejects(stager.selection(id, source, validRange, "x"), (error: unknown) =>
      error instanceof ContextStageError && error.code === "DENIED_CONTEXT_ID_INVALID");
  }
  for (const range of [
    { start: { line: -1, character: 0 }, end: { line: 0, character: 0 } },
    { start: { line: 0.5, character: 0 }, end: { line: 1, character: 0 } },
    { start: { line: 2, character: 0 }, end: { line: 1, character: 0 } },
  ]) {
    await assert.rejects(stager.selection("range", source, range, "x"), (error: unknown) =>
      error instanceof ContextStageError && error.code === "DENIED_CONTEXT_RANGE_INVALID");
  }
  await assert.rejects(stager.file("directory", root), (error: unknown) =>
    error instanceof ContextStageError && error.code === "DENIED_CONTEXT_PATH_INVALID");

  const mismatched = new ContextStager(repo) as unknown as { selection: ContextStager["selection"]; store: SecureArtifactStore };
  const originalStore = mismatched.store.store.bind(mismatched.store);
  mismatched.store.store = async (...args) => ({ ...(await originalStore(...args)), sha256: "0".repeat(64) });
  await assert.rejects(mismatched.selection("mismatch", source, validRange, "x"), (error: unknown) =>
    error instanceof ContextStageError && error.code === "DENIED_CONTEXT_SNAPSHOT_INVALID");
});

test("artifact metadata verification compares structured JSON values by value", async (t) => {
  const { repo } = await workspace(t);
  const content = Buffer.from(`structured-metadata-${randomUUID()}`);
  const store = new SecureArtifactStore(repo, "context");
  const metadata = { origin: { kind: "selection", tags: ["dirty", "unicode"] } };
  const staged = await store.store(content, metadata);
  const cache = path.dirname(staged.target);
  t.after(async () => {
    await fs.rm(staged.target, { force: true });
    await fs.rm(path.join(cache, `${staged.sha256}.meta.json`), { force: true });
  });
  await assert.doesNotReject(store.store(content, structuredClone(metadata)));
  const sidecar = JSON.parse(await fs.readFile(path.join(cache, `${staged.sha256}.meta.json`), "utf8"));
  assert.deepEqual(sidecar.origin, metadata.origin);
});

test("a precreated content-addressed symlink or junction is rejected before artifact bytes are read", async (t) => {
  const { repo, root } = await workspace(t);
  const source = path.join(root, "dirty-link.ts");
  await fs.writeFile(source, "saved", "utf8");
  const exact = `redirect-${randomUUID()}`;
  const digest = createHash("sha256").update(Buffer.from(exact, "utf8")).digest("hex");
  const cache = path.join(repo, "AI", "work", "orchestration", "ui-cache", "context", "sha256");
  const target = path.join(cache, digest);
  await fs.mkdir(cache, { recursive: true });
  const outside = path.join(root, "outside-object");
  await fs.writeFile(outside, exact, "utf8");
  let redirectedSentinel = outside;
  t.after(() => fs.unlink(target).catch(() => undefined));
  try {
    await fs.symlink(outside, target, "file");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EPERM") throw error;
    const outsideDirectory = path.join(root, "outside-directory");
    await fs.mkdir(outsideDirectory);
    redirectedSentinel = path.join(outsideDirectory, "sentinel");
    await fs.writeFile(redirectedSentinel, exact, "utf8");
    await fs.symlink(outsideDirectory, target, "junction");
  }

  await assert.rejects(
    new ContextStager(repo).selection(
      "selection-link",
      source,
      { start: { line: 0, character: 0 }, end: { line: 0, character: exact.length } },
      exact,
    ),
    (error: unknown) => error instanceof ContextStageError && error.code === "DENIED_CONTEXT_REPARSE_ESCAPE",
  );
  assert.equal(await fs.readFile(redirectedSentinel, "utf8"), exact, "the redirected object remains unread/untouched by materialization");
});

test("file references have the exact daemon shape and reject outside/cache/reparse escapes", async (t) => {
  const { repo, root } = await workspace(t);
  const source = path.join(root, "small.txt");
  await fs.writeFile(source, "small", "utf8");
  const stager = new ContextStager(repo);
  const staged = await stager.file("file-1", source);
  assert.deepEqual(staged.ref, {
    id: "file-1",
    kind: "file",
    source_path: path.relative(repo, source).replace(/\\/g, "/"),
  });
  await assert.rejects(stager.file("outside", path.resolve(repo, "..")), (error: unknown) =>
    error instanceof ContextStageError && error.code === "DENIED_CONTEXT_PATH_INVALID");

  const cacheSource = path.join(repo, "AI", "work", "orchestration", "ui-cache", "context", "forbidden.txt");
  t.after(() => fs.rm(cacheSource, { force: true }));
  await fs.mkdir(path.dirname(cacheSource), { recursive: true });
  await fs.writeFile(cacheSource, "no", "utf8");
  await assert.rejects(stager.file("cache", cacheSource), (error: unknown) =>
    error instanceof ContextStageError && error.code === "DENIED_CONTEXT_PATH_INVALID");

  const junction = path.join(root, "escape-junction");
  try {
    await fs.symlink(path.resolve(repo, ".."), junction, "junction");
    await assert.rejects(stager.file("reparse", junction), (error: unknown) =>
      error instanceof ContextStageError && error.code === "DENIED_CONTEXT_REPARSE_ESCAPE");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EPERM") throw error;
  }
});

test("host envelope validation enforces 8 refs, 256 KiB each, and 1 MiB total", () => {
  const item = (index: number, bytes: number): StagedContext => ({
    ref: { id: `file-${index}`, kind: "file", source_path: `file-${index}.txt` },
    label: `file-${index}.txt`,
    bytes,
  });
  assert.doesNotThrow(() => validateContextEnvelope(Array.from({ length: 4 }, (_, index) => item(index, CONTEXT_ITEM_BYTES))));
  assert.doesNotThrow(() => validateContextEnvelope(Array.from({ length: 8 }, (_, index) => item(index, CONTEXT_ITEM_BYTES / 2))));
  assert.throws(() => validateContextEnvelope(Array.from({ length: 5 }, (_, index) => item(index, CONTEXT_ITEM_BYTES))),
    (error: unknown) => error instanceof ContextStageError && error.code === "DENIED_CONTEXT_TOTAL_TOO_LARGE");
  assert.throws(() => validateContextEnvelope([item(0, CONTEXT_ITEM_BYTES + 1)]),
    (error: unknown) => error instanceof ContextStageError && error.code === "DENIED_CONTEXT_TOO_LARGE");
  assert.throws(() => validateContextEnvelope(Array.from({ length: 9 }, (_, index) => item(index, 1))),
    (error: unknown) => error instanceof ContextStageError && error.code === "DENIED_CONTEXT_COUNT");
});
