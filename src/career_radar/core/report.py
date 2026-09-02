"""Emit the daily shortlist as four status-aware sections.

    New (7+, not yet shown)          -> what the inbox will prompt next
    Awaiting your review (shown)     -> surfaced but no verdict yet
    Interested / apply               -> the running list of "yes" postings
    Borderline (5-6)                 -> calibration material for criteria.md

`not_interested` postings never appear. This file is the daily artifact (and
what Cowork will read in phase 4); src/review.py is the interactive surface
that actually records verdicts.
"""

import json
import logging
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from career_radar.core import dedupe

logger = logging.getLogger(__name__)

REVIEW_QUEUE_SIZE = 6
PER_EMPLOYER_CAP = 5
# Borderline rows are collapsed before they are trimmed to REVIEW_QUEUE_SIZE,
# so the query has to over-fetch or the locale duplicates eat the whole slice.
BORDERLINE_FETCH_MULTIPLIER = 8
PRECISION_WINDOW_DAYS = 30


def fmt_salary(row: sqlite3.Row) -> str:
    """Salary range for display — shared by the shortlist, inbox, and tracker."""
    if row["salary_min"] is None:
        return "salary not stated"
    return f"${row['salary_min']:,} - ${row['salary_max']:,}"


def fmt_remote(row: sqlite3.Row) -> str:
    """Remote-type for display; ATS rows with no value are on-site roles."""
    return row["remote_type"] or "on-site"


def _fmt_row(row: sqlite3.Row, today: str) -> str:
    flags = json.loads(row["flags"]) if row["flags"] else []
    flag_text = " ".join(f"`[{f}]`" for f in flags)
    closed = "" if row["last_seen"] == today else " · **may be closed**"
    line = (
        f"- **{row['title']}** — {row['employer']} ({row['score']}/10) {flag_text}\n"
        f"  - {fmt_salary(row)} · {row['location']} · posted {row['posted_date']}{closed}\n"
        f"  - {row['rationale']}\n"
        f"  - {row['url']}"
    )
    return line


def _section(lines: list[str], title: str, rows: list[sqlite3.Row], today: str) -> None:
    lines += [f"## {title}", ""]
    if rows:
        lines += [_fmt_row(r, today) + "\n" for r in rows]
    else:
        lines += ["None.", ""]


# --- Locale duplicate collapse ----------------------------------------------
# Boards sometimes publish one job as several reqs, one per locale.
# Each is a distinct req_id, so dedupe.py stores them all, but showing
# the exact same job multiple times is noise. Collapse at report/review time
# rather than at insert time so no raw data is lost from the DB.

_TITLE_SPLIT_RE = re.compile(r"\s*[|\u2014]\s*")
_TRAILING_LOCALE_RE = re.compile(r"^(.*?)\s*[-,]\s*([^-,]+)$")
_BARE_MODE_RE = re.compile(r"^(remote|hybrid|on[- ]?site|onsite)$", re.IGNORECASE)

_US_STATE_CODES = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|"
    "MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC"
)
_US_RE = re.compile(
    rf"\b(United States|USA|U\.S\.A\.|U\.S\.|US|{_US_STATE_CODES})\b", re.IGNORECASE
)
_FOREIGN_RE = re.compile(
    r"\b(Canada|UK|United Kingdom|Great Britain|England|Ireland|Germany|Deutschland|"
    r"France|Spain|España|Italy|Italia|Netherlands|Australia|New Zealand|Japan|Singapore|"
    r"Brazil|Brasil|Mexico|India|Poland|Sweden|Switzerland|EMEA|APAC|LATAM|Americas|Europe)\b",
    re.IGNORECASE,
)


def _is_locale_segment(segment: str) -> bool:
    """True when a title fragment is geography rather than job description."""
    seg = segment.strip()
    if not seg:
        return True
    if _BARE_MODE_RE.match(seg):
        return True
    return bool(_FOREIGN_RE.search(seg) or _US_RE.search(seg))


def dedupe_title(title: str) -> str:
    """Strip locale decoration from a title so locale variants share a key."""
    segments = [s for s in _TITLE_SPLIT_RE.split(title or "") if not _is_locale_segment(s)]
    base = " ".join(segments) if segments else (title or "")
    # Titles also tack the locale on with a dash or comma ("Solutions Architect
    # - France"). Peel at most two, so "Analyst, Sales Strategy" keeps its comma.
    for _ in range(2):
        match = _TRAILING_LOCALE_RE.match(base)
        if not (match and _is_locale_segment(match.group(2))):
            break
        base = match.group(1)
    return re.sub(r"[^a-z0-9]+", " ", base.lower()).strip()


def _survivor_rank(row: sqlite3.Row) -> tuple:
    """Sort key picking which duplicate to keep: best paid, then newest."""
    return (
        -(row["salary_max"] or 0),
        -(row["salary_min"] or 0),
        row["posted_date"] or "",
    )


def collapse_duplicates(
    rows: list[sqlite3.Row],
) -> tuple[list[sqlite3.Row], int]:
    """Keep one row per (employer, locale-stripped title), preserving input order.

    Returns (kept, n_collapsed). The survivor of each group is chosen by
    _survivor_rank, but is emitted at the position of the group's first row so
    the caller's score ordering still holds.
    """
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    order: list[tuple[str, str]] = []
    for row in rows:
        key = (row["employer"], dedupe_title(row["title"]))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)

    kept = [min(groups[key], key=_survivor_rank) for key in order]
    return kept, len(rows) - len(kept)


def cap_per_employer(
    rows: list[sqlite3.Row], limit: int = PER_EMPLOYER_CAP
) -> tuple[list[sqlite3.Row], dict[str, int]]:
    """Cap `rows` to at most `limit` per employer, preserving input order.

    Returns (kept, held_back) where held_back maps employer -> count of rows
    removed by the cap. Employers at or under the cap are omitted from
    held_back entirely.
    """
    seen: dict[str, int] = {}
    kept: list[sqlite3.Row] = []
    held_back: dict[str, int] = {}
    for row in rows:
        employer = row["employer"]
        seen[employer] = seen.get(employer, 0) + 1
        if seen[employer] <= limit:
            kept.append(row)
        else:
            held_back[employer] = held_back.get(employer, 0) + 1
    return kept, held_back


def _precision_line(conn: sqlite3.Connection, today: str) -> str:
    """Render the shortlist-precision summary, or "" when there's nothing reviewed yet."""
    overall = dedupe.review_stats(conn)
    total = overall["interested"] + overall["not_interested"]
    if total == 0:
        return "Precision: no reviews yet"

    pct = round(100 * overall["interested"] / total)
    line = (
        f"Precision: {overall['interested']}/{total} shortlisted postings "
        f"marked interested ({pct}%)"
    )

    cutoff = (date.fromisoformat(today) - timedelta(days=PRECISION_WINDOW_DAYS)).isoformat()
    recent = dedupe.review_stats(conn, since=cutoff)
    recent_total = recent["interested"] + recent["not_interested"]
    if recent_total:
        recent_pct = round(100 * recent["interested"] / recent_total)
        line += (
            f" — last {PRECISION_WINDOW_DAYS} days: "
            f"{recent['interested']}/{recent_total} ({recent_pct}%)"
        )
    return line


def write_shortlist(conn: sqlite3.Connection, output_dir: str | Path, today: str) -> Path:
    """Write output/YYYY-MM-DD-shortlist.md and return its path."""
    new, new_dupes = collapse_duplicates(dedupe.pending_new(conn, today))
    new_capped, held_back = cap_per_employer(new, PER_EMPLOYER_CAP)
    backlog, backlog_dupes = collapse_duplicates(dedupe.pending_backlog(conn, today))
    # Not collapsed: post-verdict rows carry per-row application state in the
    # tracker, so locale siblings are no longer interchangeable.
    interested = dedupe.interested_list(conn)
    borderline, borderline_dupes = collapse_duplicates(
        dedupe.pending_borderline(
            conn, today, REVIEW_QUEUE_SIZE * BORDERLINE_FETCH_MULTIPLIER
        )
    )
    borderline = borderline[:REVIEW_QUEUE_SIZE]
    collapsed = new_dupes + backlog_dupes + borderline_dupes

    lines = [f"# Shortlist — {today}", ""]
    precision_line = _precision_line(conn, today)
    if precision_line:
        lines += [precision_line, ""]

    new_title = f"New ({dedupe.SHORTLIST_MIN}+, not yet shown to you)"
    _section(lines, new_title, new_capped, today)
    for employer, n in held_back.items():
        lines.append(f"(+{n} more from {employer} held back by the per-employer cap)")
    if held_back:
        lines.append("")

    _section(lines, "Awaiting your review", backlog, today)
    _section(lines, "Interested — apply", interested, today)
    borderline_title = (
        f"Borderline ({dedupe.BORDERLINE_MIN}-{dedupe.BORDERLINE_MAX}, "
        "for rubric calibration)"
    )
    _section(lines, borderline_title, borderline, today)

    if collapsed:
        lines += [
            "",
            f"_{collapsed} duplicate listing(s) collapsed "
            "(same role posted under several locales)._",
        ]

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{today}-shortlist.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info(
        "Wrote %s (new=%d, capped_out=%d, awaiting=%d, interested=%d, "
        "borderline=%d, duplicates_collapsed=%d)",
        path, len(new_capped), len(new) - len(new_capped), len(backlog),
        len(interested), len(borderline), collapsed,
    )
    return path


# Alias generate to write_shortlist for backwards compatibility
generate = write_shortlist
