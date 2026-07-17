"""Remote dispatch + cross-machine attach (v8 M14, plan §B.5, gate F4).

Layered like the fleet suite:

- ENGINE + APPLY legs run an in-process ``FleetStore(db_path=<scratch>, node=<injected>)`` (the
  test_control_http idiom): construction + append + the reducer touch ONLY fleet.db + git, never
  kaizen.db, so no real plane is mutated and no node_identity.json is minted.
- attach_session / approval / D7-CLI / supervisor legs need kaizen.db, so they run in a SUBPROCESS
  pinned to a fresh scratch KAIZEN_REPO_ROOT (test_fleet_coordination / test_supervisor convention:
  paths.REPO_ROOT is import-frozen, so a subprocess carries its own plane and nothing leaks).

The EXIT lab ("a second scratch node runs a dispatched refactor; the laptop approves over the ledger")
is a SINGLE-MACHINE two-plane lab per the owner's locked decision: two node identities against ONE
shared fleet.db file (the sync-server stand-in -- documented in that test's docstring), with the
dispatcher's C4 approval seeded in a scratch kaizen.db.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import REPO_ROOT, IsolatedDBTest, kaizen  # noqa: E402

from kaizen_components.denials import KaizenDenied  # noqa: E402
from kaizen_components.fleet import coordination, dispatch_remote, reducers  # noqa: E402
from kaizen_components.fleet.store import FleetStore  # noqa: E402

GIT = shutil.which("git")


def _node(node_id: str) -> dict:
    """A minimal injected node identity dict (no seed -> the byte-identical unsigned append path, no
    PyNaCl, no node_identity.json)."""
    return {"node_id": node_id, "created_at": "2026-01-01T00:00:00+00:00"}


def _held_lease(store: FleetStore, scope_key: str, holder: str) -> None:
    """Make ``store``'s node the coordinator and grant ``holder`` a lease on ``scope_key`` (the dispatch
    lease gate is on a GRANTED holder)."""
    coordination.claim_coordinator(store, summary="claim")
    coordination.grant_lease(store, scope_key, holder, summary="grant")


def _run_git(*args: str) -> subprocess.CompletedProcess[str]:
    """Run Git with bounded execution and fail with complete diagnostics."""
    result = subprocess.run([GIT, *args], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise AssertionError(
            f"Git failed: argv={result.args!r}; returncode={result.returncode}; "
            f"stdout={result.stdout!r}; stderr={result.stderr!r}"
        )
    return result


def _git(repo: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run Git against the supplied repository with checked captured output."""
    return _run_git("-C", repo, *args)


def _scratch_git_repo() -> str:
    repo = tempfile.mkdtemp(prefix="kz-rd-repo-")
    _run_git("init", "-q", repo)
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(repo, "config", k, v)
    Path(repo, "a.txt").write_text("line one\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _scratch_runner(repo: str):
    """A git runner bound to ``repo`` (coordination._default_git_runner shape) for apply legs."""
    return coordination._default_git_runner(repo)


# --- in-process scratch store base ---------------------------------------------------------------

class _StoreCase(unittest.TestCase):
    """A scratch in-process FleetStore over a temp fleet.db with an injected node identity."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-rd-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def store(self, node_id: str, *, db_path: Path | None = None) -> FleetStore:
        """Create an auto-closed scratch FleetStore with a stable node identity."""
        s = FleetStore(db_path=db_path or (self.root / "fleet.db"), node=_node(node_id))
        self.addCleanup(s.close)
        return s


# --- engine: full lifecycle ----------------------------------------------------------------------

class LifecycleHappyPathTest(_StoreCase):
    def test_request_accept_start_complete_advances_events_and_row(self) -> None:
        # The dispatcher (nA) holds the lease and dispatches to the TARGET (nB) -- but request/accept/etc
        # are node-scoped, so we drive both roles with distinct node identities against the SAME db file.
        dispatcher = self.store("nA000000000001")
        _held_lease(dispatcher, "p/main", "nA000000000001")
        req = dispatch_remote.request_dispatch(
            dispatcher, target_node="nB000000000001", task="refactor", scope_key="p/main"
        )
        self.assertEqual(req["status"], "OK")
        did = req["dispatch_id"]
        # remote_dispatches row landed with status requested + target.
        row = dispatcher._read_all("SELECT target_node_id, status FROM remote_dispatches WHERE id = ?", (did,))[0]
        self.assertEqual(row, ("nB000000000001", "requested"))

        # The TARGET node (same db file, distinct identity) accepts -> starts -> completes.
        target = FleetStore(db_path=self.root / "fleet.db", node=_node("nB000000000001"))
        self.addCleanup(target.close)
        self.assertEqual(dispatch_remote.accept_dispatch(target, did)["state"], "accepted")
        self.assertEqual(dispatch_remote.start_dispatch(target, did)["state"], "started")
        done = dispatch_remote.complete_dispatch(target, did, artifact_name="d.patch", artifact_sha="deadbeef", branch="kz/x")
        self.assertEqual(done["state"], "completed")

        # Row status advanced to completed; the reducer agrees + carries the artifact.
        status = target._read_all("SELECT status FROM remote_dispatches WHERE id = ?", (did,))[0][0]
        self.assertEqual(status, "completed")
        reduced = dispatch_remote.current_dispatches(target)[did]
        self.assertEqual(reduced["state"], "completed")
        self.assertEqual(reduced["artifact"], {"artifact": "d.patch", "sha": "deadbeef", "branch": "kz/x"})
        # The dispatch event trail is present in order.
        markers = [e["marker"] for e in target.coord_events() if e["event_kind"] == "dispatch"]
        self.assertEqual(markers, ["requested", "accepted", "started", "completed"])


class DispatchUnleasedTest(_StoreCase):
    def test_request_without_lease_refused(self) -> None:
        # EXIT: dispatch WITHOUT holding the granted lease refuses DENIED_DISPATCH_UNLEASED.
        dispatcher = self.store("nA000000000001")  # no lease held
        with self.assertRaises(KaizenDenied) as ctx:
            dispatch_remote.request_dispatch(dispatcher, target_node="nB", task="t", scope_key="p/main")
        self.assertEqual(ctx.exception.code, "DENIED_DISPATCH_UNLEASED")
        self.assertEqual(ctx.exception.exit_code, 2)

    def test_request_when_another_node_holds_refused(self) -> None:
        # The scope is held by a DIFFERENT node -> this node cannot dispatch it.
        dispatcher = self.store("nA000000000001")
        _held_lease(dispatcher, "p/main", "nOTHER00000001")  # grant to someone else
        with self.assertRaises(KaizenDenied) as ctx:
            dispatch_remote.request_dispatch(dispatcher, target_node="nB", task="t", scope_key="p/main")
        self.assertEqual(ctx.exception.code, "DENIED_DISPATCH_UNLEASED")


class NotTargetTest(_StoreCase):
    def test_accept_by_non_target_refused(self) -> None:
        dispatcher = self.store("nA000000000001")
        _held_lease(dispatcher, "p/main", "nA000000000001")
        did = dispatch_remote.request_dispatch(dispatcher, target_node="nB000000000001", task="t", scope_key="p/main")["dispatch_id"]
        # A THIRD node (not the target) tries to accept -> DENIED_DISPATCH_NOT_TARGET.
        stranger = FleetStore(db_path=self.root / "fleet.db", node=_node("nC000000000001"))
        self.addCleanup(stranger.close)
        with self.assertRaises(KaizenDenied) as ctx:
            dispatch_remote.accept_dispatch(stranger, did)
        self.assertEqual(ctx.exception.code, "DENIED_DISPATCH_NOT_TARGET")


class IllegalTransitionTest(_StoreCase):
    def test_complete_before_accept_refused(self) -> None:
        dispatcher = self.store("nA000000000001")
        _held_lease(dispatcher, "p/main", "nA000000000001")
        did = dispatch_remote.request_dispatch(dispatcher, target_node="nB000000000001", task="t", scope_key="p/main")["dispatch_id"]
        target = FleetStore(db_path=self.root / "fleet.db", node=_node("nB000000000001"))
        self.addCleanup(target.close)
        # complete before accept/start -> DENIED_DISPATCH_STATE (requested -> completed is illegal).
        with self.assertRaises(KaizenDenied) as ctx:
            dispatch_remote.complete_dispatch(target, did, artifact_name="x.patch")
        self.assertEqual(ctx.exception.code, "DENIED_DISPATCH_STATE")
        self.assertEqual(ctx.exception.exit_code, 2)

    def test_cancel_of_terminal_refused(self) -> None:
        dispatcher = self.store("nA000000000001")
        _held_lease(dispatcher, "p/main", "nA000000000001")
        did = dispatch_remote.request_dispatch(dispatcher, target_node="nB000000000001", task="t", scope_key="p/main")["dispatch_id"]
        target = FleetStore(db_path=self.root / "fleet.db", node=_node("nB000000000001"))
        self.addCleanup(target.close)
        dispatch_remote.accept_dispatch(target, did)
        dispatch_remote.start_dispatch(target, did)
        dispatch_remote.fail_dispatch(target, did, "boom")
        # canceling a failed (terminal) dispatch is refused.
        with self.assertRaises(KaizenDenied) as ctx:
            dispatch_remote.cancel_dispatch(target, did, reason="late")
        self.assertEqual(ctx.exception.code, "DENIED_DISPATCH_STATE")


class ReducerDeterminismTest(unittest.TestCase):
    """Pure reducer: terminal-wins + shuffle determinism (no DB, no store)."""

    _EVENTS = [
        {"id": "e1", "created_at": "t1", "event_kind": "dispatch", "marker": "requested",
         "scope_key": "p/m", "payload": {"dispatch_id": "d1", "target_node": "nB", "origin_node": "nA"}},
        {"id": "e2", "created_at": "t2", "event_kind": "dispatch", "marker": "accepted", "payload": {"dispatch_id": "d1"}},
        {"id": "e3", "created_at": "t3", "event_kind": "dispatch", "marker": "started", "payload": {"dispatch_id": "d1"}},
        {"id": "e4", "created_at": "t4", "event_kind": "dispatch", "marker": "completed",
         "payload": {"dispatch_id": "d1", "artifact": "d1.patch", "sha": "abc123", "branch": "kz/b"}},
        # A stray LATER-created requested (a duplicate/reorder) must NOT reopen the completed dispatch.
        {"id": "e5", "created_at": "t99", "event_kind": "dispatch", "marker": "requested",
         "payload": {"dispatch_id": "d1", "target_node": "nB"}},
        # A second, independent dispatch that only ever reached 'accepted'.
        {"id": "f1", "created_at": "t5", "event_kind": "dispatch", "marker": "requested",
         "scope_key": "p/f", "payload": {"dispatch_id": "d2", "target_node": "nC", "origin_node": "nA"}},
        {"id": "f2", "created_at": "t6", "event_kind": "dispatch", "marker": "accepted", "payload": {"dispatch_id": "d2"}},
    ]

    def test_terminal_wins_and_shuffle_determinism(self) -> None:
        import random

        ref = reducers.reduce_dispatches(self._EVENTS)
        self.assertEqual(ref["d1"]["state"], "completed")  # terminal-wins over the stray later requested
        self.assertEqual(ref["d1"]["artifact"], {"artifact": "d1.patch", "sha": "abc123", "branch": "kz/b"})
        self.assertEqual(ref["d2"]["state"], "accepted")
        self.assertEqual(ref["d1"]["origin_node"], "nA")
        self.assertEqual(ref["d1"]["target_node"], "nB")
        for seed in range(250):
            doubled = list(self._EVENTS) + [e for e in self._EVENTS if random.Random(seed).random() < 0.5]
            random.Random(seed + 1).shuffle(doubled)
            self.assertEqual(reducers.reduce_dispatches(doubled), ref, f"non-deterministic at seed {seed}")


class CompletePayloadRedactionTest(_StoreCase):
    def test_completed_carries_basename_only_no_abs_path(self) -> None:
        # A completion given an ABSOLUTE artifact path must record ONLY the basename in the SYNCED event
        # (the redaction pass-path -- an absolute/home path would trip user_home_path).
        dispatcher = self.store("nA000000000001")
        _held_lease(dispatcher, "p/main", "nA000000000001")
        did = dispatch_remote.request_dispatch(dispatcher, target_node="nB000000000001", task="t", scope_key="p/main")["dispatch_id"]
        target = FleetStore(db_path=self.root / "fleet.db", node=_node("nB000000000001"))
        self.addCleanup(target.close)
        dispatch_remote.accept_dispatch(target, did)
        dispatch_remote.start_dispatch(target, did)
        abs_path = str(self.root / "dispatch-artifacts" / f"{did}.patch")
        dispatch_remote.complete_dispatch(target, did, artifact_name=abs_path, artifact_sha="sha1")
        completed = [e for e in target.coord_events() if e["event_kind"] == "dispatch" and e["marker"] == "completed"][-1]
        # The synced payload carries the basename only -- never the absolute path.
        self.assertEqual(completed["payload"]["artifact"], f"{did}.patch")
        self.assertNotIn(str(self.root), json.dumps(completed["payload"]))


# --- apply (needs an approval; real git apply on a scratch repo) ---------------------------------

@unittest.skipIf(GIT is None, "git not available")
class ApplyTest(_StoreCase):
    def _requested_dispatch(self) -> tuple[FleetStore, str]:
        dispatcher = self.store("nA000000000001")
        _held_lease(dispatcher, "p/main", "nA000000000001")
        did = dispatch_remote.request_dispatch(dispatcher, target_node="nB000000000001", task="t", scope_key="p/main")["dispatch_id"]
        return dispatcher, did

    def _real_patch(self, repo: str) -> str:
        """Build a real unified diff against ``repo`` (edit a.txt, `git diff`, revert), returning the
        patch text -- the kind a dispatched refactor returns."""
        Path(repo, "a.txt").write_text("line one\nline two added\n", encoding="utf-8")
        diff = _git(repo, "diff").stdout
        _git(repo, "checkout", "--", "a.txt")  # restore clean tree; apply_dispatch re-applies the diff
        return diff

    def test_apply_unapproved_refused_tree_untouched(self) -> None:
        dispatcher, did = self._requested_dispatch()
        repo = _scratch_git_repo()
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        patch_text = self._real_patch(repo)
        patch_file = Path(repo, "change.patch")
        patch_file.write_text(patch_text, encoding="utf-8")
        # approvals_lookup returns a NON-approved record -> DENIED_DISPATCH_APPLY_UNAPPROVED, tree clean.
        with self.assertRaises(KaizenDenied) as ctx:
            dispatch_remote.apply_dispatch(
                dispatcher, _scratch_runner(repo), did, artifact_path=str(patch_file),
                approval_id="apr1", approvals_lookup=lambda _id: {"id": "apr1", "state": "open"},
            )
        self.assertEqual(ctx.exception.code, "DENIED_DISPATCH_APPLY_UNAPPROVED")
        self.assertEqual(Path(repo, "a.txt").read_text(encoding="utf-8"), "line one\n")  # untouched

    def test_apply_approved_applies_patch_and_records_resolution(self) -> None:
        dispatcher, did = self._requested_dispatch()
        repo = _scratch_git_repo()
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        patch_text = self._real_patch(repo)
        patch_file = Path(repo, "change.patch")
        patch_file.write_text(patch_text, encoding="utf-8")
        res = dispatch_remote.apply_dispatch(
            dispatcher, _scratch_runner(repo), did, artifact_path=str(patch_file),
            approval_id="apr1", approvals_lookup=lambda _id: {"id": "apr1", "state": "approved"},
        )
        self.assertEqual(res["status"], "OK")
        self.assertTrue(res["applied"])
        # The file content actually changed (the patch applied).
        self.assertIn("line two added", Path(repo, "a.txt").read_text(encoding="utf-8"))
        # A resolution/recorded {applied: true, basename, approval_id} was appended.
        recorded = [e for e in dispatcher.coord_events() if e["event_kind"] == "resolution" and e["marker"] == "recorded"]
        self.assertTrue(recorded)
        self.assertTrue(recorded[-1]["payload"]["applied"])
        self.assertEqual(recorded[-1]["payload"]["artifact"], "change.patch")
        self.assertEqual(recorded[-1]["payload"]["approval_id"], "apr1")

    def test_apply_corrupt_patch_fails_tree_clean(self) -> None:
        dispatcher, did = self._requested_dispatch()
        repo = _scratch_git_repo()
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        # A corrupt patch (garbage) -> git apply --check fails -> DENIED_DISPATCH_APPLY_FAILED, tree clean.
        patch_file = Path(repo, "bad.patch")
        patch_file.write_text("this is not a valid unified diff at all\n@@ nonsense @@\n", encoding="utf-8")
        with self.assertRaises(KaizenDenied) as ctx:
            dispatch_remote.apply_dispatch(
                dispatcher, _scratch_runner(repo), did, artifact_path=str(patch_file),
                approval_id="apr1", approvals_lookup=lambda _id: {"id": "apr1", "state": "approved"},
            )
        self.assertEqual(ctx.exception.code, "DENIED_DISPATCH_APPLY_FAILED")
        self.assertEqual(Path(repo, "a.txt").read_text(encoding="utf-8"), "line one\n")  # untouched

    def test_apply_uses_end_of_options_before_untrusted_artifact_path(self) -> None:
        dispatcher, did = self._requested_dispatch()
        calls: list[tuple[str, ...]] = []

        def fake_git(*args):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        dispatch_remote.apply_dispatch(
            dispatcher, fake_git, did, artifact_path="--unsafe-paths",
            approval_id="apr1", approvals_lookup=lambda _id: {"id": "apr1", "state": "approved"},
        )
        self.assertEqual(calls, [
            ("apply", "--check", "--", "--unsafe-paths"),
            ("apply", "--", "--unsafe-paths"),
        ])


# --- attach_session + decide_approval (scratch kaizen.db subprocess) -----------------------------

_KZDB_PREAMBLE = r"""
import json, os, subprocess, sys
from kaizen_components import db, session_records
from kaizen_components.denials import KaizenDenied

def checked_git(git, *args):
    result = subprocess.run([git, *args], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(
            f"Git failed: argv={result.args!r}; returncode={result.returncode}; "
            f"stdout={result.stdout!r}; stderr={result.stderr!r}"
        )
    return result

def new_session(**cols):
    sid = db.new_id("as")
    now = db.now()
    keys = ["id", "created_at", "controller", "mode", "auth_mode", "state", "summary", "content_hash", "is_test"]
    vals = {"id": sid, "created_at": now, "controller": "kaizen", "mode": "orchestrate",
            "auth_mode": "none", "state": "open", "summary": "s", "content_hash": "h", "is_test": 1}
    extra_keys = [k for k in cols if k not in keys]
    keys = keys + extra_keys
    vals.update(cols)
    def op(conn, _a):
        conn.execute("INSERT INTO agent_sessions (" + ",".join(keys) + ") VALUES (" + ",".join("?" for _ in keys) + ")",
                     tuple(vals[k] for k in keys))
    db.write_tx(op)
    return sid

def new_approval(session_id, state="open"):
    aid = db.new_id("apr")
    now = db.now()
    def op(conn, _a):
        conn.execute("INSERT INTO approval_requests (id, created_at, updated_at, session_id, request_type, state, summary, content_hash, is_test) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", (aid, now, now, session_id, "tool_approval", state, "approve me", "h", 1))
    db.write_tx(op)
    return aid

out = None
try:
    exec(BODY)
    print("RESULT " + json.dumps(out))
except KaizenDenied as e:
    print("RESULT " + json.dumps({"_denied": e.code, "_exit": e.exit_code}))
"""


class _KzdbSubprocess(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-rd-kzdb-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.assertEqual(kaizen(self.root, "K1")[0], 0)

    def plane(self, body: str, *, env: dict | None = None) -> dict:
        """Execute a body against a scratch kaizen.db plane and return its RESULT or structured denial."""
        script = "BODY = " + repr(body) + "\n" + _KZDB_PREAMBLE
        full_env = dict(os.environ)
        full_env["KAIZEN_REPO_ROOT"] = str(self.root)
        if env:
            full_env.update(env)
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            cwd=str(REPO_ROOT), env=full_env, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
        for line in proc.stdout.splitlines():
            if line.startswith("RESULT "):
                return json.loads(line[len("RESULT "):])
        self.fail(f"no RESULT.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")


class AttachSessionTest(_KzdbSubprocess):
    def test_stale_epoch_attach_refused(self) -> None:
        # EXIT: a fenced session (owning_node/node_epoch set); attach with expected_node_epoch = old-1
        # refuses DENIED_STALE_FENCE (from the concrete-epoch fence itself).
        out = self.plane(
            "sid = new_session(owning_node='nOWNER0000001', node_epoch=5)\n"
            "session_records.attach_session(sid, new_owning_node='nNEW00000001', expected_owning_node='nOWNER0000001', expected_node_epoch=4)\n"
            "out = {}\n"
        )
        self.assertEqual(out["_denied"], "DENIED_STALE_FENCE")
        self.assertEqual(out["_exit"], 2)

    def test_correct_epoch_flips_owner_and_bumps_epoch(self) -> None:
        out = self.plane(
            "sid = new_session(owning_node='nOWNER0000001', node_epoch=5)\n"
            "r = session_records.attach_session(sid, new_owning_node='nNEW00000001', expected_owning_node='nOWNER0000001', expected_node_epoch=5)\n"
            "row = db.fetch_one('SELECT owning_node, node_epoch FROM agent_sessions WHERE id = ?', (sid,))\n"
            "out = {'r': r, 'owner': row[0], 'epoch': row[1]}\n"
        )
        self.assertEqual(out["r"]["owning_node"], "nNEW00000001")
        self.assertEqual(out["r"]["node_epoch"], 6)  # bumped from 5
        self.assertEqual(out["owner"], "nNEW00000001")
        self.assertEqual(out["epoch"], 6)

    def test_unfenced_session_attaches_cleanly(self) -> None:
        # An UNFENCED session (owning_node NULL) attaches cleanly (the fence exempts a NULL-owner row).
        out = self.plane(
            "sid = new_session()\n"  # no owning_node -> NULL
            "r = session_records.attach_session(sid, new_owning_node='nNEW00000001', expected_owning_node='nANY0000001', expected_node_epoch=0)\n"
            "row = db.fetch_one('SELECT owning_node, node_epoch FROM agent_sessions WHERE id = ?', (sid,))\n"
            "out = {'owner': row[0], 'epoch': row[1]}\n"
        )
        self.assertEqual(out["owner"], "nNEW00000001")
        self.assertEqual(out["epoch"], 1)  # 0 + 1

    def test_missing_session_structured_not_found(self) -> None:
        out = self.plane(
            "session_records.attach_session('no-such-session', new_owning_node='nN', expected_owning_node='nO', expected_node_epoch=0)\n"
            "out = {}\n"
        )
        self.assertEqual(out["_denied"], "DENIED_SESSION_NOT_FOUND")


class DecideApprovalTest(_KzdbSubprocess):
    def test_decide_open_approval_approves(self) -> None:
        out = self.plane(
            "sid = new_session()\n"
            "aid = new_approval(sid)\n"
            "r = session_records.decide_approval(aid, 'approve')\n"
            "row = db.fetch_one('SELECT state, decided_by FROM approval_requests WHERE id = ?', (aid,))\n"
            "out = {'state': r['state'], 'row_state': row[0], 'decided_by': row[1]}\n"
        )
        self.assertEqual(out["state"], "approved")
        self.assertEqual(out["row_state"], "approved")
        self.assertEqual(out["decided_by"], "human")

    def test_decide_already_decided_refused(self) -> None:
        out = self.plane(
            "sid = new_session()\n"
            "aid = new_approval(sid, state='approved')\n"
            "session_records.decide_approval(aid, 'deny')\n"
            "out = {}\n"
        )
        self.assertEqual(out["_denied"], "DENIED_APPROVAL_ALREADY_DECIDED")


# --- D7 via the real CLI (op-coverage sees D7) ---------------------------------------------------

_ACTIVE = {"KAIZEN_DIST_MODE": "active"}
_OBSERVE = {"KAIZEN_DIST_MODE": "observe"}


class D7CliTest(IsolatedDBTest):
    """D7 through the real CLI (IsolatedDBTest). Satisfies op-coverage (self.kz("D7", ...)); the engine
    semantics are proven in the in-process legs above."""

    def _claim_and_grant(self, scope: str) -> str:
        rc, p = self.kz("D4", "--action", "claim", "--summary", "Claim.", env=_OBSERVE)
        self.assertEqual(rc, 0, p)
        holder = p["holder"]
        rc, p = self.kz("D5", "--action", "grant", "--scope", scope,
                        "--payload-json", json.dumps({"holder": holder}), "--summary", "Grant.", env=_OBSERVE)
        self.assertEqual(rc, 0, p)
        return holder

    def test_request_then_list(self) -> None:
        self._claim_and_grant("p/main")
        rc, p = self.kz("D7", "--action", "request", "--scope", "p/main",
                        "--payload-json", json.dumps({"target_node": "nTARGET00001", "task": "refactor"}),
                        "--summary", "Dispatch.", env=_OBSERVE)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("status"), "OK", p)
        self.assertEqual(p.get("state"), "requested", p)
        did = p["dispatch_id"]
        rc, p = self.kz("D7", "--action", "list", env=_OBSERVE)
        self.assertEqual(rc, 0, p)
        self.assertEqual(p.get("count"), 1, p)
        self.assertEqual(p["dispatches"][0]["dispatch_id"], did, p)
        self.assertEqual(p["dispatches"][0]["state"], "requested", p)

    def test_request_without_lease_refused(self) -> None:
        # No lease held on the scope -> DENIED_DISPATCH_UNLEASED (exit 2), surfaced via the CLI.
        rc, p = self.kz("D7", "--action", "request", "--scope", "p/unheld",
                        "--payload-json", json.dumps({"target_node": "nT", "task": "x"}),
                        "--summary", "Dispatch.", env=_OBSERVE)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_DISPATCH_UNLEASED", p)

    def test_off_mode_inert(self) -> None:
        rc, p = self.kz("D7", "--action", "list")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_DIST_MODE_OFF", p)

    def test_off_mode_creates_no_fleet_db(self) -> None:
        self.kz("D7", "--action", "list")
        self.assertFalse((self.root / "AI" / "db" / "fleet.db").exists())

    def test_request_missing_fields_refused(self) -> None:
        self._claim_and_grant("p/main")
        rc, p = self.kz("D7", "--action", "request", "--scope", "p/main",
                        "--payload-json", json.dumps({"task": "no target"}), "--summary", "D.", env=_OBSERVE)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_DISPATCH_FIELDS_REQUIRED", p)

    def test_action_required(self) -> None:
        rc, p = self.kz("D7", env=_ACTIVE)
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_DISPATCH_ACTION_REQUIRED", p)


# --- supervisor loopback (scratch plane subprocess) ----------------------------------------------

_SUP_PREAMBLE = r"""
import json, sys
from kaizen_components import db, session_records
from kaizen_components.orchestration.supervisor import Supervisor
from kaizen_components.denials import KaizenDenied

def open_run(summary="run"):
    from kaizen_components import agent_runs
    ns = type("NS", (), {})()
    ns.task_id = None; ns.summary = summary; ns.body = ""
    ns.payload_json = json.dumps({"agent_type": "other", "surface": "cli"}); ns.payload_json_file = None
    ns.test = False
    return agent_runs.agent_run_start(ns)["id"]

def new_session(**cols):
    sid = db.new_id("as"); now = db.now()
    base = {"id": sid, "created_at": now, "controller": "kaizen", "mode": "orchestrate",
            "auth_mode": "none", "state": "open", "summary": "s", "content_hash": "h", "is_test": 1}
    base.update(cols)
    keys = list(base)
    def op(conn, _a):
        conn.execute("INSERT INTO agent_sessions (" + ",".join(keys) + ") VALUES (" + ",".join("?" for _ in keys) + ")",
                     tuple(base[k] for k in keys))
    db.write_tx(op)
    return sid

def new_approval(session_id, state="open"):
    aid = db.new_id("apr"); now = db.now()
    def op(conn, _a):
        conn.execute("INSERT INTO approval_requests (id, created_at, updated_at, session_id, request_type, state, summary, content_hash, is_test) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", (aid, now, now, session_id, "tool_approval", state, "approve", "h", 1))
    db.write_tx(op)
    return aid

out = None
try:
    exec(BODY)
    print("RESULT " + json.dumps(out))
except KaizenDenied as e:
    print("RESULT " + json.dumps({"_denied": e.code, "_exit": e.exit_code}))
"""


class _SupSubprocess(unittest.TestCase):
    """Drives a REAL in-process Supervisor pinned to a fresh scratch KAIZEN_REPO_ROOT in a subprocess
    (test_supervisor convention). Distribution is on so the daemon holds a fleet.db handle."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-rd-sup-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.assertEqual(kaizen(self.root, "K1")[0], 0)

    def drive(self, body: str, *, env: dict | None = None) -> dict:
        """Execute a supervisor body in observe mode against a scratch two-plane environment and return RESULT."""
        script = "BODY = " + repr(body) + "\n" + _SUP_PREAMBLE
        full_env = dict(os.environ)
        full_env["KAIZEN_REPO_ROOT"] = str(self.root)
        full_env["KAIZEN_DIST_MODE"] = "observe"
        if env:
            full_env.update(env)
        proc = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            cwd=str(REPO_ROOT), env=full_env, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
        for line in proc.stdout.splitlines():
            if line.startswith("RESULT "):
                return json.loads(line[len("RESULT "):])
        self.fail(f"no RESULT.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")


class SupervisorSteerTest(_SupSubprocess):
    def test_steer_records_session_instruction(self) -> None:
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "sid = new_session()\n"
            "resp = sup._handle_control({'op': 'steer', 'args': {'session_id': sid, 'instruction': 'focus on tests'}})\n"
            "tl = session_records.session_timeline(type('NS', (), {'session_id': sid})())\n"
            "sup.shutdown()\n"
            "out = {'resp': resp, 'instructions': [i['instruction'] for i in tl['instructions']]}\n"
        )
        self.assertEqual(out["resp"]["status"], "OK")
        self.assertIn("focus on tests", out["instructions"])

    def test_steer_unknown_run_denied(self) -> None:
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "resp = sup._handle_control({'op': 'steer', 'args': {'agent_run_id': 'no-such-run', 'instruction': 'hi'}})\n"
            "sup.shutdown()\n"
            "out = {'resp': resp}\n"
        )
        self.assertEqual(out["resp"]["code"], "DENIED_AGENT_RUN_NOT_FOUND")


class SupervisorCancelTest(_SupSubprocess):
    def test_cancel_force_finalizes_nonterminal_run(self) -> None:
        # A seeded non-terminal run (T5) with no live child -> cancel force-finalizes it truthfully.
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "rid = open_run('cancel me')\n"
            "resp = sup._handle_control({'op': 'cancel', 'args': {'agent_run_id': rid}})\n"
            "from kaizen_components import agent_runs\n"
            "state = agent_runs.agent_run_inspect(type('NS', (), {'agent_run_id': rid})())['state']\n"
            "sup.shutdown()\n"
            "out = {'resp': resp, 'terminal': state['terminal'], 'terminal_state': state['terminal_state']}\n"
        )
        self.assertEqual(out["resp"]["status"], "OK")
        self.assertTrue(out["resp"]["finalized"])
        self.assertEqual(out["resp"]["conclusion"], "canceled")
        self.assertTrue(out["terminal"])
        self.assertEqual(out["terminal_state"], "failure")  # canceled is a non-success finalize

    def test_cancel_unknown_run_denied(self) -> None:
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "resp = sup._handle_control({'op': 'cancel', 'args': {'agent_run_id': 'nope'}})\n"
            "sup.shutdown()\n"
            "out = {'resp': resp}\n"
        )
        self.assertEqual(out["resp"]["code"], "DENIED_AGENT_RUN_NOT_FOUND")


class SupervisorApproveTest(_SupSubprocess):
    def test_approve_decides_c4_row(self) -> None:
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "sid = new_session()\n"  # unfenced session
            "aid = new_approval(sid)\n"
            "resp = sup._handle_control({'op': 'approve', 'args': {'approval_id': aid, 'decision': 'approve'}})\n"
            "row = db.fetch_one('SELECT state FROM approval_requests WHERE id = ?', (aid,))\n"
            "sup.shutdown()\n"
            "out = {'resp': resp, 'state': row[0]}\n"
        )
        self.assertEqual(out["resp"]["status"], "OK")
        self.assertEqual(out["state"], "approved")

    def test_approve_fenced_session_stale_epoch_refused(self) -> None:
        # The approval's session is fenced to a DIFFERENT node -> the fenced decide refuses
        # DENIED_STALE_FENCE (approval authority binds to the epoch-current controller).
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "sid = new_session(owning_node='nSOMEOTHERNODE', node_epoch=3)\n"
            "aid = new_approval(sid)\n"
            "resp = sup._handle_control({'op': 'approve', 'args': {'approval_id': aid, 'decision': 'approve'}})\n"
            "sup.shutdown()\n"
            "out = {'resp': resp}\n"
        )
        self.assertEqual(out["resp"]["code"], "DENIED_STALE_FENCE")


class SupervisorAttachTest(_SupSubprocess):
    def test_attach_over_loopback_takes_ownership(self) -> None:
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "node = sup._fleet.node_id\n"
            "sid = new_session(owning_node='nOLDOWNER0001', node_epoch=2)\n"
            "resp = sup._handle_control({'op': 'attach', 'args': {'session_id': sid, 'expected_owning_node': 'nOLDOWNER0001', 'expected_node_epoch': 2}})\n"
            "row = db.fetch_one('SELECT owning_node, node_epoch FROM agent_sessions WHERE id = ?', (sid,))\n"
            "sup.shutdown()\n"
            "out = {'resp': resp, 'owner': row[0], 'epoch': row[1], 'node': node}\n"
        )
        self.assertEqual(out["resp"]["status"], "OK")
        self.assertEqual(out["owner"], out["node"])  # this daemon's node now owns it
        self.assertEqual(out["epoch"], 3)  # 2 + 1


class SupervisorDispatchPollTest(_SupSubprocess):
    def test_poll_with_runner_executes_self_dispatch_end_to_end(self) -> None:
        # A self-targeted dispatch (this node holds the lease + is the target) -> poll with an INJECTED
        # runner accepts/starts/runs/completes it end-to-end (the artifact is the runner's patch text).
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "from kaizen_components.fleet import coordination, dispatch_remote\n"
            "node = sup._fleet.node_id\n"
            "coordination.claim_coordinator(sup._fleet, summary='c')\n"
            "coordination.grant_lease(sup._fleet, 'p/main', node, summary='g')\n"
            "req = dispatch_remote.request_dispatch(sup._fleet, target_node=node, task='refactor', scope_key='p/main')\n"
            "did = req['dispatch_id']\n"
            "sup._dispatch_runner = lambda task, workdir: {'artifact_text': 'PATCH for ' + task, 'branch': 'kz/auto'}\n"
            "poll = sup._handle_control({'op': 'dispatch/poll', 'args': {}})\n"
            "reduced = dispatch_remote.current_dispatches(sup._fleet)[did]\n"
            "sup.shutdown()\n"
            "out = {'poll': poll, 'state': reduced['state'], 'artifact': reduced.get('artifact')}\n"
        )
        self.assertEqual(out["poll"]["status"], "OK")
        self.assertEqual(len(out["poll"]["executed"]), 1)
        self.assertEqual(out["poll"]["executed"][0]["state"], "completed")
        self.assertEqual(out["state"], "completed")
        self.assertEqual(out["artifact"]["branch"], "kz/auto")
        self.assertIsNotNone(out["artifact"]["sha"])

    def test_poll_no_runner_fails_truthfully(self) -> None:
        # DEFAULT runner is None -> the executor accepts/starts then fail_dispatch("no runner configured")
        # TRUTHFULLY (never a fabricated completion).
        out = self.drive(
            "sup = Supervisor(); sup.boot()\n"
            "from kaizen_components.fleet import coordination, dispatch_remote\n"
            "node = sup._fleet.node_id\n"
            "coordination.claim_coordinator(sup._fleet, summary='c')\n"
            "coordination.grant_lease(sup._fleet, 'p/main', node, summary='g')\n"
            "did = dispatch_remote.request_dispatch(sup._fleet, target_node=node, task='t', scope_key='p/main')['dispatch_id']\n"
            "poll = sup._handle_control({'op': 'dispatch/poll', 'args': {}})\n"
            "reduced = dispatch_remote.current_dispatches(sup._fleet)[did]\n"
            "sup.shutdown()\n"
            "out = {'poll': poll, 'state': reduced['state'], 'executed': poll['executed']}\n"
        )
        self.assertEqual(out["state"], "failed")
        self.assertEqual(out["executed"][0]["reason"], "no runner configured")


# --- EXIT: single-machine two-plane lab ----------------------------------------------------------

@unittest.skipIf(GIT is None, "git not available")
class TwoPlaneDispatchLabTest(_KzdbSubprocess):
    """EXIT (plan §5.3 M14): "a second scratch node runs a dispatched refactor; the laptop approves over
    the ledger". SINGLE-MACHINE two-plane lab per the owner's LOCKED decision (no second physical
    machine, no real tailnet): plane A (dispatcher/laptop) + plane B (target/worker) are two node
    identities against ONE shared fleet.db file, opened SEQUENTIALLY in one process. The shared-file
    two-identity form STANDS IN for the synced two-plane form (the coord ledger is the sync-server
    stand-in); we do NOT run a tursodb server in unittest. The dispatcher's C4 approval that gates the
    apply is seeded in this test's scratch kaizen.db (the laptop's local approval)."""

    def test_second_node_runs_dispatch_and_laptop_approves_apply(self) -> None:
        out = self.plane(
            "import shutil, subprocess, tempfile\n"
            "from pathlib import Path\n"
            "from kaizen_components.fleet import coordination, dispatch_remote, reducers\n"
            "from kaizen_components.fleet.store import FleetStore\n"
            "GIT = shutil.which('git')\n"
            "shared_db = str(Path(os.environ['KAIZEN_REPO_ROOT']) / 'AI' / 'db' / 'shared_fleet.db')\n"
            "nodeA = {'node_id': 'nLAPTOP000001', 'created_at': 't'}\n"
            "nodeB = {'node_id': 'nWORKER000001', 'created_at': 't'}\n"
            "# --- plane A (dispatcher): claim + grant self the lease, then dispatch to B ---\n"
            "sA = FleetStore(db_path=shared_db, node=nodeA)\n"
            "coordination.claim_coordinator(sA, summary='claim')\n"
            "coordination.grant_lease(sA, 'p/main', 'nLAPTOP000001', summary='grant')\n"
            "req = dispatch_remote.request_dispatch(sA, target_node='nWORKER000001', task='refactor', scope_key='p/main')\n"
            "did = req['dispatch_id']\n"
            "sA.close()\n"
            "# --- a scratch git repo B works in; the refactor edits a.txt and returns a real diff ---\n"
            "repoB = tempfile.mkdtemp(prefix='kz-lab-B-')\n"
            "checked_git(GIT, 'init', '-q', repoB)\n"
            "for k, v in (('user.email','t@t'),('user.name','t'),('commit.gpgsign','false')):\n"
            "    checked_git(GIT, '-C', repoB, 'config', k, v)\n"
            "Path(repoB, 'a.txt').write_text('one\\n'); checked_git(GIT, '-C', repoB, 'add', '-A')\n"
            "checked_git(GIT, '-C', repoB, 'commit', '-qm', 'init')\n"
            "Path(repoB, 'a.txt').write_text('one\\ntwo from worker\\n')\n"
            "patch_text = checked_git(GIT, '-C', repoB, 'diff').stdout\n"
            "checked_git(GIT, '-C', repoB, 'checkout', '--', 'a.txt')\n"
            "# --- plane B (target/worker): accept -> start -> complete with the patch metadata ---\n"
            "sB = FleetStore(db_path=shared_db, node=nodeB)\n"
            "dispatch_remote.accept_dispatch(sB, did)\n"
            "dispatch_remote.start_dispatch(sB, did)\n"
            "sha = dispatch_remote.sha256_text(patch_text)\n"
            "dispatch_remote.complete_dispatch(sB, did, artifact_name=did + '.patch', artifact_sha=sha, branch='kz/refactor')\n"
            "b_markers = [e['marker'] for e in sB.coord_events() if e['event_kind'] == 'dispatch']\n"
            "sB.close()\n"
            "# --- plane A reduces: sees completed + artifact; seed the laptop's C4 approval; apply ---\n"
            "sA2 = FleetStore(db_path=shared_db, node=nodeA)\n"
            "reduced = dispatch_remote.current_dispatches(sA2)[did]\n"
            "sid = new_session()\n"
            "aid = new_approval(sid, state='approved')\n"  # the laptop's LOCAL approval gating the apply
            "repoA = tempfile.mkdtemp(prefix='kz-lab-A-')\n"
            "checked_git(GIT, 'init', '-q', repoA)\n"
            "for k, v in (('user.email','t@t'),('user.name','t'),('commit.gpgsign','false')):\n"
            "    checked_git(GIT, '-C', repoA, 'config', k, v)\n"
            "Path(repoA, 'a.txt').write_text('one\\n'); checked_git(GIT, '-C', repoA, 'add', '-A')\n"
            "checked_git(GIT, '-C', repoA, 'commit', '-qm', 'init')\n"
            "patch_file = Path(repoA, 'from_worker.patch'); patch_file.write_text(patch_text)\n"
            "from kaizen_components import db\n"
            "def lookup(a):\n"
            "    r = db.fetch_one('SELECT id, session_id, state, decided_by FROM approval_requests WHERE id = ?', (a,))\n"
            "    return {'id': r[0], 'session_id': r[1], 'state': r[2], 'decided_by': r[3]} if r else None\n"
            "applied = dispatch_remote.apply_dispatch(sA2, coordination._default_git_runner(repoA), did, artifact_path=str(patch_file), approval_id=aid, approvals_lookup=lookup)\n"
            "a_content = Path(repoA, 'a.txt').read_text()\n"
            "all_markers = [e['marker'] for e in sA2.coord_events() if e['event_kind'] == 'dispatch']\n"
            "resolutions = [e for e in sA2.coord_events() if e['event_kind'] == 'resolution' and e['marker'] == 'recorded']\n"
            "sA2.close()\n"
            "shutil.rmtree(repoA, ignore_errors=True); shutil.rmtree(repoB, ignore_errors=True)\n"
            "out = {\n"
            "  'b_markers': b_markers,\n"
            "  'reduced_state': reduced['state'],\n"
            "  'reduced_artifact': reduced.get('artifact'),\n"
            "  'applied': applied['applied'],\n"
            "  'a_content_has_change': 'two from worker' in a_content,\n"
            "  'all_markers': all_markers,\n"
            "  'resolution_applied': resolutions[-1]['payload']['applied'] if resolutions else None,\n"
            "}\n"
        )
        # The worker (plane B) drove the full lifecycle.
        self.assertEqual(out["b_markers"], ["requested", "accepted", "started", "completed"])
        # Plane A reduces the shared ledger and sees the completion + artifact (basename + sha only).
        self.assertEqual(out["reduced_state"], "completed")
        self.assertEqual(out["reduced_artifact"]["branch"], "kz/refactor")
        self.assertIsNotNone(out["reduced_artifact"]["sha"])
        # The laptop approved locally, and the patch applied onto A's scratch repo (file changed).
        self.assertTrue(out["applied"])
        self.assertTrue(out["a_content_has_change"])
        self.assertEqual(out["resolution_applied"], True)
        # The full shared event trail on A: the dispatch lifecycle is complete.
        self.assertEqual(out["all_markers"], ["requested", "accepted", "started", "completed"])


if __name__ == "__main__":
    unittest.main()
