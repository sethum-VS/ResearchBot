"""
AcademicScraper.py — Tavily-powered academic paper discovery.
Scoped to arxiv.org and researchgate.net. Returns top 5 papers as Markdown.
"""

import os
from tavily import TavilyClient


ACADEMIC_DOMAINS = ["arxiv.org", "researchgate.net", "scholar.google.com"]


def search_academic_papers(topic: str, max_results: int = 5) -> str:
    """Search academic domains for research papers on a topic."""
    api_key = os.getenv("TAVILY_API_KEY", "")
    if not api_key:
        return "# Error\nTAVILY_API_KEY not set. Cannot search academic papers."

    try:
        client = TavilyClient(api_key=api_key)
        response = client.search(
            query=f"{topic} research paper methodology findings",
            include_domains=ACADEMIC_DOMAINS,
            max_results=max_results,
        )

        results = response.get("results", [])
        if not results:
            return f"# No academic papers found for: {topic}"

        lines = [f"# Academic Papers: {topic}\n"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "Untitled Paper")
            url = r.get("url", "")
            snippet = r.get("content", "No abstract available.")
            lines.append(f"## {i}. {title}")
            lines.append(f"**Source:** {url}\n")
            lines.append(f"{snippet}\n")
            lines.append("---\n")

        return "\n".join(lines)
    except Exception as e:
        return f"# Academic Search Error\nFailed to search for `{topic}`: {e}"
