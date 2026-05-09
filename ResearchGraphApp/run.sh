#!/bin/bash

# ResearchBot — Sprint 1 Run Script
# Automated environment setup and build-launch pipeline.

set -e

# --- Configuration ---
PROJECT_ROOT=$(pwd)
BACKEND_DIR="$PROJECT_ROOT/Backend"
APP_DIR="$PROJECT_ROOT/App"
SCHEME="ResearchBot"
DERIVED_DATA_PATH="$PROJECT_ROOT/build"

echo "Starting ResearchBot Sprint 1 Pipeline..."

# --- 1. Python Environment Setup ---
echo "Setting up Python environment..."
cd "$BACKEND_DIR"

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate
echo "Installing dependencies..."
pip install -q --upgrade pip
pip install -q -r requirements.txt

# --- 2. Build macOS App ---
echo "Building macOS App ($SCHEME)..."
cd "$APP_DIR"

# Ensure we have a project to build
if [ ! -d "ResearchBot.xcodeproj" ]; then
    echo "Error: ResearchBot.xcodeproj not found in $APP_DIR"
    exit 1
fi

xcodebuild build \
    -project ResearchBot.xcodeproj \
    -scheme "$SCHEME" \
    -derivedDataPath "$DERIVED_DATA_PATH" \
    -destination 'platform=macOS' \
    | xcbeautify || xcodebuild build -project ResearchBot.xcodeproj -scheme "$SCHEME" -derivedDataPath "$DERIVED_DATA_PATH" -destination 'platform=macOS'

# --- 3. Launch App ---
echo "Launching ResearchBot..."
APP_PATH=$(find "$DERIVED_DATA_PATH" -name "$SCHEME.app" -type d | head -n 1)

if [ -z "$APP_PATH" ]; then
    echo "Error: Could not find built .app in $DERIVED_DATA_PATH"
    exit 1
fi

open "$APP_PATH"

echo "App launched successfully."
