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
