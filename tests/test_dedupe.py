from datetime import date

from career_radar.core import dedupe
from career_radar.core.normalize import Posting


def _make_posting(req_id: str, title: str = "Engineer", desc: str = "Build things", employer: str = "Acme") -> Posting:
    return Posting(
        source="greenhouse:acme",
        employer=employer,
        title=title,
        req_id=req_id,
        url=f"https://example.com/{req_id}",
        location="Remote",
        department="Engineering",
        remote_type="Remote",
        salary_min=100000,
        salary_max=150000,
        posted_date="2026-09-01",
        description=desc,
    )

def test_database_lifecycle(tmp_path):
    db_file = tmp_path / "test.db"
    conn = dedupe.connect(db_file)
    today = date.today().isoformat()

    p1 = _make_posting("1", "Backend Engineer", "Python and SQLite")
    p2 = _make_posting("2", "Frontend Engineer", "React and TypeScript")

    inserted = dedupe.insert_new(conn, [p1, p2], today)
    assert inserted == 2

    known = dedupe.known_req_ids(conn, "greenhouse:acme")
    assert known == {"1", "2"}

    # Duplicate insertion of exact same postings
    inserted_again = dedupe.insert_new(conn, [p1], today)
    assert inserted_again == 0

    # Test touch_seen
    dedupe.touch_seen(conn, "greenhouse:acme", {"1"}, "2026-09-02")
    row = conn.execute("SELECT last_seen FROM postings WHERE req_id = '1'").fetchone()
    assert row["last_seen"] == "2026-09-02"

def test_fuzzy_duplicate_detection(tmp_path):
    db_file = tmp_path / "fuzzy.db"
    conn = dedupe.connect(db_file)
    today = date.today().isoformat()

    long_desc = "We are seeking a senior software engineer to lead the backend services team using Python."
    p1 = _make_posting("req-101", "Senior Software Engineer", long_desc, employer="Stripe")
    dedupe.insert_new(conn, [p1], today)

    # Near-identical posting (same employer, slightly different title / same description)
    p2 = _make_posting("req-102", "Senior Software Engineer - US", long_desc, employer="Stripe")
    inserted = dedupe.insert_new(conn, [p2], today)
    assert inserted == 0  # Should be dropped by fuzzy deduplication

def test_verdict_lifecycle(tmp_path):
    db_file = tmp_path / "verdicts.db"
    conn = dedupe.connect(db_file)
    today = date.today().isoformat()

    p = _make_posting("v-1", "Staff Engineer", "Desc")
    dedupe.insert_new(conn, [p], today)

    # Mark surfaced
    dedupe.mark_surfaced(conn, p.source, p.req_id, today)
    row = conn.execute("SELECT status FROM postings WHERE req_id = 'v-1'").fetchone()
    assert row["status"] == dedupe.SURFACED

    # Record interested
    dedupe.record_verdict(conn, p.source, p.req_id, dedupe.INTERESTED, "Love the tech", today)
    interested = dedupe.interested_list(conn)
    assert len(interested) == 1
    assert interested[0]["req_id"] == "v-1"

    # Stats
    stats = dedupe.review_stats(conn)
    assert stats["interested"] == 1
    assert stats["not_interested"] == 0
