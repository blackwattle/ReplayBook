"""The CycloneDX SBOM lists exactly the pinned dependencies, with hashes, reproducibly."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from replaybook.sbom import SbomUnavailable, write_sbom

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv on PATH")
def test_sbom_lists_pinned_dependencies_with_hashes(tmp_path: Path) -> None:
    pytest.importorskip("cyclonedx_py")
    out = write_sbom(tmp_path / "sbom.cdx.json", REPO)
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["bomFormat"] == "CycloneDX"
    assert doc["metadata"]["component"]["name"] == "replaybook"
    names = {c["name"] for c in doc["components"]}
    assert {"pydantic", "typer", "mcp", "pyyaml"} <= names
    assert "pytest" not in names and "cyclonedx-bom" not in names, "dev tools are not shipped"
    for component in doc["components"]:
        hashes = [h for ref in component["externalReferences"] for h in ref.get("hashes", [])]
        assert hashes and all(h["alg"] == "SHA-256" for h in hashes), component["name"]
    assert all(c.get("purl", "").startswith("pkg:pypi/") for c in doc["components"])
    assert "serialNumber" not in doc, "reproducible output has no random serial"


def test_sbom_fails_clearly_without_a_lock_file(tmp_path: Path) -> None:
    with pytest.raises(SbomUnavailable):
        write_sbom(tmp_path / "sbom.cdx.json", tmp_path)
