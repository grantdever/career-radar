"""SQLite persistence: one durable postings table keyed on (source, req_id).

Rows are never deleted (they feed the longitudinal labor-market dataset);
first_seen/last_seen make posting duration measurable. A posting is "new"
when its key has never been recorded.

Postings also carry a review lifecycle so a job is only "surfaced" when it is
actually shown to the user, and only "reviewed" when they record a verdict:

    new -> surfaced -> interested | not_interested

A `not_interested` verdict is durable: it is never surfaced again. The reason
given is stored in reaction_note and (after user approval) appended to
criteria.md so the rubric learns preferences.
"""

import difflib
import logging
import os
import sqlite3
from collections import defaultdict
from pathlib import Path

from career_radar.core import prefilter
from career_radar.core.normalize import Posting

logger = logging.getLogger(__name__)

DB_PATH = Path(
    os.environ.get("CAREER_RADAR_DB_PATH", Path.home() / ".local" / "share" / "career-radar" / "scanner.db")
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
    source      TEXT NOT NULL,
    req_id      TEXT NOT NULL,
    employer    TEXT NOT NULL,
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    location    TEXT,
    department  TEXT,
    remote_type TEXT,
    salary_min  INTEGER,
    salary_max  INTEGER,
    posted_date TEXT,
    description TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    score       INTEGER,
    rationale   TEXT,
    flags       TEXT,
    scored_at   TEXT,
    status      TEXT NOT NULL DEFAULT 'new',
    surfaced_at TEXT,
    reviewed_at TEXT,
    reaction_note TEXT,
    app_status  TEXT NOT NULL DEFAULT 'to_apply',
    app_note    TEXT,
    app_updated_at TEXT,
    applied_at  TEXT,
    PRIMARY KEY (source, req_id)
)
"""

# Columns added after the original phase-1 schema shipped. connect() migrates
# existing databases in place so the longitudinal data is never rebuilt.
_LIFECYCLE_COLUMNS = {
    "department": "TEXT",
    "status": "TEXT NOT NULL DEFAULT 'new'",
    "surfaced_at": "TEXT",
    "reviewed_at": "TEXT",
    "reaction_note": "TEXT",
    "app_status": "TEXT NOT NULL DEFAULT 'to_apply'",
    "app_note": "TEXT",
    "app_updated_at": "TEXT",
    "applied_at": "TEXT",
}

NEW = "new"
SURFACED = "surfaced"
INTERESTED = "interested"
NOT_INTERESTED = "not_interested"
# Retired by a later tightening of src/prefilter.py: the posting was stored
# under an older, looser rule set and no longer qualifies. Distinct from
# NOT_INTERESTED so it never pollutes the precision stat — Grant did not
# reject these, the filter did. See retire_filtered().
FILTERED = "filtered"

# Application-tracking stages for `interested` postings (src/tracker.py).
# `status` still gates what the tracker shows; `app_status` records what
# happened after the interested verdict. The column defaults to TO_APPLY
# (ALTER TABLE backfills old rows), so it is never NULL. app_updated_at is
# the audit timestamp of the last tracker edit; applied_at is stamped once,
# on the first move to APPLIED.
TO_APPLY = "to_apply"
APPLIED = "applied"
INTERVIEWING = "interviewing"
OFFER = "offer"
REJECTED = "rejected"
WITHDRAWN = "withdrawn"
STALE = "stale"
APP_STATUSES = (TO_APPLY, APPLIED, INTERVIEWING, OFFER, REJECTED, WITHDRAWN, STALE)
# Terminal stages: dropped from the default apply list (shortlist, inbox
# count) and collapsed in the tracker. An offer counts as closed here — it
# no longer needs the apply-list nudge.
APP_CLOSED = (OFFER, REJECTED, WITHDRAWN, STALE)

# Scoring thresholds that drive the shortlist sections (report.py renders
# these, but the numbers live here since the SQL that applies them does too).
SHORTLIST_MIN = 7
BORDERLINE_MIN = 5
BORDERLINE_MAX = 6


def _migrate(conn: sqlite3.Connection) -> None:
    """Add any missing lifecycle columns to an existing postings table."""
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(postings)")}
    for name, ddl in _LIFECYCLE_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE postings ADD COLUMN {name} {ddl}")
    conn.commit()


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the scanner database, migrated to current schema."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    conn.commit()
    _migrate(conn)
    return conn


def known_req_ids(conn: sqlite3.Connection, source: str) -> set[str]:
    """Return every req_id already recorded for a source."""
    rows = conn.execute("SELECT req_id FROM postings WHERE source = ?", (source,))
    return {r["req_id"] for r in rows}



def _is_fuzzy_duplicate(p1_title: str, p1_desc: str, p2_title: str, p2_desc: str) -> bool:
    title_ratio = difflib.SequenceMatcher(None, p1_title.lower(), p2_title.lower()).ratio()

    # Fast length heuristic: if lengths differ by >25%, desc_ratio cannot exceed 75%
    len_ratio = min(len(p1_desc), len(p2_desc)) / max(1, max(len(p1_desc), len(p2_desc)))

    desc_ratio = 0.0
    if len_ratio >= 0.75:
        # Only compute expensive difflib if lengths are somewhat close
        desc_ratio = difflib.SequenceMatcher(None, p1_desc, p2_desc).ratio()

        # Log edge cases (high description similarity, but title might be borderline)
        if desc_ratio >= 0.90:
            status = "DROPPED" if (title_ratio >= 0.60) else "KEPT (Title mismatch)"
            logger.info("Fuzzy Dedup [%s]: '%s' vs '%s' (Title: %.2f, Desc: %.2f)",
                        status, p1_title, p2_title, title_ratio, desc_ratio)

    if title_ratio >= 0.85 and desc_ratio >= 0.75:
        return True
    if title_ratio >= 0.60 and desc_ratio >= 0.90:
        return True
    return False

def insert_new(conn: sqlite3.Connection, postings: list[Posting], today: str) -> int:
    """Insert first-time postings with first_seen = last_seen = today."""
    if not postings:
        return 0

    # 1. Fuzzy Deduplication: Drop postings that match an existing job
    employers = {p.employer for p in postings}
    placeholders = ",".join("?" * len(employers))
    cur = conn.execute(
        f"SELECT employer, title, description FROM postings WHERE employer IN ({placeholders})",
        list(employers)
    )
    existing = defaultdict(list)
    for row in cur:
        existing[row["employer"]].append((row["title"], row["description"] or ""))

    kept_postings = []
    for p in postings:
        is_dup = False
        employer_jobs = existing[p.employer]
        for ex_title, ex_desc in employer_jobs:
            if _is_fuzzy_duplicate(p.title, p.description or "", ex_title, ex_desc):
                is_dup = True
                break
        if not is_dup:
            kept_postings.append(p)
            existing[p.employer].append((p.title, p.description or ""))

    if not kept_postings:
        return 0

    # 2. Insert the surviving postings
    conn.executemany(
        """INSERT OR IGNORE INTO postings
           (source, req_id, employer, title, url, location, department, remote_type,
            salary_min, salary_max, posted_date, description, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (p.source, p.req_id, p.employer, p.title, p.url, p.location, p.department,
             p.remote_type, p.salary_min, p.salary_max, p.posted_date,
             p.description, today, today)
            for p in kept_postings
        ],
    )
    conn.commit()
    return len(kept_postings)


def touch_seen(conn: sqlite3.Connection, source: str, req_ids: set[str], today: str) -> None:
    """Update last_seen for postings observed again today (batch, not per-row)."""
    if not req_ids:
        return
    placeholders = ",".join("?" * len(req_ids))
    conn.execute(
        f"UPDATE postings SET last_seen = ? WHERE source = ? AND req_id IN ({placeholders})",
        (today, source, *req_ids),
    )
    conn.commit()


def unscored(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return postings that have never been scored."""
    return list(conn.execute(
        "SELECT * FROM postings WHERE scored_at IS NULL ORDER BY posted_date DESC"
    ))


def record_score(
    conn: sqlite3.Connection,
    source: str,
    req_id: str,
    score: int,
    rationale: str,
    flags: str,
    scored_at: str,
) -> None:
    """Persist one scoring result."""
    conn.execute(
        """UPDATE postings SET score = ?, rationale = ?, flags = ?, scored_at = ?
           WHERE source = ? AND req_id = ?""",
        (score, rationale, flags, scored_at, source, req_id),
    )


# --- Review lifecycle -------------------------------------------------------

def mark_surfaced(conn: sqlite3.Connection, source: str, req_id: str, today: str) -> None:
    """Mark a still-new posting as shown to Grant (never demotes a verdict)."""
    conn.execute(
        """UPDATE postings
           SET surfaced_at = COALESCE(surfaced_at, ?), status = ?
           WHERE source = ? AND req_id = ? AND status = ?""",
        (today, SURFACED, source, req_id, NEW),
    )


def record_verdict(
    conn: sqlite3.Connection,
    source: str,
    req_id: str,
    status: str,
    note: str,
    today: str,
) -> None:
    """Record an explicit verdict (interested / not_interested) with a note."""
    conn.execute(
        """UPDATE postings
           SET status = ?, reviewed_at = ?, reaction_note = ?,
               surfaced_at = COALESCE(surfaced_at, ?)
           WHERE source = ? AND req_id = ?""",
        (status, today, note, today, source, req_id),
    )



def withdraw_interest(
    conn: sqlite3.Connection, source: str, req_id: str, note: str, today: str
) -> int:
    """Take a posting off the apply list: interested -> not_interested.

    Only rows currently `interested` change (returns the row count, 0 = not on
    the apply list), so a stale tracker page cannot flip an unrelated row. The
    verdict is durable — the posting never resurfaces — and counts in the
    precision stat like any other rejection. Does not commit.
    """
    cur = conn.execute(
        """UPDATE postings
           SET status = ?, reviewed_at = ?, reaction_note = ?
           WHERE source = ? AND req_id = ? AND status = ?""",
        (NOT_INTERESTED, today, note, source, req_id, INTERESTED),
    )
    return cur.rowcount


def record_app_status(
    conn: sqlite3.Connection,
    source: str,
    req_id: str,
    app_status: str,
    note: str,
    today: str,
) -> int:
    """Update application tracking for one posting on the apply list.

    applied_at is stamped the first time the status reaches APPLIED and is
    never overwritten, so it stays the date of the actual application. Only
    rows with an `interested` verdict can be updated; returns the number of
    rows changed (0 = unknown posting, or not on the apply list). Like the
    other lifecycle writers, does not commit — the caller owns the
    transaction boundary. The note is always written: a caller moving only
    the stage must resend the current note or it is cleared.
    """
    if app_status not in APP_STATUSES:
        raise ValueError(f"unknown app_status: {app_status!r}")
    cur = conn.execute(
        """UPDATE postings
           SET app_status = ?, app_note = ?, app_updated_at = ?,
               applied_at = COALESCE(applied_at, ?)
           WHERE source = ? AND req_id = ? AND status = ?""",
        (app_status, note, today,
         today if app_status == APPLIED else None,
         source, req_id, INTERESTED),
    )
    return cur.rowcount


def retire_filtered(conn: sqlite3.Connection) -> dict[str, int]:
    """Re-apply the current pre-filter to unreviewed rows and retire the failures.

    The pre-filter runs at fetch time, so rows stored under an older rule set
    survive in the DB and keep showing up in the shortlist. Running this at the
    top of every scan means a rule change (e.g. the 2026-08-18 sales and
    non-US cuts) cleans up its own backlog instead of needing a manual purge.

    Only `new` and `surfaced` rows are touched — an explicit verdict from the user
    always wins over a filter rule. Returns reason -> count.
    """
    rows = list(conn.execute(
        "SELECT source, req_id, title, location, remote_type FROM postings "
        "WHERE status IN (?, ?)",
        (NEW, SURFACED),
    ))
    retired: dict[str, int] = {}
    updates = []
    for row in rows:
        ok, reason = prefilter.classify(
            row["title"], row["location"] or "", row["remote_type"] or ""
        )
        if ok:
            continue
        retired[reason] = retired.get(reason, 0) + 1
        updates.append((FILTERED, f"auto-retired: {reason}", row["source"], row["req_id"]))
    if updates:
        conn.executemany(
            "UPDATE postings SET status = ?, reaction_note = ? "
            "WHERE source = ? AND req_id = ?",
            updates,
        )
        conn.commit()
    return retired

def pending_new(conn: sqlite3.Connection, today: str) -> list[sqlite3.Row]:
    """Score >= SHORTLIST_MIN, never shown, still on the board today. Priority review."""
    return list(conn.execute(
        """SELECT * FROM postings
           WHERE score >= ? AND status = ? AND last_seen = ?
           ORDER BY score DESC, salary_max DESC NULLS LAST, posted_date DESC""",
        (SHORTLIST_MIN, NEW, today),
    ))


def pending_backlog(conn: sqlite3.Connection, today: str) -> list[sqlite3.Row]:
    """Shown but no verdict yet, still on the board today. Oldest first."""
    return list(conn.execute(
        """SELECT * FROM postings
           WHERE status = ? AND last_seen = ?
           ORDER BY surfaced_at ASC, score DESC""",
        (SURFACED, today),
    ))


def interested_list(
    conn: sqlite3.Connection, include_closed: bool = False
) -> list[sqlite3.Row]:
    """Postings marked worth applying to — live candidates by default.

    Applications that reached a terminal stage (APP_CLOSED) are excluded
    unless include_closed=True; the tracker passes True to show closed
    applications in its own collapsed section.
    """
    rows = list(conn.execute(
        """SELECT * FROM postings WHERE status = ?
           ORDER BY score DESC, salary_max DESC NULLS LAST, posted_date DESC""",
        (INTERESTED,),
    ))
    if include_closed:
        return rows
    return [r for r in rows if r["app_status"] not in APP_CLOSED]


def pending_borderline(conn: sqlite3.Connection, today: str, limit: int = 6) -> list[sqlite3.Row]:
    """Score BORDERLINE_MIN-BORDERLINE_MAX, unreviewed, still open — calibration material."""
    return list(conn.execute(
        """SELECT * FROM postings
           WHERE score BETWEEN ? AND ? AND status IN (?, ?) AND last_seen = ?
           ORDER BY score DESC, salary_max DESC NULLS LAST LIMIT ?""",
        (BORDERLINE_MIN, BORDERLINE_MAX, NEW, SURFACED, today, limit),
    ))


def review_stats(conn: sqlite3.Connection, since: str | None = None) -> dict:
    """Return {"interested": n, "not_interested": n} verdict counts.

    Counts only rows with an explicit verdict (status IN interested,
    not_interested); surfaced-but-unreviewed rows are excluded. When `since`
    is given, only verdicts recorded on or after that ISO date are counted
    (compares against `reviewed_at`).
    """
    query = "SELECT status, COUNT(*) AS n FROM postings WHERE status IN (?, ?)"
    params: list[str] = [INTERESTED, NOT_INTERESTED]
    if since is not None:
        query += " AND reviewed_at >= ?"
        params.append(since)
    query += " GROUP BY status"
    counts = {INTERESTED: 0, NOT_INTERESTED: 0}
    for row in conn.execute(query, params):
        counts[row["status"]] = row["n"]
    return {"interested": counts[INTERESTED], "not_interested": counts[NOT_INTERESTED]}
