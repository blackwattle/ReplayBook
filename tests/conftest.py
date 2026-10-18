from __future__ import annotations

from pathlib import Path

import pytest

from replaybook.synth.generator import GenerateOptions, generate

SMALL_N = 600


@pytest.fixture(scope="session")
def small_dataset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A small generated dataset shared by the whole test session."""
    out = tmp_path_factory.mktemp("data") / "generated"
    generate(out, GenerateOptions(seed=7, n_tickets=SMALL_N))
    return out
