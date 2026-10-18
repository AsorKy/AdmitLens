"""No guard output channel ever carries a matched value.

Each test plants fakes, runs a mode end to end, and checks stdout and stderr
with ``assert_absent`` (full value and every 10-char window).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from support.fakes import (
    Secret,
    add_gitleaks_config,
    assert_absent_all,
    git_repo,
    make_fake,
    msg,
    require_gitleaks,
)

pytestmark = pytest.mark.git


def _markers_toml(name: Secret, mail: Secret, vault: Secret) -> str:
    return (
        "[private]\n"
        f'real_names = ["{name.reveal()}"]\n'
        f'personal_emails = ["{mail.reveal()}"]\n'
        f'paths = ["{vault.reveal()}"]\n'
    )


def _plant(tmp_path: Path, seed: str):  # noqa: ANN202
    repo = git_repo(tmp_path)
    pat = make_fake("github-pat", seed)
    name = make_fake("real-name", seed)
    mail = make_fake("personal-email", seed)
    vault = make_fake("private-path", seed)
    repo.write(".guards.local.toml", _markers_toml(name, mail, vault))
    repo.write(".gitignore", "*.local.toml\n")
    add_gitleaks_config(repo)  # no commits yet: the work-tree copy is used (bootstrap)
    repo.write(
        "config/app.txt",
        f"owner = {name.reveal()}\ncontact = {mail.reveal()}\nnotes = {vault.reveal()}\ntoken = {pat.reveal()}\n",
    )
    return repo, [pat, name, mail, vault]


@pytest.mark.gitleaks
def test_tree_output_has_no_values(tmp_path: Path) -> None:
    require_gitleaks()
    repo, fakes = _plant(tmp_path, "tree")
    repo.git("add", ".gitignore", "config/app.txt")
    res = repo.guards("tree")
    assert res.rc == 1
    assert {"github-pat", "content-private-marker", "content-email"} <= res.rules
    for f in res.findings:
        assert len(f) in (3, 4)
        assert f[1] == "config/app.txt"
        assert f[2].isdigit()
    assert_absent_all(res, fakes, "tree")


@pytest.mark.gitleaks
def test_staged_output_has_no_values(tmp_path: Path) -> None:
    require_gitleaks()
    repo, fakes = _plant(tmp_path, "staged")
    repo.git("add", ".gitignore", "config/app.txt")
    res = repo.guards("staged")
    assert res.rc == 1
    assert {"github-pat", "content-private-marker"} <= res.rules
    assert_absent_all(res, fakes, "staged")


@pytest.mark.gitleaks
def test_message_output_has_no_values(tmp_path: Path) -> None:
    require_gitleaks()
    repo, fakes = _plant(tmp_path, "message")
    pat, name, mail, _ = fakes
    repo.write(".git/MSG", msg(f"by {name.reveal()}") + f"token {pat.reveal()}\nping {mail.reveal()}\n")
    res = repo.guards("message", ".git/MSG")
    assert res.rc == 1
    assert {"github-pat", "msg-private-marker", "msg-email"} <= res.rules
    assert_absent_all(res, fakes, "message")


def test_identity_output_has_no_values(tmp_path: Path) -> None:
    repo, fakes = _plant(tmp_path, "identity")
    _, name, mail, _ = fakes
    base = repo.commit(msg("base"))
    repo.commit(
        msg("bad author"),
        extra_env={"GIT_AUTHOR_NAME": name.reveal(), "GIT_AUTHOR_EMAIL": mail.reveal()},
    )
    res = repo.guards("identity", f"{base}..HEAD")
    assert res.rc == 1
    assert {"identity-email", "identity-name"} <= res.rules
    assert_absent_all(res, fakes, "identity")


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode-000 files")
def test_unreadable_file_error_has_no_values(tmp_path: Path) -> None:
    repo, fakes = _plant(tmp_path, "unreadable")
    pat = fakes[0]
    repo.write("Makefile", f"TOKEN_HINT = {pat.reveal()}\n")
    repo.git("add", "Makefile")
    target = repo.root / "Makefile"
    target.chmod(0)
    try:
        res = repo.guards("lint")
    finally:
        target.chmod(0o600)
    assert res.rc == 2
    assert "Makefile" in res.stderr
    assert_absent_all(res, fakes, "unreadable")


def test_bad_markers_toml_error_has_no_values(tmp_path: Path) -> None:
    repo = git_repo(tmp_path)
    mail = make_fake("personal-email", "badtoml")
    repo.write(".guards.local.toml", f'[private]\npersonal_emails = ["{mail.reveal()}"\nreal_names = 3 3\n')
    res = repo.guards("lint")
    assert res.rc == 2
    assert ".guards.local.toml" in res.stderr
    assert_absent_all(res, [mail], "bad-toml")


def test_bad_markers_types_error_has_no_values(tmp_path: Path) -> None:
    repo = git_repo(tmp_path)
    mail = make_fake("personal-email", "badtype")
    repo.write(".guards.local.toml", f'[private]\npersonal_emails = "{mail.reveal()}"\n')
    res = repo.guards("tree")
    assert res.rc == 2
    assert_absent_all(res, [mail], "bad-toml-type")


def test_bad_yaml_finding_has_no_values(tmp_path: Path) -> None:
    repo = git_repo(tmp_path)
    pat = make_fake("github-pat", "yaml")
    repo.write(".github/workflows/x.yml", f"on: [push\njobs:\n  a: {{ token: {pat.reveal()} ]\n")
    repo.git("add", ".github/workflows/x.yml")
    res = repo.guards("lint")
    assert res.rc == 1
    assert res.rules == {"wf-yaml-invalid"}
    assert_absent_all(res, [pat], "bad-yaml")
