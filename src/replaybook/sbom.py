"""CycloneDX SBOM for Replaybook, built from uv.lock so it lists exactly what is installed.

Needs the sbom dependency group (`uv sync --group sbom`) and uv on PATH. The output is
reproducible for the same lock file: no serial number, no timestamp unless
SOURCE_DATE_EPOCH is set. The Docker image is built with one at /app/sbom.cdx.json.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


class SbomUnavailable(RuntimeError):
    """The SBOM cannot be written here: missing uv, lock file or the cyclonedx-bom package."""


def write_sbom(out: Path, project_dir: Path | None = None) -> Path:
    project_dir = Path(project_dir or Path.cwd())
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.exists() or not (project_dir / "uv.lock").exists():
        raise SbomUnavailable(f"no pyproject.toml and uv.lock in {project_dir}")
    uv = shutil.which("uv")
    if uv is None:
        raise SbomUnavailable("uv is not on PATH")
    try:
        import cyclonedx_py  # noqa: F401
    except ImportError as exc:
        raise SbomUnavailable(
            "cyclonedx-bom is not installed; run `uv sync --group sbom` first"
        ) from exc

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="replaybook-sbom-") as tmp:
        requirements = Path(tmp) / "requirements.txt"
        _run(
            [
                uv,
                "export",
                "--frozen",
                "--no-dev",
                "--no-emit-project",
                "--quiet",
                "--output-file",
                str(requirements),
            ],
            project_dir,
        )
        _run(
            [
                sys.executable,
                "-m",
                "cyclonedx_py",
                "requirements",
                str(requirements),
                "--pyproject",
                str(pyproject),
                "--mc-type",
                "application",
                "--output-reproducible",
                "--of",
                "JSON",
                "--output-file",
                str(out),
            ],
            project_dir,
        )
    return out


def _run(command: list[str], cwd: Path) -> None:
    completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise SbomUnavailable(
            f"{Path(command[0]).name} failed ({completed.returncode}): {completed.stderr[-2000:]}"
        )
