"""Normalize raw ATS payloads into the common posting schema."""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html.parser import HTMLParser

HOURLY_ANNUALIZATION = 2080  # hourly ranges are annualized for comparability
HOURLY_THRESHOLD = 500  # a "salary" number below this is assumed to be per-hour

# Workday req IDs take several forms (R12345, JR102757, R-40937, R0291673,
# REQ_232113) and bulletFields[0] is NOT always the req ID — Rochester
# Regional Health puts location first and the ID at index 2. Detect the ID by
# shape instead of position.
_REQ_ID_RE = re.compile(r"^(?:REQ_|JR|R-?)\d")


@dataclass
class Posting:
    """Common schema every fetcher normalizes into (matches the DB row)."""

    source: str
    employer: str
    title: str
    req_id: str
    url: str
    location: str
    department: str
    remote_type: str
    salary_min: int | None
    salary_max: int | None
    posted_date: str  # ISO date; approximate for "Posted N Days Ago" formats
    description: str


class _TextExtractor(HTMLParser):
    """Strip tags from HTML, preserving rough block separation."""

    def __init__(self) -> None:
        super().__init__()
        self.chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self.chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("p", "li", "div", "br", "ul", "h1", "h2", "h3", "h4"):
            self.chunks.append("\n")


def html_to_text(html: str) -> str:
    """Convert an HTML description to plain text with normalized whitespace."""
    parser = _TextExtractor()
    parser.feed(html or "")
    text = "".join(parser.chunks)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def extract_element_by_id(html: str, element_id: str) -> str:
    """Return the inner HTML of the first element with the given id attribute.

    Tracks nested same-name tags with a depth counter so a description div
    containing inner divs (Breezy, JazzHR) is returned whole rather than cut
    at the first inner close. Returns "" when the element isn't found.
    """
    m = re.search(
        r'<([a-zA-Z][\w-]*)[^>]*\bid\s*=\s*["\']' + re.escape(element_id) + r'["\']',
        html or "",
        re.S,
    )
    if not m:
        return ""
    tag = m.group(1)
    gt = (html or "").find(">", m.end())
    if gt == -1:
        return ""
    open_re = re.compile(r"<" + tag + r"(?=[\s/>])")
    close_re = re.compile(r"</" + tag + r"\s*>")
    depth = 1
    pos = gt + 1
    start = gt + 1
    while depth > 0:
        o = open_re.search(html, pos)
        c = close_re.search(html, pos)
        if c is None:
            return html[start:]
        if o is not None and o.start() < c.start():
            depth += 1
            pos = o.end()
        else:
            depth -= 1
            if depth == 0:
                return html[start:c.start()]
            pos = c.end()
    return ""


def parse_posted_on(posted_on: str, today: date) -> str:
    """Convert Workday's relative postedOn text to an approximate ISO date.

    "Posted 30+ Days Ago" floors at 30 days; unrecognized text falls back to
    today so a parsing surprise never drops a posting.
    """
    text = (posted_on or "").lower()
    if "today" in text:
        return today.isoformat()
    if "yesterday" in text:
        return (today - timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\+?\s*days?\s*ago", text)
    if m:
        return (today - timedelta(days=int(m.group(1)))).isoformat()
    return today.isoformat()


_RANGE_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s*(?:-|–|—|to)\s*\$?\s*([\d,]+(?:\.\d+)?)"
)


def parse_salary(text: str) -> tuple[int | None, int | None]:
    """Extract an annualized (min, max) salary range from description text.

    Prefers a range near a "Compensation" label (the UR Workday pattern),
    falling back to the first dollar range anywhere — NY pay-transparency law
    means most postings state one somewhere. Values that look hourly are
    annualized. Returns (None, None) when no range is found.
    """
    if not text:
        return None, None
    comp_idx = text.lower().find("compensation")
    m = None
    if comp_idx >= 0:
        window = _RANGE_RE.search(text[comp_idx:comp_idx + 300])
        if window:
            # The 300-char window can cut a number in half ("$65,000–$85,"),
            # which would read as an $85 max. Re-match at the same offset
            # against the untruncated text so the number is complete.
            m = _RANGE_RE.search(text, comp_idx + window.start()) or window
    m = m or _RANGE_RE.search(text)
    if not m:
        return None, None
    lo = float(m.group(1).replace(",", ""))
    hi = float(m.group(2).replace(",", ""))
    if lo > hi:
        lo, hi = hi, lo
    if hi < HOURLY_THRESHOLD:
        lo *= HOURLY_ANNUALIZATION
        hi *= HOURLY_ANNUALIZATION
    return int(lo), int(hi)


def extract_req_id(listing: dict) -> str:
    """Extract a Workday posting's req ID regardless of bulletFields order.

    Scans bulletFields for the first token shaped like a req ID (R12345,
    JR102757, R-40937, REQ_232113), then falls back to the first bullet or the
    externalPath. Never key on position — RRH's index 0 is a location.
    """
    bullets = listing.get("bulletFields") or []
    for b in bullets:
        s = str(b).strip()
        if _REQ_ID_RE.match(s):
            return s
    if bullets:
        return str(bullets[0]).strip()
    return str(listing.get("externalPath", "")).strip()


def _parse_iso_datetime(value, today: date) -> str:
    """Parse an ISO datetime (Greenhouse/Ashby) to an approximate ISO date.

    Lenient: accepts tz-aware or naive timestamps and date-only strings;
    anything unrecognized falls back to today so a parsing surprise never
    drops a posting.
    """
    if not value:
        return today.isoformat()
    s = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).date().isoformat()
    except ValueError:
        try:
            return date.fromisoformat(s[:10]).isoformat()
        except ValueError:
            return today.isoformat()


def _parse_epoch_ms(value, today: date) -> str:
    """Parse Lever's epoch-millis createdAt to an approximate ISO date."""
    if value is None:
        return today.isoformat()
    try:
        return datetime.fromtimestamp(int(value) / 1000).date().isoformat()
    except (ValueError, TypeError, OSError):
        return today.isoformat()


def _remote_from_location(location: str) -> str:
    """Best-effort remote flag when the ATS has no explicit remote field."""
    return "Remote" if "remote" in (location or "").lower() else ""


def from_greenhouse(
    job: dict,
    source: str,
    employer: str,
    today: date,
) -> Posting:
    """Build a Posting from a Greenhouse board job dict (content included)."""
    description = html_to_text(job.get("content", ""))
    salary_min, salary_max = parse_salary(description)
    location_obj = job.get("location") or {}
    location = location_obj.get("name", "") if isinstance(location_obj, dict) else str(location_obj)
    return Posting(
        source=source,
        employer=employer,
        title=(job.get("title") or "").strip(),
        req_id=str(job.get("id", "")),
        url=job.get("absolute_url", ""),
        location=location,
        department="",
        remote_type=_remote_from_location(location),
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=_parse_iso_datetime(job.get("updated_at"), today),
        description=description,
    )


def from_lever(
    posting: dict,
    source: str,
    employer: str,
    today: date,
) -> Posting:
    """Build a Posting from a Lever posting dict (mode=json)."""
    description = html_to_text(posting.get("description", "")) or posting.get("descriptionPlain", "")
    additional = posting.get("additionalPlain", "") or html_to_text(posting.get("additional", ""))
    salary_min, salary_max = parse_salary(f"{additional}\n{description}")
    categories = posting.get("categories") or {}
    location = categories.get("location", "") or ""
    workplace = (posting.get("workplaceType") or "").strip()
    return Posting(
        source=source,
        employer=employer,
        title=(posting.get("text") or "").strip(),
        req_id=str(posting.get("id", "")),
        url=posting.get("hostedUrl", ""),
        location=location,
        department="",
        remote_type=workplace or _remote_from_location(location),
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=_parse_epoch_ms(posting.get("createdAt"), today),
        description=description,
    )


def _ashby_salary(comp: dict) -> tuple[int | None, int | None]:
    """Extract (min, max) from Ashby's structured compensation payload.

    Prefers the summary salary component (min/max as real numbers), falling
    back to the first tier's salary component, then to a text parse of the
    tier summary (which uses en-dashes and `K` suffixes that the range regex
    can't always read).
    """
    def _salary_component(components):
        for c in components or []:
            if c.get("compensationType") == "Salary":
                lo, hi = c.get("minValue"), c.get("maxValue")
                if lo is not None and hi is not None:
                    return int(lo), int(hi)
        return None

    found = _salary_component(comp.get("summaryComponents"))
    if found:
        return found
    for tier in comp.get("compensationTiers") or []:
        found = _salary_component(tier.get("components"))
        if found:
            return found
    summary = comp.get("compensationTierSummary", "") or ""
    return parse_salary(summary)


def from_ashby(
    job: dict,
    source: str,
    employer: str,
    today: date,
) -> Posting:
    """Build a Posting from an Ashby board job dict (description + comp included)."""
    description = html_to_text(job.get("descriptionHtml", "")) or job.get("descriptionPlain", "")
    comp = job.get("compensation") or {}
    salary_min, salary_max = _ashby_salary(comp)
    if salary_min is None:
        salary_min, salary_max = parse_salary(description)
    location = job.get("location", "") or ""
    workplace = (job.get("workplaceType") or "").strip()
    published = job.get("publishedAt") or job.get("publishedDate")
    url = job.get("applyUrl") or job.get("jobUrl", "")
    return Posting(
        source=source,
        employer=employer,
        title=(job.get("title") or "").strip(),
        req_id=str(job.get("id", "")),
        url=url,
        location=location,
        department="",
        remote_type=workplace or ("Remote" if job.get("isRemote") else ""),
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=_parse_iso_datetime(published, today),
        description=description,
    )


def from_workday(
    listing: dict,
    detail: dict,
    source: str,
    employer: str,
    host: str,
    site: str,
    today: date,
) -> Posting:
    """Build a Posting from a Workday listing summary plus its detail payload."""
    external_path = listing.get("externalPath", "")
    req_id = extract_req_id(listing) or external_path
    description = html_to_text(detail.get("jobDescription", ""))
    salary_min, salary_max = parse_salary(description)
    return Posting(
        source=source,
        employer=employer,
        title=listing.get("title", "").strip(),
        req_id=req_id,
        url=f"https://{host}/{site}{external_path}",
        location=listing.get("locationsText", ""),
        department="",
        remote_type=listing.get("remoteType", "") or detail.get("remoteType", "") or "",
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=parse_posted_on(listing.get("postedOn", ""), today),
        description=description,
    )


def _remote_from_workplace(value) -> str:
    """Map an ATS workplaceType enum (ON_SITE / REMOTE / HYBRID) to a label."""
    v = (value or "").strip().upper()
    if v == "REMOTE":
        return "Remote"
    if v == "HYBRID":
        return "Hybrid"
    return ""


def from_rippling(
    item: dict,
    detail: dict,
    source: str,
    employer: str,
    today: date,
) -> Posting:
    """Build a Posting from a Rippling listing item plus its detail jobPost."""
    desc = detail.get("description") or {}
    description = html_to_text(
        f"{desc.get('company', '')}\n{desc.get('role', '')}"
    )
    salary_min, salary_max = parse_salary(description)
    work_locations = detail.get("workLocations") or []
    if work_locations:
        location = ", ".join(str(x) for x in work_locations)
    else:
        location = ", ".join(
            (loc.get("name") or "") for loc in (item.get("locations") or [])
        )
    remote_type = ""
    for loc in item.get("locations") or []:
        remote_type = _remote_from_workplace(loc.get("workplaceType"))
        if remote_type:
            break
    if not remote_type:
        remote_type = _remote_from_location(location)
    return Posting(
        source=source,
        employer=employer,
        title=(detail.get("name") or item.get("name") or "").strip(),
        req_id=str(detail.get("uuid") or item.get("id") or ""),
        url=item.get("url") or detail.get("url") or "",
        location=location,
        department="",
        remote_type=remote_type,
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=_parse_iso_datetime(detail.get("createdOn"), today),
        description=description,
    )


def from_paylocity(
    job: dict,
    description: str,
    source: str,
    employer: str,
    today: date,
) -> Posting:
    """Build a Posting from a Paylocity listing job plus its scraped description."""
    salary_min, salary_max = parse_salary(description)
    location = job.get("LocationName") or ""
    remote_type = "Remote" if job.get("IsRemote") else ""
    job_id = job.get("JobId")
    return Posting(
        source=source,
        employer=employer,
        title=(job.get("JobTitle") or "").strip(),
        req_id=str(job_id),
        url=f"https://recruiting.paylocity.com/Recruiting/Jobs/Details/{job_id}",
        location=location,
        department="",
        remote_type=remote_type,
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=_parse_iso_datetime(job.get("PublishedDate"), today),
        description=description,
    )


def from_jazzhr(
    listing: dict,
    detail: dict,
    source: str,
    employer: str,
    today: date,
) -> Posting:
    """Build a Posting from a JazzHR listing summary plus its scraped detail."""
    description = html_to_text(detail.get("description_html", ""))
    salary_min, salary_max = parse_salary(description)
    location = detail.get("location") or listing.get("location") or ""
    return Posting(
        source=source,
        employer=employer,
        title=(detail.get("title") or listing.get("title") or "").strip(),
        req_id=str(listing.get("req_id") or ""),
        url=listing.get("url") or "",
        location=location,
        department="",
        remote_type=_remote_from_location(location),
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=today.isoformat(),  # JazzHR exposes no posted date
        description=description,
    )


def _rendered(value) -> str:
    """Unwrap a WP REST `{"rendered": ...}` field (or pass a plain string through)."""
    if isinstance(value, dict):
        return value.get("rendered", "") or ""
    return str(value or "")


def _wp_location(class_list) -> str:
    """Recover a location from WP Job Openings `job-location-*` term slugs.

    The REST payload exposes taxonomy terms only through `class_list`, so
    `job-location-washington-d-c` becomes "Washington D C". Sites that don't
    register the taxonomy yield "" and the pre-filter treats that as unknown.
    """
    names = []
    for cls in class_list or []:
        slug = str(cls)
        if slug.startswith("job-location-"):
            names.append(slug[len("job-location-"):].replace("-", " ").strip().title())
    return ", ".join(names)


def from_wpjobs(
    job: dict,
    source: str,
    employer: str,
    today: date,
) -> Posting:
    """Build a Posting from a WordPress REST job custom-post-type record."""
    description = html_to_text(_rendered(job.get("content")))
    salary_min, salary_max = parse_salary(description)
    location = _wp_location(job.get("class_list"))
    return Posting(
        source=source,
        employer=employer,
        title=_rendered(job.get("title")).strip(),
        req_id=str(job.get("id", "")),
        url=job.get("link", ""),
        location=location,
        department="",
        remote_type=_remote_from_location(location),
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=_parse_iso_datetime(job.get("date_gmt") or job.get("date"), today),
        description=description,
    )


def _icims_location(posting: dict) -> str:
    """Best-effort location string from a schema.org JobPosting jobLocation."""
    places = posting.get("jobLocation") or []
    if isinstance(places, dict):
        places = [places]
    parts = []
    for place in places:
        address = (place or {}).get("address") or {}
        bits = [address.get("addressLocality"), address.get("addressRegion")]
        text = ", ".join(b for b in bits if b and b != "UNAVAILABLE")
        if text:
            parts.append(text)
    return "; ".join(parts)


def _schema_salary(posting: dict) -> tuple[int | None, int | None]:
    """Extract (min, max) from a schema.org `baseSalary` block, if present."""
    value = (posting.get("baseSalary") or {}).get("value") or {}
    lo, hi = value.get("minValue"), value.get("maxValue")
    if lo is None or hi is None:
        return None, None
    try:
        lo, hi = float(lo), float(hi)
    except (TypeError, ValueError):
        return None, None
    if hi < HOURLY_THRESHOLD:
        lo *= HOURLY_ANNUALIZATION
        hi *= HOURLY_ANNUALIZATION
    return int(lo), int(hi)


def from_icims(
    listing: dict,
    posting: dict,
    source: str,
    employer: str,
    today: date,
) -> Posting:
    """Build a Posting from an iCIMS listing card plus its JobPosting JSON-LD."""
    description = html_to_text(posting.get("description", ""))
    salary_min, salary_max = _schema_salary(posting)
    if salary_min is None:
        salary_min, salary_max = parse_salary(description)
    location = listing.get("location") or _icims_location(posting)
    # The scraped href carries `?in_iframe=1`; store the shareable page URL.
    url = (posting.get("url") or listing.get("url", "")).split("?in_iframe=")[0]
    # iCIMS synthesizes datePosted at request time (every job on a board comes
    # back stamped exactly two years before "now", to the same millisecond), so
    # it is useless for recency. Fall back to today like the JazzHR fetcher.
    return Posting(
        source=source,
        employer=employer,
        title=(posting.get("title") or listing.get("title") or "").strip(),
        req_id=str(listing.get("req_id") or ""),
        url=url,
        location=location,
        department="",
        remote_type=_remote_from_location(location),
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=today.isoformat(),
        description=description,
    )


def _adp_rate(value, unit_code: str) -> int | None:
    """Convert one ADP pay-rate field to an annual figure."""
    if value in (None, ""):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    # Unit codes are 'AN' (annual), 'HR'/'H' (hourly), 'MO', ... Anything
    # non-annual that looks like a wage is annualized the same way parse_salary
    # does, so downstream comparisons stay apples-to-apples.
    if (unit_code or "").upper().startswith("H") or amount < HOURLY_THRESHOLD:
        amount *= HOURLY_ANNUALIZATION
    return int(amount)


def from_adp_joblist(
    req: dict,
    source: str,
    employer: str,
    today: date,
) -> Posting:
    """Build a Posting from an ADP job-list requisition record."""
    description = html_to_text(req.get("Posting_longName", ""))
    salary_min = _adp_rate(req.get("MinimumRateValue"), req.get("MinimumRateUnitCode", ""))
    salary_max = _adp_rate(req.get("MaximumRateValue"), req.get("MaximumRateUnitCode", ""))
    if salary_min is None and salary_max is None:
        salary_min, salary_max = parse_salary(description)
    location = re.sub(r"\s*,\s*", ", ", (req.get("Locations") or "").strip())
    return Posting(
        source=source,
        employer=employer,
        title=(req.get("JobTitle") or req.get("Job_Name") or "").strip(),
        req_id=str(req.get("ClientRequisitionId") or ""),
        url=req.get("InternetAddress_uri") or req.get("Links_href", ""),
        location=location,
        department="",
        remote_type=_remote_from_location(location),
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=_parse_iso_datetime(req.get("PostDate") or req.get("EffectiveDate"), today),
        description=description,
    )


def from_breezy(
    item: dict,
    description: str,
    source: str,
    employer: str,
    today: date,
) -> Posting:
    """Build a Posting from a Breezy feed item plus its scraped description."""
    description = html_to_text(description)
    salary_min, salary_max = parse_salary(description)
    if salary_min is None:
        salary_min, salary_max = parse_salary(item.get("salary") or "")
    loc = item.get("location") or {}
    location = loc.get("name", "") if isinstance(loc, dict) else str(loc)
    remote_type = "Remote" if (isinstance(loc, dict) and loc.get("is_remote")) else _remote_from_location(location)
    return Posting(
        source=source,
        employer=employer,
        title=(item.get("name") or "").strip(),
        req_id=str(item.get("id") or item.get("friendly_id") or ""),
        url=item.get("url") or "",
        location=location,
        department="",
        remote_type=remote_type,
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=_parse_iso_datetime(item.get("published_date"), today),
        description=description,
    )

def from_usajobs(listing: dict, source: str, employer_name: str, today: date) -> Posting:
    desc = listing.get("MatchedObjectDescriptor", {})
    req_id = listing.get("MatchedObjectId")
    url = desc.get("PositionURI", "")

    salary_min = None
    salary_max = None
    remun = desc.get("PositionRemuneration", [])
    if remun:
        try:
            r = remun[0]
            salary_min = int(float(r.get("MinimumRange", 0)))
            salary_max = int(float(r.get("MaximumRange", 0)))
        except (ValueError, TypeError):
            pass

    # Extract description text from QualificationSummary or UserArea
    qual = desc.get("QualificationSummary", "")

    return Posting(
        source=source,
        employer=employer_name,
        title=desc.get("PositionTitle", ""),
        req_id=str(req_id),
        url=url,
        location=desc.get("PositionLocationDisplay", ""),
        department="",
        remote_type="remote" if "remote" in desc.get("PositionLocationDisplay", "").lower() else "unspecified",
        salary_min=salary_min,
        salary_max=salary_max,
        posted_date=today.isoformat(),
        description=qual
    )

def from_adzuna(listing: dict, source: str, employer_name: str, today: date) -> Posting:
    req_id = listing.get("id")
    url = listing.get("redirect_url", "")

    salary_min = listing.get("salary_min")
    salary_max = listing.get("salary_max")

    loc = listing.get("location", {}).get("display_name", "")

    return Posting(
        source=source,
        employer=listing.get("company", {}).get("display_name", employer_name),
        title=listing.get("title", ""),
        req_id=str(req_id),
        url=url,
        location=loc,
        department="",
        remote_type="remote" if "remote" in loc.lower() else "unspecified",
        salary_min=int(salary_min) if salary_min else None,
        salary_max=int(salary_max) if salary_max else None,
        posted_date=today.isoformat(),
        description=html_to_text(listing.get("description", ""))
    )

def from_universal_llm(listing: dict, source: str, employer_name: str, today: date) -> Posting:
    req_id = listing.get("req_id", "")
    url = listing.get("url", "")

    salary_min = listing.get("salary_min")
    salary_max = listing.get("salary_max")

    loc = listing.get("location", "")

    return Posting(
        source=source,
        employer=employer_name,
        title=listing.get("title", ""),
        req_id=str(req_id),
        url=url,
        location=loc,
        department="",
        remote_type="remote" if "remote" in loc.lower() else "unspecified",
        salary_min=int(salary_min) if salary_min else None,
        salary_max=int(salary_max) if salary_max else None,
        posted_date=today.isoformat(),
        description=listing.get("description", "")
    )
