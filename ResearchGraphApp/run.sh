#!/bin/bash

# ResearchBot — Master App Launcher (Xcode Bootstrapper)
# Performs all infra checks, builds the macOS app via xcodebuild,
# and launches the compiled .app bundle.
#
# Usage:  ./run.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/Backend"
APP_DIR="$SCRIPT_DIR/App"
SCHEME="ResearchBot"
DERIVED_DATA_PATH="$SCRIPT_DIR/build"

echo "========================================="
echo "🚀 Starting ResearchBot Pipeline Setup"
echo "========================================="

if [ -x "$SCRIPT_DIR/cleanup_repo_layout.sh" ]; then
    "$SCRIPT_DIR/cleanup_repo_layout.sh"
fi

# --- 0. Backend Python venv (required by execute_pipeline.sh / VertexProxy) ---
echo "🐍 Checking Backend Python environment..."
if [ ! -d "$BACKEND_DIR/.venv" ]; then
    echo "📦 Creating Backend/.venv and installing requirements..."
    python3 -m venv "$BACKEND_DIR/.venv"
    # shellcheck disable=SC1091
    source "$BACKEND_DIR/.venv/bin/activate"
    pip install -r "$BACKEND_DIR/requirements.txt"
    echo "✅ Backend dependencies installed."
else
    echo "✅ Backend .venv found."
fi

# --- 0b. Graphify CLI Tool Setup ---
echo "📊 Checking for Graphify CLI..."
GRAPHIFY_BIN=""
if command -v graphify &> /dev/null; then
    GRAPHIFY_BIN=$(command -v graphify)
elif [ -x "$HOME/.local/bin/graphify" ]; then
    GRAPHIFY_BIN="$HOME/.local/bin/graphify"
elif [ -x "/opt/homebrew/bin/graphify" ]; then
    GRAPHIFY_BIN="/opt/homebrew/bin/graphify"
elif [ -x "/usr/local/bin/graphify" ]; then
    GRAPHIFY_BIN="/usr/local/bin/graphify"
fi

if [ -n "$GRAPHIFY_BIN" ]; then
    echo "✅ Graphify CLI found at $GRAPHIFY_BIN"
    # Ensure it's in the current script PATH if it was found in ~/.local/bin but not in PATH
    if ! command -v graphify &> /dev/null && [ "$GRAPHIFY_BIN" = "$HOME/.local/bin/graphify" ]; then
        export PATH="$HOME/.local/bin:$PATH"
    fi
else
    echo "⚠️  Graphify CLI not found. Attempting automatic installation of 'graphifyy'..."
    if command -v uv &> /dev/null; then
        echo "📦 Installing graphifyy via uv..."
        uv tool install graphifyy
        export PATH="$HOME/.local/bin:$PATH"
    elif command -v pipx &> /dev/null; then
        echo "📦 Installing graphifyy via pipx..."
        pipx install graphifyy
        export PATH="$HOME/.local/bin:$PATH"
    else
        echo "📦 Installing graphifyy via pip3..."
        pip3 install --user graphifyy
        export PATH="$HOME/.local/bin:$PATH"
        USER_PYTHON_BIN=$(python3 -m site --user-base 2>/dev/null)/bin
        if [ -d "$USER_PYTHON_BIN" ]; then
            export PATH="$USER_PYTHON_BIN:$PATH"
        fi
    fi

    # Re-verify installation status
    if command -v graphify &> /dev/null || [ -x "$HOME/.local/bin/graphify" ]; then
        echo "✅ Graphify CLI successfully installed."
        if [ -x "$HOME/.local/bin/graphify" ] && ! echo "$PATH" | grep -q "$HOME/.local/bin"; then
            export PATH="$HOME/.local/bin:$PATH"
        fi
    else
        echo "❌ Warning: Graphify installation completed, but 'graphify' executable is still not found in PATH."
        echo "   Please manually run: pip install graphifyy and add its directory to your PATH."
    fi
fi

# Double check if PATH needs permanent update advice
if [ -x "$HOME/.local/bin/graphify" ] && ! echo "$PATH" | grep -q "$HOME/.local/bin"; then
    echo "⚠️  Note: 'graphify' is installed in ~/.local/bin, which is not in your shell's permanent PATH."
    echo "   We have temporarily added it for this session, but you should add it to your shell profile permanently:"
    echo "   echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc"
fi


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
    FIRECRAWL_UP=false
    for attempt in 1 2; do
        if [ "$attempt" -gt 1 ]; then
            echo "🔄 Retrying Firecrawl bootstrap (attempt $attempt)..."
            docker compose pull --ignore-pull-failures 2>/dev/null || true
        fi
        if docker compose up -d; then
            FIRECRAWL_UP=true
            break
        fi
    done
    cd "$SCRIPT_DIR"
    if [ "$FIRECRAWL_UP" = true ]; then
        echo "⏳ Waiting for Firecrawl to initialize (15s)..."
        sleep 15
    else
        echo "⚠️  Firecrawl Docker failed. App will run with degraded web scraping."
    fi
else
    echo "✅ Firecrawl is running at localhost:3002."
fi

# --- 3. Xcode Build ---
echo "🍏 Building macOS App ($SCHEME)..."
cd "$APP_DIR"

if command -v xcbeautify &> /dev/null; then
    xcodebuild build \
        -project "ResearchBot.xcodeproj" \
        -scheme "$SCHEME" \
        -derivedDataPath "../build" \
        -destination 'platform=macOS,arch=arm64' | xcbeautify
else
    echo "ℹ️  xcbeautify not found, using standard output..."
    xcodebuild build \
        -project "ResearchBot.xcodeproj" \
        -scheme "$SCHEME" \
        -derivedDataPath "../build" \
        -destination 'platform=macOS,arch=arm64'
fi

# --- 4. Launch App ---
echo "🚀 Launching ResearchBot..."
APP_PATH=$(find "../build" -name "*.app" -type d | head -n 1)

if [ -z "$APP_PATH" ]; then
    echo "❌ Error: Could not find built .app in ../build"
    exit 1
fi

open "$APP_PATH"
echo "✅ App launched successfully."
