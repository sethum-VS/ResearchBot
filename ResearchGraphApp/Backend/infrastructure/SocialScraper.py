"""
SocialScraper.py — Tavily-powered Reddit & X thread discovery.
Scoped to social domains only. Queries are enhanced with engagement
modifiers to surface only highly-upvoted, viral, or top-commented threads.
"""

import os
from tavily import TavilyClient


SOCIAL_DOMAINS = ["reddit.com", "x.com", "twitter.com"]


def search_social_threads(topic: str, max_results: int = 5) -> str:
    """Search Reddit and X for high-engagement discussion threads on a topic."""
    api_key = os.getenv("TAVILY_API_KEY", "")
    if not api_key:
        return "# Error\nTAVILY_API_KEY not set. Cannot search social threads."

    try:
        enhanced_query = f"{topic} ('highly upvoted' OR 'top comments' OR 'viral' OR 'retweets') discussion"

        client = TavilyClient(api_key=api_key)
        response = client.search(
            query=enhanced_query,
            include_domains=SOCIAL_DOMAINS,
            max_results=max_results,
        )

        results = response.get("results", [])
        if not results:
            return f"# No social threads found for: {topic}"

        lines = [f"# Social Threads: {topic}\n"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "Untitled")
            url = r.get("url", "")
            snippet = r.get("content", "No preview available.")
            lines.append(f"## {i}. {title}")
            lines.append(f"**Source:** {url}\n")
            lines.append(f"{snippet}\n")
            lines.append("---\n")

        return "\n".join(lines)
    except Exception as e:
        return f"# Social Search Error\nFailed to search for `{topic}`: {e}"
