"""``support/ci_strict.py``: with ``CI=true`` a skip fails the run; without it a skip is a skip."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from support.fakes import GUARDS_ROOT, run

pytestmark = pytest.mark.unit

SAMPLE = '''
import pytest


def test_runtime_skip():
    pytest.skip("not here")


def test_ok():
    pass
'''

COLLECT_SKIP = '''
import pytest

pytest.skip("whole module", allow_module_level=True)
'''


@pytest.fixture(scope="module")
def sample(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("ci-strict")
    (d / "test_runtime.py").write_text(SAMPLE, encoding="utf-8")
    (d / "test_collect.py").write_text(COLLECT_SKIP, encoding="utf-8")
    (d / "test_ok.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    return d


def _pytest(sample: Path, name: str, ci: str | None) -> int:
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "support.ci_strict", name, "test_ok.py"]
    env = {"PYTHONPATH": str(GUARDS_ROOT / "tests"), "CI": ci or "false"}
    return run(argv, sample, home=sample.parent / ".home", extra_env=env, timeout=60).rc


@pytest.mark.parametrize("name", ["test_runtime.py", "test_collect.py"])
def test_skip_fails_in_ci(sample: Path, name: str) -> None:
    assert _pytest(sample, name, "true") == 1


@pytest.mark.parametrize("name", ["test_runtime.py", "test_collect.py"])
def test_skip_is_a_skip_outside_ci(sample: Path, name: str) -> None:
    assert _pytest(sample, name, None) == 0
