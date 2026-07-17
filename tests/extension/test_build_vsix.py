"""Unit tests for extension/build_vsix.py packaging — failure gates, reproducibility, provider-neutral allowlist."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
EXTENSION_ROOT = REPO_ROOT / "extension"
_SPEC = importlib.util.spec_from_file_location("kaizen_extension_build_vsix", EXTENSION_ROOT / "build_vsix.py")
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("extension build_vsix.py could not be loaded")
build_vsix = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build_vsix)


def guarded_test_temp_root() -> Path:
    """Threat-model guard: resolves/validates scratch root confined to AI/work, off the Windows system drive, physical (non-symlink/junction) ancestry; raises on escape. Warrants a docstring."""
    work_root = REPO_ROOT / "AI" / "work"
    configured = os.environ.get("KAIZEN_TEST_TEMP_ROOT", "").strip()
    if configured and not Path(configured).is_absolute():
        raise RuntimeError("KAIZEN_TEST_TEMP_ROOT must be absolute")
    root = Path(configured) if configured else REPO_ROOT / "AI" / "work" / "test-temp" / f"vsix-direct-{os.getpid()}"
    root = Path(os.path.abspath(root))
    try:
        inside_work = os.path.commonpath((str(work_root), str(root))) == str(work_root)
    except ValueError:
        inside_work = False
    if not inside_work or root == work_root:
        raise RuntimeError(f"VSIX test temp root escaped AI/work: {root}")
    if os.name == "nt":
        system_drive = os.environ.get("SystemDrive", "C:").rstrip("\\/").casefold()
        if root.drive.rstrip("\\/").casefold() == system_drive:
            raise unittest.SkipTest(f"VSIX tests must not use the Windows system drive: {root}")
    probe = root
    while not os.path.lexists(probe):
        if probe.parent == probe:
            raise RuntimeError(f"no existing ancestor for VSIX test temp root: {root}")
        probe = probe.parent
    if probe.is_symlink() or getattr(probe, "is_junction", lambda: False)() or probe.resolve(strict=True) != probe:
        raise RuntimeError(f"VSIX test temp root ancestor must be a physical directory: {probe}")
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or getattr(root, "is_junction", lambda: False)() or root.resolve(strict=True) != root:
        raise RuntimeError(f"VSIX test temp root must be a physical directory: {root}")
    return root


class BuildVsixEntrypointTest(unittest.TestCase):
    def make_root(self) -> tempfile.TemporaryDirectory[str]:
        """Thin helper: TemporaryDirectory rooted under guarded AI/work scratch; one line suffices (optional)."""
        guarded = guarded_test_temp_root()
        out = guarded / "vsix"
        out.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        if not os.environ.get("KAIZEN_TEST_TEMP_ROOT", "").strip():
            self.addCleanup(shutil.rmtree, guarded, ignore_errors=True)
        temporary = tempfile.TemporaryDirectory(prefix="vsix-test-", dir=out)
        return temporary

    def test_missing_extension_entry_fails_before_packaging(self) -> None:
        with self.make_root() as raw:
            root = Path(raw)
            (root / "package.json").write_text(json.dumps({}), encoding="utf-8")
            (root / "out" / "src").mkdir(parents=True)
            (root / "out" / "src" / "other.js").write_text("", encoding="utf-8")
            error = io.StringIO()
            with mock.patch.object(build_vsix, "HERE", root), contextlib.redirect_stderr(error):
                self.assertEqual(build_vsix.main(), 1)
            self.assertIn("out/src/extension.js is missing", error.getvalue())
            self.assertEqual(list(root.glob("*.vsix")), [])

    def test_missing_webview_entry_fails_before_packaging(self) -> None:
        with self.make_root() as raw:
            root = Path(raw)
            (root / "package.json").write_text(json.dumps({}), encoding="utf-8")
            (root / "out" / "src").mkdir(parents=True)
            (root / "out" / "src" / "extension.js").write_text("", encoding="utf-8")
            (root / "out" / "webview").mkdir(parents=True)
            error = io.StringIO()
            with mock.patch.object(build_vsix, "HERE", root), contextlib.redirect_stderr(error):
                self.assertEqual(build_vsix.main(), 1)
            self.assertIn("out/webview/main.js is missing", error.getvalue())
            self.assertEqual(list(root.glob("*.vsix")), [])

    def test_reproducible_provider_neutral_module_inventory(self) -> None:
        with self.make_root() as raw:
            root = Path(raw)
            package = {
                "name": "kaizen-test",
                "version": "0.1.0",
                "publisher": "agent-kaizen",
                "displayName": "Kaizen Test",
                "description": "test",
                "categories": ["Other"],
                "engines": {"vscode": "^1.85.0"},
            }
            (root / "package.json").write_text(json.dumps(package), encoding="utf-8")
            (root / "README.md").write_text("readme", encoding="utf-8")
            (root / "LICENSE").write_text("license", encoding="utf-8")
            (root / "media" / "webview").mkdir(parents=True)
            (root / "media" / "kaizen.svg").write_text("<svg/>", encoding="utf-8")
            (root / "media" / "webview" / "chat.html").write_text("<div/>", encoding="utf-8")
            (root / "media" / "webview" / "chat.css").write_text("", encoding="utf-8")
            (root / "out" / "src").mkdir(parents=True)
            for name in build_vsix.OUT_JS_FILES:
                (root / "out" / "src" / name).write_text("", encoding="utf-8")
            (root / "out" / "src" / "staleUnapprovedName.js").write_text("must not ship", encoding="utf-8")
            forbidden = {
                "node_modules/@anthropic-ai/claude-agent-sdk/index.js": "SHOULD_NOT_SHIP_SDK",
                "vendor_workers/claude_agent/worker.ts": "SHOULD_NOT_SHIP_WORKER_SOURCE",
                "provider-runtime/claude-agent/current.json": "SHOULD_NOT_SHIP_RUNTIME_POINTER",
                "provider-runtime/claude-agent/runtime-integrity.json": "SHOULD_NOT_SHIP_INTEGRITY",
                "cache/credential.json": "SHOULD_NOT_SHIP_PROVIDER_SECRET",
                "out/src/extension.js.map": "SHOULD_NOT_SHIP_SOURCE_MAP",
                "test/providerRuntime.test.js": "SHOULD_NOT_SHIP_TEST",
                "bin/claude.exe": "SHOULD_NOT_SHIP_NATIVE_BINARY",
            }
            for relative, marker in forbidden.items():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(marker, encoding="utf-8")
            (root / "out" / "webview").mkdir(parents=True)
            for name in build_vsix.WEBVIEW_JS_FILES:
                (root / "out" / "webview" / name).write_text("", encoding="utf-8")

            with mock.patch.object(build_vsix, "HERE", root):
                self.assertEqual(build_vsix.main(), 0)
            target = root / "kaizen-test-0.1.0.vsix"
            first = target.read_bytes()
            first_sha256 = hashlib.sha256(first).hexdigest()
            with mock.patch.object(build_vsix, "HERE", root):
                self.assertEqual(build_vsix.main(), 0)
            second = target.read_bytes()
            self.assertEqual(second, first, "identical inputs must produce byte-identical VSIX archives")
            self.assertEqual(hashlib.sha256(second).hexdigest(), first_sha256)
            with zipfile.ZipFile(target) as archive:
                names = set(archive.namelist())
                payloads = [archive.read(name) for name in names]
            for name in (
                "extension/out/src/chatPanelManager.js",
                "extension/out/src/panelState.js",
                "extension/out/src/sessionIndex.js",
                "extension/out/src/historyModel.js",
                "extension/out/src/mentionIndex.js",
                "extension/out/src/contextStager.js",
                "extension/out/src/artifactStore.js",
                "extension/out/src/imageStager.js",
                "extension/out/src/streamReducer.js",
                "extension/out/src/diffModel.js",
                "extension/out/src/diffProvider.js",
                "extension/out/src/testExtension.js",
                "extension/out/src/testExtensionCore.js",
                "extension/out/src/testExtensionTerminal.js",
                "extension/out/webview/main.js",
                "extension/out/webview/safeMarkdown.js",
            ):
                self.assertIn(name, names)
            self.assertNotIn("extension/out/src/staleUnapprovedName.js", names)
            lowered = {name.casefold() for name in names}
            for lowered_name in lowered:
                parts = Path(lowered_name).parts
                self.assertNotIn("node_modules", parts)
                self.assertNotIn("vendor_workers", parts)
                self.assertNotIn("provider-runtime", parts)
                self.assertNotIn("cache", parts)
                self.assertNotIn("test", parts)
                self.assertNotIn("tests", parts)
                self.assertFalse(lowered_name.startswith("extension/src/"))
                self.assertFalse(lowered_name.endswith((".map", ".ts")))
                self.assertNotIn("runtime-integrity.json", lowered_name)
                self.assertFalse(lowered_name.endswith("/current.json"))
                self.assertFalse(lowered_name.endswith(("/claude", "/claude.exe")))
                self.assertNotIn("credential", lowered_name)
            joined = b"\n".join(payloads)
            self.assertNotIn(b"@anthropic-ai/claude-agent-sdk", joined)
            for marker in forbidden.values():
                self.assertNotIn(marker.encode("utf-8"), joined)

    def test_actual_production_inputs_build_twice_reproducibly_and_remain_provider_neutral(self) -> None:
        production = EXTENSION_ROOT
        production_files = [
            "package.json", "README.md", "LICENSE", "media/kaizen.svg",
            "media/webview/chat.html", "media/webview/chat.css",
            *(f"out/src/{name}" for name in build_vsix.OUT_JS_FILES),
            *(f"out/webview/{name}" for name in build_vsix.WEBVIEW_JS_FILES),
        ]
        with self.make_root() as raw:
            root = Path(raw)
            for relative in production_files:
                source = production / relative
                self.assertTrue(source.is_file(), f"compiled production input is missing: {relative}")
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)

            package = json.loads((root / "package.json").read_text(encoding="utf-8"))
            self.assertFalse(package.get("dependencies"), "the production VSIX must not declare runtime dependencies")
            with mock.patch.object(build_vsix, "HERE", root):
                self.assertEqual(build_vsix.main(), 0)
            target = root / f"{package['name']}-{package['version']}.vsix"
            first = target.read_bytes()
            first_sha256 = hashlib.sha256(first).hexdigest()
            with mock.patch.object(build_vsix, "HERE", root):
                self.assertEqual(build_vsix.main(), 0)
            second = target.read_bytes()
            self.assertEqual(second, first, "actual production inputs must produce byte-identical VSIX archives")
            self.assertEqual(hashlib.sha256(second).hexdigest(), first_sha256)

            expected_names = {
                "[Content_Types].xml", "extension.vsixmanifest", "extension/package.json",
                "extension/README.md", "extension/LICENSE.txt", "extension/media/kaizen.svg",
                "extension/media/webview/chat.html", "extension/media/webview/chat.css",
                *(f"extension/out/src/{name}" for name in build_vsix.OUT_JS_FILES),
                *(f"extension/out/webview/{name}" for name in build_vsix.WEBVIEW_JS_FILES),
            }
            with zipfile.ZipFile(target) as archive:
                names = set(archive.namelist())
                payload = b"\n".join(archive.read(name) for name in sorted(names))
            self.assertEqual(names, expected_names, "the production package inventory must equal the allowlist")
            for name in {item.casefold() for item in names}:
                parts = Path(name).parts
                self.assertFalse({"node_modules", "vendor_workers", "provider-runtime", "cache", "test", "tests"} & set(parts))
                self.assertFalse(name.startswith("extension/src/"))
                self.assertFalse(name.endswith((".map", ".ts", ".exe", ".dll", ".node")))
                self.assertFalse(name.endswith("/current.json"))
            for needle in (
                b"@anthropic-ai/claude-agent-sdk",
                b"vendor_workers/claude_agent",
                b"provider-runtime/claude-agent",
                b"runtime-integrity.json",
                b"credential.json",
                b"-----BEGIN PRIVATE KEY-----",
            ):
                self.assertNotIn(needle, payload)


if __name__ == "__main__":
    unittest.main()
