/** Pin extension-test scratch to a physical, non-system-drive root under AI/work; the runner sets cwd to extension/ and supplies its bare run root through KAIZEN_TEST_TEMP_ROOT. */
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

let root: string | undefined;
let ownsRoot = false;
const ownedSocketRoots = new Set<string>();
const MAX_POSIX_SOCKET_PATH_BYTES = 103;

/** Compare resolved paths case-insensitively on Windows without resolving links. */
function samePath(left: string, right: string): boolean {
  const normalize = process.platform === "win32" ? (value: string) => path.resolve(value).toLowerCase() : (value: string) => path.resolve(value);
  return normalize(left) === normalize(right);
}

/** Reject Windows candidates on the system drive; this is a no-op elsewhere. */
function rejectSystemDrive(candidate: string): void {
  if (process.platform !== "win32") return;
  const systemDrive = (process.env.SystemDrive || "C:").replace(/[\\/]+$/, "").toLowerCase();
  const candidateDrive = path.parse(candidate).root.replace(/[\\/]+$/, "").toLowerCase();
  if (candidateDrive === systemDrive) throw new Error(`extension tests must not use the Windows system drive: ${candidate}`);
}

/** Require a physical directory whose native real path equals the candidate. */
function assertPlainDirectory(candidate: string): void {
  const stat = fs.lstatSync(candidate);
  if (!stat.isDirectory()) throw new Error(`extension test temp root must be a physical directory: ${candidate}`);
  if (!samePath(fs.realpathSync.native(candidate), candidate)) {
    throw new Error(`extension test temp root must not traverse a link or junction: ${candidate}`);
  }
}

/** Return true only when child is strictly inside parent. */
function isDescendant(parent: string, child: string): boolean {
  const relative = path.relative(parent, child);
  return relative !== "" && relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);
}

/** Walk upward to the nearest existing path and require it to be a physical directory. */
function assertNearestExistingAncestor(candidate: string): void {
  let probe = candidate;
  while (!fs.existsSync(probe)) {
    const parent = path.dirname(probe);
    if (parent === probe) throw new Error(`no existing ancestor for extension test temp root: ${candidate}`);
    probe = parent;
  }
  assertPlainDirectory(probe);
}

/** Resolve, validate, create, and memoize the test root; an override must be absolute and inside AI/work. */
export function testTempRoot(): string {
  if (root) return root;
  const extensionRoot = path.resolve(process.cwd());
  const repoRoot = path.resolve(extensionRoot, "..");
  const workRoot = path.join(repoRoot, "AI", "work");
  const configured = process.env.KAIZEN_TEST_TEMP_ROOT?.trim();
  if (configured && !path.isAbsolute(configured)) throw new Error("KAIZEN_TEST_TEMP_ROOT must be absolute");
  const candidate = path.resolve(configured || path.join(workRoot, "test-temp", `extension-direct-${process.pid}`));
  rejectSystemDrive(candidate);
  if (!isDescendant(workRoot, candidate)) throw new Error(`extension test temp root escaped AI/work: ${candidate}`);
  assertNearestExistingAncestor(candidate);
  fs.mkdirSync(candidate, { recursive: true });
  assertPlainDirectory(candidate);
  root = candidate;
  ownsRoot = !configured;
  return root;
}

/** Create a unique child directory using a filename-safe prefix that starts with an alphanumeric character. */
export function makeTestTempDir(prefix: string): string {
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(prefix) || prefix === "..") throw new Error(`invalid test temp prefix: ${prefix}`);
  return fs.mkdtempSync(path.join(testTempRoot(), prefix));
}

/** Create a short physical repo root for POSIX UDS fixtures without leaving AI/work. */
export function makeSocketTestRepoRoot(): string {
  if (process.platform === "win32") return makeTestTempDir("kaizen-ext-");
  const workRoot = path.join(path.resolve(process.cwd(), ".."), "AI", "work");
  fs.mkdirSync(workRoot, { recursive: true });
  assertPlainDirectory(workRoot);
  const candidate = fs.mkdtempSync(path.join(workRoot, "k"));
  try {
    if (!isDescendant(workRoot, candidate)) throw new Error(`socket fixture escaped AI/work: ${candidate}`);
    assertPlainDirectory(candidate);
    const socketPath = path.join(candidate, "AI", "work", "orchestration", "runtime", "control.sock");
    if (Buffer.byteLength(socketPath, "utf-8") > MAX_POSIX_SOCKET_PATH_BYTES) {
      throw new Error(`socket fixture path exceeds the portable POSIX UDS limit: ${socketPath}`);
    }
  } catch (error) {
    fs.rmSync(candidate, { recursive: true, force: true });
    throw error;
  }
  ownedSocketRoots.add(candidate);
  return candidate;
}

/** Remove only an auto-owned root that remains a physical directory. */
process.once("exit", () => {
  for (const candidate of ownedSocketRoots) {
    if (!fs.existsSync(candidate)) continue;
    try {
      assertPlainDirectory(candidate);
      fs.rmSync(candidate, { recursive: true, force: true, maxRetries: 3, retryDelay: 50 });
    } catch {
      // Exit cleanup is best-effort; never turn a filesystem race into a noisy test-process failure.
    }
  }
  if (!ownsRoot || !root || !fs.existsSync(root)) return;
  try {
    assertPlainDirectory(root);
    fs.rmSync(root, { recursive: true, force: true, maxRetries: 3, retryDelay: 50 });
  } catch {
    // Exit cleanup is best-effort; never turn a filesystem race into a noisy test-process failure.
  }
});

/** Require TEMP, TMP, TMPDIR, and os.tmpdir() to resolve to the pinned root. */
export function assertAmbientTempIsPinned(): void {
  const expected = testTempRoot();
  for (const key of ["TEMP", "TMP", "TMPDIR"] as const) {
    const value = process.env[key];
    if (!value || !samePath(value, expected)) throw new Error(`${key} is not pinned to the extension test temp root`);
  }
  if (!samePath(os.tmpdir(), expected)) throw new Error("os.tmpdir() is not pinned to the extension test temp root");
}
