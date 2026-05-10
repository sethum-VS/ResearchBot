"""
WebScraper.py — Local Firecrawl URL-to-Markdown extraction & advanced search.
Points the Firecrawl SDK at a locally-hosted Docker Compose instance
(port 3002, USE_DB_AUTHENTICATION=false) to avoid cloud API costs.

Provides two modes:
  1. scrape_url_to_markdown() — single URL scrape (legacy / fallback)
  2. firecrawl_advanced_search() — keyword search + deep crawl powered
     by Phase 1.5 InputAnalyzer output
"""

from firecrawl import FirecrawlApp


def _get_app() -> FirecrawlApp:
    """Return a Firecrawl client pointed at the local Docker instance."""
    return FirecrawlApp(api_url='http://localhost:3002', api_key='local-dummy-key')


# ── Single URL Scrape (existing) ─────────────────────────────────────────────

def scrape_url_to_markdown(url: str) -> str:
    """Scrape a URL and return its content as Markdown using local Firecrawl."""
    try:
        app = _get_app()
        result = app.scrape_url(url, params={'formats': ['markdown']})
        return result.get('markdown', f"# No content extracted from {url}")
    except Exception as e:
        return f"# Scrape Error\nFailed to scrape `{url}` locally: {e}"


# ── Advanced Search + Deep Crawl ─────────────────────────────────────────────

def firecrawl_advanced_search(
    keywords: list[str],
    target_urls: list[str],
) -> str:
    """
    Perform an intelligent Firecrawl operation based on Phase 1.5 output.

    - If target_urls exist  → deep-crawl the first URL (up to 3 pages).
    - If only keywords exist → search the web with the first keyword
      and scrape the top results.
    - Returns concatenated Markdown from all discovered pages.
    """
    app = _get_app()
    sections: list[str] = []

    try:
        # Path A: Deep-crawl a known URL
        if target_urls:
            crawl_result = app.crawl_url(
                target_urls[0],
                params={
                    'limit': 3,
                    'scrapeOptions': {'formats': ['markdown']},
                },
            )
            for page in (crawl_result.get('data') or []):
                md = page.get('markdown', '')
                if md:
                    source = page.get('metadata', {}).get('sourceURL', 'unknown')
                    sections.append(f"<!-- source: {source} -->\n{md}")

        # Path B: Keyword-driven web search
        if keywords and not sections:
            search_result = app.search(
                keywords[0],
                params={'scrapeOptions': {'formats': ['markdown']}},
            )
            for item in (search_result.get('data') or []):
                md = item.get('markdown', '')
                if md:
                    source = item.get('metadata', {}).get('sourceURL', 'unknown')
                    sections.append(f"<!-- source: {source} -->\n{md}")

    except Exception as e:
        sections.append(f"# Firecrawl Advanced Error\n{e}")

    if not sections:
        query_desc = target_urls[0] if target_urls else (keywords[0] if keywords else "unknown")
        return f"# No results\nFirecrawl returned no data for: {query_desc}"

    return "\n\n---\n\n".join(sections)
