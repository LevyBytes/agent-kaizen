import assert from "node:assert/strict";
import { test } from "node:test";

import { MentionIndex, normalizeRelativePath, parseGitLsFiles } from "../../extension/src/mentionIndex";

test("NUL records are split before UTF-8 decoding and preserve whitespace/newlines in safe paths", () => {
  const bytes = Buffer.from("src/alpha.ts\0notes/file with spaces.md\0odd/line\nname.txt\0odd/carriage\rreturn.txt\0", "utf8");
  assert.deepEqual(parseGitLsFiles(bytes), [
    "src/alpha.ts", "notes/file with spaces.md", "odd/line\nname.txt", "odd/carriage\rreturn.txt",
  ]);
  assert.throws(
    () => parseGitLsFiles(Buffer.from("unterminated", "utf8")),
    /^Error: git ls-files returned a non-NUL-terminated record$/,
  );
  assert.throws(
    () => parseGitLsFiles(Buffer.alloc((4 * 1024 * 1024) + 1)),
    /^Error: git ls-files output exceeded 4 MiB$/,
  );
});

test("query is filter-only for the injected listing runner and sorted results are capped at 50", async () => {
  const calls: string[] = [];
  const paths = Array.from({ length: 70 }, (_, index) => `src/file-${String(index).padStart(2, "0")}.ts`);
  const index = new MentionIndex(
    "D:\\repo",
    async () => [],
    async (cwd) => {
      calls.push(cwd);
      return Buffer.from(`${[...paths, paths[0]].reverse().join("\0")}\0`, "utf8");
    },
  );
  const result = await index.search("$(danger); --not-an-argument");
  assert.deepEqual(calls, ["D:\\repo"]);
  assert.deepEqual(result, []);
  const capped = await index.search("file-");
  assert.equal(capped.length, 50);
  assert.equal(capped[0], "src/file-00.ts");
  assert.equal(capped[49], "src/file-49.ts");
  assert.equal(capped.filter((value) => value === "src/file-00.ts").length, 1, "duplicate paths are removed");
});

test("nonzero/error runner falls back to safe findFiles values and normalization rejects escapes", async () => {
  let fallbacks = 0;
  let gitAttempts = 0;
  const index = new MentionIndex(
    "D:\\repo",
    async () => {
      fallbacks += 1;
      return ["src\\ok.ts", "../escape.txt", "D:\\outside.txt", "./README.md"];
    },
    async () => {
      gitAttempts += 1;
      if (gitAttempts === 1) throw new Error("git unavailable");
      return Buffer.from("src/from-git.ts\0", "utf8");
    },
  );
  assert.deepEqual(await index.search(), ["README.md", "src/ok.ts"]);
  assert.equal(fallbacks, 1);
  assert.deepEqual(await index.search(), ["src/from-git.ts"], "fallback results must not be cached after Git recovers");
  assert.equal(gitAttempts, 2);
  assert.equal(fallbacks, 1);
  assert.equal(normalizeRelativePath("../../x"), undefined);
  assert.equal(normalizeRelativePath("/absolute"), undefined);
  assert.equal(normalizeRelativePath("C:/absolute"), undefined);
  assert.equal(normalizeRelativePath("a//b"), undefined);
  assert.equal(normalizeRelativePath("a/./b"), undefined);
});
