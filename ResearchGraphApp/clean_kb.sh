#!/bin/bash

# clean_kb.sh — Deep clear research artifacts while preserving top-level folders
# This script deletes everything INSIDE the subfolders of research_knowledge_base.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
KB_DIR="$SCRIPT_DIR/research_knowledge_base"

if [ -d "$KB_DIR" ]; then
    echo "🧹 Deep cleaning subfolders in $KB_DIR..."
    
    # Loop through each item in the knowledge base directory
    for subdir in "$KB_DIR"/*; do
        if [ -d "$subdir" ]; then
            echo "   -> Clearing $(basename "$subdir")..."
            # Delete everything inside the subfolder (including sub-subfolders and hidden files)
            # We use /* and /.* to catch everything, excluding . and ..
            rm -rf "${subdir:?}"/*
            rm -rf "${subdir:?}"/.[!.]*
        fi
    done
    
    echo "✅ Done. Knowledge base is empty but top-level folders remain."
else
    echo "❌ Error: Directory $KB_DIR not found."
    exit 1
fi
