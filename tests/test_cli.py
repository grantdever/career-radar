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
