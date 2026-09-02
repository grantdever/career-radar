"""USAJOBS API fetcher for federal government opportunities.

Queries the official USAJOBS Search API using an authorization key.
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)


def fetch_listings(api_key: str, location_name: str = "Remote", user_agent: str | None = None) -> list[dict]:
    url = "https://data.usajobs.gov/api/search"
    ua = user_agent or os.environ.get("USAJOBS_USER_AGENT", "career-radar-client@example.com")
    headers = {
        "Authorization-Key": api_key,
        "User-Agent": ua,
    }
    params = {
        "LocationName": location_name,
        "ResultsPerPage": 500,
    }
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data.get("SearchResult", {}).get("SearchResultItems", [])
    except Exception as e:
        logger.error("USAJOBS error: %s", e)
        return []


def fetch_detail(url: str) -> str:
    # USAJOBS Search API already returns a decent amount of detail in SearchResultItems.
    return ""
