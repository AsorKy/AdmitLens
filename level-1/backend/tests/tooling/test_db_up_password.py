"""`make db-up` against a real PostgreSQL in a unique compose project (needs Docker).

The volume keeps the password of its first init. A .env regenerated over that
volume must make `db-up` fail clearly instead of reporting a healthy database that
the app cannot log in to. Never touches the developer's `admitlens-l1` project:
the copy gets its own project name and a free loopback port, and is removed after.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from tooling_support import (
    assert_absent,
    assert_contains,
    assert_recipe_failed,
    child_env,
    copy_level,
    in_ci,
    make,
    read_env_file,
    run,
    tool,
    unique_project,
)

pytestmark = [pytest.mark.tooling, pytest.mark.docker]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def fresh_env(level: Path, port: int) -> str:
    """`make env`, then move the copy to a free port. Returns the new password."""
    (level / ".env").unlink(missing_ok=True)
    result = make("env", level)
    assert result.rc == 0, result
    text = (level / ".env").read_text()
    text = text.replace("ADMITLENS_DB_PORT=5432\n", f"ADMITLENS_DB_PORT={port}\n")
    (level / ".env").write_text(text.replace(":5432/", f":{port}/"))
    return read_env_file(level / ".env")["POSTGRES_PASSWORD"]


@pytest.fixture
def db_level(tmp_path: Path) -> Iterator[tuple[Path, dict[str, str]]]:
    if in_ci():
        pytest.skip("CI has no compose database (it uses a service container)")
    docker = tool("docker", optional=True)
    if run([docker, "info"], tmp_path, timeout=20).rc != 0:
        pytest.skip("the Docker daemon is not running")
    project = unique_project("pw")
    level = copy_level(tmp_path / "level", project)
    env = child_env({"COMPOSE_PROJECT_NAME": project})
    try:
        yield level, env
    finally:
        # `down` interpolates the file too; a throwaway value satisfies `:?`.
        down_env = {**env, "POSTGRES_PASSWORD": "teardown"}
        run([docker, "compose", "-p", project, "down", "-v", "--remove-orphans"], level,
            down_env, timeout=120)  # fmt: skip


def test_db_up_rejects_a_env_regenerated_over_an_existing_volume(
    db_level: tuple[Path, dict[str, str]],
) -> None:
    level, env = db_level
    port = free_port()

    first_password = fresh_env(level, port)
    first = make("db-up", level, env, timeout=180)
    assert_absent(first.out + first.err, first_password, "first db-up")
    assert first.rc == 0, first

    second_password = fresh_env(level, port)
    second = make("db-up", level, env, timeout=180)
    for value in (first_password, second_password):
        assert_absent(second.out + second.err, value, "second db-up")
    assert second.rc != 0, second
    assert_contains(second.err, "initialised with another password); run make db-down", "stderr")
    assert_recipe_failed(second.err, "db-up", 1)
