"""A10 (make) and the KM contract: the verbs exist, stubs say who implements them,
`check` passes with no app code, `make test` without a database fails fast and
clearly, the database targets guard themselves, and `make -n` never shows a value.

Every target that touches compose runs in a throwaway copy of the level with a
unique compose project name, so these tests never see the developer's database.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from tooling_support import (
    KE_NAMES,
    LEVEL_DIR,
    assert_absent,
    assert_contains,
    assert_lacks,
    assert_recipe_failed,
    canary,
    child_env,
    copy_level,
    make,
    skip_if_nested,
    unique_project,
)

pytestmark = pytest.mark.tooling

KM_VERBS = (
    "help", "install", "lint", "typecheck", "test-unit", "test", "check", "openapi",
    "openapi-check", "env", "db-up", "db-stop", "db-down", "db-check", "migrate", "seed", "dev",
)  # fmt: skip
STUBS = {
    "openapi": "L1-P4",
    "openapi-check": "L1-P4",
    "migrate": "L1-P2",
    "seed": "L1-P4",
    "dev": "L1-P4",
}
MAKEFILE = (LEVEL_DIR / "Makefile").read_text()
TARGET_RE = re.compile(r"^([a-zA-Z0-9_-]+):(?!=)", re.MULTILINE)


def makefile_targets() -> list[str]:
    return sorted(set(TARGET_RE.findall(MAKEFILE)))


def isolated(tmp_path: Path, label: str, **extra: str) -> tuple[Path, dict[str, str]]:
    project = unique_project(label)
    level = copy_level(tmp_path / "level", project)
    return level, child_env({"COMPOSE_PROJECT_NAME": project, **extra})


def test_makefile_defines_every_km_verb() -> None:
    missing = set(KM_VERBS) - set(makefile_targets())
    assert not missing


def test_help_lists_every_km_verb() -> None:
    result = make("help", LEVEL_DIR)
    assert result.rc == 0, result
    listed = {line.split()[0] for line in result.out.splitlines() if line.strip()}
    assert set(KM_VERBS) <= listed
    assert len(listed) == len(result.out.splitlines())  # one line per target, no duplicates


@pytest.mark.parametrize(("target", "plan"), sorted(STUBS.items()))
def test_reserved_verbs_exit_2_naming_their_plan(target: str, plan: str) -> None:
    result = make(target, LEVEL_DIR)
    assert result.rc == 2, result
    assert_contains(result.err, f"implemented in {plan}", "stderr")
    assert_recipe_failed(result.err, target, 2)  # the recipe's own code


def test_check_is_lint_plus_test_unit_without_docker() -> None:
    assert re.search(r"^check: lint test-unit(\s|$)", MAKEFILE, re.MULTILINE)
    recipe = MAKEFILE.split("\ncheck:", 1)[1].split("\n\n", 1)[0]
    assert "docker" not in recipe and "typecheck" not in recipe


def test_check_passes_with_no_app_code() -> None:
    skip_if_nested()
    assert (LEVEL_DIR / "app" / "__init__.py").read_text() == ""
    result = make("check", LEVEL_DIR, timeout=180)
    assert result.rc == 0, result


def test_make_test_without_a_database_fails_fast_and_clearly(tmp_path: Path) -> None:
    level, env = isolated(tmp_path, "nodb")
    password = canary("POSTGRES_PASSWORD")
    (level / ".env").write_text(f"POSTGRES_PASSWORD={password}\n")
    result = make("test", level, env, timeout=30)
    output = result.err + result.out
    assert_absent(output, password, "make test output")
    assert result.rc != 0, result
    assert result.seconds < 10, f"took {result.seconds:.1f}s"
    assert_contains(result.err, "run make db-up", "stderr")
    assert_lacks(output, "Traceback", "make test output")
    assert_lacks(output.lower(), "postgresql", "make test output")


def test_db_check_without_env_file_says_make_env(tmp_path: Path) -> None:
    level, env = isolated(tmp_path, "noenv")
    result = make("db-check", level, env, timeout=30)
    assert result.rc != 0, result
    assert_contains(result.err, "run make env", "stderr")


@pytest.mark.parametrize("value", ["true", "1"])
def test_db_check_is_skipped_in_ci(tmp_path: Path, value: str) -> None:
    level, env = isolated(tmp_path, "ci", CI=value)
    result = make("db-check", level, env, timeout=30)
    assert result.rc == 0, result
    assert_contains(result.out, "skipped in CI", "stdout")


def test_db_check_runs_when_ci_is_false(tmp_path: Path) -> None:
    level, env = isolated(tmp_path, "cifalse", CI="false")
    result = make("db-check", level, env, timeout=30)
    assert result.rc != 0, result
    assert_lacks(result.out, "skipped in CI", "stdout")
    assert_contains(result.err, "run make env", "stderr")


def test_db_down_aborts_without_a_tty(tmp_path: Path) -> None:
    level, env = isolated(tmp_path, "down")
    result = make("db-down", level, env, timeout=30)
    assert result.rc != 0, result
    assert_contains(result.err, "interactive terminal", "stderr")
    assert_recipe_failed(result.err, "db-down", 2)


def test_db_up_without_the_sql_fails_clearly(tmp_path: Path) -> None:
    project = unique_project("nosql")
    level = copy_level(tmp_path / "level", project, with_sql=False)
    env = child_env({"COMPOSE_PROJECT_NAME": project})
    result = make("db-up", level, env, timeout=30)
    assert result.rc != 0, result
    assert_contains(result.err, "missing ops/postgres/ensure-test-db.sql", "stderr")


def test_makefile_is_safe_by_construction() -> None:
    """No include of .env, no constructs newer than Make 3.81, every recipe line starts with @."""
    make_only = (".ONESHELL", ".SHELLFLAGS", ".RECIPEPREFIX", "::=", "!=", "&:")
    continued = False
    for line in MAKEFILE.splitlines():
        recipe = line.startswith("\t")
        if recipe and not continued:
            assert line.startswith("\t@"), line
        if not recipe and not continued and not line.lstrip().startswith("#"):
            assert not re.match(r"\s*-?s?include\s.*\.env", line), line
            for construct in make_only:
                assert construct not in line, construct
        for function in ("$(file ", "$(let "):
            assert function not in line, function
        continued = line.endswith("\\")


def test_make_n_shows_no_values(tmp_path: Path) -> None:
    targets = makefile_targets()
    assert set(KM_VERBS) <= set(targets)  # never vacuous
    values = {name: canary(name) for name in KE_NAMES}
    level, env = isolated(tmp_path, "dryrun", **values)
    (level / ".env").write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    for ci in (False, True):
        run_env = {**env, "CI": "true"} if ci else env
        for target in targets:
            result = make(target, level, run_env, dry=True, timeout=30)
            for name, value in values.items():
                assert_absent(result.out + result.err, value, f"make -n {target} ({name})")
