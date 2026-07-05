#!/usr/bin/env bash
# Self-contained Linux/macOS installer for Agent Kaizen.
set -euo pipefail
# Keep apt/dpkg from opening interactive prompts (e.g. tzdata) on a bare machine.
export DEBIAN_FRONTEND=noninteractive

REPO_URL="${AK_REPO_SOURCE:-https://github.com/LevyBytes/agent-kaizen.git}"
REPO_NAME="agent-kaizen"
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
LOG_ROOT=""
COMMAND_COUNTER=0
CURRENT_STEP=0
CURRENT_STEP_NAME=""
STEPS=(
  "preflight|Resolve DEVROOT and installer mode"
  "prereqs|Install or validate git and Python 3.12+"
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
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --devroot) DEVROOT_ARG="${2:-}"; shift ;;
    --ref) REF="${2:-}"; shift ;;
    --repo-source) REPO_URL="${2:-}"; shift ;;
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

json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
have() { command -v "$1" >/dev/null 2>&1; }
have_cc() { have cc || have gcc || have clang; }
die() { printf 'ERROR: %s\n' "$1" >&2; exit "${2:-1}"; }

show_plan() {
  printf '\nAgent Kaizen installer (Linux/macOS)\n'
  printf '  DEVROOT: %s\n' "$DEVROOT"
  printf '  Source : %s\n' "$REPO_URL"
  [ -z "$REF" ] || printf '  Ref    : %s\n' "$REF"
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
  wait "$pid"
  rc=$?
  printf '\nFINISHED: %s\nEXIT CODE: %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$rc" >> "$log"
  [ "$AK_NO_PROGRESS" -eq 1 ] || printf '\n' >&2
  if [ "$rc" -ne 0 ]; then
    printf 'Command failed with exit code %s. Log: %s\n' "$rc" "$log" >&2
    tail -n 12 "$log" >&2 || true
    return "$rc"
  fi
  printf '    command completed successfully. Log: %s\n' "$log"
}

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
  ensure_build_toolchain
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
  bash "${args[@]}"
}

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

run_step preflight "Resolve DEVROOT and installer mode" step_preflight
run_step prereqs "Install or validate git and Python 3.12+" step_prereqs
run_step devroot "Create DEVROOT folder" step_devroot
run_step clone "Clone or update the Agent Kaizen repository" step_clone
run_step handoff "Run the repo-local setup engine" step_handoff
