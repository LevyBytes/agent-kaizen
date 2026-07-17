"""Provider-neutral, whole-request workspace proposal application.

The approval broker owns immutable before/proposed snapshots and the durable resolution.  This module
owns only the final filesystem apply: capture one exact base, stage replacement bytes beside their
destinations, revalidate every base and staged payload immediately before the first mutation, apply in
request order, and report exact post-state mismatches without attempting rollback.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from .apply_evidence import MAX_MISMATCHES, MAX_PATH_CHARS
from .workspace_path_authority import WorkspacePathAuthority, WorkspacePathError


DENIED_APPROVAL_BODY_INVALID = "DENIED_APPROVAL_BODY_INVALID"
DENIED_APPROVAL_SNAPSHOT_INVALID = "DENIED_APPROVAL_SNAPSHOT_INVALID"
DENIED_APPROVAL_STALE_RERUN_REQUIRED = "DENIED_APPROVAL_STALE_RERUN_REQUIRED"
DENIED_WORKSPACE_RECOVERY_REQUIRED = "DENIED_WORKSPACE_RECOVERY_REQUIRED"
DENIED_WORKSPACE_PROPOSAL_ALREADY_APPLIED = "DENIED_WORKSPACE_PROPOSAL_ALREADY_APPLIED"
DENIED_WORKSPACE_RECOVERY_PERSIST_FAILED = "DENIED_WORKSPACE_RECOVERY_PERSIST_FAILED"
DENIED_TOOL_UNSUPPORTED = "DENIED_TOOL_UNSUPPORTED"
_RECOVERY_CALLBACK_UNSET = object()
_MAX_FILE_BYTES = 8 * 1024 * 1024
_MAX_STAGED_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_CHANGES = 64


class ProposalExecutionError(ValueError):
    """A safe failure while preparing an authoritative proposal."""

    def __init__(
        self,
        code: str,
        *,
        mismatches: Sequence[Mapping[str, Any]] = (),
        required_action: str = "stop this turn and rebuild the approval preview",
    ) -> None:
        super().__init__(code)
        self.code = code
        self.mismatches = tuple(dict(item) for item in mismatches)
        self.required_action = required_action


@dataclass(frozen=True)
class _FileState:
    exists: bool
    sha256: str | None
    size: int | None
    regular: bool
    identity: tuple[int, int] | None = None


@dataclass(frozen=True)
class _Baseline:
    kind: str
    path: str
    old_path: str | None
    target: _FileState
    source: _FileState | None
    final_sha256: str | None


@dataclass(frozen=True)
class _StagedFile:
    temporary: Path
    target: Path
    sha256: str
    size: int
    promote: bool


@dataclass(frozen=True)
class PreparedWorkspaceProposal:
    """One immutable base capture; it may be applied at most once."""

    _executor: "WorkspaceProposalExecutor"
    _proposal_sha256: str
    _baselines: tuple[_Baseline, ...]
    _apply_lock: Any = field(default_factory=Lock, init=False, repr=False, compare=False)
    _applied: bool = field(default=False, init=False, repr=False, compare=False)

    @property
    def baselines(self) -> tuple[Mapping[str, Any], ...]:
        """Content-free hashes/sizes derived from the immutable capture for tests and diagnostics."""

        return tuple(self._executor._baseline_metadata(item) for item in self._baselines)

    def apply(
        self,
        approved: Mapping[str, Any],
        *,
        recovery_callback: Callable[[Mapping[str, Any] | None], bool] | None | object = _RECOVERY_CALLBACK_UNSET,
    ) -> Mapping[str, Any]:
        """Consume this prepared capture once, then apply a hash-matching approved body and return structured OK/DENIED without raising."""
        with self._apply_lock:
            if self._applied:
                return self._executor._denied(
                    DENIED_WORKSPACE_PROPOSAL_ALREADY_APPLIED,
                    False,
                    ({"path": "", "reason": "prepared_proposal_already_applied"},),
                )
            object.__setattr__(self, "_applied", True)
        return self._executor._apply(
            approved, self._proposal_sha256, self._baselines,
            recovery_callback=recovery_callback,
        )


class WorkspaceProposalExecutor:
    """Prepare and apply normalized create/modify/delete/rename requests under one workspace root."""

    def __init__(
        self,
        workspace_root: str | Path,
        recovery_callback: Callable[[Mapping[str, Any] | None], bool] | None = None,
        *,
        path_authority: WorkspacePathAuthority | None = None,
    ) -> None:
        """Normalize the root and authority; apply requires a callable recovery callback here or per call so pre-mutation state can be persisted."""
        root = Path(os.path.abspath(workspace_root))
        if path_authority is not None and os.path.normcase(str(path_authority.workspace_root)) \
                != os.path.normcase(str(root)):
            raise ValueError("workspace path authority has a different root")
        self.workspace_root = root
        self._authority = path_authority or WorkspacePathAuthority(root)
        self._owns_authority = path_authority is None
        self._recovery_callback = recovery_callback

    def close(self) -> None:
        """Closes the authority only when self-created; idempotent (flips _owns_authority)."""
        if self._owns_authority:
            self._authority.close()
            self._owns_authority = False

    def prepare(self, proposal: Mapping[str, Any]) -> PreparedWorkspaceProposal:
        """Capture one immutable validated base per change while enforcing platform and request-wide byte budgets."""
        normalized = self._validated_proposal(proposal)
        if os.name != "nt" and any(
            change["kind"] in ("modify", "delete", "rename")
            for change in normalized["changes"]
        ):
            raise ProposalExecutionError(
                DENIED_TOOL_UNSUPPORTED,
                mismatches=({"path": "", "reason": "exact_removal_unsupported"},),
                required_action="apply existing-file proposals on the supported Windows workspace plane",
            )
        baselines: list[_Baseline] = []
        read_total = 0
        staged_total = sum(
            len(str(change["content"]).encode("utf-8"))
            for change in normalized["changes"] if "content" in change
        )
        for change in normalized["changes"]:
            kind = str(change["kind"])
            path = str(change["path"])
            target = self._path(path, allow_missing=True)
            target_state, read_total, staged_total = self._bounded_prepare_state(
                target,
                read_total=read_total,
                staged_total=staged_total,
                staged_multiplier=1 if kind in ("modify", "delete") else 0,
            )
            old_path: str | None = None
            source_state: _FileState | None = None
            if kind == "create":
                if target_state.exists:
                    raise ProposalExecutionError(
                        DENIED_APPROVAL_STALE_RERUN_REQUIRED,
                        mismatches=(self._state_mismatch(
                            path, _FileState(False, None, None, False), target_state,
                        ),),
                    )
                final_sha256 = self._text_sha256(str(change["content"]))
            elif kind in ("modify", "delete"):
                if not target_state.regular:
                    raise ProposalExecutionError(
                        DENIED_APPROVAL_STALE_RERUN_REQUIRED,
                        mismatches=(self._state_mismatch(path, _FileState(True, None, None, True), target_state),),
                    )
                final_sha256 = self._text_sha256(str(change["content"])) if kind == "modify" else None
            else:
                old_path = str(change["old_path"])
                source = self._path(old_path, allow_missing=True)
                source_state, read_total, staged_total = self._bounded_prepare_state(
                    source,
                    read_total=read_total,
                    staged_total=staged_total,
                    staged_multiplier=1 if "content" in change else 2,
                )
                if not source_state.regular:
                    raise ProposalExecutionError(
                        DENIED_APPROVAL_STALE_RERUN_REQUIRED,
                        mismatches=(self._state_mismatch(
                            old_path, _FileState(True, None, None, True), source_state,
                        ),),
                    )
                if target_state.exists:
                    raise ProposalExecutionError(
                        DENIED_APPROVAL_STALE_RERUN_REQUIRED,
                        mismatches=(self._state_mismatch(
                            path, _FileState(False, None, None, False), target_state,
                        ),),
                    )
                final_sha256 = self._text_sha256(str(change["content"])) \
                    if "content" in change else source_state.sha256
            baselines.append(_Baseline(
                kind=kind,
                path=path,
                old_path=old_path,
                target=target_state,
                source=source_state,
                final_sha256=final_sha256,
            ))
        return PreparedWorkspaceProposal(
            self,
            self._proposal_sha256(normalized),
            tuple(baselines),
        )

    def _apply(
        self,
        approved: Mapping[str, Any],
        proposal_sha256: str,
        baselines: Sequence[_Baseline],
        *,
        recovery_callback: Callable[[Mapping[str, Any] | None], bool] | None | object = _RECOVERY_CALLBACK_UNSET,
    ) -> Mapping[str, Any]:
        """Apply with staging and mutating recovery markers, returning mismatches without rollback."""
        callback = self._recovery_callback \
            if recovery_callback is _RECOVERY_CALLBACK_UNSET else recovery_callback
        try:
            proposal = self._validated_proposal(approved)
        except ProposalExecutionError as error:
            return self._denied(error.code, False, error.mismatches)
        if self._proposal_sha256(proposal) != proposal_sha256 or len(proposal["changes"]) != len(baselines):
            return self._denied(
                DENIED_APPROVAL_BODY_INVALID,
                False,
                ({"path": "", "reason": "approval_input_changed"},),
            )

        staged: list[_StagedFile] = []
        staged_payloads: dict[Path, bytes] = {}
        mutation_started = False
        recovery_phase: str | None = None
        outcome: dict[str, Any] | None = None
        try:
            staged_total = 0
            for change, baseline in zip(proposal["changes"], baselines):
                staged_total += self._planned_stage_bytes(change, baseline)
                if staged_total > _MAX_STAGED_TOTAL_BYTES:
                    raise ProposalExecutionError(
                        DENIED_APPROVAL_SNAPSHOT_INVALID,
                        mismatches=({"path": "", "reason": "proposal_staging_limit_exceeded"},),
                    )
            mismatches = self._base_mismatches(proposal["changes"], baselines)
            if mismatches:
                outcome = self._denied(DENIED_APPROVAL_STALE_RERUN_REQUIRED, False, mismatches)
            else:
                stage_index = 0
                for change, baseline in zip(proposal["changes"], baselines):
                    kind = str(change["kind"])
                    target = self._path(change["path"], allow_missing=True)
                    source_payload: bytes | None = None
                    if kind == "rename":
                        source_payload = self._read_regular(
                            self._path(change["old_path"], allow_missing=False),
                        )
                    if kind != "delete":
                        payload = source_payload if source_payload is not None \
                            and "content" not in change else str(change["content"]).encode("utf-8")
                        payload_hash = hashlib.sha256(payload).hexdigest()
                        if baseline.final_sha256 is None or payload_hash != baseline.final_sha256:
                            raise OSError("proposal staged payload differs from approved bytes")
                        item = _StagedFile(
                            self._staged_path(target, stage_index), target,
                            payload_hash, len(payload), True,
                        )
                        stage_index += 1
                        staged.append(item)
                        staged_payloads[item.temporary] = payload

                    backup_target: Path | None = None
                    backup_state: _FileState | None = None
                    backup_payload: bytes | None = None
                    if kind in ("modify", "delete"):
                        backup_target = target
                        backup_state = baseline.target
                        backup_payload = self._read_regular(target)
                    elif kind == "rename":
                        backup_target = self._path(change["old_path"], allow_missing=False)
                        backup_state = baseline.source
                        backup_payload = source_payload
                    if backup_target is not None and backup_state is not None \
                            and backup_payload is not None:
                        backup_hash = hashlib.sha256(backup_payload).hexdigest()
                        if not backup_state.regular or backup_state.sha256 != backup_hash \
                                or backup_state.size != len(backup_payload):
                            raise OSError("proposal backup differs from approved base")
                        backup = _StagedFile(
                            self._staged_path(backup_target, stage_index), backup_target,
                            backup_hash, len(backup_payload), False,
                        )
                        stage_index += 1
                        staged.append(backup)
                        staged_payloads[backup.temporary] = backup_payload

                if not self._persist_recovery(
                    self._recovery_state("staging", staged, baselines), callback,
                ):
                    outcome = self._denied(
                        DENIED_WORKSPACE_RECOVERY_PERSIST_FAILED,
                        False,
                        ({"path": "", "reason": "recovery_state_persist_failed"},),
                    )
                else:
                    recovery_phase = "staging"
                    for item in staged:
                        self._write_staged(item, staged_payloads[item.temporary])

                    # Last non-mutating checkpoint: rehash every approved base and staged payload.
                    mismatches = self._base_mismatches(proposal["changes"], baselines)
                    if mismatches:
                        outcome = self._denied(DENIED_APPROVAL_STALE_RERUN_REQUIRED, False, mismatches)
                    else:
                        invalid_stage = next((
                            item for item in staged
                            if (state := self._state(item.temporary)).regular is False
                            or state.sha256 != item.sha256 or state.size != item.size
                        ), None)
                        if invalid_stage is not None:
                            outcome = self._denied(
                                DENIED_APPROVAL_SNAPSHOT_INVALID,
                                False,
                                ({"path": self._relative(invalid_stage.target),
                                  "reason": "staged_payload_changed"},),
                            )
                        elif not self._persist_recovery(
                            self._recovery_state("mutating", staged, baselines), callback,
                        ):
                            outcome = self._recovery_denied(False, ())
                        else:
                            recovery_phase = "mutating"
                            by_target = {item.target: item for item in staged if item.promote}
                            for change, baseline in zip(proposal["changes"], baselines):
                                kind = str(change["kind"])
                                target = self._path(change["path"], allow_missing=True)
                                mutation_started = True
                                if kind == "delete":
                                    self._unlink_baseline(target, baseline.target)
                                    continue
                                if kind == "modify":
                                    self._unlink_baseline(target, baseline.target)
                                self._promote_staged(by_target[target])
                                if kind == "rename":
                                    if baseline.source is None:
                                        raise OSError("proposal rename source proof is unavailable")
                                    self._unlink_baseline(
                                        self._path(change["old_path"], allow_missing=False),
                                        baseline.source,
                                    )
                            mismatches = self._safe_final_mismatches(proposal["changes"], baselines)
                            outcome = self._recovery_denied(True, mismatches) if mismatches else {
                                "status": "OK", "partial_apply": False, "mismatches": [],
                            }
        except ProposalExecutionError as error:
            outcome = self._denied(error.code, False, error.mismatches)
        except Exception as error:  # noqa: BLE001 -- only safe structured state reaches provider surfaces
            try:
                partial_apply = mutation_started and bool(
                    self._base_mismatches(proposal["changes"], baselines)
                )
            except Exception:  # noqa: BLE001 -- exact application extent is unavailable
                partial_apply = mutation_started
            exact_rows = [
                item for item in self._safe_final_mismatches(proposal["changes"], baselines)
                if item.get("reason") != "apply_outcome_uncertain"
            ]
            rows = exact_rows
            outcome = self._recovery_denied(
                partial_apply, rows,
                outcome_uncertain=not rows,
            ) if recovery_phase == "mutating" else self._denied(
                DENIED_APPROVAL_STALE_RERUN_REQUIRED,
                partial_apply,
                ({"path": "", "reason": "apply_outcome_uncertain"}, *rows[:MAX_MISMATCHES - 1]),
            )

        cleanup_allowed = recovery_phase != "mutating" \
            or outcome is not None and outcome.get("status") == "OK"
        cleanup_failures = [{
            "path": self._relative(item.temporary), "reason": "staged_cleanup_unproven",
        } for item in staged if cleanup_allowed and not self._cleanup_staged(item)]
        if cleanup_failures:
            known_rows = [
                item for item in self._safe_final_mismatches(proposal["changes"], baselines)
                if item.get("reason") not in ("apply_outcome_uncertain", "staged_cleanup_unproven")
            ]
            outcome = self._recovery_denied(
                bool(outcome and outcome.get("partial_apply")),
                (*cleanup_failures, *known_rows),
            )
        elif recovery_phase is not None:
            targets_still_base = not mutation_started \
                and not self._base_mismatches(proposal["changes"], baselines)
            may_clear = outcome is not None and (
                outcome.get("status") == "OK" or recovery_phase == "staging" and targets_still_base
            )
            if may_clear and self._persist_recovery(None, callback):
                recovery_phase = None
                if outcome.get("code") == DENIED_WORKSPACE_RECOVERY_REQUIRED and not mutation_started:
                    outcome["code"] = DENIED_APPROVAL_STALE_RERUN_REQUIRED
            else:
                prior_rows = list(outcome.get("mismatches") or []) if outcome is not None else []
                outcome = self._recovery_denied(
                    bool(outcome and outcome.get("partial_apply")), prior_rows,
                )
        return outcome or self._recovery_denied(mutation_started, ())

    @staticmethod
    def _denied(
        code: str,
        partial_apply: bool,
        mismatches: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        return {
            "status": "DENIED",
            "code": code,
            "partial_apply": partial_apply,
            "mismatches": [dict(item) for item in mismatches],
        }

    def _recovery_denied(
        self,
        partial_apply: bool,
        rows: Sequence[Mapping[str, Any]],
        *,
        outcome_uncertain: bool = False,
    ) -> dict[str, Any]:
        """Deduplicate rows by canonical JSON, retain uncertainty first, and mark truncation."""
        retained: list[dict[str, Any]] = []
        if outcome_uncertain:
            retained.append({"path": "", "reason": "apply_outcome_uncertain"})
        seen = {
            json.dumps(item, ensure_ascii=True, sort_keys=True, separators=(",", ":")) for item in retained
        }
        overflow = False
        for raw in rows:
            row = dict(raw)
            identity = json.dumps(row, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            if identity in seen:
                continue
            if len(retained) == MAX_MISMATCHES:
                overflow = True
                break
            seen.add(identity)
            retained.append(row)
        if overflow:
            marker = {"path": "", "reason": "mismatch_evidence_overflow"}
            if retained:
                retained[-1] = marker
            else:
                retained.append(marker)
        return self._denied(DENIED_WORKSPACE_RECOVERY_REQUIRED, partial_apply, retained)

    def _persist_recovery(
        self,
        state: Mapping[str, Any] | None,
        callback: Callable[[Mapping[str, Any] | None], bool] | None | object,
    ) -> bool:
        if not callable(callback):
            return False
        try:
            return callback(dict(state) if state is not None else None) is True
        except Exception:  # noqa: BLE001 -- a recovery-marker fault fails closed before mutation
            return False

    def _recovery_state(
        self,
        phase: str,
        staged: Sequence[_StagedFile],
        baselines: Sequence[_Baseline],
    ) -> dict[str, Any]:
        targets: list[dict[str, Any]] = []
        for baseline in baselines:
            final_exists = baseline.kind != "delete"
            targets.append({
                "path": baseline.path,
                "base_exists": baseline.target.regular,
                "base_sha256": baseline.target.sha256 if baseline.target.regular else None,
                "final_exists": final_exists,
                "final_sha256": baseline.final_sha256 if final_exists else None,
            })
            if baseline.old_path is not None and baseline.source is not None:
                targets.append({
                    "path": baseline.old_path,
                    "base_exists": baseline.source.regular,
                    "base_sha256": baseline.source.sha256 if baseline.source.regular else None,
                    "final_exists": False,
                    "final_sha256": None,
                })
        return {
            "phase": phase,
            "staged": [{
                "path": self._relative(item.temporary),
                "sha256": item.sha256,
                "bytes": item.size,
            } for item in staged],
            "targets": targets,
        }

    def _validated_proposal(self, proposal: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(proposal, Mapping):
            raise ProposalExecutionError(DENIED_APPROVAL_BODY_INVALID)
        value = dict(proposal)
        if set(value) - {"summary", "changes"}:
            raise ProposalExecutionError(DENIED_APPROVAL_BODY_INVALID)
        summary = value.get("summary")
        if summary is not None and (not isinstance(summary, str) or not summary.strip()):
            raise ProposalExecutionError(DENIED_APPROVAL_BODY_INVALID)
        changes = value.get("changes")
        if not isinstance(changes, list) or not changes or len(changes) > _MAX_CHANGES:
            raise ProposalExecutionError(DENIED_APPROVAL_BODY_INVALID)
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        explicit_content_bytes = 0
        for raw in changes:
            if not isinstance(raw, Mapping):
                raise ProposalExecutionError(DENIED_APPROVAL_BODY_INVALID)
            change = dict(raw)
            kind = change.get("kind")
            allowed = {"kind", "path", "content"} if kind in ("create", "modify") else \
                {"kind", "path"} if kind == "delete" else \
                {"kind", "old_path", "path", "content"} if kind == "rename" else set()
            required = {"kind", "path", "content"} if kind in ("create", "modify") else \
                {"kind", "path"} if kind == "delete" else \
                {"kind", "old_path", "path"} if kind == "rename" else set()
            if not allowed or set(change) - allowed or not required.issubset(change):
                raise ProposalExecutionError(DENIED_APPROVAL_BODY_INVALID)
            path = self._relative(self._path(change.get("path"), allow_missing=True))
            path_identity = os.path.normcase(path)
            if path_identity in seen:
                raise ProposalExecutionError(DENIED_APPROVAL_BODY_INVALID)
            seen.add(path_identity)
            clean: dict[str, Any] = {"kind": kind, "path": path}
            if kind == "rename":
                old_path = self._relative(self._path(change.get("old_path"), allow_missing=True))
                old_identity = os.path.normcase(old_path)
                if old_identity == path_identity or old_identity in seen:
                    raise ProposalExecutionError(DENIED_APPROVAL_BODY_INVALID)
                seen.add(old_identity)
                clean["old_path"] = old_path
            if "content" in change:
                if not isinstance(change["content"], str):
                    raise ProposalExecutionError(DENIED_APPROVAL_BODY_INVALID)
                content_bytes = len(change["content"].encode("utf-8"))
                if content_bytes > _MAX_FILE_BYTES:
                    raise ProposalExecutionError(DENIED_APPROVAL_BODY_INVALID)
                explicit_content_bytes += content_bytes
                if explicit_content_bytes > _MAX_STAGED_TOTAL_BYTES:
                    raise ProposalExecutionError(
                        DENIED_APPROVAL_SNAPSHOT_INVALID,
                        mismatches=({"path": "", "reason": "proposal_staging_limit_exceeded"},),
                    )
                clean["content"] = change["content"]
            normalized.append(clean)
        result: dict[str, Any] = {"changes": normalized}
        if summary is not None:
            result["summary"] = summary.strip()
        return result

    def _base_mismatches(
        self,
        changes: Sequence[Mapping[str, Any]],
        baselines: Sequence[_Baseline],
    ) -> list[dict[str, Any]]:
        """Recomputes exact current state per change; rows where kind/path/old_path or on-disk identity diverged from baseline."""
        mismatches: list[dict[str, Any]] = []
        if len(changes) != len(baselines):
            return [{"path": "", "reason": "approval_input_changed"}]
        for change, baseline in zip(changes, baselines):
            if change.get("kind") != baseline.kind or change.get("path") != baseline.path \
                    or change.get("old_path") != baseline.old_path:
                mismatches.append({"path": str(change.get("path") or ""), "reason": "approval_input_changed"})
                continue
            try:
                target = self._path(baseline.path, allow_missing=True)
                target_state = self._state(target)
                if target_state != baseline.target:
                    mismatches.append(self._state_mismatch(baseline.path, baseline.target, target_state))
                if baseline.old_path is not None and baseline.source is not None:
                    source = self._path(baseline.old_path, allow_missing=True)
                    source_state = self._state(source)
                    if source_state != baseline.source:
                        mismatches.append(self._state_mismatch(
                            baseline.old_path, baseline.source, source_state,
                        ))
            except (OSError, ValueError, ProposalExecutionError):
                mismatches.append(self._unreadable_mismatch(baseline.path))
        return mismatches

    def _final_mismatches(
        self,
        changes: Sequence[Mapping[str, Any]],
        baselines: Sequence[_Baseline],
    ) -> list[dict[str, Any]]:
        mismatches: list[dict[str, Any]] = []
        for change, baseline in zip(changes, baselines):
            try:
                target = self._path(baseline.path, allow_missing=True)
                actual = self._state(target)
            except Exception:  # noqa: BLE001 -- unreadable state is not fabricated as missing
                mismatches.append(self._unreadable_mismatch(baseline.path))
                actual = None
            expected_exists = baseline.kind != "delete"
            expected_hash = baseline.final_sha256 if expected_exists else None
            expected = _FileState(expected_exists, expected_hash, None, expected_exists)
            if actual is not None and (
                actual.exists != expected.exists or actual.regular != expected.regular
                or (expected.regular and actual.sha256 != expected.sha256)
            ):
                mismatches.append(self._state_mismatch(
                    baseline.path, expected, actual, reason="final_state_mismatch",
                ))
            if baseline.old_path is not None:
                try:
                    old_actual = self._state(self._path(baseline.old_path, allow_missing=True))
                except Exception:  # noqa: BLE001 -- unreadable state is not fabricated as missing
                    mismatches.append(self._unreadable_mismatch(baseline.old_path))
                    old_actual = None
                if old_actual is not None and old_actual.exists:
                    mismatches.append(self._state_mismatch(
                        baseline.old_path,
                        _FileState(False, None, None, False),
                        old_actual,
                        reason="final_state_mismatch",
                    ))
        return mismatches

    def _safe_final_mismatches(
        self,
        changes: Sequence[Mapping[str, Any]],
        baselines: Sequence[_Baseline],
    ) -> list[dict[str, Any]]:
        try:
            return self._final_mismatches(changes, baselines)
        except Exception:  # noqa: BLE001 -- executor errors remain bounded structured outcomes
            return [{"path": "", "reason": "apply_outcome_uncertain"}]

    @staticmethod
    def _unreadable_mismatch(path: str) -> dict[str, Any]:
        return {"path": path, "reason": "target_state_unreadable"}

    def _path(self, raw: Any, *, allow_missing: bool) -> Path:
        if not isinstance(raw, str) or not raw or len(raw) > 1_024 or "\x00" in raw:
            raise ProposalExecutionError(DENIED_APPROVAL_BODY_INVALID)
        relative = Path(raw)
        if relative.is_absolute() or any(part in ("", ".", "..") or ":" in part for part in relative.parts):
            raise ProposalExecutionError(DENIED_APPROVAL_BODY_INVALID)
        try:
            candidate = self.workspace_root.joinpath(*relative.parts)
            candidate.relative_to(self.workspace_root)
        except ValueError:
            raise ProposalExecutionError(DENIED_APPROVAL_BODY_INVALID) from None
        return candidate

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.workspace_root).as_posix()

    def _state(self, path: Path) -> _FileState:
        relative = self._relative(path)
        try:
            if self._authority.identity(relative) is None:
                return _FileState(False, None, None, False)
            stable = self._authority.read(relative, _MAX_FILE_BYTES)
        except WorkspacePathError:
            return _FileState(True, None, None, False)
        return _FileState(
            True, stable.sha256, stable.size, True,
            (stable.identity.device, stable.identity.inode),
        )

    def _bounded_prepare_state(
        self,
        path: Path,
        *,
        read_total: int,
        staged_total: int,
        staged_multiplier: int,
    ) -> tuple[_FileState, int, int]:
        """Read one base only when both request-wide I/O and staging budgets still admit it."""

        relative = self._relative(path)
        try:
            identity = self._authority.identity(relative)
            if identity is None:
                return _FileState(False, None, None, False), read_total, staged_total
            if identity.size > _MAX_FILE_BYTES \
                    or read_total + identity.size > _MAX_STAGED_TOTAL_BYTES \
                    or staged_total + identity.size * staged_multiplier > _MAX_STAGED_TOTAL_BYTES:
                raise ProposalExecutionError(
                    DENIED_APPROVAL_SNAPSHOT_INVALID,
                    mismatches=({"path": relative, "reason": "proposal_staging_limit_exceeded"},),
                )
            stable = self._authority.read(relative, _MAX_FILE_BYTES)
            if stable.identity != identity:
                return _FileState(True, None, None, False), read_total + identity.size, staged_total
        except ProposalExecutionError:
            raise
        except WorkspacePathError:
            return _FileState(True, None, None, False), read_total, staged_total
        return (
            _FileState(
                True, stable.sha256, stable.size, True,
                (stable.identity.device, stable.identity.inode),
            ),
            read_total + stable.size,
            staged_total + stable.size * staged_multiplier,
        )

    @staticmethod
    def _planned_stage_bytes(change: Mapping[str, Any], baseline: _Baseline) -> int:
        """Pre-mutation staging estimate per kind; raises SNAPSHOT_INVALID when a required base size is absent."""
        kind = str(change["kind"])
        proposed = len(str(change["content"]).encode("utf-8")) if "content" in change else 0
        if kind == "create":
            return proposed
        if kind in ("modify", "delete"):
            if baseline.target.size is None:
                raise ProposalExecutionError(DENIED_APPROVAL_SNAPSHOT_INVALID)
            return baseline.target.size + proposed
        if baseline.source is None or baseline.source.size is None:
            raise ProposalExecutionError(DENIED_APPROVAL_SNAPSHOT_INVALID)
        return baseline.source.size + (proposed if "content" in change else baseline.source.size)

    def _read_regular(self, path: Path) -> bytes:
        try:
            return self._authority.read(self._relative(path), _MAX_FILE_BYTES).data
        except WorkspacePathError as error:
            raise OSError("proposal source is not a bounded regular file") from error

    def _write_staged(self, item: _StagedFile, content: bytes) -> None:
        try:
            stable = self._authority.create_exact_exclusive(
                self._relative(item.temporary), content, max_bytes=_MAX_FILE_BYTES,
            )
        except WorkspacePathError as error:
            raise OSError("proposal staged payload could not be created safely") from error
        if stable.sha256 != item.sha256 or stable.size != item.size:
            raise OSError("proposal staged payload proof differs from journal")

    def _staged_path(self, target: Path, index: int) -> Path:
        temporary = target.parent / (
            f".{target.name}.kaizen-{os.getpid()}-{index}-{secrets.token_hex(12)}.tmp"
        )
        if len(self._relative(temporary)) > MAX_PATH_CHARS:
            raise OSError("staged recovery path exceeds the durable evidence bound")
        return temporary

    def _cleanup_staged(self, item: _StagedFile) -> bool:
        relative = self._relative(item.temporary)
        try:
            if self._authority.identity(relative) is None:
                return True
            return self._authority.unlink_exact(
                relative, sha256=item.sha256, size=item.size, max_bytes=_MAX_FILE_BYTES,
            )
        except WorkspacePathError:
            return False

    def _promote_staged(self, item: _StagedFile) -> None:
        try:
            self._authority.replace_from_exact(
                self._relative(item.temporary), self._relative(item.target),
                staged_sha256=item.sha256,
                staged_size=item.size,
                expected_target=None,
                max_bytes=_MAX_FILE_BYTES,
            )
        except WorkspacePathError as error:
            raise OSError("proposal no-clobber promotion failed") from error

    def _unlink_baseline(self, path: Path, baseline: _FileState) -> None:
        if not baseline.regular or baseline.sha256 is None or baseline.size is None:
            raise OSError("proposal removal proof is unavailable")
        try:
            removed = self._authority.unlink_exact(
                self._relative(path), sha256=baseline.sha256,
                size=baseline.size, max_bytes=_MAX_FILE_BYTES,
            )
        except WorkspacePathError as error:
            raise OSError("proposal exact removal failed") from error
        if not removed:
            raise OSError("proposal exact removal target disappeared")

    @staticmethod
    def _text_sha256(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _proposal_sha256(proposal: Mapping[str, Any]) -> str:
        encoded = json.dumps(
            proposal, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _state_mismatch(
        path: str,
        expected: _FileState,
        actual: _FileState,
        *,
        reason: str = "base_changed",
    ) -> dict[str, Any]:
        if actual.exists and (not actual.regular or actual.sha256 is None):
            return WorkspaceProposalExecutor._unreadable_mismatch(path)
        if expected.exists and (not expected.regular or expected.sha256 is None):
            return {"path": path, "reason": reason}
        return {
            "path": path,
            "reason": reason,
            "expected_exists": expected.exists,
            "actual_exists": actual.exists,
            "expected_sha256": expected.sha256 if expected.exists else None,
            "actual_sha256": actual.sha256 if actual.exists else None,
        }

    @staticmethod
    def _baseline_metadata(baseline: _Baseline) -> dict[str, Any]:
        value: dict[str, Any] = {
            "kind": baseline.kind,
            "path": baseline.path,
            "target_exists": baseline.target.regular,
            "target_sha256": baseline.target.sha256,
            "target_bytes": baseline.target.size,
            "final_sha256": baseline.final_sha256,
        }
        if baseline.old_path is not None and baseline.source is not None:
            value.update({
                "old_path": baseline.old_path,
                "source_exists": baseline.source.regular,
                "source_sha256": baseline.source.sha256,
                "source_bytes": baseline.source.size,
            })
        return value


__all__ = [
    "DENIED_APPROVAL_BODY_INVALID",
    "DENIED_APPROVAL_SNAPSHOT_INVALID",
    "DENIED_APPROVAL_STALE_RERUN_REQUIRED",
    "DENIED_WORKSPACE_RECOVERY_REQUIRED",
    "DENIED_TOOL_UNSUPPORTED",
    "PreparedWorkspaceProposal",
    "ProposalExecutionError",
    "WorkspaceProposalExecutor",
]
