"""Fetcher for Workday cxs job boards (validated against University of Rochester).

Listing endpoint: POST https://{host}/wday/cxs/{tenant}/{site}/jobs
Detail endpoint:  GET  https://{host}/wday/cxs/{tenant}/{site}{externalPath}

There is no server-side posted-date filter; recency is handled downstream.
externalPath must always come from the listing response, never be constructed
from a title (paths and titles can diverge).
"""

import logging
import time

import requests

logger = logging.getLogger(__name__)

PAGE_SIZE = 20
REQUEST_DELAY_S = 0.5
MAX_RETRIES = 3
TIMEOUT_S = 30


def _request_with_retry(method: str, url: str, **kwargs) -> dict:
    """Issue a request with exponential backoff; return parsed JSON."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.request(method, url, timeout=TIMEOUT_S, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"retryable status {resp.status_code}")
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            if attempt == MAX_RETRIES - 1:
                raise
            wait = 2 ** (attempt + 1)
            logger.warning("Request failed (%s), retrying in %ss: %s", url, wait, e)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def fetch_listings(host: str, tenant: str, site: str) -> list[dict]:
    """Fetch every job posting summary on the board, paging through offsets."""
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    postings: list[dict] = []
    offset = 0
    total = None
    while True:
        body = {"appliedFacets": {}, "limit": PAGE_SIZE, "offset": offset, "searchText": ""}
        data = _request_with_retry("POST", url, json=body)
        page = data.get("jobPostings", [])
        if total is None:
            total = data.get("total", 0)
            logger.info("%s/%s: %d total postings", tenant, site, total)
        postings.extend(page)
        offset += PAGE_SIZE
        if not page or offset >= total:
            break
        time.sleep(REQUEST_DELAY_S)
    return postings


def fetch_detail(host: str, tenant: str, site: str, external_path: str) -> dict:
    """Fetch full posting detail (jobPostingInfo) for one listing's externalPath."""
    url = f"https://{host}/wday/cxs/{tenant}/{site}{external_path}"
    data = _request_with_retry("GET", url)
    return data.get("jobPostingInfo", {}) or {}
