"""Atomic owned-run registry updates and the daemon's single workspace-writer lease.

The in-memory claim is authoritative while a supervisor is alive.  A reserved entry in the existing
``owned_runs.json`` file is recovery evidence only: it closes the pre-record crash window without a new
database table or a second runtime registry.  All file read/modify/replace operations share one process
lock because tests may construct more than one ``Supervisor`` in the same interpreter.
"""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from .workspace_path_authority import WorkspacePathAuthority, WorkspacePathError


WRITER_MARKER_KEY = "__workspace_writer__"
MAX_APPLY_RECOVERY_BYTES = 4 * 1024 * 1024
MAX_OWNERSHIP_REGISTRY_BYTES = 8 * 1024 * 1024
MAX_STAGED_FILE_BYTES = 8 * 1024 * 1024
MAX_APPLY_STAGED_DESCRIPTORS = 128
MAX_APPLY_STAGED_TOTAL_BYTES = 64 * 1024 * 1024
_REGISTRY_LOCK = threading.RLock()
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class OwnershipStateError(RuntimeError):
    """The ownership registry cannot be trusted or updated safely."""


class OwnershipRegistry:
    """Locked, atomic facade over the legacy flat owned-run mapping."""

    def __init__(
        self,
        path: Path,
        *,
        workspace_root: str | Path,
        authority: WorkspacePathAuthority | None = None,
    ) -> None:
        """Containment invariant (registry path inside workspace root), authority-sharing/ownership semantics, OwnershipStateError cases."""
        root = Path(os.path.abspath(workspace_root))
        target = Path(os.path.abspath(path))
        try:
            relative = target.relative_to(root)
        except ValueError:
            raise OwnershipStateError("owned-run registry must be inside the workspace") from None
        if not relative.parts:
            raise OwnershipStateError("owned-run registry path is invalid")
        self.path = target
        self.workspace_root = root
        self.relative_path = relative
        if authority is not None and os.path.normcase(str(authority.workspace_root)) \
                != os.path.normcase(str(root)):
            raise OwnershipStateError("owned-run registry authority has a different workspace root")
        self._authority = authority or WorkspacePathAuthority(root)
        self._owns_authority = authority is None

    @property
    def authority(self) -> WorkspacePathAuthority:
        return self._authority

    def close(self) -> None:
        if self._owns_authority:
            self._authority.close()

    def load(self) -> dict[str, dict[str, Any]]:
        """Returns full decoded mapping incl. writer marker; {} when absent; raises on unreadable/invalid shape."""
        with self._authority.exclusive():
            with _REGISTRY_LOCK:
                try:
                    if self._authority.identity(self.relative_path.as_posix()) is None:
                        return {}
                    raw = self._authority.read(
                        self.relative_path.as_posix(), MAX_OWNERSHIP_REGISTRY_BYTES,
                    ).data
                except WorkspacePathError as error:
                    raise OwnershipStateError("owned-run registry is unreadable") from error
                return self._decode(raw)

    @staticmethod
    def _decode(raw: bytes) -> dict[str, dict[str, Any]]:
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise OwnershipStateError("owned-run registry is unreadable") from error
        if not isinstance(value, dict) or any(
            not isinstance(key, str) or not isinstance(item, dict) for key, item in value.items()
        ):
            raise OwnershipStateError("owned-run registry has an invalid shape")
        return {key: dict(item) for key, item in value.items()}

    def runs(self) -> dict[str, dict[str, Any]]:
        """Owned runs with writer-marker key stripped."""
        value = self.load()
        value.pop(WRITER_MARKER_KEY, None)
        return value

    def update(self, mutator: Callable[[dict[str, dict[str, Any]]], None]) -> dict[str, dict[str, Any]]:
        """Locked read-modify-atomic-replace under CAS expected; mutator contract; size bound; returns post-mutation snapshot."""
        with self._authority.exclusive():
            with _REGISTRY_LOCK:
                relative = self.relative_path.as_posix()
                try:
                    exists = self._authority.identity(relative) is not None
                    expected = self._authority.read(
                        relative, MAX_OWNERSHIP_REGISTRY_BYTES,
                    ) if exists else None
                    value = self._decode(expected.data) if expected is not None else {}
                    mutator(value)
                    encoded = json.dumps(
                        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                    ).encode("utf-8")
                    if len(encoded) > MAX_OWNERSHIP_REGISTRY_BYTES:
                        raise OwnershipStateError(
                            "owned-run registry exceeds its private recovery bound"
                        )
                    self._authority.atomic_replace(
                        relative, encoded, expected=expected,
                        max_bytes=MAX_OWNERSHIP_REGISTRY_BYTES,
                    )
                except WorkspacePathError as error:
                    raise OwnershipStateError("owned-run registry could not be updated safely") from error
                return {key: dict(item) for key, item in value.items()}

    def record_run(self, agent_run_id: str, *, pid: int, nonce: str) -> None:
        """Add or replace an owned-run registry entry keyed by agent run id."""
        self.update(lambda value: value.__setitem__(agent_run_id, {"pid": pid, "nonce": nonce}))

    def clear_run(self, agent_run_id: str) -> None:
        """Remove an owned-run registry entry when present."""
        def mutate(value: dict[str, dict[str, Any]]) -> None:
            value.pop(agent_run_id, None)

        self.update(mutate)

    def writer_marker(self) -> dict[str, Any] | None:
        """Return a copy of the durable singleton writer marker when present."""
        marker = self.load().get(WRITER_MARKER_KEY)
        return dict(marker) if marker is not None else None

    def put_writer_marker(self, marker: dict[str, Any]) -> None:
        """Atomically replace the durable singleton writer marker."""
        self.update(lambda value: value.__setitem__(WRITER_MARKER_KEY, dict(marker)))

    def clear_writer_marker(self, claim_id: str) -> bool:
        """Constant-time claim_id-gated removal; returns whether a matching marker was cleared."""
        removed = False

        def mutate(value: dict[str, dict[str, Any]]) -> None:
            nonlocal removed
            marker = value.get(WRITER_MARKER_KEY)
            if marker is not None and secrets.compare_digest(str(marker.get("claim_id") or ""), claim_id):
                value.pop(WRITER_MARKER_KEY, None)
                removed = True

        self.update(mutate)
        return removed


@dataclass(frozen=True)
class WriterClaim:
    """Immutable claim token; field meanings (spawn_pending, child_pids, session/run linkage)."""
    claim_id: str
    permission_mode: str
    acquired_at: str
    session_id: str | None = None
    agent_run_id: str | None = None
    spawn_pending: bool = False
    child_pids: tuple[int, ...] = ()

    def marker(self, *, pid: int, nonce: str) -> dict[str, Any]:
        """Project this claim into a durable marker including recovery fields."""
        return {
            "pid": pid,
            "nonce": nonce,
            "claim_id": self.claim_id,
            "permission_mode": self.permission_mode,
            "acquired_at": self.acquired_at,
            "session_id": self.session_id,
            "agent_run_id": self.agent_run_id,
            "spawn_pending": self.spawn_pending,
            "child_pids": list(self.child_pids),
            "recovery_required": False,
            "cleanup_paths": [],
            "apply_recovery": None,
        }

    def safe_holder(self) -> dict[str, Any]:
        """Return the redacted holder view exposed by busy denials."""
        return {
            "session_id": self.session_id,
            "agent_run_id": self.agent_run_id,
            "permission_mode": self.permission_mode,
            "acquired_at": self.acquired_at,
        }


class WorkspaceWriterLease:
    """One claim-token lease for the supervisor's canonical workspace."""

    def __init__(self, registry: OwnershipRegistry, *, pid: int, nonce: str,
                 clock: Callable[[], str], workspace_root: str | Path) -> None:
        """Workspace-root/registry match invariant; OwnershipStateError on mismatch."""
        self._registry = registry
        self._authority = registry.authority
        self._pid = pid
        self._nonce = nonce
        self._clock = clock
        self._lock = threading.RLock()
        self._holder: WriterClaim | None = None
        self._recovery_required = False
        self._recovery_reason = ""
        self._cleanup_paths: tuple[str, ...] = ()
        self._apply_recovery: dict[str, Any] | None = None
        self._workspace_root = Path(os.path.abspath(workspace_root))
        if os.path.normcase(str(self._workspace_root)) \
                != os.path.normcase(str(registry.workspace_root)):
            raise OwnershipStateError("writer lease workspace does not match its ownership registry")

    @contextmanager
    def _authority_locked(self):
        """Use one lock order everywhere: bound workspace authority, then lease state."""

        with self._authority.exclusive():
            with self._lock:
                yield

    @property
    def recovery_required(self) -> bool:
        """Return whether durable recovery refusal blocks new writers."""
        with self._lock:
            return self._recovery_required

    @property
    def active(self) -> bool:
        """Return whether a live writer claim is currently held."""
        with self._lock:
            return self._holder is not None

    @property
    def apply_recovery_active(self) -> bool:
        """Return whether an apply-recovery journal is currently in flight."""
        with self._lock:
            return self._apply_recovery is not None

    def _set_recovery(self, reason: str) -> None:
        self._recovery_required = True
        self._recovery_reason = reason

    def _durable_recovery_refusal(self, holder: WriterClaim, reason: str) -> bool:
        """Keep the last valid apply journal and durably refuse release/new writers."""

        self._set_recovery(reason)
        try:
            self._registry.put_writer_marker(self._marker(holder))
        except (OwnershipStateError, OSError):
            # The previous marker remains the crash authority.  In-memory state still closes this daemon.
            self._set_recovery("workspace recovery refusal could not be persisted")
        return False

    def _marker(self, holder: WriterClaim) -> dict[str, Any]:
        marker = holder.marker(pid=self._pid, nonce=self._nonce)
        marker.update({
            "recovery_required": self._recovery_required,
            "cleanup_paths": list(self._cleanup_paths),
            "apply_recovery": copy.deepcopy(self._apply_recovery),
        })
        if self._recovery_required:
            marker["recovery_reason"] = self._recovery_reason
        return marker

    @staticmethod
    def _valid_cleanup_path(value: Any) -> str | None:
        if not isinstance(value, str) or not value or len(value) > 1_024 or "\x00" in value:
            return None
        relative = Path(value)
        if relative.is_absolute() \
                or any(part in ("", ".", "..") or ":" in part for part in relative.parts):
            return None
        return relative.as_posix()

    @staticmethod
    def _path_identity(value: str) -> str:
        """Filesystem identity for duplicate/overlap checks (case-folded by normcase on Windows)."""

        return os.path.normcase(str(Path(value)))

    def require_recovery(self, reason: str, *, cleanup_paths: tuple[str, ...] = ()) -> bool:
        """Retain the durable holder and deny new writers after an uncertain cleanup boundary."""

        with self._authority_locked():
            self._set_recovery(reason)
            normalized = tuple(
                path for raw in cleanup_paths
                if (path := self._valid_cleanup_path(raw)) is not None
            )
            self._cleanup_paths = tuple(dict.fromkeys((*self._cleanup_paths, *normalized)))
            holder = self._holder
            if holder is None:
                return False
            marker = self._marker(holder)
            try:
                self._registry.put_writer_marker(marker)
            except (OwnershipStateError, OSError):
                self._set_recovery("workspace recovery marker could not be persisted")
                return False
            return True

    @classmethod
    def _validated_apply_recovery(
        cls,
        value: Any,
        *,
        owner_pid: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != {"phase", "staged", "targets"}:
            raise OwnershipStateError("workspace apply recovery state is malformed")
        phase = value.get("phase")
        staged = value.get("staged")
        targets = value.get("targets")
        if phase not in ("staging", "mutating") or not isinstance(staged, list) \
                or not isinstance(targets, list) or len(staged) > MAX_APPLY_STAGED_DESCRIPTORS \
                or not 1 <= len(targets) <= MAX_APPLY_STAGED_DESCRIPTORS:
            raise OwnershipStateError("workspace apply recovery state is malformed")
        clean_staged: list[dict[str, Any]] = []
        staged_paths: set[str] = set()
        staged_identities: set[str] = set()
        staged_total_bytes = 0
        for raw in staged:
            if not isinstance(raw, Mapping) or set(raw) != {"path", "sha256", "bytes"}:
                raise OwnershipStateError("workspace staged recovery descriptor is malformed")
            path = cls._valid_cleanup_path(raw.get("path"))
            digest = raw.get("sha256")
            byte_count = raw.get("bytes")
            identity = cls._path_identity(path) if path is not None else ""
            if path is None or identity in staged_identities or not isinstance(digest, str) \
                    or _SHA256.fullmatch(digest) is None or isinstance(byte_count, bool) \
                    or not isinstance(byte_count, int) or not 0 <= byte_count <= MAX_STAGED_FILE_BYTES:
                raise OwnershipStateError("workspace staged recovery descriptor is malformed")
            staged_paths.add(path)
            staged_identities.add(identity)
            staged_total_bytes += byte_count
            if staged_total_bytes > MAX_APPLY_STAGED_TOTAL_BYTES:
                raise OwnershipStateError("workspace staged recovery payload exceeds its aggregate bound")
            clean_staged.append({"path": path, "sha256": digest, "bytes": byte_count})
        clean_targets: list[dict[str, Any]] = []
        target_paths: set[str] = set()
        target_identities: set[str] = set()
        for raw in targets:
            keys = {"path", "base_exists", "base_sha256", "final_exists", "final_sha256"}
            if not isinstance(raw, Mapping) or set(raw) != keys:
                raise OwnershipStateError("workspace target recovery descriptor is malformed")
            path = cls._valid_cleanup_path(raw.get("path"))
            base_exists, final_exists = raw.get("base_exists"), raw.get("final_exists")
            base_hash, final_hash = raw.get("base_sha256"), raw.get("final_sha256")
            hashes_valid = (
                (not base_exists and base_hash is None)
                or base_exists and isinstance(base_hash, str) and _SHA256.fullmatch(base_hash) is not None
            ) and (
                (not final_exists and final_hash is None)
                or final_exists and isinstance(final_hash, str) and _SHA256.fullmatch(final_hash) is not None
            )
            identity = cls._path_identity(path) if path is not None else ""
            if path is None or identity in target_identities or not isinstance(base_exists, bool) \
                    or not isinstance(final_exists, bool) or not (base_exists or final_exists) \
                    or not hashes_valid:
                raise OwnershipStateError("workspace target recovery descriptor is malformed")
            target_paths.add(path)
            target_identities.add(identity)
            clean_targets.append({
                "path": path,
                "base_exists": base_exists,
                "base_sha256": base_hash,
                "final_exists": final_exists,
                "final_sha256": final_hash,
            })
        if staged_identities & target_identities:
            raise OwnershipStateError("workspace staged and target recovery paths overlap")
        for staged_path in staged_paths:
            candidate = Path(staged_path)
            matched = False
            for target_path in target_paths:
                target = Path(target_path)
                if cls._path_identity(candidate.parent.as_posix()) \
                        != cls._path_identity(target.parent.as_posix()):
                    continue
                prefix = f".{target.name}.kaizen-"
                candidate_name = os.path.normcase(candidate.name)
                normalized_prefix = os.path.normcase(prefix)
                temp_match = re.fullmatch(
                            r"[1-9][0-9]*-(?:0|[1-9][0-9]*)-[0-9a-f]{24}",
                            candidate_name[len(normalized_prefix):-4],
                        ) if candidate_name.startswith(normalized_prefix) \
                    and candidate_name.endswith(".tmp") else None
                if temp_match is not None and (
                    owner_pid is None or int(temp_match.group(0).split("-", 1)[0]) == owner_pid
                ):
                    matched = True
                    break
            if not matched:
                raise OwnershipStateError("workspace staged recovery path is not an exact Kaizen temp")
        clean = {"phase": phase, "staged": clean_staged, "targets": clean_targets}
        if len(json.dumps(clean, ensure_ascii=True, separators=(",", ":")).encode("utf-8")) \
                > MAX_APPLY_RECOVERY_BYTES:
            raise OwnershipStateError("workspace apply recovery state exceeds its private bound")
        return clean

    @staticmethod
    def _apply_transition_allowed(
        previous: Mapping[str, Any] | None,
        current: Mapping[str, Any] | None,
    ) -> bool:
        """Accept only the monotonic per-apply phase graph with immutable descriptors."""

        if previous is None:
            return current is None or current.get("phase") == "staging"
        if current is None:
            return True
        previous_phase = previous.get("phase")
        current_phase = current.get("phase")
        same_descriptors = previous.get("staged") == current.get("staged") \
            and previous.get("targets") == current.get("targets")
        return same_descriptors and (
            previous_phase == current_phase
            or previous_phase == "staging" and current_phase == "mutating"
        )

    def _apply_clear_is_proven(self, previous: Mapping[str, Any]) -> bool:
        """Re-prove the only phase-specific state that may erase a durable apply journal."""

        try:
            all_base, all_final = self._target_recovery_mode(previous)
            required_targets = all_base if previous.get("phase") == "staging" else all_final
            staged_absent = all(
                not self._recovery_file_state(str(item["path"]))["exists"]
                for item in previous["staged"]
            )
            return required_targets and staged_absent
        except (OwnershipStateError, OSError, KeyError, TypeError):
            return False

    def set_apply_recovery(self, claim_id: str | None, state: Mapping[str, Any] | None) -> bool:
        """Persist one exact staging/mutation phase before its corresponding filesystem action."""

        with self._authority_locked():
            holder = self._holder
            if claim_id is None or holder is None \
                    or not secrets.compare_digest(holder.claim_id, claim_id):
                # A delayed callback from a released claim must fail closed for that call without
                # poisoning whichever later claim currently owns the workspace.
                return False
            if self._recovery_required:
                return False
            try:
                normalized = self._validated_apply_recovery(
                    state, owner_pid=self._pid,
                ) if state is not None else None
            except OwnershipStateError:
                return self._durable_recovery_refusal(
                    holder, "workspace apply recovery state was invalid",
                )
            previous = self._apply_recovery
            if not self._apply_transition_allowed(previous, normalized):
                return self._durable_recovery_refusal(
                    holder, "workspace apply recovery phase transition was invalid",
                )
            if previous is not None and normalized is None \
                    and not self._apply_clear_is_proven(previous):
                return self._durable_recovery_refusal(
                    holder, "workspace apply recovery clear could not be re-proven",
                )
            self._apply_recovery = normalized
            try:
                self._registry.put_writer_marker(self._marker(holder))
            except (OwnershipStateError, OSError):
                self._apply_recovery = previous
                return self._durable_recovery_refusal(
                    holder, "workspace apply recovery marker could not be persisted",
                )
            return True

    def recovery_denial(self) -> dict[str, Any]:
        """Return the non-retryable recovery-required denial payload."""
        with self._lock:
            return {
                "status": "DENIED",
                "code": "DENIED_WORKSPACE_RECOVERY_REQUIRED",
                "retryable": False,
                "required_action": self._recovery_reason or "restart the daemon after prior writer reconciliation",
            }

    @staticmethod
    def _busy_denial(holder: WriterClaim) -> dict[str, Any]:
        """Return the retryable busy denial with a redacted holder view."""
        return {
            "status": "DENIED",
            "code": "DENIED_WORKSPACE_WRITER_BUSY",
            "retryable": True,
            "holder": holder.safe_holder(),
            "required_action": "retry after the current workspace writer releases",
        }

    def acquire(self, permission_mode: str, *, session_id: str | None = None,
                agent_run_id: str | None = None) -> tuple[WriterClaim | None, dict[str, Any] | None]:
        """Returns (claim,None) on grant or (None,denial); denial codes; resets apply/cleanup state."""
        with self._authority_locked():
            if self._recovery_required:
                return None, self.recovery_denial()
            if self._holder is not None:
                return None, self._busy_denial(self._holder)
            claim = WriterClaim(
                claim_id=secrets.token_hex(16),
                permission_mode=permission_mode,
                acquired_at=self._clock(),
                session_id=session_id,
                agent_run_id=agent_run_id,
            )
            self._apply_recovery = None
            self._cleanup_paths = ()
            try:
                self._registry.put_writer_marker(self._marker(claim))
            except OwnershipStateError:
                self._set_recovery("repair the malformed owned-run registry before retrying")
                return None, self.recovery_denial()
            except OSError:
                self._set_recovery("repair the owned-run registry write path before retrying")
                return None, self.recovery_denial()
            self._holder = claim
            return claim, None

    def promote(self, claim_id: str, *, session_id: str, agent_run_id: str) -> WriterClaim:
        """Binds session/run to live claim durably; fail-closed on identity loss; raises OwnershipStateError."""
        with self._authority_locked():
            holder = self._holder
            if holder is None or not secrets.compare_digest(holder.claim_id, claim_id):
                self._set_recovery("writer claim identity was lost during record promotion")
                raise OwnershipStateError("writer claim identity mismatch")
            promoted = replace(holder, session_id=session_id, agent_run_id=agent_run_id)
            try:
                self._registry.put_writer_marker(self._marker(promoted))
            except (OwnershipStateError, OSError) as error:
                self._set_recovery("writer claim could not be promoted durably")
                raise OwnershipStateError("writer claim promotion failed") from error
            self._holder = promoted
            return promoted

    def begin_spawn(self, claim_id: str) -> WriterClaim:
        """Commit the pre-spawn uncertainty marker before any vendor process can exist."""

        with self._authority_locked():
            holder = self._holder
            if holder is None or not secrets.compare_digest(holder.claim_id, claim_id):
                self._set_recovery("writer claim identity was lost before vendor spawn")
                raise OwnershipStateError("writer claim identity mismatch")
            pending = replace(holder, spawn_pending=True)
            try:
                self._registry.put_writer_marker(self._marker(pending))
            except (OwnershipStateError, OSError) as error:
                self._set_recovery("vendor spawn could not be marked durably")
                raise OwnershipStateError("writer spawn marker failed") from error
            self._holder = pending
            return pending

    def track_spawned_child(self, claim_id: str, child_pid: int) -> WriterClaim:
        """Append the spawned child PID and clear ``spawn_pending`` in one durable replacement."""

        if isinstance(child_pid, bool) or not isinstance(child_pid, int) or child_pid <= 0:
            self.require_recovery("vendor spawn returned an invalid child pid")
            raise OwnershipStateError("invalid child pid")
        with self._authority_locked():
            holder = self._holder
            if holder is None or not secrets.compare_digest(holder.claim_id, claim_id) \
                    or not holder.spawn_pending:
                self._set_recovery("vendor spawn result could not be matched to its pending claim")
                raise OwnershipStateError("writer spawn identity mismatch")
            tracked = replace(
                holder,
                spawn_pending=False,
                child_pids=tuple(dict.fromkeys((*holder.child_pids, child_pid))),
            )
            try:
                self._registry.put_writer_marker(self._marker(tracked))
            except (OwnershipStateError, OSError) as error:
                self._set_recovery("spawned vendor child could not be tracked durably")
                raise OwnershipStateError("writer child tracking failed") from error
            self._holder = tracked
            return tracked

    def verify(self, claim_id: str | None, *, agent_run_id: str) -> dict[str, Any] | None:
        """Confirms claim still owns workspace for agent_run_id; None on ok else denial, poisons into recovery."""
        with self._authority_locked():
            if self._recovery_required:
                return self.recovery_denial()
            holder = self._holder
            if claim_id is None or holder is None or not secrets.compare_digest(holder.claim_id, claim_id) \
                    or holder.agent_run_id != agent_run_id:
                self._set_recovery("the active turn no longer owns its required writer claim")
                return self.recovery_denial()
            return None

    def release(self, claim_id: str | None, *, termination_proven: bool = True) -> bool:
        """Idempotent for None/absent holder; refuses on unproven termination or uncleared apply-recovery; return meaning."""
        if claim_id is None:
            return True
        with self._authority_locked():
            holder = self._holder
            if holder is None:
                return True
            if not secrets.compare_digest(holder.claim_id, claim_id):
                return False  # stale cleanup must never release a later holder
            if self._apply_recovery is not None:
                reason = self._recovery_reason if self._recovery_required \
                    else "workspace apply recovery state was not cleared before release"
                return self._durable_recovery_refusal(
                    holder, reason,
                )
            if self._recovery_required:
                return False
            if not termination_proven:
                return self._durable_recovery_refusal(
                    holder, "prior writer termination could not be proven",
                )
            try:
                removed = self._registry.clear_writer_marker(claim_id)
            except (OwnershipStateError, OSError):
                self._set_recovery("writer recovery marker could not be cleared durably")
                return False
            if not removed:
                self._set_recovery("writer recovery marker no longer matches the in-memory claim")
                return False
            self._holder = None
            return True

    def _recovery_file_state(
        self,
        raw_path: str,
        *,
        max_bytes: int = MAX_STAGED_FILE_BYTES,
    ) -> dict[str, Any]:
        """Read one bounded regular file through the daemon-lifetime workspace authority."""

        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise OwnershipStateError("workspace recovery read bound must be a positive integer")
        normalized = self._valid_cleanup_path(raw_path)
        if normalized is None:
            raise OwnershipStateError("workspace recovery path is not safely contained")
        try:
            identity = self._authority.identity(normalized)
            if identity is None:
                return {"exists": False, "sha256": None, "bytes": None}
            if identity.size > max_bytes:
                raise OwnershipStateError("workspace recovery state exceeds its cumulative read bound")
            stable = self._authority.read(
                normalized, min(MAX_STAGED_FILE_BYTES, max(1, max_bytes)),
            )
            return {"exists": True, "sha256": stable.sha256, "bytes": stable.size}
        except WorkspacePathError as error:
            raise OwnershipStateError("workspace recovery state is unreadable") from error

    @staticmethod
    def _recovery_state_matches(
        actual: Mapping[str, Any],
        descriptor: Mapping[str, Any],
        prefix: str,
    ) -> bool:
        exists = descriptor.get(f"{prefix}_exists")
        digest = descriptor.get(f"{prefix}_sha256")
        return actual.get("exists") is exists and (
            not exists or actual.get("sha256") == digest
        )

    def _target_recovery_mode(self, state: Mapping[str, Any]) -> tuple[bool, bool]:
        all_base = True
        all_final = True
        total_bytes = 0
        for descriptor in state["targets"]:
            path = str(descriptor["path"])
            found = self._recovery_file_state(
                path, max_bytes=MAX_APPLY_STAGED_TOTAL_BYTES - total_bytes,
            )
            if found["exists"]:
                total_bytes += int(found["bytes"])
                if total_bytes > MAX_APPLY_STAGED_TOTAL_BYTES:
                    raise OwnershipStateError(
                        "workspace target recovery payload exceeds its aggregate bound"
                    )
            all_base = all_base and self._recovery_state_matches(found, descriptor, "base")
            all_final = all_final and self._recovery_state_matches(found, descriptor, "final")
        return all_base, all_final

    def _reconcile_apply_recovery(self, claim_id: str, state: Mapping[str, Any]) -> bool:
        before_mode = self._target_recovery_mode(state)
        if not any(before_mode):
            raise OwnershipStateError("workspace targets are neither wholly base nor wholly final")

        staged_states: list[dict[str, Any]] = []
        for descriptor in state["staged"]:
            found = self._recovery_file_state(str(descriptor["path"]))
            if found["exists"] and (
                found["sha256"] != descriptor["sha256"] or found["bytes"] != descriptor["bytes"]
            ):
                raise OwnershipStateError("workspace staged recovery payload does not match its journal")
            staged_states.append(found)

        # No filesystem mutation occurs until every target and staged payload has passed exact validation.
        for descriptor, initial in zip(state["staged"], staged_states):
            if not initial["exists"]:
                continue
            immediate = self._recovery_file_state(str(descriptor["path"]))
            if immediate != initial:
                raise OwnershipStateError("workspace staged recovery payload changed before cleanup")
            try:
                removed = self._authority.unlink_exact(
                    str(descriptor["path"]),
                    sha256=str(descriptor["sha256"]),
                    size=int(descriptor["bytes"]),
                    max_bytes=MAX_STAGED_FILE_BYTES,
                )
            except WorkspacePathError as error:
                raise OwnershipStateError("workspace staged recovery payload could not be removed") from error
            if not removed or self._recovery_file_state(str(descriptor["path"]))["exists"]:
                raise OwnershipStateError("workspace staged recovery cleanup could not be proven")

        after_mode = self._target_recovery_mode(state)
        if after_mode != before_mode or not any(after_mode):
            raise OwnershipStateError("workspace targets changed during staged recovery cleanup")
        if any(
            self._recovery_file_state(str(item["path"]))["exists"]
            for item in state["staged"]
        ):
            raise OwnershipStateError("workspace staged recovery paths are not all absent")
        if not self._registry.clear_writer_marker(claim_id):
            raise OwnershipStateError("workspace recovery marker could not be cleared")
        return True

    def reconcile_after_orphan_sweep(
        self,
        pid_alive: Callable[[int], bool],
        *,
        allow_clear: bool = True,
        blocked_reason: str = "",
    ) -> bool:
        """Reconcile a dead owner only from exact process and phase-aware filesystem evidence."""

        with self._authority_locked():
            try:
                marker = self._registry.writer_marker()
            except (OwnershipStateError, OSError):
                self._set_recovery("repair the malformed owned-run registry before enabling writers")
                return False
            if marker is None:
                self._holder = None
                self._recovery_required = False
                self._recovery_reason = ""
                self._cleanup_paths = ()
                self._apply_recovery = None
                return True
            try:
                owner_pid = marker.get("pid")
                owner_nonce = marker.get("nonce")
                claim_id = marker["claim_id"]
                spawn_pending = marker["spawn_pending"]
                raw_child_pids = marker["child_pids"]
                recovery_required = marker.get("recovery_required", False)
                raw_cleanup_paths = marker.get("cleanup_paths", [])
                raw_apply_recovery = marker.get("apply_recovery")
            except (KeyError, TypeError, ValueError):
                self._set_recovery("repair the invalid workspace-writer recovery marker")
                return False
            cleanup_paths = tuple(
                path for raw in raw_cleanup_paths
                if (path := self._valid_cleanup_path(raw)) is not None
            ) if isinstance(raw_cleanup_paths, list) else ()
            if isinstance(owner_pid, bool) or not isinstance(owner_pid, int) or owner_pid <= 0 \
                    or not isinstance(owner_nonce, str) \
                    or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", owner_nonce) is None \
                    or not isinstance(claim_id, str) or re.fullmatch(r"[0-9a-f]{32}", claim_id) is None \
                    or not isinstance(spawn_pending, bool) or not isinstance(raw_child_pids, list) \
                    or any(isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
                           for pid in raw_child_pids) or not isinstance(recovery_required, bool) \
                    or not isinstance(raw_cleanup_paths, list) \
                    or len(cleanup_paths) != len(raw_cleanup_paths) or not claim_id:
                self._set_recovery("repair the invalid workspace-writer spawn marker")
                return False
            try:
                apply_recovery = self._validated_apply_recovery(
                    raw_apply_recovery, owner_pid=owner_pid,
                ) \
                    if raw_apply_recovery is not None else None
            except OwnershipStateError:
                self._set_recovery("repair the invalid workspace apply recovery marker")
                return False
            if owner_pid == self._pid and owner_nonce == self._nonce:
                if self._holder is not None and secrets.compare_digest(
                    self._holder.claim_id, claim_id,
                ):
                    return True
                self._set_recovery("the current process lost its in-memory workspace writer holder")
                return False
            if owner_pid > 0 and pid_alive(owner_pid):
                self._set_recovery("a prior workspace writer is still alive; stop it before retrying")
                return False
            if spawn_pending:
                self._set_recovery("a prior vendor spawn outcome is unknown; reconcile it before retrying")
                return False
            if any(pid_alive(pid) for pid in raw_child_pids):
                self._set_recovery("a prior workspace-writer child is still alive; stop it before retrying")
                return False
            self._cleanup_paths = cleanup_paths
            self._apply_recovery = apply_recovery
            if not allow_clear:
                self._set_recovery(
                    blocked_reason or "another durable recovery boundary prevents writer reconciliation"
                )
                return False
            if apply_recovery is None:
                if recovery_required or cleanup_paths:
                    reason = str(marker.get("recovery_reason") or "") if recovery_required else ""
                    self._set_recovery(
                        reason or "a prior workspace writer requires explicit recovery reconciliation"
                    )
                    return False
                try:
                    removed = self._registry.clear_writer_marker(claim_id)
                except (OwnershipStateError, OSError):
                    removed = False
                if not removed:
                    self._set_recovery("the dead workspace writer marker could not be cleared")
                    return False
            else:
                recovery_reason = str(marker.get("recovery_reason") or "") \
                    if recovery_required else ""
                staged_identities = {
                    self._path_identity(str(item["path"])) for item in apply_recovery["staged"]
                }
                cleanup_identities = {self._path_identity(path) for path in cleanup_paths}
                cleanup_is_journal_subset = cleanup_identities <= staged_identities
                allowed_recovery_reasons = {
                    "workspace apply recovery state was not cleared before release",
                    "staged workspace mutation cleanup could not be proven",
                }
                if not cleanup_is_journal_subset or recovery_required \
                        and recovery_reason not in allowed_recovery_reasons:
                    self._set_recovery(
                        recovery_reason or "workspace marker carries unrelated recovery requirements"
                    )
                    return False
                try:
                    self._reconcile_apply_recovery(claim_id, apply_recovery)
                except (OwnershipStateError, OSError):
                    self._set_recovery(
                        "workspace apply recovery could not prove one exact base or final state"
                    )
                    return False
            self._holder = None
            self._recovery_required = False
            self._recovery_reason = ""
            self._cleanup_paths = ()
            self._apply_recovery = None
            return True


__all__ = [
    "OwnershipRegistry",
    "OwnershipStateError",
    "WRITER_MARKER_KEY",
    "WorkspaceWriterLease",
    "WriterClaim",
]
