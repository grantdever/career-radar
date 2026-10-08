import json
from pathlib import Path

import pytest
import yaml

from career_radar.core import decision
from career_radar.core.spend import Ledger
from career_radar.core.transports import (
    LLMStructured,
    OpenRouterDecisions,
    Replay,
    TransportError,
    _schema_for,
)

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "decision.backend-engineer.yaml"
WIRE = {"domain": {"type": "score", "score": 2.0, "confidence": 0.8},
        "work": {"type": "score", "score": 3.0, "confidence": 0.7},
        "seniority": {"type": "choice", "choice": "in_range", "confidence": 0.9},
        "org_fit": {"type": "choice", "choice": "good", "confidence": 0.6},
        "hard_veto": {"type": "noul", "noul": 0.04, "confidence": 0.9}}


class _Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._d


def test_openrouter_decisions_wire_format_and_ledger(monkeypatch, tmp_path):
    cfg = decision.load_config(EXAMPLE)
    seen = {}

    def fake_post(url, headers, json, timeout):
        seen.update(url=url, body=json, auth=headers["Authorization"])
        return _Resp({"id": "gen-1", "model": json["model"], "answers": WIRE,
                      "usage": {"input_tokens": 1200, "output_tokens": 0, "cost": 0.00005}})

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr("requests.post", fake_post)
    ledger = Ledger.at(tmp_path / "spend.db")
    tr = OpenRouterDecisions(model=cfg.transport.model, ledger=ledger)
    state = {"posting": {"title": "Backend Engineer"}}
    res = tr.decide(state, cfg)
    assert seen["url"].endswith("/api/alpha/decisions")
    assert seen["body"]["model"] == "typesafe/jev-1.13-20260917"
    assert seen["body"]["state"] == state
    assert seen["body"]["questions"]["seniority"]["criteria"]["out"].startswith("Director")
    assert seen["body"]["questions"]["domain"]["type"] == "score"
    assert res.answers["work"].value == 3 and res.cost_usd == 0.00005
    rows = ledger.summary()
    assert rows[0]["calls"] == 1 and rows[0]["cost_usd"] == pytest.approx(0.00005)


def test_openrouter_missing_key_and_bad_answers(monkeypatch):
    cfg = decision.load_config(EXAMPLE)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(TransportError, match="OPENROUTER_API_KEY"):
        OpenRouterDecisions(model="m").decide({}, cfg)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    bad = dict(WIRE, seniority={"choice": "senior"})
    monkeypatch.setattr("requests.post", lambda *a, **k: _Resp({"answers": bad}))
    with pytest.raises(decision.AnswerError):
        OpenRouterDecisions(model="m").decide({}, cfg)


def test_llm_structured_marks_uncalibrated(monkeypatch):
    cfg = decision.load_config(EXAMPLE)
    reply = {n: {"value": (v.get("score", v.get("choice", v.get("noul")))), "confidence": 0.8}
             for n, v in WIRE.items()}

    class Msg:
        content = json.dumps(reply)

    class Choice:
        message = Msg()

    class R:
        choices = [Choice()]
        usage = None

    captured = {}

    def fake_completion(**kw):
        captured.update(kw)
        return R()

    monkeypatch.setattr("litellm.completion", fake_completion)
    res = LLMStructured(model="gemini/flash").decide({"posting": {"title": "x"}}, cfg)
    assert all(not a.calibrated for a in res.answers.values())
    schema = captured["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["seniority"]["properties"]["value"]["enum"] == ["in_range", "stretch", "out"]
    assert schema == _schema_for(cfg)


def test_replay_hits_and_misses(tmp_path):
    cfg = decision.load_config(EXAMPLE)
    state = {"posting": {"title": "Backend Engineer"}}
    rec = {n: {"type": v["type"], "value": v.get("score", v.get("choice", v.get("noul"))),
               "confidence": v["confidence"]} for n, v in WIRE.items()}
    p = tmp_path / "replay.jsonl"
    p.write_text(json.dumps({"state_key": decision.state_key(state), "answers": rec}) + "\n")
    tr = Replay(path=str(p))
    assert tr.decide(state, cfg).answers["domain"].value == 2
    with pytest.raises(TransportError, match="no recorded answers"):
        tr.decide({"posting": {"title": "Other"}}, cfg)
    with pytest.raises(TransportError, match="needs a path"):
        Replay()


def test_example_yaml_is_valid_yaml():
    assert yaml.safe_load(EXAMPLE.read_text())["surface_cutoff"] == 8
