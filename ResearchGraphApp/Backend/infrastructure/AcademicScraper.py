"""
AcademicScraper.py — Semantic Scholar and Tavily-powered academic paper discovery.
Queries Semantic Scholar first, falls back to Tavily scoped to academic domains.
Uses Gemini 2.5 Flash to extract specific academic details.
"""

import os
import requests
from tavily import TavilyClient
from google import genai


ACADEMIC_DOMAINS = ["semanticscholar.org", "arxiv.org", "researchgate.net"]


def search_academic_papers(topic: str, max_results: int = 20) -> str:
    """Search academic domains for research papers on a topic and extract key academic points via LLM."""
    
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT_ID")
    if not project_id:
        return "# Error\nGOOGLE_CLOUD_PROJECT_ID not set. Cannot process with Gemini."
        
    raw_content = ""
    
    # 1. Try Semantic Scholar API
    try:
        ss_url = f"https://api.semanticscholar.org/graph/v1/paper/search?query={topic}&limit={max_results}&fields=title,url,abstract,authors,year,citationCount"
        response = requests.get(ss_url, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            papers = data.get("data", [])
            if papers:
                for p in papers:
                    title = p.get("title", "Untitled")
                    url = p.get("url", "")
                    abstract = p.get("abstract", "No abstract.")
                    year = p.get("year", "Unknown Year")
                    authors_list = p.get("authors", [])
                    authors = ", ".join([a.get("name", "") for a in authors_list]) if authors_list else "Unknown Authors"
                    
                    raw_content += f"Title: {title}\nAuthors: {authors}\nYear: {year}\nURL: {url}\nAbstract: {abstract}\n\n"
    except Exception as e:
        print(f"Semantic Scholar API failed: {e}")
        
    # 2. Fallback to Tavily if Semantic Scholar yielded no results
    if not raw_content:
        api_key = os.getenv("TAVILY_API_KEY", "")
        if not api_key:
            return "# Error\nTAVILY_API_KEY not set. Cannot search academic papers as fallback."
            
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
                
            for r in results:
                title = r.get("title", "Untitled Paper")
                url = r.get("url", "")
                snippet = r.get("content", "No abstract available.")
                raw_content += f"Title: {title}\nURL: {url}\nAbstract/Content: {snippet}\n\n"
        except Exception as e:
            return f"# Academic Search Error\nFailed to search for `{topic}`: {e}"
            
    # 3. LLM Parsing
    try:
        genai_client = genai.Client(vertexai=True, project=project_id, location="global")
        model = "gemini-2.5-flash"
        
        prompt = (
            f"You are an academic research assistant analyzing papers on the topic: {topic}.\n"
            "Below is raw metadata and abstracts from research papers. For EACH paper, extract and format the output exactly as follows:\n\n"
            "- **Current Work in the Domain:** (Summary of the paper's approach)\n"
            "- **Limitations:** (What the authors explicitly state as shortcomings)\n"
            "- **Future Work:** (What the authors recommend doing next)\n"
            "- **Citation:** (Title, Authors, Year, URL)\n\n"
            "Separate each paper's analysis with a markdown horizontal rule (---).\n\n"
            f"Raw Data:\n{raw_content}"
        )
        
        genai_response = genai_client.models.generate_content(
            model=model,
            contents=[prompt],
        )
        
        return f"# Academic Papers: {topic}\n\n{genai_response.text}"
    except Exception as e:
        return f"# Academic Parsing Error\nFailed to parse papers with Gemini: {e}"
