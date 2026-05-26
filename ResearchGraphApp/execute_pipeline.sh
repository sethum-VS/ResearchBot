#!/bin/bash

# ResearchBot — Execution Bridge
# Called by Swift PythonBridge or test scripts.
# Activates the python environment, manages the VertexProxy lifecycle,
# and forwards all arguments to Backend/main.py.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/Backend"

# Load .env from Application Support (set by Swift) or dev repo root.
if [ -n "${APP_SUPPORT_DIR:-}" ]; then
    ENV_FILE="${APP_SUPPORT_DIR}/.env"
else
    ENV_FILE="${HOME}/Library/Application Support/AutonomousResearchGraph/.env"
fi

if [ ! -f "$ENV_FILE" ]; then
    DEV_ENV_FILE="$SCRIPT_DIR/../.env"
    if [ -f "$DEV_ENV_FILE" ]; then
        ENV_FILE="$DEV_ENV_FILE"
    fi
fi

if [ -f "$ENV_FILE" ]; then
    echo "📄 Loading environment variables from $ENV_FILE"
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
else
    echo "⚠️  .env not found at $ENV_FILE"
fi

# Pipeline exit cleanup — VertexProxy on :8000 is intentionally left running so the
# Swift Graph Terminal can POST /api/graph/query|path after the run completes.
cleanup() {
    echo "🧹 Pipeline finished (VertexProxy left running on :8000 for Graph Console)."
}

trap cleanup EXIT

# --- Activate Python Environment ---
cd "$BACKEND_DIR"
if [ ! -d ".venv" ]; then
    echo "⚠️  .venv not found. Please run ./run.sh first to bootstrap dependencies."
    exit 1
fi
source .venv/bin/activate

# --- Start Vertex AI Proxy (pipeline only; export does not need it) ---
SKIP_VERTEX=0
for arg in "$@"; do
    if [ "$arg" = "export_to_workspace" ]; then
        SKIP_VERTEX=1
        break
    fi
done

if [ "$SKIP_VERTEX" -eq 0 ]; then
    "$SCRIPT_DIR/ensure_vertex_proxy.sh"
    echo "✅ VertexProxy ready on port 8000."
else
    echo "⏭️  Skipping VertexProxy for workspace export."
fi

# --- Execute Backend (pass all args through to main.py) ---
# Full pipeline (default):
#   python3 main.py --idea "topic" [--url "..."]
# Google Workspace export:
#   python3 main.py --command export_to_workspace --session-id "session_..." --kb-root "/abs/path/research_knowledge_base"
export PYTHONUNBUFFERED=1
python3 -u main.py "$@"
