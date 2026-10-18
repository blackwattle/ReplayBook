from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from replaybook.cli import app

runner = CliRunner()


def test_generate_command(tmp_path: Path) -> None:
    out = tmp_path / "data"
    result = runner.invoke(app, ["generate", "--out", str(out), "--seed", "1", "--n", "60"])
    assert result.exit_code == 0, result.output
    assert "tickets" in result.output
    assert (out / "tickets.jsonl").exists()
    assert (out / "ground_truth.jsonl").exists()
    assert (out / "platform.yaml").exists()
    assert (out / "DATASET.md").exists()
