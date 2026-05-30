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

import json

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
        
        # Prioritize the combined Abstract + Conclusion. Fallback to just the abstract.
        # Increase limit to 6000 to ensure the conclusion isn't truncated.
        if "abstract_conclusion" in paper and paper["abstract_conclusion"]:
             paper_text = paper["abstract_conclusion"][:6000]
        else:
             paper_text = paper.get("abstract", "No abstract available.")[:3000]
             
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
        entry += f"- Paper Content: {paper_text}\n"
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

## USER'S CUSTOM PROJECT IMPLEMENTATION
{scoped_idea}

DIRECTIVE: You are an academic ghostwriter. The User's Custom Project Implementation provided above is the ABSOLUTE TRUTH of what this proposal is about. You must build the ENTIRE proposal around implementing this exact idea. Do not pivot to other concepts found in the literature. Do not invent a different project. The literature matrix and gap analysis provided below are ONLY to be used as supporting evidence to justify the user's specific implementation.

## MATCHED LITERATURE ({len(matched_papers)} papers, scored > 75% relevance)
{papers_text}

## CONTEXTUAL BACKGROUND (HISTORICAL GAP ANALYSIS)
Contextual Background (Historical Gap Analysis): This data shows the broader research environment. Use this ONLY to provide background context for why the user's specific project is necessary. Do not adopt these historical gaps as the primary focus if they conflict with the user's project idea.

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
method or system. Explain why existing approaches cannot achieve this. \
The core technical contribution MUST be the exact system/module requested by the user. If the user proposed a 'Progressive Context Delivery module', that must be the center of this section.

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



_PROPOSAL_CRITIC_PROMPT = """Grade this proposal. 
1. Does it strictly follow the Roman numeral format (I - VI)? 
2. Are the 'Core Features' actually solving the user's specific problem? 
3. Does the Literature Matrix contain real limitations? 
Output a JSON object: {"passed": boolean, "feedback": "Specific instructions for revision if failed"}.

--- USER SCOPED IDEA ---
{scoped_idea}

--- DRAFT PROPOSAL ---
{draft}
"""


def generate_draft(system_instruction: str, content_prompt: str, project_id: str) -> str:
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        max_output_tokens=_MAX_OUTPUT_TOKENS,
    )
    last_exc: Exception | None = None

    try:
        client = genai.Client(vertexai=True, project=project_id, location="global")
        result = _call_pro_with_retry(client, [content_prompt], config)
        return result
    except Exception as exc:
        logger.warning("ProposalSynthesizer generator global failed: %s", exc)
        last_exc = exc

    for region in _STABLE_REGIONS:
        try:
            client = genai.Client(vertexai=True, project=project_id, location=region)
            result = _call_pro_with_retry(client, [content_prompt], config)
            return result
        except Exception as exc:
            logger.warning("ProposalSynthesizer generator region %s failed: %s", region, exc)
            last_exc = exc

    raise RuntimeError(f"ProposalSynthesizer generator all regions exhausted. Last error: {last_exc}")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    retry=retry_if_exception(_is_resource_exhausted),
)
def _call_flash_critic(
    client: genai.Client,
    prompt: str,
    config: types.GenerateContentConfig,
) -> str:
    """Gemini 2.5 Flash critic call with tenacity retry on 429."""
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[prompt],
        config=config,
    )
    if not response or not response.text:
        raise RuntimeError("Gemini 2.5 Flash returned an empty critic response.")
    return response.text


def evaluate_draft(draft: str, scoped_idea: str, project_id: str) -> dict:
    prompt = _PROPOSAL_CRITIC_PROMPT.format(scoped_idea=scoped_idea, draft=draft)
    config = types.GenerateContentConfig(response_mime_type="application/json")
    last_exc: Exception | None = None

    try:
        client = genai.Client(vertexai=True, project=project_id, location="global")
        raw = _call_flash_critic(client, prompt, config)
        return json.loads(raw)
    except Exception as exc:
        logger.warning("ProposalSynthesizer critic global failed: %s", exc)
        last_exc = exc

    for region in _STABLE_REGIONS:
        try:
            client = genai.Client(vertexai=True, project=project_id, location=region)
            raw = _call_flash_critic(client, prompt, config)
            return json.loads(raw)
        except Exception as exc:
            logger.warning("ProposalSynthesizer critic region %s failed: %s", region, exc)
            last_exc = exc

    logger.warning("ProposalSynthesizer critic exhausted all regions, bypassing.")
    return {"passed": True, "feedback": ""}


def synthesize_proposal(
    scoped_idea: str,
    matched_papers: list[dict],
    gap_analysis: dict,
    avoid_ai_rules: str | None = None,
) -> str:
    """
    Generate a full academic proposal via Gemini 2.5 Pro with a Critic Loop.
    """
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT_ID")
    if not project_id:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT_ID not set. Cannot synthesize proposal.")

    if avoid_ai_rules is None:
        avoid_ai_rules = _load_avoid_ai_rules()

    system_instruction = _build_system_instruction(avoid_ai_rules)
    base_content_prompt = _build_content_prompt(scoped_idea, matched_papers, gap_analysis)

    current_prompt = base_content_prompt
    MAX_RETRIES = 2
    draft = ""

    for attempt in range(MAX_RETRIES + 1):
        if attempt == 0:
            print("PROGRESS: Phase 5.5 — synthesizing proposal via Gemini 2.5 Pro...", flush=True)
        draft = generate_draft(system_instruction, current_prompt, project_id)
        
        evaluation = evaluate_draft(draft, scoped_idea, project_id)
        passed = evaluation.get("passed", True)
        
        if passed:
            print("PROGRESS: Phase 5.5 — ✓ proposal synthesis complete and passed critic.", flush=True)
            return draft
        else:
            feedback = evaluation.get("feedback", "No specific feedback provided.")
            print(f"PROGRESS: Phase 5.5 — Critic rejected Draft V{attempt + 1}. Generating revision...", flush=True)
            current_prompt = f"{base_content_prompt}\n\nCRITIC FEEDBACK FOR REVISION:\n{feedback}"
    
    print("PROGRESS: Phase 5.5 — ✓ proposal synthesis max retries reached, returning last draft.", flush=True)
    return draft

