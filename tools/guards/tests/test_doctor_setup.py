"""``make setup`` and ``make doctor`` on throwaway repos.

``doctor`` must fail in every broken state, pass after ``setup``, never print a
value (e-mail, name), and ``setup`` must be idempotent and write nothing outside
the temporary repo's ``.git/config`` (HOME is a temporary directory).
"""

from __future__ import annotations

import os
import platform as gl_platform
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from support.fakes import (
    GUARDS_ROOT,
    REPO_ROOT,
    GitRepo,
    RunResult,
    Secret,
    assert_absent_all,
    email,
    git_repo,
    make_fake,
    require_gitleaks,
    run,
)
from support.wired import WiredRepo, bare_wired_repo

from guards.findings import GuardError
from guards.gitleaks import binary_ok, binary_pin, check_version, file_sha256, platform_key

pytestmark = [pytest.mark.make, pytest.mark.git]


@pytest.fixture
def fresh(tmp_path: Path, uv_cache: Path) -> WiredRepo:
    require_gitleaks()
    return bare_wired_repo(tmp_path, uv_cache)


def _setup(repo: WiredRepo) -> RunResult:
    res = repo.make("setup")
    assert res.rc == 0, res
    return res


def _lines(res: RunResult) -> dict[str, str]:
    """check name -> status, from doctor's ``<status> <check>`` lines."""
    out = {}
    for line in res.stdout.splitlines():
        status, _, name = line.partition(" ")
        if status in ("ok", "warn", "FAIL"):
            out[name.strip()] = status
    return out


def _snapshot(home: Path) -> dict[str, bytes]:
    return {str(p.relative_to(home)): (p.read_bytes() if p.is_file() else b"<dir>") for p in sorted(home.rglob("*"))}


def _fake_gitleaks(tmp_path: Path, version: str) -> str:
    script = tmp_path / "fake-gitleaks"
    script.write_text(f"#!/bin/sh\necho {version}\n")
    script.chmod(0o755)
    return str(script)


def test_doctor_fails_before_setup(fresh: WiredRepo) -> None:
    res = fresh.make("doctor")
    assert res.rc != 0
    assert _lines(res)["git config core.hooksPath"] == "FAIL"


def test_doctor_passes_after_setup(fresh: WiredRepo) -> None:
    _setup(fresh)
    res = fresh.make("doctor")
    assert res.rc == 0, res
    lines = _lines(res)
    assert "FAIL" not in lines.values()
    assert lines[".guards.local.toml present (real-name checks are off)"] == "warn"
    assert len(lines) >= 15


def test_setup_is_idempotent_and_leaves_home_untouched(fresh: WiredRepo) -> None:
    fresh.git("status", "-s")  # make sure HOME exists before the snapshot
    home_before = _snapshot(fresh.home)
    _setup(fresh)
    config_once = (fresh.root / ".git" / "config").read_bytes()
    _setup(fresh)
    assert (fresh.root / ".git" / "config").read_bytes() == config_once
    assert fresh.make("doctor").rc == 0
    assert _snapshot(fresh.home) == home_before


def test_setup_never_touches_user_email_and_warns_by_count(fresh: WiredRepo) -> None:
    other = Secret("personal-email", email("dev", "corp.example.org"))
    fresh.git("config", "user.email", other.reveal())
    res = _setup(fresh)
    assert "user.email is not a GitHub noreply address" in res.stdout
    assert fresh.git("config", "user.email").stdout.strip() == other.reveal()
    assert_absent_all(res, [other], "setup-email")


Breaker = Callable[[WiredRepo, Path], dict[str, str]]


def _no_hooks_path(repo: WiredRepo, _: Path) -> dict[str, str]:
    repo.git("config", "--unset", "core.hooksPath")
    return {}


def _wrong_hooks_path(repo: WiredRepo, _: Path) -> dict[str, str]:
    repo.git("config", "core.hooksPath", ".git/hooks")
    return {}


def _missing_hook(repo: WiredRepo, _: Path) -> dict[str, str]:
    (repo.root / ".githooks" / "pre-push").unlink()
    return {}


def _non_executable_hook(repo: WiredRepo, _: Path) -> dict[str, str]:
    os.chmod(repo.root / ".githooks" / "commit-msg", 0o644)
    return {}


def _wrong_gitleaks(_: WiredRepo, tmp_path: Path) -> dict[str, str]:
    return {"GITLEAKS": _fake_gitleaks(tmp_path, "8.29.0")}


def _missing_gitleaks(_: WiredRepo, tmp_path: Path) -> dict[str, str]:
    return {"GITLEAKS": str(tmp_path / "absent")}


def _gitleaksignore(repo: WiredRepo, _: Path) -> dict[str, str]:
    repo.write("sub/.gitleaksignore", "x\n")
    return {}


def _non_noreply_email(repo: WiredRepo, _: Path) -> dict[str, str]:
    repo.git("config", "user.email", email("dev", "corp.example.org"))
    return {}


def _real_name(repo: WiredRepo, _: Path) -> dict[str, str]:
    name = make_fake("real-name", "doctor")
    repo.write(".guards.local.toml", f'[private]\nreal_names = ["{name.reveal()}"]\n')
    repo.git("config", "user.name", name.reveal())
    return {}


def _no_use_config_only(repo: WiredRepo, _: Path) -> dict[str, str]:
    repo.git("config", "user.useConfigOnly", "false")
    return {}


BROKEN: list[tuple[str, Breaker, str]] = [
    ("no-hooks-path", _no_hooks_path, "git config core.hooksPath"),
    ("wrong-hooks-path", _wrong_hooks_path, "git config core.hooksPath"),
    ("missing-hook", _missing_hook, "hook pre-push present"),
    ("non-executable-hook", _non_executable_hook, "hook commit-msg executable"),
    ("gitleaks-wrong-version", _wrong_gitleaks, "gitleaks 8.30.1 at the configured path"),
    ("gitleaks-missing", _missing_gitleaks, "gitleaks 8.30.1 at the configured path"),
    ("gitleaksignore", _gitleaksignore, "no .gitleaksignore"),
    ("non-noreply-email", _non_noreply_email, "user.email is a GitHub noreply address"),
    ("real-name", _real_name, "user.name is not a private real name"),
    ("use-config-only-off", _no_use_config_only, "git config user.useConfigOnly"),
]


@pytest.mark.parametrize(("case", "breaker", "check"), BROKEN, ids=[b[0] for b in BROKEN])
def test_doctor_fails_in_broken_state(fresh: WiredRepo, tmp_path: Path, case: str, breaker: Breaker, check: str) -> None:
    _setup(fresh)
    env = breaker(fresh, tmp_path)
    res = fresh.make("doctor", env=env)
    assert res.rc != 0, case
    assert _lines(res).get(check) == "FAIL", case
    # Values never appear: neither the e-mail nor the name set by the breaker.
    for key in ("user.email", "user.name"):
        value = fresh.git("config", key).stdout.strip()
        assert_absent_all(res, [Secret(key, value)], f"doctor-{case}")


# --------------------------------------------------------------------------- doctor without make (W9-R1a)
# These call ``python -m guards doctor`` directly on a plain throwaway repo, so they
# do not depend on the Makefile; they assert single check lines only.


def _doctor_lines(repo: GitRepo) -> dict[str, str]:
    return _lines(repo.guards("doctor"))


@pytest.fixture(scope="module")
def broken_doctor(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict[str, str], dict[str, str]]:
    """(lines with every R1a breaker applied, lines of a repo with none of them)."""
    bad = git_repo(tmp_path_factory.mktemp("doctor-bad"))
    bad.git("config", "push.default", "matching")
    bad.git("config", "push.followTags", "true")
    bad.git("config", "status.showUntrackedFiles", "no")
    bad.git("config", "core.autocrlf", "true")
    bad.write(".git/info/attributes", "*.txt -diff\n")
    bad.write(".guards.local.toml", "[private]\nreal_names = []\n")
    good = git_repo(tmp_path_factory.mktemp("doctor-good"))
    good.write(".git/info/attributes", "\n")  # present but empty is fine
    return _doctor_lines(bad), _doctor_lines(good)


@pytest.mark.parametrize(
    ("check", "status"),
    [
        ("git config push.default", "FAIL"),
        ("git config push.followTags", "FAIL"),
        ("git config status.showUntrackedFiles", "warn"),
        ("git config core.autocrlf", "warn"),
        ("no .git/info/attributes", "FAIL"),
        (".guards.local.toml real_names filled in (real-name checks are off)", "warn"),
    ],
)
def test_doctor_r1a_breakers(broken_doctor: tuple[dict[str, str], dict[str, str]], check: str, status: str) -> None:
    assert broken_doctor[0].get(check) == status


def test_doctor_r1a_clean_states(broken_doctor: tuple[dict[str, str], dict[str, str]]) -> None:
    good = broken_doctor[1]
    assert good["no .git/info/attributes"] == "ok"
    assert good[".guards.local.toml present (real-name checks are off)"] == "warn"


# --------------------------------------------------------------------------- binary pin (L9, W9-R2 L4)


def test_binary_ok_requires_the_versioned_pin(tmp_path: Path) -> None:
    require_gitleaks()
    real = REPO_ROOT / ".tools" / "bin" / "gitleaks"
    assert binary_ok(real)  # the installed binary matches the versioned pin for this platform
    fake = tmp_path / "gitleaks"
    fake.write_bytes(b"#!/bin/sh\necho 8.30.1\n")
    fake.chmod(0o755)
    # A local record next to the binary no longer counts: only the versioned pin does.
    (tmp_path / "gitleaks.sha256").write_text(file_sha256(fake) + "\n")
    assert not binary_ok(fake)
    other = "linux_x64" if platform_key() == "darwin_arm64" else "darwin_arm64"
    assert binary_pin(other) != binary_pin()
    assert not binary_ok(real, plat=other)  # another platform's pin never matches


def test_installer_is_idempotent_on_a_pinned_binary(tmp_path: Path) -> None:
    """With the pinned binary in place the installer exits 0 without touching the network."""
    require_gitleaks()
    dest = tmp_path / "bin" / "gitleaks"
    dest.parent.mkdir()
    shutil.copy2(REPO_ROOT / ".tools" / "bin" / "gitleaks", dest)
    no_net = _no_curl(tmp_path)
    res = run(["sh", str(GUARDS_ROOT / "bin" / "install-gitleaks.sh"), str(dest)], tmp_path, path=no_net)
    assert res.rc == 0, res
    assert "installed" in res.stdout


def test_installer_does_not_accept_a_tampered_binary(tmp_path: Path) -> None:
    """A binary that differs from the pin is reinstalled (here: download refused, rc 2), never kept."""
    dest = tmp_path / "bin" / "gitleaks"
    dest.parent.mkdir()
    dest.write_bytes(b"#!/bin/sh\necho 8.30.1\n")
    dest.chmod(0o755)
    Path(str(dest) + ".sha256").write_text(file_sha256(dest) + "\n")  # a forged local record
    res = run(["sh", str(GUARDS_ROOT / "bin" / "install-gitleaks.sh"), str(dest)], tmp_path, path=_no_curl(tmp_path))
    assert res.rc == 2, res
    assert "download" in res.stderr


def _no_curl(tmp_path: Path) -> str:
    """A PATH whose ``curl`` always fails, so no test reaches the network."""
    stub = tmp_path / "stub"
    stub.mkdir()
    curl = stub / "curl"
    curl.write_text("#!/bin/sh\nexit 22\n")
    curl.chmod(0o755)
    return f"{stub}:/usr/bin:/bin"


@pytest.mark.gitleaks
def test_tampered_binary_is_rc2(tmp_path: Path) -> None:
    require_gitleaks()
    repo = git_repo(tmp_path)
    repo.write(".gitleaks.toml", (REPO_ROOT / ".gitleaks.toml").read_bytes())
    tampered = tmp_path / "gl" / "gitleaks"
    tampered.parent.mkdir()
    shutil.copy2(REPO_ROOT / ".tools" / "bin" / "gitleaks", tampered)
    with tampered.open("ab") as fh:
        fh.write(b"\0")
    res = repo.guards("tree", extra_env={"GUARDS_GITLEAKS": str(tampered)})
    assert res.rc == 2
    assert "make setup" in res.stderr


def test_unsupported_platform_is_rc2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gl_platform, "system", lambda: "Plan9")
    with pytest.raises(GuardError, match="unsupported platform"):
        check_version(str(REPO_ROOT / ".tools" / "bin" / "gitleaks"))
    assert not binary_ok(REPO_ROOT / ".tools" / "bin" / "gitleaks")
