from datetime import date

from career_radar.core import prefilter
from career_radar.core.normalize import Posting


def test_prefilter_classify_negative_title():
    ok, reason = prefilter.classify("Staffing Coordinator")
    assert not ok
    assert reason == "negative title"

    ok, reason = prefilter.classify("Senior Technical Recruiter")
    assert not ok
    assert reason == "negative title"

def test_prefilter_classify_part_time():
    ok, reason = prefilter.classify("Part-Time Software Engineer")
    assert not ok
    assert reason == "part-time/seasonal"

def test_prefilter_classify_allowed_locations():
    # Matches "Remote"
    ok, reason = prefilter.classify("Software Engineer", location="Remote", remote_type="remote")
    assert ok
    assert reason == ""

    # Matches "New York, NY"
    ok, reason = prefilter.classify("Product Manager", location="New York, NY")
    assert ok
    assert reason == ""

    # Unknown location passes through to scorer
    ok, reason = prefilter.classify("Product Manager", location="")
    assert ok
    assert reason == ""

def test_prefilter_apply():
    today = date.today().isoformat()
    postings = [
        Posting(
            source="test:1", employer="TestCorp", title="Senior Engineer", req_id="1",
            url="https://example.com/1", location="Remote", department="", remote_type="remote",
            salary_min=120000, salary_max=150000, posted_date=today, description="Job desc"
        ),
        Posting(
            source="test:2", employer="TestCorp", title="Part-Time Recruiter", req_id="2",
            url="https://example.com/2", location="Remote", department="", remote_type="",
            salary_min=None, salary_max=None, posted_date=today, description="Job desc"
        ),
    ]
    kept, dropped = prefilter.apply(postings)
    assert len(kept) == 1
    assert kept[0].req_id == "1"
    assert len(dropped) == 1
    assert dropped[0][0].req_id == "2"
