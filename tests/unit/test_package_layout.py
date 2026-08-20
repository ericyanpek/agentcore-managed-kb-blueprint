"""Guard the package layout invariants that would otherwise fail at runtime."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def test_kbp_package_is_importable():
    import kbp

    assert Path(kbp.__file__).parent == ROOT / "kbp"


def test_no_top_level_package_shadows_stdlib():
    """A top-level `platform` package breaks botocore's User-Agent construction.

    botocore calls platform.system(); if a local package shadows the stdlib
    module, every boto3 call from the repository root raises AttributeError.
    """
    shadowed = [
        name
        for name in ("platform", "types", "json", "io", "select", "code")
        if (ROOT / name / "__init__.py").exists()
    ]
    assert shadowed == []


def test_boto3_works_from_repository_root():
    result = subprocess.run(
        [sys.executable, "-c", "import boto3; boto3.session.Session()"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
