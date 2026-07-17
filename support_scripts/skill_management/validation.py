"""Structured, stdlib-only validation for Claude/Codex skill packages."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .core import SCHEMA_VERSION, is_directory_link, path_text, resolved, sha256_file


_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_PLACEHOLDER_RE = re.compile(r"\b(?:TODO|TBD)\b")
_SKIP_DIRS = {".git", "__pycache__", "node_modules"}


def _strip_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_frontmatter(text: str) -> tuple[dict[str, object], list[str]]:
    """Parse the small YAML subset used by skill frontmatter."""
    errors: list[str] = []
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, ["SKILL.md must start with YAML frontmatter (`---`)"]
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return {}, ["SKILL.md frontmatter has no closing `---`"]
    body = lines[1:end]
    data: dict[str, object] = {}
    i = 0
    while i < len(body):
        line = body[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if line[:1].isspace() or ":" not in line:
            errors.append(f"unsupported frontmatter line {i + 2}: {line.strip()}")
            i += 1
            continue
        key, raw = line.split(":", 1)
        key = key.strip()
        raw = raw.strip()
        if key in data:
            errors.append(f"duplicate frontmatter key: {key}")
        if raw in (">", ">-", "|", "|-"):
            parts: list[str] = []
            i += 1
            while i < len(body) and (not body[i].strip() or body[i][:1].isspace()):
                parts.append(body[i].strip())
                i += 1
            data[key] = ("\n" if raw.startswith("|") else " ").join(parts).strip()
            continue
        if not raw:
            values: list[str] = []
            i += 1
            while i < len(body) and (not body[i].strip() or body[i][:1].isspace()):
                item = body[i].strip()
                if item.startswith("- "):
                    values.append(_strip_scalar(item[2:]))
                elif item:
                    errors.append(f"unsupported nested frontmatter at line {i + 2}: {item}")
                i += 1
            data[key] = values
            continue
        if raw.startswith("[") and raw.endswith("]"):
            data[key] = [_strip_scalar(item) for item in raw[1:-1].split(",") if item.strip()]
        else:
            data[key] = _strip_scalar(raw)
        i += 1
    return data, errors


def _iter_package_files(root: Path, onerror=None):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=onerror):
        kept: list[str] = []
        current = Path(dirpath)
        for dirname in sorted(d for d in dirnames if d not in _SKIP_DIRS):
            candidate = current / dirname
            if is_directory_link(candidate):
                yield candidate
            else:
                kept.append(dirname)
        dirnames[:] = kept
        for filename in sorted(filenames):
            yield current / filename


def package_sha256(root: str | os.PathLike[str]) -> str:
    """Hash package-relative paths and bytes without following directory links."""
    root = resolved(root)
    digest = hashlib.sha256()
    for path in _iter_package_files(root):
        try:
            rel = path.relative_to(root).as_posix().encode("utf-8")
            digest.update(rel)
            digest.update(b"\0")
            if path.is_symlink() or is_directory_link(path):
                try:
                    target = os.readlink(path)
                except OSError:
                    target = os.path.realpath(path)
                digest.update(b"link\0")
                digest.update(str(target).encode("utf-8"))
            else:
                digest.update(path.read_bytes())
            digest.update(b"\0")
        except OSError:
            digest.update(b"<unreadable>\0")
    return digest.hexdigest()


def _mask_markdown_code(text: str) -> str:
    """Mask fenced blocks and backtick spans while preserving string offsets."""
    chars = list(text)
    fence: tuple[str, int] | None = None
    offset = 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        mask_line = fence is not None
        if fence is None:
            match = _FENCE_RE.match(body)
            if match and (match.group(1)[0] == "~" or "`" not in match.group(2)):
                fence = (match.group(1)[0], len(match.group(1)))
                mask_line = True
        else:
            marker, minimum = fence
            if re.fullmatch(rf" {{0,3}}{re.escape(marker)}{{{minimum},}}[ \t]*", body):
                fence = None
        if mask_line:
            chars[offset : offset + len(line)] = " " * len(line)
        offset += len(line)

    masked = "".join(chars)
    i = 0
    while i < len(masked):
        if masked[i] != "`":
            i += 1
            continue
        opening = i
        while i < len(masked) and masked[i] == "`":
            i += 1
        width = i - opening
        closing = i
        while closing < len(masked):
            if masked[closing] != "`":
                closing += 1
                continue
            end = closing
            while end < len(masked) and masked[end] == "`":
                end += 1
            if end - closing == width:
                chars[opening:end] = " " * (end - opening)
                i = end
                break
            closing = end
        else:
            i = opening + width
    return "".join(chars)


def _matching_bracket(text: str, opening: int) -> int | None:
    depth = 1
    i = opening + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _link_title_close(text: str, start: int) -> int | None:
    i = start
    while i < len(text) and text[i].isspace():
        i += 1
    if i < len(text) and text[i] == ")":
        return i
    if i >= len(text) or text[i] not in "\"'(":
        return None
    opening = text[i]
    closing = ")" if opening == "(" else opening
    depth = 1
    i += 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if opening == "(" and text[i] == opening:
            depth += 1
        elif text[i] == closing:
            depth -= 1
            if depth == 0:
                i += 1
                break
        i += 1
    else:
        return None
    while i < len(text) and text[i].isspace():
        i += 1
    return i if i < len(text) and text[i] == ")" else None


def _link_destination(text: str, opening: int) -> tuple[str | None, int | None]:
    i = opening + 1
    while i < len(text) and text[i].isspace():
        i += 1
    if i >= len(text):
        return None, None
    if text[i] == ")":
        return "", i
    if text[i] == "<":
        start = i + 1
        i = start
        while i < len(text):
            if text[i] == "\\":
                i += 2
                continue
            if text[i] == ">":
                return text[start:i], _link_title_close(text, i + 1)
            if text[i] in "\r\n":
                return None, None
            i += 1
        return None, None

    start = i
    depth = 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            if depth == 0:
                return text[start:i], i
            depth -= 1
        elif text[i].isspace() and depth == 0:
            return text[start:i], _link_title_close(text, i)
        i += 1
    return None, None


def _markdown_link_errors(root: Path, path: Path, text: str) -> list[str]:
    errors: list[str] = []
    text = _mask_markdown_code(text)
    i = 0
    while i < len(text):
        if text[i] != "[" or (i > 0 and text[i - 1] == "!"):
            i += 1
            continue
        label_end = _matching_bracket(text, i)
        if label_end is None or label_end + 1 >= len(text) or text[label_end + 1] != "(":
            i += 1
            continue
        raw, link_end = _link_destination(text, label_end + 1)
        if raw is None or link_end is None:
            preview = text[label_end + 2 :].splitlines()[0].strip()[:160]
            errors.append(
                f"{path.relative_to(root).as_posix()}: malformed Markdown link destination: {preview}"
            )
            i = label_end + 2
            continue
        raw = raw.strip()
        if re.search(r"\[[^\]\r\n]+\]\(", raw):
            errors.append(
                f"{path.relative_to(root).as_posix()}: malformed Markdown link destination: {raw}"
            )
            i = link_end + 1
            continue
        try:
            parsed = urlsplit(raw)
        except ValueError:
            errors.append(
                f"{path.relative_to(root).as_posix()}: malformed Markdown link destination: {raw}"
            )
            i = link_end + 1
            continue
        if not raw or raw.startswith("#") or parsed.scheme or raw.startswith("//"):
            i = link_end + 1
            continue
        target_text = unquote(parsed.path)
        if not target_text or target_text.startswith("/"):
            i = link_end + 1
            continue
        target = (path.parent / target_text).resolve(strict=False)
        try:
            target.relative_to(root.resolve(strict=False))
        except ValueError:
            errors.append(f"{path.relative_to(root).as_posix()}: local link escapes package: {raw}")
            i = link_end + 1
            continue
        if not target.exists():
            errors.append(f"{path.relative_to(root).as_posix()}: missing local link target: {raw}")
        i = link_end + 1
    return errors


def _reference_errors(root: Path, required: bool) -> list[str]:
    """Preserve the skill-drafting validator's reference-corpus gates."""
    references = root / "references"
    index = references / "INDEX.md"
    topics = root / "references" / "topics.json"
    errors: list[str] = []
    if not references.is_dir():
        return ["references/ directory is missing"] if required else []
    if not index.is_file():
        errors.append("references/INDEX.md is missing")
    if not topics.is_file():
        errors.append("references/topics.json is missing")
        return errors
    try:
        value = json.loads(topics.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"references/topics.json is invalid: {type(exc).__name__}: {exc}")
        return errors
    topic_rows = value.get("topics") if isinstance(value, dict) else None
    if not isinstance(topic_rows, list) or not topic_rows:
        errors.append("references/topics.json has no topics array")
        return errors
    try:
        index_text = index.read_text(encoding="utf-8") if index.is_file() else ""
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"references/INDEX.md is unreadable: {type(exc).__name__}: {exc}")
        index_text = ""
    seen: set[str] = set()
    for number, row in enumerate(topic_rows, start=1):
        if not isinstance(row, dict):
            errors.append(f"topic {number} is not an object")
            continue
        if not row.get("topic"):
            errors.append(f"topic {number} is missing topic")
        if not row.get("summary"):
            errors.append(f"topic {number} is missing summary")
        if not isinstance(row.get("keywords"), list) or not row.get("keywords"):
            errors.append(f"topic {number} is missing keywords")
        filename = row.get("file")
        if not isinstance(filename, str) or not filename.startswith("references/"):
            errors.append(f"topic {number} has invalid file value")
            continue
        if filename in seen:
            errors.append(f"duplicate topic file in topics.json: {filename}")
        seen.add(filename)
        if not (root / filename).is_file():
            errors.append(f"topic file does not exist: {filename}")
        if filename.removeprefix("references/") not in index_text:
            errors.append(f"topic file is not listed in references/INDEX.md: {filename}")
    return errors


def _placeholder_errors(root: Path) -> list[str]:
    errors: list[str] = []
    for rel in ("SKILL.md", "references/INDEX.md", "references/topics.json"):
        path = root / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        in_fence = False
        for line_number, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = not in_fence
                continue
            if in_fence or "allow-placeholder" in line:
                continue
            if _PLACEHOLDER_RE.search(re.sub(r"`[^`]*`", "", line)):
                errors.append(f"placeholder-like text in {rel}:{line_number}")
    return errors


def _surface_errors(root: Path, text: str, metadata: dict[str, object]) -> list[str]:
    errors: list[str] = []
    description = metadata.get("description", "")
    trigger_terms = ("use when", "when", "trigger", "fires", "asks", "working with")
    if isinstance(description, str) and description and not any(term in description.lower() for term in trigger_terms):
        errors.append("description lacks concrete trigger language")
    lower = text.lower()
    for section in ("workflow", "references", "verification"):
        if section not in lower:
            errors.append(f"SKILL.md is missing {section} guidance")
    evals_gotcha = root / "evals" / "GOTCHA.md"
    legacy_gotcha = root / "GOTCHA.md"
    if "gotcha" not in lower and not evals_gotcha.is_file() and not legacy_gotcha.is_file():
        errors.append("SKILL.md is missing gotcha guidance (inline or evals/GOTCHA.md)")
    if evals_gotcha.is_file():
        try:
            if not evals_gotcha.read_text(encoding="utf-8").strip():
                errors.append("evals/GOTCHA.md is present but empty")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"cannot read evals/GOTCHA.md: {exc}")
    if legacy_gotcha.is_file():
        errors.append("legacy sibling GOTCHA.md exists; move the surface to evals/GOTCHA.md")
    if "gotcha.md" in lower and "evals/gotcha.md" not in lower and not legacy_gotcha.is_file():
        errors.append("SKILL.md references GOTCHA.md without using evals/GOTCHA.md")
    errors.extend(_placeholder_errors(root))
    return errors


def _subskill_dirs(root: Path) -> list[Path]:
    try:
        return sorted(
            (
                child
                for child in root.iterdir()
                if child.is_dir() and not is_directory_link(child) and (child / "SKILL.md").is_file()
            ),
            key=lambda child: child.name.casefold(),
        )
    except OSError:
        return []


def _empty_record(root: Path, errors: list[str]) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "path": path_text(root),
        "name": root.name,
        "display_name": root.name,
        "description": "",
        "covers": [],
        "layout": "flat",
        "valid": False,
        "errors": sorted(set(errors)),
        "warnings": [],
        "subskills": [],
        "skill_md_path": path_text(root / "SKILL.md"),
        "skill_md_sha256": "",
        "package_sha256": package_sha256(root) if root.is_dir() else "",
    }


def _validate_node(root: Path, *, leaf: bool) -> dict[str, object]:
    """Validate one router or leaf node; router children are handled by the caller."""
    errors: list[str] = []
    warnings: list[str] = []
    metadata: dict[str, object] = {}
    skill_md = root / "SKILL.md"
    text = ""
    if not root.is_dir():
        return _empty_record(root, ["skill package directory does not exist"])
    if not skill_md.is_file():
        return _empty_record(root, ["missing SKILL.md"])
    try:
        text = skill_md.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        errors.append(f"SKILL.md is not UTF-8: {exc}")
    except OSError as exc:
        errors.append(f"cannot read SKILL.md: {type(exc).__name__}: {exc}")
    if text:
        metadata, fm_errors = parse_frontmatter(text)
        errors.extend(fm_errors)
        name = metadata.get("name")
        description = metadata.get("description")
        if not isinstance(name, str) or not name:
            errors.append("frontmatter `name` must be a non-empty string")
        elif not _NAME_RE.fullmatch(name):
            errors.append("frontmatter `name` must be kebab-case")
        elif name != root.name:
            errors.append(f"frontmatter name `{name}` does not match directory `{root.name}`")
        if not isinstance(description, str) or not description.strip():
            errors.append("frontmatter `description` must be a non-empty string")
        covers = metadata.get("covers", [])
        if covers and (not isinstance(covers, list) or not all(isinstance(x, str) for x in covers)):
            errors.append("frontmatter `covers` must be a string list when present")
        errors.extend(_surface_errors(root, text, metadata))
    errors.extend(_reference_errors(root, required=leaf))
    child_roots = set(_subskill_dirs(root)) if not leaf else set()
    walk_errors: list[str] = []

    def walk_error(exc: OSError) -> None:
        walk_errors.append(f"cannot enumerate package path: {type(exc).__name__}: {exc}")

    for path in _iter_package_files(root, onerror=walk_error):
        if any(child == path or child in path.parents for child in child_roots):
            continue
        if path.is_symlink() or is_directory_link(path):
            errors.append(f"package links are not allowed: {path.relative_to(root).as_posix()}")
            continue
        if path.suffix.lower() != ".md":
            try:
                path.read_bytes()
            except OSError as exc:
                errors.append(f"cannot read {path.relative_to(root).as_posix()}: {exc}")
            continue
        try:
            md_text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            errors.append(f"{path.relative_to(root).as_posix()} is not UTF-8: {exc}")
            continue
        except OSError as exc:
            errors.append(f"cannot read {path.relative_to(root).as_posix()}: {exc}")
            continue
        errors.extend(_markdown_link_errors(root, path, md_text))
    errors.extend(walk_errors)
    covers_value = metadata.get("covers", []) if metadata else []
    return {
        "schema_version": SCHEMA_VERSION,
        "path": path_text(root),
        "name": metadata.get("name", root.name) if metadata else root.name,
        "display_name": metadata.get("name", root.name) if metadata else root.name,
        "description": metadata.get("description", "") if metadata else "",
        "covers": list(covers_value) if isinstance(covers_value, list) else [],
        "layout": "flat",
        "valid": not errors,
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
        "subskills": [],
        "skill_md_path": path_text(skill_md),
        "skill_md_sha256": sha256_file(skill_md),
        "package_sha256": package_sha256(root),
    }


def _validate_package(root: Path) -> dict[str, object]:
    children = _subskill_dirs(root) if root.is_dir() else []
    record = _validate_node(root, leaf=not children)
    if not children:
        return record
    router_text = ""
    try:
        router_text = (root / "SKILL.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        pass
    subskill_records = [_validate_package(child) for child in children]
    router_errors = list(record["errors"])
    for child, child_record in zip(children, subskill_records):
        if f"{child.name}/SKILL.md" not in router_text.replace("\\", "/"):
            router_errors.append(f"subskill not referenced in router SKILL.md: {child.name}")
        if not child_record["valid"]:
            router_errors.append(f"subskill `{child.name}` failed validation ({len(child_record['errors'])} errors)")
    record["layout"] = "router"
    record["subskills"] = subskill_records
    record["errors"] = sorted(set(router_errors))
    record["valid"] = not record["errors"]
    return record


def validate_skill_package(skill_dir: str | os.PathLike[str]) -> dict[str, object]:
    """Return recursive, structured diagnostics and hashes for one skill package."""
    return _validate_package(resolved(skill_dir))
