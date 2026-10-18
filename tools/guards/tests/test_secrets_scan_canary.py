"""``make secrets-scan`` is armed: work tree + every commit on every ref.

Make itself always exits 2 when a recipe fails, so the recipe's own code is
read from make's ``Error N`` line: 1 = findings, 2 = error or missing tool.
GNU Make 3.81 prints ``make: *** [secrets-scan] Error 1``; 4.x prints
``make: *** [Makefile:NN: secrets-scan] Error 1``; both are accepted.
Each engine must be able to fail the scan on its own.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from support.fakes import RunResult, assert_absent_all, make_fake, msg
from support.wired import WiredRepo, bare_wired_repo

pytestmark = [pytest.mark.git, pytest.mark.gitleaks, pytest.mark.make]


def scan(repo: WiredRepo, env: dict[str, str] | None = None) -> RunResult:
    return repo.make("secrets-scan", env=env)


RECIPE_ERROR = re.compile(r"\[(?:[^\]]*: )?secrets-scan\] Error (\d+)")
GITLEAKS_MISSING = "gitleaks not found at the configured path"


def parse_recipe_rc(rc: int, stderr: str) -> int:
    if rc == 0:
        return 0
    m = RECIPE_ERROR.search(stderr)
    return int(m.group(1)) if m else -1


def recipe_rc(res: RunResult) -> int:
    return parse_recipe_rc(res.rc, res.stderr)


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        ("make: *** [secrets-scan] Error 1\n", 1),  # GNU Make 3.81 (macOS)
        ("make: *** [secrets-scan] Error 2\n", 2),
        ("make: *** [Makefile:58: secrets-scan] Error 1\n", 1),  # GNU Make 4.x (ubuntu-latest)
        ("make: *** [Makefile:58: secrets-scan] Error 2\n", 2),
        ("make: *** [Makefile:9: other] Error 1\n", -1),
    ],
)
def test_recipe_rc_parses_make_381_and_4x(stderr: str, code: int) -> None:
    assert parse_recipe_rc(2, stderr) == code


def commit(repo: WiredRepo, message: str, *paths: str) -> str:
    if paths:
        repo.git("add", "-f", "--", *paths)
    repo.git("commit", "-q", "--allow-empty", "--no-verify", "-F", "-", input=message)
    return repo.head()


def test_clean_repo_with_one_commit_passes(wired: WiredRepo) -> None:
    res = scan(wired)
    assert res.rc == 0, res
    assert res.stdout == ""


def test_repo_with_no_commits_passes(tmp_path: Path, uv_cache: Path) -> None:
    repo = bare_wired_repo(tmp_path, uv_cache)
    res = scan(repo)
    assert res.rc == 0, res


def test_planted_fake_fails_and_only_gitleaks_sees_it(wired: WiredRepo) -> None:
    """A GitHub token shape is a gitleaks built-in rule; the Python guard has none."""
    pat = make_fake("github-pat", "planted")
    wired.write("config/settings.txt", f"url = {pat.reveal()}\n")
    sha = commit(wired, msg("plant"), "config/settings.txt")
    res = scan(wired)
    assert recipe_rc(res) == 1, res
    assert res.rules == {"github-pat"}
    assert ("github-pat", "config/settings.txt", "1", sha) in res.findings
    assert_absent_all(res, [pat], "scan-planted")


def test_planted_dsn_fails_with_the_custom_gitleaks_rule(wired: WiredRepo) -> None:
    dsn = make_fake("dsn-password", "planted")
    wired.write("config/settings.txt", f"url = {dsn.reveal()}\n")
    sha = commit(wired, msg("plant"), "config/settings.txt")
    res = scan(wired)
    assert recipe_rc(res) == 1, res
    assert ("admitlens-dsn-password", "config/settings.txt", "1", sha) in res.findings
    assert_absent_all(res, [dsn], "scan-planted-dsn")


def test_planted_then_deleted_is_still_found_in_history(wired: WiredRepo) -> None:
    dsn = make_fake("dsn-password", "deleted")
    wired.write("config/settings.txt", f"url = {dsn.reveal()}\n")
    planted = commit(wired, msg("plant"), "config/settings.txt")
    wired.git("rm", "-q", "config/settings.txt")
    commit(wired, msg("delete"))
    res = scan(wired)
    assert recipe_rc(res) == 1, res
    assert {f[3] for f in res.findings if len(f) == 4} == {planted}
    assert_absent_all(res, [dsn], "scan-deleted")


def test_fake_only_in_merge_is_found(wired: WiredRepo) -> None:
    pat = make_fake("github-pat", "scan-merge")
    wired.git("checkout", "-q", "-b", "side")
    wired.write("side.txt", "side\n")
    commit(wired, msg("side"), "side.txt")
    wired.git("checkout", "-q", "main")
    wired.write("main.txt", "main\n")
    commit(wired, msg("main"), "main.txt")
    wired.git("merge", "-q", "--no-ff", "--no-commit", "side")
    wired.write("merged.txt", f"token {pat.reveal()}\n")
    merge = commit(wired, "Merge branch 'side'\n", "merged.txt")
    wired.git("rm", "-q", "merged.txt")
    commit(wired, msg("clean up"))
    res = scan(wired)
    assert recipe_rc(res) == 1, res
    assert ("github-pat", "merged.txt") in {(f[0], f[1]) for f in res.findings if len(f) == 4 and f[3] == merge}
    assert_absent_all(res, [pat], "scan-merge")


def test_fake_only_in_commit_message_is_found(wired: WiredRepo) -> None:
    pat = make_fake("aws-access-token", "scan-msg")
    sha = commit(wired, msg("leaky") + f"key {pat.reveal()}\n")
    res = scan(wired)
    assert recipe_rc(res) == 1, res
    assert [f for f in res.findings if f[0] == "aws-access-token" and f[1] == ":message" and f[3] == sha]
    assert_absent_all(res, [pat], "scan-msg")


def test_fake_only_in_annotated_tag_message_is_found(wired: WiredRepo) -> None:
    pat = make_fake("github-pat", "scan-tag")
    wired.git("tag", "-a", "v0", "-F", "-", input=f"release {pat.reveal()}\n")
    res = scan(wired)
    assert recipe_rc(res) == 1, res
    assert [f for f in res.findings if f[0] == "github-pat" and f[1] == ":tag-message"]
    assert_absent_all(res, [pat], "scan-tag")


def test_finding_only_the_python_guard_sees(wired: WiredRepo) -> None:
    """gitleaks' default allowlist skips any path containing ``gitleaks.toml``:
    a secret there is invisible to gitleaks, but the path itself is a finding."""
    pat = make_fake("github-pat", "shadow")
    wired.write("notes/gitleaks.toml", f'value = "{pat.reveal()}"\n')  # no secret-named key
    commit(wired, msg("shadow"), "notes/gitleaks.toml")
    res = scan(wired)
    assert recipe_rc(res) == 1, res
    assert res.rules == {"gitleaks-config-shadow"}
    assert_absent_all(res, [pat], "scan-shadow")


def test_gitleaks_missing_fails_closed(wired: WiredRepo, tmp_path: Path) -> None:
    res = scan(wired, env={"GITLEAKS": str(tmp_path / "absent")})
    assert recipe_rc(res) == 2, res
    assert GITLEAKS_MISSING in res.stderr


def test_gitleaksignore_is_rc2(wired: WiredRepo) -> None:
    wired.write(".gitleaksignore", "x\n")
    res = scan(wired)
    assert recipe_rc(res) == 2, res


def test_config_missing_from_head_is_rc2(wired: WiredRepo) -> None:
    wired.git("rm", "-q", "--cached", ".gitleaks.toml")
    commit(wired, msg("drop config"))
    res = scan(wired)
    assert recipe_rc(res) == 2, res
    assert ".gitleaks.toml" in res.stderr
