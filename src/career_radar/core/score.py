"""Score unscored postings against config/criteria.md with an LLM."""

import concurrent.futures
import json
import logging
import sqlite3
from datetime import date
from pathlib import Path

import litellm

from career_radar.core import dedupe

logger = logging.getLogger(__name__)

BATCH_SIZE = 8
DESCRIPTION_CHARS = 3500
CALIBRATION_FILENAME = "calibration.jsonl"
CALIBRATION_REASON_CHARS = 120

class ScoringError(Exception):
    """A scoring backend failed or returned unusable output."""

SCORING_GUIDE = (
    "Evaluate each job posting against the provided criteria and return a score from 1 to 10.\n"
    "Before scoring, write a brief step-by-step 'thinking' trace evaluating the title, "
    "seniority, organization, and daily work against the candidate's preferences.\n"
    "Then provide a final 'score' (integer 1-10) and a one-sentence 'rationale'.\n"
    "Score high (7-10) only if the candidate would plausibly apply. "
    "Score low (1-3) for bad organizational fit, wrong seniority, or sales/fundraising roles.\n"
)

def load_calibration(path: str | Path) -> list[dict]:
    file = Path(path)
    if not file.exists():
        return []
    examples: list[dict] = []
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            examples.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return examples

def _calibration_digest(examples: list[dict]) -> str:
    lines: list[str] = []
    for ex in examples:
        verdict = "YES" if ex.get("verdict") == "interested" else "NO"
        reason = str(ex.get("reason") or "").strip()[:CALIBRATION_REASON_CHARS]
        salary = str(ex.get("salary") or "").strip()
        head = f"{verdict} [{reason}]" if reason else verdict
        tail = f" ({salary})" if salary and salary != "not stated" else ""
        lines.append(f"{head}: {ex.get('title', '?')} @ {ex.get('employer', '?')}{tail}")
    return "\n".join(lines)

def _prompt(criteria: str, postings: list[dict], calibration: list[dict] | None = None) -> str:
    digest = _calibration_digest(calibration or [])
    calibration_block = (
        "Real past verdicts from the candidate — match this revealed "
        f"preference:\n{digest}\n\n" if digest else ""
    )
    return (
        "You are scoring job postings for fit against one specific candidate. "
        "Read the criteria, then rate each posting.\n\n"
        f"<criteria>\n{criteria}\n</criteria>\n\n"
        f"{SCORING_GUIDE}\n"
        f"{calibration_block}"
        "Return ONLY a JSON array, one object per posting, no prose:\n"
        '[{"req_id": "...", "thinking": "brief reasoning", "score": 1, '
        '"rationale": "one sentence", "flags": []}]\n'
        'Allowed flags: "no-salary", "stretch".\n\n'
        f"Postings:\n{json.dumps(postings, indent=1)}"
    )

def _posting_payload(row: sqlite3.Row) -> dict:
    salary = (
        f"${row['salary_min']:,} - ${row['salary_max']:,}"
        if row["salary_min"] is not None
        else "not stated"
    )
    return {
        "req_id": row["req_id"],
        "title": row["title"],
        "employer": row["employer"],
        "location": row["location"],
        "remote_type": row["remote_type"] or "unspecified",
        "salary": salary,
        "posted_date": row["posted_date"],
        "description": (row["description"] or "")[:DESCRIPTION_CHARS],
    }

def extract_json_array(raw: str) -> list:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        if "results" in parsed and isinstance(parsed["results"], list):
            return parsed["results"]
        for v in parsed.values():
            if isinstance(v, list):
                return v
        return [parsed]

    decoder = json.JSONDecoder()
    idx = raw.find("[")
    while idx != -1:
        try:
            value, _ = decoder.raw_decode(raw, idx)
            if isinstance(value, list):
                return value
        except json.JSONDecodeError:
            pass
        idx = raw.find("[", idx + 1)

    # Also try decoding a single JSON object or wrapped dict
    idx = raw.find("{")
    while idx != -1:
        try:
            value, _ = decoder.raw_decode(raw, idx)
            if isinstance(value, dict):
                if "results" in value and isinstance(value["results"], list):
                    return value["results"]
                return [value]
        except json.JSONDecodeError:
            pass
        idx = raw.find("{", idx + 1)

    raise ScoringError(f"no JSON array in output: {raw[:200]!r}")

def _validate_item(item: dict) -> tuple[str, int, str, str]:
    try:
        req_id = str(item["req_id"])
    except (KeyError, TypeError) as e:
        raise ScoringError(f"item missing req_id: {item!r}") from e
    try:
        score = int(item["score"])
    except (KeyError, TypeError, ValueError) as e:
        raise ScoringError(f"missing/invalid score for {req_id}: {item.get('score')}") from e
    if not 1 <= score <= 10:
        raise ScoringError(f"score out of range for {req_id}: {score}")
    rationale = str(item.get("rationale", ""))[:300]
    model_flags = item.get("flags") or []
    merged = list(set([str(f) for f in model_flags]))
    return req_id, score, rationale, json.dumps(merged)

def _run_llm(prompt: str, config_dir: Path | str | None = None) -> str:
    """Run the scoring prompt through litellm."""
    from career_radar.config import load_model
    model_name = load_model(config_dir=config_dir)
    try:
        try:
            response = litellm.completion(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
        except Exception as e:
            err_str = str(e).lower()
            if "response_format" in err_str or "unsupported" in err_str or "json_object" in err_str:
                response = litellm.completion(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                )
            else:
                raise
        return response.choices[0].message.content
    except Exception as e:
        err_name = type(e).__name__
        err_str = str(e).lower()
        if "auth" in err_name.lower() or "apikey" in err_str or "api_key" in err_str or "key not found" in err_str:
            raise ScoringError("API_KEY_MISSING") from e
        raise ScoringError(f"LLM call failed ({err_name}): {e}") from e

def _score_batch(
    criteria: str,
    batch: list[sqlite3.Row],
    calibration: list[dict] | None = None,
    config_dir: Path | str | None = None,
) -> dict[str, tuple[int, str, str]]:
    payloads = [_posting_payload(r) for r in batch]
    prompt = _prompt(criteria, payloads, calibration)
    # litellm json_object response format requires the string "JSON" in the prompt
    prompt += "\nOutput must be a JSON object with a 'results' key containing the array."
    raw = _run_llm(prompt, config_dir=config_dir)

    # Try to extract from {"results": [...]}
    try:
        data = json.loads(raw)
        if "results" in data:
            items = data["results"]
        else:
            items = extract_json_array(raw)
    except Exception as e:
        if str(e) == "API_KEY_MISSING":
            raise
        items = extract_json_array(raw)

    results = {}
    for item in items:
        try:
            req_id, score, rationale, flags = _validate_item(item)
            results[req_id] = (score, rationale, flags)
        except Exception as e:
            logger.warning("Discarding malformed item %r: %s", item, e)
    return results

def score_unscored(
    conn: sqlite3.Connection,
    criteria_path: str | Path,
    config_dir: Path | str | None = None,
) -> tuple[int, int]:
    """Score every unscored posting in parallel. Returns (scored, failed) counts."""
    criteria = Path(criteria_path).read_text(encoding="utf-8")
    calibration = load_calibration(Path(criteria_path).parent / CALIBRATION_FILENAME)
    rows = dedupe.unscored(conn)
    if not rows:
        logger.info("Nothing to score.")
        return 0, 0
    logger.info("Scoring %d postings in batches of %d (%d calibration examples)",
                len(rows), BATCH_SIZE, len(calibration))
    today = date.today().isoformat()
    scored = failed = 0

    chunks = [rows[i:i + BATCH_SIZE] for i in range(0, len(rows), BATCH_SIZE)]

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        future_to_chunk = {
            executor.submit(_score_batch, criteria, chunk, calibration, config_dir): chunk
            for chunk in chunks
        }

        for future in concurrent.futures.as_completed(future_to_chunk):
            chunk = future_to_chunk[future]
            try:
                results = future.result()
            except Exception as e:
                if "API_KEY_MISSING" in str(e):
                    logger.error("🛑 Error: No API key found. Career Radar requires an LLM to score jobs.")
                    logger.error("Please set your key by running `export OPENAI_API_KEY='your-key'`")
                    logger.error("or adding it to a .env file. See the README for details.")
                    break
                logger.warning("Batch failed (%s: %s); falling back per-posting", type(e).__name__, e)
                results = {}

            for row in chunk:
                key = row["req_id"]
                if key not in results:
                    # For now, skip the fallback logic since we use a robust LiteLLM API call
                    failed += 1
                    continue
                score, rationale, flags = results[key]
                dedupe.record_score(conn, row["source"], key, score, rationale, flags, today)
                scored += 1

            conn.commit()
            logger.info("Progress: %d/%d scored", scored, len(rows))

    return scored, failed

