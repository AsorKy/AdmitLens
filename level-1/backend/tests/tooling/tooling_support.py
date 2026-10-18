"""Helpers for the level tooling tests: run make, sh and docker without leaking values.

* Executables are resolved to absolute paths; a missing tool fails the test, or
  skips it only outside CI when the tool is optional (docker).
* Child processes get a minimal, explicit environment: no inherited CI, MAKEFLAGS
  or COMPOSE_* variables, stdin from /dev/null and a timeout.
* Raw process output is never compared with a bare ``assert``: pytest's assertion
  rewriting would print it. ``assert_absent`` fails with a fingerprint of the value;
  ``assert_contains``, ``assert_lacks`` and ``assert_empty`` fail with the channel
  and its length only. Check ``assert_absent`` first, before any other assertion.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

LEVEL_DIR = Path(__file__).resolve().parents[2]

# Set in the environment of every child make, so a nested `make check` does not
# run the tests that call make again (they would recurse forever).
NESTED_VAR = "ADMITLENS_TOOLING_NESTED"

KE_NAMES = (
    "POSTGRES_USER",
    "POSTGRES_DB",
    "POSTGRES_PASSWORD",
    "ADMITLENS_DB_PORT",
    "ADMITLENS_DATABASE_URL",
    "ADMITLENS_TEST_DATABASE_URL",
)

# Files a throwaway copy of the level needs to run its Makefile and compose file.
LEVEL_FILES = ("Makefile", "docker-compose.yml", ".env.example", "pyproject.toml", "uv.lock")


def in_ci() -> bool:
    """Same rule as the Makefile: CI is on only when it is `true` or `1`."""
    return os.environ.get("CI", "") in ("true", "1")  # case-sensitive, like $(filter)


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:10]


def canary(label: str) -> str:
    """A unique, value-shaped marker built at runtime; never a literal in the repo."""
    del label  # kept for readability at call sites; the value is random hex only
    return "cn" + secrets.token_hex(16)


WINDOW = 10


def assert_absent(text: str, value: str, channel: str, *, windows: bool = True) -> None:
    """Fail if ``value``, or any 10-character window of it, appears in ``text``."""
    if not value:
        return
    parts = [value]
    if windows and len(value) > WINDOW:
        parts += [value[i : i + WINDOW] for i in range(len(value) - WINDOW + 1)]
    if any(p in text for p in parts):
        pytest.fail(f"value {fingerprint(value)} leaked on {channel}", pytrace=False)


def assert_contains(text: str, needle: str, channel: str) -> None:
    if needle not in text:
        pytest.fail(f"{channel}: expected text missing ({len(text)} chars)", pytrace=False)


def assert_lacks(text: str, needle: str, channel: str) -> None:
    if needle in text:
        pytest.fail(f"{channel}: forbidden text present ({len(text)} chars)", pytrace=False)


def assert_recipe_failed(err: str, target: str, code: int) -> None:
    """The recipe itself exited with ``code``: make 3.81 prints ``[target] Error N``,
    make 4.x prints ``[Makefile:44: target] Error N``."""
    pattern = rf"\[(?:[^\]\s]+:\d+: )?{re.escape(target)}\] Error {code}\b"
    if not re.search(pattern, err):
        pytest.fail(f"stderr: no '{target}' Error {code} line ({len(err)} chars)", pytrace=False)


def assert_empty(text: str, channel: str) -> None:
    if text:
        pytest.fail(f"{channel}: expected no output ({len(text)} chars)", pytrace=False)


def tool(name: str, *, optional: bool = False) -> str:
    path = shutil.which(name)
    if path:
        return path
    if optional and not in_ci():
        pytest.skip(f"{name} is not installed")
    pytest.fail(f"{name} is required for this test", pytrace=False)


def skip_if_nested() -> None:
    if os.environ.get(NESTED_VAR):
        pytest.skip("runs make on this level; skipped inside a nested make run")


@dataclass(frozen=True)
class RunResult:
    rc: int
    out: str
    err: str
    seconds: float

    def __repr__(self) -> str:  # never show the captured text
        return f"RunResult(rc={self.rc}, out={len(self.out)} chars, err={len(self.err)} chars)"


def child_env(extra: dict[str, str] | None = None, *, drop_ci: bool = True) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(Path.home()),  # uv keeps its cache under HOME
        "LC_ALL": "C",
        "TZ": "UTC",
        NESTED_VAR: "1",
    }
    for name in ("TMPDIR", "UV_CACHE_DIR", "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT"):
        if name in os.environ:
            env[name] = os.environ[name]
    if not drop_ci and "CI" in os.environ:
        env["CI"] = os.environ["CI"]
    env.update(extra or {})
    return env


def run(
    argv: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    *,
    timeout: float = 120,
    umask: int = -1,
) -> RunResult:
    start = time.monotonic()
    proc = subprocess.run(
        argv,
        cwd=cwd,
        env=env if env is not None else child_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        umask=umask,
        check=False,
    )
    return RunResult(proc.returncode, proc.stdout, proc.stderr, time.monotonic() - start)


def make(
    target: str,
    cwd: Path,
    env: dict[str, str] | None = None,
    *,
    dry: bool = False,
    timeout: float = 120,
) -> RunResult:
    argv = [tool("make"), "-s"]
    if dry:
        argv.append("-n")
    return run([*argv, target], cwd, env, timeout=timeout)


def copy_level(dest: Path, project: str | None = None, *, with_sql: bool = True) -> Path:
    """A throwaway copy of the level's Makefile, compose file and ops scripts.

    The compose ``name:`` is rewritten to a unique project, so even a run that
    forgets COMPOSE_PROJECT_NAME can never reach the developer's database.
    """
    dest.mkdir(parents=True, exist_ok=True)
    for name in LEVEL_FILES:
        shutil.copy2(LEVEL_DIR / name, dest / name)
    compose = dest / "docker-compose.yml"
    text = compose.read_text()
    if "\nname: admitlens-l1\n" not in text:
        pytest.fail("docker-compose.yml lost its `name: admitlens-l1` line", pytrace=False)
    project = project or unique_project("copy")
    compose.write_text(text.replace("\nname: admitlens-l1\n", f"\nname: {project}\n", 1))
    (dest / "ops").mkdir()
    shutil.copy2(LEVEL_DIR / "ops" / "make-env.sh", dest / "ops" / "make-env.sh")
    (dest / "ops" / "postgres").mkdir()
    sql = LEVEL_DIR / "ops" / "postgres" / "ensure-test-db.sql"
    if with_sql and sql.exists():
        shutil.copy2(sql, dest / "ops" / "postgres" / sql.name)
    return dest


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            name, _, value = line.partition("=")
            values[name] = value
    return values


def unique_project(label: str) -> str:
    """A compose project name no developer uses, so tests never touch the real database."""
    return f"admitlens-l1-tooling-{label}-{secrets.token_hex(4)}"
