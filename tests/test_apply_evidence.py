"""Contract tests: apply-evidence normalization/path-encoding round-trip + supervisor driven-turn loopback envelope bounds (128-row T6 cap, MAX_EVIDENCE/EVENT bytes)."""
from __future__ import annotations

import json
import unittest

from kaizen_components.hashing import word_count
from kaizen_components.orchestration.apply_evidence import (
    MAX_EVIDENCE_BYTES,
    MAX_EVENT_BYTES,
    PATH_ENCODING,
    WORST_CASE_EVIDENCE_BYTES,
    decode_mismatch_path,
    normalize_apply_evidence,
)
from kaizen_components.orchestration.adapters.claude_sdk import ClaudeSdkAdapter
from kaizen_components.orchestration import supervisor as supervisor_module
from kaizen_components.orchestration.supervisor import Supervisor

_MAX_PATH_CHARS = 1_024


class ApplyEvidenceTest(unittest.TestCase):
    """Invariant family: mismatch-evidence completeness/uncertainty flags, whitespace-free path encoding safety, loopback-budget compliance."""

    @staticmethod
    def _long_path(index: int) -> str:
        """Build an exactly bounded path with a dNNN prefix, preserved spaces, and trailing z."""
        prefix = f"d{index:03}/"
        return prefix + (" " * (_MAX_PATH_CHARS - len(prefix) - 1)) + "z"

    def test_128_full_state_rows_retain_exact_whitespace_paths_below_t6_and_envelope_bounds(self) -> None:
        source_rows = [{
            "path": self._long_path(index),
            "reason": "r" * 64,
            "expected_exists": True,
            "actual_exists": True,
            "expected_sha256": "a" * 64,
            "actual_sha256": "b" * 64,
        } for index in range(128)]

        evidence = normalize_apply_evidence({"partial_apply": True, "mismatches": source_rows})
        durable = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
        provider_wire = json.dumps(evidence, ensure_ascii=True, separators=(",", ":"))

        self.assertEqual(evidence["mismatch_count"], 128)
        self.assertTrue(evidence["mismatch_evidence_complete"])
        self.assertFalse(evidence["mismatch_evidence_uncertain"])
        self.assertEqual(evidence["mismatch_path_encoding"], PATH_ENCODING)
        self.assertEqual(
            [decode_mismatch_path(item["path"]) for item in evidence["mismatches"]],
            [item["path"] for item in source_rows],
        )
        self.assertEqual(word_count(durable), 1)
        self.assertLess(len(durable.encode("utf-8")), MAX_EVIDENCE_BYTES)
        self.assertLess(len(provider_wire.encode("utf-8")), MAX_EVENT_BYTES)
        self.assertLess(WORST_CASE_EVIDENCE_BYTES + 4 * 1_024, MAX_EVENT_BYTES)

        safe_result = ClaudeSdkAdapter._safe_tool_result_metadata({
            "status": "DENIED", "partial_apply": True, "mismatches": source_rows,
        })
        durable_body = json.dumps({
            "name": "kaizen_propose_changes",
            "tool_call_id": "tool-" + "f" * 64,
            "result": safe_result,
        }, ensure_ascii=False, separators=(",", ":"))
        event = {
            "sequence_no": 1,
            "event_kind": "tool_call",
            "marker": "close_fail",
            "summary": "x" * 256,
            "correlation_id": "tool-" + "f" * 64,
            "code": "DENIED_APPROVAL_STALE_RERUN_REQUIRED",
            "body": durable_body,
        }
        supervisor = Supervisor.__new__(Supervisor)
        turn_state_calls: list[tuple[str, bool]] = []

        def driven_turn_state(run_id: str, terminal: bool) -> str:
            turn_state_calls.append((run_id, terminal))
            return "terminal"

        supervisor._driven_turn_state = driven_turn_state
        response = supervisor._events_payload(
            "ar-" + "a" * 64, [event], 0, True, "failed", controller="driven",
        )

        self.assertEqual(turn_state_calls, [("ar-" + "a" * 64, True)])
        self.assertFalse(response.get("body_omitted", False))
        self.assertEqual(response["events"], [event])
        self.assertLessEqual(
            Supervisor._response_bytes(response), supervisor_module._LOOPBACK_RESPONSE_BUDGET,
        )

    def test_reasonless_full_state_row_gets_stable_reason_without_losing_truth(self) -> None:
        raw = {
            "path": "nested/name with space.txt",
            "expected_exists": True,
            "actual_exists": False,
            "expected_sha256": "c" * 64,
            "actual_sha256": None,
        }
        evidence = normalize_apply_evidence({"partial_apply": True, "mismatches": [raw]})

        row = evidence["mismatches"][0]
        self.assertEqual(row["reason"], "final_state_mismatch")
        self.assertEqual(decode_mismatch_path(row["path"]), raw["path"])
        self.assertEqual(row["expected_sha256"], raw["expected_sha256"])
        self.assertIsNone(row["actual_sha256"])
        self.assertTrue(evidence["mismatch_evidence_complete"])

    def test_path_encoding_round_trips_backslashes_unicode_and_whitespace(self) -> None:
        # ``\u0020`` below is literal backslash text; the tab and ordinary spaces are real whitespace.
        raw = "dir\\literal\\u0020/name~with\twhitespace and nonascii 雪.txt"
        evidence = normalize_apply_evidence({
            "partial_apply": False,
            "mismatches": [{"path": raw, "reason": "base_changed"}],
        })
        self.assertEqual(decode_mismatch_path(evidence["mismatches"][0]["path"]), raw)
        self.assertNotIn(" ", evidence["mismatches"][0]["path"])
        self.assertNotIn("\t", evidence["mismatches"][0]["path"])

    def test_backslash_joined_traversal_is_rejected_on_every_host(self) -> None:
        evidence = normalize_apply_evidence({
            "partial_apply": True,
            "mismatches": [{"path": "dir\\..\\outside.txt", "reason": "base_changed"}],
        })
        self.assertEqual(evidence["mismatches"], [{
            "path": "", "reason": "mismatch_evidence_invalid",
        }])

    def test_overflow_invalid_shape_hash_and_reason_fail_to_explicit_uncertainty(self) -> None:
        cases = [
            [{"path": f"{index}.txt", "reason": "base_changed"} for index in range(129)],
            [{"path": "x.txt", "reason": "base_changed", "content": "forbidden"}],
            [{
                "path": "x.txt", "reason": "final_state_mismatch",
                "expected_exists": True, "actual_exists": False,
                "expected_sha256": "BAD", "actual_sha256": None,
            }],
            [{"path": "x.txt", "reason": "contains whitespace"}],
        ]
        expected_reasons = [
            "mismatch_evidence_overflow",
            "mismatch_evidence_invalid",
            "mismatch_evidence_invalid",
            "mismatch_evidence_invalid",
        ]

        for rows, expected_reason in zip(cases, expected_reasons):
            with self.subTest(expected_reason=expected_reason):
                evidence = normalize_apply_evidence({"partial_apply": True, "mismatches": rows})
                self.assertEqual(evidence["mismatches"], [{"path": "", "reason": expected_reason}])
                self.assertFalse(evidence["mismatch_evidence_complete"])
                self.assertTrue(evidence["mismatch_evidence_uncertain"])

    def test_malformed_or_noncanonical_encoded_paths_are_rejected(self) -> None:
        for value in ("raw space", "raw\\slash", "raw雪", "dangling~", "~z", "~0000zz", "~000041"):
            with self.subTest(value=value):
                evidence = normalize_apply_evidence({
                    "partial_apply": True,
                    "mismatch_path_encoding": PATH_ENCODING,
                    "mismatches": [{"path": value, "reason": "base_changed"}],
                })
                self.assertEqual(evidence["mismatches"], [{
                    "path": "", "reason": "mismatch_evidence_invalid",
                }])

    def test_partial_apply_without_rows_is_never_claimed_exact(self) -> None:
        evidence = normalize_apply_evidence({"partial_apply": True, "mismatches": []})
        self.assertEqual(evidence["mismatches"], [{
            "path": "", "reason": "mismatch_evidence_missing",
        }])
        self.assertFalse(evidence["mismatch_evidence_complete"])
        self.assertTrue(evidence["mismatch_evidence_uncertain"])

    def test_empty_locator_can_never_be_claimed_complete(self) -> None:
        evidence = normalize_apply_evidence({
            "partial_apply": False,
            "mismatches": [{"path": "", "reason": "base_changed"}],
        })
        self.assertEqual(evidence["mismatches"], [{"path": "", "reason": "base_changed"}])
        self.assertFalse(evidence["mismatch_evidence_complete"])
        self.assertTrue(evidence["mismatch_evidence_uncertain"])

    def test_lone_surrogate_rejects_once_and_normalization_is_idempotent(self) -> None:
        evidence = normalize_apply_evidence({
            "partial_apply": True,
            "mismatches": [{"path": "bad\ud800.txt", "reason": "base_changed"}],
        })
        self.assertEqual(evidence["mismatches"], [{
            "path": "", "reason": "mismatch_evidence_invalid",
        }])
        self.assertEqual(normalize_apply_evidence(evidence), evidence)


if __name__ == "__main__":
    unittest.main()
