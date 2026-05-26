#!/bin/bash
# Ensures VertexProxy (FastAPI) is listening on port 8000 for Graph Terminal HTTP calls.
# Restarts any existing listener so pipeline runs always pick up latest VertexProxy.py.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/Backend"

if lsof -Pi :8000 -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo "Restarting VertexProxy to load latest proxy code…" >&2
    pkill -f "uvicorn infrastructure.VertexProxy:app" 2>/dev/null || true
    sleep 1
fi

if [ ! -d "$BACKEND_DIR/.venv" ]; then
    echo "VertexProxy: Backend/.venv not found. Run ./run.sh to bootstrap dependencies." >&2
    exit 1
fi

cd "$BACKEND_DIR"
# shellcheck disable=SC1091
source .venv/bin/activate

echo "Starting VertexProxy on port 8000…" >&2
uvicorn infrastructure.VertexProxy:app --port 8000 --log-level warning >/dev/null 2>&1 &
sleep 2

if lsof -Pi :8000 -sTCP:LISTEN -t >/dev/null 2>&1; then
    exit 0
fi

echo "VertexProxy failed to bind port 8000." >&2
exit 1
