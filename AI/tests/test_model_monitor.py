"""B6 model-monitor: read-only backend-status view. Offline + deterministic through the isolated
harness -- no model load, no network. Ollama is forced unreachable (dead port); GPU is shape-only
(nvidia-smi may or may not exist on the runner). Confirms: dispatches OK with no args, the four lanes
reflect env config WITHOUT importing torch, the JSON payload shape holds, and the human render draws."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root -> import the module directly
from _harness import IsolatedDBTest, run  # noqa: E402
from kaizen_components import model_monitor as mm  # noqa: E402

# A dead loopback port so /api/ps is refused fast -> deterministic ollama.reachable == False.
_DEAD_OLLAMA = {"KAIZEN_LLM_BASE_URL": "http://127.0.0.1:1/v1"}
_CONFIGURED = {
    "KAIZEN_LLM_BASE_URL": "http://127.0.0.1:1/v1",
    "KAIZEN_EMBED_BACKEND": "sentence-transformers",
    "KAIZEN_RERANK_BACKEND": "sentence-transformers",
    "KAIZEN_PII_MODEL": "fastino/gliner2-privacy-filter-PII-multi",
    "KAIZEN_TORCH_DEVICE": "auto",
}


class MonitorShapeTest(IsolatedDBTest):
    def test_b6_ok_with_no_args(self):
        rc, p = self.kz("B6", env=_DEAD_OLLAMA)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("status"), "OK", p)
        for key in ("gpu", "ollama", "backends", "recent"):
            self.assertIn(key, p, p)

    def test_four_lanes_present_and_unconfigured_by_default(self):
        rc, p = self.kz("B6", env=_DEAD_OLLAMA)
        self.assertEqual(rc, 0, p)
        lanes = {b["lane"]: b for b in p["backends"]}
        self.assertEqual(set(lanes), {"embed", "text", "rerank", "pii"}, p)
        # No KAIZEN_* activation vars -> every lane off (deterministic-fallback invariant).
        self.assertFalse(any(lanes[k]["configured"] for k in lanes), p)

    def test_env_config_reflected_without_torch(self):
        rc, p = self.kz("B6", env=_CONFIGURED)
        self.assertEqual(rc, 0, p)
        lanes = {b["lane"]: b for b in p["backends"]}
        self.assertTrue(lanes["embed"]["configured"], p)
        self.assertEqual(lanes["embed"]["backend"], "sentence-transformers", p)
        self.assertIn("F2LLM-v2-1.7B", lanes["embed"]["model"], p)
        self.assertTrue(lanes["rerank"]["configured"], p)
        self.assertIn("ettin-reranker", lanes["rerank"]["model"], p)
        self.assertTrue(lanes["pii"]["configured"], p)
        self.assertEqual(lanes["pii"]["backend"], "gliner2", p)
        # No --probe -> no weights loaded, so no real device/dim was resolved.
        self.assertNotIn("probe", lanes["embed"], p)

    def test_ollama_unreachable_is_graceful(self):
        rc, p = self.kz("B6", env=_DEAD_OLLAMA)
        self.assertEqual(rc, 0, p)
        self.assertFalse(p["ollama"]["reachable"], p)
        self.assertEqual(p["ollama"]["loaded"], [], p)

    def test_gpu_snapshot_shape(self):
        rc, p = self.kz("B6", env=_DEAD_OLLAMA)
        self.assertEqual(rc, 0, p)
        gpu = p["gpu"]
        self.assertIn("available", gpu)
        self.assertIsInstance(gpu["available"], bool)
        if gpu["available"]:
            self.assertTrue(gpu["devices"], p)
            self.assertIn("temp_c", gpu["devices"][0], p)

    def test_recent_empty_on_fresh_db(self):
        rc, p = self.kz("B6", env=_DEAD_OLLAMA)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["recent"], [], p)


class MonitorRenderTest(IsolatedDBTest):
    def test_human_render_draws_one_shot(self):
        # No --json -> the op renders the lane view to stdout (watch falls back to one-shot: no TTY).
        proc = run(self.root, "B6", env=_DEAD_OLLAMA)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("backend monitor", proc.stdout)
        self.assertIn("Live GPU model processes", proc.stdout)

    def test_watch_falls_back_to_one_shot_without_tty(self):
        # --watch on a piped (non-TTY) stdout must NOT loop forever; it renders once and exits.
        proc = run(self.root, "B6", "--watch", env=_DEAD_OLLAMA)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("backend monitor", proc.stdout)


class RenderBranchTest(unittest.TestCase):
    """render() is a pure function -- exercise the running-panel (GPU processes + Ollama), the
    recent-activity feed, and the demoted config footer deterministically, no network/model load."""

    def _snapshot(self) -> dict:
        return {
            "gpu": {"available": True, "devices": [{"index": 0, "name": "RTX 5080", "temp_c": 41,
                     "fan_pct": 0, "util_pct": 3, "mem_used_mb": 1300, "mem_total_mb": 16303,
                     "power_w": 25, "power_limit_w": 360}]},
            "gpu_processes": {"available": True, "procs": [
                {"pid": 36832, "name": "llama-server.exe", "vram_mb": None,
                 "gpu_mem_mb": 1603.6, "gpu_util_pct": 0.0, "kind": "ollama"}]},
            "ollama": {"reachable": True, "endpoint": "http://127.0.0.1:11434",
                       "loaded": [{"model": "Qwen3.5-9B:Q4_K_M", "size_vram_mb": 1200,
                                   "expires_at": None, "keep_alive": "3m41s"}]},
            "backends": [
                {"lane": "text", "configured": True, "backend": "ollama",
                 "model": "Qwen3.5-9B:Q4_K_M", "device": "server"},
                {"lane": "embed", "configured": False, "backend": None, "model": None, "device": None},
            ],
            "recent": [{"created_at": "2026-07-06T17:36:10Z", "kind": "judge",
                        "model": "Qwen3.5-9B", "provider": "ollama", "latency_ms": 840}],
        }

    def test_render_shows_running_recent_and_config(self):
        out = mm.render(self._snapshot(), interval=None)
        self.assertIn("RTX 5080", out)
        self.assertIn("Live GPU model processes", out)
        self.assertIn("llama-server.exe", out)   # GPU AI process (cross-project)
        self.assertIn("1603.6 MB", out)          # per-process VRAM from perf counters
        self.assertIn("Qwen3.5-9B:Q4_K_M", out)
        self.assertIn("3m41s", out)              # keep-alive
        self.assertIn("Recent Kaizen model calls", out)
        self.assertIn("judge", out)              # recent activity feed
        self.assertIn("840ms", out)
        self.assertIn("text=loaded", out)        # demoted config footer reflects resident model

    def test_ollama_root_strips_v1(self):
        self.assertEqual(mm._ollama_root("http://127.0.0.1:11434/v1"), "http://127.0.0.1:11434")
        self.assertEqual(mm._ollama_root("http://host:1234"), "http://host:1234")

    def test_keep_alive_expired_and_none(self):
        self.assertIsNone(mm._keep_alive(None))
        self.assertEqual(mm._keep_alive("2000-01-01T00:00:00+00:00"), "expired")


class EmergencyStopTest(unittest.TestCase):
    """stop_gpu_models() unloads every Ollama model + kills the GPU AI processes, and never raises --
    exercised with the network/kill calls stubbed (no real unload/kill)."""

    def _patch(self, unload, kill):
        orig = (mm.ollama_loaded, mm.gpu_processes, mm._ollama_unload, mm._kill_pid)
        mm.ollama_loaded = lambda: {"reachable": True, "endpoint": "http://x",
                                    "loaded": [{"model": "m1"}, {"model": "m2"}]}
        mm.gpu_processes = lambda exclude_pid=None: {"available": True, "procs": [
            {"pid": 111, "name": "python.exe"}, {"pid": 222, "name": "llama-server.exe"}]}
        mm._ollama_unload = unload
        mm._kill_pid = kill
        self.addCleanup(lambda: setattr_all(mm, orig))

    def test_unloads_and_kills(self):
        unloaded, killed = [], []
        self._patch(lambda root, name: unloaded.append(name), lambda pid: killed.append(pid))
        r = mm.stop_gpu_models(kill_processes=True)
        self.assertEqual(unloaded, ["m1", "m2"], r)
        self.assertEqual(killed, [111, 222], r)
        self.assertEqual(r["total"], 4, r)
        self.assertEqual(r["errors"], [], r)

    def test_survives_errors_and_never_raises(self):
        def boom(*a, **k):
            raise RuntimeError("nope")
        self._patch(boom, boom)
        r = mm.stop_gpu_models(kill_processes=True)
        self.assertEqual(r["status"], "OK", r)   # collected, not raised
        self.assertEqual(len(r["errors"]), 4, r)  # 2 failed unloads + 2 failed kills
        self.assertEqual(r["total"], 0, r)


def setattr_all(module, orig):
    module.ollama_loaded, module.gpu_processes, module._ollama_unload, module._kill_pid = orig


class GpuProcessFilterTest(unittest.TestCase):
    """gpu_processes() filters the WDDM firehose to AI executables and drops the monitor's own pid --
    verified with a canned nvidia-smi CSV (no live GPU)."""

    def test_filters_to_ai_exes_and_excludes_self(self):
        from types import SimpleNamespace

        csv = (
            "5132, C:\\Windows\\explorer.exe, [N/A]\n"
            "27080, C:\\Users\\d\\Ollama\\lib\\ollama\\llama-server.exe, 1333\n"
            "44100, D:\\dev\\Python\\venvs\\kaizen\\Scripts\\python.exe, [N/A]\n"
            "9999, C:\\other\\python.exe, [N/A]\n"          # own pid -> excluded
            "3000, C:\\Program Files\\Discord\\Discord.exe, [N/A]\n"
        )
        original = mm.subprocess.run
        mm.subprocess.run = lambda *a, **k: SimpleNamespace(returncode=0, stdout=csv)
        try:
            out = mm.gpu_processes(exclude_pid=9999)
        finally:
            mm.subprocess.run = original
        self.assertTrue(out["available"])
        names = {p["name"] for p in out["procs"]}
        self.assertEqual(names, {"llama-server.exe", "python.exe"}, out)  # explorer/Discord/self dropped
        by_pid = {p["pid"]: p for p in out["procs"]}
        self.assertEqual(by_pid[27080]["kind"], "ollama", out)
        self.assertEqual(by_pid[27080]["vram_mb"], 1333, out)
        self.assertEqual(by_pid[44100]["kind"], "torch/python", out)
        self.assertIsNone(by_pid[44100]["vram_mb"], out)  # [N/A] -> None


class GpuPerProcessTest(unittest.TestCase):
    """gpu_per_process() parses typeperf PDH-CSV: sum Dedicated Usage across LUIDs, max Utilization
    across engines, skip the -1 invalid-sample sentinel. Verified with a canned CSV (no live GPU)."""

    def test_parses_pdh_csv(self):
        from types import SimpleNamespace

        pdh = (
            '"(PDH-CSV 4.0)",'
            '"\\\\H\\GPU Process Memory(pid_100_luid_0x0_0x1)\\Dedicated Usage",'
            '"\\\\H\\GPU Process Memory(pid_100_luid_0x0_0x2)\\Dedicated Usage",'
            '"\\\\H\\GPU Engine(pid_100_luid_0x0_0x1_eng_0_engtype_3D)\\Utilization Percentage",'
            '"\\\\H\\GPU Engine(pid_100_luid_0x0_0x1_eng_1_engtype_Compute)\\Utilization Percentage",'
            '"\\\\H\\GPU Process Memory(pid_200_luid_0x0_0x1)\\Dedicated Usage"\n'
            '"07/06/2026 22:27:00.000","1048576.000000","2097152.000000","10.000000","55.500000","-1.000000"\n'
        )
        original = mm.subprocess.run
        mm.subprocess.run = lambda *a, **k: SimpleNamespace(returncode=0, stdout=pdh)
        try:
            out = mm.gpu_per_process()
        finally:
            mm.subprocess.run = original
        self.assertEqual(out[100]["gpu_mem_mb"], 3.0, out)     # (1048576+2097152)/1024/1024, summed LUIDs
        self.assertEqual(out[100]["gpu_util_pct"], 55.5, out)  # max(10.0, 55.5) across engines
        self.assertNotIn(200, out, out)                        # only -1 sentinel -> dropped

    def test_non_pdh_output_returns_empty(self):
        from types import SimpleNamespace

        original = mm.subprocess.run
        mm.subprocess.run = lambda *a, **k: SimpleNamespace(returncode=0, stdout="not a counter set")
        try:
            self.assertEqual(mm.gpu_per_process(), {})
        finally:
            mm.subprocess.run = original


class ProbeMergeTest(unittest.TestCase):
    """_probe_lanes merges each configured backend's probe() -- exercised with fakes so no weights
    load (the live --probe path is the only one that loads models; here we prove the merge logic)."""

    def test_probe_merges_real_device_for_configured_lanes(self):
        class _Fake:
            def probe(self):
                return {"device": "cuda:0", "model": "fake"}

        originals = (mm.get_embedding_backend, mm.get_rerank_backend)
        mm.get_embedding_backend = lambda: _Fake()
        mm.get_rerank_backend = lambda: _Fake()
        try:
            lanes = [
                {"lane": "embed", "configured": True, "backend": "sentence-transformers", "device": "auto"},
                {"lane": "rerank", "configured": False},
            ]
            mm._probe_lanes(lanes)
        finally:
            mm.get_embedding_backend, mm.get_rerank_backend = originals
        self.assertTrue(lanes[0]["reachable"])
        self.assertEqual(lanes[0]["probe"]["device"], "cuda:0")
        self.assertNotIn("probe", lanes[1])  # unconfigured lane is skipped


class HoldProbeTest(unittest.TestCase):
    """_probe_hold loads every configured backend and keeps a reference so its weights stay resident.
    Exercised with fakes + hold=0 (no sleep) and stubbed GPU/Ollama reads -- nothing loads, no network."""

    def test_hold_probes_configured_lanes_and_reports_held(self):
        from types import SimpleNamespace

        class _Fake:
            def probe(self):
                return {"device": "cuda:0", "model": "fake"}

        orig = (mm.describe_backends, mm.get_embedding_backend, mm.get_rerank_backend,
                mm.gpu_snapshot, mm.gpu_processes, mm.ollama_loaded, mm.recent_activity)
        mm.describe_backends = lambda: [
            {"lane": "embed", "configured": True, "backend": "sentence-transformers", "device": "auto"},
            {"lane": "rerank", "configured": True, "backend": "sentence-transformers", "device": "auto"},
            {"lane": "text", "configured": False},
            {"lane": "pii", "configured": False},
        ]
        mm.get_embedding_backend = lambda: _Fake()
        mm.get_rerank_backend = lambda: _Fake()
        mm.gpu_snapshot = lambda: {"available": False}
        mm.gpu_processes = lambda exclude_pid=None: {"available": False, "procs": []}
        mm.ollama_loaded = lambda: {"reachable": False, "loaded": []}
        mm.recent_activity = lambda limit: []
        try:
            out = mm._probe_hold(SimpleNamespace(limit=5), hold=0, interval=2.0, as_json=True)
        finally:
            (mm.describe_backends, mm.get_embedding_backend, mm.get_rerank_backend,
             mm.gpu_snapshot, mm.gpu_processes, mm.ollama_loaded, mm.recent_activity) = orig
        self.assertEqual(out["status"], "OK", out)
        self.assertEqual(out["held"], 2, out)  # embed + rerank probed and held; text/pii off
        lanes = {b["lane"]: b for b in out["backends"]}
        self.assertTrue(lanes["embed"]["reachable"], out)
        self.assertEqual(lanes["embed"]["probe"]["device"], "cuda:0", out)

    def test_hold_release_unloads_ollama_models_on_exit(self):
        """The finally-release evicts every Ollama model the probe loaded (so VRAM frees when the hold
        ends / the window closes), and bounds keep-alive on load as a hard-kill backstop."""
        from types import SimpleNamespace

        class _Fake:
            model = "qwen"

            def probe(self):
                return {"model": "qwen", "endpoint": "http://127.0.0.1:11434"}

        unloaded, keepalive = [], []
        orig = (mm.describe_backends, mm.get_text_backend, mm.gpu_snapshot, mm.gpu_processes,
                mm.ollama_loaded, mm.recent_activity, mm._ollama_unload, mm._ollama_set_keepalive)
        mm.describe_backends = lambda: [
            {"lane": "text", "configured": True, "backend": "ollama", "model": "qwen",
             "transport": "http://127.0.0.1:11434/v1", "device": "server"},
            {"lane": "embed", "configured": False}, {"lane": "rerank", "configured": False},
            {"lane": "pii", "configured": False},
        ]
        mm.get_text_backend = lambda: _Fake()
        mm.gpu_snapshot = lambda: {"available": False}
        mm.gpu_processes = lambda exclude_pid=None: {"available": False, "procs": []}
        mm.ollama_loaded = lambda: {"reachable": False, "loaded": []}
        mm.recent_activity = lambda limit: []
        mm._ollama_unload = lambda root, model: unloaded.append((root, model))
        mm._ollama_set_keepalive = lambda root, model, secs: keepalive.append((root, model, secs))
        try:
            out = mm._probe_hold(SimpleNamespace(limit=5), hold=0, interval=2.0, as_json=True)
        finally:
            (mm.describe_backends, mm.get_text_backend, mm.gpu_snapshot, mm.gpu_processes,
             mm.ollama_loaded, mm.recent_activity, mm._ollama_unload, mm._ollama_set_keepalive) = orig
        self.assertEqual(out["held"], 1, out)
        self.assertEqual(unloaded, [("http://127.0.0.1:11434", "qwen")], out)   # evicted on release
        self.assertEqual(keepalive, [("http://127.0.0.1:11434", "qwen", 16)], out)  # hold(1)+15 backstop


if __name__ == "__main__":
    unittest.main()
