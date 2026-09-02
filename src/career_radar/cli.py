"""Command-line interface for Career Radar.

Provides commands to initialize configurations, run ATS scans,
and interactively review surfaced job postings.
"""

from pathlib import Path

import click

from career_radar.config import CONFIG_DIR


@click.group()
def main() -> None:
    """Career Radar: AI-powered job search CLI that learns your preferences."""
    pass

@main.command()
@click.option(
    "--config-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Custom configuration directory (default: ~/.config/career-radar)",
)
def init(config_dir: Path | None) -> None:
    """Initialize configuration in ~/.config/career-radar"""
    target_dir = config_dir or CONFIG_DIR
    if target_dir.exists() and any(target_dir.iterdir()):
        click.echo(f"Configuration directory already exists at {target_dir}")
        return

    target_dir.mkdir(parents=True, exist_ok=True)

    # Create template files
    employers_yaml = target_dir / "employers.yaml"
    criteria_md = target_dir / "criteria.md"
    filters_yaml = target_dir / "filters.yaml"

    employers_yaml.write_text("""# List your target employers and their ATS configurations.
# Examples:
# - name: Stripe
#   ats: workday
#   host: stripe.wd1.myworkdayjobs.com
#   tenant: stripe
#   site: careers
# - name: OpenAI
#   ats: greenhouse
#   board: openai
# - name: Figma
#   ats: lever
#   company: figma
""", encoding="utf-8")

    criteria_md.write_text("""# Career Radar Rubric

## Your Preferences
(This file acts as the rubric for the LLM when scoring jobs).

- Comp floor: $100k
- Positive texture: autonomous, open source, remote, product-led growth
- Negative texture: micromanagement, legacy tech, intense travel, rigid hours

## Feedback Log
(When you reject jobs in the review UI, your feedback will be appended here to teach the system).

# Example:
# - YYYY-MM-DD: NO: Project Manager - sounds too process-heavy and lacks product ownership.
""", encoding="utf-8")

    filters_yaml.write_text("""# Hard filters applied before scoring (determines what gets dropped completely)
locations:
  - Remote
  - New York, NY
negative_titles:
  - Staffing
  - Recruiter
drop_part_time: true
llm_model: gpt-4o-mini
""", encoding="utf-8")

    click.echo(f"Initialized configuration at {target_dir}")
    click.echo("Please edit employers.yaml, filters.yaml, and criteria.md to match your preferences.")

@main.command()
@click.option('--skip-score', is_flag=True, help="Fetch and dedupe only, skip LLM scoring")
@click.option('--config-dir', type=click.Path(path_type=Path), default=None, help="Custom configuration directory")
@click.option('--db', type=click.Path(path_type=Path), default=None, help="Custom SQLite database path")
@click.option('--output-dir', type=click.Path(path_type=Path), default=None, help="Custom report output directory")
def scan(
    skip_score: bool,
    config_dir: Path | None,
    db: Path | None,
    output_dir: Path | None,
) -> None:
    """Scan employer boards and score new postings."""
    click.echo("Scanning jobs...")
    from career_radar.core import pipeline
    pipeline.run_pipeline(
        skip_score=skip_score,
        config_dir=config_dir,
        db_path=db,
        output_dir=output_dir,
    )

@main.command()
@click.option('--db', type=click.Path(path_type=Path), default=None, help="Custom SQLite database path")
@click.option('--config-dir', type=click.Path(path_type=Path), default=None, help="Custom configuration directory")
def review(db: Path | None, config_dir: Path | None) -> None:
    """Review highly scored job matches in the terminal."""
    from career_radar.ui import review
    review.main(db_path=db, config_dir=config_dir)

if __name__ == "__main__":
    main()
