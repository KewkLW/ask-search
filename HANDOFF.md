# ask-search Handoff

## What This Is

Self-hosted web search tool for AI agents, wrapping SearxNG. Forked from [ythx-101/ask-search](https://github.com/ythx-101/ask-search) and heavily upgraded from a thin curl wrapper to a stateful search client with caching, retries, deduplication, and 8 MCP tools.

## Current State

**v2.0.0** — Phase 1 complete. Fully functional and wired into Claude Code as the default search tool.

### Infrastructure
- SearxNG runs in Docker on `localhost:8080` (container name: `searxng`)
- Docker Compose config: `~/ask-search/searxng/docker-compose.yml`
- SearxNG settings: `~/ask-search/searxng/searxng.yml` (uses `use_default_settings: true` with overrides)
- Secret key in `~/ask-search/searxng/.env` (gitignored)
- SQLite cache/history at `~/.ask-search/cache.db`

### Files Changed From Upstream
- `scripts/core.py` — Complete rewrite (94 -> 528 lines). `SearchClient` class with SQLite cache, retries, dedup, health checks, enrichment. New CLI flags.
- `mcp/server.py` — Complete rewrite (2 -> 8 MCP tools). Uses `SearchClient` singleton.
- `searxng/searxng.yml` — Replaced minimal config with `use_default_settings: true` approach (original was invalid schema).
- `searxng/.env` — Created with generated secret key.

### Claude Code Integration
- MCP server registered globally in `~/.claude.json` under `mcpServers.ask-search`
- Project CLAUDE.md at `~/.claude/projects/C--Users-kewkd/CLAUDE.md` instructs to always prefer ask-search over WebSearch
- Memory entry at `~/.claude/projects/C--Users-kewkd/memory/ask-search-setup.md`

## Architecture

```
CLI (ask-search "query")
    |
    v
SearchClient (scripts/core.py)
    |-- SQLite cache (~/.ask-search/cache.db)
    |   |-- search_cache: query -> results with TTL
    |   |-- search_history: timestamped query log
    |   |-- seen_urls: cross-query URL tracking
    |-- Health check + auto-recovery (docker start searxng)
    |-- Retry with exponential backoff (default 2 retries)
    |-- URL deduplication (normalize protocol/www/trailing slash/params)
    |-- Metadata enrichment (domain, engine_count, score)
    |
    v
SearxNG (Docker, localhost:8080)
    |-- Aggregates: Google, Bing, DuckDuckGo, Brave, Wikipedia, Startpage, + 60 more
    |-- JSON API: /search?q=...&format=json
```

## MCP Tools (8 total)

| Tool | Purpose |
|------|---------|
| `web_search` | General search with site/time/filetype/engine/page filters |
| `web_search_news` | News category with time range |
| `web_search_it` | IT/tech sources (StackOverflow, GitHub, docs) |
| `web_search_science` | Academic sources (arXiv, PubMed, Scholar) |
| `web_search_multi` | Parallel batch search (up to 10 queries, 5 workers) |
| `check_search_health` | Verify SearxNG is up, auto-start if down |
| `search_history` | Recall recent searches to avoid duplicates |

## CLI Reference

```bash
ask-search "query"                          # basic search, 10 results
ask-search "query" --num 20                 # more results
ask-search "query" --page 2                 # pagination
ask-search "query" --time week              # last week only
ask-search "query" --site github.com        # site-restricted
ask-search "query" --filetype pdf           # file type filter
ask-search "query" --categories news        # SearxNG category
ask-search "query" --engines google,brave   # specific engines
ask-search "query" --format json            # JSON output
ask-search "query" --format md              # markdown output
ask-search "query" --format csv             # CSV output
ask-search "query" --format urls            # URLs only
ask-search "query" --snippet-len 500        # longer snippets
ask-search "query" --no-cache               # skip cache
ask-search "query" --no-dedupe              # keep duplicates
ask-search --health                         # check SearxNG status
ask-search --history                        # show recent searches
```

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `SEARXNG_URL` | `http://localhost:8080` | SearxNG endpoint |
| `SEARXNG_CACHE_TTL` | `3600` (1 hour) | Cache TTL in seconds, 0 to disable |
| `SEARXNG_RETRIES` | `2` | Retry count on failure |
| `ASKSEARCH_DATA` | `~/.ask-search` | Data directory for SQLite DB |

## Operations

**Start SearxNG:**
```bash
cd ~/ask-search/searxng && docker compose up -d
```

**Stop SearxNG:**
```bash
docker stop searxng
```

**Check health:**
```bash
ask-search --health
# or: curl -s http://localhost:8080/search?q=test&format=json | python3 -m json.tool
```

**Clear cache:**
```bash
rm ~/.ask-search/cache.db
```

**View search history:**
```bash
ask-search --history
```

## Remaining Work (Phase 2-4)

### Phase 2 — Core features
- [ ] Deep search mode (`--deep`): auto-fetch top N URLs, extract text, return in one call
- [ ] Query expansion for CVE/security research (auto-append site-specific suffixes)
- [ ] Fallback rephrasing on 0 results

### Phase 3 — MCP power tools
- [ ] `web_search_deep` MCP tool
- [ ] Proxy chain integration (`--proxy` flag, `ASKSEARCH_PROXY` env, SearxNG outgoing config through Mullvad)
- [ ] Category tagging (auto-tag results as cve/exploit/writeup/docs/forum based on URL patterns)

### Phase 4 — Polish
- [ ] Compact output mode (token-efficient for LLMs)
- [ ] SearxNG outgoing proxy config for routing upstream queries through VPN
- [ ] Engine-specific rate limit tuning in searxng.yml
- [ ] `--dork` mode for explicit Google dork passthrough

## Known Issues
- `searxng.yml` has `secret_key: "ultrasecretkey"` hardcoded; actual secret is injected via `SEARXNG_SECRET` env var from `.env` file, but the YAML fallback is weak. Not a real risk since it's bound to 127.0.0.1 only.
- `web_search_multi` creates a new SQLite connection per thread; works but not ideal. Could use connection pooling if it becomes a bottleneck.
- The `list[str]` type hint in `web_search_multi` requires Python 3.9+.
