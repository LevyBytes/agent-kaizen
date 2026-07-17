"""Default-deny policy chokepoint (v8 M3, plan §3.4 / §4).

ONE decision seam every lane funnels through before any vendor/tool action runs. It is
default-deny and fail-closed: nothing unmatched is ever silently allowed (the default is
``ask``, never ``allow``), and a set of CODE INVARIANTS hard-deny above every DB rule.

Design:
- :class:`RequestedAction` is the vendor-neutral request (engine actor + verb + targets +
  optional shell command + opaque vendor ``raw``). ``canonicalize()`` produces the casefolded
  canonical target forms an allow-rule / protected-prefix can match against.
- :func:`PolicyEngine.decide` evaluates deny > ask > allow with the order: canonicalize ->
  CODE INVARIANTS (git-push, protected-path, vendor-config, stale-epoch) -> DB authority rules
  (deny, then ask, then allow) -> default ``ask``. Every decision is IDEMPOTENT and re-assertable:
  the same (thread_id, epoch, command-normal-form) key returns the SAME cached Decision object,
  and distinct items never share one (per-item gating; Roo #11210, dedupe per Codex #10187).
- INVARIANTS are a module-level tuple documented as NON-REMOVABLE: they live in code and no DB
  rule can override them. An exact_command allow rule over ``git push`` still denies; a path_prefix
  allow rule over a protected prefix still denies.
- Protected prefixes are LOADED from the DB (active ``private_policy`` rows WHERE
  ``trigger='protected-path'``), never hardcoded -- so tracked source ships no real protected path.
  Vendor global-config paths resolve via ``os.path.expanduser`` at engine construction.

Purity: record-plane logic only. This module imports NONE of subprocess/socket/asyncio/http.server
(it lives in the allowlisted ``orchestration/`` prefix but is pure logic + DB reads/writes). It
persists ``ask`` decisions as C4 ``approval_requests`` via :mod:`session_records` and reloads pending
ones on daemon restart, so a re-surfaced approval is never lost.
"""

from __future__ import annotations

import argparse
import json
import ntpath
import os
import posixpath
import re
import shlex
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Mapping

from .. import session_records
from ..db import fetch_all, new_id, now, write_tx
from ..hashing import utc_text_hash
from ..schemas import validate_record

# --- verb + result vocabularies ----------------------------------------------------------------

# The RequestedAction verb space. exec/spawn carry a shell ``command`` (scanned for git-push and
# write-evidence); the rest gate on canonical targets. Mirrors KAIZEN_ENUMS['policy_verb'] minus 'any'
# (which is a rule-side wildcard, never an action verb).
VERBS: tuple[str, ...] = ("tool", "file_read", "file_write", "net", "git", "exec", "spawn")

# Verbs that mutate state (the stale-epoch invariant hard-denies these off a stale epoch; a stale
# non-mutating verb degrades to ask, never allow).
MUTATING_VERBS: frozenset[str] = frozenset({"file_write", "git", "exec", "spawn", "net"})

# Verbs that carry a shell string in ``command``.
SHELL_VERBS: frozenset[str] = frozenset({"exec", "spawn"})

ALLOW = "allow"
ASK = "ask"
DENY = "deny"

# Mutating-command tokens that, alongside a protected-path token in the same shell segment, are
# write evidence (a bare protected-path mention without one of these falls through to the default ask).
# basename+casefold matched, so full paths and .exe suffixes hit.
_MUTATOR_TOKENS: frozenset[str] = frozenset({
    "rm", "del", "erase", "rmdir", "rd", "mv", "move", "cp", "copy", "mkdir", "md", "touch", "tee",
    "truncate", "dd", "robocopy", "xcopy", "new-item", "set-content", "add-content", "out-file",
    "remove-item", "move-item", "copy-item",
})


# --- path canonicalization ---------------------------------------------------------------------

_MNT_RE = re.compile(r"^/mnt/([A-Za-z])(/.*)?$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_LONG_UNC = "\\\\?\\UNC\\"
_LONG_UNC_FWD = "//?/UNC/"
_LONG_PREFIX = "\\\\?\\"
_LONG_PREFIX_FWD = "//?/"


def canonicalize_path(raw: str | None, cwd: str | None = None) -> str | None:
    r"""Canonical comparison form of a raw path token, platform-independent.

    Windows-origin forms (drive-letter, UNC, ``\\?\`` long-path, ``\\?\UNC\``, WSL ``/mnt/<letter>/``)
    normalize to a casefolded backslash form via ``ntpath`` (case-insensitive, so ``C:\X`` == ``c:/x``).
    Pure POSIX absolute paths (a leading ``/`` that is not ``/mnt/<letter>`` and carries no backslash or
    drive) normalize via ``posixpath`` and KEEP case (POSIX is case-sensitive). A relative path resolves
    against ``cwd`` when given, else is returned normalized-but-non-canonical (it can never match an
    allow rule; protected-prefix scanning still sees it best-effort). ``ntpath``/``posixpath`` are used
    explicitly rather than ``os.path`` so behavior does not depend on the running OS.
    """
    if raw is None:
        return None
    s = str(raw).strip().strip('"').strip("'")
    if not s:
        return s

    # WSL drive mount -> Windows drive path (checked before the generic POSIX branch, since it maps to
    # a case-insensitive Windows volume). /mnt/c/x -> c:\x.
    mnt = _MNT_RE.match(s)
    if mnt:
        drive, rest = mnt.group(1), (mnt.group(2) or "")
        return ntpath.normpath(f"{drive}:{rest}").casefold()

    # Strip \\?\ long-path and \\?\UNC\ prefixes to their plain form before normalizing.
    if s.startswith(_LONG_UNC) or s.startswith(_LONG_UNC_FWD):
        s = "\\\\" + s[len(_LONG_UNC):]
    elif s.startswith(_LONG_PREFIX) or s.startswith(_LONG_PREFIX_FWD):
        s = s[len(_LONG_PREFIX):]

    looks_windows = bool(_DRIVE_RE.match(s)) or s.startswith("\\\\") or "\\" in s and not s.startswith("/")
    if s.startswith("/") and not looks_windows:
        # Pure POSIX absolute: case-sensitive normalize, keep case.
        return posixpath.normpath(s)

    if not (s.startswith("/") or _DRIVE_RE.match(s) or s.startswith("\\\\")):
        # Relative path. Resolve against cwd when we have one (so it can canonicalize to an absolute
        # comparable form); otherwise leave it non-canonical (never allow-matchable, still scannable).
        if cwd:
            base = canonicalize_path(cwd)
            if base is not None:
                joined = ntpath.join(base, s.replace("/", "\\")) if "\\" in base else posixpath.join(base, s)
                return canonicalize_path(joined)
        # Non-canonical: casefold the ntpath form so best-effort prefix scans are stable.
        return ntpath.normpath(s).casefold()

    return ntpath.normpath(s).casefold()


def _prefix_sep(prefix: str) -> str:
    """Pick the separator (`\\` or `/`) implied by an already-canonical prefix."""
    return "\\" if "\\" in prefix else "/"


def prefix_match(target: str | None, prefix: str | None) -> bool:
    r"""Boundary-aware prefix test: ``c:\foo`` matches ``c:\foo\bar`` and ``c:\foo`` itself, but NOT
    ``c:\foobar``. Both operands are already-canonical strings."""
    if not target or not prefix:
        return False
    if target == prefix:
        return True
    sep = _prefix_sep(prefix)
    anchored = prefix if prefix.endswith(sep) else prefix + sep
    return target.startswith(anchored)


# --- shell string scanning ---------------------------------------------------------------------

def split_segments(command: str) -> list[str]:
    """Split a shell string into command segments on ``; && || | &`` and newlines. Coarse and
    conservative -- it over-splits harmlessly (an operator inside a quoted string still splits), which
    only ever makes the invariant scan MORE eager to deny."""
    tmp = command.replace("&&", "\n").replace("||", "\n").replace("|", "\n").replace(";", "\n").replace("&", "\n")
    return [seg.strip() for seg in tmp.splitlines() if seg.strip()]


def _segment_token_lists(segment: str) -> tuple[list[str], list[str]]:
    """Two token views of one segment: shlex(posix=True) per the contract, plus a raw ``str.split()``.
    The raw split is the documented fallback for backslash paths that posix-shlex mangles (``D:\tools\
    git.exe`` -> the basename check would otherwise miss it); both views are scanned so a git/mutator
    token is caught in whichever survives."""
    try:
        shlex_toks = shlex.split(segment, posix=True)
    except ValueError:
        shlex_toks = segment.split()
    return shlex_toks, segment.split()


def _basename_low(token: str) -> str:
    # ntpath.basename so a full path (any separator) reduces to its command name.
    """Brief docstring: casefolded ntpath basename for verb/command-name matching across separators. (audit-confirmed)."""
    return ntpath.basename(token).casefold()


def _tokens_have_git_push(tokens: list[str]) -> bool:
    """A ``git``/``git.exe`` token (basename match) followed LATER in the same token list by a bare
    ``push`` token."""
    git_at = None
    for i, tok in enumerate(tokens):
        if _basename_low(tok) in ("git", "git.exe"):
            git_at = i
            break
    if git_at is None:
        return False
    return any(tok.casefold() == "push" for tok in tokens[git_at + 1:])


def command_has_git_push(command: str) -> bool:
    """True when any segment is a git-push invocation. Conservative OVER-deny by design (e.g.
    ``echo git push`` denies): a false git-push denial is safe; a missed one is not."""
    for segment in split_segments(command):
        shlex_toks, raw_toks = _segment_token_lists(segment)
        if _tokens_have_git_push(shlex_toks) or _tokens_have_git_push(raw_toks):
            return True
    return False


# Deliberately conservative: quoted redirection-like text may over-match and deny, never widen access.
_REDIRECT_RE = re.compile(r">>?\s*([^\s;|&]+)")


def _segment_write_targets(segment: str) -> tuple[list[str], bool]:
    """Return (redirect-target tokens, has-mutator-token) for one segment. Redirect targets come from
    ``>``/``>>``; the mutator flag is any _MUTATOR_TOKENS basename in the segment."""
    redirects = [m.group(1) for m in _REDIRECT_RE.finditer(segment)]
    shlex_toks, raw_toks = _segment_token_lists(segment)
    has_mutator = any(
        _basename_low(tok) in _MUTATOR_TOKENS for tok in (*shlex_toks, *raw_toks)
    )
    return redirects, has_mutator


def _segment_path_tokens(segment: str) -> list[str]:
    """Candidate path tokens in a segment: every token that looks like a path (drive/UNC/absolute/
    contains a separator), from both token views, plus redirect targets."""
    shlex_toks, raw_toks = _segment_token_lists(segment)
    out: list[str] = []
    for tok in (*shlex_toks, *raw_toks):
        if _DRIVE_RE.match(tok) or tok.startswith("\\\\") or tok.startswith("/") or "\\" in tok or "/" in tok:
            out.append(tok)
    out.extend(m.group(1) for m in _REDIRECT_RE.finditer(segment))
    return out


# --- request + decision dataclasses ------------------------------------------------------------

@dataclass(frozen=True)
class Actor:
    """The engine identity behind a request (session + epoch fence + optional vendor thread)."""
    engine: str
    session_id: str
    epoch: int
    thread_id: str | None = None


@dataclass(frozen=True)
class RequestedAction:
    """One vendor-neutral action awaiting a decision.

    ``verb`` is one of :data:`VERBS`; ``targets`` are raw path/url strings; ``command`` is the shell
    string for exec/spawn (None otherwise); ``raw`` is the opaque vendor-native mapping (may carry
    ``cwd`` for relative-path resolution). ``canonicalize()`` computes the canonical target forms.
    """
    actor: Actor
    verb: str
    targets: tuple[str, ...] = ()
    command: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def cwd(self) -> str | None:
        value = self.raw.get("cwd") if isinstance(self.raw, Mapping) else None
        return str(value) if value else None

    def canonical_targets(self) -> tuple[str, ...]:
        """Canonicalize each explicit target (relative resolves against ``raw['cwd']``)."""
        out: list[str] = []
        for target in self.targets:
            canon = canonicalize_path(target, cwd=self.cwd)
            if canon:
                out.append(canon)
        return tuple(out)

    def canonicalize(self) -> tuple[str, ...]:
        """Recompute canonical targets on every call; no result is cached."""
        return self.canonical_targets()

    def normal_form(self) -> str:
        """Command-normal-form: the whitespace-collapsed stripped command, or (when no command)
        ``verb + sorted canonical targets``. The dedupe axis for :class:`Decision` caching."""
        if self.command is not None:
            return " ".join(self.command.split())
        return " ".join((self.verb, *sorted(self.canonical_targets())))

    def dedupe_key(self) -> str:
        """Per-item dedupe key = (thread_id, epoch, command-normal-form). Distinct items never collide;
        an identical re-assertion in the same thread+epoch hits the same cached Decision."""
        thread = self.actor.thread_id or ""
        return f"{thread}\x1f{self.actor.epoch}\x1f{self.normal_form()}"


@dataclass(frozen=True)
class Decision:
    """A policy decision. ``result`` is allow|ask|deny. ``rule_id`` is set for a DB-rule allow/ask;
    ``invariant_id`` is set for a code-invariant deny. ``dedupe_key`` is the caching axis."""
    result: str
    reason: str
    dedupe_key: str
    rule_id: str | None = None
    invariant_id: str | None = None

    @property
    def correlation_hash(self) -> str:
        """Stable hash of the dedupe key -- the C4 approval correlation_id for an ask."""
        return utc_text_hash({"dedupe_key": self.dedupe_key})


# --- code invariants (NON-REMOVABLE) -----------------------------------------------------------
# These four hard-deny gates live in CODE and can never be overridden by any DB authority rule. They
# are evaluated a-c then stale-epoch (deterministic order). Each id is stable so a denial is traceable.
INV_GIT_PUSH = "INV_GIT_PUSH"
INV_PROTECTED_PATH = "INV_PROTECTED_PATH"
INV_VENDOR_CONFIG = "INV_VENDOR_CONFIG"
INV_STALE_EPOCH = "INV_STALE_EPOCH"

INVARIANTS: tuple[str, ...] = (INV_GIT_PUSH, INV_PROTECTED_PATH, INV_VENDOR_CONFIG, INV_STALE_EPOCH)


# --- permission modes (H2.1, owner-locked matrix) ----------------------------------------------
# Four code-owned permission modes form a layer BETWEEN the code invariants and the DB authority
# rules. Decision precedence (plan §"Decision precedence", verbatim order):
#   code invariants -> mode hard-deny CEILING -> owner deny -> owner ask -> owner allow -> mode DEFAULT.
# A mode CEILING (a DENY cell) is a hard deny no DB allow can widen; a mode DEFAULT (Allow/Ask cell)
# applies only when no explicit rule matched. Bumping the owner-locked matrix bumps the version stamp.
PERMISSION_MODE_VERSION = 1
PROTECTED_PATH_VERSION = 1

PERMISSION_MODES: tuple[str, ...] = ("plan", "ask", "agent", "full")

# Action CELLS the matrix keys on. Every RequestedAction classifies to exactly one cell (see
# _classify_cell): reads split on workspace membership; writes split designated/workspace/outside;
# exec+spawn share the exec cell; net + git are their own cells. git push is an invariant, never a cell.
CELL_READ_IN = "read_in"
CELL_READ_OUT = "read_out"
CELL_WRITE_DESIGNATED = "write_designated"
CELL_WRITE_WORKSPACE = "write_workspace"
CELL_WRITE_OUT = "write_out"
CELL_EXEC = "exec"
CELL_NET = "net"
CELL_GIT = "git"

# The owner-locked mode matrix: mode -> cell -> {allow|ask|deny}. A DENY cell is a hard ceiling; an
# allow/ask cell is a default applied only when no explicit DB rule matched. Values are the module
# result constants (ALLOW/ASK/DENY).
MODE_MATRIX: dict[str, dict[str, str]] = {
    "plan": {
        CELL_READ_IN: ALLOW,
        CELL_READ_OUT: ASK,
        CELL_WRITE_DESIGNATED: ALLOW,
        CELL_WRITE_WORKSPACE: DENY,   # ceiling
        CELL_WRITE_OUT: DENY,         # ceiling
        CELL_EXEC: DENY,              # ceiling
        CELL_NET: DENY,               # ceiling
        CELL_GIT: DENY,               # ceiling
    },
    "ask": {
        CELL_READ_IN: ALLOW,
        CELL_READ_OUT: ASK,
        CELL_WRITE_DESIGNATED: ASK,
        CELL_WRITE_WORKSPACE: ASK,
        CELL_WRITE_OUT: ASK,
        CELL_EXEC: ASK,
        CELL_NET: ASK,
        CELL_GIT: ASK,
    },
    "agent": {
        CELL_READ_IN: ALLOW,
        CELL_READ_OUT: ASK,
        CELL_WRITE_DESIGNATED: ALLOW,
        CELL_WRITE_WORKSPACE: ALLOW,
        CELL_WRITE_OUT: ASK,
        CELL_EXEC: ASK,
        CELL_NET: ASK,
        CELL_GIT: ASK,
    },
    "full": {
        CELL_READ_IN: ALLOW,
        CELL_READ_OUT: ALLOW,
        CELL_WRITE_DESIGNATED: ALLOW,
        CELL_WRITE_WORKSPACE: ALLOW,
        CELL_WRITE_OUT: ALLOW,
        CELL_EXEC: ALLOW,
        CELL_NET: ALLOW,
        CELL_GIT: ALLOW,
    },
}

# Verbs that are pure reads (classified to a read cell) vs pure writes (classified to a write cell).
_READ_VERBS: frozenset[str] = frozenset({"file_read"})
_WRITE_VERBS: frozenset[str] = frozenset({"file_write"})


# --- shipped protected floor -------------------------------------------------------------------
# OS-critical prefixes that ship as a protected floor on EVERY engine (H2.0): a fresh install with no
# operator X1 rows still refuses to write OS roots, shell startup files, and vendor profile locations.
# These are public-knowledge system locations, safe in tracked source; the operator's private
# protected-path rows load from the DB ON TOP of this floor. INV_PROTECTED_PATH enforces both above
# every DB rule and every future mode (incl. Full). Entries are deliberately NARROW: OS roots, the
# per-user Startup FOLDER, and profile FILES only -- never a generic user/home/temp dir (tests write
# scratch roots under %TEMP%, which the floor must not catch).

# Absolute OS roots (drive/POSIX). Static, no expansion.
_SHIPPED_OS_ROOTS: tuple[str, ...] = (
    # Windows system trees.
    "C:\\Windows",
    "C:\\Program Files",
    "C:\\Program Files (x86)",
    "C:\\ProgramData",
    # Unix system trees.
    "/etc", "/usr", "/boot", "/bin", "/sbin", "/lib", "/var",
)

# Per-user files/folders resolved from env/home at engine build (NOT hardcoded to any user).
def _shipped_user_protected() -> list[str]:
    """Per-user protected floor entries expanded via env/home at build time (never a hardcoded user
    path). Windows: the Startup FOLDER + PowerShell profile locations. Unix: shell rc FILES. Narrow by
    design -- only these specific files/folders, so a generic AppData/home/temp write is NOT caught."""
    out: list[str] = []
    home = os.path.expanduser("~")
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        documents = ntpath.join(home, "Documents") if home else None
        roaming = appdata or (ntpath.join(home, "AppData", "Roaming") if home else None)
        if roaming:
            out.append(ntpath.join(roaming, "Microsoft", "Windows", "Start Menu", "Programs", "Startup"))
        if documents:
            out.append(ntpath.join(documents, "WindowsPowerShell", "Microsoft.PowerShell_profile.ps1"))
            out.append(ntpath.join(documents, "PowerShell", "Microsoft.PowerShell_profile.ps1"))
    elif os.name == "posix" and home:
        for rc in (".bashrc", ".profile", ".zshrc", ".bash_profile", ".zprofile"):
            out.append(posixpath.join(home, rc))
    return out


def shipped_protected_paths() -> list[str]:
    """The canonical shipped protected floor: OS roots + per-user startup/profile files. Merged into
    every engine at construction so a rule-less engine still enforces INV_PROTECTED_PATH on them."""
    raw = [*_SHIPPED_OS_ROOTS, *_shipped_user_protected()]
    return [p for p in (canonicalize_path(x) for x in raw) if p]


# --- DB loaders --------------------------------------------------------------------------------

def load_protected_paths() -> list[str]:
    """Canonical protected-path prefixes = active ``private_policy`` rows WHERE trigger='protected-path'
    (body = one path prefix per line). CONNECT-PER-CALL via fetch_all; the daemon loads these once at
    boot. Never hardcodes a real path -- the operator mints them through the X1 CLI op."""
    rows = fetch_all(
        "SELECT body FROM private_policy WHERE status = 'active' AND trigger = 'protected-path'"
    )
    prefixes: list[str] = []
    for (body,) in rows:
        for line in (body or "").splitlines():
            canon = canonicalize_path(line)
            if canon:
                prefixes.append(canon)
    return prefixes


def _rule_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": row[0],
        "rule_type": row[1],
        "verb": row[2],
        "match_kind": row[3],
        "pattern": row[4],
        "engine": row[5],
        "enabled": int(row[6]) if row[6] is not None else 1,
    }


def _load_rules_with_count() -> tuple[list[dict[str, Any]], int]:
    """Load enabled authority rules and return valid rows plus the skipped-row count."""
    rows = fetch_all(
        "SELECT id, rule_type, verb, match_kind, pattern, engine, enabled FROM authority_rules "
        "WHERE enabled = 1 ORDER BY created_at"
    )
    loaded: list[dict[str, Any]] = []
    skipped = 0
    for row in rows:
        rule = _rule_from_row(row)
        try:
            validate_record(
                "authority_rule",
                {
                    "rule_type": rule["rule_type"],
                    "verb": rule["verb"],
                    "match_kind": rule["match_kind"],
                    "pattern": rule["pattern"],
                    "engine": rule["engine"],
                    "summary": "loaded authority rule",
                },
            )
        except Exception:  # noqa: BLE001 -- a malformed rule row is dropped, not fatal
            skipped += 1
            continue
        loaded.append(rule)
    return loaded, skipped


class _RuleLoader:
    """Callable compatibility API whose ``skipped`` count is isolated per calling thread."""

    def __init__(self) -> None:
        self._local = threading.local()

    @property
    def skipped(self) -> int:
        return int(getattr(self._local, "skipped", 0))

    def __call__(self) -> list[dict[str, Any]]:
        loaded, skipped = _load_rules_with_count()
        self._local.skipped = skipped
        return loaded


load_rules = _RuleLoader()


def store_rule(rule: dict[str, Any], is_test: bool = False) -> str:
    """Persist one authority rule (validate_record + write_tx insert). Returns the new id."""
    clean = {
        "rule_type": rule["rule_type"],
        "verb": rule["verb"],
        "match_kind": rule["match_kind"],
        "pattern": rule["pattern"],
        "engine": rule.get("engine"),
        "summary": rule.get("summary", "authority rule"),
    }
    validate_record("authority_rule", {k: v for k, v in clean.items() if v not in (None, "")})
    record_id = new_id("aurule")
    created = now()
    enabled = 0 if rule.get("enabled") is False else 1
    content_hash = utc_text_hash({"id": record_id, **clean})
    test_flag = 1 if is_test else 0

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "INSERT INTO authority_rules "
            "(id, created_at, updated_at, rule_type, verb, match_kind, pattern, engine, enabled, "
            "summary, content_hash, is_test) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record_id, created, created, clean["rule_type"], clean["verb"], clean["match_kind"],
                clean["pattern"], clean["engine"], enabled, clean["summary"], content_hash, test_flag,
            ),
        )

    write_tx(op)
    return record_id


# --- namespace shim (build C4 args in code, never shell out) -----------------------------------

class _Namespace(argparse.Namespace):
    """Minimal argparse.Namespace builder so record_ask can call session_records.approval_upsert with
    an in-code args object (never shells out to kaizen.py). Mirrors supervisor._Namespace; only supplied
    fields are set; normal ``argparse.Namespace`` behavior raises ``AttributeError`` for an unset field."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__()
        for key, value in kwargs.items():
            setattr(self, key, value)


# --- policy engine -----------------------------------------------------------------------------

class PolicyEngine:
    """The default-deny decision chokepoint. Construct with protected prefixes + authority rules (both
    loaded from the DB at daemon boot via :func:`build_engine_from_db`); ``decide`` returns a cached,
    re-assertable :class:`Decision`. ``record_ask`` persists an ask as a C4 approval, and
    ``replay_pending`` re-surfaces open approvals after a daemon restart."""

    def __init__(
        self,
        protected_paths: list[str],
        rules: list[dict[str, Any]],
        vendor_config_paths: list[str] | None = None,
        *,
        permission_mode: str = "ask",
        workspace_root: str | None = None,
        designated_write_roots: list[str] | None = None,
    ) -> None:
        # Protected prefixes are already canonical (loader canonicalizes); re-canonicalize defensively
        # so a caller passing raw prefixes still gets correct matching. The SHIPPED floor (OS roots +
        # startup/profile files) merges in at construction so EVERY engine -- DB-built or directly
        # constructed -- enforces INV_PROTECTED_PATH on them above any DB rule, deduped against the
        # operator's DB prefixes.
        merged = [*shipped_protected_paths(), *(canonicalize_path(x) for x in protected_paths)]
        self.protected_paths = list(dict.fromkeys(p for p in merged if p))
        self.rules = list(rules)
        # Permission mode (code-owned layer between invariants and DB rules). Unknown mode -> the most
        # restrictive posture that still has no ceiling beyond invariants ('ask'), never a silent 'full'.
        self.permission_mode = permission_mode if permission_mode in MODE_MATRIX else "ask"
        # Canonical workspace root: the engine's own workspace prefix (reads/writes classify against it).
        # None means "unknown workspace" -> every read/write classifies OUTSIDE (the safe direction).
        self.workspace_root = canonicalize_path(workspace_root) if workspace_root else None
        # Designated Plan write roots: normalized+canonicalized, dedup-stable order. A write under one is
        # a designated-write cell (Allow in plan/agent per the matrix); explicit deny/ask rules still
        # outrank per precedence. Accepted as a plain arg this wave (get_mode_profile wiring is wave-2).
        roots = [canonicalize_path(x) for x in (designated_write_roots or [])]
        self.designated_write_roots = list(dict.fromkeys(p for p in roots if p))
        # Vendor global-config paths resolve via expanduser at construction (all path forms), then
        # canonicalize -- so a tilde form and an already-expanded form compare equal.
        vendor = vendor_config_paths if vendor_config_paths is not None else default_vendor_config_paths()
        self.vendor_config_paths = [
            p for p in (canonicalize_path(os.path.expanduser(x)) for x in vendor) if p
        ]
        self._cache: dict[str, Decision] = {}

    # --- invariants -----------------------------------------------------------------------------

    def _is_protected(self, canon_target: str | None) -> bool:
        return any(prefix_match(canon_target, prefix) for prefix in self.protected_paths)

    def _is_vendor_config(self, canon_target: str | None) -> bool:
        return canon_target is not None and canon_target in self.vendor_config_paths

    def is_guarded_target(self, canon_target: str | None) -> bool:
        """Return whether a canonical target is protected or vendor configuration."""

        return self._is_protected(canon_target) or self._is_vendor_config(canon_target)

    def _command_write_hits(
        self,
        command: str,
        predicate: Callable[[str | None], bool],
        cwd: str | None = None,
    ) -> bool:
        """True when a shell string carries write evidence to a path ``predicate`` accepts: a redirect
        (>/>>) to such a path, OR a segment with BOTH a mutator token AND such a path token. Relative
        redirect/write tokens resolve against ``cwd`` when given (H2.0 fix), so ``echo x > child.txt``
        under a protected cwd is seen as a write to that cwd; absolute tokens are unaffected. ``predicate``
        receives each canonicalized path or ``None`` when canonicalization cannot produce one."""
        for segment in split_segments(command):
            redirects, has_mutator = _segment_write_targets(segment)
            for target in redirects:
                if predicate(canonicalize_path(target, cwd=cwd)):
                    return True
            if has_mutator:
                for tok in _segment_path_tokens(segment):
                    if predicate(canonicalize_path(tok, cwd=cwd)):
                        return True
        return False

    def _check_invariants(self, action: RequestedAction, current_epoch: int) -> tuple[str, str] | None:
        """Return (invariant_id, reason) for the first tripped invariant, else None. Order: git-push,
        protected-path, vendor-config, then stale-epoch."""
        verb = action.verb
        command = action.command or ""
        canon = action.canonical_targets()
        cwd = action.cwd

        # INV_GIT_PUSH: verb==git push, OR any exec/spawn/tool shell string with a git-push invocation.
        if verb == "git" and any(t.casefold() == "push" for t in action.targets):
            return INV_GIT_PUSH, "git push is never permitted (non-removable invariant)"
        if verb == "git" and isinstance(action.raw, Mapping) and any(
            isinstance(value, str) and value.strip().casefold() == "push"
            for value in action.raw.values()
        ):
            return INV_GIT_PUSH, "git push is never permitted (non-removable invariant)"
        if verb == "git" and command and command_has_git_push(command):
            return INV_GIT_PUSH, "git push is never permitted (non-removable invariant)"
        if verb in SHELL_VERBS or verb == "tool":
            if command and command_has_git_push(command):
                return INV_GIT_PUSH, "shell string contains a git push invocation (non-removable invariant)"

        # INV_PROTECTED_PATH: file_write under a protected prefix, or exec/spawn write-evidence to one.
        if verb == "file_write" and any(self._is_protected(t) for t in canon):
            return INV_PROTECTED_PATH, "write targets a protected path (non-removable invariant)"
        if verb in SHELL_VERBS and command and self._command_write_hits(command, self._is_protected, cwd):
            return INV_PROTECTED_PATH, "shell write targets a protected path (non-removable invariant)"

        # INV_VENDOR_CONFIG: any write-intent action whose canonical target is a vendor global config.
        if verb == "file_write" and any(self._is_vendor_config(t) for t in canon):
            return INV_VENDOR_CONFIG, "write targets a vendor global-config file (non-removable invariant)"
        if verb in SHELL_VERBS and command and self._command_write_hits(command, self._is_vendor_config, cwd):
            return INV_VENDOR_CONFIG, "shell write targets a vendor global-config file (non-removable invariant)"

        # INV_STALE_EPOCH: a mutating verb off a stale epoch hard-denies; a stale non-mutating verb is
        # handled by the caller (degrades to ask, never allow).
        if action.actor.epoch != current_epoch and verb in MUTATING_VERBS:
            return INV_STALE_EPOCH, "mutating action from a stale epoch (non-removable invariant)"

        return None

    # --- permission mode (ceiling + default) ----------------------------------------------------

    def _in_workspace(self, canon_target: str | None) -> bool:
        """True when a canonical target sits inside the engine's workspace root. Unknown workspace ->
        False (classify OUTSIDE -- the safe direction, since outside cells are never MORE permissive)."""
        return self.workspace_root is not None and prefix_match(canon_target, self.workspace_root)

    def _in_designated(self, canon_target: str | None) -> bool:
        return any(prefix_match(canon_target, root) for root in self.designated_write_roots)

    def _classify_cell(self, action: RequestedAction) -> str:
        """Map an action to exactly one matrix cell. Reads split on workspace membership; writes split
        designated -> workspace -> outside (first match wins, most-specific first); exec+spawn share the
        exec cell; net + git their own. A shell exec/spawn is always the exec cell (its writes are the
        exec surface, not a file_write). git push never reaches here (invariant-denied upstream)."""
        verb = action.verb
        if verb in SHELL_VERBS:
            return CELL_EXEC
        if verb == "net":
            return CELL_NET
        if verb == "git":
            return CELL_GIT
        canon = action.canonical_targets()
        if verb in _READ_VERBS:
            # Any target outside the workspace -- or NO resolvable target at all (bare relative path,
            # no cwd) -- classifies as an outside read: fail-safe upward to ask under plan/ask/agent.
            # Only a read whose EVERY target provably sits inside the workspace earns READ_IN.
            if canon and all(self._in_workspace(t) for t in canon):
                return CELL_READ_IN
            return CELL_READ_OUT
        if verb in _WRITE_VERBS:
            if canon and all(self._in_designated(t) for t in canon):
                return CELL_WRITE_DESIGNATED
            if canon and all(self._in_workspace(t) for t in canon):
                return CELL_WRITE_WORKSPACE
            return CELL_WRITE_OUT
        # 'tool' (and any non-classified verb): treat as an exec-surface request (ceiling+default follow
        # the exec cell -- a tool call is an execution the same modes gate).
        return CELL_EXEC

    def _mode_cell_result(self, action: RequestedAction) -> str:
        """The matrix result (allow|ask|deny) for this action under the active mode."""
        return MODE_MATRIX[self.permission_mode][self._classify_cell(action)]

    def _check_mode_ceiling(self, action: RequestedAction) -> tuple[str, str] | None:
        """Return (cell, reason) when the active mode's cell is a hard-deny CEILING (no DB allow may
        widen it), else None. Evaluated AFTER invariants, BEFORE DB rules -- so an explicit owner allow
        cannot lift a ceiling, but the invariant floor still sits above it."""
        cell = self._classify_cell(action)
        if MODE_MATRIX[self.permission_mode][cell] == DENY:
            return cell, f"{self.permission_mode} mode denies '{cell}' (mode ceiling)"
        return None

    # --- DB authority rules ---------------------------------------------------------------------

    def _rule_matches_scope(self, rule: dict[str, Any], action: RequestedAction) -> bool:
        """Match one rule's enabled, engine, and verb scope.

        Persisted rows use integer ``0``/``1``; legacy in-memory rules retain truthiness semantics.
        A null/empty engine matches all engines, and verb ``any`` matches every action.
        """
        if not rule.get("enabled", 1):
            return False
        rule_engine = rule.get("engine")
        if rule_engine not in (None, "") and rule_engine != action.actor.engine:
            return False
        rule_verb = rule.get("verb")
        return rule_verb == "any" or rule_verb == action.verb

    def _eval_rules(self, action: RequestedAction, *, suppress_allow: bool = False) -> tuple[str, str, str] | None:
        """Return (result, rule_id, reason) from the DB rules, else None. deny rules first (any enabled,
        in-scope deny rule denies), then ask rules, then exact_command / path_prefix allow rules. An
        opaque exec/spawn command NEVER allows via a path_prefix rule (only exact_command).

        Precedence is deny > ask > allow (H2.0 fix): an explicit ask outranks an explicit allow, so an
        action matching BOTH asks rather than allows -- fail-safe toward human review. A path_prefix
        deny/ask matches explicit canonical targets only; shell command bodies require exact_command.

        ``suppress_allow`` skips the allow lattice entirely (a stale non-mutating action may still hit a
        deny/ask rule but can NEVER be allowed -- the contract's "stale never allows")."""
        normal = action.normal_form()
        canon = action.canonical_targets()
        is_shell = action.verb in SHELL_VERBS

        # 1. deny rules (highest DB authority; still below the code invariants).
        for rule in self.rules:
            if rule["rule_type"] == DENY and self._rule_matches_scope(rule, action):
                if self._rule_pattern_hits(rule, normal, canon):
                    return DENY, rule["id"], "matched a DB deny rule"

        # 2. ask rules (above allow -- an explicit ask outranks an explicit allow, fail-safe to review).
        for rule in self.rules:
            if rule["rule_type"] == ASK and self._rule_matches_scope(rule, action):
                if self._rule_pattern_hits(rule, normal, canon):
                    return ASK, rule["id"], "matched a DB ask rule"

        # 3. allow rules (lowest DB authority; skipped when suppress_allow -- a stale action never allows).
        if not suppress_allow:
            for rule in self.rules:
                if rule["rule_type"] != ALLOW or not self._rule_matches_scope(rule, action):
                    continue
                if rule["match_kind"] == "exact_command":
                    if normal == " ".join((rule["pattern"] or "").split()):
                        return ALLOW, rule["id"], "matched an exact_command allow rule"
                elif rule["match_kind"] == "path_prefix":
                    # A path_prefix allow rule must NOT allow an opaque shell command (only exact_command can).
                    if is_shell:
                        continue
                    prefix = canonicalize_path(rule["pattern"])
                    if canon and all(prefix_match(t, prefix) for t in canon):
                        return ALLOW, rule["id"], "matched a path_prefix allow rule"

        return None

    @staticmethod
    def _rule_pattern_hits(rule: dict[str, Any], normal: str, canon: tuple[str, ...]) -> bool:
        """deny/ask matcher: exact_command == normal-form; path_prefix == any explicit canonical target
        under the rule prefix. Opaque shell command bodies are not path-tokenized here."""
        if rule["match_kind"] == "exact_command":
            return normal == " ".join((rule["pattern"] or "").split())
        prefix = canonicalize_path(rule["pattern"])
        return any(prefix_match(t, prefix) for t in canon)

    # --- decide ---------------------------------------------------------------------------------

    def decide(self, action: RequestedAction, current_epoch: int) -> Decision:
        """The chokepoint. Idempotent + re-assertable: the same dedupe key returns the SAME cached
        Decision object. Order: canonicalize -> code INVARIANTS (deny) -> mode CEILING (deny) -> DB
        rules (deny > ask > allow) -> stale non-mutating -> mode DEFAULT. Nothing unmatched is ever
        allowed unless the active mode's default cell allows it."""
        key = action.dedupe_key()
        # Cache axis = actor identity + dedupe key. Rule matching is engine-scoped, so two actors
        # sharing a (thread=None, epoch, normal-form) key must never share a cached decision -- an
        # engine-A allow leaking to engine B would widen policy. The Decision's dedupe_key (and its C4
        # correlation hash) stays the contract-shaped per-item key.
        cache_key = f"{action.actor.engine}\x1f{action.actor.session_id}\x1f{current_epoch}\x1f{key}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        invariant = self._check_invariants(action, current_epoch)
        if invariant is not None:
            decision = Decision(result=DENY, reason=invariant[1], dedupe_key=key, invariant_id=invariant[0])
            self._cache[cache_key] = decision
            return decision

        # Mode CEILING: a hard-deny cell in the active mode sits above every DB rule (an owner allow may
        # not widen it) but below the invariant floor. Recorded as an invariant-style deny keyed by cell.
        ceiling = self._check_mode_ceiling(action)
        if ceiling is not None:
            decision = Decision(
                result=DENY, reason=ceiling[1], dedupe_key=key, invariant_id=f"MODE_CEILING:{ceiling[0]}"
            )
            self._cache[cache_key] = decision
            return decision

        # A stale action reaching here is non-mutating (mutating stale already hard-denied by
        # INV_STALE_EPOCH). It may still be denied/asked by a DB rule, but can NEVER be allowed: pass
        # suppress_allow so the allow lattice is skipped, and let the default fall to the mode default.
        stale = action.actor.epoch != current_epoch
        ruled = self._eval_rules(action, suppress_allow=stale)
        if ruled is not None:
            result, rule_id, reason = ruled
            decision = Decision(result=result, reason=reason, dedupe_key=key, rule_id=rule_id)
            self._cache[cache_key] = decision
            return decision

        # Mode DEFAULT: no explicit rule matched -> the active mode's default cell decides. A stale
        # non-mutating action can never allow -> its default is coerced up to ask. Full's allow default
        # is where an unmatched action may finally be allowed (invariants + explicit deny/ask still ran).
        mode_default = self._mode_cell_result(action)
        if stale and mode_default == ALLOW:
            mode_default = ASK
        if stale:
            reason = "stale epoch (non-mutating): defer to approval"
        else:
            reason = f"no rule matched: {self.permission_mode} mode default '{self._classify_cell(action)}' ({mode_default})"
        decision = Decision(result=mode_default, reason=reason, dedupe_key=key)
        self._cache[cache_key] = decision
        return decision

    # --- ask persistence + replay ---------------------------------------------------------------

    def record_ask(self, decision: Decision, session_id: str) -> dict[str, Any]:
        """Persist an ask decision as a C4 ``approval_request`` (request_type tool_approval, state open,
        correlation_id = the dedupe-key hash, summary = the decision reason). IDEMPOTENT: while an open
        row with that correlation_id already exists for the session, no duplicate row is written."""
        if decision.result != ASK:
            raise ValueError("record_ask requires an ask decision")
        correlation = decision.correlation_hash
        existing = _open_approval_id(session_id, correlation)
        if existing is not None:
            return {"status": "OK", "id": existing, "created": False, "correlation_id": correlation}
        ns = _Namespace(
            session_id=session_id,
            summary=decision.reason,
            payload_json=_approval_payload(correlation),
        )
        result = session_records.approval_upsert(ns)
        return {"status": "OK", "id": result["id"], "created": True, "correlation_id": correlation}

    def replay_pending(self, session_id: str) -> list[dict[str, Any]]:
        """Return open approval rows for a session so a reloaded daemon can re-surface them."""
        rows = fetch_all(
            "SELECT id, correlation_id, summary FROM approval_requests "
            "WHERE session_id = ? AND state = 'open' ORDER BY created_at",
            (session_id,),
        )
        return [{"id": row[0], "correlation_id": row[1], "summary": row[2]} for row in rows]

    # --- snapshot construction ------------------------------------------------------------------

    @classmethod
    def from_snapshot(cls, snapshot: "PolicySnapshot") -> "PolicyEngine":
        """Build an engine that decides FROM an immutable snapshot: the snapshot's materialized rule set,
        protected paths, mode, workspace, and designated roots -- nothing re-read from the DB. Later DB
        edits cannot leak in (the snapshot copied everything at build time). Existing direct-constructor
        callers are unaffected; this is an additive alternate constructor."""
        eng = cls(
            protected_paths=list(snapshot.protected_paths),
            rules=[dict(r) for r in snapshot.rules],
            vendor_config_paths=list(snapshot.vendor_config_paths),
            permission_mode=snapshot.permission_mode,
            workspace_root=snapshot.workspace_root,
            designated_write_roots=list(snapshot.designated_write_roots),
        )
        return eng


# --- module-level C4 helpers -------------------------------------------------------------------

def _approval_payload(correlation_id: str) -> str:
    return json.dumps({"request_type": "tool_approval", "correlation_id": correlation_id})


def _open_approval_id(session_id: str, correlation_id: str) -> str | None:
    return next(iter(_approval_ids(session_id, correlation_id, state="open")), None)


def _approval_ids(session_id: str, correlation_id: str, *, state: str | None = None) -> list[str]:
    """Newest-first C4 ids scoped to the public approval alternate key."""

    sql = "SELECT id FROM approval_requests WHERE session_id = ? AND correlation_id = ?"
    params: tuple[str, ...] = (session_id, correlation_id)
    if state is not None:
        sql += " AND state = ?"
        params += (state,)
    rows = fetch_all(sql + " ORDER BY created_at DESC, id DESC", params)
    return [str(row[0]) for row in rows]


def default_vendor_config_paths() -> list[str]:
    """The two vendor global-config files, resolved via expanduser (both files, canonicalized in the
    engine): ``~/.claude/settings.json`` and ``~/.codex/config.toml``."""
    return [
        os.path.expanduser("~/.claude/settings.json"),
        os.path.expanduser("~/.codex/config.toml"),
    ]


def build_engine_from_db() -> PolicyEngine:
    """Factory: a PolicyEngine loaded from the DB (protected paths + authority rules) with the default
    vendor-config paths. The supervisor calls this at boot so protected prefixes load at daemon start."""
    return PolicyEngine(
        protected_paths=load_protected_paths(),
        rules=load_rules(),
        vendor_config_paths=default_vendor_config_paths(),
    )


# --- immutable per-session policy snapshot (H2.1) ----------------------------------------------

@dataclass(frozen=True)
class PolicySnapshot:
    """An IMMUTABLE per-session capture of the full policy inputs at session open. Once built, later DB
    rule/protected-path edits can NEVER change any decision this snapshot drives (every field is a
    materialized copy: tuples of dicts/strings, not live DB handles).

    Fields:
    - ``engine``: the engine label the snapshot was cut for.
    - ``permission_mode``: one of :data:`PERMISSION_MODES`.
    - ``workspace_root``: canonical workspace prefix (reads/writes classify against it).
    - ``designated_write_roots``: normalized+canonical Plan write roots (tuple).
    - ``rules``: the materialized applicable authority-rule set (tuple of frozen dicts-as-tuples-of-items).
    - ``protected_paths``: canonical protected prefixes incl. the shipped floor (tuple).
    - ``vendor_config_paths``: canonical vendor global-config files (tuple).
    - ``permission_mode_version`` / ``protected_path_version``: the stamps the ``profile_hash`` binds.
    - ``profile_hash``: sha256 identity of this profile (see :func:`compute_profile_hash`).

    Drive decisions via ``PolicyEngine.from_snapshot(snapshot).decide(...)`` or ``snapshot.decide(...)``.
    """
    engine: str
    permission_mode: str
    workspace_root: str | None
    designated_write_roots: tuple[str, ...]
    rules: tuple[tuple[tuple[str, Any], ...], ...]
    protected_paths: tuple[str, ...]
    vendor_config_paths: tuple[str, ...]
    permission_mode_version: int
    protected_path_version: int
    profile_hash: str

    def rule_dicts(self) -> list[dict[str, Any]]:
        """Rehydrate the frozen rule tuples back into plain dicts (a fresh copy per call)."""
        return [dict(items) for items in self.rules]

    def build_engine(self) -> PolicyEngine:
        """Construct a fresh PolicyEngine that decides purely from this snapshot's captured inputs."""
        return PolicyEngine(
            protected_paths=list(self.protected_paths),
            rules=self.rule_dicts(),
            vendor_config_paths=list(self.vendor_config_paths),
            permission_mode=self.permission_mode,
            workspace_root=self.workspace_root,
            designated_write_roots=list(self.designated_write_roots),
        )

    def decide(self, action: RequestedAction, current_epoch: int) -> Decision:
        """Decide an action from this snapshot. Builds a one-shot engine (cheap, pure) and delegates --
        so the snapshot itself carries no mutable decision cache and stays a value object."""
        return self.build_engine().decide(action, current_epoch)


def _rule_identity(rule: Mapping[str, Any]) -> tuple[str, str, str]:
    """The hash-relevant identity of one applicable rule: (id, rule_type, pattern). Two profiles differ
    when any applicable rule's id/type/pattern differs; ordering is normalized away (sorted) upstream."""
    return (str(rule.get("id", "")), str(rule.get("rule_type", "")), str(rule.get("pattern", "")))


def compute_profile_hash(
    engine: str,
    permission_mode: str,
    workspace_root: str | None,
    designated_write_roots: list[str] | tuple[str, ...],
    rules: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    permission_mode_version: int = PERMISSION_MODE_VERSION,
    protected_path_version: int = PROTECTED_PATH_VERSION,
) -> str:
    """Stable sha256 identity of a policy profile over the canonical JSON of:
    (engine, permission_mode, permission-mode version, canonical workspace_root, sorted normalized
    designated roots, sorted applicable rule ids+type+pattern, protected-path version).

    Order-INSENSITIVE (roots + rule identities are sorted) so two constructions with differently ordered
    inputs hash equal; changing ANY of those inputs changes the hash. ``args``: designated_write_roots and
    rules are canonicalized/normalized here so a caller may pass raw or pre-normalized forms."""
    norm_roots = sorted({p for p in (canonicalize_path(r) for r in designated_write_roots) if p})
    rule_ids = sorted(_rule_identity(r) for r in rules)
    payload = {
        "engine": engine,
        "permission_mode": permission_mode,
        "permission_mode_version": permission_mode_version,
        "workspace_root": canonicalize_path(workspace_root) if workspace_root else None,
        "designated_write_roots": norm_roots,
        "rules": rule_ids,
        "protected_path_version": protected_path_version,
    }
    return utc_text_hash(payload)


def build_policy_snapshot(
    engine: str,
    permission_mode: str,
    workspace_root: str | None,
    designated_write_roots: list[str],
    rules: list[dict[str, Any]],
    protected_paths: list[str] | None = None,
    vendor_config_paths: list[str] | None = None,
) -> PolicySnapshot:
    """Cut an immutable :class:`PolicySnapshot` for a session.

    ``rules`` is the applicable authority-rule set (already engine-filtered by the caller if desired;
    the snapshot materializes a copy so later DB edits cannot leak in). ``protected_paths`` defaults to
    the DB-loaded prefixes; the shipped floor is always merged in by the engine, so the snapshot captures
    the FULL protected set (floor + DB + operator) for hash stability. ``vendor_config_paths`` defaults
    to :func:`default_vendor_config_paths`. ``permission_mode`` unknown -> falls to the engine's 'ask'.

    Wave-2 supervisor consumes this: it filters rules for the engine, resolves the mode profile's
    designated roots, then calls this once per session and stores ``snapshot.profile_hash`` on the T5 row.
    """
    prot_in = protected_paths if protected_paths is not None else load_protected_paths()
    vendor_in = vendor_config_paths if vendor_config_paths is not None else default_vendor_config_paths()
    # Build a transient engine to materialize the CANONICAL, floor-merged, deduped forms exactly as the
    # decider will see them -- the snapshot then captures those canonical forms verbatim.
    tmp = PolicyEngine(
        protected_paths=list(prot_in),
        rules=[dict(r) for r in rules],
        vendor_config_paths=list(vendor_in),
        permission_mode=permission_mode,
        workspace_root=workspace_root,
        designated_write_roots=list(designated_write_roots),
    )
    frozen_rules = tuple(tuple(sorted(dict(rule).items())) for rule in tmp.rules)
    profile_hash = compute_profile_hash(
        engine=engine,
        permission_mode=tmp.permission_mode,
        workspace_root=tmp.workspace_root,
        designated_write_roots=tmp.designated_write_roots,
        rules=tmp.rules,
    )
    return PolicySnapshot(
        engine=engine,
        permission_mode=tmp.permission_mode,
        workspace_root=tmp.workspace_root,
        designated_write_roots=tuple(tmp.designated_write_roots),
        rules=frozen_rules,
        protected_paths=tuple(tmp.protected_paths),
        vendor_config_paths=tuple(tmp.vendor_config_paths),
        permission_mode_version=PERMISSION_MODE_VERSION,
        protected_path_version=PROTECTED_PATH_VERSION,
        profile_hash=profile_hash,
    )


def snapshot_to_json(snapshot: PolicySnapshot) -> str:
    """Serialize an immutable PolicySnapshot for durable storage on the C1 session row (conversation
    continuation across daemon restarts): rehydration must decide from the ORIGINAL captured inputs,
    never re-read live rules. Round-trips exactly through :func:`snapshot_from_json`."""
    return json.dumps({
        "engine": snapshot.engine,
        "permission_mode": snapshot.permission_mode,
        "workspace_root": snapshot.workspace_root,
        "designated_write_roots": list(snapshot.designated_write_roots),
        "rules": snapshot.rule_dicts(),
        "protected_paths": list(snapshot.protected_paths),
        "vendor_config_paths": list(snapshot.vendor_config_paths),
        "permission_mode_version": snapshot.permission_mode_version,
        "protected_path_version": snapshot.protected_path_version,
        "profile_hash": snapshot.profile_hash,
    }, ensure_ascii=True, sort_keys=True)


def snapshot_from_json(text: str) -> PolicySnapshot:
    """Rehydrate a stored PolicySnapshot verbatim (no re-canonicalization, no live DB reads -- the
    stored form IS the canonical materialized capture). Raises ValueError/KeyError on a malformed
    payload; callers fail closed (a conversation without a valid snapshot is not resumable)."""
    data = json.loads(text)
    return PolicySnapshot(
        engine=str(data["engine"]),
        permission_mode=str(data["permission_mode"]),
        workspace_root=data["workspace_root"],
        designated_write_roots=tuple(data["designated_write_roots"]),
        rules=tuple(tuple(sorted(dict(rule).items())) for rule in data["rules"]),
        protected_paths=tuple(data["protected_paths"]),
        vendor_config_paths=tuple(data["vendor_config_paths"]),
        permission_mode_version=int(data["permission_mode_version"]),
        protected_path_version=int(data["protected_path_version"]),
        profile_hash=str(data["profile_hash"]),
    )
