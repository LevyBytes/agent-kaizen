"""Focused D1/D2 streaming transport, normalization, ring, and fail-closed proof."""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from kaizen_components.backends.openai_compat import OpenAICompatClient
from kaizen_components.denials import KaizenDenied
from kaizen_components.orchestration import policy
from kaizen_components.orchestration.adapters.claude_sdk import ClaudeSdkAdapter
from kaizen_components.orchestration.claude_worker_protocol import WorkerProtocolError
from kaizen_components.orchestration.adapters.local_llm import LocalLLMAdapter, _FinalDeltaExtractor
from kaizen_components.orchestration.session_drive import (
    DELTA_BATCH_MAX_BYTES,
    DELTA_RING_MAX_BYTES,
    DELTA_RING_MAX_CHUNKS,
    DeltaRing,
    DrivenSession,
)
from kaizen_components.orchestration.supervisor import Supervisor


class _Lines:
    """Minimal context-managed urlopen response stub that returns queued byte lines; supplied data may intentionally exceed the requested readline limit for denial tests."""
    def __init__(self, lines: list[bytes]) -> None:
        self.lines = list(lines)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def readline(self, _limit: int) -> bytes:
        return self.lines.pop(0) if self.lines else b""


class OpenAIStreamingTest(unittest.TestCase):
    def _chat(self, lines: list[bytes]):
        client = OpenAICompatClient("http://127.0.0.1:1/v1")
        deltas: list[str] = []
        with mock.patch("urllib.request.urlopen", return_value=_Lines(lines)):
            result = client.chat_stream([{"role": "user", "content": "x"}], "m", deltas.append)
        return result, deltas

    def test_openai_sse_is_ordered_and_aggregated(self) -> None:
        result, deltas = self._chat([
            b': keepalive\n',
            b'data: {"model":"m2","choices":[{"delta":{"content":"Hel"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"lo"}}],"usage":{"output_tokens":2}}\n',
            b'data: [DONE]\n',
        ])
        self.assertEqual(deltas, ["Hel", "lo"])
        self.assertEqual(result, {"text": "Hello", "usage": {"output_tokens": 2}, "model": "m2"})

    def test_empty_data_heartbeat_is_ignored(self) -> None:
        result, deltas = self._chat([
            b"data:\n",
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n',
            b"data: [DONE]\n",
        ])
        self.assertEqual(result["text"], "ok")
        self.assertEqual(deltas, ["ok"])

    def test_bom_is_accepted_only_on_the_first_stream_line(self) -> None:
        result, _ = self._chat([
            b'\xef\xbb\xbfdata: {"choices":[{"delta":{"content":"ok"}}]}\n',
            b"data: [DONE]\n",
        ])
        self.assertEqual(result["text"], "ok")
        with self.assertRaises(KaizenDenied) as caught:
            self._chat([
                b": heartbeat\n",
                b'\xef\xbb\xbfdata: {"choices":[{"delta":{"content":"bad"}}]}\n',
            ])
        self.assertEqual(caught.exception.code, "DENIED_BACKEND_MALFORMED")

    def test_multiple_stream_choices_are_denied(self) -> None:
        with self.assertRaises(KaizenDenied) as caught:
            self._chat([b'data: {"choices":[{"delta":{}},{"delta":{}}]}\n'])
        self.assertEqual(caught.exception.code, "DENIED_BACKEND_MALFORMED")

    def test_buffered_chat_forces_nonstream_and_rejects_multiple_choices(self) -> None:
        client = OpenAICompatClient("http://127.0.0.1:1/v1")
        client._post = mock.Mock(return_value={"choices": [{"message": {"content": "ok"}}]})
        self.assertEqual(client.chat([], "m", stream=True)["text"], "ok")
        self.assertFalse(client._post.call_args.args[1]["stream"])
        client._post.return_value = {"choices": [{"message": {}}, {"message": {}}]}
        with self.assertRaises(KaizenDenied) as caught:
            client.chat([], "m")
        self.assertEqual(caught.exception.code, "DENIED_BACKEND_MALFORMED")

    def test_ollama_jsonl_and_one_shot_fallback(self) -> None:
        result, deltas = self._chat([
            b'{"model":"q","message":{"content":"one"},"done":false}\n',
            b'{"message":{"content":" two"},"done":true}\n',
        ])
        self.assertEqual(result["text"], "one two")
        self.assertEqual(deltas, ["one", " two"])

        result, deltas = self._chat([
            b'{"choices":[{"message":{"content":"complete"}}],"model":"fallback"}\n',
        ])
        self.assertEqual(result["text"], "complete")
        self.assertEqual(deltas, ["complete"])

    def test_callback_failure_does_not_fail_transport(self) -> None:
        client = OpenAICompatClient("http://127.0.0.1:1/v1")
        with mock.patch("urllib.request.urlopen", return_value=_Lines([
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n', b'data: [DONE]\n',
        ])):
            result = client.chat_stream(
                [{"role": "user", "content": "x"}], "m",
                lambda _text: (_ for _ in ()).throw(RuntimeError("sink")),
            )
        self.assertEqual(result["text"], "ok")

    def test_malformed_json_and_utf8_fail_closed(self) -> None:
        for line in (b"data: {bad}\n", b"data: \xff\n"):
            with self.subTest(line=line), self.assertRaises(KaizenDenied) as caught:
                self._chat([line])
            self.assertEqual(caught.exception.code, "DENIED_BACKEND_MALFORMED")

    def test_overlong_stream_line_fails_closed(self) -> None:
        with self.assertRaises(KaizenDenied) as caught:
            self._chat([b"x" * (OpenAICompatClient._STREAM_LINE_MAX_BYTES + 1)])
        self.assertEqual(caught.exception.code, "DENIED_BACKEND_MALFORMED")


class LocalFinalStreamingTest(unittest.TestCase):
    def test_lexer_suppresses_tool_json_and_decodes_split_escapes(self) -> None:
        tool = _FinalDeltaExtractor()
        self.assertEqual(tool.feed('{"tool":"write_file","args":{"content":"secret"}}'), "")

        answer = 'line one\nquote: "x" and emoji \U0001f600'
        encoded = json.dumps({"final": answer}, ensure_ascii=True)
        final = _FinalDeltaExtractor()
        self.assertEqual("".join(final.feed(char) for char in encoded), answer)

    def test_adapter_emits_only_final_text_with_stable_turn_id(self) -> None:
        replies = [
            json.dumps({"tool": "unknown", "args": {"context": "must-not-stream"}}),
            json.dumps({"final": 'hello\n"world" \U0001f600'}, ensure_ascii=True),
        ]
        state = {"index": 0}

        def provider(_messages, **_opts):
            raise AssertionError("buffered provider path must not run")

        def stream(_messages, on_delta, **_opts):
            text = replies[state["index"]]
            state["index"] += 1
            for char in text:
                on_delta(char)
            return {"text": text}

        provider.stream_chat = stream  # type: ignore[attr-defined]
        ids = iter(("turn-one",))
        adapter = LocalLLMAdapter(
            policy.PolicyEngine([], [], []), chat_provider=provider,
            id_factory=lambda _prefix: next(ids), logger=lambda _message: None,
        )
        adapter.start_session()
        deltas: list[dict[str, str]] = []
        adapter.on_delta(deltas.append)
        result = adapter.start_turn("answer")
        self.assertEqual(result["status"], "OK")
        self.assertEqual("".join(item["text"] for item in deltas), 'hello\n"world" \U0001f600')
        self.assertEqual({item["turn_id"] for item in deltas}, {"turn-one"})
        self.assertNotIn("must-not-stream", "".join(item["text"] for item in deltas))

    def test_malformed_partial_has_terminal_failure_not_tool_metadata(self) -> None:
        raw = '{"final":"partial'

        def provider(_messages):
            return {"text": raw}

        def stream(_messages, on_delta):
            on_delta(raw)
            return {"text": raw}

        provider.stream_chat = stream  # type: ignore[attr-defined]
        adapter = LocalLLMAdapter(
            policy.PolicyEngine([], [], []), chat_provider=provider, max_turns=1,
            id_factory=lambda _prefix: "turn-failed", logger=lambda _message: None,
        )
        adapter.start_session()
        deltas: list[dict[str, str]] = []
        adapter.on_delta(deltas.append)
        result = adapter.start_turn("x")
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual("".join(item["text"] for item in deltas), "partial")


class DeltaRingTest(unittest.TestCase):
    def test_monotonic_nonconsuming_reads_and_turn_correlation(self) -> None:
        ring = DeltaRing()
        ring.append("turn-a", "one")
        ring.append("turn-a", "two")
        first = ring.read(0)
        second = ring.read(0)
        self.assertEqual(first, second)
        self.assertEqual([item["seq"] for item in first[0]], [1, 2])
        self.assertEqual([item["text"] for item in first[0]], ["one", "two"])
        self.assertFalse(first[2])

    def test_count_drop_returns_no_incomplete_suffix(self) -> None:
        ring = DeltaRing()
        for _ in range(DELTA_RING_MAX_CHUNKS + 1):
            ring.append("turn", "x")
        deltas, cursor, dropped = ring.read(0)
        self.assertEqual(deltas, [])
        self.assertEqual(cursor, DELTA_RING_MAX_CHUNKS + 1)
        self.assertTrue(dropped)

    def test_byte_drop_returns_no_incomplete_suffix(self) -> None:
        ring = DeltaRing()
        chunk_bytes = 32 * 1024
        chunk_count = DELTA_RING_MAX_BYTES // chunk_bytes + 1
        for _ in range(chunk_count):
            ring.append("turn", "x" * chunk_bytes)
        deltas, cursor, dropped = ring.read(0)
        self.assertEqual(deltas, [])
        self.assertEqual(cursor, chunk_count)
        self.assertTrue(dropped)

    def test_unicode_split_and_pages_are_bounded(self) -> None:
        ring = DeltaRing()
        text = "\U0001f600" * 200_000
        ring.append("turn", text)
        deltas, cursor, dropped = ring.read(0)
        self.assertFalse(dropped)
        self.assertGreater(cursor, 0)
        wire = json.dumps({"deltas": deltas}, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(wire), DELTA_BATCH_MAX_BYTES)
        # Read every bounded page and prove UTF-8 splitting did not alter text.
        parts: list[str] = []
        cursor = 0
        while True:
            page, next_cursor, page_dropped = ring.read(cursor)
            self.assertFalse(page_dropped)
            parts.extend(item["text"] for item in page)
            if next_cursor == cursor:
                break
            cursor = next_cursor
        self.assertEqual("".join(parts), text)

    def test_driven_session_accepts_only_live_running_deltas(self) -> None:
        session = DrivenSession("s", "r", object(), "local_llm", 1.0, streaming=True)
        self.assertEqual(session.append_delta("turn", "before"), 0)
        self.assertIsNone(session.begin_turn())
        self.assertEqual(session.append_delta("turn", "during"), 1)
        self.assertEqual(session.complete_delta_turn(), "turn")
        self.assertIsNone(session.complete_delta_turn())


class SupervisorDeltaTest(unittest.TestCase):
    def test_restart_and_observed_fallback(self) -> None:
        supervisor = Supervisor()
        self.assertEqual(supervisor._run_delta_state("missing", "driven", 7), ([], 0, True))
        self.assertEqual(supervisor._run_delta_state("observed", "observed", 7), ([], 7, False))

    def test_durable_final_can_correlate_to_ephemeral_turn(self) -> None:
        supervisor = Supervisor()
        captured: list[dict] = []
        supervisor.funnel_event = lambda *_args, **kwargs: captured.append(kwargs)  # type: ignore[method-assign]
        supervisor._record_chat_message(
            "run", "assistant", "final", turn_id="vendor-result", correlation_id="stream-turn",
        )
        self.assertEqual(captured[0]["correlation_id"], "stream-turn")
        self.assertEqual(json.loads(captured[0]["body"])["turn_id"], "vendor-result")

    def test_events_payload_carries_delta_state_within_loopback_budget(self) -> None:
        supervisor = Supervisor()
        payload = supervisor._events_payload(
            "run", [], 0, False, None,
            delta_state=([{"seq": 1, "turn_id": "turn", "text": "x"}], 1, False),
        )
        self.assertEqual(payload["delta_cursor"], 1)
        self.assertFalse(payload["delta_dropped"])
        self.assertLess(supervisor._response_bytes(payload), 1024 * 1024)

    def test_long_poll_returns_on_delta_and_does_not_consume_it(self) -> None:
        supervisor = Supervisor()
        session = DrivenSession("s", "run", object(), "local_llm", 1.0, streaming=True)
        session.begin_turn()
        supervisor._driven["run"] = session
        now = [0.0]

        def sleep(seconds: float) -> None:
            now[0] += seconds
            session.append_delta("turn", "arrived")

        supervisor._clock = lambda: now[0]
        supervisor._sleep = sleep
        with mock.patch.object(supervisor, "_conversation_run_controller", return_value="driven"), \
                mock.patch.object(supervisor, "_read_run_events", return_value=[]), \
                mock.patch.object(supervisor, "_driven_terminal", return_value=(False, None)):
            first = supervisor._handle_session_events({
                "agent_run_id": "run", "since": 0, "delta_since": 0, "wait": 1.0,
            })
            second = supervisor._handle_session_events({
                "agent_run_id": "run", "since": 0, "delta_since": 0, "wait": 0,
            })
        self.assertEqual(first["deltas"], second["deltas"])
        self.assertEqual(first["deltas"][0]["text"], "arrived")


class ClaudeStreamingTest(unittest.TestCase):
    @staticmethod
    def _adapter() -> ClaudeSdkAdapter:
        adapter = ClaudeSdkAdapter(worker_command=["node", "unused-worker.mjs"])
        adapter._active_turn = "turn-claude"
        return adapter

    def test_sdk_incremental_events_are_ordered(self) -> None:
        adapter = self._adapter()
        seen: list[dict[str, str]] = []
        adapter.on_delta(seen.append)
        for seq, fragment in enumerate(("Hel", "lo"), start=1):
            adapter._handle_event({
                "event": "delta", "session_id": adapter._worker_session_id,
                "turn_id": "turn-claude", "seq": seq, "body": {"text": fragment},
            })
        self.assertEqual("".join(item["text"] for item in seen), "Hello")
        self.assertEqual({item["turn_id"] for item in seen}, {"turn-claude"})

    def test_sdk_rejects_duplicate_delta_sequence(self) -> None:
        adapter = self._adapter()
        frame = {
            "event": "delta", "session_id": adapter._worker_session_id,
            "turn_id": "turn-claude", "seq": 1, "body": {"text": "H"},
        }
        adapter._handle_event(frame)
        with self.assertRaises(WorkerProtocolError):
            adapter._handle_event(frame)


@unittest.skipUnless(
    os.environ.get("KAIZEN_RUN_LIVE") == "1"
    and os.environ.get("KAIZEN_LLM_MODEL")
    and os.environ.get("KAIZEN_LLM_BASE_URL"),
    "bounded real Ollama streaming acceptance",
)
class LiveOllamaStreamingTest(unittest.TestCase):
    def test_final_only_delta_matches_durable_assistant(self) -> None:
        from test_session_drive import LiveDrivenSmokeTest

        prompt = 'Reply with exactly {"final":"STREAM_OK"} and no other text.'
        body = (
            "sup = Supervisor(); sup.boot()\n"
            "sup._driven_test_records = True\n"
            "start = sup._handle_control({'op':'session/start','args':{'engine':'local_llm','prompt':"
            + repr(prompt) + ",'max_turns':2,'model':"
            + repr(os.environ["KAIZEN_LLM_MODEL"]) + "}})\n"
            "if start.get('status') != 'OK':\n"
            "    sup.shutdown(); out = {'start': start}\n"
            "else:\n"
            "    rid = start['agent_run_id']; wait_idle(sup, rid, budget=120.0)\n"
            "    wire = sup._handle_control({'op':'session/events','args':{'agent_run_id':rid,'since':0,'delta_since':0}})\n"
            "    chats = [event for event in wire.get('events',[]) if event.get('event_kind') == 'chat_message']\n"
            "    assistant = [event for event in chats if json.loads(event['body']).get('role') == 'assistant'][-1]\n"
            "    assistant_body = json.loads(assistant['body'])\n"
            "    deltas = wire.get('deltas',[])\n"
            "    close = sup._handle_control({'op':'session/close','args':{'agent_run_id':rid}})\n"
            "    sup.shutdown()\n"
            "    out = {'start': start, 'delta_text': ''.join(item['text'] for item in deltas),\n"
            "           'delta_turns': sorted(set(item['turn_id'] for item in deltas)),\n"
            "           'durable_text': assistant_body.get('text'), 'durable_turn': assistant_body.get('turn_id'),\n"
            "           'correlation': assistant.get('correlation_id'), 'dropped': wire.get('delta_dropped'),\n"
            "           'close': close}\n"
        )
        helper = LiveDrivenSmokeTest(methodName="test_real_ollama_driven_two_turns_share_context_and_close_once")
        out = helper._drive_real(body)
        self.assertEqual(out["start"]["status"], "OK", out)
        self.assertEqual(out["delta_text"], out["durable_text"], out)
        self.assertEqual(out["durable_text"], "STREAM_OK", out)
        self.assertEqual(out["delta_turns"], [out["durable_turn"]], out)
        self.assertEqual(out["correlation"], out["durable_turn"], out)
        self.assertFalse(out["dropped"], out)
        self.assertEqual(out["close"]["status"], "OK", out)


if __name__ == "__main__":
    unittest.main()
