"""Fetcher for iCIMS career portals ({subdomain}.icims.com).

iCIMS has no public JSON board API, but the portal is server-rendered HTML and
each job page embeds a schema.org `JobPosting` JSON-LD block containing the
full description, posted date, employment type and location. So the listing is
scraped for the cheap fields the pre-filter needs (title / location / req id)
and the detail is read out of structured JSON-LD rather than out of markup.

Requesting `?in_iframe=1` is important: without it some tenants return the
employer's own wrapper page instead of the iCIMS content.

Confirmed 2026-08-17 against careers-aei.icims.com (AEI). Scraping an
employer's own ATS pages for descriptions is approved (CLAUDE.md non-goals).
"""

import html
import json
import logging
import re

from career_radar.fetchers.http import request_text

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
}

MAX_PAGES = 25

_CARD_RE = re.compile(r'<li class="iCIMS_JobCardItem">(.*?)</li>', re.S)
_LOCATION_RE = re.compile(r"Job Locations</span>\s*<span[^>]*>\s*(.*?)\s*</span>", re.S)
_TITLE_LINK_RE = re.compile(
    r'<a\s+href="([^"]+)"[^>]*class="iCIMS_Anchor"[^>]*>.*?<h3[^>]*>\s*(.*?)\s*</h3>', re.S
)
_REQ_ID_RE = re.compile(r"/jobs/(\d+)/")
_LD_JSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S
)


def _clean(text: str) -> str:
    """Collapse whitespace and unescape HTML entities in a scraped fragment."""
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text))).strip()


def _parse_cards(page_html: str, subdomain: str) -> list[dict]:
    """Parse one search-results page into listing summaries."""
    listings = []
    for block in _CARD_RE.findall(page_html):
        m = _TITLE_LINK_RE.search(block)
        if not m:
            continue
        url, title = html.unescape(m.group(1)), _clean(m.group(2))
        req = _REQ_ID_RE.search(url)
        if not req:
            continue
        loc_m = _LOCATION_RE.search(block)
        listings.append({
            "req_id": req.group(1),
            "title": title,
            "location": _clean(loc_m.group(1)) if loc_m else "",
            "url": url,
            "subdomain": subdomain,
        })
    return listings


def fetch_listings(subdomain: str) -> list[dict]:
    """Fetch every posting summary on an iCIMS portal (no detail yet).

    Pages via the portal's `pr` parameter until a page yields no cards.
    """
    listings: list[dict] = []
    seen: set[str] = set()
    for page in range(MAX_PAGES):
        url = f"https://{subdomain}.icims.com/jobs/search?ss=1&in_iframe=1&pr={page}"
        batch = _parse_cards(request_text("GET", url, headers=HEADERS), subdomain)
        fresh = [b for b in batch if b["req_id"] not in seen]
        if not fresh:
            break
        seen.update(b["req_id"] for b in fresh)
        listings.extend(fresh)
    logger.info("iCIMS %s: %d jobs", subdomain, len(listings))
    return listings


def _job_posting_from_ld(payload) -> dict:
    """Pull the JobPosting node out of a JSON-LD payload (bare or @graph)."""
    if isinstance(payload, list):
        nodes = payload
    elif isinstance(payload, dict):
        nodes = payload.get("@graph") or [payload]
    else:
        return {}
    for node in nodes:
        if isinstance(node, dict) and node.get("@type") == "JobPosting":
            return node
    return {}


def fetch_detail(job_url: str) -> dict:
    """Fetch one job page and return its schema.org JobPosting JSON-LD dict."""
    page = request_text("GET", job_url, headers=HEADERS)
    for block in _LD_JSON_RE.findall(page):
        try:
            payload = json.loads(block)
        except json.JSONDecodeError as e:
            logger.debug("iCIMS %s: unparseable ld+json (%s)", job_url, e)
            continue
        posting = _job_posting_from_ld(payload)
        if posting:
            return posting
    logger.warning("iCIMS %s: no JobPosting JSON-LD found", job_url)
    return {}
