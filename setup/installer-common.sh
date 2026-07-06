#!/usr/bin/env bash
# Shared installer helpers for Agent Kaizen POSIX setup scripts.

AK_NAME="Agent Kaizen setup"
AK_REPO_ROOT=""
AK_DEVROOT=""
AK_LOG_ROOT=""
AK_STATE_PATH=""
AK_PLAN_ONLY="${AK_PLAN_ONLY:-0}"
AK_NO_PROGRESS="${AK_NO_PROGRESS:-0}"
AK_NO_NETWORK="${AK_NO_NETWORK:-0}"
AK_NO_EXTERNAL="${AK_NO_EXTERNAL:-0}"
AK_NO_USER_ENV="${AK_NO_USER_ENV:-0}"
AK_ASSUME_YES="${AK_ASSUME_YES:-0}"
AK_NO_INPUT="${AK_NO_INPUT:-0}"
AK_SELF_TEST="${AK_SELF_TEST:-0}"
AK_COMMAND_COUNTER=0
AK_CURRENT_STEP=0
AK_CURRENT_STEP_NAME=""
AK_STEPS=()
AK_STEP_IDS=()
AK_STEP_RECORDS=()
# Per-step user-skip signal (set by ak_step_skipped; read by ak_run_step).
AK_STEP_SKIPPED=0
AK_STEP_SKIP_REASON=""
# Persisted-env accumulators (written to the generated env file by ak_write_env_file).
AK_ENV_PATHS=()
AK_ENV_VARS=()
# sysexits.h-style codes for scripted callers (see cli-design errors-and-exit-codes).
AK_EX_USAGE=64
AK_EX_UNAVAILABLE=69
AK_EX_SOFTWARE=70
AK_EX_TEMPFAIL=75

# ANSI status colors, only when stderr is a TTY and NO_COLOR is unset (cli-design color rules).
AK_C_OK=""; AK_C_SKIP=""; AK_C_WARN=""; AK_C_ERR=""; AK_C_RESET=""
ak_color_init() {
  if [ -z "${NO_COLOR:-}" ] && [ -t 2 ]; then
    AK_C_OK=$'\033[32m'; AK_C_SKIP=$'\033[90m'; AK_C_WARN=$'\033[33m'; AK_C_ERR=$'\033[31m'; AK_C_RESET=$'\033[0m'
  fi
}
ak_color_init

ak_die() {
  # Optional second arg = exit code (defaults to 1; use the AK_EX_* codes for scripted callers).
  printf 'ERROR: %s\n' "$1" >&2
  exit "${2:-1}"
}

ak_step_skipped() {
  # Mark the current step user-skipped; ak_run_step reports SKIPPED instead of OK.
  AK_STEP_SKIPPED=1
  AK_STEP_SKIP_REASON="${1:-}"
  [ -z "${1:-}" ] || printf 'Skipped: %s\n' "$1"
}

ak_step_obj() {
  printf '%s|%s' "$1" "$2"
}

ak_resolve_devroot() {
  local supplied="${1:-}"
  local repo_root="${2:?repo root required}"
  if [ -n "$supplied" ]; then
    (cd "$supplied" 2>/dev/null && pwd) || printf '%s\n' "$supplied"
    return
  fi
  if [ -n "${DEVROOT:-}" ]; then
    (cd "$DEVROOT" 2>/dev/null && pwd) || printf '%s\n' "$DEVROOT"
    return
  fi
  (cd "$repo_root/.." && pwd)
}

ak_init() {
  AK_NAME="$1"
  AK_REPO_ROOT="$2"
  AK_DEVROOT="$3"
  shift 3
  AK_STEPS=("$@")
  AK_STEP_IDS=()
  local entry id name
  for entry in "${AK_STEPS[@]}"; do
    id="${entry%%|*}"
    name="${entry#*|}"
    AK_STEP_IDS+=("$id")
    [ -n "$name" ] || ak_die "invalid empty setup step name for $id"
  done
  AK_LOG_ROOT="$AK_DEVROOT/agent-kaizen-setup/logs"
  AK_STATE_PATH="$AK_DEVROOT/agent-kaizen-setup/setup-state.json"
  if [ "$AK_PLAN_ONLY" -eq 0 ] && [ "$AK_SELF_TEST" -eq 0 ]; then
    mkdir -p "$AK_LOG_ROOT"
  fi
}

ak_json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

ak_show_plan() {
  printf '\n%s\n' "$AK_NAME"
  printf '  RepoRoot: %s\n' "$AK_REPO_ROOT"
  printf '  DEVROOT : %s\n' "$AK_DEVROOT"
  printf '  Logs    : %s\n\n' "$AK_LOG_ROOT"
  printf 'Planned setup steps:\n'
  local total="${#AK_STEPS[@]}"
  [ "$total" -gt 0 ] || total=1
  local i entry name from to
  for ((i=0; i<${#AK_STEPS[@]}; i++)); do
    entry="${AK_STEPS[$i]}"
    name="${entry#*|}"
    from=$(( i * 100 / total ))
    to=$(( (i + 1) * 100 / total ))
    printf '%2d. %-58s %3d%% -> %3d%%\n' "$((i + 1))" "$name" "$from" "$to"
  done
}

ak_write_plan_json() {
  local path="$1"
  local dir
  dir="$(dirname "$path")"
  mkdir -p "$dir"
  {
    printf '{\n'
    printf '  "installer": "%s",\n' "$(ak_json_escape "$AK_NAME")"
    printf '  "repoRoot": "%s",\n' "$(ak_json_escape "$AK_REPO_ROOT")"
    printf '  "devRoot": "%s",\n' "$(ak_json_escape "$AK_DEVROOT")"
    printf '  "logRoot": "%s",\n' "$(ak_json_escape "$AK_LOG_ROOT")"
    printf '  "planOnly": %s,\n' "$([ "$AK_PLAN_ONLY" -eq 1 ] && printf true || printf false)"
    printf '  "selfTest": %s,\n' "$([ "$AK_SELF_TEST" -eq 1 ] && printf true || printf false)"
    printf '  "safety": {"noNetwork": %s, "noExternalActions": %s, "noUserEnvWrites": %s, "noInput": %s},\n' \
      "$([ "$AK_NO_NETWORK" -eq 1 ] && printf true || printf false)" \
      "$([ "$AK_NO_EXTERNAL" -eq 1 ] && printf true || printf false)" \
      "$([ "$AK_NO_USER_ENV" -eq 1 ] && printf true || printf false)" \
      "$([ "$AK_NO_INPUT" -eq 1 ] && printf true || printf false)"
    printf '  "steps": [\n'
    local i entry id name comma
    for ((i=0; i<${#AK_STEPS[@]}; i++)); do
      entry="${AK_STEPS[$i]}"
      id="${entry%%|*}"
      name="${entry#*|}"
      comma=","
      [ "$i" -eq "$((${#AK_STEPS[@]} - 1))" ] && comma=""
      printf '    {"index": %d, "id": "%s", "name": "%s"}%s\n' "$((i + 1))" "$(ak_json_escape "$id")" "$(ak_json_escape "$name")" "$comma"
    done
    printf '  ]\n'
    printf '}\n'
  } > "$path"
  printf 'Plan JSON written: %s\n' "$path"
}

ak_save_state() {
  [ "$AK_PLAN_ONLY" -eq 0 ] || return 0
  [ "$AK_SELF_TEST" -eq 0 ] || return 0
  mkdir -p "$(dirname "$AK_STATE_PATH")"
  {
    printf '{\n'
    printf '  "installer": "%s",\n' "$(ak_json_escape "$AK_NAME")"
    printf '  "lastRun": "%s",\n' "$(date '+%Y-%m-%dT%H:%M:%S')"
    printf '  "repoRoot": "%s",\n' "$(ak_json_escape "$AK_REPO_ROOT")"
    printf '  "devRoot": "%s",\n' "$(ak_json_escape "$AK_DEVROOT")"
    printf '  "steps": [\n'
    local i comma
    for ((i=0; i<${#AK_STEP_RECORDS[@]}; i++)); do
      comma=","
      [ "$i" -eq "$((${#AK_STEP_RECORDS[@]} - 1))" ] && comma=""
      printf '    %s%s\n' "${AK_STEP_RECORDS[$i]}" "$comma"
    done
    printf '  ]\n'
    printf '}\n'
  } > "$AK_STATE_PATH"
}

ak_progress() {
  [ "$AK_NO_PROGRESS" -eq 0 ] || return 0
  local completed="$1" step="$2" status="$3" name="$4"
  local total="${#AK_STEPS[@]}"
  [ "$total" -gt 0 ] || total=1
  local pct=$(( completed * 100 / total )) c=""
  case "$status" in
    OK) c="$AK_C_OK" ;;
    SKIPPED) c="$AK_C_SKIP" ;;
    FAILED) c="$AK_C_ERR" ;;
    working) c="$AK_C_WARN" ;;
  esac
  printf '[%3d%%] [%d/%d] %s%s%s: %s\n' "$pct" "$step" "$total" "$c" "$status" "$AK_C_RESET" "$name" >&2
}

ak_step_index() {
  local needle="$1" i entry id
  for ((i=0; i<${#AK_STEPS[@]}; i++)); do
    entry="${AK_STEPS[$i]}"
    id="${entry%%|*}"
    if [ "$id" = "$needle" ]; then
      printf '%d\n' "$((i + 1))"
      return
    fi
  done
  printf '%d\n' "$((${AK_CURRENT_STEP:-0} + 1))"
}

ak_run_step() {
  local id="$1" name="$2"
  shift 2
  local step_no before start end elapsed status reason
  step_no="$(ak_step_index "$id")"
  AK_CURRENT_STEP="$step_no"
  AK_CURRENT_STEP_NAME="$name"
  before=$(( step_no - 1 ))
  start="$(date +%s)"
  ak_progress "$before" "$step_no" "working" "$name"
  printf '\n=== [%d/%d] %s ===\n' "$step_no" "${#AK_STEPS[@]}" "$name"
  status="OK"
  reason=""
  AK_STEP_SKIPPED=0
  AK_STEP_SKIP_REASON=""
  if [ "$AK_PLAN_ONLY" -eq 1 ]; then
    printf 'Plan-only: step not executed.\n'
    status="PLAN"
  elif [ "$AK_SELF_TEST" -eq 1 ]; then
    printf 'Self-test: validating step shape only; no external actions.\n'
    status="SELFTEST"
  else
    if ! "$@"; then
      status="FAILED"
      reason="command returned non-zero"
    elif [ "$AK_STEP_SKIPPED" -eq 1 ]; then
      status="SKIPPED"
      reason="$AK_STEP_SKIP_REASON"
    fi
  fi
  end="$(date +%s)"
  elapsed=$(( end - start ))
  AK_STEP_RECORDS+=("{\"id\":\"$(ak_json_escape "$id")\",\"name\":\"$(ak_json_escape "$name")\",\"status\":\"$status\",\"elapsedSeconds\":$elapsed,\"reason\":\"$(ak_json_escape "$reason")\"}")
  ak_save_state
  if [ "$status" = "FAILED" ]; then
    ak_progress "$before" "$step_no" "$status" "$name"
    return 1
  fi
  ak_progress "$step_no" "$step_no" "$status" "$name"
}

ak_assert_external_allowed() {
  if [ "$AK_PLAN_ONLY" -eq 1 ] || [ "$AK_SELF_TEST" -eq 1 ] || [ "$AK_NO_EXTERNAL" -eq 1 ]; then
    ak_die "External command blocked by installer safety mode: $*"
  fi
}

ak_assert_network_allowed() {
  if [ "$AK_PLAN_ONLY" -eq 1 ] || [ "$AK_SELF_TEST" -eq 1 ] || [ "$AK_NO_NETWORK" -eq 1 ]; then
    ak_die "Network request blocked by installer safety mode: $1"
  fi
}

ak_command_log_path() {
  AK_COMMAND_COUNTER=$((AK_COMMAND_COUNTER + 1))
  mkdir -p "$AK_LOG_ROOT"
  local leaf stamp
  leaf="$(basename "$1")"
  leaf="${leaf%.*}"
  leaf="$(printf '%s' "$leaf" | tr -c 'A-Za-z0-9_.-' '_')"
  stamp="$(date '+%Y%m%d-%H%M%S')"
  printf '%s/command-%s-%03d-%s.log' "$AK_LOG_ROOT" "$stamp" "$AK_COMMAND_COUNTER" "$leaf"
}

ak_tail_log() {
  local log="$1" lines="${2:-8}"
  [ -f "$log" ] || return 0
  tail -n "$lines" "$log" 2>/dev/null || true
}

ak_run() {
  local note=""
  local cwd=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --note) note="${2:-}"; shift 2 ;;
      --cwd) cwd="${2:-}"; shift 2 ;;
      --) shift; break ;;
      *) break ;;
    esac
  done
  [ $# -gt 0 ] || ak_die "ak_run called without a command"
  ak_assert_external_allowed "$@"
  local log start pid rc spinner frames elapsed
  log="$(ak_command_log_path "$1")"
  {
    printf 'COMMAND:'
    printf ' %q' "$@"
    printf '\nSTARTED: %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')"
    [ -z "$cwd" ] || printf 'WORKDIR: %s\n' "$cwd"
  } > "$log"
  if [ -n "$cwd" ]; then
    (cd "$cwd" && "$@" >> "$log" 2>&1) &
  else
    ("$@" >> "$log" 2>&1) &
  fi
  pid=$!
  start="$(date +%s)"
  frames='|/-\'
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$AK_NO_PROGRESS" -eq 0 ]; then
      elapsed=$(( $(date +%s) - start ))
      spinner="${frames:$((elapsed % 4)):1}"
      printf '\r[%s] step %d/%d elapsed %02d:%02d:%02d %s' "$spinner" "$AK_CURRENT_STEP" "${#AK_STEPS[@]}" "$((elapsed/3600))" "$(((elapsed/60)%60))" "$((elapsed%60))" "$AK_CURRENT_STEP_NAME" >&2
    fi
    sleep 1
  done
  wait "$pid"
  rc=$?
  printf '\nFINISHED: %s\nEXIT CODE: %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$rc" >> "$log"
  [ "$AK_NO_PROGRESS" -eq 1 ] || printf '\n' >&2
  if [ "$rc" -ne 0 ]; then
    printf 'Command failed with exit code %s. Log: %s\n' "$rc" "$log" >&2
    ak_tail_log "$log" 12 >&2
    return "$rc"
  fi
  printf '    command completed successfully. Log: %s\n' "$log"
}

ak_download() {
  local uri="$1"
  local out="$2"
  local min="${3:-1}"
  # Verify pillar: reuse a cached file that already meets the minimum size instead of refetching.
  if [ -f "$out" ]; then
    local sz
    sz="$(wc -c < "$out" 2>/dev/null || printf 0)"
    if [ "${sz:-0}" -ge "$min" ]; then
      printf '%s already downloaded: %s\n' "$(basename "$out")" "$out"
      return 0
    fi
  fi
  ak_assert_network_allowed "$uri"
  mkdir -p "$(dirname "$out")"
  if command -v curl >/dev/null 2>&1; then
    ak_run --note "Downloading with curl; percent and remaining bytes are shown when the server exposes a total." -- curl -L --fail --progress-bar -o "$out" "$uri"
  elif command -v wget >/dev/null 2>&1; then
    ak_run --note "Downloading with wget; percent and remaining bytes are shown when the server exposes a total." -- wget -O "$out" "$uri"
  else
    ak_die "Neither curl nor wget is available for download: $uri" "$AK_EX_UNAVAILABLE"
  fi
}

ak_resolve_python() {
  local supplied="${1:-}"
  local devroot="${2:?devroot required}"
  if [ -n "$supplied" ]; then
    printf '%s\n' "$supplied"
    return
  fi
  local shared="$devroot/Python/venvs/kaizen/bin/python"
  if [ -x "$shared" ]; then
    printf '%s\n' "$shared"
    return
  fi
  command -v python3 || command -v python || true
}

# ---------------------------------------------------------------------------
# User environment persistence (mirror of the Windows Add-AkPathEntry contract)
# ---------------------------------------------------------------------------
# A single generated file under DEVROOT holds all exports; each login shell rc
# sources it via one managed line. Rewriting the file each call keeps entries
# deduped and idempotent, and a warm re-run is a no-op.

ak_env_file() { printf '%s/agent-kaizen-setup/env.sh' "$AK_DEVROOT"; }

ak_write_env_file() {
  [ "$AK_NO_USER_ENV" -eq 0 ] || return 0
  local f v d
  f="$(ak_env_file)"
  mkdir -p "$(dirname "$f")"
  {
    printf '# Generated by Agent Kaizen setup. Safe to delete; regenerated on the next setup run.\n'
    for v in "${AK_ENV_VARS[@]:-}"; do
      [ -z "$v" ] && continue
      printf 'export %s="%s"\n' "${v%%=*}" "${v#*=}"
    done
    for d in "${AK_ENV_PATHS[@]:-}"; do
      [ -z "$d" ] && continue
      printf 'case ":$PATH:" in *":%s:"*) ;; *) export PATH="%s:$PATH" ;; esac\n' "$d" "$d"
    done
  } > "$f"
  ak_link_env_file
}

ak_link_env_file() {
  [ "$AK_NO_USER_ENV" -eq 0 ] || return 0
  local f rc line
  f="$(ak_env_file)"
  line="[ -f \"$f\" ] && . \"$f\"  # agent-kaizen"
  # Always seed ~/.profile; only touch ~/.bashrc / ~/.zshrc if they already exist.
  for rc in "$HOME/.profile" "$HOME/.bashrc" "$HOME/.zshrc"; do
    if [ "$rc" = "$HOME/.profile" ] || [ -f "$rc" ]; then
      if [ ! -f "$rc" ] || ! grep -qF '# agent-kaizen' "$rc" 2>/dev/null; then
        printf '\n%s\n' "$line" >> "$rc"
      fi
    fi
  done
}

ak_add_path_entry() {
  # Add DIR to the current process PATH now, and persist it (deduped) for future shells.
  # A missing DIR is a no-op, so calling this for a not-yet-created toolchain dir is safe.
  local dir="$1" d
  [ -n "$dir" ] || return 0
  [ -d "$dir" ] || return 0
  case ":$PATH:" in *":$dir:"*) ;; *) PATH="$dir:$PATH"; export PATH ;; esac
  for d in "${AK_ENV_PATHS[@]:-}"; do [ "$d" = "$dir" ] && return 0; done
  AK_ENV_PATHS+=("$dir")
  ak_write_env_file
}

ak_persist_env() {
  # Set NAME=VALUE now and persist it for future shells (replacing any prior value).
  local name="$1" value="$2" e kept=()
  [ -n "$name" ] || return 0
  export "$name=$value"
  for e in "${AK_ENV_VARS[@]:-}"; do
    [ -z "$e" ] && continue
    [ "${e%%=*}" = "$name" ] && continue
    kept+=("$e")
  done
  kept+=("$name=$value")
  AK_ENV_VARS=("${kept[@]}")
  ak_write_env_file
}

# ---------------------------------------------------------------------------
# Rust toolchain (for building pyturso from source when no wheel is available)
# ---------------------------------------------------------------------------
ak_ensure_rust() {
  # Ensure cargo is available, scoped under DEVROOT so a system Rust is never touched. Persists
  # CARGO_HOME/RUSTUP_HOME + the cargo bin. Returns 0 if cargo is usable afterwards.
  export CARGO_HOME="${CARGO_HOME:-$AK_DEVROOT/rust/.cargo}"
  export RUSTUP_HOME="${RUSTUP_HOME:-$AK_DEVROOT/rust/.rustup}"
  ak_add_path_entry "$CARGO_HOME/bin"
  if command -v cargo >/dev/null 2>&1; then
    ak_persist_env CARGO_HOME "$CARGO_HOME"
    ak_persist_env RUSTUP_HOME "$RUSTUP_HOME"
    printf 'Rust already available: %s\n' "$(command -v cargo)"
    return 0
  fi
  [ "$AK_NO_NETWORK" -eq 0 ] || return 1
  ak_assert_network_allowed 'Rust toolchain (rustup) download'
  mkdir -p "$CARGO_HOME" "$RUSTUP_HOME"
  local init
  init="$AK_DEVROOT/agent-kaizen-setup/downloads/rustup-init.sh"
  ak_download 'https://sh.rustup.rs' "$init" 1000
  ak_run --note "Installing the Rust toolchain (rustup, minimal profile) under DEVROOT." -- sh "$init" -y --default-toolchain stable --profile minimal --no-modify-path
  ak_add_path_entry "$CARGO_HOME/bin"
  ak_persist_env CARGO_HOME "$CARGO_HOME"
  ak_persist_env RUSTUP_HOME "$RUSTUP_HOME"
  command -v cargo >/dev/null 2>&1
}

# ---------------------------------------------------------------------------
# Skills: enumerate the owner's published skills from GitHub, install the ones
# selected (skill-drafting always) into the shared DEVROOT store, and link them
# into a project's .agents/.claude. Best-effort -- never fatal. Mirrors the
# Windows Ensure-Skills so a normal install offers the same skills menu.
# ---------------------------------------------------------------------------
ak_install_skills() {
  # $1 = project root (.agents/.claude live here); $2 = python; $3 = shared skills store dir.
  local repo="$1" py="$2" store="$3"
  mkdir -p "$store" "$repo/.agents/skills" "$repo/.claude/skills"
  if [ "$AK_NO_NETWORK" -eq 1 ] || [ "$AK_NO_EXTERNAL" -eq 1 ]; then
    printf 'skills: download blocked by safety mode; store left empty (run setup/link-skills.sh later).\n'
    return 0
  fi
  command -v git >/dev/null 2>&1 || { printf 'skills: git not found; skipping.\n'; return 0; }
  [ -x "$py" ] || py="$(command -v python3 || command -v python || true)"
  [ -n "$py" ] || { printf 'skills: python not found; skipping.\n'; return 0; }

  local list
  list="$("$py" - <<'PY' 2>/dev/null
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

  local -a names=() urls=() want=()
  local sname surl
  while IFS=$'\t' read -r sname surl; do
    [ -n "$sname" ] || continue
    names+=("$sname"); urls+=("$surl")
  done <<< "$list"
  if [ "${#names[@]}" -eq 0 ]; then
    printf 'skills: none found (GitHub unreachable?); store left empty.\n'
    return 0
  fi

  # skill-drafting is always installed; the rest come from the prompt (or none when headless).
  local i
  for i in "${!names[@]}"; do [ "${names[$i]}" = "skill-drafting" ] && want+=("$i"); done
  if [ "$AK_NO_INPUT" -eq 0 ] && [ -t 0 ]; then
    printf '\nAgent Kaizen skills (skill-drafting is always installed):\n' >&2
    for i in "${!names[@]}"; do
      [ "${names[$i]}" = "skill-drafting" ] && continue
      local mark='   '
      [ -d "$store/${names[$i]}" ] && mark='[*]'
      printf '  %2d) %s %s\n' "$((i + 1))" "$mark" "${names[$i]}" >&2
    done
    printf "Enter numbers to add (comma-separated), 'all', or Enter for skill-drafting only: " >&2
    local ans
    read -r ans || ans=""
    case "$ans" in
      all|ALL|a) for i in "${!names[@]}"; do want+=("$i"); done ;;
      "") : ;;
      *)
        local tok
        local -a sel
        IFS=', ' read -r -a sel <<< "$ans"
        for tok in "${sel[@]}"; do
          case "$tok" in ''|*[!0-9]*) continue ;; esac
          local idx=$((tok - 1))
          [ "$idx" -ge 0 ] && [ "$idx" -lt "${#names[@]}" ] && want+=("$idx")
        done ;;
    esac
  else
    printf 'skills: non-interactive; installing skill-drafting only.\n'
  fi

  local n url linked=0 m
  for i in "${want[@]}"; do
    n="${names[$i]}"
    url="${urls[$i]}"
    if [ ! -d "$store/$n" ] && [ -n "$url" ]; then
      ak_run --note "Cloning skill $n into the shared store." -- git clone --depth 1 "$url" "$store/$n" \
        || { printf '  [warn] clone failed: %s\n' "$n" >&2; continue; }
    fi
    [ -f "$store/$n/SKILL.md" ] || continue
    for m in "$repo/.agents/skills" "$repo/.claude/skills"; do
      [ -e "$m/$n" ] || ln -s "$store/$n" "$m/$n"
    done
    linked=$((linked + 1))
  done
  printf '%d skill(s) linked into .agents/skills and .claude/skills (store: %s).\n' "$linked" "$store"

  local builder="$store/skill-drafting/scripts/skill_builder.py"
  if [ -f "$builder" ]; then
    ak_run --note "Regenerating the skill index." -- "$py" "$builder" index "$repo/.claude/skills" --mirror "$repo/.agents/skills" \
      || printf '  [warn] skill index regeneration skipped.\n' >&2
  fi
}

# ---------------------------------------------------------------------------
# Failure report (mirror of the Windows Write-AkFailureReport)
# ---------------------------------------------------------------------------
ak_failure_report() {
  local rc="${1:-1}"
  printf '\n%sAgent Kaizen setup failed.%s\n' "$AK_C_ERR" "$AK_C_RESET" >&2
  [ -z "$AK_CURRENT_STEP_NAME" ] || printf '  Step  : %d/%d %s\n' "$AK_CURRENT_STEP" "${#AK_STEPS[@]}" "$AK_CURRENT_STEP_NAME" >&2
  [ -z "$AK_LOG_ROOT" ] || printf '  Logs  : %s\n' "$AK_LOG_ROOT" >&2
  [ -z "$AK_STATE_PATH" ] || printf '  State : %s\n' "$AK_STATE_PATH" >&2
  printf '  Nothing was force-overwritten. Fix the issue above and rerun; the installer is safe to repeat.\n' >&2
}
