#!/bin/bash

# ResearchBot — Backend Sandbox Tester
# Automates infra checks and triggers the pipeline with a static topic.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/Backend"

echo "========================================="
echo "🧪 Starting ResearchBot Backend Sandbox"
echo "========================================="

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
set +e
./execute_pipeline.sh --idea "AI Agents for Automated Code Review"
PIPELINE_EXIT=$?
set -e

if [ "$PIPELINE_EXIT" -ne 0 ]; then
    echo "❌ Pipeline exited with code $PIPELINE_EXIT"
    exit "$PIPELINE_EXIT"
fi

# --- 4. Verify graph artifacts (nodes present in graph.json + graph.html) ---
echo "🔍 Verifying knowledge graph artifacts..."
"$BACKEND_DIR/.venv/bin/python3" <<VERIFY
import json
import re
import sys
from pathlib import Path

kb = Path("$SCRIPT_DIR/research_knowledge_base/graphify-out")
json_path = kb / "graph.json"
html_path = kb / "graph.html"

if not json_path.is_file():
    print(f"❌ Missing {json_path}")
    sys.exit(1)
if not html_path.is_file():
    print(f"❌ Missing {html_path}")
    sys.exit(1)

data = json.loads(json_path.read_text(encoding="utf-8"))
nodes = data.get("nodes") or []
links = data.get("links") or []
if not nodes:
    print("❌ graph.json has zero nodes")
    sys.exit(1)

if len(nodes) < 5:
    print(f"⚠️  graph.json has only {len(nodes)} nodes (expected richer graph from corpus)")

html = html_path.read_text(encoding="utf-8", errors="ignore")
html_ids = set(re.findall(r'"id"\s*:\s*"([^"]+)"', html))
json_ids = {n.get("id") for n in nodes if n.get("id")}
missing_in_html = json_ids - html_ids
extra_in_html = html_ids - json_ids

print(f"✅ graph.json: {len(nodes)} nodes, {len(links)} links")
print(f"✅ graph.html: {len(html_ids)} node id references in markup")

if missing_in_html:
    sample = sorted(missing_in_html)[:8]
    print(f"⚠️  {len(missing_in_html)} node id(s) in graph.json not found in graph.html (sample: {sample})")
    # Warn only — Graphify HTML layout may embed nodes differently
else:
    print("✅ All graph.json node ids appear in graph.html")

sys.exit(0)
VERIFY


echo "✅ Backend test completed successfully."
