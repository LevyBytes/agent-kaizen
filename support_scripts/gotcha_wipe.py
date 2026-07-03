#!/usr/bin/env python3
"""gotcha_wipe.py — deterministically clear all tracked GOTCHA.md files, with a restorable backup.

The target set is DISCOVERED at runtime (never hardcoded): every git-tracked file whose basename is
`GOTCHA.md`, including project and skill command surfaces under `evals/`. `--include-ignored` also picks
up the gitignored working-tree copies under `AI/work/`.

"Clearing" empties each file to a minimal stub that keeps its `# Title — Gotchas` heading, so skill packages
stay structurally valid (the validator still finds a non-empty sibling). A backup is written first (unless
`--no-backup`): an exact path->content JSON manifest that `restore` replays byte-for-byte, plus a read-only
`digest.md` that shows each unique rule once with the files that contained it (duplication is common across
skill GOTCHAs). Restore depends only on the exact manifest, never on the dedup.

Stdlib only. Backups default to the gitignored <repo>/AI/support_scripts_work/gotcha-backups/ area, so results are
never committed. Built for both terminal use (interactive confirm) and LLM use (`--yes --json`).

Usage (run from anywhere; the repo root is found via git):
  python support_scripts/gotcha_wipe.py wipe --dry-run            # preview the target set, no writes
  python support_scripts/gotcha_wipe.py wipe                      # back up, confirm, then clear
  python support_scripts/gotcha_wipe.py wipe --include-ignored    # also clear AI/work scratch copies
  python support_scripts/gotcha_wipe.py wipe --exclude evals/GOTCHA.md  # keep the project file
  python support_scripts/gotcha_wipe.py wipe --yes --json         # non-interactive (LLM)
  python support_scripts/gotcha_wipe.py restore <backup-dir>      # restore exact contents
  python support_scripts/gotcha_wipe.py list                     # list available backups
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

GOTCHA_NAME = "GOTCHA.md"
PRUNE_DIRS = {".git", "node_modules", "__pycache__"}


# --------------------------------------------------------------------------- repo / discovery

def repo_root() -> Path:
    """Repo root via git; fall back to this script's parent's parent (support_scripts/ is one level down)."""
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent))
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).resolve()
    except Exception:
        pass
    return Path(__file__).resolve().parent.parent


def _git_lines(root: Path, args: list) -> list:
    out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if out.returncode != 0:
        return []
    return [ln for ln in out.stdout.split("\0") if ln] if "-z" in args else out.stdout.splitlines()


def tracked_gotchas(root: Path) -> list:
    """Repo-relative POSIX paths of every git-tracked file named GOTCHA.md."""
    rels = _git_lines(root, ["ls-files", "-z"])
    return sorted(r for r in rels if r.rsplit("/", 1)[-1] == GOTCHA_NAME)


def worktree_gotchas(root: Path) -> list:
    """Repo-relative POSIX paths of every GOTCHA.md on disk (pruning .git/node_modules/etc., perm-safe)."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]
        if GOTCHA_NAME in filenames:
            found.append((Path(dirpath) / GOTCHA_NAME).resolve().relative_to(root).as_posix())
    return sorted(found)


def discover(root: Path, include_ignored: bool, excludes: list) -> list:
    """The target list: tracked GOTCHA.md (always), plus working-tree copies if --include-ignored,
    minus any matching an --exclude glob (matched against the repo-relative POSIX path or basename)."""
    rels = set(tracked_gotchas(root))
    if include_ignored:
        rels |= set(worktree_gotchas(root))
    def excluded(rel: str) -> bool:
        # Match the repo-relative POSIX path (NOT the basename — every target is named GOTCHA.md, so a
        # basename match would drop everything). `--exclude evals/GOTCHA.md` drops just the project file; `--exclude
        # ".claude/*"` drops that mirror; `--exclude "*some-skill*"` drops a skill.
        return any(fnmatch.fnmatch(rel, pat) for pat in excludes)
    return sorted(r for r in rels if not excluded(r))


# --------------------------------------------------------------------------- helpers

def assert_inside(root: Path, target: Path) -> Path:
    """Resolve target and require it under root (GOTCHA G7); abort otherwise."""
    rt, tp = root.resolve(), target.resolve()
    if rt != tp and rt not in tp.parents:
        sys.exit(f"REFUSING to write outside the repo root: {tp}")
    return tp


def read_bytes(root: Path, rel: str) -> bytes:
    return (root / rel).read_bytes()


def h1_line(text: str) -> str:
    """The file's first `# ` heading (without trailing CR), or a generic default."""
    for ln in text.split("\n"):
        s = ln.rstrip("\r")
        if s.startswith("# "):
            return s
    return "# GOTCHA"


def make_stub(title: str, ts: str, backup_rel: str | None) -> bytes:
    restore = (f"restore: python support_scripts/gotcha_wipe.py restore {backup_rel}"
               if backup_rel else "no backup was taken")
    return f"{title}\n\n<!-- cleared by gotcha_wipe.py on {ts}; {restore} -->\n".encode("utf-8")


def now_stamp() -> tuple:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%SZ"), dt.isoformat()


def default_backup_root(root: Path) -> Path:
    return root / "AI" / "support_scripts_work" / "gotcha-backups"


def confirm(action: str, n: int, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        print(f"refusing to {action} {n} file(s) non-interactively; pass --yes", file=sys.stderr)
        return False
    return input(f"Type 'yes' to {action} {n} file(s): ").strip().lower() == "yes"


def group_summary(rels: list) -> str:
    """Compact 'N under <area>' grouping for the human summary."""
    counts: dict = {}
    for r in rels:
        if "/" not in r:
            key = "(root)"
        else:
            parts = r.split("/")
            key = "/".join(parts[:3]) if len(parts) > 3 else "/".join(parts[:-1])
        counts[key] = counts.get(key, 0) + 1
    return "\n".join(f"    {c:>3}  {k}/" for k, c in sorted(counts.items()))


# --------------------------------------------------------------------------- backup / digest

def normalize_rule(line: str) -> str:
    return " ".join(line.strip().lstrip("-").strip().split())


def write_backup(root: Path, rels: list, backup_root: Path, dir_stamp: str, iso: str, scope: dict) -> Path:
    bdir = assert_inside(root, backup_root / dir_stamp)
    bdir.mkdir(parents=True, exist_ok=True)
    files = []
    for rel in rels:
        raw = read_bytes(root, rel)
        files.append({"path": rel, "sha256": hashlib.sha256(raw).hexdigest(),
                      "bytes": len(raw), "content": raw.decode("utf-8")})
    manifest = {"tool": "gotcha_wipe.py", "created": iso, "repo_root": root.name,
                "scope": scope, "count": len(files), "files": files}
    (bdir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                        encoding="utf-8", newline="\n")
    (bdir / "digest.md").write_text(build_digest(files, iso), encoding="utf-8", newline="\n")
    return bdir


def build_digest(files: list, iso: str) -> str:
    rule_files: dict = {}     # normalized rule -> [rels]
    rule_text: dict = {}      # normalized rule -> first-seen display text
    nobullets = []
    for f in files:
        bullets = [ln for ln in f["content"].split("\n") if ln.lstrip().startswith("- ")]
        if not bullets:
            nobullets.append(f["path"])
        for b in bullets:
            key = normalize_rule(b)
            if not key:
                continue
            rule_files.setdefault(key, [])
            if f["path"] not in rule_files[key]:
                rule_files[key].append(f["path"])
            rule_text.setdefault(key, b.strip().lstrip("-").strip())
    shared = sorted(((k, v) for k, v in rule_files.items() if len(v) > 1),
                    key=lambda kv: (-len(kv[1]), kv[0]))
    out = [f"# GOTCHA backup digest ({iso})", "",
           f"{len(files)} file(s); {len(rule_files)} distinct rule(s); "
           f"{len(shared)} shared across >1 file. Read-only insight — restore uses `manifest.json`, not this.",
           "", "## Shared rules (in more than one file)", ""]
    if shared:
        for key, owners in shared:
            out.append(f"- ({len(owners)} files) {rule_text[key]}")
            out.append(f"  - files: {', '.join(sorted(owners))}")
    else:
        out.append("_(none)_")
    out += ["", "## Per-file unique rules", ""]
    for f in files:
        uniq = [rule_text[normalize_rule(ln)] for ln in f["content"].split("\n")
                if ln.lstrip().startswith("- ") and normalize_rule(ln)
                and len(rule_files[normalize_rule(ln)]) == 1]
        if uniq:
            out.append(f"### {f['path']}")
            out += [f"- {u}" for u in uniq]
            out.append("")
    if nobullets:
        out += ["## Files with no bullet rules", ""] + [f"- {p}" for p in nobullets]
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- commands

def cmd_wipe(args) -> int:
    root = repo_root()
    rels = discover(root, args.include_ignored, args.exclude or [])
    scope = {"include_ignored": bool(args.include_ignored), "exclude": args.exclude or [], "count": len(rels)}
    result = {"action": "wipe", "repo_root": str(root), "scope": scope, "targets": rels}

    if not rels:
        msg = "no GOTCHA.md files matched."
        print(json.dumps({**result, "status": "empty"}) if args.json else msg)
        return 0

    print(f"gotcha_wipe: {len(rels)} GOTCHA.md target(s)", file=sys.stderr)
    print(group_summary(rels), file=sys.stderr)

    if args.dry_run:
        result["status"] = "dry-run"
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print("dry-run: no files changed.", file=sys.stderr)
        return 0

    # git-dirty warning (the outer git rollback won't fully cover already-modified targets).
    dirty = [ln[3:] for ln in _git_lines(root, ["status", "--porcelain", "--", *rels]) if ln]
    if dirty:
        print(f"  WARNING: {len(dirty)} target(s) have uncommitted git changes; the backup still captures them.",
              file=sys.stderr)

    if not confirm("WIPE", len(rels), args.yes):
        print("aborted.", file=sys.stderr)
        return 1

    dir_stamp, iso = now_stamp()
    backup_rel = None
    if args.no_backup:
        print("  --no-backup: NOT backing up (restore will be impossible from this tool).", file=sys.stderr)
    else:
        backup_root = Path(args.backup_dir).resolve() if args.backup_dir else default_backup_root(root)
        bdir = write_backup(root, rels, backup_root, dir_stamp, iso, scope)
        backup_rel = bdir.relative_to(root).as_posix() if root in bdir.parents else str(bdir)
        result["backup"] = backup_rel
        print(f"  backup -> {backup_rel} (manifest.json + digest.md)", file=sys.stderr)

    for rel in rels:
        target = assert_inside(root, root / rel)
        title = h1_line(target.read_bytes().decode("utf-8", errors="replace"))
        target.write_bytes(make_stub(title, iso, backup_rel))
    result["status"] = "wiped"
    result["wiped"] = len(rels)
    print(json.dumps(result, indent=2) if args.json else f"wiped {len(rels)} file(s).", file=sys.stderr if not args.json else sys.stdout)
    return 0


def cmd_restore(args) -> int:
    root = repo_root()
    src = Path(args.backup).resolve()
    manifest_path = src if src.is_file() else src / "manifest.json"
    if not manifest_path.is_file():
        sys.exit(f"no manifest.json at {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("files", [])
    if args.filter:
        entries = [e for e in entries if fnmatch.fnmatch(e["path"], args.filter)]
    result = {"action": "restore", "manifest": str(manifest_path), "count": len(entries)}

    if not entries:
        print(json.dumps({**result, "status": "empty"}) if args.json else "no entries match.", file=sys.stderr)
        return 0
    print(f"gotcha_restore: {len(entries)} file(s) from {manifest_path}", file=sys.stderr)
    if args.dry_run:
        result["status"] = "dry-run"
        print(json.dumps(result, indent=2) if args.json else "dry-run: no files changed.",
              file=sys.stdout if args.json else sys.stderr)
        return 0
    if not confirm("RESTORE", len(entries), args.yes):
        print("aborted.", file=sys.stderr)
        return 1

    restored, mism = 0, []
    for e in entries:
        target = assert_inside(root, root / e["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        data = e["content"].encode("utf-8")
        target.write_bytes(data)
        if "sha256" in e and hashlib.sha256(data).hexdigest() != e["sha256"]:
            mism.append(e["path"])
        restored += 1
    result["status"] = "restored"
    result["restored"] = restored
    if mism:
        result["sha_mismatch"] = mism
        print(f"  WARNING: {len(mism)} file(s) had a sha256 mismatch after write.", file=sys.stderr)
    print(json.dumps(result, indent=2) if args.json else f"restored {restored} file(s).",
          file=sys.stdout if args.json else sys.stderr)
    return 0


def cmd_list(args) -> int:
    root = repo_root()
    broot = Path(args.backup_dir).resolve() if args.backup_dir else default_backup_root(root)
    backups = []
    if broot.is_dir():
        for d in sorted(broot.iterdir()):
            mf = d / "manifest.json"
            if mf.is_file():
                try:
                    m = json.loads(mf.read_text(encoding="utf-8"))
                    backups.append({"dir": d.relative_to(root).as_posix() if root in d.parents else str(d),
                                    "created": m.get("created"), "count": m.get("count"),
                                    "scope": m.get("scope")})
                except Exception:
                    pass
    if args.json:
        print(json.dumps({"action": "list", "backup_root": str(broot), "backups": backups}, indent=2))
    elif not backups:
        print(f"no backups under {broot}", file=sys.stderr)
    else:
        for b in backups:
            print(f"{b['created']}  {b['count']:>4} files  {b['dir']}", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="gotcha_wipe.py", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("wipe", help="clear all tracked GOTCHA.md to a stub (backup first)")
    w.add_argument("--include-ignored", action="store_true", help="also clear gitignored working-tree copies (AI/work)")
    w.add_argument("--exclude", action="append", metavar="GLOB", help="skip targets matching this glob (repeatable)")
    w.add_argument("--no-backup", action="store_true", help="do NOT back up first (restore becomes impossible)")
    w.add_argument("--backup-dir", help="backup root (default: <repo>/AI/support_scripts_work/gotcha-backups)")
    w.add_argument("--dry-run", action="store_true", help="list the target set and exit without writing")
    w.add_argument("--yes", action="store_true", help="skip the interactive confirmation (for non-interactive/LLM use)")
    w.add_argument("--json", action="store_true", help="emit a machine-readable result to stdout")
    w.set_defaults(func=cmd_wipe)

    r = sub.add_parser("restore", help="restore GOTCHA.md contents from a backup manifest")
    r.add_argument("backup", help="a backup directory or its manifest.json")
    r.add_argument("--filter", metavar="GLOB", help="restore only paths matching this glob")
    r.add_argument("--dry-run", action="store_true", help="list what would be restored and exit")
    r.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    r.add_argument("--json", action="store_true", help="emit a machine-readable result to stdout")
    r.set_defaults(func=cmd_restore)

    l = sub.add_parser("list", help="list available backups")
    l.add_argument("--backup-dir", help="backup root (default: <repo>/AI/support_scripts_work/gotcha-backups)")
    l.add_argument("--json", action="store_true", help="emit a machine-readable result to stdout")
    l.set_defaults(func=cmd_list)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
