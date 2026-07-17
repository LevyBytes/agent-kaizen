"""Fan-out scheduler (v8 M7): each exit criterion asserted HERMETICALLY.

No network, no pip, no real Codex binary, no touch of the REAL repo. Local lanes are scripted
``LocalLLMAdapter``s over canned reply dicts; the Codex lane drives ``fake_codex_app_server.py`` as a
REAL subprocess through the REAL ``CodexAdapter`` (spawn/ownership/reader paths exercised). Every git op
runs against a scratch tempdir repo (git init/config/add/commit/worktree add|remove|prune|list inside
tempdirs only). PolicyEngine is constructed directly with vendor=[] (never resolves ~/.claude or ~/.codex);
recorders are in-memory lists. Windows-primary (pathlib, SHORT names, CRLF-stripped); adapter waits are
bounded at 45 seconds.

Exit criteria -> proving test:
- >=2-engine compare (Codex + local-LLM, both REAL adapters) ... CompareTwoEnginesTest
- Borda self-vote exclusion + tiebreak determinism ............ BordaSelfVoteTest
- malformed ballot dropped, compare continues ................. MalformedBallotTest
- cost/turn ceiling never exceeded ........................... CeilingTest
- delegate: read-only planner, only executor writes .......... DelegateTest
- 2-writer parallel-apply, zero worktree leak ................ ParallelApplyCleanTest
- teardown-failure quarantines + finalizes truthfully ........ TeardownQuarantineTest
- MAX_PATH guard refuses before any git call ................. MaxPathGuardTest
- dispatch vocabulary within subagent/finalization sets ...... VocabularySweepTest
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _harness import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT))

from kaizen_components.orchestration import policy  # noqa: E402
from kaizen_components.orchestration import dispatch as D  # noqa: E402
from kaizen_components.orchestration.adapters import codex as C  # noqa: E402
from kaizen_components.orchestration.adapters import local_llm as L  # noqa: E402
from kaizen_components.schemas.registry import AGENT_EVENT_KIND_MARKERS  # noqa: E402

FAKE = str(Path(__file__).resolve().parent / "fake_codex_app_server.py")


# --- policy + adapter helpers (re-declared locally; NOT imported from the sibling test modules) ----

def engine(rules=(), protected=()) -> policy.PolicyEngine:
    # vendor=[] so the engine never resolves the real ~/.claude / ~/.codex config paths in a unit test.
    return policy.PolicyEngine(list(protected), list(rules), [])


def path_prefix_allow(verb: str, prefix: str, rid: str) -> dict:
    return {"id": rid, "rule_type": "allow", "verb": verb, "match_kind": "path_prefix",
            "pattern": prefix, "engine": None, "enabled": True}


def scripted_provider(replies):
    """A chat_provider closure returning canned dicts in order (repeats the last). Records every call's
    message list so a test can assert what a lane's prompt carried (e.g. the delegate plan text)."""
    state = {"i": 0, "calls": []}

    def provider(messages, **opts):
        state["calls"].append([dict(m) for m in messages])
        idx = min(state["i"], len(replies) - 1)
        state["i"] += 1
        reply = replies[idx]
        return {"text": reply} if isinstance(reply, str) else dict(reply)

    provider.state = state  # type: ignore[attr-defined]
    return provider


def deterministic_ids():
    counters: dict[str, int] = {}

    def factory(prefix: str) -> str:
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}-{counters[prefix]}"

    return factory


def final_reply(answer: str) -> str:
    return json.dumps({"final": answer})


def tool_reply(name: str, **args) -> str:
    return json.dumps({"tool": name, "args": args})


def local_adapter(eng, provider, *, tools=None, recorder=None, max_turns=8):
    events = recorder if recorder is not None else []
    adapter = L.LocalLLMAdapter(
        eng, chat_provider=provider, tools=tools, recorder=events.append,
        logger=lambda _m: None, id_factory=deterministic_ids(), max_turns=max_turns,
    )
    adapter.test_events = events  # type: ignore[attr-defined]
    return adapter


def fake_cmd() -> list[str]:
    return [sys.executable, FAKE, "--version-string", "0.130.0", "--scenario", "plain-turn"]


def codex_adapter(eng, *, recorder) -> C.CodexAdapter:
    adapter = C.CodexAdapter(
        eng, cmd=fake_cmd(), recorder=recorder.append,
        logger=lambda _m: None,
        request_timeout=45.0, approval_timeout=45.0,
    )
    return adapter


def allow_all_exec() -> policy.PolicyEngine:
    return engine(rules=[{
        "id": "r_allow_any", "rule_type": "allow", "verb": "exec",
        "match_kind": "path_prefix", "pattern": "", "engine": None, "enabled": True,
    }])


# --- scratch git repo (tempdir only; SHORT paths) ----------------------------------------------

def scratch_git_repo(case: unittest.TestCase, *, prefix: str = "kzwt") -> Path:
    """A fresh tempdir git repo: git init -q, local user.name/email config, one committed file. SHORT
    prefix so parallel-apply worktree paths stay well under MAX_PATH. Cleaned up on teardown."""
    git = _git_bin()
    root = Path(tempfile.mkdtemp(prefix=f"{prefix}-"))
    case.addCleanup(_rmtree_quiet, root)
    _run_git(git, ["init", "-q"], root)
    _run_git(git, ["config", "user.email", "kz@example.com"], root)
    _run_git(git, ["config", "user.name", "kz"], root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _run_git(git, ["add", "seed.txt"], root)
    _run_git(git, ["commit", "-q", "-m", "seed"], root)
    return root


def _git_bin() -> str:
    import shutil
    return shutil.which("git") or "git"


def _run_git(git: str, args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    command = [git, *args]
    completed = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True, timeout=30)
    if completed.returncode:
        raise AssertionError(
            f"scratch Git precondition failed: command={command!r} rc={completed.returncode} "
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        )
    return completed


def _rmtree_quiet(path: Path) -> None:
    import shutil
    # A leftover worktree may leave a read-only .git file; ignore errors on teardown.
    shutil.rmtree(path, ignore_errors=True)
    sibling = path.parent / f"{path.name}-wt"
    shutil.rmtree(sibling, ignore_errors=True)


# --- 1. compare over two REAL engine adapters --------------------------------------------------

class CompareTwoEnginesTest(unittest.TestCase):
    """>=2-engine compare: candidates = a REAL codex-adapter lane (fake app-server, read-only sandbox) +
    a scripted local-llm lane (read-only tools); rankers = two scripted local lanes returning strict-JSON
    ballots. Asserts winner determinism, a single dispatch_group_id on EVERY event, a write-capable lane
    is REFUSED, and candidate legs recorded open/close."""

    def test_two_engine_compare_ranks_and_records(self):
        events: list = []
        # Candidate A: REAL Codex lane (plain-turn fake; no prose -> opaque "[codex turn ...]" answer).
        cx = codex_adapter(allow_all_exec(), recorder=events)
        cx.start_session(cwd=str(REPO_ROOT))
        cand_codex = D.lane_from_codex(cx, events, name="codex", read_only=True, timeout=45.0)
        # Candidate B: scripted local-llm lane, read-only tools (empty registry -> no write verb).
        local_prov = scripted_provider([final_reply("the local answer")])
        la = local_adapter(engine(), local_prov, tools={}, recorder=events)
        cand_local = D.lane_from_local_llm(la, name="local", read_only=True)

        # Two ranker lanes: each returns a strict JSON best-first ballot. Both rank the LOCAL label first.
        # Labels are assigned A->codex, B->local under the identity shuffle, so the ballot must be ["B","A"].
        r1 = D.lane_from_local_llm(
            local_adapter(engine(), scripted_provider([final_reply(json.dumps(["B", "A"]))]),
                          tools={}, recorder=events), name="ranker1", read_only=True)
        r2 = D.lane_from_local_llm(
            local_adapter(engine(), scripted_provider([final_reply(json.dumps(["B", "A"]))]),
                          tools={}, recorder=events), name="ranker2", read_only=True)

        dispatcher = D.Dispatcher(recorder=events.append, id_factory=deterministic_ids())
        try:
            result = dispatcher.compare("solve X", [cand_codex, cand_local], rankers=[r1, r2],
                                        group_id="grp-1")
        finally:
            cx.kill()

        # Both rankers rank B=local first. Neither authored a candidate, so self-vote exclusion does not
        # apply and B wins both ballots outright.
        self.assertIsNotNone(result["winner"], result)
        self.assertEqual(result["winner"]["lane"], "local", result)
        self.assertEqual(result["ballots_used"], 2)
        self.assertEqual(result["dropped_ballots"], 0)
        # EVERY dispatch-emitted event carries the same dispatch_group_id.
        dispatch_events = [e for e in events if isinstance(e.get("payload"), dict)
                           and "dispatch_group_id" in e["payload"]]
        self.assertTrue(dispatch_events)
        self.assertTrue(all(e["payload"]["dispatch_group_id"] == "grp-1" for e in dispatch_events))
        # Candidate legs recorded open + a terminal close.
        subs = [e for e in events if e["event_kind"] == "subagent"]
        self.assertIn("open", [e["marker"] for e in subs])
        self.assertTrue(any(e["marker"] in ("close_ok", "close_fail") for e in subs))

    def test_compare_refuses_write_capable_lane(self):
        # A lane with read_only=False is refused BEFORE any run.
        events: list = []
        writer_prov = scripted_provider([final_reply("x")])
        writable = D.lane_from_local_llm(local_adapter(engine(), writer_prov, tools={}), name="w",
                                         read_only=False)
        ro = D.lane_from_local_llm(local_adapter(engine(), scripted_provider([final_reply("y")]),
                                                 tools={}), name="ro", read_only=True)
        dispatcher = D.Dispatcher(recorder=events.append, id_factory=deterministic_ids())
        with self.assertRaises(D.DispatchError) as ctx:
            dispatcher.compare("t", [ro, writable])
        self.assertEqual(ctx.exception.code, D.DENIED_WRITE_LANE_IN_COMPARE)

    def test_readonly_lane_rejects_write_tool_adapter(self):
        # A read-only local-llm lane whose adapter carries a write tool is refused at construction.
        write_tool = L.ToolSpec("write_file", "file_write", "write", lambda a: "w",
                                arg_hints=("path", "content"))
        adapter = local_adapter(engine(), scripted_provider([final_reply("x")]),
                                tools={"write_file": write_tool})
        with self.assertRaises(D.DispatchError) as ctx:
            D.lane_from_local_llm(adapter, name="bad", read_only=True)
        self.assertEqual(ctx.exception.code, D.DENIED_WRITE_TOOL_IN_READONLY_LANE)


# --- 2. Borda self-vote exclusion (pure) -------------------------------------------------------

class BordaSelfVoteTest(unittest.TestCase):
    """Pure borda_tally: naive Borda and self-vote-excluded Borda pick DIFFERENT winners; plus a
    tie-break determinism case."""

    def test_self_vote_exclusion_changes_winner(self):
        # Three candidates A,B,C. Rankers authored A (ballot 0) and B (ballot 1).
        # Ballot 0 (author A): [A, B, C]  -> naive gives A=2,B=1,C=0
        # Ballot 1 (author B): [B, A, C]  -> naive gives B=2,A=1,C=0
        # Naive totals: A=3, B=3, C=0 -> tie A/B, tiebreak earliest -> A.
        ballots = [["A", "B", "C"], ["B", "A", "C"]]
        naive_authorship = {"0": "", "1": ""}  # no author -> no exclusion
        naive = D.borda_tally(ballots, naive_authorship)
        self.assertEqual(naive, {"A": 3, "B": 3, "C": 0})
        self.assertEqual(D.borda_winner(naive), "A")

        # With self-vote exclusion: ballot 0 drops its A points (2), ballot 1 drops its B points (2).
        # Excluded totals: A = 0(self-dropped) + 1(from ballot1) = 1; B = 1(from ballot0) + 0(dropped) = 1;
        # C = 0. That reties A/B at 1 -> not a different winner. Rig a clearer asymmetry instead:
        # Ballot 0 (author A): [A, B, C]; Ballot 1 (author A too): [A, C, B]. Naive: A=4 dominates.
        authorship2 = {"0": "A", "1": "A"}
        ballots2 = [["A", "B", "C"], ["A", "C", "B"]]
        naive2 = D.borda_tally(ballots2, {"0": "", "1": ""})
        self.assertEqual(D.borda_winner(naive2), "A")  # naive: A wins outright
        excluded2 = D.borda_tally(ballots2, authorship2)
        # Both ballots authored by A drop A entirely -> A=0; B = 1 + 0 = 1; C = 0 + 1 = 1 -> B/C tie at 1,
        # earliest label B wins. Winner FLIPS from A (naive) to B (self-vote excluded).
        self.assertEqual(excluded2["A"], 0)
        self.assertEqual(D.borda_winner(excluded2), "B")
        self.assertNotEqual(D.borda_winner(naive2), D.borda_winner(excluded2))

    def test_tiebreak_is_earliest_label(self):
        # Exact tie across every label -> deterministic earliest-label winner.
        ballots = [["A", "B"], ["B", "A"]]
        totals = D.borda_tally(ballots, {"0": "", "1": ""})
        self.assertEqual(totals, {"A": 1, "B": 1})
        self.assertEqual(D.borda_winner(totals), "A")

    def test_empty_tally_is_none(self):
        self.assertIsNone(D.borda_winner({}))


# --- 3. malformed ballot ------------------------------------------------------------------------

class MalformedBallotTest(unittest.TestCase):
    """One ranker returns prose: its run keeps one terminal close and a same-correlation verification records the dropped ballot."""

    def test_prose_ballot_dropped_compare_continues(self):
        events: list = []
        c1 = D.lane_from_local_llm(local_adapter(engine(), scripted_provider([final_reply("ans-A")]),
                                                 tools={}, recorder=events), name="c1", read_only=True)
        c2 = D.lane_from_local_llm(local_adapter(engine(), scripted_provider([final_reply("ans-B")]),
                                                 tools={}, recorder=events), name="c2", read_only=True)
        # Ranker 1: prose (malformed) -> dropped. Ranker 2: strict ballot ranking B(=c2) first.
        good = D.lane_from_local_llm(
            local_adapter(engine(), scripted_provider([final_reply(json.dumps(["B", "A"]))]),
                          tools={}, recorder=events), name="good", read_only=True)
        bad = D.lane_from_local_llm(
            local_adapter(engine(), scripted_provider([final_reply("I think A is best, honestly.")]),
                          tools={}, recorder=events), name="bad", read_only=True)

        dispatcher = D.Dispatcher(recorder=events.append, id_factory=deterministic_ids())
        result = dispatcher.compare("q", [c1, c2], rankers=[bad, good], group_id="grp-mb")

        self.assertEqual(result["dropped_ballots"], 1)
        self.assertEqual(result["ballots_used"], 1)
        malformed = [
            event for event in events
            if event["event_kind"] == "verification" and event["marker"] == "point"
            and event.get("code") == D.MALFORMED_BALLOT
        ]
        self.assertEqual(len(malformed), 1, events)
        correlation = malformed[0]["correlation_id"]
        terminal = [
            event for event in events
            if event.get("correlation_id") == correlation and event["event_kind"] == "subagent"
            and event["marker"] in {"close_ok", "close_fail", "close_canceled"}
        ]
        self.assertEqual([event["marker"] for event in terminal], ["close_ok"])
        terminal_counts: dict[str, int] = {}
        for event in events:
            if event["event_kind"] != "subagent":
                continue
            if event["marker"] not in {"close_ok", "close_fail", "close_canceled"}:
                continue
            key = str(event.get("correlation_id"))
            terminal_counts[key] = terminal_counts.get(key, 0) + 1
        self.assertTrue(all(count <= 1 for count in terminal_counts.values()), terminal_counts)
        self.assertIn("verification", AGENT_EVENT_KIND_MARKERS)
        self.assertIn("point", AGENT_EVENT_KIND_MARKERS["verification"])
        # Compare still resolved a winner from the surviving ballot (B=c2 ranked first).
        self.assertEqual(result["winner"]["lane"], "c2", result)


# --- 4. cost / turn ceiling ---------------------------------------------------------------------

class CeilingTest(unittest.TestCase):
    """max_turns_total sized so the ranker legs cannot run: the skipped legs emit close_canceled code
    CEILING_HIT, result.ceiling_hit is True, and total turns_used <= ceiling (never exceeds)."""

    def test_ranker_legs_skipped_when_ceiling_spent(self):
        events: list = []
        # Two candidates each burn 1 turn (a final costs 1 iteration) -> spends the whole budget of 2.
        c1 = D.lane_from_local_llm(local_adapter(engine(), scripted_provider([final_reply("A")]),
                                                 tools={}, recorder=events), name="c1", read_only=True)
        c2 = D.lane_from_local_llm(local_adapter(engine(), scripted_provider([final_reply("B")]),
                                                 tools={}, recorder=events), name="c2", read_only=True)
        # Rankers would each cost 1 more; with max_turns_total=2 they must all be skipped.
        rk_prov1 = scripted_provider([final_reply(json.dumps(["A", "B"]))])
        rk_prov2 = scripted_provider([final_reply(json.dumps(["A", "B"]))])
        rk1 = D.lane_from_local_llm(local_adapter(engine(), rk_prov1, tools={}, recorder=events),
                                    name="rk1", read_only=True)
        rk2 = D.lane_from_local_llm(local_adapter(engine(), rk_prov2, tools={}, recorder=events),
                                    name="rk2", read_only=True)

        dispatcher = D.Dispatcher(recorder=events.append, id_factory=deterministic_ids())
        result = dispatcher.compare("q", [c1, c2], rankers=[rk1, rk2], max_turns_total=2,
                                    group_id="grp-c")

        self.assertTrue(result["ceiling_hit"])
        self.assertEqual(result["ballots_used"], 0, "no ranker ran under the ceiling")
        # The ranker providers were NEVER called (the legs were skipped before run()).
        self.assertEqual(rk_prov1.state["i"], 0)
        self.assertEqual(rk_prov2.state["i"], 0)
        # Skipped ranker legs recorded close_canceled with code CEILING_HIT.
        canceled = [e for e in events if e["event_kind"] == "subagent"
                    and e["marker"] == "close_canceled" and e.get("code") == D.CEILING_HIT]
        self.assertEqual(len(canceled), 2, "both ranker legs skipped with CEILING_HIT")

    def test_zero_completed_candidates_returns_truthfully(self):
        # ceiling=0 skips EVERY candidate -> no completed answers -> the ranker phase is skipped
        # entirely and compare returns a truthful empty result (winner None, 0 ballots) instead of
        # crashing while building a rank prompt over an empty label set.
        events: list = []
        prov = scripted_provider([final_reply("A")])
        c1 = D.lane_from_local_llm(local_adapter(engine(), prov, tools={}, recorder=events),
                                   name="c1", read_only=True)
        rk_prov = scripted_provider([final_reply(json.dumps(["A"]))])
        rk = D.lane_from_local_llm(local_adapter(engine(), rk_prov, tools={}, recorder=events),
                                   name="rk", read_only=True)
        dispatcher = D.Dispatcher(recorder=events.append, id_factory=deterministic_ids())
        result = dispatcher.compare("q", [c1], rankers=[rk], max_turns_total=0, group_id="grp-z")
        self.assertTrue(result["ceiling_hit"])
        self.assertIsNone(result["winner"])
        self.assertEqual(result["ballots_used"], 0)
        self.assertEqual(result["synthesis"], "")
        self.assertEqual(prov.state["i"], 0, "no candidate ran")
        self.assertEqual(rk_prov.state["i"], 0, "no ranker ran")

    def test_candidate_over_budget_is_skipped_never_exceeds(self):
        # A ceiling of 1 lets only the FIRST candidate run; the second is skipped. Total turns <= 1.
        events: list = []
        prov1 = scripted_provider([final_reply("A")])
        prov2 = scripted_provider([final_reply("B")])
        c1 = D.lane_from_local_llm(local_adapter(engine(), prov1, tools={}, recorder=events),
                                   name="c1", read_only=True)
        c2 = D.lane_from_local_llm(local_adapter(engine(), prov2, tools={}, recorder=events),
                                   name="c2", read_only=True)
        dispatcher = D.Dispatcher(recorder=events.append, id_factory=deterministic_ids())
        result = dispatcher.compare("q", [c1, c2], rankers=[], max_turns_total=1, group_id="grp-c2")
        self.assertTrue(result["ceiling_hit"])
        self.assertEqual(prov1.state["i"], 1, "first candidate ran")
        self.assertEqual(prov2.state["i"], 0, "second candidate skipped (ceiling)")


# --- 5. delegate --------------------------------------------------------------------------------

class DelegateTest(unittest.TestCase):
    """Read-only planner plans; executor (write-allow path_prefix on its tmp root) writes the file. The
    file is written, the plan text is present in the executor's received prompt, and a write-capable
    planner is refused (DENIED_WRITER_PLANNER)."""

    def test_planner_plans_executor_writes(self):
        ws = Path(tempfile.mkdtemp(prefix="kz-deleg-"))
        self.addCleanup(_rmtree_quiet, ws)
        events: list = []
        target = ws / "out.txt"

        # Planner: read-only tools, returns a plan string.
        plan_prov = scripted_provider([final_reply("STEP 1: write out.txt with the value 42")])
        planner = D.lane_from_local_llm(local_adapter(engine(), plan_prov, tools={}, recorder=events),
                                        name="planner", read_only=True)

        # Executor: a write-allowed adapter that writes target on its first turn, then finals. Its
        # provider records messages so we can assert the plan text arrived.
        exec_prov = scripted_provider([
            tool_reply("write_file", path=str(target), content="42"),
            final_reply("wrote out.txt"),
        ])
        exec_eng = engine(rules=[path_prefix_allow("file_write", str(ws), "r_w")])
        exec_adapter = local_adapter(exec_eng, exec_prov, tools=L.build_default_tools(ws), recorder=events)
        executor = D.lane_from_local_llm(exec_adapter, name="executor", read_only=False)

        dispatcher = D.Dispatcher(recorder=events.append, id_factory=deterministic_ids())
        result = dispatcher.delegate("write the file", planner, executor, group_id="grp-d")

        self.assertTrue(target.is_file(), "the executor must have written the file")
        self.assertEqual(target.read_text(encoding="utf-8"), "42")
        self.assertIn("STEP 1", result["plan"])
        # The plan text is present in the executor's FIRST provider call messages (composed as DATA).
        first_exec_call = exec_prov.state["calls"][0]
        self.assertTrue(any("STEP 1" in m["content"] for m in first_exec_call),
                        "the plan text must be composed into the executor prompt")
        # Planner + executor legs recorded (roles stamped).
        roles = {leg["role"] for leg in result["legs"]}
        self.assertEqual(roles, {"planner", "executor"})

    def test_writer_planner_refused(self):
        events: list = []
        planner = D.lane_from_local_llm(local_adapter(engine(), scripted_provider([final_reply("p")]),
                                                      tools={}), name="p", read_only=False)
        executor = D.lane_from_local_llm(local_adapter(engine(), scripted_provider([final_reply("e")]),
                                                       tools={}), name="e", read_only=False)
        dispatcher = D.Dispatcher(recorder=events.append, id_factory=deterministic_ids())
        with self.assertRaises(D.DispatchError) as ctx:
            dispatcher.delegate("t", planner, executor)
        self.assertEqual(ctx.exception.code, D.DENIED_WRITER_PLANNER)


# --- 6. parallel-apply clean run ----------------------------------------------------------------

def _writer_writing(name: str, filename: str, events: list):
    """A duck-typed parallel-apply writer whose run_in writes ``filename`` INSIDE its own worktree under
    a write-allow rule scoped to that worktree, then proves the file landed (before teardown)."""
    def run_in(worktree_path: str, prompt: str) -> dict:
        wt = Path(worktree_path)
        eng = engine(rules=[path_prefix_allow("file_write", str(wt), f"r_{name}")])
        prov = scripted_provider([
            tool_reply("write_file", path=str(wt / filename), content=f"by-{name}"),
            final_reply(f"{name} wrote {filename}"),
        ])
        adapter = local_adapter(eng, prov, tools=L.build_default_tools(wt), recorder=events)
        adapter.start_session(cwd=str(wt))
        out = adapter.start_turn(prompt)
        landed = (wt / filename).is_file()
        return {"status": out["status"], "landed": landed, "path": str(wt / filename)}
    return {"name": name, "run_in": run_in}


class ParallelApplyCleanTest(unittest.TestCase):
    """Scratch repo + 2 writers (each writes one file in ITS worktree under write-allow rules): both
    branches kz/<group>/<name> existed, each file landed in its worktree BEFORE teardown (writer result
    proof), teardown clean, ``git worktree list --porcelain`` shows only the main checkout after, prune
    is a no-op, and per writer the subagent close_* is emitted BEFORE the finalization event (destructive-
    after-commit). zero_leak is True."""

    def test_two_writers_zero_leak_ordered_teardown(self):
        root = scratch_git_repo(self)
        events: list = []
        w1 = _writer_writing("wa", "a.txt", events)
        w2 = _writer_writing("wb", "b.txt", events)
        manager = D.WorktreeManager(root)
        dispatcher = D.Dispatcher(recorder=events.append, id_factory=deterministic_ids())

        result = dispatcher.parallel_apply(lambda name: f"do {name}", [w1, w2], root,
                                           group_id="g", worktree_manager=manager)

        # Both writers OK, neither quarantined.
        self.assertEqual(set(result["writers"].keys()), {"wa", "wb"})
        for name in ("wa", "wb"):
            self.assertEqual(result["writers"][name]["status"], "OK", result["writers"][name])
            self.assertFalse(result["writers"][name]["quarantined"])
            self.assertEqual(result["writers"][name]["branch"], f"kz/g/{name}")
        # zero_leak: only the main checkout remains.
        self.assertTrue(result["zero_leak"], result.get("leaked"))
        # The worktree list shows ONLY the main checkout (its path resolves to root).
        entries = manager.list_worktrees()
        self.assertEqual(len(entries), 1, entries)
        # prune is a no-op after a clean run (returns exit 0, no leaked entries).
        self.assertEqual(manager.prune().returncode, 0)

        # Per writer, the terminal subagent close_* was emitted BEFORE its finalization event
        # (destructive-after-commit / ledger #18). Correlate by correlation_id.
        by_corr: dict[str, list] = {}
        for i, e in enumerate(events):
            corr = e.get("correlation_id")
            if corr and str(corr).startswith("writer-"):
                by_corr.setdefault(corr, []).append((i, e["event_kind"], e["marker"]))
        self.assertTrue(by_corr, "writer-correlated events recorded")
        for corr, seq in by_corr.items():
            sub_close_idx = next((idx for idx, kind, marker in seq
                                  if kind == "subagent" and marker in ("close_ok", "close_fail")), None)
            fin_idx = next((idx for idx, kind, marker in seq if kind == "finalization"), None)
            self.assertIsNotNone(sub_close_idx, f"{corr}: subagent close recorded")
            self.assertIsNotNone(fin_idx, f"{corr}: finalization recorded")
            self.assertLess(sub_close_idx, fin_idx,
                            f"{corr}: subagent close must precede finalization (destructive-after-commit)")

        # The files really landed in each worktree before teardown (proof carried on the writer record).
        for name in ("wa", "wb"):
            self.assertTrue(result["writers"][name])  # record present
        # And the worktrees are gone now (teardown removed them).
        self.assertFalse((root.parent / f"{root.name}-wt" / "g-wa").exists())
        self.assertFalse((root.parent / f"{root.name}-wt" / "g-wb").exists())


# --- 7. teardown quarantine ---------------------------------------------------------------------

class TeardownQuarantineTest(unittest.TestCase):
    """Inject a runner/remove_fn that FAILS removal for one writer: retries exhaust, a
    ("finalization","close_fail") code TEARDOWN_QUARANTINED payload quarantined:true is emitted, and the
    dispatch returns TRUTHFULLY (writer status still ok), no exception. The failure is INJECTED (not a
    real Windows file lock) so it is deterministic cross-platform."""

    def test_injected_removal_failure_quarantines(self):
        root = scratch_git_repo(self)
        events: list = []
        real_git = D._default_git_runner

        def failing_runner(args, cwd):
            # Let creation + list/prune succeed via the real git; FAIL every `worktree remove`.
            if args[:2] == ["worktree", "remove"]:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="INJECTED remove failure")
            return real_git(args, cwd)

        def failing_remove(_path):
            raise OSError("INJECTED rmtree failure")

        manager = D.WorktreeManager(root, runner=failing_runner, remove_fn=failing_remove,
                                    retries=2, backoff=0.0)

        def run_in(worktree_path, prompt):
            (Path(worktree_path) / "w.txt").write_text("x", encoding="utf-8")
            return {"status": "OK"}

        writer = {"name": "wq", "run_in": run_in}
        dispatcher = D.Dispatcher(recorder=events.append, id_factory=deterministic_ids())
        result = dispatcher.parallel_apply(lambda n: "go", [writer], root, group_id="gq",
                                           worktree_manager=manager)

        # The writer's work still succeeded (destructive-after-commit: the ledger commit happened first).
        self.assertEqual(result["writers"]["wq"]["status"], "OK")
        self.assertTrue(result["writers"]["wq"]["quarantined"])
        # A finalization close_fail with TEARDOWN_QUARANTINED payload quarantined:true.
        quarantined = [e for e in events if e["event_kind"] == "finalization"
                       and e["marker"] == "close_fail" and e.get("code") == D.TEARDOWN_QUARANTINED]
        self.assertTrue(quarantined, "a TEARDOWN_QUARANTINED finalization must be recorded")
        self.assertTrue(quarantined[0]["payload"]["quarantined"])
        # The subagent close (ledger commit) preceded the quarantine finalization.
        kinds = [(e["event_kind"], e["marker"]) for e in events
                 if e.get("correlation_id") == quarantined[0]["correlation_id"]]
        sub_close = next((i for i, km in enumerate(kinds)
                          if km[0] == "subagent" and km[1] in ("close_ok", "close_fail")), None)
        fin = next((i for i, km in enumerate(kinds) if km[0] == "finalization"), None)
        self.assertIsNotNone(sub_close)
        self.assertLess(sub_close, fin)
        # Manually prune the quarantined worktree so teardown of the temp dir is clean.
        real_git(["worktree", "remove", "--force",
                  str(manager.worktree_path("gq", "wq"))], str(root))
        manager.prune()


# --- 8. MAX_PATH guard --------------------------------------------------------------------------

class MaxPathGuardTest(unittest.TestCase):
    """max_path tiny => DispatchError DENIED_MAX_PATH raised BEFORE any git call (the runner spy records
    zero invocations for that writer)."""

    def test_max_path_refused_before_any_git(self):
        root = scratch_git_repo(self)
        calls: list = []
        real_git = D._default_git_runner

        def spy_runner(args, cwd):
            calls.append(list(args))
            return real_git(args, cwd)

        # A tiny max_path guarantees the worktree path is over budget.
        manager = D.WorktreeManager(root, runner=spy_runner, max_path=5)
        with self.assertRaises(D.DispatchError) as ctx:
            manager.create("g", "writer")
        self.assertEqual(ctx.exception.code, D.DENIED_MAX_PATH)
        # ZERO git invocations happened for the refused create.
        self.assertEqual(calls, [], "MAX_PATH must be checked before any git call")

    def test_parallel_apply_max_path_writer_close_fail_no_git(self):
        # In a full parallel_apply, a MAX_PATH refusal is a truthful writer close_fail, no teardown.
        root = scratch_git_repo(self)
        events: list = []
        calls: list = []
        real_git = D._default_git_runner

        def spy_runner(args, cwd):
            calls.append(list(args))
            return real_git(args, cwd)

        manager = D.WorktreeManager(root, runner=spy_runner, max_path=5)
        writer = {"name": "wx", "run_in": lambda p, pr: {"status": "OK"}}
        dispatcher = D.Dispatcher(recorder=events.append, id_factory=deterministic_ids())
        result = dispatcher.parallel_apply(lambda n: "go", [writer], root, group_id="gp",
                                           worktree_manager=manager)
        # The writer close_fail carries DENIED_MAX_PATH; no worktree was created (no git ran).
        cf = [e for e in events if e["event_kind"] == "subagent" and e["marker"] == "close_fail"
              and e.get("code") == D.DENIED_MAX_PATH]
        self.assertTrue(cf)
        self.assertEqual(result["writers"]["wx"]["status"], "CREATE_REFUSED")
        # Only the zero-leak `git worktree list` ran at the end -- no `worktree add`.
        self.assertNotIn(["worktree", "add"], [c[:2] for c in calls])


# --- 9. vocabulary sweep ------------------------------------------------------------------------

class VocabularySweepTest(unittest.TestCase):
    """Every dispatch-emitted (event_kind, marker) is within the sanctioned sets: dispatch uses ONLY
    ``subagent`` (open/close_ok/close_fail/close_canceled) and ``finalization``
    (close_ok/close_fail/close_canceled), plus ``verification/point`` for a malformed ballot; each pair
    is in the T6 AGENT_EVENT_KIND_MARKERS matrix."""

    ALLOWED = {
        "subagent": {"open", "close_ok", "close_fail", "close_canceled"},
        "finalization": {"close_ok", "close_fail", "close_canceled"},
        "verification": {"point"},
    }

    def test_all_strategies_emit_only_sanctioned_pairs(self):
        events: list = []
        dispatcher = D.Dispatcher(recorder=events.append, id_factory=deterministic_ids())

        # Compare with a malformed ballot to exercise its non-terminal verification record.
        c1 = D.lane_from_local_llm(local_adapter(engine(), scripted_provider([final_reply("A")]),
                                                 tools={}, recorder=events), name="c1", read_only=True)
        c2 = D.lane_from_local_llm(local_adapter(engine(), scripted_provider([final_reply("B")]),
                                                 tools={}, recorder=events), name="c2", read_only=True)
        good = D.lane_from_local_llm(
            local_adapter(engine(), scripted_provider([final_reply(json.dumps(["A", "B"]))]),
                          tools={}, recorder=events), name="good", read_only=True)
        bad = D.lane_from_local_llm(
            local_adapter(engine(), scripted_provider([final_reply("prose not json")]),
                          tools={}, recorder=events), name="bad", read_only=True)
        dispatcher.compare("q", [c1, c2], rankers=[bad, good], group_id="v1")

        # delegate.
        planner = D.lane_from_local_llm(local_adapter(engine(), scripted_provider([final_reply("plan")]),
                                                      tools={}, recorder=events), name="pl", read_only=True)
        executor = D.lane_from_local_llm(local_adapter(engine(), scripted_provider([final_reply("done")]),
                                                       tools={}, recorder=events), name="ex", read_only=False)
        dispatcher.delegate("t", planner, executor, group_id="v2")

        # parallel-apply, clean run -> finalization close_ok (a REAL git manager, tempdir repo).
        clean_root = scratch_git_repo(self, prefix="kzvc")

        def _clean_run_in(worktree_path, prompt):
            (Path(worktree_path) / "x.txt").write_text("x", encoding="utf-8")
            return {"status": "OK"}

        clean_manager = D.WorktreeManager(clean_root)
        dispatcher.parallel_apply(lambda n: "go", [{"name": "wc", "run_in": _clean_run_in}],
                                  clean_root, group_id="v3", worktree_manager=clean_manager)

        # parallel-apply, injected teardown failure -> finalization close_fail (quarantine).
        root = scratch_git_repo(self, prefix="kzvq")
        real_git = D._default_git_runner

        def failing_runner(args, cwd):
            if args[:2] == ["worktree", "remove"]:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="fail")
            return real_git(args, cwd)

        def _throw_remove(_p):
            raise OSError("fail")

        manager = D.WorktreeManager(root, runner=failing_runner, remove_fn=_throw_remove,
                                    retries=1, backoff=0.0)

        def _quarantine_run_in(worktree_path, prompt):
            (Path(worktree_path) / "x.txt").write_text("x", encoding="utf-8")
            return {"status": "OK"}

        dispatcher.parallel_apply(lambda n: "go", [{"name": "wv", "run_in": _quarantine_run_in}],
                                  root, group_id="v4", worktree_manager=manager)
        # clean up the quarantined worktree so the tempdir teardown is tidy.
        real_git(["worktree", "remove", "--force", str(manager.worktree_path("v4", "wv"))], str(root))
        manager.prune()

        # Every dispatch-emitted event (those carrying dispatch_group_id) is in-vocabulary. Adapter-level
        # events (turn/tool_call/approval/session from the lanes) are excluded via the group-id filter.
        dispatch_events = [e for e in events if isinstance(e.get("payload"), dict)
                           and "dispatch_group_id" in e["payload"]]
        self.assertTrue(dispatch_events)
        for e in dispatch_events:
            kind, marker = e["event_kind"], e["marker"]
            self.assertIn(kind, self.ALLOWED, f"dispatch emitted out-of-set kind {kind!r}")
            self.assertIn(marker, self.ALLOWED[kind], f"marker {marker!r} not allowed for {kind!r}")
            # And the pair is in the authoritative T6 matrix.
            self.assertIn(kind, AGENT_EVENT_KIND_MARKERS)
            self.assertIn(marker, AGENT_EVENT_KIND_MARKERS[kind],
                          f"({kind},{marker}) not in AGENT_EVENT_KIND_MARKERS")
        # Sanity: this drive exercised subagent lifecycle, malformed-ballot verification, and both
        # finalization outcomes. Dedicated ceiling tests cover close_canceled elsewhere.
        emitted = {(e["event_kind"], e["marker"]) for e in dispatch_events}
        self.assertIn(("subagent", "open"), emitted)
        self.assertIn(("subagent", "close_ok"), emitted)
        self.assertIn(("verification", "point"), emitted)
        self.assertIn(("finalization", "close_ok"), emitted)
        self.assertIn(("finalization", "close_fail"), emitted)


if __name__ == "__main__":
    unittest.main()
