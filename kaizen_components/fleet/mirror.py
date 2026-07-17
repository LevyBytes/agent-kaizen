"""Git mirroring + strict conflict handling (v8 M12, plan §B.4).

Git = the DATA plane; the coord ledger = the CONTROL plane. This module composes with (never
duplicates) :class:`fleet.coordination.HandoffEngine` (the shadow push-before-release sequence) and the
lease reducers: it adds the ENFORCEMENT leg the plan calls the "new holder acquires" step -- divergence
computation over an injected git runner, the single FF-only auto-move, surface-and-confirm fork
handling, the merge-conflict blocking span, parallel-apply fan-in, and the peer-fetch fallback.

Invariants held here (plan §B.4 / §C.1):

- **FF is the ONLY auto-move.** A clean fast-forward to a confirmed watermark is the sole tree mutation
  this module performs automatically; a true fork NEVER auto-merges -- it records a
  ``Decision | Options | Recommendation | Confirmation`` prompt and (when enforcing) refuses.
- **Hub push stays inside the git-push code invariant.** This module NEVER pushes; push lives in
  :class:`HandoffEngine` behind its explicit ``allow_push`` gate (labs use scratch bare remotes; the
  owner pushes real remotes). Mirror only READS (fetch/rev-list/diff) and does holder-side merges.
- **Transport gate (§C.1).** Plain HTTP git is legitimate ONLY over the tailnet (host ends in the
  tailnet suffix AND ``on_tailnet()``); local paths and ssh forms are always fine; everything else on
  http(s) refuses ``DENIED_MIRROR_TRANSPORT``.
- **Record-then-refuse (the M10b house pattern).** Every refusal FIRST appends the audit coord_event
  (``divergence/detected`` or ``conflict/detected``) -- append-only commits immediately, so the event
  survives the raise -- THEN raises.
- **Redaction caution.** Only shas / scope keys / short reasons / peer INDEXES (never absolute local
  paths or remote URLs) ever enter a synced coord_event payload; absolute scratch/home paths trip the
  redaction pass-path (``user_home_path``). The HandoffEngine basename-only precedent is followed.

Divergence is computed over an injected ``GitRunner`` (the same shape as
:func:`fleet.coordination._default_git_runner`): tests inject a runner bound to a SCRATCH repo, so this
module never defaults to acting on the real repo.
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlsplit

from ..denials import KaizenDenied
from . import net
from .coordination import GitRunner, _default_git_runner, current_leases

# --- env + naming (plan §C.1 knobs) --------------------------------------------------------------

HUB_REMOTE_ENV = "KAIZEN_GIT_HUB_REMOTE"
TAILNET_SUFFIX_ENV = "KAIZEN_TAILNET_SUFFIX"
DEFAULT_TAILNET_SUFFIX = ".ts.net"

# The §B.4 fork-decision option menu (the recorded Decision|Options|Recommendation|Confirmation).
FORK_OPTIONS = ["rebase", "merge-on-holder", "parallel-apply-fan-in", "discard-one"]
FORK_RECOMMENDATION = "merge-on-holder"
_SCP_SSH_REMOTE = re.compile(r"^[A-Za-z0-9._-]+@(?:[A-Za-z0-9._-]+|\[[0-9A-Fa-f:.]+\]):\S+$")


def _tailnet_suffix(suffix_env: Any = None) -> str:
    """The configured tailnet suffix (``KAIZEN_TAILNET_SUFFIX``, default ``.ts.net``). ``suffix_env`` is
    the mapping to read (defaults to ``os.environ``); tests inject a plain dict."""
    env = suffix_env if suffix_env is not None else os.environ
    value = env.get(TAILNET_SUFFIX_ENV)
    return value if value else DEFAULT_TAILNET_SUFFIX


def node_branch(task: str, node_id: str, worktree: int | None = None) -> str:
    """The per-node branch name ``kz/<task>/<node>[/w<n>]`` (plan §B.4 branch-per-node).

    ``task`` is sanitized: an empty/whitespace task, a task containing whitespace, or one carrying a
    path-traversal / separator segment (``..``, ``/``, ``\\``) refuses ``DENIED_MIRROR_BRANCH_INVALID``
    -- a branch name is a ref path, so an unsanitized task could escape the ``kz/`` namespace."""
    raw = task if isinstance(task, str) else ""
    stripped = raw.strip()
    bad = (
        not stripped
        or any(ch.isspace() for ch in raw)
        or ".." in stripped
        or "/" in stripped
        or "\\" in stripped
        or stripped.startswith("-")
    )
    if bad:
        raise KaizenDenied(
            "DENIED_MIRROR_BRANCH_INVALID",
            {
                "task": raw,
                "required_action": "task must be a single non-empty token with no spaces, slashes, or '..'",
            },
            exit_code=2,
        )
    node = node_id.strip() if isinstance(node_id, str) else ""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", node) or ".." in node or node.endswith((".", ".lock")):
        raise KaizenDenied(
            "DENIED_MIRROR_BRANCH_INVALID",
            {"node_id": node_id, "required_action": "node_id must be a single non-empty ref-safe token"},
            exit_code=2,
        )
    branch = f"kz/{stripped}/{node}"
    if worktree is not None:
        branch = f"{branch}/w{int(worktree)}"
    return branch


def branch_scope_key(project_id: str, branch: str) -> str:
    """The default lease scope for a branch: ``<project_id>/<branch>`` (plan §B.4 -- default lease scope
    is the branch)."""
    return f"{project_id}/{branch}"


# --- transport safety (plan §C.1: plain HTTP git only over the tailnet) ---------------------------

def _looks_local_path(remote: str) -> bool:
    """True when ``remote`` is a filesystem path (labs / bare repos on disk), not a network URL.

    Local forms: a Windows drive spec (``C:\\...`` / ``C:/...``); a rooted POSIX path (``/srv/...``);
    a ``file://`` URL; or a plain relative path with no ``scheme://`` and no ``user@host:``
    ssh-scp shape. A URL scheme (``http://``, ``ssh://``, ``git://``) or the scp-like ``user@host:path``
    is NOT local."""
    text = remote.strip()
    if not text:
        return False
    if text.lower().startswith("file://"):
        return True
    # Windows drive-letter path: C:\repo or C:/repo (two+ leading alpha => a scheme, handled below).
    if len(text) >= 3 and text[1] == ":" and text[0].isalpha() and text[2] in ("\\", "/"):
        return True
    # Rooted POSIX path.
    if text.startswith("/"):
        return True
    # A scheme:// URL or an scp-like user@host:path is a network remote, not a local path.
    if "://" in text:
        return False
    # scp-like ssh shorthand user@host:path (a ':' before any '/').
    colon = text.find(":")
    slash = text.find("/")
    if "@" in text and colon != -1 and (slash == -1 or colon < slash):
        return False
    # Otherwise a bare relative path (no scheme, no scp shape) -- treat as local (labs).
    return True


def assert_remote_transport_safe(
    remote: str,
    *,
    tailnet_probe: Any = net.on_tailnet,
    suffix_env: Any = None,
) -> None:
    """Refuse ``DENIED_MIRROR_TRANSPORT`` for a remote that is not transport-safe (plan §C.1).

    Allowed unconditionally: local filesystem paths / ``file://`` (labs, bare repos on disk) and ssh
    forms (validated ``user@host:path`` or ``ssh://...``, WireGuard-independent, already encrypted). ``http(s)://``
    is allowed ONLY when the host ends in the configured tailnet suffix AND ``tailnet_probe()`` is True
    -- plain HTTP git is legitimate solely over the tailnet's WireGuard E2E encryption; a public
    http(s) remote, or a tailnet-named host while OFF the tailnet, refuses."""
    text = (remote or "").strip()
    if not text:
        raise KaizenDenied(
            "DENIED_MIRROR_TRANSPORT",
            {"remote": remote, "required_action": "provide a local path, ssh remote, or a tailnet http(s) remote"},
            exit_code=2,
        )
    if _looks_local_path(text):
        return
    lowered = text.lower()
    if lowered.startswith("ssh://") or _SCP_SSH_REMOTE.fullmatch(text):
        return
    if lowered.startswith("http://") or lowered.startswith("https://"):
        suffix = _tailnet_suffix(suffix_env)
        host = _url_host(text)
        on_tailnet = bool(tailnet_probe()) if callable(tailnet_probe) else bool(tailnet_probe)
        if host.endswith(suffix.lower()) and on_tailnet:
            return
        raise KaizenDenied(
            "DENIED_MIRROR_TRANSPORT",
            {
                "remote": remote,
                "host": host,
                "tailnet_suffix": suffix,
                "on_tailnet": on_tailnet,
                "required_action": (
                    "plain http(s) git is allowed ONLY over the tailnet (host ends in the tailnet "
                    "suffix AND on_tailnet()); use ssh or a local path otherwise"
                ),
            },
            exit_code=2,
        )
    # Any other scheme (git://, ftp://, ...) is not an approved transport.
    raise KaizenDenied(
        "DENIED_MIRROR_TRANSPORT",
        {"remote": remote, "required_action": "use a local path, ssh remote, or a tailnet http(s) remote"},
        exit_code=2,
    )


def _url_host(url: str) -> str:
    """Return a lowercased HTTP(S) hostname, including an IPv6 literal without brackets."""
    try:
        parsed = urlsplit(url.strip())
        if parsed.scheme.lower() not in ("http", "https"):
            return ""
        return (parsed.hostname or "").lower()
    except ValueError:
        return ""


# --- git runner helpers --------------------------------------------------------------------------

def _runner_for(git_runner: GitRunner | None, repo_root: str | None) -> GitRunner:
    """The injected runner, or a default runner bound to ``repo_root`` (tests always inject; the default
    is a convenience for a caller that passes a repo_root instead)."""
    if git_runner is not None:
        return git_runner
    if repo_root is None:
        raise KaizenDenied(
            "DENIED_MIRROR_TRANSPORT",
            {"required_action": "pass a git_runner or a repo_root"},
            exit_code=2,
        )
    return _default_git_runner(str(repo_root))


def _git_out(git: GitRunner, *args: str) -> tuple[int, str]:
    """Run the non-raising GitRunner and return ``(returncode, stripped stdout)``."""
    proc = git(*args)
    return proc.returncode, (proc.stdout or "").strip()


# --- divergence computation (pure-ish over an injected GitRunner) ---------------------------------

def _ahead_behind(git: GitRunner, left_ref: str, right_ref: str) -> tuple[int, int, bool]:
    """Return ``(ahead, behind, computed)`` for ``left_ref...right_ref``."""
    rc, out = _git_out(git, "rev-list", "--left-right", "--count", f"{left_ref}...{right_ref}")
    if rc != 0 or not out:
        return (0, 0, False)
    parts = out.split()
    if len(parts) < 2:
        return (0, 0, False)
    try:
        return (int(parts[1]), int(parts[0]), True)
    except ValueError:
        return (0, 0, False)


def ahead_behind(git: GitRunner, watermark_sha: str, right_ref: str = "HEAD") -> tuple[int, int]:
    """``(ahead, behind)`` of ``right_ref`` relative to ``watermark_sha`` via rev-list.

    The left count is watermark-only commits (commits HEAD is BEHIND), the right count is HEAD-only
    commits (AHEAD) -- verified empirically. A non-zero rev-list (unknown sha, gitless) yields ``(0, 0)``
    (nothing computable)."""
    ahead, behind, _computed = _ahead_behind(git, watermark_sha, right_ref)
    return (ahead, behind)


def classify(dirty: bool | None, ahead: int, behind: int, was_holder: bool) -> list[str]:
    """The §B.4 divergence taxonomy for a snapshot: a list of reason strings (empty == clean).

    - ``DIRTY_NON_HOLDER`` -- uncommitted work in a tree this node does not hold.
    - ``BEHIND_WATERMARK`` -- HEAD is strictly behind and NOT ahead (a clean FF is possible).
    - ``DIVERGED_FORK`` -- ahead AND behind (a true fork; HEAD != watermark, no FF).

    (``STALE_REPLICA_EPOCH`` and ``STALE_LEDGER`` are ledger-level, added by :func:`acquire_scope`;
    ``HEAD_NOT_WATERMARK`` is covered by the ahead/behind pair.)"""
    reasons: list[str] = []
    if dirty and not was_holder:
        reasons.append("DIRTY_NON_HOLDER")
    if behind > 0 and ahead > 0:
        reasons.append("DIVERGED_FORK")
    elif behind > 0:
        reasons.append("BEHIND_WATERMARK")
    return reasons


# --- audit appends (record-then-refuse) ----------------------------------------------------------

def _record_divergence(store: Any, scope_key: str, reasons: list[str], *, enforced: bool, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """Append ``divergence/detected`` carrying the refusal shape (the M10b record-then-refuse audit).
    Only shas/scope/reasons enter the payload (never absolute paths -- the redaction pass-path)."""
    key = "blocked" if enforced else "would_block"
    payload: dict[str, Any] = {key: "DENIED_COORD_DIVERGED", "reasons": list(reasons), "enforced": bool(enforced)}
    if detail:
        payload.update(detail)
    return store.append_coord_event(
        "divergence",
        "detected",
        summary=f"Scope divergence on {scope_key} ({', '.join(reasons) or 'diverged'}); enforced={enforced}.",
        scope_key=scope_key,
        payload=payload,
    )


def record_fork_decision(store: Any, scope_key: str, detail: dict[str, Any]) -> dict[str, Any]:
    """Append the §B.4 surface-and-confirm ``conflict/detected`` -- the recorded
    ``Decision | Options | Recommendation | Confirmation`` prompt.

    Payload: ``{decision, options (the fork menu), recommendation "merge-on-holder", confirmation
    "pending", ...detail}``. ``detail`` carries ONLY shas/counts (ahead/behind/head_sha/watermark_sha),
    never absolute paths."""
    payload: dict[str, Any] = {
        "decision": f"fork on {scope_key}",
        "options": list(FORK_OPTIONS),
        "recommendation": FORK_RECOMMENDATION,
        "confirmation": "pending",
    }
    payload.update(detail or {})
    return store.append_coord_event(
        "conflict",
        "detected",
        summary=f"Fork on {scope_key}: choose {'/'.join(FORK_OPTIONS)} (recommend {FORK_RECOMMENDATION}).",
        scope_key=scope_key,
        payload=payload,
    )


def resolve_conflict(store: Any, conflict_event_id: str, scope_key: str, resolution_summary: str, chosen: str) -> dict[str, Any]:
    """Close a fork/merge-conflict blocking span: append ``conflict/resolved``
    ``{source_conflict_id, chosen}`` then ``resolution/recorded``. The ``source_conflict_id`` links the
    resolution to the open ``conflict/detected`` so :func:`fleet.reducers.reduce_conflicts` marks it
    closed."""
    resolved = store.append_coord_event(
        "conflict",
        "resolved",
        summary=resolution_summary or f"Conflict on {scope_key} resolved ({chosen}).",
        scope_key=scope_key,
        payload={"source_conflict_id": conflict_event_id, "chosen": chosen},
    )
    recorded = store.append_coord_event(
        "resolution",
        "recorded",
        summary=f"Resolution recorded for {scope_key} ({chosen}).",
        scope_key=scope_key,
        payload={"source_conflict_id": conflict_event_id, "chosen": chosen},
    )
    return {"status": "OK", "scope_key": scope_key, "chosen": chosen, "resolved_id": resolved["id"], "recorded_id": recorded["id"]}


# --- acquire_scope: the §B.4 "new holder acquires" leg (post-grant verify) ------------------------

def acquire_scope(
    store: Any,
    git_runner: GitRunner | None,
    scope_key: str,
    *,
    watermark_sha: str | None,
    enforce: bool = True,
    was_prior_holder: bool = False,
    repo_root: str | None = None,
    tailnet_probe: Any = net.on_tailnet,
) -> dict[str, Any]:
    """The §B.4 "new holder acquires" step: after the grant, VERIFY the working tree against the
    handed-off watermark and either clean-resume, FF-only auto-move, or surface-and-refuse a divergence.

    Order (each refusal FIRST records ``divergence/detected`` -- record-then-refuse):

    1. **stale-replica-epoch** -- the reduced max epoch over the coord ledger is BELOW this node's
       un-synced watermark (``store.watermark()``): the replica has not pulled, so its git watermark is
       untrustworthy ⇒ divergence ``STALE_REPLICA_EPOCH`` (pull first).
    2. snapshot dirty + HEAD via git; dirty AND NOT ``was_prior_holder`` ⇒ ``DIRTY_NON_HOLDER``.
    3. ``watermark_sha is None`` ⇒ a fresh scope, nothing to verify ⇒ ``{status OK, action "fresh"}``.
    4. ``HEAD == watermark`` ⇒ clean resume ⇒ ``{status OK, action "resume"}``.
    5. ``behind>0, ahead==0`` ⇒ the ONLY auto-move: ``git merge --ff-only <watermark_sha>`` (the sha
       must be present locally -- the caller fetches first; a failed FF is treated as a fork) ⇒
       ``{action "ff-moved"}``.
    6. ``ahead>0 and behind>0`` (a true fork, or a failed FF) ⇒ :func:`record_fork_decision` then, when
       ``enforce`` -- raise ``DENIED_COORD_DIVERGED`` carrying the decision event id; when shadow
       (``enforce=False``) -- return the recorded decision non-blocking.

    ``enforce`` defaults True (F3 is live); callers pass ``enforce=False`` for F2-shadow parity."""
    git = _runner_for(git_runner, repo_root)

    # (1) stale-replica-epoch: the ledger is behind this node's un-synced watermark => the replica has
    # not pulled; its git watermark cannot be trusted. Record-then-refuse before touching git.
    stale = _stale_replica_epoch(store)
    if stale is not None:
        reasons = ["STALE_REPLICA_EPOCH"]
        detail = {"ledger_epoch": stale[0], "watermark": stale[1]}
        event = _record_divergence(store, scope_key, reasons, enforced=enforce, detail=detail)
        if enforce:
            raise KaizenDenied(
                "DENIED_COORD_DIVERGED",
                {"scope_key": scope_key, "reasons": reasons, "divergence_event_id": event["id"], **detail,
                 "required_action": "pull the coord ledger (turso.sync) before acquiring this scope"},
                exit_code=2,
            )
        return {"status": "RECORDED", "scope_key": scope_key, "action": "diverged", "reasons": reasons, "divergence_event_id": event["id"]}

    # (2) snapshot dirty + HEAD.
    status_rc, status_out = _git_out(git, "status", "--porcelain")
    dirty = bool(status_out) if status_rc == 0 else None
    head_rc, head_sha = _git_out(git, "rev-parse", "HEAD")
    head_sha = head_sha if head_rc == 0 else None

    snapshot_reasons = classify(dirty, 0, 0, was_prior_holder)
    if "DIRTY_NON_HOLDER" in snapshot_reasons:
        reasons = snapshot_reasons
        detail = {"head_sha": head_sha}
        event = _record_divergence(store, scope_key, reasons, enforced=enforce, detail=detail)
        if enforce:
            raise KaizenDenied(
                "DENIED_COORD_DIVERGED",
                {"scope_key": scope_key, "reasons": reasons, "divergence_event_id": event["id"], **detail,
                 "required_action": "commit/stash the dirty tree or acquire as the prior holder"},
                exit_code=2,
            )
        return {"status": "RECORDED", "scope_key": scope_key, "action": "diverged", "reasons": reasons, "divergence_event_id": event["id"]}

    # (3) fresh scope: no watermark to verify.
    if not watermark_sha:
        return {"status": "OK", "scope_key": scope_key, "action": "fresh", "head_sha": head_sha}

    # (4) clean resume.
    if head_sha is not None and head_sha == watermark_sha:
        return {"status": "OK", "scope_key": scope_key, "action": "resume", "head_sha": head_sha}

    ahead, behind = ahead_behind(git, watermark_sha)

    # (5) FF-only auto-move (behind-only). THE ONLY AUTO-MOVE.
    if behind > 0 and ahead == 0:
        ff_rc, _ = _git_out(git, "merge", "--ff-only", watermark_sha)
        if ff_rc == 0:
            new_rc, new_head = _git_out(git, "rev-parse", "HEAD")
            return {
                "status": "OK", "scope_key": scope_key, "action": "ff-moved",
                "head_sha": new_head if new_rc == 0 else watermark_sha, "watermark_sha": watermark_sha,
                "head_read_failed": new_rc != 0,
            }
        # A failed FF collapses to the fork path below (the sha may be absent locally, or refs moved).

    # (6) true fork (ahead AND behind, or a failed FF) => surface-and-confirm.
    detail = {"ahead": ahead, "behind": behind, "head_sha": head_sha, "watermark_sha": watermark_sha}
    decision = record_fork_decision(store, scope_key, detail)
    _record_divergence(store, scope_key, ["DIVERGED_FORK"], enforced=enforce, detail={"conflict_event_id": decision["id"], **detail})
    if enforce:
        raise KaizenDenied(
            "DENIED_COORD_DIVERGED",
            {
                "scope_key": scope_key,
                "reasons": ["DIVERGED_FORK"],
                "conflict_event_id": decision["id"],
                **detail,
                "required_action": f"resolve the fork: {'/'.join(FORK_OPTIONS)} (recommend {FORK_RECOMMENDATION})",
            },
            exit_code=2,
        )
    return {
        "status": "RECORDED", "scope_key": scope_key, "action": "fork",
        "reasons": ["DIVERGED_FORK"], "conflict_event_id": decision["id"], **detail,
    }


def _stale_replica_epoch(store: Any) -> tuple[int, int] | None:
    """``(ledger_epoch, watermark)`` when the reduced max epoch is BELOW the stored watermark (the
    replica has not pulled -- kaizen.db watermark ahead of the synced ledger), else None.

    A negative/absent watermark (fresh node) never trips this. Mirrors the §B.3 max-epoch-wins reading:
    the un-synced watermark is the high-water mark, so a ledger below it is behind."""
    from . import reducers

    try:
        watermark = int(store.watermark())
    except (AttributeError, TypeError, ValueError):
        return None
    if watermark <= -1:
        return None
    ledger_epoch = reducers.max_epoch(store.coord_events())
    if ledger_epoch < watermark:
        return (ledger_epoch, watermark)
    return None


# --- merge-conflict blocking span ----------------------------------------------------------------

def _is_active_holder(store: Any, scope_key: str) -> bool:
    """True when THIS node is the reduced ACTIVE holder of ``scope_key`` (a held lease whose holder is
    ``store.node_id``)."""
    active = current_leases(store).get(scope_key)
    return bool(active and active.get("state") == "held" and active.get("holder") == store.node_id)


def attempt_holder_merge(
    store: Any,
    git_runner: GitRunner | None,
    scope_key: str,
    other_branch: str,
    *,
    holder_check: bool = True,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Holder-side ``git merge --no-ff --no-edit <other_branch>`` (the §B.4 merge-on-holder path).

    ``holder_check`` (default True): refuse ``DENIED_MIRROR_NOT_HOLDER`` unless this node is the reduced
    active holder of ``scope_key``. On a merge CONFLICT (non-zero exit AND conflict markers in
    ``status --porcelain``): ``git merge --abort`` (restore the clean tree), append ``conflict/detected``
    (an OPEN blocking span that blocks completion like an open approval), and raise
    ``DENIED_MIRROR_MERGE_CONFLICT`` carrying the event id. On success return the merge sha. NEVER
    auto-resolves."""
    git = _runner_for(git_runner, repo_root)
    if holder_check and not _is_active_holder(store, scope_key):
        raise KaizenDenied(
            "DENIED_MIRROR_NOT_HOLDER",
            {
                "scope_key": scope_key,
                "node_id": store.node_id,
                "required_action": "only the active lease holder may merge on the integration branch",
            },
            exit_code=2,
        )
    merge_rc, _ = _git_out(git, "merge", "--no-ff", "--no-edit", other_branch)
    if merge_rc != 0:
        # Distinguish a real conflict (unmerged paths) from any other merge failure; both abort and
        # refuse, but ONLY a real conflict opens the first-class blocking span -- a plain failure
        # (unknown branch, unrelated histories) must not pollute the digest with a false conflict.
        _status_rc, status_out = _git_out(git, "status", "--porcelain")
        conflicted = _has_conflict_markers(status_out)
        if conflicted:
            _git_out(git, "merge", "--abort")
        fields: dict[str, Any] = {
            "scope_key": scope_key,
            "other_branch": other_branch,
            "conflicted": conflicted,
            "required_action": "resolve the conflict off-band then resolve_conflict(); the tree was restored clean"
            if conflicted
            else "merge failed without conflict markers (unknown branch / unrelated history); fix the ref and retry",
        }
        if conflicted:
            event = store.append_coord_event(
                "conflict",
                "detected",
                summary=f"Merge conflict on {scope_key} merging {other_branch}; aborted, blocking span open.",
                scope_key=scope_key,
                payload={
                    "decision": f"merge conflict on {scope_key}",
                    "options": list(FORK_OPTIONS),
                    "recommendation": FORK_RECOMMENDATION,
                    "confirmation": "pending",
                    "other_branch": other_branch,
                    "conflict": True,
                },
            )
            fields["conflict_event_id"] = event["id"]
        raise KaizenDenied("DENIED_MIRROR_MERGE_CONFLICT", fields, exit_code=2)
    rc, merge_sha = _git_out(git, "rev-parse", "HEAD")
    return {"status": "OK", "scope_key": scope_key, "merge_sha": merge_sha if rc == 0 else None, "other_branch": other_branch}


def _has_conflict_markers(status_porcelain: str) -> bool:
    """True when a porcelain status shows an unmerged path (``UU``/``AA``/``DD``/``AU``/``UA``/``DU``/
    ``UD``) -- the conflict XY codes."""
    unmerged = {"UU", "AA", "DD", "AU", "UA", "DU", "UD"}
    for line in status_porcelain.splitlines():
        code = line[:2]
        if code in unmerged:
            return True
    return False


# --- parallel-apply fan-in (§B.4) ----------------------------------------------------------------

def fan_in_compare(git_runner: GitRunner | None, base_branch: str, node_branches: list[str], *, repo_root: str | None = None) -> dict[str, Any]:
    """READ-ONLY compare of each node branch against ``base_branch`` (plan §B.4 parallel-apply). NO
    lease needed, NO mutation: per branch, ``(ahead, behind)`` vs base (rev-list counts) plus the
    ``git diff --stat`` text. ``computed`` is false when rev-list fails, distinguishing an invalid ref
    from a branch with zero divergence."""
    git = _runner_for(git_runner, repo_root)
    branches: dict[str, Any] = {}
    for branch in node_branches:
        ahead, behind, computed = _ahead_behind(git, base_branch, branch)
        stat_rc, stat_out = _git_out(git, "diff", "--stat", f"{base_branch}...{branch}")
        branches[branch] = {
            "ahead": ahead,
            "behind": behind,
            "diffstat": stat_out,
            "computed": computed and stat_rc == 0,
        }
    return {"status": "OK", "base_branch": base_branch, "branches": branches}


def fan_in_integrate(
    store: Any,
    git_runner: GitRunner | None,
    project_id: str,
    base_branch: str,
    chosen_branch: str,
    *,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Merge the human/delegate-selected ``chosen_branch`` into ``base_branch`` -- but ONLY when this
    node holds the base-branch integration lease (``branch_scope_key(project_id, base_branch)``). The
    merge goes through :func:`attempt_holder_merge` (holder-checked; conflict = blocking span). A
    non-holder refuses ``DENIED_MIRROR_NOT_HOLDER``."""
    scope_key = branch_scope_key(project_id, base_branch)
    # attempt_holder_merge enforces the holder check against this exact scope_key.
    result = attempt_holder_merge(store, git_runner, scope_key, chosen_branch, holder_check=True, repo_root=repo_root)
    result["chosen_branch"] = chosen_branch
    result["base_branch"] = base_branch
    return result


# --- peer fetch fallback (§B.4) ------------------------------------------------------------------

def fetch_with_fallback(git_runner: GitRunner | None, hub_remote: str, peer_remotes: list[str], *, repo_root: str | None = None, tailnet_probe: Any = net.on_tailnet, suffix_env: Any = None) -> dict[str, Any]:
    """Fetch the hub, falling back to peers in order (plan §B.4 peer git-over-tailnet fetch fallback).

    Transport-checks EVERY remote first (hub + all peers) -- an unsafe remote refuses
    ``DENIED_MIRROR_TRANSPORT`` before any fetch. Then ``git fetch <hub>``; on non-zero, try each peer
    in order; the first success returns ``{source, remote, fetched: true}`` where ``remote`` is the
    peer INDEX (``"peer[<n>]"``) or ``"hub"`` -- NEVER an absolute path/URL (the redaction pass-path).
    All remotes failing refuses ``DENIED_MIRROR_FETCH_FAILED``."""
    git = _runner_for(git_runner, repo_root)
    peers = list(peer_remotes or [])
    for remote in [hub_remote, *peers]:
        assert_remote_transport_safe(remote, tailnet_probe=tailnet_probe, suffix_env=suffix_env)

    hub_rc, _ = _git_out(git, "fetch", hub_remote)
    if hub_rc == 0:
        return {"status": "OK", "source": "hub", "remote": "hub", "fetched": True}

    for index, peer in enumerate(peers):
        peer_rc, _ = _git_out(git, "fetch", peer)
        if peer_rc == 0:
            return {"status": "OK", "source": "peer", "remote": f"peer[{index}]", "fetched": True}

    raise KaizenDenied(
        "DENIED_MIRROR_FETCH_FAILED",
        {
            "peers_tried": len(peers),
            "required_action": "verify the hub is reachable over the tailnet, or add a live peer remote",
        },
        exit_code=2,
    )


# --- enforcement transition record (F2->F3) ------------------------------------------------------

def record_enforcement_transition(store: Any, scope_key_or_project: str, frm: str, to: str) -> dict[str, Any]:
    """Append the F2->F3 shadow->enforced transition record the M12 exit criterion names.

    One consistent event: ``resolution/recorded`` carrying ``{transition: "<frm>-><to>", from, to}``
    -- a durable, digest-visible marker that this scope/project moved between shadow parity and live
    enforcement (F2->F3 is the canonical direction; the payload states the ACTUAL direction passed).
    Consumers distinguish this transition from conflict resolutions by the ``transition`` payload key."""
    transition = f"{frm}->{to}"
    event = store.append_coord_event(
        "resolution",
        "recorded",
        summary=f"Enforcement transition {frm}->{to} for {scope_key_or_project}.",
        scope_key=scope_key_or_project,
        payload={"transition": transition, "from": frm, "to": to},
    )
    return {"status": "OK", "scope_key": scope_key_or_project, "transition": transition, "id": event["id"]}
