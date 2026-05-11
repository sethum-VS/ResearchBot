#!/bin/bash

# ResearchBot — Backend Sandbox Tester
# Automates infra checks and triggers the pipeline with a static topic.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/Backend"

echo "========================================="
echo "🧪 Starting ResearchBot Backend Sandbox"
echo "========================================="

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
if ! curl -s http://localhost:3002 > /dev/null; then
    echo "⚠️  Firecrawl is down. Attempting bootstrap..."

    FIRECRAWL_DIR="$BACKEND_DIR/infrastructure/firecrawl"

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
    docker compose up -d
    cd "$SCRIPT_DIR"
    echo "⏳ Waiting for Firecrawl to initialize (15s)..."
    sleep 15
else
    echo "✅ Firecrawl is running at localhost:3002."
fi

# --- 3. Execute Pipeline via Bridge ---
echo "🚀 Triggering Pipeline via execute_pipeline.sh..."
cd "$SCRIPT_DIR"
./execute_pipeline.sh --idea "AI Agents for Automated Code Review"

echo "✅ Backend test completed successfully."
