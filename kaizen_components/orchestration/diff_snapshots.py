"""Immutable approval-time file snapshots and exact pre/post-apply verification.

Raw bytes stay in the workspace-local content-addressed cache. Durable approval events receive only
the frozen metadata shapes validated by :mod:`approvals`; no unified diff or file content is emitted.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import os
import stat
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .artifact_cache import ArtifactCache, ArtifactCacheError
from .approvals import (
    DEFAULT_TIMEOUT_SECONDS,
    DENIED_SNAPSHOT_INVALID,
    DENIED_STALE,
    MAX_FILE_CHANGES,
    MAX_SNAPSHOT_SET_BYTES,
    MAX_TEXT_SIDE_BYTES,
    canonical_snapshot_set_sha256,
    validate_approval_open_body,
)
from .workspace_path_authority import WorkspacePathAuthority, WorkspacePathError

DENIED_PARTIAL_APPLY = DENIED_STALE  # shared rerun code; the audit body carries the exact extent
_CACHE_RELATIVE = Path("AI") / "work" / "orchestration" / "ui-cache" / "diffs" / "sha256"
_APPLY_EXTENTS = frozenset(("none", "complete", "partial", "uncertain"))
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class DiffSnapshotError(ValueError):
    """Snapshot construction or verification failed with a locked approval denial code.

    ``DENIED_APPROVAL_SNAPSHOT_INVALID`` and ``DENIED_APPROVAL_STALE`` are retained; every other supplied
    code is coerced to ``DENIED_APPROVAL_SNAPSHOT_INVALID`` so callers cannot mint new denial vocabulary.
    """

    def __init__(self, code: str, reason: str = "") -> None:
        super().__init__(reason or code)
        self.code = code if code in (DENIED_SNAPSHOT_INVALID, DENIED_STALE) else DENIED_SNAPSHOT_INVALID


@dataclass(frozen=True)
class PreparedDiff:
    body: dict[str, Any]
    engine: str
    updated_input: dict[str, Any] | None
    scope_id: str = "unscoped"


@dataclass
class _PendingDiff:
    current: PreparedDiff
    agent_run_id: str
    artifact_refs: set[str]


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _expires() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=DEFAULT_TIMEOUT_SECONDS)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


class DiffSnapshotManager:
    """One daemon's cache/materialization and in-memory live-approval state."""

    def __init__(
        self,
        workspace_root: str | Path,
        artifact_cache: ArtifactCache | None = None,
        workspace_path_authority: WorkspacePathAuthority | None = None,
    ) -> None:
        self.workspace_root = Path(os.path.abspath(os.fspath(workspace_root)))
        if workspace_path_authority is not None \
                and os.path.normcase(str(workspace_path_authority.workspace_root)) \
                != os.path.normcase(str(self.workspace_root)):
            raise ValueError("workspace path authority root differs from snapshot workspace")
        self._workspace_path_authority = workspace_path_authority
        self._artifact_cache = artifact_cache or ArtifactCache(self.workspace_root)
        self.cache_root = self._artifact_cache.kind_root("diffs", create=False)
        self._pending: dict[str, _PendingDiff] = {}
        self._staged: dict[tuple[str, str, str], PreparedDiff] = {}
        self._lock = threading.RLock()

    # ---- materialization ------------------------------------------------------------------

    def prepare_request(
        self,
        engine: str,
        request: Mapping[str, Any],
        *,
        revision: int = 1,
        scope_id: str = "unscoped",
    ) -> PreparedDiff:
        """Normalize a private adapter request; raw input never becomes an event body."""

        tool_name = str(request.get("tool_name") or "")
        tool_input = request.get("tool_input")
        if not isinstance(tool_input, Mapping):
            raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "missing tool input")
        original = dict(tool_input)
        if tool_name.casefold() in ("write_file", "write"):
            raw_path = tool_input.get("path", tool_input.get("file_path"))
            if not isinstance(raw_path, str):
                raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "write path missing")
            content = tool_input.get("content")
            if not isinstance(content, (str, bytes)):
                raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "write content missing")
            path, target = self._workspace_path(raw_path)
            kind = "modify" if target.is_file() else "create"
            if target.exists() and not target.is_file():
                raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "write target is not a file")
            return self.prepare_changes(
                engine,
                [{"path": path, "kind": kind, "proposed_content": content}],
                revision=revision,
                updated_input=original,
                scope_id=scope_id,
            )
        changes = request.get("changes")
        if isinstance(changes, Sequence) and not isinstance(changes, (str, bytes, bytearray)):
            updated = request.get("updated_input")
            return self.prepare_changes(
                engine,
                list(changes),
                revision=revision,
                updated_input=dict(updated) if isinstance(updated, Mapping) else original,
                scope_id=scope_id,
            )
        raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "unsupported file-change request")

    def prepare_changes(
        self,
        engine: str,
        changes: Sequence[Mapping[str, Any]],
        *,
        revision: int = 1,
        updated_input: Mapping[str, Any] | None = None,
        scope_id: str = "unscoped",
    ) -> PreparedDiff:
        """Validate, bounded-read, and materialize a metadata-only prepared diff."""
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "invalid revision")
        if not (1 <= len(changes) <= MAX_FILE_CHANGES):
            raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "invalid file count")

        material: list[dict[str, Any]] = []
        total_bytes = 0
        seen_paths: set[str] = set()
        for index, raw in enumerate(changes):
            if not isinstance(raw, Mapping):
                raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "change is not an object")
            path, target = self._workspace_path(raw.get("path"))
            kind = raw.get("kind")
            if kind not in ("create", "modify", "delete", "rename") or path in seen_paths:
                raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "invalid or duplicate change")
            seen_paths.add(path)
            old_path: str | None = None
            source = target
            if kind == "rename":
                old_path, source = self._workspace_path(raw.get("old_path"))
                if old_path == path or old_path in seen_paths:
                    raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "invalid rename")
                seen_paths.add(old_path)

            exists = source.exists()
            if kind == "create":
                if target.exists():
                    raise DiffSnapshotError(DENIED_STALE, "create collision")
                before = None
            else:
                if not exists or not source.is_file():
                    raise DiffSnapshotError(DENIED_STALE, "base file missing")
                before = self._read_file(source)
            if kind == "rename" and target.exists():
                raise DiffSnapshotError(DENIED_STALE, "rename collision")

            if kind == "delete":
                proposed = None
            elif "proposed_content" in raw:
                proposed = self._content_bytes(raw.get("proposed_content"))
            elif kind == "rename" and before is not None:
                proposed = before
            else:
                raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "proposed content missing")
            total_bytes += sum(len(value) for value in (before, proposed) if value is not None)
            if total_bytes > MAX_SNAPSHOT_SET_BYTES:
                raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "snapshot aggregate exceeds 64 MiB")
            material.append({
                "index": index, "path": path, "kind": kind, "old_path": old_path,
                "before_bytes": before, "proposed_bytes": proposed,
            })

        file_changes: list[dict[str, Any]] = []
        for entry in material:
            before_side, before_reason = self._side(entry["before_bytes"], scope_id)
            proposed_side, proposed_reason = self._side(entry["proposed_bytes"], scope_id)
            reason = self._preview_reason(before_reason, proposed_reason)
            signature = json.dumps(
                [entry["path"], entry["old_path"], entry["kind"], entry["index"]],
                ensure_ascii=True, separators=(",", ":"),
            ).encode("utf-8")
            file_changes.append({
                "change_id": "chg-" + hashlib.sha256(signature).hexdigest()[:20],
                "path": entry["path"], "kind": entry["kind"], "old_path": entry["old_path"],
                "preview_mode": "metadata" if reason else "text", "preview_reason": reason,
                "before": before_side, "proposed": proposed_side,
            })
        file_changes.sort(key=lambda item: (item["path"], item["old_path"] or "", item["change_id"]))
        body = {
            "approval_revision": revision,
            # Expiry is advisory and intentionally excluded from the canonical snapshot-set hash.
            "expires_at": _expires(),
            "snapshot_set_sha256": "0" * 64,
            "file_changes": file_changes,
        }
        body["snapshot_set_sha256"] = canonical_snapshot_set_sha256(body)
        normalized = validate_approval_open_body(body)
        return PreparedDiff(
            body=normalized,
            engine=str(engine),
            updated_input=copy.deepcopy(dict(updated_input)) if updated_input is not None else None,
            scope_id=str(scope_id or "unscoped"),
        )

    def _workspace_path(self, value: Any) -> tuple[str, Path]:
        """Return a contained POSIX-relative path and resolved absolute path."""
        if not isinstance(value, str) or not value or "\x00" in value:
            raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "invalid path")
        raw = Path(value)
        candidate = raw if raw.is_absolute() else self.workspace_root / raw
        try:
            resolved = candidate.resolve(strict=False)
            relative = resolved.relative_to(self.workspace_root).as_posix()
        except (OSError, ValueError):
            raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "path escapes workspace") from None
        if not relative or relative == "." or any(part in ("", ".", "..") for part in relative.split("/")):
            raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "invalid workspace path")
        return relative, resolved

    @staticmethod
    def _content_bytes(value: Any) -> bytes:
        if isinstance(value, str):
            return value.encode("utf-8")
        if isinstance(value, bytes):
            return value
        raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "content must be text or bytes")

    def _read_file(self, path: Path, *, error_code: str = DENIED_SNAPSHOT_INVALID) -> bytes:
        """Read one bounded regular workspace file through handle-based path authority."""

        authority: WorkspacePathAuthority | None = None
        try:
            authority = WorkspacePathAuthority(self.workspace_root)
            relative = path.relative_to(self.workspace_root).as_posix()
            return authority.read(relative, MAX_SNAPSHOT_SET_BYTES).data
        except (OSError, ValueError, WorkspacePathError):
            raise DiffSnapshotError(error_code, "bounded workspace read failed") from None
        finally:
            if authority is not None:
                authority.close()

    @staticmethod
    def _classify(content: bytes) -> tuple[str | None, str | None, str | None]:
        if len(content) > MAX_TEXT_SIDE_BYTES:
            try:
                content.decode("utf-8")
            except UnicodeDecodeError:
                return None, "application/octet-stream", "oversize"
            return "utf-8", None, "oversize"
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            return None, "application/octet-stream", "unsupported_encoding"
        if b"\x00" in content:
            return None, "application/octet-stream", "binary"
        return "utf-8", None, None

    @staticmethod
    def _preview_reason(*reasons: str | None) -> str | None:
        for candidate in ("oversize", "binary", "unsupported_encoding"):
            if candidate in reasons:
                return candidate
        return None

    def _side(self, content: bytes | None, scope_id: str) -> tuple[dict[str, Any] | None, str | None]:
        if content is None:
            return None, None
        digest = _sha256(content)
        encoding, media_type, reason = self._classify(content)
        self._store_artifact(digest, content, scope_id)
        return {
            "artifact_ref": f"sha256:{digest}", "sha256": digest, "bytes": len(content),
            "encoding": encoding, "media_type": media_type,
        }, reason

    def _store_artifact(self, digest: str, content: bytes, scope_id: str) -> None:
        try:
            self._artifact_cache.store(
                "diffs", content, scope_id=scope_id,
                expected_sha256=digest, expected_bytes=len(content), origin="approval_diff",
            )
            self.cache_root = self._artifact_cache.kind_root("diffs", create=False)
        except ArtifactCacheError:
            raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "snapshot materialization failed") from None

    @staticmethod
    def _is_reparse(path: Path) -> bool:
        try:
            if path.is_symlink():
                return True
            is_junction = getattr(path, "is_junction", None)
            if callable(is_junction) and is_junction():
                return True
            attrs = getattr(os.lstat(path), "st_file_attributes", 0)
            return bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)
        except OSError:
            return False

    def _assert_cache_components(self, *, create: bool) -> None:
        current = self.workspace_root
        if self._is_reparse(current):
            raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "workspace cache root is a reparse point")
        for part in _CACHE_RELATIVE.parts:
            current = current / part
            if current.exists():
                if not current.is_dir() or self._is_reparse(current):
                    raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "diff cache contains a reparse point")
            elif create:
                try:
                    current.mkdir()
                except OSError:
                    raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "diff cache creation failed") from None
                if self._is_reparse(current):
                    raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "diff cache creation was redirected")
            else:
                break
        if self.cache_root.exists():
            try:
                resolved = self.cache_root.resolve(strict=True)
                resolved.relative_to(self.workspace_root)
            except (OSError, ValueError):
                raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "diff cache escapes workspace") from None

    def _assert_artifact_path(self, path: Path, digest: str, *, partial: bool = False) -> None:
        if path.parent != self.cache_root:
            raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "artifact path escapes cache")
        valid_name = path.name.startswith(digest + ".") and path.name.endswith(".part") if partial \
            else path.name == digest and len(digest) == 64 and all(char in "0123456789abcdef" for char in digest)
        if not valid_name or (path.exists() and self._is_reparse(path)):
            raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "artifact path is unsafe")
        self._assert_cache_components(create=False)

    def _verify_artifact_path(self, path: Path, digest: str, expected_bytes: int) -> bytes:
        self._assert_artifact_path(path, digest)
        try:
            content = path.read_bytes()
        except OSError:
            raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "snapshot artifact missing") from None
        if len(content) != expected_bytes or _sha256(content) != digest:
            raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "snapshot artifact corrupt")
        return content

    def read_artifact(self, side: Mapping[str, Any]) -> bytes:
        digest = side.get("sha256")
        size = side.get("bytes")
        reference = side.get("artifact_ref")
        if not isinstance(digest, str) or reference != f"sha256:{digest}" \
                or isinstance(size, bool) or not isinstance(size, int):
            raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "invalid artifact metadata")
        try:
            return self._artifact_cache.read(
                "diffs", str(reference), expected_sha256=digest, expected_bytes=size,
            )
        except ArtifactCacheError:
            raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "snapshot artifact invalid") from None

    # ---- live approval state ---------------------------------------------------------------

    def register(self, approval_id: str, prepared: PreparedDiff, *, agent_run_id: str = "") -> None:
        """Marks diff artifacts open (retention) then records live pending under `approval_id`; `setdefault` so a re-register is a no-op."""
        refs = self._body_artifact_refs(prepared.body)
        try:
            self._artifact_cache.mark_diff_open(refs, approval_id)
        except ArtifactCacheError:
            raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "snapshot retention metadata invalid") from None
        with self._lock:
            self._pending.setdefault(approval_id, _PendingDiff(prepared, agent_run_id, set(refs)))

    def stage(self, agent_run_id: str, correlation_id: str, prepared: PreparedDiff) -> None:
        """Stage a prepared snapshot set under its run, correlation, and digest key."""
        with self._lock:
            self._staged[(agent_run_id, correlation_id, prepared.body["snapshot_set_sha256"])] = prepared

    def unstage(self, agent_run_id: str, correlation_id: str, snapshot_set_sha256: str) -> None:
        """Remove a staged-adoption key after release or cancellation."""
        with self._lock:
            self._staged.pop((agent_run_id, correlation_id, snapshot_set_sha256), None)

    def discard(self, approval_id: str) -> None:
        with self._lock:
            pending = self._pending.pop(approval_id, None)
        if pending is not None:
            try:
                self._artifact_cache.mark_diff_terminal(
                    pending.artifact_refs, approval_id,
                )
            except ArtifactCacheError:
                pass

    def discard_run(self, agent_run_id: str) -> None:
        """Forget transient approval state when its driven run ends or a turn exits abnormally."""

        with self._lock:
            approval_ids = [
                approval_id for approval_id, pending in self._pending.items()
                if pending.agent_run_id == agent_run_id
            ]
            self._staged = {
                key: prepared for key, prepared in self._staged.items() if key[0] != agent_run_id
            }
        for approval_id in approval_ids:
            self.discard(approval_id)

    def commit_refresh(self, approval_id: str, prepared: PreparedDiff) -> None:
        """Marks new refs open, replaces `pending.current` with the rebased prepared, unions artifact_refs; raises `DENIED_SNAPSHOT_INVALID` if the live approval is gone."""
        refs = self._body_artifact_refs(prepared.body)
        try:
            self._artifact_cache.mark_diff_open(refs, approval_id)
        except ArtifactCacheError:
            raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "snapshot retention metadata invalid") from None
        with self._lock:
            pending = self._pending.get(approval_id)
            if pending is None:
                raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "live approval missing")
            pending.current = prepared
            pending.artifact_refs.update(refs)

    @staticmethod
    def _body_artifact_refs(body: Mapping[str, Any]) -> list[str]:
        return list(dict.fromkeys(
            str(side["artifact_ref"])
            for change in body.get("file_changes", [])
            for side in (change.get("before"), change.get("proposed"))
            if isinstance(side, Mapping) and isinstance(side.get("artifact_ref"), str)
        ))

    def _ordered_file_changes(self, prepared: PreparedDiff) -> list[Mapping[str, Any]]:
        """Return snapshot changes in the exact approved tool-input order."""

        file_changes = prepared.body.get("file_changes")
        if not isinstance(file_changes, list) or not file_changes:
            raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "approved file changes missing")
        by_path = {
            str(change.get("path")): change
            for change in file_changes
            if isinstance(change, Mapping) and isinstance(change.get("path"), str)
        }
        if len(by_path) != len(file_changes):
            raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "approved file changes malformed")
        updated = prepared.updated_input
        nested = updated.get("changes") if isinstance(updated, Mapping) else None
        if not isinstance(nested, list):
            return list(file_changes)
        if len(nested) != len(file_changes):
            raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "approved input count changed")
        ordered: list[Mapping[str, Any]] = []
        seen: set[str] = set()
        for raw in nested:
            if not isinstance(raw, Mapping):
                raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "approved input change malformed")
            path, _target = self._workspace_path(raw.get("path"))
            change = by_path.get(path)
            if change is None or path in seen or raw.get("kind") != change.get("kind"):
                raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "approved input structure changed")
            if change.get("kind") == "rename":
                old_path, _source = self._workspace_path(raw.get("old_path"))
                if old_path != change.get("old_path"):
                    raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "approved rename changed")
            seen.add(path)
            ordered.append(change)
        if seen != set(by_path):
            raise DiffSnapshotError(DENIED_SNAPSHOT_INVALID, "approved input paths changed")
        return ordered

    @staticmethod
    def _approved_base(change: Mapping[str, Any]) -> dict[str, Any]:
        """Build the content-free executor contract bound to one approved snapshot revision."""

        kind = str(change.get("kind") or "")
        before = change.get("before")
        proposed = change.get("proposed")
        before_side = before if isinstance(before, Mapping) else None
        proposed_side = proposed if isinstance(proposed, Mapping) else None
        value: dict[str, Any] = {
            "kind": kind,
            "path": str(change.get("path") or ""),
            "target_exists": kind not in ("create", "rename"),
            "target_sha256": before_side.get("sha256") if kind not in ("create", "rename")
            and before_side is not None else None,
            "target_bytes": before_side.get("bytes") if kind not in ("create", "rename")
            and before_side is not None else None,
            "final_sha256": proposed_side.get("sha256") if proposed_side is not None else None,
        }
        if kind == "rename":
            value.update({
                "old_path": str(change.get("old_path") or ""),
                "source_exists": True,
                "source_sha256": before_side.get("sha256") if before_side is not None else None,
                "source_bytes": before_side.get("bytes") if before_side is not None else None,
            })
        return value

    def _approved_bases(self, prepared: PreparedDiff) -> tuple[Mapping[str, Any], ...]:
        return tuple(self._approved_base(change) for change in self._ordered_file_changes(prepared))

    def validate_release(self, context: Mapping[str, Any]) -> dict[str, Any]:
        """Validate a release, adopting staged retention outside the in-memory state lock."""
        approval_id = str(context.get("approval_id") or "")
        approval = context.get("approval")
        staged_key: tuple[str, str, str] | None = None
        staged: PreparedDiff | None = None
        with self._lock:
            pending = self._pending.get(approval_id)
            prepared = pending.current if pending is not None else None
            if prepared is None and isinstance(approval, Mapping):
                staged_key = (
                    str(context.get("agent_run_id") or ""),
                    str(context.get("correlation_id") or ""),
                    str(approval.get("snapshot_set_sha256") or ""),
                )
                staged = self._staged.get(staged_key)
        if prepared is None and staged is not None and staged_key is not None:
            refs = set(self._body_artifact_refs(staged.body))
            try:
                self._artifact_cache.mark_diff_open(refs, approval_id)
            except ArtifactCacheError:
                staged = None
            if staged is not None:
                with self._lock:
                    pending = self._pending.get(approval_id)
                    if pending is not None:
                        prepared = pending.current
                    elif self._staged.get(staged_key) is staged:
                        self._pending[approval_id] = _PendingDiff(
                            staged, str(context.get("agent_run_id") or ""), refs,
                        )
                        prepared = staged
                if prepared is None:
                    try:
                        self._artifact_cache.mark_diff_terminal(refs, approval_id)
                    except ArtifactCacheError:
                        pass
        if prepared is None or not isinstance(approval, Mapping):
            return {"status": "DENIED", "code": DENIED_SNAPSHOT_INVALID}
        if approval.get("approval_revision") != prepared.body.get("approval_revision") \
                or approval.get("snapshot_set_sha256") != prepared.body.get("snapshot_set_sha256"):
            return {"status": "DENIED", "code": DENIED_SNAPSHOT_INVALID}
        try:
            stale = self._stale_changes(prepared.body)
        except DiffSnapshotError as error:
            return {"status": "DENIED", "code": error.code}
        if not stale:
            try:
                approved_bases = self._approved_bases(prepared)
            except DiffSnapshotError as error:
                return {"status": "DENIED", "code": error.code}
            return {
                "status": "OK", "updated_input": copy.deepcopy(prepared.updated_input),
                "approval_revision": prepared.body["approval_revision"],
                "snapshot_set_sha256": prepared.body["snapshot_set_sha256"],
                "approved_bases": approved_bases,
                "post_apply": lambda aid=approval_id: self.audit(aid),
            }
        if prepared.engine not in ("local_llm", "claude"):
            return {"status": "DENIED", "code": DENIED_STALE}
        if any(
            change["preview_mode"] != "text" or change["kind"] != "modify"
            for change in prepared.body["file_changes"]
        ):
            return {"status": "DENIED", "code": DENIED_STALE}
        try:
            refreshed = self._rebase(prepared, stale)
        except DiffSnapshotError:
            return {"status": "DENIED", "code": DENIED_STALE}
        return {
            "status": "REFRESH", "body": refreshed.body, "prepared": refreshed,
            "commit": lambda aid=approval_id, value=refreshed: self.commit_refresh(aid, value),
        }

    def _stale_changes(self, body: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Verify cached sides and return changes whose bounded authoritative base read drifted."""
        stale: list[dict[str, Any]] = []
        for raw in body.get("file_changes", []):
            change = dict(raw)
            before = change.get("before")
            proposed = change.get("proposed")
            if isinstance(before, Mapping):
                self.read_artifact(before)
            if isinstance(proposed, Mapping):
                self.read_artifact(proposed)
            _relative, target = self._workspace_path(change["path"])
            if change["kind"] == "create":
                if target.exists():
                    stale.append(change)
                continue
            base_path = change["old_path"] if change["kind"] == "rename" else change["path"]
            _base_relative, base = self._workspace_path(base_path)
            if not base.is_file():
                stale.append(change)
                continue
            actual_hash = _sha256(self._read_file(base, error_code=DENIED_STALE))
            if not isinstance(before, Mapping) or actual_hash != before.get("sha256"):
                stale.append(change)
            if change["kind"] == "rename" and target.exists():
                stale.append(change)
        return stale

    def _rebase(self, prepared: PreparedDiff, stale: Sequence[Mapping[str, Any]]) -> PreparedDiff:
        """3-way reapply of stale text-modify edits onto current content via unique-context anchoring; re-emits a revision+1 PreparedDiff. Fail-closed `DENIED_STALE` on non-unique/overlapping spans, non-text, or vendor-input shape drift."""
        stale_ids = {str(change["change_id"]) for change in stale}
        change_specs: list[dict[str, Any]] = []
        rebased_by_path: dict[str, str] = {}
        for change in prepared.body["file_changes"]:
            before = self.read_artifact(change["before"])
            proposed = self.read_artifact(change["proposed"])
            _relative, target = self._workspace_path(change["path"])
            if str(change["change_id"]) in stale_ids:
                try:
                    current = self._read_file(target, error_code=DENIED_STALE)
                    base_text = before.decode("utf-8")
                    proposed_text = proposed.decode("utf-8")
                    current_text = current.decode("utf-8")
                except (DiffSnapshotError, UnicodeDecodeError):
                    raise DiffSnapshotError(DENIED_STALE, "text rebase unavailable") from None
                replacement = self._reapply_unique(base_text, proposed_text, current_text)
                proposed_content: bytes | str = replacement
                rebased_by_path[change["path"]] = replacement
            else:
                proposed_content = proposed
            change_specs.append({
                "path": change["path"], "kind": "modify", "proposed_content": proposed_content,
            })
        updated_input = copy.deepcopy(prepared.updated_input) if prepared.updated_input is not None else None
        if updated_input is not None and isinstance(updated_input.get("changes"), list):
            body_paths = {str(change["path"]) for change in prepared.body["file_changes"]}
            nested: list[dict[str, Any]] = []
            seen: set[str] = set()
            for raw in updated_input["changes"]:
                if not isinstance(raw, Mapping) or set(raw) != {"kind", "path", "content"} \
                        or raw.get("kind") != "modify" or not isinstance(raw.get("content"), str):
                    raise DiffSnapshotError(DENIED_STALE, "rebased proposal structure changed")
                path, _target = self._workspace_path(raw.get("path"))
                if path in seen or path not in body_paths:
                    raise DiffSnapshotError(DENIED_STALE, "rebased proposal paths changed")
                seen.add(path)
                item = dict(raw)
                if path in rebased_by_path:
                    item["content"] = rebased_by_path[path]
                nested.append(item)
            if seen != body_paths:
                raise DiffSnapshotError(DENIED_STALE, "rebased proposal paths changed")
            updated_input["changes"] = nested
        elif updated_input is not None and len(rebased_by_path) == 1 and "content" in updated_input:
            updated_input["content"] = next(iter(rebased_by_path.values()))
        elif updated_input is not None:
            raise DiffSnapshotError(DENIED_STALE, "vendor input cannot carry rebased content")
        return self.prepare_changes(
            prepared.engine,
            change_specs,
            revision=int(prepared.body["approval_revision"]) + 1,
            updated_input=updated_input,
            scope_id=prepared.scope_id,
        )

    @staticmethod
    def _reapply_unique(base: str, proposed: str, current: str) -> str:
        base_lines = base.splitlines(keepends=True)
        proposed_lines = proposed.splitlines(keepends=True)
        current_lines = current.splitlines(keepends=True)
        matcher = difflib.SequenceMatcher(None, base_lines, proposed_lines, autojunk=False)
        replacements: list[tuple[int, int, list[str]]] = []
        occupied: list[tuple[int, int]] = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            left = max(0, i1 - 1)
            right = min(len(base_lines), i2 + 1)
            pattern = base_lines[left:right]
            if not pattern:
                raise DiffSnapshotError(DENIED_STALE, "edit has no unique context")
            replacement = [*base_lines[left:i1], *proposed_lines[j1:j2], *base_lines[i2:right]]
            matches = [
                index for index in range(0, len(current_lines) - len(pattern) + 1)
                if current_lines[index:index + len(pattern)] == pattern
            ]
            if len(matches) != 1:
                raise DiffSnapshotError(DENIED_STALE, "base edit is not unique")
            start = matches[0]
            end = start + len(pattern)
            if any(not (end <= prior_start or start >= prior_end) for prior_start, prior_end in occupied):
                raise DiffSnapshotError(DENIED_STALE, "rebased edits overlap")
            occupied.append((start, end))
            replacements.append((start, end, replacement))
        if not replacements:
            raise DiffSnapshotError(DENIED_STALE, "no changed span to reapply")
        for start, end, replacement in sorted(replacements, reverse=True):
            current_lines[start:end] = replacement
        return "".join(current_lines)

    # ---- post-apply evidence ---------------------------------------------------------------

    @staticmethod
    def _audit_state(
        authority: WorkspacePathAuthority,
        value: Any,
    ) -> tuple[str, str | None, int | None]:
        """Return one bounded handle-authoritative state for a canonical approval path."""

        if not isinstance(value, str) or not value or "\x00" in value:
            return "unreadable", None, None
        try:
            identity = authority.identity(value)
            if identity is None:
                return "missing", None, None
            stable = authority.read(value, MAX_SNAPSHOT_SET_BYTES)
            return "regular", stable.sha256, stable.size
        except WorkspacePathError:
            return "unreadable", None, None

    @staticmethod
    def _matches_side(
        state: tuple[str, str | None, int | None],
        side: Mapping[str, Any] | None,
    ) -> bool:
        kind, digest, size = state
        if side is None:
            return kind == "missing"
        return kind == "regular" and digest == side.get("sha256") and size == side.get("bytes")

    @staticmethod
    def _extent(path_states: Sequence[tuple[bool, bool, bool]]) -> str:
        """Classify exact final state against both immutable approval sides."""

        if not path_states or any(unreadable or (not before and not proposed)
                                  for before, proposed, unreadable in path_states):
            return "uncertain"
        if all(proposed for _before, proposed, _unreadable in path_states):
            return "complete"
        if all(before for before, _proposed, _unreadable in path_states):
            return "none"
        return "partial"

    def audit(self, approval_id: str) -> dict[str, Any]:
        """Post-apply evidence: re-reads each approved path via bounded handle authority, classifies `apply_extent`, records mismatches, ALWAYS discards pending in `finally`; returns OK only when extent=="complete". Side effect: `discard(approval_id)`."""
        with self._lock:
            pending = self._pending.get(approval_id)
            prepared = pending.current if pending is not None else None
        if prepared is None:
            return {"status": "DENIED", "code": DENIED_PARTIAL_APPLY,
                    "apply_extent": "uncertain", "partial_apply": True,
                    "mismatches": [{"path": "", "reason": "post_apply_audit_unavailable"}]}
        mismatches: list[dict[str, Any]] = []
        path_states: list[tuple[bool, bool, bool]] = []
        authority = self._workspace_path_authority
        owns_authority = authority is None
        try:
            if authority is None:
                authority = WorkspacePathAuthority(self.workspace_root)
            for change in prepared.body["file_changes"]:
                path = str(change["path"])
                before = None if change["kind"] in ("create", "rename") else change.get("before")
                expected = change.get("proposed")
                expected_exists = expected is not None
                state = self._audit_state(authority, path)
                actual_kind, actual_hash, _actual_bytes = state
                before_match = self._matches_side(state, before if isinstance(before, Mapping) else None)
                proposed_match = self._matches_side(
                    state, expected if isinstance(expected, Mapping) else None,
                )
                unreadable = actual_kind == "unreadable"
                path_states.append((before_match, proposed_match, unreadable))
                if unreadable:
                    mismatches.append({"path": path, "reason": "target_state_unreadable"})
                elif not proposed_match:
                    actual_exists = actual_kind == "regular"
                    expected_hash = expected.get("sha256") if isinstance(expected, Mapping) else None
                    mismatches.append({
                        "path": path, "reason": "final_state_mismatch",
                        "expected_exists": expected_exists,
                        "actual_exists": actual_exists, "expected_sha256": expected_hash,
                        "actual_sha256": actual_hash,
                    })
                if change["kind"] != "rename":
                    continue
                old_path = str(change["old_path"])
                old_before = change.get("before")
                old_state = self._audit_state(authority, old_path)
                old_kind, old_hash, _old_bytes = old_state
                old_before_match = self._matches_side(
                    old_state, old_before if isinstance(old_before, Mapping) else None,
                )
                old_proposed_match = self._matches_side(old_state, None)
                old_unreadable = old_kind == "unreadable"
                path_states.append((old_before_match, old_proposed_match, old_unreadable))
                if old_unreadable:
                    mismatches.append({"path": old_path, "reason": "target_state_unreadable"})
                elif not old_proposed_match:
                    mismatches.append({
                        "path": old_path, "reason": "final_state_mismatch",
                        "expected_exists": False,
                        "actual_exists": True, "expected_sha256": None,
                        "actual_sha256": old_hash,
                    })
        except Exception:  # noqa: BLE001 -- audit failure remains explicit bounded uncertainty
            # Earlier mismatch rows are only a partial scan prefix; this sentinel marks them incomplete.
            path_states.append((False, False, True))
            if len(mismatches) < MAX_FILE_CHANGES * 2:
                mismatches.append({"path": "", "reason": "post_apply_audit_unavailable"})
        finally:
            if owns_authority and authority is not None:
                authority.close()
            self.discard(approval_id)
        extent = self._extent(path_states)
        if extent not in _APPLY_EXTENTS:  # pragma: no cover - closed classifier invariant
            extent = "uncertain"
        uncertainty_reasons = {
            "apply_outcome_uncertain", "post_apply_audit_unavailable", "target_state_unreadable",
        }
        if extent == "uncertain" and not any(
            not item.get("path") or item.get("reason") in uncertainty_reasons for item in mismatches
        ):
            mismatches = [
                {"path": "", "reason": "apply_outcome_uncertain"},
                *mismatches[:MAX_FILE_CHANGES * 2 - 1],
            ]
        partial_apply = extent in ("partial", "uncertain")
        if extent != "complete":
            return {"status": "DENIED", "code": DENIED_PARTIAL_APPLY,
                    "apply_extent": extent, "partial_apply": partial_apply,
                    "mismatches": mismatches[:MAX_FILE_CHANGES * 2]}
        return {"status": "OK", "apply_extent": extent, "partial_apply": False, "mismatches": []}


__all__ = [
    "DENIED_PARTIAL_APPLY", "DiffSnapshotError", "DiffSnapshotManager", "PreparedDiff",
]
