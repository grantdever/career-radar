"""Fetcher for Rippling job boards (ats.rippling.com/{slug}/jobs).

The board is a Next.js SPA: the listing page embeds every live job in
`__NEXT_DATA__` (React Query dehydrated state), but the listing entries carry
only id/title/department/location — no description or salary. The job detail
page embeds the full posting (description HTML + created date + salary in the
description text) in its own `__NEXT_DATA__` under `apiData.jobPost`.

So this is a two-call fetcher like Workday: fetch_listings (cheap) then
fetch_detail per surviving posting.
"""

import json
import logging
import re

from career_radar.fetchers.http import request_text

logger = logging.getLogger(__name__)

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
    re.S,
)


def _next_data(html: str) -> dict:
    """Extract and parse the `__NEXT_DATA__` JSON blob from a Next.js page."""
    m = _NEXT_DATA_RE.search(html)
    if not m:
        raise ValueError("Rippling page had no __NEXT_DATA__ blob")
    return json.loads(m.group(1))


def fetch_listings(slug: str) -> list[dict]:
    """Fetch every live posting summary on a Rippling board (no detail yet)."""
    url = f"https://ats.rippling.com/{slug}/jobs"
    data = _next_data(request_text("GET", url))
    queries = data["props"]["pageProps"].get("dehydratedState", {}).get("queries", [])
    for q in queries:
        key = q.get("queryKey")
        if isinstance(key, list) and "job-posts" in key:
            payload = q.get("state", {}).get("data") or {}
            items = payload.get("items", []) if isinstance(payload, dict) else []
            logger.info("Rippling %s: %d jobs", slug, len(items))
            return items
    logger.warning("Rippling %s: no job-posts query found", slug)
    return []


def fetch_detail(job_url: str) -> dict:
    """Fetch one job's full `jobPost` payload from its detail page."""
    data = _next_data(request_text("GET", job_url))
    return data["props"]["pageProps"]["apiData"].get("jobPost") or {}
