#!/usr/bin/env python3
"""
ask-search v2.0.0 — Cross-environment SearxNG search skill

Works in: OpenClaw (CLI) | Claude Code (CLI) | Antigravity (MCP)

Usage:
  ask-search "query"                    # top 10 results
  ask-search "query" --num 5            # limit results
  ask-search "AI news" --categories news
  ask-search "query" --lang zh-CN
  ask-search "query" --urls-only        # URL list only (pipe to web_fetch)
  ask-search "query" --json             # raw JSON output
  ask-search "query" --page 2           # page 2
  ask-search "query" --time week        # last week only
  ask-search "query" --site github.com  # site-specific search
  ask-search "query" --filetype pdf     # file type filter

Environment:
  SEARXNG_URL         SearxNG endpoint (default: http://localhost:8080)
  SEARXNG_CACHE_TTL   Cache TTL in seconds (default: 3600, 0 to disable)
  SEARXNG_RETRIES     Retry count (default: 2)
"""
import sys
import json
import urllib.parse
import argparse
import os
import subprocess
import time
import hashlib
import sqlite3
import pathlib

VERSION = "2.0.0"

CACHE_DIR = pathlib.Path(os.environ.get("ASKSEARCH_DATA", os.path.expanduser("~/.ask-search")))
CACHE_DB = CACHE_DIR / "cache.db"
CACHE_TTL = int(os.environ.get("SEARXNG_CACHE_TTL", "3600"))
MAX_RETRIES = int(os.environ.get("SEARXNG_RETRIES", "2"))


class SearchClient:
    """Stateful search client with caching, retries, dedup, and health checks."""

    def __init__(self, base_url=None, cache_ttl=None, retries=None):
        self.base_url = (base_url or os.environ.get("SEARXNG_URL", "http://localhost:8080")).rstrip("/")
        self.cache_ttl = cache_ttl if cache_ttl is not None else CACHE_TTL
        self.retries = retries if retries is not None else MAX_RETRIES
        self._db = None

    # --- Database / Cache ---

    def _get_db(self):
        if self._db is None:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(CACHE_DB), timeout=5)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS search_cache (
                    cache_key TEXT PRIMARY KEY,
                    query TEXT,
                    params_json TEXT,
                    results_json TEXT,
                    result_count INTEGER,
                    created_at REAL
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS search_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT,
                    params_json TEXT,
                    result_count INTEGER,
                    created_at REAL
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS seen_urls (
                    url_normalized TEXT PRIMARY KEY,
                    url_original TEXT,
                    title TEXT,
                    first_seen REAL,
                    last_seen REAL,
                    hit_count INTEGER DEFAULT 1
                )
            """)
            self._db.commit()
        return self._db

    def _cache_key(self, query, **params):
        blob = json.dumps({"q": query, **params}, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:20]

    def _cache_get(self, key):
        if self.cache_ttl <= 0:
            return None
        db = self._get_db()
        row = db.execute(
            "SELECT results_json, created_at FROM search_cache WHERE cache_key = ?", (key,)
        ).fetchone()
        if row and (time.time() - row[1]) < self.cache_ttl:
            return json.loads(row[0])
        if row:
            db.execute("DELETE FROM search_cache WHERE cache_key = ?", (key,))
            db.commit()
        return None

    def _cache_set(self, key, query, params, results):
        if self.cache_ttl <= 0:
            return
        db = self._get_db()
        db.execute(
            "INSERT OR REPLACE INTO search_cache (cache_key, query, params_json, results_json, result_count, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (key, query, json.dumps(params), json.dumps(results), len(results), time.time())
        )
        db.commit()

    def _record_history(self, query, params, result_count):
        db = self._get_db()
        db.execute(
            "INSERT INTO search_history (query, params_json, result_count, created_at) VALUES (?, ?, ?, ?)",
            (query, json.dumps(params), result_count, time.time())
        )
        db.commit()

    def _record_seen_urls(self, results):
        db = self._get_db()
        now = time.time()
        for r in results:
            url = r.get("url", "")
            norm = self._normalize_url(url)
            if not norm:
                continue
            existing = db.execute(
                "SELECT hit_count FROM seen_urls WHERE url_normalized = ?", (norm,)
            ).fetchone()
            if existing:
                db.execute(
                    "UPDATE seen_urls SET last_seen = ?, hit_count = hit_count + 1 WHERE url_normalized = ?",
                    (now, norm)
                )
            else:
                db.execute(
                    "INSERT INTO seen_urls (url_normalized, url_original, title, first_seen, last_seen, hit_count) VALUES (?, ?, ?, ?, ?, 1)",
                    (norm, url, r.get("title", ""), now, now)
                )
        db.commit()

    def get_history(self, limit=20):
        db = self._get_db()
        rows = db.execute(
            "SELECT query, result_count, created_at FROM search_history ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [{"query": r[0], "result_count": r[1], "timestamp": r[2]} for r in rows]

    # --- URL normalization & dedup ---

    @staticmethod
    def _normalize_url(url):
        if not url:
            return ""
        url = url.rstrip("/").split("?")[0].split("#")[0]
        url = url.replace("http://", "https://").replace("www.", "")
        return url.lower()

    @staticmethod
    def deduplicate(results):
        seen = set()
        out = []
        for r in results:
            norm = SearchClient._normalize_url(r.get("url", ""))
            if not norm or norm in seen:
                continue
            seen.add(norm)
            out.append(r)
        return out

    # --- Health check ---

    def health_check(self):
        """Check if SearxNG is reachable. Returns (ok: bool, message: str)."""
        url = self.base_url
        try:
            result = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "5", url],
                capture_output=True, text=True, timeout=8
            )
            code = result.stdout.strip()
            if code in ("200", "301", "302"):
                return True, f"SearxNG reachable at {url} (HTTP {code})"
            return False, f"SearxNG returned HTTP {code} at {url}"
        except Exception as e:
            return False, f"SearxNG unreachable at {url}: {e}"

    def ensure_running(self):
        """Check SearxNG health; attempt docker start if down."""
        ok, msg = self.health_check()
        if ok:
            return True, msg
        # Try to start the container
        try:
            subprocess.run(
                ["docker", "start", "searxng"],
                capture_output=True, text=True, timeout=15
            )
            time.sleep(3)
            ok, msg = self.health_check()
            if ok:
                return True, f"SearxNG was down, auto-started container. {msg}"
        except Exception:
            pass
        return False, f"SearxNG is down and could not auto-start. Run: cd ~/ask-search/searxng && docker compose up -d"

    # --- Metadata enrichment ---

    @staticmethod
    def enrich_results(results):
        for r in results:
            url = r.get("url", "")
            # Domain extraction
            try:
                r["domain"] = urllib.parse.urlparse(url).netloc.replace("www.", "")
            except Exception:
                r["domain"] = ""
            # Engine count (agreement signal)
            engines = r.get("engines", [])
            r["engine_count"] = len(engines)
            # Score (SearxNG provides this)
            r.setdefault("score", 0.0)
        return results

    # --- Core search ---

    def _raw_search(self, query, num=10, engines=None, lang=None, categories=None, page=1, time_range=None):
        """Single attempt to query SearxNG. Returns parsed results list."""
        params = {"q": query, "format": "json", "pageno": page}
        if engines:
            params["engines"] = engines
        if lang:
            params["language"] = lang
        if categories:
            params["categories"] = categories
        if time_range:
            params["time_range"] = time_range

        url = self.base_url + "/search?" + urllib.parse.urlencode(params)
        result = subprocess.run(
            ["curl", "-s", "--max-time", "15", url],
            capture_output=True, text=True, timeout=20
        )
        if result.returncode != 0:
            raise RuntimeError(f"curl failed (exit {result.returncode}): {result.stderr[:200]}")

        stdout = result.stdout.strip()
        if not stdout:
            raise RuntimeError("SearxNG returned empty response (is it running?)")

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            preview = stdout[:300].replace("\n", " ")
            raise RuntimeError(f"SearxNG returned non-JSON (is it starting up?): {preview}")

        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"SearxNG error: {data['error']}")

        raw_results = data.get("results", [])
        # Filter out results missing url or title
        valid = [r for r in raw_results if r.get("url") and r.get("title")]
        return valid

    def search(self, query, num=10, engines=None, lang=None, categories=None,
               page=1, time_range=None, site=None, filetype=None, dedupe=True):
        """
        Search with retries, caching, deduplication, and enrichment.

        Args:
            query: Search query
            num: Max results to return
            engines: Comma-separated engine list
            lang: Language code
            categories: SearxNG category
            page: Page number (default 1)
            time_range: day, week, month, year
            site: Restrict to domain (prepends site: to query)
            filetype: File type filter (prepends filetype: to query)
            dedupe: Deduplicate results by URL (default True)
        """
        # Build effective query with site/filetype modifiers
        effective_query = query
        if site:
            effective_query = f"site:{site} {effective_query}"
        if filetype:
            effective_query = f"filetype:{filetype} {effective_query}"

        # Cache lookup
        cache_params = {
            "num": num, "engines": engines, "lang": lang,
            "categories": categories, "page": page, "time_range": time_range
        }
        cache_key = self._cache_key(effective_query, **cache_params)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        # Search with retries
        last_error = None
        for attempt in range(self.retries + 1):
            try:
                results = self._raw_search(
                    effective_query, num, engines, lang, categories, page, time_range
                )
                break
            except Exception as e:
                last_error = e
                if attempt < self.retries:
                    delay = 2 ** attempt
                    time.sleep(delay)
        else:
            raise RuntimeError(f"Search failed after {self.retries + 1} attempts: {last_error}")

        # Deduplicate
        if dedupe:
            results = self.deduplicate(results)

        # Trim to requested count
        results = results[:num]

        # Enrich with metadata
        results = self.enrich_results(results)

        # Cache and record
        self._cache_set(cache_key, effective_query, cache_params, results)
        self._record_history(effective_query, cache_params, len(results))
        self._record_seen_urls(results)

        return results


# --- Module-level convenience functions (backward compat) ---

_default_client = None

def _get_client():
    global _default_client
    if _default_client is None:
        _default_client = SearchClient()
    return _default_client


def search(query, num=10, engines=None, lang=None, categories=None,
           page=1, time_range=None, site=None, filetype=None):
    """Backward-compatible search function."""
    return _get_client().search(
        query, num, engines, lang, categories, page, time_range, site, filetype
    )


def searxng_search(query, num=15, **kwargs):
    """MCP/legacy interface — returns JSON string."""
    try:
        client = _get_client()
        results = client.search(query, num, **kwargs)
        return json.dumps({
            "query": query,
            "count": len(results),
            "results": results
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e), "query": query})


def fmt_results(results, urls_only=False, snippet_len=300):
    if urls_only:
        return "\n".join(r.get("url", "") for r in results)
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "").strip()
        url = r.get("url", "")
        content = r.get("content", "").strip()
        engines = ",".join(r.get("engines", []))
        domain = r.get("domain", "")
        engine_count = r.get("engine_count", 0)
        score = r.get("score", 0)

        header = f"[{i}] {title}"
        if domain:
            header += f"  ({domain})"
        lines.append(header)
        lines.append(f"    {url}")
        if content:
            lines.append(f"    {content[:snippet_len]}")
        meta_parts = []
        if engines:
            meta_parts.append(f"engines:{engines}")
        if engine_count > 1:
            meta_parts.append(f"agree:{engine_count}")
        if score:
            meta_parts.append(f"score:{score:.1f}")
        if meta_parts:
            lines.append(f"    [{' | '.join(meta_parts)}]")
        lines.append("")
    return "\n".join(lines).strip()


def fmt_markdown(results, snippet_len=300):
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "").strip()
        url = r.get("url", "")
        content = r.get("content", "").strip()
        lines.append(f"{i}. **[{title}]({url})**")
        if content:
            lines.append(f"   {content[:snippet_len]}")
    return "\n".join(lines)


def fmt_csv(results):
    lines = ['"title","url","domain","engines","score"']
    for r in results:
        title = r.get("title", "").replace('"', '""')
        url = r.get("url", "").replace('"', '""')
        domain = r.get("domain", "")
        engines = ",".join(r.get("engines", []))
        score = r.get("score", 0)
        lines.append(f'"{title}","{url}","{domain}","{engines}",{score}')
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(
        description="SearxNG search (cross-environment)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               '  ask-search "CVE-2024-1234"\n'
               '  ask-search "admin panel" --site target.com --time week\n'
               '  ask-search "config" --filetype pdf --num 20\n'
               '  ask-search "query" --page 2 --format md\n'
    )
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--num",        "-n", type=int, default=10, help="Max results (default 10)")
    p.add_argument("--engines",    "-e", help="Engines: google,bing,duckduckgo,brave")
    p.add_argument("--lang",       "-l", help="Language: zh-CN, en, ja, ko")
    p.add_argument("--categories", "-c", help="Category: general,news,images,science,it")
    p.add_argument("--page",       "-p", type=int, default=1, help="Page number (default 1)")
    p.add_argument("--time",       "-t", help="Time range: day, week, month, year")
    p.add_argument("--site",       "-s", help="Restrict to domain (site:example.com)")
    p.add_argument("--filetype",   "-f", help="File type: pdf, xlsx, doc, zip")
    p.add_argument("--format",     default="text", choices=["text", "json", "md", "csv", "urls"],
                   help="Output format (default: text)")
    p.add_argument("--snippet-len", type=int, default=300, help="Snippet length (default 300)")
    p.add_argument("--no-cache",   action="store_true", help="Skip cache for this query")
    p.add_argument("--no-dedupe",  action="store_true", help="Don't deduplicate results")
    p.add_argument("--health",     action="store_true", help="Check SearxNG health and exit")
    p.add_argument("--history",    action="store_true", help="Show recent search history")
    p.add_argument("--version",    "-V", action="version", version=f"ask-search {VERSION}")

    # Backward compat aliases
    p.add_argument("--json",       "-j", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--urls-only",  "-u", action="store_true", help=argparse.SUPPRESS)

    args = p.parse_args()

    client = SearchClient()

    # Health check mode
    if args.health:
        ok, msg = client.ensure_running()
        print(msg)
        sys.exit(0 if ok else 1)

    # History mode
    if args.history:
        history = client.get_history()
        if not history:
            print("No search history yet.")
            sys.exit(0)
        for h in history:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(h["timestamp"]))
            print(f"  [{ts}] ({h['result_count']} results) {h['query']}")
        sys.exit(0)

    # Require query for actual searches
    if not args.query:
        p.error("query is required (unless using --health or --history)")

    # Handle backward compat flags
    if args.json:
        args.format = "json"
    if args.urls_only:
        args.format = "urls"

    # Override cache if requested
    if args.no_cache:
        client.cache_ttl = 0

    try:
        results = client.search(
            args.query, args.num, args.engines, args.lang, args.categories,
            args.page, args.time, args.site, args.filetype,
            dedupe=not args.no_dedupe
        )
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)

    if not results:
        print(json.dumps({"error": "No results", "query": args.query}))
        sys.exit(1)

    if args.format == "json":
        print(json.dumps({"query": args.query, "count": len(results), "results": results},
                         ensure_ascii=False, indent=2))
    elif args.format == "md":
        print(fmt_markdown(results, args.snippet_len))
    elif args.format == "csv":
        print(fmt_csv(results))
    elif args.format == "urls":
        print(fmt_results(results, urls_only=True))
    else:
        print(fmt_results(results, snippet_len=args.snippet_len))


if __name__ == "__main__":
    main()
