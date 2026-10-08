"""Transports: the only provider seam for decision scoring.

``Transport.decide(state, cfg) -> DecideResult`` asks every question in the
config about one state and returns typed, validated answers. Swapping
providers means swapping the transport; the questions, the fold and the
cutoff stay the same.

    openrouter-decisions  any decision model on OpenRouter's Decisions API
                          (POST /api/alpha/decisions; Score/Choice/Noul answers)
    llm-structured        any LiteLLM chat model, answering through a JSON
                          schema; answers are marked calibrated: false
    mock                  deterministic word-overlap heuristic; no network
    replay                answers recorded earlier (JSONL), keyed by state;
                          zero-cost sweeps over a frozen run
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from career_radar.core.decision import (
    AnswerError,
    DecisionConfig,
    coerce_answers,
    state_key,
)

OPENROUTER_DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
TRANSPORT_KINDS = ("openrouter-decisions", "llm-structured", "mock", "replay")


class TransportError(RuntimeError):
    """The provider call failed or returned something unusable."""


@dataclass
class DecideResult:
    answers: dict
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    raw: Any = None


class BaseTransport:
    kind = "base"

    def __init__(self, model: str = "", ledger=None, **options: Any):
        self.model = model
        self.ledger = ledger
        self.options = options

    def decide(self, state: dict, cfg: DecisionConfig) -> DecideResult:  # pragma: no cover
        raise NotImplementedError


class OpenRouterDecisions(BaseTransport):
    """OpenRouter Decisions API. Needs OPENROUTER_API_KEY."""

    kind = "openrouter-decisions"

    def decide(self, state: dict, cfg: DecisionConfig) -> DecideResult:
        import requests

        key = os.environ.get(self.options.get("api_key_env", "OPENROUTER_API_KEY"), "")
        if not key:
            raise TransportError("openrouter-decisions needs OPENROUTER_API_KEY")
        url = self.options.get("url", OPENROUTER_DECISIONS_URL)
        t0 = time.time()
        try:
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                         "X-Title": "career-radar"},
                json={"model": self.model, "state": state, "questions": cfg.wire_questions()},
                timeout=float(self.options.get("timeout", 60)),
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            raise TransportError(f"Decisions API call failed: {e}") from e
        latency = (time.time() - t0) * 1000
        usage = data.get("usage") or {}
        cost = float(usage.get("cost") or 0.0)
        if self.ledger is not None:
            self.ledger.record(provider="openrouter", model=data.get("model") or self.model,
                               key=state_key(state),
                               input_tokens=usage.get("input_tokens", 0),
                               output_tokens=usage.get("output_tokens", 0),
                               cost_usd=cost, latency_ms=latency,
                               generation_id=data.get("id"))
        if "answers" not in data:
            raise TransportError(f"Decisions API returned no answers: {str(data)[:200]}")
        return DecideResult(coerce_answers(cfg, data["answers"]), cost, latency, data)


def _schema_for(cfg: DecisionConfig) -> dict:
    props: dict[str, Any] = {}
    for name, q in cfg.questions.items():
        if q.type == "score":
            v = {"type": "integer", "minimum": 0, "maximum": q.levels - 1}
        elif q.type == "choice":
            v = {"type": "string", "enum": list(q.options)}
        else:
            v = {"type": "number", "minimum": 0, "maximum": 1}
        props[name] = {"type": "object", "additionalProperties": False,
                       "required": ["value", "confidence"],
                       "properties": {"value": v, "confidence": {"type": "number"}}}
    return {"type": "object", "additionalProperties": False,
            "required": list(props), "properties": props}


def structured_prompt(state: dict, cfg: DecisionConfig) -> str:
    lines = ["Answer each question about the job posting below. Return JSON only, "
             "matching the schema: for every question an object with 'value' and "
             "'confidence' (0-1, how sure you are).", ""]
    for name, q in cfg.questions.items():
        if q.type == "score":
            lines.append(f"- {name} (integer 0..{q.levels - 1}): {q.instructions}")
            for i, c in enumerate(q.criteria):
                lines.append(f"    {i}: {json.dumps(c) if not isinstance(c, str) else c}")
        elif q.type == "choice":
            lines.append(f"- {name} (one of {list(q.options)}): {q.instructions}")
            for opt, desc in q.options.items():
                lines.append(f"    {opt}: {desc}")
        else:
            lines.append(f"- {name} (probability 0..1 that the answer is yes): {q.instructions}")
    lines += ["", "State:", json.dumps(state, indent=1)]
    return "\n".join(lines)


class LLMStructured(BaseTransport):
    """Any LiteLLM chat model, answering through a JSON schema.

    Chat models give no calibrated probabilities here, so every answer is
    marked ``calibrated: false`` (the fold adds an ``uncalibrated`` flag).
    """

    kind = "llm-structured"

    def decide(self, state: dict, cfg: DecisionConfig) -> DecideResult:
        import litellm

        prompt = structured_prompt(state, cfg)
        t0 = time.time()
        try:
            resp = litellm.completion(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_schema",
                                 "json_schema": {"name": "decisions", "strict": True,
                                                 "schema": _schema_for(cfg)}},
            )
            content = resp.choices[0].message.content
        except Exception as e:
            raise TransportError(f"llm-structured call failed: {e}") from e
        latency = (time.time() - t0) * 1000
        try:
            data = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            m = re.search(r"\{.*\}", content or "", re.S)
            if not m:
                raise TransportError(f"llm-structured returned no JSON: {str(content)[:200]}")
            data = json.loads(m.group(0))
        usage = getattr(resp, "usage", None)
        cost = 0.0
        try:
            cost = float(litellm.completion_cost(completion_response=resp) or 0.0)
        except Exception:
            pass
        if self.ledger is not None:
            self.ledger.record(provider="litellm", model=self.model, key=state_key(state),
                               input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                               output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                               cost_usd=cost, latency_ms=latency)
        return DecideResult(coerce_answers(cfg, data, calibrated=False), cost, latency, data)


_WORD = re.compile(r"[a-z][a-z+#.-]{2,}")
_STOP = set("the and for with that this are you your will from have has not but our who "
            "all any can its into their they them was were what when which job role work "
            "about does more than such also only does".split())


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


def _text_of(x: Any) -> str:
    return x if isinstance(x, str) else json.dumps(x)


class Mock(BaseTransport):
    """Deterministic, offline stand-in for a decision model.

    Picks the score level / choice option whose criteria text shares the most
    words with the posting, and answers noul questions by word overlap with
    the instructions. Crude, but stable, free, and good enough to exercise the
    whole pipeline in tests and demos.
    """

    kind = "mock"

    def decide(self, state: dict, cfg: DecisionConfig) -> DecideResult:
        post = state.get("posting", state)
        words = _words(" ".join(str(v) for v in post.values()))
        title_words = _words(str(post.get("title", "")))
        out: dict[str, dict] = {}
        for name, q in cfg.questions.items():
            if q.type == "score":
                best, conf = 0, 0.5
                if q.criteria:
                    overlaps = [len(words & _words(_text_of(c)))
                                + 2 * len(title_words & _words(_text_of(c)))
                                for c in q.criteria]
                    best = max(range(len(overlaps)), key=lambda i: (overlaps[i], i))
                    if overlaps[best] == 0:
                        best = 0
                    conf = 0.9 if overlaps[best] >= 3 else 0.5
                out[name] = {"score": best, "confidence": conf}
            elif q.type == "choice":
                opts = list(q.options)
                scores = [len(words & _words(f"{o} {q.options[o]}")) for o in opts]
                best = max(range(len(opts)), key=lambda i: (scores[i], -i))
                out[name] = {"choice": opts[best], "confidence": 0.7}
            else:
                key_terms = _words(q.instructions) - _STOP
                hits = len(words & key_terms)
                out[name] = {"noul": min(1.0, hits / 6.0), "confidence": 0.6}
        return DecideResult(coerce_answers(cfg, out), 0.0, 0.0, out)


class Replay(BaseTransport):
    """Answers recorded earlier, looked up by state key. Never calls anything.

    ``path`` points at a JSONL file of ``{"state_key": ..., "answers": {...}}``
    rows (``career-radar eval run`` writes these). A miss is an error, not a
    silent default.
    """

    kind = "replay"

    def __init__(self, model: str = "", ledger=None, **options: Any):
        super().__init__(model, ledger, **options)
        path = options.get("path")
        if not path:
            raise TransportError("replay transport needs a path")
        self.path = Path(path).expanduser()
        self._answers: dict[str, dict] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    if "state_key" in row and "answers" in row:
                        self._answers[row["state_key"]] = row["answers"]

    def decide(self, state: dict, cfg: DecisionConfig) -> DecideResult:
        k = state_key(state)
        if k not in self._answers:
            raise TransportError(f"replay: no recorded answers for state {k} in {self.path}")
        raw = {n: _to_wire(a) for n, a in self._answers[k].items()}
        try:
            return DecideResult(coerce_answers(cfg, raw), 0.0, 0.0, raw)
        except AnswerError as e:
            raise TransportError(f"replay: recorded answers do not fit this config: {e}") from e


def _to_wire(a: dict) -> dict:
    """Recorded Answer.to_dict() rows back into raw answer shape."""
    if "value" not in a:
        return a
    key = {"score": "score", "choice": "choice", "noul": "noul"}.get(a.get("type"), "value")
    return {key: a["value"], "confidence": a.get("confidence"),
            "probabilities": a.get("probabilities"), "calibrated": a.get("calibrated", True)}


_KINDS = {c.kind: c for c in (OpenRouterDecisions, LLMStructured, Mock, Replay)}


def make_transport(spec, ledger=None, **overrides: Any) -> BaseTransport:
    """Build a transport from a config ``Transport`` spec (or kind string)."""
    kind = spec if isinstance(spec, str) else spec.kind
    model = "" if isinstance(spec, str) else spec.model
    options = {} if isinstance(spec, str) else dict(spec.options)
    options.update(overrides)
    if kind not in _KINDS:
        raise TransportError(f"unknown transport kind {kind!r}")
    return _KINDS[kind](model=model, ledger=ledger, **options)
