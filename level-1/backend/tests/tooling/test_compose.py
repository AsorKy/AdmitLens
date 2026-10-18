"""A10 (compose): the static shape of docker-compose.yml and `docker compose config -q`.

`config` is only ever run with -q: without it the rendered file shows the password.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from tooling_support import (
    LEVEL_DIR,
    RunResult,
    assert_absent,
    assert_contains,
    assert_empty,
    canary,
    child_env,
    copy_level,
    run,
    tool,
    unique_project,
)

pytestmark = pytest.mark.tooling

COMPOSE = yaml.safe_load((LEVEL_DIR / "docker-compose.yml").read_text())
IMAGE_RE = re.compile(r"postgres:16\.\d+-bookworm@sha256:[0-9a-f]{64}")


def db() -> dict[str, Any]:
    return COMPOSE["services"]["db"]


def test_single_db_service_with_project_name() -> None:
    assert COMPOSE["name"] == "admitlens-l1"
    assert list(COMPOSE["services"]) == ["db"]


def test_image_pinned_by_minor_tag_and_digest() -> None:
    assert IMAGE_RE.fullmatch(db()["image"])


def test_password_is_mandatory() -> None:
    assert db()["environment"]["POSTGRES_PASSWORD"].startswith("${POSTGRES_PASSWORD:?")


def test_port_bound_to_loopback_only() -> None:
    ports = db()["ports"]
    assert ports and all(str(p).startswith("127.0.0.1:") for p in ports)


def test_healthcheck_uses_pg_isready_over_tcp() -> None:
    check = " ".join(db()["healthcheck"]["test"])
    assert "pg_isready" in check
    assert "-h 127.0.0.1" in check  # the socket-only server of the first init never counts


def test_named_volume_and_read_only_ops_mount() -> None:
    volumes = db()["volumes"]
    assert "pgdata:/var/lib/postgresql/data" in volumes
    assert "pgdata" in COMPOSE["volumes"]
    assert any(v.startswith("./ops/postgres:") and v.endswith(":ro") for v in volumes)


def test_sessions_forced_to_utc() -> None:
    assert db()["environment"]["TZ"] == "UTC"
    assert "timezone=UTC" in db()["command"]


def _config_q(tmp_path: Path, env_file_text: str) -> RunResult:
    project = unique_project("config")
    level = copy_level(tmp_path / "level", project)
    env_file = tmp_path / "compose.env"
    env_file.write_text(env_file_text)
    env = child_env({"COMPOSE_PROJECT_NAME": project})
    return run(
        [tool("docker", optional=True), "compose", "--env-file", str(env_file), "config", "-q"],
        level,
        env,
        timeout=30,
    )


def test_config_q_fails_without_password(tmp_path: Path) -> None:
    result = _config_q(tmp_path, "")
    assert result.rc != 0, result
    assert_contains(result.err, "POSTGRES_PASSWORD", "stderr")
    assert_empty(result.out, "stdout")


def test_config_q_passes_with_password_and_prints_nothing(tmp_path: Path) -> None:
    password = canary("POSTGRES_PASSWORD")
    result = _config_q(tmp_path, f"POSTGRES_PASSWORD={password}\n")
    assert_absent(result.out, password, "stdout")
    assert_absent(result.err, password, "stderr")
    assert result.rc == 0, result
    assert_empty(result.out, "stdout")
