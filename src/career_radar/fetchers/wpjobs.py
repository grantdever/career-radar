"""Fetcher for WordPress job boards exposed through the WP REST API.

A lot of policy shops run their careers page as a WordPress custom post type
(the "WP Job Openings" plugin registers `awsm_job_openings`; hand-rolled
themes often register a plain `job` type). Either way `wp-json/wp/v2/{type}`
is a clean paged JSON collection whose `content.rendered` holds the whole job
description, so this behaves like Greenhouse: one listing call, no per-job
detail fetch.

Confirmed 2026-08-17:
  taxfoundation.org        -> post type `awsm_job_openings`
  www.niskanencenter.org   -> post type `job` (Cloudflare in front, so a
                              browser User-Agent is required; the default
                              `requests` UA gets a 403)

Location, when the site uses the plugin's taxonomies, is only exposed on the
`class_list` term slugs (`job-location-remote`), so it is recovered from
there; sites without those taxonomies simply have no location and the
pre-filter leaves them for the scorer.
"""

import logging

from career_radar.fetchers.http import request_json

logger = logging.getLogger(__name__)

# Several of these sites sit behind Cloudflare, which 403s the default
# `requests` User-Agent. A plain browser UA is enough; no cookies needed.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

PER_PAGE = 100
MAX_PAGES = 10


def fetch_jobs(host: str, post_type: str) -> list[dict]:
    """Fetch every published posting of one WP custom post type.

    Pages until a short page comes back (or MAX_PAGES, so a misconfigured
    endpoint can't loop forever). WP REST returns a 400 past the last page,
    which `request_json` raises; that is treated as "no more pages" only when
    at least one page already succeeded.
    """
    base = f"https://{host}/wp-json/wp/v2/{post_type}"
    jobs: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        params = {"per_page": PER_PAGE, "page": page, "status": "publish"}
        try:
            batch = request_json("GET", base, params=params, headers=HEADERS)
        except Exception as e:
            if jobs:
                logger.debug("WP %s/%s: page %d ended paging (%s)", host, post_type, page, e)
                break
            raise
        if not isinstance(batch, list):
            logger.warning("WP %s/%s: unexpected payload type %s", host, post_type, type(batch))
            break
        jobs.extend(batch)
        if len(batch) < PER_PAGE:
            break
    logger.info("WP %s/%s: %d jobs", host, post_type, len(jobs))
    return jobs
