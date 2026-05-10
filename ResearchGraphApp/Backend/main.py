#!/usr/bin/env python3
"""
main.py — ResearchBot Backend Entry Point (Phases 2–4)
Accepts CLI arguments from the Swift PythonBridge, delegates to
IngestSeedUseCase which runs scraping, storage, synthesis, and graphify.

Usage:
    python3 main.py --idea "Your research idea" --url "https://example.com"
"""

import argparse
import json
import os
import sys

from dotenv import load_dotenv

# Load .env from repo root (two levels up from Backend/)
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

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
            "code": 1
        }

    print(json.dumps(result, indent=2, ensure_ascii=False))

    if result.get("status") == "error":
        sys.exit(result.get("code", 1))


if __name__ == "__main__":
    main()
