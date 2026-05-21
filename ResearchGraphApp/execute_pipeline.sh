#!/bin/bash

# ResearchBot — Execution Bridge
# Called by Swift PythonBridge or test scripts.
# Activates the python environment, manages the VertexProxy lifecycle,
# and forwards all arguments to Backend/main.py.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/Backend"

# Load .env from the project root (one level above ResearchGraphApp/)
ENV_FILE="$SCRIPT_DIR/../.env"
if [ -f "$ENV_FILE" ]; then
    echo "📄 Loading environment variables from .env"
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

# --- Start Vertex AI Proxy (persists after this script exits) ---
"$SCRIPT_DIR/ensure_vertex_proxy.sh"
echo "✅ VertexProxy ready on port 8000."

# --- Execute Pipeline (pass all args through to main.py) ---
python3 main.py "$@"
