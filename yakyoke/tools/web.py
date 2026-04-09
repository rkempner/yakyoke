"""Web tools: search and URL fetch.

Lifted in spirit from gemma-chat. The signatures are slightly different
because yakyoke tools take a workspace path as the first argument (even
though these particular tools don't use it). This keeps the tool interface
uniform across the registry.
"""

from __future__ import annotations

from pathlib import Path

from yakyoke.tools.registry import ToolSpec

# Cap on returned page text so a single fetch can't blow out the context.
MAX_PAGE_CHARS = 8000


def web_search(workspace: Path, query: str, max_results: int = 5) -> str:
    """DuckDuckGo search; returns formatted top results."""
    try:
        from ddgs import DDGS
    except ImportError:
        return "web_search unavailable: install `ddgs`"

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
    except Exception as e:
        return f"Search failed: {e}"

    if not results:
        return "No results found."

    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "(no title)")
        url = r.get("href") or r.get("url") or ""
        snippet = r.get("body", "")
        lines.append(f"{i}. {title}\n   URL: {url}\n   {snippet}")
    return "\n\n".join(lines)


def fetch_url(workspace: Path, url: str) -> str:
    """Fetch a URL and extract the main text content."""
    try:
        import trafilatura
    except ImportError:
        return "fetch_url unavailable: install `trafilatura`"

    try:
        downloaded = trafilatura.fetch_url(url)
    except Exception as e:
        return f"Failed to fetch {url}: {e}"

    if not downloaded:
        return f"Failed to fetch {url} (no content)"

    text = trafilatura.extract(
        downloaded,
        include_comments=False,
        include_tables=True,
        favor_recall=True,
    )
    if not text:
        return f"No extractable content at {url}"

    if len(text) > MAX_PAGE_CHARS:
        omitted = len(text) - MAX_PAGE_CHARS
        text = text[:MAX_PAGE_CHARS] + f"\n\n[...truncated, {omitted} more chars...]"
    return text


def web_search_spec() -> ToolSpec:
    return ToolSpec(
        name="web_search",
        func=web_search,
        schema={
            "type": "function",
            "function": {
                "name": "web_search",
                "description": (
                    "Search the web for current or factual information using "
                    "DuckDuckGo. Returns the top results with title, URL, and a "
                    "short snippet. Use this when you need facts you don't already "
                    "know, or to find authoritative sources to read with fetch_url."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The search query. Be specific and concise.",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "How many results to return (default 5).",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
    )


def fetch_url_spec() -> ToolSpec:
    return ToolSpec(
        name="fetch_url",
        func=fetch_url,
        schema={
            "type": "function",
            "function": {
                "name": "fetch_url",
                "description": (
                    "Fetch a web page and return its main text content with "
                    "boilerplate (nav, ads, footer) stripped. Use this after "
                    "web_search to read the full content of a promising result, "
                    "or whenever you need the actual contents of a known URL."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The full URL to fetch (include https://).",
                        },
                    },
                    "required": ["url"],
                },
            },
        },
    )
