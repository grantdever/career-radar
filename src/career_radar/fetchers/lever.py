"""Fetcher for Lever job postings (api.lever.co).

One GET with mode=json returns a bare JSON array of every live posting,
including full descriptions (`description`/`descriptionPlain`) and an
`additional` field that often carries the compensation line. The company
token is the slug in the public careers URL, e.g.
https://jobs.lever.co/{company}.
"""

import logging

from career_radar.fetchers.http import request_json

logger = logging.getLogger(__name__)


def fetch_postings(company: str) -> list[dict]:
    """Fetch all live postings for a Lever company (full content included)."""
    url = f"https://api.lever.co/v0/postings/{company}"
    data = request_json("GET", url, params={"mode": "json"})
    postings = data if isinstance(data, list) else data.get("postings", [])
    logger.info("Lever %s: %d postings", company, len(postings))
    return postings
