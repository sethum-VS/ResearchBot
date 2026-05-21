#!/bin/bash
# cleanup_repo_layout.sh — Remove stray graphify-out folders outside session workspaces.
# Safe to run repeatedly. Does not delete runs/session_* data.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

remove_if_graphify_out() {
    local path="$1"
    if [ -d "$path" ] && { [ -f "$path/graph.json" ] || [ -f "$path/graph.html" ]; }; then
        echo "   -> Removing stray graphify-out: $path"
        rm -rf "$path"
    elif [ -d "$path" ]; then
        local count
        count="$(find "$path" -maxdepth 1 -type f 2>/dev/null | wc -l | tr -d ' ')"
        if [ "$count" = "0" ]; then
            echo "   -> Removing empty directory: $path"
            rm -rf "$path"
        fi
    fi
}

echo "🧹 Removing stray graphify-out directories outside session workspaces ..."

remove_if_graphify_out "$REPO_ROOT/graphify-out"
remove_if_graphify_out "$SCRIPT_DIR/graphify-out"
remove_if_graphify_out "$SCRIPT_DIR/Backend/graphify-out"

# Legacy KB root duplicates (clean_kb also handles these).
for legacy in agent_scrapes raw_ingestion processed_summaries graphify-out; do
    remove_if_graphify_out "$SCRIPT_DIR/research_knowledge_base/$legacy"
done

echo "✅ Repo layout cleanup complete."
