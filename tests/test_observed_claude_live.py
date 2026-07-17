"""Live observed-Claude acceptance (plan v3 "Live acceptance" / H2.5 leg deferred to H2.7).

Gated OFF by default: @skipUnless(KAIZEN_RUN_LIVE == "1" and shutil.which("claude")). The real flow,
end to end: a REAL daemon subprocess bound to a fresh D-drive plane, the REAL ``hooks install
--mode hooked-observe`` verb writing the plane's .claude/settings.local.json, then a REAL post-install
``claude -p`` session (two prompts, one Read tool decision) ended and RESUMED. Assertions read the
plane DB + the live ``session list`` aggregation: one observed C1, two ordered linked T5 lifecycles,
complete final messages, the tool decision, one finalization per lifecycle -- no transcript parsing.

Plane mechanics: the plane is git-init'ed so Claude Code treats it as its own project root (the plane
nests inside this repo; without a boundary the ENCLOSING repo's settings would apply -- the same
nesting hazard g_20260710191602 recorded for relative write targets). The installed hook command
embeds the plane-rooted shim path, so the real shim file is copied to <plane>/kaizen_components/orchestration/ and
the claude child gets PYTHONPATH=<real repo> (the shim imports kaizen_components) plus
KAIZEN_REPO_ROOT=<plane> (the shim contacts the PLANE daemon and records to the PLANE DB).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import KAIZEN, REPO_ROOT, kaizen  # noqa: E402
from test_session_drive import _rmtree  # noqa: E402

_LIVE = os.environ.get("KAIZEN_RUN_LIVE") == "1"
_CLAUDE = shutil.which("claude")
_SCRATCH_PARENT = REPO_ROOT / "AI/work/harness-ui-v1"


@unittest.skipUnless(
    _LIVE and _CLAUDE,
    "live observed-Claude acceptance -- set KAIZEN_RUN_LIVE=1 with the real claude CLI installed",
)
class LiveObservedClaudeAcceptanceTest(unittest.TestCase):
    """Fresh post-install observed capture: two prompts, one tool decision, end + resume, one C1."""

    def setUp(self) -> None:
        _SCRATCH_PARENT.mkdir(parents=True, exist_ok=True)
        self.plane = Path(tempfile.mkdtemp(prefix="h2.7-observed-", dir=str(_SCRATCH_PARENT)))
        self.addCleanup(_rmtree, self.plane)
        rc, payload = kaizen(self.plane, "K1")
        self.assertEqual(rc, 0, payload)
        # Its own git root: Claude Code must resolve the PLANE as the project, not the enclosing repo.
        git = shutil.which("git")
        self.assertIsNotNone(git, "git required to isolate the plane as its own project root")
        subprocess.run([git, "init", "-q", str(self.plane)], check=True, capture_output=True, timeout=30)
        # The install-written hook command embeds <plane>/kaizen_components/orchestration/claude_hook_shim.py.
        shim_src = REPO_ROOT / "kaizen_components" / "orchestration" / "claude_hook_shim.py"
        shim_dst = self.plane / "kaizen_components" / "orchestration" / "claude_hook_shim.py"
        shim_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(shim_src, shim_dst)
        (self.plane / "notes.txt").write_text("kaizen observed acceptance corpus\n", encoding="utf-8")
        self._daemon: subprocess.Popen | None = None

    def tearDown(self) -> None:
        if self._daemon and self._daemon.poll() is None:
            self._daemon.terminate()
            try:
                self._daemon.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._daemon.kill()
                self._daemon.wait(timeout=5)

    def _plane_env(self, **extra: str) -> dict[str, str]:
        env = dict(os.environ)
        env["KAIZEN_REPO_ROOT"] = str(self.plane)
        env.update(extra)
        return env

    def _kz(self, *args: str) -> dict:
        proc = subprocess.run(
            [sys.executable, str(KAIZEN), *args, "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(REPO_ROOT), env=self._plane_env(), timeout=60,
        )
        raw = proc.stdout.strip() or proc.stderr.strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            self.fail(f"non-JSON from kaizen {' '.join(args)} rc={proc.returncode}: {raw[:800]}")

    def _start_daemon(self) -> None:
        self._daemon = subprocess.Popen(
            [sys.executable, str(KAIZEN), "daemon", "run"],
            cwd=str(REPO_ROOT), env=self._plane_env(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 45.0
        while time.monotonic() < deadline:
            status = self._kz("daemon", "status")
            if status.get("running"):
                return
            self.assertIsNone(self._daemon.poll(), "daemon exited during boot")
            time.sleep(0.5)
        self.fail("daemon did not report running within 45s")

    def _claude(self, *args: str, timeout: float = 300.0) -> dict:
        # Nesting scrub (probed 2026-07-10): a claude child spawned from inside a Claude Code session
        # inherits CLAUDECODE/CLAUDE_CODE_*/CLAUDE_AGENT_SDK* markers that distort its behavior.
        env = self._plane_env(PYTHONPATH=str(REPO_ROOT))
        for key in [k for k in env if k.startswith(("CLAUDECODE", "CLAUDE_CODE_", "CLAUDE_AGENT_SDK"))]:
            env.pop(key, None)
        proc = subprocess.run(
            [_CLAUDE, *args, "--output-format", "json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(self.plane), env=env, timeout=timeout, stdin=subprocess.DEVNULL,
        )
        self.assertEqual(proc.returncode, 0, f"claude failed: {proc.stderr[-1500:]}")
        # --output-format json emits ONE line: a JSON array of message objects; the terminal element
        # of interest is the type=="result" object (probed on claude 2.1.195).
        data = json.loads(proc.stdout.strip())
        if isinstance(data, list):
            payload = next((x for x in data if isinstance(x, dict) and x.get("type") == "result"), None)
            self.assertIsNotNone(payload, data)
        else:
            payload = data
        self.assertEqual(payload.get("subtype"), "success", payload)
        return payload

    def _read_plane(self, query: str, args: tuple = ()) -> list:
        script = (
            "import json, sys\n"
            "from kaizen_components import db\n"
            "rows = db.fetch_all(" + repr(query) + ", " + repr(args) + ")\n"
            "print('ROWS ' + json.dumps([list(r) for r in rows], ensure_ascii=True))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(REPO_ROOT), env=self._plane_env(), timeout=60,
        )
        for line in proc.stdout.splitlines():
            if line.startswith("ROWS "):
                return json.loads(line[len("ROWS "):])
        self.fail(f"plane read failed rc={proc.returncode}: {proc.stderr[-800:]}")

    def test_fresh_post_install_session_resume_yields_one_c1_with_linked_runs(self) -> None:
        self._start_daemon()

        installed = self._kz("daemon", "hooks", "install", "--mode", "hooked-observe")
        self.assertEqual(installed.get("status"), "OK", installed)
        self.assertTrue(installed.get("installed"), installed)
        self.assertTrue(str(installed.get("path", "")).startswith(str(self.plane)), installed)
        verify = self._kz("daemon", "hooks", "verify")
        self.assertTrue(verify.get("installed"), verify)
        self.assertTrue(verify.get("shim_present"), verify)
        self.assertFalse(verify.get("tracked"), verify)

        first = self._claude(
            "-p", "Use the Read tool to read the file notes.txt in this directory, then reply with exactly INGESTED."
        )
        vendor_sid = first.get("session_id")
        self.assertTrue(vendor_sid, first)
        self.assertIn("INGESTED", str(first.get("result", "")), first)

        # --resume needs the equals form: with a space, the optional-value flag leaves the id unbound
        # and the id becomes the prompt (probed on claude 2.1.195). Resume PRESERVES the session id.
        second = self._claude("-p", "--resume=" + str(vendor_sid), "Reply with exactly RESUMED.")
        self.assertIn("RESUMED", str(second.get("result", "")), second)
        self.assertEqual(second.get("session_id"), vendor_sid, second)

        # Both lifecycles ended (SessionEnd fired at each claude exit): poll for two finalizations.
        deadline = time.monotonic() + 30.0
        finals: list = []
        while time.monotonic() < deadline:
            finals = self._read_plane(
                "SELECT agent_run_id FROM agent_events WHERE event_kind = 'finalization'"
            )
            if len(finals) >= 2:
                break
            time.sleep(1.0)
        self.assertEqual(len(finals), 2, finals)

        listed = self._kz("daemon", "session", "list", "--controller", "observed")
        self.assertEqual(listed.get("status"), "OK", listed)
        self.assertEqual(len(listed.get("sessions", [])), 1, listed)
        self.assertEqual(len(listed["sessions"][0].get("runs", [])), 2, listed)

        sessions = self._read_plane(
            "SELECT id, controller, engine, auth_mode, vendor_session_root_id FROM agent_sessions"
        )
        self.assertEqual(len(sessions), 1, sessions)
        self.assertEqual(sessions[0][1:], ["observed", "claude", "none", str(vendor_sid)], sessions)

        runs = self._read_plane(
            "SELECT id, session_id FROM agent_runs ORDER BY created_at, id"
        )
        self.assertEqual(len(runs), 2, runs)
        self.assertEqual({row[1] for row in runs}, {sessions[0][0]}, runs)

        chat = [json.loads(row[0]) for row in self._read_plane(
            "SELECT body FROM agent_events WHERE event_kind = 'chat_message' ORDER BY created_at, sequence_no"
        )]
        roles = [m.get("role") for m in chat]
        self.assertEqual(roles.count("user"), 2, chat)
        self.assertEqual(roles.count("assistant"), 2, chat)
        self.assertTrue(all(m.get("source") == "observed" for m in chat), chat)
        assistant_text = " ".join(m.get("text", "") for m in chat if m.get("role") == "assistant")
        self.assertIn("INGESTED", assistant_text, chat)
        self.assertIn("RESUMED", assistant_text, chat)

        tools = self._read_plane(
            "SELECT correlation_id FROM agent_events WHERE event_kind = 'hook_event' AND correlation_id IS NOT NULL"
        )
        self.assertGreaterEqual(len(tools), 1, "no PreToolUse tool decision was captured")


if __name__ == "__main__":
    unittest.main()
