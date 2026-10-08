"""Configuration loaders for Career Radar.

Loads employers, filtering rules, scoring criteria, and model preferences
from the active configuration directory.
"""

import os
from pathlib import Path

import yaml

CONFIG_DIR = Path(
    os.environ.get("CAREER_RADAR_CONFIG_DIR", Path.home() / ".config" / "career-radar")
)

DEFAULT_OUTPUT_DIR = Path.home() / ".local" / "share" / "career-radar" / "output"


def get_output_dir(custom_dir: Path | str | None = None) -> Path:
    """Return the report output directory.

    Precedence: explicit argument (``--output-dir``), then the
    ``CAREER_RADAR_OUTPUT_DIR`` environment variable, then
    ``~/.local/share/career-radar/output``. The env var is read at call time
    so it works however the process was launched (cron, systemd, shell).
    """
    if custom_dir:
        return Path(custom_dir)
    env = os.environ.get("CAREER_RADAR_OUTPUT_DIR")
    return Path(env) if env else DEFAULT_OUTPUT_DIR


def get_config_dir(custom_dir: Path | str | None = None) -> Path:
    """Return the resolved configuration directory."""
    if custom_dir:
        return Path(custom_dir)
    return CONFIG_DIR

def load_yaml(filename: str, config_dir: Path | str | None = None) -> dict | list:
    """Load a YAML file from the config directory."""
    cfg = get_config_dir(config_dir)
    path = cfg / filename
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def load_employers(config_dir: Path | str | None = None) -> list[dict]:
    """Load the employers configuration."""
    data = load_yaml("employers.yaml", config_dir=config_dir)
    if isinstance(data, list):
        return data
    return []

def load_filters(config_dir: Path | str | None = None) -> dict:
    """Load the hard filters configuration."""
    data = load_yaml("filters.yaml", config_dir=config_dir)
    return data if isinstance(data, dict) else {}

def load_criteria(config_dir: Path | str | None = None) -> str:
    """Load the scoring rubric from criteria.md."""
    cfg = get_config_dir(config_dir)
    path = cfg / "criteria.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")

def load_model(config_dir: Path | str | None = None) -> str:
    """Load the preferred LLM model from filters.yaml, defaulting to gpt-4o-mini."""
    filters = load_filters(config_dir=config_dir)
    if isinstance(filters, dict):
        return filters.get("llm_model", "gpt-4o-mini")
    return "gpt-4o-mini"


SCORERS = ("llm", "decision")


def load_scorer(config_dir: Path | str | None = None) -> str:
    """Return the scorer named in filters.yaml (``scorer: llm|decision``; default llm).

    An unknown value raises instead of silently picking a default, so a typo
    can never switch scoring backends without anyone noticing.
    """
    value = str(load_filters(config_dir=config_dir).get("scorer") or "llm").strip().lower()
    if value not in SCORERS:
        raise ValueError(f"filters.yaml: scorer must be one of {SCORERS}, got {value!r}")
    return value
