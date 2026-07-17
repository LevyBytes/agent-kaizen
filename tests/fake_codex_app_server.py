"""Hermetic stdlib Codex app-server double for v8 compatibility and H2.3 logical sessions."""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from pathlib import Path

_LOCK = threading.Lock()


def _emit(value: dict) -> None:
    with _LOCK:
        sys.stdout.write(json.dumps(value) + "\n")
        sys.stdout.flush()


def _log(enabled: bool, message: str) -> None:
    if enabled:
        print(f"[fake-codex] {message}", file=sys.stderr, flush=True)


def _method_schema(methods: list[str]) -> dict:
    return {
        "oneOf": [{"type": "object", "properties": {"method": {"enum": [method]}}}
                  for method in methods]
    }


def _generate_schema(argv: list[str]) -> int:
    """Writes ClientRequest/ServerRequest/v2 schema JSON under --out; --incompatible-schema toggles bounded-access variant; returns 0."""
    if "--out" not in argv or argv.index("--out") == len(argv) - 1:
        raise SystemExit("generate-json-schema requires --out <directory>")
    out_value = argv[argv.index("--out") + 1]
    out = Path(out_value)
    (out / "v2").mkdir(parents=True, exist_ok=True)
    client_methods = [
        "model/list", "config/read", "hooks/list", "thread/start", "turn/start", "turn/steer",
        "turn/interrupt", "command/exec", "thread/shellCommand", "windowsSandbox/readiness",
    ]
    server_methods = [
        "item/commandExecution/requestApproval", "item/fileChange/requestApproval",
        "item/permissions/requestApproval", "item/tool/requestUserInput",
        "mcpServer/elicitation/request", "execCommandApproval", "applyPatchApproval",
    ]
    bounded = "--incompatible-schema" not in argv
    read_only = {"type": {"enum": ["readOnly"]}, "networkAccess": {"type": "boolean"}}
    workspace = {"type": {"enum": ["workspaceWrite"]}, "networkAccess": {"type": "boolean"}}
    if bounded:
        read_only["access"] = {"type": "object"}
        workspace["readOnlyAccess"] = {"type": "object"}
    schemas = {
        out / "ClientRequest.json": _method_schema(client_methods),
        out / "ServerRequest.json": _method_schema(server_methods),
        out / "v2" / "TurnStartParams.json": {
            "definitions": {"SandboxPolicy": {"oneOf": [
                {"properties": read_only}, {"properties": workspace},
            ]}},
        },
        out / "v2" / "HooksListResponse.json": {"definitions": {
            "HookEventName": {"enum": ["preToolUse"]},
            "HookHandlerType": {"enum": ["command"]},
        }},
    }
    for path, value in schemas.items():
        path.write_text(json.dumps(value), encoding="utf-8")
    return 0


class FakeServer:
    """MISSED by audit — one-line: stateful JSON-RPC scenario driver; per-scenario turn/approval choreography over stdin/stdout."""
    def __init__(self, version: str, scenario: str, debug: bool) -> None:
        self.version = version
        self.scenario = scenario
        self.debug = debug
        self.thread_id = "thread-fake-0001"
        self.session_root = "session-root-fake-0001"
        self.turn_index = 0
        self.turn_id = ""
        self.item_id = ""
        self.prompt = ""
        self.prompts: list[str] = []
        self._server_req_id = 0
        self._pending: dict[int, str] = {}
        self.thread_params: dict = {}

    def handle_line(self, message: dict) -> None:
        """Dispatches one parsed JSON-RPC message: server-reply (int id, no method) vs client request/notification by method; scenario-driven side effects."""
        if "method" not in message and isinstance(message.get("id"), int):
            self._on_server_reply(message)
            return
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params") or {}
        if method == "initialize":
            if self.scenario == "stderr-secret":
                sys.stderr.write(str(os.environ.get("KAIZEN_TEST_STDERR_SENTINEL", "sentinel")) + "\n")
                sys.stderr.flush()
            self._reply(msg_id, {
                "userAgent": f"agent-kaizen-fake/{self.version} (FakeOS 0.0.0; x86_64)",
                "codexHome": "D:\\fake\\.codex",
                "platformFamily": "windows",
            })
            _emit({"method": "remoteControl/status/changed", "params": {"status": "disabled"}})
        elif method == "initialized":
            return
        elif method == "model/list":
            self._reply(msg_id, {"data": [{
                "id": "gpt-fake", "model": "gpt-fake", "displayName": "GPT Fake", "hidden": False,
                "isDefault": True, "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low", "description": "low"},
                    {"reasoningEffort": "medium", "description": "medium"},
                    {"reasoningEffort": "high", "description": "high"},
                ],
            }], "nextCursor": None})
        elif method == "config/read":
            self._reply(msg_id, {"config": {
                "model": "gpt-fake", "model_reasoning_effort": "medium",
                "_test_env": {
                    "openai_key_present": bool(os.environ.get("OPENAI_API_KEY")),
                    "anthropic_key_present": bool(os.environ.get("ANTHROPIC_API_KEY")),
                    "credential_path_present": any(key in os.environ for key in (
                        "KAIZEN_CODEX_API_KEY_FILE", "KAIZEN_CLAUDE_API_KEY_FILE")),
                    "codex_home": os.environ.get("CODEX_HOME"),
                    "temp": os.environ.get("TEMP"),
                    "tmp": os.environ.get("TMP"),
                    "tmpdir": os.environ.get("TMPDIR"),
                },
            }})
        elif method == "windowsSandbox/readiness":
            self._reply(msg_id, {"status": "ready"})
        elif method == "thread/start":
            self.thread_params = dict(params)
            model = params.get("model") or "gpt-fake"
            effort = (params.get("config") or {}).get("model_reasoning_effort") or "medium"
            if self.scenario == "profile-mismatch":
                model, effort = "gpt-substituted", "low"
            self._reply(msg_id, {
                "thread": {"id": self.thread_id, "sessionId": self.session_root,
                           "ephemeral": bool(params.get("ephemeral", True)), "path": "D:\\fake\\rollout.jsonl"},
                "model": model,
                "reasoningEffort": effort,
                "modelProvider": "openai",
                "approvalPolicy": params.get("approvalPolicy"),
                "sandbox": {"type": "readOnly" if params.get("sandbox") == "read-only" else "workspaceWrite"},
            })
        elif method == "hooks/list":
            hooks = (self.thread_params.get("config") or {}).get("hooks") or {}
            loaded = bool(hooks.get("PreToolUse"))
            metadata = [{
                "eventName": "preToolUse", "handlerType": "command", "source": "sessionFlags",
                "enabled": True, "trustStatus": "trusted",
            }] if loaded and self.scenario != "helper-failure" else []
            errors = ([{"message": "helper unavailable", "path": "D:\\fake\\hook"}]
                      if self.scenario == "helper-failure" else [])
            self._reply(msg_id, {"data": [{"cwd": (params.get("cwds") or [""])[0],
                                            "hooks": metadata, "errors": errors, "warnings": []}]})
        elif method == "thread/resume":
            self._reply(msg_id, {"thread": {"id": params.get("threadId", self.thread_id),
                                               "sessionId": self.session_root}})
        elif method == "turn/start":
            self.turn_index += 1
            self.turn_id = f"turn-fake-{self.turn_index:04d}"
            self.item_id = f"item-fake-{self.turn_index:04d}"
            inputs = params.get("input") or []
            self.prompt = str(inputs[0].get("text") if inputs and isinstance(inputs[0], dict) else "")
            self.prompts.append(self.prompt)
            if self.scenario == "early-terminal":
                self._drive_turn()
                self._reply(msg_id, {"turn": {"id": self.turn_id, "status": "inProgress"}})
            else:
                self._reply(msg_id, {"turn": {"id": self.turn_id, "status": "inProgress"}})
                self._drive_turn()
        elif method == "turn/steer":
            if params.get("expectedTurnId") != self.turn_id:
                if msg_id is not None:
                    _emit({"id": msg_id, "error": {"code": -32001, "message": "wrong active turn"}})
            else:
                self._reply(msg_id, {"turnId": params.get("expectedTurnId")})
        elif method == "turn/interrupt":
            self._reply(msg_id, {})
            _emit({"method": "turn/completed", "params": {"threadId": self.thread_id,
                   "turn": {"id": params.get("turnId"), "status": "interrupted", "items": []}}})
        elif method == "command/exec":
            self._reply(msg_id, {"exitCode": 0, "stdout": "", "stderr": "",
                                 "echoedSandboxPolicy": params.get("sandboxPolicy")})
        elif method == "thread/shellCommand":
            self._reply(msg_id, {"unexpected": "thread/shellCommand should never be sent"})
        elif msg_id is not None:
            self._reply(msg_id, {})

    def _drive_turn(self) -> None:
        """Emits the turn item/approval/completion sequence selected by self.scenario."""
        _emit({"method": "turn/started", "params": {"threadId": self.thread_id,
               "turn": {"id": self.turn_id, "status": "inProgress", "items": []}}})
        if self.scenario == "child-failure":
            raise SystemExit(17)
        if self.scenario == "malformed-notification":
            _emit({"method": "turn/completed", "params": ["malformed"]})
            return
        if self.scenario == "unknown-request":
            self._request("unknown", "vendor/bogus/unknownServerRequest", {"threadId": self.thread_id})
            self._complete("commandExecution", "completed", True)
            return
        if self.scenario == "interrupt-hang":
            self._item_started("commandExecution", command=self._command())
            return
        family = {
            "approval": "command", "deny": "command", "file-approval": "file",
            "permissions-approval": "permissions", "user-input": "user_input",
            "mcp-elicitation": "mcp", "legacy-exec": "legacy_exec", "legacy-patch": "legacy_patch",
        }.get(self.scenario)
        if family:
            self._start_approval(family)
            return
        self._item_started("commandExecution", command=self._command())
        self._complete("commandExecution", "completed", True)

    def _command(self) -> list[str]:
        if self.scenario == "deny":
            return ["git", "push", "origin", "main"]
        return ["powershell", "-Command", "Set-Content probe.txt hi"]

    def _item_started(self, item_type: str, **fields) -> None:
        _emit({"method": "item/started", "params": {"threadId": self.thread_id, "turnId": self.turn_id,
               "item": {"id": self.item_id, "type": item_type, "status": "inProgress", **fields}}})

    def _request(self, kind: str, method: str, params: dict) -> None:
        self._server_req_id += 1
        self._pending[self._server_req_id] = kind
        _emit({"id": self._server_req_id, "method": method, "params": params})

    def _start_approval(self, family: str) -> None:
        """Emits item-started + server requestApproval frame for the given approval family."""
        common = {"threadId": self.thread_id, "turnId": self.turn_id, "itemId": self.item_id,
                  "startedAtMs": 1}
        if family in ("command", "legacy_exec"):
            self._item_started("commandExecution", command=self._command())
            if family == "command":
                self._request(family, "item/commandExecution/requestApproval",
                              {**common, "command": self._command(), "cwd": "D:\\fake\\cwd"})
            else:
                self._request(family, "execCommandApproval", {
                    "callId": self.item_id, "conversationId": self.thread_id, "command": self._command(),
                    "cwd": "D:\\fake\\cwd", "parsedCmd": [],
                })
        elif family in ("file", "legacy_patch"):
            self._item_started("fileChange", changes=[{"path": "AI/work/allowed.txt", "kind": "add"}])
            if family == "file":
                self._request(family, "item/fileChange/requestApproval", common)
            else:
                self._request(family, "applyPatchApproval", {
                    "callId": self.item_id, "conversationId": self.thread_id,
                    "fileChanges": {"AI/work/allowed.txt": {"type": "add", "content": "x"}},
                })
        elif family == "permissions":
            self._item_started("commandExecution", command=["request_permissions"])
            self._request(family, "item/permissions/requestApproval", {
                **common, "cwd": "D:\\fake\\cwd",
                "permissions": {"network": {"enabled": True}},
            })
        elif family == "user_input":
            self._item_started("mcpToolCall", server="app", tool="mutate", arguments={}, status="inProgress")
            self._request(family, "item/tool/requestUserInput", {
                **common, "questions": [{"id": "decision", "header": "Approve", "question": "Continue?",
                                          "options": [{"label": "Accept", "description": "run"},
                                                      {"label": "Decline", "description": "stop"}]}],
            })
        elif family == "mcp":
            self._item_started("mcpToolCall", server="demo", tool="form", arguments={}, status="inProgress")
            self._request(family, "mcpServer/elicitation/request", {
                "threadId": self.thread_id, "turnId": self.turn_id, "serverName": "demo", "mode": "form",
                "requestedSchema": {"type": "object", "properties": {"confirm": {"type": "boolean", "default": True}},
                                    "required": ["confirm"]},
            })

    def _on_server_reply(self, message: dict) -> None:
        """Maps a client reply for a pending server request (id->kind) to matching item/completed + final-answer emission."""
        kind = self._pending.pop(message["id"], None)
        result = message.get("result") or {}
        if kind == "unknown":
            return
        if kind in ("command", "file"):
            decision = result.get("decision")
            approved = decision in ("accept", "acceptForSession")
            self._complete("fileChange" if kind == "file" else "commandExecution", "completed", approved,
                           decision=decision)
        elif kind in ("legacy_exec", "legacy_patch"):
            decision = result.get("decision")
            self._complete("fileChange" if kind == "legacy_patch" else "commandExecution", "completed",
                           decision == "approved", decision=decision)
        elif kind == "permissions":
            approved = bool((result.get("permissions") or {}).get("network", {}).get("enabled"))
            self._complete("commandExecution", "completed", approved, decision="granted" if approved else "denied")
        elif kind == "user_input":
            answers = result.get("answers")
            decision = answers.get("decision") if isinstance(answers, dict) else None
            labels = decision.get("answers") if isinstance(decision, dict) else None
            if not isinstance(labels, list):
                raise ValueError("malformed user-input reply: answers.decision.answers must be a list")
            approved = any(str(label).casefold() == "accept" for label in labels)
            self._complete("mcpToolCall", "completed", approved, decision=labels[0] if labels else None)
        elif kind == "mcp":
            approved = result.get("action") == "accept"
            self._complete("mcpToolCall", "completed", approved, decision=result.get("action"))

    def _complete(self, item_type: str, status: str, command_run: bool, decision=None) -> None:
        _emit({"method": "item/completed", "params": {"threadId": self.thread_id, "turnId": self.turn_id,
               "completedAtMs": 2, "item": {"id": self.item_id, "type": item_type, "status": status,
               "receivedDecision": decision, "commandRun": command_run}}})
        prior = " | ".join(self.prompts[:-1]) or "none"
        text = f"turn {self.turn_index}: {self.prompt}; prior={prior}"
        message = {"id": f"agent-{self.turn_index}", "type": "agentMessage", "phase": "final_answer", "text": text}
        messages = [message]
        if self.scenario == "multiple-final":
            commentary = {"id": f"commentary-{self.turn_index}", "type": "agentMessage",
                          "phase": "commentary", "text": "intermediate commentary"}
            last = {"id": f"agent-last-{self.turn_index}", "type": "agentMessage",
                    "phase": "final_answer", "text": f"last final for turn {self.turn_index}"}
            messages = [message, commentary, last]
        for completed_message in messages:
            _emit({"method": "item/completed", "params": {"threadId": self.thread_id,
                   "turnId": self.turn_id, "completedAtMs": 3, "item": completed_message}})
        _emit({"method": "turn/completed", "params": {"threadId": self.thread_id,
               "turn": {"id": self.turn_id, "status": "completed", "items": messages}}})

    @staticmethod
    def _reply(message_id, result: dict) -> None:
        if message_id is not None:
            _emit({"id": message_id, "result": result})


def main(argv: list[str] | None = None) -> int:
    """Two modes: generate-json-schema subcommand else parse args + run stdin JSON-RPC loop; returns exit code."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(newline="\n")
    raw = list(sys.argv[1:] if argv is None else argv)
    if "generate-json-schema" in raw:
        return _generate_schema(raw)
    parser = argparse.ArgumentParser()
    parser.add_argument("--version-string", default="0.130.0")
    parser.add_argument("--scenario", default="plain-turn", choices=[
        "approval", "deny", "interrupt-hang", "plain-turn", "unknown-request", "file-approval",
        "permissions-approval", "user-input", "mcp-elicitation", "legacy-exec", "legacy-patch",
        "malformed-notification", "child-failure", "profile-mismatch", "helper-failure", "early-terminal",
        "multiple-final", "stderr-secret",
    ])
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(raw)
    server = FakeServer(args.version_string, args.scenario, args.debug)
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if isinstance(message, dict):
                server.handle_line(message)
        except SystemExit:
            raise
        except Exception as error:  # noqa: BLE001 -- fake bugs are always visible on stderr
            print(f"[fake-codex] handler error: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
