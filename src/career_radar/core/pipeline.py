#!/usr/bin/env python3
"""Daily pipeline entry point: fetch -> pre-filter -> normalize -> dedupe -> score -> report.

Run from the repo root:
    .venv/bin/python -m src.pipeline [--skip-score]

Every run pages each employer's full listing (the listing endpoints can't
server-side filter by date or keyword, and paging is cheap), but only
never-before-seen postings that also pass the deterministic pre-filter
(src/prefilter.py) get a detail fetch and an LLM score. Irrelevant postings
are never stored, so the DB holds only plausibly-relevant rows.
"""

import logging
import os
import re
import time
from datetime import date
from pathlib import Path

from career_radar.core import dedupe, normalize, prefilter, report, score
from career_radar.fetchers import (
    adp_joblist,
    adzuna,
    ashby,
    breezy,
    greenhouse,
    icims,
    jazzhr,
    lever,
    paylocity,
    rippling,
    universal_llm,
    usajobs,
    workday,
    wpjobs,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent


def scan_workday_employer(
    conn, emp: dict, today: date, rules: prefilter.FilterRules | None = None
) -> int:
    """Fetch one Workday board; descriptions live on each job's detail endpoint."""
    active_rules = rules or prefilter.FilterRules.default()
    host, tenant, site = emp["host"], emp["tenant"], emp["site"]
    source = f"workday:{tenant}/{site}"
    listings = workday.fetch_listings(host, tenant, site)

    by_req: dict[str, dict] = {}
    for listing in listings:
        req_id = normalize.extract_req_id(listing) or listing.get("externalPath", "")
        by_req[req_id] = listing

    known = dedupe.known_req_ids(conn, source)
    new_ids = set(by_req) - known
    dedupe.touch_seen(conn, source, set(by_req) & known, today.isoformat())

    new_postings = []
    dropped = 0
    for req_id in sorted(new_ids):
        listing = by_req[req_id]
        ok, reason = active_rules.classify(
            listing.get("title", ""),
            listing.get("locationsText", ""),
            listing.get("remoteType", ""),
        )
        if not ok:
            dropped += 1
            logger.debug("  dropped [%s] %r", reason, listing.get("title", ""))
            continue
        external_path = listing.get("externalPath")
        if not external_path:
            # Some boards (notably global ones like Iberdrola) occasionally
            # carry talent-community / evergreen listings with no externalPath;
            # they can't be fetched or linked, so skip rather than crash.
            dropped += 1
            logger.debug("  dropped [no externalPath] %r", listing.get("title", ""))
            continue
        detail = workday.fetch_detail(host, tenant, site, external_path)
        new_postings.append(
            normalize.from_workday(listing, detail, source, emp["name"], host, site, today)
        )
        if len(new_postings) % 25 == 0:
            logger.info("%s: fetched detail %d", emp["name"], len(new_postings))
        time.sleep(0.3)
    dedupe.insert_new(conn, new_postings, today.isoformat())
    logger.info("%s: %d on board, %d new, %d kept (%d filtered)",
                emp["name"], len(by_req), len(new_ids), len(new_postings), dropped)
    return len(new_postings)


def _record_postings(
    conn,
    source: str,
    postings: list[normalize.Posting],
    today: date,
    rules: prefilter.FilterRules | None = None,
) -> int:
    """Pre-filter new postings, insert survivors, touch last_seen for known ones."""
    active_rules = rules or prefilter.FilterRules.default()
    by_req: dict[str, normalize.Posting] = {p.req_id: p for p in postings if p.req_id}
    known = dedupe.known_req_ids(conn, source)
    new = {k: v for k, v in by_req.items() if k not in known}
    dedupe.touch_seen(conn, source, set(by_req) & known, today.isoformat())
    kept, dropped = active_rules.apply(list(new.values()))
    dedupe.insert_new(conn, kept, today.isoformat())
    if dropped:
        logger.info("%s: pre-filter dropped %d new postings (%d kept)",
                    source, len(dropped), len(kept))
        for p, reason in dropped:
            logger.debug("  dropped [%s] %r", reason, p.title)
    return len(kept)


def scan_greenhouse_employer(
    conn, emp: dict, today: date, rules: prefilter.FilterRules | None = None
) -> int:
    """Fetch one Greenhouse board; the listing call already carries descriptions."""
    board = emp["board"]
    source = f"greenhouse:{board}"
    jobs = greenhouse.fetch_jobs(board)
    postings = [normalize.from_greenhouse(j, source, emp["name"], today) for j in jobs]
    n_new = _record_postings(conn, source, postings, today, rules=rules)
    logger.info("%s: %d on board, %d new", emp["name"], len(postings), n_new)
    return n_new


def scan_lever_employer(
    conn, emp: dict, today: date, rules: prefilter.FilterRules | None = None
) -> int:
    """Fetch one Lever company; the listing call already carries descriptions."""
    company = emp["company"]
    source = f"lever:{company}"
    raw = lever.fetch_postings(company)
    postings = [normalize.from_lever(p, source, emp["name"], today) for p in raw]
    n_new = _record_postings(conn, source, postings, today, rules=rules)
    logger.info("%s: %d on board, %d new", emp["name"], len(postings), n_new)
    return n_new


def scan_ashby_employer(
    conn, emp: dict, today: date, rules: prefilter.FilterRules | None = None
) -> int:
    """Fetch one Ashby board; the listing call already carries descriptions."""
    board = emp["board"]
    source = f"ashby:{board}"
    jobs = ashby.fetch_jobs(board)
    postings = [normalize.from_ashby(j, source, emp["name"], today) for j in jobs]
    n_new = _record_postings(conn, source, postings, today, rules=rules)
    logger.info("%s: %d on board, %d new", emp["name"], len(postings), n_new)
    return n_new


def _rippling_fields(item: dict) -> tuple[str, str, str]:
    """(title, location, remote_type) for a Rippling listing item."""
    locations = item.get("locations") or []
    location = ", ".join((loc.get("name") or "") for loc in locations)
    remote = ""
    for loc in locations:
        remote = normalize._remote_from_workplace(loc.get("workplaceType"))
        if remote:
            break
    return item.get("name", ""), location, remote


def scan_rippling_employer(
    conn, emp: dict, today: date, rules: prefilter.FilterRules | None = None
) -> int:
    """Fetch one Rippling board; descriptions/salary live on each job page."""
    active_rules = rules or prefilter.FilterRules.default()
    slug = emp["slug"]
    source = f"rippling:{slug}"
    items = rippling.fetch_listings(slug)

    by_req: dict[str, dict] = {it.get("id"): it for it in items if it.get("id")}
    known = dedupe.known_req_ids(conn, source)
    new_ids = set(by_req) - known
    dedupe.touch_seen(conn, source, set(by_req) & known, today.isoformat())

    new_postings = []
    dropped = 0
    for req_id in sorted(new_ids):
        item = by_req[req_id]
        title, location, remote = _rippling_fields(item)
        ok, reason = active_rules.classify(title, location, remote)
        if not ok:
            dropped += 1
            logger.debug("  dropped [%s] %r", reason, title)
            continue
        job_url = item.get("url") or f"https://ats.rippling.com/{slug}/jobs/{req_id}"
        detail = rippling.fetch_detail(job_url)
        new_postings.append(normalize.from_rippling(item, detail, source, emp["name"], today))
        time.sleep(0.2)
    dedupe.insert_new(conn, new_postings, today.isoformat())
    logger.info("%s: %d on board, %d new, %d kept (%d filtered)",
                emp["name"], len(by_req), len(new_ids), len(new_postings), dropped)
    return len(new_postings)


def scan_paylocity_employer(
    conn, emp: dict, today: date, rules: prefilter.FilterRules | None = None
) -> int:
    """Fetch one Paylocity board; the full description lives on each job page."""
    active_rules = rules or prefilter.FilterRules.default()
    company_id = emp["company_id"]
    source = f"paylocity:{company_id}"
    jobs = paylocity.fetch_listings(company_id)

    by_req: dict[str, dict] = {str(j.get("JobId")): j for j in jobs if j.get("JobId")}
    known = dedupe.known_req_ids(conn, source)
    new_ids = set(by_req) - known
    dedupe.touch_seen(conn, source, set(by_req) & known, today.isoformat())

    new_postings = []
    dropped = 0
    for req_id in sorted(new_ids):
        job = by_req[req_id]
        remote = "Remote" if job.get("IsRemote") else ""
        ok, reason = active_rules.classify(job.get("JobTitle", ""), job.get("LocationName", ""), remote)
        if not ok:
            dropped += 1
            logger.debug("  dropped [%s] %r", reason, job.get("JobTitle", ""))
            continue
        description = paylocity.fetch_detail(job["JobId"])
        new_postings.append(normalize.from_paylocity(job, description, source, emp["name"], today))
        time.sleep(0.2)
    dedupe.insert_new(conn, new_postings, today.isoformat())
    logger.info("%s: %d on board, %d new, %d kept (%d filtered)",
                emp["name"], len(by_req), len(new_ids), len(new_postings), dropped)
    return len(new_postings)


def scan_jazzhr_employer(
    conn, emp: dict, today: date, rules: prefilter.FilterRules | None = None
) -> int:
    """Fetch one JazzHR board (HTML scrape); descriptions live on each job page."""
    active_rules = rules or prefilter.FilterRules.default()
    slug = emp["slug"]
    source = f"jazzhr:{slug}"
    listings = jazzhr.fetch_listings(slug)

    by_req: dict[str, dict] = {item["req_id"]: item for item in listings if item.get("req_id")}
    known = dedupe.known_req_ids(conn, source)
    new_ids = set(by_req) - known
    dedupe.touch_seen(conn, source, set(by_req) & known, today.isoformat())

    new_postings = []
    dropped = 0
    for req_id in sorted(new_ids):
        listing = by_req[req_id]
        remote = normalize._remote_from_location(listing.get("location", ""))
        ok, reason = active_rules.classify(listing.get("title", ""), listing.get("location", ""), remote)
        if not ok:
            dropped += 1
            logger.debug("  dropped [%s] %r", reason, listing.get("title", ""))
            continue
        detail = jazzhr.fetch_detail(listing["url"])
        new_postings.append(normalize.from_jazzhr(listing, detail, source, emp["name"], today))
        time.sleep(0.2)
    dedupe.insert_new(conn, new_postings, today.isoformat())
    logger.info("%s: %d on board, %d new, %d kept (%d filtered)",
                emp["name"], len(by_req), len(new_ids), len(new_postings), dropped)
    return len(new_postings)


def scan_breezy_employer(
    conn, emp: dict, today: date, rules: prefilter.FilterRules | None = None
) -> int:
    """Fetch one Breezy board; the JSON feed lacks descriptions, so scrape them."""
    active_rules = rules or prefilter.FilterRules.default()
    slug = emp["slug"]
    source = f"breezy:{slug}"
    items = breezy.fetch_jobs(slug)

    def key_of(it: dict) -> str:
        return str(it.get("id") or it.get("friendly_id") or "")

    by_req: dict[str, dict] = {key_of(it): it for it in items if key_of(it)}
    known = dedupe.known_req_ids(conn, source)
    new_ids = set(by_req) - known
    dedupe.touch_seen(conn, source, set(by_req) & known, today.isoformat())

    new_postings = []
    dropped = 0
    for req_id in sorted(new_ids):
        item = by_req[req_id]
        loc = item.get("location") or {}
        location = loc.get("name", "") if isinstance(loc, dict) else str(loc)
        remote = "Remote" if (isinstance(loc, dict) and loc.get("is_remote")) else normalize._remote_from_location(location)
        ok, reason = active_rules.classify(item.get("name", ""), location, remote)
        if not ok:
            dropped += 1
            logger.debug("  dropped [%s] %r", reason, item.get("name", ""))
            continue
        description = breezy.fetch_detail(item.get("url", ""))
        new_postings.append(normalize.from_breezy(item, description, source, emp["name"], today))
        time.sleep(0.2)
    dedupe.insert_new(conn, new_postings, today.isoformat())
    logger.info("%s: %d on board, %d new, %d kept (%d filtered)",
                emp["name"], len(by_req), len(new_ids), len(new_postings), dropped)
    return len(new_postings)


def scan_wpjobs_employer(
    conn, emp: dict, today: date, rules: prefilter.FilterRules | None = None
) -> int:
    """Fetch one WordPress job custom post type; the REST call carries descriptions."""
    host, post_type = emp["host"], emp["post_type"]
    source = f"wpjobs:{host}/{post_type}"
    jobs = wpjobs.fetch_jobs(host, post_type)
    postings = [normalize.from_wpjobs(j, source, emp["name"], today) for j in jobs]
    n_new = _record_postings(conn, source, postings, today, rules=rules)
    logger.info("%s: %d on board, %d new", emp["name"], len(postings), n_new)
    return n_new


def scan_adp_joblist_employer(
    conn, emp: dict, today: date, rules: prefilter.FilterRules | None = None
) -> int:
    """Fetch one ADP job-list proxy; requisitions already carry descriptions."""
    host, data_source_id = emp["host"], emp["data_source_id"]
    source = f"adp_joblist:{host}/{data_source_id}"
    reqs = adp_joblist.fetch_jobs(host, data_source_id)
    postings = [normalize.from_adp_joblist(r, source, emp["name"], today) for r in reqs]
    n_new = _record_postings(conn, source, postings, today, rules=rules)
    logger.info("%s: %d on board, %d new", emp["name"], len(postings), n_new)
    return n_new


def scan_icims_employer(
    conn, emp: dict, today: date, rules: prefilter.FilterRules | None = None
) -> int:
    """Fetch one iCIMS portal; descriptions live in each job page's JSON-LD."""
    active_rules = rules or prefilter.FilterRules.default()
    subdomain = emp["subdomain"]
    source = f"icims:{subdomain}"
    listings = icims.fetch_listings(subdomain)

    by_req: dict[str, dict] = {item["req_id"]: item for item in listings if item.get("req_id")}
    known = dedupe.known_req_ids(conn, source)
    new_ids = set(by_req) - known
    dedupe.touch_seen(conn, source, set(by_req) & known, today.isoformat())

    new_postings = []
    dropped = 0
    for req_id in sorted(new_ids):
        listing = by_req[req_id]
        location = listing.get("location", "")
        ok, reason = active_rules.classify(
            listing.get("title", ""), location, normalize._remote_from_location(location)
        )
        if not ok:
            dropped += 1
            logger.debug("  dropped [%s] %r", reason, listing.get("title", ""))
            continue
        posting = icims.fetch_detail(listing["url"])
        new_postings.append(normalize.from_icims(listing, posting, source, emp["name"], today))
        time.sleep(0.2)
    dedupe.insert_new(conn, new_postings, today.isoformat())
    logger.info("%s: %d on board, %d new, %d kept (%d filtered)",
                emp["name"], len(by_req), len(new_ids), len(new_postings), dropped)
    return len(new_postings)


def scan_usajobs_employer(
    conn, emp: dict, today: date, rules: prefilter.FilterRules | None = None
) -> int:
    active_rules = rules or prefilter.FilterRules.default()
    loc = emp.get("location", "Remote")
    emp_slug = re.sub(r"[^a-z0-9]+", "_", emp.get("name", "usajobs").lower()).strip("_")
    loc_slug = re.sub(r"[^a-z0-9]+", "_", loc.lower()).strip("_")
    source = f"usajobs:{emp_slug}:{loc_slug}"
    api_key = os.environ.get("USAJOBS_API_KEY")
    if not api_key:
        logger.warning("USAJOBS_API_KEY not found in environment or .env")
        return 0
    listings = usajobs.fetch_listings(api_key, loc)

    by_req = {str(item.get("MatchedObjectId")): item for item in listings if item.get("MatchedObjectId")}
    known = dedupe.known_req_ids(conn, source)
    new_ids = set(by_req) - known
    dedupe.touch_seen(conn, source, set(by_req) & known, today.isoformat())

    new_postings = []
    dropped = 0
    for req_id in sorted(new_ids):
        listing = by_req[req_id]
        desc = listing.get("MatchedObjectDescriptor", {})
        ok, reason = active_rules.classify(
            desc.get("PositionTitle", ""),
            desc.get("PositionLocationDisplay", ""),
            "remote" if "remote" in desc.get("PositionLocationDisplay", "").lower() else ""
        )
        if not ok:
            dropped += 1
            continue
        new_postings.append(normalize.from_usajobs(listing, source, emp["name"], today))

    dedupe.insert_new(conn, new_postings, today.isoformat())
    logger.info("%s: %d on board, %d new, %d kept (%d filtered)", emp["name"], len(by_req), len(new_ids), len(new_postings), dropped)
    return len(new_postings)


def scan_adzuna_employer(
    conn, emp: dict, today: date, rules: prefilter.FilterRules | None = None
) -> int:
    active_rules = rules or prefilter.FilterRules.default()
    where = emp.get("where", "Remote")
    what = emp.get("what", "")
    emp_slug = re.sub(r"[^a-z0-9]+", "_", emp.get("name", "adzuna").lower()).strip("_")
    where_slug = re.sub(r"[^a-z0-9]+", "_", where.lower()).strip("_")
    what_slug = re.sub(r"[^a-z0-9]+", "_", what.lower()).strip("_") if what else "all"
    source = f"adzuna:{emp_slug}:{where_slug}:{what_slug}"
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        logger.warning("ADZUNA_APP_ID or ADZUNA_APP_KEY not found in environment or .env")
        return 0
    listings = adzuna.fetch_listings(app_id, app_key, where, what)

    by_req = {str(item.get("id")): item for item in listings if item.get("id")}
    known = dedupe.known_req_ids(conn, source)
    new_ids = set(by_req) - known
    dedupe.touch_seen(conn, source, set(by_req) & known, today.isoformat())

    new_postings = []
    dropped = 0
    for req_id in sorted(new_ids):
        listing = by_req[req_id]
        loc = listing.get("location", {}).get("display_name", "")
        ok, reason = active_rules.classify(
            listing.get("title", ""),
            loc,
            "remote" if "remote" in loc.lower() else ""
        )
        if not ok:
            dropped += 1
            continue
        new_postings.append(normalize.from_adzuna(listing, source, emp["name"], today))

    dedupe.insert_new(conn, new_postings, today.isoformat())
    logger.info("%s: %d on board, %d new, %d kept (%d filtered)", emp["name"], len(by_req), len(new_ids), len(new_postings), dropped)
    return len(new_postings)


def scan_universal_llm_employer(
    conn,
    emp: dict,
    today: date,
    rules: prefilter.FilterRules | None = None,
    config_dir: Path | str | None = None,
) -> int:
    active_rules = rules or prefilter.FilterRules.default()
    source = f"universal:{emp['name']}"
    url = emp.get("url")
    if not url:
        logger.warning(f"No URL provided for universal_llm employer {emp['name']}")
        return 0

    listings = universal_llm.fetch_listings(url, emp["name"], config_dir=config_dir)

    by_req = {str(item.get("req_id", i)): item for i, item in enumerate(listings)}
    known = dedupe.known_req_ids(conn, source)
    new_ids = set(by_req) - known
    dedupe.touch_seen(conn, source, set(by_req) & known, today.isoformat())

    new_postings = []
    dropped = 0
    for req_id in sorted(new_ids):
        listing = by_req[req_id]
        loc = listing.get("location", "")
        ok, reason = active_rules.classify(
            listing.get("title", ""),
            loc,
            "remote" if "remote" in loc.lower() else ""
        )
        if not ok:
            dropped += 1
            continue
        new_postings.append(normalize.from_universal_llm(listing, source, emp["name"], today))

    dedupe.insert_new(conn, new_postings, today.isoformat())
    logger.info("%s: %d on board, %d new, %d kept (%d filtered)", emp["name"], len(by_req), len(new_ids), len(new_postings), dropped)
    return len(new_postings)

_SCANNERS = {
    'universal_llm': scan_universal_llm_employer,
    'usajobs': scan_usajobs_employer,
    'adzuna': scan_adzuna_employer,
    "workday": scan_workday_employer,
    "greenhouse": scan_greenhouse_employer,
    "lever": scan_lever_employer,
    "ashby": scan_ashby_employer,
    "rippling": scan_rippling_employer,
    "paylocity": scan_paylocity_employer,
    "jazzhr": scan_jazzhr_employer,
    "breezy": scan_breezy_employer,
    "wpjobs": scan_wpjobs_employer,
    "icims": scan_icims_employer,
    "adp_joblist": scan_adp_joblist_employer,
}



def run_pipeline(
    skip_score: bool = False,
    config_dir: Path | str | None = None,
    db_path: Path | str | None = None,
    output_dir: Path | str | None = None,
) -> None:
    import os

    from career_radar.config import get_config_dir, load_employers
    from career_radar.core import dedupe

    cfg_dir = get_config_dir(config_dir)
    # Load .env from current working directory or config directory
    try:
        from dotenv import load_dotenv
        for env_file in [Path.cwd() / ".env", cfg_dir / ".env"]:
            if env_file.exists():
                load_dotenv(env_file)
    except ImportError:
        for env_file in [Path.cwd() / ".env", cfg_dir / ".env"]:
            if env_file.exists():
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ[k.strip()] = v.strip().strip("'").strip('"')

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    employers = load_employers(config_dir=cfg_dir)
    target_db = Path(db_path) if db_path else dedupe.DB_PATH
    out_dir = Path(output_dir) if output_dir else (Path.home() / ".local" / "share" / "career-radar" / "output")
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = dedupe.connect(target_db)
    today = date.today()

    try:
        if not employers:
            logger.warning("No employers found in config. Please add some to %s/employers.yaml", cfg_dir)
            report.write_shortlist(conn, out_dir, today.isoformat())
            return

        rules = prefilter.FilterRules.from_config_dir(cfg_dir)

        for emp in employers:
            ats = emp.get("ats")
            if not ats:
                continue
            scanner = _SCANNERS.get(ats)
            if scanner:
                try:
                    if ats == "universal_llm":
                        scanner(conn, emp, today, rules=rules, config_dir=cfg_dir)
                    else:
                        scanner(conn, emp, today, rules=rules)
                except Exception as e:
                    logger.error("Failed to scan %s: %s", emp.get("name"), e)
            else:
                logger.warning("Unknown ATS type %r for %s", ats, emp.get("name"))

        if not skip_score:
            score.score_unscored(conn, cfg_dir / "criteria.md", config_dir=cfg_dir)

        report.write_shortlist(conn, out_dir, today.isoformat())
    finally:
        conn.close()
