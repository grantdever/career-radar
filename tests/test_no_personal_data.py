import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_STRINGS = [
    "/Users/",
    "/home/g/",
    "grantdever@",
    "sk-proj-",
    "sk-or-v1-",
    "ghp_",
    "github_pat_",
    "Bearer sk-",
]


def _private_terms() -> list[str]:
    """Maintainer-specific terms, kept out of the repo itself.

    Read from the PRIVATE_TERMS environment variable (comma-separated; set as a
    CI secret) and from an untracked .private-terms file (one term per line).
    Listing them here would publish the very strings this test guards.
    """
    terms = [t.strip() for t in os.environ.get("PRIVATE_TERMS", "").split(",")]
    local = ROOT / ".private-terms"
    if local.is_file():
        terms += [t.strip() for t in local.read_text(encoding="utf-8").splitlines()]
    return [t for t in terms if t and not t.startswith("#")]

ALLOWED_EXACT_FILES = {
    "LICENSE",  # Contains copyright notice
    "tests/test_no_personal_data.py",  # Test specification itself
    ".private-terms",  # Untracked list of private terms
}


class TestNoPersonalData:
    """Gatekeeper test verifying that no private terms, internal paths, or credentials leak."""

    def test_no_personal_data_or_secrets(self) -> None:
        violating: list[str] = []
        private = [t.lower() for t in _private_terms()]

        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(ROOT).as_posix()

            # Skip caches, build artifacts, git internals, and virtualenv
            if any(part in rel for part in [".venv", ".git", "__pycache__", ".pytest_cache", ".ruff_cache", "dist", "build"]):
                continue

            if rel in ALLOWED_EXACT_FILES:
                continue

            try:
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except Exception:
                continue

            for line_no, line in enumerate(lines, start=1):
                # Narrowly permit public author name in pyproject.toml
                if rel == "pyproject.toml" and 'name = "Grant Dever"' in line:
                    continue

                for forbidden in FORBIDDEN_STRINGS:
                    if forbidden in line:
                        violating.append(f"{rel}:{line_no}: contains {forbidden!r}")
                lowered = line.lower()
                for term in private:
                    if term in lowered:
                        violating.append(f"{rel}:{line_no}: contains a private term")

        assert not violating, "Found personal data or credentials in repository:\n" + "\n".join(violating)
