"""Daemon-owned durable approval broker.

The broker composes the C4 approval projection and its authoritative T6 approval event in one
``write_tx``. In-memory waiter callbacks are deliberately invoked only after that transaction commits.
No filesystem snapshot materialization happens here; D3 supplies the optional pre-release validator.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Collection, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import db
from ..agent_runs import _refresh_cache
from ..denials import KaizenDenied
from ..hashing import utc_text_hash
from ..redaction import assert_redacted
from ..schemas import validate_record
from ..session_records import insert_approval_conn, transition_approval_conn


DEFAULT_TIMEOUT_SECONDS = 300
MAX_FILE_CHANGES = 64
MAX_TEXT_SIDE_BYTES = 8 * 1024 * 1024
MAX_SNAPSHOT_SET_BYTES = 64 * 1024 * 1024

DENIED_BODY_INVALID = "DENIED_APPROVAL_BODY_INVALID"
DENIED_SNAPSHOT_INVALID = "DENIED_APPROVAL_SNAPSHOT_INVALID"
DENIED_TIMEOUT = "DENIED_APPROVAL_TIMEOUT"
DENIED_STALE = "DENIED_APPROVAL_STALE_RERUN_REQUIRED"
DENIED_APPROVAL_CONFLICT = "DENIED_APPROVAL_CONFLICT"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OPEN_SOURCE_RE = re.compile(r"^approval:(?P<approval_id>[^:]+):open:(?P<revision>[1-9][0-9]*)$")
_BODY_KEYS = {"approval_revision", "expires_at", "snapshot_set_sha256", "file_changes"}
_CHANGE_KEYS = {
    "change_id", "path", "kind", "old_path", "preview_mode", "preview_reason", "before", "proposed",
}
_SIDE_KEYS = {"artifact_ref", "sha256", "bytes", "encoding", "media_type"}
_KINDS = {"create", "modify", "delete", "rename"}
_PREVIEW_REASONS = {"binary", "unsupported_encoding", "oversize"}

WaiterRelease = Callable[[str, str, str, Any], bool]
PreReleaseValidator = Callable[[dict[str, Any]], dict[str, Any] | None]


class ApprovalValidationError(ValueError):
    """An untrusted structured approval body cannot become a pending card."""

    def __init__(self, denial_code: str = DENIED_BODY_INVALID):
        super().__init__(denial_code)
        self.denial_code = denial_code


def _invalid(code: str = DENIED_BODY_INVALID) -> None:
    raise ApprovalValidationError(code)


def _exact_keys(value: Mapping[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        _invalid()


def _bounded_text(value: Any, *, maximum: int = 128) -> str:
    if not isinstance(value, str) or not (1 <= len(value) <= maximum) or any(ord(char) < 32 for char in value):
        _invalid()
    return value


def _relative_path(value: Any) -> str:
    path = _bounded_text(value, maximum=1024)
    parts = path.split("/")
    if (
        "\\" in path
        or ":" in path
        or path.startswith("/")
        or re.match(r"^[A-Za-z]:", path)
        or any(part in ("", ".", "..") for part in parts)
    ):
        _invalid(DENIED_SNAPSHOT_INVALID)
    return path


def _sha256(value: Any) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        _invalid(DENIED_SNAPSHOT_INVALID)
    return value


def _utc_z(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        _invalid()
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _invalid()
    return parsed


def _snapshot_side(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        _invalid()
    _exact_keys(value, _SIDE_KEYS)
    digest = _sha256(value.get("sha256"))
    if value.get("artifact_ref") != f"sha256:{digest}":
        _invalid(DENIED_SNAPSHOT_INVALID)
    size = value.get("bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        _invalid(DENIED_SNAPSHOT_INVALID)
    encoding = value.get("encoding")
    media_type = value.get("media_type")
    if (encoding is None and media_type is None) or (encoding is not None and media_type is not None):
        _invalid()
    if encoding is not None and encoding != "utf-8":
        _invalid()
    if media_type is not None:
        _bounded_text(media_type, maximum=128)
    return {
        "artifact_ref": value["artifact_ref"], "sha256": digest, "bytes": size,
        "encoding": encoding, "media_type": media_type,
    }


def _manifest_side(side: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if side is None:
        return None
    return {key: side.get(key) for key in ("sha256", "bytes", "encoding", "media_type")}


def canonical_snapshot_set_sha256(body: Mapping[str, Any]) -> str:
    """Hash the negotiated manifest (never artifact paths or bytes). Validation is separate."""
    changes = []
    for raw in body.get("file_changes", []):
        changes.append({
            "change_id": raw.get("change_id"), "path": raw.get("path"), "kind": raw.get("kind"),
            "old_path": raw.get("old_path"), "preview_mode": raw.get("preview_mode"),
            "preview_reason": raw.get("preview_reason"), "before": _manifest_side(raw.get("before")),
            "proposed": _manifest_side(raw.get("proposed")),
        })
    changes.sort(key=lambda item: (item["path"], item["old_path"] or "", item["kind"], item["change_id"]))
    manifest = {"approval_revision": body.get("approval_revision"), "file_changes": changes}
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_approval_open_body(body: Any) -> dict[str, Any]:
    """Validate and normalize the negotiated approval/open metadata-only body."""
    if not isinstance(body, Mapping):
        _invalid()
    _exact_keys(body, _BODY_KEYS)
    revision = body.get("approval_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        _invalid()
    expires_at = body.get("expires_at")
    _utc_z(expires_at)
    declared_set_hash = _sha256(body.get("snapshot_set_sha256"))
    raw_changes = body.get("file_changes")
    if not isinstance(raw_changes, list) or not (1 <= len(raw_changes) <= MAX_FILE_CHANGES):
        _invalid()

    changes: list[dict[str, Any]] = []
    change_ids: set[str] = set()
    total_bytes = 0
    for raw in raw_changes:
        if not isinstance(raw, Mapping):
            _invalid()
        _exact_keys(raw, _CHANGE_KEYS)
        change_id = _bounded_text(raw.get("change_id"))
        if change_id in change_ids:
            _invalid()
        change_ids.add(change_id)
        path = _relative_path(raw.get("path"))
        kind = raw.get("kind")
        if kind not in _KINDS:
            _invalid()
        old_path = raw.get("old_path")
        if old_path is not None:
            old_path = _relative_path(old_path)
        preview_mode = raw.get("preview_mode")
        preview_reason = raw.get("preview_reason")
        if preview_mode == "text":
            if preview_reason is not None:
                _invalid()
        elif preview_mode == "metadata":
            if preview_reason not in _PREVIEW_REASONS:
                _invalid()
        else:
            _invalid()
        before = _snapshot_side(raw.get("before"))
        proposed = _snapshot_side(raw.get("proposed"))
        if kind == "create" and (before is not None or proposed is None or old_path is not None):
            _invalid()
        if kind == "delete" and (before is None or proposed is not None or old_path is not None):
            _invalid()
        if kind == "modify" and (before is None or proposed is None or old_path is not None):
            _invalid()
        if kind == "rename" and (before is None or proposed is None or old_path is None or old_path == path):
            _invalid()
        sides = [side for side in (before, proposed) if side is not None]
        if preview_mode == "text":
            if any(side["encoding"] != "utf-8" or side["media_type"] is not None for side in sides):
                _invalid(DENIED_SNAPSHOT_INVALID)
            if any(side["bytes"] > MAX_TEXT_SIDE_BYTES for side in sides):
                _invalid(DENIED_SNAPSHOT_INVALID)
        total_bytes += sum(side["bytes"] for side in sides)
        changes.append({
            "change_id": change_id, "path": path, "kind": kind, "old_path": old_path,
            "preview_mode": preview_mode, "preview_reason": preview_reason,
            "before": before, "proposed": proposed,
        })
    if total_bytes > MAX_SNAPSHOT_SET_BYTES:
        _invalid(DENIED_SNAPSHOT_INVALID)
    normalized = {
        "approval_revision": revision, "expires_at": expires_at,
        "snapshot_set_sha256": declared_set_hash, "file_changes": changes,
    }
    if canonical_snapshot_set_sha256(normalized) != declared_set_hash:
        _invalid(DENIED_SNAPSHOT_INVALID)
    return normalized


def _json_body(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ApprovalValidationError() from error
    assert_redacted({"approval_body": encoded})
    return encoded


def _denial_response(
    code: str,
    required_action: str,
    *,
    current_revision: int | None,
    current_hash: str | None,
    metadata_required: bool,
) -> dict[str, Any]:
    return {
        "status": "DENIED", "code": code, "retryable": True,
        "required_action": required_action, "current_revision": current_revision,
        "current_snapshot_set_sha256": current_hash,
        "metadata_confirmation_required": metadata_required,
    }


def _terminal_fixture(denial_code: str) -> tuple[str, str]:
    if denial_code in (DENIED_BODY_INVALID, DENIED_SNAPSHOT_INVALID):
        summary = "approval preview unavailable; denied fail-closed"
        presentation = "unavailable"
    elif denial_code == DENIED_TIMEOUT:
        summary = "approval timed out; denied fail-closed"
        presentation = "timed_out"
    else:
        summary = "approval stale; denied fail-closed"
        presentation = "stale"
    return summary, _json_body({"decision": "denied", "denial_code": denial_code, "presentation": presentation})


def _load_body(raw: Any) -> dict[str, Any] | None:
    """Parse one stored JSON object, returning an empty object for no body and None when malformed."""

    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _assert_run_link(conn: Any, agent_run_id: str, session_id: str) -> None:
    row = conn.execute("SELECT session_id FROM agent_runs WHERE id = ?", (agent_run_id,)).fetchone()
    if row is None:
        raise KaizenDenied(
            "DENIED_AGENT_RUN_NOT_FOUND",
            {"agent_run_id": agent_run_id, "required_action": "open the run with T5 first"},
            exit_code=1,
        )
    if row[0] != session_id:
        raise KaizenDenied(
            DENIED_BODY_INVALID,
            {"required_action": "the approval session and driven run must match"},
            exit_code=2,
        )


def _insert_event_conn(
    conn: Any,
    *,
    agent_run_id: str,
    correlation_id: str,
    marker: str,
    source_event_id: str,
    summary: str,
    body: str,
    code: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    existing = conn.execute(
        "SELECT id, sequence_no FROM agent_events WHERE agent_run_id = ? AND source_event_id = ?",
        (agent_run_id, source_event_id),
    ).fetchone()
    if existing is not None:
        return {"id": existing[0], "sequence_no": existing[1], "deduplicated": True}
    clean = {
        k: v
        for k, v in {
            "agent_run_id": agent_run_id, "source_event_id": source_event_id,
            "correlation_id": correlation_id, "event_kind": "approval", "marker": marker,
            "code": code, "name": name, "summary": summary, "body": body,
        }.items()
        if v not in (None, "")
    }
    validate_record("agent_event", clean)
    assert_redacted({
        "source_event_id": source_event_id, "correlation_id": correlation_id,
        "summary": summary, "body": body, "code": code or "", "name": name or "",
    })
    event_id = db.new_id("ae")
    created = db.now()
    sequence_no = int(conn.execute(
        "SELECT COALESCE(MAX(sequence_no), 0) FROM agent_events WHERE agent_run_id = ?",
        (agent_run_id,),
    ).fetchone()[0]) + 1
    content_hash = utc_text_hash({"id": event_id, "agent_run_id": agent_run_id, **clean})
    conn.execute(
        "INSERT OR IGNORE INTO agent_events "
        "(id, created_at, agent_run_id, sequence_no, source_event_id, correlation_id, event_kind, "
        "marker, code, name, status_message, summary, body, content_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, 'approval', ?, ?, ?, NULL, ?, ?, ?)",
        (event_id, created, agent_run_id, sequence_no, source_event_id, correlation_id, marker,
         code, name, summary, body, content_hash),
    )
    landed = conn.execute(
        "SELECT id, sequence_no FROM agent_events WHERE agent_run_id = ? AND source_event_id = ?",
        (agent_run_id, source_event_id),
    ).fetchone()
    _refresh_cache(conn, agent_run_id)
    return {"id": landed[0], "sequence_no": landed[1], "deduplicated": landed[0] != event_id}


def _active_open_conn(conn: Any, approval_id: str, agent_run_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT source_event_id, body, name, sequence_no FROM agent_events "
        "WHERE agent_run_id = ? AND source_event_id LIKE ? ORDER BY sequence_no DESC LIMIT 1",
        (agent_run_id, f"approval:{approval_id}:open:%"),
    ).fetchone()
    if row is None:
        return None
    match = _OPEN_SOURCE_RE.fullmatch(row[0] or "")
    if match is None or match.group("approval_id") != approval_id:
        return None
    body = _load_body(row[1])
    return {
        "source_event_id": row[0], "body": body, "negotiated": row[2] == "diff_snapshot",
        "revision": int(match.group("revision")), "sequence_no": row[3],
    }


class ApprovalBroker:
    """One daemon's durable approval authority plus a post-commit waiter-release seam."""

    def __init__(
        self,
        release_waiter: WaiterRelease | None = None,
        pre_release_validator: PreReleaseValidator | None = None,
    ) -> None:
        self._release_waiter = release_waiter
        self._pre_release_validator = pre_release_validator

    def get(self, approval_id: str) -> dict[str, Any] | None:
        """Read one C4 plus its broker-managed active preview metadata, if present."""
        def read(conn: Any) -> dict[str, Any] | None:
            row = conn.execute(
                "SELECT id, session_id, correlation_id, request_type, state, decided_by, rule_id, "
                "summary, created_at, updated_at FROM approval_requests WHERE id = ?",
                (approval_id,),
            ).fetchone()
            if row is None:
                return None
            event = conn.execute(
                "SELECT e.agent_run_id, e.source_event_id, e.body, e.name "
                "FROM agent_events e JOIN agent_runs r ON r.id = e.agent_run_id "
                "WHERE r.session_id = ? AND e.correlation_id = ? AND e.source_event_id LIKE ? "
                "ORDER BY e.sequence_no DESC LIMIT 1",
                (row[1], row[2], f"approval:{approval_id}:open:%"),
            ).fetchone()
            managed = False
            active: dict[str, Any] | None = None
            if event is not None:
                match = _OPEN_SOURCE_RE.fullmatch(event[1] or "")
                managed = match is not None and match.group("approval_id") == approval_id
                if managed:
                    event_body = _load_body(event[2])
                    active = {
                        "agent_run_id": event[0], "source_event_id": event[1], "body": event_body,
                        "negotiated": event[3] == "diff_snapshot",
                        "revision": int(match.group("revision")),
                    }
            return {
                "id": row[0], "session_id": row[1], "correlation_id": row[2],
                "request_type": row[3], "state": row[4], "decided_by": row[5], "rule_id": row[6],
                "summary": row[7], "created_at": row[8], "updated_at": row[9],
                "broker_managed": managed, "active": active,
            }

        return db.read_retry(read)

    def _release(self, session_id: str, agent_run_id: str, correlation_id: str, decision: Any) -> bool:
        if self._release_waiter is None:
            return False
        try:
            return bool(self._release_waiter(session_id, agent_run_id, correlation_id, decision))
        except Exception:  # noqa: BLE001 -- commit already won; reconciliation owns callback recovery
            return False

    def open(
        self,
        *,
        session_id: str,
        agent_run_id: str,
        correlation_id: str,
        body: Mapping[str, Any] | None = None,
        negotiated: bool = False,
        request_type: str = "tool_approval",
        summary: str = "Approval required.",
        is_test: bool = False,
        session_fence: Any = None,
    ) -> dict[str, Any]:
        """Atomically create pending C4 + approval/open, or a sanitized terminal denial on bad input."""
        approval_id = db.new_id("apr")
        opened_at = datetime.now(timezone.utc)
        created = opened_at.isoformat()
        revision = 1
        try:
            if negotiated:
                normalized_body = validate_approval_open_body(body)
                revision = normalized_body["approval_revision"]
                if revision != 1:
                    raise ApprovalValidationError(DENIED_BODY_INVALID)
                # The daemon owns the approval clock. A caller-supplied RFC3339 value is structural
                # input only; the durable card always receives exactly the locked five-minute budget.
                normalized_body["expires_at"] = (
                    opened_at + timedelta(seconds=DEFAULT_TIMEOUT_SECONDS)
                ).isoformat().replace("+00:00", "Z")
            else:
                normalized_body = dict(body or {})
            serialized_body = _json_body(normalized_body)
        except (ApprovalValidationError, KaizenDenied) as error:
            denial_code = error.denial_code if isinstance(error, ApprovalValidationError) else DENIED_BODY_INVALID
            return self.deny_invalid(
                session_id=session_id, agent_run_id=agent_run_id, correlation_id=correlation_id,
                denial_code=denial_code, approval_id=approval_id, request_type=request_type,
                is_test=is_test, session_fence=session_fence,
            )
        def op(conn: Any, _attempt: int) -> dict[str, Any]:
            _assert_run_link(conn, agent_run_id, session_id)
            existing_rows = conn.execute(
                "SELECT id, content_hash, request_type FROM approval_requests "
                "WHERE session_id = ? AND correlation_id = ? AND state = 'open' ORDER BY created_at",
                (session_id, correlation_id),
            ).fetchall()
            for existing in existing_rows:
                # A legacy/direct C4 with the same correlation is not this broker's waiter/card. Only
                # reuse a row whose deterministic open event exists on this exact driven run and whose
                # immutable request is byte-for-byte equivalent. A conflicting replay must not capture
                # the original waiter or replace the visible preview.
                active = _active_open_conn(conn, existing[0], agent_run_id)
                if active is not None:
                    same_body = active["body"] == normalized_body
                    if negotiated and isinstance(active["body"], Mapping):
                        # The daemon replaces the caller's expiry with a fresh five-minute clock on
                        # every attempt. It is not part of request identity; the canonical manifest is.
                        same_body = {
                            key: value for key, value in active["body"].items() if key != "expires_at"
                        } == {
                            key: value for key, value in normalized_body.items() if key != "expires_at"
                        }
                    if (
                        active["negotiated"] == negotiated
                        and same_body
                        and existing[2] == request_type
                    ):
                        return {
                            "approval_id": existing[0], "content_hash": existing[1],
                            "deduplicated": True, "conflict": False,
                        }
                    return {
                        "approval_id": existing[0], "content_hash": existing[1],
                        "deduplicated": True, "conflict": True,
                    }
            inserted = insert_approval_conn(
                conn, approval_id=approval_id, session_id=session_id, correlation_id=correlation_id,
                request_type=request_type, state="open", summary=summary, created_at=created,
                is_test=is_test,
            )
            event = _insert_event_conn(
                conn, agent_run_id=agent_run_id, correlation_id=correlation_id, marker="open",
                source_event_id=f"approval:{approval_id}:open:{revision}", summary=summary,
                body=serialized_body, name="diff_snapshot" if negotiated else None,
            )
            return {"approval_id": approval_id, "content_hash": inserted["content_hash"],
                    "event": event, "deduplicated": False, "conflict": False}

        result = db.write_tx(op, session_fence=session_fence)
        if result["conflict"]:
            return {
                "status": "DENIED", "code": DENIED_APPROVAL_CONFLICT, "retryable": False,
                "required_action": "rerun_turn", "id": result["approval_id"], "state": "open",
                "deduplicated": True, "expose": False, "waiter_should_park": False,
                "waiter_released": False,
            }
        return {
            "status": "OK", "id": result["approval_id"], "state": "open",
            "content_hash": result["content_hash"], "deduplicated": result["deduplicated"],
            "expose": True, "waiter_should_park": True, "waiter_released": False,
        }

    def refresh(
        self,
        approval_id: str,
        agent_run_id: str,
        body: Mapping[str, Any],
        *,
        session_fence: Any = None,
    ) -> dict[str, Any]:
        """Publish one immutable negotiated revision while keeping the C4 row and waiter open.

        The original five-minute deadline is retained. A wrong next revision is a non-mutating refresh
        denial; an invalid snapshot body is terminalized through the same sanitized fail-closed fixture
        as initial materialization.
        """

        current = self.get(approval_id)
        if current is None:
            raise KaizenDenied(
                "DENIED_APPROVAL_NOT_FOUND",
                {"id": approval_id, "required_action": "check the approval id with C5"},
                exit_code=1,
            )
        active = current.get("active")
        if current.get("state") != "open" or not current.get("broker_managed") \
                or not isinstance(active, Mapping) or not active.get("negotiated"):
            return {
                "status": "DENIED", "code": "DENIED_APPROVAL_ALREADY_DECIDED", "retryable": False,
                "required_action": "refresh_preview", "current_revision": None,
                "current_snapshot_set_sha256": None, "metadata_confirmation_required": False,
            }
        try:
            normalized = validate_approval_open_body(body)
        except ApprovalValidationError as error:
            return self.deny_invalid(
                session_id=str(current["session_id"]), agent_run_id=agent_run_id,
                correlation_id=str(current["correlation_id"]), denial_code=error.denial_code,
                approval_id=approval_id, session_fence=session_fence,
            )

        def op(conn: Any, _attempt: int) -> dict[str, Any]:
            row = conn.execute(
                "SELECT session_id, correlation_id, state FROM approval_requests WHERE id = ?",
                (approval_id,),
            ).fetchone()
            if row is None:
                raise KaizenDenied(
                    "DENIED_APPROVAL_NOT_FOUND",
                    {"id": approval_id, "required_action": "check the approval id with C5"},
                    exit_code=1,
                )
            session_id, correlation_id, state = row
            _assert_run_link(conn, agent_run_id, session_id)
            live = _active_open_conn(conn, approval_id, agent_run_id)
            if state != "open" or live is None or not live["negotiated"]:
                return {"denial": {
                    "status": "DENIED", "code": "DENIED_APPROVAL_ALREADY_DECIDED", "retryable": False,
                    "required_action": "refresh_preview", "current_revision": None,
                    "current_snapshot_set_sha256": None, "metadata_confirmation_required": False,
                }}
            try:
                live_body = validate_approval_open_body(live["body"])
            except ApprovalValidationError:
                return {"invalid": True, "session_id": session_id, "correlation_id": correlation_id}
            current_revision = int(live_body["approval_revision"])
            requested_revision = int(normalized["approval_revision"])
            if requested_revision == current_revision \
                    and normalized["snapshot_set_sha256"] == live_body["snapshot_set_sha256"]:
                # Crash-after-commit replay: the immutable revision already landed. Return it without
                # appending a third open event or extending the original deadline.
                return {
                    "event": {"deduplicated": True}, "revision": current_revision,
                    "snapshot_set_sha256": live_body["snapshot_set_sha256"],
                    "expires_at": live_body["expires_at"], "deduplicated": True,
                }
            expected = current_revision + 1
            if requested_revision != expected:
                return {"denial": _denial_response(
                    "DENIED_APPROVAL_REVISION_MISMATCH", "refresh_preview",
                    current_revision=current_revision,
                    current_hash=str(live_body["snapshot_set_sha256"]),
                    metadata_required=any(
                        change["preview_mode"] == "metadata" for change in live_body["file_changes"]
                    ),
                )}
            refreshed = dict(normalized)
            refreshed["expires_at"] = live_body["expires_at"]
            serialized = _json_body(refreshed)
            event = _insert_event_conn(
                conn, agent_run_id=agent_run_id, correlation_id=correlation_id, marker="open",
                source_event_id=f"approval:{approval_id}:open:{expected}",
                summary="approval preview refreshed", body=serialized, name="diff_snapshot",
            )
            return {
                "event": event, "revision": expected,
                "snapshot_set_sha256": refreshed["snapshot_set_sha256"],
                "expires_at": refreshed["expires_at"], "deduplicated": event["deduplicated"],
            }

        result = db.write_tx(op, session_fence=session_fence)
        if "denial" in result:
            return result["denial"]
        if result.get("invalid"):
            return self.deny_invalid(
                session_id=result["session_id"], agent_run_id=agent_run_id,
                correlation_id=result["correlation_id"], denial_code=DENIED_SNAPSHOT_INVALID,
                approval_id=approval_id, session_fence=session_fence,
            )
        return {
            "status": "OK", "id": approval_id, "state": "open", "expose": True,
            "waiter_should_park": False, "waiter_released": False,
            "current_revision": result["revision"],
            "current_snapshot_set_sha256": result["snapshot_set_sha256"],
            "expires_at": result["expires_at"], "deduplicated": result["deduplicated"],
        }

    def deny_invalid(
        self,
        *,
        session_id: str,
        agent_run_id: str,
        correlation_id: str,
        denial_code: str = DENIED_BODY_INVALID,
        approval_id: str | None = None,
        request_type: str = "tool_approval",
        is_test: bool = False,
        session_fence: Any = None,
    ) -> dict[str, Any]:
        """Persist the sanitized invalid-body/snapshot terminal fixture, then release denied once."""
        if denial_code not in (DENIED_BODY_INVALID, DENIED_SNAPSHOT_INVALID, DENIED_STALE):
            denial_code = DENIED_BODY_INVALID
        approval_id = approval_id or db.new_id("apr")
        summary, terminal_body = _terminal_fixture(denial_code)
        created = db.now()

        def op(conn: Any, _attempt: int) -> dict[str, Any]:
            _assert_run_link(conn, agent_run_id, session_id)
            row = conn.execute("SELECT state FROM approval_requests WHERE id = ?", (approval_id,)).fetchone()
            if row is None:
                inserted = insert_approval_conn(
                    conn, approval_id=approval_id, session_id=session_id, correlation_id=correlation_id,
                    request_type=request_type, state="denied", decided_by="auto", summary=summary,
                    created_at=created, is_test=is_test,
                )
                winner = True
            elif row[0] == "open":
                inserted = transition_approval_conn(
                    conn, approval_id, "denied", decided_by="auto", summary=summary, open_only=True,
                )
                winner = True
            else:
                return {"winner": False, "state": row[0], "content_hash": None}
            event = _insert_event_conn(
                conn, agent_run_id=agent_run_id, correlation_id=correlation_id, marker="declined",
                source_event_id=f"approval:{approval_id}:terminal", summary=summary,
                body=terminal_body, code=denial_code,
            )
            return {"winner": winner, "state": "denied", "content_hash": inserted["content_hash"],
                    "event": event}

        result = db.write_tx(op, session_fence=session_fence)
        released = False
        if result["winner"]:
            released = self._release(session_id, agent_run_id, correlation_id, "denied")
        return {
            "status": "DENIED", "code": denial_code, "retryable": False,
            "required_action": "rerun_turn", "id": approval_id, "state": result["state"],
            "expose": False, "waiter_should_park": False, "waiter_released": released,
        }

    def check_accept(
        self,
        approval_id: str,
        agent_run_id: str,
        *,
        expected_revision: int | None,
        snapshot_set_sha256: str | None,
        metadata_confirmed: bool,
    ) -> dict[str, Any]:
        """Read and validate the active negotiated Accept without mutating C4 or the event stream."""

        current = self.get(approval_id)
        if current is None:
            raise KaizenDenied(
                "DENIED_APPROVAL_NOT_FOUND",
                {"id": approval_id, "required_action": "check the approval id with C5"},
                exit_code=1,
            )
        active = current.get("active")
        if current.get("state") != "open":
            return {
                "status": "DENIED", "code": "DENIED_APPROVAL_ALREADY_DECIDED", "retryable": False,
                "required_action": "refresh_preview", "current_revision": None,
                "current_snapshot_set_sha256": None, "metadata_confirmation_required": False,
            }
        if not current.get("broker_managed") or not isinstance(active, Mapping) \
                or active.get("agent_run_id") != agent_run_id:
            raise KaizenDenied(
                DENIED_BODY_INVALID,
                {"required_action": "only the active broker-managed approval can be accepted"},
                exit_code=2,
            )
        if not active.get("negotiated"):
            return {"status": "OK", "negotiated": False, "record": current, "approval": None}
        try:
            structured = validate_approval_open_body(active.get("body"))
        except ApprovalValidationError as error:
            return {"status": "DENIED", "code": error.denial_code, "retryable": False,
                    "required_action": "rerun_turn", "current_revision": None,
                    "current_snapshot_set_sha256": None, "metadata_confirmation_required": False}
        revision = structured["approval_revision"]
        current_hash = structured["snapshot_set_sha256"]
        metadata_required = any(change["preview_mode"] == "metadata" for change in structured["file_changes"])
        if expected_revision is None or isinstance(expected_revision, bool) \
                or not isinstance(expected_revision, int) or not isinstance(snapshot_set_sha256, str):
            return _denial_response(
                "DENIED_APPROVAL_CONFIRMATION_REQUIRED", "refresh_preview",
                current_revision=revision, current_hash=current_hash,
                metadata_required=metadata_required,
            )
        if expected_revision != revision or snapshot_set_sha256 != current_hash:
            return _denial_response(
                "DENIED_APPROVAL_REVISION_MISMATCH", "refresh_preview",
                current_revision=revision, current_hash=current_hash,
                metadata_required=metadata_required,
            )
        if metadata_required and metadata_confirmed is not True:
            return _denial_response(
                "DENIED_APPROVAL_CONFIRMATION_REQUIRED", "confirm_metadata",
                current_revision=revision, current_hash=current_hash, metadata_required=True,
            )
        return {"status": "OK", "negotiated": True, "record": current, "approval": structured}

    def resolve(
        self,
        approval_id: str,
        agent_run_id: str,
        decision: str,
        *,
        expected_revision: int | None = None,
        snapshot_set_sha256: str | None = None,
        metadata_confirmed: bool = False,
        decided_by: str = "human",
        denial_code: str | None = None,
        release_value: Any = None,
        session_fence: Any = None,
    ) -> dict[str, Any]:
        """Resolve one broker-managed open request; one guarded C4 update wins the terminal event."""
        normalized_decision = str(decision).strip().lower()
        if normalized_decision not in ("approve", "deny"):
            raise KaizenDenied(
                "DENIED_APPROVAL_DECISION_INVALID",
                {"decision": decision, "allowed": ["approve", "deny"],
                 "required_action": "decision must be approve|deny"},
                exit_code=2,
            )
        target_state = "approved" if normalized_decision == "approve" else "denied"
        marker = "resolved" if target_state == "approved" else "declined"
        event_summary = "approval accepted" if target_state == "approved" else "approval declined"
        if target_state == "approved" and denial_code is not None:
            raise KaizenDenied(
                "DENIED_APPROVAL_DECISION_INVALID",
                {"required_action": "omit denial_code when approving"},
                exit_code=2,
            )
        if denial_code is None:
            event_body = _json_body({"decision": target_state})
        else:
            event_summary, event_body = _terminal_fixture(denial_code)

        # Compatibility validator execution is deliberately outside write_tx. D3 production performs
        # filesystem preparation in the supervisor and leaves this callback unset.
        if normalized_decision == "approve" and self._pre_release_validator is not None:
            checked = self.check_accept(
                approval_id, agent_run_id, expected_revision=expected_revision,
                snapshot_set_sha256=snapshot_set_sha256, metadata_confirmed=metadata_confirmed,
            )
            if checked.get("status") != "OK":
                if checked.get("code") in (DENIED_BODY_INVALID, DENIED_SNAPSHOT_INVALID):
                    record = checked.get("record") or self.get(approval_id) or {}
                    return self.deny_invalid(
                        session_id=str(record.get("session_id") or ""), agent_run_id=agent_run_id,
                        correlation_id=str(record.get("correlation_id") or ""),
                        denial_code=str(checked.get("code")), approval_id=approval_id,
                        session_fence=session_fence,
                    )
                return checked
            if checked.get("negotiated"):
                try:
                    pre_release = self._pre_release_validator({
                        "approval_id": approval_id,
                        "session_id": checked["record"]["session_id"],
                        "agent_run_id": agent_run_id,
                        "correlation_id": checked["record"]["correlation_id"],
                        "approval": checked["approval"],
                    })
                except Exception:  # noqa: BLE001 -- compatibility validator remains fail-closed
                    pre_release = {"status": "DENIED"}
                if pre_release is not None:
                    record = checked["record"]
                    return self.deny_invalid(
                        session_id=str(record["session_id"]), agent_run_id=agent_run_id,
                        correlation_id=str(record["correlation_id"]),
                        denial_code=DENIED_SNAPSHOT_INVALID, approval_id=approval_id,
                        session_fence=session_fence,
                    )

        def op(conn: Any, _attempt: int) -> dict[str, Any]:
            row = conn.execute(
                "SELECT session_id, correlation_id, state FROM approval_requests WHERE id = ?",
                (approval_id,),
            ).fetchone()
            if row is None:
                raise KaizenDenied(
                    "DENIED_APPROVAL_NOT_FOUND",
                    {"id": approval_id, "required_action": "check the approval id with C5"},
                    exit_code=1,
                )
            session_id, correlation_id, current_state = row
            _assert_run_link(conn, agent_run_id, session_id)
            active = _active_open_conn(conn, approval_id, agent_run_id)
            if active is None:
                raise KaizenDenied(
                    DENIED_BODY_INVALID,
                    {"required_action": "only a broker-managed driven approval can be resolved here"},
                    exit_code=2,
                )
            if current_state != "open":
                return {"denial": {
                    "status": "DENIED", "code": "DENIED_APPROVAL_ALREADY_DECIDED", "retryable": False,
                    "required_action": "refresh_preview", "current_revision": None,
                    "current_snapshot_set_sha256": None, "metadata_confirmation_required": False,
                }}
            structured: dict[str, Any] | None = None
            if active["negotiated"]:
                try:
                    structured = validate_approval_open_body(active["body"])
                except ApprovalValidationError as error:
                    invalid_summary, invalid_body = _terminal_fixture(error.denial_code)
                    transitioned = transition_approval_conn(
                        conn, approval_id, "denied", decided_by="auto", summary=invalid_summary,
                        open_only=True,
                    )
                    _insert_event_conn(
                        conn, agent_run_id=agent_run_id, correlation_id=correlation_id, marker="declined",
                        source_event_id=f"approval:{approval_id}:terminal", summary=invalid_summary,
                        body=invalid_body, code=error.denial_code,
                    )
                    return {"winner": True, "session_id": session_id, "correlation_id": correlation_id,
                            "state": "denied", "content_hash": transitioned["content_hash"],
                            "decision": "denied", "denial_code": error.denial_code}
                current_revision = structured["approval_revision"]
                current_hash = structured["snapshot_set_sha256"]
                metadata_required = any(
                    change["preview_mode"] == "metadata" for change in structured["file_changes"]
                )
                if normalized_decision == "approve":
                    if (
                        expected_revision is None
                        or isinstance(expected_revision, bool)
                        or not isinstance(expected_revision, int)
                        or snapshot_set_sha256 is None
                        or not isinstance(snapshot_set_sha256, str)
                    ):
                        return {"denial": _denial_response(
                            "DENIED_APPROVAL_CONFIRMATION_REQUIRED", "refresh_preview",
                            current_revision=current_revision, current_hash=current_hash,
                            metadata_required=metadata_required,
                        )}
                    if expected_revision != current_revision or snapshot_set_sha256 != current_hash:
                        return {"denial": _denial_response(
                            "DENIED_APPROVAL_REVISION_MISMATCH", "refresh_preview",
                            current_revision=current_revision, current_hash=current_hash,
                            metadata_required=metadata_required,
                        )}
                    if metadata_required and metadata_confirmed is not True:
                        return {"denial": _denial_response(
                            "DENIED_APPROVAL_CONFIRMATION_REQUIRED", "confirm_metadata",
                            current_revision=current_revision, current_hash=current_hash,
                            metadata_required=True,
                        )}
            transitioned = transition_approval_conn(
                conn, approval_id, target_state, decided_by=decided_by, open_only=True,
            )
            _insert_event_conn(
                conn, agent_run_id=agent_run_id, correlation_id=correlation_id, marker=marker,
                source_event_id=f"approval:{approval_id}:terminal", summary=event_summary,
                body=event_body, code=denial_code,
            )
            return {"winner": True, "session_id": session_id, "correlation_id": correlation_id,
                    "state": target_state, "content_hash": transitioned["content_hash"],
                    "decision": target_state, "denial_code": denial_code}

        result = db.write_tx(op, session_fence=session_fence)
        if "denial" in result:
            return result["denial"]
        waiter_value = release_value if result["decision"] == "approved" and release_value is not None \
            else result["decision"]
        released = self._release(result["session_id"], agent_run_id, result["correlation_id"], waiter_value)
        if result.get("denial_code"):
            return {
                "status": "DENIED", "code": result["denial_code"], "retryable": False,
                "required_action": "rerun_turn", "id": approval_id, "state": result["state"],
                "waiter_released": released,
            }
        return {"status": "OK", "id": approval_id, "state": result["state"],
                "waiter_released": released, "content_hash": result["content_hash"]}

    def timeout(
        self,
        approval_id: str,
        agent_run_id: str,
        *,
        session_fence: Any = None,
    ) -> dict[str, Any]:
        """Persist the five-minute fail-closed timeout before releasing the waiter."""
        summary, body = _terminal_fixture(DENIED_TIMEOUT)

        def op(conn: Any, _attempt: int) -> dict[str, Any]:
            row = conn.execute(
                "SELECT session_id, correlation_id, state FROM approval_requests WHERE id = ?",
                (approval_id,),
            ).fetchone()
            if row is None:
                raise KaizenDenied(
                    "DENIED_APPROVAL_NOT_FOUND",
                    {"id": approval_id, "required_action": "check the approval id with C5"},
                    exit_code=1,
                )
            session_id, correlation_id, current_state = row
            _assert_run_link(conn, agent_run_id, session_id)
            if _active_open_conn(conn, approval_id, agent_run_id) is None:
                raise KaizenDenied(
                    DENIED_BODY_INVALID,
                    {"required_action": "only a broker-managed driven approval can time out here"},
                    exit_code=2,
                )
            if current_state != "open":
                return {"denial": {
                    "status": "DENIED", "code": "DENIED_APPROVAL_ALREADY_DECIDED", "retryable": False,
                    "required_action": "rerun_turn", "current_revision": None,
                    "current_snapshot_set_sha256": None, "metadata_confirmation_required": False,
                }}
            transitioned = transition_approval_conn(
                conn, approval_id, "denied", decided_by="auto", summary=summary, open_only=True,
            )
            _insert_event_conn(
                conn, agent_run_id=agent_run_id, correlation_id=correlation_id, marker="timed_out",
                source_event_id=f"approval:{approval_id}:terminal", summary=summary, body=body,
                code=DENIED_TIMEOUT,
            )
            return {"session_id": session_id, "correlation_id": correlation_id,
                    "content_hash": transitioned["content_hash"]}

        result = db.write_tx(op, session_fence=session_fence)
        if "denial" in result:
            return result["denial"]
        released = self._release(result["session_id"], agent_run_id, result["correlation_id"], "denied")
        return {"status": "DENIED", "code": DENIED_TIMEOUT, "retryable": False,
                "required_action": "rerun_turn", "id": approval_id, "state": "denied",
                "waiter_released": released, "content_hash": result["content_hash"]}

    def reconcile_orphans(
        self,
        live_approval_ids: Collection[str],
        *,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        """Close broker-managed open C4 rows that have no live waiter; ignore every legacy C4 row."""
        live = set(live_approval_ids)
        rows = db.fetch_all(
            "SELECT a.id, a.session_id, a.correlation_id, a.created_at, e.agent_run_id, "
            "e.source_event_id, e.body, e.name, e.sequence_no "
            "FROM approval_requests a "
            "JOIN agent_runs r ON r.session_id = a.session_id "
            "JOIN agent_events e ON e.agent_run_id = r.id AND e.correlation_id = a.correlation_id "
            "WHERE a.state = 'open' AND e.source_event_id LIKE ('approval:' || a.id || ':open:%') "
            "ORDER BY a.id, e.sequence_no DESC"
        )
        now_at = at or datetime.now(timezone.utc)
        latest: dict[str, tuple[Any, ...]] = {}
        for row in rows:
            match = _OPEN_SOURCE_RE.fullmatch(row[5] or "")
            if match is not None and match.group("approval_id") == row[0] and row[0] not in latest:
                latest[row[0]] = row
        outcomes: list[dict[str, Any]] = []
        for approval_id, row in latest.items():
            if approval_id in live:
                outcomes.append({"id": approval_id, "action": "live"})
                continue
            expires_at: datetime | None = None
            if row[7] == "diff_snapshot":
                parsed = _load_body(row[6])
                try:
                    expires_at = _utc_z(parsed.get("expires_at")) if parsed is not None else None
                except ApprovalValidationError:
                    expires_at = None
            if expires_at is None:
                try:
                    created = datetime.fromisoformat(str(row[3]).replace("Z", "+00:00"))
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    expires_at = created + timedelta(seconds=DEFAULT_TIMEOUT_SECONDS)
                except ValueError:
                    expires_at = now_at
            if expires_at <= now_at:
                result = self.timeout(approval_id, row[4])
                outcomes.append({"id": approval_id, "action": "timed_out", "result": result})
            else:
                result = self.resolve(
                    approval_id, row[4], "deny", decided_by="auto", denial_code=DENIED_STALE,
                )
                outcomes.append({"id": approval_id, "action": "stale", "result": result})
        return {"status": "OK", "managed": len(latest), "outcomes": outcomes}


__all__ = [
    "ApprovalBroker", "ApprovalValidationError", "DEFAULT_TIMEOUT_SECONDS", "DENIED_APPROVAL_CONFLICT",
    "canonical_snapshot_set_sha256", "validate_approval_open_body",
]
