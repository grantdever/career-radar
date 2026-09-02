from datetime import date, timedelta

from career_radar.core import normalize


def test_html_to_text():
    raw_html = "<p>Hello <b>World</b></p><br/><div>Line 2</div>"
    text = normalize.html_to_text(raw_html)
    assert "Hello World" in text
    assert "Line 2" in text

def test_extract_element_by_id():
    html = """
    <div class="container">
        <div id="target-id">
            <p>Nested <span>content</span></p>
        </div>
    </div>
    """
    extracted = normalize.extract_element_by_id(html, "target-id")
    assert "<p>Nested <span>content</span></p>" in extracted

def test_extract_element_by_id_missing():
    html = "<div>No id here</div>"
    assert normalize.extract_element_by_id(html, "target-id") == ""

def test_remote_from_location():
    assert normalize._remote_from_location("Remote, US") == "Remote"
    assert normalize._remote_from_location("Remote - San Francisco, CA") == "Remote"
    assert normalize._remote_from_location("Austin, TX") == ""

def test_parse_posted_on():
    today = date(2026, 9, 2)
    assert normalize.parse_posted_on("Posted Today", today) == "2026-09-02"
    assert normalize.parse_posted_on("Posted Yesterday", today) == (today - timedelta(days=1)).isoformat()
    assert normalize.parse_posted_on("Posted 3 Days Ago", today) == (today - timedelta(days=3)).isoformat()
