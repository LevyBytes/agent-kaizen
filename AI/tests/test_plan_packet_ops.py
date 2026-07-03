"""W2-W8: task updates, plan create/revise, and subagent/diagnostic packets."""

from __future__ import annotations

import re

from _harness import IsolatedDBTest

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class PlanPacketOpsTest(IsolatedDBTest):
    def _table_counts(self) -> dict:
        rc, p = self.kz("K6")
        self.assertEqual(rc, 0, p)
        return p["manifest"]["table_counts"]

    def _start_task(self) -> tuple[str, str]:
        rc, p = self.kz("W1", "--title", "Seed task", "--summary", "Seed a task for update tests.", "--body", "b")
        self.assertEqual(rc, 0, p)
        return p["id"], p["revision_id"]

    # ---- W2 task-update -------------------------------------------------

    def test_w2_missing_id_is_denied(self):
        rc, p = self.kz("W2", "--status", "done")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("status"), "DENIED")
        self.assertEqual(p.get("code"), "DENIED_ID_REQUIRED")

    def test_w2_unknown_id_is_not_found(self):
        rc, p = self.kz("W2", "--id", "t_nope", "--status", "done")
        self.assertEqual(rc, 1, p)
        self.assertEqual(p.get("code"), "DENIED_RECORD_NOT_FOUND")
        self.assertEqual(p.get("table"), "tasks")
        self.assertEqual(p.get("id"), "t_nope")

    def test_w2_updates_status_and_creates_second_revision(self):
        task_id, first_revision = self._start_task()
        rc, p = self.kz("W2", "--id", task_id, "--status", "done")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["id"], task_id)
        self.assertNotEqual(p["revision_id"], first_revision, p)
        self.assertTrue(_HASH_RE.match(p.get("content_hash", "")), p)
        # revision_number bumped to 2 == two task_revision rows for this DB.
        self.assertEqual(self._table_counts()["task_revision"], 2)
        # Status is observable through the R1 task-report file (status extra column).
        rc, p = self.kz("R1")
        self.assertEqual(rc, 0, p)
        self.assertIn("status", p["columns"], p)
        report = (self.root / p["path"]).read_text(encoding="utf-8")
        line = next(l for l in report.splitlines() if task_id in l)
        self.assertIn("status=done", line, report)

    def test_w2_each_update_returns_a_new_revision_id(self):
        task_id, first_revision = self._start_task()
        rc, p2 = self.kz("W2", "--id", task_id, "--summary", "First update to the task.")
        self.assertEqual(rc, 0, p2)
        rc, p3 = self.kz("W2", "--id", task_id, "--summary", "Second update to the task.")
        self.assertEqual(rc, 0, p3)
        self.assertEqual(len({first_revision, p2["revision_id"], p3["revision_id"]}), 3)
        self.assertEqual(self._table_counts()["task_revision"], 3)

    # ---- W3 plan-create / W4 plan-revise --------------------------------

    def test_w3_missing_title_is_denied(self):
        rc, p = self.kz("W3", "--summary", "A plan without a title.")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_TITLE_REQUIRED")

    def test_w3_title_without_summary_is_denied(self):
        rc, p = self.kz("W3", "--title", "Plan with no summary")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_SUMMARY_REQUIRED")

    def test_w3_create_returns_plan_and_first_revision(self):
        rc, p = self.kz("W3", "--title", "Rollout plan", "--summary", "Plan the rollout.", "--body", "steps")
        self.assertEqual(rc, 0, p)
        self.assertEqual(p["status"], "OK")
        self.assertTrue(p["id"].startswith("p_"), p)
        self.assertTrue(p["revision_id"].startswith("pr_"), p)
        self.assertTrue(_HASH_RE.match(p.get("content_hash", "")), p)
        counts = self._table_counts()
        self.assertEqual(counts["plans"], 1)
        self.assertEqual(counts["plan_revision"], 1)

    def test_w4_missing_id_is_denied(self):
        rc, p = self.kz("W4", "--summary", "Revision without an id.")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_ID_REQUIRED")

    def test_w4_unknown_id_is_not_found(self):
        rc, p = self.kz("W4", "--id", "p_nope", "--summary", "Revise a missing plan.")
        self.assertEqual(rc, 1, p)
        self.assertEqual(p.get("code"), "DENIED_RECORD_NOT_FOUND")
        self.assertEqual(p.get("table"), "plans")

    def test_w4_revise_bumps_revision_each_time(self):
        rc, created = self.kz("W3", "--title", "Living plan", "--summary", "Initial plan summary.")
        self.assertEqual(rc, 0, created)
        rc, rev2 = self.kz("W4", "--id", created["id"], "--summary", "Revised plan summary.")
        self.assertEqual(rc, 0, rev2)
        self.assertEqual(rev2["id"], created["id"])
        self.assertTrue(rev2["revision_id"].startswith("pr_"), rev2)
        self.assertNotEqual(rev2["revision_id"], created["revision_id"])
        self.assertTrue(_HASH_RE.match(rev2.get("content_hash", "")), rev2)
        rc, rev3 = self.kz("W4", "--id", created["id"], "--summary", "Revised again with new text.")
        self.assertEqual(rc, 0, rev3)
        self.assertNotEqual(rev3["revision_id"], rev2["revision_id"])
        self.assertNotEqual(rev3["content_hash"], rev2["content_hash"])
        counts = self._table_counts()
        self.assertEqual(counts["plans"], 1)
        # create wrote revision 1; each W4 appended one more.
        self.assertEqual(counts["plan_revision"], 3)

    # ---- W5-W8 subagent / diagnostic packets -----------------------------

    def test_w5_w6_subagent_packets_round_trip(self):
        # Default payload "{}" and default title: only --summary is required.
        rc, p5 = self.kz("W5", "--summary", "Request packet for a subagent.")
        self.assertEqual(rc, 0, p5)
        self.assertTrue(p5["id"].startswith("sp_"), p5)
        self.assertTrue(_HASH_RE.match(p5.get("content_hash", "")), p5)
        rc, p6 = self.kz("W6", "--summary", "Ingest packet from a subagent.", "--payload-json", '{"result": "ok"}')
        self.assertEqual(rc, 0, p6)
        self.assertTrue(p6["id"].startswith("sp_"), p6)
        counts = self._table_counts()
        self.assertEqual(counts["subagent_packets"], 2)
        self.assertEqual(counts["diagnostic_packets"], 0)

    def test_w7_w8_diagnostic_packets_round_trip(self):
        rc, p7 = self.kz("W7", "--summary", "Diagnostic request packet.")
        self.assertEqual(rc, 0, p7)
        self.assertTrue(p7["id"].startswith("dp_"), p7)
        rc, p8 = self.kz("W8", "--summary", "Diagnostic result packet.", "--payload-json", '{"verdict": "pass"}')
        self.assertEqual(rc, 0, p8)
        self.assertTrue(p8["id"].startswith("dp_"), p8)
        counts = self._table_counts()
        self.assertEqual(counts["diagnostic_packets"], 2)
        self.assertEqual(counts["subagent_packets"], 0)

    def test_packet_missing_summary_is_denied(self):
        # Title and payload have defaults, but the summary discipline still applies.
        rc, p = self.kz("W5")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_SUMMARY_REQUIRED")

    def test_packet_invalid_payload_json_is_denied(self):
        rc, p = self.kz("W7", "--summary", "Bad payload packet.", "--payload-json", "{not json")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("status"), "DENIED")
        self.assertEqual(p.get("code"), "DENIED_JSON_INVALID")

    def test_packet_payload_json_file_is_accepted(self):
        payload_file = self.root / "packet-payload.json"
        payload_file.write_text('{"kind": "diag", "n": 1}', encoding="utf-8")
        rc, p = self.kz("W7", "--summary", "Packet payload from a file.", "--payload-json-file", str(payload_file))
        self.assertEqual(rc, 0, p)
        self.assertTrue(p["id"].startswith("dp_"), p)
        self.assertEqual(self._table_counts()["diagnostic_packets"], 1)

    def test_packet_payload_json_file_missing_is_denied(self):
        rc, p = self.kz("W5", "--summary", "Packet with a bad file path.", "--payload-json-file", str(self.root / "absent.json"))
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_FILE_NOT_FOUND")


class PacketPayloadTypeTest(IsolatedDBTest):
    """A6: packets align with the lab's object-only payload contract."""

    def test_w5_rejects_non_object_payload(self):
        rc, p = self.kz("W5", "--summary", "Packet with a list payload.", "--payload-json", "[1,2]")
        self.assertEqual(rc, 2, p)
        self.assertEqual(p.get("code"), "DENIED_PAYLOAD_TYPE", p)
