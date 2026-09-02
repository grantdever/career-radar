"""Fetcher for JazzHR job boards ({slug}.applytojob.com/apply).

JazzHR (the old Resumator) serves a server-rendered HTML board with no clean
public JSON. The listing page has one `<li class="list-group-item">` per job
(title + link + location); the detail page has the full description in a
`#job-description` div plus a location and an employment-type attribute. This
fetcher scrapes both (scraping approved 2026-08-13 for JazzHR/Breezy, whose
boards are HTML-only or lack descriptions in their JSON feed).

There is no posted-date field anywhere in the JazzHR pages, so normalize.py
falls back to today for recency.
"""

import logging
import re

from career_radar.core.normalize import extract_element_by_id
from career_radar.fetchers.http import request_text

logger = logging.getLogger(__name__)

# Capture each listing card up to the next card (or the list's closing </ul>)
# so the nested location <li> inside each card isn't mistaken for the card's
# own closing tag.
_LIST_ITEM_RE = re.compile(
    r'<li class="list-group-item">(.*?)(?=<li class="list-group-item">|</ul>)', re.S
)
_LINK_RE = re.compile(r'<a\s+href="([^"]+)">(.*?)</a>', re.S)
_MAP_MARKER_RE = re.compile(r"fa-map-marker[^>]*></i>\s*(.*?)\s*</li>", re.S)
# The detail page opens with a "Thanks for visiting..." <h2>, and the
# stylesheet also mentions `.job-header` (which a bare string search would hit
# first). Anchor to the actual `<div class='job-header'>` element and take the
# first <h2> inside it.
_TITLE_RE = re.compile(r"<div[^>]*class=['\"]job-header['\"][^>]*>.*?<h2>(.*?)</h2>", re.S)
_ATTR_RE = re.compile(r'<div title="Location">\s*<i[^>]*></i>\s*(.*?)\s*</div>', re.S)
_EMPLOYMENT_RE = re.compile(
    r"id=['\"]resumator-job-employment['\"][^>]*>\s*<i[^>]*></i>\s*(.*?)\s*</div>", re.S
)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def fetch_listings(slug: str) -> list[dict]:
    """Fetch every live posting summary on a JazzHR board (no detail yet)."""
    url = f"https://{slug}.applytojob.com/apply"
    text = request_text("GET", url)
    listings = []
    for block in _LIST_ITEM_RE.findall(text):
        m = _LINK_RE.search(block)
        if not m:
            continue
        href, title = m.group(1), _clean(m.group(2))
        location = ""
        lm = _MAP_MARKER_RE.search(block)
        if lm:
            location = _clean(lm.group(1))
        parts = href.rstrip("/").split("/")
        req_id = parts[-2] if len(parts) >= 2 else ""
        listings.append({"title": title, "url": href, "location": location, "req_id": req_id})
    logger.info("JazzHR %s: %d jobs", slug, len(listings))
    return listings


def fetch_detail(job_url: str) -> dict:
    """Fetch one job's title/location/type/description HTML from its detail page."""
    text = request_text("GET", job_url)
    title = _clean(_TITLE_RE.search(text).group(1)) if _TITLE_RE.search(text) else ""
    location = _clean(_ATTR_RE.search(text).group(1)) if _ATTR_RE.search(text) else ""
    employment_type = ""
    em = _EMPLOYMENT_RE.search(text)
    if em:
        employment_type = _clean(em.group(1))
    description_html = extract_element_by_id(text, "job-description")
    return {
        "title": title,
        "location": location,
        "employment_type": employment_type,
        "description_html": description_html,
    }
