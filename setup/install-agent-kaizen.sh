#!/usr/bin/env bash
# Self-contained Linux/macOS installer for Agent Kaizen.
set -euo pipefail
# Keep apt/dpkg from opening interactive prompts (e.g. tzdata) on a bare machine.
export DEBIAN_FRONTEND=noninteractive

REPO_URL="${AK_REPO_SOURCE:-https://github.com/LevyBytes/agent-kaizen.git}"
# Project folder + workspace name (default agent-kaizen); --project-name or AK_PROJECT_NAME overrides.
REPO_NAME="${AK_PROJECT_NAME:-agent-kaizen}"
# Track whether the name was set explicitly (flag/env) so a fresh install only PROMPTS when it wasn't.
AK_NAME_EXPLICIT=0
[ -n "${AK_PROJECT_NAME:-}" ] && AK_NAME_EXPLICIT=1
REF="${AK_REF:-}"
DEVROOT_ARG=""
LIST_STEPS=0
EMIT_PLAN_JSON=""
AK_PLAN_ONLY=0
AK_SELF_TEST=0
AK_NO_PROGRESS=0
AK_NO_NETWORK=0
AK_NO_EXTERNAL=0
AK_NO_USER_ENV=0
AK_ASSUME_YES=0
AK_NO_INPUT=0
# Optional-dependency selection (empty = ask interactively; 0/1 = explicit via --with-*/env).
AK_NO_DEV_TOOLS="${AK_NO_DEV_TOOLS:-0}"
AK_WITH_RUST="${AK_WITH_RUST:-}"
AK_WITH_NODE="${AK_WITH_NODE:-}"
AK_WITH_CMAKE="${AK_WITH_CMAKE:-}"
# Provider runtime selection is independent of the optional developer-tool menu. The approved
# environment form is normalized after argv parsing so the CLI flag has higher precedence.
AK_WITH_CLAUDE_RUNTIME="${AK_WITH_CLAUDE_RUNTIME:-0}"
TOOL_RUST=0
TOOL_NODE=0
TOOL_CMAKE=0
# In-installer duplication: make a NEW project from an existing agent-kaizen (shared engine + venv,
# own DB). Empty = off; --duplicate NAME (or interactive offer when an existing project is detected).
AK_DUPLICATE="${AK_DUPLICATE:-}"
AK_NEW_VENV="${AK_NEW_VENV:-}"
AK_SOURCE_PROJECT=""
DUP_VENV=""
LOG_ROOT=""
COMMAND_COUNTER=0
CURRENT_STEP=0
CURRENT_STEP_NAME=""
STEPS=(
  "preflight|Resolve DEVROOT and installer mode"
  "toolselect|Choose optional dependencies to install"
  "prereqs|Install or validate git and Python 3.12+"
  "devtools|Install selected optional dev tools"
  "devroot|Create DEVROOT folder"
  "clone|Clone or update the Agent Kaizen repository"
  "handoff|Run the repo-local setup engine"
)

usage() {
  cat <<'USAGE'
Usage:
  bash install-agent-kaizen.sh [DEVROOT] [options]

Options:
  --devroot DIR              parent dev folder (default: $DEVROOT, else $HOME/dev)
  --ref REF                  git tag/ref to install (default: $AK_REF, else main)
  --repo-source URL_OR_PATH  clone source (default: $AK_REPO_SOURCE, else GitHub)
  --project-name NAME        project folder + workspace name (default: agent-kaizen)
  --duplicate NAME           make a new project from an existing agent-kaizen (shared engine + venv, own DB)
  --new-venv NAME            with --duplicate: create a separate venv instead of sharing kaizen
  --list-steps               show the step plan and exit
  --emit-plan-json PATH      write a plan snapshot
  --plan-only                show the plan without installers, downloads, clone, or setup
  --self-test                validate the installer shape without external actions
  --no-progress              disable live progress lines
  --no-network               block package installs, downloads, pulls, and clones
  --no-external-actions      block external commands
  --no-user-env-writes       block user environment writes in repo-local setup
  --assume-yes               skip confirmation prompts
  --no-input                 never prompt; fail fast if input is required
  --no-dev-tools             skip the optional-dependency menu (install the core only)
  --with-rust                install Rust + C build toolchain (to build Turso from source)
  --with-node                install Node.js
  --with-cmake               install CMake
  --with-claude-runtime      install the pinned Claude subscription provider runtime (requires DEVROOT-local Node)
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --devroot) DEVROOT_ARG="${2:-}"; shift ;;
    --ref) REF="${2:-}"; shift ;;
    --repo-source) REPO_URL="${2:-}"; shift ;;
    --project-name) REPO_NAME="${2:-}"; AK_NAME_EXPLICIT=1; shift ;;
    --duplicate) AK_DUPLICATE="${2:-}"; shift ;;
    --new-venv) AK_NEW_VENV="${2:-}"; shift ;;
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
    --no-dev-tools) AK_NO_DEV_TOOLS=1 ;;
    --with-rust) AK_WITH_RUST=1 ;;
    --with-node) AK_WITH_NODE=1 ;;
    --with-cmake) AK_WITH_CMAKE=1 ;;
    --with-claude-runtime) AK_WITH_CLAUDE_RUNTIME=1 ;;
    -h|--help) usage; exit 0 ;;
    -* ) printf 'unknown arg: %s\n' "$1" >&2; usage >&2; exit 64 ;;
    * ) if [ -z "$DEVROOT_ARG" ]; then DEVROOT_ARG="$1"; else printf 'unexpected arg: %s\n' "$1" >&2; exit 64; fi ;;
  esac
  shift
done

DEVROOT_ENV="${DEVROOT:-}"
DEVROOT="${DEVROOT_ARG:-${DEVROOT_ENV:-$HOME/dev}}"
DEVROOT_DEFAULTED=0
[ -n "$DEVROOT_ARG" ] || [ -n "$DEVROOT_ENV" ] || DEVROOT_DEFAULTED=1

validate_devroot() {
  case "$1" in
    ""|"/"|/usr|/usr/*|/bin|/sbin|/lib|/lib/*|/etc|/etc/*|/var|/var/*|/boot|/boot/*|/sys|/sys/*|/proc|/proc/*|/dev|/dev/*)
      die "refusing to use a protected system path as DEVROOT: $1" 64 ;;
  esac
}

# Confirm a defaulted DEVROOT interactively; otherwise state the default plainly (not a silent fallback).
if [ "$DEVROOT_DEFAULTED" -eq 1 ]; then
  if [ "$AK_NO_INPUT" -eq 0 ] && [ "$AK_ASSUME_YES" -eq 0 ] && [ -t 0 ]; then
    printf 'DEVROOT not specified. Install under [%s]? Enter a path or press Enter to accept: ' "$DEVROOT" >&2
    read -r _ak_reply || _ak_reply=""
    [ -z "$_ak_reply" ] || DEVROOT="$_ak_reply"
  else
    printf 'DEVROOT not specified; defaulting to %s (pass --devroot to change).\n' "$DEVROOT" >&2
  fi
fi
validate_devroot "$DEVROOT"
LOG_ROOT="$DEVROOT/agent-kaizen-setup/logs"

# Escape backslashes and quotes for installer-controlled JSON string values; callers reject unsafe path/name control characters.
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
have() { command -v "$1" >/dev/null 2>&1; }
have_cc() { have cc || have gcc || have clang; }
die() { printf 'ERROR: %s\n' "$1" >&2; exit "${2:-1}"; }

normalize_bool() {
  # Approved setup booleans are deliberately explicit; typos fail instead of silently enabling.
  case "$2" in
    1|true|enabled) printf '1' ;;
    0|false|disabled|"") printf '0' ;;
    *) die "invalid $1 boolean '$2' (expected 1/true/enabled or 0/false/disabled)" 64 ;;
  esac
}
AK_WITH_CLAUDE_RUNTIME="$(normalize_bool AK_WITH_CLAUDE_RUNTIME "$AK_WITH_CLAUDE_RUNTIME")"

# The project name becomes a folder + workspace-file name; keep it to safe path/JSON characters.
case "$REPO_NAME" in
  *[!A-Za-z0-9._-]*|""|.|..) die "invalid --project-name (letters, digits, '.', '-', '_' only): '$REPO_NAME'" 64 ;;
esac
# --duplicate / --new-venv names (when set) share the same safe folder/JSON rule.
for _ak_nm in "$AK_DUPLICATE" "$AK_NEW_VENV"; do
  [ -z "$_ak_nm" ] && continue
  case "$_ak_nm" in
    *[!A-Za-z0-9._-]*|.|..) die "invalid name (letters, digits, '.', '-', '_' only): '$_ak_nm'" 64 ;;
  esac
done

show_plan() {
  printf '\nAgent Kaizen installer (Linux/macOS)\n'
  printf '  DEVROOT: %s\n' "$DEVROOT"
  printf '  Source : %s\n' "$REPO_URL"
  [ -z "$REF" ] || printf '  Ref    : %s\n' "$REF"
  printf '  Claude : %s\n' "$([ "$AK_WITH_CLAUDE_RUNTIME" -eq 1 ] && printf selected || printf not-selected)"
  printf '  Logs   : %s\n\n' "$LOG_ROOT"
  printf 'Planned setup steps:\n'
  local total="${#STEPS[@]}" i entry name from to
  for ((i=0; i<${#STEPS[@]}; i++)); do
    entry="${STEPS[$i]}"
    name="${entry#*|}"
    from=$(( i * 100 / total ))
    to=$(( (i + 1) * 100 / total ))
    printf '%2d. %-58s %3d%% -> %3d%%\n' "$((i + 1))" "$name" "$from" "$to"
  done
}

write_plan_json() {
  local path="$1" i entry id name comma
  mkdir -p "$(dirname "$path")"
  {
    printf '{\n'
    printf '  "installer": "Agent Kaizen installer (Linux/macOS)",\n'
    printf '  "devRoot": "%s",\n' "$(json_escape "$DEVROOT")"
    printf '  "repoSource": "%s",\n' "$(json_escape "$REPO_URL")"
    printf '  "ref": "%s",\n' "$(json_escape "$REF")"
    printf '  "logRoot": "%s",\n' "$(json_escape "$LOG_ROOT")"
    printf '  "planOnly": %s,\n' "$([ "$AK_PLAN_ONLY" -eq 1 ] && printf true || printf false)"
    printf '  "selfTest": %s,\n' "$([ "$AK_SELF_TEST" -eq 1 ] && printf true || printf false)"
    printf '  "selection": {"claudeRuntime": %s},\n' \
      "$([ "$AK_WITH_CLAUDE_RUNTIME" -eq 1 ] && printf true || printf false)"
    printf '  "safety": {"noNetwork": %s, "noExternalActions": %s, "noUserEnvWrites": %s, "noInput": %s},\n' \
      "$([ "$AK_NO_NETWORK" -eq 1 ] && printf true || printf false)" \
      "$([ "$AK_NO_EXTERNAL" -eq 1 ] && printf true || printf false)" \
      "$([ "$AK_NO_USER_ENV" -eq 1 ] && printf true || printf false)" \
      "$([ "$AK_NO_INPUT" -eq 1 ] && printf true || printf false)"
    printf '  "steps": [\n'
    for ((i=0; i<${#STEPS[@]}; i++)); do
      entry="${STEPS[$i]}"
      id="${entry%%|*}"
      name="${entry#*|}"
      comma=","
      [ "$i" -eq "$((${#STEPS[@]} - 1))" ] && comma=""
      printf '    {"index": %d, "id": "%s", "name": "%s"}%s\n' "$((i + 1))" "$(json_escape "$id")" "$(json_escape "$name")" "$comma"
    done
    printf '  ]\n'
    printf '}\n'
  } > "$path"
  printf 'Plan JSON written: %s\n' "$path"
}

progress() {
  [ "$AK_NO_PROGRESS" -eq 0 ] || return 0
  local done="$1" step="$2" status="$3" name="$4"
  local total="${#STEPS[@]}" pct
  pct=$(( done * 100 / total ))
  printf '[%3d%%] [%d/%d] %s: %s\n' "$pct" "$step" "$total" "$status" "$name" >&2
}

command_log_path() {
  COMMAND_COUNTER=$((COMMAND_COUNTER + 1))
  mkdir -p "$LOG_ROOT"
  local leaf stamp
  leaf="$(basename "$1")"
  leaf="${leaf%.*}"
  leaf="$(printf '%s' "$leaf" | tr -c 'A-Za-z0-9_.-' '_')"
  stamp="$(date '+%Y%m%d-%H%M%S')"
  printf '%s/command-%s-%03d-%s.log' "$LOG_ROOT" "$stamp" "$COMMAND_COUNTER" "$leaf"
}

assert_external() {
  if [ "$AK_PLAN_ONLY" -eq 1 ] || [ "$AK_SELF_TEST" -eq 1 ] || [ "$AK_NO_EXTERNAL" -eq 1 ]; then
    die "External command blocked by installer safety mode: $*"
  fi
}

assert_network() {
  if [ "$AK_PLAN_ONLY" -eq 1 ] || [ "$AK_SELF_TEST" -eq 1 ] || [ "$AK_NO_NETWORK" -eq 1 ]; then
    die "Network action blocked by installer safety mode: $1"
  fi
}

# Run argv in a background child, log stdout/stderr and timestamps, show progress, and return the child's exit code; accepts optional `--note TEXT`.
run_cmd() {
  local note=""
  if [ "${1:-}" = "--note" ]; then note="${2:-}"; shift 2; fi
  [ $# -gt 0 ] || die "run_cmd called without a command"
  assert_external "$@"
  local log pid start rc elapsed spinner frames
  log="$(command_log_path "$1")"
  {
    printf 'COMMAND:'
    printf ' %q' "$@"
    printf '\nSTARTED: %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')"
    [ -z "$note" ] || printf 'NOTE: %s\n' "$note"
  } > "$log"
  ("$@" >> "$log" 2>&1) &
  pid=$!
  start="$(date +%s)"
  frames='|/-\'
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$AK_NO_PROGRESS" -eq 0 ]; then
      elapsed=$(( $(date +%s) - start ))
      spinner="${frames:$((elapsed % 4)):1}"
      printf '\r[%s] step %d/%d elapsed %02d:%02d:%02d %s' "$spinner" "$CURRENT_STEP" "${#STEPS[@]}" "$((elapsed/3600))" "$(((elapsed/60)%60))" "$((elapsed%60))" "$CURRENT_STEP_NAME" >&2
    fi
    sleep 1
  done
  if wait "$pid"; then rc=0; else rc=$?; fi
  printf '\nFINISHED: %s\nEXIT CODE: %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$rc" >> "$log"
  [ "$AK_NO_PROGRESS" -eq 1 ] || printf '\n' >&2
  if [ "$rc" -ne 0 ]; then
    printf 'Command failed with exit code %s. Log: %s\n' "$rc" "$log" >&2
    tail -n 12 "$log" >&2 || true
    return "$rc"
  fi
  printf '    command completed successfully. Log: %s\n' "$log"
}

# Resolve a step id, update progress state, and execute argv unless plan-only or self-test mode suppresses external work.
run_step() {
  local id="$1" name="$2"
  shift 2
  local step=0 i entry
  for ((i=0; i<${#STEPS[@]}; i++)); do
    entry="${STEPS[$i]}"
    [ "${entry%%|*}" = "$id" ] && step=$((i + 1))
  done
  [ "$step" -gt 0 ] || step=$((CURRENT_STEP + 1))
  CURRENT_STEP="$step"
  CURRENT_STEP_NAME="$name"
  progress "$((step - 1))" "$step" "working" "$name"
  printf '\n=== [%d/%d] %s ===\n' "$step" "${#STEPS[@]}" "$name"
  if [ "$AK_PLAN_ONLY" -eq 1 ]; then
    printf 'Plan-only: step not executed.\n'
  elif [ "$AK_SELF_TEST" -eq 1 ]; then
    printf 'Self-test: validating step shape only; no external actions.\n'
  else
    "$@"
  fi
  progress "$step" "$step" "OK" "$name"
}

# Set global SUDO to empty for root or `sudo` when available; return 1 without setting it when elevation is unavailable.
need_sudo() {
  if [ "$(id -u)" -eq 0 ]; then SUDO=""; return 0; fi
  if have sudo; then SUDO="sudo"; return 0; fi
  return 1
}

py_ok() {
  have python3 || return 1
  python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,12) else 1)' >/dev/null 2>&1 || return 1
  # `import venv` passes even when the Debian/Ubuntu python3.X-venv package (ensurepip's bundled pip
  # wheels) is missing -- a bare cloud image has the module but cannot build a real venv. Actually
  # create a throwaway venv to confirm venv is usable end to end.
  local probe rc
  probe="$(mktemp -d 2>/dev/null)" || return 1
  python3 -m venv "$probe/v" >/dev/null 2>&1; rc=$?
  rm -rf "$probe" 2>/dev/null || true
  return $rc
}

manual_prereqs() {
  printf 'ERROR: could not auto-install prerequisites (%s).\n' "$1" >&2
  printf 'Install these, then run this installer again:\n' >&2
  printf '  git    : https://git-scm.com/download\n' >&2
  printf '  Python : https://www.python.org/downloads/   (3.12 or newer, with venv + pip)\n' >&2
  exit 1
}

# Package-manager dispatch: $1 = human note; optional $2 = --lean (apt --no-install-recommends);
# remaining args = package names.
pkg_install() {
  local note="$1"; shift
  local recommends=1
  if [ "${1:-}" = "--lean" ]; then recommends=0; shift; fi
  assert_network "$PKG package install"
  case "$PKG" in
    apt-get)
      need_sudo || manual_prereqs "no sudo"
      run_cmd --note "apt-get update downloads package metadata." $SUDO apt-get update
      # --lean (build toolchain): --no-install-recommends drops the ~130 recommended extras (fonts,
      # image libs) build-essential otherwise drags in -- leaner and far fewer fetches to fail. The
      # core git/Python install keeps recommends so python3-venv's full closure (ensurepip) installs.
      if [ "$recommends" -eq 0 ]; then
        run_cmd --note "$note" $SUDO apt-get install -y --no-install-recommends "$@"
      else
        run_cmd --note "$note" $SUDO apt-get install -y "$@"
      fi
      ;;
    dnf) need_sudo || manual_prereqs "no sudo"; run_cmd --note "$note" $SUDO dnf install -y "$@" ;;
    yum) need_sudo || manual_prereqs "no sudo"; run_cmd --note "$note" $SUDO yum install -y "$@" ;;
    pacman) need_sudo || manual_prereqs "no sudo"; run_cmd --note "$note" $SUDO pacman -S --needed --noconfirm "$@" ;;
    zypper) need_sudo || manual_prereqs "no sudo"; run_cmd --note "$note" $SUDO zypper --non-interactive install "$@" ;;
    brew) run_cmd --note "$note" brew install "$@" ;;
    *) manual_prereqs "no supported package manager" ;;
  esac
}

install_pkgs() {
  case "$PKG" in
    apt-get) pkg_install "Installing git, Python, venv, and pip via apt-get." git python3 python3-venv python3-pip ;;
    dnf|yum) pkg_install "Installing git and Python." git python3 python3-pip ;;
    pacman) pkg_install "Installing git and Python via pacman." git python ;;
    zypper) pkg_install "Installing git and Python via zypper." git python3 python3-venv python3-pip ;;
    brew) pkg_install "Installing git and Python 3.12 via Homebrew." git python@3.12 ;;
    *) manual_prereqs "no supported package manager" ;;
  esac
}

install_build_pkgs() {
  # pyturso has no manylinux wheel in the common case, so it compiles from source. Provide a C
  # toolchain plus curl/CA certs (rustup and the native build both need them) up front.
  case "$PKG" in
    apt-get) pkg_install "Installing the C build toolchain (build-essential, pkg-config, curl)." --lean build-essential pkg-config curl ca-certificates ;;
    dnf|yum) pkg_install "Installing the C build toolchain (gcc, make, pkg-config, curl)." gcc gcc-c++ make pkgconfig curl ca-certificates ;;
    pacman) pkg_install "Installing the C build toolchain (base-devel, curl)." base-devel curl ca-certificates ;;
    zypper) pkg_install "Installing the C build toolchain (gcc, make, pkg-config, curl)." gcc gcc-c++ make pkg-config curl ca-certificates ;;
    brew) printf '  [note] On macOS the Xcode Command Line Tools provide the C toolchain (xcode-select --install).\n' ;;
    *) printf '  [warn] unknown package manager; install a C toolchain + curl manually for the pyturso build.\n' >&2 ;;
  esac
}

ensure_build_toolchain() {
  if have_cc && have curl; then
    printf 'build  : C toolchain present (%s)\n' "$(command -v cc || command -v gcc || command -v clang)"
    return 0
  fi
  if [ -z "$PKG" ]; then
    printf '  [warn] no supported package manager; install a C toolchain + curl manually for the pyturso build.\n' >&2
    return 0
  fi
  install_build_pkgs
}

ensure_ca_certs() {
  # pip talks to PyPI over HTTPS; a bare image can lack the CA bundle. Idempotent -- skip if present.
  if [ -f /etc/ssl/certs/ca-certificates.crt ] || [ -f /etc/pki/tls/certs/ca-bundle.crt ]; then return 0; fi
  [ -n "$PKG" ] || return 0
  if [ "$PKG" = "brew" ]; then return 0; fi
  pkg_install "Installing CA certificates for HTTPS (pip/PyPI)." ca-certificates
}

ask_yes_no() {
  # $1 = prompt, $2 = default (y|n). Prints 1 (yes) or 0 (no); the prompt goes to stderr so this
  # can be used in $(...). Reads from the terminal (works inside command substitution).
  local prompt="$1" def="$2" reply hint='[y/N]'
  if [ "$def" = "y" ]; then hint='[Y/n]'; fi
  printf '%s %s ' "$prompt" "$hint" >&2
  read -r reply || reply=""
  if [ -z "$reply" ]; then reply="$def"; fi
  case "$reply" in [Yy]*) printf '1' ;; *) printf '0' ;; esac
}

resolve_tool() {
  # $1 = explicit flag (empty = ask), $2 = prompt, $3 = interactive(0/1). Prints 0/1.
  if [ -n "$1" ]; then printf '%s' "$1"
  elif [ "$3" -eq 1 ]; then ask_yes_no "$2" n
  else printf '0'; fi
}

select_dev_tools() {
  # Interactive optional-dependency menu (mirrors the Windows dev-tool selection). The core install
  # -- git, Python, venv, and Turso from its prebuilt wheel -- is automatic and not asked about.
  printf 'Claude subscription runtime: %s\n' "$([ "$AK_WITH_CLAUDE_RUNTIME" -eq 1 ] && printf selected || printf not-selected)"
  if [ "$AK_NO_DEV_TOOLS" -eq 1 ]; then
    printf 'Optional-dependency menu disabled (--no-dev-tools); installing the core only.\n'
    return 0
  fi
  local interactive=0
  if [ "$AK_NO_INPUT" -eq 0 ] && [ -t 0 ]; then interactive=1; fi
  if [ "$interactive" -eq 1 ]; then
    printf '\nAgent Kaizen installs git, Python, a virtual environment, and Turso (from a prebuilt\n' >&2
    printf 'wheel) automatically. The items below are OPTIONAL -- press Enter for the default (No):\n' >&2
  fi
  TOOL_RUST=$(resolve_tool "$AK_WITH_RUST" '  Install Rust + C build toolchain (only needed if no Turso wheel matches your platform)?' "$interactive")
  TOOL_NODE=$(resolve_tool "$AK_WITH_NODE" '  Install Node.js?' "$interactive")
  TOOL_CMAKE=$(resolve_tool "$AK_WITH_CMAKE" '  Install CMake?' "$interactive")
  printf 'Optional selections: rust=%s node=%s cmake=%s\n' "$TOOL_RUST" "$TOOL_NODE" "$TOOL_CMAKE"
}

install_optional_tools() {
  local pkgs=()
  if [ "$TOOL_NODE" -eq 1 ]; then pkgs+=(nodejs npm); fi
  if [ "$TOOL_CMAKE" -eq 1 ]; then pkgs+=(cmake); fi
  if [ "${#pkgs[@]}" -eq 0 ]; then printf 'No optional dev tools selected.\n'; return 0; fi
  if [ -z "$PKG" ]; then printf '  [warn] no package manager; cannot install optional tools: %s\n' "${pkgs[*]}" >&2; return 0; fi
  # Package names above are the Debian/Ubuntu (apt) spelling; other managers may differ. Optional tools
  # must never abort the core install: if the package manager fails (e.g. a name not in the base repos
  # on a bare image), warn and continue so the clone, venv, and workspace still complete.
  if ! pkg_install "Installing selected optional dev tools: ${pkgs[*]}." "${pkgs[@]}"; then
    printf '  [warn] optional dev tools failed to install (%s); continuing with the core install.\n' "${pkgs[*]}" >&2
  fi
  return 0
}

step_preflight() {
  printf 'DEVROOT: %s\n' "$DEVROOT"
  printf 'Source : %s\n' "$REPO_URL"
  [ -z "$REF" ] || printf 'Ref    : %s\n' "$REF"
}

step_prereqs() {
  PKG=""
  for c in apt-get dnf yum pacman zypper brew; do
    if have "$c"; then PKG="$c"; break; fi
  done
  if have git && py_ok; then
    printf 'git    : %s\n' "$(git --version)"
    printf 'python : %s\n' "$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
  else
    [ -n "$PKG" ] || manual_prereqs "no supported package manager"
    install_pkgs
    if [ "$PKG" = "brew" ] && ! have git; then
      [ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)"
      [ -x /usr/local/bin/brew ] && eval "$(/usr/local/bin/brew shellenv)"
    fi
    have git || manual_prereqs "git"
    py_ok || manual_prereqs "python3 (3.12+ with venv)"
    printf 'git    : %s\n' "$(git --version)"
    printf 'python : %s\n' "$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
  fi
  ensure_ca_certs
  # The C build toolchain is only needed to compile Turso from source; skip it when Turso will come
  # from its prebuilt wheel (the common case, TOOL_RUST=0).
  if [ "$TOOL_RUST" -eq 1 ]; then ensure_build_toolchain; fi
}

step_devroot() {
  mkdir -p "$DEVROOT"
  mkdir -p "$LOG_ROOT"
  printf 'DEVROOT ready: %s\n' "$DEVROOT"
}

sync_existing() {
  if [ -n "$REF" ]; then
    assert_network "git fetch"
    run_cmd --note "Fetching tags before checking out the requested ref." git -C "$REPO_PATH" fetch --tags --quiet origin || true
    run_cmd --note "Checking out requested Agent Kaizen ref." git -C "$REPO_PATH" checkout "$REF" || printf '      [warn] could not checkout %s; leaving as-is.\n' "$REF"
  else
    assert_network "git pull"
    run_cmd --note "Fast-forwarding an existing Agent Kaizen checkout." git -C "$REPO_PATH" pull --ff-only || printf '      [warn] git pull did not fast-forward; leaving as-is.\n'
  fi
}

step_clone() {
  REPO_PATH="$DEVROOT/$REPO_NAME"
  if [ -f "$REPO_PATH/kaizen.py" ]; then
    printf 'clone already exists: %s\n' "$REPO_PATH"
    [ -d "$REPO_PATH/.git" ] && sync_existing
  elif [ -e "$REPO_PATH" ]; then
    if [ -d "$REPO_PATH/.git" ]; then
      sync_existing
    else
      die "a non-git folder already occupies $REPO_PATH; refusing to overwrite"
    fi
  else
    assert_network "$REPO_URL"
    if [ -n "$REF" ]; then
      run_cmd --note "Cloning Agent Kaizen at the requested ref." git clone --branch "$REF" "$REPO_URL" "$REPO_PATH"
    else
      run_cmd --note "Cloning Agent Kaizen repository data." git clone "$REPO_URL" "$REPO_PATH"
    fi
  fi
  printf 'repo at: %s\n' "$REPO_PATH"
}

step_handoff() {
  REPO_PATH="$DEVROOT/$REPO_NAME"
  [ -f "$REPO_PATH/setup/setup.sh" ] || die "repo-local setup script not found: $REPO_PATH/setup/setup.sh"
  args=("$REPO_PATH/setup/setup.sh" "$DEVROOT")
  [ "$AK_NO_PROGRESS" -eq 0 ] || args+=("--no-progress")
  [ "$AK_NO_NETWORK" -eq 0 ] || args+=("--no-network")
  [ "$AK_NO_EXTERNAL" -eq 0 ] || args+=("--no-external-actions")
  [ "$AK_NO_USER_ENV" -eq 0 ] || args+=("--no-user-env-writes")
  [ "$AK_ASSUME_YES" -eq 0 ] || args+=("--assume-yes")
  [ "$AK_NO_INPUT" -eq 0 ] || args+=("--no-input")
  [ "$AK_WITH_CLAUDE_RUNTIME" -eq 0 ] || args+=("--with-claude-runtime")
  # Pass the resolved Rust/toolchain choice to the repo-local engine (drives its rust + deps steps).
  export AK_TOOL_RUST="$TOOL_RUST"
  export AK_PROJECT_NAME="$REPO_NAME"
  bash "${args[@]}"
}

# --- Duplication (make a new project from an existing agent-kaizen) -----------
# Engine/support = LINKED (shared codebase); customizable files = COPIED (per-project, editable). The
# result has agent-kaizen's layout, each entry either a link or a real copy -- never a blanket copy.
DUP_LINK_ITEMS=(kaizen.py kaizen_components support_scripts prompt-builder-scripts requirements-kaizen.txt Kaizen_System.md)
DUP_COPY_ITEMS=(AGENTS.md CLAUDE.md README.md setup evals docs LICENSE .gitignore .gitattributes .prettierrc.json .prettierignore)

detect_source_project() {
  # Echo the path of a properly git-backed agent-kaizen under DEVROOT (ANY folder name, e.g. a renamed
  # install), else nothing. A duplicated project is skipped: it has no .git and a symlinked kaizen.py.
  local p
  for p in "$DEVROOT"/*/; do
    p="${p%/}"
    if [ -d "$p/.git" ] && [ -f "$p/kaizen.py" ] && [ ! -L "$p/kaizen.py" ] && [ -d "$p/kaizen_components" ]; then
      printf '%s' "$p"; return 0
    fi
  done
}

set_dup_steps() {
  STEPS=(
    "preflight|Resolve DEVROOT and source project"
    "prereqs|Validate git and Python 3.12+"
    "create|Create the new project (link engine, copy customizable files)"
    "venv|Select or create the Python venv"
    "wrapper|Write the project wrapper"
    "db|Initialize the new project's Kaizen DB"
    "skills|Select and link skills"
    "workspace|Generate VS Code workspace and launcher"
    "summary|Print completion summary"
  )
}

read_project_name() {
  # $1 = prompt, $2 = default (may be empty). Echo a valid name to stdout; prompt/errors to stderr
  # so this can be used in $(...). Mirrors the Windows Read-AkProjectName.
  local prompt="$1" def="$2" name=""
  while [ -z "$name" ]; do
    if [ -n "$def" ]; then printf '%s [%s]: ' "$prompt" "$def" >&2; else printf '%s: ' "$prompt" >&2; fi
    if ! read -r name; then name="$def"; fi
    [ -n "$name" ] || name="$def"
    case "$name" in
      ""|.|..|*[!A-Za-z0-9._-]*) printf '  Use letters, digits, dot, dash, underscore only.\n' >&2; name="" ;;
    esac
  done
  printf '%s' "$name"
}

maybe_offer_duplication() {
  # Decide fresh-install-with-optional-rename vs duplicate-from-existing, before the normal steps.
  # Mirrors the Windows Resolve-AkProjectMode (3-option duplication menu + fresh-install rename prompt).
  AK_SOURCE_PROJECT="$(detect_source_project)"
  if [ -n "$AK_DUPLICATE" ]; then
    [ -n "$AK_SOURCE_PROJECT" ] || AK_SOURCE_PROJECT="$DEVROOT/$REPO_NAME"
    return 0
  fi
  if [ "$AK_SELF_TEST" -eq 1 ]; then return 0; fi
  local interactive=0
  if [ "$AK_NO_INPUT" -eq 0 ] && [ -t 0 ]; then interactive=1; fi

  if [ -n "$AK_SOURCE_PROJECT" ]; then
    if [ "$interactive" -eq 0 ]; then return 0; fi
    printf '\nAn Agent Kaizen project already exists at %s.\n' "$AK_SOURCE_PROJECT" >&2
    printf '\nDuplicate the framework into a new project? (shared engine + its own database)\n' >&2
    printf '  1) New project - same shared venv\n' >&2
    printf '  2) New project - a separate venv\n' >&2
    printf '  3) No - skip duplication; do a normal Agent Kaizen install/update\n' >&2
    local choice=""
    while :; do
      printf 'Choose [1/2/3] (default 3): ' >&2
      read -r choice || choice=""
      [ -n "$choice" ] || choice=3
      case "$choice" in 1|2|3) break ;; *) printf '  Please enter 1, 2, or 3.\n' >&2 ;; esac
    done
    if [ "$choice" = "3" ]; then return 0; fi
    AK_DUPLICATE="$(read_project_name 'New project folder name' '')"
    # Option 2 gets its own venv (named after the project); option 1 shares the kaizen venv.
    if [ "$choice" = "2" ]; then AK_NEW_VENV="$AK_DUPLICATE"; fi
    return 0
  fi

  # Fresh install: let an interactive user choose the project folder/workspace name (rename).
  if [ "$interactive" -eq 1 ] && [ "$AK_NAME_EXPLICIT" -eq 0 ]; then
    REPO_NAME="$(read_project_name 'Project folder name' "$REPO_NAME")"
  fi
}

dup_target() { printf '%s/%s' "$DEVROOT" "$AK_DUPLICATE"; }

step_dup_preflight() {
  printf 'DEVROOT: %s\n' "$DEVROOT"
  printf 'Source : %s\n' "$AK_SOURCE_PROJECT"
  printf 'New    : %s\n' "$(dup_target)"
  [ -n "$AK_SOURCE_PROJECT" ] && [ -f "$AK_SOURCE_PROJECT/kaizen.py" ] || die "no source agent-kaizen project found under $DEVROOT" 69
}

step_dup_create() {
  local src="$AK_SOURCE_PROJECT" tgt item; tgt="$(dup_target)"
  if [ -e "$tgt" ] && [ -n "$(ls -A "$tgt" 2>/dev/null)" ]; then die "target already exists and is non-empty: $tgt" 64; fi
  mkdir -p "$tgt"
  for item in "${DUP_LINK_ITEMS[@]}"; do
    if [ -e "$src/$item" ] && [ ! -e "$tgt/$item" ]; then ln -s "$src/$item" "$tgt/$item"; printf '  link : %s\n' "$item"; fi
  done
  for item in "${DUP_COPY_ITEMS[@]}"; do
    if [ -e "$src/$item" ]; then cp -r "$src/$item" "$tgt/$item"; printf '  copy : %s\n' "$item"; fi
  done
  mkdir -p "$tgt/AI" "$tgt/.agents/skills" "$tgt/.claude/skills"
  [ -f "$src/.claude/settings.local.json" ] && cp "$src/.claude/settings.local.json" "$tgt/.claude/" 2>/dev/null || true
  printf 'created: %s\n' "$tgt"
}

step_dup_venv() {
  if [ -n "$AK_NEW_VENV" ]; then
    DUP_VENV="$DEVROOT/Python/venvs/$AK_NEW_VENV"
    if [ -x "$DUP_VENV/bin/python" ]; then
      printf 'new venv already present: %s\n' "$DUP_VENV"
    else
      mkdir -p "$DEVROOT/Python/venvs"
      run_cmd --note "Creating a separate venv for the new project." python3 -m venv "$DUP_VENV"
      assert_network "new-venv dependency installation"
      run_cmd --note "Upgrading pip in the new venv." "$DUP_VENV/bin/python" -m pip install --upgrade pip
      run_cmd --note "Installing pinned dependencies into the new venv (wheel-only)." "$DUP_VENV/bin/python" -m pip install --only-binary=:all: -r "$AK_SOURCE_PROJECT/requirements-kaizen.txt"
    fi
  else
    DUP_VENV="$DEVROOT/Python/venvs/kaizen"
    [ -x "$DUP_VENV/bin/python" ] || die "shared kaizen venv not found at $DUP_VENV; run a normal install first" 69
    printf 'venv (shared): %s\n' "$DUP_VENV"
  fi
}

step_dup_wrapper() {
  local tgt; tgt="$(dup_target)"
  local wrapper="$tgt/kaizen.sh"
  {
    printf '#!/usr/bin/env bash\n'
    printf '# Agent Kaizen project wrapper: run the shared engine against THIS project'"'"'s own DB.\n'
    printf 'export KAIZEN_REPO_ROOT="%s"\n' "$tgt"
    printf 'exec "%s/bin/python" "%s/kaizen.py" "$@"\n' "$DUP_VENV" "$tgt"
  } > "$wrapper"
  chmod +x "$wrapper"
  printf 'wrapper: %s (sets KAIZEN_REPO_ROOT so the linked engine writes here)\n' "$wrapper"
}

step_dup_db() {
  local tgt; tgt="$(dup_target)"
  run_cmd --note "Initializing the new project's own Kaizen DB." bash "$tgt/kaizen.sh" K1 --json
}

step_dup_skills() {
  local tgt; tgt="$(dup_target)"
  local store="$DEVROOT/SKILLS/skills"
  mkdir -p "$store" "$tgt/.agents/skills" "$tgt/.claude/skills"
  local -a names=() urls=() want=()
  if [ "$AK_NO_INPUT" -eq 0 ] && [ "$AK_NO_NETWORK" -eq 0 ] && [ "$AK_NO_EXTERNAL" -eq 0 ] && [ -t 0 ]; then
    local list
    list="$("$DUP_VENV/bin/python" - <<'PY' 2>/dev/null
import json,re,sys,urllib.request
try:
    req=urllib.request.Request("https://api.github.com/users/LevyBytes/repos?per_page=100",headers={"User-Agent":"agent-kaizen"})
    data=json.load(urllib.request.urlopen(req,timeout=20))
except Exception:
    sys.exit(0)
if not isinstance(data,list):
    sys.exit(0)
for r in sorted(data,key=lambda x:x.get("name","")):
    n=r.get("name","")
    if re.match(r"^AI-SKILL-",n) or re.match(r"^AI-skill-",n):
        print(re.sub(r"^AI-SKILL-","",re.sub(r"^AI-skill-","skill-",n))+"\t"+r.get("clone_url",""))
PY
)" || true
    local sname surl
    while IFS=$'\t' read -r sname surl; do
      [ -n "$sname" ] || continue
      names+=("$sname"); urls+=("$surl")
    done <<< "$list"
    # Fall back to whatever is already in the shared store if GitHub gave nothing.
    if [ "${#names[@]}" -eq 0 ]; then
      local d
      for d in "$store"/*/; do
        [ -d "$d" ] || continue
        names+=("$(basename "$d")"); urls+=("")
      done
    fi
    if [ "${#names[@]}" -gt 0 ]; then
      printf '\nSkills ([*] = already in the shared store, links with no download):\n' >&2
      local i mark
      for i in "${!names[@]}"; do
        mark='   '; [ -d "$store/${names[$i]}" ] && mark='[*]'
        printf '  %2d) %s %s\n' "$((i+1))" "$mark" "${names[$i]}" >&2
      done
      printf "Enter numbers to link (comma-separated), 'all', or Enter for none: " >&2
      local ans; read -r ans || ans=""
      case "$ans" in
        all|ALL|a) for i in "${!names[@]}"; do want+=("$i"); done ;;
        "") : ;;
        *) local tok; local -a sel; IFS=', ' read -r -a sel <<< "$ans"
           for tok in "${sel[@]}"; do
             case "$tok" in ''|*[!0-9]*) continue ;; esac
             local idx=$((tok-1)); [ "$idx" -ge 0 ] && [ "$idx" -lt "${#names[@]}" ] && want+=("$idx")
           done ;;
      esac
      # Clone any selected skill not yet in the shared store (one store serves all projects).
      if [ "${#want[@]}" -gt 0 ]; then
        for i in "${want[@]}"; do
          if [ ! -d "$store/${names[$i]}" ] && [ -n "${urls[$i]}" ]; then
            assert_network "clone skill ${names[$i]}"
            run_cmd --note "Cloning skill ${names[$i]} into the shared store." git clone --depth 1 "${urls[$i]}" "$store/${names[$i]}" || true
          fi
        done
      fi
    fi
  fi
  # Link the selected skills into the new project's mirrors.
  local i n m linked=0
  if [ "${#want[@]}" -gt 0 ]; then
    for i in "${want[@]}"; do
      n="${names[$i]}"
      [ -d "$store/$n" ] && [ -f "$store/$n/SKILL.md" ] || continue
      for m in "$tgt/.agents/skills" "$tgt/.claude/skills"; do
        [ -e "$m/$n" ] || ln -s "$store/$n" "$m/$n"
      done
      linked=$((linked + 1)); printf '  linked: %s\n' "$n"
    done
  fi
  printf '%d skill(s) linked into the new project (shared store: %s).\n' "$linked" "$store"
}

step_dup_workspace() {
  local tgt; tgt="$(dup_target)"
  local name="$AK_DUPLICATE" ws_dir ws_file launcher
  ws_dir="$tgt/_workspace"; ws_file="$ws_dir/${name}-tools.code-workspace"
  mkdir -p "$ws_dir"
  {
    printf '{\n'
    printf '  "folders": [\n'
    printf '    { "name": "%s", "path": ".." },\n' "$name"
    printf '    { "name": "SKILLS", "path": "../../SKILLS" }\n'
    printf '  ],\n'
    printf '  "settings": {\n'
    printf '    "python.defaultInterpreterPath": "%s/bin/python",\n' "$DUP_VENV"
    printf '    "python.terminal.activateEnvironment": false,\n'
    printf '    "terminal.integrated.env.linux": { "KAIZEN_REPO_ROOT": "%s" },\n' "$tgt"
    printf '    "files.exclude": { "**/__pycache__": true },\n'
    printf '    "search.exclude": { "**/__pycache__": true }\n'
    printf '  }\n'
    printf '}\n'
  } > "$ws_file"
  printf 'workspace: %s\n' "$ws_file"
  launcher="$tgt/open-${name}-vscode.sh"
  { printf '#!/usr/bin/env bash\n'; printf 'exec code -n "%s"\n' "$ws_file"; } > "$launcher"
  chmod +x "$launcher"
  printf 'launcher : %s\n' "$launcher"
}

step_dup_summary() {
  local tgt; tgt="$(dup_target)"
  printf '\nNew Agent Kaizen project ready.\n'
  printf '  Project : %s\n' "$tgt"
  printf '  Engine  : linked from %s (shared codebase)\n' "$AK_SOURCE_PROJECT"
  printf '  Venv    : %s\n' "$DUP_VENV"
  printf '  DB      : %s/AI/db/kaizen.db (its own)\n' "$tgt"
  printf '  Run     : bash "%s/kaizen.sh" K1 --json\n' "$tgt"
  printf '  Open    : code -n "%s/_workspace/%s-tools.code-workspace"\n' "$tgt" "$AK_DUPLICATE"
}

# When --duplicate is set by flag, reflect the duplication step plan in --list-steps / --plan-only.
[ -z "$AK_DUPLICATE" ] || set_dup_steps
if [ "$LIST_STEPS" -eq 1 ] || [ "$AK_PLAN_ONLY" -eq 1 ]; then
  show_plan
  [ -z "$EMIT_PLAN_JSON" ] || write_plan_json "$EMIT_PLAN_JSON"
  exit 0
fi
[ "$AK_SELF_TEST" -eq 0 ] || show_plan

fail_report() {
  local rc="${1:-1}"
  printf '\nAgent Kaizen installer failed (exit %s).\n' "$rc" >&2
  if [ -n "$CURRENT_STEP_NAME" ]; then printf '  Step : %d/%d %s\n' "$CURRENT_STEP" "${#STEPS[@]}" "$CURRENT_STEP_NAME" >&2; fi
  if [ -d "$LOG_ROOT" ]; then printf '  Logs : %s\n' "$LOG_ROOT" >&2; fi
  printf '  Rerun is safe; completed steps are detected and reused.\n' >&2
}
trap 'fail_report "$?"' ERR

# Offer duplication when an existing git-backed agent-kaizen is detected (or --duplicate was passed).
maybe_offer_duplication

if [ -n "$AK_DUPLICATE" ]; then
  set_dup_steps
  run_step preflight "Resolve DEVROOT and source project" step_dup_preflight
  run_step prereqs "Validate git and Python 3.12+" step_prereqs
  run_step create "Create the new project (link engine, copy customizable files)" step_dup_create
  run_step venv "Select or create the Python venv" step_dup_venv
  run_step wrapper "Write the project wrapper" step_dup_wrapper
  run_step db "Initialize the new project's Kaizen DB" step_dup_db
  run_step skills "Select and link skills" step_dup_skills
  run_step workspace "Generate VS Code workspace and launcher" step_dup_workspace
  run_step summary "Print completion summary" step_dup_summary
else
  run_step preflight "Resolve DEVROOT and installer mode" step_preflight
  run_step toolselect "Choose optional dependencies to install" select_dev_tools
  run_step prereqs "Install or validate git and Python 3.12+" step_prereqs
  run_step devtools "Install selected optional dev tools" install_optional_tools
  run_step devroot "Create DEVROOT folder" step_devroot
  run_step clone "Clone or update the Agent Kaizen repository" step_clone
  run_step handoff "Run the repo-local setup engine" step_handoff
fi
