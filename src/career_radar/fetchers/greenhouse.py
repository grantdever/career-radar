"""Fetcher for Greenhouse job boards (boards-api.greenhouse.io).

One GET returns every live job with full description + location, so there is
no separate detail call. The board token is the slug in the public careers
URL, e.g. https://boards.greenhouse.io/{board}.

Greenhouse's public API exposes `updated_at` (not a posted date), so
normalize.py treats it as the best available recency signal.
"""

import logging

from career_radar.fetchers.http import request_json

logger = logging.getLogger(__name__)


def fetch_jobs(board: str) -> list[dict]:
    """Fetch all live postings for a board (full content included)."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
    data = request_json("GET", url, params={"content": "true"})
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    logger.info("Greenhouse %s: %d jobs", board, len(jobs))
    return jobs
