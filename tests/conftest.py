from __future__ import annotations

import os
from pathlib import Path

import pytest

from replaybook.synth.generator import GenerateOptions, generate

# GitHub Actions sets GITHUB_ACTIONS, and Typer then forces a terminal: help and error
# output come out through Rich with ANSI styling, which splits option names such as
# `--transport` and fails tests that read the output. The tests read plain text wherever
# they run. Typer reads this when it first renders help, which is after this file loads.
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"

SMALL_N = 600


@pytest.fixture(scope="session")
def small_dataset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A small generated dataset shared by the whole test session."""
    out = tmp_path_factory.mktemp("data") / "generated"
    generate(out, GenerateOptions(seed=7, n_tickets=SMALL_N))
    return out
