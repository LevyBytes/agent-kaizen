#!/usr/bin/env bash
# Populate the SKILLS store and create absolute skill links in .agents/.claude.
# Usage: link-skills.sh [STORE_URL|--store-url URL] [--devroot PATH] [--python-exe PATH]
#        link-skills.sh --list-steps|--plan-only|--self-test [--emit-plan-json PATH]
# Safety: --no-network --no-external-actions --no-user-env-writes --assume-yes --no-input --no-progress.
# Absolute links are deliberate; rerun after relocating DEVROOT/store to retarget them.
set -euo pipefail

STORE_URL=""
DEVROOT_ARG=""
PYTHON_EXE=""
LIST_STEPS=0
EMIT_PLAN_JSON=""

usage() {
  printf '%s\n' 'Usage: link-skills.sh [STORE_URL|--store-url URL] [--devroot PATH] [--python-exe PATH]' '       link-skills.sh --list-steps|--plan-only|--self-test [--emit-plan-json PATH]' 'Safety: --no-network --no-external-actions --no-user-env-writes --assume-yes --no-input --no-progress'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --store-url) [ $# -ge 2 ] || { printf '%s\n' '--store-url requires a value' >&2; exit 2; }; STORE_URL="$2"; shift 2; continue ;;
    --devroot) [ $# -ge 2 ] || { printf '%s\n' '--devroot requires a value' >&2; exit 2; }; DEVROOT_ARG="$2"; shift 2; continue ;;
    --python-exe) [ $# -ge 2 ] || { printf '%s\n' '--python-exe requires a value' >&2; exit 2; }; PYTHON_EXE="$2"; shift 2; continue ;;
    --list-steps) LIST_STEPS=1 ;;
    --emit-plan-json) [ $# -ge 2 ] || { printf '%s\n' '--emit-plan-json requires a value' >&2; exit 2; }; EMIT_PLAN_JSON="$2"; shift 2; continue ;;
    --plan-only) AK_PLAN_ONLY=1 ;;
    --self-test) AK_SELF_TEST=1 ;;
    --no-progress) AK_NO_PROGRESS=1 ;;
    --no-network) AK_NO_NETWORK=1 ;;
    --no-external-actions) AK_NO_EXTERNAL=1 ;;
    --no-user-env-writes) AK_NO_USER_ENV=1 ;;
    --assume-yes) AK_ASSUME_YES=1 ;; # accepted for cross-installer parity; this script has no confirmation prompt
    --no-input) AK_NO_INPUT=1 ;; # accepted for cross-installer parity; this script has no input prompt
    -h|--help) usage; exit 0 ;;
    -* ) printf 'unknown arg: %s\n' "$1" >&2; exit 2 ;;
    * ) if [ -z "$STORE_URL" ]; then STORE_URL="$1"; else printf 'unexpected arg: %s\n' "$1" >&2; exit 2; fi ;;
  esac
  shift
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
if [ "$LIST_STEPS" -eq 1 ]; then AK_PLAN_ONLY=1; fi
# shellcheck source=setup/installer-common.sh
source "$SCRIPT_DIR/installer-common.sh"

DEVROOT_RESOLVED="$(ak_resolve_devroot "$DEVROOT_ARG" "$REPO_ROOT")"
STORE="$DEVROOT_RESOLVED/SKILLS"
SKILLS_DIR="$STORE/skills"

ak_init "Agent Kaizen skill linker" "$REPO_ROOT" "$DEVROOT_RESOLVED" \
  "$(ak_step_obj preflight 'Resolve store paths and validate inputs')" \
  "$(ak_step_obj store 'Clone or update optional skills store')" \
  "$(ak_step_obj links 'Create .agents and .claude skill links')" \
  "$(ak_step_obj index 'Regenerate skill index when skill-drafting is available')"

if [ "$AK_PLAN_ONLY" -eq 1 ]; then
  ak_show_plan
  [ -z "$EMIT_PLAN_JSON" ] || ak_write_plan_json "$EMIT_PLAN_JSON"
  exit 0
fi
[ "$AK_SELF_TEST" -eq 0 ] || ak_show_plan

step_preflight() {
  printf 'DEVROOT : %s\n' "$DEVROOT_RESOLVED"
  printf 'RepoRoot: %s\n' "$REPO_ROOT"
  printf 'Store   : %s\n' "$STORE"
  if [ -z "$STORE_URL" ] && [ ! -d "$SKILLS_DIR" ]; then
    ak_die "No skills found at $SKILLS_DIR. Pass a store URL or populate the store first."
  fi
}

step_store() {
  # Pull a git store, clone into an empty location, or refuse populated non-git content.
  if [ -z "$STORE_URL" ]; then
    printf 'No store URL supplied; using the existing local store.\n'
    return
  fi
  if [ "$AK_NO_NETWORK" -eq 1 ]; then ak_step_skipped 'store URL present but network is disabled'; return; fi
  ak_assert_network_allowed "$STORE_URL"
  command -v git >/dev/null 2>&1 || ak_die "git not found on PATH."
  if [ -d "$STORE/.git" ]; then
    ak_run --note "Updating existing skills store with git pull --ff-only." -- git -C "$STORE" pull --ff-only
  else
    skills_real="$([ -d "$SKILLS_DIR" ] && find "$SKILLS_DIR" -mindepth 1 -maxdepth 1 ! -name .gitkeep -print 2>/dev/null | sed -n '1p' || true)"
    root_real="$([ -d "$STORE" ] && find "$STORE" -mindepth 1 -maxdepth 1 ! -name .gitkeep ! -name skills -print 2>/dev/null | sed -n '1p' || true)"
    if [ -n "$skills_real" ] || [ -n "$root_real" ]; then
      ak_die "SKILLS already has content at $STORE; manage it manually instead of cloning over it."
    fi
    mkdir -p "$STORE"
    ak_run --note "Cloning the selected skills store." -- git clone "$STORE_URL" "$STORE"
  fi
}

step_links() {
  if [ "$AK_NO_EXTERNAL" -eq 1 ]; then ak_step_skipped 'link creation blocked by --no-external-actions'; return; fi
  if [ ! -d "$SKILLS_DIR" ]; then ak_step_skipped "no skills found at $SKILLS_DIR"; return; fi
  local linked=0
  local d name m
  for d in "$SKILLS_DIR"/*/; do
    [ -f "${d}SKILL.md" ] || continue
    name="$(basename "$d")"
    for m in "$REPO_ROOT/.agents/skills" "$REPO_ROOT/.claude/skills"; do
      mkdir -p "$m"
      if [ -L "$m/$name" ]; then
        ln -sfn "${d%/}" "$m/$name"
      elif [ ! -e "$m/$name" ]; then
        ln -s "${d%/}" "$m/$name"
      fi
    done
    linked=$((linked + 1))
    printf 'linked: %s\n' "$name"
  done
  if [ "$linked" -eq 0 ]; then ak_step_skipped "no directories containing SKILL.md found under $SKILLS_DIR"; return; fi
  printf '%d skill(s) linked into .agents/skills and .claude/skills.\n' "$linked"
}

step_index() {
  if [ "$AK_NO_EXTERNAL" -eq 1 ]; then ak_step_skipped 'index generation blocked by --no-external-actions'; return; fi
  local BUILDER="$SKILLS_DIR/skill-drafting/scripts/skill_builder.py"
  if [ ! -f "$BUILDER" ]; then
    ak_step_skipped 'skill-drafting/scripts/skill_builder.py not found in the store'
    return
  fi
  local PY
  PY="$(ak_resolve_python "$PYTHON_EXE" "$DEVROOT_RESOLVED")"
  [ -n "$PY" ] || ak_die "python not found; run the core installer first or pass --python-exe"
  ak_run --note "Regenerating skill index files from the linked skill store." -- "$PY" "$BUILDER" index "$REPO_ROOT/.claude/skills" --mirror "$REPO_ROOT/.agents/skills"
}

ak_run_step preflight 'Resolve store paths and validate inputs' step_preflight
ak_run_step store 'Clone or update optional skills store' step_store
ak_run_step links 'Create .agents and .claude skill links' step_links
ak_run_step index 'Regenerate skill index when skill-drafting is available' step_index
