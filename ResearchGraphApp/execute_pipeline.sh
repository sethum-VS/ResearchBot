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

# Cleanup function to close all processors on backend ports
cleanup() {
    echo "🧹 Cleaning up backend processes..."
    # Kill process on port 8000 (VertexProxy)
    lsof -ti :8000 | xargs kill -9 2>/dev/null || true
    # We leave 3002 (Firecrawl) running as it is often a heavy Docker container, 
    # but the user can add it here if they want strict cleanup.
}

trap cleanup EXIT

# --- Activate Python Environment ---
cd "$BACKEND_DIR"
if [ ! -d ".venv" ]; then
    echo "⚠️  .venv not found. Please run ./run.sh first to bootstrap dependencies."
    exit 1
fi
source .venv/bin/activate

# --- Start Vertex AI Proxy ---
if lsof -Pi :8000 -sTCP:LISTEN -t >/dev/null ; then
    echo "✅ VertexProxy is already running on port 8000."
else
    echo "🚀 Starting Vertex AI Proxy..."
    uvicorn infrastructure.VertexProxy:app --port 8000 --log-level warning &
    sleep 2
fi

# --- Execute Pipeline (pass all args through to main.py) ---
python3 main.py "$@"
