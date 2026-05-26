"""
ProposalSynthesizer.py — Phase 5: Academic proposal generation via Gemini 2.5 Pro.

Produces a rigorous, structured Markdown proposal by combining the scoped idea,
matched papers, and session gap analysis into a single long-context Pro call.

AvoidBeingAI rules are injected as system-level instructions to enforce
professional, human-sounding academic writing.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from google import genai
from google.genai import types
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

logger = logging.getLogger(__name__)

_STABLE_REGIONS: list[str] = [
    "europe-west4",
    "us-east4",
    "asia-northeast1",
    "us-central1",
]

_MAX_OUTPUT_TOKENS = 65536


def _is_resource_exhausted(exc: Exception) -> bool:
    exc_str = str(exc).lower()
    return (
        "429" in exc_str
        or "resourceexhausted" in exc_str
        or "resource_exhausted" in exc_str
    )


def _load_avoid_ai_rules() -> str:
    """Load AvoidBeingAI.md from the repo root."""
    # Walk up from Backend/application/ to find AvoidBeingAI.md at repo root
    current = Path(__file__).resolve().parent
    for _ in range(6):
        candidate = current / "AvoidBeingAI.md"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
        current = current.parent

    logger.warning("ProposalSynthesizer: AvoidBeingAI.md not found on disk.")
    return ""


def _build_system_instruction(avoid_ai_text: str) -> str:
    """Construct the system prompt with writing rules."""
    rules = """\

MANDATORY WRITING RULES — THESE OVERRIDE ALL OTHER INSTRUCTIONS:

1. NEVER use any word or phrase from the banned lists above. If you catch \
yourself writing one, replace it immediately with a plain, specific alternative.
2. Vary sentence length substantially. Mix short punchy sentences with longer \
analytical ones. Some paragraphs should have 2 sentences; others 5.
3. Do NOT use Oxford commas. Write "A, B and C" not "A, B, and C".
4. Use contractions occasionally: "we've", "isn't", "can't", "doesn't". \
Not every sentence, but enough to sound natural.
5. Write with a specific point of view. Make claims. Take positions on which \
approach is better and why. Do not hedge everything.
6. NO em dashes (—). Use semicolons, parentheses or periods instead.
7. Keep the conclusion/summary SHORT (3-4 sentences max). Do not repeat \
what was already written in earlier sections.
8. Use specific proper nouns: name actual datasets (ImageNet, MIMIC-III), \
actual tools (PyTorch, scikit-learn), actual methods (BERT, LoRA). Never \
genericize to "a popular dataset" or "modern frameworks".
9. Paragraphs should have varied lengths. Not every paragraph should be the same size.
10. Start some sentences with "And" or "But" for natural flow.
11. Do NOT start the conclusion with "In conclusion", "In summary", or "Overall".\
"""

    if avoid_ai_text.strip():
        return (
            "YOU ARE WRITING AN ACADEMIC PROJECT PROPOSAL. The following file "
            "contains lists of words, phrases and patterns that AI systems "
            "typically produce. You MUST avoid ALL of them.\n\n"
            "--- BEGIN BANNED WORDS/PHRASES/PATTERNS ---\n"
            f"{avoid_ai_text}\n"
            "--- END BANNED WORDS/PHRASES/PATTERNS ---\n\n"
            f"{rules}"
        )

    return (
        "YOU ARE WRITING AN ACADEMIC PROJECT PROPOSAL.\n\n"
        f"{rules}"
    )


def _build_content_prompt(
    scoped_idea: str,
    matched_papers: list[dict],
    gap_analysis: dict,
) -> str:
    """Build the user content prompt with the data payload."""

    # Format matched papers into a structured block
    papers_block = []
    for i, paper in enumerate(matched_papers, 1):
        title = paper.get("title", "Untitled")
        abstract = paper.get("abstract", "No abstract available.")[:2000]
        source = paper.get("source_url", paper.get("source", ""))
        year = paper.get("year", "N/A")
        score = paper.get("match_score", "N/A")
        pdf_url = paper.get("pdf_url", "")

        entry = (
            f"### Paper {i}: {title}\n"
            f"- Year: {year}\n"
            f"- Source: {source}\n"
            f"- Match Score: {score}%\n"
        )
        if pdf_url:
            entry += f"- PDF: {pdf_url}\n"
        entry += f"- Abstract: {abstract}\n"
        papers_block.append(entry)

    papers_text = "\n".join(papers_block)

    # Format gap analysis
    gap_text = ""
    if gap_analysis:
        summary = gap_analysis.get("summary", "")
        if summary:
            gap_text += f"Executive Summary: {summary}\n\n"

        holes = gap_analysis.get("structural_holes", [])
        if holes:
            gap_text += "Structural Holes:\n"
            for h in holes:
                gap_text += f"- {h.get('title', '')}: {h.get('description', '')}\n"
                if h.get("bridging_opportunity"):
                    gap_text += f"  Opportunity: {h['bridging_opportunity']}\n"
            gap_text += "\n"

        limitations = gap_analysis.get("high_degree_limitations", [])
        if limitations:
            gap_text += "High-Degree Limitations:\n"
            for lim in limitations:
                gap_text += f"- {lim.get('title', '')}: {lim.get('description', '')}\n"
                if lim.get("evidence"):
                    gap_text += f"  Evidence: {lim['evidence']}\n"
            gap_text += "\n"

        orphans = gap_analysis.get("orphaned_solutions", [])
        if orphans:
            gap_text += "Orphaned Solutions:\n"
            for sol in orphans:
                gap_text += f"- {sol.get('title', '')}: {sol.get('description', '')}\n"
                if sol.get("technical_contribution"):
                    gap_text += f"  Contribution: {sol['technical_contribution']}\n"
            gap_text += "\n"

    return f"""\
Generate a rigorous academic project proposal based on the following inputs.

## SCOPED RESEARCH IDEA
{scoped_idea}

## MATCHED LITERATURE ({len(matched_papers)} papers, scored > 75% relevance)
{papers_text}

## GAP ANALYSIS FROM KNOWLEDGE GRAPH (Phase 4.5 output)
{gap_text if gap_text else "No gap analysis available for this session."}

## REQUIRED OUTPUT STRUCTURE

Produce a Markdown document with EXACTLY these sections in this order. \
Use `#` (single hash) for top-level section headings (I through VI) and \
`##` (double hash) for subsections (V.A, V.B). Do NOT deviate from this \
heading hierarchy.

# I. Executive Summary (The Problem & The Novelty)
Write 2-3 paragraphs in a persuasive, narrative style. The first paragraph \
must identify the core societal or technical problem this research addresses \
and explain why it matters in concrete terms. The second paragraph must \
explain why existing approaches (from the matched literature) fall short. \
The third paragraph must introduce the proposed solution and articulate its \
novelty in simple, direct language. Do NOT use bullet points, tables or \
technical parameters in this section. This is pure academic narrative.

# II. Project Definition
State the research topic as three components on one line:
- Task: what is being done
- Domain: the application area
- Constraint: the specific boundary or requirement

# III. Targeted Literature Review
Create a Markdown table comparing ALL {len(matched_papers)} matched papers:

| # | Paper | Year | Method | Domain | Key Finding | Limitation |
|---|-------|------|--------|--------|-------------|------------|

Each row must cite the actual paper title. The Limitation column must identify \
a specific weakness, not a generic comment.

# IV. Evidence-Based Research Gap
Explicitly cite flaws from the literature matrix above. Reference specific \
papers by name when identifying what is missing. Connect this to the gap \
analysis findings (structural holes, limitations, orphaned solutions) where \
applicable.

# V. Technical Architecture & Contribution
Describe the novel element being built. Be specific about the architecture, \
method or system. Explain why existing approaches cannot achieve this.

## V.A Feature Set
Organize features into three tiers:
- **Core Features**: directly address the research gap
- **Derived Features**: extend core capabilities
- **Contextual Features**: support deployment or evaluation

Each feature must cite at least one paper from the literature review as \
justification.

## V.B Anticipated Challenges
List 3-5 practical challenges for deployment, data collection or evaluation. \
Be specific (not generic "data quality" or "computational cost"). Reference \
actual constraints from the literature where relevant.

# VI. Dataset & Evaluation Strategy
ONLY include this section if the proposed work involves training or evaluating \
machine learning models. If the work is purely systems/architecture, skip this \
section entirely.

If included, compare 5 candidate datasets in a table:
| Dataset | Size | Domain | Format | Pros | Cons |

Recommend one dataset with justification.\
"""


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    retry=retry_if_exception(_is_resource_exhausted),
)
def _call_pro_with_retry(
    client: genai.Client,
    contents: list[str],
    config: types.GenerateContentConfig,
) -> str:
    response = client.models.generate_content(
        model="gemini-2.5-pro",
        contents=contents,
        config=config,
    )
    if not response or not response.text:
        raise RuntimeError("Gemini 2.5 Pro returned an empty response.")
    return response.text


def synthesize_proposal(
    scoped_idea: str,
    matched_papers: list[dict],
    gap_analysis: dict,
    avoid_ai_rules: str | None = None,
) -> str:
    """
    Generate a full academic proposal via a single Gemini 2.5 Pro call.

    Args:
        scoped_idea: The Task/Domain/Constraint scoped query.
        matched_papers: List of paper dicts with match_score, title, abstract, etc.
        gap_analysis: The session's academic_gap_analysis dict.
        avoid_ai_rules: Optional override for AvoidBeingAI.md text.

    Returns:
        Clean Markdown string of the complete proposal.

    Raises:
        RuntimeError: If all regions are exhausted.
    """
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT_ID")
    if not project_id:
        raise RuntimeError(
            "GOOGLE_CLOUD_PROJECT_ID not set. Cannot synthesize proposal."
        )

    if avoid_ai_rules is None:
        avoid_ai_rules = _load_avoid_ai_rules()

    system_instruction = _build_system_instruction(avoid_ai_rules)
    content_prompt = _build_content_prompt(scoped_idea, matched_papers, gap_analysis)

    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        max_output_tokens=_MAX_OUTPUT_TOKENS,
    )

    last_exc: Exception | None = None

    # Primary: global endpoint
    try:
        print(
            "PROGRESS: Phase 5 — synthesizing proposal via Gemini 2.5 Pro (global)...",
            flush=True,
        )
        client = genai.Client(vertexai=True, project=project_id, location="global")
        result = _call_pro_with_retry(client, [content_prompt], config)
        print("PROGRESS: Phase 5 — ✓ proposal synthesis complete.", flush=True)
        return result
    except Exception as exc:
        logger.warning(
            "ProposalSynthesizer: global endpoint failed (%s). Trying regions...",
            exc,
        )
        last_exc = exc

    # Regional failover
    for region in _STABLE_REGIONS:
        try:
            print(
                f"PROGRESS: Phase 5 — synthesis failover → {region}...",
                flush=True,
            )
            client = genai.Client(
                vertexai=True, project=project_id, location=region
            )
            result = _call_pro_with_retry(client, [content_prompt], config)
            print(
                f"PROGRESS: Phase 5 — ✓ proposal synthesis complete (via {region}).",
                flush=True,
            )
            return result
        except Exception as exc:
            logger.warning(
                "ProposalSynthesizer: region %s failed: %s", region, exc
            )
            last_exc = exc
            continue

    raise RuntimeError(
        f"ProposalSynthesizer: all regions exhausted. Last error: {last_exc}"
    )
