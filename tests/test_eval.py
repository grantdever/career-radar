import json
import shutil
from pathlib import Path

import pytest
import yaml

from career_radar.core import eval as ev

ROOT = Path(__file__).resolve().parent.parent
SYN = ROOT / "examples" / "synthetic"


@pytest.fixture
def evdir(tmp_path):
    d = tmp_path / "synthetic"
    d.mkdir()
    for name in ("pool.jsonl", "labels.jsonl", "criteria.md", "decision.titles-only.yaml"):
        shutil.copy(SYN / name, d / name)
    shutil.copy(ROOT / "examples" / "decision.backend-engineer.yaml", tmp_path / "decision.backend-engineer.yaml")
    shutil.copy(SYN / "eval.yaml", d / "eval.yaml")
    return d


def test_synthetic_end_to_end(evdir):
    spec = ev.load_spec(evdir)
    ev.freeze(spec)
    ev.run(spec, progress=lambda m: None)
    out = ev.report(spec, sweep=True)
    assert out["n_labelled"] == 200
    assert out["n_positive"] >= 15
    for c in out["contestants"].values():
        assert 0 <= c["f1"] <= 1 and c["failure_rate"] == 0
        lo, hi = c["recall_ci95"]
        assert lo <= c["recall"] <= hi
    assert out["verdict"]["outcome"] in ("winner", "tie")
    assert "paired_bootstrap" in out
    md = (evdir / "report.md").read_text()
    assert "EXPLORATORY" in md and "Pre-registered verdict" in md
    assert json.loads((evdir / "report.json").read_text())["name"] == "synthetic-backend-engineer"


def test_freeze_once_and_run_sealed(evdir):
    spec = ev.load_spec(evdir)
    ev.freeze(spec)
    with pytest.raises(ev.EvalError, match="already exists"):
        ev.freeze(spec)
    ev.run(spec, progress=lambda m: None)
    with pytest.raises(ev.EvalError, match="sealed"):
        ev.run(spec, progress=lambda m: None)


@pytest.mark.parametrize("change,what", [
    (lambda d: (d / "pool.jsonl").write_text((d / "pool.jsonl").read_text()[:-200] + "\n"), "pool changed"),
    (lambda d: (d / "criteria.md").write_text("new rubric"), "criteria changed"),
    (lambda d: (d / "decision.titles-only.yaml").write_text(
        (d / "decision.titles-only.yaml").read_text().replace("table: [1, 2, 3, 4, 6, 7, 9]",
                                                              "table: [1, 2, 3, 4, 6, 8, 9]")),
     "contestant titles-only changed"),
])
def test_run_refuses_on_drift(evdir, change, what):
    spec = ev.load_spec(evdir)
    ev.freeze(spec)
    change(evdir)
    with pytest.raises(ev.EvalError, match=what):
        ev.run(ev.load_spec(evdir), progress=lambda m: None)


def test_cutoff_change_is_drift(evdir):
    spec = ev.load_spec(evdir)
    ev.freeze(spec)
    data = yaml.safe_load((evdir / "eval.yaml").read_text())
    data["contestants"][0]["cutoff"] = 7
    (evdir / "eval.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(ev.EvalError, match="full-posting changed .*cutoff"):
        ev.run(ev.load_spec(evdir), progress=lambda m: None)


def test_replay_reproduces_live_run(evdir):
    spec = ev.load_spec(evdir)
    ev.freeze(spec)
    ev.run(spec, progress=lambda m: None)
    first = {(p["id"], p["contestant"]): p["score"] for p in ev.read_jsonl(evdir / "predictions.jsonl")}
    # Same experiment, answers replayed from disk: zero calls, same scores.
    rep = evdir.parent / "replayed"
    shutil.copytree(evdir, rep)
    for f in ("LOCK.yaml", "predictions.jsonl"):
        (rep / f).unlink()
    data = yaml.safe_load((rep / "eval.yaml").read_text())
    for c in data["contestants"]:
        c["transport"] = {"kind": "replay", "path": f"replay-{c['name']}.jsonl"}
    (rep / "eval.yaml").write_text(yaml.safe_dump(data))
    spec2 = ev.load_spec(rep)
    ev.freeze(spec2)
    ev.run(spec2, progress=lambda m: None)
    second = {(p["id"], p["contestant"]): p["score"] for p in ev.read_jsonl(rep / "predictions.jsonl")}
    assert first == second


def test_blind_label_queue_hides_scores_and_records(evdir):
    (evdir / "labels.jsonl").unlink()
    data = yaml.safe_load((evdir / "eval.yaml").read_text())
    data["queue"] = {"random_neither": 10, "seed": 3}
    (evdir / "eval.yaml").write_text(yaml.safe_dump(data))
    spec = ev.load_spec(evdir)
    ev.freeze(spec)
    ev.run(spec, progress=lambda m: None)
    shown, answers = [], iter(["y", "n", "2", "too much frontend", "s", "q"])
    n = ev.label_interactive(spec, ask=lambda p: next(answers), show=shown.append)
    assert n == 3
    text = "\n".join(shown)
    assert "score" not in text.lower() and "surfaced" not in text.lower()
    labels = ev.read_jsonl(evdir / "labels.jsonl")
    assert [lab["verdict"] for lab in labels] == ["interested", "not_interested", "skip"]
    assert labels[1]["reason"] == "role-shape"
    queue = ev.read_jsonl(evdir / "queue.jsonl")
    strata = {q["stratum"] for q in queue}
    assert "neither-sample" in strata
    assert all(q["weight"] > 1 for q in queue if q["stratum"] == "neither-sample")


def test_stats_helpers():
    lo, hi = ev.wilson(8, 10)
    assert 0.44 < lo < 0.5 and 0.94 < hi < 0.98
    assert ev.wilson(0, 0) == (0.0, 1.0)
    pairs = [(True, True)] * 8 + [(True, False)] * 2 + [(False, True)] * 2 + [(False, False)] * 8
    m = ev.prf(ev.confusion(pairs))
    assert m["precision"] == pytest.approx(0.8) and m["f1"] == pytest.approx(0.8)
    b = ev.paired_bootstrap(pairs, pairs, iters=200)
    assert b["delta_f1"] == 0 and b["ci95"] == [0.0, 0.0]


def test_rule_underpowered_and_winner():
    lock = {"stopping_rule": {"min_positives": 15, "extra_batch": 150},
            "decision_rule": {"min_gap": 0.10, "min_winner_recall": 0.80}}
    assert ev.apply_rule({"n_positive": 10, "contestants": {}}, lock)["outcome"] == "underpowered"
    out = {"n_positive": 20, "contestants": {"a": {"f1": 0.8, "recall": 0.85},
                                             "b": {"f1": 0.6, "recall": 0.9}}}
    assert ev.apply_rule(out, lock) == {**ev.apply_rule(out, lock), "outcome": "winner", "winner": "a"}
    out["contestants"]["a"]["recall"] = 0.7
    assert ev.apply_rule(out, lock)["outcome"] == "tie"
