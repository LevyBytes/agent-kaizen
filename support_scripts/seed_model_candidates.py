#!/usr/bin/env python
"""Seed the model-candidate registry (evals/model-candidates.json) into source_locks via S1.

Idempotent: skips candidates whose exact source_id already exists (checked with S2). Use --dry-run
to print planned actions without writing; it still performs read-only S2 existence probes. Use
--test to mark seeded rows is_test=1 (K7-purgeable). Respects KAIZEN_REPO_ROOT (inherited by the
kaizen.py subprocesses)."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "evals" / "model-candidates.json"
KAIZEN = REPO_ROOT / "kaizen.py"


def _kaizen(*args: str) -> tuple[int, dict]:
    """Run a JSON Kaizen operation and return its code plus parsed payload or raw-output wrapper."""
    proc = subprocess.run(
        [sys.executable, str(KAIZEN), *args, "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(REPO_ROOT),
    )
    raw = proc.stdout.strip() or proc.stderr.strip()
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {"_raw": raw}
    return proc.returncode, payload


def _exists(source_id: str) -> bool:
    """Return whether S2 contains an exact source ID; fail closed when the query fails."""
    rc, payload = _kaizen("S2", "--query", source_id, "--limit", "1000000")
    if rc != 0 or payload.get("status") != "OK":
        detail = payload.get("code") or payload.get("_raw") or payload
        raise RuntimeError(f"S2 existence probe failed: {detail}")
    return any(r.get("source_id") == source_id for r in payload.get("records", []))


def main(argv: list[str] | None = None) -> int:
    """Seed missing candidates via S1, honoring dry-run/test; return 1 on failure or 2 on bad input."""
    parser = argparse.ArgumentParser(description="Seed the model-candidate registry into source_locks.")
    parser.add_argument("--dry-run", action="store_true", help="print planned actions without writing")
    parser.add_argument("--test", action="store_true", help="mark seeded records is_test=1 (K7-purgeable)")
    args = parser.parse_args(argv)

    try:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        candidates = data["candidates"]
        if not isinstance(candidates, list):
            raise TypeError("'candidates' must be a list")
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        print(f"manifest error: {MANIFEST}: {exc}", file=sys.stderr)
        return 2
    created = skipped = failed = 0
    for cand in candidates:
        sid = cand["source_id"]
        try:
            exists = _exists(sid)
        except RuntimeError as exc:
            print(f"FAILED: {sid}: {exc}", file=sys.stderr)
            failed += 1
            continue
        if exists:
            print(f"skip (exists): {sid}")
            skipped += 1
            continue
        summary = f"{cand['role']}. Disposition: {cand['disposition']}."
        if args.dry_run:
            print(f"would add: {sid} [{cand['authority_tier']}] {cand['license']} -> {cand['url']}")
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as handle:
            handle.write(cand["notes"])
            body_file = handle.name
        try:
            call = ["S1", "--source-id", sid, "--authority-tier", cand["authority_tier"],
                    "--url-or-repository", cand["url"], "--version-or-commit", cand["version_or_commit"],
                    "--license", cand["license"], "--summary", summary, "--body-file", body_file]
            if args.test:
                call.append("--test")
            rc, payload = _kaizen(*call)
        finally:
            Path(body_file).unlink(missing_ok=True)
        if rc == 0 and payload.get("status") == "OK":
            print(f"added: {sid} ({payload.get('id')})")
            created += 1
        else:
            detail = payload.get("code") or payload.get("_raw") or payload
            print(f"FAILED: {sid}: {detail}", file=sys.stderr)
            failed += 1
    print(f"\nseeded={created} skipped={skipped} failed={failed} (total={len(candidates)})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
