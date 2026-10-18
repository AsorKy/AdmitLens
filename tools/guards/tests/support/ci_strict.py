"""In CI a skipped test is a failure (plan PLAN-CA-0005 A1).

Loaded by ``tests/conftest.py``. A skip at run time is turned into a failure in
the report; a skip during collection (which has no run-time report) still
fails the session through its exit status. Outside CI skips stay skips.
"""

from __future__ import annotations

import os
from collections.abc import Generator

import pytest


def in_ci() -> bool:
    return os.environ.get("CI", "") == "true"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Generator[None, None, None]:
    outcome = yield
    report = outcome.get_result()
    if in_ci() and report.skipped:
        report.outcome = "failed"
        report.longrepr = f"skipped in CI (CI=true): {item.nodeid}"


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not in_ci():
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped = len(reporter.stats.get("skipped", [])) if reporter is not None else 0
    if skipped and session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
