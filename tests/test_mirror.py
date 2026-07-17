"""Git mirror + strict conflict handling (v8 M12, plan §B.4 — the authoritative contract).

Hermetic. Two harness styles, mirroring the existing suite:

1. **In-process, fake store + REAL git against SCRATCH repos** (the bulk). ``mirror.py`` takes an
   injected store and git_runner, so a lightweight ``FakeStore`` (an in-memory coord_event list that
   runs the real :mod:`fleet.reducers`) plus a real ``git`` runner bound to a tempfile repo exercises
   the append/reduce/refuse/FF/fork/merge/fan-in/fetch logic with ZERO DB and ZERO network. Every git
   operation runs inside SCRATCH temp repos these tests ``git init`` — never the real repo's tree.
2. **Subprocess scratch-plane** (``_ScratchSubprocess``, the test_fleet_coordination pattern) for the
   two legs that need a REAL ``FleetStore`` + ``HandoffEngine`` push→confirm and a real
   ``digest()["conflicts"]`` — a fresh ``KAIZEN_REPO_ROOT`` per test so no real AI/db is touched.

Real-git classes are ``skipUnless(shutil.which("git"))``. The runner pins temporary storage beneath
``AI/work``; tests assert no synced coord_event payload carries that absolute scratch path, proving
the basename/index/sha-only discipline holds.
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

from _harness import REPO_ROOT, kaizen  # noqa: E402

from kaizen_components.denials import KaizenDenied  # noqa: E402
from kaizen_components.fleet import mirror, reducers  # noqa: E402

GIT = shutil.which("git")


# --- a DB-free fake store: an in-memory coord_event list over the REAL reducers -------------------

class FakeStore:
    """A minimal FleetStore stand-in for mirror.py: it records appended coord_events in a list and
    reads them back, so the real reducers (current_leases / reduce_conflicts) run over them. NO DB, NO
    network. ``append_coord_event`` mirrors the real signature and validates (kind x marker) against the
    registry so a bad pair fails here exactly as it would in the store."""

    def __init__(self, node_id: str = "nMirrorTest00", watermark: int = -1) -> None:
        self._node_id = node_id
        self._events: list[dict] = []
        self._watermark = watermark
        self._seq = 0

    @property
    def node_id(self) -> str:
        return self._node_id

    def watermark(self, project_id: str | None = None) -> int:
        return self._watermark

    def append_coord_event(self, event_kind, marker, *, summary, scope_key=None, epoch=None, payload=None, project_id=None, source_event_id=None):
        from kaizen_components.schemas.registry import COORD_EVENT_KIND_MARKERS

        assert event_kind in COORD_EVENT_KIND_MARKERS and marker in COORD_EVENT_KIND_MARKERS[event_kind], (
            f"unregistered coord pair ({event_kind}, {marker})"
        )
        self._seq += 1
        eid = f"ce_{self._seq:04d}_{self._node_id[-6:]}"
        created = f"2026-07-09T00:00:{self._seq:02d}+00:00"
        row = {
            "id": eid, "created_at": created, "node_id": self._node_id, "project_id": project_id,
            "event_kind": event_kind, "marker": marker, "scope_key": scope_key, "epoch": epoch,
            "payload": payload if isinstance(payload, dict) else None,
        }
        self._events.append(row)
        return {"status": "OK", "id": eid, "event_kind": event_kind, "marker": marker}

    def coord_events(self) -> list[dict]:
        return list(self._events)

    # Test helpers.
    def events_of(self, kind, marker=None):
        return [e for e in self._events if e["event_kind"] == kind and (marker is None or e["marker"] == marker)]

    def grant_self(self, scope_key, epoch=1, seq=1):
        """Inject a lease/granted so this node is the reduced active holder of scope_key."""
        self.append_coord_event("lease", "granted", summary="grant", scope_key=scope_key, epoch=epoch,
                                payload={"grant_seq": seq, "holder": self._node_id, "mode": "authoritative", "ttl_s": 900})


# --- scratch git helpers (in-process; SCRATCH repos only) ----------------------------------------

def _run_git(*args, cwd=None):
    result = subprocess.run([GIT, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    if result.returncode != 0:
        raise AssertionError(f"scratch git failed: argv={args!r}; rc={result.returncode}; stdout={result.stdout!r}; stderr={result.stderr!r}")
    return result


def make_scratch_repo(where: str) -> str:
    # -b main pins the initial branch so the default-branch name is deterministic regardless of the
    # host's init.defaultBranch (master vs main) -- a mismatch makes a clone land on an UNBORN branch
    # and turns a clean handoff into a false fork (proven empirically).
    _run_git("init", "-q", "-b", "main", where)
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _run_git("-C", where, "config", k, v)
    (Path(where) / "base.txt").write_text("base\n", encoding="utf-8")
    _run_git("-C", where, "add", "-A")
    _run_git("-C", where, "commit", "-qm", "base")
    return where


def make_bare_hub(where: str) -> str:
    _run_git("init", "--bare", "-q", "-b", "main", where)
    return where


def clone_from(src: str, dst: str) -> str:
    _run_git("clone", "-q", src, dst)
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _run_git("-C", dst, "config", k, v)
    return dst


def head_sha(repo: str) -> str:
    return _run_git("-C", repo, "rev-parse", "HEAD").stdout.strip()


def runner_for(repo: str):
    from kaizen_components.fleet.coordination import _default_git_runner

    return _default_git_runner(repo)


class _TmpBase(unittest.TestCase):
    def tmpdir(self, prefix="kz-m-"):
        d = tempfile.mkdtemp(prefix=prefix)
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d

    def assertNoAbsPathInPayloads(self, store, needle):
        """No synced coord_event payload may carry the absolute scratch path (the redaction pass-path
        rejects home/temp paths; mirror keeps only basename/index/sha)."""
        blob = json.dumps([e["payload"] for e in store.coord_events()])
        self.assertNotIn(json.dumps(needle)[1:-1], blob, "an absolute scratch path leaked into a synced payload")


# --- naming ---------------------------------------------------------------------------------------

class NamingTest(unittest.TestCase):
    def test_node_branch_shape(self):
        self.assertEqual(mirror.node_branch("t123", "nAbc"), "kz/t123/nAbc")
        self.assertEqual(mirror.node_branch("t123", "nAbc", worktree=2), "kz/t123/nAbc/w2")

    def test_node_branch_refuses_invalid_task(self):
        for bad in ("", "  ", "a b", "../etc", "a/b", "a\\b", "-x"):
            with self.assertRaises(KaizenDenied) as ctx:
                mirror.node_branch(bad, "nAbc")
            self.assertEqual(ctx.exception.code, "DENIED_MIRROR_BRANCH_INVALID")
            self.assertEqual(ctx.exception.exit_code, 2)

    def test_branch_scope_key_shape(self):
        self.assertEqual(mirror.branch_scope_key("proj9", "kz/t/nA"), "proj9/kz/t/nA")


# --- transport guard ------------------------------------------------------------------------------

class TransportGuardTest(_TmpBase):
    def test_local_path_ok(self):
        repo = make_scratch_repo(self.tmpdir())
        mirror.assert_remote_transport_safe(repo)  # existing path
        mirror.assert_remote_transport_safe("relative/path/repo")  # bare relative
        mirror.assert_remote_transport_safe("file:///srv/hub.git")
        mirror.assert_remote_transport_safe("/srv/kaizen-hub.git")

    def test_ssh_forms_ok(self):
        mirror.assert_remote_transport_safe("git@gb10.example.ts.net:kaizen.git")
        mirror.assert_remote_transport_safe("alice@gb10.example.ts.net:team/kaizen.git")
        mirror.assert_remote_transport_safe("ssh://git@host/repo.git")

    def test_malformed_scp_forms_are_refused(self):
        for remote in ("bad user@host:repo.git", "@host:repo.git", "alice@:repo.git", "alice@host:"):
            with self.subTest(remote=remote), self.assertRaises(KaizenDenied) as raised:
                mirror.assert_remote_transport_safe(remote)
            self.assertEqual(raised.exception.code, "DENIED_MIRROR_TRANSPORT")

    def test_https_non_tailnet_refused(self):
        # probe False AND a non-tailnet host => refused.
        with self.assertRaises(KaizenDenied) as ctx:
            mirror.assert_remote_transport_safe(
                "https://github.com/x/y.git", tailnet_probe=lambda: False, suffix_env={"KAIZEN_TAILNET_SUFFIX": ".ts.net"}
            )
        self.assertEqual(ctx.exception.code, "DENIED_MIRROR_TRANSPORT")

    def test_https_tailnet_host_on_tailnet_ok(self):
        mirror.assert_remote_transport_safe(
            "https://gb10.tail1234.ts.net/kaizen.git",
            tailnet_probe=lambda: True,
            suffix_env={"KAIZEN_TAILNET_SUFFIX": ".ts.net"},
        )

    def test_https_tailnet_host_off_tailnet_refused(self):
        # probe False even with a tailnet-suffixed host => refused (plain HTTP only WHEN on the tailnet).
        with self.assertRaises(KaizenDenied) as ctx:
            mirror.assert_remote_transport_safe(
                "https://gb10.tail1234.ts.net/kaizen.git",
                tailnet_probe=lambda: False,
                suffix_env={"KAIZEN_TAILNET_SUFFIX": ".ts.net"},
            )
        self.assertEqual(ctx.exception.code, "DENIED_MIRROR_TRANSPORT")

    def test_other_scheme_refused(self):
        with self.assertRaises(KaizenDenied) as ctx:
            mirror.assert_remote_transport_safe("git://host/repo.git", tailnet_probe=lambda: True)
        self.assertEqual(ctx.exception.code, "DENIED_MIRROR_TRANSPORT")


# --- ahead/behind + classify (real git) ----------------------------------------------------------

@unittest.skipUnless(GIT, "git binary required")
class AheadBehindTest(_TmpBase):
    def _fork_repos(self):
        """A repo forked into A and B from a common base; returns (repo, base_sha, a_sha, b_sha) where
        the checked-out HEAD is B and A is a divergent branch."""
        repo = make_scratch_repo(self.tmpdir())
        base = head_sha(repo)
        _run_git("-C", repo, "checkout", "-q", "-b", "a")
        (Path(repo) / "a.txt").write_text("a\n", encoding="utf-8")
        _run_git("-C", repo, "add", "-A"); _run_git("-C", repo, "commit", "-qm", "a")
        a_sha = head_sha(repo)
        _run_git("-C", repo, "checkout", "-q", "-b", "b", base)
        (Path(repo) / "b.txt").write_text("b\n", encoding="utf-8")
        _run_git("-C", repo, "add", "-A"); _run_git("-C", repo, "commit", "-qm", "b")
        b_sha = head_sha(repo)
        return repo, base, a_sha, b_sha

    def test_equal(self):
        repo = make_scratch_repo(self.tmpdir())
        git = runner_for(repo)
        self.assertEqual(mirror.ahead_behind(git, head_sha(repo)), (0, 0))

    def test_ahead_only(self):
        repo = make_scratch_repo(self.tmpdir())
        base = head_sha(repo)
        (Path(repo) / "x.txt").write_text("x\n", encoding="utf-8")
        _run_git("-C", repo, "add", "-A"); _run_git("-C", repo, "commit", "-qm", "x")
        git = runner_for(repo)
        # HEAD is 1 ahead of the base watermark, 0 behind.
        self.assertEqual(mirror.ahead_behind(git, base), (1, 0))

    def test_behind_only(self):
        repo = make_scratch_repo(self.tmpdir())
        (Path(repo) / "x.txt").write_text("x\n", encoding="utf-8")
        _run_git("-C", repo, "add", "-A"); _run_git("-C", repo, "commit", "-qm", "x")
        wm = head_sha(repo)
        _run_git("-C", repo, "checkout", "-q", "-b", "old", "HEAD~1")
        git = runner_for(repo)
        # HEAD (old, at base) is 0 ahead, 1 behind the watermark.
        self.assertEqual(mirror.ahead_behind(git, wm), (0, 1))

    def test_forked(self):
        repo, base, a_sha, b_sha = self._fork_repos()  # HEAD == b
        git = runner_for(repo)
        # HEAD (b) is 1 ahead (its own commit) and 1 behind (a's commit) vs the a watermark.
        self.assertEqual(mirror.ahead_behind(git, a_sha), (1, 1))

    def test_classify_taxonomy(self):
        self.assertEqual(mirror.classify(False, 0, 0, False), [])
        self.assertEqual(mirror.classify(True, 0, 0, False), ["DIRTY_NON_HOLDER"])
        self.assertEqual(mirror.classify(True, 0, 0, True), [])  # holder dirty is fine
        self.assertEqual(mirror.classify(False, 0, 2, False), ["BEHIND_WATERMARK"])
        self.assertEqual(mirror.classify(False, 1, 1, False), ["DIVERGED_FORK"])


# --- acquire_scope: exit criteria (real git + fake store) ----------------------------------------

@unittest.skipUnless(GIT, "git binary required")
class AcquireScopeTest(_TmpBase):
    def test_no_silent_divergence_ff_moves_to_watermark(self):
        # EXIT CRITERION "node hands off with no silent divergence" (git leg):
        # a replica behind the pushed watermark FF-moves cleanly, ZERO divergence events, HEAD==watermark.
        repo = make_scratch_repo(self.tmpdir())
        (Path(repo) / "n.txt").write_text("newer\n", encoding="utf-8")
        _run_git("-C", repo, "add", "-A"); _run_git("-C", repo, "commit", "-qm", "newer")
        watermark = head_sha(repo)
        _run_git("-C", repo, "checkout", "-q", "-b", "replica", "HEAD~1")  # behind by 1
        store = FakeStore()
        res = mirror.acquire_scope(store, runner_for(repo), "proj/kz/t/nA", watermark_sha=watermark, enforce=True)
        self.assertEqual(res["action"], "ff-moved")
        self.assertEqual(head_sha(repo), watermark)  # HEAD advanced to the watermark
        self.assertEqual(store.events_of("divergence", "detected"), [])  # ZERO divergence

    def test_clean_resume_when_head_is_watermark(self):
        repo = make_scratch_repo(self.tmpdir())
        wm = head_sha(repo)
        store = FakeStore()
        res = mirror.acquire_scope(store, runner_for(repo), "proj/s", watermark_sha=wm, enforce=True)
        self.assertEqual(res["action"], "resume")
        self.assertEqual(store.events_of("divergence", "detected"), [])

    def test_fresh_scope_no_watermark(self):
        repo = make_scratch_repo(self.tmpdir())
        store = FakeStore()
        res = mirror.acquire_scope(store, runner_for(repo), "proj/fresh", watermark_sha=None, enforce=True)
        self.assertEqual(res["action"], "fresh")

    def _build_fork(self, repo):
        """Build a real fork off ``main``: HEAD ends on branch 'mine' (ahead 1), watermark = branch
        'theirs' tip (a divergent 1 commit). Returns (mine_sha, watermark)."""
        base = head_sha(repo)
        _run_git("-C", repo, "checkout", "-q", "-b", "mine")
        (Path(repo) / "mine.txt").write_text("mine\n", encoding="utf-8")
        _run_git("-C", repo, "add", "-A"); _run_git("-C", repo, "commit", "-qm", "mine")
        mine_sha = head_sha(repo)
        _run_git("-C", repo, "checkout", "-q", "-b", "theirs", base)
        (Path(repo) / "theirs.txt").write_text("theirs\n", encoding="utf-8")
        _run_git("-C", repo, "add", "-A"); _run_git("-C", repo, "commit", "-qm", "theirs")
        watermark = head_sha(repo)
        _run_git("-C", repo, "checkout", "-q", "mine")  # HEAD on the ahead side, forked vs watermark
        return mine_sha, watermark

    def test_fork_enforce_raises_and_records_decision_prompt(self):
        # EXIT CRITERION "fork => Decision prompt recorded": a real fork; enforce=True raises
        # DENIED_COORD_DIVERGED AND a conflict/detected exists with the EXACT fork menu.
        repo = make_scratch_repo(self.tmpdir())
        mine_sha, watermark = self._build_fork(repo)
        store = FakeStore()
        with self.assertRaises(KaizenDenied) as ctx:
            mirror.acquire_scope(store, runner_for(repo), "proj/fork", watermark_sha=watermark, enforce=True)
        self.assertEqual(ctx.exception.code, "DENIED_COORD_DIVERGED")
        forks = store.events_of("conflict", "detected")
        self.assertEqual(len(forks), 1)
        p = forks[0]["payload"]
        self.assertEqual(p["options"], ["rebase", "merge-on-holder", "parallel-apply-fan-in", "discard-one"])
        self.assertEqual(p["recommendation"], "merge-on-holder")
        self.assertEqual(p["confirmation"], "pending")
        # tree NOT moved (fork never auto-moves): HEAD still on the 'mine' tip.
        self.assertEqual(head_sha(repo), mine_sha)
        self.assertNoAbsPathInPayloads(store, repo)

    def test_fork_shadow_returns_nonblocking_with_same_recording(self):
        # enforce=False => non-blocking RECORDED with the SAME conflict/detected prompt (shadow parity).
        repo = make_scratch_repo(self.tmpdir())
        mine_sha, watermark = self._build_fork(repo)
        store = FakeStore()
        res = mirror.acquire_scope(store, runner_for(repo), "proj/fork", watermark_sha=watermark, enforce=False)
        self.assertEqual(res["status"], "RECORDED")
        self.assertEqual(res["action"], "fork")
        self.assertEqual(head_sha(repo), mine_sha)  # shadow: tree untouched too
        forks = store.events_of("conflict", "detected")
        self.assertEqual(len(forks), 1)
        self.assertEqual(forks[0]["payload"]["options"], ["rebase", "merge-on-holder", "parallel-apply-fan-in", "discard-one"])

    def test_dirty_non_holder_refused(self):
        # EXIT CRITERION "dirty-non-holder refused".
        repo = make_scratch_repo(self.tmpdir())
        wm = head_sha(repo)
        (Path(repo) / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")  # dirty tree
        store = FakeStore()
        with self.assertRaises(KaizenDenied) as ctx:
            mirror.acquire_scope(store, runner_for(repo), "proj/s", watermark_sha=wm, enforce=True, was_prior_holder=False)
        self.assertEqual(ctx.exception.code, "DENIED_COORD_DIVERGED")
        divs = store.events_of("divergence", "detected")
        self.assertEqual(len(divs), 1)
        self.assertIn("DIRTY_NON_HOLDER", divs[0]["payload"]["reasons"])

    def test_dirty_prior_holder_clean_resume_unaffected(self):
        # was_prior_holder=True + HEAD==watermark => clean resume even with a dirty tree.
        repo = make_scratch_repo(self.tmpdir())
        wm = head_sha(repo)
        (Path(repo) / "dirty.txt").write_text("wip\n", encoding="utf-8")
        store = FakeStore()
        res = mirror.acquire_scope(store, runner_for(repo), "proj/s", watermark_sha=wm, enforce=True, was_prior_holder=True)
        self.assertEqual(res["action"], "resume")
        self.assertEqual(store.events_of("divergence", "detected"), [])

    def test_ff_only_forked_never_moves_tree(self):
        # FF-only: forked HEAD is NEVER moved after the refusal (tree unchanged).
        repo = make_scratch_repo(self.tmpdir())
        mine_sha, watermark = self._build_fork(repo)
        store = FakeStore()
        with self.assertRaises(KaizenDenied):
            mirror.acquire_scope(store, runner_for(repo), "proj/fork", watermark_sha=watermark, enforce=True)
        self.assertEqual(head_sha(repo), mine_sha)  # unchanged

    def test_stale_replica_epoch_refused(self):
        # stale-replica-epoch: the reduced ledger epoch is BELOW the node's un-synced watermark => the
        # replica has not pulled. Monkeypatch-free: FakeStore.watermark returns a HIGH value while the
        # ledger carries a low epoch. (We seed a low-epoch event so the reduced max_epoch < watermark.)
        repo = make_scratch_repo(self.tmpdir())
        wm = head_sha(repo)
        store = FakeStore(watermark=50)
        store.append_coord_event("coordinator", "granted", summary="low", epoch=2)  # ledger epoch 2 < 50
        with self.assertRaises(KaizenDenied) as ctx:
            mirror.acquire_scope(store, runner_for(repo), "proj/s", watermark_sha=wm, enforce=True)
        self.assertEqual(ctx.exception.code, "DENIED_COORD_DIVERGED")
        divs = store.events_of("divergence", "detected")
        self.assertIn("STALE_REPLICA_EPOCH", divs[-1]["payload"]["reasons"])


# --- merge-conflict blocking span (real git) -----------------------------------------------------

@unittest.skipUnless(GIT, "git binary required")
class MergeConflictTest(_TmpBase):
    def _conflicting_repo(self):
        """A repo where 'other' conflicts with HEAD on the same file."""
        repo = make_scratch_repo(self.tmpdir())
        (Path(repo) / "c.txt").write_text("original\n", encoding="utf-8")
        _run_git("-C", repo, "add", "-A"); _run_git("-C", repo, "commit", "-qm", "orig")
        _run_git("-C", repo, "checkout", "-q", "-b", "other")
        (Path(repo) / "c.txt").write_text("OTHER-SIDE\n", encoding="utf-8")
        _run_git("-C", repo, "add", "-A"); _run_git("-C", repo, "commit", "-qm", "other")
        _run_git("-C", repo, "checkout", "-q", "main")
        (Path(repo) / "c.txt").write_text("HOLDER-SIDE\n", encoding="utf-8")
        _run_git("-C", repo, "add", "-A"); _run_git("-C", repo, "commit", "-qm", "holder")
        return repo

    def test_merge_conflict_blocks_restores_and_opens_span_then_resolve_closes(self):
        # EXIT CRITERION "merge conflict": DENIED_MIRROR_MERGE_CONFLICT, tree restored clean,
        # conflict/detected open span; reduce_conflicts shows it open; resolve_conflict closes it.
        repo = self._conflicting_repo()
        store = FakeStore()
        store.grant_self("proj/main")  # this node is the active holder
        with self.assertRaises(KaizenDenied) as ctx:
            mirror.attempt_holder_merge(store, runner_for(repo), "proj/main", "other")
        self.assertEqual(ctx.exception.code, "DENIED_MIRROR_MERGE_CONFLICT")
        # tree restored clean (merge --abort ran).
        self.assertEqual(_run_git("-C", repo, "status", "--porcelain").stdout.strip(), "")
        conflict_id = ctx.exception.fields["conflict_event_id"]
        reduced = reducers.reduce_conflicts(store.coord_events())
        self.assertEqual(len(reduced["open"]), 1)
        self.assertEqual(reduced["open"][0]["id"], conflict_id)
        # resolve closes it.
        mirror.resolve_conflict(store, conflict_id, "proj/main", "merged by hand", "merge-on-holder")
        reduced2 = reducers.reduce_conflicts(store.coord_events())
        self.assertEqual(reduced2["open"], [])
        self.assertEqual(reduced2["resolved_count"], 1)
        self.assertNoAbsPathInPayloads(store, repo)

    def test_non_holder_merge_refused(self):
        repo = self._conflicting_repo()
        store = FakeStore()  # holds nothing
        with self.assertRaises(KaizenDenied) as ctx:
            mirror.attempt_holder_merge(store, runner_for(repo), "proj/main", "other")
        self.assertEqual(ctx.exception.code, "DENIED_MIRROR_NOT_HOLDER")

    def test_non_conflict_merge_failure_opens_no_span(self):
        # Audit regression: a merge that fails WITHOUT unmerged paths (unknown branch) must refuse but
        # NOT open a conflict/detected blocking span -- a false span would pollute the digest.
        repo = make_scratch_repo(self.tmpdir())
        store = FakeStore()
        store.grant_self("proj/main")
        with self.assertRaises(KaizenDenied) as ctx:
            mirror.attempt_holder_merge(store, runner_for(repo), "proj/main", "no-such-branch")
        self.assertEqual(ctx.exception.code, "DENIED_MIRROR_MERGE_CONFLICT")
        self.assertFalse(ctx.exception.fields["conflicted"])
        self.assertNotIn("conflict_event_id", ctx.exception.fields)
        self.assertEqual(store.events_of("conflict", "detected"), [])

    def test_clean_merge_on_holder_returns_sha(self):
        # A non-conflicting merge on the holder returns the merge sha.
        repo = make_scratch_repo(self.tmpdir())
        _run_git("-C", repo, "checkout", "-q", "-b", "other")
        (Path(repo) / "new.txt").write_text("new\n", encoding="utf-8")
        _run_git("-C", repo, "add", "-A"); _run_git("-C", repo, "commit", "-qm", "other")
        _run_git("-C", repo, "checkout", "-q", "main")
        store = FakeStore()
        store.grant_self("proj/main")
        res = mirror.attempt_holder_merge(store, runner_for(repo), "proj/main", "other")
        self.assertEqual(res["status"], "OK")
        self.assertTrue(res["merge_sha"])


# --- fan-in (real git) ---------------------------------------------------------------------------

@unittest.skipUnless(GIT, "git binary required")
class FanInTest(_TmpBase):
    def _multi_branch_repo(self):
        repo = make_scratch_repo(self.tmpdir())
        for name, fname in (("na", "na.txt"), ("nb", "nb.txt")):
            _run_git("-C", repo, "checkout", "-q", "-b", f"kz/t/{name}", "main")
            (Path(repo) / fname).write_text(f"{name}\n", encoding="utf-8")
            _run_git("-C", repo, "add", "-A"); _run_git("-C", repo, "commit", "-qm", name)
            _run_git("-C", repo, "checkout", "-q", "main")
        return repo, "main"

    def test_fan_in_compare_read_only(self):
        repo, base = self._multi_branch_repo()
        before = head_sha(repo)
        res = mirror.fan_in_compare(runner_for(repo), base, ["kz/t/na", "kz/t/nb"])
        # read-only: HEAD unchanged, tree clean.
        self.assertEqual(head_sha(repo), before)
        self.assertEqual(_run_git("-C", repo, "status", "--porcelain").stdout.strip(), "")
        self.assertEqual(res["branches"]["kz/t/na"]["ahead"], 1)
        self.assertEqual(res["branches"]["kz/t/na"]["behind"], 0)
        self.assertIn("na.txt", res["branches"]["kz/t/na"]["diffstat"])

    def test_fan_in_integrate_on_holder_merges(self):
        repo, base = self._multi_branch_repo()
        store = FakeStore()
        scope = mirror.branch_scope_key("projX", base)
        store.grant_self(scope)  # hold the integration lease for the base branch
        res = mirror.fan_in_integrate(store, runner_for(repo), "projX", base, "kz/t/na")
        self.assertEqual(res["status"], "OK")
        self.assertTrue(res["merge_sha"])
        self.assertEqual(res["chosen_branch"], "kz/t/na")

    def test_fan_in_integrate_non_holder_refused(self):
        repo, base = self._multi_branch_repo()
        store = FakeStore()  # holds nothing
        with self.assertRaises(KaizenDenied) as ctx:
            mirror.fan_in_integrate(store, runner_for(repo), "projX", base, "kz/t/na")
        self.assertEqual(ctx.exception.code, "DENIED_MIRROR_NOT_HOLDER")


# --- fetch fallback (real git) -------------------------------------------------------------------

@unittest.skipUnless(GIT, "git binary required")
class FetchFallbackTest(_TmpBase):
    def test_dead_hub_live_peer_returns_peer_index(self):
        peer = make_scratch_repo(self.tmpdir())
        clone = clone_from(peer, self.tmpdir())
        dead_hub = os.path.join(self.tmpdir(), "no-such-hub")  # nonexistent path
        res = mirror.fetch_with_fallback(runner_for(clone), dead_hub, [peer])
        self.assertEqual(res["source"], "peer")
        self.assertEqual(res["remote"], "peer[0]")  # INDEX only, never the abs path
        self.assertNotIn(json.dumps(peer)[1:-1], json.dumps(res))  # no absolute path in the return

    def test_live_hub_returns_hub(self):
        hub = clone_from(make_scratch_repo(self.tmpdir()), self.tmpdir())  # a normal (non-bare) hub w/ refs
        clone = clone_from(hub, self.tmpdir())
        res = mirror.fetch_with_fallback(runner_for(clone), hub, [])
        self.assertEqual(res["source"], "hub")
        self.assertEqual(res["remote"], "hub")

    def test_all_dead_refuses(self):
        clone = clone_from(make_scratch_repo(self.tmpdir()), self.tmpdir())
        dead_hub = os.path.join(self.tmpdir(), "dead-hub")
        dead_peer = os.path.join(self.tmpdir(), "dead-peer")
        with self.assertRaises(KaizenDenied) as ctx:
            mirror.fetch_with_fallback(runner_for(clone), dead_hub, [dead_peer])
        self.assertEqual(ctx.exception.code, "DENIED_MIRROR_FETCH_FAILED")

    def test_unsafe_remote_refused_before_fetch(self):
        clone = clone_from(make_scratch_repo(self.tmpdir()), self.tmpdir())
        with self.assertRaises(KaizenDenied) as ctx:
            mirror.fetch_with_fallback(
                runner_for(clone), "https://github.com/x/y.git", [], tailnet_probe=lambda: False,
                suffix_env={"KAIZEN_TAILNET_SUFFIX": ".ts.net"},
            )
        self.assertEqual(ctx.exception.code, "DENIED_MIRROR_TRANSPORT")


# --- enforcement transition record ---------------------------------------------------------------

class EnforcementTransitionTest(unittest.TestCase):
    def test_records_shadow_to_enforced(self):
        # EXIT CRITERION "shadow->enforced transition recorded".
        store = FakeStore()
        res = mirror.record_enforcement_transition(store, "projX", "shadow", "enforced")
        self.assertEqual(res["transition"], "shadow->enforced")
        rows = store.events_of("resolution", "recorded")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["transition"], "shadow->enforced")
        self.assertEqual(rows[0]["payload"]["from"], "shadow")
        self.assertEqual(rows[0]["payload"]["to"], "enforced")

    def test_transition_payload_reflects_actual_direction(self):
        # Audit regression: the payload transition string states the ACTUAL direction passed, not a
        # hardcoded shadow->enforced.
        store = FakeStore()
        res = mirror.record_enforcement_transition(store, "projX", "enforced", "shadow")
        self.assertEqual(res["transition"], "enforced->shadow")
        rows = store.events_of("resolution", "recorded")
        self.assertEqual(rows[-1]["payload"]["transition"], "enforced->shadow")


# --- reduce_conflicts determinism ----------------------------------------------------------------

class ReduceConflictsTest(unittest.TestCase):
    _EVENTS = [
        {"id": "cd1", "created_at": "t1", "event_kind": "conflict", "marker": "detected", "scope_key": "s/a",
         "payload": {"decision": "fork on s/a", "options": mirror.FORK_OPTIONS, "recommendation": "merge-on-holder"}},
        {"id": "cd2", "created_at": "t2", "event_kind": "conflict", "marker": "detected", "scope_key": "s/b",
         "payload": {"decision": "fork on s/b", "options": mirror.FORK_OPTIONS, "recommendation": "merge-on-holder"}},
        {"id": "cr1", "created_at": "t3", "event_kind": "conflict", "marker": "resolved", "scope_key": "s/a",
         "payload": {"source_conflict_id": "cd1", "chosen": "merge-on-holder"}},
        {"id": "noise", "created_at": "t4", "event_kind": "lease", "marker": "granted", "scope_key": "s/c", "epoch": 1, "payload": {}},
    ]

    def test_open_and_resolved(self):
        reduced = reducers.reduce_conflicts(self._EVENTS)
        self.assertEqual([o["id"] for o in reduced["open"]], ["cd2"])  # cd1 closed, cd2 open
        self.assertEqual(reduced["resolved_count"], 1)
        self.assertEqual(reduced["open"][0]["scope_key"], "s/b")
        self.assertEqual(reduced["open"][0]["options"], mirror.FORK_OPTIONS)

    def test_deterministic_under_shuffle_and_duplication(self):
        import random

        ref = reducers.reduce_conflicts(self._EVENTS)
        for seed in range(200):
            rng = random.Random(seed)
            doubled = list(self._EVENTS) + [e for e in self._EVENTS if rng.random() < 0.5]
            rng.shuffle(doubled)
            self.assertEqual(reducers.reduce_conflicts(doubled), ref, f"drift @ seed {seed}")

    def test_resolved_before_detected_still_closes(self):
        # membership, not sequence: a resolved appearing BEFORE its detected still closes the span.
        reordered = [self._EVENTS[2], self._EVENTS[0], self._EVENTS[1]]  # cr1, cd1, cd2
        reduced = reducers.reduce_conflicts(reordered)
        self.assertEqual([o["id"] for o in reduced["open"]], ["cd2"])


# --- subprocess scratch-plane legs: real FleetStore + HandoffEngine + digest ----------------------

_PREAMBLE = r"""
import json, os, shutil, subprocess, sys, tempfile
from kaizen_components.fleet import identity, reducers, coordination as co, mirror
from kaizen_components.fleet.store import FleetStore
from kaizen_components.denials import KaizenDenied

GIT = shutil.which("git")

def git(*args):
    result = subprocess.run([GIT, *args], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"scratch git failed: argv={args!r}; rc={result.returncode}; stdout={result.stdout!r}; stderr={result.stderr!r}")
    return result

def make_repo():
    repo = tempfile.mkdtemp(prefix="kz-m-repo-")
    git("init", "-q", "-b", "main", repo)
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        git("-C", repo, "config", k, v)
    open(os.path.join(repo, "base.txt"), "w").write("base\n")
    git("-C", repo, "add", "-A")
    git("-C", repo, "commit", "-qm", "base")
    return repo

def make_bare():
    hub = tempfile.mkdtemp(prefix="kz-m-hub-")
    git("init", "--bare", "-q", "-b", "main", hub)
    return hub

def clone_cfg(hub):
    dst = tempfile.mkdtemp(prefix="kz-m-cl-"); shutil.rmtree(dst)
    git("clone", "-q", hub, dst)
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        git("-C", dst, "config", k, v)
    return dst

def head(repo):
    return git("-C", repo, "rev-parse", "HEAD").stdout.strip()

out = None
try:
    exec(BODY)
    print("RESULT " + json.dumps(out))
except KaizenDenied as e:
    print("RESULT " + json.dumps({"_denied": e.code, "_exit": e.exit_code}))
"""


@unittest.skipUnless(GIT, "git binary required")
class HandoffToAcquireExitCriterionTest(unittest.TestCase):
    """EXIT CRITERION "node hands off with no silent divergence" end-to-end: HandoffEngine push→confirm
    on clone A, then acquire_scope on clone B. Runs in a scratch KAIZEN_REPO_ROOT so no real AI/db is
    touched (one FleetStore for the lab ledger; the two 'nodes' are the two clones)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="kaizen-mirror-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        rc, payload = kaizen(self.root, "K1")
        self.assertEqual(rc, 0, f"K1 init failed: {payload}")

    def plane(self, body: str) -> dict:
        script = "BODY = " + repr(body) + "\n" + _PREAMBLE
        env = dict(os.environ)
        env["KAIZEN_REPO_ROOT"] = str(self.root)
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, cwd=str(REPO_ROOT), env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, f"scratch plane exited {proc.returncode}; stdout={proc.stdout!r}; stderr={proc.stderr!r}")
        for line in proc.stdout.splitlines():
            if line.startswith("RESULT "):
                return json.loads(line[len("RESULT "):])
        self.fail(f"no RESULT.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")

    def test_handoff_push_confirm_then_acquire_ff_no_divergence(self):
        out = self.plane(
            "hub = make_bare()\n"
            "# seed the hub main from a source repo.\n"
            "srcA = make_repo()\n"
            "git('-C', srcA, 'push', '-q', hub, 'main')\n"
            "# clone A off the hub, branch off main, commit, HandoffEngine push+confirm.\n"
            "cloneA = clone_cfg(hub)\n"
            "branch = mirror.node_branch('t1', 'na')\n"
            "git('-C', cloneA, 'checkout', '-q', '-b', branch)\n"
            "open(os.path.join(cloneA, 'a.txt'), 'w').write('A work\\n')\n"
            "s = FleetStore(); co.claim_coordinator(s, summary='c'); co.grant_lease(s, 'proj/'+branch, s.node_id, summary='g')\n"
            "rec = co.HandoffEngine(s, cloneA, hub_remote=hub, allow_wip_commit=True, allow_push=True).shadow_handoff('proj/'+branch)\n"
            "pushed = [r for r in s.coord_events() if r['event_kind']=='handoff' and r['marker']=='pushed']\n"
            "pushed_sha = pushed[0]['payload']['sha'] if pushed else None\n"
            "aHead = head(cloneA)\n"
            "# clone B fetches the hub branch, starts one behind (origin/main), then FF to the pushed sha.\n"
            "cloneB = clone_cfg(hub)\n"
            "git('-C', cloneB, 'fetch', '-q', hub, branch)\n"
            "git('-C', cloneB, 'checkout', '-q', '-b', branch, 'origin/main')\n"
            "before_divs = len([r for r in s.coord_events() if r['event_kind']=='divergence'])\n"
            "res = mirror.acquire_scope(s, co._default_git_runner(cloneB), 'proj/'+branch, watermark_sha=pushed_sha, enforce=True)\n"
            "after_divs = len([r for r in s.coord_events() if r['event_kind']=='divergence'])\n"
            "bHead = head(cloneB)\n"
            "out = {'pushed_sha': pushed_sha, 'a_head': aHead, 'action': res['action'], 'b_head': bHead,\n"
            "       'sha_match_a': pushed_sha == aHead, 'sha_match_b': bHead == pushed_sha,\n"
            "       'new_divs': after_divs - before_divs}\n"
            "s.close(); shutil.rmtree(hub, ignore_errors=True); shutil.rmtree(srcA, ignore_errors=True)\n"
            "shutil.rmtree(cloneA, ignore_errors=True); shutil.rmtree(cloneB, ignore_errors=True)\n"
        )
        self.assertNotIn("_denied", out, f"unexpected denial: {out}")
        self.assertTrue(out["pushed_sha"], "handoff must emit a confirmed pushed sha")
        self.assertTrue(out["sha_match_a"], "pushed sha must equal A HEAD")
        self.assertIn(out["action"], ("ff-moved", "resume"))
        self.assertTrue(out["sha_match_b"], "B HEAD must equal the pushed watermark after acquire")
        self.assertEqual(out["new_divs"], 0, "ZERO divergence events on a clean handoff+acquire")

    def test_digest_surfaces_open_conflict_span(self):
        # digest()["conflicts"]["open_count"] reflects an open merge-conflict span, then 0 after resolve.
        out = self.plane(
            "s = FleetStore()\n"
            "# record an open fork/conflict directly through mirror, then resolve it.\n"
            "d = mirror.record_fork_decision(s, 'proj/main', {'ahead': 1, 'behind': 1, 'head_sha': 'abc', 'watermark_sha': 'def'})\n"
            "open_before = s.digest()['conflicts']['open_count']\n"
            "mirror.resolve_conflict(s, d['id'], 'proj/main', 'resolved', 'merge-on-holder')\n"
            "open_after = s.digest()['conflicts']['open_count']\n"
            "has_key = 'conflicts' in s.digest()\n"
            "out = {'open_before': open_before, 'open_after': open_after, 'has_key': has_key}\n"
            "s.close()\n"
        )
        self.assertNotIn("_denied", out, f"unexpected denial: {out}")
        self.assertTrue(out["has_key"])
        self.assertEqual(out["open_before"], 1)
        self.assertEqual(out["open_after"], 0)


if __name__ == "__main__":
    unittest.main()
