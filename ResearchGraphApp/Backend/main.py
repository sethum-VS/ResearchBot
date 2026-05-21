#!/usr/bin/env python3
"""
main.py — ResearchBot Backend Entry Point (Phases 1.5–4.5)
Accepts CLI arguments from the Swift PythonBridge, delegates to
IngestSeedUseCase which runs scraping, storage, synthesis, graphify, and gap analysis.

Usage:
    python3 main.py --idea "Your research idea" --url "https://example.com"
"""

import argparse
import json
import os
import sys

# Line-buffer stdout so pipeline logs flush promptly when redirected to a file.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (ValueError, OSError):
        pass

from dotenv import load_dotenv

# Load .env from repo root (two levels up from Backend/)
current_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.abspath(os.path.join(current_dir, "..", "..", ".env"))
load_dotenv(env_path)

from application.IngestSeedUseCase import execute


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ResearchBot Backend Orchestrator")
    parser.add_argument("--idea", type=str, default="", help="Research idea or topic seed")
    parser.add_argument("--url", type=str, default="", help="Optional seed URL")
    return parser.parse_args()


def main():
    args = parse_args()

    try:
        result = execute(args.idea, args.url)
    except Exception as e:
        result = {
            "status": "error",
            "message": f"Unexpected fatal error: {str(e)}",
            "code": 1,
        }

    print("\n---PIPELINE_RESULT_START---")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print("---PIPELINE_RESULT_END---")

    # Graphify failures stay status=success with graphify.error per PROJECT_SPEC.
    if result.get("status") == "error":
        sys.exit(result.get("code", 1))


if __name__ == "__main__":
    main()
