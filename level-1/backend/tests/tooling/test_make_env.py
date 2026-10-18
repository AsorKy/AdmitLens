"""A8: `make env` (ops/make-env.sh) per ADR-0010 §2.5 and PLAN-CA-0005 §6.4.

Values are checked with ``assert_absent`` first; raw output is only ever searched
through the helpers, which fail without printing it.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest
from tooling_support import (
    KE_NAMES,
    LEVEL_DIR,
    RunResult,
    assert_absent,
    assert_contains,
    assert_recipe_failed,
    child_env,
    copy_level,
    fingerprint,
    make,
    read_env_file,
    run,
    skip_if_nested,
    tool,
)

pytestmark = pytest.mark.tooling

SCRIPT = LEVEL_DIR / "ops" / "make-env.sh"
EXAMPLE = LEVEL_DIR / ".env.example"
HEX48 = re.compile(r"[0-9a-f]{48}")
VOLUME_HINT = "run make db-down first"
SAFE_DEFAULTS = ("POSTGRES_USER", "POSTGRES_DB", "ADMITLENS_DB_PORT")


def make_env(
    workdir: Path,
    example: Path = EXAMPLE,
    *,
    env: dict[str, str] | None = None,
    umask: int = -1,
) -> RunResult:
    return run(
        [tool("sh"), str(SCRIPT), str(example), str(workdir / ".env")],
        workdir,
        env,
        umask=umask,
        timeout=20,
    )


def leftovers(workdir: Path) -> list[str]:
    return sorted(p.name for p in workdir.iterdir() if p.name.startswith(".env"))


def fake_bin(tmp_path: Path, openssl_body: str | None) -> Path:
    """A PATH with the tools make-env.sh uses; openssl is a stub (or absent if None)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("mktemp", "chmod", "ln", "rm"):
        (bindir / name).symlink_to(tool(name))
    if openssl_body is not None:
        stub = bindir / "openssl"
        stub.write_text(f"#!{tool('sh')}\n{openssl_body}\n")
        stub.chmod(0o755)
    return bindir


def test_example_has_exactly_the_ke_names() -> None:
    values = read_env_file(EXAMPLE)
    assert tuple(values) == KE_NAMES
    assert values["POSTGRES_PASSWORD"] == "__GENERATE__"


def test_writes_mode_600_even_with_umask_000(tmp_path: Path) -> None:
    result = make_env(tmp_path, umask=0)
    assert result.rc == 0, result
    mode = stat.S_IMODE((tmp_path / ".env").stat().st_mode)
    assert mode == 0o600, oct(mode)
    assert leftovers(tmp_path) == [".env"]


def test_prints_names_only(tmp_path: Path) -> None:
    result = make_env(tmp_path)
    values = read_env_file(tmp_path / ".env")
    for name, value in values.items():
        if name in SAFE_DEFAULTS:
            continue  # short public defaults, not secrets
        windows = name == "POSTGRES_PASSWORD"  # URLs share public prefixes with the text
        assert_absent(result.out, value, "stdout", windows=windows)
        assert_absent(result.err, value, "stderr", windows=windows)
    assert result.rc == 0, result
    for name in KE_NAMES:
        assert_contains(result.out, name, "stdout")
    assert_contains(result.out, VOLUME_HINT, "stdout")


def test_generates_48_hex_distinct_between_runs(tmp_path: Path) -> None:
    prints = set()
    for i in range(2):
        work = tmp_path / str(i)
        work.mkdir()
        assert make_env(work).rc == 0
        password = read_env_file(work / ".env")["POSTGRES_PASSWORD"]
        if not HEX48.fullmatch(password):
            pytest.fail("generated password is not 48 lowercase hex characters", pytrace=False)
        prints.add(fingerprint(password))
    assert len(prints) == 2


def test_composes_the_urls_in_the_shell(tmp_path: Path) -> None:
    assert make_env(tmp_path).rc == 0
    v = read_env_file(tmp_path / ".env")
    base = (
        f"postgresql+psycopg://{v['POSTGRES_USER']}:{v['POSTGRES_PASSWORD']}"
        f"@127.0.0.1:{v['ADMITLENS_DB_PORT']}/"
    )
    # Compare by fingerprint so a failure never prints the password.
    assert fingerprint(v["ADMITLENS_DATABASE_URL"]) == fingerprint(base + v["POSTGRES_DB"])
    assert fingerprint(v["ADMITLENS_TEST_DATABASE_URL"]) == fingerprint(base + "admitlens_test")
    for name, value in v.items():
        if "${" in value or "__GENERATE" in value:
            pytest.fail(f"{name} still has a placeholder", pytrace=False)


def test_refuses_to_overwrite_and_keeps_bytes_and_mtime(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("KEEP=me\n")
    os.utime(target, (1_000_000_000, 1_000_000_000))
    before = target.stat().st_mtime_ns
    result = make_env(tmp_path)
    assert result.rc == 1, result
    assert_contains(result.err, "already exists", "stderr")
    assert_contains(result.err, VOLUME_HINT, "stderr")
    assert target.read_text() == "KEEP=me\n"
    assert target.stat().st_mtime_ns == before
    assert leftovers(tmp_path) == [".env"]


def test_refuses_a_dangling_symlink(tmp_path: Path) -> None:
    (tmp_path / ".env").symlink_to(tmp_path / "nowhere")
    assert make_env(tmp_path).rc == 1
    assert not (tmp_path / "nowhere").exists()


@pytest.mark.parametrize(
    "bad_tail",
    [
        "not an assignment",
        "lower=case",
        "POSTGRES_USER=twice",
        "URL=${UNDEFINED}",
        "URL=${bad-ref}",
        "TYPO=__GENERATE_",
        "CRLF=value\r",
        "ACCENT_É=x",
        "É=x",
    ],
    ids=["no-equals", "lowercase", "duplicate", "undefined-ref", "bad-ref", "placeholder",
         "carriage-return", "non-ascii-name", "non-ascii-only"],
)  # fmt: skip
def test_invalid_example_never_leaves_a_partial_env(tmp_path: Path, bad_tail: str) -> None:
    example = tmp_path / "example"
    example.write_bytes(
        ("POSTGRES_USER=u\nPOSTGRES_PASSWORD=__GENERATE__\n" + bad_tail + "\n").encode()
    )
    # A UTF-8 locale must not let lowercase or non-ASCII names through.
    result = make_env(
        tmp_path, example, env=child_env({"LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"})
    )
    assert result.rc == 2, result
    assert leftovers(tmp_path) == []


def test_without_openssl_fails_closed(tmp_path: Path) -> None:
    bindir = fake_bin(tmp_path, None)
    work = tmp_path / "w"
    work.mkdir()
    result = make_env(work, env=child_env({"PATH": str(bindir)}))
    assert result.rc == 2, result
    assert_contains(result.err, "openssl is required", "stderr")
    assert leftovers(work) == []


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("short", "printf '%s\\n' 0123456789abcdef"),
        ("uppercase", "printf '%s\\n' 0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF"),
        ("not-hex", "printf '%s\\n' zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"),
        ("fails", "exit 1"),
    ],
)
def test_bad_openssl_output_fails_closed(tmp_path: Path, label: str, body: str) -> None:
    bindir = fake_bin(tmp_path, body)
    work = tmp_path / "w"
    work.mkdir()
    result = make_env(work, env=child_env({"PATH": str(bindir)}))
    assert result.rc == 2, (label, result)
    assert leftovers(work) == []


def test_make_env_target_creates_once_then_refuses(tmp_path: Path) -> None:
    skip_if_nested()
    level = copy_level(tmp_path / "level")
    first = make("env", level)
    assert first.rc == 0, first
    assert (level / ".env").exists()
    second = make("env", level)
    assert second.rc != 0, second
    assert_contains(second.err, "already exists", "stderr")
    assert_recipe_failed(second.err, "env", 1)  # the script's own code, not make's
