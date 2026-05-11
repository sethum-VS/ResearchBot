#!/bin/bash

# ResearchBot — Execution Bridge
# Called by Swift PythonBridge or test scripts.
# Activates the python environment, manages the VertexProxy lifecycle,
# and forwards all arguments to Backend/main.py.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/Backend"

# Kill the Vertex proxy on exit.
trap 'if [ -n "$PROXY_PID" ]; then kill "$PROXY_PID" 2>/dev/null; fi' EXIT

# --- Activate Python Environment ---
cd "$BACKEND_DIR"
if [ ! -d ".venv" ]; then
    echo "⚠️  .venv not found. Please run ./run.sh first to bootstrap dependencies."
    exit 1
fi
source .venv/bin/activate

# --- Start Vertex AI Proxy ---
uvicorn infrastructure.VertexProxy:app --port 8000 --log-level warning &
PROXY_PID=$!
sleep 2

# --- Execute Pipeline (pass all args through to main.py) ---
python3 main.py "$@"
