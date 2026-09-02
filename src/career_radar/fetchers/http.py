"""Shared HTTP plumbing for the API fetchers.

Each ATS board is a public endpoint with no auth; these helpers add the same
retry/backoff behavior workday.py uses, so a transient 429/5xx doesn't kill a
whole daily run. `request_json` is for clean JSON APIs (Greenhouse, Lever,
Ashby, Breezy); `request_text` is for HTML-scraping fetchers (Rippling,
Paylocity, JazzHR) that parse the raw page.
"""

import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
TIMEOUT_S = 30


def _request(method: str, url: str, **kwargs: Any) -> requests.Response:
    """Issue a request with exponential backoff; return the Response.

    Raises on a terminal failure after MAX_RETRIES attempts.
    """
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.request(method, url, timeout=TIMEOUT_S, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"retryable status {resp.status_code}")
            resp.raise_for_status()
            return resp
        except (requests.RequestException, ValueError) as e:
            if attempt == MAX_RETRIES - 1:
                raise
            wait = 2 ** (attempt + 1)
            logger.warning("Request failed (%s), retrying in %ss: %s", url, wait, e)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def request_json(method: str, url: str, **kwargs: Any) -> Any:
    """Issue a request with retry and return parsed JSON.

    Returns whatever the endpoint responds with (a list for Lever/Breezy, a
    dict for Greenhouse/Ashby).
    """
    return _request(method, url, **kwargs).json()


def request_text(method: str, url: str, **kwargs: Any) -> str:
    """Issue a request with retry and return the raw response body as text."""
    return _request(method, url, **kwargs).text
