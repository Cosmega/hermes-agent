#!/usr/bin/env bash
# install-iris.sh — self-contained installer for THIS repository.
#
# Unlike the upstream one-liner (which downloads from nousresearch.com),
# this installs Iris entirely from the repo you are holding: your clone
# is the single source of truth for code, updates, and docs.
#
# One-command install (no clone needed — it clones for you, into ~/iris):
#   curl -fsSL https://raw.githubusercontent.com/Cosmega/hermes-agent/main/scripts/install-iris.sh | bash
#
# Or from a clone of your repo:
#   git clone https://github.com/Cosmega/hermes-agent.git iris && cd iris
#   scripts/install-iris.sh
#
# Env overrides for the one-command mode:
#   IRIS_REPO_URL   repo to clone (default: https://github.com/Cosmega/hermes-agent.git)
#   IRIS_DIR        where to clone (default: ~/iris)
#
# What it does — and nothing else:
#   1. Installs the uv toolchain if missing (the only non-repo download,
#      besides Python packages from PyPI declared in pyproject.toml)
#   2. Creates .venv in the repo and installs the repo into it (editable)
#   3. Puts `iris` and `hermes` launchers in ~/.local/bin
#   4. Marks this install as standalone so `iris update` pulls from YOUR
#      origin and never suggests linking the upstream repo
#
# Options:
#   --lite     install core only (skip the [all] extras — faster, fewer tools)
#   --home DIR agent home directory (default: ~/.hermes; config/memory/state)

set -euo pipefail

LITE=0
AGENT_HOME="${HERMES_HOME:-$HOME/.hermes}"
while [ $# -gt 0 ]; do
  case "$1" in
    --lite) LITE=1; shift ;;
    --home) AGENT_HOME="${2:?--home requires a directory}"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

# Resolve the repo root. When piped from curl there is no script file on
# disk, so clone the repo and re-exec the installer from inside it.
SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"
if [ -n "$SCRIPT_SOURCE" ] && [ -f "$SCRIPT_SOURCE" ] \
   && [ -f "$(cd "$(dirname "$SCRIPT_SOURCE")/.." && pwd)/pyproject.toml" ]; then
  REPO_ROOT="$(cd "$(dirname "$SCRIPT_SOURCE")/.." && pwd)"
else
  REPO_URL="${IRIS_REPO_URL:-https://github.com/Cosmega/hermes-agent.git}"
  REPO_ROOT="${IRIS_DIR:-$HOME/iris}"
  if [ -d "$REPO_ROOT/.git" ]; then
    echo "• existing clone at $REPO_ROOT — pulling latest…"
    git -C "$REPO_ROOT" pull --ff-only || true
  else
    echo "• cloning $REPO_URL → $REPO_ROOT…"
    git clone "$REPO_URL" "$REPO_ROOT"
  fi
  ARGS=()
  [ "$LITE" = 1 ] && ARGS+=(--lite)
  [ "$AGENT_HOME" != "${HERMES_HOME:-$HOME/.hermes}" ] && ARGS+=(--home "$AGENT_HOME")
  exec bash "$REPO_ROOT/scripts/install-iris.sh" ${ARGS[@]+"${ARGS[@]}"}
fi
cd "$REPO_ROOT"

echo "── Iris standalone install ─────────────────────────"
echo "• repo:  $REPO_ROOT"
echo "• home:  $AGENT_HOME"

# 1. uv toolchain -------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  echo "• installing uv (Python package manager)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# 2. venv + editable install ---------------------------------------------------
if [ ! -d .venv ]; then
  echo "• creating .venv (Python 3.11)…"
  uv venv .venv --python 3.11
fi
if [ "$LITE" = 1 ]; then
  echo "• installing core (editable, --lite)…"
  uv pip install --python .venv/bin/python -e .
else
  echo "• installing with all extras (editable)…"
  uv pip install --python .venv/bin/python -e ".[all]" || {
    echo "⚠ [all] extras failed (platform-specific dep?) — falling back to core."
    uv pip install --python .venv/bin/python -e .
  }
fi

# 3. launchers ------------------------------------------------------------------
BIN="$HOME/.local/bin"
mkdir -p "$BIN"
for name in iris hermes; do
  cat > "$BIN/$name" <<EOF
#!/usr/bin/env bash
export HERMES_HOME="\${HERMES_HOME:-$AGENT_HOME}"
exec "$REPO_ROOT/.venv/bin/$name" "\$@"
EOF
  chmod +x "$BIN/$name"
done
echo "• launchers: $BIN/iris and $BIN/hermes"
case ":$PATH:" in
  *":$BIN:"*) : ;;
  *) echo "⚠ $BIN is not on your PATH — add:  export PATH=\"$BIN:\$PATH\"" ;;
esac

# 4. standalone marker ------------------------------------------------------------
mkdir -p "$AGENT_HOME"
touch "$AGENT_HOME/.skip_upstream_prompt"
echo "• standalone: 'iris update' will pull from this repo's origin only"

echo "────────────────────────────────────────────────────"
echo "Installed. Next:"
echo "  scripts/setup-iris.sh --vault ~/Documents/YourVault   # model+memory+persona"
echo "  iris                                                  # start chatting"
