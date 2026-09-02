from datetime import date
from pathlib import Path

import yaml

from career_radar.core import dedupe, pipeline


def _setup_test_config(config_dir: Path, employers: list[dict], filters: dict | None = None) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    with open(config_dir / "employers.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(employers, f)
    with open(config_dir / "filters.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(filters or {"locations": ["Remote"], "negative_titles": ["Staffing"], "drop_part_time": True}, f)
    with open(config_dir / "criteria.md", "w", encoding="utf-8") as f:
        f.write("# Criteria\nScore high for senior Python engineers.\n")


def test_run_pipeline_clean_config(tmp_path, monkeypatch):
    """Verify run_pipeline executes cleanly from scratch in an isolated environment."""
    cfg_dir = tmp_path / "config"
    db_path = tmp_path / "test.db"
    out_dir = tmp_path / "output"

    employers = [
        {"name": "MockCorp", "ats": "greenhouse", "board": "mockcorp"}
    ]
    _setup_test_config(cfg_dir, employers)

    # Mock greenhouse.fetch_jobs
    def mock_fetch_jobs(board):
        return [
            {
                "id": 101,
                "title": "Senior Python Engineer",
                "absolute_url": "https://example.com/101",
                "location": {"name": "Remote"},
                "departments": [{"name": "Engineering"}],
                "updated_at": "2026-09-01T00:00:00Z",
                "content": "<p>Build Python microservices.</p>",
            }
        ]

    monkeypatch.setattr("career_radar.fetchers.greenhouse.fetch_jobs", mock_fetch_jobs)

    pipeline.run_pipeline(
        skip_score=True,
        config_dir=cfg_dir,
        db_path=db_path,
        output_dir=out_dir,
    )

    # Check database persistence
    conn = dedupe.connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM postings").fetchall()
        assert len(rows) == 1
        assert rows[0]["employer"] == "MockCorp"
        assert rows[0]["title"] == "Senior Python Engineer"
    finally:
        conn.close()

    # Check report generation
    reports = list(out_dir.glob("*-shortlist.md"))
    assert len(reports) == 1
    assert reports[0].exists()


def test_run_pipeline_scanner_failure_isolation(tmp_path, monkeypatch):
    """Verify that a failure in one scanner does not crash the pipeline for other employers."""
    cfg_dir = tmp_path / "config"
    db_path = tmp_path / "test.db"
    out_dir = tmp_path / "output"

    employers = [
        {"name": "FailingCorp", "ats": "greenhouse", "board": "failboard"},
        {"name": "WorkingCorp", "ats": "lever", "company": "workcompany"},
    ]
    _setup_test_config(cfg_dir, employers)

    def mock_fail(board):
        raise RuntimeError("Network timeout connecting to Greenhouse")

    def mock_lever(company):
        return [
            {
                "id": "lev-1",
                "text": "Platform Engineer",
                "hostedUrl": "https://example.com/lev-1",
                "categories": {"location": "Remote", "team": "Platform", "commitment": "Full-time"},
                "createdAt": 1725200000000,
                "descriptionPlain": "Build cloud platforms.",
            }
        ]

    monkeypatch.setattr("career_radar.fetchers.greenhouse.fetch_jobs", mock_fail)
    monkeypatch.setattr("career_radar.fetchers.lever.fetch_postings", mock_lever)

    # Should not raise exception
    pipeline.run_pipeline(
        skip_score=True,
        config_dir=cfg_dir,
        db_path=db_path,
        output_dir=out_dir,
    )

    conn = dedupe.connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM postings").fetchall()
        assert len(rows) == 1
        assert rows[0]["employer"] == "WorkingCorp"
    finally:
        conn.close()


def test_custom_config_propagation(tmp_path, monkeypatch):
    """Verify custom filters and custom model from --config-dir are properly honored."""
    cfg_dir = tmp_path / "config"
    db_path = tmp_path / "test.db"
    out_dir = tmp_path / "output"

    custom_filters = {
        "locations": ["Tokyo, Japan"],
        "negative_titles": ["Manager"],
        "drop_part_time": True,
        "llm_model": "test-custom-model-v1",
    }
    employers = [
        {"name": "GlobalCorp", "ats": "greenhouse", "board": "globalcorp"}
    ]
    _setup_test_config(cfg_dir, employers, custom_filters)

    def mock_fetch_jobs(board):
        return [
            {
                "id": 201,
                "title": "Engineering Manager",  # Negative title in custom config
                "absolute_url": "https://example.com/201",
                "location": {"name": "Tokyo, Japan"},
                "departments": [],
                "updated_at": "2026-09-01T00:00:00Z",
                "content": "<p>Lead teams.</p>",
            },
            {
                "id": 202,
                "title": "Software Engineer",
                "absolute_url": "https://example.com/202",
                "location": {"name": "Tokyo, Japan"},  # Matches custom location
                "departments": [],
                "updated_at": "2026-09-01T00:00:00Z",
                "content": "<p>Write code.</p>",
            },
        ]

    monkeypatch.setattr("career_radar.fetchers.greenhouse.fetch_jobs", mock_fetch_jobs)

    model_received = []
    def mock_score_unscored(conn, criteria_path, config_dir=None):
        from career_radar.config import load_model
        model_received.append(load_model(config_dir=config_dir))
        return 1, 0

    monkeypatch.setattr("career_radar.core.score.score_unscored", mock_score_unscored)

    pipeline.run_pipeline(
        skip_score=False,
        config_dir=cfg_dir,
        db_path=db_path,
        output_dir=out_dir,
    )

    # Verify custom negative title dropped 201 and kept 202
    conn = dedupe.connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM postings").fetchall()
        assert len(rows) == 1
        assert rows[0]["req_id"] == "202"
    finally:
        conn.close()

    # Verify custom model was propagated
    assert model_received == ["test-custom-model-v1"]


def test_adzuna_and_usajobs_source_namespacing(tmp_path, monkeypatch):
    """Verify distinct search queries with overlapping IDs do not collide in SQLite."""
    today = date.today()
    conn = dedupe.connect(tmp_path / "source_test.db")

    try:
        monkeypatch.setenv("USAJOBS_API_KEY", "mock-key")
        monkeypatch.setenv("ADZUNA_APP_ID", "mock-id")
        monkeypatch.setenv("ADZUNA_APP_KEY", "mock-key")

        # Each query returns a posting with the exact same ID "overlap-999"
        monkeypatch.setattr(
            "career_radar.fetchers.usajobs.fetch_listings",
            lambda k, loc: [{
                "MatchedObjectId": "overlap-999",
                "MatchedObjectDescriptor": {
                    "PositionTitle": "Software Engineer",
                    "PositionLocationDisplay": loc,
                    "PositionURI": "https://example.com/usajobs",
                },
            }],
        )
        monkeypatch.setattr(
            "career_radar.fetchers.adzuna.fetch_listings",
            lambda id, k, where, what: [{
                "id": "overlap-999",
                "title": f"Engineer {what}",
                "redirect_url": "https://example.com/adzuna",
                "location": {"display_name": where},
            }],
        )

        emp_usa1 = {"name": "Tech Jobs", "ats": "usajobs", "location": "Remote"}
        emp_usa2 = {"name": "NY Jobs", "ats": "usajobs", "location": "New York, NY"}

        emp_adz1 = {"name": "Python Search", "ats": "adzuna", "where": "Remote", "what": "Python"}
        emp_adz2 = {"name": "Rust Search", "ats": "adzuna", "where": "Remote", "what": "Rust"}

        n1 = pipeline.scan_usajobs_employer(conn, emp_usa1, today)
        n2 = pipeline.scan_usajobs_employer(conn, emp_usa2, today)
        n3 = pipeline.scan_adzuna_employer(conn, emp_adz1, today)
        n4 = pipeline.scan_adzuna_employer(conn, emp_adz2, today)

        # All 4 scanners should keep and insert their job despite shared req_id
        assert n1 == 1
        assert n2 == 1
        assert n3 == 1
        assert n4 == 1

        rows = conn.execute("SELECT source, req_id FROM postings").fetchall()
        assert len(rows) == 4

        sources = {r["source"] for r in rows}
        assert len(sources) == 4
        assert "usajobs:tech_jobs:remote" in sources
        assert "usajobs:ny_jobs:new_york_ny" in sources
        assert "adzuna:python_search:remote:python" in sources
        assert "adzuna:rust_search:remote:rust" in sources
    finally:
        conn.close()
