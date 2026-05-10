"""
WikiAPI.py — MediaWiki REST API integration.
Fetches page summaries and full content from Wikipedia as Markdown.
Uses the Wikimedia REST API (/api/rest_v1/) — no API key required.
"""

import requests
from urllib.parse import quote


WIKI_BASE = "https://en.wikipedia.org"
USER_AGENT = "ResearchBot/1.0 (https://github.com/sethum-VS/ResearchBot)"


def get_wiki_summary(topic: str) -> str:
    """Fetch a Wikipedia summary for the given topic, returned as Markdown."""
    safe_title = quote(topic.replace(" ", "_"))
    url = f"{WIKI_BASE}/api/rest_v1/page/summary/{safe_title}"
    headers = {"User-Agent": USER_AGENT}

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 404:
            return f"# Wiki: {topic}\nNo Wikipedia article found for `{topic}`."
        resp.raise_for_status()

        data = resp.json()
        title = data.get("title", topic)
        extract = data.get("extract", "No summary available.")
        page_url = data.get("content_urls", {}).get("desktop", {}).get("page", "")

        lines = [
            f"# Wikipedia: {title}\n",
            f"**Source:** {page_url}\n",
            f"{extract}\n",
        ]
        return "\n".join(lines)
    except Exception as e:
        return f"# Wiki Error\nFailed to fetch Wikipedia data for `{topic}`: {e}"


def get_wiki_full_content(topic: str) -> str:
    """Fetch full Wikipedia article content as HTML, converted to Markdown-style text."""
    safe_title = quote(topic.replace(" ", "_"))
    url = f"{WIKI_BASE}/api/rest_v1/page/html/{safe_title}"
    headers = {"User-Agent": USER_AGENT}

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 404:
            return f"# Wiki: {topic}\nNo Wikipedia article found for `{topic}`."
        resp.raise_for_status()
        # Return raw HTML — downstream LLM can parse structure
        return f"# Wikipedia Full Content: {topic}\n\n{resp.text[:15000]}"
    except Exception as e:
        return f"# Wiki Error\nFailed to fetch full content for `{topic}`: {e}"
