#!/usr/bin/env bash
# setup-iris.sh — one-command setup for the Iris preset (see IRIS.md).
#
# Configures an installed Iris as a fully
# self-hosted agent with local inference (Ollama), hardened approvals,
# and Obsidian-vault memory.
#
# Usage:
#   scripts/setup-iris.sh --vault ~/Documents/MyVault [options]
#
# Options:
#   --vault PATH     Path to your Obsidian vault (required for memory)
#   --model NAME     Ollama model to use (default: hermes3)
#   --folder NAME    Vault subfolder Iris writes into (default: Iris)
#   --base-url URL   OpenAI-compatible endpoint (default: http://127.0.0.1:11434/v1)
#   --skip-model     Don't touch model config (keep your current provider)
#   --dry-run        Print what would be done without doing it
#
# Idempotent: safe to re-run. Never overwrites an existing SOUL.md.

set -euo pipefail

VAULT=""
MODEL="hermes3"
FOLDER="Iris"
BASE_URL="http://127.0.0.1:11434/v1"
SKIP_MODEL=0
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --vault)     VAULT="${2:?--vault requires a path}"; shift 2 ;;
    --model)     MODEL="${2:?--model requires a name}"; shift 2 ;;
    --folder)    FOLDER="${2:?--folder requires a name}"; shift 2 ;;
    --base-url)  BASE_URL="${2:?--base-url requires a URL}"; shift 2 ;;
    --skip-model) SKIP_MODEL=1; shift ;;
    --dry-run)   DRY_RUN=1; shift ;;
    -h|--help)   grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

run() {
  if [ "$DRY_RUN" = 1 ]; then
    echo "[dry-run] $*"
  else
    "$@"
  fi
}

if command -v iris >/dev/null 2>&1; then
  CLI=iris
elif command -v hermes >/dev/null 2>&1; then
  CLI=hermes
else
  echo "error: 'iris' not found. Install from this repo first:" >&2
  echo "  scripts/install-iris.sh" >&2
  exit 1
fi

echo "── Iris setup ──────────────────────────────────────"

# 1. Local model -------------------------------------------------------------
if [ "$SKIP_MODEL" = 1 ]; then
  echo "• model: skipped (--skip-model)"
else
  if command -v ollama >/dev/null 2>&1; then
    if ! ollama list 2>/dev/null | awk '{print $1}' | grep -q "^${MODEL}"; then
      echo "• pulling ollama model '${MODEL}' (this can take a while)…"
      run ollama pull "$MODEL"
    fi
  else
    echo "⚠ ollama not found — configuring the endpoint anyway (${BASE_URL})."
    echo "  Install it later: curl -fsSL https://ollama.com/install.sh | sh"
  fi
  echo "• model: ${MODEL} via ${BASE_URL}"
  run "$CLI" config set model.provider ollama
  run "$CLI" config set model.base_url "$BASE_URL"
  run "$CLI" config set model.default "$MODEL"
fi

# 2. Hardening ---------------------------------------------------------------
echo "• approvals: manual (human-in-the-loop for dangerous commands)"
run "$CLI" config set approvals.mode manual

# 3. Obsidian memory ----------------------------------------------------------
if [ -n "$VAULT" ]; then
  VAULT_EXPANDED="${VAULT/#\~/$HOME}"
  if [ ! -d "$VAULT_EXPANDED" ]; then
    echo "error: vault not found: $VAULT_EXPANDED" >&2
    exit 1
  fi
  echo "• memory: obsidian → ${VAULT_EXPANDED} (folder: ${FOLDER}/)"
  run "$CLI" config set memory.provider obsidian
  run "$CLI" config set "plugins.obsidian-memory.vault_path" "$VAULT_EXPANDED"
  run "$CLI" config set "plugins.obsidian-memory.folder" "$FOLDER"
else
  echo "⚠ no --vault given — skipping Obsidian memory."
  echo "  Enable later: $CLI memory setup   (select 'obsidian')"
fi

# 4. Persona ------------------------------------------------------------------
SOUL="$HERMES_HOME/SOUL.md"
if [ -f "$SOUL" ] && grep -q "Iris" "$SOUL" 2>/dev/null; then
  echo "• persona: SOUL.md already mentions Iris — left untouched"
elif [ -f "$SOUL" ] && [ -s "$SOUL" ]; then
  echo "⚠ persona: $SOUL exists — not overwriting. Paste the Iris persona"
  echo "  from IRIS.md yourself if you want it."
else
  echo "• persona: writing Iris SOUL.md"
  if [ "$DRY_RUN" = 1 ]; then
    echo "[dry-run] write $SOUL"
  else
    mkdir -p "$HERMES_HOME"
    cat > "$SOUL" <<'EOF'
# Iris

You are Iris — messenger of the gods, a self-hosted personal agent running
entirely on your user's own hardware. Nothing you read or write leaves this
machine.

Voice: swift, luminous, direct. You carry messages faithfully — you say what
you know, what you don't, and what you'd try next. No flattery, no filler.

Memory: your long-term memory is the user's Obsidian vault. Search it before
answering questions about the user, their projects, or past decisions.
Persist durable knowledge as linked notes so the knowledge graph deepens —
like a rainbow, every note you leave should connect two points.
EOF
  fi
fi

echo "────────────────────────────────────────────────────"
echo "Done. Start your agent with:  $CLI"
