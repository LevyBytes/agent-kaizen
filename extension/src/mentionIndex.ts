/** Git-first 4-MiB-bounded mention index; fallback ignore fidelity is caller-defined. */

import { spawn } from "node:child_process";

const RESULT_LIMIT = 50;
const OUTPUT_LIMIT = 4 * 1024 * 1024;

export type MentionFallback = () => Promise<string[]>;
export type GitListRunner = (cwd: string) => Promise<Buffer>;

/** Lazily load and cache mention paths until invalidate() clears the cache. */
export class MentionIndex {
  private paths: string[] | undefined;
  private loading: Promise<string[]> | undefined;

  constructor(
    private readonly workspaceRoot: string,
    private readonly fallback: MentionFallback,
    private readonly gitList: GitListRunner = runGitList,
  ) {}

  /** Return up to 50 case-insensitive substring matches; blank queries return the first 50. */
  async search(query = ""): Promise<string[]> {
    const paths = await this.load();
    const needle = query.trim().toLowerCase();
    const matches = needle ? paths.filter((candidate) => candidate.toLowerCase().includes(needle)) : paths;
    return matches.slice(0, RESULT_LIMIT);
  }

  /** Clear cached and in-flight state so the next search re-lists files. */
  invalidate(): void {
    this.paths = undefined;
    this.loading = undefined;
  }

  /** Load with Git first and cache it; return an uncached fallback after transient Git errors. */
  private async load(): Promise<string[]> {
    if (this.paths) return this.paths;
    if (this.loading) return this.loading;
    this.loading = (async () => {
      let candidates: string[];
      try {
        candidates = parseGitLsFiles(await this.gitList(this.workspaceRoot));
      } catch {
        candidates = (await this.fallback()).map(normalizeRelativePath).filter((value): value is string => !!value);
        return [...new Set(candidates)].sort((left, right) => left.localeCompare(right));
      }
      this.paths = [...new Set(candidates)].sort((left, right) => left.localeCompare(right));
      return this.paths;
    })();
    try {
      return await this.loading;
    } finally {
      this.loading = undefined;
    }
  }
}

/** Parse normalized NUL-delimited Git records, rejecting overflow or an incomplete tail. */
export function parseGitLsFiles(bytes: Buffer): string[] {
  if (bytes.length > OUTPUT_LIMIT) throw new Error("git ls-files output exceeded 4 MiB");
  const paths: string[] = [];
  let start = 0;
  for (let index = 0; index < bytes.length; index += 1) {
    if (bytes[index] !== 0) continue;
    if (index > start) {
      const raw = bytes.subarray(start, index);
      const decoded = raw.toString("utf8");
      const normalized = Buffer.from(decoded, "utf8").equals(raw) ? normalizeRelativePath(decoded) : undefined;
      if (normalized) paths.push(normalized);
    }
    start = index + 1;
  }
  // `-z` must terminate every record. A partial final byte sequence is transport corruption.
  if (start !== bytes.length) throw new Error("git ls-files returned a non-NUL-terminated record");
  return paths;
}

/** Normalize a file record and reject absolute, drive, traversal, or empty components. */
export function normalizeRelativePath(value: string): string | undefined {
  const normalized = value.replace(/\\/g, "/").replace(/^\.\/+/, "");
  if (!normalized || normalized.startsWith("/") || /^[A-Za-z]:\//.test(normalized)) return undefined;
  if (normalized.split("/").some((part) => part === "" || part === "." || part === "..")) return undefined;
  return normalized;
}

/** Spawn bounded, shell-free `git ls-files -co --exclude-standard -z`. */
function runGitList(cwd: string): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const child = spawn("git", ["ls-files", "-co", "--exclude-standard", "-z"], {
      cwd,
      shell: false,
      windowsHide: true,
      stdio: ["ignore", "pipe", "ignore"],
    });
    const chunks: Buffer[] = [];
    let length = 0;
    let settled = false;
    child.stdout.on("data", (chunk: Buffer) => {
      if (settled) return;
      length += chunk.length;
      if (length > OUTPUT_LIMIT) {
        settled = true;
        child.kill();
        child.stdout.destroy();
        reject(new Error("git ls-files output exceeded 4 MiB"));
        return;
      }
      chunks.push(chunk);
    });
    child.stdout.once("error", (error) => {
      if (!settled) {
        settled = true;
        reject(error);
      }
    });
    child.once("error", (error) => {
      if (!settled) {
        settled = true;
        reject(error);
      }
    });
    child.once("close", (code, signal) => {
      if (settled) return;
      settled = true;
      if (code !== 0) reject(new Error(
        signal ? `git ls-files terminated by ${signal}` : `git ls-files failed with exit code ${code ?? "unknown"}`,
      ));
      else resolve(Buffer.concat(chunks, length));
    });
  });
}
