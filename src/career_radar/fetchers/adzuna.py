"""Adzuna API fetcher for job listings across multiple roles and locations.

Queries the public Adzuna Search API using an application ID and key.
"""

import logging
from typing import Any, Dict, List

import requests

logger = logging.getLogger(__name__)

def fetch_listings(app_id: str, app_key: str, where: str, what: str = "") -> List[Dict[str, Any]]:
    """
    Fetch job listings from the Adzuna API.

    Args:
        app_id: Adzuna API app ID.
        app_key: Adzuna API app key.
        where: Location to search (e.g., "Remote" or "New York, NY").
        what: Keywords to search for.

    Returns:
        List of job dictionaries returned by the API.
    """
    url = "https://api.adzuna.com/v1/api/jobs/us/search/1"
    all_results = []

    # Adzuna doesn't support 'OR' well in the free API 'what' field,
    # so we split the query and make multiple requests.
    keywords = [kw.strip() for kw in what.split(" OR ")] if what else [""]

    for kw in keywords:
        params = {
            "app_id": app_id,
            "app_key": app_key,
            "where": where,
            "results_per_page": 50
        }
        if kw:
            params["what"] = kw

        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            results = resp.json().get("results", [])
            all_results.extend(results)
        except Exception as e:
            logger.error("Adzuna error for '%s': %s", kw, e)

    # Deduplicate by Adzuna ID
    unique = {str(r.get("id")): r for r in all_results if r.get("id")}
    return list(unique.values())
