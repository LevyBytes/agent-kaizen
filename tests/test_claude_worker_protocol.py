"""Verify frame, capability-probe, reference, model-catalog, and profile contracts for the Claude worker protocol."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from kaizen_components.orchestration import claude_worker_protocol as protocol_module
from kaizen_components.orchestration.claude_worker_protocol import (
    CAPABILITY_PROBE_FEATURES,
    DENIAL_CODES,
    MAX_DELTA_BYTES,
    MAX_FRAME_BYTES,
    TOOL_NAMES,
    WorkerProtocolError,
    capability_probe_digest,
    decode_frame,
    encode_frame,
    sanitize_model_catalog,
    validate_capability_probe_result,
    validate_profile,
    validate_reference,
)


class FrameContractTest(unittest.TestCase):
    def test_request_response_event_round_trip(self) -> None:
        frames = [
            {"v": 1, "type": "request", "id": "r1", "op": "initialize", "session_id": "s1", "body": {}},
            {"v": 1, "type": "request", "id": "r2", "op": "capability.probe", "session_id": "s1",
             "body": {"feature": "streaming", "challenge": "cp-1"}},
            {"v": 1, "type": "response", "id": "r1", "ok": True, "body": {}},
            {"v": 1, "type": "event", "event": "delta", "session_id": "s1", "turn_id": "t1",
             "seq": 1, "body": {"text": "hello"}},
        ]
        for frame in frames:
            self.assertEqual(decode_frame(encode_frame(frame)), frame)

    def test_unknown_operation_and_event_fail_closed(self) -> None:
        for frame in (
            {"v": 1, "type": "request", "id": "r1", "op": "shell", "body": {}},
            {"v": 1, "type": "event", "event": "mystery", "session_id": "s1", "seq": 1, "body": {}},
        ):
            with self.assertRaises(WorkerProtocolError) as raised:
                encode_frame(frame)
            self.assertEqual(raised.exception.code, "DENIED_WORKER_PROTOCOL")

    def test_operation_and_event_scopes_are_mandatory(self) -> None:
        invalid = (
            {"v": 1, "type": "request", "id": "r1", "op": "initialize", "body": {}},
            {"v": 1, "type": "request", "id": "r2", "op": "turn.start", "session_id": "s1", "body": {}},
            {"v": 1, "type": "request", "id": "r3", "op": "session.close", "session_id": "s1",
             "turn_id": "t1", "body": {}},
            {"v": 1, "type": "event", "event": "delta", "session_id": "s1", "seq": 1,
             "body": {"text": "wrong turn"}},
            {"v": 1, "type": "event", "event": "initialized", "session_id": "s1", "turn_id": "t1",
             "seq": 1, "body": {}},
        )
        for frame in invalid:
            with self.subTest(frame=frame), self.assertRaises(WorkerProtocolError):
                encode_frame(frame)

    def test_exact_frame_ceiling_and_multiple_lines(self) -> None:
        frame = {"v": 1, "type": "response", "id": "r1", "ok": True, "body": {"x": ""}}
        accepted = {**frame, "body": {"x": "x" * (MAX_FRAME_BYTES - len(encode_frame(frame)))}}
        self.assertEqual(len(encode_frame(accepted)), MAX_FRAME_BYTES)
        rejected = {**accepted, "body": {"x": accepted["body"]["x"] + "x"}}
        with self.assertRaises(WorkerProtocolError) as raised:
            encode_frame(rejected)
        self.assertEqual(raised.exception.code, "DENIED_WORKER_OVERSIZE")
        valid_line = encode_frame({"v": 1, "type": "response", "id": "r1", "ok": True, "body": {}})
        with self.assertRaises(WorkerProtocolError) as multiline:
            decode_frame(valid_line + valid_line)
        self.assertEqual(multiline.exception.code, "DENIED_WORKER_PROTOCOL")

    def test_contract_constants(self) -> None:
        self.assertEqual(MAX_FRAME_BYTES, 1_048_576)
        self.assertEqual(MAX_DELTA_BYTES, 32_768)
        self.assertEqual(len(TOOL_NAMES), 5)
        self.assertEqual(len(CAPABILITY_PROBE_FEATURES), 6)
        self.assertEqual(len(set(CAPABILITY_PROBE_FEATURES)), 6)
        self.assertIn("DENIED_AUTH_MODE_MISMATCH", DENIAL_CODES)
        self.assertIn("MODEL_CALL_BUDGET_EXHAUSTED", DENIAL_CODES)

    def test_runtime_manifest_matches_frozen_contract(self) -> None:
        manifest_path = Path(protocol_module.__file__).resolve().with_name("vendor_workers") / "claude_agent" / "runtime-manifest.json"
        self.assertTrue(manifest_path.is_file(), f"Claude runtime manifest missing: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["sdk_version"], "0.3.207")
        self.assertEqual(manifest["zod_version"], "4.4.3")
        self.assertEqual(manifest["tools"], list(TOOL_NAMES))
        self.assertEqual(manifest["max_frame_bytes"], MAX_FRAME_BYTES)
        self.assertEqual(manifest["max_delta_bytes"], MAX_DELTA_BYTES)
        self.assertEqual(manifest["capability_probes"], list(CAPABILITY_PROBE_FEATURES))
        self.assertFalse(manifest["lifecycle_scripts_allowed"])


class CapabilityProbeContractTest(unittest.TestCase):
    @staticmethod
    def result(feature: str, challenge: str = "cp-contract") -> dict:
        """Build challenge-bound proven evidence and add the required artifact reference for diff_snapshots."""
        value = {
            "probe_version": 1,
            "feature": feature,
            "challenge": challenge,
            "status": "proven",
            "evidence_sha256": capability_probe_digest(feature, challenge),
        }
        if feature == "diff_snapshots":
            value["artifact_ref"] = {
                "root": "runtime", "path": "outbox/probe.utf8", "sha256": "a" * 64,
                "bytes": 5, "encoding": "utf-8",
            }
        return value

    def test_each_feature_requires_its_own_exact_challenge_bound_evidence(self) -> None:
        for feature in CAPABILITY_PROBE_FEATURES:
            with self.subTest(feature=feature):
                value = self.result(feature)
                self.assertEqual(
                    validate_capability_probe_result(value, feature=feature, challenge="cp-contract"), value,
                )

    def test_malformed_cross_feature_and_unproven_results_fail_closed(self) -> None:
        cases = []
        wrong_digest = self.result("streaming")
        wrong_digest["evidence_sha256"] = "0" * 64
        cases.append((wrong_digest, "streaming", "cp-contract"))
        cross_feature = self.result("streaming")
        cases.append((cross_feature, "image_attachments", "cp-contract"))
        wrong_challenge = self.result("streaming")
        cases.append((wrong_challenge, "streaming", "cp-other"))
        unsupported = self.result("streaming")
        unsupported["status"] = "unsupported"
        cases.append((unsupported, "streaming", "cp-contract"))
        extra = self.result("streaming")
        extra["features"] = list(CAPABILITY_PROBE_FEATURES)
        cases.append((extra, "streaming", "cp-contract"))
        missing_artifact = self.result("diff_snapshots")
        missing_artifact.pop("artifact_ref")
        cases.append((missing_artifact, "diff_snapshots", "cp-contract"))
        for value, feature, challenge in cases:
            with self.subTest(value=value, feature=feature), self.assertRaises(WorkerProtocolError):
                validate_capability_probe_result(value, feature=feature, challenge=challenge)


class ReferenceContractTest(unittest.TestCase):
    def test_metadata_only_reference(self) -> None:
        ref = {"root": "cache", "path": "objects/aa/file", "sha256": "a" * 64, "bytes": 7,
               "encoding": "utf-8"}
        self.assertEqual(validate_reference(ref), ref)

    def test_reference_rejects_escape_and_raw_bytes(self) -> None:
        base = {"root": "cache", "path": "objects/file", "sha256": "a" * 64, "bytes": 7}
        for bad in ({**base, "path": "../file"}, {**base, "content": "secret"}, {**base, "bytes": True}):
            with self.assertRaises(WorkerProtocolError):
                validate_reference(bad)


class ModelContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = sanitize_model_catalog([{
            "value": "claude-account-model", "displayName": "Account Model", "description": "available",
            "supportedEffortLevels": ["low", "high"], "supportsAdaptiveThinking": True,
            "supportsFastMode": False,
        }])

    def test_dynamic_catalog_and_profile(self) -> None:
        self.assertEqual(self.catalog[0]["id"], "claude-account-model")
        self.assertEqual(
            validate_profile(self.catalog, "claude-account-model", "high", 8, "subscription"),
            {"model": "claude-account-model", "reasoning_effort": "high", "max_turns": 8,
             "auth_mode": "subscription"},
        )

    def test_missing_model_effort_budget_and_auth_have_stable_codes(self) -> None:
        cases = [
            (("gone", "high", 8, "subscription"), "DENIED_MODEL_UNAVAILABLE"),
            (("claude-account-model", None, 8, "subscription"), "DENIED_EFFORT_UNSUPPORTED"),
            (("claude-account-model", "max", 8, "subscription"), "DENIED_EFFORT_UNSUPPORTED"),
            (("claude-account-model", "high", 33, "subscription"), "MODEL_CALL_BUDGET_EXHAUSTED"),
            (("claude-account-model", "high", 8, "api-key"), "DENIED_AUTH_MODE_MISMATCH"),
        ]
        for args, code in cases:
            with self.subTest(code=code), self.assertRaises(WorkerProtocolError) as raised:
                validate_profile(self.catalog, *args)
            self.assertEqual(raised.exception.code, code)

    def test_models_without_effort_choices_require_no_effort_and_accept_both_turn_bounds(self) -> None:
        catalog = sanitize_model_catalog([{
            "value": "claude-no-effort", "displayName": "No Effort", "supportedEffortLevels": [],
        }])
        for max_turns in (1, 32):
            with self.subTest(max_turns=max_turns):
                self.assertEqual(
                    validate_profile(catalog, "claude-no-effort", None, max_turns, "subscription"),
                    {"model": "claude-no-effort", "max_turns": max_turns, "auth_mode": "subscription"},
                )
        with self.assertRaises(WorkerProtocolError) as raised:
            validate_profile(catalog, "claude-no-effort", "high", 8, "subscription")
        self.assertEqual(raised.exception.code, "DENIED_EFFORT_UNSUPPORTED")

    def test_malformed_or_empty_catalog_fails_closed(self) -> None:
        for value in ([], [{"value": "m", "displayName": "M", "supportedEffortLevels": ["turbo"]}]):
            with self.assertRaises(WorkerProtocolError) as raised:
                sanitize_model_catalog(value)
            self.assertEqual(raised.exception.code, "DENIED_MODEL_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
