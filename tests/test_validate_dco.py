"""Deterministic offline tests for the repository-bound DCO validator."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "security" / "validate_dco.py"
WORK_DIR = REPO_ROOT / "AI" / "work"

SPEC = importlib.util.spec_from_file_location("validate_dco_under_test", SCRIPT)
assert SPEC and SPEC.loader
DCO = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = DCO
SPEC.loader.exec_module(DCO)

REPOSITORY = "LevyBytes/agent-kaizen"
REPOSITORY_ID = 987654
BASE_SHA = "a" * 40
SIGNED = "Fix a bug\n\nSigned-off-by: Ada Lovelace <ada@example.com>"


def json_bytes_at_size(size: int) -> bytes:
    prefix = b'{"padding":"'
    suffix = b'"}'
    if size < len(prefix) + len(suffix):
        raise ValueError("size is too small for the JSON envelope")
    return prefix + (b"x" * (size - len(prefix) - len(suffix))) + suffix


def sha(index: int) -> str:
    return f"{index:040x}"


def commit(
    index: int,
    message: str = SIGNED,
    *,
    login: str | None = None,
    author_type: str = "User",
    parents: list[str] | None = None,
) -> dict:
    """Builds a GitHub compare commit; default parents form a linear chain from BASE_SHA, while explicit parents model merges."""
    commit_sha = sha(index)
    if parents is None:
        parents = [BASE_SHA if index == 1 else sha(index - 1)]
    author = None if login is None else {"login": login, "type": author_type}
    return {
        "sha": commit_sha,
        "commit": {"message": message},
        "author": author,
        "parents": [{"sha": parent} for parent in parents],
    }


def pull_payload(
    commits: int,
    head_sha: str,
    *,
    base_sha: str = BASE_SHA,
    login: str = "octocat",
    user_type: str = "User",
    head_repository: str = REPOSITORY,
) -> dict:
    return {
        "number": 17,
        "commits": commits,
        "base": {
            "sha": base_sha,
            "repo": {"id": REPOSITORY_ID, "full_name": REPOSITORY},
        },
        "head": {
            "sha": head_sha,
            "repo": {"id": REPOSITORY_ID, "full_name": head_repository},
        },
        "user": {"login": login, "type": user_type},
    }


def event_payload(pull: dict) -> dict:
    """Deep-copies API pull metadata and removes the API-only commit count to model the workflow event shape."""
    event_pull = json.loads(json.dumps(pull))
    event_pull.pop("commits", None)
    return {
        "number": 17,
        "repository": {"id": REPOSITORY_ID, "full_name": REPOSITORY},
        "pull_request": event_pull,
    }


class FakeResponse:
    def __init__(
        self,
        value=None,
        *,
        url: str,
        link: str | None = None,
        raw: bytes | None = None,
        status: int = 200,
    ) -> None:
        self.status = status
        self.url = url
        self.headers = {} if link is None else {"Link": link}
        self.raw = json.dumps(value).encode("utf-8") if raw is None else raw

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def getcode(self) -> int:
        return self.status

    def geturl(self) -> str:
        return self.url

    def read(self, limit: int) -> bytes:
        return self.raw[:limit]


class FakeOpener:
    def __init__(self, routes: dict[str, list[FakeResponse]]) -> None:
        self.routes = routes
        self.calls: list[tuple[object, float]] = []

    def __call__(self, request, timeout: float):
        self.calls.append((request, timeout))
        queue = self.routes.get(request.full_url)
        if not queue:
            raise AssertionError(f"unexpected API request: {request.full_url}")
        return queue.pop(0)


def pull_url() -> str:
    return f"{DCO.API_ROOT}/repos/{REPOSITORY}/pulls/17"


def compare_url(head_sha: str, page: int = 1, base_sha: str = BASE_SHA) -> str:
    base = f"{DCO.API_ROOT}/repos/{REPOSITORY}/compare/{base_sha}...{head_sha}?per_page=100"
    return base if page == 1 else f"{base}&page={page}"


def add_compare_pages(
    routes: dict[str, list[FakeResponse]],
    entries: list[dict],
    url_factory,
    *,
    total: int,
    base_sha: str,
) -> None:
    chunks = [entries[index:index + 100] for index in range(0, len(entries), 100)]
    for index, chunk in enumerate(chunks, start=1):
        url = url_factory(index)
        next_url = url_factory(index + 1) if index < len(chunks) else None
        link = None if next_url is None else f'<{next_url}>; rel="next"'
        value = {
            "total_commits": total,
            "ahead_by": total,
            "base_commit": {"sha": base_sha},
            "merge_base_commit": {"sha": base_sha},
            "commits": chunk,
        }
        routes[url] = [FakeResponse(value, url=url, link=link)]


def api_fixture(
    entries: list[dict],
    *,
    compared: list[dict] | None = None,
    compare_total: int | None = None,
    payload: dict | None = None,
    final_payload: dict | None = None,
) -> tuple[dict, FakeOpener]:
    """Queues initial/final pull metadata plus repository-bound paginated compare responses for one validation run."""
    payload = payload or pull_payload(len(entries), entries[-1]["sha"])
    final_payload = final_payload or payload
    routes: dict[str, list[FakeResponse]] = {
        pull_url(): [
            FakeResponse(payload, url=pull_url()),
            FakeResponse(final_payload, url=pull_url()),
        ]
    }
    compared = entries if compared is None else compared
    total = len(entries) if compare_total is None else compare_total
    add_compare_pages(
        routes,
        compared,
        lambda page: compare_url(payload["head"]["sha"], page, payload["base"]["sha"]),
        total=total,
        base_sha=payload["base"]["sha"],
    )
    return event_payload(payload), FakeOpener(routes)


def parsed_commit(raw: dict):
    return DCO._parse_commit(raw)


class WholeLineSignoff(unittest.TestCase):
    def test_valid_whole_lines(self):
        messages = [
            SIGNED,
            "Title\r\n\r\nSigned-off-by: Ada Lovelace <ada@example.com>\r\n",
            "Work\n\nReviewed-by: X <x@y.example>\nSigned-off-by: A <a@b.example>",
            "Fix\n\nSigned-off-by: Åda Lovelace <ada@example.com>",
        ]
        for message in messages:
            with self.subTest(message=message):
                self.assertTrue(DCO.message_has_signoff(message))

    def test_malformed_or_non_whole_lines_fail(self):
        messages = [
            "Fix Signed-off-by: Ada <ada@example.com> inline",
            "Fix\n  Signed-off-by: Ada <ada@example.com>",
            "Fix\nSigned-off-by:",
            "Fix\nSigned-off-by: Ada",
            "Fix\nSigned-off-by: <ada@example.com>",
            "Fix\nSigned-off-by: Ada <a@b@example.com>",
            "Fix\nSigned-off-by: Ada <a@ example.com>",
            "Fix\nSigned-off-by: Ada <a@example.com> trailing",
            "Fix\nSigned-off-by: Ada <a@example.com> ",
            "Fix\nSigned-off-by: Ada > <a@example.com>",
            "Fix\nSigned-off-by: Ada\x00 <a@example.com>",
            "Fix\nSigned-off-by: Ada <a\x00@example.com>",
        ]
        for message in messages:
            with self.subTest(message=message):
                self.assertFalse(DCO.message_has_signoff(message))

    def test_non_string_fails_closed(self):
        for value in (None, [], {}, 7):
            with self.subTest(value=value):
                self.assertFalse(DCO.message_has_signoff(value))


class CommitEvaluation(unittest.TestCase):
    def test_signed_merge_is_checked_and_passes(self):
        merge = parsed_commit(commit(3, parents=[sha(1), sha(2)]))
        result = DCO.evaluate([merge], exempt_dependabot=False)
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["failed"], [])

    def test_unsigned_merge_fails(self):
        merge = parsed_commit(commit(3, "merge", parents=[sha(1), sha(2)]))
        result = DCO.evaluate([merge], exempt_dependabot=False)
        self.assertEqual([item["sha"] for item in result["failed"]], [sha(3)])

    def test_dependabot_requires_both_pr_and_commit_provenance(self):
        bot = parsed_commit(commit(1, "bump", login="dependabot[bot]", author_type="Bot"))
        self.assertEqual(DCO.evaluate([bot], exempt_dependabot=True)["exempt"], [sha(1)])
        self.assertEqual(len(DCO.evaluate([bot], exempt_dependabot=False)["failed"]), 1)

    def test_other_or_spoofed_bot_is_not_exempt(self):
        entries = [
            parsed_commit(commit(1, "bump", login="renovate[bot]", author_type="Bot")),
            parsed_commit(commit(2, "bump", login="dependabot[bot]", author_type="User")),
        ]
        result = DCO.evaluate(entries, exempt_dependabot=True)
        self.assertEqual(result["exempt"], [])
        self.assertEqual(len(result["failed"]), 2)


class RepositoryBoundApi(unittest.TestCase):
    def test_short_pr_binds_identity_and_headers(self):
        entries = [commit(1), commit(2)]
        event, opener = api_fixture(entries)
        result = DCO.validate(event, REPOSITORY, "token-value", opener)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["coverage"], "compare-base-head")
        self.assertEqual(result["head_sha"], sha(2))
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["api_pages"], 3)
        self.assertEqual(len(opener.calls), 3)
        for request, timeout in opener.calls:
            headers = dict(request.header_items())
            self.assertEqual(headers["Authorization"], "Bearer token-value")
            self.assertEqual(headers["User-agent"], "agent-kaizen-dco-validator")
            self.assertEqual(headers["X-github-api-version"], DCO.API_VERSION)
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, DCO.REQUEST_TIMEOUT_SECONDS)

    def test_event_repository_name_mismatch_fails_before_api(self):
        entries = [commit(1)]
        event, opener = api_fixture(entries)
        event["repository"]["full_name"] = "attacker/elsewhere"
        with self.assertRaisesRegex(DCO.DcoError, "workflow repository") as caught:
            DCO.validate(event, REPOSITORY, "token-value", opener)
        self.assertEqual(caught.exception.code, "REPOSITORY_MISMATCH")
        self.assertEqual(opener.calls, [])

    def test_event_repository_id_mismatch_fails_before_api(self):
        entries = [commit(1)]
        event, opener = api_fixture(entries)
        event["pull_request"]["base"]["repo"]["id"] += 1
        with self.assertRaises(DCO.DcoError) as caught:
            DCO.validate(event, REPOSITORY, "token-value", opener)
        self.assertEqual(caught.exception.code, "REPOSITORY_MISMATCH")
        self.assertEqual(opener.calls, [])

    def test_repository_dot_segment_is_rejected(self):
        entries = [commit(1)]
        event, opener = api_fixture(entries)
        event["repository"]["full_name"] = "../repo"
        event["pull_request"]["base"]["repo"]["full_name"] = "../repo"
        with self.assertRaises(DCO.DcoError) as caught:
            DCO.validate(event, "../repo", "token-value", opener)
        self.assertEqual(caught.exception.code, "REPOSITORY_INVALID")
        self.assertEqual(opener.calls, [])

    def test_api_head_mismatch_fails_closed(self):
        entries = [commit(1)]
        payload = pull_payload(1, sha(1))
        event, opener = api_fixture(entries, payload=payload)
        opener.routes[pull_url()][0] = FakeResponse(
            pull_payload(1, sha(9)),
            url=pull_url(),
        )
        with self.assertRaises(DCO.DcoError) as caught:
            DCO.validate(event, REPOSITORY, "token-value", opener)
        self.assertEqual(caught.exception.code, "PR_STATE_MISMATCH")

    def test_pr_change_during_fetch_fails_closed(self):
        entries = [commit(1)]
        payload = pull_payload(1, sha(1))
        final = pull_payload(2, sha(1))
        event, opener = api_fixture(entries, payload=payload, final_payload=final)
        with self.assertRaises(DCO.DcoError) as caught:
            DCO.validate(event, REPOSITORY, "token-value", opener)
        self.assertEqual(caught.exception.code, "PR_CHANGED_DURING_VALIDATION")

    def test_compare_base_mismatch_fails_closed(self):
        entries = [commit(1)]
        event, opener = api_fixture(entries)
        url = compare_url(sha(1))
        value = {
            "total_commits": 1,
            "ahead_by": 1,
            "base_commit": {"sha": "b" * 40},
            "merge_base_commit": {"sha": BASE_SHA},
            "commits": entries,
        }
        opener.routes[url] = [FakeResponse(value, url=url)]
        with self.assertRaises(DCO.DcoError) as caught:
            DCO.validate(event, REPOSITORY, "token-value", opener)
        self.assertEqual(caught.exception.code, "BASE_SHA_MISMATCH")

    def test_api_timeout_fails_closed(self):
        entries = [commit(1)]
        event, _ = api_fixture(entries)

        def timeout_opener(request, timeout):
            raise TimeoutError

        with self.assertRaises(DCO.DcoError) as caught:
            DCO.validate(event, REPOSITORY, "token-value", timeout_opener)
        self.assertEqual(caught.exception.code, "API_UNAVAILABLE")

    def test_malformed_token_fails_before_api(self):
        entries = [commit(1)]
        event, opener = api_fixture(entries)
        with self.assertRaises(DCO.DcoError) as caught:
            DCO.validate(event, REPOSITORY, "token value", opener)
        self.assertEqual(caught.exception.code, "TOKEN_INVALID")
        self.assertEqual(opener.calls, [])

    def test_malformed_commit_shape_is_infrastructure_error(self):
        entries = [commit(1)]
        event, opener = api_fixture(entries)
        bad_url = compare_url(sha(1))
        bad_value = {
            "total_commits": 1,
            "ahead_by": 1,
            "base_commit": {"sha": BASE_SHA},
            "merge_base_commit": {"sha": BASE_SHA},
            "commits": [{"sha": sha(1)}],
        }
        opener.routes[bad_url] = [FakeResponse(bad_value, url=bad_url)]
        with self.assertRaises(DCO.DcoError) as caught:
            DCO.validate(event, REPOSITORY, "token-value", opener)
        self.assertEqual(caught.exception.code, "API_SHAPE_INVALID")

    def test_duplicate_commit_sha_is_infrastructure_error(self):
        entries = [commit(1), commit(1)]
        payload = pull_payload(2, sha(1))
        event, opener = api_fixture(entries, payload=payload)
        with self.assertRaises(DCO.DcoError) as caught:
            DCO.validate(event, REPOSITORY, "token-value", opener)
        self.assertEqual(caught.exception.code, "API_COMMIT_DUPLICATE")


class PaginationAndCoverage(unittest.TestCase):
    def test_101_commits_follow_link_and_cover_all(self):
        entries = [commit(index) for index in range(1, 102)]
        event, opener = api_fixture(entries)
        result = DCO.validate(event, REPOSITORY, "token-value", opener)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["total"], 101)
        self.assertEqual(result["api_pages"], 4)

    def test_exactly_250_commits_are_complete_without_fallback(self):
        entries = [commit(index) for index in range(1, 251)]
        event, opener = api_fixture(entries)
        result = DCO.validate(event, REPOSITORY, "token-value", opener)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["total"], 250)
        self.assertEqual(result["coverage"], "compare-base-head")
        self.assertEqual(result["api_pages"], 5)

    def test_251_commits_are_fully_paginated_by_compare(self):
        entries = [commit(index) for index in range(1, 252)]
        event, opener = api_fixture(entries)
        result = DCO.validate(event, REPOSITORY, "token-value", opener)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["total"], 251)
        self.assertEqual(result["coverage"], "compare-base-head")
        self.assertEqual(result["api_pages"], 5)

    def test_compare_checks_unsigned_commit_beyond_pr_endpoint_cap(self):
        entries = [commit(index) for index in range(1, 252)]
        entries[-1] = commit(251, "unsigned final commit")
        event, opener = api_fixture(entries)
        result = DCO.validate(event, REPOSITORY, "token-value", opener)
        self.assertEqual(result["status"], "DENIED")
        self.assertEqual([item["sha"] for item in result["failed"]], [sha(251)])

    def test_compare_count_drift_fails_closed(self):
        entries = [commit(index) for index in range(1, 252)]
        event, opener = api_fixture(entries, compare_total=252)
        with self.assertRaises(DCO.DcoError) as caught:
            DCO.validate(event, REPOSITORY, "token-value", opener)
        self.assertEqual(caught.exception.code, "COMMIT_COUNT_MISMATCH")

    def test_compare_page_count_drift_fails_closed(self):
        entries = [commit(index) for index in range(1, 102)]
        event, opener = api_fixture(entries)
        second_url = compare_url(sha(101), page=2)
        value = {
            "total_commits": 102,
            "ahead_by": 102,
            "base_commit": {"sha": BASE_SHA},
            "merge_base_commit": {"sha": BASE_SHA},
            "commits": entries[100:],
        }
        opener.routes[second_url] = [FakeResponse(value, url=second_url)]
        with self.assertRaises(DCO.DcoError) as caught:
            DCO.validate(event, REPOSITORY, "token-value", opener)
        self.assertEqual(caught.exception.code, "COMMIT_COUNT_MISMATCH")

    def test_short_endpoint_result_fails_closed(self):
        entries = [commit(index) for index in range(1, 201)]
        event, opener = api_fixture(entries, compared=entries[:100])
        with self.assertRaises(DCO.DcoError) as caught:
            DCO.validate(event, REPOSITORY, "token-value", opener)
        self.assertEqual(caught.exception.code, "COMMIT_COVERAGE_INCOMPLETE")

    def test_external_next_link_is_rejected_before_token_can_follow(self):
        entries = [commit(1), commit(2)]
        payload = pull_payload(2, sha(2))
        event, opener = api_fixture(entries, payload=payload)
        first_url = compare_url(sha(2))
        value = {
            "total_commits": 2,
            "ahead_by": 2,
            "base_commit": {"sha": BASE_SHA},
            "merge_base_commit": {"sha": BASE_SHA},
            "commits": [entries[0]],
        }
        opener.routes[first_url] = [
            FakeResponse(
                value,
                url=first_url,
                link='<https://attacker.example/steal?page=2>; rel="next"',
            )
        ]
        with self.assertRaises(DCO.DcoError) as caught:
            DCO.validate(event, REPOSITORY, "token-value", opener)
        self.assertEqual(caught.exception.code, "API_LINK_INVALID")
        self.assertEqual(len(opener.calls), 2)

    def test_pagination_loop_is_rejected(self):
        entries = [commit(1), commit(2)]
        payload = pull_payload(2, sha(2))
        event, opener = api_fixture(entries, payload=payload)
        first_url = compare_url(sha(2))
        value = {
            "total_commits": 2,
            "ahead_by": 2,
            "base_commit": {"sha": BASE_SHA},
            "merge_base_commit": {"sha": BASE_SHA},
            "commits": [entries[0]],
        }
        opener.routes[first_url] = [
            FakeResponse(value, url=first_url, link=f'<{first_url}>; rel="next"')
        ]
        with self.assertRaises(DCO.DcoError) as caught:
            DCO.validate(event, REPOSITORY, "token-value", opener)
        self.assertEqual(caught.exception.code, "API_PAGINATION_LOOP")


class BotProvenance(unittest.TestCase):
    def test_native_dependabot_pr_is_exempt(self):
        entries = [commit(1, "bump", login="dependabot[bot]", author_type="Bot")]
        payload = pull_payload(
            1,
            sha(1),
            login="dependabot[bot]",
            user_type="Bot",
        )
        event, opener = api_fixture(entries, payload=payload)
        result = DCO.validate(event, REPOSITORY, "token-value", opener)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["exempt"], [sha(1)])
        self.assertEqual(result["checked"], 0)

    def test_human_pr_cannot_spoof_dependabot_commit_author(self):
        entries = [commit(1, "bump", login="dependabot[bot]", author_type="Bot")]
        event, opener = api_fixture(entries)
        result = DCO.validate(event, REPOSITORY, "token-value", opener)
        self.assertEqual(result["status"], "DENIED")
        self.assertEqual(result["exempt"], [])

    def test_dependabot_named_fork_is_not_exempt(self):
        entries = [commit(1, "bump", login="dependabot[bot]", author_type="Bot")]
        payload = pull_payload(
            1,
            sha(1),
            login="dependabot[bot]",
            user_type="Bot",
            head_repository="attacker/fork",
        )
        event, opener = api_fixture(entries, payload=payload)
        result = DCO.validate(event, REPOSITORY, "token-value", opener)
        self.assertEqual(result["status"], "DENIED")


class InputBoundaryContract(unittest.TestCase):
    def test_event_exact_limit_is_accepted_and_limit_plus_one_is_denied(self):
        with tempfile.TemporaryDirectory(dir=WORK_DIR) as temp_dir:
            event_path = Path(temp_dir) / "event.json"
            event_path.write_bytes(json_bytes_at_size(DCO.MAX_EVENT_BYTES))
            accepted = DCO._read_event(str(event_path))
            self.assertEqual(len(accepted["padding"]), DCO.MAX_EVENT_BYTES - len(b'{"padding":""}'))
            event_path.write_bytes(json_bytes_at_size(DCO.MAX_EVENT_BYTES + 1))
            with self.assertRaises(DCO.DcoError) as denied:
                DCO._read_event(str(event_path))
        self.assertEqual(denied.exception.code, "EVENT_TOO_LARGE")

    def test_api_response_exact_limit_is_accepted_and_limit_plus_one_is_denied(self):
        exact = json_bytes_at_size(DCO.MAX_RESPONSE_BYTES)
        opener = FakeOpener({pull_url(): [FakeResponse(url=pull_url(), raw=exact)]})
        accepted = DCO.GitHubApi("token-value", REPOSITORY, REPOSITORY_ID, opener).pull(17)
        self.assertEqual(len(accepted["padding"]), DCO.MAX_RESPONSE_BYTES - len(b'{"padding":""}'))

        oversized = json_bytes_at_size(DCO.MAX_RESPONSE_BYTES + 1)
        opener = FakeOpener({pull_url(): [FakeResponse(url=pull_url(), raw=oversized)]})
        with self.assertRaises(DCO.DcoError) as denied:
            DCO.GitHubApi("token-value", REPOSITORY, REPOSITORY_ID, opener).pull(17)
        self.assertEqual(denied.exception.code, "API_RESPONSE_TOO_LARGE")


class CliContract(unittest.TestCase):
    def run_main(self, entries: list[dict], *, json_mode: bool = True, token: str | None = "token-value"):
        event, opener = api_fixture(entries)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory(dir=WORK_DIR) as temp_dir:
            event_path = Path(temp_dir) / "event.json"
            event_path.write_text(json.dumps(event), encoding="utf-8")
            argv = ["--event-file", str(event_path), "--repository", REPOSITORY]
            if json_mode:
                argv.append("--json")
            env = {} if token is None else {"GITHUB_TOKEN": token}
            code = DCO.main(argv, env=env, opener=opener, stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_json_success_is_one_stdout_envelope(self):
        code, stdout, stderr = self.run_main([commit(1)])
        self.assertEqual(code, 0)
        self.assertEqual(len(stdout.splitlines()), 1)
        self.assertEqual(json.loads(stdout)["status"], "OK")
        self.assertIn("coverage=compare-base-head", stderr)

    def test_json_denial_uses_exit_two_and_stderr_diagnostics(self):
        code, stdout, stderr = self.run_main([commit(1, "unsigned")])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout)["status"], "DENIED")
        self.assertIn(f"unsigned: {sha(1)}", stderr)

    def test_non_json_success_keeps_stdout_empty(self):
        code, stdout, stderr = self.run_main([commit(1)], json_mode=False)
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertIn("validate-dco", stderr)

    def test_missing_token_is_exit_one_without_token_echo(self):
        code, stdout, stderr = self.run_main([commit(1)], token=None)
        self.assertEqual(code, 1)
        payload = json.loads(stdout)
        self.assertEqual(payload["error_code"], "TOKEN_MISSING")
        self.assertNotIn("token-value", stdout + stderr)

    def test_argument_error_uses_infrastructure_exit(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = DCO.main(["--json", "--unknown"], env={}, stdout=stdout, stderr=stderr)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout.getvalue())["error_code"], "ARGUMENT_ERROR")

    def test_unexpected_internal_traceback_requires_explicit_debug(self):
        event, opener = api_fixture([commit(1)])
        with tempfile.TemporaryDirectory(dir=WORK_DIR) as temp_dir:
            event_path = Path(temp_dir) / "event.json"
            event_path.write_text(json.dumps(event), encoding="utf-8")
            base = ["--event-file", str(event_path), "--repository", REPOSITORY, "--json"]
            for debug in (False, True):
                with self.subTest(debug=debug):
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with mock.patch.object(DCO, "validate", side_effect=RuntimeError("fixture failure")):
                        code = DCO.main(
                            [*base, *(["--debug"] if debug else [])],
                            env={"GITHUB_TOKEN": "token-value"},
                            opener=opener,
                            stdout=stdout,
                            stderr=stderr,
                        )
                    payload = json.loads(stdout.getvalue())
                    self.assertEqual(code, 1)
                    self.assertEqual(payload["error_code"], "INTERNAL_ERROR")
                    self.assertIn("RuntimeError", payload["reason"])
                    self.assertEqual("Traceback (most recent call last):" in stderr.getvalue(), debug)

    def test_duplicate_event_key_is_rejected(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory(dir=WORK_DIR) as temp_dir:
            event_path = Path(temp_dir) / "event.json"
            event_path.write_text('{"number":17,"number":18}', encoding="utf-8")
            code = DCO.main(
                ["--event-file", str(event_path), "--repository", REPOSITORY, "--json"],
                env={"GITHUB_TOKEN": "token-value"},
                stdout=stdout,
                stderr=stderr,
            )
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout.getvalue())["error_code"], "EVENT_JSON_INVALID")

    def test_real_entrypoint_fails_closed_offline(self):
        with tempfile.TemporaryDirectory(dir=WORK_DIR) as temp_dir:
            event_path = Path(temp_dir) / "event.json"
            event_path.write_text("{}", encoding="utf-8")
            env = os.environ.copy()
            env.pop("GITHUB_TOKEN", None)
            process = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--event-file",
                    str(event_path),
                    "--repository",
                    REPOSITORY,
                    "--json",
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=10,
                check=False,
            )
        self.assertEqual(process.returncode, 1)
        self.assertEqual(json.loads(process.stdout)["error_code"], "TOKEN_MISSING")


if __name__ == "__main__":
    unittest.main()
