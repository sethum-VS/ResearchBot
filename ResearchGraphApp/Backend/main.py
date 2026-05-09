#!/usr/bin/env python3
"""
main.py — Phase 1 Ingestion Stub
Entry point for the Backend orchestrator. Accepts CLI arguments from the
Swift PythonBridge and simulates ingestion by printing structured JSON output.

Usage:
    python3 main.py --idea "Your research idea" --url "https://example.com"
"""

import argparse
import json
import sys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ResearchBot Backend Orchestrator")
    parser.add_argument("--idea", type=str, default="", help="Research idea or topic seed")
    parser.add_argument("--url", type=str, default="", help="Optional seed URL")
    return parser.parse_args()


def simulate_ingestion(idea: str, url: str) -> dict:
    """
    Stub: simulate the ingestion phase.
    In future sprints this will call IngestSeedUseCase from /application.
    """
    if not idea.strip():
        return {
            "status": "error",
            "code": 1,
            "message": "Idea cannot be empty. Provide a research topic via --idea.",
        }

    result = {
        "status": "success",
        "phase": "Phase 1 — Ingestion (Stub)",
        "received": {
            "idea": idea.strip(),
            "url": url.strip() or None,
        },
        "next_step": "Phase 2 — Context Expansion & Scraping (not yet implemented)",
    }
    return result


def main() -> None:
    args = parse_args()
    output = simulate_ingestion(args.idea, args.url)

    # Always emit JSON to stdout so Swift Process() can parse it reliably
    print(json.dumps(output, indent=2))

    if output.get("status") == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
