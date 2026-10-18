"""Shared fixtures for the tests that drive the root Makefile and the git hooks."""

from __future__ import annotations

from pathlib import Path

import pytest
from support.ci_strict import pytest_runtest_makereport, pytest_sessionfinish  # noqa: F401 - pytest hooks
from support.fakes import require_gitleaks
from support.wired import WiredRepo, build_template, clone_template


@pytest.fixture(scope="session")
def uv_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A uv cache outside every test HOME, shared by the session."""
    return tmp_path_factory.mktemp("uv-cache")


@pytest.fixture(scope="session")
def wired_template(tmp_path_factory: pytest.TempPathFactory, uv_cache: Path) -> Path:
    require_gitleaks()
    base = tmp_path_factory.mktemp("wired-template")
    build_template(base, uv_cache)
    return base


@pytest.fixture
def wired(tmp_path: Path, wired_template: Path, uv_cache: Path) -> WiredRepo:
    """A wired repo with hooks on, one clean commit, pushed to ``origin`` (bare)."""
    return clone_template(wired_template, tmp_path / "w", uv_cache)
