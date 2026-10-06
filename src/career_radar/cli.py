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
# One live public board is enabled so your first `career-radar scan` finds
# real postings. Replace it with the employers you actually want to track.
- name: GitLab
  ats: greenhouse
  board: gitlab

# More examples (see docs/ats-guide.md for every supported ATS):
# - name: Example Workday Employer
#   ats: workday
#   host: example.wd1.myworkdayjobs.com
#   tenant: example
#   site: careers
# - name: Example Greenhouse Employer
#   ats: greenhouse
#   board: example
# - name: Example Lever Employer
#   ats: lever
#   company: example
# - name: Example Ashby Employer
#   ats: ashby
#   board: example
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
# Scorer: "llm" (one rubric prompt per batch, via LiteLLM) or "decision"
# (typed questions + an auditable fold; needs decision.yaml in this
# directory - see examples/decision.backend-engineer.yaml).
scorer: llm
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

@main.command("check-decision")
@click.argument("path", required=False, type=click.Path(path_type=Path))
def check_decision(path: Path | None) -> None:
    """Validate a decision.yaml without calling any model."""
    from career_radar.core import decision

    target = path or (CONFIG_DIR / decision.DECISION_FILENAME)
    try:
        cfg = decision.load_config(target)
    except decision.DecisionConfigError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"OK {target}")
    click.echo(f"  qhash {cfg.qhash}; transport {cfg.transport.kind} {cfg.transport.model}")
    for name, q in cfg.questions.items():
        extra = (f"{q.levels} levels" if q.type == "score"
                 else f"options {list(q.options)}" if q.type == "choice" else "probability")
        click.echo(f"  {name}: {q.type} ({extra})")
    click.echo(f"  surface cutoff {cfg.surface_cutoff}")


@main.command()
@click.option('--db', type=click.Path(path_type=Path), default=None, help="Custom SQLite database path")
@click.option('--since', default=None, help="Only calls on or after this ISO date")
def spend(db: Path | None, since: str | None) -> None:
    """Show paid model calls recorded in the spend ledger."""
    from career_radar.core import dedupe
    from career_radar.core.spend import Ledger

    conn = dedupe.connect(db or dedupe.DB_PATH)
    rows = Ledger(conn).summary(since)
    if not rows:
        click.echo("No paid calls recorded.")
        return
    for r in rows:
        click.echo(f"{r['provider']:<11} {r['model']:<40} {r['calls']:>5} calls  "
                   f"{(r['input_tokens'] or 0):>9} in / {(r['output_tokens'] or 0):>7} out  "
                   f"${(r['cost_usd'] or 0):.6f}  {(r['mean_latency_ms'] or 0):.0f} ms avg")


@main.group("eval")
def eval_group() -> None:
    """Pre-registered scorer evaluation: freeze, run, label, report."""


def _spec(path: Path):
    from career_radar.core import eval as ev

    try:
        return ev.load_spec(path)
    except ev.EvalError as e:
        raise click.ClickException(str(e)) from e


@eval_group.command("freeze")
@click.argument("eval_dir", type=click.Path(path_type=Path, exists=True))
@click.option("--note", default="", help="Free-text note stored in LOCK.yaml")
def eval_freeze(eval_dir: Path, note: str) -> None:
    """Write LOCK.yaml (hashes, cutoffs, decision and stopping rules). Commit it before running."""
    from career_radar.core import eval as ev

    try:
        lock = ev.freeze(_spec(eval_dir), note=note)
    except (ev.EvalError, ValueError) as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"Frozen: {lock}")


@eval_group.command("run")
@click.argument("eval_dir", type=click.Path(path_type=Path, exists=True))
def eval_run(eval_dir: Path) -> None:
    """Score the frozen pool with every contestant; refuses if anything drifted."""
    from career_radar.core import eval as ev

    try:
        out = ev.run(_spec(eval_dir), progress=click.echo)
    except ev.EvalError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"Sealed predictions: {out}")


@eval_group.command("label")
@click.argument("eval_dir", type=click.Path(path_type=Path, exists=True))
def eval_label(eval_dir: Path) -> None:
    """Blind, shuffled labelling queue in the terminal (no scores shown)."""
    from career_radar.core import eval as ev

    n = ev.label_interactive(_spec(eval_dir), ask=lambda p: click.prompt(p, default="", show_default=False),
                             show=click.echo)
    click.echo(f"Recorded {n} labels.")


@eval_group.command("report")
@click.argument("eval_dir", type=click.Path(path_type=Path, exists=True))
@click.option("--sweep", is_flag=True, help="Add an EXPLORATORY cutoff sweep (not evidence)")
def eval_report(eval_dir: Path, sweep: bool) -> None:
    """P/R/F1 with CIs, paired bootstrap, disagreements, cost; writes report.json + report.md."""
    from career_radar.core import eval as ev

    try:
        out = ev.report(_spec(eval_dir), sweep=sweep)
    except ev.EvalError as e:
        raise click.ClickException(str(e)) from e
    for name, c in out["contestants"].items():
        click.echo(f"{name:<16} cutoff {c['cutoff']:>2}  P {c['precision']:.2f}  "
                   f"R {c['recall']:.2f}  F1 {c['f1']:.3f}")
    click.echo(out["verdict"]["text"])
    click.echo(f"Wrote {eval_dir / 'report.md'} and report.json")


if __name__ == "__main__":
    main()
