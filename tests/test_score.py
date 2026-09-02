from unittest.mock import MagicMock

import pytest

from career_radar.core import score


def test_extract_json_array_plain():
    text = '[{"req_id": "1", "score": 8, "rationale": "Great fit"}]'
    res = score.extract_json_array(text)
    assert len(res) == 1
    assert res[0]["req_id"] == "1"


def test_extract_json_array_markdown_fences():
    text = """
    Here are the scoring results:
    ```json
    [
      {"req_id": "2", "score": 9, "rationale": "Strong alignment"}
    ]
    ```
    Hope this helps!
    """
    res = score.extract_json_array(text)
    assert len(res) == 1
    assert res[0]["score"] == 9


def test_extract_json_array_single_object():
    text = '{"req_id": "3", "score": 7, "rationale": "Good match"}'
    res = score.extract_json_array(text)
    assert len(res) == 1
    assert res[0]["req_id"] == "3"


def test_validate_item():
    valid = {"req_id": "r-1", "score": 8, "rationale": "Solid fit", "flags": ["remote"]}
    req_id, s, rat, flags = score._validate_item(valid)
    assert req_id == "r-1"
    assert s == 8
    assert rat == "Solid fit"

    # Out-of-bounds score clamped or raised
    with pytest.raises(score.ScoringError):
        score._validate_item({"req_id": "r-2", "score": 15, "rationale": "Too high"})

    # Missing req_id
    with pytest.raises(score.ScoringError):
        score._validate_item({"score": 5, "rationale": "No id"})


def test_run_llm_fallback_on_unsupported_format(monkeypatch):
    """Test fallback when response_format is unsupported by LLM backend."""
    calls = []

    def mock_completion(model, messages, **kwargs):
        calls.append(kwargs)
        if "response_format" in kwargs:
            raise RuntimeError("Model does not support response_format parameter")
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content='[{"req_id": "1", "score": 8, "rationale": "Fit"}]'))]
        return mock_resp

    monkeypatch.setattr("litellm.completion", mock_completion)

    result = score._run_llm("Analyze this job")
    assert "req_id" in result
    assert len(calls) == 2
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]
