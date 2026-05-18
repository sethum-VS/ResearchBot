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

from firecrawl import FirecrawlApp

_CRAWL_MAX_WORKERS = 5


def _get_app() -> FirecrawlApp:
    """Return a Firecrawl client pointed at the local Docker instance."""
    return FirecrawlApp(api_url='http://localhost:3002', api_key='local-dummy-key')


# ── Single URL Scrape (existing) ─────────────────────────────────────────────

def scrape_url_to_markdown(url: str) -> str:
    """Scrape a URL and return its content as Markdown using local Firecrawl."""
    try:
        app = _get_app()
        result = app.scrape(url, formats=['markdown'])
        return result.markdown if result.markdown else f"# No content extracted from {url}"
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
            data_list = getattr(crawl_result, 'data', None)
            if data_list is None and hasattr(crawl_result, 'get'):
                data_list = crawl_result.get('data') or []
                
            for page in data_list:
                md = getattr(page, 'markdown', None)
                if md is None and hasattr(page, 'get'):
                    md = page.get('markdown', '')
                    
                if md:
                    meta = getattr(page, 'metadata', None)
                    if meta is None and hasattr(page, 'get'):
                        meta = page.get('metadata', {})
                        
                    source = getattr(meta, 'sourceURL', getattr(meta, 'source_url', 'unknown')) if meta else 'unknown'
                    if source == 'unknown' and hasattr(meta, 'get'):
                        source = meta.get('sourceURL', meta.get('source_url', 'unknown'))
                        
                    sections.append(f"<!-- source: {source} -->\n{md}")

        # Path B: Keyword-driven web search
        if keywords and not sections:
            search_result = app.search(
                keywords[0],
                scrape_options={'formats': ['markdown']},
            )
            for item in (getattr(search_result, 'web', None) or []):
                md = getattr(item, 'markdown', '') or ''
                if md:
                    metadata = getattr(item, 'metadata', None)
                    source = getattr(metadata, 'source_url', 'unknown') if metadata else 'unknown'
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
    try:
        app = _get_app()
        result = app.scrape(url, formats=['markdown'])
        md = result.markdown
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

    sections: list[str] = []

    if len(urls) == 1:
        # Fast path: skip executor overhead for a single URL
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
