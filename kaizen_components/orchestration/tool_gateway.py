"""Provider-neutral, workspace-bounded tools for driven model clients.

The gateway is deliberately independent of any vendor SDK.  A provider adapter supplies the frozen
policy decision function, durable approval broker, mutation guard, and (for approved file proposals)
the daemon-owned executor.  Model input is data: every argument is validated before policy, broker,
process, or filesystem mutation seams are reached.
"""

from __future__ import annotations

import fnmatch
import codecs
import hashlib
import json
import os
import re
import subprocess
import stat
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import children, policy
from .adapters import BrokerApprovalResult, check_mutation_guard, resolve_broker_result
from .apply_evidence import MAX_MISMATCHES, normalize_apply_evidence
from .proposal_executor import ProposalExecutionError


READ_MAX_BYTES = 256 * 1024
READ_MAX_LINES = 2_000
READ_CHUNK_BYTES = 64 * 1024
LIST_MAX_DEPTH = 8
LIST_MAX_ENTRIES = 500
LIST_MAX_SCANNED = 10_000
SEARCH_MAX_QUERY_BYTES = 4 * 1024
SEARCH_MAX_FILE_BYTES = 256 * 1024
SEARCH_MAX_TOTAL_BYTES = 8 * 1024 * 1024
SEARCH_MAX_RESULTS = 200
SEARCH_MAX_FILES = 2_000
SEARCH_MAX_PREVIEW_CODEPOINTS = 500
PROCESS_MAX_ARGS = 64
PROCESS_MAX_ARG_BYTES = 4 * 1024
PROCESS_MAX_COMMAND_BYTES = 32 * 1024
PROCESS_DEFAULT_TIMEOUT_MS = 120_000
PROCESS_MAX_TIMEOUT_MS = 1_800_000
PROCESS_MAX_OUTPUT_BYTES = 256 * 1024
STRUCTURED_OUTPUT_MAX_BYTES = 256 * 1024
PROPOSAL_MAX_CHANGES = 64
PROPOSAL_MAX_SIDE_BYTES = 8 * 1024 * 1024
PROPOSAL_MAX_TOTAL_BYTES = 64 * 1024 * 1024

READ_FILE = "kaizen_read_file"
LIST_FILES = "kaizen_list_files"
SEARCH_TEXT = "kaizen_search_text"
RUN_PROCESS = "kaizen_run_process"
PROPOSE_CHANGES = "kaizen_propose_changes"
TOOL_NAMES = (READ_FILE, LIST_FILES, SEARCH_TEXT, RUN_PROCESS, PROPOSE_CHANGES)

_FATAL_APPROVAL_CODES = frozenset({
    "DENIED_APPROVAL_BODY_INVALID",
    "DENIED_APPROVAL_SNAPSHOT_INVALID",
    "DENIED_APPROVAL_STALE_RERUN_REQUIRED",
    "DENIED_APPROVAL_TIMEOUT",
    "ERROR_TOOL_APPROVAL_TRANSPORT",
})
_CONTROL_OK = frozenset((9, 10, 13))
_APPLY_EXTENTS = frozenset(("none", "complete", "partial", "uncertain"))
_REGEX_TIMEOUT_SECONDS = 3.0
_REGEX_WORKER = """import json,re,sys
value=json.load(sys.stdin)
flags=0 if value['case_sensitive'] else re.IGNORECASE
expression=re.compile(value['query'],flags)
matches=[]
truncated=False
maximum=value['maximum']
for document in value['documents']:
    for line_number,line in enumerate(document['text'].splitlines(),start=1):
        match=expression.search(line)
        if match is None:
            continue
        matches.append({'path':document['path'],'line':line_number,'column':match.start()+1,'preview':line[:__PREVIEW_LIMIT__]})
        if len(matches)>=maximum:
            truncated=True
            break
    if len(matches)>=maximum:
        break
json.dump({'matches':matches,'truncated':truncated},sys.stdout,ensure_ascii=True,separators=(',',':'))
""".replace("__PREVIEW_LIMIT__", str(SEARCH_MAX_PREVIEW_CODEPOINTS))


PolicyDecider = Callable[[policy.RequestedAction, int], policy.Decision]
ApprovalBroker = Callable[[Mapping[str, Any]], Any]
ProposalExecutor = Callable[[Mapping[str, Any]], Mapping[str, Any]]
OwnedSpawner = Callable[..., children.OwnedChild]


@dataclass(frozen=True)
class ToolContext:
    actor: policy.Actor
    current_epoch: int
    tool_call_id: str


@dataclass(frozen=True)
class _BrokerOutcome:
    result: BrokerApprovalResult
    code: str | None = None
    fatal: bool = False


@dataclass
class _ActiveProcess:
    child: children.OwnedChild
    owner_thread_id: int
    canceled: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)


class _BoundedOutput:
    """Drain both pipes completely while retaining one shared bounded prefix."""

    def __init__(self, maximum: int) -> None:
        self._maximum = maximum
        self._remaining = maximum
        self._lock = threading.Lock()
        self._chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
        self.truncated = False
        self.failed = False

    def drain(self, stream: Any, channel: str) -> None:
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(8 * 1024)
                if not chunk:
                    break
                encoded = str(chunk).encode("utf-8", errors="replace")
                with self._lock:
                    retained = encoded[:self._remaining]
                    if retained:
                        self._chunks[channel].append(retained)
                        self._remaining -= len(retained)
                    if len(retained) != len(encoded):
                        self.truncated = True
        except Exception:  # noqa: BLE001 -- pipe errors are reported without provider-facing details
            self.failed = True

    def text(self, channel: str) -> str:
        with self._lock:
            return b"".join(self._chunks[channel]).decode("utf-8", errors="ignore")


class ToolGatewayError(ValueError):
    """A safe structured tool denial; exception text is never a result surface."""

    def __init__(
        self,
        code: str,
        *,
        fatal: bool = False,
        status: str = "DENIED",
        retryable: bool = False,
        required_action: str = "correct the tool request and retry",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.fatal = fatal
        self.status = status
        self.retryable = retryable
        self.required_action = required_action
        self.details = dict(details or {})

    def payload(self, tool_name: str) -> dict[str, Any]:
        payload = {
            "status": self.status,
            "tool": tool_name,
            "code": self.code,
            "fatal": self.fatal,
            "retryable": self.retryable,
            "required_action": self.required_action,
        }
        payload.update(self.details)
        return payload


class ToolGateway:
    """Five bounded Kaizen tools with injectable authority and execution collaborators."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        decide: PolicyDecider,
        approval_broker: ApprovalBroker | None = None,
        mutation_guard: Callable[[Any], Mapping[str, Any] | None] | None = None,
        proposal_executor: ProposalExecutor | None = None,
        recovery_callback_factory: Callable[
            [], Callable[[Mapping[str, Any] | None], bool] | None
        ] | None = None,
        spawner: OwnedSpawner = children.spawn_owned,
    ) -> None:
        self.workspace_root = Path(os.path.abspath(workspace_root))
        if not self.workspace_root.is_dir() or self._is_reparse(self.workspace_root):
            raise ValueError("workspace root must be a non-reparse directory")
        self._decide = decide
        self._approval_broker = approval_broker
        self._mutation_guard = mutation_guard
        self._proposal_executor = proposal_executor
        self._recovery_callback_factory = recovery_callback_factory
        self._spawner = spawner
        self._active_lock = threading.RLock()
        self._cancel_lock = threading.Lock()
        self._active_processes: dict[int, _ActiveProcess] = {}

    def _register_process(self, child: children.OwnedChild) -> _ActiveProcess:
        active = _ActiveProcess(child=child, owner_thread_id=threading.get_ident())
        with self._active_lock:
            self._active_processes[id(child)] = active
        return active

    def _complete_process(self, active: _ActiveProcess, *, termination_proven: bool) -> None:
        active.done.set()
        if termination_proven:
            with self._active_lock:
                self._active_processes.pop(id(active.child), None)

    @staticmethod
    def _close_process_streams(child: children.OwnedChild) -> None:
        for stream in (child.process.stdin, child.process.stdout, child.process.stderr):
            if stream is not None and not getattr(stream, "closed", True):
                try:
                    stream.close()
                except OSError:
                    pass

    def cancel_active_processes(self, timeout: float = 5.0) -> dict[str, Any]:
        """Cancel and join all governed process tools; uncertainty remains registered fail-closed."""

        with self._cancel_lock:
            deadline = time.monotonic() + max(0.0, timeout)
            with self._active_lock:
                active = list(self._active_processes.values())
            proven = True
            for item in active:
                item.canceled.set()
                remaining = max(0.0, deadline - time.monotonic())
                try:
                    item.child.kill_tree(timeout=remaining)
                except Exception:
                    proven = False
            current = threading.get_ident()
            for item in active:
                if item.owner_thread_id != current:
                    item.done.wait(max(0.0, deadline - time.monotonic()))
                stopped = item.child.poll() is not None
                joined = item.done.is_set() or item.owner_thread_id == current
                proven = proven and stopped and joined
                if stopped and joined:
                    with self._active_lock:
                        self._active_processes.pop(id(item.child), None)
            with self._active_lock:
                remaining_count = len(self._active_processes)
        return {
            "termination_proven": proven and remaining_count == 0,
            "active_count": remaining_count,
        }

    def close(self) -> bool:
        """Release proposal-owned path authority after permanent adapter teardown."""

        close = getattr(self._proposal_executor, "close", None)
        if not callable(close):
            return True
        try:
            close()
            return True
        except Exception:
            return False

    def execute(self, name: str, args: Any, context: ToolContext) -> dict[str, Any]:
        """Validate and execute one model-requested tool without leaking raw exceptions."""

        try:
            if name == READ_FILE:
                result = self._read_file(args, context)
            elif name == LIST_FILES:
                result = self._list_files(args, context)
            elif name == SEARCH_TEXT:
                result = self._search_text(args, context)
            elif name == RUN_PROCESS:
                result = self._run_process(args, context)
            elif name == PROPOSE_CHANGES:
                result = self._propose_changes(args, context)
            else:
                raise ToolGatewayError("DENIED_TOOL_UNKNOWN", required_action="use an advertised Kaizen tool")
            if name == PROPOSE_CHANGES:
                result = {**result, **normalize_apply_evidence(result)}
            return {"status": "OK", "tool": name, "fatal": False, "result": result}
        except ToolGatewayError as error:
            payload = error.payload(name)
            if name == PROPOSE_CHANGES:
                payload.update(normalize_apply_evidence(payload))
            return payload
        except Exception:  # noqa: BLE001 -- provider-facing tool failures never expose exception text
            return ToolGatewayError(
                "ERROR_TOOL_GATEWAY",
                fatal=True,
                status="ERROR",
                required_action="stop this turn and inspect the Kaizen tool gateway",
            ).payload(name)

    # -- common validation and policy ------------------------------------------------------

    @staticmethod
    def _mapping(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ToolGatewayError("DENIED_TOOL_INPUT_INVALID")
        return dict(value)

    @staticmethod
    def _exact_keys(value: Mapping[str, Any], allowed: set[str], required: set[str] = frozenset()) -> None:
        if set(value) - allowed or not required.issubset(value):
            raise ToolGatewayError("DENIED_TOOL_INPUT_INVALID")

    @staticmethod
    def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ToolGatewayError("DENIED_TOOL_INPUT_INVALID")
        return value

    @staticmethod
    def _is_reparse(path: Path) -> bool:
        try:
            info = path.lstat()
        except OSError:
            return False
        return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400)

    def _path(self, raw: Any, *, allow_missing: bool = False) -> tuple[str, Path]:
        if not isinstance(raw, str) or not raw or len(raw) > 1_024 or "\x00" in raw:
            raise ToolGatewayError("DENIED_TOOL_PATH_INVALID")
        relative = Path(raw)
        if relative.is_absolute() or any(part == ".." for part in relative.parts):
            raise ToolGatewayError("DENIED_TOOL_PATH_INVALID")
        candidate = self.workspace_root.joinpath(relative)
        current = self.workspace_root
        for component in relative.parts:
            if component in ("", "."):
                continue
            current = current / component
            if self._is_reparse(current):
                raise ToolGatewayError("DENIED_TOOL_PATH_REPARSE")
        try:
            resolved = candidate.resolve(strict=not allow_missing)
            resolved.relative_to(self.workspace_root)
        except (OSError, ValueError):
            raise ToolGatewayError("DENIED_TOOL_PATH_INVALID") from None
        normalized = resolved.relative_to(self.workspace_root).as_posix()
        return normalized if normalized else ".", resolved

    @staticmethod
    def _text(content: bytes, *, code: str = "DENIED_TOOL_BINARY") -> str:
        if b"\x00" in content:
            raise ToolGatewayError(code)
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            raise ToolGatewayError(code) from None
        if any(ord(char) < 32 and ord(char) not in _CONTROL_OK for char in text):
            raise ToolGatewayError(code)
        return text

    def _action(
        self,
        context: ToolContext,
        verb: str,
        *,
        targets: Sequence[str] = (),
        command: str | None = None,
        tool: str,
    ) -> policy.RequestedAction:
        return policy.RequestedAction(
            actor=context.actor,
            verb=verb,
            targets=tuple(targets),
            command=command,
            raw={"cwd": str(self.workspace_root), "tool": tool, "tool_call_id": context.tool_call_id},
        )

    @staticmethod
    def _bounded_items(items: Sequence[Mapping[str, Any]], maximum: int) -> tuple[list[dict[str, Any]], bool]:
        selected: list[dict[str, Any]] = []
        used = 2  # enclosing JSON array
        for item in items[:maximum]:
            clean = dict(item)
            encoded = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if used + len(encoded) + (1 if selected else 0) > STRUCTURED_OUTPUT_MAX_BYTES:
                return selected, True
            selected.append(clean)
            used += len(encoded) + (1 if len(selected) > 1 else 0)
        return selected, len(items) > len(selected)

    def _decision(self, action: policy.RequestedAction, context: ToolContext) -> policy.Decision:
        try:
            decision = self._decide(action, context.current_epoch)
        except Exception:
            raise ToolGatewayError(
                "ERROR_TOOL_POLICY",
                fatal=True,
                status="ERROR",
                required_action="stop this turn and repair the policy decision seam",
            ) from None
        if not isinstance(decision, policy.Decision) or decision.result not in (policy.ALLOW, policy.ASK, policy.DENY):
            raise ToolGatewayError("ERROR_TOOL_POLICY", fatal=True, status="ERROR")
        if decision.result == policy.DENY:
            raise ToolGatewayError(
                decision.invariant_id or decision.rule_id or "DENIED_TOOL_POLICY",
                required_action="choose an action permitted by the frozen session policy",
            )
        return decision

    def _broker(self, request: Mapping[str, Any]) -> _BrokerOutcome:
        """Invoke the durable approval broker exactly once; normalize legacy strings, typed BrokerApprovalResult, and `{decision,code,fatal}` mappings into a fail-closed _BrokerOutcome; any off-contract shape -> denied (DENIED_APPROVAL_BODY_INVALID, fatal). Codes in _FATAL_APPROVAL_CODES or explicit fatal=True terminalize the turn."""
        if self._approval_broker is None:
            return _BrokerOutcome(BrokerApprovalResult("denied"), "ERROR_TOOL_APPROVAL_TRANSPORT", True)
        try:
            raw = self._approval_broker(dict(request))
        except Exception:
            return _BrokerOutcome(BrokerApprovalResult("denied"), "ERROR_TOOL_APPROVAL_TRANSPORT", True)
        if isinstance(raw, Mapping) and not isinstance(raw, BrokerApprovalResult):
            decision = raw.get("decision")
            code = raw.get("code")
            if decision == "denied" and isinstance(code, str):
                return _BrokerOutcome(
                    BrokerApprovalResult("denied"),
                    code,
                    bool(raw.get("fatal")) or code in _FATAL_APPROVAL_CODES,
                )
            return _BrokerOutcome(BrokerApprovalResult("denied"), "DENIED_APPROVAL_BODY_INVALID", True)
        if raw not in ("approved", "denied") and not isinstance(raw, BrokerApprovalResult):
            return _BrokerOutcome(BrokerApprovalResult("denied"), "DENIED_APPROVAL_BODY_INVALID", True)
        normalized = resolve_broker_result(lambda _request: raw, request)
        return _BrokerOutcome(normalized, None, False)

    @staticmethod
    def _broker_denial(outcome: _BrokerOutcome) -> None:
        if outcome.result.decision == "approved":
            return
        if outcome.fatal:
            raise ToolGatewayError(
                outcome.code or "ERROR_TOOL_APPROVAL_TRANSPORT",
                fatal=True,
                required_action="stop this turn and refresh the approval state",
            )
        raise ToolGatewayError(
            outcome.code or "DENIED_TOOL_REJECTED",
            fatal=False,
            required_action="respect the rejection and choose another action",
        )

    def _mutation_denial(self, action: policy.RequestedAction) -> None:
        denial = check_mutation_guard(self._mutation_guard, action)
        if denial is not None:
            raise ToolGatewayError(
                str(denial.get("code") or "DENIED_WORKSPACE_RECOVERY_REQUIRED"),
                fatal=True,
                retryable=bool(denial.get("retryable")),
                required_action=str(denial.get("required_action") or "reconcile the workspace writer"),
            )

    # -- read/list/search ------------------------------------------------------------------

    def _read_file(self, raw_args: Any, context: ToolContext) -> dict[str, Any]:
        args = self._mapping(raw_args)
        self._exact_keys(args, {"path", "start_line", "end_line", "max_bytes"}, {"path"})
        relative, target = self._path(args["path"])
        if not target.is_file() or self._is_reparse(target):
            raise ToolGatewayError("DENIED_TOOL_PATH_INVALID")
        self._decision(self._action(context, "file_read", targets=(relative,), tool=READ_FILE), context)
        max_bytes = self._bounded_int(args.get("max_bytes"), READ_MAX_BYTES, 1, READ_MAX_BYTES)
        start = self._positive_int(args.get("start_line"), 1)
        raw_end = args.get("end_line")
        requested_end = self._positive_int(raw_end, start + READ_MAX_LINES - 1)
        if requested_end < start:
            raise ToolGatewayError("DENIED_TOOL_INPUT_INVALID")
        if requested_end - start + 1 > READ_MAX_LINES:
            raise ToolGatewayError("DENIED_TOOL_TOO_LARGE")
        selected, total_lines, digest = self._stream_text_range(
            target,
            start_line=start,
            end_line=requested_end,
            max_bytes=max_bytes,
        )
        if start > max(1, total_lines) or raw_end is not None and requested_end > total_lines:
            raise ToolGatewayError("DENIED_TOOL_INPUT_INVALID")
        end = requested_end if raw_end is not None else min(total_lines, requested_end)
        if total_lines == 0:
            end = 0
        return {
            "path": relative,
            "sha256": digest,
            "text": selected,
            "start_line": start,
            "end_line": end,
            "total_lines": total_lines,
            "truncated": bool(total_lines and (start != 1 or end != total_lines)),
        }

    @staticmethod
    def _positive_int(value: Any, default: int) -> int:
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ToolGatewayError("DENIED_TOOL_INPUT_INVALID")
        return value

    def _stream_text_range(
        self,
        target: Path,
        *,
        start_line: int,
        end_line: int,
        max_bytes: int,
    ) -> tuple[str, int, str]:
        """Validate/hash the whole regular UTF-8 file while retaining only the requested bounded range."""

        digest = hashlib.sha256()
        selected = bytearray()
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        line_number = 1
        completed_lines = 0
        current_has_content = False
        pending_cr = False
        split_characters = frozenset(("\n", "\x85", "\u2028", "\u2029"))

        def append_selected(character: str) -> None:
            if not start_line <= line_number <= end_line:
                return
            encoded = character.encode("utf-8")
            if len(selected) + len(encoded) > max_bytes:
                raise ToolGatewayError("DENIED_TOOL_TOO_LARGE")
            selected.extend(encoded)

        def finish_line() -> None:
            nonlocal line_number, completed_lines, current_has_content
            completed_lines += 1
            line_number += 1
            current_has_content = False

        def consume(text: str) -> None:
            nonlocal current_has_content, pending_cr
            for character in text:
                codepoint = ord(character)
                if codepoint < 32 and codepoint not in _CONTROL_OK:
                    raise ToolGatewayError("DENIED_TOOL_BINARY")
                if pending_cr:
                    if character == "\n":
                        append_selected(character)
                        finish_line()
                        pending_cr = False
                        continue
                    finish_line()
                    pending_cr = False
                append_selected(character)
                current_has_content = True
                if character == "\r":
                    pending_cr = True
                elif character in split_characters:
                    finish_line()

        try:
            before_path = os.lstat(target)
            if stat.S_ISLNK(before_path.st_mode) or not stat.S_ISREG(before_path.st_mode) \
                    or bool(getattr(before_path, "st_file_attributes", 0) & 0x400):
                raise ToolGatewayError("DENIED_TOOL_PATH_REPARSE")
            with target.open("rb") as handle:
                before = os.fstat(handle.fileno())
                while True:
                    chunk = handle.read(READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    digest.update(chunk)
                    consume(decoder.decode(chunk, final=False))
                consume(decoder.decode(b"", final=True))
                after = os.fstat(handle.fileno())
        except ToolGatewayError:
            raise
        except UnicodeDecodeError:
            raise ToolGatewayError("DENIED_TOOL_BINARY") from None
        except OSError:
            raise ToolGatewayError("DENIED_TOOL_READ_FAILED") from None
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after:
            raise ToolGatewayError("DENIED_TOOL_READ_FAILED")
        if pending_cr:
            finish_line()
        elif current_has_content:
            finish_line()
        return selected.decode("utf-8"), completed_lines, digest.hexdigest()

    def _walk(self, root: Path, *, max_depth: int, scan_limit: int) -> tuple[list[Path], bool]:
        pending = deque([(root, 0)])
        found: list[Path] = []
        scanned = 0
        truncated = False
        while pending:
            directory, depth = pending.popleft()
            try:
                entries = sorted(directory.iterdir(), key=lambda item: (item.name.casefold(), item.name))
            except OSError:
                raise ToolGatewayError("DENIED_TOOL_READ_FAILED") from None
            for entry in entries:
                scanned += 1
                if scanned > scan_limit:
                    truncated = True
                    return found, truncated
                if self._is_reparse(entry):
                    raise ToolGatewayError("DENIED_TOOL_PATH_REPARSE")
                found.append(entry)
                if entry.is_dir() and depth < max_depth:
                    pending.append((entry, depth + 1))
        return found, truncated

    def _list_files(self, raw_args: Any, context: ToolContext) -> dict[str, Any]:
        args = self._mapping(raw_args)
        self._exact_keys(args, {"path", "glob", "max_depth", "max_entries"})
        relative, target = self._path(args.get("path", "."))
        if not target.is_dir():
            raise ToolGatewayError("DENIED_TOOL_PATH_INVALID")
        self._decision(self._action(context, "file_read", targets=(relative,), tool=LIST_FILES), context)
        max_depth = self._bounded_int(args.get("max_depth"), LIST_MAX_DEPTH, 0, LIST_MAX_DEPTH)
        max_entries = self._bounded_int(args.get("max_entries"), LIST_MAX_ENTRIES, 1, LIST_MAX_ENTRIES)
        pattern = args.get("glob")
        if pattern is not None and (not isinstance(pattern, str) or not pattern or len(pattern) > 1_024):
            raise ToolGatewayError("DENIED_TOOL_INPUT_INVALID")
        paths, scan_truncated = self._walk(target, max_depth=max_depth, scan_limit=LIST_MAX_SCANNED)
        entries: list[dict[str, Any]] = []
        for item in paths:
            item_relative = item.relative_to(self.workspace_root).as_posix()
            if pattern is not None and not fnmatch.fnmatchcase(item_relative, pattern):
                continue
            entry: dict[str, Any] = {"path": item_relative, "type": "directory" if item.is_dir() else "file"}
            if item.is_file():
                try:
                    entry["bytes"] = item.stat().st_size
                except OSError:
                    raise ToolGatewayError("DENIED_TOOL_READ_FAILED") from None
            entries.append(entry)
        entries.sort(key=lambda item: (str(item["path"]).casefold(), str(item["path"])))
        selected, output_truncated = self._bounded_items(entries, max_entries)
        truncated = scan_truncated or output_truncated
        return {"path": relative, "entries": selected, "truncated": truncated}

    def _search_text(self, raw_args: Any, context: ToolContext) -> dict[str, Any]:
        args = self._mapping(raw_args)
        self._exact_keys(
            args,
            {"query", "path", "mode", "glob", "case_sensitive", "max_results"},
            {"query"},
        )
        query = args["query"]
        if not isinstance(query, str) or not query or len(query.encode("utf-8")) > SEARCH_MAX_QUERY_BYTES:
            raise ToolGatewayError("DENIED_TOOL_INPUT_INVALID")
        mode = args.get("mode", "literal")
        case_sensitive = args.get("case_sensitive", False)
        if mode not in ("literal", "regex") or not isinstance(case_sensitive, bool):
            raise ToolGatewayError("DENIED_TOOL_INPUT_INVALID")
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            expression = re.compile(re.escape(query) if mode == "literal" else query, flags)
        except re.error:
            raise ToolGatewayError("DENIED_TOOL_REGEX_INVALID") from None
        relative, target = self._path(args.get("path", "."))
        if not (target.is_dir() or target.is_file()):
            raise ToolGatewayError("DENIED_TOOL_PATH_INVALID")
        self._decision(self._action(context, "file_read", targets=(relative,), tool=SEARCH_TEXT), context)
        maximum = self._bounded_int(args.get("max_results"), SEARCH_MAX_RESULTS, 1, SEARCH_MAX_RESULTS)
        pattern = args.get("glob")
        if pattern is not None and (not isinstance(pattern, str) or not pattern or len(pattern) > 1_024):
            raise ToolGatewayError("DENIED_TOOL_INPUT_INVALID")
        if target.is_file():
            candidates, walk_truncated = [target], False
        else:
            walked, walk_truncated = self._walk(
                target, max_depth=LIST_MAX_DEPTH, scan_limit=LIST_MAX_SCANNED,
            )
            candidates = [item for item in walked if item.is_file()]
        candidates_with_paths = [
            (candidate, candidate.relative_to(self.workspace_root).as_posix())
            for candidate in candidates
        ]
        candidates_with_paths.sort(key=lambda item: (item[1].casefold(), item[1]))
        results: list[dict[str, Any]] = []
        bytes_scanned = 0
        files_scanned = 0
        skipped_binary = 0
        skipped_oversize = 0
        truncated = walk_truncated
        regex_documents: list[dict[str, str]] = []
        for candidate, rel in candidates_with_paths:
            if files_scanned >= SEARCH_MAX_FILES or len(results) >= maximum:
                truncated = True
                break
            if pattern is not None and not fnmatch.fnmatchcase(rel, pattern):
                continue
            try:
                size = candidate.stat().st_size
            except OSError:
                raise ToolGatewayError("DENIED_TOOL_READ_FAILED") from None
            if size > SEARCH_MAX_FILE_BYTES or bytes_scanned + size > SEARCH_MAX_TOTAL_BYTES:
                skipped_oversize += 1
                truncated = True
                continue
            try:
                content = candidate.read_bytes()
                text = self._text(content)
            except ToolGatewayError as error:
                if error.code != "DENIED_TOOL_BINARY":
                    raise
                skipped_binary += 1
                continue
            except OSError:
                raise ToolGatewayError("DENIED_TOOL_READ_FAILED") from None
            files_scanned += 1
            bytes_scanned += len(content)
            if mode == "regex":
                regex_documents.append({"path": rel, "text": text})
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                match = expression.search(line)
                if match is None:
                    continue
                preview = line[:SEARCH_MAX_PREVIEW_CODEPOINTS]
                results.append({
                    "path": rel,
                    "line": line_number,
                    "column": match.start() + 1,
                    "preview": preview,
                })
                if len(results) >= maximum:
                    truncated = True
                    break
        if mode == "regex":
            results, regex_truncated = self._isolated_regex_search(
                query, case_sensitive=case_sensitive, documents=regex_documents, maximum=maximum,
            )
            truncated = truncated or regex_truncated
        selected, output_truncated = self._bounded_items(results, maximum)
        return {
            "path": relative,
            "mode": mode,
            "matches": selected,
            "truncated": truncated or output_truncated,
            "files_scanned": files_scanned,
            "bytes_scanned": bytes_scanned,
            "skipped_binary": skipped_binary,
            "skipped_oversize": skipped_oversize,
        }

    def _isolated_regex_search(
        self,
        query: str,
        *,
        case_sensitive: bool,
        documents: Sequence[Mapping[str, str]],
        maximum: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Security-critical isolation boundary: runs the user regex in a fresh `-I` isolated Python child with scrubbed env, DEVNULL stderr, bounded timeout; fail-closed on non-proven termination (DENIED_WORKSPACE_RECOVERY_REQUIRED), cancellation, timeout, nonzero exit, oversized/invalid JSON, or off-schema match rows. Returns validated (path,line,column,preview) rows + truncated flag."""
        payload = json.dumps(
            {
                "query": query,
                "case_sensitive": case_sensitive,
                "documents": list(documents),
                "maximum": maximum,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            child = self._spawner(
                [sys.executable, "-I", "-c", _REGEX_WORKER],
                cwd=str(self.workspace_root),
                env=self._prepare_process_env(),
                stdin=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            raise ToolGatewayError(
                "ERROR_TOOL_REGEX_RUNTIME",
                fatal=True,
                status="ERROR",
                required_action="stop this turn and inspect isolated regex execution",
            ) from None
        active = self._register_process(child)
        termination_proven = True
        timed_out = False
        stdout = ""
        try:
            stdout, _stderr = child.process.communicate(input=payload, timeout=_REGEX_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                child.kill_tree()
            except Exception:
                termination_proven = False
        except Exception:
            termination_proven = child.poll() is not None
        finally:
            if child.poll() is not None:
                try:
                    child.release()
                except Exception:
                    termination_proven = False
            else:
                termination_proven = False
            if termination_proven:
                self._close_process_streams(child)
            self._complete_process(active, termination_proven=termination_proven)
        if not termination_proven:
            raise ToolGatewayError(
                "DENIED_WORKSPACE_RECOVERY_REQUIRED",
                fatal=True,
                required_action="stop this turn; isolated regex termination was not proven",
            )
        if active.canceled.is_set():
            raise ToolGatewayError(
                "DENIED_TOOL_PROCESS_CANCELED",
                fatal=True,
                required_action="stop this turn; tool cancellation was requested",
            )
        if timed_out:
            raise ToolGatewayError(
                "DENIED_TOOL_REGEX_TIMEOUT",
                required_action="use literal mode or a simpler bounded regular expression",
            )
        if child.process.returncode != 0 or len(str(stdout).encode("utf-8")) > STRUCTURED_OUTPUT_MAX_BYTES * 2:
            raise ToolGatewayError("DENIED_TOOL_REGEX_INVALID")
        try:
            decoded = json.loads(str(stdout))
        except (TypeError, ValueError):
            raise ToolGatewayError("DENIED_TOOL_REGEX_INVALID") from None
        if not isinstance(decoded, Mapping) or not isinstance(decoded.get("matches"), list) \
                or not isinstance(decoded.get("truncated"), bool):
            raise ToolGatewayError("DENIED_TOOL_REGEX_INVALID")
        matches: list[dict[str, Any]] = []
        for item in decoded["matches"]:
            if not isinstance(item, Mapping) or set(item) != {"path", "line", "column", "preview"}:
                raise ToolGatewayError("DENIED_TOOL_REGEX_INVALID")
            if not isinstance(item["path"], str) or not isinstance(item["preview"], str) \
                    or not isinstance(item["line"], int) or not isinstance(item["column"], int):
                raise ToolGatewayError("DENIED_TOOL_REGEX_INVALID")
            matches.append(dict(item))
        return matches, bool(decoded["truncated"])

    # -- direct argv process ---------------------------------------------------------------

    def _prepare_process_env(self) -> dict[str, str]:
        """Create the private runtime directory and return the scrubbed child environment."""
        runtime = self.workspace_root / "AI" / "work" / "orchestration" / "tool-runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        keep = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "SystemDrive")
        base = {key: os.environ[key] for key in keep if key in os.environ}
        runtime_text = str(runtime)
        base.update({
            "HOME": runtime_text,
            "USERPROFILE": runtime_text,
            "APPDATA": runtime_text,
            "LOCALAPPDATA": runtime_text,
            "TEMP": runtime_text,
            "TMP": runtime_text,
            "TMPDIR": runtime_text,
        })
        return children.compose_child_env(base)

    def _run_process(self, raw_args: Any, context: ToolContext) -> dict[str, Any]:
        args = self._mapping(raw_args)
        self._exact_keys(args, {"executable", "argv", "cwd", "timeout_ms"}, {"executable"})
        executable = args["executable"]
        if not isinstance(executable, str) or not executable or "\x00" in executable \
                or len(executable.encode("utf-8")) > PROCESS_MAX_ARG_BYTES:
            raise ToolGatewayError("DENIED_TOOL_INPUT_INVALID")
        argv = args.get("argv", [])
        if not isinstance(argv, list) or len(argv) > PROCESS_MAX_ARGS \
                or any(not isinstance(item, str) or "\x00" in item
                       or len(item.encode("utf-8")) > PROCESS_MAX_ARG_BYTES for item in argv):
            raise ToolGatewayError("DENIED_TOOL_INPUT_INVALID")
        command = [executable, *argv]
        command_text = subprocess.list2cmdline(command)
        if len(command_text.encode("utf-8")) > PROCESS_MAX_COMMAND_BYTES:
            raise ToolGatewayError("DENIED_TOOL_TOO_LARGE")
        relative_cwd, cwd = self._path(args.get("cwd", "."))
        if not cwd.is_dir():
            raise ToolGatewayError("DENIED_TOOL_PATH_INVALID")
        timeout_ms = self._bounded_int(
            args.get("timeout_ms"), PROCESS_DEFAULT_TIMEOUT_MS, 1_000, PROCESS_MAX_TIMEOUT_MS,
        )
        action = self._action(
            context,
            "exec",
            targets=(relative_cwd,),
            command=command_text,
            tool=RUN_PROCESS,
        )
        decision = self._decision(action, context)
        if decision.result == policy.ASK:
            outcome = self._broker({
                "correlation_id": decision.correlation_hash,
                "request_type": "tool_approval",
                "summary": f"Run {Path(executable).name}",
                "body": {"tool": RUN_PROCESS, "executable": executable, "argv": list(argv),
                         "cwd": relative_cwd, "timeout_ms": timeout_ms},
            })
            self._broker_denial(outcome)
        self._mutation_denial(action)
        try:
            child = self._spawner(command, cwd=str(cwd), env=self._prepare_process_env())
        except Exception:
            raise ToolGatewayError(
                "ERROR_TOOL_PROCESS_SPAWN",
                fatal=True,
                status="ERROR",
                required_action="stop this turn and inspect process ownership",
            ) from None
        active = self._register_process(child)
        output = _BoundedOutput(PROCESS_MAX_OUTPUT_BYTES)
        drainers = [
            threading.Thread(
                target=output.drain,
                args=(child.process.stdout, "stdout"),
                name=f"kaizen-tool-stdout-{child.pid}",
                daemon=True,
            ),
            threading.Thread(
                target=output.drain,
                args=(child.process.stderr, "stderr"),
                name=f"kaizen-tool-stderr-{child.pid}",
                daemon=True,
            ),
        ]
        for drainer in drainers:
            drainer.start()
        timed_out = False
        termination_proven = True
        try:
            child.process.wait(timeout=timeout_ms / 1000.0)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                child.kill_tree()
            except Exception:
                termination_proven = False
        finally:
            if child.poll() is not None:
                try:
                    child.release()
                except Exception:
                    termination_proven = False
            else:
                termination_proven = False
            if termination_proven:
                for drainer in drainers:
                    drainer.join(timeout=1.0)
                if any(drainer.is_alive() for drainer in drainers):
                    termination_proven = False
                else:
                    self._close_process_streams(child)
            self._complete_process(active, termination_proven=termination_proven)
        if active.canceled.is_set():
            raise ToolGatewayError(
                "DENIED_TOOL_PROCESS_CANCELED" if termination_proven
                else "DENIED_WORKSPACE_RECOVERY_REQUIRED",
                fatal=True,
                required_action="stop this turn; governed process cancellation was requested",
                details={"effects_unknown": True, "timed_out": False},
            )
        if timed_out or not termination_proven:
            raise ToolGatewayError(
                "DENIED_TOOL_PROCESS_TIMEOUT" if timed_out and termination_proven
                else "DENIED_WORKSPACE_RECOVERY_REQUIRED",
                fatal=True,
                required_action="stop this turn; the process timed out or termination was not proven",
                details={"effects_unknown": True, "timed_out": timed_out},
            )
        if output.failed:
            raise ToolGatewayError(
                "ERROR_TOOL_PROCESS_IO",
                fatal=True,
                status="ERROR",
                required_action="stop this turn and inspect process output handling",
                details={"effects_unknown": True, "timed_out": False},
            )
        exit_code = child.process.returncode
        return {
            "exit_code": int(exit_code) if isinstance(exit_code, int) else -1,
            "stdout": output.text("stdout"),
            "stderr": output.text("stderr"),
            "timed_out": False,
            "truncated": output.truncated,
            "effects_unknown": True,
            "policy_decision": decision.result,
        }

    # -- whole-request proposals -----------------------------------------------------------

    def _normalize_proposal(self, raw_args: Any) -> tuple[dict[str, Any], list[dict[str, Any]], tuple[str, ...]]:
        args = self._mapping(raw_args)
        self._exact_keys(args, {"summary", "changes"}, {"changes"})
        summary = args.get("summary", "Review model-proposed workspace changes")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 256:
            raise ToolGatewayError("DENIED_TOOL_INPUT_INVALID")
        changes = args["changes"]
        if not isinstance(changes, list) or not 1 <= len(changes) <= PROPOSAL_MAX_CHANGES:
            raise ToolGatewayError("DENIED_TOOL_INPUT_INVALID")
        normalized: list[dict[str, Any]] = []
        diff_changes: list[dict[str, Any]] = []
        targets: list[str] = []
        seen: set[str] = set()
        total = 0
        for raw in changes:
            change = self._mapping(raw)
            kind = change.get("kind")
            if kind in ("create", "modify"):
                self._exact_keys(change, {"kind", "path", "content"}, {"kind", "path", "content"})
            elif kind == "delete":
                self._exact_keys(change, {"kind", "path"}, {"kind", "path"})
            elif kind == "rename":
                self._exact_keys(change, {"kind", "old_path", "path", "content"},
                                 {"kind", "old_path", "path"})
            else:
                raise ToolGatewayError("DENIED_TOOL_INPUT_INVALID")
            path, _target = self._path(change.get("path"), allow_missing=True)
            if path in seen:
                raise ToolGatewayError("DENIED_TOOL_INPUT_INVALID")
            seen.add(path)
            targets.append(path)
            clean: dict[str, Any] = {"kind": kind, "path": path}
            diff: dict[str, Any] = {"kind": kind, "path": path}
            if kind == "rename":
                old_path, _source = self._path(change.get("old_path"), allow_missing=True)
                if old_path == path or old_path in seen:
                    raise ToolGatewayError("DENIED_TOOL_INPUT_INVALID")
                seen.add(old_path)
                targets.append(old_path)
                clean["old_path"] = old_path
                diff["old_path"] = old_path
            content = change.get("content")
            if kind in ("create", "modify") or content is not None:
                if not isinstance(content, str):
                    raise ToolGatewayError("DENIED_TOOL_INPUT_INVALID")
                size = len(content.encode("utf-8"))
                if size > PROPOSAL_MAX_SIDE_BYTES:
                    raise ToolGatewayError("DENIED_TOOL_TOO_LARGE")
                total += size
                if total > PROPOSAL_MAX_TOTAL_BYTES:
                    raise ToolGatewayError("DENIED_TOOL_TOO_LARGE")
                clean["content"] = content
                diff["proposed_content"] = content
            normalized.append(clean)
            diff_changes.append(diff)
        return {"summary": summary.strip(), "changes": normalized}, diff_changes, tuple(targets)

    @staticmethod
    def _validate_rebased_proposal(
        original: Mapping[str, Any],
        approved: Mapping[str, Any],
        revision: int | None,
    ) -> bool:
        """Allow only revision-2+ content replacements on the same ordered modify request."""

        if approved == original:
            return False
        if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 1 \
                or approved.get("summary") != original.get("summary"):
            raise ToolGatewayError("DENIED_APPROVAL_BODY_INVALID", fatal=True)
        before = original.get("changes")
        after = approved.get("changes")
        if not isinstance(before, list) or not isinstance(after, list) or len(before) != len(after):
            raise ToolGatewayError("DENIED_APPROVAL_BODY_INVALID", fatal=True)
        changed = False
        for expected, actual in zip(before, after):
            if not isinstance(expected, Mapping) or not isinstance(actual, Mapping) \
                    or set(expected) != set(actual) or expected.get("kind") != "modify":
                raise ToolGatewayError("DENIED_APPROVAL_BODY_INVALID", fatal=True)
            if any(actual.get(key) != value for key, value in expected.items() if key != "content"):
                raise ToolGatewayError("DENIED_APPROVAL_BODY_INVALID", fatal=True)
            changed = changed or actual.get("content") != expected.get("content")
        if not changed:
            raise ToolGatewayError("DENIED_APPROVAL_BODY_INVALID", fatal=True)
        return True

    @staticmethod
    def _approved_contract(result: BrokerApprovalResult, change_count: int) -> tuple[dict[str, Any], ...] | None:
        fields = (result.approval_revision, result.snapshot_set_sha256, result.approved_bases)
        if all(value is None for value in fields):
            return None
        revision, snapshot_hash, raw_bases = fields
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1 \
                or not isinstance(snapshot_hash, str) or re.fullmatch(r"[0-9a-f]{64}", snapshot_hash) is None \
                or not isinstance(raw_bases, tuple) or len(raw_bases) != change_count:
            raise ToolGatewayError("DENIED_APPROVAL_BODY_INVALID", fatal=True)
        bases: list[dict[str, Any]] = []
        for raw in raw_bases:
            if not isinstance(raw, Mapping):
                raise ToolGatewayError("DENIED_APPROVAL_BODY_INVALID", fatal=True)
            value = dict(raw)
            kind = value.get("kind")
            keys = {
                "kind", "path", "target_exists", "target_sha256", "target_bytes", "final_sha256",
            }
            if kind == "rename":
                keys.update({"old_path", "source_exists", "source_sha256", "source_bytes"})
            if kind not in ("create", "modify", "delete", "rename") or set(value) != keys \
                    or not isinstance(value.get("path"), str) or not value["path"] \
                    or not isinstance(value.get("target_exists"), bool):
                raise ToolGatewayError("DENIED_APPROVAL_BODY_INVALID", fatal=True)
            if kind == "rename" and (not isinstance(value.get("old_path"), str)
                                     or not isinstance(value.get("source_exists"), bool)):
                raise ToolGatewayError("DENIED_APPROVAL_BODY_INVALID", fatal=True)
            for name in ("target_sha256", "final_sha256", "source_sha256"):
                if name in value and value[name] is not None \
                        and (not isinstance(value[name], str)
                             or re.fullmatch(r"[0-9a-f]{64}", value[name]) is None):
                    raise ToolGatewayError("DENIED_APPROVAL_BODY_INVALID", fatal=True)
            for name in ("target_bytes", "source_bytes"):
                if name in value and value[name] is not None \
                        and (isinstance(value[name], bool) or not isinstance(value[name], int) or value[name] < 0):
                    raise ToolGatewayError("DENIED_APPROVAL_BODY_INVALID", fatal=True)
            bases.append(value)
        return tuple(bases)

    @staticmethod
    def _contract_mismatches(
        expected: Sequence[Mapping[str, Any]],
        actual: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        mismatches: list[dict[str, Any]] = []
        maximum = max(len(expected), len(actual))
        for index in range(maximum):
            wanted = expected[index] if index < len(expected) else {}
            found = actual[index] if index < len(actual) else {}
            if dict(wanted) != dict(found):
                mismatches.append({
                    "path": str(wanted.get("path") or found.get("path") or ""),
                    "reason": "approved_base_changed",
                })
        return mismatches

    def _propose_changes(self, raw_args: Any, context: ToolContext) -> dict[str, Any]:
        proposal, diff_changes, targets = self._normalize_proposal(raw_args)
        action = self._action(context, "file_write", targets=targets, tool=PROPOSE_CHANGES)
        decision = self._decision(action, context)
        if self._proposal_executor is None:
            raise ToolGatewayError(
                "DENIED_TOOL_UNSUPPORTED",
                fatal=True,
                required_action="bind the authoritative daemon diff executor before proposing changes",
            )
        prepared_executor: Any = None
        prepare = getattr(self._proposal_executor, "prepare", None)
        if callable(prepare):
            try:
                prepared_executor = prepare(proposal)
            except ProposalExecutionError as error:
                raise ToolGatewayError(
                    error.code,
                    fatal=True,
                    required_action=error.required_action,
                    details={"partial_apply": False, "mismatches": list(error.mismatches)},
                ) from None
        outcome = self._broker({
            "correlation_id": decision.correlation_hash,
            "request_type": "tool_approval",
            "summary": proposal["summary"],
            "negotiated": True,
            "diff_request": {"tool_name": PROPOSE_CHANGES, "tool_input": proposal,
                             "changes": diff_changes, "updated_input": proposal},
        })
        self._broker_denial(outcome)
        approved = outcome.result
        if approved.post_apply is None:
            raise ToolGatewayError(
                "DENIED_APPROVAL_BODY_INVALID",
                fatal=True,
                required_action="stop this turn and repair the negotiated proposal executor",
            )
        preapply_failure: ToolGatewayError | None = None
        normalized_approved = proposal
        try:
            approved_input = approved.updated_input if approved.updated_input is not None else proposal
            try:
                normalized_approved, _diff_approved, _targets_approved = self._normalize_proposal(approved_input)
            except ToolGatewayError:
                raise ToolGatewayError(
                    "DENIED_APPROVAL_BODY_INVALID",
                    fatal=True,
                    required_action="stop this turn and rebuild the approval preview",
                ) from None
            rebased = self._validate_rebased_proposal(
                proposal, normalized_approved, approved.approval_revision,
            )
            contract = self._approved_contract(approved, len(normalized_approved["changes"]))
            if rebased and contract is None:
                raise ToolGatewayError("DENIED_APPROVAL_BODY_INVALID", fatal=True)
            if contract is not None:
                if not callable(prepare):
                    raise ToolGatewayError("DENIED_APPROVAL_BODY_INVALID", fatal=True)
                try:
                    rebound = prepare(normalized_approved)
                except ProposalExecutionError as error:
                    raise ToolGatewayError(
                        error.code,
                        fatal=True,
                        required_action=error.required_action,
                        details={"partial_apply": False, "mismatches": list(error.mismatches)},
                    ) from None
                actual_bases = getattr(rebound, "baselines", None)
                if not isinstance(actual_bases, tuple) \
                        or not all(isinstance(item, Mapping) for item in actual_bases):
                    raise ToolGatewayError("DENIED_APPROVAL_BODY_INVALID", fatal=True)
                mismatches = self._contract_mismatches(contract, actual_bases)
                if mismatches:
                    raise ToolGatewayError(
                        "DENIED_APPROVAL_STALE_RERUN_REQUIRED",
                        fatal=True,
                        required_action="stop this turn because approved bases changed before apply",
                        details={"partial_apply": False, "mismatches": mismatches},
                    )
                prepared_executor = rebound
        except ToolGatewayError as error:
            preapply_failure = ToolGatewayError(
                error.code,
                fatal=True,
                status=error.status,
                retryable=error.retryable,
                required_action=error.required_action,
                details={"partial_apply": False, **error.details},
            )
        if preapply_failure is None:
            try:
                self._mutation_denial(action)
            except ToolGatewayError as error:
                preapply_failure = ToolGatewayError(
                    error.code,
                    fatal=True,
                    status=error.status,
                    retryable=error.retryable,
                    required_action=error.required_action,
                    details={"partial_apply": False, **error.details},
                )
        recovery_callback: Callable[[Mapping[str, Any] | None], bool] | None = None
        if preapply_failure is None and self._recovery_callback_factory is not None:
            try:
                recovery_callback = self._recovery_callback_factory()
            except Exception:
                recovery_callback = None
            if not callable(recovery_callback) or prepared_executor is None:
                preapply_failure = ToolGatewayError(
                    "DENIED_WORKSPACE_RECOVERY_REQUIRED",
                    fatal=True,
                    required_action="bind the exact current writer claim before workspace apply",
                    details={"partial_apply": False},
                )
        apply_result: Mapping[str, Any] | None = None
        apply_failed = False
        apply_raised = False
        if preapply_failure is not None:
            apply_result = {
                "status": "DENIED", "code": preapply_failure.code, "partial_apply": False,
                "mismatches": list(preapply_failure.details.get("mismatches") or []),
            }
            apply_failed = True
        else:
            try:
                if prepared_executor is not None:
                    if recovery_callback is not None:
                        apply_result = prepared_executor.apply(
                            normalized_approved, recovery_callback=recovery_callback,
                        )
                    else:
                        apply_result = prepared_executor.apply(normalized_approved)
                else:
                    apply_result = self._proposal_executor(normalized_approved)
                if not isinstance(apply_result, Mapping) or apply_result.get("status") != "OK":
                    apply_failed = True
            except Exception:
                apply_failed = True
                apply_raised = True
        try:
            audit = approved.post_apply()
        except Exception:
            audit = {
                "status": "DENIED",
                "apply_extent": "uncertain",
                "partial_apply": True,
                "mismatches": [{"path": "", "reason": "post_apply_audit_unavailable"}],
            }
        if isinstance(audit, Mapping) and "apply_extent" in audit:
            audit_extent = str(audit.get("apply_extent")) \
                if audit.get("apply_extent") in _APPLY_EXTENTS else "uncertain"
        elif isinstance(audit, Mapping) and isinstance(audit.get("partial_apply"), bool):
            audit_extent = "partial" if audit["partial_apply"] else (
                "complete" if audit.get("status") == "OK" else "none"
            )
        else:
            audit_extent = "uncertain"
        audit_failed = not isinstance(audit, Mapping) or audit.get("status") != "OK" \
            or audit_extent != "complete"
        if apply_failed or audit_failed:
            apply_mismatches = list(apply_result.get("mismatches") or []) \
                if isinstance(apply_result, Mapping) else []
            audit_mismatches = list(audit.get("mismatches") or []) if isinstance(audit, Mapping) else []
            uncertain_reasons = {
                "apply_outcome_uncertain", "mismatch_evidence_invalid", "mismatch_evidence_missing",
                "mismatch_evidence_overflow", "post_apply_audit_unavailable",
                "staged_cleanup_unproven", "target_state_unreadable",
            }
            priority: list[Any] = []
            remaining: list[Any] = []
            for item in apply_mismatches:
                target = priority if isinstance(item, Mapping) and (
                    not item.get("path") or item.get("reason") in uncertain_reasons
                ) else remaining
                target.append(item)
            merged: list[dict[str, Any]] = []
            seen_rows: set[str] = set()
            # Preserve uncertainty sentinels first, post-state audit truth second, and remaining
            # apply details last; MAX_MISMATCHES truncates only the lower-priority tail.
            for item in (*priority, *audit_mismatches, *remaining):
                if not isinstance(item, Mapping):
                    continue
                row = dict(item)
                try:
                    identity = json.dumps(row, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
                except (TypeError, ValueError):
                    row = {"path": "", "reason": "mismatch_evidence_invalid"}
                    identity = json.dumps(row, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
                if identity in seen_rows:
                    continue
                seen_rows.add(identity)
                merged.append(row)
                if len(merged) == MAX_MISMATCHES:
                    break
            mismatches = merged
            if audit_extent == "uncertain" and not any(
                not item.get("path") or item.get("reason") in uncertain_reasons
                for item in mismatches if isinstance(item, Mapping)
            ):
                mismatches = [
                    {"path": "", "reason": "apply_outcome_uncertain"},
                    *mismatches[:MAX_MISMATCHES - 1],
                ]
            if apply_raised or not isinstance(apply_result, Mapping):
                # An exception can escape before, during, or after a mutation. Even a clean post-state
                # audit cannot prove the executor made no unmodeled side effect, so retain bounded audit
                # rows but mark the overall application extent explicitly uncertain.
                exact_rows = [
                    item for item in mismatches
                    if isinstance(item, Mapping) and item.get("reason") != "apply_outcome_uncertain"
                ]
                mismatches = [
                    {"path": "", "reason": "apply_outcome_uncertain"},
                    *exact_rows[:MAX_MISMATCHES - 1],
                ]
            apply_extent = audit_extent
            executor_uncertain = apply_raised or not isinstance(apply_result, Mapping)
            unstructured_partial = prepared_executor is None \
                and isinstance(apply_result, Mapping) \
                and apply_result.get("partial_apply") is True
            proven_no_mutation = preapply_failure is not None or (
                prepared_executor is not None
                and isinstance(apply_result, Mapping)
                and apply_result.get("code") == "DENIED_APPROVAL_STALE_RERUN_REQUIRED"
                and apply_result.get("partial_apply") is False
            )
            if executor_uncertain:
                apply_extent = "uncertain"
            elif proven_no_mutation:
                apply_extent = "none"
            elif unstructured_partial and audit_extent in ("none", "complete"):
                apply_extent = "partial"
            partial_apply = apply_extent in ("partial", "uncertain")
            code = str(apply_result.get("code")) \
                if isinstance(apply_result, Mapping) and isinstance(apply_result.get("code"), str) \
                else "DENIED_APPROVAL_STALE_RERUN_REQUIRED"
            raise ToolGatewayError(
                code,
                fatal=True,
                status=preapply_failure.status if preapply_failure is not None else "DENIED",
                retryable=preapply_failure.retryable if preapply_failure is not None else False,
                required_action=preapply_failure.required_action if preapply_failure is not None
                else "stop this turn; inspect exact partial-apply mismatches and rerun",
                details={
                    "apply_extent": apply_extent,
                    "partial_apply": partial_apply,
                    "mismatches": mismatches,
                },
            ) from None
        # Sole success path: application and complete post-state audit both passed without mismatches.
        return {
            "applied": True,
            "partial_apply": False,
            "mismatches": [],
            "change_count": len(normalized_approved["changes"]),
            "executor_status": "OK",
        }


__all__ = [
    "LIST_FILES",
    "PROPOSE_CHANGES",
    "READ_FILE",
    "RUN_PROCESS",
    "SEARCH_TEXT",
    "TOOL_NAMES",
    "ToolContext",
    "ToolGateway",
    "ToolGatewayError",
]
