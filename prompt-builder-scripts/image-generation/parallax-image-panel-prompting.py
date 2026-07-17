"""Windows-first interactive parallax prompt builder with atomically persisted sibling-log state."""

from __future__ import annotations

import os
import re
import sys
import json
import ctypes
import hashlib
import secrets
import subprocess
from ctypes import wintypes
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime
from functools import cache
from pathlib import Path


# Force deterministic Unicode output for Windows terminals and redirected streams.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")
if hasattr(sys.stdin, "reconfigure"):
    # utf-8-sig strips the BOM that Windows shells can prepend to piped input
    sys.stdin.reconfigure(encoding="utf-8-sig")


LOG_PATH = Path(__file__).with_suffix(".log")
SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPT_STEM = Path(__file__).stem


def find_repo_root(start: Path) -> Path:
    """Find the repository root by walking upward from a starting folder.

    The script uses this to build default paths that live inside the current
    repo instead of hard-coding one user's machine path.
    """
    for candidate in (start, *start.parents):
        if (candidate / "AGENTS.md").is_file() or (candidate / ".git").exists():
            return candidate
    return start


REPO_ROOT = find_repo_root(SCRIPT_DIR)

SETTINGS_MARKER = "# ==== SETTINGS (current defaults; rewritten in place by the script) ===="
HISTORY_MARKER = "# ==== RUN HISTORY (append-only; newest last) ===="

SETTING_KEYS = (
    "mode",
    "output_folder",
    "results_log_folder",
    "prompts_given_folder",
    "copy_to_clipboard",
    "backup_mode",
    "backup_folder",
    "art_style",
    "palette",
    "light",
    "matte",
    "canvas",
    "crop_target",
    "groundline_pct",
    "world",
    "mood",
)

DEFAULT_SETTINGS = OrderedDict(
    (
        ("mode", "new panel"),
        ("output_folder", str(REPO_ROOT / "AI" / "generation")),
        ("results_log_folder", str(REPO_ROOT / "user" / "prompt-results-logs")),
        ("prompts_given_folder", str(REPO_ROOT / "user" / "prompts-given")),
        ("copy_to_clipboard", "yes"),
        ("backup_mode", "ask"),
        ("backup_folder", "user/user-generated-prompts"),
        (
            "art_style",
            "16-bit pixel art: crisp pixel clusters, clean dark outlines, "
            "limited palette of about 24 colors, shapes readable at 50% zoom",
        ),
        (
            "palette",
            "mossy green, deep forest green, warm stone gray, soft purple "
            "accents, golden highlights",
        ),
        ("light", "upper-left"),
        ("matte", "pure white"),
        ("canvas", "16:9"),
        ("crop_target", "21:9"),
        ("groundline_pct", "70"),
        ("world", "bright enchanted ruins in a lush fantasy forest"),
        ("mood", "peaceful, magical, adventurous"),
    )
)

DEFAULT_MUST_CONTAIN = (
    "large leafy trees, ancient stone ruins, broken columns and arches, "
    "stone platforms, lush grass, ivy"
)
DEFAULT_NICE_TO_HAVE = "moss, small flowers, scattered rocks"
DEFAULT_LAYER = "MIDDLE GROUND"
DEFAULT_PANELS_PLANNED = 1
DEFAULT_PANEL_NUMBER = 1
CROP_PROTECTION_PCT = "10"
GREEN_MATTE = "#00FF00"
SKY_FORBIDDEN_DETAIL_WORDS = {
    "arch",
    "arches",
    "branch",
    "branches",
    "bush",
    "bushes",
    "canyon",
    "canyons",
    "cliff",
    "cliffs",
    "column",
    "columns",
    "creek",
    "creeks",
    "flower",
    "flowers",
    "foliage",
    "forest",
    "forests",
    "grass",
    "ground",
    "hill",
    "hills",
    "ivy",
    "lake",
    "lakes",
    "moss",
    "mountain",
    "mountains",
    "plant",
    "plants",
    "platform",
    "platforms",
    "river",
    "rivers",
    "rock",
    "rocks",
    "ruin",
    "ruins",
    "sci",
    "shrub",
    "shrubs",
    "stone",
    "stream",
    "streams",
    "structure",
    "structures",
    "tech",
    "terrain",
    "tree",
    "trees",
    "vine",
    "vines",
    "water",
}

START_MODES = (
    "new panel",
)
CONTINUE_MODES = (
    "extend right",
    "extend left",
)
ALL_ACTIVE_MODES = START_MODES + CONTINUE_MODES
LEGACY_MODES = ALL_ACTIVE_MODES + ("new layer, same world", "tileable panel")
BACKUP_MODES = ("ask", "always", "never")
HISTORY_METADATA_KEYS = ("session", "root", "parent", "pass", "review", "overrides")
PASS_TYPES = ("new", "continue", "rollover")

# Short keys for the first mode question, in the same order as START_MODES.
MODE_KEYS = OrderedDict(
    (
        ("n", "new panel"),
    )
)
CONTINUE_MODE_NUMBER = str(len(START_MODES) + 1)

MODE_DESCRIPTIONS = {
    "new panel": "standalone first panel of a layer",
    "extend right": "continue the accepted panel's right edge",
    "extend left": "continue the accepted panel's left edge",
}

LAYER_RECIPES = OrderedDict(
    (
        (
            "SKY",
            "the sky and atmosphere plane only: visual atmosphere fills the entire "
            "canvas, with no ground, terrain, structures, foliage, water, matte, "
            "or non-sky objects of any kind",
        ),
        (
            "FAR BACKGROUND",
            "the farthest scenery plane: pale, hazy, low-detail silhouettes of "
            "distant mountains and a faint treeline, desaturated as if seen from "
            "far away, floating as a horizontal band on the flat matte, with empty "
            "matte filling everything above and below the silhouettes — no painted "
            "sky, no sharp detail, no near objects",
        ),
        (
            "BACKGROUND",
            "a mid-distance scenery band: forested hills, cliff faces, far ruins "
            "at moderate detail and slightly muted color, standing on the flat "
            "matte with empty matte above and below — no painted sky behind it, "
            "no near-ground detail",
        ),
        (
            "MIDDLE GROUND",
            "the main terrain band of the level: ground, stone platforms, trees, "
            "ruins at full detail and full saturation, standing on the flat matte, "
            "with empty matte above the treetops and below the terrain base — a "
            "single cut-out layer like a sprite sheet, NOT a full scene: no sky, "
            "no horizon, no distant mountains behind the artwork",
        ),
        (
            "FOREGROUND",
            "the nearest decoration strip: oversized grass tufts, leaves, vines, "
            "rocks along the lower part of the frame, bold and rich in color, on "
            "the flat matte; this strip may bleed off the bottom edge; nothing "
            "distant appears behind it",
        ),
    )
)

PROMPT_TEMPLATE = """You are an art director producing ONE production game asset: a single parallax
layer for a side-scrolling 2D game, delivered as exactly one image - no
variations, no extra versions. When you call your image tool, carry every
constraint below into the image prompt; they are requirements, not suggestions.

Do not delete, overwrite, replace, move, or clean up any existing repository
file or generated image. If a path already exists or cleanup seems necessary,
stop and ask the user what to do instead of deleting or replacing anything.

{intro_note}

BRIEF
- Layer: {layer_recipe}
- World: {world}
- Mood: {mood}
- Art style: {art_style}
- Palette: {palette}
- Light: one light source from the {light}; every shadow and highlight
  obeys it across the whole image
{layer_brief}
- Canvas: the widest landscape format you support ({canvas}); I will crop to
  {crop_target} later, so keep important content out of the top and bottom {crop_protection_pct}% of
  the frame.

COMPOSITION
{composition_block}
{composition_extra}
EXCLUDE (hard): text, lettering, numbers, watermark, signature, frame or
border, checkerboard pattern, people, creatures, UI elements{exclude_extra}.

Before rendering, confirm to yourself: {final_check}"""

TILEABLE_BY_LAYER = {
    "SKY": (
        "- Make the sky/atmosphere tile horizontally: color, value, texture, and "
        "any user-requested sky detail continue cleanly across the left and right "
        "edges. No mirrored symmetry."
    ),
    "FAR BACKGROUND": (
        "- Make the distant scenery band tile horizontally: band height, silhouette "
        "shape continuation, and color match across the left and right edges. No "
        "mirrored symmetry."
    ),
    "BACKGROUND": (
        "- Make the mid-distance scenery band tile horizontally: band height, shape "
        "continuation, and color match across the left and right edges. No mirrored "
        "symmetry."
    ),
    "MIDDLE GROUND": (
        "- Make the terrain band tile horizontally: ground height, shape continuation, "
        "and color match across the left and right edges. No mirrored symmetry."
    ),
    "FOREGROUND": (
        "- Make the foreground strip tile horizontally: strip height, shape "
        "continuation, and color match across the left and right edges. No mirrored "
        "symmetry."
    ),
}

NEW_LAYER_SNIPPET = """Same world, new depth plane: now render the Layer Recipe named in the BRIEF
below. Reuse the BRIEF's World, Mood, Art style, Palette, Light, Canvas, and
layer-specific matte or transparency rules exactly as stated below, so the
layers stack into one scene."""

OPENING_MARKER = "==== CUSTOMIZED PROMPT — paste to your image model ===="
CLOSING_MARKER = "==== END CUSTOMIZED PROMPT ===="
FOLLOW_UP_LABEL = "FOLLOW-UP (use after accepting panel 1; attach that panel)"

WORKFLOW_TEMPLATE = """VS CODE WORKFLOW
- Before generating or editing any image, save the exact prompt you received to this absolute local path: {prompt_archive_absolute_path}.
- Verify that the prompt archive file exists and has content before calling the image tool.
- Save or copy the accepted generated panel image to this absolute local path: {panel_absolute_path}.
- If your image tool saves to its own generated-images folder first, copy the accepted image to the image path above and leave the original in place.
{pillow_workflow_line}
- After completing the work, write a Markdown work log to this absolute local path: {results_log_absolute_path}. Put a summary at the top, then list key actions, files changed, verification, whether the prompt archive was saved before image generation, and any unresolved issues."""


@dataclass
class HistoryEntry:
    """Store the values from one prompt-building run.

    Each object becomes one line in the RUN HISTORY section of the sibling
    log. World and mood are recorded per run so earlier world/mood combos can
    be reused with the h command even after the current settings change.
    ``reprint_only`` marks a replay of an existing prompt rather than a new generation.
    """

    timestamp: str
    mode: str
    layer: str
    world: str
    mood: str
    must_contain: str
    nice_to_have: str
    panels_planned: int
    panel_number: int
    session_id: str = ""
    root_id: str = ""
    parent_id: str = ""
    pass_type: str = ""
    review: str = ""
    overrides: str = ""
    reprint_only: bool = False


@dataclass
class ContinueSession:
    """Summarize one resumable panel/layer chain for the continue menu.

    The prompt entry is the newest history line in that chain. The accepted
    entry is the newest line with a matching saved image file. The suggested
    next mode/layer/panel values are previewed before the user accepts or edits
    them. ``status`` is one of ``pending output``, ``active``, ``layer complete``, or ``ambiguous``.
    """

    root_id: str
    latest_entry: HistoryEntry
    latest_index: int
    accepted_entry: HistoryEntry | None
    accepted_index: int | None
    status: str
    next_mode: str
    next_layer: str
    next_panel_number: int
    warnings: list[str]


@dataclass
class PillowStatus:
    """Describe the VS Code workspace Python and Pillow availability."""

    python_path: Path
    python_source: str
    workspace_path: Path | None
    available: bool
    version: str = ""
    message: str = ""
    install_attempted: bool = False
    install_succeeded: bool = False


def normalize_spaces(value: str) -> str:
    """Collapse any run of whitespace into single spaces.

    This keeps user-entered values neat when they are saved to the log or
    inserted into generated prompts.
    """
    return " ".join(value.split())


def clean_for_log(value: str) -> str:
    """Make a user-entered value safe for the simple pipe-delimited log format.

    The function preserves the meaning of the user's text while removing
    characters that would confuse history parsing or prompt placeholder checks.
    """
    # " | " is the history-line field separator; [ ] would read as unresolved
    # placeholders in the finished prompt, so both are neutralized at entry.
    return (
        normalize_spaces(value)
        .replace(" | ", " / ")
        .replace("[", "(")
        .replace("]", ")")
    )


def read_log() -> tuple[OrderedDict[str, str], list[HistoryEntry], list[str], bool]:
    """Read settings and history from the sibling log file.

    Returns settings, history entries, a list of recoverable issues, and a
    boolean that tells the caller whether this is the first run with no log yet.
    """
    settings = OrderedDict(DEFAULT_SETTINGS)
    history: list[HistoryEntry] = []
    issues: list[str] = []

    if not LOG_PATH.exists():
        return settings, history, issues, True

    try:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        issues.append(f"could not read log ({exc}); using built-in defaults")
        return settings, history, issues, False

    zone = None
    seen_settings: set[str] = set()
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if line == SETTINGS_MARKER:
            zone = "settings"
            continue
        if line == HISTORY_MARKER:
            zone = "history"
            continue
        if not line or line.startswith("#"):
            continue

        if zone == "settings":
            if "=" not in raw_line:
                issues.append(f"line {line_number} ignored")
                continue
            key, value = raw_line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key not in DEFAULT_SETTINGS:
                issues.append(f"unknown setting on line {line_number} ignored")
                continue
            if key == "mode" and value not in START_MODES:
                settings[key] = DEFAULT_SETTINGS[key]
                issues.append(f"mode on line {line_number} reset to default")
                seen_settings.add(key)
                continue
            if key == "groundline_pct" and not is_int(value):
                issues.append(f"groundline_pct on line {line_number} reset to default")
                seen_settings.add(key)
                continue
            if key == "copy_to_clipboard" and not is_yes_no(value):
                issues.append(f"copy_to_clipboard on line {line_number} reset to default")
                seen_settings.add(key)
                continue
            if key == "backup_mode" and value not in BACKUP_MODES:
                issues.append(f"backup_mode on line {line_number} reset to default")
                seen_settings.add(key)
                continue
            if not value:
                issues.append(f"{key} on line {line_number} reset to default")
                seen_settings.add(key)
                continue
            settings[key] = value
            seen_settings.add(key)
        elif zone == "history":
            parsed = parse_history_line(raw_line, issues=issues, line_number=line_number)
            if parsed is None:
                issues.append(f"history line {line_number} ignored")
            else:
                # lines written before world/mood tracking reuse the current
                # settings values as a silent best-effort backfill
                if not parsed.world:
                    parsed.world = settings["world"]
                if not parsed.mood:
                    parsed.mood = settings["mood"]
                if parsed.panel_number < 1:
                    # Legacy rows lack chain metadata, so parsed-row order is the best available estimate.
                    parsed.panel_number = min(len(history) + 1, parsed.panels_planned)
                history.append(parsed)
        else:
            issues.append(f"line {line_number} outside a known zone ignored")

    for key in SETTING_KEYS:
        if key not in seen_settings:
            issues.append(f"{key} missing; using built-in default")

    return settings, history, issues, False


def parse_history_line(
    line: str, issues: list[str] | None = None, line_number: int | None = None
) -> HistoryEntry | None:
    """Convert one RUN HISTORY text line into a HistoryEntry object.

    If the line does not match the expected format, None is returned so the
    caller can skip the bad line without crashing the script. Lines written
    before world/mood tracking (without world= and mood= fields) are still
    accepted; their world and mood come back empty and read_log backfills
    them from the current settings.
    """
    match = re.match(r"^\[(?P<timestamp>[^\]]+)\]\s+(?P<body>.+)$", line.strip())
    if not match:
        return None

    values: dict[str, str] = {}
    for part in match.group("body").split(" | "):
        if "=" not in part:
            return None
        key, value = part.split("=", 1)
        values[key.strip()] = value.strip()

    full_required = {"mode", "layer", "world", "mood", "must", "nice", "panels_planned"}
    legacy_required = {"mode", "layer", "must", "nice", "panels_planned"}
    required = full_required if full_required <= set(values) else legacy_required
    if not required <= set(values):
        return None
    optional = set(HISTORY_METADATA_KEYS) | {"panel_number"}
    unknown = set(values) - full_required - legacy_required - optional
    if unknown and issues is not None:
        location = f" on line {line_number}" if line_number is not None else ""
        issues.append(
            f"unknown history metadata{location} ignored: {', '.join(sorted(unknown))}"
        )
    if values["mode"] not in LEGACY_MODES:
        return None
    if values["layer"] not in LAYER_RECIPES:
        return None
    if not is_int(values["panels_planned"]):
        return None

    panels_planned = int(values["panels_planned"])
    if panels_planned < 1:
        return None
    panel_number = 0
    if "panel_number" in values:
        if not is_int(values["panel_number"]):
            return None
        panel_number = int(values["panel_number"])
        if panel_number < 1 or panel_number > panels_planned:
            return None
    pass_type = values.get("pass", "")
    if pass_type and pass_type not in PASS_TYPES:
        if issues is not None:
            location = f" on line {line_number}" if line_number is not None else ""
            issues.append(f"history pass metadata{location} ignored")
        pass_type = ""

    return HistoryEntry(
        timestamp=match.group("timestamp"),
        mode=values["mode"],
        layer=values["layer"],
        world=values.get("world", ""),
        mood=values.get("mood", ""),
        must_contain=values["must"],
        nice_to_have=values["nice"],
        panels_planned=panels_planned,
        panel_number=panel_number,
        session_id=values.get("session", ""),
        root_id=values.get("root", ""),
        parent_id=values.get("parent", ""),
        pass_type=pass_type,
        review=values.get("review", ""),
        overrides=values.get("overrides", ""),
    )


def write_log(settings: OrderedDict[str, str], history: list[HistoryEntry]) -> None:
    """Write the full log file atomically.

    The script writes to a temporary file first, then replaces the real log, so
    an interrupted write is less likely to leave a broken log behind.
    """
    lines = [SETTINGS_MARKER]
    for key in SETTING_KEYS:
        lines.append(f"{key} = {settings[key]}")
    lines.append(HISTORY_MARKER)
    for entry in history:
        lines.append(format_history_entry(entry))
    content = "\n".join(lines) + "\n"

    tmp_path = LOG_PATH.with_name(LOG_PATH.name + ".tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, LOG_PATH)
    except OSError:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def format_history_entry(entry: HistoryEntry) -> str:
    """Format one HistoryEntry as the human-readable RUN HISTORY log line."""
    line = (
        f"[{entry.timestamp}] mode={entry.mode} | layer={entry.layer} | "
        f"world={entry.world} | mood={entry.mood} | "
        f"must={entry.must_contain} | nice={entry.nice_to_have} | "
        f"panels_planned={entry.panels_planned} | panel_number={entry.panel_number}"
    )
    metadata = (
        ("session", entry.session_id),
        ("root", entry.root_id),
        ("parent", entry.parent_id),
        ("pass", entry.pass_type),
        ("review", entry.review),
        ("overrides", entry.overrides),
    )
    for key, value in metadata:
        if value:
            line += f" | {key}={value}"
    return line


def is_int(value: str) -> bool:
    """Return True when a string can be converted to an integer."""
    try:
        int(value)
    except ValueError:
        return False
    return True


def is_yes_no(value: str) -> bool:
    """Return True when text is one of the accepted yes/no setting values."""
    return value.strip().lower() in {
        "yes",
        "y",
        "no",
        "n",
        "true",
        "false",
        "1",
        "0",
        "on",
        "off",
    }


def is_enabled(value: str) -> bool:
    """Interpret a yes/no style setting as a Python boolean."""
    return value.strip().lower() in {"yes", "y", "true", "1", "on"}


BACK_COMMANDS = {"b", "back"}
QUIT_COMMANDS = {"q", "quit", "exit"}


class GoBack(Exception):
    """Signal that the user wants to return to the previous question."""


class UserQuit(Exception):
    """Signal that the user wants to quit without continuing the run."""


def is_back_command(value: str) -> bool:
    """Return True when the user typed a recognized back command."""
    return value.strip().lower() in BACK_COMMANDS


def is_quit_command(value: str) -> bool:
    """Return True when the user typed a recognized quit command."""
    return value.strip().lower() in QUIT_COMMANDS


def handle_navigation_command(answer: str, allow_back: bool) -> bool:
    """Handle q/quit and b/back commands before normal answer parsing.

    Returns True when the answer was a navigation command that was handled
    locally. Otherwise returns False so the caller can parse the answer.
    """
    if is_quit_command(answer):
        raise UserQuit()
    if is_back_command(answer):
        if allow_back:
            raise GoBack()
        print("Already at the first question; there is nowhere to go back.")
        return True
    return False


def prompt_block(
    prompt: str,
    hints: list[tuple[str, str]],
    controls: str,
) -> str:
    """Print a readable multi-line question and return the raw answer."""
    print()
    print(prompt)
    for label, value in hints:
        print(f"  {label}: {truncate(value, 70)}")
    print(f"  controls: {controls}")
    return input("> ").strip()


def prompt_text(prompt: str, default: str, allow_back: bool = False) -> str:
    """Ask a text question and return either the answer or the default.

    Pressing Enter accepts the shown default. Non-default answers are cleaned
    before they can be saved to the log. q quits; b goes back when the caller
    allows it.
    """
    answer = prompt_block(
        prompt,
        [("default", default)],
        format_prompt_controls(allow_back=allow_back),
    )
    if handle_navigation_command(answer, allow_back):
        return prompt_text(prompt, default, allow_back=allow_back)
    return default if answer == "" else clean_for_log(answer)


def format_prompt_controls(
    history: bool = False,
    typed: str = "type new value",
    allow_back: bool = True,
    allow_quit: bool = True,
    extra: tuple[str, ...] = (),
) -> str:
    """Build the parenthesized control hint shown after prompt defaults.

    The hint keeps Enter and h behavior visible at the exact question where the
    user needs it, instead of relying on a separate instruction line.
    """
    controls = ["Enter=keep"]
    if history:
        controls.append("h=history")
    if allow_back:
        controls.append("b=back")
    if allow_quit:
        controls.append("q=quit")
    controls.extend(extra)
    if typed:
        controls.append(typed)
    return " | ".join(controls)


def default_source_label(
    default: str, previous_values: list[str], fallback_label: str
) -> str:
    """Choose a display label for a default value.

    If the current default matches the newest history value, it is shown as a
    previous value. Otherwise the caller's fallback label, such as current
    setting or example, is used.
    """
    if previous_values and default == previous_values[0]:
        return "previous"
    return fallback_label


def field_history(history: list[HistoryEntry], attribute: str) -> list[str]:
    """Collect distinct previous values of one history field, newest first.

    Feeds the h control at descriptor questions; capped at 9 entries so a
    single keypress selects any of them.
    """
    values: list[str] = []
    for entry in reversed(history):
        value = str(getattr(entry, attribute)).strip()
        if value and value not in values:
            values.append(value)
        if len(values) == 9:
            break
    return values


def field_history_for_layer(
    history: list[HistoryEntry], attribute: str, layer: str
) -> list[str]:
    """Collect distinct previous field values only for one layer type.

    Layer-specific detail fields should not cross-pollinate. For example, a
    SKY atmosphere detail should not become the default for FAR BACKGROUND.
    """
    values: list[str] = []
    for entry in reversed(history):
        if entry.layer != layer:
            continue
        value = str(getattr(entry, attribute)).strip()
        if value and value not in values:
            values.append(value)
        if len(values) == 9:
            break
    return values


def layer_detail_default(
    current_answer: object | None,
    template: HistoryEntry | None,
    layer: str,
    attribute: str,
    previous_values: list[str],
    fallback: str,
) -> tuple[str, str]:
    """Choose a same-layer default and display label for must/nice fields."""
    if current_answer is not None:
        return str(current_answer), "current answer"
    if template and template.layer == layer:
        value = str(getattr(template, attribute)).strip()
        if value:
            return value, "previous"
    if previous_values:
        return previous_values[0], "previous"
    return fallback, "example"


def is_sky_compatible_detail(value: str) -> bool:
    """Return True when text can safely be used as a SKY detail.

    SKY prompts must not inherit old must/nice values for trees, tech, terrain,
    foliage, or other non-sky content. The check is intentionally conservative:
    if any blocked word appears, the detail is treated as cross-layer pollution.
    """
    words = {match.group(0).lower() for match in re.finditer(r"[A-Za-z0-9]+", value)}
    if not words:
        return False
    return not words.intersection(SKY_FORBIDDEN_DETAIL_WORDS)


def sky_compatible_history(values: list[str]) -> list[str]:
    """Filter a newest-first history list down to SKY-safe detail values."""
    return [value for value in values if is_sky_compatible_detail(value)]


def sky_detail_default(
    current_answer: object | None,
    template_value: str | None,
    previous_values: list[str],
) -> tuple[str, str]:
    """Choose the default value and label for a SKY detail question.

    Current answers win when the user goes back and forward. Reused templates
    and history values are used only when they pass the SKY compatibility check;
    otherwise the default is blank so old non-sky prompt history does not leak
    into a sky-only prompt.
    """
    if current_answer is not None:
        return str(current_answer), "current answer"
    if template_value and is_sky_compatible_detail(template_value):
        return template_value, "previous"
    for value in previous_values:
        if is_sky_compatible_detail(value):
            return value, "previous"
    return "", "default"


def pick_previous_value(previous: list[str], width: int = 70) -> str | None:
    """Let the user pick one full value from a display-truncated history list.

    The list is shown newest-first. Display values may be shortened for the
    terminal, but the returned value is always the full stored string.
    """
    if not previous:
        print("No previous entries recorded for this field.")
        return None
    print("previous entries:")
    for index, value in enumerate(previous, start=1):
        print(f"  {index}. {truncate(value, width)}")
    while True:
        print(f"  controls: Enter=back | q=quit | type number 1-{len(previous)}")
        choice = input("> ").strip()
        if choice == "":
            return None
        if is_quit_command(choice):
            raise UserQuit()
        if is_back_command(choice):
            return None
        if is_int(choice):
            index = int(choice)
            if 1 <= index <= len(previous):
                return previous[index - 1]
        print(f"Enter a number from 1 to {len(previous)}, or press Enter to go back.")


def prompt_text_with_history(
    prompt: str,
    default: str,
    previous: list[str],
    default_label: str = "default",
    example: str | None = None,
    example_label: str = "example",
    allow_back: bool = True,
) -> str:
    """Ask a text question with Enter, h-for-history, and free-typing paths.

    Enter accepts the shown default; h lists this field's distinct previous
    entries to pick by number (Enter backs out); anything else is cleaned and
    used as the new value. The display labels describe where defaults and
    examples came from without changing the accepted value. Trade-off: a
    literal value of just "h" cannot be typed at these questions, which is not
    a realistic descriptor value.
    """
    while True:
        hints = [(default_label, default)]
        # Preserve the baseline example when the default came from history, even if text matches.
        if example is not None and (example != default or default_label == "previous"):
            hints.append((example_label, example))
        answer = prompt_block(
            prompt,
            hints,
            format_prompt_controls(history=True, allow_back=allow_back),
        )
        if answer == "":
            return default
        if handle_navigation_command(answer, allow_back):
            continue
        if answer.lower() != "h":
            return clean_for_log(answer)
        selected = pick_previous_value(previous)
        if selected is not None:
            return selected
        # Enter backs out of history selection and re-asks the question


def prompt_int(
    prompt: str,
    default: int | str,
    minimum: int = 1,
    maximum: int | None = None,
    default_label: str | None = None,
    previous: list[str] | None = None,
    allow_back: bool = False,
) -> int:
    """Ask for an integer until the answer is inside the allowed range.

    The default is used when the user presses Enter. The optional maximum lets
    callers restrict values like percentages to 0 through 100. The optional
    default_label makes reused defaults clearer in terminal prompts. When
    previous values are provided, h opens the same history picker used by text
    prompts.
    """
    default_text = str(default)
    while True:
        answer = prompt_block(
            prompt,
            [(default_label or "default", default_text)],
            format_prompt_controls(
                history=previous is not None,
                typed="type number",
                allow_back=allow_back,
            ),
        )
        if answer == "":
            value = default_text
        elif handle_navigation_command(answer, allow_back):
            continue
        elif previous is not None and answer.lower() == "h":
            selected = pick_previous_value(previous)
            if selected is None:
                continue
            value = selected
        else:
            value = answer
        if is_int(value):
            number = int(value)
            if number >= minimum and (maximum is None or number <= maximum):
                return number
        if maximum is None:
            print(f"Enter an integer {minimum} or greater.")
        else:
            print(f"Enter an integer from {minimum} to {maximum}.")


def ask_yes_no(prompt: str, default: str = "n", allow_back: bool = False) -> bool:
    """Ask a yes/no question and return True for yes or False for no."""
    default = default.lower()
    while True:
        answer = prompt_block(
            prompt,
            [("default", default)],
            format_prompt_controls(
                typed="type y or n",
                allow_back=allow_back,
            ),
        ).lower()
        if handle_navigation_command(answer, allow_back):
            continue
        value = default if answer == "" else answer
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Enter y or n.")


def find_workspace_file() -> Path | None:
    """Return a VS Code workspace file from this repo's `_workspace/` folder, if any.

    Repo-name-agnostic: picks the first `*.code-workspace` found, so the repo can be
    renamed freely. (`_workspace/` is local and gitignored.)
    """
    workspace_dir = REPO_ROOT / "_workspace"
    if not workspace_dir.is_dir():
        return None
    workspaces = sorted(workspace_dir.glob("*.code-workspace"))
    return workspaces[0] if workspaces else None


def read_workspace_json(workspace_path: Path) -> tuple[dict[str, object] | None, str]:
    """Read a VS Code workspace JSON file without failing the whole script."""
    try:
        data = json.loads(workspace_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"could not read workspace file ({exc})"
    if not isinstance(data, dict):
        return None, "workspace file did not contain a JSON object"
    return data, ""


def workspace_folder_roots(
    workspace_data: dict[str, object], workspace_path: Path
) -> dict[str, Path]:
    """Map folder names last-wins and add ``default`` for the first valid workspace folder."""
    roots: dict[str, Path] = {}
    folders = workspace_data.get("folders", [])
    if not isinstance(folders, list):
        return roots
    for item in folders:
        if not isinstance(item, dict):
            continue
        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        raw_name = item.get("name")
        folder_path = Path(raw_path)
        if not folder_path.is_absolute():
            folder_path = workspace_path.parent / folder_path
        resolved = folder_path.resolve()
        if isinstance(raw_name, str) and raw_name.strip():
            roots[raw_name] = resolved
        roots.setdefault("default", resolved)
    return roots


def expand_workspace_folder_vars(
    value: str, workspace_data: dict[str, object], workspace_path: Path
) -> str:
    """Expand simple VS Code ${workspaceFolder} variables in path settings."""
    roots = workspace_folder_roots(workspace_data, workspace_path)
    if not roots:
        roots["default"] = REPO_ROOT

    def replace_match(match: re.Match[str]) -> str:
        """Resolve one workspaceFolder placeholder against named or default roots."""
        folder_name = match.group(1)
        if folder_name:
            return str(roots.get(folder_name, REPO_ROOT))
        return str(roots.get("default", REPO_ROOT))

    return re.sub(r"\$\{workspaceFolder(?::([^}]+))?\}", replace_match, value)


def resolve_workspace_path(
    value: str, workspace_data: dict[str, object], workspace_path: Path
) -> Path:
    """Expand workspace variables, but resolve any remaining relative path against ``REPO_ROOT``."""
    expanded = expand_workspace_folder_vars(value.strip(), workspace_data, workspace_path)
    path = Path(expanded)
    if path.is_absolute():
        return path.resolve()
    return (REPO_ROOT / path).resolve()


def discover_workspace_python() -> tuple[Path, str, Path | None, str]:
    """Find the Python interpreter declared by the VS Code workspace."""
    fallback = Path(sys.executable).resolve()
    workspace_path = find_workspace_file()
    if workspace_path is None:
        return fallback, "current Python fallback", None, "workspace file not found"

    workspace_data, issue = read_workspace_json(workspace_path)
    if workspace_data is None:
        return fallback, "current Python fallback", workspace_path, issue

    settings = workspace_data.get("settings", {})
    if not isinstance(settings, dict):
        return fallback, "current Python fallback", workspace_path, "workspace settings missing"
    raw_python = settings.get("python.defaultInterpreterPath")
    if not isinstance(raw_python, str) or not raw_python.strip():
        return (
            fallback,
            "current Python fallback",
            workspace_path,
            "python.defaultInterpreterPath missing",
        )

    python_path = resolve_workspace_path(raw_python, workspace_data, workspace_path)
    if not python_path.is_file():
        return (
            fallback,
            "current Python fallback",
            workspace_path,
            f"workspace interpreter not found: {python_path}",
        )
    return python_path, "workspace python.defaultInterpreterPath", workspace_path, ""


def check_pillow_status(
    python_path: Path,
    python_source: str,
    workspace_path: Path | None,
    message: str = "",
) -> PillowStatus:
    """Check whether Pillow is importable from the selected Python interpreter."""
    code = (
        "import importlib.util, sys\n"
        "spec = importlib.util.find_spec('PIL')\n"
        "if spec is None:\n"
        "    print('MISSING')\n"
        "    raise SystemExit(0)\n"
        "import PIL\n"
        "print('FOUND ' + getattr(PIL, '__version__', 'unknown'))\n"
    )
    try:
        result = subprocess.run(
            [str(python_path), "-B", "-c", code],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return PillowStatus(
            python_path=python_path,
            python_source=python_source,
            workspace_path=workspace_path,
            available=False,
            message=f"{message}; Pillow check failed ({exc})".strip("; "),
        )

    output = result.stdout.strip()
    if result.returncode != 0:
        detail = result.stderr.strip() or output or f"exit code {result.returncode}"
        return PillowStatus(
            python_path=python_path,
            python_source=python_source,
            workspace_path=workspace_path,
            available=False,
            message=f"{message}; Pillow check failed ({detail})".strip("; "),
        )
    if output.startswith("FOUND "):
        return PillowStatus(
            python_path=python_path,
            python_source=python_source,
            workspace_path=workspace_path,
            available=True,
            version=output.removeprefix("FOUND ").strip() or "unknown",
            message=message,
        )
    return PillowStatus(
        python_path=python_path,
        python_source=python_source,
        workspace_path=workspace_path,
        available=False,
        message=message or "Pillow not installed",
    )


def install_pillow(python_path: Path) -> tuple[bool, str]:
    """Install Pillow into the selected interpreter with pip."""
    try:
        result = subprocess.run(
            [str(python_path), "-m", "pip", "install", "Pillow"],
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, f"pip exited with code {result.returncode}"
    return True, ""


def pillow_workflow_line(status: PillowStatus | None) -> str:
    """Build workflow guidance for image metadata verification."""
    if status is None:
        return (
            "- Image verification environment: no Pillow preflight was run; use a "
            "non-mutating fallback metadata check and record that Pillow status was "
            "not checked in the work log."
        )
    if status.available:
        return (
            "- Image verification environment: Python checked "
            f"({status.python_source}) at {status.python_path}; Pillow "
            f"{status.version} is available, so you may use Pillow for image "
            "metadata checks and record that in the work log."
        )
    return (
        "- Image verification environment: Python checked "
        f"({status.python_source}) at {status.python_path}; Pillow unavailable. "
        "Use a non-mutating fallback metadata check and record `Pillow unavailable` "
        "in the work log."
    )


def print_pillow_status(status: PillowStatus) -> None:
    """Print a concise terminal preflight summary."""
    if status.available:
        print(
            f"Python ({status.python_source}): {status.python_path} | "
            f"Pillow: {status.version}"
        )
    else:
        suffix = f" ({status.message})" if status.message else ""
        print(
            f"Python ({status.python_source}): {status.python_path} | "
            f"Pillow: unavailable{suffix}"
        )


def run_pillow_preflight() -> PillowStatus:
    """Check Pillow and optionally install it when the user approves."""
    python_path, python_source, workspace_path, issue = discover_workspace_python()
    if issue:
        print(f"Workspace Python notice: {issue}")
    status = check_pillow_status(python_path, python_source, workspace_path, issue)
    print_pillow_status(status)
    if status.available:
        return status

    try:
        should_install = ask_yes_no(
            f"Install Pillow into {python_path} for image metadata verification?",
            "n",
        )
    except EOFError:
        print("Input ended at Pillow install prompt; continuing without Pillow.")
        return status
    if not should_install:
        print("Pillow install skipped; prompt generation will continue.")
        return status

    print(f"Installing Pillow with: {python_path} -m pip install Pillow")
    installed, install_issue = install_pillow(python_path)
    checked = check_pillow_status(python_path, python_source, workspace_path, issue)
    checked.install_attempted = True
    checked.install_succeeded = installed and checked.available
    if checked.install_succeeded:
        print_pillow_status(checked)
        return checked
    checked.message = install_issue or checked.message or "Pillow install did not verify"
    print(f"Pillow install did not verify: {checked.message}")
    return checked


def prompt_choice(
    prompt: str, choices: tuple[str, ...], default: str, allow_back: bool = False
) -> str:
    """Ask a question whose answer must be one of a fixed set of choices."""
    while True:
        answer = prompt_block(
            prompt,
            [("default", default), ("choices", " / ".join(choices))],
            format_prompt_controls(
                typed="type choice",
                allow_back=allow_back,
            ),
        ).lower()
        if handle_navigation_command(answer, allow_back):
            continue
        value = default if answer == "" else answer
        if value in choices:
            return value
        print("Choose one of the listed options.")


def prompt_yes_no_setting(prompt: str, default: str, allow_back: bool = False) -> str:
    """Ask for a yes/no setting and return the normalized text yes or no."""
    return (
        "yes"
        if ask_yes_no(prompt, "y" if is_enabled(default) else "n", allow_back=allow_back)
        else "no"
    )


def normalize_hex_color(value: str) -> str | None:
    """Return a normalized #RRGGBB color or None when the text is invalid."""
    cleaned = value.strip()
    if cleaned.startswith("#"):
        cleaned = cleaned[1:]
    if re.fullmatch(r"[0-9A-Fa-f]{6}", cleaned):
        return f"#{cleaned.upper()}"
    return None


def is_transparency_matte(value: str) -> bool:
    """Return True when the matte setting requests a real alpha channel."""
    return value.strip().lower() in {"transparency", "transparent", "true transparency"}


def matte_choice_label(value: str) -> str:
    """Describe the current matte or transparency setting for the prompt."""
    normalized_hex = normalize_hex_color(value)
    lowered = value.strip().lower()
    if lowered in {"pure white", "white"}:
        return "White"
    if lowered in {"pure black", "black"}:
        return "Black"
    if lowered in {"green", "chroma key green", GREEN_MATTE.lower()}:
        return f"Green ({GREEN_MATTE})"
    if normalized_hex:
        return normalized_hex
    if is_transparency_matte(value):
        return "Transparency"
    return value


def prompt_hex_color(allow_back: bool = True) -> str:
    """Ask for a real 6-digit hexadecimal color and normalize it."""
    while True:
        answer = prompt_block(
            "hexadecimal matte color",
            [("example", "#FFBB00 or FFBB00")],
            format_prompt_controls(
                typed="type exactly 6 hex digits, with optional #",
                allow_back=allow_back,
                allow_quit=True,
            ),
        )
        if handle_navigation_command(answer, allow_back):
            continue
        normalized = normalize_hex_color(answer)
        if normalized is not None:
            return normalized
        print("Enter exactly 6 hexadecimal digits, optionally prefixed with #, such as #FFBB00 or FFBB00.")


def ask_matte_or_alpha(default: str, allow_back: bool = True) -> str:
    """Ask how non-sky transparent areas should be represented."""
    print(
        "Non-sky layers need a matte or alpha choice. Recommendation: use a solid "
        "matte color because image LLMs often struggle to create proper PNG alpha channels."
    )
    while True:
        answer = prompt_block(
            "matte or transparency for this non-sky layer",
            [
                ("default", matte_choice_label(default)),
                ("1", "White"),
                ("2", "Black"),
                ("3", f"Green ({GREEN_MATTE})"),
                ("4", "Hexadecimal color"),
                ("5", "Transparency"),
            ],
            format_prompt_controls(
                typed="type 1-5",
                allow_back=allow_back,
                allow_quit=True,
            ),
        ).lower()
        if answer == "":
            normalized_default = normalize_hex_color(default)
            if normalized_default is not None:
                return normalized_default
            if is_transparency_matte(default):
                print("Transparency selected. Warning: image LLMs often fail to produce true PNG alpha channels.")
                return "true transparency"
            lowered = default.strip().lower()
            if lowered in {"pure white", "white"}:
                return "pure white"
            if lowered in {"pure black", "black"}:
                return "pure black"
            if lowered in {"green", "chroma key green", GREEN_MATTE.lower()}:
                return GREEN_MATTE
            return default
        if handle_navigation_command(answer, allow_back):
            continue
        if answer == "1":
            return "pure white"
        if answer == "2":
            return "pure black"
        if answer == "3":
            return GREEN_MATTE
        if answer == "4":
            return prompt_hex_color(allow_back=True)
        if answer == "5":
            print("Transparency selected. Warning: image LLMs often fail to produce true PNG alpha channels.")
            return "true transparency"
        print("Choose 1, 2, 3, 4, or 5.")


def ensure_output_folder_if_requested(output_folder: str) -> None:
    """Create the output folder only after the user explicitly agrees.

    If the folder already exists, nothing happens. This keeps folder creation in
    the script's ask-first permission tier.
    """
    folder = Path(output_folder)
    if folder.exists():
        return
    if ask_yes_no(f"Output folder does not exist: {folder}. Create it?", "n"):
        folder.mkdir(parents=True, exist_ok=True)
        print(f"Created output folder: {folder}")
    else:
        print("Output folder not created; the prompt footer will still show the target path.")


def walk_settings(
    settings: OrderedDict[str, str], history: list[HistoryEntry] | None = None
) -> tuple[OrderedDict[str, str], bool]:
    """Ask the user to review and optionally change every persistent setting.

    A copy of the settings is edited and returned with a boolean that tells the
    caller whether anything changed. `mode` is skipped here because it is
    already the first question of every run; its SETTINGS entry only remembers
    the last-used default. World and mood can reuse RUN HISTORY values when
    history exists because those are the persistent settings with useful per-run history.
    """
    history = history or []
    updated = OrderedDict(settings)
    editable_keys = [key for key in SETTING_KEYS if key != "mode"]
    print("Settings: press Enter to keep each default.")
    index = 0
    while index < len(editable_keys):
        key = editable_keys[index]
        try:
            if key == "groundline_pct":
                updated[key] = str(
                    prompt_int(
                        "groundline_pct",
                        updated[key],
                        minimum=0,
                        maximum=100,
                        allow_back=index > 0,
                    )
                )
            elif key == "copy_to_clipboard":
                updated[key] = prompt_yes_no_setting(
                    "copy_to_clipboard", updated[key], allow_back=index > 0
                )
            elif key == "backup_mode":
                updated[key] = prompt_choice(
                    "backup_mode", BACKUP_MODES, updated[key], allow_back=index > 0
                )
            elif key in {"world", "mood"} and field_history(history, key):
                previous = field_history(history, key)
                updated[key] = prompt_text_with_history(
                    key,
                    updated[key],
                    previous,
                    default_source_label(updated[key], previous, "current setting"),
                    DEFAULT_SETTINGS[key],
                    allow_back=index > 0,
                )
            else:
                updated[key] = prompt_text(key, updated[key], allow_back=index > 0)
        except GoBack:
            index = max(0, index - 1)
            continue
        index += 1
    return updated, updated != settings


def resolve_mode(answer: str) -> str | None:
    """Translate a mode answer (digit, short key, or full name) into a mode."""
    if answer in START_MODES:
        return answer
    if answer in MODE_KEYS:
        return MODE_KEYS[answer]
    if is_int(answer):
        index = int(answer)
        if 1 <= index <= len(START_MODES):
            return START_MODES[index - 1]
    return None


def print_mode_menu() -> None:
    """Show the numbered mode menu with short keys and one-line descriptions."""
    print("modes:")
    for index, (key, mode_name) in enumerate(MODE_KEYS.items(), start=1):
        print(f"  {index} / {key:<2} - {mode_name}: {MODE_DESCRIPTIONS[mode_name]}")
    print(f"  {CONTINUE_MODE_NUMBER} / c  - continue/resume: continue an existing panel session")
    print("commands: r = reprint last prompt | s = settings | h = history options")


def truncate(value: str, width: int) -> str:
    """Shorten display text to a fixed width with an ellipsis."""
    if width <= 0:
        return ""
    return value if len(value) <= width else value[: width - 1] + "…"


def effective_entry_id(entry: HistoryEntry, index: int) -> str:
    """Return a stable ID for a history entry, including legacy log lines."""
    if entry.session_id:
        return entry.session_id
    identity = "|".join(
        (
            str(index),
            entry.timestamp,
            entry.mode,
            entry.layer,
            entry.world,
            entry.mood,
            entry.must_contain,
            entry.nice_to_have,
            str(entry.panels_planned),
            str(entry.panel_number),
        )
    )
    return "legacy" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:8]


def known_history_ids(history: list[HistoryEntry]) -> set[str]:
    """Collect explicit and derived history IDs that new sessions must avoid."""
    known: set[str] = set()
    for index, entry in enumerate(history):
        for value in (
            effective_entry_id(entry, index),
            entry.session_id,
            entry.root_id,
            entry.parent_id,
        ):
            if value:
                known.add(value)
    return known


def generate_session_id(history: list[HistoryEntry]) -> str:
    """Create a collision-checked 8-hex-character session ID."""
    known = known_history_ids(history)
    while True:
        candidate = secrets.token_hex(4)
        if candidate not in known:
            return candidate


def next_layer_after(layer: str) -> str | None:
    """Return the next Layer Recipe name after the given layer, if one exists."""
    layers = list(LAYER_RECIPES)
    if layer not in layers:
        return None
    next_index = layers.index(layer) + 1
    if next_index >= len(layers):
        return None
    return layers[next_index]


def next_continue_mode(entry: HistoryEntry) -> str:
    """Choose the default continue direction from the latest history entry."""
    if entry.mode in {"extend right", "extend left"}:
        return entry.mode
    return "extend right"


def legacy_asset_filenames(run: HistoryEntry) -> list[str]:
    """Return older image filenames that may already exist for a history entry."""
    number = run.panel_number
    world_word = first_slug_word(run.world, skip_stopwords=True, fallback="world")
    mood_word = first_slug_word(run.mood, fallback="mood")
    layer = layer_slug(run.layer)
    stamp = filename_stamp(run.timestamp)
    date = filename_date(run.timestamp)
    return [
        (
            f"{world_word}-{mood_word}_{layer}_panel_{number:02d}-of-"
            f"{run.panels_planned:02d}-{stamp}.png"
        ),
        (
            f"{world_word}-{mood_word}_{layer}_panel{number:02d}-of-"
            f"{run.panels_planned:02d}_{date}.png"
        ),
        (
            f"{world_word}-{mood_word}-panel_{number:02d}-of-"
            f"{run.panels_planned:02d}-{stamp}.png"
        ),
        f"{world_word}-{mood_word}-panel_{number:02d}.png",
        f"panel_{number:02d}.png",
    ]


def asset_filename_stems(
    run: HistoryEntry, panel_number: int | None = None
) -> list[str]:
    """Return current and legacy image filename stems for one panel identity."""
    target_run = (
        replace(run, panel_number=panel_number) if panel_number is not None else run
    )
    filenames = [asset_filename(target_run), *legacy_asset_filenames(target_run)]
    stems: list[str] = []
    seen: set[str] = set()
    for filename in filenames:
        stem = Path(filename).stem
        key = stem.lower()
        if key not in seen:
            stems.append(stem)
            seen.add(key)
    return stems


def asset_family_version(filename: str, stem: str) -> int | None:
    """Return 1 for the canonical PNG, N for a case-insensitive ``-vNN`` member, or None."""
    if filename.lower() == f"{stem}.png".lower():
        return 1
    match = re.fullmatch(rf"{re.escape(stem)}-v(\d{{2,}})\.png", filename, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def image_family_paths(
    settings: OrderedDict[str, str],
    run: HistoryEntry,
    panel_number: int | None = None,
    include_legacy: bool = True,
) -> list[Path]:
    """Return exact and versioned image paths for one panel filename family."""
    folder = absolute_artifact_folder(settings["output_folder"])
    stems = asset_filename_stems(run, panel_number)
    if not include_legacy:
        stems = stems[:1]

    paths: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        """Append one path once using a case-insensitive identity key."""
        key = str(path).lower()
        if key not in seen:
            paths.append(path)
            seen.add(key)

    for stem in stems:
        add(folder / f"{stem}.png")
        try:
            if folder.is_dir():
                for candidate in folder.glob(f"{stem}-v*.png"):
                    if asset_family_version(candidate.name, stem) is not None:
                        add(candidate)
        except OSError:
            continue

    return paths


def accepted_output_paths(settings: OrderedDict[str, str], run: HistoryEntry) -> list[Path]:
    """Return current, versioned, and legacy paths that can prove acceptance."""
    return image_family_paths(settings, run, include_legacy=True)


def accepted_output_path(
    settings: OrderedDict[str, str], run: HistoryEntry
) -> Path | None:
    """Return the newest non-empty saved image path for an accepted panel."""
    candidates: list[tuple[float, int, str, Path]] = []
    for path in accepted_output_paths(settings, run):
        try:
            if path.is_file() and path.stat().st_size > 0:
                version_match = re.search(r"-v(\d{2,})\.png$", path.name, re.IGNORECASE)
                version = int(version_match.group(1)) if version_match else 1
                candidates.append((path.stat().st_mtime, version, path.name.lower(), path))
        except OSError:
            continue
    if candidates:
        return max(candidates, key=lambda item: (item[0], item[1], item[2]))[3]
    return None


def accepted_output_exists(settings: OrderedDict[str, str], run: HistoryEntry) -> bool:
    """Return True when a history entry has a non-empty saved image output."""
    return accepted_output_path(settings, run) is not None


def reference_panel_entries(
    settings: OrderedDict[str, str],
    history: list[HistoryEntry],
    run: HistoryEntry,
) -> list[tuple[HistoryEntry, Path]]:
    """Return accepted same-root, same-layer panels needed as references."""
    target_root = run.root_id
    if not target_root:
        raise RuntimeError(
            "extend prompt requires a logged continue root id; start from a new "
            "panel generated by the current script before continuing"
        )

    by_panel: dict[int, tuple[int, HistoryEntry, Path]] = {}
    for index, entry in enumerate(history):
        if entry.root_id != target_root:
            continue
        if entry.layer != run.layer:
            continue
        if entry.panel_number >= run.panel_number:
            continue
        path = accepted_output_path(settings, entry)
        if path is None:
            continue
        by_panel[entry.panel_number] = (index, entry, path)

    return [
        (entry, path)
        for _panel_number, (_index, entry, path) in sorted(by_panel.items())
    ]


def reference_panels_section(
    settings: OrderedDict[str, str],
    history: list[HistoryEntry],
    run: HistoryEntry,
) -> str:
    """Build the REFERENCE PANELS section required for extend prompts."""
    references = reference_panel_entries(settings, history, run)
    if not references:
        raise RuntimeError(
            "extend prompt needs at least one accepted same-layer reference image"
        )

    immediate_entry, immediate_path = references[-1]
    lines = [
        "REFERENCE PANELS",
        "- Load these verified accepted panel image files as visual references before generation.",
    ]
    for entry, path in references:
        lines.append(f"- Panel {entry.panel_number}: {path}")
    lines.append(
        f"- Immediate seam reference: panel {immediate_entry.panel_number}: {immediate_path}"
    )
    return "\n".join(lines)


def find_continue_sessions(
    settings: OrderedDict[str, str], history: list[HistoryEntry]
) -> list[ContinueSession]:
    """Build newest-first chains, classifying ambiguous, pending, active, and layer-complete state.

    Empty roots are excluded, duplicate session ids make a chain ambiguous, and next mode/layer/panel
    values derive from the latest accepted output or the still-pending prompt.
    """
    session_counts: dict[str, int] = {}
    for entry in history:
        if entry.session_id:
            session_counts[entry.session_id] = session_counts.get(entry.session_id, 0) + 1
    duplicate_ids = {
        session_id for session_id, count in session_counts.items() if count > 1
    }

    chains: dict[str, list[tuple[int, HistoryEntry]]] = {}
    for index, entry in enumerate(history):
        root_id = entry.root_id
        if not root_id:
            continue
        chains.setdefault(root_id, []).append((index, entry))

    sessions: list[ContinueSession] = []
    for root_id, entries in chains.items():
        latest_index, latest = max(entries, key=lambda item: item[0])
        accepted_entries = [
            (index, entry)
            for index, entry in entries
            if accepted_output_exists(settings, entry)
        ]
        if accepted_entries:
            accepted_index, accepted = max(accepted_entries, key=lambda item: item[0])
        else:
            accepted_index, accepted = None, None
        warnings = [
            f"duplicate session id {entry.session_id}"
            for _index, entry in entries
            if entry.session_id in duplicate_ids
        ]
        if warnings:
            status = "ambiguous"
        elif accepted is None:
            status = "pending output"
        elif latest_index > accepted_index:
            status = "pending output"
        elif accepted.panel_number < accepted.panels_planned:
            status = "active"
        else:
            status = "layer complete"

        if status == "pending output":
            next_mode = latest.mode
            next_layer = latest.layer
            next_panel_number = latest.panel_number
        elif status == "active" and accepted is not None:
            next_mode = next_continue_mode(accepted)
            next_layer = accepted.layer
            next_panel_number = accepted.panel_number + 1
        elif accepted is not None:
            next_mode = "new panel"
            next_layer = next_layer_after(accepted.layer) or ""
            next_panel_number = 1
        else:
            next_mode = "new panel"
            next_layer = next_layer_after(latest.layer) or ""
            next_panel_number = 1

        sessions.append(
            ContinueSession(
                root_id=root_id,
                latest_entry=latest,
                latest_index=latest_index,
                accepted_entry=accepted,
                accepted_index=accepted_index,
                status=status,
                next_mode=next_mode,
                next_layer=next_layer,
                next_panel_number=next_panel_number,
                warnings=sorted(set(warnings)),
            )
        )

    return sorted(sessions, key=lambda session: session.latest_index, reverse=True)


def describe_continue_session(session: ContinueSession) -> str:
    """Build one readable continue-menu line for a session."""
    latest = session.latest_entry
    prompt_position = f"{latest.panel_number} of {latest.panels_planned}"
    if session.accepted_entry is not None:
        accepted = session.accepted_entry
        accepted_text = (
            f"latest accepted panel {accepted.panel_number} of "
            f"{accepted.panels_planned}"
        )
    else:
        accepted_text = "no accepted image found"

    if session.status == "pending output":
        next_text = (
            f"pending prompt panel {prompt_position}; image not found | "
            f"next panel {session.next_panel_number} of {latest.panels_planned}"
        )
    elif session.status == "active":
        next_text = (
            f"next {session.next_mode}, panel {session.next_panel_number} "
            f"of {latest.panels_planned}"
        )
    elif session.next_layer:
        next_text = f"next layer {session.next_layer}, new panel"
    else:
        next_text = "choose a layer"
    warning = f" | {'; '.join(session.warnings)}" if session.warnings else ""
    return (
        f"{session.root_id} | {latest.layer} | latest prompt panel {prompt_position} | "
        f"{accepted_text} | {latest.mode} | {session.status} | {next_text}{warning}"
    )


def choose_continue_session(
    settings: OrderedDict[str, str], history: list[HistoryEntry]
) -> ContinueSession | None:
    """Let the user select one resumable session from newest-first state."""
    sessions = find_continue_sessions(settings, history)
    if not sessions:
        print("No continue sessions are available. Start with a new panel first.")
        return None

    print("continue sessions (newest first):")
    for index, session in enumerate(sessions, start=1):
        selectable = "not selectable" if session.status == "ambiguous" else "selectable"
        print(f"  {index}. {describe_continue_session(session)} | {selectable}")

    recommended = next(
        (index for index, session in enumerate(sessions, start=1) if session.status in {"pending output", "active"}),
        next((index for index, session in enumerate(sessions, start=1) if session.status != "ambiguous"), None),
    )
    if recommended is None:
        print("All continue sessions are ambiguous; start a new panel instead.")
        return None

    while True:
        print(
            f"  controls: Enter=recommended {recommended} | q=quit | "
            f"type number 1-{len(sessions)}"
        )
        answer = input("> ").strip()
        if answer == "":
            return sessions[recommended - 1]
        if is_quit_command(answer):
            raise UserQuit()
        if is_back_command(answer):
            return None
        if is_int(answer):
            index = int(answer)
            if 1 <= index <= len(sessions):
                session = sessions[index - 1]
                if session.status == "ambiguous":
                    print("That session has duplicate IDs and cannot be continued safely.")
                    continue
                return session
        print(f"Enter a number from 1 to {len(sessions)}, or press Enter for the recommendation.")


def pick_previous_run(
    history: list[HistoryEntry], allowed_modes: tuple[str, ...] | None = None
) -> HistoryEntry | None:
    """Let the user pick a previous run to reuse as this run's defaults.

    Runs are listed newest first and deduplicated by content combo (layer,
    world, mood, must, nice), keeping the newest of each combo, capped at 9
    so a single keypress selects any entry.
    """
    candidates: list[HistoryEntry] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for entry in reversed(history):
        if allowed_modes is not None and entry.mode not in allowed_modes:
            continue
        combo = (entry.layer, entry.world, entry.mood, entry.must_contain, entry.nice_to_have)
        if combo in seen:
            continue
        seen.add(combo)
        candidates.append(entry)
        if len(candidates) == 9:
            break
    if not candidates:
        print("No previous runs are available in the log.")
        return None

    print("previous runs (newest first):")
    for index, entry in enumerate(candidates, start=1):
        print(
            f"  {index}. [{entry.timestamp}] {entry.layer} | "
            f"{truncate(entry.world, 40)} | {truncate(entry.mood, 25)} | "
            f"must: {truncate(entry.must_contain, 45)}"
        )
    while True:
        print(f"  controls: Enter=back | q=quit | type number 1-{len(candidates)}")
        answer = input("> ").strip()
        if answer == "":
            return None
        if is_quit_command(answer):
            raise UserQuit()
        if is_back_command(answer):
            return None
        if is_int(answer):
            index = int(answer)
            if 1 <= index <= len(candidates):
                return candidates[index - 1]
        print(f"Enter a number from 1 to {len(candidates)}, or press Enter to go back.")


def pick_mode_history(history: list[HistoryEntry]) -> tuple[str | None, HistoryEntry | None]:
    """Offer mode-specific history choices for the first question.

    The user can either pick only a previous mode value or reuse a whole
    previous run as this run's defaults. Returning a HistoryEntry marks the
    whole-run reuse path.
    """
    if not history:
        print("No previous entries recorded for mode.")
        return None, None

    while True:
        print("history options:")
        print("  1. pick previous mode only")
        print("  2. reuse full previous run as defaults")
        print("  controls: Enter=back | q=quit | type number 1-2")
        answer = input("> ").strip()
        if answer == "":
            return None, None
        if is_quit_command(answer):
            raise UserQuit()
        if is_back_command(answer):
            return None, None
        if answer == "1":
            previous_modes = [
                mode for mode in field_history(history, "mode") if mode in START_MODES
            ]
            picked_mode = pick_previous_value(previous_modes, width=40)
            if picked_mode is not None:
                return picked_mode, None
        elif answer == "2":
            picked_run = pick_previous_run(history, START_MODES)
            if picked_run is not None:
                return picked_run.mode, picked_run
        else:
            print("Choose 1 or 2, or press Enter to go back.")


def default_detail_values_for_layer(
    history: list[HistoryEntry], layer: str
) -> tuple[str, str]:
    """Return sensible must/nice defaults for a layer during rollover."""
    must_history = field_history_for_layer(history, "must_contain", layer)
    nice_history = field_history_for_layer(history, "nice_to_have", layer)
    if layer == "SKY":
        must_default, _must_label = sky_detail_default(
            None, None, sky_compatible_history(must_history)
        )
        nice_default, _nice_label = sky_detail_default(
            None, None, sky_compatible_history(nice_history)
        )
        return must_default, nice_default
    must_default, _must_label = layer_detail_default(
        None, None, layer, "must_contain", must_history, DEFAULT_MUST_CONTAIN
    )
    nice_default, _nice_label = layer_detail_default(
        None, None, layer, "nice_to_have", nice_history, DEFAULT_NICE_TO_HAVE
    )
    return must_default, nice_default


def continue_panel_count_default(history: list[HistoryEntry], layer: str) -> int:
    """Choose a panel-count default when rolling over to a new layer."""
    for entry in reversed(history):
        if entry.layer == layer and entry.panels_planned >= 1:
            return entry.panels_planned
    return DEFAULT_PANELS_PLANNED


def build_continue_candidate(
    settings: OrderedDict[str, str],
    history: list[HistoryEntry],
    selected_session: ContinueSession,
) -> HistoryEntry:
    """Create the proposed next history entry for a continue session."""
    latest = selected_session.latest_entry
    if selected_session.status == "pending output":
        print("Reprinting the pending prompt; no new history line will be added.")
        return replace(latest, reprint_only=True)

    base_entry = selected_session.accepted_entry or latest
    base_index = (
        selected_session.accepted_index
        if selected_session.accepted_index is not None
        else selected_session.latest_index
    )
    parent_id = effective_entry_id(base_entry, base_index)
    session_id = generate_session_id(history)
    root_id = selected_session.root_id or parent_id

    if selected_session.status == "active":
        mode = selected_session.next_mode
        layer = base_entry.layer
        panels_planned = base_entry.panels_planned
        panel_number = selected_session.next_panel_number
        must_contain = base_entry.must_contain
        nice_to_have = base_entry.nice_to_have
        pass_type = "continue"
    else:
        layer = selected_session.next_layer
        if not layer:
            print("The selected session is complete at the final layer.")
            layer = ask_layer(DEFAULT_LAYER, "default", field_history(history, "layer"))
        mode = "new panel"
        panels_planned = prompt_int(
            "How many panels do you plan to prompt for this specific layer?",
            continue_panel_count_default(history, layer),
            minimum=1,
            previous=field_history(history, "panels_planned"),
            allow_back=False,
        )
        panel_number = 1
        must_contain, nice_to_have = default_detail_values_for_layer(history, layer)
        pass_type = "rollover"

    return HistoryEntry(
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
        mode=mode,
        layer=layer,
        world=base_entry.world,
        mood=base_entry.mood,
        must_contain=must_contain,
        nice_to_have=nice_to_have,
        panels_planned=panels_planned,
        panel_number=panel_number,
        session_id=session_id,
        root_id=root_id,
        parent_id=parent_id,
        pass_type=pass_type,
    )


def confirm_continue_group(title: str, lines: list[str]) -> bool:
    """Show one continue review group and return True when accepted."""
    print()
    print(f"{title}:")
    for line in lines:
        print(f"  {line}")
    return ask_yes_no(f"Use these {title.lower()} values?", default="y")


def mark_changed(
    overrides: set[str],
    before: dict[str, object],
    after: dict[str, object],
) -> None:
    """Record every field whose value changed during a review group."""
    for key, before_value in before.items():
        if after.get(key) != before_value:
            overrides.add(key)


def review_continue_target(
    candidate: HistoryEntry, history: list[HistoryEntry]
) -> bool:
    """Review and optionally edit mode/layer/panel targeting fields."""
    accepted = confirm_continue_group(
        "Target",
        [
            f"mode: {candidate.mode}",
            f"layer: {candidate.layer}",
            f"panel: {candidate.panel_number} of {candidate.panels_planned}",
        ],
    )
    if accepted:
        return True

    original_layer = candidate.layer
    candidate.mode = prompt_choice("continue mode", CONTINUE_MODES, candidate.mode)
    candidate.layer = ask_layer(
        candidate.layer,
        "current answer",
        field_history(history, "layer"),
    )
    candidate.panels_planned = prompt_int(
        "How many panels do you plan to prompt for this specific layer?",
        candidate.panels_planned,
        minimum=1,
        previous=field_history(history, "panels_planned"),
        allow_back=False,
    )
    if candidate.panels_planned == 1:
        candidate.panel_number = 1
    else:
        candidate.panel_number = prompt_int(
            "panel_number",
            min(candidate.panel_number, candidate.panels_planned),
            minimum=1,
            maximum=candidate.panels_planned,
            previous=field_history(history, "panel_number"),
            allow_back=False,
        )
    if candidate.layer != original_layer:
        candidate.must_contain, candidate.nice_to_have = default_detail_values_for_layer(
            history, candidate.layer
        )
    return False


def review_continue_scene(
    candidate: HistoryEntry, history: list[HistoryEntry]
) -> bool:
    """Review and optionally edit world and mood for a continue run."""
    accepted = confirm_continue_group(
        "Scene",
        [f"world: {candidate.world}", f"mood: {candidate.mood}"],
    )
    if accepted:
        return True
    candidate.world = prompt_text_with_history(
        "world",
        candidate.world,
        field_history(history, "world"),
        "current answer",
        DEFAULT_SETTINGS["world"],
        allow_back=False,
    )
    candidate.mood = prompt_text_with_history(
        "mood",
        candidate.mood,
        field_history(history, "mood"),
        "current answer",
        DEFAULT_SETTINGS["mood"],
        allow_back=False,
    )
    return False


def review_continue_details(
    candidate: HistoryEntry,
    settings: OrderedDict[str, str],
    history: list[HistoryEntry],
) -> bool:
    """Review and optionally edit layer-specific must/nice details."""
    lines = [
        f"must: {candidate.must_contain or '(blank)'}",
        f"nice: {candidate.nice_to_have or '(blank)'}",
    ]
    if candidate.layer != "SKY":
        lines.append(f"matte/transparency: {settings['matte']}")
    accepted = confirm_continue_group("Layer Details", lines)
    if accepted:
        return True

    must_history = field_history_for_layer(history, "must_contain", candidate.layer)
    nice_history = field_history_for_layer(history, "nice_to_have", candidate.layer)
    if candidate.layer == "SKY":
        sky_must_history = sky_compatible_history(must_history)
        sky_nice_history = sky_compatible_history(nice_history)
        candidate.must_contain = prompt_text_with_history(
            "sky/atmosphere must-have details",
            candidate.must_contain,
            sky_must_history,
            "current answer",
            "leave blank if none",
            example_label="guidance",
            allow_back=False,
        )
        candidate.nice_to_have = prompt_text_with_history(
            "optional sky/atmosphere details",
            candidate.nice_to_have,
            sky_nice_history,
            "current answer",
            "leave blank if none",
            example_label="guidance",
            allow_back=False,
        )
    else:
        candidate.must_contain = prompt_text_with_history(
            "must_contain",
            candidate.must_contain,
            must_history,
            "current answer",
            DEFAULT_MUST_CONTAIN,
            allow_back=False,
        )
        candidate.nice_to_have = prompt_text_with_history(
            "nice_to_have",
            candidate.nice_to_have,
            nice_history,
            "current answer",
            DEFAULT_NICE_TO_HAVE,
            allow_back=False,
        )
        settings["matte"] = ask_matte_or_alpha(settings["matte"], allow_back=False)
    return False


def review_continue_settings(settings: OrderedDict[str, str]) -> tuple[bool, set[str]]:
    """Review and optionally edit output and silent settings for continue."""
    keys = (
        "output_folder",
        "art_style",
        "palette",
        "light",
        "canvas",
        "crop_target",
        "groundline_pct",
    )
    accepted = confirm_continue_group(
        "Output/Settings",
        [f"{key}: {settings[key]}" for key in keys],
    )
    if accepted:
        return True, set()

    before = {key: settings[key] for key in keys}
    settings["output_folder"] = prompt_text("output_folder", settings["output_folder"])
    settings["art_style"] = prompt_text("art_style", settings["art_style"])
    settings["palette"] = prompt_text("palette", settings["palette"])
    settings["light"] = prompt_text("light", settings["light"])
    settings["canvas"] = prompt_text("canvas", settings["canvas"])
    settings["crop_target"] = prompt_text("crop_target", settings["crop_target"])
    settings["groundline_pct"] = str(
        prompt_int(
            "groundline_pct",
            settings["groundline_pct"],
            minimum=0,
            maximum=100,
        )
    )
    changed = {
        key for key, before_value in before.items() if settings[key] != before_value
    }
    return False, changed


def format_review_metadata(review: dict[str, str]) -> str:
    """Serialize grouped continue review decisions for the history log."""
    return ",".join(f"{key}:{review[key]}" for key in ("target", "scene", "details", "settings"))


def ask_continue_run(
    settings: OrderedDict[str, str], history: list[HistoryEntry]
) -> HistoryEntry | None:
    """Run the continue workflow and return the proposed HistoryEntry."""
    selected_session = choose_continue_session(settings, history)
    if selected_session is None:
        return None

    candidate = build_continue_candidate(settings, history, selected_session)
    if candidate.reprint_only:
        return candidate
    review: dict[str, str] = {}
    overrides: set[str] = set()

    before = {
        "mode": candidate.mode,
        "layer": candidate.layer,
        "panels_planned": candidate.panels_planned,
        "panel_number": candidate.panel_number,
    }
    accepted = review_continue_target(candidate, history)
    review["target"] = "y" if accepted else "n"
    mark_changed(
        overrides,
        before,
        {
            "mode": candidate.mode,
            "layer": candidate.layer,
            "panels_planned": candidate.panels_planned,
            "panel_number": candidate.panel_number,
        },
    )

    before = {"world": candidate.world, "mood": candidate.mood}
    accepted = review_continue_scene(candidate, history)
    review["scene"] = "y" if accepted else "n"
    mark_changed(overrides, before, {"world": candidate.world, "mood": candidate.mood})

    before = {
        "must": candidate.must_contain,
        "nice": candidate.nice_to_have,
        "matte": settings["matte"],
    }
    accepted = review_continue_details(candidate, settings, history)
    review["details"] = "y" if accepted else "n"
    mark_changed(
        overrides,
        before,
        {
            "must": candidate.must_contain,
            "nice": candidate.nice_to_have,
            "matte": settings["matte"],
        },
    )

    accepted, setting_changes = review_continue_settings(settings)
    review["settings"] = "y" if accepted else "n"
    overrides.update(setting_changes)

    candidate.review = format_review_metadata(review)
    candidate.overrides = "none" if not overrides else ",".join(sorted(overrides))
    return candidate


def ask_mode(
    settings: OrderedDict[str, str],
    history: list[HistoryEntry],
    pillow_status: PillowStatus | None,
) -> tuple[str, HistoryEntry | None, HistoryEntry | None]:
    """Ask for the run mode or handle the single-letter control commands.

    Returns the chosen mode, an optional template run, and an optional complete
    continue-run candidate. Commands: c continues a session, r (or last)
    reprints the previous prompt, s edits settings, h opens history choices.
    """
    template: HistoryEntry | None = None
    print_mode_menu()
    while True:
        answer = prompt_block(
            "mode",
            [("default", settings["mode"])],
            format_prompt_controls(
                history=True,
                typed="type number/mode",
                allow_back=False,
                extra=(f"{CONTINUE_MODE_NUMBER}/c=continue", "r=last", "s=settings"),
            ),
        ).lower()
        if handle_navigation_command(answer, allow_back=False):
            continue
        value = settings["mode"] if answer == "" else answer
        if value in {"r", "last"}:
            if not history:
                print("No previous run is available in the log.")
                continue
            output = build_output_safely(
                settings=settings,
                run=history[-1],
                history=history,
                pillow_status=pillow_status,
            )
            if output is None:
                raise SystemExit(1)
            emit_output(settings, output)
            raise SystemExit(0)
        if value in {"s", "settings"}:
            new_settings, _changed = walk_settings(settings, history)
            settings.clear()
            settings.update(new_settings)
            write_log(settings, history)
            continue
        if value in {CONTINUE_MODE_NUMBER, "c", "continue", "resume"}:
            continue_run = ask_continue_run(settings, history)
            if continue_run is not None:
                return continue_run.mode, None, continue_run
            continue
        if value in {"h", "history"}:
            picked_mode, picked_run = pick_mode_history(history)
            if picked_mode is not None:
                template = picked_run
                settings["mode"] = picked_mode
                return picked_mode, template, None
            continue
        mode = resolve_mode(value)
        if mode is not None:
            settings["mode"] = mode
            return mode, template, None
        print(f"Choose 1/new panel, {CONTINUE_MODE_NUMBER}/continue, or r / s / h.")


def ask_layer(
    default: str, default_label: str = "default", previous: list[str] | None = None
) -> str:
    """Ask which parallax layer recipe to use for this run.

    The user can choose by number or by name. The caller supplies the default
    (the reused template's layer, the last run's layer, or MIDDLE GROUND) and
    a label that explains where that default came from. Previous layer values
    from RUN HISTORY are available through h when provided.
    """
    previous = previous or []
    print("Layer Recipes:")
    for index, name in enumerate(LAYER_RECIPES, start=1):
        print(f"  {index}. {name}")
    while True:
        answer = prompt_block(
            "layer 1-5",
            [(default_label, default)],
            format_prompt_controls(history=True, typed="type number/name"),
        )
        if answer == "":
            return default
        if handle_navigation_command(answer, allow_back=True):
            continue
        if answer.lower() == "h":
            selected = pick_previous_value(previous, width=30)
            if selected is not None:
                return selected
            continue
        if is_int(answer):
            index = int(answer)
            if 1 <= index <= len(LAYER_RECIPES):
                return list(LAYER_RECIPES.keys())[index - 1]
        value = answer.upper()
        if value in LAYER_RECIPES:
            return value
        print("Choose a layer number from 1 to 5, or type a layer name.")


def ask_run(
    settings: OrderedDict[str, str],
    history: list[HistoryEntry],
    pillow_status: PillowStatus | None = None,
) -> HistoryEntry:
    """Run the nine-step interview with GoBack rewind and SKY's hidden-matte step skipped.

    Defaults come from the reused template run when the user picked one with
    the h command, otherwise from the previous run and the current settings.
    The final world and mood answers are written back to settings so they stay
    the project-level defaults for the next run.
    """
    world_history = field_history(history, "world")
    mood_history = field_history(history, "mood")
    panels_history = field_history(history, "panels_planned")
    panel_number_history = field_history(history, "panel_number")
    answers: dict[str, object] = {}
    template: HistoryEntry | None = None
    last: HistoryEntry | None = history[-1] if history else None
    step = 0
    step_count = 9

    while step < step_count:
        try:
            if step == 0:
                mode, template, continue_run = ask_mode(settings, history, pillow_status)
                if continue_run is not None:
                    return continue_run
                answers["mode"] = mode
                last = template or (history[-1] if history else None)
                for key in (
                    "layer",
                    "world",
                    "mood",
                    "must_contain",
                    "nice_to_have",
                    "panels_planned",
                    "panel_number",
                    "matte",
                ):
                    answers.pop(key, None)
            elif step == 1:
                if "layer" in answers:
                    layer_default = str(answers["layer"])
                    layer_label = "current answer"
                else:
                    layer_default = last.layer if last else DEFAULT_LAYER
                    layer_label = "previous" if last else "default"
                previous_layer_answer = answers.get("layer")
                selected_layer = ask_layer(
                    layer_default,
                    layer_label,
                    field_history(history, "layer"),
                )
                if previous_layer_answer is not None and selected_layer != previous_layer_answer:
                    for key in ("matte", "must_contain", "nice_to_have"):
                        answers.pop(key, None)
                answers["layer"] = selected_layer
            elif step == 2:
                if str(answers["layer"]) == "SKY":
                    answers.pop("matte", None)
                else:
                    matte_default = str(answers.get("matte", settings["matte"]))
                    answers["matte"] = ask_matte_or_alpha(matte_default, allow_back=True)
            elif step == 3:
                if "world" in answers:
                    world_default = str(answers["world"])
                    world_label = "current answer"
                else:
                    world_default = template.world if template else settings["world"]
                    world_label = (
                        "previous"
                        if template
                        else default_source_label(
                            world_default, world_history, "current setting"
                        )
                    )
                answers["world"] = prompt_text_with_history(
                    "world",
                    world_default,
                    world_history,
                    world_label,
                    DEFAULT_SETTINGS["world"],
                )
            elif step == 4:
                if "mood" in answers:
                    mood_default = str(answers["mood"])
                    mood_label = "current answer"
                else:
                    mood_default = template.mood if template else settings["mood"]
                    mood_label = (
                        "previous"
                        if template
                        else default_source_label(
                            mood_default, mood_history, "current setting"
                        )
                    )
                answers["mood"] = prompt_text_with_history(
                    "mood",
                    mood_default,
                    mood_history,
                    mood_label,
                    DEFAULT_SETTINGS["mood"],
                )
            elif step == 5:
                selected_layer = str(answers["layer"])
                must_history = field_history_for_layer(
                    history, "must_contain", selected_layer
                )
                if selected_layer == "SKY":
                    sky_must_history = sky_compatible_history(must_history)
                    must_default, must_label = sky_detail_default(
                        answers.get("must_contain"),
                        template.must_contain
                        if template and template.layer == selected_layer
                        else None,
                        sky_must_history,
                    )
                    answers["must_contain"] = prompt_text_with_history(
                        "sky/atmosphere must-have details",
                        must_default,
                        sky_must_history,
                        must_label,
                        "leave blank if none",
                        example_label="guidance",
                    )
                else:
                    must_default, must_label = layer_detail_default(
                        answers.get("must_contain"),
                        template,
                        selected_layer,
                        "must_contain",
                        must_history,
                        DEFAULT_MUST_CONTAIN,
                    )
                    answers["must_contain"] = prompt_text_with_history(
                        "must_contain",
                        must_default,
                        must_history,
                        must_label,
                        DEFAULT_MUST_CONTAIN,
                    )
            elif step == 6:
                selected_layer = str(answers["layer"])
                nice_history = field_history_for_layer(
                    history, "nice_to_have", selected_layer
                )
                if selected_layer == "SKY":
                    sky_nice_history = sky_compatible_history(nice_history)
                    nice_default, nice_label = sky_detail_default(
                        answers.get("nice_to_have"),
                        template.nice_to_have
                        if template and template.layer == selected_layer
                        else None,
                        sky_nice_history,
                    )
                    answers["nice_to_have"] = prompt_text_with_history(
                        "optional sky/atmosphere details",
                        nice_default,
                        sky_nice_history,
                        nice_label,
                        "leave blank if none",
                        example_label="guidance",
                    )
                else:
                    nice_default, nice_label = layer_detail_default(
                        answers.get("nice_to_have"),
                        template,
                        selected_layer,
                        "nice_to_have",
                        nice_history,
                        DEFAULT_NICE_TO_HAVE,
                    )
                    answers["nice_to_have"] = prompt_text_with_history(
                        "nice_to_have",
                        nice_default,
                        nice_history,
                        nice_label,
                        DEFAULT_NICE_TO_HAVE,
                    )
            elif step == 7:
                if "panels_planned" in answers:
                    panels_default: int | str = int(answers["panels_planned"])
                    panels_label = "current answer"
                else:
                    panels_default = (
                        template.panels_planned if template else DEFAULT_PANELS_PLANNED
                    )
                    panels_label = "previous" if template else None
                answers["panels_planned"] = prompt_int(
                    "How many panels do you plan to prompt for this specific layer?",
                    panels_default,
                    minimum=1,
                    default_label=panels_label,
                    previous=panels_history,
                    allow_back=True,
                )
                if "panel_number" in answers and int(answers["panel_number"]) > int(answers["panels_planned"]):
                    answers.pop("panel_number", None)
            elif step == 8:
                panels_planned_value = int(answers["panels_planned"])
                if panels_planned_value == 1:
                    answers["panel_number"] = 1
                else:
                    if "panel_number" in answers:
                        panel_default: int | str = int(answers["panel_number"])
                        panel_label = "current answer"
                    elif template and template.panel_number:
                        panel_default = min(template.panel_number, panels_planned_value)
                        panel_label = "previous"
                    elif last and last.panel_number:
                        panel_default = min(last.panel_number, panels_planned_value)
                        panel_label = "previous"
                    else:
                        panel_default = min(DEFAULT_PANEL_NUMBER, panels_planned_value)
                        panel_label = None
                    answers["panel_number"] = prompt_int(
                        "panel_number",
                        panel_default,
                        minimum=1,
                        maximum=panels_planned_value,
                        default_label=panel_label,
                        previous=panel_number_history,
                        allow_back=True,
                    )
        except GoBack:
            if step == 0:
                print("Already at the first question; there is nowhere to go back.")
            elif step == 3 and str(answers.get("layer")) == "SKY":
                # SKY never enters matte step 2, so rewind directly to its preceding layer step.
                step = 1
            else:
                step -= 1
            continue
        step += 1

    mode = str(answers["mode"])
    layer = str(answers["layer"])
    world = str(answers["world"])
    mood = str(answers["mood"])
    must_contain = str(answers["must_contain"])
    nice_to_have = str(answers["nice_to_have"])
    panels_planned = int(answers["panels_planned"])
    panel_number = int(answers["panel_number"])
    settings["world"] = world
    settings["mood"] = mood
    if layer != "SKY" and "matte" in answers:
        settings["matte"] = str(answers["matte"])

    print(
        "using: "
        f"{settings['art_style']} | {settings['palette']} | matte {settings['matte']} | "
        f"canvas {settings['canvas']}→{settings['crop_target']} | "
        f"light {settings['light']} | groundline {settings['groundline_pct']}%"
    )
    print(
        f"output: {settings['output_folder']} | clipboard {settings['copy_to_clipboard']} | "
        f"backup {settings['backup_mode']} → {settings['backup_folder']}"
    )

    return HistoryEntry(
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
        mode=mode,
        layer=layer,
        world=world,
        mood=mood,
        must_contain=must_contain,
        nice_to_have=nice_to_have,
        panels_planned=panels_planned,
        panel_number=panel_number,
    )


def matte_prompt_instruction(matte: str) -> str:
    """Describe the selected matte or transparency requirement for the prompt."""
    if is_transparency_matte(matte):
        return (
            "Request true PNG transparency outside the artwork using a real alpha "
            "channel. Do not fake transparency with checkerboard, gray squares, "
            "painted emptiness, or a background color. If the image tool cannot "
            "produce true transparency, say so in one sentence and use the closest "
            "supported option."
        )
    return (
        "Every area outside the artwork is one flat, even, edge-to-edge field of "
        f"{matte}. This is a clean keying matte, like a sticker sheet. The matte "
        "stays perfectly empty: no specks, no texture, no gradient, and no shadows "
        "cast onto it."
    )


def layer_brief_lines(settings: OrderedDict[str, str], run: HistoryEntry) -> str:
    """Build the BRIEF-only lines that differ by parallax layer."""
    if run.layer == "SKY":
        lines = [
            "- Layer content: sky and atmosphere only. Do not add specific sky "
            "objects unless they are explicitly listed below as user-requested "
            "sky/atmosphere details.",
            "- Background fill: sky/atmosphere fills the whole canvas edge to edge.",
        ]
        if is_sky_compatible_detail(run.must_contain):
            lines.append(
                f"- User-requested sky/atmosphere must-have details: {run.must_contain}"
            )
        if is_sky_compatible_detail(run.nice_to_have):
            lines.append(
                f"- User-requested sky/atmosphere nice-to-have details: {run.nice_to_have}"
            )
        return "\n".join(lines)
    return "\n".join(
        (
            f"- Must contain: {run.must_contain}",
            f"- Nice to have: {run.nice_to_have}",
            f"- Matte or transparency: {matte_prompt_instruction(settings['matte'])}",
        )
    )


def composition_block(settings: OrderedDict[str, str], run: HistoryEntry) -> str:
    """Build the COMPOSITION section for exactly one layer type."""
    if run.layer == "SKY":
        return "\n".join(
            (
                "- Straight-on side-scroller sky/atmosphere layer only. The sky "
                "fills the entire canvas with no groundline, no terrain band, no "
                "horizon line, no vanishing point, no camera tilt, and no perspective.",
                "- Keep any user-requested sky/atmosphere details fully inside the "
                "frame and out of the top and bottom crop-protection zones.",
                "- Scale, value, and texture stay consistent from left to right; no "
                "object grows or shrinks with perspective.",
            )
        )
    if run.layer == "FAR BACKGROUND":
        return "\n".join(
            (
                "- Straight-on side-scroller far-background layer only: pale distant "
                "mountain and faint treeline silhouettes as one horizontal scenery "
                f"band around {settings['groundline_pct']}% of frame height.",
                "- No painted sky behind it; the selected matte or true transparency "
                "fills everything above and below the distant band.",
                "- Keep detail low, hazy, and distant. No near objects, no foreground "
                "plants, no main terrain, and no camera perspective.",
            )
        )
    if run.layer == "BACKGROUND":
        return "\n".join(
            (
                "- Straight-on side-scroller background layer only: mid-distance "
                "forested hills, cliff faces, and far ruins as one horizontal "
                f"scenery band around {settings['groundline_pct']}% of frame height.",
                "- No painted sky behind it; the selected matte or true transparency "
                "fills everything above and below the scenery band.",
                "- Moderate detail only. No near-ground detail, no foreground strip, "
                "no main playable terrain, and no perspective.",
            )
        )
    if run.layer == "FOREGROUND":
        return "\n".join(
            (
                "- Straight-on side-scroller foreground strip only: nearest decoration "
                "along the lower part of the frame, with oversized grass tufts, "
                "leaves, vines, and rocks where appropriate.",
                "- The strip may bleed off the bottom edge. The selected matte or true "
                "transparency fills empty space above and around the strip.",
                "- No sky, no horizon, no distant mountains, no background scenery, "
                "and no playable main terrain band.",
            )
        )
    return "\n".join(
        (
            "- Straight-on side view, like a classic 2D platformer background: the "
            f"main terrain reads as a level horizontal band at about {settings['groundline_pct']}% "
            "of frame height. Zero perspective, zero vanishing point, zero camera "
            "tilt, zero diagonal horizon.",
            "- One consistent scale: a tree at the right edge is the same size as a "
            "tree at the left edge.",
            "- The terrain band and its silhouette run unbroken from the left edge "
            "to the right edge, so this panel can sit beside a neighboring panel.",
            "- Every important object sits fully inside the frame; only the continuing "
            "terrain band touches the left and right edges.",
            "- No sky, no horizon, and no distant mountains behind the artwork.",
        )
    )


def exclude_extra_for_layer(run: HistoryEntry) -> str:
    """Return layer-specific hard exclusions appended to the common list."""
    if run.layer == "SKY":
        return (
            ", ground, terrain, structures, ruins, sci-fi tech, foliage, trees, "
            "plants, flowers, moss, water"
        )
    if run.layer in {"FAR BACKGROUND", "BACKGROUND"}:
        return ", painted sky, foreground objects, near-ground detail, UI-like alpha checkerboards"
    if run.layer == "FOREGROUND":
        return ", sky, horizon, distant mountains, background scenery"
    return ", sky, horizon, distant mountains, UI-like alpha checkerboards"


def final_check_for_layer(run: HistoryEntry) -> str:
    """Return the layer-specific final self-check sentence."""
    if run.layer == "SKY":
        return (
            "only sky content appears; the sky fills the whole canvas; one light "
            "direction; no groundline, terrain, structures, foliage, tech, water, "
            "or matte; nothing from EXCLUDE."
        )
    if run.layer in {"FAR BACKGROUND", "BACKGROUND"}:
        return (
            "every required item that belongs in this distance layer is present; "
            "no sky appears; selected matte or true transparency is clean; one "
            "light direction; level horizontal scenery band; nothing from EXCLUDE."
        )
    if run.layer == "FOREGROUND":
        return (
            "every required item that belongs in this foreground strip is present; "
            "no sky or distant background appears; selected matte or true transparency "
            "is clean; one light direction; nothing from EXCLUDE."
        )
    return (
        "every Must contain item is present; only this layer's content appears; "
        "selected matte or true transparency is clean; one light direction; level "
        "groundline; nothing from EXCLUDE."
    )


def composition_extra_for_mode(active_mode: str, layer: str, panels_planned: int) -> str:
    """Return any automatic tile/continuation rule for a mode and layer pair."""
    if active_mode == "tileable panel" or panels_planned > 1:
        return TILEABLE_BY_LAYER[layer] + "\n"
    return ""


def intro_note_for_layer(layer: str) -> str:
    """Return the prompt intro note that matches the selected layer.

    SKY fills the whole canvas and has no matte or alpha requirement, so it
    avoids transparency fallback language that only matters to cut-out layers.
    """
    if layer == "SKY":
        return "Generate exactly one image from the requirements below."
    return (
        "Generate exactly one image from the requirements below. If your image tool\n"
        "cannot honor true transparency or the requested canvas ratio, say so in one\n"
        "sentence and use the closest supported option. Never fake transparency with a\n"
        "checkerboard pattern, gray squares, or a painted placeholder background."
    )


def build_prompt(
    settings: OrderedDict[str, str],
    run: HistoryEntry,
    mode: str | None = None,
    history: list[HistoryEntry] | None = None,
) -> str:
    """Build the self-contained image prompt for one mode and history entry.

    The optional mode parameter lets follow-up prompts reuse the same run data
    while changing how the prompt is framed.
    """
    active_mode = mode or run.mode

    full_prompt = PROMPT_TEMPLATE.format(
        intro_note=intro_note_for_layer(run.layer),
        layer_recipe=LAYER_RECIPES[run.layer],
        world=run.world,
        mood=run.mood,
        art_style=settings["art_style"],
        palette=settings["palette"],
        light=settings["light"],
        layer_brief=layer_brief_lines(settings, run),
        canvas=settings["canvas"],
        crop_target=settings["crop_target"],
        crop_protection_pct=CROP_PROTECTION_PCT,
        composition_block=composition_block(settings, run),
        composition_extra=composition_extra_for_mode(
            active_mode, run.layer, run.panels_planned
        ),
        exclude_extra=exclude_extra_for_layer(run),
        final_check=final_check_for_layer(run),
    )

    if active_mode in {"extend right", "extend left"}:
        direction = "RIGHT" if active_mode == "extend right" else "LEFT"
        references = reference_panels_section(settings, history or [], run)
        return (
            f"{full_prompt}\n\n"
            f"{references}\n\n"
            f"{build_extend_snippet(settings, run, direction)}"
        )

    if active_mode == "new layer, same world":
        return f"{NEW_LAYER_SNIPPET}\n\n{full_prompt}"

    return full_prompt


def build_extend_snippet(
    settings: OrderedDict[str, str], run: HistoryEntry, direction: str
) -> str:
    """Build the seam-continuation text for extending left or right.

    The direction decides which edge of the new panel should match which edge
    of the already accepted panel. Layer wording avoids applying terrain rules
    to sky or background-only layers.
    """
    if direction == "RIGHT":
        new_edge = "LEFT"
        attached_edge = "RIGHT"
    else:
        new_edge = "RIGHT"
        attached_edge = "LEFT"

    if run.layer == "SKY":
        layer_continuity = (
            "continue the sky/atmosphere color, value, texture, and any "
            "user-requested sky detail across the seam; do not add ground, "
            "terrain, structures, foliage, water, or matte"
        )
    elif run.layer in {"FAR BACKGROUND", "BACKGROUND"}:
        layer_continuity = (
            "continue the horizontal scenery band at the same vertical placement "
            "and keep the selected matte or true transparency clean above and below it"
        )
    elif run.layer == "FOREGROUND":
        layer_continuity = (
            "continue the foreground strip at the same lower-frame placement and "
            "keep the selected matte or true transparency clean around it"
        )
    else:
        layer_continuity = (
            f"continue the terrain band at about {settings['groundline_pct']}% of frame "
            "height and keep the selected matte or true transparency clean around it"
        )

    return (
        f"Use the reference panel paths above. Create the NEXT panel to the {direction} as a\n"
        "brand-new composition - do not copy, mirror, or re-render any reference image.\n"
        "Reuse the full BRIEF above exactly: same world, style, palette, light,\n"
        "canvas, crop protection, object scale, and layer-specific rules. At the\n"
        f"new panel's {new_edge} edge, continue what the immediate seam reference's {attached_edge}\n"
        f"edge shows: {layer_continuity}. Everything else in the panel is new artwork."
    )


def build_output(
    settings: OrderedDict[str, str],
    run: HistoryEntry,
    history: list[HistoryEntry] | None = None,
    pillow_status: PillowStatus | None = None,
) -> str:
    """Assemble the full terminal output for one run.

    The output includes the prompt markers, workflow instructions, and save
    footer. Later panel prompts are generated after accepted image files exist.
    """
    prompt = build_prompt(settings, run, history=history)
    verify_prompt(prompt)
    workflow = build_workflow_instructions(settings, run, pillow_status=pillow_status)
    verify_no_brackets(workflow)
    _panel_relative_path, panel_absolute_path = output_image_paths(settings, run)

    lines = [
        OPENING_MARKER,
        prompt,
        "",
        workflow,
        CLOSING_MARKER,
        f"save accepted output to {panel_absolute_path}",
    ]

    if run.panels_planned > run.panel_number:
        lines.extend(
            (
                "",
                (
                    "After accepting and saving this panel, rerun this script and "
                    "choose continue so the next prompt can include verified "
                    "reference panel paths."
                ),
            )
        )

    return "\n".join(lines)


def build_output_safely(
    settings: OrderedDict[str, str],
    run: HistoryEntry,
    history: list[HistoryEntry] | None = None,
    pillow_status: PillowStatus | None = None,
) -> str | None:
    """Build output and convert prompt verification failures into None.

    This lets the main flow fail cleanly without appending a bad run to history.
    """
    try:
        return build_output(settings, run, history, pillow_status)
    except RuntimeError as exc:
        print(f"Prompt failed verification and was not emitted: {exc}")
        return None


def build_follow_up_prompt(
    settings: OrderedDict[str, str],
    run: HistoryEntry,
    history: list[HistoryEntry] | None = None,
) -> str:
    """Build the next extension prompt when reference image files already exist.

    Normal output no longer prints speculative follow-ups because references
    must point to verified accepted images.
    """
    follow_up_run = replace(
        run,
        mode="extend right",
        panel_number=min(run.panel_number + 1, run.panels_planned),
    )
    return build_prompt(settings, follow_up_run, history=history)


def filename_stamp(timestamp: str) -> str:
    """Convert a history timestamp into a filename-safe stamp.

    "2026-06-12 14:10" becomes "20260612-1410". Deriving the stamp from the
    stored run timestamp keeps workflow filenames reproducible when `last`
    rebuilds a previous prompt.
    """
    return timestamp.replace("-", "").replace(":", "").replace(" ", "-")


def filename_date(timestamp: str) -> str:
    """Convert a history timestamp into a compact YYYYMMDD filename date."""
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", timestamp.strip())
    if match:
        return "".join(match.groups())
    digits = re.sub(r"\D+", "", timestamp)
    if len(digits) >= 8:
        return digits[:8]
    if timestamp not in WARNED_BAD_FILENAME_TIMESTAMPS:
        WARNED_BAD_FILENAME_TIMESTAMPS.add(timestamp)
        print(f"Warning: malformed history timestamp {timestamp!r}; using 00000000 in filenames.", file=sys.stderr)
    return "00000000"


WARNED_BAD_FILENAME_TIMESTAMPS: set[str] = set()


FILENAME_STOPWORDS = {
    "a",
    "an",
    "and",
    "at",
    "for",
    "from",
    "in",
    "into",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}


def first_slug_word(value: str, skip_stopwords: bool = False, fallback: str = "asset") -> str:
    """Return a lowercase filename-safe first word from a descriptive value."""
    for match in re.finditer(r"[A-Za-z0-9]+", value):
        word = match.group(0).lower()
        if skip_stopwords and word in FILENAME_STOPWORDS:
            continue
        return word
    return fallback


def layer_slug(layer: str) -> str:
    """Return the filename-safe layer tag for a parallax layer name."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", layer.strip().lower()).strip("_")
    return slug or "layer"


def artifact_hash(run: HistoryEntry) -> str:
    """Return the root continue-session hash used to group artifact filenames."""
    explicit_hash = run.root_id or run.session_id
    if explicit_hash:
        cleaned = re.sub(r"[^A-Za-z0-9]+", "", explicit_hash).lower()
        if cleaned:
            return cleaned
    identity = "|".join(
        (
            run.timestamp,
            run.mode,
            run.layer,
            run.world,
            run.mood,
            run.must_contain,
            run.nice_to_have,
            str(run.panels_planned),
            str(run.panel_number),
        )
    )
    return hashlib.sha1(identity.encode("utf-8")).hexdigest()[:8]


def asset_filename(run: HistoryEntry, panel_number: int | None = None) -> str:
    """Build the descriptive production image filename for a run."""
    number = panel_number if panel_number is not None else run.panel_number
    world_word = first_slug_word(run.world, skip_stopwords=True, fallback="world")
    mood_word = first_slug_word(run.mood, fallback="mood")
    return (
        f"{artifact_hash(run)}-{world_word}-{mood_word}_{layer_slug(run.layer)}_"
        f"{panel_tag(run, number)}_"
        f"{filename_date(run.timestamp)}.png"
    )


def panel_tag(run: HistoryEntry, panel_number: int | None = None) -> str:
    """Return the panel position tag shared by all run artifact filenames."""
    number = panel_number if panel_number is not None else run.panel_number
    return f"panel{number:02d}-of-{run.panels_planned:02d}"


def prompt_artifact_filename(
    run: HistoryEntry, source: str, label: str = "", results: bool = False
) -> str:
    """Build a prompt or result-log filename tagged by layer and source."""
    suffix = f"-{label}" if label else ""
    result_suffix = "-results" if results else ""
    return (
        f"{artifact_hash(run)}-{filename_date(run.timestamp)}-{SCRIPT_STEM}_"
        f"{layer_slug(run.layer)}_{panel_tag(run)}_{source}{suffix}{result_suffix}.md"
    )


def absolute_artifact_path(folder: str, filename: str) -> Path:
    """Resolve an artifact filename to an absolute path without hard-coded roots."""
    return (absolute_artifact_folder(folder) / filename).resolve()


def absolute_artifact_folder(folder: str) -> Path:
    """Resolve a configured artifact folder to an absolute local path."""
    folder_path = Path(folder)
    if folder_path.is_absolute():
        return folder_path.resolve()
    return (REPO_ROOT / folder_path).resolve()


def artifact_paths(folder: str, filename: str) -> tuple[str, str]:
    """Return repo-relative and absolute paths for one generated artifact."""
    relative = join_repo_relative_path(folder, filename)
    absolute = str(absolute_artifact_path(folder, filename))
    return relative, absolute


def non_overwriting_path(path: Path, content: str) -> tuple[Path, bool, str]:
    """Choose a Markdown path without replacing existing different content.

    Returns the selected path, whether the caller should write content there,
    and a short status label: new, identical, versioned, or versioned-identical. Version numbers use
    at least two digits and may reach 999; exhaustion raises RuntimeError.
    """
    if not path.exists():
        return path, True, "new"
    try:
        if path.read_text(encoding="utf-8") == content:
            return path, False, "identical"
    except OSError:
        pass

    for version in range(2, 1000):
        candidate = path.with_name(f"{path.stem}-v{version:02d}{path.suffix}")
        if not candidate.exists():
            return candidate, True, "versioned"
        try:
            if candidate.read_text(encoding="utf-8") == content:
                return candidate, False, "versioned-identical"
        except OSError:
            continue

    raise RuntimeError(f"could not find a non-overwriting filename for {path}")


def save_text_without_overwrite(path: Path, content: str, label: str) -> Path:
    """Save non-empty text without overwriting different content, then verify a non-empty file."""
    target, should_write, status = non_overwriting_path(path, content)
    if should_write:
        target.write_text(content, encoding="utf-8")
        print(f"Saved {label}: {target}")
    else:
        print(f"{label.capitalize()} already exists with the same content: {target}")
    if not target.exists() or target.stat().st_size == 0:
        raise OSError(f"{label} save could not be verified")
    if status == "versioned":
        print(f"Original {label} path already existed; wrote versioned file: {target}")
    return target


def ensure_artifact_folder_if_requested(folder: str, label: str) -> bool:
    """Create a configured artifact folder only after explicit user approval."""
    folder_path = absolute_artifact_folder(folder)
    if folder_path.exists():
        return True
    try:
        if ask_yes_no(f"{label} folder does not exist: {folder_path}. Create it?", "n"):
            folder_path.mkdir(parents=True, exist_ok=True)
            print(f"Created {label} folder: {folder_path}")
            return True
    except (EOFError, UserQuit):
        pass
    print(f"{label} folder not created; related file save was skipped.")
    return False


def output_image_paths(
    settings: OrderedDict[str, str], run: HistoryEntry, panel_number: int | None = None
) -> tuple[str, str]:
    """Return repo-relative and absolute image paths for workflow text."""
    return artifact_paths(
        settings["output_folder"],
        next_available_asset_filename(settings, run, panel_number),
    )


def next_available_asset_filename(
    settings: OrderedDict[str, str], run: HistoryEntry, panel_number: int | None = None
) -> str:
    """Return a free canonical or minimum-two-digit ``-vNN`` family name, raising after version 999."""
    canonical = asset_filename(run, panel_number)
    stem = Path(canonical).stem
    folder = absolute_artifact_folder(settings["output_folder"])
    existing_versions: set[int] = set()

    try:
        if not (folder / canonical).exists():
            return canonical
    except OSError:
        pass

    for path in image_family_paths(
        settings, run, panel_number=panel_number, include_legacy=False
    ):
        version = asset_family_version(path.name, stem)
        if version is None:
            continue
        try:
            if path.exists():
                existing_versions.add(version)
        except OSError:
            continue

    if not existing_versions:
        return canonical

    for version in range(2, 1000):
        if version in existing_versions:
            continue
        candidate = f"{stem}-v{version:02d}.png"
        try:
            if not (folder / candidate).exists():
                return candidate
        except OSError:
            return candidate

    raise RuntimeError(f"could not find a non-overwriting image filename for {canonical}")


def script_prompt_archive_paths(
    settings: OrderedDict[str, str], run: HistoryEntry
) -> tuple[str, str]:
    """Return the repo-relative and absolute paths for the script prompt archive."""
    return artifact_paths(
        settings["prompts_given_folder"],
        prompt_artifact_filename(run, "script"),
    )


def build_workflow_instructions(
    settings: OrderedDict[str, str],
    run: HistoryEntry,
    panel_number: int | None = None,
    label: str = "",
    pillow_status: PillowStatus | None = None,
) -> str:
    """Build instructions for the AI coding agent that receives the prompt.

    Every write target is rendered as a resolved absolute local path so the
    receiving agent can save/copy artifacts without guessing where the repo is.
    Prompt archives and result logs follow the <timestamp>-<script filename>.md
    naming convention.
    """
    _panel_save_path, panel_absolute_path = output_image_paths(settings, run, panel_number)
    _prompt_archive_path, prompt_archive_absolute_path = artifact_paths(
        settings["prompts_given_folder"],
        prompt_artifact_filename(run, "LLM", label=label),
    )
    _results_log_path, results_log_absolute_path = artifact_paths(
        settings["results_log_folder"],
        prompt_artifact_filename(run, "LLM", label=label, results=True),
    )
    return WORKFLOW_TEMPLATE.format(
        panel_absolute_path=panel_absolute_path,
        prompt_archive_absolute_path=prompt_archive_absolute_path,
        results_log_absolute_path=results_log_absolute_path,
        pillow_workflow_line=pillow_workflow_line(pillow_status),
    )


def join_repo_relative_path(folder: str, filename: str) -> str:
    """Join a configured folder and filename as a repo-relative path string."""
    base = to_repo_relative_path(folder)
    parts = [part for part in base.replace("\\", "/").split("/") if part and part != "."]
    if not parts:
        parts = ["."]
    return "/".join(parts + [filename])


def to_repo_relative_path(value: str) -> str:
    """Return a repo-relative path when possible, preserving cross-drive absolute paths."""
    raw = value.strip()
    if not raw:
        return "."

    path = Path(raw)
    if path.is_absolute():
        try:
            relative = path.resolve().relative_to(REPO_ROOT)
        except ValueError:
            try:
                relative = Path(os.path.relpath(path, REPO_ROOT))
            except ValueError:
                return path.as_posix()
    else:
        relative = path

    return relative.as_posix()


def verify_no_brackets(text: str) -> None:
    """Raise an error if generated text still has square-bracket placeholders."""
    if "[" in text or "]" in text:
        raise RuntimeError("generated text still contains bracket placeholders")


REQUIRED_PROMPT_SECTIONS = (
    "\nBRIEF\n",
    "\nCOMPOSITION\n",
    "\nEXCLUDE (hard):",
    "Before rendering, confirm to yourself:",
)


def verify_prompt(prompt: str) -> None:
    """Check that a generated prompt has the required sections and one recipe.

    The function raises RuntimeError when a prompt is incomplete or ambiguous.
    """
    verify_no_brackets(prompt)
    for section in REQUIRED_PROMPT_SECTIONS:
        if section not in prompt:
            raise RuntimeError(
                f"generated prompt is missing the {section.strip()} section"
            )
    recipe_count = sum(prompt.count(recipe) for recipe in LAYER_RECIPES.values())
    if recipe_count != 1:
        raise RuntimeError(
            f"generated prompt contains {recipe_count} Layer Recipes; expected exactly 1"
        )


@cache
def _clipboard_apis() -> tuple[object, object]:
    """Configure and cache the Windows clipboard API handles."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HANDLE]
    kernel32.GlobalFree.restype = wintypes.HANDLE
    return user32, kernel32


def copy_to_clipboard(text: str) -> tuple[bool, str]:
    """Copy text to the Windows clipboard using only the standard library.

    Returns a success flag and error text. SetClipboardData success transfers HGLOBAL ownership to
    Windows; every earlier failure frees the handle locally.
    """
    if os.name != "nt":
        return False, "clipboard copy is only implemented for Windows"

    user32, kernel32 = _clipboard_apis()

    cf_unicode_text = 13
    gmem_moveable = 0x0002
    data = (text + "\0").encode("utf-16-le")

    handle = kernel32.GlobalAlloc(gmem_moveable, len(data))
    if not handle:
        return False, "GlobalAlloc failed"

    locked = kernel32.GlobalLock(handle)
    if not locked:
        kernel32.GlobalFree(handle)
        return False, "GlobalLock failed"

    ctypes.memmove(locked, data, len(data))
    kernel32.GlobalUnlock(handle)

    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(handle)
        return False, "OpenClipboard failed"

    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(cf_unicode_text, handle):
            kernel32.GlobalFree(handle)
            return False, "SetClipboardData failed"
    finally:
        user32.CloseClipboard()

    return True, ""


def clipboard_payload(output: str) -> str:
    """Extract just the paste-ready prompt section from the terminal output."""
    # Only the content between the markers is the paste-ready unit; the
    # markers, footer, and follow-up section are terminal-only guidance.
    start = output.find(OPENING_MARKER)
    end = output.find(CLOSING_MARKER)
    if start == -1 or end == -1 or end <= start:
        return output
    return output[start + len(OPENING_MARKER):end].strip("\n")


def save_prompt_backup(
    settings: OrderedDict[str, str], prompt_text_value: str, run: HistoryEntry
) -> Path | None:
    """Optionally save the prompt backup based on the backup_mode setting.

    If piped input runs out at the ask question, the backup is skipped
    silently instead of crashing after the prompt was already emitted.
    """
    backup_mode = settings["backup_mode"]
    if backup_mode == "never":
        return None
    folder = absolute_artifact_folder(settings["backup_folder"])
    should_save = backup_mode == "always"
    try:
        if backup_mode == "ask":
            should_save = ask_yes_no(f"Save a Markdown backup to {folder}?", "n")
        if not should_save:
            return None
        if not folder.exists():
            if not ask_yes_no(f"Backup folder does not exist: {folder}. Create it?", "n"):
                print("Backup not saved; folder was not created.")
                return None
            folder.mkdir(parents=True, exist_ok=True)
    except EOFError:
        print("\nInput ended; backup skipped.")
        return None
    except UserQuit:
        print("\nBackup skipped.")
        return None
    path = folder / prompt_artifact_filename(run, "backup")
    try:
        return save_text_without_overwrite(path, prompt_text_value + "\n", "prompt backup")
    except (OSError, RuntimeError) as exc:
        print(f"Prompt backup was not saved: {exc}")
        return None


def save_script_prompt_archive(
    settings: OrderedDict[str, str], run: HistoryEntry, prompt_text_value: str
) -> Path | None:
    """Save the paste-ready prompt to the script-owned prompt archive file.

    The sibling log stores variables only, so this separate Markdown file is a
    clean user-facing backup of exactly what the script copied to the clipboard.
    A failed archive write is reported but does not stop prompt printing.
    """
    if not ensure_artifact_folder_if_requested(
        settings["prompts_given_folder"], "Prompt archive"
    ):
        print("Warning: script prompt archive was not saved.")
        return None

    _relative_path, absolute_path = script_prompt_archive_paths(settings, run)
    path = Path(absolute_path)
    try:
        saved_path = save_text_without_overwrite(
            path, prompt_text_value + "\n", "script prompt archive"
        )
    except (OSError, RuntimeError) as exc:
        print(f"Warning: script prompt archive was not saved: {exc}")
        return None

    return saved_path


def console_will_close_on_exit() -> bool:
    """Detect a one-click launch whose console window dies with this process.

    On Windows, GetConsoleProcessList counts the processes attached to this
    console. One or two (python, or the py launcher plus python) means nothing
    else owns the window, so it closes the moment the script exits. From a
    shell, the shell itself stays attached and keeps the window alive.
    """
    if os.name != "nt":
        return False
    try:
        process_list = (wintypes.DWORD * 8)()
        get_processes = ctypes.windll.kernel32.GetConsoleProcessList
        get_processes.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
        get_processes.restype = wintypes.DWORD
        count = get_processes(process_list, 8)
    except (AttributeError, OSError):
        return True  # fall back to the cautious isatty heuristic: pause
    if count == 0:
        return True  # API failed; pausing is the safe default
    return count <= 2


def should_pause_before_exit() -> bool:
    """Return True when an interactive one-click launch should pause at exit."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    if "vscode" in term_program:
        return False
    if os.environ.get("VSCODE_INJECTION") or os.environ.get("VSCODE_PID"):
        return False
    return console_will_close_on_exit()


def pause_before_exit_if_needed() -> None:
    """Keep one-click terminal windows open long enough for manual copying."""
    if should_pause_before_exit():
        try:
            input("Press Enter to exit.")
        except (EOFError, KeyboardInterrupt):
            pass


def emit_output(settings: OrderedDict[str, str], output: str, payload: str | None = None) -> None:
    """Copy the prompt to the clipboard when enabled, then print all output."""
    if is_enabled(settings["copy_to_clipboard"]):
        copied, message = copy_to_clipboard(payload if payload is not None else clipboard_payload(output))
        if copied:
            print("Copied generated prompt to clipboard.")
        else:
            print(f"Clipboard copy skipped: {message}.")
    print(output)


def ensure_run_metadata(run: HistoryEntry, history: list[HistoryEntry]) -> None:
    """Attach session metadata to new runs that do not already have it."""
    if run.session_id:
        return
    session_id = generate_session_id(history)
    run.session_id = session_id
    run.root_id = session_id
    run.parent_id = "none"
    run.pass_type = "new"
    run.review = "manual"
    run.overrides = "manual"


def main() -> int:
    """Run the interactive prompt builder and return a process exit code.

    Return 0 for success or clean quit, 1 for build/EOF failure, and 130 for KeyboardInterrupt.
    """
    settings, history, issues, first_run = read_log()
    if issues:
        print(
            "Log had unparseable or missing values; built-in defaults were used where needed."
        )
    try:
        pillow_status = run_pillow_preflight()

        if first_run:
            print(f"No {LOG_PATH.name} found; first run setup will create it.")
            write_log(settings, history)
            settings, _changed = walk_settings(settings, history)
            write_log(settings, history)

        run = ask_run(settings, history, pillow_status)
        ensure_run_metadata(run, history)

        if run.reprint_only:
            output = build_output_safely(
                settings, run, history=history, pillow_status=pillow_status
            )
            if output is None:
                return 1
            emit_output(settings, output)
            return 0

        ensure_output_folder_if_requested(settings["output_folder"])

        # Build (and verify) before logging so a failed build never records a
        # history line that would poison later `last` runs.
        output = build_output_safely(
            settings, run, history=history, pillow_status=pillow_status
        )
        if output is None:
            return 1

        prompt_payload = clipboard_payload(output)
        history.append(run)
        write_log(settings, history)
        save_script_prompt_archive(settings, run, prompt_payload)
        emit_output(settings, output, prompt_payload)
        save_prompt_backup(settings, prompt_payload, run)
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted; log remains valid.")
        return 130
    except EOFError:
        print("\nInput ended unexpectedly; no run was recorded and the log remains valid.")
        return 1
    except UserQuit:
        print("\nQuit; no run was recorded and the log remains valid.")
        return 0
    finally:
        pause_before_exit_if_needed()


if __name__ == "__main__":
    raise SystemExit(main())
