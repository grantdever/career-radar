"""Fetcher for ADP Workforce Now recruiting feeds proxied by a Sitecore site.

NYSERDA's careers page looks like a JS SPA, but the Vue app behind it
(`/Assets/PageParts/Scripts/ADPJobList.js`) calls a plain JSON endpoint on the
same host:

    GET https://{host}/rapi/adpjoblistapi/GetRequisitions?DataSourceId={id}

The response is `{"Requisitions": [...]}` where each requisition carries the
title, locations, ADP apply URL, post date, a real min/max pay range, and the
full description HTML in `Posting_longName`. That makes it a listing-includes-
description source like Greenhouse — no per-job detail fetch needed.

The `DataSourceId` is a per-site GUID printed inline on the careers page
(`adpJobList = { dataSourceId: '...' }`); it lives in config/employers.yaml.

Confirmed 2026-08-17 against www.nyserda.ny.gov (NYSERDA + NY Green Bank).
"""

import logging

from career_radar.fetchers.http import request_json

logger = logging.getLogger(__name__)


def fetch_jobs(host: str, data_source_id: str) -> list[dict]:
    """Fetch every open requisition from one ADP job-list proxy endpoint."""
    url = f"https://{host}/rapi/adpjoblistapi/GetRequisitions"
    params = {"DataSourceId": data_source_id, "ReqId": "", "Department": "",
              "Location": "", "Type": ""}
    payload = request_json("GET", url, params=params)
    requisitions = (payload or {}).get("Requisitions") or []
    logger.info("ADP %s: %d requisitions", host, len(requisitions))
    return requisitions
