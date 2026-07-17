import assert from "node:assert/strict";
import { test } from "node:test";

import { historyFromSessionList } from "../../extension/src/historyModel";

test("history is newest-first with title/snippet fallbacks and precise driven/observed states", () => {
  const history = historyFromSessionList({
    status: "OK",
    sessions: [
      {
        session_id: "older",
        created_at: "2026-07-10T10:00:00Z",
        controller: "driven",
        engine: "claude",
        title: null,
        snippet: "First prompt becomes the fallback title",
        latest_run_id: "run-old",
        latest_run_state: "terminal",
        latest_terminal_state: "failure",
        resumable: true,
        runs: [{ id: "run-old", terminal: true }],
      },
      {
        session_id: "newer",
        created_at: "2026-07-11T10:00:00Z",
        controller: "observed",
        engine: "claude",
        title: "Observed title",
        snippet: null,
        latest_run_id: "run-new",
        latest_run_state: "open",
        resumable: true,
        runs: [{ id: "run-new", terminal: false }],
      },
      {
        session_id: "untitled",
        created_at: "2026-07-09T10:00:00Z",
        controller: "driven",
        latest_run_id: "run-untitled",
        latest_run_state: "terminal",
        resumable: false,
        runs: [{ id: "run-untitled", terminal: true }],
      },
    ],
  });
  assert.deepEqual(history.entries.map((entry) => entry.sessionId), ["newer", "older", "untitled"]);
  assert.deepEqual(history.entries.map((entry) => entry.agentRunId), ["run-new", "run-old", "run-untitled"]);
  assert.equal(history.entries[0].title, "Observed title");
  assert.equal(history.entries[0].readOnly, true, "observed is always read-only");
  assert.equal(history.entries[0].resumable, false, "observed never becomes resumable from a malformed flag");
  assert.equal(history.entries[1].title, "First prompt becomes the fallback title");
  assert.equal(history.entries[1].state, "resumable");
  assert.equal(history.entries[1].readOnly, false);
  assert.equal(history.entries[2].title, "Untitled conversation");
  assert.equal(history.entries[2].state, "read-only");
});

test("history exposes response/run omissions and metadata-only reduced-fidelity artifacts", () => {
  const history = historyFromSessionList({
    status: "OK",
    truncated: true,
    sessions_omitted: 3,
    sessions: [{
      session_id: "s1",
      created_at: "2026-07-11T10:00:00Z",
      controller: "driven",
      latest_run_id: "r1",
      latest_run_state: "terminal",
      resumable: true,
      transcript_seeded: true,
      omitted_message_count: 4,
      runs_total: 5,
      runs_returned: 2,
      runs_truncated: true,
      expired_artifacts: [{ kind: "selection", name: "app.ts selection", sha256: "a".repeat(64), bytes: 12 }],
      runs: [{ id: "r1", terminal: true }],
    }],
  });
  assert.equal(history.truncated, true);
  assert.equal(history.sessionsOmitted, 3);
  const entry = history.entries[0];
  assert.equal(entry.fidelity?.mode, "reduced");
  assert.equal(entry.omittedMessages, 4);
  assert.equal(entry.runsOmitted, 3);
  assert.deepEqual(entry.expiredArtifacts, [{ kind: "selection", name: "app.ts selection", sha256: "a".repeat(64), bytes: 12 }]);
  assert.deepEqual(Object.keys(entry).sort(), [
    "agentRunId", "controller", "createdAt", "expiredArtifacts", "fidelity", "omittedMessages",
    "readOnly", "resumable", "runsOmitted", "sessionId", "state", "title",
  ]);
});

test("history exposes independent omission branches and fail-closed roots", () => {
  assert.deepEqual(historyFromSessionList(undefined), { entries: [], truncated: false });
  assert.deepEqual(historyFromSessionList({ status: "OK" }), { entries: [], truncated: false });
  const responseOmission = historyFromSessionList({ sessions_omitted: 2, sessions: [] });
  assert.deepEqual(responseOmission, { entries: [], sessionsOmitted: 2, truncated: true });
  const runOmission = historyFromSessionList({ sessions: [{
    session_id: "s", controller: "driven", runs_truncated: true, runs: [{ id: "r" }],
  }] });
  assert.equal(runOmission.entries[0].runsOmitted, 1);
});

test("live artifacts are not force-expired when an expired-artifact list is also present", () => {
  const history = historyFromSessionList({
    sessions: [{
      session_id: "s1",
      controller: "driven",
      latest_run_id: "r1",
      artifacts: [
        { kind: "image", name: "live.png", available: true },
        { kind: "context", name: "expired.txt", expired: true, sha256: "bad" },
        { kind: "selection", name: "unavailable.txt", available: false, sha256: "b".repeat(64) },
      ],
      expired_artifacts: [{ kind: "old", name: "old.bin" }],
      runs: [{ id: "r1" }],
    }],
  });
  assert.deepEqual(history.entries[0].expiredArtifacts, [
    { kind: "context", name: "expired.txt" },
    { kind: "selection", name: "unavailable.txt", sha256: "b".repeat(64) },
  ]);
});

test("history covers title truncation, idle/full fidelity, run fallback, and stable tie ordering", () => {
  const history = historyFromSessionList({ sessions: [
    {
      session_id: "a", created_at: "2026-07-11T10:00:00Z", controller: "driven",
      snippet: "x".repeat(81), latest_run_state: "idle", fidelity: "vendor", runs: [{ id: "run-a" }],
    },
    {
      session_id: "b", created_at: "2026-07-11T10:00:00Z", controller: "driven",
      latest_run_id: "run-b", runs: [{ id: "run-b" }],
    },
  ] });
  assert.deepEqual(history.entries.map((entry) => entry.sessionId), ["b", "a"]);
  const fallback = history.entries[1];
  assert.equal(fallback.agentRunId, "run-a");
  assert.equal([...fallback.title].length, 80);
  assert.match(fallback.title, /\u2026$/);
  assert.equal(fallback.state, "idle");
  assert.equal(fallback.fidelity?.mode, "full");
});

test("old-daemon rows without controller or run identity fail closed while basic empty history remains valid", () => {
  assert.deepEqual(historyFromSessionList({ status: "OK", sessions: [{ session_id: "legacy" }] }), {
    entries: [],
    truncated: false,
  });
});
