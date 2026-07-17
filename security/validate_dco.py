#!/usr/bin/env python3
"""Validate DCO sign-offs against repository-bound GitHub pull-request data.

The workflow must run this file from the trusted base revision, not from the pull-request checkout.
The GitHub token is accepted only through ``GITHUB_TOKEN`` and is never written to output.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, TextIO

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
REQUEST_TIMEOUT_SECONDS = 15
TOTAL_TIMEOUT_SECONDS = 300
MAX_API_PAGES = 100
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
EXEMPT_BOT_LOGIN = "dependabot[bot]"

_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_SHA_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")


class DcoError(Exception):
    """A fail-closed infrastructure or untrusted-input error safe to report."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DcoError("ARGUMENT_ERROR", message)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_HTTP_OPENER = urllib.request.build_opener(_NoRedirect())


def _open_request(request: urllib.request.Request, timeout: float):
    return _HTTP_OPENER.open(request, timeout=timeout)


@dataclass(frozen=True)
class PullRequestState:
    repository: str
    repository_id: int
    number: int
    head_sha: str
    base_sha: str
    head_repository: str
    user_login: str
    user_type: str
    commits: int | None


@dataclass(frozen=True)
class CommitData:
    sha: str
    message: str
    author_login: str | None
    author_type: str | None
    parents: tuple[str, ...]


def _reject_constant(value: str):
    raise ValueError(f"non-finite JSON value: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _decode_json(raw: bytes, code: str):
    try:
        text = raw.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise DcoError(code, "input is not unambiguous UTF-8 JSON") from exc


def _read_event(path: str) -> dict:
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(MAX_EVENT_BYTES + 1)
    except OSError as exc:
        raise DcoError("EVENT_UNREADABLE", "GitHub event file is unreadable") from exc
    if len(raw) > MAX_EVENT_BYTES:
        raise DcoError("EVENT_TOO_LARGE", "GitHub event file exceeds the size limit")
    value = _decode_json(raw, "EVENT_JSON_INVALID")
    if not isinstance(value, dict):
        raise DcoError("EVENT_SHAPE_INVALID", "GitHub event must be a JSON object")
    return value


def _required_dict(value, field: str) -> dict:
    if not isinstance(value, dict):
        raise DcoError("API_SHAPE_INVALID", f"{field} must be an object")
    return value


def _required_string(value, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise DcoError("API_SHAPE_INVALID", f"{field} must be a non-empty string")
    return value


def _required_int(value, field: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise DcoError("API_SHAPE_INVALID", f"{field} must be an integer >= {minimum}")
    return value


def _required_sha(value, field: str) -> str:
    text = _required_string(value, field)
    if not _SHA_RE.fullmatch(text):
        raise DcoError("API_SHAPE_INVALID", f"{field} must be a full hexadecimal commit SHA")
    return text.lower()


def _same_repository(actual: str, expected: str, field: str) -> None:
    if actual.casefold() != expected.casefold():
        raise DcoError("REPOSITORY_MISMATCH", f"{field} does not match the workflow repository")


def _parse_event(event: dict, repository: str) -> PullRequestState:
    """Bind a pull-request event to the expected repository, IDs, SHAs, number, and user identity."""
    repository_parts = repository.split("/")
    if (
        not _REPOSITORY_RE.fullmatch(repository)
        or len(repository_parts) != 2
        or any(part in {".", ".."} for part in repository_parts)
    ):
        raise DcoError("REPOSITORY_INVALID", "workflow repository must be OWNER/REPO")
    event_repo = _required_dict(event.get("repository"), "event.repository")
    event_repo_name = _required_string(event_repo.get("full_name"), "event.repository.full_name")
    _same_repository(event_repo_name, repository, "event.repository.full_name")
    repository_id = _required_int(event_repo.get("id"), "event.repository.id")

    pull = _required_dict(event.get("pull_request"), "event.pull_request")
    number = _required_int(event.get("number"), "event.number")
    if _required_int(pull.get("number"), "event.pull_request.number") != number:
        raise DcoError("PR_MISMATCH", "event pull-request numbers disagree")

    base = _required_dict(pull.get("base"), "event.pull_request.base")
    base_repo = _required_dict(base.get("repo"), "event.pull_request.base.repo")
    base_repo_name = _required_string(base_repo.get("full_name"), "event.pull_request.base.repo.full_name")
    _same_repository(base_repo_name, repository, "event.pull_request.base.repo.full_name")
    if _required_int(base_repo.get("id"), "event.pull_request.base.repo.id") != repository_id:
        raise DcoError("REPOSITORY_MISMATCH", "event repository IDs disagree")

    head = _required_dict(pull.get("head"), "event.pull_request.head")
    head_repo = _required_dict(head.get("repo"), "event.pull_request.head.repo")
    user = _required_dict(pull.get("user"), "event.pull_request.user")
    return PullRequestState(
        repository=event_repo_name,
        repository_id=repository_id,
        number=number,
        head_sha=_required_sha(head.get("sha"), "event.pull_request.head.sha"),
        base_sha=_required_sha(base.get("sha"), "event.pull_request.base.sha"),
        head_repository=_required_string(head_repo.get("full_name"), "event.pull_request.head.repo.full_name"),
        user_login=_required_string(user.get("login"), "event.pull_request.user.login"),
        user_type=_required_string(user.get("type"), "event.pull_request.user.type"),
        commits=None,
    )


def _parse_api_pull(value: object, event: PullRequestState) -> PullRequestState:
    """Re-bind API pull metadata to the repository, PR, SHA, head-repository, and user event snapshot."""
    pull = _required_dict(value, "API pull request")
    number = _required_int(pull.get("number"), "API pull_request.number")
    if number != event.number:
        raise DcoError("PR_MISMATCH", "API pull-request number does not match the event")

    base = _required_dict(pull.get("base"), "API pull_request.base")
    base_repo = _required_dict(base.get("repo"), "API pull_request.base.repo")
    repository = _required_string(base_repo.get("full_name"), "API pull_request.base.repo.full_name")
    _same_repository(repository, event.repository, "API pull_request.base.repo.full_name")
    repository_id = _required_int(base_repo.get("id"), "API pull_request.base.repo.id")
    if repository_id != event.repository_id:
        raise DcoError("REPOSITORY_MISMATCH", "API and event repository IDs disagree")

    head = _required_dict(pull.get("head"), "API pull_request.head")
    head_repo = _required_dict(head.get("repo"), "API pull_request.head.repo")
    user = _required_dict(pull.get("user"), "API pull_request.user")
    state = PullRequestState(
        repository=repository,
        repository_id=repository_id,
        number=number,
        head_sha=_required_sha(head.get("sha"), "API pull_request.head.sha"),
        base_sha=_required_sha(base.get("sha"), "API pull_request.base.sha"),
        head_repository=_required_string(head_repo.get("full_name"), "API pull_request.head.repo.full_name"),
        user_login=_required_string(user.get("login"), "API pull_request.user.login"),
        user_type=_required_string(user.get("type"), "API pull_request.user.type"),
        commits=_required_int(pull.get("commits"), "API pull_request.commits"),
    )
    if (
        state.head_sha != event.head_sha
        or state.base_sha != event.base_sha
        or state.head_repository.casefold() != event.head_repository.casefold()
        or state.user_login != event.user_login
        or state.user_type != event.user_type
    ):
        raise DcoError("PR_STATE_MISMATCH", "API pull-request identity does not match the event snapshot")
    return state


def _parse_link_next(header: str | None) -> str | None:
    if not header:
        return None
    next_urls: list[str] = []
    for raw_part in header.split(","):
        part = raw_part.strip()
        if not part.startswith("<") or ">" not in part:
            raise DcoError("API_LINK_INVALID", "GitHub API returned a malformed Link header")
        url, params = part[1:].split(">", 1)
        rels: list[str] = []
        for raw_param in params.split(";"):
            param = raw_param.strip()
            if not param:
                continue
            if "=" not in param:
                raise DcoError("API_LINK_INVALID", "GitHub API returned a malformed Link parameter")
            key, value = param.split("=", 1)
            if key.strip().casefold() == "rel":
                rels.extend(value.strip().strip('"').split())
        if "next" in rels:
            next_urls.append(url)
    if len(next_urls) > 1:
        raise DcoError("API_LINK_INVALID", "GitHub API returned multiple next-page links")
    return next_urls[0] if next_urls else None


def _validate_api_url(
    url: str,
    allowed_paths: set[str],
    allowed_query: set[str],
) -> None:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise DcoError("API_LINK_INVALID", "GitHub API URL is malformed") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.github.com"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.path not in allowed_paths
    ):
        raise DcoError("API_LINK_INVALID", "GitHub API URL escaped the repository-bound endpoint")
    try:
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise DcoError("API_LINK_INVALID", "GitHub API URL has an invalid query") from exc
    if not set(query) <= allowed_query or any(len(values) != 1 for values in query.values()):
        raise DcoError("API_LINK_INVALID", "GitHub API URL has unexpected query fields")
    if "per_page" in query and query["per_page"] != ["100"]:
        raise DcoError("API_LINK_INVALID", "GitHub API pagination changed per_page")
    if "page" in query:
        try:
            if int(query["page"][0]) < 1:
                raise ValueError
        except ValueError as exc:
            raise DcoError("API_LINK_INVALID", "GitHub API page must be a positive integer") from exc


class GitHubApi:
    """Repository-bound GitHub REST client that fails closed on request-safety violations."""

    def __init__(self, token: str, repository: str, repository_id: int, opener: Callable | None = None) -> None:
        """Validate the bearer token and arm the total request deadline."""
        if not token or token != token.strip() or any(character.isspace() for character in token):
            raise DcoError("TOKEN_INVALID", "GITHUB_TOKEN is missing or malformed")
        self.token = token
        self.repository = repository
        self.repository_id = repository_id
        self.opener = opener or _open_request
        self.deadline = time.monotonic() + TOTAL_TIMEOUT_SECONDS
        self.request_count = 0

    def _request_json(
        self,
        url: str,
        allowed_paths: set[str],
        allowed_query: set[str],
    ) -> tuple[object, str | None]:
        """Fetch 200-only JSON after pre/post URL checks, bounded time, and a response-size cap."""
        _validate_api_url(url, allowed_paths, allowed_query)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise DcoError("API_TIMEOUT", "GitHub API validation exceeded its total timeout")
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "agent-kaizen-dco-validator",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
        try:
            with self.opener(request, min(REQUEST_TIMEOUT_SECONDS, remaining)) as response:
                status = getattr(response, "status", None)
                if status is None:
                    status = response.getcode()
                if status != 200:
                    raise DcoError("API_HTTP_ERROR", f"GitHub API returned HTTP {status}")
                final_url = response.geturl()
                _validate_api_url(final_url, allowed_paths, allowed_query)
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                link = response.headers.get("Link")
        except DcoError:
            raise
        except urllib.error.HTTPError as exc:
            raise DcoError("API_HTTP_ERROR", f"GitHub API returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DcoError("API_UNAVAILABLE", "GitHub API request failed or timed out") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise DcoError("API_RESPONSE_TOO_LARGE", "GitHub API response exceeds the size limit")
        self.request_count += 1
        return _decode_json(raw, "API_JSON_INVALID"), _parse_link_next(link)

    def pull(self, number: int) -> object:
        """Fetch repository-bound pull metadata and reject unexpected pagination."""
        path = f"/repos/{self.repository}/pulls/{number}"
        numeric_path = f"/repositories/{self.repository_id}/pulls/{number}"
        value, next_url = self._request_json(
            API_ROOT + path,
            {path, numeric_path},
            set(),
        )
        if next_url is not None:
            raise DcoError("API_LINK_INVALID", "pull-request metadata unexpectedly paginated")
        return value

    def compare_pages(self, base_sha: str, head_sha: str) -> Iterable[dict]:
        """Yield repository-bound three-dot compare pages with bounded, loop-safe pagination."""
        comparison = f"{base_sha}...{head_sha}"
        path = f"/repos/{self.repository}/compare/{comparison}"
        numeric_path = f"/repositories/{self.repository_id}/compare/{comparison}"
        query = urllib.parse.urlencode({"per_page": 100})
        url: str | None = f"{API_ROOT}{path}?{query}"
        seen: set[str] = set()
        for _ in range(MAX_API_PAGES):
            if url is None:
                return
            if url in seen:
                raise DcoError("API_PAGINATION_LOOP", "GitHub API pagination looped")
            seen.add(url)
            value, url = self._request_json(
                url,
                {path, numeric_path},
                {"per_page", "page"},
            )
            page = _required_dict(value, "GitHub compare page")
            commits = page.get("commits")
            if not isinstance(commits, list):
                raise DcoError("API_SHAPE_INVALID", "GitHub compare commits must be a JSON array")
            if not commits and url is not None:
                raise DcoError("API_PAGINATION_INVALID", "empty GitHub API page has a next link")
            yield page
        if url is not None:
            raise DcoError("API_PAGE_LIMIT", "GitHub API pagination exceeded the safety limit")


def _parse_commit(value: object) -> CommitData:
    entry = _required_dict(value, "API commit entry")
    sha = _required_sha(entry.get("sha"), "API commit.sha")
    commit = _required_dict(entry.get("commit"), "API commit.commit")
    message = commit.get("message")
    if not isinstance(message, str):
        raise DcoError("API_SHAPE_INVALID", "API commit.commit.message must be a string")

    author = entry.get("author")
    if author is None:
        author_login = None
        author_type = None
    else:
        author_object = _required_dict(author, "API commit.author")
        author_login = _required_string(author_object.get("login"), "API commit.author.login")
        author_type = _required_string(author_object.get("type"), "API commit.author.type")

    parents_value = entry.get("parents")
    if not isinstance(parents_value, list):
        raise DcoError("API_SHAPE_INVALID", "API commit.parents must be an array")
    parents = tuple(
        _required_sha(_required_dict(parent, "API commit parent").get("sha"), "API commit parent.sha")
        for parent in parents_value
    )
    if len(set(parents)) != len(parents):
        raise DcoError("API_SHAPE_INVALID", "API commit has duplicate parents")
    return CommitData(sha, message, author_login, author_type, parents)


def _append_unique(target: list[CommitData], seen: dict[str, CommitData], raw_page: list) -> None:
    for raw_entry in raw_page:
        entry = _parse_commit(raw_entry)
        if entry.sha in seen:
            raise DcoError("API_COMMIT_DUPLICATE", "GitHub API returned a duplicate commit SHA")
        seen[entry.sha] = entry
        target.append(entry)


def _collect_commits(api: GitHubApi, state: PullRequestState) -> tuple[list[CommitData], str, int]:
    """Enforce commit-count, base/head SHA, and cross-page merge-base binding invariants."""
    assert state.commits is not None
    commits: list[CommitData] = []
    by_sha: dict[str, CommitData] = {}
    pages = 0
    merge_base_sha: str | None = None
    for page in api.compare_pages(state.base_sha, state.head_sha):
        pages += 1
        total = _required_int(page.get("total_commits"), "GitHub compare total_commits")
        ahead = _required_int(page.get("ahead_by"), "GitHub compare ahead_by", minimum=0)
        base_commit = _required_dict(page.get("base_commit"), "GitHub compare base_commit")
        merge_base = _required_dict(page.get("merge_base_commit"), "GitHub compare merge_base_commit")
        page_base_sha = _required_sha(base_commit.get("sha"), "GitHub compare base_commit.sha")
        page_merge_base_sha = _required_sha(
            merge_base.get("sha"),
            "GitHub compare merge_base_commit.sha",
        )
        if total != state.commits or ahead != total:
            raise DcoError("COMMIT_COUNT_MISMATCH", "PR metadata and compare commit counts disagree")
        if page_base_sha != state.base_sha:
            raise DcoError("BASE_SHA_MISMATCH", "compare response is not bound to the current base SHA")
        if merge_base_sha is None:
            merge_base_sha = page_merge_base_sha
        elif merge_base_sha != page_merge_base_sha:
            raise DcoError("API_PAGE_DRIFT", "compare pages disagree on the merge-base SHA")
        _append_unique(commits, by_sha, page["commits"])
        if len(commits) > total:
            raise DcoError("COMMIT_COUNT_MISMATCH", "compare pages exceeded the declared commit count")

    if len(commits) != state.commits:
        raise DcoError("COMMIT_COVERAGE_INCOMPLETE", "compare pagination did not cover every PR commit")
    if not commits or commits[-1].sha != state.head_sha:
        raise DcoError("HEAD_SHA_MISMATCH", "compare commit set is not bound to the current head SHA")
    return commits, "compare-base-head", pages


def _is_valid_signoff_line(line: str) -> bool:
    """Accept a safe DCO trailer with non-empty local/domain parts without requiring full RFC 5322 syntax."""
    prefix = "Signed-off-by: "
    if not line.startswith(prefix) or not line.endswith(">"):
        return False
    name, separator, address = line[len(prefix):].rpartition(" <")
    if (
        separator != " <"
        or not name
        or name != name.strip()
        or any(character in "<>" or ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        return False
    email = address[:-1]
    if email.count("@") != 1 or any(
        character.isspace() or character in "<>" or ord(character) < 32 or ord(character) == 127
        for character in email
    ):
        return False
    local, domain = email.split("@", 1)
    return bool(local) and bool(domain)


def message_has_signoff(message: str) -> bool:
    """Return whether a commit message contains a valid whole-line DCO trailer."""
    if not isinstance(message, str):
        return False
    normalized = message.replace("\r\n", "\n").replace("\r", "\n")
    return any(_is_valid_signoff_line(line) for line in normalized.split("\n"))


def _dependabot_pr_is_trusted(event: PullRequestState, api_state: PullRequestState) -> bool:
    return (
        event.user_login == EXEMPT_BOT_LOGIN
        and event.user_type == "Bot"
        and api_state.user_login == EXEMPT_BOT_LOGIN
        and api_state.user_type == "Bot"
        and event.head_repository.casefold() == event.repository.casefold()
        and api_state.head_repository.casefold() == api_state.repository.casefold()
    )


def evaluate(commits: list[CommitData], *, exempt_dependabot: bool) -> dict:
    """Return a deterministic DCO verdict for already validated API commit objects."""
    failed: list[dict[str, str]] = []
    exempt: list[str] = []
    checked = 0
    for entry in commits:
        if (
            exempt_dependabot
            and entry.author_login == EXEMPT_BOT_LOGIN
            and entry.author_type == "Bot"
        ):
            exempt.append(entry.sha)
            continue
        checked += 1
        if not message_has_signoff(entry.message):
            failed.append({"sha": entry.sha, "reason": "missing a valid Signed-off-by trailer"})
    return {
        "total": len(commits),
        "checked": checked,
        "exempt": exempt,
        "failed": failed,
    }


def validate(event: dict, repository: str, token: str, opener: Callable | None = None) -> dict:
    """Fetch, bind, and validate one current pull request."""
    event_state = _parse_event(event, repository)
    api = GitHubApi(token, repository, event_state.repository_id, opener)
    initial = _parse_api_pull(api.pull(event_state.number), event_state)
    commits, coverage, _ = _collect_commits(api, initial)
    final = _parse_api_pull(api.pull(event_state.number), event_state)
    if final != initial:
        raise DcoError("PR_CHANGED_DURING_VALIDATION", "pull request changed during DCO validation")

    result = evaluate(commits, exempt_dependabot=_dependabot_pr_is_trusted(event_state, initial))
    ok = not result["failed"]
    envelope = {
        "status": "OK" if ok else "DENIED",
        "op": "validate-dco",
        "repository": initial.repository,
        "pull_request": initial.number,
        "head_sha": initial.head_sha,
        "coverage": coverage,
        "api_pages": api.request_count,
        **result,
    }
    if not ok:
        envelope["required_action"] = "sign every commit with 'git commit -s'; see CONTRIBUTING.md"
    return envelope


def _emit_error(exc: DcoError, *, json_mode: bool, stdout: TextIO, stderr: TextIO) -> int:
    envelope = {
        "status": "ERROR",
        "op": "validate-dco",
        "error_code": exc.code,
        "reason": exc.reason,
    }
    if json_mode:
        print(json.dumps(envelope, sort_keys=True), file=stdout)
    print(f"validate-dco: ERROR {exc.code}: {exc.reason}", file=stderr)
    return 1


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    opener: Callable | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    environment = os.environ if env is None else env
    parser = _ArgumentParser(description="Validate DCO sign-offs on every current pull-request commit.")
    parser.add_argument("--event-file", help="GitHub pull_request event JSON; defaults to GITHUB_EVENT_PATH")
    parser.add_argument("--repository", help="expected OWNER/REPO; defaults to GITHUB_REPOSITORY")
    parser.add_argument("--json", action="store_true", help="write one machine-readable envelope to stdout")
    parser.add_argument("--debug", action="store_true", help="write an internal-error traceback to stderr")
    raw_argv = argv if argv is not None else sys.argv[1:]
    json_mode = "--json" in raw_argv
    debug_mode = "--debug" in raw_argv
    try:
        args = parser.parse_args(argv)
        json_mode = args.json
        event_file = args.event_file or environment.get("GITHUB_EVENT_PATH")
        repository = args.repository or environment.get("GITHUB_REPOSITORY")
        token = environment.get("GITHUB_TOKEN")
        if not event_file:
            raise DcoError("EVENT_PATH_MISSING", "--event-file or GITHUB_EVENT_PATH is required")
        if not repository:
            raise DcoError("REPOSITORY_MISSING", "--repository or GITHUB_REPOSITORY is required")
        if token is None:
            raise DcoError("TOKEN_MISSING", "GITHUB_TOKEN is required")
        envelope = validate(_read_event(event_file), repository, token, opener)
    except DcoError as exc:
        return _emit_error(exc, json_mode=json_mode, stdout=stdout, stderr=stderr)
    except Exception as exc:  # fail closed without leaking untrusted data or credentials
        if debug_mode:
            traceback.print_exc(file=stderr)
        safe = DcoError("INTERNAL_ERROR", f"unexpected internal error ({type(exc).__name__})")
        return _emit_error(safe, json_mode=json_mode, stdout=stdout, stderr=stderr)

    if json_mode:
        print(json.dumps(envelope, sort_keys=True), file=stdout)
    print(
        f"validate-dco: {envelope['checked']} checked, {len(envelope['exempt'])} exempt, "
        f"{len(envelope['failed'])} unsigned; coverage={envelope['coverage']}",
        file=stderr,
    )
    for item in envelope["failed"]:
        print(f"  unsigned: {item['sha']} - {item['reason']}", file=stderr)
    return 0 if envelope["status"] == "OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
