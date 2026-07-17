/** Resolve the shared interpreter used by the ESM extension-test runner and direct temp-root tests. */
const fs = require("node:fs");
const path = require("node:path");

/** Apply override, shared-venv, then PATH fallback precedence for an absolute repository root. */
function sharedPython({ env = process.env, platform = process.platform, repoRoot }) {
  const configured = env.KAIZEN_PYTHON?.trim() || env.PYTHON?.trim();
  if (configured) return configured;
  const configuredDevRoot = env.DEVROOT?.trim();
  const devRoot = configuredDevRoot ? path.resolve(configuredDevRoot) : path.dirname(path.resolve(repoRoot));
  const candidate =
    platform === "win32"
      ? path.join(devRoot, "Python", "venvs", "kaizen", "Scripts", "python.exe")
      : path.join(devRoot, "Python", "venvs", "kaizen", "bin", "python");
  try {
    if (fs.statSync(candidate).isFile()) return candidate;
  } catch {
    // Missing or inaccessible shared interpreter falls through to PATH.
  }
  return platform === "win32" ? "python.exe" : "python3";
}

module.exports = { sharedPython };
