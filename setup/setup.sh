#!/usr/bin/env bash
# Agent Kaizen setup for Linux / macOS after the repo is cloned.
set -euo pipefail

DEVROOT_ARG=""
LIST_STEPS=0
EMIT_PLAN_JSON=""
AK_PYTURSO_WHEEL_URL="${AK_PYTURSO_WHEEL_URL:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --devroot) DEVROOT_ARG="${2:-}"; shift ;;
    --list-steps) LIST_STEPS=1 ;;
    --emit-plan-json) EMIT_PLAN_JSON="${2:-}"; shift ;;
    --plan-only) AK_PLAN_ONLY=1 ;;
    --self-test) AK_SELF_TEST=1 ;;
    --no-progress) AK_NO_PROGRESS=1 ;;
    --no-network) AK_NO_NETWORK=1 ;;
    --no-external-actions) AK_NO_EXTERNAL=1 ;;
    --no-user-env-writes) AK_NO_USER_ENV=1 ;;
    --assume-yes) AK_ASSUME_YES=1 ;;
    --no-input) AK_NO_INPUT=1 ;;
    --pyturso-wheel-url) AK_PYTURSO_WHEEL_URL="${2:-}"; shift ;;
    -* ) printf 'unknown arg: %s\n' "$1" >&2; exit 64 ;;
    * ) if [ -z "$DEVROOT_ARG" ]; then DEVROOT_ARG="$1"; else printf 'unexpected arg: %s\n' "$1" >&2; exit 2; fi ;;
  esac
  shift
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
if [ "$LIST_STEPS" -eq 1 ]; then AK_PLAN_ONLY=1; fi
# shellcheck source=setup/installer-common.sh
source "$SCRIPT_DIR/installer-common.sh"
trap 'ak_failure_report "$?"' ERR

DEVROOT_RESOLVED="$(ak_resolve_devroot "$DEVROOT_ARG" "$REPO_ROOT")"
ak_init "Agent Kaizen setup (Linux/macOS)" "$REPO_ROOT" "$DEVROOT_RESOLVED" \
  "$(ak_step_obj preflight 'Validate git, Python 3.12+, and setup roots')" \
  "$(ak_step_obj venv 'Create or validate shared Python venv')" \
  "$(ak_step_obj rust 'Ensure Rust toolchain for building pyturso')" \
  "$(ak_step_obj deps 'Install pinned Agent Kaizen dependencies')" \
  "$(ak_step_obj skills 'Select and install Agent Kaizen skills')" \
  "$(ak_step_obj workspace 'Generate VS Code workspace')" \
  "$(ak_step_obj db 'Initialize local Kaizen DB')" \
  "$(ak_step_obj summary 'Print completion summary')"

if [ "$LIST_STEPS" -eq 1 ] || [ "$AK_PLAN_ONLY" -eq 1 ]; then
  ak_show_plan
  [ -z "$EMIT_PLAN_JSON" ] || ak_write_plan_json "$EMIT_PLAN_JSON"
  exit 0
fi
[ "$AK_SELF_TEST" -eq 0 ] || ak_show_plan

PY=""
VENV="$DEVROOT_RESOLVED/Python/venvs/kaizen"
VENV_PY="$VENV/bin/python"
WS_DIR="$REPO_ROOT/_workspace"
# Project + workspace naming. The bootstrap may rename the clone (folder + workspace file); the
# canonical name keeps the pretty "Agent Kaizen" label, a renamed project uses its own name.
PROJECT_NAME="${AK_PROJECT_NAME:-$(basename "$REPO_ROOT")}"
if [ "$PROJECT_NAME" = "agent-kaizen" ]; then WS_LABEL="Agent Kaizen"; else WS_LABEL="$PROJECT_NAME"; fi
WS_FILE="$WS_DIR/${PROJECT_NAME}-tools.code-workspace"
WHEEL_CACHE="$DEVROOT_RESOLVED/wheels"
# Whether the user opted into the Rust + C build toolchain (set by the bootstrap's dependency menu).
# Default 0: Turso installs from its prebuilt wheel, so no toolchain and no source build.
TOOL_RUST="${AK_TOOL_RUST:-0}"

ak_have_pyturso_wheel() {
  local d
  for d in "$WHEEL_CACHE" "$REPO_ROOT/wheels"; do
    [ -d "$d" ] || continue
    ls "$d"/pyturso-*.whl >/dev/null 2>&1 && return 0
  done
  return 1
}

step_preflight() {
  command -v git >/dev/null 2>&1 || ak_die "git not found on PATH."
  PY="$(command -v python3 || true)"
  [ -n "$PY" ] || ak_die "python3 not found (Python 3.12+ required)."
  PYVER="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
  "$PY" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,12) else 1)' >/dev/null 2>&1 || ak_die "Python 3.12 or newer required (found $PYVER)."
  "$PY" -c 'import venv' >/dev/null 2>&1 || ak_die "python venv module not available."
  printf 'repo    : %s\n' "$REPO_ROOT"
  printf 'DEVROOT : %s\n' "$DEVROOT_RESOLVED"
  printf 'python  : %s (%s)\n' "$PYVER" "$PY"
}

step_venv() {
  if [ ! -x "$VENV_PY" ]; then
    mkdir -p "$DEVROOT_RESOLVED/Python/venvs"
    ak_run --note "Creating the shared Agent Kaizen Python virtual environment." -- "$PY" -m venv "$VENV"
  else
    printf 'venv already present: %s\n' "$VENV"
  fi
  ak_run --note "Upgrading pip in the shared Agent Kaizen venv." -- "$VENV_PY" -m pip install --upgrade pip
}

step_rust() {
  # Rust is only needed to compile Turso from source. On Linux Turso installs from a prebuilt wheel,
  # so unless the user opted in (dependency menu / --with-rust) this is skipped.
  if [ "$TOOL_RUST" -ne 1 ]; then
    ak_step_skipped 'not selected; Turso installs from its prebuilt wheel'
    return 0
  fi
  if [ -x "$VENV_PY" ] && "$VENV_PY" -c 'import turso' >/dev/null 2>&1; then
    ak_step_skipped 'Turso already installed; Rust not needed'
    return 0
  fi
  if ak_have_pyturso_wheel; then
    ak_step_skipped 'prebuilt Turso wheel present; Rust not needed'
    return 0
  fi
  if ! command -v cc >/dev/null 2>&1 && ! command -v gcc >/dev/null 2>&1 && ! command -v clang >/dev/null 2>&1; then
    printf '%s[warn]%s no C compiler (cc/gcc/clang) found; the pyturso build will likely fail.\n' "$AK_C_WARN" "$AK_C_RESET" >&2
    printf '       Install your distro build tools (e.g. apt: build-essential) and rerun.\n' >&2
  fi
  ak_ensure_rust || ak_die "Rust (cargo) is required to build pyturso and could not be installed. Install rustup, or provide a prebuilt wheel via AK_PYTURSO_WHEEL_URL / a pyturso-*.whl in $WHEEL_CACHE. See setup/SETUP.md." "$AK_EX_UNAVAILABLE"
}

step_deps() {
  # Verify pillar: if pyturso already imports in the venv, dependencies are satisfied -- skip the
  # pip resolve and any pyturso rebuild.
  if "$VENV_PY" -c 'import turso' >/dev/null 2>&1; then
    printf 'Python dependencies already satisfied (pyturso imports); skipping.\n'
    return 0
  fi
  ak_run --note "Upgrading pip in the shared Agent Kaizen venv." -- "$VENV_PY" -m pip install --upgrade pip \
    || ak_die "Could not upgrade pip in the shared venv." "$AK_EX_SOFTWARE"

  mkdir -p "$WHEEL_CACHE"
  # Optional prebuilt pyturso wheel (skips the Rust/C source build), mirroring Windows -PyTursoWheelUrl.
  if [ -n "$AK_PYTURSO_WHEEL_URL" ] && [ "$AK_NO_NETWORK" -eq 0 ]; then
    ak_download "$AK_PYTURSO_WHEEL_URL" "$WHEEL_CACHE/$(basename "${AK_PYTURSO_WHEEL_URL%%\?*}")" 100000 \
      || printf '  [warn] could not download the pyturso wheel from AK_PYTURSO_WHEEL_URL\n' >&2
  fi

  local find_links=() d
  for d in "$WHEEL_CACHE" "$REPO_ROOT/wheels"; do
    [ -d "$d" ] && find_links+=(--find-links "$d")
  done

  local built_from_source=0
  if [ "$TOOL_RUST" -eq 1 ] && ! ak_have_pyturso_wheel; then
    # Toolchain selected and no local wheel -> compile from source; make sure cargo is reachable.
    built_from_source=1
    export CARGO_HOME="${CARGO_HOME:-$DEVROOT_RESOLVED/rust/.cargo}"
    ak_add_path_entry "$CARGO_HOME/bin"
    command -v cargo >/dev/null 2>&1 \
      || ak_die "Turso must be compiled but cargo is not available (the 'rust' step should have installed it)." "$AK_EX_UNAVAILABLE"
  fi

  if [ "$TOOL_RUST" -eq 1 ]; then
    # Toolchain available: prefer a wheel, fall back to a source build.
    ak_run --note "Installing Agent Kaizen dependencies (Turso: prebuilt wheel if available, else source)." -- "$VENV_PY" -m pip install --prefer-binary "${find_links[@]}" -r "$REPO_ROOT/requirements-kaizen.txt" \
      || ak_die "pip install of Agent Kaizen dependencies failed (see the command log above)." "$AK_EX_SOFTWARE"
  else
    # No toolchain: wheel-only, so a missing wheel fails cleanly instead of half-starting a build.
    ak_run --note "Installing Agent Kaizen dependencies (Turso from its prebuilt wheel)." -- "$VENV_PY" -m pip install --only-binary=:all: "${find_links[@]}" -r "$REPO_ROOT/requirements-kaizen.txt" \
      || ak_die "No prebuilt Turso wheel for your platform and the Rust build toolchain was not selected. Re-run and choose 'Install Rust + C build toolchain' (or pass --with-rust), or supply a wheel via AK_PYTURSO_WHEEL_URL / a pyturso-*.whl in $WHEEL_CACHE." "$AK_EX_UNAVAILABLE"
  fi

  if [ "$built_from_source" -eq 1 ]; then
    ak_run --note "Caching the built Turso wheel so later runs skip the toolchain." -- "$VENV_PY" -m pip wheel pyturso==0.6.1 -w "$WHEEL_CACHE" \
      || printf '  [warn] could not cache the built Turso wheel\n' >&2
  fi

  # Import smoke check -- the real acceptance test for the native module.
  "$VENV_PY" -c 'import turso; print("turso import OK")' \
    || ak_die "Turso installed but failed to import (see the setup command logs)." "$AK_EX_SOFTWARE"
}

step_skills() {
  local store="$DEVROOT_RESOLVED/SKILLS/skills"
  mkdir -p "$store"
  if [ ! -f "$DEVROOT_RESOLVED/SKILLS/README.md" ]; then
    printf '# SKILLS store\n\nShared skill store for Agent Kaizen projects, populated by the installer. Add more later with setup/link-skills.sh.\n' > "$DEVROOT_RESOLVED/SKILLS/README.md"
  fi
  ak_install_skills "$REPO_ROOT" "$VENV_PY" "$store"
}

step_workspace() {
  mkdir -p "$WS_DIR"
  {
    printf '{\n'
    printf '  "folders": [\n'
    printf '    { "name": "%s", "path": ".." },\n' "$WS_LABEL"
    printf '    { "name": "SKILLS", "path": "../../SKILLS" }\n'
    printf '  ],\n'
    printf '  "settings": {\n'
    printf '    "python.defaultInterpreterPath": "${workspaceFolder:%s}/../Python/venvs/kaizen/bin/python",\n' "$WS_LABEL"
    printf '    "python.terminal.activateEnvironment": false,\n'
    printf '    "files.exclude": { "**/__pycache__": true },\n'
    printf '    "search.exclude": { "**/__pycache__": true }\n'
    printf '  }\n'
    printf '}\n'
  } > "$WS_FILE"
  printf 'workspace: %s\n' "$WS_FILE"
  # Convenience launcher (parity with the duplication flow + the Windows installer): open the
  # workspace in a new VS Code window. On native Linux `code` opens locally; over Remote-WSL open
  # from the Windows side with `code --remote wsl+<distro> <workspace>`.
  local launcher="$REPO_ROOT/open-${PROJECT_NAME}-vscode.sh"
  { printf '#!/usr/bin/env bash\n'; printf 'exec code -n "%s" "$@"\n' "$WS_FILE"; } > "$launcher"
  chmod +x "$launcher"
  printf 'launcher : %s\n' "$launcher"
}

step_db() {
  ak_run --note "Initializing/checking the local Kaizen DB." -- "$VENV_PY" "$REPO_ROOT/kaizen.py" K1 --json
}

step_summary() {
  printf '\nAgent Kaizen is ready.\n'
  printf '  DEVROOT : %s\n' "$DEVROOT_RESOLVED"
  printf '  Repo    : %s\n' "$REPO_ROOT"
  printf '  Venv    : %s\n' "$VENV"
  printf '  Open it : code "%s"\n' "$WS_FILE"
  printf '  Skills  : %s/SKILLS/skills (add more later with setup/link-skills.sh)\n' "$DEVROOT_RESOLVED"
}

ak_run_step preflight 'Validate git, Python 3.12+, and setup roots' step_preflight
ak_run_step venv 'Create or validate shared Python venv' step_venv
ak_run_step rust 'Ensure Rust toolchain for building pyturso' step_rust
ak_run_step deps 'Install pinned Agent Kaizen dependencies' step_deps
ak_run_step skills 'Select and install Agent Kaizen skills' step_skills
ak_run_step workspace 'Generate VS Code workspace' step_workspace
ak_run_step db 'Initialize local Kaizen DB' step_db
ak_run_step summary 'Print completion summary' step_summary
