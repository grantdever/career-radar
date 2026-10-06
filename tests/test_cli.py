from click.testing import CliRunner

from career_radar.cli import main


def test_cli_help():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "Career Radar" in result.output
    assert "scan" in result.output
    assert "review" in result.output
    assert "init" in result.output

def test_cli_init_custom_dir(tmp_path):
    runner = CliRunner()
    config_dir = tmp_path / "custom_config"
    result = runner.invoke(main, ["init", "--config-dir", str(config_dir)])
    assert result.exit_code == 0
    assert (config_dir / "employers.yaml").exists()
    assert (config_dir / "filters.yaml").exists()
    assert (config_dir / "criteria.md").exists()

def test_cli_scan_help():
    runner = CliRunner()
    result = runner.invoke(main, ["scan", "--help"])
    assert result.exit_code == 0
    assert "--skip-score" in result.output

def test_cli_review_help():
    runner = CliRunner()
    result = runner.invoke(main, ["review", "--help"])
    assert result.exit_code == 0
    assert "--db" in result.output

def test_cli_review_empty_db(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "empty.db"
    result = runner.invoke(main, ["review", "--db", str(db_path)])
    assert result.exit_code == 0
    assert "Inbox is empty" in result.output


def test_init_template_has_a_live_board(tmp_path):
    """Quick-start regression: a fresh init must yield at least one employer to scan."""
    from career_radar.config import load_employers, load_scorer

    runner = CliRunner()
    cfg = tmp_path / "cfg"
    assert runner.invoke(main, ["init", "--config-dir", str(cfg)]).exit_code == 0
    employers = load_employers(cfg)
    assert len(employers) >= 1
    assert employers[0]["ats"] in ("greenhouse", "lever", "ashby")
    assert load_scorer(cfg) == "llm"


def test_output_dir_env_override(tmp_path, monkeypatch):
    from career_radar.config import DEFAULT_OUTPUT_DIR, get_output_dir

    monkeypatch.delenv("CAREER_RADAR_OUTPUT_DIR", raising=False)
    assert get_output_dir() == DEFAULT_OUTPUT_DIR
    monkeypatch.setenv("CAREER_RADAR_OUTPUT_DIR", str(tmp_path / "env-out"))
    assert get_output_dir() == tmp_path / "env-out"
    assert get_output_dir(tmp_path / "flag-out") == tmp_path / "flag-out"


def test_scan_writes_report_to_env_output_dir(tmp_path, monkeypatch):
    import yaml

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "employers.yaml").write_text(yaml.safe_dump([]))
    monkeypatch.setenv("CAREER_RADAR_OUTPUT_DIR", str(tmp_path / "env-out"))
    result = CliRunner().invoke(main, ["scan", "--skip-score", "--config-dir", str(cfg),
                                       "--db", str(tmp_path / "db.sqlite")])
    assert result.exit_code == 0, result.output
    assert list((tmp_path / "env-out").glob("*-shortlist.md"))


def test_check_decision_command(tmp_path):
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "examples" / "decision.backend-engineer.yaml"
    ok = CliRunner().invoke(main, ["check-decision", str(example)])
    assert ok.exit_code == 0 and "qhash" in ok.output
    bad = tmp_path / "d.yaml"
    bad.write_text(example.read_text().replace("sum: [domain, work]", "sum: [domain, wrok]"))
    res = CliRunner().invoke(main, ["check-decision", str(bad)])
    assert res.exit_code != 0 and "wrok" in res.output


def test_eval_cli_end_to_end(tmp_path):
    import shutil
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    d = tmp_path / "synthetic"
    shutil.copytree(root / "examples" / "synthetic", d,
                    ignore=shutil.ignore_patterns("LOCK.yaml", "predictions.jsonl", "queue.jsonl",
                                                  "replay-*", "report.*", "spend.db"))
    shutil.copy(root / "examples" / "decision.backend-engineer.yaml", tmp_path)
    runner = CliRunner()
    for args in (["eval", "freeze", str(d)], ["eval", "run", str(d)], ["eval", "report", str(d)]):
        r = runner.invoke(main, args)
        assert r.exit_code == 0, r.output
    assert (d / "report.md").exists()
    r = runner.invoke(main, ["eval", "run", str(d)])
    assert r.exit_code != 0 and "sealed" in r.output
