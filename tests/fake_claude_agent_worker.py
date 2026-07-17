"""Deterministic subprocess fixture for the private Claude worker protocol; never imports an SDK.

Runtime roots come from ``KAIZEN_CLAUDE_SESSION_ROOT`` and ``KAIZEN_CLAUDE_CACHE_ROOT``. ``KAIZEN_FAKE_CLAUDE_*`` variables select authentication, profile, catalog, probe-tampering, and fault-injection scenarios used by the owning tests.
"""

from __future__ import annotations

import json
import hashlib
import os
import sys
import time
from pathlib import Path


CAPABILITY_EVIDENCE = {
    "streaming": "sdk-delta-parser-and-32k-fragmentation",
    "image_attachments": "verified-reference-to-sdk-image-block",
    "governed_context": "verified-reference-to-governed-prompt",
    "diff_snapshots": "proposal-outbox-reference-roundtrip",
    "controlled_tools": "exact-kaizen-tool-schema-set",
    "process_execution": "direct-argv-process-schema",
}

MAX_FRAME_BYTES = 1024 * 1024


def hang_forever() -> None:
    while True:
        time.sleep(60)


def emit(value: dict) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def response(request: dict, *, body: dict | None = None, error: dict | None = None) -> None:
    value = {"v": 1, "type": "response", "id": request["id"], "ok": error is None}
    if error is not None:
        value["error"] = error
    else:
        value["body"] = body or {}
    emit(value)


def event(request: dict, name: str, seq: int, body: dict) -> None:
    value = {"v": 1, "type": "event", "event": name,
             "session_id": request.get("session_id", "fake-session"), "seq": seq, "body": body}
    if request.get("turn_id"):
        value["turn_id"] = request["turn_id"]
    emit(value)


def read_reference(value: dict) -> bytes:
    """Resolve ref under runtime\\ | cache root; reject abs/empty/./.. parts + symlink escape (relative_to re-check); verify bytes+sha256; ValueError on any mismatch."""
    if not isinstance(value, dict) or value.get("root") not in ("runtime", "cache"):
        raise ValueError("bad reference")
    root_name = "KAIZEN_CLAUDE_SESSION_ROOT" if value["root"] == "runtime" else "KAIZEN_CLAUDE_CACHE_ROOT"
    root_value = os.environ.get(root_name)
    if not root_value:
        raise ValueError(f"missing required environment variable: {root_name}")
    root = Path(root_value).resolve()
    relative = Path(value["path"])
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise ValueError("bad reference path")
    target = (root / relative).resolve(strict=True)
    target.relative_to(root)
    content = target.read_bytes()
    if len(content) != value.get("bytes") or hashlib.sha256(content).hexdigest() != value.get("sha256"):
        raise ValueError("bad reference content")
    return content


def capability_probe(request: dict) -> dict:
    """Build proof frame; feature must be known + challenge str; image/governed verify staged refs; diff_snapshots writes outbox artifact; DARK/MALFORMED/CROSS_CLAIM tamper output."""
    body = request.get("body") or {}
    feature = body.get("feature")
    challenge = body.get("challenge")
    if feature not in CAPABILITY_EVIDENCE or not isinstance(challenge, str):
        raise ValueError("bad capability probe")
    dark = set(filter(None, os.environ.get("KAIZEN_FAKE_CLAUDE_PROBE_DARK", "").split(",")))
    if feature == "image_attachments":
        if read_reference(body.get("prompt_ref")) != b"KAIZEN_CAPABILITY_PROBE_PROMPT":
            raise ValueError("bad image prompt")
        image = read_reference(body.get("image_ref"))
        if not image.startswith(b"\x89PNG\r\n\x1a\n") or body["image_ref"].get("media_type") != "image/png":
            raise ValueError("bad image")
    elif feature == "governed_context":
        if read_reference(body.get("prompt_ref")) != b"KAIZEN_CAPABILITY_PROBE_PROMPT" \
                or read_reference(body.get("context_ref")) != b"KAIZEN_CAPABILITY_CONTEXT":
            raise ValueError("bad governed context")
    result = {
        "probe_version": 1,
        "feature": feature,
        "challenge": challenge,
        "status": "unsupported" if feature in dark else "proven",
    }
    # This NUL-delimited material is the protocol's locked capability-proof digest contract.
    material = f"kaizen-capability-probe-v1\0{feature}\0{challenge}\0{CAPABILITY_EVIDENCE[feature]}"
    result["evidence_sha256"] = hashlib.sha256(material.encode("utf-8")).hexdigest()
    if feature == "diff_snapshots" and feature not in dark:
        content = f"KAIZEN_CAPABILITY_DIFF:{challenge}".encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        relative = Path("outbox") / "sha256" / digest[:2] / f"{digest}.utf8"
        session_root = os.environ.get("KAIZEN_CLAUDE_SESSION_ROOT")
        if not session_root:
            raise ValueError("missing required environment variable: KAIZEN_CLAUDE_SESSION_ROOT")
        target = Path(session_root).resolve() / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        result["artifact_ref"] = {
            "root": "runtime", "path": relative.as_posix(), "sha256": digest,
            "bytes": len(content), "encoding": "utf-8",
        }
    if feature == os.environ.get("KAIZEN_FAKE_CLAUDE_PROBE_MALFORMED"):
        result["evidence_sha256"] = "0" * 64
    if feature == os.environ.get("KAIZEN_FAKE_CLAUDE_PROBE_CROSS_CLAIM"):
        result["feature"] = "streaming" if feature != "streaming" else "image_attachments"
    return result


def model_catalog(*, refresh: bool) -> list[dict]:
    """SDK-shape fixture catalog; REPLACE/MALFORMED+refresh drive add/remove/dup; NO_EFFORT strips efforts."""
    mode = os.environ.get("KAIZEN_FAKE_CLAUDE_REFRESH_CATALOG", "")
    efforts = [] if os.environ.get("KAIZEN_FAKE_CLAUDE_NO_EFFORT") == "1" else ["low", "high"]

    def entry(model_id: str, label: str) -> dict:
        return {
            "value": model_id,
            "displayName": label,
            "description": "offline fixture",
            "supportedEffortLevels": efforts,
            "supportsAdaptiveThinking": True,
            "supportsFastMode": False,
        }

    if mode == "replace":
        return [
            entry("claude-stable-model", "Claude Stable Model"),
            entry("claude-new-model" if refresh else "claude-removed-model",
                  "Claude New Model" if refresh else "Claude Removed Model"),
        ]
    if mode == "malformed" and refresh:
        duplicate = entry("claude-test-model", "Claude Test Model")
        return [duplicate, dict(duplicate)]
    return [entry("claude-test-model", "Claude Test Model")]


def main() -> int:
    """Stdin->stdout JSON loop; dispatch initialize/capability.probe/turn.*/session.close/tool.result; FAULT injects protocol/oversize/hang/death; return 2 on exception, 0 on clean close."""
    seq = 0
    initialize_count = 0
    fault = os.environ.get("KAIZEN_FAKE_CLAUDE_FAULT", "")
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request.get("v") != 1 or request.get("type") != "request":
                raise ValueError("bad frame")
            op = request.get("op")
            if op == "initialize":
                if fault == "initialize-field":
                    response(request, error={"code": "DENIED_WORKER_PROTOCOL", "field": "account"})
                    continue
                if fault == "initialize-invalid-field":
                    response(request, error={
                        "code": "DENIED_WORKER_PROTOCOL",
                        "field": "account\nPRIVATE_FIELD_MUST_NOT_ESCAPE",
                    })
                    continue
                if fault == "initialize-oversize-field":
                    response(request, error={
                        "code": "DENIED_WORKER_PROTOCOL",
                        "field": "PRIVATE_FIELD_MUST_NOT_ESCAPE" + ("x" * 128),
                    })
                    continue
                if fault == "malformed-stdout":
                    sys.stdout.write("{not-json}\n")
                    sys.stdout.flush()
                    hang_forever()
                if fault == "oversize-stdout":
                    sys.stdout.write("x" * (MAX_FRAME_BYTES + 1) + "\n")
                    sys.stdout.flush()
                    hang_forever()
                if fault == "multibyte-oversize-stdout":
                    sys.stdout.buffer.write((b"\xc3\xa9" * ((MAX_FRAME_BYTES // 2) + 1)) + b"\n")
                    sys.stdout.buffer.flush()
                    hang_forever()
                if fault == "stderr-flood":
                    sys.stderr.write("PRIVATE_STDERR_MUST_NOT_ESCAPE:" + "s" * (70 * 1024) + "\n")
                    sys.stderr.flush()
                body = request.get("body") or {}
                refreshing = initialize_count > 0
                if refreshing and body:
                    raise ValueError("refresh initialize body must be empty")
                if ("model" in body and body["model"] is None) \
                        or ("reasoning_effort" in body and body["reasoning_effort"] is None):
                    raise ValueError("null profile field")
                auth_source = os.environ.get("KAIZEN_FAKE_CLAUDE_AUTH_SOURCE", "oauth")
                models = model_catalog(refresh=refreshing)
                initialized = {
                    "runtime_kind": "claude-agent-sdk",
                    "runtime_version": "0.3.207",
                    "auth_source": auth_source,
                    "effective_model": body.get("model", models[0]["value"]),
                    "models": models,
                    "tools": ["kaizen_read_file", "kaizen_list_files", "kaizen_search_text",
                              "kaizen_run_process", "kaizen_propose_changes"],
                }
                if os.environ.get("KAIZEN_FAKE_CLAUDE_CLAIM_SUBSCRIPTION") == "1":
                    initialized["auth_mode"] = "subscription"
                initialize_count += 1
                seq += 1
                event(request, "initialized", seq, initialized)
                response(request, body=initialized)
            elif op == "capability.probe":
                response(request, body=capability_probe(request))
            elif op == "turn.start":
                if fault == "worker-death-on-turn":
                    os._exit(23)
                seq += 1
                event(request, "turn.open", seq, {})
                text = str((request.get("body") or {}).get("fixture_text") or "FAKE_CLAUDE_OK")
                for fragment in (text[: len(text) // 2], text[len(text) // 2 :]):
                    if fragment:
                        seq += 1
                        event(request, "delta", seq, {"text": fragment})
                seq += 1
                event(request, "turn.result", seq, {
                    "status": "OK", "final_text": text, "fatal": False, "num_turns": 1,
                })
                response(request, body={"accepted": True})
            elif op in ("turn.steer", "turn.interrupt", "session.close", "tool.result"):
                if fault == "hang-interrupt" and op == "turn.interrupt":
                    hang_forever()
                if fault == "hang-close" and op == "session.close":
                    hang_forever()
                response(request, body={"accepted": True, "termination_proven": op == "session.close"})
                if op == "session.close":
                    break
            else:
                response(request, error={"code": "DENIED_WORKER_PROTOCOL"})
        except Exception:
            emit({"v": 1, "type": "event", "event": "fatal", "session_id": "fake-session",
                  "seq": max(1, seq + 1), "body": {"code": "DENIED_WORKER_PROTOCOL"}})
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
