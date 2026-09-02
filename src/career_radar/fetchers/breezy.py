"""Fetcher for Breezy HR job boards ({slug}.breezy.hr).

Breezy exposes a clean JSON feed at `{slug}.breezy.hr/json` with title,
location, type, department, published date, and (sometimes) salary — but it
has NO description. The scorer depends on description texture, so this fetcher
also scrapes each job's detail page (`{url}`) for the description in its
`<div id="description">` block (scraping approved 2026-08-13).
"""

import logging

from career_radar.core.normalize import extract_element_by_id
from career_radar.fetchers.http import request_json, request_text

logger = logging.getLogger(__name__)


def fetch_jobs(slug: str) -> list[dict]:
    """Fetch every live posting summary from the JSON feed (no description)."""
    url = f"https://{slug}.breezy.hr/json"
    jobs = request_json("GET", url)
    jobs = jobs if isinstance(jobs, list) else []
    logger.info("Breezy %s: %d jobs", slug, len(jobs))
    return jobs


def fetch_detail(job_url: str) -> str:
    """Scrape one job's description HTML from its detail page."""
    text = request_text("GET", job_url)
    return extract_element_by_id(text, "description")
