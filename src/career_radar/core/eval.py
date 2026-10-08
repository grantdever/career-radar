"""Pre-registered scorer evaluation: freeze -> run -> label -> report.

An eval lives in one directory with an ``eval.yaml`` spec::

    name: my-bakeoff
    pool: pool.jsonl            # one posting per line (id, title, employer, ...)
    criteria: criteria.md       # the rubric every contestant reads
    contestants:
      - {name: decision, scorer: decision, config: decision.yaml, cutoff: 8}
      - {name: llm, scorer: llm, model: gpt-4o-mini, cutoff: 7}
    primary_metric: f1
    decision_rule: {min_gap: 0.10, min_winner_recall: 0.80}
    stopping_rule: {min_positives: 15, extra_batch: 150}
    queue: {random_neither: 50, seed: 7}   # optional; default = label every row

1. ``freeze`` writes LOCK.yaml: hashes of the pool, criteria, and each
   contestant (questions, fold, model, cutoff), plus the decision and
   stopping rules. Commit it before scoring.
2. ``run`` refuses if anything hashed in LOCK.yaml has drifted, then writes
   sealed predictions (scores, answers, cost, latency) and replay files.
3. ``label`` shows a blind, shuffled queue (no scores) and records verdicts
   with reason codes.
4. ``report`` computes P/R/F1 with Wilson intervals, a paired bootstrap of
   the F1 gap, disagreements, errors by reason, cost and latency, applies
   the pre-registered rule, and shows any cutoff sweep under an EXPLORATORY
   banner. Writes report.json and report.md.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

LOCK_FILE = "LOCK.yaml"
PREDICTIONS_FILE = "predictions.jsonl"
LABELS_FILE = "labels.jsonl"
QUEUE_FILE = "queue.jsonl"
DEFAULT_REASONS = ["domain", "role-shape", "seniority", "location", "comp", "org", "other"]
DEFAULT_DECISION_RULE = {"min_gap": 0.10, "min_winner_recall": 0.80}
DEFAULT_STOPPING_RULE = {"min_positives": 15, "extra_batch": 150}
BOOTSTRAP_ITERS = 2000


class EvalError(RuntimeError):
    """The eval spec, lock, or files are inconsistent."""


# --- Files --------------------------------------------------------------------


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha_obj(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")


@dataclass
class Spec:
    dir: Path
    data: dict

    @property
    def name(self) -> str:
        return str(self.data.get("name") or self.dir.name)

    def path(self, key: str) -> Path:
        value = self.data.get(key)
        if not value:
            raise EvalError(f"eval.yaml needs {key!r}")
        return (self.dir / value).resolve()

    @property
    def contestants(self) -> list[dict]:
        cs = self.data.get("contestants") or []
        if not cs:
            raise EvalError("eval.yaml needs at least one contestant")
        names = [c.get("name") for c in cs]
        if None in names or len(set(names)) != len(names):
            raise EvalError("every contestant needs a unique name")
        for c in cs:
            if c.get("scorer") not in ("decision", "llm"):
                raise EvalError(f"contestant {c['name']}: scorer must be decision or llm")
            if not isinstance(c.get("cutoff"), int):
                raise EvalError(f"contestant {c['name']}: integer cutoff required (pre-register it)")
        return cs

    def pool(self) -> list[dict]:
        rows = read_jsonl(self.path("pool"))
        ids = [r.get("id") for r in rows]
        if None in ids or len(set(ids)) != len(ids):
            raise EvalError("every pool row needs a unique 'id'")
        return rows

    def criteria(self) -> str:
        p = self.data.get("criteria")
        return self.path("criteria").read_text(encoding="utf-8") if p else ""


def load_spec(path: str | Path) -> Spec:
    p = Path(path)
    if p.is_dir():
        p = p / "eval.yaml"
    if not p.exists():
        raise EvalError(f"{p} not found")
    return Spec(p.parent.resolve(), yaml.safe_load(p.read_text(encoding="utf-8")) or {})


# --- Contestants --------------------------------------------------------------


def _decision_cfg(spec: Spec, c: dict):
    from career_radar.core import decision

    cfg = decision.load_config(spec.dir / c["config"])
    return cfg


def contestant_fingerprint(spec: Spec, c: dict) -> dict:
    """Everything that, if changed, would make a run a different experiment."""
    fp: dict[str, Any] = {"scorer": c["scorer"], "cutoff": c["cutoff"],
                          "transport_override": c.get("transport")}
    if c["scorer"] == "decision":
        cfg = _decision_cfg(spec, c)
        fp.update({"qhash": cfg.qhash, "model": cfg.transport.model,
                   "transport": c.get("transport", {}).get("kind") or cfg.transport.kind,
                   "config_sha256": _sha(spec.dir / c["config"])})
    else:
        from career_radar.core.score import SCORING_GUIDE
        fp.update({"model": c.get("model", ""), "prompt_sha256": _sha_obj(SCORING_GUIDE)})
    fp["hash"] = _sha_obj({k: v for k, v in fp.items() if k != "hash"})[:16]
    return fp


def current_hashes(spec: Spec) -> dict:
    return {
        "pool_sha256": _sha(spec.path("pool")),
        "criteria_sha256": _sha(spec.path("criteria")) if spec.data.get("criteria") else None,
        "contestants": {c["name"]: contestant_fingerprint(spec, c) for c in spec.contestants},
    }


# --- freeze -------------------------------------------------------------------


def freeze(spec: Spec, *, note: str = "") -> Path:
    lock = spec.dir / LOCK_FILE
    if lock.exists():
        raise EvalError(f"{lock} already exists; an eval is frozen once. "
                        "Start a new eval directory to change anything.")
    pool = spec.pool()
    hashes = current_hashes(spec)
    data = {
        "name": spec.name,
        "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pool_rows": len(pool),
        "hashes": hashes,
        "cutoffs": {c["name"]: c["cutoff"] for c in spec.contestants},
        "primary_metric": spec.data.get("primary_metric", "f1"),
        "decision_rule": {**DEFAULT_DECISION_RULE, **(spec.data.get("decision_rule") or {})},
        "stopping_rule": {**DEFAULT_STOPPING_RULE, **(spec.data.get("stopping_rule") or {})},
        "rule_text": (
            "Primary metric F1 at each contestant's pre-registered cutoff. If labelled "
            "positives < stopping_rule.min_positives, label one extra batch of "
            "stopping_rule.extra_batch rows, then stop; if still short, report "
            "'underpowered' and do not apply the decision rule. Otherwise a contestant "
            "wins only if its F1 exceeds the other's by more than decision_rule.min_gap "
            "AND its recall >= decision_rule.min_winner_recall; anything else is a "
            "declared tie, settled on cost, latency and reliability."),
    }
    if note:
        data["note"] = note
    lock.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return lock


def load_lock(spec: Spec) -> dict:
    lock = spec.dir / LOCK_FILE
    if not lock.exists():
        raise EvalError(f"{lock} missing; run `career-radar eval freeze` first")
    return yaml.safe_load(lock.read_text(encoding="utf-8"))


def drift(spec: Spec, lock: dict) -> list[str]:
    """Human-readable list of anything that changed since freeze."""
    now, then = current_hashes(spec), lock["hashes"]
    out = []
    for k in ("pool_sha256", "criteria_sha256"):
        if now[k] != then.get(k):
            out.append(f"{k.split('_')[0]} changed")
    for name, fp in then["contestants"].items():
        cur = now["contestants"].get(name)
        if cur is None:
            out.append(f"contestant {name} removed")
        elif cur["hash"] != fp["hash"]:
            diff = [k for k in fp if k != "hash" and fp.get(k) != cur.get(k)]
            out.append(f"contestant {name} changed ({', '.join(diff)})")
    for name in now["contestants"]:
        if name not in then["contestants"]:
            out.append(f"contestant {name} added")
    return out


# --- run ----------------------------------------------------------------------


def _posting(row: dict) -> dict:
    p = dict(row)
    p.setdefault("req_id", str(row["id"]))
    p.setdefault("source", "eval")
    for k in ("location", "remote_type", "description", "posted_date"):
        p.setdefault(k, "")
    p.setdefault("salary_min", None)
    p.setdefault("salary_max", None)
    return p


def _run_decision(spec: Spec, c: dict, pool: list[dict], criteria: str, ledger,
                  progress: Callable[[str], None]) -> tuple[list[dict], list[dict]]:
    from career_radar.core import decision
    from career_radar.core.transports import make_transport

    cfg = _decision_cfg(spec, c)
    override = dict(c.get("transport") or {})
    if override.get("path"):
        override["path"] = str((spec.dir / override["path"]).resolve())
    spec_t = decision.Transport(
        kind=override.pop("kind", None) or cfg.transport.kind,
        model=override.pop("model", None) or cfg.transport.model,
        options={**cfg.transport.options, **override})
    tr = make_transport(spec_t, ledger=ledger)
    preds, replay = [], []
    for i, row in enumerate(pool, 1):
        posting = _posting(row)
        state = decision.build_state(cfg, posting, criteria)
        key = decision.state_key(state)
        t0 = time.time()
        try:
            res = tr.decide(state, cfg)
            folded = decision.fold(cfg, res.answers)
            answers = {n: a.to_dict() for n, a in res.answers.items()}
            preds.append({"id": row["id"], "contestant": c["name"], "score": folded.score,
                          "surfaced": folded.score >= c["cutoff"], "flags": folded.flags,
                          "answers": answers, "state_key": key, "cost_usd": res.cost_usd,
                          "latency_ms": round(res.latency_ms or (time.time() - t0) * 1000, 1),
                          "error": None})
            replay.append({"state_key": key, "answers": answers})
        except Exception as e:
            preds.append({"id": row["id"], "contestant": c["name"], "score": None,
                          "surfaced": False, "flags": [], "answers": None, "state_key": key,
                          "cost_usd": 0.0, "latency_ms": round((time.time() - t0) * 1000, 1),
                          "error": f"{type(e).__name__}: {str(e)[:200]}"})
        if i % 25 == 0:
            progress(f"  {c['name']}: {i}/{len(pool)}")
    return preds, replay


def _run_llm(spec: Spec, c: dict, pool: list[dict], criteria: str,
             progress: Callable[[str], None]) -> list[dict]:
    from career_radar.core import score as llm_score

    preds = []
    batch = llm_score.BATCH_SIZE
    for start in range(0, len(pool), batch):
        chunk = [_posting(r) for r in pool[start:start + batch]]
        t0 = time.time()
        try:
            prompt = llm_score._prompt(criteria, [llm_score._posting_payload(p) for p in chunk])
            prompt += "\nOutput must be a JSON object with a 'results' key containing the array."
            raw = _llm_call(prompt, c.get("model", ""))
            items = llm_score.extract_json_array(raw)
            results = {}
            for it in items:
                try:
                    rid, s, _r, fl = llm_score._validate_item(it)
                    results[rid] = (s, json.loads(fl))
                except Exception:
                    pass
            err = None
        except Exception as e:
            results, err = {}, f"{type(e).__name__}: {str(e)[:200]}"
        per_row = round((time.time() - t0) * 1000 / max(1, len(chunk)), 1)
        for p in chunk:
            s, fl = results.get(p["req_id"], (None, []))
            preds.append({"id": p["id"], "contestant": c["name"], "score": s,
                          "surfaced": s is not None and s >= c["cutoff"], "flags": fl,
                          "answers": None, "state_key": None, "cost_usd": 0.0,
                          "latency_ms": per_row,
                          "error": None if s is not None else (err or "row missing from output")})
        progress(f"  {c['name']}: {min(start + batch, len(pool))}/{len(pool)}")
    return preds


def _llm_call(prompt: str, model: str) -> str:
    import litellm

    resp = litellm.completion(model=model, messages=[{"role": "user", "content": prompt}],
                              response_format={"type": "json_object"})
    return resp.choices[0].message.content


def run(spec: Spec, *, progress: Callable[[str], None] = print) -> Path:
    lock = load_lock(spec)
    changed = drift(spec, lock)
    if changed:
        raise EvalError("refusing to run: inputs drifted since freeze: " + "; ".join(changed))
    out = spec.dir / PREDICTIONS_FILE
    if out.exists():
        raise EvalError(f"{out} exists; predictions are sealed once written")
    from career_radar.core.spend import Ledger

    ledger = Ledger.at(spec.dir / "spend.db")
    pool, criteria = spec.pool(), spec.criteria()
    preds: list[dict] = []
    for c in spec.contestants:
        progress(f"scoring {len(pool)} rows with {c['name']} ({c['scorer']})")
        if c["scorer"] == "decision":
            p, replay = _run_decision(spec, c, pool, criteria, ledger, progress)
            write_jsonl(spec.dir / f"replay-{c['name']}.jsonl", replay)
        else:
            p = _run_llm(spec, c, pool, criteria, progress)
        preds += p
    write_jsonl(out, preds)
    return out


# --- label --------------------------------------------------------------------


def build_queue(spec: Spec) -> list[dict]:
    """Rows to label, with sampling weights; written once, then reused.

    Default: every pool row. With ``queue.random_neither: N`` and sealed
    predictions present: every row any contestant surfaced, every row whose
    ``stratum`` is ``organic``, and a seeded random N of the rest, weighted
    by inverse sampling probability so recall can be estimated.
    """
    qpath = spec.dir / QUEUE_FILE
    if qpath.exists():
        return read_jsonl(qpath)
    pool = spec.pool()
    qcfg = spec.data.get("queue") or {}
    seed = int(qcfg.get("seed", 7))
    n_neither = qcfg.get("random_neither")
    preds = read_jsonl(spec.dir / PREDICTIONS_FILE)
    if n_neither is None or not preds:
        queue = [{"id": r["id"], "stratum": r.get("stratum", "all"), "weight": 1.0} for r in pool]
    else:
        surfaced = {p["id"] for p in preds if p["surfaced"]}
        queue, rest = [], []
        for r in pool:
            if r["id"] in surfaced:
                queue.append({"id": r["id"], "stratum": "surfaced", "weight": 1.0})
            elif r.get("stratum") == "organic":
                queue.append({"id": r["id"], "stratum": "organic", "weight": 1.0})
            else:
                rest.append(r)
        rng = random.Random(seed)
        k = min(int(n_neither), len(rest))
        w = len(rest) / k if k else 0.0
        queue += [{"id": r["id"], "stratum": "neither-sample", "weight": round(w, 4)}
                  for r in rng.sample(rest, k)]
    random.Random(seed + 1).shuffle(queue)
    write_jsonl(qpath, queue)
    return queue


def label_interactive(spec: Spec, *, ask: Callable[[str], str] = input,
                      show: Callable[[str], None] = print, desc_chars: int = 1800) -> int:
    """Blind terminal labelling. Shows postings only, never scores. Resumable."""
    pool = {r["id"]: r for r in spec.pool()}
    queue = build_queue(spec)
    lpath = spec.dir / LABELS_FILE
    done = {r["id"] for r in read_jsonl(lpath)}
    todo = [q for q in queue if q["id"] not in done]
    reasons = spec.data.get("reason_codes") or DEFAULT_REASONS
    show(f"{len(done)} labelled, {len(todo)} to go. Answers: y = would apply, n = pass, "
         "s = skip, q = quit (progress is saved).")
    n = 0
    for q in todo:
        r = pool[q["id"]]
        salary = ""
        if r.get("salary_min"):
            salary = f" | ${r['salary_min']:,}" + (f"-{r['salary_max']:,}" if r.get("salary_max") else "")
        show("\n" + "=" * 72)
        show(f"{r.get('title', '')} — {r.get('employer', '')}")
        show(f"{r.get('location', '') or 'location not stated'} {r.get('remote_type') or ''}{salary}")
        show("-" * 72)
        show((r.get("description") or "")[:desc_chars])
        while True:
            a = ask("Apply? [y/n/s/q] ").strip().lower()
            if a in ("y", "n", "s", "q"):
                break
        if a == "q":
            break
        rec = {"id": q["id"], "verdict": {"y": "interested", "n": "not_interested",
                                          "s": "skip"}[a], "reason": None, "note": "",
               "labelled_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if a == "n":
            menu = " ".join(f"{i}={c}" for i, c in enumerate(reasons, 1))
            pick = ask(f"Reason? {menu} ").strip()
            rec["reason"] = reasons[int(pick) - 1] if pick.isdigit() and 1 <= int(pick) <= len(reasons) else "other"
            rec["note"] = ask("Note (optional): ").strip()
        with lpath.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
        n += 1
    return n


# --- report -------------------------------------------------------------------


def wilson(k: float, n: float, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def confusion(pairs: list[tuple[bool, bool]], weights: list[float] | None = None) -> dict:
    w = weights or [1.0] * len(pairs)
    tp = sum(wi for (s, y), wi in zip(pairs, w) if s and y)
    fp = sum(wi for (s, y), wi in zip(pairs, w) if s and not y)
    fn = sum(wi for (s, y), wi in zip(pairs, w) if not s and y)
    tn = sum(wi for (s, y), wi in zip(pairs, w) if not s and not y)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn}


def prf(cm: dict) -> dict:
    tp, fp, fn = cm["tp"], cm["fp"], cm["fn"]
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return {"precision": p, "recall": r, "f1": f1}


def _f1(pairs: list[tuple[bool, bool]]) -> float:
    return prf(confusion(pairs))["f1"]


def paired_bootstrap(a: list[tuple[bool, bool]], b: list[tuple[bool, bool]],
                     iters: int = BOOTSTRAP_ITERS, seed: int = 11) -> dict:
    """Bootstrap the F1 gap (a - b) by resampling labelled rows jointly."""
    rng = random.Random(seed)
    n = len(a)
    deltas = []
    for _ in range(iters):
        idx = [rng.randrange(n) for _ in range(n)]
        deltas.append(_f1([a[i] for i in idx]) - _f1([b[i] for i in idx]))
    deltas.sort()
    return {"delta_f1": _f1(a) - _f1(b), "ci95": [deltas[int(0.025 * iters)],
                                                 deltas[int(0.975 * iters) - 1]],
            "p_a_better": sum(d > 0 for d in deltas) / iters, "iters": iters}


def report(spec: Spec, *, sweep: bool = False) -> dict:
    lock = load_lock(spec)
    pool = {r["id"]: r for r in spec.pool()}
    preds = read_jsonl(spec.dir / PREDICTIONS_FILE)
    if not preds:
        raise EvalError("no predictions; run `career-radar eval run` first")
    labels = {r["id"]: r for r in read_jsonl(spec.dir / LABELS_FILE) if r["verdict"] != "skip"}
    queue = {q["id"]: q for q in read_jsonl(spec.dir / QUEUE_FILE)}
    names = [c["name"] for c in spec.contestants]
    by = {n: {p["id"]: p for p in preds if p["contestant"] == n} for n in names}
    ids = sorted(i for i in labels if all(i in by[n] for n in names))
    truth = {i: labels[i]["verdict"] == "interested" for i in ids}
    weights = [float(queue.get(i, {}).get("weight", 1.0)) for i in ids]
    positives = sum(truth.values())

    out: dict[str, Any] = {
        "name": spec.name, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "lock": {"frozen_at": lock["frozen_at"], "hashes": lock["hashes"],
                 "cutoffs": lock["cutoffs"], "decision_rule": lock["decision_rule"],
                 "stopping_rule": lock["stopping_rule"]},
        "n_labelled": len(ids), "n_positive": positives, "contestants": {},
    }
    pairs_by: dict[str, list[tuple[bool, bool]]] = {}
    for n in names:
        rows = [by[n][i] for i in ids]
        pairs = [(bool(p["surfaced"]), truth[p["id"]]) for p in rows]
        pairs_by[n] = pairs
        cm = confusion(pairs)
        m = prf(cm)
        all_rows = list(by[n].values())
        errors = sum(1 for p in all_rows if p.get("error"))
        lat = [p["latency_ms"] for p in all_rows if p.get("latency_ms")]
        entry = {
            "cutoff": lock["cutoffs"][n], **{k: int(v) for k, v in cm.items()}, **m,
            "precision_ci95": wilson(cm["tp"], cm["tp"] + cm["fp"]),
            "recall_ci95": wilson(cm["tp"], cm["tp"] + cm["fn"]),
            "surfaced_all": sum(1 for p in all_rows if p["surfaced"]),
            "cost_usd_total": round(sum(p.get("cost_usd") or 0 for p in all_rows), 6),
            "cost_usd_per_row": round(sum(p.get("cost_usd") or 0 for p in all_rows)
                                      / max(1, len(all_rows)), 8),
            "latency_ms_mean": round(sum(lat) / len(lat), 1) if lat else None,
            "failure_rate": errors / max(1, len(all_rows)),
            "false_positive_reasons": _reason_counts(
                [labels[i] for (s, y), i in zip(pairs, ids) if s and not y]),
            "false_negatives": [i for (s, y), i in zip(pairs, ids) if not s and y],
        }
        if any(w != 1.0 for w in weights):
            wm = prf(confusion(pairs, weights))
            entry["weighted_recall_estimate"] = wm["recall"]
        out["contestants"][n] = entry

    if len(names) >= 2 and ids:
        a, b = names[0], names[1]
        out["paired_bootstrap"] = {"a": a, "b": b, **paired_bootstrap(pairs_by[a], pairs_by[b])}
        out["disagreements"] = [
            {"id": i, "title": pool[i].get("title"), "employer": pool[i].get("employer"),
             "label": "interested" if truth[i] else "not_interested",
             **{f"{n}_score": by[n][i]["score"] for n in names}}
            for k, i in enumerate(ids) if pairs_by[a][k][0] != pairs_by[b][k][0]]
    out["verdict"] = apply_rule(out, lock)
    if sweep:
        out["exploratory_sweep"] = {
            n: [{"cutoff": cut, "preregistered": cut == lock["cutoffs"][n],
                 **prf(confusion([((by[n][i]["score"] or 0) >= cut, truth[i]) for i in ids]))}
                for cut in range(1, 11)] for n in names}
    (spec.dir / "report.json").write_text(json.dumps(out, indent=1, default=float) + "\n",
                                          encoding="utf-8")
    (spec.dir / "report.md").write_text(render_markdown(out), encoding="utf-8")
    return out


def _reason_counts(rows: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for r in rows:
        k = r.get("reason") or "unspecified"
        counts[k] = counts.get(k, 0) + 1
    return counts


def apply_rule(out: dict, lock: dict) -> dict:
    stop, rule = lock["stopping_rule"], lock["decision_rule"]
    if out["n_positive"] < stop["min_positives"]:
        return {"outcome": "underpowered",
                "text": f"{out['n_positive']} positives < {stop['min_positives']}: label one "
                        f"extra batch of {stop['extra_batch']} if not done yet; otherwise report "
                        "as underpowered and do not apply the decision rule."}
    cs = out["contestants"]
    if len(cs) < 2:
        return {"outcome": "single-contestant", "text": "Nothing to compare."}
    ranked = sorted(cs.items(), key=lambda kv: kv[1]["f1"], reverse=True)
    (w, wm), (_l, lm) = ranked[0], ranked[1]
    gap = wm["f1"] - lm["f1"]
    if gap > rule["min_gap"] and wm["recall"] >= rule["min_winner_recall"]:
        return {"outcome": "winner", "winner": w,
                "text": f"{w} wins: F1 gap {gap:.3f} > {rule['min_gap']} and recall "
                        f"{wm['recall']:.2f} >= {rule['min_winner_recall']}."}
    return {"outcome": "tie",
            "text": f"Declared tie (F1 gap {gap:.3f}, leader recall {wm['recall']:.2f}): "
                    "settle on cost, latency and reliability."}


def render_markdown(out: dict) -> str:
    L = [f"# Eval report: {out['name']}", "",
         f"Frozen {out['lock']['frozen_at']}; {out['n_labelled']} labelled rows, "
         f"{out['n_positive']} positive.", "",
         f"**Pre-registered verdict:** {out['verdict']['text']}", "",
         "| Contestant | Cutoff | P [95% CI] | R [95% CI] | F1 | TP/FP/FN/TN | $/row | ms/row | Fail |",
         "|---|---|---|---|---|---|---|---|---|"]
    for n, c in out["contestants"].items():
        pc, rc = c["precision_ci95"], c["recall_ci95"]
        L.append(f"| {n} | {c['cutoff']} | {c['precision']:.2f} [{pc[0]:.2f}, {pc[1]:.2f}] | "
                 f"{c['recall']:.2f} [{rc[0]:.2f}, {rc[1]:.2f}] | {c['f1']:.3f} | "
                 f"{c['tp']}/{c['fp']}/{c['fn']}/{c['tn']} | {c['cost_usd_per_row']:.6f} | "
                 f"{c['latency_ms_mean'] if c['latency_ms_mean'] is not None else '-'} | "
                 f"{c['failure_rate']:.0%} |")
    for n, c in out["contestants"].items():
        if "weighted_recall_estimate" in c:
            L.append(f"\n{n}: recall re-weighted for queue sampling = "
                     f"{c['weighted_recall_estimate']:.2f}")
    if "paired_bootstrap" in out:
        b = out["paired_bootstrap"]
        L += ["", f"Paired bootstrap, F1({b['a']}) - F1({b['b']}) = {b['delta_f1']:+.3f}, "
                  f"95% CI [{b['ci95'][0]:+.3f}, {b['ci95'][1]:+.3f}], "
                  f"P({b['a']} better) = {b['p_a_better']:.2f} ({b['iters']} resamples)."]
    L += ["", "## False positives by label reason", ""]
    for n, c in out["contestants"].items():
        reasons = ", ".join(f"{k}: {v}" for k, v in sorted(c["false_positive_reasons"].items()))
        L.append(f"- {n}: {reasons or 'none'}")
    if out.get("disagreements"):
        L += ["", f"## Disagreements ({len(out['disagreements'])})", ""]
        names = list(out["contestants"])
        L += ["| Title | Employer | Label | " + " | ".join(names) + " |",
              "|---|---|---|" + "---|" * len(names)]
        for d in out["disagreements"][:50]:
            L.append(f"| {d['title']} | {d['employer']} | {d['label']} | "
                     + " | ".join(str(d[f'{n}_score']) for n in names) + " |")
    if "exploratory_sweep" in out:
        L += ["", "## EXPLORATORY: cutoff sweep (chosen after seeing labels; not evidence)", "",
              "Only the pre-registered row (marked *) counts. Any better cutoff here was "
              "found after the fact.", ""]
        for n, rows in out["exploratory_sweep"].items():
            L += [f"**{n}**", "", "| Cutoff | P | R | F1 |", "|---|---|---|---|"]
            for r in rows:
                star = "*" if r["preregistered"] else ""
                L.append(f"| {r['cutoff']}{star} | {r['precision']:.2f} | {r['recall']:.2f} | "
                         f"{r['f1']:.3f} |")
            L.append("")
    L += ["", "## Hashes", "", "```json", json.dumps(out["lock"]["hashes"], indent=1), "```", ""]
    return "\n".join(L)
