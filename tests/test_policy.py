"""Policy chokepoint (v8 M3): the default-deny decision seam every lane funnels through.

Two test surfaces (the test_agent_runs.py idiom):
- PURE unit tests construct a PolicyEngine directly (no DB) and exhaust canonicalization, the four
  code INVARIANTS, the DB-rule lattice, stale-epoch, and per-item idempotency.
- DB-BACKED tests subclass IsolatedDBTest: X1 mints protected-path rows and C1/C4 drive approvals
  through the real CLI, while the in-process policy DB functions (store_rule/load_rules/
  load_protected_paths/build_engine_from_db/record_ask/replay_pending) run in a SEPARATE process with
  KAIZEN_REPO_ROOT pinned to the isolated root -- separate-process is the suite idiom for touching the
  isolated DB in-process (turso's Windows file lock; see test_schema._direct_sql).

Exit criteria proven here map 1:1 to the M3 contract; each test's docstring/name names its criterion.
"""

from __future__ import annotations

import json
import ntpath
import os
import subprocess
import sys
import unittest
from pathlib import Path

from _harness import REPO_ROOT, IsolatedDBTest

sys.path.insert(0, str(REPO_ROOT))

from kaizen_components.orchestration import policy as P  # noqa: E402


# --- helpers ----------------------------------------------------------------------------------

def act(
    verb: str,
    *,
    targets: tuple[str, ...] = (),
    command: str | None = None,
    engine: str = "codex",
    epoch: int = 1,
    thread: str | None = "t1",
    session: str = "sess",
    cwd: str | None = None,
) -> P.RequestedAction:
    """Construct a RequestedAction fixture and normalize tuple-valued targets and metadata defaults."""
    raw = {"cwd": cwd} if cwd else {}
    return P.RequestedAction(P.Actor(engine, session, epoch, thread), verb, tuple(targets), command, raw)


def engine(protected=(), rules=(), vendor=None, *, mode="ask", workspace=None, designated=()) -> P.PolicyEngine:
    """Construct the EngineCapabilities fixture used by the policy matrix."""
    return P.PolicyEngine(
        list(protected), list(rules), vendor,
        permission_mode=mode, workspace_root=workspace, designated_write_roots=list(designated),
    )


def rule(rule_type, verb, match_kind, pattern, *, engine=None, enabled=True, rid=None):
    """Construct an authority-rule fixture with the supplied effect, kinds, and target constraints."""
    return {
        "id": rid or f"r_{rule_type}_{match_kind}",
        "rule_type": rule_type,
        "verb": verb,
        "match_kind": match_kind,
        "pattern": pattern,
        "engine": engine,
        "enabled": enabled,
    }


# A scratch protected prefix minted only inside tests (drive/UNC/\\?\/WSL spellings all canonicalize
# to this). NEVER a real machine path -- the contract forbids hardcoding one in tracked source.
SCRATCH_PROTECTED = "c:\\kaizen-scratch\\protected"
SCRATCH_PROTECTED_FORMS = [
    "c:\\kaizen-scratch\\protected\\secret.txt",
    "C:/Kaizen-Scratch/Protected/secret.txt",
    "\\\\?\\c:\\kaizen-scratch\\protected\\secret.txt",
    "/mnt/c/kaizen-scratch/protected/secret.txt",
]


# --- pure canonicalization matrix -------------------------------------------------------------

class CanonicalizationTest(unittest.TestCase):
    def test_six_forms_of_one_path_canonicalize_identically(self):
        forms = [
            "C:\\x\\y",
            "c:/x/y",
            "\\\\?\\C:\\x\\y",
            "\\\\?\\c:\\x\\y",
            "/mnt/c/x/y",
            "C:\\X\\Y",  # case folds
        ]
        canon = {P.canonicalize_path(f) for f in forms}
        self.assertEqual(len(canon), 1, canon)
        self.assertEqual(canon.pop(), "c:\\x\\y")

    def test_unc_long_and_plain_forms_match(self):
        a = P.canonicalize_path("\\\\srv\\share\\x")
        b = P.canonicalize_path("\\\\?\\UNC\\srv\\share\\x")
        self.assertEqual(a, b)
        self.assertEqual(a, "\\\\srv\\share\\x")

    def test_posix_case_is_preserved(self):
        # A pure POSIX absolute path (not /mnt/<letter>) is case-sensitive: keep case.
        self.assertEqual(P.canonicalize_path("/etc/Passwd"), "/etc/Passwd")
        self.assertNotEqual(P.canonicalize_path("/etc/Passwd"), P.canonicalize_path("/etc/passwd"))

    def test_boundary_aware_prefix(self):
        p = P.canonicalize_path("c:\\foo")
        self.assertTrue(P.prefix_match(P.canonicalize_path("c:\\foo\\bar"), p))
        self.assertTrue(P.prefix_match(P.canonicalize_path("c:\\foo"), p))  # equal
        self.assertFalse(P.prefix_match(P.canonicalize_path("c:\\foobar"), p))  # NOT a child

    def test_relative_resolves_against_cwd(self):
        a = act("file_write", targets=("sub\\f.txt",), cwd="c:\\base")
        canon = a.canonical_targets()
        self.assertEqual(canon, ("c:\\base\\sub\\f.txt",))

    def test_relative_without_cwd_is_noncanonical_and_never_matches_allow(self):
        # No cwd -> stays non-canonical; a path_prefix allow over c:\base cannot match it.
        eng = engine(rules=[rule("allow", "file_write", "path_prefix", "c:\\base")])
        d = eng.decide(act("file_write", targets=("sub\\f.txt",)), 1)
        self.assertEqual(d.result, "ask")  # unmatched -> default ask (never allow)


# --- hard-deny suite (non-removable code invariants) ------------------------------------------

class HardDenyTest(unittest.TestCase):
    def test_git_push_via_git_verb(self):
        d = engine().decide(act("git", targets=("push", "origin", "main")), 1)
        self.assertEqual(d.result, "deny")
        self.assertEqual(d.invariant_id, P.INV_GIT_PUSH)

    def test_git_push_via_every_exec_string_variant(self):
        variants = [
            "git push",
            "git push origin main",
            "git -C repo push",
            "cd x && git push",
            "echo safe; git push --force",
            "D:\\tools\\git.exe push",  # full path, basename match (posix-shlex mangles -> raw split hits)
        ]
        for cmd in variants:
            d = engine().decide(act("exec", command=cmd), 1)
            self.assertEqual(d.result, "deny", cmd)
            self.assertEqual(d.invariant_id, P.INV_GIT_PUSH, cmd)

    def test_documented_over_deny_echo_git_push(self):
        # Conservative over-deny is accepted by design (documented in policy.py).
        d = engine().decide(act("exec", command="echo git push"), 1)
        self.assertEqual(d.result, "deny")
        self.assertEqual(d.invariant_id, P.INV_GIT_PUSH)

    def test_git_non_push_is_not_hard_denied(self):
        d = engine().decide(act("git", targets=("status",)), 1)
        self.assertNotEqual(d.result, "deny")

    def test_protected_path_file_write_denied_in_every_form(self):
        eng = engine(protected=[SCRATCH_PROTECTED])
        for form in SCRATCH_PROTECTED_FORMS:
            d = eng.decide(act("file_write", targets=(form,)), 1)
            self.assertEqual(d.result, "deny", form)
            self.assertEqual(d.invariant_id, P.INV_PROTECTED_PATH, form)

    def test_protected_boundary_sibling_is_not_denied(self):
        eng = engine(protected=[SCRATCH_PROTECTED])
        d = eng.decide(act("file_write", targets=("c:\\kaizen-scratch\\protected-sibling\\x",)), 1)
        self.assertNotEqual(d.result, "deny")

    def test_exec_redirect_into_protected_denied(self):
        eng = engine(protected=[SCRATCH_PROTECTED])
        for cmd in (
            "echo hi > c:\\kaizen-scratch\\protected\\x",
            "echo hi >> c:\\kaizen-scratch\\protected\\x",
        ):
            d = eng.decide(act("exec", command=cmd), 1)
            self.assertEqual(d.result, "deny", cmd)
            self.assertEqual(d.invariant_id, P.INV_PROTECTED_PATH, cmd)

    def test_exec_mutator_token_plus_protected_denied(self):
        eng = engine(protected=[SCRATCH_PROTECTED])
        d = eng.decide(act("exec", command="rm -rf c:\\kaizen-scratch\\protected\\x"), 1)
        self.assertEqual(d.result, "deny")
        self.assertEqual(d.invariant_id, P.INV_PROTECTED_PATH)

    def test_relative_file_write_under_protected_cwd_denied(self):
        # H2.0: a relative file_write target resolves against action.cwd; a protected cwd denies.
        eng = engine(protected=[SCRATCH_PROTECTED])
        d = eng.decide(act("file_write", targets=("child.txt",), cwd=SCRATCH_PROTECTED), 1)
        self.assertEqual(d.result, "deny")
        self.assertEqual(d.invariant_id, P.INV_PROTECTED_PATH)

    def test_relative_shell_redirect_under_protected_cwd_denied(self):
        # H2.0: a relative shell redirect (echo x > child.txt) resolves against cwd for the write-evidence
        # scan; a protected cwd denies. Pre-fix, _command_write_hits ignored cwd and this fell to ask.
        eng = engine(protected=[SCRATCH_PROTECTED])
        d = eng.decide(act("exec", command="echo x > child.txt", cwd=SCRATCH_PROTECTED), 1)
        self.assertEqual(d.result, "deny")
        self.assertEqual(d.invariant_id, P.INV_PROTECTED_PATH)

    def test_relative_shell_mutator_pathlike_under_protected_cwd_denied(self):
        # A mutator token (rm) with a relative PATH-LIKE token (has a separator, so the write-evidence
        # scan treats it as a path) resolves against cwd. A bare filename with no separator stays outside
        # the mutator path-token heuristic (pre-existing, unchanged) -- the redirect case above covers the
        # plan's named gap.
        eng = engine(protected=[SCRATCH_PROTECTED])
        d = eng.decide(act("exec", command="rm sub/child.txt", cwd=SCRATCH_PROTECTED), 1)
        self.assertEqual(d.result, "deny")
        self.assertEqual(d.invariant_id, P.INV_PROTECTED_PATH)

    def test_same_relative_write_under_nonprotected_cwd_is_not_denied(self):
        # Identical relative redirect under a NON-protected cwd behaves as before (not a hard-deny).
        eng = engine(protected=[SCRATCH_PROTECTED])
        d = eng.decide(act("exec", command="echo x > child.txt", cwd="c:\\somewhere\\else"), 1)
        self.assertNotEqual(d.result, "deny")

    def test_absolute_shell_redirect_unaffected_by_cwd_threading(self):
        # An absolute redirect into a protected path still denies regardless of cwd (behavior unchanged).
        eng = engine(protected=[SCRATCH_PROTECTED])
        d = eng.decide(
            act("exec", command="echo x > c:\\kaizen-scratch\\protected\\y", cwd="c:\\unrelated"), 1
        )
        self.assertEqual(d.result, "deny")
        self.assertEqual(d.invariant_id, P.INV_PROTECTED_PATH)

    def test_bare_protected_mention_without_write_evidence_falls_through(self):
        # A read of a protected path (no redirect, no mutator token) is NOT a hard-deny; default ask.
        eng = engine(protected=[SCRATCH_PROTECTED])
        d = eng.decide(act("exec", command="type c:\\kaizen-scratch\\protected\\x"), 1)
        self.assertEqual(d.result, "ask")

    def test_vendor_global_config_writes_denied(self):
        claude = os.path.expanduser("~/.claude/settings.json")
        codex = os.path.expanduser("~/.codex/config.toml")
        eng = engine()  # default vendor paths = the two expanduser files
        targets = [claude, codex]
        if os.name == "nt":
            targets.append("\\\\?\\" + claude.replace("/", "\\"))
        for target in targets:
            d = eng.decide(act("file_write", targets=(target,)), 1)
            self.assertEqual(d.result, "deny", target)
            self.assertEqual(d.invariant_id, P.INV_VENDOR_CONFIG, target)

    def test_non_removable_exact_allow_rule_still_denies_git_push(self):
        # An exact_command allow rule matching `git push origin main` must STILL deny (deny > allow;
        # the invariant is non-overridable).
        eng = engine(rules=[rule("allow", "exec", "exact_command", "git push origin main")])
        d = eng.decide(act("exec", command="git push origin main"), 1)
        self.assertEqual(d.result, "deny")
        self.assertEqual(d.invariant_id, P.INV_GIT_PUSH)

    def test_non_removable_path_prefix_allow_still_denies_protected_write(self):
        eng = engine(
            protected=[SCRATCH_PROTECTED],
            rules=[rule("allow", "file_write", "path_prefix", SCRATCH_PROTECTED)],
        )
        d = eng.decide(act("file_write", targets=(SCRATCH_PROTECTED + "\\x",)), 1)
        self.assertEqual(d.result, "deny")
        self.assertEqual(d.invariant_id, P.INV_PROTECTED_PATH)


# --- shipped protected floor (H2.0) -----------------------------------------------------------

class ShippedProtectedFloorTest(unittest.TestCase):
    """H2.0: OS-critical roots + per-user startup/profile files ship as a protected floor merged into
    EVERY engine at construction, so a fresh install with no operator X1 rows still hard-denies them
    above any DB rule. The floor stays narrow -- it must NOT catch generic user/temp scratch dirs."""

    def test_os_root_write_denied_on_ruleless_engine(self):
        eng = engine()  # no protected paths, no rules -- only the shipped floor
        for target in ("c:\\windows\\system32\\evil.dll", "C:\\Program Files\\app\\x.exe",
                       "c:\\programdata\\x", "/etc/passwd", "/usr/bin/x"):
            d = eng.decide(act("file_write", targets=(target,)), 1)
            self.assertEqual(d.result, "deny", target)
            self.assertEqual(d.invariant_id, P.INV_PROTECTED_PATH, target)

    def test_startup_folder_write_denied(self):
        floor = P.shipped_protected_paths()
        eng = engine()
        if os.name == "nt":
            # The per-user Startup folder is an auto-run vector; writes under it hard-deny.
            startup = next((p for p in floor if "startup" in p), None)
            self.assertIsNotNone(startup, floor)
            d = eng.decide(act("file_write", targets=(startup + "\\evil.lnk",)), 1)
            self.assertEqual(d.result, "deny")
            self.assertEqual(d.invariant_id, P.INV_PROTECTED_PATH)
        elif os.name == "posix":
            # POSIX protects the deliberate shell-startup file floor, not a Windows Startup folder.
            for name in (".bashrc", ".profile", ".zshrc", ".bash_profile", ".zprofile"):
                target = P.canonicalize_path(str(Path.home() / name))
                self.assertIn(target, floor)
                d = eng.decide(act("file_write", targets=(target,)), 1)
                self.assertEqual(d.result, "deny", target)
                self.assertEqual(d.invariant_id, P.INV_PROTECTED_PATH, target)

    def test_scratch_temp_write_is_not_caught_by_shipped_floor(self):
        # A write under a %TEMP%-style scratch dir must NOT be a hard-deny (tests use scratch temp roots).
        import tempfile
        eng = engine()
        target = ntpath.join(tempfile.gettempdir(), "kaizen-scratch", "x.txt")
        d = eng.decide(act("file_write", targets=(target,)), 1)
        self.assertNotEqual(d.result, "deny", target)

    def test_generic_appdata_not_caught_by_floor(self):
        # A generic AppData write (not the Startup folder, not a profile file) is not floor-protected.
        appdata = os.environ.get("APPDATA")
        if not appdata:
            self.skipTest("no APPDATA")
        eng = engine()
        d = eng.decide(act("file_write", targets=(ntpath.join(appdata, "SomeApp", "settings.ini"),)), 1)
        self.assertNotEqual(d.result, "deny")

    def test_db_allow_rule_cannot_override_the_floor(self):
        # A path_prefix allow rule over an OS root is powerless -- INV_PROTECTED_PATH sits above every rule.
        eng = engine(rules=[rule("allow", "file_write", "path_prefix", "c:\\windows")])
        d = eng.decide(act("file_write", targets=("c:\\windows\\system32\\x.dll",)), 1)
        self.assertEqual(d.result, "deny")
        self.assertEqual(d.invariant_id, P.INV_PROTECTED_PATH)


# --- DB-rule lattice (pure engine, rules injected) --------------------------------------------

class RuleLatticeTest(unittest.TestCase):
    def test_exact_command_allow_allows_with_rule_id(self):
        r = rule("allow", "exec", "exact_command", "npm run build", rid="r_build")
        eng = engine(rules=[r])
        d = eng.decide(act("exec", command="npm run build"), 1)
        self.assertEqual(d.result, "allow")
        self.assertEqual(d.rule_id, "r_build")

    def test_same_command_without_rule_is_ask(self):
        d = engine().decide(act("exec", command="npm run build"), 1)
        self.assertEqual(d.result, "ask")

    def test_deny_rule_beats_allow_rule(self):
        eng = engine(rules=[
            rule("allow", "exec", "exact_command", "do it", rid="r_allow"),
            rule("deny", "exec", "exact_command", "do it", rid="r_deny"),
        ])
        d = eng.decide(act("exec", command="do it"), 1)
        self.assertEqual(d.result, "deny")
        self.assertEqual(d.rule_id, "r_deny")

    def test_ask_rule_beats_allow_rule(self):
        # H2.0 precedence fix: deny > ASK > allow. An action matching BOTH an ask and an allow rule now
        # ASKS (fail-safe to human review), where the pre-fix order (deny > allow > ask) wrongly allowed.
        eng = engine(rules=[
            rule("allow", "exec", "exact_command", "do it", rid="r_allow"),
            rule("ask", "exec", "exact_command", "do it", rid="r_ask"),
        ])
        d = eng.decide(act("exec", command="do it"), 1)
        self.assertEqual(d.result, "ask")
        self.assertEqual(d.rule_id, "r_ask")

    def test_ask_via_path_prefix_beats_file_write_allow(self):
        # An ask path_prefix rule outranks an allow path_prefix rule over the same file_write target.
        eng = engine(rules=[
            rule("allow", "file_write", "path_prefix", "c:\\ws", rid="r_allow"),
            rule("ask", "file_write", "path_prefix", "c:\\ws", rid="r_ask"),
        ])
        d = eng.decide(act("file_write", targets=("c:\\ws\\out.txt",)), 1)
        self.assertEqual(d.result, "ask")
        self.assertEqual(d.rule_id, "r_ask")

    def test_deny_still_beats_both_ask_and_allow(self):
        eng = engine(rules=[
            rule("allow", "exec", "exact_command", "do it", rid="r_allow"),
            rule("ask", "exec", "exact_command", "do it", rid="r_ask"),
            rule("deny", "exec", "exact_command", "do it", rid="r_deny"),
        ])
        d = eng.decide(act("exec", command="do it"), 1)
        self.assertEqual(d.result, "deny")
        self.assertEqual(d.rule_id, "r_deny")

    def test_allow_still_wins_when_no_ask_or_deny_matches(self):
        # Precedence change must not regress the plain allow path: an allow with no competing ask/deny
        # still allows.
        eng = engine(rules=[rule("allow", "exec", "exact_command", "do it", rid="r_allow")])
        d = eng.decide(act("exec", command="do it"), 1)
        self.assertEqual(d.result, "allow")
        self.assertEqual(d.rule_id, "r_allow")

    def test_suppress_allow_still_blocks_allow_under_new_order(self):
        # A stale non-mutating action with a matching allow rule (and no ask/deny) must still degrade to
        # ask -- the allow lattice stays suppressed regardless of the ask-before-allow reordering.
        eng = engine(rules=[rule("allow", "file_read", "path_prefix", "c:\\ok", rid="r_read")])
        d = eng.decide(act("file_read", targets=("c:\\ok\\f",), epoch=0), 1)
        self.assertEqual(d.result, "ask")

    def test_disabled_rule_is_ignored(self):
        eng = engine(rules=[rule("allow", "exec", "exact_command", "risky", enabled=False)])
        d = eng.decide(act("exec", command="risky"), 1)
        self.assertEqual(d.result, "ask")  # disabled -> unmatched -> default ask

    def test_engine_scoped_rule_matches_only_its_engine(self):
        # Distinct threads per engine (a thread belongs to one engine; the dedupe key is
        # (thread, epoch, normal-form), so same-thread cross-engine would collide by design).
        eng = engine(rules=[rule("allow", "exec", "exact_command", "cmd x", engine="codex", rid="r_codex")])
        d_codex = eng.decide(act("exec", command="cmd x", engine="codex", thread="codex-thread"), 1)
        self.assertEqual(d_codex.result, "allow")
        d_claude = eng.decide(act("exec", command="cmd x", engine="claude", thread="claude-thread"), 1)
        self.assertEqual(d_claude.result, "ask")  # engine mismatch -> rule does not apply

    def test_null_engine_rule_matches_any_engine(self):
        eng = engine(rules=[rule("allow", "exec", "exact_command", "cmd y", engine=None)])
        self.assertEqual(eng.decide(act("exec", command="cmd y", engine="codex", thread="ct"), 1).result, "allow")
        self.assertEqual(eng.decide(act("exec", command="cmd y", engine="claude", thread="lt"), 1).result, "allow")

    def test_path_prefix_allow_never_allows_an_exec_command(self):
        # A path_prefix allow rule must NOT widen an opaque shell command (only exact_command can).
        eng = engine(rules=[rule("allow", "exec", "path_prefix", "c:\\workspace")])
        d = eng.decide(act("exec", command="do c:\\workspace\\thing"), 1)
        self.assertEqual(d.result, "ask")  # not allowed via path_prefix

    def test_path_prefix_allow_allows_a_file_write_target(self):
        eng = engine(rules=[rule("allow", "file_write", "path_prefix", "c:\\workspace", rid="r_ws")])
        d = eng.decide(act("file_write", targets=("c:\\workspace\\out.txt",)), 1)
        self.assertEqual(d.result, "allow")
        self.assertEqual(d.rule_id, "r_ws")

    def test_verb_any_rule_matches_every_verb(self):
        eng = engine(rules=[rule("deny", "any", "path_prefix", "c:\\nogo", rid="r_any")])
        self.assertEqual(eng.decide(act("file_read", targets=("c:\\nogo\\x",)), 1).result, "deny")
        self.assertEqual(eng.decide(act("file_write", targets=("c:\\nogo\\y",)), 1).result, "deny")


# --- stale epoch ------------------------------------------------------------------------------

class StaleEpochTest(unittest.TestCase):
    def test_mutating_verbs_deny_off_stale_epoch(self):
        eng = engine()
        for verb, kwargs in (
            ("file_write", {"targets": ("c:\\ok\\f",)}),
            ("git", {"targets": ("commit",)}),
            ("exec", {"command": "echo hi"}),
            ("spawn", {"command": "run tool"}),
            ("net", {"targets": ("https://example.com",)}),
        ):
            d = eng.decide(act(verb, epoch=0, **kwargs), 1)  # actor epoch 0 != current 1
            self.assertEqual(d.result, "deny", verb)
            self.assertEqual(d.invariant_id, P.INV_STALE_EPOCH, verb)

    def test_non_mutating_stale_asks_never_allows(self):
        eng = engine(rules=[rule("allow", "file_read", "path_prefix", "c:\\ok", rid="r_read")])
        # Even with a matching allow rule, a stale non-mutating action degrades to ask (never allow).
        d = eng.decide(act("file_read", targets=("c:\\ok\\f",), epoch=0), 1)
        self.assertEqual(d.result, "ask")


# --- per-item gating + idempotency ------------------------------------------------------------

class GatingIdempotencyTest(unittest.TestCase):
    def test_identical_thread_epoch_normalform_returns_same_object(self):
        eng = engine()
        a = act("exec", command="echo same", thread="t7", epoch=3)
        first = eng.decide(a, 3)
        second = eng.decide(a, 3)
        self.assertIs(first, second)  # SAME cached Decision object, re-assertable

    def test_whitespace_variants_share_a_decision(self):
        eng = engine()
        d1 = eng.decide(act("exec", command="echo   spaced", thread="t7", epoch=3), 3)
        d2 = eng.decide(act("exec", command="echo spaced", thread="t7", epoch=3), 3)
        self.assertIs(d1, d2)  # command-normal-form collapses whitespace

    def test_n_distinct_commands_get_n_independent_decisions(self):
        eng = engine()
        cmds = [f"echo item{i}" for i in range(5)]
        decisions = [eng.decide(act("exec", command=c, thread="batch", epoch=1), 1) for c in cmds]
        keys = {d.dedupe_key for d in decisions}
        self.assertEqual(len(keys), 5)  # per-item gating: no two items share a decision
        self.assertEqual(len({id(d) for d in decisions}), 5)

    def test_different_items_never_share_a_decision(self):
        eng = engine()
        a = act("file_write", targets=("c:\\a\\x",), thread="t", epoch=1)
        b = act("file_write", targets=("c:\\b\\y",), thread="t", epoch=1)
        self.assertNotEqual(a.dedupe_key(), b.dedupe_key())
        self.assertIsNot(eng.decide(a, 1), eng.decide(b, 1))

    def test_cross_actor_identical_key_never_shares_a_cached_decision(self):
        # Rule matching is engine-scoped, so two actors sharing a (thread=None, epoch, normal-form)
        # dedupe key must not share a cache entry: an engine-A allow leaking to engine B widens policy.
        eng = engine(rules=[rule("allow", "exec", "exact_command", "echo hi", engine="codex", rid="r_a")])
        a = act("exec", command="echo hi", engine="codex", session="sa", thread=None, epoch=1)
        b = act("exec", command="echo hi", engine="local-llm", session="sb", thread=None, epoch=1)
        self.assertEqual(a.dedupe_key(), b.dedupe_key())  # same contract key...
        da, db = eng.decide(a, 1), eng.decide(b, 1)
        self.assertEqual(da.result, "allow")
        self.assertEqual(db.result, "ask")  # ...but never a shared decision


# --- subagent inheritance + cross-adapter chokepoint stub -------------------------------------

class FakeAdapter:
    """Cross-adapter chokepoint seam (M4/M6 fulfill this): maps a vendor-ish request dict into a
    RequestedAction, then hands it to decide(). This is the ONE place every future adapter converges;
    the mapping here is deliberately minimal (the real vendor translation lands at M4/M6)."""

    def __init__(self, eng: P.PolicyEngine, current_epoch: int) -> None:
        self.engine = eng
        self.current_epoch = current_epoch

    def to_action(self, req: dict) -> P.RequestedAction:
        actor = P.Actor(
            engine=req.get("engine", "codex"),
            session_id=req.get("session_id", "s"),
            epoch=req.get("epoch", self.current_epoch),
            thread_id=req.get("thread_id"),
        )
        return P.RequestedAction(
            actor=actor,
            verb=req["verb"],
            targets=tuple(req.get("targets", ())),
            command=req.get("command"),
            raw=req.get("raw", {}),
        )

    def gate(self, req: dict) -> P.Decision:
        return self.engine.decide(self.to_action(req), self.current_epoch)


class ChokepointStubTest(unittest.TestCase):
    def test_subagent_child_hits_the_same_invariants(self):
        # A child session (different session_id, same engine instance) is gated by the SAME invariants
        # as its parent -- inheritance is structural (the engine is per-daemon, not per-session).
        eng = engine(protected=[SCRATCH_PROTECTED])
        parent = eng.decide(act("file_write", targets=(SCRATCH_PROTECTED + "\\x",), session="parent"), 1)
        child = eng.decide(act("file_write", targets=(SCRATCH_PROTECTED + "\\x",), session="child_sub"), 1)
        self.assertEqual(parent.result, "deny")
        self.assertEqual(child.result, "deny")
        self.assertEqual(child.invariant_id, P.INV_PROTECTED_PATH)

    def test_fake_adapter_maps_vendor_request_to_decision(self):
        eng = engine(rules=[rule("allow", "exec", "exact_command", "safe cmd", rid="r_safe")])
        adapter = FakeAdapter(eng, current_epoch=1)
        allow = adapter.gate({"verb": "exec", "command": "safe cmd", "thread_id": "v1"})
        self.assertEqual(allow.result, "allow")
        deny = adapter.gate({"verb": "exec", "command": "git push", "thread_id": "v2"})
        self.assertEqual(deny.result, "deny")
        self.assertEqual(deny.invariant_id, P.INV_GIT_PUSH)


# --- permission mode matrix (H2.1) ------------------------------------------------------------

# One canonical workspace + a designated Plan write root under it, plus paths that classify to each
# matrix cell. NEVER a real machine path (scratch-only, per the contract).
WS = "c:\\kaizen-scratch\\ws"
DESIGNATED = "c:\\kaizen-scratch\\ws\\ai\\work"


def mode_engine(mode, *, rules=(), protected=()):
    """A mode-configured engine over the scratch workspace + one designated root."""
    return engine(protected=protected, rules=rules, mode=mode, workspace=WS, designated=[DESIGNATED])


# The eight matrix cells expressed as (label -> action factory). Each classifies to exactly one cell.
def _cell_actions():
    """Return the canonical action set exercised for each engine and permission-mode matrix cell."""
    return {
        "read_in": lambda: act("file_read", targets=(WS + "\\src\\f",)),
        "read_out": lambda: act("file_read", targets=("c:\\kaizen-scratch\\elsewhere\\f",)),
        "write_designated": lambda: act("file_write", targets=(DESIGNATED + "\\out.txt",)),
        "write_workspace": lambda: act("file_write", targets=(WS + "\\src\\out.txt",)),
        "write_out": lambda: act("file_write", targets=("c:\\kaizen-scratch\\elsewhere\\out.txt",)),
        "exec": lambda: act("exec", command="ls -la"),
        "net": lambda: act("net", targets=("https://example.com",)),
        "git": lambda: act("git", targets=("status",)),
    }


# The owner-locked expected result per (mode, cell). Mirrors the plan table verbatim.
_EXPECTED = {
    "plan": {
        "read_in": "allow", "read_out": "ask", "write_designated": "allow",
        "write_workspace": "deny", "write_out": "deny", "exec": "deny", "net": "deny", "git": "deny",
    },
    "ask": {
        "read_in": "allow", "read_out": "ask", "write_designated": "ask",
        "write_workspace": "ask", "write_out": "ask", "exec": "ask", "net": "ask", "git": "ask",
    },
    "agent": {
        "read_in": "allow", "read_out": "ask", "write_designated": "allow",
        "write_workspace": "allow", "write_out": "ask", "exec": "ask", "net": "ask", "git": "ask",
    },
    "full": {
        "read_in": "allow", "read_out": "allow", "write_designated": "allow",
        "write_workspace": "allow", "write_out": "allow", "exec": "allow", "net": "allow", "git": "allow",
    },
}


class ModeMatrixTest(unittest.TestCase):
    """Every mode x every cell decides exactly as the owner-locked matrix says, with NO explicit DB rule
    present (so the pure mode default/ceiling is what is measured)."""

    def test_full_matrix_default_cells(self):
        actions = _cell_actions()
        for mode, cells in _EXPECTED.items():
            # A fresh engine per cell so the per-cell dedupe cache never crosses actions.
            for cell, expected in cells.items():
                eng = mode_engine(mode)
                d = eng.decide(actions[cell](), 1)
                self.assertEqual(d.result, expected, f"{mode}/{cell}")

    def test_unknown_mode_falls_to_ask_posture(self):
        # An unrecognized mode string must degrade to the no-ceiling 'ask' posture, never a silent 'full'.
        eng = engine(mode="bogus", workspace=WS, designated=[DESIGNATED])
        self.assertEqual(eng.permission_mode, "ask")
        self.assertEqual(eng.decide(act("exec", command="ls"), 1).result, "ask")


class ModeCeilingTest(unittest.TestCase):
    """A mode CEILING (a DENY cell) is a hard deny an explicit DB allow can NOT widen; a mode DEFAULT
    (Allow/Ask cell) yields only when no explicit rule matched."""

    def test_plan_ceiling_not_widened_by_explicit_allow(self):
        # An explicit exact_command allow over an exec cannot lift plan's exec ceiling.
        eng = mode_engine("plan", rules=[rule("allow", "exec", "exact_command", "ls -la", rid="r_ok")])
        d = eng.decide(act("exec", command="ls -la"), 1)
        self.assertEqual(d.result, "deny")
        self.assertEqual(d.invariant_id, "MODE_CEILING:exec")

    def test_plan_workspace_write_ceiling_not_widened_by_allow(self):
        # A path_prefix allow over a workspace path cannot lift plan's other-workspace-write ceiling.
        eng = mode_engine("plan", rules=[rule("allow", "file_write", "path_prefix", WS, rid="r_ws")])
        d = eng.decide(act("file_write", targets=(WS + "\\src\\x",)), 1)
        self.assertEqual(d.result, "deny")
        self.assertEqual(d.invariant_id, "MODE_CEILING:write_workspace")

    def test_plan_designated_write_allowed_while_sibling_workspace_write_denied(self):
        # The named exit criterion: a designated-root write is Allow in plan while a sibling
        # non-designated workspace write DENIES (ceiling) -- same engine, two targets.
        eng = mode_engine("plan")
        allow = eng.decide(act("file_write", targets=(DESIGNATED + "\\out.txt",)), 1)
        deny = eng.decide(act("file_write", targets=(WS + "\\src\\out.txt",)), 1)
        self.assertEqual(allow.result, "allow")
        self.assertEqual(deny.result, "deny")
        self.assertEqual(deny.invariant_id, "MODE_CEILING:write_workspace")

    def test_explicit_deny_still_denies_designated_write_in_plan(self):
        # Precedence: an explicit owner deny outranks the mode's designated-write Allow default.
        eng = mode_engine("plan", rules=[rule("deny", "file_write", "path_prefix", DESIGNATED, rid="r_d")])
        d = eng.decide(act("file_write", targets=(DESIGNATED + "\\out.txt",)), 1)
        self.assertEqual(d.result, "deny")
        self.assertEqual(d.rule_id, "r_d")

    def test_explicit_ask_still_asks_designated_write_in_plan(self):
        # An explicit owner ask outranks the designated-write Allow default (deny > ask > allow > default).
        eng = mode_engine("plan", rules=[rule("ask", "file_write", "path_prefix", DESIGNATED, rid="r_a")])
        d = eng.decide(act("file_write", targets=(DESIGNATED + "\\out.txt",)), 1)
        self.assertEqual(d.result, "ask")
        self.assertEqual(d.rule_id, "r_a")


class FullModeHonorsExplicitRulesTest(unittest.TestCase):
    """Full mode allows by default but STILL honors code invariants AND explicit owner deny AND explicit
    owner ask -- an explicit ask must still ask in full."""

    def test_full_default_allows(self):
        eng = mode_engine("full")
        self.assertEqual(eng.decide(act("exec", command="rm -rf x"), 1).result, "allow")

    def test_full_honors_explicit_deny(self):
        eng = mode_engine("full", rules=[rule("deny", "exec", "exact_command", "danger", rid="r_d")])
        d = eng.decide(act("exec", command="danger"), 1)
        self.assertEqual(d.result, "deny")
        self.assertEqual(d.rule_id, "r_d")

    def test_full_honors_explicit_ask(self):
        # The named criterion: an explicit ask rule must STILL ask in full (not be swallowed by the
        # allow-by-default).
        eng = mode_engine("full", rules=[rule("ask", "exec", "exact_command", "review me", rid="r_a")])
        d = eng.decide(act("exec", command="review me"), 1)
        self.assertEqual(d.result, "ask")
        self.assertEqual(d.rule_id, "r_a")

    def test_full_still_honors_invariants(self):
        # git push is an invariant deny even in full.
        d = mode_engine("full").decide(act("git", targets=("push", "origin", "main")), 1)
        self.assertEqual(d.result, "deny")
        self.assertEqual(d.invariant_id, P.INV_GIT_PUSH)

    def test_full_still_honors_shipped_floor(self):
        d = engine(mode="full").decide(act("file_write", targets=("c:\\windows\\system32\\x.dll",)), 1)
        self.assertEqual(d.result, "deny")
        self.assertEqual(d.invariant_id, P.INV_PROTECTED_PATH)


# --- immutable snapshots (H2.1) ---------------------------------------------------------------

class PolicySnapshotTest(unittest.TestCase):
    """Immutable per-session snapshots: two snapshots (plan vs full) decide the same action differently
    with zero cross-leakage; mutating the source rules list AFTER snapshot build does not change snapshot
    decisions; profile_hash is stable across construction order and changes when any input changes."""

    def _plan_snapshot(self, rules=None):
        return P.build_policy_snapshot(
            "codex", "plan", WS, [DESIGNATED], list(rules or []), protected_paths=[],
        )

    def test_two_snapshots_decide_the_same_action_differently(self):
        plan = self._plan_snapshot()
        full = P.build_policy_snapshot("codex", "full", WS, [DESIGNATED], [], protected_paths=[])
        a = act("exec", command="ls -la")
        self.assertEqual(plan.decide(a, 1).result, "deny")   # plan exec ceiling
        self.assertEqual(full.decide(a, 1).result, "allow")  # full default
        # Zero cross-leakage: re-deciding through plan again is unchanged.
        self.assertEqual(plan.decide(a, 1).result, "deny")

    def test_engine_from_snapshot_matches_snapshot_decide(self):
        plan = self._plan_snapshot()
        a = act("file_write", targets=(DESIGNATED + "\\x",))
        eng = P.PolicyEngine.from_snapshot(plan)
        self.assertEqual(eng.decide(a, 1).result, plan.decide(a, 1).result)
        self.assertEqual(eng.decide(a, 1).result, "allow")

    def test_mutating_source_rules_after_build_does_not_change_snapshot(self):
        # Build a snapshot from a rules list, THEN mutate the list -- the snapshot's decisions are frozen.
        src = [rule("deny", "exec", "exact_command", "ls -la", rid="r_deny")]
        snap = P.build_policy_snapshot("codex", "full", WS, [], src, protected_paths=[])
        a = act("exec", command="ls -la")
        self.assertEqual(snap.decide(a, 1).result, "deny")  # captured deny rule fires
        src.clear()                                          # later DB edit removes the rule
        src.append(rule("allow", "exec", "exact_command", "ls -la", rid="r_allow"))
        # Snapshot is immune: still deny (the materialized copy did not change).
        self.assertEqual(snap.decide(a, 1).result, "deny")

    def test_profile_hash_stable_across_construction_order(self):
        a = P.build_policy_snapshot("codex", "plan", WS, ["c:\\a", "c:\\b"], [], protected_paths=[])
        b = P.build_policy_snapshot("codex", "plan", WS, ["c:\\b", "c:\\a"], [], protected_paths=[])
        self.assertEqual(a.profile_hash, b.profile_hash)

    def test_profile_hash_changes_when_mode_changes(self):
        a = P.build_policy_snapshot("codex", "plan", WS, [DESIGNATED], [], protected_paths=[])
        b = P.build_policy_snapshot("codex", "full", WS, [DESIGNATED], [], protected_paths=[])
        self.assertNotEqual(a.profile_hash, b.profile_hash)

    def test_profile_hash_changes_when_designated_roots_change(self):
        a = P.build_policy_snapshot("codex", "plan", WS, [DESIGNATED], [], protected_paths=[])
        b = P.build_policy_snapshot("codex", "plan", WS, [DESIGNATED, "c:\\extra"], [], protected_paths=[])
        self.assertNotEqual(a.profile_hash, b.profile_hash)

    def test_profile_hash_changes_when_engine_changes(self):
        a = P.build_policy_snapshot("codex", "plan", WS, [DESIGNATED], [], protected_paths=[])
        b = P.build_policy_snapshot("claude", "plan", WS, [DESIGNATED], [], protected_paths=[])
        self.assertNotEqual(a.profile_hash, b.profile_hash)

    def test_profile_hash_changes_when_workspace_changes(self):
        a = P.build_policy_snapshot("codex", "plan", WS, [DESIGNATED], [], protected_paths=[])
        b = P.build_policy_snapshot("codex", "plan", "c:\\kaizen-scratch\\ws2", [], [], protected_paths=[])
        self.assertNotEqual(a.profile_hash, b.profile_hash)

    def test_profile_hash_changes_when_a_rule_changes(self):
        r1 = [rule("allow", "exec", "exact_command", "cmd a", rid="r1")]
        r2 = [rule("allow", "exec", "exact_command", "cmd b", rid="r1")]  # same id, different pattern
        a = P.build_policy_snapshot("codex", "plan", WS, [], r1, protected_paths=[])
        b = P.build_policy_snapshot("codex", "plan", WS, [], r2, protected_paths=[])
        self.assertNotEqual(a.profile_hash, b.profile_hash)

    def test_rule_order_does_not_change_hash(self):
        r_ab = [
            rule("allow", "exec", "exact_command", "cmd a", rid="ra"),
            rule("deny", "exec", "exact_command", "cmd b", rid="rb"),
        ]
        r_ba = list(reversed(r_ab))
        a = P.build_policy_snapshot("codex", "plan", WS, [], r_ab, protected_paths=[])
        b = P.build_policy_snapshot("codex", "plan", WS, [], r_ba, protected_paths=[])
        self.assertEqual(a.profile_hash, b.profile_hash)

    def test_snapshot_captures_shipped_floor_and_versions(self):
        snap = self._plan_snapshot()
        self.assertEqual(snap.permission_mode_version, P.PERMISSION_MODE_VERSION)
        self.assertEqual(snap.protected_path_version, P.PROTECTED_PATH_VERSION)
        # The shipped OS-root floor is captured in the snapshot's protected set.
        self.assertTrue(any("windows" in p for p in snap.protected_paths), snap.protected_paths)
        # And decisions off the snapshot enforce it.
        d = snap.decide(act("file_write", targets=("c:\\windows\\system32\\x.dll",)), 1)
        self.assertEqual(d.result, "deny")
        self.assertEqual(d.invariant_id, P.INV_PROTECTED_PATH)

    def test_snapshot_is_frozen(self):
        snap = self._plan_snapshot()
        with self.assertRaises(Exception):
            snap.permission_mode = "full"  # type: ignore[misc]


# --- DB-backed tests (IsolatedDBTest) ---------------------------------------------------------

def _run_policy_snippet(root: Path, snippet: str) -> dict:
    """Run a policy snippet in a SEPARATE process with KAIZEN_REPO_ROOT pinned to the isolated root.
    The snippet must ``print(json.dumps(result))`` on the last line. Separate-process is the suite idiom
    for touching the isolated DB in-process (turso Windows lock; cf. test_schema._direct_sql)."""
    prelude = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "from kaizen_components.orchestration import policy as P\n"
    )
    env = dict(os.environ)
    env["KAIZEN_REPO_ROOT"] = str(root)
    proc = subprocess.run(
        [sys.executable, "-c", prelude + snippet],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )
    if proc.returncode != 0:
        raise AssertionError(f"policy snippet failed: {proc.stderr or proc.stdout}")
    lines = proc.stdout.strip().splitlines()
    if not lines:
        raise AssertionError(f"policy snippet produced no stdout: {proc.stderr}")
    out = lines[-1]
    return json.loads(out)


class ProtectedPathLoaderTest(IsolatedDBTest):
    """Exit criterion: load_protected_paths reads active trigger='protected-path' X1 rows, canonicalized;
    build_engine_from_db denies a write under one. Protected paths are LOADED, never hardcoded."""

    def test_x1_protected_path_loads_and_denies_write(self):
        rc, p = self.kz(
            "X1", "--title", "Scratch protected prefix",
            "--trigger", "protected-path",
            "--summary", "Guard prefix for the M3 test.",
            "--body", "c:\\kaizen-scratch\\protected",
        )
        self.assertEqual(rc, 0, p)
        result = _run_policy_snippet(
            self.root,
            "prot = P.load_protected_paths()\n"
            "eng = P.build_engine_from_db()\n"
            "a = P.RequestedAction(P.Actor('codex','s',1,'t'),'file_write',"
            "('c:\\\\kaizen-scratch\\\\protected\\\\x',),None,{})\n"
            "d = eng.decide(a, 1)\n"
            "print(json.dumps({'prot': prot, 'result': d.result, 'inv': d.invariant_id}))\n",
        )
        self.assertIn("c:\\kaizen-scratch\\protected", result["prot"])
        self.assertEqual(result["result"], "deny")
        self.assertEqual(result["inv"], "INV_PROTECTED_PATH")


class AuthorityRuleStoreTest(IsolatedDBTest):
    """store_rule persists + load_rules loads; a garbage row is skipped-and-counted, never raised."""

    def test_store_and_load_roundtrip(self):
        result = _run_policy_snippet(
            self.root,
            "rid = P.store_rule({'rule_type':'allow','verb':'exec','match_kind':'exact_command',"
            "'pattern':'echo ok','summary':'allow echo ok'})\n"
            "rules = P.load_rules()\n"
            "print(json.dumps({'rid': rid, 'count': len(rules), 'skipped': getattr(P.load_rules,'skipped',None)}))\n",
        )
        self.assertTrue(result["rid"].startswith("aurule_"), result)
        self.assertEqual(result["count"], 1, result)
        self.assertEqual(result["skipped"], 0, result)

    def test_loader_skips_an_invalid_row(self):
        # Insert a garbage row directly (separate process, releases the turso lock), then load_rules
        # must survive and count it as skipped.
        db_path = self.root / "AI" / "db" / "kaizen.db"
        insert = (
            "import sys, turso\n"
            "conn = turso.connect(sys.argv[1])\n"
            "conn.execute(\"INSERT INTO authority_rules (id, created_at, updated_at, rule_type, verb, "
            "match_kind, pattern, engine, enabled, summary, content_hash, is_test) VALUES "
            "('bad_row','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00','NONSENSE','weird',"
            "'nope','','',1,'garbage','h',0)\")\n"
            "conn.commit(); conn.close()\n"
        )
        proc = subprocess.run([sys.executable, "-c", insert, str(db_path)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        result = _run_policy_snippet(
            self.root,
            "P.store_rule({'rule_type':'deny','verb':'net','match_kind':'exact_command',"
            "'pattern':'net exfil','summary':'deny exfil'})\n"
            "rules = P.load_rules()\n"
            "print(json.dumps({'count': len(rules), 'skipped': getattr(P.load_rules,'skipped',None)}))\n",
        )
        self.assertEqual(result["count"], 1, result)   # only the valid rule loads
        self.assertGreaterEqual(result["skipped"], 1, result)  # garbage row counted


class ApprovalPersistReplayTest(IsolatedDBTest):
    """Exit criterion: an ask persists ONE open C4 approval; a duplicate ask keeps one row; a fresh
    engine's replay_pending() surfaces it; after C4 approve it is no longer pending."""

    def test_record_ask_creates_one_row_and_replay_surfaces_it(self):
        rc, s = self.kz("C1", "--summary", "Governed session for policy asks.")
        self.assertEqual(rc, 0, s)
        sid = s["id"]
        result = _run_policy_snippet(
            self.root,
            "eng = P.build_engine_from_db()\n"
            f"sid = {sid!r}\n"
            "a = P.RequestedAction(P.Actor('codex',sid,1,'tX'),'exec',(),"
            "'rm scratchfile',{})\n"
            "d = eng.decide(a, 1)\n"
            "r1 = eng.record_ask(d, sid)\n"
            "r2 = eng.record_ask(d, sid)\n"  # duplicate ask, same key -> no new row
            "fresh = P.build_engine_from_db()\n"
            "pending = fresh.replay_pending(sid)\n"
            "print(json.dumps({'ask': d.result, 'created1': r1['created'], 'created2': r2['created'],"
            " 'id1': r1['id'], 'id2': r2['id'], 'pending': len(pending), 'corr': pending[0]['correlation_id'] if pending else None}))\n",
        )
        self.assertEqual(result["ask"], "ask", result)
        self.assertTrue(result["created1"], result)
        self.assertFalse(result["created2"], result)          # idempotent: no duplicate open row
        self.assertEqual(result["id1"], result["id2"], result)
        self.assertEqual(result["pending"], 1, result)         # fresh engine re-surfaces it
        approval_id = result["id1"]

        # C5 confirms exactly one open approval, then C4 approves it.
        rc, tl = self.kz("C5", "--session-id", sid)
        self.assertEqual(rc, 0, tl)
        self.assertEqual(tl["counts"]["approvals"], 1, tl)
        rc, ap = self.kz("C4", "--id", approval_id, "--status", "approved",
                         "--payload-json", '{"decided_by":"human"}')
        self.assertEqual(rc, 0, ap)
        self.assertEqual(ap["state"], "approved", ap)

        # After approval the row is no longer pending.
        after = _run_policy_snippet(
            self.root,
            "eng = P.build_engine_from_db()\n"
            f"pending = eng.replay_pending({sid!r})\n"
            "print(json.dumps({'pending': len(pending)}))\n",
        )
        self.assertEqual(after["pending"], 0, after)


class SupervisorBootWiringTest(unittest.TestCase):
    """Exit criterion: Supervisor.boot() builds the policy engine and reports policy_loaded + counts.
    Drives the real CLI daemon boot (the --exit-after-boot seam) with an X1 protected-path row present,
    then asserts the in-process boot summary reports policy_loaded with the loaded prefix counted."""

    def test_boot_reports_policy_loaded(self):
        import tempfile
        from _harness import kaizen, run

        root = Path(tempfile.mkdtemp(prefix="kaizen-m3-"))
        self.addCleanup(__import__("shutil").rmtree, root, ignore_errors=True)
        rc, _ = kaizen(root, "K1")
        self.assertEqual(rc, 0)
        # Mint a protected-path row so the boot engine loads at least one prefix.
        rc, _ = kaizen(
            root, "X1", "--title", "Boot guard", "--trigger", "protected-path",
            "--summary", "Guard prefix present at boot.", "--body", "c:\\kaizen-scratch\\bootguard",
        )
        self.assertEqual(rc, 0)
        # Boot the supervisor in-process against the isolated root and read its boot summary.
        result = _run_policy_snippet(
            root,
            "from kaizen_components.orchestration.supervisor import Supervisor\n"
            "from kaizen_components.paths import REPO_ROOT\n"
            "sup = Supervisor(REPO_ROOT)\n"
            "summary = sup.boot()\n"
            "sup.shutdown()\n"
            "print(json.dumps({'policy_loaded': summary.get('policy_loaded'),"
            " 'protected': summary['policy']['protected_paths'] if summary.get('policy') else None}))\n",
        )
        self.assertTrue(result["policy_loaded"], result)
        self.assertGreaterEqual(result["protected"], 1, result)


class SnapshotSerializationTest(unittest.TestCase):
    """Conversation continuation (2026-07-10): a stored snapshot must rehydrate VERBATIM -- equality,
    hash identity, and decisions all survive the JSON round-trip (no live re-canonicalization)."""

    def test_snapshot_round_trips_verbatim(self):
        snap = P.build_policy_snapshot(
            engine="local_llm",
            permission_mode="plan",
            workspace_root="D:/w",
            designated_write_roots=["D:/w/AI/work", "D:/w/AI/generation"],
            rules=[{
                "id": "r_allow", "rule_type": "allow", "verb": "file_write",
                "match_kind": "path_prefix", "pattern": "D:/w/AI/work", "engine": None, "enabled": True,
            }],
            protected_paths=["C:/protected"],
            vendor_config_paths=[],
        )
        again = P.snapshot_from_json(P.snapshot_to_json(snap))
        self.assertEqual(again, snap)
        self.assertEqual(again.profile_hash, snap.profile_hash)
        self.assertEqual(again.rule_dicts(), snap.rule_dicts())

    def test_malformed_stored_snapshot_raises_for_fail_closed_callers(self):
        with self.assertRaises((ValueError, KeyError, TypeError)):
            P.snapshot_from_json("{\"engine\": \"local_llm\"}")
        with self.assertRaises((ValueError, KeyError, TypeError)):
            P.snapshot_from_json("not-json")


if __name__ == "__main__":
    unittest.main()
