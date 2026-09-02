from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_STRINGS = [
    "/Users/grantdever",
    "/Users/",
    "grantdever@",
    "ALI_Books",
    "ALI Books",
    "ali-books",
    "FREOPP",
    "sk-proj-",
    "ghp_",
    "Bearer sk-",
]

ALLOWED_EXACT_FILES = {
    "LICENSE",  # Contains copyright notice
    "tests/test_no_personal_data.py",  # Test specification itself
}


class TestNoPersonalData:
    """Gatekeeper test verifying that no private terms, internal paths, or credentials leak."""

    def test_no_personal_data_or_secrets(self) -> None:
        violating: list[str] = []

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

        assert not violating, "Found personal data or credentials in repository:\n" + "\n".join(violating)
