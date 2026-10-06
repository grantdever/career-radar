import copy
import json
from pathlib import Path

import pytest
import yaml

from career_radar.core import decision, dedupe, pipeline
from career_radar.core.decision import DecisionConfigError, coerce_answers, fold, parse_config
from career_radar.core.transports import Mock, make_transport

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "decision.backend-engineer.yaml"


def _raw():
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def _answers(cfg, **over):
    base = {"domain": {"score": 3, "confidence": 0.9}, "work": {"score": 3, "confidence": 0.9},
            "seniority": {"choice": "in_range", "confidence": 0.9},
            "org_fit": {"choice": "neutral", "confidence": 0.9},
            "hard_veto": {"noul": 0.1, "confidence": 0.9}}
    base.update(over)
    return coerce_answers(cfg, base)


def test_example_config_loads():
    cfg = decision.load_config(EXAMPLE)
    assert set(cfg.questions) == {"domain", "work", "seniority", "org_fit", "hard_veto"}
    assert len(cfg.qhash) == 12
    assert cfg.surface_cutoff == 8


@pytest.mark.parametrize("mutate,msg", [
    (lambda d: d["fold"]["base"].update(sum=["domian", "work"]), "unknown question 'domian'"),
    (lambda d: d["fold"]["base"].update(sum=["seniority"]), "not score"),
    (lambda d: d["fold"]["base"].update(table=[1, 2, 3]), "exactly 7 entries"),
    (lambda d: d["fold"]["clamp"].append({"if": {"seniority": "outt"}, "max": 3}), "not an option"),
    (lambda d: d["fold"]["clamp"].append({"if": {"hard_veto": "big"}, "max": 3}), ">=0.5"),
    (lambda d: d["fold"]["clamp"].append({"if": {"hard_veto": ">=1.5"}, "max": 3}), "within 0..1"),
    (lambda d: d["fold"]["clamp"].append({"if": {"nope": "x"}, "max": 3}), "unknown question"),
    (lambda d: d["fold"]["clamp"].append({"if": {"seniority": "out"}}), "max or min"),
    (lambda d: d["questions"]["domain"].update(levels=5), "4 criteria entries for 5 levels"),
    (lambda d: d["questions"]["work"].update(type="scale"), "has type 'scale'"),
    (lambda d: d["transport"].update(kind="carrier-pigeon"), "transport.kind"),
    (lambda d: d["transport"].pop("model"), "needs a model"),
    (lambda d: d["state"].update(fields=["title", "ssn"]), "unknown fields"),
    (lambda d: d.update(surface_cutoff=11), "1..10"),
])
def test_bad_configs_fail_at_load(mutate, msg):
    d = copy.deepcopy(_raw())
    mutate(d)
    with pytest.raises(DecisionConfigError, match=msg):
        parse_config(d)


def test_fold_base_adjust_and_clamps():
    cfg = parse_config(_raw())
    assert fold(cfg, _answers(cfg)).score == 9
    assert fold(cfg, _answers(cfg, org_fit={"choice": "good"})).score == 10
    r = fold(cfg, _answers(cfg, seniority={"choice": "stretch"}))
    assert r.score == 8 and "stretch" in r.flags
    assert fold(cfg, _answers(cfg, seniority={"choice": "out"})).score == 3
    r = fold(cfg, _answers(cfg, hard_veto={"noul": 0.5}))
    assert r.score == 2 and "hard-veto" in r.flags
    assert fold(cfg, _answers(cfg, hard_veto={"noul": 0.49})).score == 9
    low = {k: {**v, "confidence": 0.3} for k, v in {
        "domain": {"score": 1}, "work": {"score": 1}, "seniority": {"choice": "in_range"},
        "org_fit": {"choice": "neutral"}, "hard_veto": {"noul": 0.0}}.items()}
    r = fold(cfg, coerce_answers(cfg, low))
    assert r.score == 3 and "low-confidence" in r.flags


def test_bad_answers_rejected():
    cfg = parse_config(_raw())
    with pytest.raises(decision.AnswerError):
        _answers(cfg, domain={"score": 7})
    with pytest.raises(decision.AnswerError):
        _answers(cfg, seniority={"choice": "senior"})
    with pytest.raises(decision.AnswerError):
        _answers(cfg, hard_veto={"noul": float("nan")})
    with pytest.raises(decision.AnswerError, match="missing"):
        coerce_answers(cfg, {"domain": {"score": 1}})


def test_uncalibrated_answers_flagged():
    cfg = parse_config(_raw())
    raw = {"domain": {"value": 3, "confidence": 0.9}, "work": {"value": 3, "confidence": 0.9},
           "seniority": {"value": "in_range", "confidence": 0.9},
           "org_fit": {"value": "neutral", "confidence": 0.9},
           "hard_veto": {"value": 0.0, "confidence": 0.9}}
    r = fold(cfg, coerce_answers(cfg, raw, calibrated=False))
    assert "uncalibrated" in r.flags


def test_qhash_changes_with_questions_fold_state_model():
    base = parse_config(_raw()).qhash
    for mutate in (lambda d: d["fold"]["base"].update(table=[1, 2, 3, 4, 6, 7, 10]),
                   lambda d: d["questions"]["hard_veto"].update(instructions="x?"),
                   lambda d: d["state"].update(fields=["title"]),
                   lambda d: d["transport"].update(model="other/model")):
        d = copy.deepcopy(_raw())
        mutate(d)
        assert parse_config(d).qhash != base
    d = copy.deepcopy(_raw())
    d["transport"] = {"kind": "mock", "model": d["transport"]["model"]}
    assert parse_config(d).qhash == base  # transport kind is not part of the version


def test_state_respects_fields_and_criteria():
    d = copy.deepcopy(_raw())
    d["state"] = {"fields": ["title", "salary"], "include_criteria": False}
    cfg = parse_config(d)
    st = decision.build_state(cfg, {"title": "T", "salary_min": 100000, "salary_max": 120000,
                                    "description": "secret"}, "rubric")
    assert st == {"posting": {"title": "T", "salary": "$100,000 - $120,000"}}


def _setup(cfg_dir: Path, decision_yaml: dict, scorer="decision"):
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "employers.yaml").write_text(yaml.safe_dump(
        [{"name": "MockCorp", "ats": "greenhouse", "board": "mockcorp"}]))
    (cfg_dir / "filters.yaml").write_text(yaml.safe_dump(
        {"locations": ["Remote"], "scorer": scorer}))
    (cfg_dir / "criteria.md").write_text("Backend engineer.\n")
    (cfg_dir / "decision.yaml").write_text(yaml.safe_dump(decision_yaml))


def _fake_jobs(board):
    return [{"id": 1, "title": "Senior Backend Engineer", "absolute_url": "https://example.com/1",
             "location": {"name": "Remote"}, "updated_at": "2026-09-01T00:00:00Z",
             "content": "<p>Build backend services and APIs in Go; distributed systems.</p>"}]


def test_pipeline_decision_mode_scores_and_versions(tmp_path, monkeypatch):
    d = copy.deepcopy(_raw())
    d["transport"] = {"kind": "mock"}
    _setup(tmp_path / "cfg", d)
    monkeypatch.setattr("career_radar.fetchers.greenhouse.fetch_jobs", _fake_jobs)
    pipeline.run_pipeline(config_dir=tmp_path / "cfg", db_path=tmp_path / "db.sqlite",
                          output_dir=tmp_path / "out")
    conn = dedupe.connect(tmp_path / "db.sqlite")
    row = conn.execute("SELECT * FROM postings").fetchone()
    assert row["score"] is not None
    assert row["scorer_version"] == f"decision:{parse_config(d).qhash}"
    assert set(json.loads(row["answers"])) == set(d["questions"])


def test_pipeline_bad_decision_config_fails_before_any_call(tmp_path, monkeypatch):
    d = copy.deepcopy(_raw())
    d["fold"]["clamp"].append({"if": {"seniority": "outt"}, "max": 3})
    _setup(tmp_path / "cfg", d)
    monkeypatch.setattr("career_radar.fetchers.greenhouse.fetch_jobs", _fake_jobs)
    calls = []
    monkeypatch.setattr("requests.post", lambda *a, **k: calls.append(1))
    with pytest.raises(DecisionConfigError):
        pipeline.run_pipeline(config_dir=tmp_path / "cfg", db_path=tmp_path / "db.sqlite",
                              output_dir=tmp_path / "out")
    assert calls == []


def test_unknown_scorer_is_an_error(tmp_path, monkeypatch):
    d = copy.deepcopy(_raw())
    _setup(tmp_path / "cfg", d, scorer="decisoin")
    monkeypatch.setattr("career_radar.fetchers.greenhouse.fetch_jobs", _fake_jobs)
    with pytest.raises(ValueError, match="scorer must be one of"):
        pipeline.run_pipeline(config_dir=tmp_path / "cfg", db_path=tmp_path / "db.sqlite",
                              output_dir=tmp_path / "out")


def test_mock_transport_is_deterministic():
    d = copy.deepcopy(_raw())
    cfg = parse_config(d)
    st = decision.build_state(cfg, {"title": "Backend Engineer",
                                    "description": "Build backend services and APIs in Go."})
    a = Mock().decide(st, cfg).answers
    b = make_transport("mock").decide(st, cfg).answers
    assert {k: v.value for k, v in a.items()} == {k: v.value for k, v in b.items()}
    assert a["domain"].value == 3
