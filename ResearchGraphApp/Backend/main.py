#!/usr/bin/env python3
"""
main.py — ResearchBot Backend Entry Point (Phases 1.5–4.5, Phase 5 Proposal)
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

def _resolve_env_path() -> str:
    support = os.environ.get("APP_SUPPORT_DIR", "").strip()
    if support:
        return os.path.join(support, ".env")
    default = os.path.join(
        os.path.expanduser("~"),
        "Library",
        "Application Support",
        "AutonomousResearchGraph",
        ".env",
    )
    if os.path.isfile(default):
        return default
    current_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(current_dir, "..", "..", ".env"))


load_dotenv(_resolve_env_path())

from application.ExportWorkspaceUseCase import export_to_workspace
from application.IngestSeedUseCase import execute
from application.ProposalOrchestrator import generate_proposal

WORKSPACE_START = "---WORKSPACE_EXPORT_RESULT_START---"
WORKSPACE_END = "---WORKSPACE_EXPORT_RESULT_END---"
PROPOSAL_START = "---PROPOSAL_RESULT_START---"
PROPOSAL_END = "---PROPOSAL_RESULT_END---"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ResearchBot Backend Orchestrator")
    parser.add_argument(
        "--command",
        type=str,
        default="pipeline",
        choices=("pipeline", "export_to_workspace", "generate_proposal", "export_proposal_to_workspace"),
        help="pipeline (default), export_to_workspace, generate_proposal, or export_proposal_to_workspace",
    )
    parser.add_argument("--idea", type=str, default="", help="Research idea or topic seed")
    parser.add_argument("--url", type=str, default="", help="Optional seed URL")
    parser.add_argument(
        "--session-id",
        type=str,
        default="",
        help="Session directory name for export_to_workspace",
    )
    parser.add_argument(
        "--kb-root",
        type=str,
        default="",
        help="Absolute research_knowledge_base path for export_to_workspace",
    )
    parser.add_argument(
        "--project-idea",
        type=str,
        default="",
        help="Project idea for generate_proposal command",
    )
    parser.add_argument(
        "--proposal-path",
        type=str,
        default="",
        help="Absolute path to proposal .md file for export_proposal_to_workspace",
    )
    parser.add_argument(
        "--matched-papers-json",
        type=str,
        default="",
        help="Absolute path to matched_papers.json for export_proposal_to_workspace",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if args.command == "export_to_workspace":
        _run_workspace_export(args)
        return

    if args.command == "generate_proposal":
        _run_proposal_generation(args)
        return

    if args.command == "export_proposal_to_workspace":
        _run_proposal_export(args)
        return

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


def _run_workspace_export(args: argparse.Namespace) -> None:
    session_id = (args.session_id or "").strip()
    if not session_id:
        result = {"status": "error", "message": "--session-id is required for export_to_workspace."}
    else:
        try:
            result = export_to_workspace(session_id, args.kb_root or "")
        except Exception as e:
            result = {"status": "error", "message": f"Workspace export failed: {e}"}

    print(f"\n{WORKSPACE_START}")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(WORKSPACE_END)

    if result.get("status") == "error":
        sys.exit(1)


def _run_proposal_generation(args: argparse.Namespace) -> None:
    session_id = (args.session_id or "").strip()
    project_idea = (args.project_idea or "").strip()
    if not session_id:
        result = {"status": "error", "message": "--session-id is required for generate_proposal."}
    elif not project_idea:
        result = {"status": "error", "message": "--project-idea is required for generate_proposal."}
    else:
        try:
            result = generate_proposal(
                session_id=session_id,
                user_project_idea=project_idea,
                kb_root=args.kb_root or "",
            )
        except Exception as e:
            result = {"status": "error", "message": f"Proposal generation failed: {e}"}

    print(f"\n{PROPOSAL_START}")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(PROPOSAL_END)

    if result.get("status") == "error":
        sys.exit(1)


def _run_proposal_export(args: argparse.Namespace) -> None:
    from infrastructure.GoogleWorkspaceManager import export_proposal_to_workspace

    session_id = (args.session_id or "").strip()
    proposal_path = (args.proposal_path or "").strip()
    matched_papers_json = (args.matched_papers_json or "").strip()
    if not session_id:
        result = {"status": "error", "message": "--session-id is required."}
    elif not proposal_path:
        result = {"status": "error", "message": "--proposal-path is required."}
    else:
        try:
            result = export_proposal_to_workspace(
                session_id=session_id,
                proposal_path=proposal_path,
                matched_papers_json=matched_papers_json or None,
                kb_root=args.kb_root or None,
            )
        except Exception as e:
            result = {"status": "error", "message": f"Proposal export failed: {e}"}

    print(f"\n{WORKSPACE_START}")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(WORKSPACE_END)

    if result.get("status") == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
