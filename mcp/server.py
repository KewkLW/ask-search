#!/usr/bin/env python3
"""
ask-search MCP Server — for Antigravity / Claude Code MCP integration

Install:
  pip install mcp

Add to your MCP config:
  {
    "mcpServers": {
      "ask-search": {
        "command": "python3",
        "args": ["/path/to/ask-search/mcp/server.py"],
        "env": {"SEARXNG_URL": "http://localhost:8080"}
      }
    }
  }
"""
import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("Error: mcp package not installed. Run: pip install mcp", file=sys.stderr)
    sys.exit(1)

from core import SearchClient

mcp = FastMCP("ask-search")
client = SearchClient()


@mcp.tool()
def web_search(query: str, num_results: int = 10, site: str = "", time_range: str = "",
               filetype: str = "", engines: str = "", page: int = 1) -> str:
    """
    Search the web via self-hosted SearxNG. Aggregates Google, Bing, DuckDuckGo, Brave and 70+ engines.
    Returns deduplicated results with title, url, content, domain, score, and engine agreement count.

    Args:
        query: Search query string
        num_results: Number of results to return (default 10, max 50)
        site: Restrict to domain (e.g. "github.com")
        time_range: Filter by time: "day", "week", "month", "year"
        filetype: File type filter: "pdf", "xlsx", "doc", "zip"
        engines: Comma-separated engines: "google,bing,duckduckgo,brave"
        page: Page number for pagination (default 1)
    """
    try:
        results = client.search(
            query, min(num_results, 50),
            engines=engines or None,
            page=page,
            time_range=time_range or None,
            site=site or None,
            filetype=filetype or None
        )
        return json.dumps({
            "query": query,
            "count": len(results),
            "page": page,
            "results": results
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e), "query": query})


@mcp.tool()
def web_search_news(query: str, num_results: int = 10, time_range: str = "") -> str:
    """
    Search recent news via SearxNG news category.

    Args:
        query: News search query
        num_results: Number of results (default 10)
        time_range: Filter by time: "day", "week", "month", "year"
    """
    try:
        results = client.search(
            query, min(num_results, 50),
            categories="news",
            time_range=time_range or None
        )
        return json.dumps({
            "query": query,
            "category": "news",
            "count": len(results),
            "results": results
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e), "query": query})


@mcp.tool()
def web_search_it(query: str, num_results: int = 10) -> str:
    """
    Search technical/IT sources: StackOverflow, GitHub, docs.
    Best for CVE lookups, code examples, API documentation, security advisories.

    Args:
        query: Technical search query
        num_results: Number of results (default 10)
    """
    try:
        results = client.search(query, min(num_results, 50), categories="it")
        return json.dumps({
            "query": query,
            "category": "it",
            "count": len(results),
            "results": results
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e), "query": query})


@mcp.tool()
def web_search_science(query: str, num_results: int = 10) -> str:
    """
    Search academic/science sources: arXiv, PubMed, Google Scholar.
    Best for research papers, academic publications, scientific data.

    Args:
        query: Academic search query
        num_results: Number of results (default 10)
    """
    try:
        results = client.search(query, min(num_results, 50), categories="science")
        return json.dumps({
            "query": query,
            "category": "science",
            "count": len(results),
            "results": results
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e), "query": query})


@mcp.tool()
def web_search_multi(queries: list[str], num_results: int = 5) -> str:
    """
    Run multiple searches in parallel. Returns all results keyed by query.
    Useful for comparative research, recon fan-out, or exploring multiple angles at once.

    Args:
        queries: List of search queries to run in parallel
        num_results: Results per query (default 5, max 20)
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    num = min(num_results, 20)
    all_results = {}

    def do_search(q):
        try:
            return q, client.search(q, num)
        except Exception as e:
            return q, {"error": str(e)}

    with ThreadPoolExecutor(max_workers=min(len(queries), 5)) as executor:
        futures = [executor.submit(do_search, q) for q in queries[:10]]  # cap at 10 queries
        for future in as_completed(futures):
            q, results = future.result()
            all_results[q] = results

    return json.dumps({
        "queries": list(all_results.keys()),
        "total_queries": len(all_results),
        "results": all_results
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def check_search_health() -> str:
    """
    Check if the SearxNG backend is reachable. Returns status and URL.
    Call this before doing searches if you suspect SearxNG might be down.
    Will attempt to auto-start the Docker container if it's not running.
    """
    ok, msg = client.ensure_running()
    return json.dumps({"status": "ok" if ok else "error", "message": msg})


@mcp.tool()
def search_history(limit: int = 20) -> str:
    """
    Show recent search history. Useful to recall what was already searched
    and avoid duplicate searches.

    Args:
        limit: Number of recent searches to return (default 20)
    """
    history = client.get_history(limit)
    return json.dumps({"count": len(history), "history": history}, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()
