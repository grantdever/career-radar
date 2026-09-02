"""Fetcher for Ashby job boards (api.ashbyhq.com).

One GET returns every live job with full description and (with
`includeCompensation=true`) structured salary data, so a single call carries
everything normalize.py needs. The board token is the last path segment of
the hosted job board, e.g. https://jobs.ashbyhq.com/{board}.
"""

import logging

from career_radar.fetchers.http import request_json

logger = logging.getLogger(__name__)


def fetch_jobs(board: str) -> list[dict]:
    """Fetch all live postings for an Ashby board (description + comp included)."""
    url = f"https://api.ashbyhq.com/posting-api/job-board/{board}"
    data = request_json("GET", url, params={"includeCompensation": "true"})
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    logger.info("Ashby %s: %d jobs", board, len(jobs))
    return jobs
