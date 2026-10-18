"""CLI contract KG: modes, exit codes (0 clean, 1 findings, 2 error) and fail-closed."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from support.fakes import GitRepo, add_gitleaks_config, git_repo, msg, require_gitleaks

pytestmark = pytest.mark.git


def _clean_repo(tmp_path: Path) -> GitRepo:
    repo = git_repo(tmp_path)
    repo.write("README.md", "# demo\n")
    repo.write(".gitignore", ".env\n.env.*\n!.env.example\n")
    repo.write(".env.example", "POSTGRES_PASSWORD=__GENERATE__\n")
    repo.commit(msg("init"), "README.md", ".gitignore", ".env.example", add_gitleaks_config(repo))
    return repo


def _fake_gitleaks_path(tmp_path: Path, version: str | None) -> str:
    """A PATH with git (and python via sys.executable) but a fake or no gitleaks."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    git = shutil.which("git")
    assert git
    (bindir / "git").symlink_to(git)
    if version is not None:
        script = bindir / "gitleaks"
        script.write_text(f"#!/bin/sh\necho {version}\n")
        script.chmod(0o755)
    return os.pathsep.join([str(bindir), "/usr/bin", "/bin"])


@pytest.mark.gitleaks
def test_staged_clean_and_dirty(tmp_path: Path) -> None:
    require_gitleaks()
    repo = _clean_repo(tmp_path)
    repo.write("app.py", "x = 1\n")
    repo.git("add", "app.py")
    assert repo.guards("staged").rc == 0
    repo.write(".env", "A=1\n")
    repo.git("add", "-f", ".env")
    res = repo.guards("staged")
    assert res.rc == 1
    assert ("path-env", ".env", "0") in res.findings


@pytest.mark.gitleaks
def test_tree_clean(tmp_path: Path) -> None:
    require_gitleaks()
    repo = _clean_repo(tmp_path)
    res = repo.guards("tree")
    assert res.rc == 0, res
    assert res.stdout == ""


@pytest.mark.gitleaks
def test_gitleaksignore_anywhere_is_rc2(tmp_path: Path) -> None:
    require_gitleaks()
    repo = _clean_repo(tmp_path)
    repo.write("sub/.gitleaksignore", "x\n")
    for mode in (["tree"], ["staged"]):
        res = repo.guards(*mode)
        assert res.rc == 2, mode
        assert ".gitleaksignore" in res.stderr


@pytest.mark.gitleaks
def test_message_modes(tmp_path: Path) -> None:
    require_gitleaks()
    repo = _clean_repo(tmp_path)
    repo.write(".git/OK", msg("fine"))
    repo.write(".git/BAD", "wip\n")
    assert repo.guards("message", ".git/OK").rc == 0
    bad = repo.guards("message", ".git/BAD")
    assert bad.rc == 1
    assert bad.findings == [("msg-format", ":message", "1")]  # never the real path (L11)
    assert repo.guards("message", ".git/MISSING").rc == 2


def test_identity_and_lint_clean(tmp_path: Path) -> None:
    repo = _clean_repo(tmp_path)
    assert repo.guards("identity", "HEAD").rc == 0
    assert repo.guards("lint").rc == 0


def test_lint_finding(tmp_path: Path) -> None:
    repo = _clean_repo(tmp_path)
    repo.write("Makefile", "include .env\n")
    res = repo.guards("lint")
    assert res.rc == 1
    assert res.findings == [("make-include-env", "Makefile", "1")]


@pytest.mark.parametrize(
    "args",
    [[], ["bogus"], ["range", "origin"], ["identity", "--output=x"], ["identity", "nope..HEAD"], ["message"]],
)
def test_usage_errors_are_rc2(tmp_path: Path, args: list[str]) -> None:
    repo = _clean_repo(tmp_path)
    assert repo.guards(*args).rc == 2


def test_outside_a_repo_is_rc2(tmp_path: Path) -> None:
    d = tmp_path / "plain"
    d.mkdir()
    from support.fakes import guards

    assert guards(["lint"], d, home=tmp_path / ".home").rc == 2


@pytest.mark.parametrize("mode", [["staged"], ["tree"]])
def test_gitleaks_missing_fails_closed(tmp_path: Path, mode: list[str]) -> None:
    repo = _clean_repo(tmp_path)
    res = repo.guards(*mode, path=_fake_gitleaks_path(tmp_path, None))
    assert res.rc == 2
    assert "gitleaks" in res.stderr


@pytest.mark.parametrize("mode", [["staged"], ["tree"]])
def test_gitleaks_wrong_version_fails_closed(tmp_path: Path, mode: list[str]) -> None:
    repo = _clean_repo(tmp_path)
    res = repo.guards(*mode, path=_fake_gitleaks_path(tmp_path, "8.29.0"))
    assert res.rc == 2
    assert "8.30.1" in res.stderr


def test_message_mode_fails_closed_without_gitleaks(tmp_path: Path) -> None:
    repo = _clean_repo(tmp_path)
    repo.write(".git/OK", msg("fine"))
    assert repo.guards("message", ".git/OK", path=_fake_gitleaks_path(tmp_path, None)).rc == 2


def test_clean_env_tripwire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from support.fakes import FORBIDDEN_GIT_VARS, clean_env

    for var in FORBIDDEN_GIT_VARS:
        monkeypatch.setenv(var, str(tmp_path / "elsewhere"))
    env = clean_env(tmp_path / ".home", extra={"GIT_DIR": "x"})
    for var in FORBIDDEN_GIT_VARS:
        assert var not in env
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["LC_ALL"] == "C"
    assert env["TZ"] == "UTC"


# --------------------------------------------------------------------------- config fail-closed


@pytest.mark.gitleaks
def test_config_only_in_work_tree_after_first_commit_is_rc2(tmp_path: Path) -> None:
    """With commits, the work-tree config is never trusted: it must be in the index."""
    require_gitleaks()
    repo = git_repo(tmp_path)
    repo.write("README.md", "# demo\n")
    repo.commit(msg("init"), "README.md")
    add_gitleaks_config(repo)
    repo.write("app.txt", "x\n")
    repo.git("add", "app.txt")
    res = repo.guards("staged")
    assert res.rc == 2
    assert ".gitleaks.toml" in res.stderr


@pytest.mark.gitleaks
def test_bootstrap_uses_work_tree_config_without_commits(tmp_path: Path) -> None:
    require_gitleaks()
    repo = git_repo(tmp_path)
    add_gitleaks_config(repo)
    repo.write("app.txt", "x\n")
    repo.git("add", "app.txt")
    assert repo.guards("staged").rc == 0
    assert repo.guards("tree").rc == 0
    assert repo.guards("history").rc == 0


@pytest.mark.gitleaks
def test_no_config_anywhere_is_rc2(tmp_path: Path) -> None:
    require_gitleaks()
    repo = git_repo(tmp_path)
    repo.write("app.txt", "x\n")
    repo.git("add", "app.txt")
    assert repo.guards("staged").rc == 2


@pytest.mark.gitleaks
def test_range_tip_without_config_is_rc2(tmp_path: Path) -> None:
    require_gitleaks()
    repo = _clean_repo(tmp_path)
    base = repo.head()
    repo.git("rm", "-q", ".gitleaks.toml")
    repo.commit(msg("drop config"))
    res = repo.guards("range", "origin", base, "HEAD")
    assert res.rc == 2
    assert ".gitleaks.toml" in res.stderr


def test_doctor_usage_error_is_rc2(tmp_path: Path) -> None:
    repo = _clean_repo(tmp_path)
    assert repo.guards("doctor", "extra").rc == 2
