#!/bin/bash

# ResearchBot — Backend Sandbox Tester
# Automates infra checks and triggers the pipeline with a static topic.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/Backend"

echo "========================================="
echo "🧪 Starting ResearchBot Backend Sandbox"
echo "========================================="

# Remove stray graphify-out folders outside session workspaces.
if [ -x "$SCRIPT_DIR/cleanup_repo_layout.sh" ]; then
    "$SCRIPT_DIR/cleanup_repo_layout.sh"
fi

# Cleanup function to close all processors on backend ports
cleanup() {
    echo "🧹 Finalizing cleanup..."
    # The execute_pipeline.sh handles port 8000.
    # We only stop Firecrawl if the user explicitly wants to "clear all".
    if [ "$FIRECRAWL_STARTED" = true ]; then
        echo "🐳 Stopping Firecrawl Docker containers..."
        cd "$FIRECRAWL_DIR" && docker compose down && cd "$SCRIPT_DIR"
    fi
}

trap cleanup EXIT

# --- 1. GCP Auth Auto-Remedy ---
echo "☁️  Checking Google Cloud ADC..."
GCP_CREDS="$HOME/.config/gcloud/application_default_credentials.json"
if [ ! -f "$GCP_CREDS" ]; then
    echo "⚠️  GCP ADC not found. Prompting for authentication..."
    gcloud auth application-default login
else
    echo "✅ GCP credentials found."
fi

# --- 2. Firecrawl Auto-Remedy ---
echo "🔥 Checking Firecrawl local instance..."
FIRECRAWL_DIR="$BACKEND_DIR/infrastructure/firecrawl"
if ! curl -s http://localhost:3002 > /dev/null; then
    echo "⚠️  Firecrawl is down. Attempting bootstrap..."

    if [ ! -d "$FIRECRAWL_DIR" ]; then
        echo "📥 Cloning Firecrawl from GitHub..."
        mkdir -p "$(dirname "$FIRECRAWL_DIR")"
        git clone https://github.com/mendableai/firecrawl.git "$FIRECRAWL_DIR"

        echo "🔧 Configuring Firecrawl (disabling authentication)..."
        cd "$FIRECRAWL_DIR"
        cp apps/api/.env.example apps/api/.env
        if [[ "$OSTYPE" == "darwin"* ]]; then
            sed -i '' 's/USE_DB_AUTHENTICATION=true/USE_DB_AUTHENTICATION=false/g' apps/api/.env
        else
            sed -i 's/USE_DB_AUTHENTICATION=true/USE_DB_AUTHENTICATION=false/g' apps/api/.env
        fi
        cd "$SCRIPT_DIR"
    fi

    echo "🐳 Starting Firecrawl via Docker Compose..."
    cd "$FIRECRAWL_DIR"
    if docker compose up -d; then
        FIRECRAWL_STARTED=true
        cd "$SCRIPT_DIR"
        echo "⏳ Waiting for Firecrawl to initialize (15s)..."
        sleep 15
    else
        cd "$SCRIPT_DIR"
        echo "⚠️  Firecrawl Docker failed (is Docker Desktop running?). Continuing with degraded web scraping."
    fi
else
    echo "✅ Firecrawl is running at localhost:3002."
fi

# --- 3. Execute Pipeline via Bridge ---
echo "🚀 Triggering Pipeline via execute_pipeline.sh..."
cd "$SCRIPT_DIR"
PIPELINE_LOG="$(mktemp)"
set +e
./execute_pipeline.sh --idea "AI Agents for Automated Code Review" 2>&1 | tee "$PIPELINE_LOG"
PIPELINE_EXIT=${PIPESTATUS[0]}
set -e

if [ "$PIPELINE_EXIT" -ne 0 ]; then
    echo "❌ Pipeline exited with code $PIPELINE_EXIT"
    rm -f "$PIPELINE_LOG"
    exit "$PIPELINE_EXIT"
fi

# --- 4. Verify session-isolated graph artifacts ---
echo "🔍 Verifying knowledge graph artifacts (session workspace)..."
"$BACKEND_DIR/.venv/bin/python3" <<VERIFY
import json
import re
import sys
from pathlib import Path

log_path = Path("$PIPELINE_LOG")
raw = log_path.read_text(encoding="utf-8", errors="replace")

start = raw.find("---PIPELINE_RESULT_START---")
end = raw.find("---PIPELINE_RESULT_END---")
if start == -1 or end == -1 or start >= end:
    print("❌ Pipeline result markers not found in stdout")
    sys.exit(1)

payload = json.loads(raw[start + len("---PIPELINE_RESULT_START---"):end].strip())
if payload.get("status") != "success":
    print(f"❌ Pipeline status: {payload.get('status')} — {payload.get('message')}")
    sys.exit(1)

session_path = payload.get("session_path")
session_id = payload.get("session_id")
graph_path = payload.get("graph_path")

if not session_path:
    print("❌ Pipeline result missing session_path (session isolation contract)")
    sys.exit(1)

session_dir = Path(session_path)
graphify_out = session_dir / "graphify-out"
json_path = graphify_out / "graph.json"
html_path = graphify_out / "graph.html"

print(f"✅ Session: {session_id}")
print(f"✅ Workspace: {session_dir}")

if graph_path and Path(graph_path).is_file():
    print(f"✅ graph_path in payload: {graph_path}")
else:
    print(f"⚠️  graph_path missing or not on disk: {graph_path}")

if not json_path.is_file():
    print(f"❌ Missing {json_path}")
    sys.exit(1)
if not html_path.is_file():
    print(f"❌ Missing {html_path}")
    sys.exit(1)

manifest = session_dir / "session_manifest.json"
gap_json = session_dir / "academic_gap_analysis.json"
if manifest.is_file():
    print(f"✅ session_manifest.json present")
else:
    print(f"⚠️  session_manifest.json missing")
if gap_json.is_file():
    print(f"✅ academic_gap_analysis.json present")
else:
    print(f"⚠️  academic_gap_analysis.json missing")

data = json.loads(json_path.read_text(encoding="utf-8"))
nodes = data.get("nodes") or []
links = data.get("links") or []
if not nodes:
    print("❌ graph.json has zero nodes")
    sys.exit(1)

html = html_path.read_text(encoding="utf-8", errors="ignore")
json_ids = {n.get("id") for n in nodes if n.get("id")}
html_ids_regex = set(re.findall(r'"id"\s*:\s*"([^"]+)"', html))
raw_ids = set()
raw_match = re.search(r"const RAW_NODES = (\[.*?\]);", html, re.DOTALL)
if not raw_match:
    print("❌ graph.html is missing const RAW_NODES (vis-network payload)")
    sys.exit(1)
try:
    raw_nodes = json.loads(raw_match.group(1))
    raw_ids = {n.get("id") for n in raw_nodes if n.get("id")}
except json.JSONDecodeError as e:
    print(f"❌ graph.html RAW_NODES is not valid JSON: {e}")
    sys.exit(1)

missing_in_raw = json_ids - raw_ids
missing_in_regex = json_ids - html_ids_regex

print(f"✅ graph.json: {len(nodes)} nodes, {len(links)} links")
print(f"✅ graph.html RAW_NODES: {len(raw_ids)} vis nodes")

if missing_in_raw:
    sample = sorted(missing_in_raw)[:8]
    print(f"❌ {len(missing_in_raw)} graph.json node id(s) missing from RAW_NODES (sample: {sample})")
    sys.exit(1)

if missing_in_regex:
    print(f"⚠️  {len(missing_in_regex)} id(s) in graph.json not matched by generic regex (RAW_NODES check passed)")

print("✅ All graph.json node ids appear in graph.html RAW_NODES")

if len(nodes) < 5:
    print(f"⚠️  graph.json has only {len(nodes)} nodes (expected richer graph from corpus)")

# Count URLRefiner docs in this session only
scrape_dir = session_dir / "agent_scrapes"
urlrefiner_count = 0
if scrape_dir.is_dir():
    urlrefiner_count = sum(
        1 for f in scrape_dir.iterdir()
        if f.suffix == ".md" and "_urlrefiner" in f.name.lower()
    )
print(f"✅ URLRefiner docs in session: {urlrefiner_count}")

graphify_err = (payload.get("graphify") or {}).get("error")
if graphify_err:
    print(f"⚠️  graphify reported error: {graphify_err}")

sys.exit(0)
VERIFY

# --- 5. Verify Graph Terminal API (VertexProxy must stay up after pipeline) ---
echo "🔍 Verifying Graph Explorer API on :8000..."
SESSION_ID="$("$BACKEND_DIR/.venv/bin/python3" -c "
import json, re, sys
from pathlib import Path
raw = Path('$PIPELINE_LOG').read_text(encoding='utf-8', errors='replace')
start = raw.find('---PIPELINE_RESULT_START---')
end = raw.find('---PIPELINE_RESULT_END---')
if start == -1 or end == -1:
    sys.exit(1)
payload = json.loads(raw[start + len('---PIPELINE_RESULT_START---'):end].strip())
print(payload.get('session_id') or '')
")"

if [ -z "$SESSION_ID" ]; then
    echo "❌ Cannot verify graph API — session_id missing from pipeline result"
    rm -f "$PIPELINE_LOG"
    exit 1
fi

set +e
"$BACKEND_DIR/.venv/bin/python3" <<GRAPH_API
import json
import sys
import urllib.error
import urllib.request

session_id = """$SESSION_ID"""

def get(path: str) -> tuple[int, dict]:
    req = urllib.request.Request(f"http://localhost:8000{path}", method="GET")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))

def post(path: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"http://localhost:8000{path}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))

try:
    status, payload = get("/api/graph/sessions")
except urllib.error.URLError as e:
    print(f"❌ VertexProxy not reachable on :8000 — {e}")
    sys.exit(1)

if status != 200:
    print(f"❌ /api/graph/sessions returned HTTP {status}")
    sys.exit(1)

sessions = payload.get("sessions") or []
print(f"✅ /api/graph/sessions — {len(sessions)} session(s)")

if session_id not in sessions:
    print(f"⚠️  Active session {session_id} not listed (may still resolve by id)")

status, result = post(
    "/api/graph/query",
    {
        "session_id": session_id,
        "question": "List three node labels from this graph.",
    },
)

if status != 200 or not result.get("ok"):
    err = result.get("error") or f"HTTP {status}"
    print(f"❌ /api/graph/query failed — {err}")
    sys.exit(1)

stdout = (result.get("stdout") or "").strip()
if not stdout:
    print("⚠️  /api/graph/query returned empty stdout (graphify may need API keys)")
else:
    preview = stdout[:200].replace("\n", " ")
    print(f"✅ /api/graph/query — {len(stdout)} chars (preview: {preview}…)")

sys.exit(0)
GRAPH_API

GRAPH_API_EXIT=$?
set -e
rm -f "$PIPELINE_LOG"

if [ "$GRAPH_API_EXIT" -ne 0 ]; then
    echo "❌ Graph Explorer API verification failed"
    exit "$GRAPH_API_EXIT"
fi

echo "✅ Backend test completed successfully."
