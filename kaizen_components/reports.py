"""Build R-family reports and the R0 session digest from durable Kaizen records."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .db import fetch_all, fetch_one, new_id, write_tx
from .denials import KaizenDenied
from .hashing import file_sha256, utc_text_hash
from .paths import EXPORT_ROOT, repo_relative
from .policy_records import PRIORITY_ORDER_SQL
from .text_search import like_pattern


REPORT_MAP = {
    "R1": ("task", "tasks"),
    "R2": ("ledger", "ledger_events"),
    "R3": ("learning", "learning"),
    "R4": ("proof", "verification_events"),
    "R5": ("eval", "eval_cases"),
    "R6": ("source", "source_locks"),
    "R7": ("anti-pattern", "anti_patterns"),
    "R8": ("weekly", "ledger_events"),
    "R9": ("monthly", "ledger_events"),
    "R10": ("yearly", "ledger_events"),
    "R11": ("topic", "ledger_events"),
}

# Report tables whose schema has no `body` column; text search falls back to summary only.
_NO_BODY_TABLES = {"anti_patterns"}

# Time-windowed ledger reports: rows are limited to events within the trailing window.
_WINDOW_DAYS = {"R8": 7, "R9": 30, "R10": 365}

# Context columns surfaced per table so a report can be triaged without inspecting each
# record: the DB stores severity/actionability/conclusion etc., and dropping them made
# reports unactionable dumps (id + summary only).
_EXTRA_COLUMNS = {
    "tasks": ["status"],
    "ledger_events": ["task_id", "status"],
    "learning": ["status"],
    "verification_events": ["conclusion", "severity", "actionability", "task_id"],
    "eval_cases": ["category", "status"],
    "source_locks": ["authority_tier"],
    "anti_patterns": ["status"],
}

# Verification conclusions that signal open work: surfaced by the R0 session digest.
_BLOCKING_CONCLUSIONS = ("VERIFICATION_FAILED", "NEEDS_HUMAN_DECISION", "STRUCTURAL_REWORK_RECOMMENDED")


def make_report(args: Any) -> dict[str, Any]:
    """Builds an R1-R11 markdown report under EXPORT_ROOT/reports, inserts a `reports` row, returns {status,id,path,rows,columns,sha256}; --severity/--actionability are R4(verification_events)-only; R11 requires --query. CONFIRMED."""
    operation = getattr(args, "operation")
    if operation not in REPORT_MAP:
        raise KaizenDenied(
            "DENIED_REPORT_TYPE_INVALID",
            {"operation": operation, "required_action": f"use one of: {'|'.join(REPORT_MAP)}"},
            exit_code=2,
        )
    report_type, table = REPORT_MAP[operation]
    query = getattr(args, "query", None)
    limit = int(getattr(args, "limit", None) or 50)
    if operation == "R11" and not query:
        raise KaizenDenied(
            "DENIED_QUERY_REQUIRED",
            {"required_action": "pass --query <topic> for a topic report"},
            exit_code=2,
        )
    conditions: list[str] = []
    params: list[Any] = []
    if query:
        pattern = like_pattern(query)
        if table in _NO_BODY_TABLES:
            conditions.append("summary LIKE ? ESCAPE '\\'")
            params.append(pattern)
        else:
            conditions.append("(summary LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')")
            params.extend((pattern, pattern))
    for flag in ("severity", "actionability"):
        value = getattr(args, flag, None)
        if value:
            if table != "verification_events":
                raise KaizenDenied(
                    "DENIED_FILTER_UNSUPPORTED",
                    {
                        "flag": f"--{flag}",
                        "report": operation,
                        "required_action": f"--{flag} filters apply to R4 (proof report) only",
                    },
                    exit_code=2,
                )
            conditions.append(f"{flag} = ?")
            params.append(value)
    window_days = _WINDOW_DAYS.get(operation)
    if window_days is not None:
        # Lexicographic >= is valid only because every created_at is written via db.now()
        # (timezone-aware UTC isoformat), matching this cutoff's format byte-for-byte.
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
        conditions.append("created_at >= ?")
        params.append(cutoff)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    extra_columns = _EXTRA_COLUMNS.get(table, [])
    select_columns = ["id", "created_at", *extra_columns, "summary"]
    rows = fetch_all(
        f"SELECT {', '.join(select_columns)} FROM {table}{where} ORDER BY created_at DESC LIMIT ?",
        tuple(params),
    )
    out_dir = EXPORT_ROOT / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    generated = datetime.now(timezone.utc)
    generated_at = generated.isoformat()
    stamp = generated.strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{report_type}-report-{stamp}.md"
    lines = [f"# {report_type.title()} Report", "", f"Generated: {generated_at}", f"Rows: {len(rows)}", ""]
    for row in rows:
        extras = " ".join(
            f"{name}={row[2 + offset]}" for offset, name in enumerate(extra_columns) if row[2 + offset] not in (None, "")
        )
        marker = f" [{extras}]" if extras else ""
        lines.append(f"- `{row[0]}` {row[1]}{marker} - {row[-1]}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report_id = new_id("r")
    content_hash = file_sha256(path)

    def op(conn: Any, _attempt: int) -> None:
        conn.execute(
            "INSERT INTO reports (id, created_at, report_type, scope, path, summary, content_hash, is_test) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                report_id,
                generated_at,
                report_type,
                getattr(args, "scope", None) or "project",
                repo_relative(path),
                f"{report_type.title()} report with {len(rows)} rows.",
                content_hash,
                1 if getattr(args, "test", False) else 0,
            ),
        )

    try:
        write_tx(op)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return {
        "status": "OK",
        "id": report_id,
        "path": repo_relative(path),
        "rows": len(rows),
        "columns": select_columns,
        "sha256": content_hash,
    }


def session_digest(args: Any) -> dict[str, Any]:
    """R0: compact session-start digest -- the read-back half of Manage.

    One read-only call returns active private policy, open GOTCHAs, blocking
    verification conclusions, recent LEARNED lessons, and active tasks (summaries
    only), so a session or post-compaction continuation starts from the record
    plane instead of chaining X5/G2/R4/L7. No report file, no DB write.
    """
    limit = int(getattr(args, "limit", None) or 5)
    policies = fetch_all(
        "SELECT id, priority, trigger, title, summary, body FROM private_policy "
        f"WHERE status = 'active' ORDER BY {PRIORITY_ORDER_SQL}, created_at DESC LIMIT 20"
    )
    policy_records = [
        {"id": r[0], "priority": r[1], "trigger": r[2], "title": r[3], "summary": r[4], "body": r[5]}
        for r in policies
    ]
    gotchas = fetch_all(
        "SELECT id, title, summary, created_at FROM gotcha WHERE status = 'active' "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    placeholders = ", ".join("?" for _ in _BLOCKING_CONCLUSIONS)
    blockers = fetch_all(
        "SELECT id, task_id, conclusion, severity, actionability, summary, created_at "
        f"FROM verification_events WHERE conclusion IN ({placeholders}) "
        "ORDER BY created_at DESC LIMIT ?",
        (*_BLOCKING_CONCLUSIONS, limit),
    )
    learned = fetch_all(
        "SELECT id, title, summary, created_at FROM learned ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    tasks = fetch_all(
        "SELECT id, title, status, summary, updated_at FROM tasks WHERE status = 'active' "
        "ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    )
    # Advisory drift signals (no enforcement): a session that sees "3 active tasks, 0
    # verifications this week" knows the harness is being written to but not verified.
    week_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    counts = {
        "policies_active": int(fetch_one("SELECT COUNT(*) FROM private_policy WHERE status = 'active'")[0]),
        "gotchas_active": int(fetch_one("SELECT COUNT(*) FROM gotcha WHERE status = 'active'")[0]),
        "blocking_verifications": int(
            fetch_one(f"SELECT COUNT(*) FROM verification_events WHERE conclusion IN ({placeholders})", _BLOCKING_CONCLUSIONS)[0]
        ),
        "learned_total": int(fetch_one("SELECT COUNT(*) FROM learned")[0]),
        "tasks_active": int(fetch_one("SELECT COUNT(*) FROM tasks WHERE status = 'active'")[0]),
        "active_tasks_without_ledger": int(
            fetch_one(
                "SELECT COUNT(*) FROM tasks WHERE status = 'active' AND id NOT IN "
                "(SELECT DISTINCT task_id FROM ledger_events WHERE task_id IS NOT NULL)"
            )[0]
        ),
        "ledger_events_last_7d": int(
            fetch_one("SELECT COUNT(*) FROM ledger_events WHERE created_at >= ?", (week_cutoff,))[0]
        ),
        "verifications_last_7d": int(
            fetch_one("SELECT COUNT(*) FROM verification_events WHERE created_at >= ?", (week_cutoff,))[0]
        ),
    }
    # Orchestration ledger visibility (T5-T8): active runs, off-screen approvals, live children, and
    # the child-leak canary. Lazy import keeps reports independent of the agent_runs module load order.
    from .agent_runs import session_digest_sections

    orchestration = session_digest_sections(limit)
    counts.update(orchestration.pop("counts", {}))
    result = {
        "status": "OK",
        "message": "Session digest loaded.",
        "policies": policy_records,
        "active_gotchas": [{"id": r[0], "title": r[1], "summary": r[2], "created_at": r[3]} for r in gotchas],
        "blocking_verifications": [
            {
                "id": r[0],
                "task_id": r[1],
                "conclusion": r[2],
                "severity": r[3],
                "actionability": r[4],
                "summary": r[5],
                "created_at": r[6],
            }
            for r in blockers
        ],
        "recent_learned": [{"id": r[0], "title": r[1], "summary": r[2], "created_at": r[3]} for r in learned],
        "active_tasks": [
            {"id": r[0], "title": r[1], "status": r[2], "summary": r[3], "updated_at": r[4]} for r in tasks
        ],
        **orchestration,
        "counts": counts,
        "required_action": (
            "apply the policy records now; treat blocking verifications and active GOTCHAs as open work; "
            "reload with R0 after compaction"
        ),
    }
    # v8 M9 fleet visibility: a 'fleet' section ONLY when distribution is on AND fleet.db exists (the
    # off-mode digest stays byte-identical -- the key is entirely ABSENT). Any failure degrades to a
    # {status: unavailable} stub and never breaks R0.
    fleet_section = _fleet_digest_section(limit)
    if fleet_section is not None:
        result["fleet"] = fleet_section
    return result


def _fleet_digest_section(limit: int) -> dict[str, Any] | None:
    """The R0 fleet block, or None when distribution is off / no fleet.db exists.

    Mode off ⇒ None (key absent = off-unchanged exit criterion). Otherwise route through the same
    daemon-loopback→break-glass path the D8 op uses, wrapped so ANY failure degrades to
    {status: unavailable, reason} rather than breaking R0."""
    from .orchestration.modes import dist_mode
    from .paths import FLEET_DB_PATH

    if dist_mode() == "off" or not FLEET_DB_PATH.exists():
        return None
    try:
        from argparse import Namespace

        from .fleet.records import fleet_digest

        args = Namespace(limit=limit or None, payload_json=None, payload_json_file=None, summary=None)
        section = fleet_digest(args)
    except Exception as error:  # noqa: BLE001 -- fleet visibility is advisory; never break the digest
        return {"status": "unavailable", "reason": type(error).__name__}
    # v8 M17 built-in metrics (ledger #15): the same advisory posture -- ride the section when they
    # compute, degrade to a stub key when they do not, never break R0.
    try:
        from .fleet import metrics as fleet_metrics_mod
        from .fleet.records import _route

        section["metrics"] = _route(
            "fleet/metrics",
            {"run": lambda store: fleet_metrics_mod.fleet_metrics(store), "wire": {}},
        )
    except Exception as error:  # noqa: BLE001 -- metrics are advisory
        section["metrics"] = {"status": "unavailable", "reason": type(error).__name__}
    return section
