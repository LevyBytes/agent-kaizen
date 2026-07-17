"""Offline unit + compiled-worker integration tests for the Claude runtime installer, source/lock integrity gates, and the vendored TS worker stdout protocol; Windows-first, AI/work-rooted fixtures; audited-artifact pinning contract."""
from __future__ import annotations

import json
import hashlib
import base64
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from kaizen_components.orchestration import claude_runtime as runtime_module
from kaizen_components.orchestration.claude_worker_protocol import (
    CAPABILITY_PROBE_FEATURES,
    validate_capability_probe_result,
)
from kaizen_components.orchestration.claude_runtime import (
    ClaudeRuntimeError,
    NODE_TYPES_VERSION,
    SDK_PACKAGE,
    SDK_VERSION,
    ZOD_VERSION,
    cleanup_abandoned_partials,
    install_runtime,
    resolve_worker_command,
    runtime_capability,
    validate_runtime,
    _npm_environment,
)
from kaizen_components.orchestration import tool_gateway


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "kaizen_components" / "orchestration" / "vendor_workers" / "claude_agent"
WORK_ROOT = REPO_ROOT / "AI" / "work"

# Exact artifact set whose native implementation was statically audited for the
# per-submitted-SDKUserMessage ``maxTurns`` reset contract.  Dependency updates
# must fail this gate until the replacement runtime receives the same audit.
AUDITED_SDK_VERSION = "0.3.207"
AUDITED_PACKAGE_LOCK_SHA256 = "d62fbfafec4a4d7b3697f077ed993542282317343c333720b2b973485f2aa3c1"
AUDITED_SDK_TARBALL_INTEGRITY = (
    "sha512-y0PkQRmQBi96MHiN5Xzfq+GaddxCZCqI/cXEQBLYBLXGa4i1nDSlulQqkMBj2Ror"
    "rrSGQJ6Wdw+uhu6OfHNPzA=="
)
AUDITED_WINDOWS_NATIVE_PACKAGE = "@anthropic-ai/claude-agent-sdk-win32-x64"
AUDITED_WINDOWS_NATIVE_TARBALL_INTEGRITY = (
    "sha512-YPjVT0q6aXEM2MgN4CI6/9fqiTXwETji+4NoPOzCYuqAkhXZqp30Jsk7/NHqYGNN"
    "SfURKrsuAoliKB0rsbpbjg=="
)
AUDITED_WINDOWS_NATIVE_RUNTIME_VERSION = b"2.1.207"
AUDITED_WINDOWS_NATIVE_SHA256 = "781fdc2c89868b1cb05cc22c253ef142a0b44e7cc36236aecd6335745c7d42d0"


def hash_and_find(path: Path, needle: bytes) -> tuple[str, bool]:
    """Stream path in 1 MiB chunks and retain only the overlap needed to find a boundary-spanning needle."""
    digest = hashlib.sha256()
    found, overlap = False, b""
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            window = overlap + chunk
            found = found or needle in window
            tail = len(needle) - 1
            overlap = window[-tail:] if tail > 0 else b""
    return digest.hexdigest(), found


def expected_source_identity(source: Path) -> str:
    """Recomputes the deterministic source-identity digest over the 5 tracked files with the source-v1 domain separator; must mirror the installer digest."""
    digest = hashlib.sha256(b"agent-kaizen.claude-runtime.source-v1\0")
    for name in ("package.json", "package-lock.json", "runtime-manifest.json", "tsconfig.json", "worker.ts"):
        digest.update(name.encode("utf-8") + b"\0" + bytes.fromhex(hashlib.sha256(
            (source / name).read_bytes(),
        ).hexdigest()))
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def package_lock(*, marker: str = "a", sdk_version: str = SDK_VERSION) -> dict[str, object]:
    """Builds a lockfileVersion-3 fixture mirroring the audited dependency/native set; marker forces cold-target identity change; sdk_version drives fail-closed cases."""
    integrity = "sha512-" + base64.b64encode(bytes(64)).decode("ascii")

    def package_entry(name: str, version: str, *, optional: bool = False) -> dict[str, object]:
        leaf = name.rsplit("/", 1)[-1]
        entry: dict[str, object] = {
            "version": version,
            "resolved": f"https://registry.npmjs.org/{name}/-/{leaf}-{version}.tgz",
            "integrity": integrity,
        }
        if optional:
            entry["optional"] = True
        return entry

    packages: dict[str, object] = {
        "": {
            "name": "@agent-kaizen/claude-agent-worker",
            "version": "0.1.0",
            "dependencies": {SDK_PACKAGE: sdk_version, "zod": ZOD_VERSION},
            "devDependencies": {"@types/node": NODE_TYPES_VERSION, "typescript": "5.9.3"},
        },
        "node_modules/@anthropic-ai/claude-agent-sdk": package_entry(SDK_PACKAGE, sdk_version),
        "node_modules/zod": package_entry("zod", ZOD_VERSION),
        "node_modules/typescript": package_entry("typescript", "5.9.3"),
        "node_modules/@types/node": package_entry("@types/node", NODE_TYPES_VERSION),
    }
    for native_package in runtime_module._NATIVE_PACKAGES:
        packages[f"node_modules/{native_package}"] = package_entry(
            native_package, sdk_version, optional=True,
        )
    return {
        "name": "@agent-kaizen/claude-agent-worker",
        "version": "0.1.0",
        "lockfileVersion": 3,
        "requires": True,
        "marker": marker,
        "packages": packages,
    }


class FakeNpmRunner:
    """Deterministic stand-in for npm ci/tsc/prune, materializing node_modules + dist fixtures; native=False simulates the missing win32-x64 binary."""
    def __init__(self, *, native: bool = True) -> None:
        self.calls: list[tuple[list[str], Path, dict[str, str]]] = []
        self.native = native

    def __call__(self, argv, *, cwd, env, **_kwargs):
        args, root, child_env = list(argv), Path(cwd), dict(env)
        self.calls.append((args, root, child_env))
        if len(args) > 1 and args[1] == "ci":
            write_json(root / "node_modules/@anthropic-ai/claude-agent-sdk/package.json",
                       {"name": SDK_PACKAGE, "version": SDK_VERSION, "type": "module"})
            (root / "node_modules/@anthropic-ai/claude-agent-sdk/index.js").write_text(
                "export const query = true;\n", encoding="utf-8")
            write_json(root / "node_modules/zod/package.json", {"name": "zod", "version": ZOD_VERSION})
            compiler = root / "node_modules/typescript/bin/tsc"
            compiler.parent.mkdir(parents=True, exist_ok=True)
            compiler.write_text("compiler", encoding="utf-8")
            write_json(root / "node_modules/typescript/package.json", {
                "name": "typescript", "version": "5.9.3",
            })
            write_json(root / "node_modules/@types/node/package.json", {
                "name": "@types/node", "version": NODE_TYPES_VERSION,
            })
            if self.native:
                native = root / "node_modules/@anthropic-ai/claude-agent-sdk-win32-x64/claude.exe"
                native.parent.mkdir(parents=True, exist_ok=True)
                native.write_bytes(b"native-fixture")
                write_json(native.parent / "package.json", {
                    "name": "@anthropic-ai/claude-agent-sdk-win32-x64", "version": SDK_VERSION,
                })
        elif len(args) > 1 and Path(args[1]).name == "tsc":
            worker = root / "dist/worker.js"
            worker.parent.mkdir(parents=True, exist_ok=True)
            worker.write_text("export const workerFixture = true;\n", encoding="utf-8")
        elif len(args) > 1 and args[1] == "prune":
            shutil.rmtree(root / "node_modules/typescript", ignore_errors=True)
            shutil.rmtree(root / "node_modules/@types/node", ignore_errors=True)
        return subprocess.CompletedProcess(args, 0, "", "")


class RuntimeFixture(unittest.TestCase):
    def setUp(self) -> None:
        WORK_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="claude-runtime-test-", dir=WORK_ROOT)
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.source = self.root / "source"
        self.source.mkdir()
        for name in ("runtime-manifest.json", "package.json", "tsconfig.json", "worker.ts"):
            shutil.copy2(SOURCE_ROOT / name, self.source / name)
        write_json(self.source / "package-lock.json", package_lock())
        self.node = self.root / "node.exe"
        self.npm = self.root / "npm.cmd"
        self.node.write_bytes(b"node-fixture")
        self.npm.write_bytes(b"npm-fixture")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def install(self, runner: FakeNpmRunner | None = None, *, source: Path | None = None):
        fake = runner or FakeNpmRunner()
        result = install_runtime(
            self.runtime,
            node_executable=self.node,
            npm_executable=self.npm,
            source_root=source or self.source,
            runner=fake,
            system="win32",
            machine="AMD64",
        )
        return result, fake


class ClaudeRuntimeInstallTest(RuntimeFixture):
    def test_fresh_install_validates_and_warm_rerun_executes_no_commands(self) -> None:
        result, runner = self.install()
        self.assertEqual(result["status"], "ready")
        self.assertFalse(result["warm"])
        self.assertEqual([call[0][1] for call in runner.calls if len(call[0]) > 1 and call[0][0] == str(self.npm)],
                         ["ci", "prune"])
        first_call_count = len(runner.calls)
        warm = install_runtime(
            self.runtime,
            node_executable=self.node,
            npm_executable=self.npm,
            source_root=self.source,
            runner=runner,
            system="win32",
            machine="AMD64",
        )
        self.assertTrue(warm["warm"])
        self.assertEqual(len(runner.calls), first_call_count)
        validated = validate_runtime(self.runtime)
        self.assertFalse((validated["target"] / "node_modules/typescript").exists())
        self.assertFalse((validated["target"] / "node_modules/@types/node").exists())
        self.assertEqual(resolve_worker_command(self.runtime), [str(self.node.resolve()),
                                                               str(validated["target"] / "dist/worker.js")])

    def test_worker_source_change_creates_new_cold_target_then_reuses_it_warm(self) -> None:
        first, _ = self.install()
        first_name = str(first["runtime"])
        with (self.source / "worker.ts").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write("\n// source identity regression\n")

        second, second_runner = self.install(FakeNpmRunner())

        self.assertFalse(second["warm"])
        self.assertNotEqual(second["runtime"], first_name)
        self.assertEqual(second["runtime"], f"{expected_source_identity(self.source)}-win32-x64")
        self.assertGreater(len(second_runner.calls), 0)
        pointer = json.loads((self.runtime / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer, {
            "schema": 1, "active": second["runtime"], "previous": first_name,
        })

        call_count = len(second_runner.calls)
        third = install_runtime(
            self.runtime,
            node_executable=self.node,
            npm_executable=self.npm,
            source_root=self.source,
            runner=second_runner,
            system="win32",
            machine="AMD64",
        )
        self.assertTrue(third["warm"])
        self.assertEqual(len(second_runner.calls), call_count)

    def test_installed_worker_source_tamper_is_refused(self) -> None:
        self.install()
        target = validate_runtime(self.runtime)["target"]
        with (target / "worker.ts").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write("\n// tamper\n")
        with self.assertRaises(ClaudeRuntimeError) as raised:
            validate_runtime(self.runtime)
        self.assertEqual(raised.exception.field, "runtime_integrity")

    def test_authoritative_source_drift_is_refused_until_reinstalled(self) -> None:
        self.install()
        with (self.source / "worker.ts").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write("\n// not deployed yet\n")
        with self.assertRaises(ClaudeRuntimeError) as raised:
            validate_runtime(self.runtime, source_root=self.source)
        self.assertEqual(raised.exception.field, "runtime_integrity")

    def test_install_routes_npm_cache_config_temp_and_never_runs_lifecycle_scripts(self) -> None:
        _, runner = self.install()
        ci, _, env = runner.calls[0]
        self.assertIn("--ignore-scripts", ci)
        self.assertIn("--registry=https://registry.npmjs.org/", ci)
        self.assertIn("--replace-registry-host=never", ci)
        self.assertEqual(Path(env["PATH"].split(os.pathsep)[0]).resolve(), self.node.parent.resolve())
        self.assertEqual(env["NPM_CONFIG_REGISTRY"], "https://registry.npmjs.org/")
        self.assertEqual(env["NPM_CONFIG_REPLACE_REGISTRY_HOST"], "never")
        self.assertEqual(env["NPM_CONFIG_STRICT_SSL"], "true")
        self.assertEqual(env["NPM_CONFIG_IGNORE_SCRIPTS"], "true")
        for key in ("TEMP", "TMP", "TMPDIR", "NPM_CONFIG_CACHE", "NPM_CONFIG_USERCONFIG",
                    "NPM_CONFIG_GLOBALCONFIG", "NPM_CONFIG_PREFIX", "NODE_REPL_HISTORY"):
            self.assertTrue(Path(env[key]).resolve().is_relative_to(self.runtime.resolve()), (key, env[key]))
        prune = next(args for args, _, _ in runner.calls if len(args) > 1 and args[1] == "prune")
        self.assertIn("--ignore-scripts", prune)

    def test_install_path_has_one_canonical_key_and_never_adds_current_directory(self) -> None:
        with mock.patch.dict(os.environ, {"Path": ""}, clear=True):
            _, runner = self.install()
        child_env = runner.calls[0][2]
        self.assertEqual([key for key in child_env if key.casefold() == "path"], ["PATH"])
        self.assertEqual(child_env["PATH"], str(self.node.parent))

    def test_runtime_child_environment_strips_credentials_tokens_and_node_injection(self) -> None:
        inherited = {
            "Path": "D:\\tools",
            "MiXeD_Session_Token": "secret",
            "provider_CREDENTIALS_file": "secret-file",
            "NODE_OPTIONS": "--require=attacker.js",
            "node_path": "D:\\attacker",
            "OTEL_EXPORTER_OTLP_HEADERS": "authorization=secret",
            "NPM_CONFIG_REGISTRY": "https://attacker.invalid/",
            "npm_config_proxy": "https://attacker.invalid/",
            "HTTPS_PROXY": "https://attacker.invalid/",
            "NODE_EXTRA_CA_CERTS": "D:\\attacker-ca.pem",
        }
        with mock.patch.dict(os.environ, inherited, clear=True):
            env = _npm_environment(self.runtime)
        normalized = {key.casefold(): value for key, value in env.items()}
        self.assertEqual(normalized["path"], "D:\\tools")
        self.assertEqual([key for key in env if key.casefold() == "path"], ["PATH"])
        self.assertEqual(normalized["npm_config_registry"], "https://registry.npmjs.org/")
        self.assertNotEqual(normalized["npm_config_registry"], inherited["NPM_CONFIG_REGISTRY"])
        for key in inherited:
            if key not in {"Path", "NPM_CONFIG_REGISTRY"}:
                self.assertNotIn(key.casefold(), normalized)

    def test_invalid_staged_integrity_is_not_published(self) -> None:
        real_write = runtime_module._write_json_atomic

        def write_then_tamper(path: Path, value: dict[str, object]) -> None:
            real_write(path, value)
            if path.name == runtime_module.INTEGRITY_FILE:
                (path.parent / "dist/worker.js").write_text("tampered", encoding="utf-8")

        with mock.patch.object(runtime_module, "_write_json_atomic", side_effect=write_then_tamper):
            with self.assertRaises(ClaudeRuntimeError) as raised:
                self.install()
        self.assertEqual(raised.exception.field, "runtime_integrity")
        self.assertFalse((self.runtime / "current.json").exists())
        published = [
            entry for entry in self.runtime.iterdir()
            if entry.is_dir() and not entry.name.startswith(".") and entry.name != "_npm"
        ]
        self.assertEqual(published, [])

    def test_missing_native_and_wrong_lock_fail_closed_without_pointer(self) -> None:
        with self.assertRaises(ClaudeRuntimeError) as missing_native:
            self.install(FakeNpmRunner(native=False))
        self.assertEqual(missing_native.exception.field, "native_binary")
        self.assertFalse((self.runtime / "current.json").exists())

        other = self.root / "bad-source"
        shutil.copytree(self.source, other)
        write_json(other / "package-lock.json", package_lock(sdk_version="0.3.206"))
        calls = FakeNpmRunner()
        with self.assertRaises(ClaudeRuntimeError) as wrong_lock:
            self.install(calls, source=other)
        self.assertEqual(wrong_lock.exception.field, "package_lock.dependencies")
        self.assertEqual(calls.calls, [])

    def test_lock_sources_integrity_root_and_all_native_packages_fail_closed_before_npm(self) -> None:
        cases: list[tuple[str, str, object]] = []

        def mutate_entry(field: str, value: object) -> object:
            def mutate(lock: dict[str, object]) -> None:
                packages = lock["packages"]
                assert isinstance(packages, dict)
                zod = packages["node_modules/zod"]
                assert isinstance(zod, dict)
                if value is None:
                    zod.pop(field, None)
                else:
                    zod[field] = value
            return mutate

        cases.extend((
            ("foreign-host", "package_lock.source", mutate_entry(
                "resolved", "https://attacker.invalid/zod.tgz",
            )),
            ("local-file", "package_lock.source", mutate_entry("resolved", "file:../zod")),
            ("query-bearing-url", "package_lock.source", mutate_entry(
                "resolved", "https://registry.npmjs.org/zod/-/zod-4.4.3.tgz?token=secret",
            )),
            ("link", "package_lock.source", mutate_entry("link", True)),
            ("missing-integrity", "package_lock.integrity", mutate_entry("integrity", None)),
            ("weak-integrity", "package_lock.integrity", mutate_entry("integrity", "sha1-Zm9v")),
            ("short-sha512", "package_lock.integrity", mutate_entry("integrity", "sha512-Zm9v")),
            ("invalid-sha512", "package_lock.integrity", mutate_entry("integrity", "sha512-***")),
        ))

        def drift_root(lock: dict[str, object]) -> None:
            packages = lock["packages"]
            assert isinstance(packages, dict)
            root = packages[""]
            assert isinstance(root, dict)
            root["version"] = "9.9.9"

        def drift_top_level_root(lock: dict[str, object]) -> None:
            lock["name"] = "attacker-worker"

        def downgrade_lock(lock: dict[str, object]) -> None:
            lock["lockfileVersion"] = 2

        def remove_native(lock: dict[str, object]) -> None:
            packages = lock["packages"]
            assert isinstance(packages, dict)
            packages.pop(f"node_modules/{runtime_module._NATIVE_PACKAGES[0]}")

        def make_native_required(lock: dict[str, object]) -> None:
            packages = lock["packages"]
            assert isinstance(packages, dict)
            native = packages[f"node_modules/{runtime_module._NATIVE_PACKAGES[0]}"]
            assert isinstance(native, dict)
            native.pop("optional")

        cases.extend((
            ("root-drift", "package_lock.root", drift_root),
            ("top-level-root-drift", "package_lock.root", drift_top_level_root),
            ("lockfile-v2", "package_lock.version", downgrade_lock),
            ("missing-native", "package_lock.native_packages", remove_native),
            ("nonoptional-native", "package_lock.native_packages", make_native_required),
        ))

        for name, expected_field, mutate in cases:
            with self.subTest(name=name):
                source = self.root / f"bad-lock-{name}"
                shutil.copytree(self.source, source)
                lock = package_lock()
                assert callable(mutate)
                mutate(lock)
                write_json(source / "package-lock.json", lock)
                runner = FakeNpmRunner()
                with self.assertRaises(ClaudeRuntimeError) as raised:
                    self.install(runner, source=source)
                self.assertEqual(raised.exception.field, expected_field)
                self.assertEqual(runner.calls, [])

    def test_tamper_is_refused_and_capability_never_exposes_paths(self) -> None:
        self.install()
        ready = runtime_capability(self.runtime)
        self.assertEqual(ready, {
            "runtime_kind": "claude-agent-sdk",
            "runtime_version": SDK_VERSION,
            "runtime_status": "ready",
            "worker_protocol": 1,
        })
        worker = Path(resolve_worker_command(self.runtime)[1])
        worker.write_text("tampered", encoding="utf-8")
        with self.assertRaises(ClaudeRuntimeError):
            validate_runtime(self.runtime)
        unavailable = runtime_capability(self.runtime)
        self.assertEqual(unavailable["runtime_status"], "unavailable")
        self.assertNotIn(str(self.runtime), json.dumps(unavailable))

    def test_installed_dependency_tree_tamper_is_refused(self) -> None:
        self.install()
        target = validate_runtime(self.runtime)["target"]
        (target / "node_modules/@anthropic-ai/claude-agent-sdk/index.js").write_text(
            "export const query = false;\n", encoding="utf-8")
        with self.assertRaises(ClaudeRuntimeError) as raised:
            validate_runtime(self.runtime)
        self.assertEqual(raised.exception.field, "installed_packages")

    def test_stale_pointer_is_repaired_and_previous_runtime_is_retained(self) -> None:
        first, _ = self.install()
        first_name = str(first["runtime"])
        write_json(self.source / "package-lock.json", package_lock(marker="second"))
        second, _ = self.install(FakeNpmRunner())
        pointer = json.loads((self.runtime / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer["active"], second["runtime"])
        self.assertEqual(pointer["previous"], first_name)
        self.assertTrue((self.runtime / first_name).is_dir())
        pointer["active"] = f"{'0' * 64}-win32-x64"
        write_json(self.runtime / "current.json", pointer)
        repaired, runner = self.install(FakeNpmRunner())
        self.assertTrue(repaired["warm"])
        self.assertEqual(runner.calls, [])

        warm_again, runner = self.install(FakeNpmRunner())
        self.assertTrue(warm_again["warm"])
        self.assertEqual(runner.calls, [])
        preserved = json.loads((self.runtime / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(preserved["previous"], first_name)

    def test_failed_stage_does_not_block_same_process_retry(self) -> None:
        with self.assertRaises(ClaudeRuntimeError) as raised:
            self.install(FakeNpmRunner(native=False))
        self.assertEqual(raised.exception.field, "native_binary")
        self.assertEqual(sorted(self.runtime.glob(f".partial-{os.getpid()}-*")), [])

        repaired, runner = self.install(FakeNpmRunner())
        self.assertFalse(repaired["warm"])
        self.assertGreater(len(runner.calls), 0)
        self.assertEqual(validate_runtime(self.runtime)["pointer"]["active"], repaired["runtime"])

    def test_corrupt_active_recovers_previous_across_failed_then_successful_repair(self) -> None:
        first, _ = self.install()
        first_name = str(first["runtime"])
        write_json(self.source / "package-lock.json", package_lock(marker="second"))
        second, _ = self.install(FakeNpmRunner())
        second_name = str(second["runtime"])
        second_target = validate_runtime(self.runtime)["target"]
        (second_target / "dist/worker.js").write_text("tampered", encoding="utf-8")

        with self.assertRaises(ClaudeRuntimeError) as failed_repair:
            self.install(FakeNpmRunner(native=False))
        self.assertEqual(failed_repair.exception.field, "native_binary")
        recovered = validate_runtime(self.runtime)
        self.assertEqual(recovered["pointer"], {"schema": 1, "active": first_name, "previous": None})
        self.assertEqual(recovered["target"].name, first_name)
        self.assertFalse((self.runtime / second_name).exists())
        self.assertEqual(len(list(self.runtime.glob(".partial-corrupt-*"))), 1)

        repaired, _ = self.install(FakeNpmRunner())
        self.assertFalse(repaired["warm"])
        pointer = validate_runtime(self.runtime)["pointer"]
        self.assertEqual(pointer["active"], second_name)
        self.assertEqual(pointer["previous"], first_name)

    def test_malformed_pointer_cannot_quarantine_unrelated_runtime_child(self) -> None:
        installed, _ = self.install()
        unrelated = self.runtime / "unrelated-owned-by-user"
        unrelated.mkdir()
        sentinel = unrelated / "sentinel.txt"
        sentinel.write_text("preserve", encoding="utf-8")
        write_json(self.runtime / "current.json", {
            "schema": 1,
            "active": unrelated.name,
            "previous": installed["runtime"],
        })

        repaired, runner = self.install(FakeNpmRunner())
        self.assertTrue(repaired["warm"])
        self.assertEqual(runner.calls, [])
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")
        self.assertEqual(list(self.runtime.glob(".partial-corrupt-*")), [])

    @unittest.skipIf(os.name == "nt", "creating a Windows reparse point requires host privilege")
    def test_installer_lock_rejects_symlink_without_touching_target(self) -> None:
        self.runtime.mkdir()
        external = self.root / "external-lock-target"
        external.write_bytes(b"external")
        (self.runtime / ".install.lock").symlink_to(external)
        with self.assertRaises(ClaudeRuntimeError) as raised:
            with runtime_module._installer_lock(self.runtime, timeout=0.1):
                self.fail("reparse lock unexpectedly accepted")
        self.assertEqual(raised.exception.field, "runtime_lock")
        self.assertEqual(external.read_bytes(), b"external")

    def test_concurrent_installs_serialize_and_second_reuses_warm_runtime(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        second_attempted_lock = threading.Event()

        class BlockingRunner(FakeNpmRunner):
            def __call__(self, argv, *, cwd, env, **kwargs):
                if len(argv) > 1 and argv[1] == "ci":
                    entered.set()
                    if not release.wait(timeout=5):
                        return subprocess.CompletedProcess(list(argv), 1, "", "")
                return super().__call__(argv, cwd=cwd, env=env, **kwargs)

        first_runner = BlockingRunner()
        second_runner = FakeNpmRunner()
        results: queue.Queue[object] = queue.Queue()
        original_installer_lock = runtime_module._installer_lock

        @contextmanager
        def observed_installer_lock(*args, **kwargs):
            if threading.current_thread().name == "second-install":
                second_attempted_lock.set()
            with original_installer_lock(*args, **kwargs):
                yield

        def install(runner: FakeNpmRunner) -> None:
            try:
                results.put(self.install(runner)[0])
            except BaseException as error:  # pragma: no cover - assertion reports the captured error
                results.put(error)

        first_thread = threading.Thread(target=install, args=(first_runner,))
        second_thread = threading.Thread(target=install, args=(second_runner,), name="second-install")
        with mock.patch.object(runtime_module, "_installer_lock", observed_installer_lock):
            first_thread.start()
            self.assertTrue(entered.wait(timeout=5))
            second_thread.start()
            self.assertTrue(second_attempted_lock.wait(timeout=5), "second installer never attempted the runtime lock")
            self.assertTrue(second_thread.is_alive(), "second installer did not wait for the held runtime lock")
            release.set()
            first_thread.join(timeout=5)
            second_thread.join(timeout=5)
        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        outcomes = [results.get_nowait(), results.get_nowait()]
        errors = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
        self.assertEqual(errors, [])
        self.assertEqual(sorted(bool(outcome["warm"]) for outcome in outcomes), [False, True])
        self.assertEqual(second_runner.calls, [])

    def test_runtime_lock_contends_across_processes(self) -> None:
        self.runtime.mkdir()
        ready = self.root / "child-lock-ready"
        script = (
            "import sys,time\n"
            "from pathlib import Path\n"
            "from kaizen_components.orchestration.claude_runtime import _installer_lock\n"
            "root,ready=Path(sys.argv[1]),Path(sys.argv[2])\n"
            "with _installer_lock(root, timeout=5):\n"
            " ready.write_text('ready',encoding='utf-8')\n"
            " time.sleep(1)\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(self.runtime), str(ready)],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 5
            while not ready.is_file() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            if not ready.is_file():
                stdout, stderr = child.communicate(timeout=1)
                self.fail(f"child lock was not acquired: {(stdout, stderr)}")
            with self.assertRaises(ClaudeRuntimeError) as raised:
                with runtime_module._installer_lock(self.runtime, timeout=0.1):
                    self.fail("cross-process lock unexpectedly acquired")
            self.assertEqual(raised.exception.field, "runtime_lock")
            stdout, stderr = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, (stdout, stderr))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

    def test_only_old_direct_partial_stages_are_cleaned(self) -> None:
        self.runtime.mkdir()
        old = self.runtime / ".partial-1"
        fresh = self.runtime / ".partial-2"
        unrelated = self.runtime / "keep"
        for path in (old, fresh, unrelated):
            path.mkdir()
        now = time.time()
        os.utime(old, (now - 90_000, now - 90_000))
        removed = cleanup_abandoned_partials(self.runtime, now=now)
        self.assertEqual(removed, [".partial-1"])
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(unrelated.exists())

    def test_repo_root_resolves_default_runtime_location(self) -> None:
        fake_repo = self.root / "repo"
        (fake_repo / "AI").mkdir(parents=True)
        (fake_repo / "kaizen.py").write_text("", encoding="utf-8")
        runtime = fake_repo / "AI/work/orchestration/provider-runtime/claude-agent"
        install_runtime(fake_repo, node_executable=self.node, npm_executable=self.npm,
                        source_root=self.source, runner=FakeNpmRunner(), system="win32", machine="AMD64")
        self.assertEqual(runtime_capability(fake_repo)["runtime_status"], "ready")
        self.assertTrue((runtime / "current.json").is_file())


class ClaudeWorkerSourceContractTest(unittest.TestCase):
    @staticmethod
    def _worker_constant(worker: str, name: str) -> int:
        match = re.search(rf"^const {re.escape(name)} = ([0-9_]+);$", worker, re.MULTILINE)
        if match is None:
            raise AssertionError(f"missing numeric worker constant: {name}")
        return int(match.group(1).replace("_", ""))

    def test_exact_packages_and_fail_closed_worker_configuration_are_source_locked(self) -> None:
        package = json.loads((SOURCE_ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["dependencies"], {SDK_PACKAGE: SDK_VERSION, "zod": ZOD_VERSION})
        worker = (SOURCE_ROOT / "worker.ts").read_text(encoding="utf-8")
        for fragment in (
            "prompt: this.input",
            "persistSession: false",
            "settingSources: []",
            "tools: []",
            "skills: []",
            "plugins: []",
            "strictMcpConfig: true",
            'permissionMode: "dontAsk"',
            "includePartialMessages: true",
            "initializationResult()",
            "supportedModels()",
            'auth_source: "oauth"',
            'runtime_version: SDK_VERSION',
            'tools: [...TOOL_NAMES]',
            'const hasModel = request.body.model !== undefined',
            'if (hasEffort && !hasModel)',
            'selected.reasoning_efforts.length === 0',
            'this.event("tool.open", { call_id: callId, name, input: forwarded }',
            'kind: z.enum(["create", "modify", "delete", "rename"])',
            'old_path: toolPathString().optional()',
            "DENIED_TOOL_UNSUPPORTED",
            '"code":"DENIED_TOOL_CONCURRENCY"',
            'throw new ProtocolFailure("DENIED_PROVIDER_CAPACITY", "turn_id")',
            "MAX_DELTA_BYTES = 32_768",
            "MAX_FRAME_BYTES = 1_048_576",
            '"num_turns",\n      0,\n      this.maxTurns!,\n      "MODEL_CALL_BUDGET_EXHAUSTED",',
            'code: "MODEL_CALL_BUDGET_EXHAUSTED", fatal: true',
        ):
            self.assertIn(fragment, worker)
        for name in (
            "kaizen_read_file", "kaizen_list_files", "kaizen_search_text",
            "kaizen_run_process", "kaizen_propose_changes",
        ):
            self.assertIn(f'tool("{name}"', worker)
        self.assertNotIn("maxBudgetUsd", worker)
        self.assertNotIn("pathToClaudeCodeExecutable", worker)
        self.assertNotIn("to_path:", worker)

    def test_per_turn_semantic_audit_pins_exact_sdk_and_windows_native_packages(self) -> None:
        lock_path = SOURCE_ROOT / "package-lock.json"
        lock_bytes = lock_path.read_bytes()
        lock = json.loads(lock_bytes)
        manifest = json.loads((SOURCE_ROOT / "runtime-manifest.json").read_text(encoding="utf-8"))
        sdk = lock["packages"]["node_modules/@anthropic-ai/claude-agent-sdk"]
        native = lock["packages"][f"node_modules/{AUDITED_WINDOWS_NATIVE_PACKAGE}"]

        self.assertEqual(SDK_VERSION, AUDITED_SDK_VERSION)
        self.assertEqual(manifest["sdk_version"], AUDITED_SDK_VERSION)
        self.assertEqual(hashlib.sha256(lock_bytes).hexdigest(), AUDITED_PACKAGE_LOCK_SHA256)
        self.assertEqual(sdk["version"], AUDITED_SDK_VERSION)
        self.assertEqual(sdk["integrity"], AUDITED_SDK_TARBALL_INTEGRITY)
        self.assertEqual(native["version"], AUDITED_SDK_VERSION)
        self.assertEqual(native["integrity"], AUDITED_WINDOWS_NATIVE_TARBALL_INTEGRITY)
        self.assertEqual(native["cpu"], ["x64"])
        self.assertEqual(native["os"], ["win32"])
        self.assertIs(native["optional"], True)

    def test_installed_windows_native_matches_semantically_audited_artifact(self) -> None:
        runtime_root = WORK_ROOT / "orchestration" / "provider-runtime" / "claude-agent"
        pointer_path = runtime_root / "current.json"
        if os.name != "nt" or not pointer_path.is_file():
            self.skipTest("installed win32-x64 Claude runtime is unavailable")
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        active = pointer.get("active")
        if not isinstance(active, str) or not active.endswith("-win32-x64"):
            self.skipTest("active Claude runtime is not the audited win32-x64 target")
        self.assertEqual(active, f"{expected_source_identity(SOURCE_ROOT)}-win32-x64")

        target = (runtime_root / active).resolve()
        self.assertEqual(target.parent, runtime_root.resolve())
        integrity = json.loads((target / runtime_module.INTEGRITY_FILE).read_text(encoding="utf-8"))
        native_relative = f"node_modules/{AUDITED_WINDOWS_NATIVE_PACKAGE}/claude.exe"
        self.assertEqual(integrity["sdk_version"], AUDITED_SDK_VERSION)
        self.assertEqual(integrity["lock_sha256"], AUDITED_PACKAGE_LOCK_SHA256)
        self.assertEqual(integrity["native_relative"], native_relative)
        self.assertEqual(integrity["native_sha256"], AUDITED_WINDOWS_NATIVE_SHA256)
        native_package = json.loads(
            (target / f"node_modules/{AUDITED_WINDOWS_NATIVE_PACKAGE}/package.json").read_text(encoding="utf-8")
        )
        self.assertEqual(native_package["version"], AUDITED_SDK_VERSION)

        native_sha256, contains_version = hash_and_find(target / native_relative,
                                                        AUDITED_WINDOWS_NATIVE_RUNTIME_VERSION)
        self.assertEqual(native_sha256, AUDITED_WINDOWS_NATIVE_SHA256)
        self.assertTrue(contains_version, "audited native runtime version marker changed")

    def test_worker_tool_limits_match_the_authoritative_gateway(self) -> None:
        worker = (SOURCE_ROOT / "worker.ts").read_text(encoding="utf-8")
        expected = {
            "MAX_TOOL_PATH_CHARS": 1_024,
            "MAX_GLOB_CHARS": 1_024,
            "MAX_SEARCH_QUERY_BYTES": tool_gateway.SEARCH_MAX_QUERY_BYTES,
            "MAX_PROCESS_ARGS": tool_gateway.PROCESS_MAX_ARGS,
            "MAX_PROCESS_ARG_BYTES": tool_gateway.PROCESS_MAX_ARG_BYTES,
            "MAX_PROPOSAL_SUMMARY_CHARS": 256,
        }
        for name, value in expected.items():
            with self.subTest(name=name):
                self.assertEqual(self._worker_constant(worker, name), value)
        for fragment in (
            "path: toolPathString()",
            "old_path: toolPathString().optional()",
            "query: utf8BoundedString(MAX_SEARCH_QUERY_BYTES, true)",
            "executable: utf8BoundedString(MAX_PROCESS_ARG_BYTES, true)",
            "argv: z.array(utf8BoundedString(MAX_PROCESS_ARG_BYTES)).max(MAX_PROCESS_ARGS)",
            "content: utf8BoundedString(MAX_PROPOSAL_FILE_BYTES).optional()",
            "summary: z.string().min(1).max(MAX_PROPOSAL_SUMMARY_CHARS)",
        ):
            self.assertIn(fragment, worker)

    def test_declared_utf8_and_disabled_sdk_surfaces_are_fail_closed(self) -> None:
        worker = (SOURCE_ROOT / "worker.ts").read_text(encoding="utf-8")
        for fragment in (
            'new TextDecoder("utf-8", { fatal: true })',
            'if (requireEncoding === "utf-8") decodeDeclaredUtf8(content);',
            'for (const field of ["agents", "skills", "plugins"] as const)',
            'assertDisabledSurface(init[field], `reported_${field}`)',
            'assertDisabledSurface(controlSurface[field], `reported_${field}`)',
        ):
            self.assertIn(fragment, worker)

    def test_worker_derives_non_exported_sdk_boundary_types(self) -> None:
        worker = (SOURCE_ROOT / "worker.ts").read_text(encoding="utf-8")
        self.assertNotIn("type CallToolResult,", worker)
        self.assertIn("type ToolCallResult = Awaited<ReturnType<Parameters<typeof tool>[3]>>;", worker)
        self.assertIn('type UserMessageContent = Exclude<SDKUserMessage["message"]["content"], string>;', worker)
        self.assertIn("const content: UserMessageContent", worker)
        self.assertNotIn("const content: Array<JsonObject>", worker)
        self.assertNotIn("as SDKUserMessage", worker)

    def test_tracked_source_has_no_credential_file_access_or_stdout_logging(self) -> None:
        worker = (SOURCE_ROOT / "worker.ts").read_text(encoding="utf-8")
        for forbidden in (".credentials.json", "credentials.json", "CLAUDE_CONFIG_DIR]", "console.log(",
                          "console.info(", "console.warn(", "console.error("):
            self.assertNotIn(forbidden, worker)
        self.assertEqual(worker.count("process.stdout.write"), 1)
        self.assertIn("_SESSION_TOKEN$", worker)
        self.assertIn("_API_KEY$", worker)
        self.assertIn('upper !== "NODE_OPTIONS"', worker)
        self.assertIn('upper !== "NODE_PATH"', worker)

    def test_source_install_refuses_to_invent_a_missing_lock(self) -> None:
        if (SOURCE_ROOT / "package-lock.json").exists():
            self.skipTest("an audited lock has since been added")
        with tempfile.TemporaryDirectory(prefix="claude-runtime-lock-test-", dir=WORK_ROOT) as temporary:
            root = Path(temporary)
            node, npm = root / "node.exe", root / "npm.cmd"
            node.write_bytes(b"node")
            npm.write_bytes(b"npm")
            with self.assertRaises(ClaudeRuntimeError) as raised:
                install_runtime(root / "runtime", node_executable=node, npm_executable=npm,
                                source_root=SOURCE_ROOT, runner=FakeNpmRunner(),
                                system="win32", machine="AMD64")
            self.assertEqual(raised.exception.field, "package_lock")


class CompiledFakeWorkerTest(unittest.TestCase):
    """Compile worker.ts against fake SDK/zod modules and drive its offline stdio protocol."""

    @classmethod
    def setUpClass(cls) -> None:
        node = Path(os.environ.get("DEVROOT", str(REPO_ROOT.parent))) / "node" / "node.exe"
        compiler = REPO_ROOT / "extension" / "node_modules" / "typescript" / "bin" / "tsc"
        if not node.is_file() or not compiler.is_file():
            raise unittest.SkipTest("warm DEVROOT Node/TypeScript is unavailable")
        cls._temporary = tempfile.TemporaryDirectory(prefix="claude-worker-fake-", dir=WORK_ROOT)
        cls.root = Path(cls._temporary.name)
        cls.package = cls.root / "package"
        shutil.copytree(SOURCE_ROOT, cls.package)
        sdk = cls.package / "node_modules/@anthropic-ai/claude-agent-sdk"
        zod = cls.package / "node_modules/zod"
        sdk.mkdir(parents=True)
        zod.mkdir(parents=True)
        write_json(sdk / "package.json", {"name": SDK_PACKAGE, "version": SDK_VERSION, "type": "module"})
        write_json(zod / "package.json", {"name": "zod", "version": ZOD_VERSION, "type": "module"})
        (sdk / "index.d.ts").write_text(
            "type FakeCallToolResult={content:Array<{type:'text';text:string}>;isError?:boolean};\n"
            "type FakeTextBlock={type:'text';text:string}; type FakeImageBlock={type:'image';"
            "source:{type:'base64';media_type:'image/png'|'image/jpeg'|'image/webp'|'image/gif';data:string}};\n"
            "export type SDKMessage=any; export type SDKUserMessage={type:'user';"
            "message:{role:'user'|'assistant'|'system';content:string|Array<FakeTextBlock|FakeImageBlock>};"
            "parent_tool_use_id:string|null;origin?:{kind:string}};\n"
            "export interface Query extends AsyncIterable<any>{initializationResult():Promise<any>;"
            "supportedModels():Promise<any[]>;mcpServerStatus():Promise<any[]>;interrupt():Promise<void>;close():void;}\n"
            "export function tool(name:string,description:string,schema:any,"
            "handler:(args:Record<string,unknown>)=>Promise<FakeCallToolResult>):any;\n"
            "export function createSdkMcpServer(options:any):any; export function query(args:any):Query;\n",
            encoding="utf-8",
        )
        (sdk / "index.js").write_text(
            "export function tool(name,description,schema,handler){return {name,description,schema,handler};}\n"
            "export function createSdkMcpServer(options){return {type:'sdk',name:options.name,__tools:options.tools};}\n"
            "function authValue(name){const value=process.env[name];return value==='missing'?undefined:(value||'oauth');}\n"
            "function surfaceValue(name){const value=process.env[name];return value==='missing'?undefined:value==='malformed'?{}:value==='nonempty'?['unexpected']:[];}\n"
            "const models=[{value:'model-a',displayName:'Model A',description:'fake',supportedEffortLevels:process.env.FAKE_NO_EFFORT==='1'?[]:['low','high'],"
            "supportsAdaptiveThinking:true,supportsFastMode:false}];\n"
            "let queryCount=0;let initCount=0;let promptCount=0;\n"
            "class FakeQuery{constructor(args){this.args=args;this.closed=false;queryCount+=1;}"
            "async initializationResult(){initCount+=1;const env=this.args.options.env;const expected=process.env.FAKE_EXPECT_RETRIES;"
            "if(expected==='absent'&&Object.hasOwn(env,'CLAUDE_CODE_MAX_RETRIES'))throw new Error('retry override leaked');"
            "if(expected==='0'&&env.CLAUDE_CODE_MAX_RETRIES!=='0')throw new Error('retry override missing');"
            "if(process.env.FAKE_ASSERT_ENV_SANITIZED&&Object.keys(env).some(k=>/(?:CREDENTIAL|_API_KEY$|_AUTH_TOKEN$|_SESSION_TOKEN$|_TOKEN$|^NODE_OPTIONS$|^NODE_PATH$)/i.test(k)))throw new Error('unsafe env');"
            "const api=authValue('FAKE_AUTH_SOURCE');const token=process.env.FAKE_TOKEN_SOURCE?authValue('FAKE_TOKEN_SOURCE'):api;"
            "return {commands:[],agents:surfaceValue('FAKE_CONTROL_AGENTS'),skills:surfaceValue('FAKE_CONTROL_SKILLS'),"
            "plugins:surfaceValue('FAKE_CONTROL_PLUGINS'),models,account:{apiKeySource:api,tokenSource:token}};}"
            "async supportedModels(){return models;} async mcpServerStatus(){return [{name:'kaizen',status:'connected'}];}"
            "async interrupt(){} close(){this.closed=true;}"
            "async *[Symbol.asyncIterator](){const o=this.args.options;const auth=process.env.FAKE_INIT_AUTH_SOURCE||authValue('FAKE_AUTH_SOURCE');"
            "yield {type:'system',subtype:'init',apiKeySource:auth,model:o.model||'model-a',permissionMode:o.permissionMode,"
            "tools:process.env.FAKE_EXTRA_TOOL?[...o.allowedTools,'Bash']:o.allowedTools,"
            "agents:surfaceValue('FAKE_INIT_AGENTS'),skills:surfaceValue('FAKE_INIT_SKILLS'),"
            "plugins:surfaceValue('FAKE_INIT_PLUGINS'),"
            "mcp_servers:[{name:'kaizen',status:'connected'}]};"
            "for await(const message of this.args.prompt){const value=message.message.content;"
            "const text=typeof value==='string'?value:(value.find(x=>x.type==='text')?.text||'');"
            "promptCount+=1;if(process.env.FAKE_ASSERT_TURN_BUDGET_RESET==='1'"
            "&&(queryCount!==1||initCount!==1||o.maxTurns!==1))throw new Error('query/init/maxTurns drift');"
            "if(text.includes('PROPOSE')){const item=o.mcpServers.kaizen.__tools.find(x=>x.name==='kaizen_propose_changes');"
            "await item.handler({summary:'fixture',changes:[{kind:'modify',path:'target.txt',content:'replacement'}]});}"
            "else if(text.includes('TOOL')){const item=o.mcpServers.kaizen.__tools.find(x=>x.name==='kaizen_run_process');"
            "await item.handler({executable:'fixture.exe',argv:['--ok'],cwd:'.',timeout_ms:1000});}"
            "yield {type:'stream_event',event:{type:'content_block_delta',delta:{type:'text_delta',text:'fake-delta'}}};"
            "const assistantModel=process.env.FAKE_ASSISTANT_MODEL;"
            "if(assistantModel!=='missing')yield {type:'assistant',message:{model:assistantModel||o.model||'model-a'}};"
            "const usageMode=process.env.FAKE_MODEL_USAGE;const modelUsage=usageMode==='selected'?{[o.model||'model-a']:{}}:usageMode==='conflict'?{'wrong-model':{}}:undefined;"
            "const finalResult=process.env.FAKE_ASSERT_TURN_BUDGET_RESET==='1'?`fake-final:q${queryCount}:i${initCount}:p${promptCount}:m${o.maxTurns}`:'fake-final';"
            "yield {type:'result',subtype:'success',is_error:false,result:finalResult,num_turns:1,stop_reason:'end_turn',"
            "terminal_reason:'completed',...(modelUsage?{modelUsage}:{})};}}} export function query(args){return new FakeQuery(args);}\n",
            encoding="utf-8",
        )
        (zod / "index.d.ts").write_text("export const z:any;\n", encoding="utf-8")
        (zod / "index.js").write_text(
            "const chain=new Proxy(function(){},{get(_t,p){if(p==='safeParse')return value=>({success:!(value&&typeof value==='object'&&('shell' in value||(typeof value.timeout_ms==='number'&&value.timeout_ms<1000)))});if(p==='strict'||p==='superRefine'||p==='optional'||p==='default'||p==='min'||p==='max'||p==='int'||p==='describe')return ()=>chain;return chain;},apply(){return chain;}});"
            "export const z=new Proxy({}, {get(){return (..._args)=>chain;}});\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [str(node), str(compiler), "-p", str(cls.package / "tsconfig.json"),
             "--noImplicitAny", "false", "--types", "node", "--typeRoots",
             str(REPO_ROOT / "extension/node_modules/@types")],
            cwd=cls.package,
            env={**os.environ, "TEMP": str(cls.root / "temp"), "TMP": str(cls.root / "temp")},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(f"offline fake worker compile failed: {result.stdout}\n{result.stderr}")
        cls.node = node
        cls.worker = cls.package / "dist/worker.js"

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def setUp(self) -> None:
        self.workspace = self.root / f"workspace-{id(self)}"
        self.session = self.root / f"session-{id(self)}"
        self.cache = self.root / f"cache-{id(self)}"
        for path in (self.workspace, self.session, self.cache):
            path.mkdir(parents=True)
        self.processes: list[subprocess.Popen[str]] = []
        self._worker_output_context: dict[int, tuple[subprocess.Popen[str], list[str]]] = {}
        self._worker_reader_threads: dict[int, tuple[threading.Thread, threading.Thread]] = {}

    def tearDown(self) -> None:
        for process in self.processes:
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                self._join_worker_readers(process)
            finally:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()

    def register_directory_link_cleanup(self, link: Path) -> None:
        def remove_link() -> None:
            if not os.path.lexists(link):
                return
            if os.name == "nt":
                try:
                    os.rmdir(link)
                    return
                except OSError:
                    pass
            link.unlink(missing_ok=True)

        self.addCleanup(remove_link)

    def make_directory_link(self, link: Path, target: Path) -> None:
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            if os.name != "nt" or os.path.lexists(link):
                self.skipTest(f"directory reparse fixture unavailable: {error}")
            comspec = os.environ.get("COMSPEC")
            if not comspec:
                self.skipTest("COMSPEC unavailable for directory-junction fixture")
            result = subprocess.run(
                [comspec, "/d", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if result.returncode != 0:
                self.skipTest(f"directory-junction fixture unavailable: {result.stderr or result.stdout}")
        self.register_directory_link_cleanup(link)

    def make_directory_junction(self, link: Path, target: Path) -> None:
        if os.name != "nt":
            self.skipTest("Windows directory junction fixture")
        comspec = os.environ.get("COMSPEC")
        if not comspec:
            self.skipTest("COMSPEC unavailable for directory-junction fixture")
        result = subprocess.run(
            [comspec, "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode != 0:
            self.skipTest(f"directory-junction fixture unavailable: {result.stderr or result.stdout}")
        self.register_directory_link_cleanup(link)
        is_junction = getattr(link, "is_junction", None)
        if callable(is_junction) and not is_junction():
            self.fail("mklink /J did not produce a directory junction")

    def start_worker(self, **extra_env: str):
        env = {
            **os.environ,
            "KAIZEN_WORKSPACE_ROOT": str(self.workspace),
            "KAIZEN_CLAUDE_SESSION_ROOT": str(self.session),
            "KAIZEN_CLAUDE_CACHE_ROOT": str(self.cache),
            "TEMP": str(self.session / "outer-temp"),
            "TMP": str(self.session / "outer-temp"),
            **extra_env,
        }
        process = subprocess.Popen(
            [str(self.node), str(self.worker)], cwd=self.package, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
        )
        self.processes.append(process)
        output: queue.Queue[str] = queue.Queue()
        stderr_lines: list[str] = []

        def reader() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                output.put(line)

        def stderr_reader() -> None:
            assert process.stderr is not None
            stderr_lines.extend(process.stderr)

        stdout_thread = threading.Thread(target=reader, daemon=True, name=f"claude-worker-stdout-{process.pid}")
        stderr_thread = threading.Thread(target=stderr_reader, daemon=True, name=f"claude-worker-stderr-{process.pid}")
        self._worker_output_context[id(output)] = (process, stderr_lines)
        self._worker_reader_threads[id(process)] = (stdout_thread, stderr_thread)
        stdout_thread.start()
        stderr_thread.start()
        return process, output

    def send(self, process: subprocess.Popen[str], value: dict[str, object]) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def receive(self, output: queue.Queue[str], *, count: int = 1) -> list[dict[str, object]]:
        values = []
        for _ in range(count):
            try:
                line = output.get(timeout=10)
            except queue.Empty:
                process, stderr_lines = self._worker_output_context[id(output)]
                self.fail(
                    f"worker emitted only {len(values)}/{count} expected frame(s); "
                    f"returncode={process.poll()}; stderr={''.join(stderr_lines)[-4000:]!r}"
                )
            values.append(json.loads(line))
        return values

    def _join_worker_readers(self, process: subprocess.Popen[str]) -> None:
        for thread in self._worker_reader_threads.get(id(process), ()):
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive(), f"worker pipe reader did not reach EOF: {thread.name}")

    def assert_failed_process_exit(self, process: subprocess.Popen[str]) -> None:
        process.wait(timeout=2)
        self._join_worker_readers(process)
        self.assertIsNotNone(process.returncode)
        self.assertNotEqual(process.returncode, 0)

    def staged_files(self) -> list[Path]:
        outbox = self.session / "outbox"
        return sorted(outbox.glob(".stage-*")) if outbox.is_dir() else []

    @staticmethod
    def frame(request_id: str, op: str, *, session_id: str = "worker-1", turn_id: str | None = None,
              body: dict[str, object] | None = None) -> dict[str, object]:
        """Wire-frame shape (v/type/id/op/session_id/optional turn_id/body) for worker requests."""
        return {"v": 1, "type": "request", "id": request_id, "op": op, "session_id": session_id,
                **({"turn_id": turn_id} if turn_id else {}), "body": body or {}}

    def initialize(self, process, output, body: dict[str, object]) -> list[dict[str, object]]:
        self.send(process, self.frame("init-1", "initialize", body=body))
        return self.receive(output, count=2)

    def test_catalog_probe_and_selected_tool_turn_use_protocol_only_stdout(self) -> None:
        probe, probe_output = self.start_worker()
        values = self.initialize(probe, probe_output, {"auth_mode": "subscription"})
        initialized = next(value for value in values if value.get("type") == "response")["body"]
        self.assertEqual(initialized["auth_source"], "oauth")
        self.assertEqual(initialized["runtime_version"], SDK_VERSION)
        self.assertEqual(initialized["tools"], [
            "kaizen_read_file", "kaizen_list_files", "kaizen_search_text",
            "kaizen_run_process", "kaizen_propose_changes",
        ])
        self.assertNotIn("selected_model", initialized)
        self.send(probe, self.frame("close-probe", "session.close"))
        self.receive(probe_output)

        process, output = self.start_worker()
        values = self.initialize(process, output, {
            "auth_mode": "subscription", "model": "model-a", "reasoning_effort": "high", "max_turns": 8,
        })
        self.assertTrue(any(value.get("event") == "initialized" for value in values))
        prompt_path = self.session / "inputs/prompt.txt"
        prompt_path.parent.mkdir(parents=True)
        prompt = b"TOOL then answer"
        prompt_path.write_bytes(prompt)
        reference = {"root": "runtime", "path": "inputs/prompt.txt",
                     "sha256": hashlib.sha256(prompt).hexdigest(), "bytes": len(prompt), "encoding": "utf-8"}
        self.send(process, self.frame("turn-1", "turn.start", turn_id="turn-1", body={"prompt_ref": reference}))
        seen: list[dict[str, object]] = []
        while not any(value.get("event") == "tool.invoke" for value in seen):
            seen.extend(self.receive(output))
        tool_open = next(value for value in seen if value.get("event") == "tool.open")
        self.assertEqual(tool_open["body"]["input"]["executable"], "fixture.exe")
        invoke = next(value for value in seen if value.get("event") == "tool.invoke")
        call_id = invoke["body"]["call_id"]
        self.send(process, self.frame("tool-result-1", "tool.result", turn_id="turn-1",
                                      body={"call_id": call_id, "ok": True, "result": {"exit_code": 0}}))
        while not any(value.get("event") == "turn.result" for value in seen):
            seen.extend(self.receive(output))
        deltas = [value["body"]["text"] for value in seen if value.get("event") == "delta"]
        self.assertEqual(deltas, ["fake-delta"])
        result = next(value for value in seen if value.get("event") == "turn.result")
        self.assertEqual(result["body"]["result"], "fake-final")
        close_started = time.monotonic()
        self.send(process, self.frame("close-1", "session.close"))
        self.receive(output)
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertLess(time.monotonic() - close_started, 2.0)
        self.assertTrue(all(isinstance(value, dict) for value in seen))

    def test_max_turns_resets_for_two_messages_on_one_worker_query(self) -> None:
        process, output = self.start_worker(FAKE_ASSERT_TURN_BUDGET_RESET="1")
        initialized = self.initialize(process, output, {
            "auth_mode": "subscription", "model": "model-a", "reasoning_effort": "low",
            "max_turns": 1,
        })
        self.assertEqual(sum(value.get("event") == "initialized" for value in initialized), 1)
        self.assertTrue(next(value for value in initialized if value.get("type") == "response")["ok"])
        observed = list(initialized)

        for index in (1, 2):
            prompt = f"bounded sequential prompt {index}".encode("utf-8")
            relative = f"inputs/bounded-{index}.txt"
            target = self.session / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(prompt)
            prompt_ref = {
                "root": "runtime", "path": relative, "sha256": hashlib.sha256(prompt).hexdigest(),
                "bytes": len(prompt), "encoding": "utf-8",
            }
            request_id, turn_id = f"turn-request-{index}", f"bounded-turn-{index}"
            self.send(process, self.frame(request_id, "turn.start", turn_id=turn_id,
                                          body={"prompt_ref": prompt_ref}))
            turn_values: list[dict[str, object]] = []
            while not any(value.get("event") in {"turn.result", "fatal"} for value in turn_values):
                turn_values.extend(self.receive(output))
            observed.extend(turn_values)
            self.assertFalse(any(value.get("event") == "fatal" for value in turn_values), turn_values)
            accepted = next(value for value in turn_values if value.get("id") == request_id)
            self.assertTrue(accepted["ok"])
            result = next(value for value in turn_values if value.get("event") == "turn.result")
            self.assertEqual(result["turn_id"], turn_id)
            self.assertTrue(result["body"]["ok"])
            self.assertEqual(result["body"]["num_turns"], 1)
            self.assertEqual(result["body"]["result"], f"fake-final:q1:i1:p{index}:m1")

        self.assertEqual(sum(value.get("event") == "initialized" for value in observed), 1)
        self.assertEqual(sum(value.get("event") == "turn.result" for value in observed), 2)
        self.send(process, self.frame("close-budget-reset", "session.close"))
        self.assertTrue(self.receive(output)[0]["ok"])
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)

    def test_each_advanced_capability_has_bounded_no_turn_worker_evidence(self) -> None:
        process, output = self.start_worker()
        self.initialize(process, output, {"auth_mode": "subscription"})

        def stage(relative: str, content: bytes, **metadata: object) -> dict[str, object]:
            target = self.session / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            return {
                "root": "runtime", "path": relative,
                "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content), **metadata,
            }

        prompt_ref = stage("capability/prompt.txt", b"KAIZEN_CAPABILITY_PROBE_PROMPT", encoding="utf-8")
        context_ref = stage("capability/context.txt", b"KAIZEN_CAPABILITY_CONTEXT", encoding="utf-8")
        image = bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c02"
            "0000000b4944415478da6364f80f00010501012718e3660000000049454e44ae426082"
        )
        image_ref = stage("capability/image.png", image, media_type="image/png")
        for index, feature in enumerate(CAPABILITY_PROBE_FEATURES):
            challenge = f"cp-runtime-{index}"
            body: dict[str, object] = {"feature": feature, "challenge": challenge}
            if feature == "image_attachments":
                body.update({"prompt_ref": prompt_ref, "image_ref": image_ref})
            elif feature == "governed_context":
                body.update({"prompt_ref": prompt_ref, "context_ref": context_ref})
            self.send(process, self.frame(f"probe-{index}", "capability.probe", body=body))
            response = self.receive(output)[0]
            self.assertTrue(response["ok"], response)
            result = validate_capability_probe_result(
                response["body"], feature=feature, challenge=challenge,
            )
            if feature == "diff_snapshots":
                artifact = result["artifact_ref"]
                landed = self.session / Path(artifact["path"])
                self.assertEqual(landed.read_text(encoding="utf-8"), f"KAIZEN_CAPABILITY_DIFF:{challenge}")
        self.send(process, self.frame("close-probes", "session.close"))
        self.assertTrue(self.receive(output)[0]["ok"])
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)

    def test_non_oauth_and_partial_profile_fail_closed(self) -> None:
        for environment, expected in (
            ({"FAKE_AUTH_SOURCE": "api-key"}, "DENIED_AUTH_MODE_MISMATCH"),
            ({"FAKE_AUTH_SOURCE": "oauth", "FAKE_TOKEN_SOURCE": "api-key"},
             "DENIED_AUTH_MODE_MISMATCH"),
            ({"FAKE_AUTH_SOURCE": "missing", "FAKE_TOKEN_SOURCE": "missing"},
             "DENIED_AUTH_UNAVAILABLE"),
        ):
            with self.subTest(expected=expected):
                process, output = self.start_worker(**environment)
                values = self.initialize(process, output, {"auth_mode": "subscription"})
                response = next(value for value in values if value.get("type") == "response")
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"]["code"], expected)
                self.assertTrue(any(value.get("event") == "fatal" for value in values))
                self.assert_failed_process_exit(process)

        process, output = self.start_worker()
        values = self.initialize(process, output, {"auth_mode": "subscription", "model": "model-a"})
        response = next(value for value in values if value.get("type") == "response")
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], {
            "code": "DENIED_EFFORT_UNSUPPORTED", "field": "reasoning_effort",
        })
        fatal = next(value for value in values if value.get("event") == "fatal")
        self.assertNotIn("turn_id", fatal)
        self.assertFalse(any(value.get("event") == "turn.open" for value in values))
        self.assert_failed_process_exit(process)

        process, output = self.start_worker()
        values = self.initialize(process, output, {
            "auth_mode": "subscription", "reasoning_effort": "high",
        })
        response = next(value for value in values if value.get("type") == "response")
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], {
            "code": "DENIED_WORKER_PROTOCOL", "field": "profile",
        })
        self.assert_failed_process_exit(process)

        process, output = self.start_worker(FAKE_NO_EFFORT="1")
        values = self.initialize(process, output, {
            "auth_mode": "subscription", "model": "model-a", "max_turns": 1,
        })
        response = next(value for value in values if value.get("type") == "response")
        self.assertTrue(response["ok"])
        self.assertEqual(response["body"]["models"][0]["reasoning_efforts"], [])
        prompt = b"no effort model turn"
        prompt_path = self.session / "inputs/no-effort.txt"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_bytes(prompt)
        prompt_ref = {
            "root": "runtime", "path": "inputs/no-effort.txt",
            "sha256": hashlib.sha256(prompt).hexdigest(), "bytes": len(prompt), "encoding": "utf-8",
        }
        self.send(process, self.frame(
            "turn-no-effort", "turn.start", turn_id="turn-no-effort", body={"prompt_ref": prompt_ref},
        ))
        seen: list[dict[str, object]] = []
        while not any(value.get("event") == "turn.result" for value in seen):
            seen.extend(self.receive(output))
        accepted = next(value for value in seen if value.get("id") == "turn-no-effort")
        self.assertTrue(accepted["ok"])
        opened = next(value for value in seen if value.get("event") == "turn.open")
        self.assertNotIn("reasoning_effort", opened["body"])
        result = next(value for value in seen if value.get("event") == "turn.result")
        self.assertTrue(result["body"]["ok"])
        self.send(process, self.frame("close-no-effort", "session.close"))
        self.assertTrue(self.receive(output)[0]["ok"])
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)

    def test_malformed_protocol_frame_is_fatal_and_exits_without_host_teardown(self) -> None:
        process, output = self.start_worker()
        assert process.stdin is not None
        process.stdin.write("{not-json}\n")
        process.stdin.flush()
        self.assert_failed_process_exit(process)
        self.assertTrue(output.empty())

    def test_reparse_roots_fail_before_worker_child_writes(self) -> None:
        for label, variable in (
            ("workspace", "KAIZEN_WORKSPACE_ROOT"),
            ("runtime", "KAIZEN_CLAUDE_SESSION_ROOT"),
            ("cache", "KAIZEN_CLAUDE_CACHE_ROOT"),
        ):
            with self.subTest(label=label):
                outside = self.root / f"outside-{label}-{id(self)}"
                linked = self.root / f"linked-{label}-{id(self)}"
                outside.mkdir()
                self.make_directory_link(linked, outside)
                process, output = self.start_worker(**{variable: str(linked)})
                values = self.initialize(process, output, {
                    "auth_mode": "subscription", "model": "model-a",
                    "reasoning_effort": "high", "max_turns": 8,
                })
                response = next(value for value in values if value.get("type") == "response")
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"], {
                    "code": "DENIED_WORKER_PROTOCOL", "field": f"{label}_root",
                })
                self.assertEqual(list(outside.iterdir()), [])
                self.assert_failed_process_exit(process)

    def test_stage_text_rejects_replaced_outbox_before_out_of_root_bytes(self) -> None:
        process, output = self.start_worker()
        self.initialize(process, output, {
            "auth_mode": "subscription", "model": "model-a",
            "reasoning_effort": "high", "max_turns": 8,
        })
        outbox = self.session / "outbox"
        outbox.rmdir()
        outside = self.root / f"outside-outbox-{id(self)}"
        outside.mkdir()
        self.make_directory_link(outbox, outside)
        self.send(process, self.frame(
            "probe-reparse-outbox", "capability.probe",
            body={"feature": "diff_snapshots", "challenge": "reparse-outbox"},
        ))
        response = self.receive(output)[0]
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], {
            "code": "DENIED_WORKER_PROTOCOL", "field": "outbox",
        })
        self.assertEqual(list(outside.iterdir()), [])
        self.send(process, self.frame("close-reparse-outbox", "session.close"))
        self.assertTrue(self.receive(output)[0]["ok"])

    @unittest.skipUnless(os.name == "nt", "Windows directory junction semantics")
    def test_windows_junction_runtime_root_and_outbox_fail_before_external_writes(self) -> None:
        outside_root = self.root / f"outside-junction-root-{id(self)}"
        junction_root = self.root / f"junction-root-{id(self)}"
        outside_root.mkdir()
        self.make_directory_junction(junction_root, outside_root)
        process, output = self.start_worker(KAIZEN_CLAUDE_SESSION_ROOT=str(junction_root))
        values = self.initialize(process, output, {
            "auth_mode": "subscription", "model": "model-a",
            "reasoning_effort": "high", "max_turns": 8,
        })
        response = next(value for value in values if value.get("type") == "response")
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], {
            "code": "DENIED_WORKER_PROTOCOL", "field": "runtime_root",
        })
        self.assertEqual(list(outside_root.iterdir()), [])
        self.assert_failed_process_exit(process)

        process, output = self.start_worker()
        self.initialize(process, output, {
            "auth_mode": "subscription", "model": "model-a",
            "reasoning_effort": "high", "max_turns": 8,
        })
        outbox = self.session / "outbox"
        outbox.rmdir()
        outside_outbox = self.root / f"outside-junction-outbox-{id(self)}"
        outside_outbox.mkdir()
        self.make_directory_junction(outbox, outside_outbox)
        self.send(process, self.frame(
            "probe-junction-outbox", "capability.probe",
            body={"feature": "diff_snapshots", "challenge": "junction-outbox"},
        ))
        response = self.receive(output)[0]
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], {
            "code": "DENIED_WORKER_PROTOCOL", "field": "outbox",
        })
        self.assertEqual(list(outside_outbox.iterdir()), [])
        self.send(process, self.frame("close-junction-outbox", "session.close"))
        self.assertTrue(self.receive(output)[0]["ok"])
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)

    def test_stage_text_race_results_remove_temps_and_preserve_primary_denial(self) -> None:
        for index, race in enumerate(("same", "collision")):
            with self.subTest(race=race):
                environment = {"KAIZEN_FAKE_CLAUDE_STAGE_RENAME_RACE": race}
                if race == "collision":
                    environment["KAIZEN_FAKE_CLAUDE_STAGE_CLEANUP_ERROR"] = "1"
                process, output = self.start_worker(**environment)
                self.initialize(process, output, {
                    "auth_mode": "subscription", "model": "model-a",
                    "reasoning_effort": "high", "max_turns": 8,
                })
                challenge = f"staged-race-{race}-{index}"
                self.send(process, self.frame(
                    f"probe-staged-race-{index}", "capability.probe",
                    body={"feature": "diff_snapshots", "challenge": challenge},
                ))
                response = self.receive(output)[0]
                if race == "same":
                    self.assertTrue(response["ok"], response)
                    artifact = response["body"]["artifact_ref"]
                    self.assertEqual(
                        (self.session / Path(artifact["path"])).read_text(encoding="utf-8"),
                        f"KAIZEN_CAPABILITY_DIFF:{challenge}",
                    )
                else:
                    self.assertFalse(response["ok"])
                    self.assertEqual(response["error"], {
                        "code": "DENIED_WORKER_PROTOCOL", "field": "outbox_collision",
                    })
                self.assertEqual(self.staged_files(), [])
                self.send(process, self.frame(f"close-staged-race-{index}", "session.close"))
                self.assertTrue(self.receive(output)[0]["ok"])
                process.wait(timeout=5)
                self.assertEqual(process.returncode, 0)

    def test_disabled_sdk_surfaces_accept_absence_but_reject_malformed_values(self) -> None:
        absent, absent_output = self.start_worker(
            FAKE_CONTROL_AGENTS="missing", FAKE_CONTROL_SKILLS="missing",
            FAKE_CONTROL_PLUGINS="missing", FAKE_INIT_AGENTS="missing",
            FAKE_INIT_SKILLS="missing", FAKE_INIT_PLUGINS="missing",
        )
        absent_values = self.initialize(absent, absent_output, {"auth_mode": "subscription"})
        self.assertTrue(next(value for value in absent_values if value.get("type") == "response")["ok"])
        self.send(absent, self.frame("close-absent-surfaces", "session.close"))
        self.assertTrue(self.receive(absent_output)[0]["ok"])
        absent.wait(timeout=5)

        for source in ("CONTROL", "INIT"):
            for surface in ("AGENTS", "SKILLS", "PLUGINS"):
                with self.subTest(source=source, surface=surface):
                    process, output = self.start_worker(**{f"FAKE_{source}_{surface}": "malformed"})
                    values = self.initialize(process, output, {"auth_mode": "subscription"})
                    response = next(value for value in values if value.get("type") == "response")
                    fatal = next(value for value in values if value.get("event") == "fatal")
                    self.assertFalse(response["ok"])
                    self.assertEqual(response["error"], {
                        "code": "DENIED_TOOL_UNSUPPORTED", "field": f"reported_{surface.lower()}",
                    })
                    self.assertEqual(fatal["body"], response["error"])
                    self.assert_failed_process_exit(process)

    def test_declared_utf8_reference_rejects_invalid_bytes_and_binary_controls(self) -> None:
        cases = (("invalid", b"\xff"), ("control", b"valid\x00binary"))
        for index, (label, content) in enumerate(cases):
            with self.subTest(label=label):
                process, output = self.start_worker()
                self.initialize(process, output, {
                    "auth_mode": "subscription", "model": "model-a",
                    "reasoning_effort": "high", "max_turns": 8,
                })
                relative = f"inputs/{label}.txt"
                target = self.session / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                reference = {
                    "root": "runtime", "path": relative,
                    "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
                    "encoding": "utf-8",
                }
                request_id = f"turn-invalid-text-{index}"
                self.send(process, self.frame(
                    request_id, "turn.start", turn_id=request_id, body={"prompt_ref": reference},
                ))
                response = self.receive(output)[0]
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"], {
                    "code": "DENIED_WORKER_PROTOCOL", "field": "reference.encoding",
                })
                self.send(process, self.frame(f"close-invalid-text-{index}", "session.close"))
                self.assertTrue(self.receive(output)[0]["ok"])
                process.wait(timeout=5)
                self.assertEqual(process.returncode, 0)

    def test_sdk_environment_sanitizes_injection_and_retry_zero_is_test_extension_only(self) -> None:
        ordinary, ordinary_output = self.start_worker(
            FAKE_EXPECT_RETRIES="absent", FAKE_ASSERT_ENV_SANITIZED="1",
            CLAUDE_CODE_MAX_RETRIES="9", mixed_Session_Token="secret",
            Provider_CREDENTIALS_Path="secret-file", NODE_OPTIONS="--no-warnings", NODE_PATH="D:\\attacker",
        )
        ordinary_values = self.initialize(ordinary, ordinary_output, {"auth_mode": "subscription"})
        self.assertTrue(next(value for value in ordinary_values if value.get("type") == "response")["ok"])

        bounded, bounded_output = self.start_worker(
            FAKE_EXPECT_RETRIES="0", KAIZEN_TEST_EXTENSION_PROVIDER_RETRIES="0",
        )
        bounded_values = self.initialize(bounded, bounded_output, {"auth_mode": "subscription"})
        self.assertTrue(next(value for value in bounded_values if value.get("type") == "response")["ok"])

    def test_effective_model_requires_nonconflicting_turn_evidence(self) -> None:
        cases = (
            ({"FAKE_ASSISTANT_MODEL": "missing"}, "fatal"),
            ({"FAKE_ASSISTANT_MODEL": "wrong-model"}, "fatal"),
            ({"FAKE_MODEL_USAGE": "conflict"}, "fatal"),
            ({"FAKE_ASSISTANT_MODEL": "missing", "FAKE_MODEL_USAGE": "selected"}, "turn.result"),
        )
        for index, (environment, terminal) in enumerate(cases):
            with self.subTest(environment=environment):
                process, output = self.start_worker(**environment)
                self.initialize(process, output, {
                    "auth_mode": "subscription", "model": "model-a",
                    "reasoning_effort": "high", "max_turns": 8,
                })
                prompt = b"model evidence"
                path = self.session / f"inputs/model-{index}.txt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(prompt)
                reference = {"root": "runtime", "path": f"inputs/model-{index}.txt",
                             "sha256": hashlib.sha256(prompt).hexdigest(), "bytes": len(prompt),
                             "encoding": "utf-8"}
                self.send(process, self.frame(f"turn-model-{index}", "turn.start",
                                              turn_id=f"turn-model-{index}", body={"prompt_ref": reference}))
                seen: list[dict[str, object]] = []
                while not any(value.get("event") == terminal for value in seen):
                    seen.extend(self.receive(output))
                if terminal == "fatal":
                    fatal = next(value for value in seen if value.get("event") == "fatal")
                    self.assertEqual(fatal["body"]["code"], "DENIED_MODEL_UNAVAILABLE")

    def test_large_tool_result_reference_is_rehashed_parsed_and_duplicate_resolution_refused(self) -> None:
        process, output = self.start_worker()
        self.initialize(process, output, {
            "auth_mode": "subscription", "model": "model-a", "reasoning_effort": "high", "max_turns": 8,
        })
        prompt = b"TOOL"
        prompt_path = self.session / "inputs/large-tool.txt"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_bytes(prompt)
        reference = {"root": "runtime", "path": "inputs/large-tool.txt",
                     "sha256": hashlib.sha256(prompt).hexdigest(), "bytes": len(prompt), "encoding": "utf-8"}
        self.send(process, self.frame("turn-large-tool", "turn.start", turn_id="turn-large-tool",
                                      body={"prompt_ref": reference}))
        seen: list[dict[str, object]] = []
        while not any(value.get("event") == "tool.invoke" for value in seen):
            seen.extend(self.receive(output))
        call_id = next(value for value in seen if value.get("event") == "tool.invoke")["body"]["call_id"]
        payload = json.dumps({"status": "OK", "content": "x" * (256 * 1024)}, separators=(",", ":")).encode()
        result_path = self.session / "tool-results/large.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_bytes(payload)
        result_ref = {"root": "runtime", "path": "tool-results/large.json",
                      "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload), "encoding": "utf-8"}
        result_frame = self.frame("tool-large-result", "tool.result", turn_id="turn-large-tool",
                                  body={"call_id": call_id, "ok": True,
                                        "result": {"status": "OK", "result_ref": result_ref, "fatal": False}})
        self.send(process, result_frame)
        while not any(value.get("event") == "turn.result" for value in seen):
            seen.extend(self.receive(output))
        accepted = next(value for value in seen if value.get("id") == "tool-large-result")
        self.assertTrue(accepted["ok"])
        duplicate = dict(result_frame)
        duplicate["id"] = "tool-large-duplicate"
        self.send(process, duplicate)
        refused = self.receive(output)[0]
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error"]["code"], "DENIED_WORKER_PROTOCOL")

    def test_large_tool_result_reference_hash_mismatch_is_refused(self) -> None:
        process, output = self.start_worker()
        self.initialize(process, output, {
            "auth_mode": "subscription", "model": "model-a", "reasoning_effort": "high", "max_turns": 8,
        })
        prompt = b"TOOL corrupt"
        prompt_path = self.session / "inputs/corrupt-tool.txt"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_bytes(prompt)
        prompt_ref = {"root": "runtime", "path": "inputs/corrupt-tool.txt",
                      "sha256": hashlib.sha256(prompt).hexdigest(), "bytes": len(prompt), "encoding": "utf-8"}
        self.send(process, self.frame("turn-corrupt-tool", "turn.start", turn_id="turn-corrupt-tool",
                                      body={"prompt_ref": prompt_ref}))
        seen: list[dict[str, object]] = []
        while not any(value.get("event") == "tool.invoke" for value in seen):
            seen.extend(self.receive(output))
        call_id = next(value for value in seen if value.get("event") == "tool.invoke")["body"]["call_id"]
        payload = json.dumps({"status": "OK", "content": "x" * (256 * 1024)}, separators=(",", ":")).encode()
        result_path = self.session / "tool-results/corrupt.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_bytes(payload)
        bad_ref = {"root": "runtime", "path": "tool-results/corrupt.json", "sha256": "0" * 64,
                   "bytes": len(payload), "encoding": "utf-8"}
        self.send(process, self.frame(
            "tool-corrupt-result", "tool.result", turn_id="turn-corrupt-tool",
            body={"call_id": call_id, "ok": True,
                  "result": {"status": "OK", "result_ref": bad_ref, "fatal": False}},
        ))
        while not any(value.get("id") == "tool-corrupt-result" for value in seen):
            seen.extend(self.receive(output))
        refused = next(value for value in seen if value.get("id") == "tool-corrupt-result")
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error"]["code"], "DENIED_WORKER_PROTOCOL")

    def test_proposal_content_is_replaced_by_verified_runtime_reference(self) -> None:
        process, output = self.start_worker()
        self.initialize(process, output, {
            "auth_mode": "subscription", "model": "model-a", "reasoning_effort": "high", "max_turns": 8,
        })
        prompt_path = self.session / "inputs/proposal.txt"
        prompt_path.parent.mkdir(parents=True)
        prompt = b"PROPOSE"
        prompt_path.write_bytes(prompt)
        reference = {"root": "runtime", "path": "inputs/proposal.txt",
                     "sha256": hashlib.sha256(prompt).hexdigest(), "bytes": len(prompt), "encoding": "utf-8"}
        self.send(process, self.frame("turn-proposal", "turn.start", turn_id="turn-proposal",
                                      body={"prompt_ref": reference}))
        seen: list[dict[str, object]] = []
        while not any(value.get("event") == "tool.invoke" for value in seen):
            seen.extend(self.receive(output))
        invoke = next(value for value in seen if value.get("event") == "tool.invoke")
        change = invoke["body"]["input"]["changes"][0]
        self.assertNotIn("content", change)
        content_ref = change["content_ref"]
        self.assertEqual(content_ref["root"], "runtime")
        staged = self.session / Path(content_ref["path"])
        self.assertEqual(staged.read_text(encoding="utf-8"), "replacement")
        self.assertEqual(content_ref["sha256"], hashlib.sha256(b"replacement").hexdigest())
        call_id = invoke["body"]["call_id"]
        self.send(process, self.frame("tool-proposal-result", "tool.result", turn_id="turn-proposal",
                                      body={"call_id": call_id, "ok": False,
                                            "error": {"code": "DENIED_POLICY", "message": "rejected"}}))
        while not any(value.get("event") == "turn.result" for value in seen):
            seen.extend(self.receive(output))
        self.send(process, self.frame("close-proposal", "session.close"))
        self.receive(output)
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)


if __name__ == "__main__":
    unittest.main()
