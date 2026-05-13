"""
SocialScraper.py — Tavily-powered Reddit & X thread discovery.
Scoped to social domains only. Queries are enhanced with engagement
modifiers to surface only highly-upvoted, viral, or top-commented threads.
"""

import os
from tavily import TavilyClient
from google import genai


SOCIAL_DOMAINS = ["reddit.com", "x.com", "twitter.com"]


def search_social_threads(topic: str, max_results: int = 5) -> str:
    """Search Reddit and X for high-engagement discussion threads on a topic, then process with LLM."""
    api_key = os.getenv("TAVILY_API_KEY", "")
    if not api_key:
        return "# Error\nTAVILY_API_KEY not set. Cannot search social threads."
    
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT_ID")
    if not project_id:
        return "# Error\nGOOGLE_CLOUD_PROJECT_ID not set. Cannot process with Gemini."

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

        # Prepare raw data for Gemini
        raw_content = ""
        for i, r in enumerate(results, 1):
            title = r.get("title", "Untitled")
            url = r.get("url", "")
            snippet = r.get("content", "No preview available.")
            raw_content += f"Title: {title}\nURL: {url}\nContent: {snippet}\n\n"

        genai_client = genai.Client(vertexai=True, project=project_id, location="global")
        model = "gemini-2.5-flash"
        
        prompt = (
            f"You are analyzing social media search results for the topic: {topic}.\n"
            "Below is the raw search data. Process it and output a markdown report.\n"
            "For each thread, strictly format its entry with the source domain explicitly identified like this:\n"
            "### [Source: <Domain (e.g., Reddit, X)>] - <Thread Title>\n"
            "Include the URL and a summary of the sentiment or key discussion points.\n\n"
            "Additionally, explicitly extract any 'leads' (such as Wikipedia links, GitHub repos, or specific academic terms) "
            "mentioned in the social conversations, and place them in a dedicated section at the very end titled:\n"
            "### Extracted Leads\n\n"
            f"Raw Data:\n{raw_content}"
        )
        
        genai_response = genai_client.models.generate_content(
            model=model,
            contents=[prompt],
        )
        
        return f"# Social Threads: {topic}\n\n{genai_response.text}"
    except Exception as e:
        return f"# Social Search Error\nFailed to search for `{topic}`: {e}"
