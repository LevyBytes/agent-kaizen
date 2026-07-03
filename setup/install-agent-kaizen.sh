#!/usr/bin/env bash
# ============================================================================
#  install-agent-kaizen.sh  --  self-contained Linux/macOS installer
# ----------------------------------------------------------------------------
#  Download this ONE file and run it. On a bare machine it installs the
#  prerequisites for you (git + Python 3, via the system package manager),
#  clones the repo, then hands off to setup/setup.sh to build the local
#  harness. Re-running is safe; existing pieces are detected and skipped.
#
#  Usage:
#    bash install-agent-kaizen.sh [DEVROOT]
#      DEVROOT  parent dev folder (default: $HOME/dev)
#
#  Test hook: set AK_REPO_SOURCE to clone from a local path/mirror instead of
#  GitHub (e.g. AK_REPO_SOURCE=/path/to/local/clone).
#
#  Public repo: https://github.com/LevyBytes/agent-kaizen
# ============================================================================
set -euo pipefail

REPO_URL="${AK_REPO_SOURCE:-https://github.com/LevyBytes/agent-kaizen.git}"
REPO_NAME="agent-kaizen"
DEVROOT="${1:-${DEVROOT:-$HOME/dev}}"

echo "=== Agent Kaizen installer (Linux/macOS) ==="

# --- helpers ----------------------------------------------------------------
have() { command -v "$1" >/dev/null 2>&1; }

SUDO=""
need_sudo() {
  if [ "$(id -u)" -eq 0 ]; then SUDO=""; return 0; fi
  if have sudo; then SUDO="sudo"; return 0; fi
  return 1
}

# python3 present AND >= 3.10 AND the venv module available.
py_ok() {
  have python3 || return 1
  python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,10) else 1)' >/dev/null 2>&1 || return 1
  python3 -c 'import venv' >/dev/null 2>&1 || return 1
  return 0
}

manual_prereqs() {
  echo "ERROR: could not auto-install prerequisites ($1)." >&2
  echo "Install these, then run this installer again:" >&2
  echo "  git    : https://git-scm.com/download" >&2
  echo "  Python : https://www.python.org/downloads/   (3.10+, with venv + pip)" >&2
  exit 1
}

install_pkgs() {
  case "$PKG" in
    apt-get) need_sudo || manual_prereqs "no sudo"; $SUDO apt-get update && $SUDO apt-get install -y git python3 python3-venv python3-pip ;;
    dnf)     need_sudo || manual_prereqs "no sudo"; $SUDO dnf install -y git python3 python3-pip ;;
    yum)     need_sudo || manual_prereqs "no sudo"; $SUDO yum install -y git python3 python3-pip ;;
    pacman)  need_sudo || manual_prereqs "no sudo"; $SUDO pacman -S --needed --noconfirm git python ;;
    zypper)  need_sudo || manual_prereqs "no sudo"; $SUDO zypper --non-interactive install git python3 python3-venv python3-pip ;;
    brew)    brew install git python@3.12 ;;
    *)       manual_prereqs "no supported package manager" ;;
  esac
}

# --- 1. prerequisites -------------------------------------------------------
echo ""
echo "[1/3] Ensuring prerequisites (git, python3, venv, pip)"
PKG=""
for c in apt-get dnf yum pacman zypper brew; do
  if have "$c"; then PKG="$c"; break; fi
done

if have git && py_ok; then
  echo "      [ok] git: $(git --version)"
  echo "      [ok] python3: $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
else
  [ -n "$PKG" ] || manual_prereqs "no supported package manager"
  echo "      installing via $PKG ..."
  install_pkgs
  # macOS Homebrew may not be on PATH for this shell yet.
  if [ "$PKG" = "brew" ] && ! have git; then
    [ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)"
    [ -x /usr/local/bin/brew ] && eval "$(/usr/local/bin/brew shellenv)"
  fi
  have git || manual_prereqs "git"
  py_ok || manual_prereqs "python3 (3.10+ with venv)"
  echo "      [ok] git: $(git --version)"
  echo "      [ok] python3: $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
fi

# --- 2. DEVROOT -------------------------------------------------------------
echo ""
echo "[2/3] DEVROOT = $DEVROOT"
mkdir -p "$DEVROOT"

# --- 3. clone ---------------------------------------------------------------
echo ""
echo "[3/3] Cloning the repository"
REPO_PATH="$DEVROOT/$REPO_NAME"
if [ -f "$REPO_PATH/kaizen.py" ]; then
  echo "      a clone already exists at $REPO_PATH"
  if [ -d "$REPO_PATH/.git" ]; then
    git -C "$REPO_PATH" pull --ff-only || echo "      [warn] git pull did not fast-forward; leaving as-is."
  fi
elif [ -e "$REPO_PATH" ]; then
  if [ -d "$REPO_PATH/.git" ]; then
    git -C "$REPO_PATH" pull --ff-only || echo "      [warn] git pull did not fast-forward; leaving as-is."
  else
    echo "ERROR: a non-git folder already occupies $REPO_PATH; refusing to overwrite." >&2
    exit 1
  fi
else
  git clone "$REPO_URL" "$REPO_PATH"
fi
echo "      [ok] repo at $REPO_PATH"

# --- hand off to the repo's setup engine ------------------------------------
echo ""
echo "--- handing off to setup/setup.sh ---"
exec bash "$REPO_PATH/setup/setup.sh" "$DEVROOT"
