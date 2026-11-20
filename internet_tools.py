import requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus
import os


def search_web(query: str, max_results: int = 5):
    """
    DuckDuckGo HTML search. No DDGS, no API key.
    Returns list of dicts: [{title, url, body}, ...]
    """
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    }

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        results = []

        for result in soup.select(".result"):
            title_tag = result.select_one(".result__title")
            link_tag = result.select_one(".result__url")
            snippet_tag = result.select_one(".result__snippet")

            if not title_tag or not link_tag:
                continue

            title = title_tag.get_text(strip=True)
            link = link_tag.get("href", "")
            snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""

            if link:
                results.append({
                    "title": title,
                    "url": link,
                    "body": snippet
                })

            if len(results) >= max_results:
                break

        return results

    except Exception as e:
        os.makedirs("logs", exist_ok=True)
        with open("logs/error_log.txt", "a", encoding="utf-8") as f:
            f.write(f"Search error for '{query}': {str(e)}\n")
        return []


def fetch_webpage(url: str):
    """
    Fetch a web page as clean text using Jina Reader.
    No API key needed.
    """
    try:
        resp = requests.get(
            f"https://r.jina.ai/{url}",
            headers={"Accept": "text/plain"},
            timeout=20
        )
        if resp.status_code == 200:
            return resp.text[:8000]
        return f"Error: HTTP {resp.status_code}"
    except Exception as e:
        return f"Error fetching page: {str(e)}"
