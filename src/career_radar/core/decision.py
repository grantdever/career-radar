"""Decision-model scoring: typed questions, a validated config, and a fold
you can audit.

Instead of asking one LLM for a 1-10 score, ``scorer: decision`` asks a set
of typed questions about each posting and folds the answers into a score in
plain code:

    score   - an ordinal level (0 .. levels-1), e.g. "how close is the domain?"
    choice  - one of a fixed set of options, e.g. seniority in_range / out
    noul    - a probability in [0, 1] that something is true (a veto)

The config lives in ``decision.yaml`` in the user's config dir::

    transport: {kind: openrouter-decisions, model: typesafe/jev-1.13-20260917}
    state: {fields: [title, employer, location, salary, description],
            include_criteria: true}
    questions:
      domain:    {type: score, levels: 4, instructions: "...",
                  criteria: [{what: "...", examples: [...], not_for: "..."}, ...]}
      seniority: {type: choice, instructions: "...",
                  options: {in_range: "...", out: "..."}}
      hard_veto: {type: noul, instructions: "Does this role require ...?"}
    fold:
      base:   {sum: [domain, writing], table: [1, 2, 3, 5, 6, 8, 9]}
      adjust: [{if: {org_fit: good}, add: 1}]
      clamp:  [{if: {seniority: out}, max: 3},
               {if: {hard_veto: ">=0.5"}, max: 2, flag: hard-veto}]
      confidence: {min: 0.55, flag: low-confidence}
    surface_cutoff: 8

Everything the fold references is checked when the file is loaded, so a
mistyped question name or option fails before any paid call is made.
"""

from __future__ import annotations

import hashlib
import json
import logging
import operator
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DECISION_FILENAME = "decision.yaml"
QUESTION_TYPES = ("score", "choice", "noul")
DEFAULT_STATE_FIELDS = ["title", "employer", "location", "remote_type", "salary", "description"]
ALLOWED_STATE_FIELDS = set(DEFAULT_STATE_FIELDS) | {"department", "posted_date", "url"}
DESCRIPTION_CHARS = 6000
CRITERIA_CHARS = 8000

_COMPARE = re.compile(r"^\s*(>=|<=|>|<|==)\s*([0-9]*\.?[0-9]+)\s*$")
_OPS = {">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt,
        "==": operator.eq}


class DecisionConfigError(ValueError):
    """decision.yaml is malformed or internally inconsistent."""


class AnswerError(ValueError):
    """A transport returned an answer that does not fit its question."""


# --- Config -------------------------------------------------------------------


@dataclass(frozen=True)
class Question:
    name: str
    type: str
    instructions: str
    levels: int = 0  # score only
    criteria: list = field(default_factory=list)  # score: one entry per level
    options: dict = field(default_factory=dict)  # choice: option -> description

    def wire(self) -> dict:
        """The question in the Decisions API wire format."""
        q: dict[str, Any] = {"type": self.type, "instructions": self.instructions}
        if self.type == "score" and self.criteria:
            q["criteria"] = list(self.criteria)
        if self.type == "choice":
            q["criteria"] = dict(self.options)
        return q


@dataclass(frozen=True)
class Condition:
    question: str
    kind: str  # "equals" (choice) or "compare" (score/noul)
    value: Any
    op: str = "=="

    def holds(self, answers: dict[str, "Answer"]) -> bool:
        a = answers[self.question]
        if self.kind == "equals":
            return a.value == self.value
        return _OPS[self.op](float(a.value), float(self.value))

    def describe(self) -> str:
        if self.kind == "equals":
            return f"{self.question}={self.value}"
        return f"{self.question}{self.op}{self.value}"


@dataclass(frozen=True)
class Rule:
    conditions: tuple[Condition, ...]
    max: int | None = None
    min: int | None = None
    add: int = 0
    flag: str | None = None

    def holds(self, answers: dict[str, "Answer"]) -> bool:
        return all(c.holds(answers) for c in self.conditions)


@dataclass(frozen=True)
class Fold:
    base_sum: tuple[str, ...]
    table: tuple[int, ...]
    adjust: tuple[Rule, ...] = ()
    clamp: tuple[Rule, ...] = ()
    confidence_min: float | None = None
    confidence_flag: str = "low-confidence"


@dataclass(frozen=True)
class Transport:
    kind: str
    model: str = ""
    options: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DecisionConfig:
    questions: dict[str, Question]
    fold: Fold
    transport: Transport
    surface_cutoff: int
    state_fields: tuple[str, ...]
    include_criteria: bool
    raw: dict = field(default_factory=dict, compare=False)

    @property
    def qhash(self) -> str:
        """Version id: hash of the questions, fold, state shape and model.

        The transport kind is left out on purpose: the same questions on a
        replayed run share a version with the live run they replay.
        """
        payload = {"questions": self.raw.get("questions"), "fold": self.raw.get("fold"),
                   "state": {"fields": list(self.state_fields),
                             "include_criteria": self.include_criteria},
                   "model": self.transport.model}
        blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:12]

    def wire_questions(self) -> dict:
        return {name: q.wire() for name, q in self.questions.items()}


def _err(msg: str) -> DecisionConfigError:
    return DecisionConfigError(f"decision.yaml: {msg}")


def _parse_question(name: str, spec: Any) -> Question:
    if not isinstance(spec, dict):
        raise _err(f"question {name!r} must be a mapping")
    qtype = spec.get("type")
    if qtype not in QUESTION_TYPES:
        raise _err(f"question {name!r} has type {qtype!r}; expected one of {QUESTION_TYPES}")
    instructions = spec.get("instructions")
    if not instructions or not isinstance(instructions, (str, dict)):
        raise _err(f"question {name!r} needs instructions")
    if isinstance(instructions, dict):
        instructions = json.dumps(instructions, sort_keys=True)
    if qtype == "score":
        criteria = spec.get("criteria") or []
        levels = spec.get("levels") or len(criteria)
        if not isinstance(levels, int) or levels < 2:
            raise _err(f"score question {name!r} needs levels >= 2 (or one criteria entry per level)")
        if criteria and len(criteria) != levels:
            raise _err(f"score question {name!r}: {len(criteria)} criteria entries for {levels} levels")
        return Question(name, qtype, instructions, levels=levels, criteria=list(criteria))
    if qtype == "choice":
        options = spec.get("options") or spec.get("criteria")
        if isinstance(options, list):
            options = {str(o): "" for o in options}
        if not isinstance(options, dict) or len(options) < 2:
            raise _err(f"choice question {name!r} needs at least two options")
        return Question(name, qtype, instructions, options={str(k): str(v) for k, v in options.items()})
    return Question(name, qtype, instructions)


def _parse_condition(questions: dict[str, Question], key: str, value: Any, where: str) -> Condition:
    if key not in questions:
        raise _err(f"{where} references unknown question {key!r}")
    q = questions[key]
    if q.type == "choice":
        if str(value) not in q.options:
            raise _err(f"{where}: {value!r} is not an option of choice question {key!r} "
                       f"(options: {sorted(q.options)})")
        return Condition(key, "equals", str(value))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        op, num = "==", float(value)
    else:
        m = _COMPARE.match(str(value))
        if not m:
            raise _err(f"{where}: condition on {q.type} question {key!r} must look like '>=0.5'")
        op, num = m.group(1), float(m.group(2))
    if q.type == "noul" and not 0.0 <= num <= 1.0:
        raise _err(f"{where}: noul threshold for {key!r} must be within 0..1")
    if q.type == "score" and not 0 <= num <= q.levels - 1:
        raise _err(f"{where}: score threshold for {key!r} must be within 0..{q.levels - 1}")
    return Condition(key, "compare", num, op)


def _parse_rule(questions: dict[str, Question], spec: Any, where: str, *, kind: str) -> Rule:
    if not isinstance(spec, dict) or not isinstance(spec.get("if"), dict) or not spec["if"]:
        raise _err(f"{where} needs an 'if' mapping of question: condition")
    conds = tuple(_parse_condition(questions, k, v, where) for k, v in spec["if"].items())
    if kind == "clamp":
        if spec.get("max") is None and spec.get("min") is None:
            raise _err(f"{where} needs max or min")
        return Rule(conds, max=_as_int(spec.get("max"), where), min=_as_int(spec.get("min"), where),
                    flag=spec.get("flag"))
    if "add" not in spec:
        raise _err(f"{where} needs add")
    return Rule(conds, add=_as_int(spec["add"], where) or 0, flag=spec.get("flag"))


def _as_int(v: Any, where: str) -> int | None:
    if v is None:
        return None
    if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 10:
        raise _err(f"{where}: bounds must be integers 1..10, got {v!r}")
    return v


def parse_config(data: Any) -> DecisionConfig:
    """Validate a decision config mapping. Raises DecisionConfigError."""
    if not isinstance(data, dict):
        raise _err("top level must be a mapping")
    qspec = data.get("questions")
    if not isinstance(qspec, dict) or not qspec:
        raise _err("needs a non-empty 'questions' mapping")
    questions = {str(n): _parse_question(str(n), s) for n, s in qspec.items()}

    fspec = data.get("fold")
    if not isinstance(fspec, dict) or not isinstance(fspec.get("base"), dict):
        raise _err("needs fold.base")
    base = fspec["base"]
    names = base.get("sum")
    if isinstance(names, str):
        names = [names]
    if not names:
        raise _err("fold.base.sum must list at least one score question")
    for n in names:
        if n not in questions:
            raise _err(f"fold.base.sum references unknown question {n!r}")
        if questions[n].type != "score":
            raise _err(f"fold.base.sum: {n!r} is a {questions[n].type} question, not score")
    max_sum = sum(questions[n].levels - 1 for n in names)
    table = base.get("table")
    if not isinstance(table, list) or len(table) != max_sum + 1:
        raise _err(f"fold.base.table needs exactly {max_sum + 1} entries "
                   f"(one per possible sum 0..{max_sum}), got {table!r}")
    for t in table:
        _as_int(t, "fold.base.table")

    adjust = tuple(_parse_rule(questions, r, f"fold.adjust[{i}]", kind="adjust")
                   for i, r in enumerate(fspec.get("adjust") or []))
    clamp = tuple(_parse_rule(questions, r, f"fold.clamp[{i}]", kind="clamp")
                  for i, r in enumerate(fspec.get("clamp") or []))
    conf = fspec.get("confidence") or {}
    conf_min = conf.get("min")
    if conf_min is not None and not (isinstance(conf_min, (int, float)) and 0 <= conf_min <= 1):
        raise _err("fold.confidence.min must be within 0..1")
    fold = Fold(tuple(names), tuple(table), adjust, clamp,
                None if conf_min is None else float(conf_min),
                str(conf.get("flag") or "low-confidence"))

    tspec = data.get("transport") or {"kind": "mock"}
    if not isinstance(tspec, dict) or not tspec.get("kind"):
        raise _err("transport needs a kind")
    from career_radar.core.transports import TRANSPORT_KINDS
    if tspec["kind"] not in TRANSPORT_KINDS:
        raise _err(f"transport.kind {tspec['kind']!r} not one of {sorted(TRANSPORT_KINDS)}")
    if tspec["kind"] in ("openrouter-decisions", "llm-structured") and not tspec.get("model"):
        raise _err(f"transport {tspec['kind']} needs a model")
    transport = Transport(tspec["kind"], str(tspec.get("model") or ""),
                          {k: v for k, v in tspec.items() if k not in ("kind", "model")})

    cutoff = data.get("surface_cutoff", 7)
    _as_int(cutoff, "surface_cutoff")

    sspec = data.get("state") or {}
    fields = sspec.get("fields") or DEFAULT_STATE_FIELDS
    bad = [f for f in fields if f not in ALLOWED_STATE_FIELDS]
    if bad:
        raise _err(f"state.fields has unknown fields {bad}; allowed: {sorted(ALLOWED_STATE_FIELDS)}")
    return DecisionConfig(questions, fold, transport, cutoff, tuple(fields),
                          bool(sspec.get("include_criteria", True)), raw=data)


def load_config(path: str | Path) -> DecisionConfig:
    p = Path(path)
    if not p.exists():
        raise DecisionConfigError(f"{p} not found (scorer: decision needs a decision.yaml; "
                                  "see examples/decision.backend-engineer.yaml)")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise DecisionConfigError(f"{p}: invalid YAML: {e}") from e
    return parse_config(data)


# --- Answers ------------------------------------------------------------------


@dataclass
class Answer:
    """One typed answer. ``value`` is an int level, an option string, or a probability."""

    name: str
    type: str
    value: Any
    confidence: float | None = None
    probabilities: dict | None = None
    calibrated: bool = True

    def to_dict(self) -> dict:
        return {"type": self.type, "value": self.value, "confidence": self.confidence,
                "probabilities": self.probabilities, "calibrated": self.calibrated}


def coerce_answer(q: Question, raw: Any, *, calibrated: bool = True) -> Answer:
    """Validate one raw answer (Decisions API shape or our own) against its question."""
    if not isinstance(raw, dict):
        raise AnswerError(f"{q.name}: expected an object, got {type(raw).__name__}")
    conf = raw.get("confidence")
    conf = None if conf is None else float(conf)
    probs = raw.get("probabilities")
    cal = bool(raw.get("calibrated", calibrated))
    if q.type == "score":
        v = raw.get("score", raw.get("value"))
        try:
            f = float(v)
        except (TypeError, ValueError) as e:
            raise AnswerError(f"{q.name}: score {v!r} is not numeric") from e
        if f != f or not 0 <= f <= q.levels - 1:
            raise AnswerError(f"{q.name}: score {v!r} outside 0..{q.levels - 1}")
        return Answer(q.name, "score", int(round(f)), conf, probs, cal)
    if q.type == "choice":
        v = raw.get("choice", raw.get("value"))
        if str(v) not in q.options:
            raise AnswerError(f"{q.name}: choice {v!r} not in {sorted(q.options)}")
        return Answer(q.name, "choice", str(v), conf, probs, cal)
    v = raw.get("noul", raw.get("value"))
    try:
        f = float(v)
    except (TypeError, ValueError) as e:
        raise AnswerError(f"{q.name}: noul {v!r} is not numeric") from e
    if f != f or not 0.0 <= f <= 1.0:
        raise AnswerError(f"{q.name}: noul {v!r} outside 0..1")
    return Answer(q.name, "noul", f, conf, probs, cal)


def coerce_answers(cfg: DecisionConfig, raw: dict, *, calibrated: bool = True) -> dict[str, Answer]:
    if not isinstance(raw, dict):
        raise AnswerError("answers must be an object keyed by question name")
    missing = [n for n in cfg.questions if n not in raw]
    if missing:
        raise AnswerError(f"answers missing questions {missing}")
    return {n: coerce_answer(q, raw[n], calibrated=calibrated) for n, q in cfg.questions.items()}


# --- Fold ---------------------------------------------------------------------


@dataclass
class FoldResult:
    score: int
    flags: list[str]
    rationale: str
    confidence: float | None
    trace: list[str]


def fold(cfg: DecisionConfig, answers: dict[str, Answer]) -> FoldResult:
    """Combine typed answers into a 1-10 score. Pure and deterministic."""
    f = cfg.fold
    total = sum(int(answers[n].value) for n in f.base_sum)
    score = f.table[total]
    trace = [f"base: sum({'+'.join(f.base_sum)})={total} -> {score}"]
    flags: list[str] = []
    for r in f.adjust:
        if r.holds(answers):
            new = max(1, min(10, score + r.add))
            trace.append(f"adjust {' & '.join(c.describe() for c in r.conditions)}: "
                         f"{score} -> {new}")
            score = new
            if r.flag:
                flags.append(r.flag)
    for r in f.clamp:
        if r.holds(answers):
            new = score
            if r.max is not None:
                new = min(new, r.max)
            if r.min is not None:
                new = max(new, r.min)
            trace.append(f"clamp {' & '.join(c.describe() for c in r.conditions)}: "
                         f"{score} -> {new}")
            score = new
            if r.flag:
                flags.append(r.flag)
    confs = [a.confidence for a in answers.values() if a.confidence is not None]
    mean_conf = sum(confs) / len(confs) if confs else None
    if f.confidence_min is not None and mean_conf is not None and mean_conf < f.confidence_min:
        flags.append(f.confidence_flag)
    if any(not a.calibrated for a in answers.values()):
        flags.append("uncalibrated")
    summary = ", ".join(f"{n}={a.value if a.type != 'noul' else round(a.value, 2)}"
                        for n, a in answers.items())
    conf_txt = f", conf {mean_conf:.2f}" if mean_conf is not None else ""
    rationale = f"{summary}{conf_txt} -> {score}/10 ({'; '.join(trace[1:]) or 'no clamps'})"
    return FoldResult(score, sorted(set(flags)), rationale[:300], mean_conf, trace)


# --- State --------------------------------------------------------------------


def build_state(cfg: DecisionConfig, posting: dict, criteria: str = "") -> dict:
    """The material handed to the decision model: named posting fields (+ rubric)."""
    post: dict[str, Any] = {}
    for name in cfg.state_fields:
        if name == "salary":
            lo, hi = posting.get("salary_min"), posting.get("salary_max")
            if lo is None:
                post["salary"] = posting.get("salary") or "not stated"
            else:
                post["salary"] = f"${lo:,}" + (f" - ${hi:,}" if hi is not None else "")
        elif name == "description":
            post["description"] = (posting.get("description") or "")[:DESCRIPTION_CHARS]
        elif name == "remote_type":
            post["remote_type"] = posting.get("remote_type") or "unspecified"
        else:
            post[name] = posting.get(name) or ""
    state: dict[str, Any] = {"posting": post}
    if cfg.include_criteria and criteria:
        state["candidate_criteria"] = criteria[:CRITERIA_CHARS]
    return state


def state_key(state: dict) -> str:
    """Stable key for a state, used by the replay transport and the spend ledger."""
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode("utf-8")).hexdigest()[:16]


# --- Scoring ------------------------------------------------------------------


@dataclass
class Decision:
    score: int
    surfaced: bool
    flags: list[str]
    rationale: str
    confidence: float | None
    answers: dict[str, Answer]
    qhash: str
    cost_usd: float = 0.0
    latency_ms: float = 0.0


def decide(cfg: DecisionConfig, transport, posting: dict, criteria: str = "") -> Decision:
    """Ask the questions about one posting and fold the answers."""
    state = build_state(cfg, posting, criteria)
    result = transport.decide(state, cfg)
    answers = result.answers
    folded = fold(cfg, answers)
    return Decision(folded.score, folded.score >= cfg.surface_cutoff, folded.flags,
                    folded.rationale, folded.confidence, answers, cfg.qhash,
                    result.cost_usd, result.latency_ms)


def score_unscored(conn, config_dir: Path | str, criteria_path: Path | str | None = None,
                   transport=None) -> tuple[int, int]:
    """Score every unscored posting with the decision config in ``config_dir``.

    The config is loaded (and validated) before anything else, so a broken
    decision.yaml raises DecisionConfigError without a single paid call.
    """
    from datetime import date

    from career_radar.core import dedupe, spend
    from career_radar.core.transports import make_transport

    cfg = load_config(Path(config_dir) / DECISION_FILENAME)
    crit_path = Path(criteria_path) if criteria_path else Path(config_dir) / "criteria.md"
    criteria = crit_path.read_text(encoding="utf-8") if crit_path.exists() else ""
    tr = transport or make_transport(cfg.transport, ledger=spend.Ledger(conn))
    rows = dedupe.unscored(conn)
    if not rows:
        logger.info("Nothing to score.")
        return 0, 0
    logger.info("Decision scorer %s (%s, qhash %s): %d postings",
                cfg.transport.kind, cfg.transport.model or "-", cfg.qhash, len(rows))
    today = date.today().isoformat()
    scored = failed = 0
    for row in rows:
        posting = dict(row)
        try:
            d = decide(cfg, tr, posting, criteria)
        except Exception as e:  # one bad row must not stop the run
            logger.warning("decision scoring failed for %s: %s", posting.get("req_id"), e)
            failed += 1
            continue
        dedupe.record_score(conn, posting["source"], posting["req_id"], d.score, d.rationale,
                            json.dumps(d.flags), today)
        conn.execute(
            "UPDATE postings SET scorer_version = ?, answers = ? WHERE source = ? AND req_id = ?",
            (f"decision:{d.qhash}",
             json.dumps({n: a.to_dict() for n, a in d.answers.items()}),
             posting["source"], posting["req_id"]))
        conn.commit()
        scored += 1
    logger.info("Decision scorer: %d scored, %d failed", scored, failed)
    return scored, failed
