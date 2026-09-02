"""Universal LLM fetcher for arbitrary employer career pages.

Fetches raw HTML, strips markup, and prompts the configured LLM to extract
standard job posting dictionaries as a JSON array.
"""

import json
import logging
import re
from pathlib import Path

import litellm
import requests

from career_radar.config import load_model
from career_radar.core.score import extract_json_array

logger = logging.getLogger(__name__)


def fetch_listings(url: str, employer_name: str, config_dir: Path | str | None = None) -> list[dict]:
    """Fetch an HTML page, strip markup, and use LLM to extract job listings as JSON."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        logger.error("universal_llm: Failed to fetch %s: %s", url, e)
        return []

    # Strip junk to save tokens
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "svg", "img", "iframe"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\n+", "\n", text)
    except ImportError:
        text = resp.text[:10000]

    prompt = (
        f"You are an automated job scraper. I am providing you the raw text of a career page for {employer_name}.\n"
        "Extract all job postings and return EXACTLY a JSON array of objects. Do not include markdown formatting or any other text.\n"
        "Each object must have:\n"
        '- "req_id": a string (use a unique ID or hash the title if no ID is present)\n'
        '- "title": the job title\n'
        '- "url": the full absolute URL to apply or view details\n'
        '- "location": the location\n'
        '- "salary_min": integer (if stated, else null)\n'
        '- "salary_max": integer (if stated, else null)\n'
        '- "description": A brief summary or the full description if available\n\n'
        f"URL Context for resolving relative links: {url}\n\n"
        f"PAGE TEXT:\n{text[:30000]}"
    )

    try:
        model_name = load_model(config_dir=config_dir)
        try:
            response = litellm.completion(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
        except Exception:
            response = litellm.completion(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
            )
        out = response.choices[0].message.content or ""

        try:
            data = json.loads(out)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                for v in data.values():
                    if isinstance(v, list):
                        return v
        except Exception:
            pass
        return extract_json_array(out)
    except Exception as e:
        logger.error("universal_llm: Extraction failed for %s: %s", employer_name, e)
        return []


def fetch_detail(url: str) -> str:
    """Universal fetcher extracts details from the listing page."""
    return ""
