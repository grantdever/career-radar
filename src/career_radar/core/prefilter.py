"""Deterministic pre-filter: cheap, auditable rules that drop obviously
irrelevant postings before we pay for a detail fetch or an LLM score.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from career_radar.config import load_filters

logger = logging.getLogger(__name__)

DEFAULT_LOCATIONS = ["Remote", "New York, NY"]
DEFAULT_NEGATIVE_TITLES = ["Staffing", "Recruiter"]


def _compile_filter(word_list: list[str], bounded: bool = True) -> re.Pattern | None:
    if not word_list:
        return None
    # Escape words safely so special regex characters don't cause compilation errors
    escaped = [re.escape(w.strip()) for w in word_list if w.strip()]
    if not escaped:
        return None
    joined = "|".join(escaped)
    if bounded:
        pattern = rf"\b({joined})\b"
    else:
        pattern = rf"({joined})"
    return re.compile(pattern, re.IGNORECASE)


@dataclass
class FilterRules:
    """Explicit runtime filter configuration."""

    locations: list[str] = field(default_factory=list)
    negative_titles: list[str] = field(default_factory=list)
    drop_part_time: bool = True

    def __post_init__(self):
        self._negative_title_re = _compile_filter(self.negative_titles, bounded=True)
        self._allowed_locations_re = _compile_filter(self.locations, bounded=False)
        self._part_time_re = (
            re.compile(r"(part[- ]?time|seasonal|temporary)", re.IGNORECASE)
            if self.drop_part_time
            else None
        )

    @classmethod
    def from_dict(cls, data: dict | None = None) -> "FilterRules":
        if not data:
            return cls.default()
        return cls(
            locations=list(data.get("locations") or []),
            negative_titles=list(data.get("negative_titles") or []),
            drop_part_time=bool(data.get("drop_part_time", True)),
        )

    @classmethod
    def from_config_dir(cls, config_dir: Path | str | None = None) -> "FilterRules":
        data = load_filters(config_dir=config_dir)
        if not data:
            return cls.default()
        return cls.from_dict(data)

    @classmethod
    def default(cls) -> "FilterRules":
        return cls(
            locations=list(DEFAULT_LOCATIONS),
            negative_titles=list(DEFAULT_NEGATIVE_TITLES),
            drop_part_time=True,
        )

    def _location_ok(self, location: str, remote_type: str) -> bool:
        loc = (location or "").strip()
        if (remote_type or "").strip().lower() == "remote":
            if self._allowed_locations_re and self._allowed_locations_re.search("Remote"):
                return True

        if not loc:
            return True  # unknown location -> let the scorer decide

        if self._allowed_locations_re and self._allowed_locations_re.search(loc):
            return True

        return False

    def classify(
        self,
        title: str,
        location: str = "",
        remote_type: str = "",
    ) -> tuple[bool, str]:
        """Return (keep, reason). Reason is "" when kept."""
        t = title or ""

        if self._part_time_re and self._part_time_re.search(t):
            return False, "part-time/seasonal"

        if self._negative_title_re and self._negative_title_re.search(t):
            return False, "negative title"

        if self._allowed_locations_re and not self._location_ok(location, remote_type):
            return False, "location"

        return True, ""

    def apply(self, postings: list) -> tuple[list, list]:
        """Split postings into (kept, dropped) where dropped is [(posting, reason)]."""
        kept = []
        dropped = []
        for p in postings:
            ok, reason = self.classify(p.title, p.location, p.remote_type)
            if ok:
                kept.append(p)
            else:
                dropped.append((p, reason))
        return kept, dropped


# Module-level convenience functions using default / runtime rules
_default_rules = FilterRules.default()


def classify(
    title: str,
    location: str = "",
    remote_type: str = "",
    rules: FilterRules | None = None,
) -> tuple[bool, str]:
    active = rules or _default_rules
    return active.classify(title, location, remote_type)


def apply(postings: list, rules: FilterRules | None = None) -> tuple[list, list]:
    active = rules or _default_rules
    return active.apply(postings)
