"""Fetcher for Paylocity recruiting boards (recruiting.paylocity.com).

The board is a server-rendered React app. The listing page embeds the full
job list (title, location, published date, remote flag, and a short
description snippet) in a `window.pageData = {...}` JS blob; the job detail
page carries the full description in its `og:description` meta tag. So this is
a two-call fetcher: fetch_listings (cheap) then fetch_detail per survivor.

The board token is the UUID in the public board URL, e.g.
https://recruiting.paylocity.com/Recruiting/Jobs/All/{company_id}.
"""

import html
import json
import logging
import re

from career_radar.fetchers.http import request_text

logger = logging.getLogger(__name__)

_PAGE_DATA_RE = re.compile(r"window\.pageData\s*=\s*(\{.*?\});\s*\n", re.S)
_OG_DESC_RE = re.compile(r'<meta property="og:description" content="([^"]*)"')


def fetch_listings(company_id: str) -> list[dict]:
    """Fetch every live job summary on a Paylocity board (no detail yet)."""
    url = f"https://recruiting.paylocity.com/Recruiting/Jobs/All/{company_id}"
    html_text = request_text("GET", url)
    m = _PAGE_DATA_RE.search(html_text)
    if not m:
        raise ValueError("Paylocity page had no window.pageData blob")
    page = json.loads(m.group(1))
    jobs = page.get("Jobs", []) if isinstance(page, dict) else []
    logger.info("Paylocity %s: %d jobs", company_id, len(jobs))
    return jobs


def fetch_detail(job_id) -> str:
    """Fetch one job's full description text from its detail page."""
    url = f"https://recruiting.paylocity.com/Recruiting/Jobs/Details/{job_id}"
    html_text = request_text("GET", url)
    m = _OG_DESC_RE.search(html_text)
    if not m:
        return ""
    # The og:description content is double-HTML-escaped (&amp;nbsp; etc.).
    return html.unescape(html.unescape(m.group(1)))
