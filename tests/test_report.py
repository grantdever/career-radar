from datetime import date

from career_radar.core import dedupe, report
from career_radar.core.normalize import Posting


def test_dedupe_title():
    assert report.dedupe_title("Software Engineer | Canada | Remote") == "software engineer"
    assert report.dedupe_title("Solutions Architect - France") == "solutions architect"
    assert report.dedupe_title("Product Manager — US") == "product manager"
    assert report.dedupe_title("Lead Developer (Remote)") == "lead developer remote"

def test_cap_per_employer(tmp_path):
    db_file = tmp_path / "report.db"
    conn = dedupe.connect(db_file)
    today = date.today().isoformat()

    titles = [
        "Backend Systems Lead",
        "Frontend React Specialist",
        "Data Platform Architect",
        "DevOps Kubernetes Engineer",
        "Mobile iOS Developer",
    ]
    postings = [
        Posting(
            source="test:emp", employer="BigCorp", title=titles[i], req_id=f"r-{i}",
            url=f"https://example.com/{i}", location="Remote", department="", remote_type="Remote",
            salary_min=100000 + i * 10000, salary_max=120000 + i * 10000,
            posted_date=today, description=f"Distinct role duties for {titles[i]}"
        )
        for i in range(5)
    ]
    dedupe.insert_new(conn, postings, today)
    for p in postings:
        dedupe.record_score(conn, p.source, p.req_id, 8, "Good fit", "[]", today)

    rows = dedupe.pending_new(conn, today)
    kept, held_back = report.cap_per_employer(rows, limit=2)
    assert len(kept) == 2
    assert held_back["BigCorp"] == 3

def test_write_shortlist(tmp_path):
    db_file = tmp_path / "shortlist_test.db"
    conn = dedupe.connect(db_file)
    today = date.today().isoformat()

    p = Posting(
        source="test:acme", employer="Acme", title="Staff Engineer", req_id="p-1",
        url="https://example.com/p-1", location="Remote", department="", remote_type="Remote",
        salary_min=150000, salary_max=180000, posted_date=today, description="Build great software"
    )
    dedupe.insert_new(conn, [p], today)
    dedupe.record_score(conn, p.source, p.req_id, 9, "Exceptional match", "[]", today)

    out_dir = tmp_path / "output"
    report_path = report.write_shortlist(conn, out_dir, today)
    assert report_path.exists()

    content = report_path.read_text(encoding="utf-8")
    assert "Staff Engineer" in content
    assert "Acme" in content
    assert "9/10" in content
