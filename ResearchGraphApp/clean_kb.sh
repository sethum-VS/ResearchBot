#!/bin/bash
# clean_kb.sh — Clear pipeline session data under the single knowledge-base root.
#
# Canonical layout (only writable location):
#   research_knowledge_base/runs/session_<timestamp>_<slug>/
#
# Legacy top-level folders (agent_scrapes, graphify-out, etc.) are no longer used.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
KB_DIR="$SCRIPT_DIR/research_knowledge_base"
RUNS_DIR="$KB_DIR/runs"

if [ ! -d "$KB_DIR" ]; then
    echo "❌ Knowledge base not found: $KB_DIR"
    exit 1
fi

echo "🧹 Cleaning session workspaces in $RUNS_DIR ..."

# Remove obsolete legacy shells at KB root (pre-session shared folders).
for legacy in agent_scrapes raw_ingestion processed_summaries graphify-out; do
    if [ -d "$KB_DIR/$legacy" ]; then
        echo "   -> Removing legacy $legacy/"
        rm -rf "$KB_DIR/$legacy"
    fi
done

mkdir -p "$RUNS_DIR"

if [ -d "$RUNS_DIR" ]; then
    for session in "$RUNS_DIR"/session_*; do
        [ -d "$session" ] || continue
        echo "   -> Clearing $(basename "$session") ..."
        rm -rf "${session:?}"/*
        rm -rf "${session:?}"/.[!.]* 2>/dev/null || true
    done
fi

echo "✅ Knowledge base cleared (runs/ preserved, legacy root folders removed)."
