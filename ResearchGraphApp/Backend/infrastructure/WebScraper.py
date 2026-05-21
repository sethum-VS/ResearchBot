"""
WebScraper.py — Local Firecrawl URL-to-Markdown extraction & advanced search.
Points the Firecrawl SDK at a locally-hosted Docker Compose instance
(port 3002, USE_DB_AUTHENTICATION=false) to avoid cloud API costs.

Provides two modes:
  1. scrape_url_to_markdown() — single URL scrape (legacy / fallback)
  2. firecrawl_advanced_search() — keyword search + deep crawl powered
     by Phase 1.5 InputAnalyzer output

Concurrency: deep_crawl_urls() uses ThreadPoolExecutor to scrape multiple
URLs in parallel (max_workers=5) with flush=True progress streaming.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.error
import urllib.request

from firecrawl import FirecrawlApp

_CRAWL_MAX_WORKERS = 5
_FIRECRAWL_BASE = "http://localhost:3002"


def _firecrawl_available() -> bool:
    """Return True when the local Firecrawl API responds on port 3002."""
    try:
        req = urllib.request.Request(_FIRECRAWL_BASE, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status < 500
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _firecrawl_unavailable_message() -> str:
    return (
        "# Firecrawl Unavailable\n"
        "Local Firecrawl is not running at http://localhost:3002. "
        "Start it via Docker Compose (see test_backend.sh / PROJECT_SPEC) "
        "or continue with Tavily, Wiki, and Semantic Scholar sources only."
    )


def _get_app() -> FirecrawlApp:
    """Return a Firecrawl client pointed at the local Docker instance."""
    return FirecrawlApp(api_url=_FIRECRAWL_BASE, api_key="local-dummy-key")


def _safe_iter_pages(crawl_result) -> list:
    """Normalize Firecrawl crawl responses to a list of page objects."""
    if crawl_result is None:
        return []
    data_list = getattr(crawl_result, "data", None)
    if data_list is None and isinstance(crawl_result, dict):
        data_list = crawl_result.get("data")
    if data_list is None:
        return []
    return list(data_list)


def _page_markdown(page) -> str:
    md = getattr(page, "markdown", None)
    if md is None and isinstance(page, dict):
        md = page.get("markdown", "")
    return md or ""


def _page_source(page, fallback: str = "unknown") -> str:
    meta = getattr(page, "metadata", None)
    if meta is None and isinstance(page, dict):
        meta = page.get("metadata") or {}
    if meta is None:
        return fallback
    source = getattr(meta, "sourceURL", None) or getattr(meta, "source_url", None)
    if source is None and isinstance(meta, dict):
        source = meta.get("sourceURL") or meta.get("source_url")
    return source or fallback


# ── Single URL Scrape (existing) ─────────────────────────────────────────────

def scrape_url_to_markdown(url: str) -> str:
    """Scrape a URL and return its content as Markdown using local Firecrawl."""
    if not _firecrawl_available():
        return _firecrawl_unavailable_message()
    try:
        app = _get_app()
        result = app.scrape(url, formats=["markdown"])
        if result is None:
            return f"# Scrape Error\nFirecrawl returned no response for `{url}`."
        md = getattr(result, "markdown", None) or ""
        return md if md else f"# No content extracted from {url}"
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
    if not _firecrawl_available():
        return _firecrawl_unavailable_message()

    app = _get_app()
    sections: list[str] = []

    try:
        # Path A: Deep-crawl a known URL
        if target_urls:
            crawl_result = app.crawl_url(
                target_urls[0],
                params={
                    "limit": 3,
                    "scrapeOptions": {"formats": ["markdown"]},
                },
            )
            for page in _safe_iter_pages(crawl_result):
                md = _page_markdown(page)
                if md:
                    source = _page_source(page)
                    sections.append(f"<!-- source: {source} -->\n{md}")

        # Path B: Keyword-driven web search
        if keywords and not sections:
            search_result = app.search(
                keywords[0],
                scrape_options={"formats": ["markdown"]},
            )
            if search_result is None:
                sections.append(
                    "# Firecrawl Advanced Error\nSearch returned no response "
                    "(is Firecrawl fully initialized?)."
                )
            else:
                web_items = getattr(search_result, "web", None) or []
                for item in web_items:
                    md = getattr(item, "markdown", "") or ""
                    if md:
                        metadata = getattr(item, "metadata", None)
                        source = (
                            getattr(metadata, "source_url", "unknown")
                            if metadata
                            else "unknown"
                        )
                        sections.append(f"<!-- source: {source} -->\n{md}")

    except Exception as e:
        sections.append(f"# Firecrawl Advanced Error\n{e}")

    if not sections:
        query_desc = target_urls[0] if target_urls else (keywords[0] if keywords else "unknown")
        return f"# No results\nFirecrawl returned no data for: {query_desc}"

    return "\n\n---\n\n".join(sections)


# ── Deep Crawl Individual URLs (Concurrent) ──────────────────────────────────

def _scrape_single_url(url: str) -> str | None:
    """Scrape one URL; returns Markdown or None on failure."""
    if not _firecrawl_available():
        return _firecrawl_unavailable_message()
    try:
        app = _get_app()
        result = app.scrape(url, formats=["markdown"])
        if result is None:
            return f"# Deep Crawl Error\nFirecrawl returned no response for `{url}`."
        md = getattr(result, "markdown", None) or ""
        if md:
            return f"<!-- source: {url} -->\n{md}"
        return None
    except Exception as e:
        return f"# Deep Crawl Error\nFailed to scrape `{url}`: {e}"


def deep_crawl_urls(urls: list[str]) -> str:
    """
    Scrape each URL in parallel and return concatenated Markdown.

    Unlike firecrawl_advanced_search (which crawls from a root),
    this function performs a targeted single-page scrape per URL
    using a ThreadPoolExecutor for concurrency.

    Parameters
    ----------
    urls : list of fully-qualified URLs to scrape.

    Returns
    -------
    Combined Markdown from all successfully scraped pages.
    """
    if not urls:
        return ""

    if not _firecrawl_available():
        return _firecrawl_unavailable_message()

    sections: list[str] = []

    if len(urls) == 1:
        result = _scrape_single_url(urls[0])
        return result or ""

    with ThreadPoolExecutor(max_workers=min(_CRAWL_MAX_WORKERS, len(urls))) as pool:
        future_to_url = {pool.submit(_scrape_single_url, u): u for u in urls}

        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                result = future.result()
                if result:
                    sections.append(result)
                print(f"PROGRESS: deep_crawl — ✓ scraped {url}", flush=True)
            except Exception as e:
                sections.append(f"# Deep Crawl Error\nFailed to scrape `{url}`: {e}")
                print(f"PROGRESS: deep_crawl — ✗ error on {url}: {e}", flush=True)

    return "\n\n---\n\n".join(sections)
