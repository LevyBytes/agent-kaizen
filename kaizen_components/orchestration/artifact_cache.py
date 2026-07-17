"""Workspace-local immutable artifacts with bounded retention metadata.

Only hashes and reference metadata cross durable protocol boundaries. Raw bytes remain below the
exact ``AI/work/orchestration/ui-cache/{images,context,diffs}/sha256`` roots.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


ARTIFACT_KINDS = ("images", "context", "diffs")
ARTIFACT_ORIGINS = frozenset({"approval_diff", "file", "host", "host_image", "selection", "test-extension"})
CONTEXT_ARTIFACT_ORIGINS = frozenset({"file", "selection"})
assert CONTEXT_ARTIFACT_ORIGINS < ARTIFACT_ORIGINS
PARTIAL_RETENTION = timedelta(hours=24)
ORPHAN_RETENTION = timedelta(hours=24)
LONG_RETENTION = timedelta(days=30)
CLEANUP_INTERVAL = timedelta(hours=24)
_MUTABLE_METADATA_KEYS = frozenset({"approval_id", "state", "resolved_at"})
_CACHE_RELATIVE = Path("AI") / "work" / "orchestration" / "ui-cache"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ArtifactCacheError(ValueError):
    """A cache path, object, or sidecar failed closed."""

    def __init__(self, code: str, reason: str = "", *, rollback_unproven: bool = False) -> None:
        """Code`/`reason` roles and the `rollback_unproven` sentinel meaning."""
        super().__init__(reason or code)
        self.code = code
        self.rollback_unproven = rollback_unproven


@dataclass(frozen=True)
class CachedArtifact:
    """Immutable ref record; `artifact_ref`=`sha256:<hex>` vs bare `sha256` field."""
    artifact_ref: str
    sha256: str
    bytes: int
    created_at: str
    media_type: str | None = None


def _utc(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_text(now: datetime | None = None) -> str:
    return _utc(now).isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc)


def _validate_expected_bytes(value: int | None) -> None:
    """Reject booleans, negative values, and non-integers in optional size declarations."""

    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ArtifactCacheError("DENIED_ARTIFACT_SIZE", "expected artifact size must be a non-negative integer")


class ArtifactCache:
    """Content-addressed cache shared by attachments, governed context, and approval diffs."""

    _IO_LOCK = threading.RLock()
    _CLEANUP_LOCK = threading.Lock()

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        logger: Callable[[str], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Workspace_root` resolution; injected `logger`/`clock`; asserts (does not create) cache root."""
        self.workspace_root = Path(workspace_root).resolve()
        self.cache_root = self.workspace_root / _CACHE_RELATIVE
        self._logger = logger or (lambda _message: None)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._assert_cache_root(create=False)

    @staticmethod
    def _is_reparse(path: Path) -> bool:
        try:
            if path.is_symlink():
                return True
            is_junction = getattr(path, "is_junction", None)
            if callable(is_junction) and is_junction():
                return True
            attrs = getattr(os.lstat(path), "st_file_attributes", 0)
            return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        except OSError:
            return False

    def _assert_cache_root(self, *, create: bool) -> None:
        current = self.workspace_root
        if self._is_reparse(current):
            raise ArtifactCacheError("DENIED_ARTIFACT_PATH", "workspace root is a reparse point")
        for component in _CACHE_RELATIVE.parts:
            current = current / component
            if current.exists():
                if not current.is_dir() or self._is_reparse(current):
                    raise ArtifactCacheError("DENIED_ARTIFACT_PATH", "cache component is unsafe")
            elif create:
                try:
                    current.mkdir()
                except OSError:
                    raise ArtifactCacheError("DENIED_ARTIFACT_WRITE", "cache creation failed") from None
                if self._is_reparse(current):
                    raise ArtifactCacheError("DENIED_ARTIFACT_PATH", "cache creation was redirected")
            else:
                break
        if self.cache_root.exists():
            try:
                self.cache_root.resolve(strict=True).relative_to(self.workspace_root)
            except (OSError, ValueError):
                raise ArtifactCacheError("DENIED_ARTIFACT_PATH", "cache escapes workspace") from None

    def kind_root(self, kind: str, *, create: bool = False) -> Path:
        """Validated `{kind}/sha256` root; `create` semantics; raises DENIED_ARTIFACT_KIND/PATH/WRITE."""
        if kind not in ARTIFACT_KINDS:
            raise ArtifactCacheError("DENIED_ARTIFACT_KIND", "unknown cache kind")
        self._assert_cache_root(create=create)
        current = self.cache_root
        for component in (kind, "sha256"):
            current = current / component
            if current.exists():
                if not current.is_dir() or self._is_reparse(current):
                    raise ArtifactCacheError("DENIED_ARTIFACT_PATH", "kind root is unsafe")
            elif create:
                try:
                    current.mkdir()
                except OSError:
                    raise ArtifactCacheError("DENIED_ARTIFACT_WRITE", "kind root creation failed") from None
                if self._is_reparse(current):
                    raise ArtifactCacheError("DENIED_ARTIFACT_PATH", "kind root creation was redirected")
            else:
                break
        return current

    @staticmethod
    def _digest(value: Any) -> str:
        digest = value[7:] if isinstance(value, str) and value.startswith("sha256:") else value
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise ArtifactCacheError("DENIED_ARTIFACT_INVALID", "invalid artifact digest")
        return digest

    @staticmethod
    def _scope_tag(scope_id: Any) -> str:
        return hashlib.sha256(str(scope_id or "unscoped").encode("utf-8")).hexdigest()[:16]

    def _paths(self, kind: str, digest: str) -> tuple[Path, Path]:
        root = self.kind_root(kind, create=False)
        artifact = root / digest
        sidecar = root / f"{digest}.meta.json"
        if artifact.parent != root or sidecar.parent != root:
            raise ArtifactCacheError("DENIED_ARTIFACT_PATH", "artifact escapes kind root")
        return artifact, sidecar

    def _assert_leaf(self, path: Path, root: Path) -> None:
        if path.parent != root or (path.exists() and self._is_reparse(path)):
            raise ArtifactCacheError("DENIED_ARTIFACT_PATH", "artifact leaf is unsafe")
        if root == self.cache_root:
            self._assert_cache_root(create=False)
        else:
            self.kind_root(root.parent.name, create=False)

    @staticmethod
    def _read_bytes(path: Path, code: str) -> bytes:
        try:
            return path.read_bytes()
        except OSError:
            raise ArtifactCacheError(code, "artifact read failed") from None

    def _read_sidecar(self, path: Path, root: Path) -> dict[str, Any] | None:
        self._assert_leaf(path, root)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ArtifactCacheError("DENIED_ARTIFACT_METADATA", "artifact sidecar is invalid") from None
        if not isinstance(value, dict):
            raise ArtifactCacheError("DENIED_ARTIFACT_METADATA", "artifact sidecar is not an object")
        return value

    def _atomic_bytes(self, target: Path, content: bytes, scope_id: Any, root: Path) -> None:
        tag = self._scope_tag(scope_id)
        partial = root / f"{target.name}.{tag}.{secrets.token_hex(8)}.part"
        self._assert_leaf(target, root)
        self._assert_leaf(partial, root)
        try:
            with partial.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            self._assert_leaf(target, root)
            os.replace(partial, target)
        except ArtifactCacheError:
            raise
        except OSError:
            raise ArtifactCacheError("DENIED_ARTIFACT_WRITE", "atomic artifact write failed") from None
        finally:
            try:
                if partial.exists() and not self._is_reparse(partial):
                    partial.unlink()
            except OSError:
                pass

    def _write_sidecar(self, path: Path, value: Mapping[str, Any], scope_id: Any, root: Path) -> None:
        encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._atomic_bytes(path, encoded, scope_id, root)

    def store(
        self,
        kind: str,
        content: bytes,
        *,
        scope_id: Any,
        expected_sha256: str | None = None,
        expected_bytes: int | None = None,
        media_type: str | None = None,
        origin: str | None = None,
    ) -> CachedArtifact:
        """Dedup-on-digest, corruption re-check of landed bytes, origin-merge into `origin`/`origins`, return contract (`sha256:`-prefixed ref)."""
        if not isinstance(content, bytes):
            raise ArtifactCacheError("DENIED_ARTIFACT_INVALID", "artifact content must be bytes")
        _validate_expected_bytes(expected_bytes)
        if origin is not None and origin not in ARTIFACT_ORIGINS:
            raise ArtifactCacheError("DENIED_ARTIFACT_INVALID", "artifact origin is not in the allowed origin set")
        digest = hashlib.sha256(content).hexdigest()
        if expected_sha256 is not None and self._digest(expected_sha256) != digest:
            raise ArtifactCacheError("DENIED_ARTIFACT_HASH", "artifact hash declaration mismatch")
        if expected_bytes is not None and expected_bytes != len(content):
            raise ArtifactCacheError("DENIED_ARTIFACT_SIZE", "artifact size declaration mismatch")
        now = _utc_text(self._clock())
        with self._IO_LOCK:
            root = self.kind_root(kind, create=True)
            artifact, sidecar = self._paths(kind, digest)
            self._assert_leaf(artifact, root)
            if artifact.exists():
                landed = self._read_bytes(artifact, "DENIED_ARTIFACT_MISSING")
                if len(landed) != len(content) or hashlib.sha256(landed).hexdigest() != digest:
                    raise ArtifactCacheError("DENIED_ARTIFACT_CORRUPT", "cached artifact is corrupt")
            else:
                self._atomic_bytes(artifact, content, scope_id, root)
            existing = self._read_sidecar(sidecar, root)
            metadata = dict(existing or {})
            metadata.update({
                "version": 1, "kind": kind, "sha256": digest, "bytes": len(content),
                "created_at": metadata.get("created_at") or now,
            })
            if media_type is not None:
                metadata["media_type"] = media_type
            if origin is not None:
                canonical_origin = metadata.get("origin")
                if canonical_origin is not None and canonical_origin not in ARTIFACT_ORIGINS:
                    raise ArtifactCacheError("DENIED_ARTIFACT_METADATA", "artifact origin is invalid")
                prior_origins = metadata.get("origins", [])
                if not isinstance(prior_origins, list) or any(
                    not isinstance(value, str) or value not in ARTIFACT_ORIGINS for value in prior_origins
                ) or len(set(prior_origins)) != len(prior_origins):
                    raise ArtifactCacheError("DENIED_ARTIFACT_METADATA", "artifact origins are invalid")
                origins = set(prior_origins)
                if isinstance(canonical_origin, str) and canonical_origin:
                    origins.add(canonical_origin)
                origins.add(origin)
                metadata["origin"] = canonical_origin or origin
                metadata["origins"] = sorted(origins)
            self._write_sidecar(sidecar, metadata, scope_id, root)
        persisted_media_type = metadata.get("media_type")
        return CachedArtifact(f"sha256:{digest}", digest, len(content), str(metadata["created_at"]), persisted_media_type)

    def store_many(
        self,
        kind: str,
        entries: Iterable[Mapping[str, Any]],
        *,
        scope_id: Any,
    ) -> list[CachedArtifact]:
        """Validate a request batch before publication and restore exact leaves on write failure."""

        if kind not in ARTIFACT_KINDS:
            raise ArtifactCacheError("DENIED_ARTIFACT_KIND", "unknown cache kind")
        prepared: list[dict[str, Any]] = []
        for entry in entries:
            content = entry.get("content") if isinstance(entry, Mapping) else None
            if not isinstance(content, bytes):
                raise ArtifactCacheError("DENIED_ARTIFACT_INVALID", "artifact content must be bytes")
            digest = hashlib.sha256(content).hexdigest()
            expected_sha256 = entry.get("expected_sha256")
            expected_bytes = entry.get("expected_bytes")
            _validate_expected_bytes(expected_bytes)
            origin = entry.get("origin")
            if origin is not None and origin not in ARTIFACT_ORIGINS:
                raise ArtifactCacheError("DENIED_ARTIFACT_INVALID", "artifact origin must be a non-empty string")
            if expected_sha256 is not None and self._digest(expected_sha256) != digest:
                raise ArtifactCacheError("DENIED_ARTIFACT_HASH", "artifact hash declaration mismatch")
            if expected_bytes is not None and expected_bytes != len(content):
                raise ArtifactCacheError("DENIED_ARTIFACT_SIZE", "artifact size declaration mismatch")
            prepared.append({
                "content": content,
                "digest": digest,
                "expected_sha256": expected_sha256,
                "expected_bytes": expected_bytes,
                "media_type": entry.get("media_type"),
                "origin": origin,
            })
        if not prepared:
            return []

        with self._IO_LOCK:
            root = self.kind_root(kind, create=True)
            snapshots: dict[Path, bytes | None] = {}
            for entry in prepared:
                artifact, sidecar = self._paths(kind, str(entry["digest"]))
                for path in (artifact, sidecar):
                    if path in snapshots:
                        continue
                    self._assert_leaf(path, root)
                    snapshots[path] = self._read_bytes(path, "DENIED_ARTIFACT_WRITE") if path.exists() else None
            try:
                return [
                    self.store(
                        kind, entry["content"], scope_id=scope_id,
                        expected_sha256=entry["expected_sha256"],
                        expected_bytes=entry["expected_bytes"],
                        media_type=entry["media_type"], origin=entry["origin"],
                    )
                    for entry in prepared
                ]
            except Exception:
                rollback_failed = False
                for path, original in reversed(tuple(snapshots.items())):
                    try:
                        self._assert_leaf(path, root)
                        if original is None:
                            if path.exists():
                                path.unlink()
                        else:
                            self._atomic_bytes(path, original, scope_id, root)
                    except Exception:  # noqa: BLE001 -- surface rollback uncertainty, not the write error
                        rollback_failed = True
                if rollback_failed:
                    raise ArtifactCacheError(
                        "DENIED_ARTIFACT_WRITE", "artifact batch rollback could not be proven",
                        rollback_unproven=True,
                    ) from None
                raise

    def read(
        self,
        kind: str,
        artifact_ref: str,
        *,
        expected_sha256: str | None = None,
        expected_bytes: int | None = None,
    ) -> bytes:
        """Hash + optional size re-verification on read; DENIED_ARTIFACT_MISSING/SIZE/CORRUPT."""
        _validate_expected_bytes(expected_bytes)
        digest = self._digest(artifact_ref)
        if expected_sha256 is not None and self._digest(expected_sha256) != digest:
            raise ArtifactCacheError("DENIED_ARTIFACT_HASH", "artifact reference/hash mismatch")
        with self._IO_LOCK:
            root = self.kind_root(kind, create=False)
            artifact, _sidecar = self._paths(kind, digest)
            self._assert_leaf(artifact, root)
            content = self._read_bytes(artifact, "DENIED_ARTIFACT_MISSING")
            if expected_bytes is not None and len(content) != expected_bytes:
                raise ArtifactCacheError("DENIED_ARTIFACT_SIZE", "artifact size mismatch")
            if hashlib.sha256(content).hexdigest() != digest:
                raise ArtifactCacheError("DENIED_ARTIFACT_CORRUPT", "artifact hash mismatch")
            return content

    def metadata(self, kind: str, artifact_ref: str) -> dict[str, Any] | None:
        """Returns a copy of the validated sidecar dict or None."""
        digest = self._digest(artifact_ref)
        with self._IO_LOCK:
            root = self.kind_root(kind, create=False)
            _artifact, sidecar = self._paths(kind, digest)
            value = self._read_sidecar(sidecar, root)
            return dict(value) if value is not None else None

    def availability(
        self,
        kind: str,
        artifact_ref: str,
        *,
        expected_sha256: str | None = None,
        expected_bytes: int | None = None,
    ) -> str:
        """Return available|expired|unavailable without reading immutable artifact bytes.

        A durable reference whose object no longer exists is expired. An object that still exists but
        whose path, stat, or sidecar cannot be proven is unavailable (fail closed). Only validated
        sidecar metadata plus file size are inspected; continuation never replays historical bytes.
        """

        try:
            _validate_expected_bytes(expected_bytes)
        except ArtifactCacheError:
            return "unavailable"
        try:
            digest = self._digest(artifact_ref)
            if expected_sha256 is not None and self._digest(expected_sha256) != digest:
                return "unavailable"
            with self._IO_LOCK:
                root = self.kind_root(kind, create=False)
                artifact, sidecar = self._paths(kind, digest)
                self._assert_leaf(artifact, root)
                self._assert_leaf(sidecar, root)
                if not artifact.is_file():
                    return "expired"
                metadata = self._read_sidecar(sidecar, root)
                if metadata is None:
                    return "unavailable"
                size = artifact.stat().st_size
                if (
                    metadata.get("kind") != kind
                    or metadata.get("sha256") != digest
                    or isinstance(metadata.get("bytes"), bool)
                    or not isinstance(metadata.get("bytes"), int)
                    or metadata.get("bytes") != size
                    or (expected_bytes is not None and expected_bytes != size)
                ):
                    return "unavailable"
                return "available"
        except (ArtifactCacheError, OSError):
            return "unavailable"

    def update_metadata(self, kind: str, refs: Iterable[str], *, scope_id: Any, **updates: Any) -> None:
        """Rewrite only mutable lifecycle metadata; integrity fields remain store-owned and immutable."""
        unknown = set(updates) - _MUTABLE_METADATA_KEYS
        if unknown:
            raise ArtifactCacheError("DENIED_ARTIFACT_METADATA", "artifact metadata update key is immutable or unknown")
        with self._IO_LOCK:
            root = self.kind_root(kind, create=False)
            for reference in dict.fromkeys(refs):
                digest = self._digest(reference)
                artifact, sidecar = self._paths(kind, digest)
                self._assert_leaf(artifact, root)
                if not artifact.is_file():
                    raise ArtifactCacheError("DENIED_ARTIFACT_MISSING", "artifact missing during metadata update")
                metadata = self._read_sidecar(sidecar, root)
                if metadata is None:
                    raise ArtifactCacheError("DENIED_ARTIFACT_METADATA", "artifact sidecar missing")
                for key, value in updates.items():
                    if value is not None:
                        metadata[key] = value
                self._write_sidecar(sidecar, metadata, scope_id, root)

    def add_image_reference(self, refs: Iterable[str], session_id: str) -> None:
        """Session-id union into `session_ids`; snapshot rollback + `rollback_unproven` mode."""
        with self._IO_LOCK:
            root = self.kind_root("images", create=False)
            pending: list[tuple[Path, dict[str, Any]]] = []
            snapshots: dict[Path, bytes] = {}
            for reference in dict.fromkeys(refs):
                digest = self._digest(reference)
                artifact, sidecar = self._paths("images", digest)
                self._assert_leaf(artifact, root)
                if not artifact.is_file():
                    raise ArtifactCacheError("DENIED_ARTIFACT_MISSING", "image artifact missing")
                metadata = self._read_sidecar(sidecar, root)
                if metadata is None:
                    raise ArtifactCacheError("DENIED_ARTIFACT_METADATA", "image sidecar missing")
                snapshots[sidecar] = self._read_bytes(sidecar, "DENIED_ARTIFACT_METADATA")
                prior_sessions = metadata.get("session_ids", [])
                if not isinstance(prior_sessions, list) or any(not isinstance(value, str) for value in prior_sessions):
                    raise ArtifactCacheError("DENIED_ARTIFACT_METADATA", "artifact session_ids are invalid")
                sessions = sorted(set(value for value in prior_sessions if value) | {session_id})
                pending.append((sidecar, {**metadata, "session_ids": sessions}))
            try:
                for sidecar, metadata in pending:
                    self._write_sidecar(sidecar, metadata, session_id, root)
            except Exception:
                rollback_failed = False
                for sidecar, original in reversed(tuple(snapshots.items())):
                    try:
                        self._atomic_bytes(sidecar, original, session_id, root)
                    except Exception:  # noqa: BLE001 -- retain the exact uncertainty signal
                        rollback_failed = True
                if rollback_failed:
                    raise ArtifactCacheError(
                        "DENIED_ARTIFACT_WRITE", "image reference rollback could not be proven",
                        rollback_unproven=True,
                    ) from None
                raise

    def mark_diff_open(self, refs: Iterable[str], approval_id: str) -> None:
        """Mark diff sidecars open so approval-pending artifacts retain indefinitely."""
        self.update_metadata("diffs", refs, scope_id=approval_id, approval_id=approval_id, state="open")

    def mark_diff_terminal(self, refs: Iterable[str], approval_id: str) -> None:
        """Mark diff sidecars terminal so their long retention window begins."""
        self.update_metadata(
            "diffs", refs, scope_id=approval_id, approval_id=approval_id,
            state="terminal", resolved_at=_utc_text(self._clock()),
        )

    def cleanup(
        self,
        *,
        is_session_resumable: Callable[[str], bool] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Best-effort cleanup, normally no more often than ``CLEANUP_INTERVAL`` unless forced."""

        if not self._CLEANUP_LOCK.acquire(blocking=False):
            return {"status": "SKIPPED", "reason": "cleanup_locked"}
        removed = 0
        skipped = 0
        try:
            now = _utc(self._clock())
            with self._IO_LOCK:
                self._assert_cache_root(create=True)
                state_path = self.cache_root / "cleanup-state.json"
                if state_path.exists() and self._is_reparse(state_path):
                    return {"status": "SKIPPED", "reason": "cleanup_state_unsafe"}
                state: dict[str, Any] = {}
                if state_path.exists():
                    try:
                        parsed = json.loads(state_path.read_text(encoding="utf-8"))
                        state = parsed if isinstance(parsed, dict) else {}
                    except (OSError, ValueError):
                        self._logger("artifact cleanup: invalid cleanup state; skipping")
                        return {"status": "SKIPPED", "reason": "cleanup_state_invalid"}
                last_run = _parse_utc(state.get("last_run_at"))
                if not force and last_run is not None and now - last_run < CLEANUP_INTERVAL:
                    return {"status": "SKIPPED", "reason": "cleanup_interval"}
                for partial in self.cache_root.glob("*.part"):
                    try:
                        if not self._is_reparse(partial) and now - datetime.fromtimestamp(
                            partial.stat().st_mtime, timezone.utc
                        ) >= PARTIAL_RETENTION:
                            partial.unlink()
                            removed += 1
                    except OSError as error:
                        skipped += 1
                        self._logger(f"artifact cleanup skipped root partial: {type(error).__name__}")
                for kind in ARTIFACT_KINDS:
                    try:
                        root = self.kind_root(kind, create=True)
                        for partial in root.glob("*.part"):
                            try:
                                if self._is_reparse(partial):
                                    skipped += 1
                                    continue
                                modified = datetime.fromtimestamp(partial.stat().st_mtime, timezone.utc)
                                if now - modified >= PARTIAL_RETENTION:
                                    partial.unlink()
                                    removed += 1
                            except OSError as error:
                                skipped += 1
                                self._logger(f"artifact cleanup skipped partial: {type(error).__name__}")
                        for artifact in list(root.iterdir()):
                            if _SHA256_RE.fullmatch(artifact.name) is None:
                                continue
                            try:
                                if not artifact.is_file() or self._is_reparse(artifact):
                                    skipped += 1
                                    continue
                                sidecar = root / f"{artifact.name}.meta.json"
                                metadata = self._read_sidecar(sidecar, root)
                                created = _parse_utc(metadata.get("created_at")) if metadata else None
                                created = created or datetime.fromtimestamp(artifact.stat().st_mtime, timezone.utc)
                                deadline: datetime | None = None
                                if kind == "context":
                                    deadline = created + LONG_RETENTION
                                elif kind == "diffs":
                                    resolved = _parse_utc(metadata.get("resolved_at")) if metadata else None
                                    if resolved is not None:
                                        deadline = resolved + LONG_RETENTION
                                    elif not metadata or metadata.get("state") != "open":
                                        deadline = created + ORPHAN_RETENTION
                                else:
                                    raw_sessions = (metadata or {}).get("session_ids", [])
                                    if not isinstance(raw_sessions, list) or any(
                                        not isinstance(value, str) for value in raw_sessions
                                    ):
                                        raise ArtifactCacheError("DENIED_ARTIFACT_METADATA", "artifact session_ids are invalid")
                                    sessions = [value for value in raw_sessions if value]
                                    resumable = bool(sessions) and is_session_resumable is not None and any(
                                        is_session_resumable(session_id) for session_id in sessions
                                    )
                                    if not resumable and sessions:
                                        unreferenced = _parse_utc((metadata or {}).get("unreferenced_at"))
                                        if unreferenced is None:
                                            metadata = dict(metadata or {})
                                            metadata["unreferenced_at"] = _utc_text(now)
                                            self._write_sidecar(sidecar, metadata, "cleanup", root)
                                        else:
                                            deadline = unreferenced + LONG_RETENTION
                                    elif not sessions:
                                        deadline = created + ORPHAN_RETENTION
                                if deadline is not None and now >= deadline:
                                    artifact.unlink()
                                    if sidecar.exists() and not self._is_reparse(sidecar):
                                        sidecar.unlink()
                                    removed += 1
                            except Exception as error:  # noqa: BLE001 -- cleanup logs/skips per object
                                skipped += 1
                                self._logger(f"artifact cleanup skipped object: {type(error).__name__}")
                    except ArtifactCacheError as error:
                        skipped += 1
                        self._logger(f"artifact cleanup skipped kind {kind}: {error.code}")
                self._atomic_bytes(
                    state_path,
                    json.dumps({"last_run_at": _utc_text(now)}, separators=(",", ":")).encode("utf-8"),
                    "cleanup", self.cache_root,
                )
            return {"status": "OK", "removed": removed, "skipped": skipped}
        finally:
            self._CLEANUP_LOCK.release()


__all__ = [
    "ARTIFACT_KINDS", "ARTIFACT_ORIGINS", "CONTEXT_ARTIFACT_ORIGINS", "ArtifactCache", "ArtifactCacheError", "CachedArtifact",
]
