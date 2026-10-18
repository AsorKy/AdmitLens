"""The scan engine on real throwaway repositories (W9-R1a fixes).

* H1: gitleaks no longer reads ``git log -p``, so ``.gitattributes`` and
  ``color.*`` / ``git -c`` overrides cannot blind ``staged``, ``range`` or
  ``history``.
* H3: ``history`` does not enforce the commit format (Dependabot); ``prepush`` does.
* M1(b): a weakened ``.gitleaks.toml`` is scanned together with the base one.
* M2: ``path-ignored`` mirrors ``.gitignore`` (``git add -f`` is caught).
* L3/L4/L5/L6: unmapped stdin hits, missing blobs, forged remote refs, symlinks.

Repositories are built once per module and each mode runs once per scenario;
the tests assert exact (rule, path) pairs on the cached results.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from support.fakes import (
    GitRepo,
    RunResult,
    Secret,
    add_gitleaks_config,
    add_real_gitignore,
    assert_absent_all,
    email,
    git_repo,
    make_fake,
    msg,
    require_gitleaks,
)

from guards.cli import _map_text_hits
from guards.findings import Finding

pytestmark = [pytest.mark.git, pytest.mark.gitleaks]

ZERO = "0" * 40
MODES = ("staged", "range", "history")


def _base_repo(root: Path) -> tuple[GitRepo, str]:
    repo = git_repo(root)
    repo.write("README.md", "# demo\n")
    base = repo.commit(msg("init"), "README.md", add_gitleaks_config(repo))
    bare = root / "remote.git"
    repo.git("init", "-q", "--bare", str(bare))
    repo.git("remote", "add", "origin", str(bare))
    repo.git("push", "-q", "--no-verify", "origin", "main")
    return repo, base


def _pairs(res: RunResult) -> set[tuple[str, str]]:
    return {(f[0], f[1]) for f in res.findings}


# --------------------------------------------------------------------------- H1: blinding


def _attr(text: str):  # noqa: ANN202
    def setup(repo: GitRepo) -> dict[str, str]:
        repo.write(".gitattributes", text)
        repo.commit(msg("attributes"), ".gitattributes")
        return {}

    return setup


def _color_config(repo: GitRepo) -> dict[str, str]:
    repo.git("config", "color.ui", "always")
    repo.git("config", "color.diff", "always")
    return {}


def _git_c(_: GitRepo) -> dict[str, str]:
    # What a hook inherits from `git -c color.ui=always -c color.diff=always …`.
    return {"GIT_CONFIG_PARAMETERS": "'color.ui'='always' 'color.diff'='always'"}


BLINDERS = {
    "attr-no-diff": _attr("*.txt -diff\n"),
    "attr-binary": _attr("*.txt binary\n"),
    "color-config": _color_config,
    "git-c-override": _git_c,
}


@pytest.fixture(scope="module")
def blinded(tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, str], tuple[RunResult, Secret]]:
    require_gitleaks()
    out: dict[tuple[str, str], tuple[RunResult, Secret]] = {}
    for case, setup in BLINDERS.items():
        repo, _ = _base_repo(tmp_path_factory.mktemp(case))
        env = setup(repo)
        base = repo.head()
        pat = make_fake("github-pat", case)
        repo.write("secret.txt", f"token = {pat.reveal()}\n")
        repo.git("add", "secret.txt")
        out[(case, "staged")] = (repo.guards("staged", extra_env=env), pat)
        repo.commit(msg("plant"))
        out[(case, "range")] = (repo.guards("range", "origin", base, "HEAD", extra_env=env), pat)
        out[(case, "history")] = (repo.guards("history", extra_env=env), pat)
    return out


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("case", sorted(BLINDERS))
def test_gitleaks_cannot_be_blinded(blinded: dict, case: str, mode: str) -> None:  # type: ignore[type-arg]
    res, pat = blinded[(case, mode)]
    assert res.rc == 1, (case, mode, res)
    assert ("github-pat", "secret.txt") in _pairs(res), (case, mode)
    assert_absent_all(res, [pat], f"blind-{case}-{mode}")


# --------------------------------------------------------------------------- H3, L5, M1(b) on one remote


@pytest.fixture(scope="module")
def pushed(tmp_path_factory: pytest.TempPathFactory) -> dict[str, RunResult]:
    require_gitleaks()
    repo, base = _base_repo(tmp_path_factory.mktemp("pushed"))
    out: dict[str, RunResult] = {}

    # H3: a Dependabot-style commit on main.
    bump = repo.commit("Bump pyyaml from 6.0.1 to 6.0.2\n\nSigned-off-by: dependabot[bot]\n")
    out["history-bump"] = repo.guards("history")
    out["prepush-bump"] = repo.guards("prepush", "origin", input=f"refs/heads/main {bump} refs/heads/main {base}\n")
    repo.git("reset", "-q", "--hard", base)

    # L5: a fake on a new branch, hidden behind a forged remote-tracking ref.
    repo.git("checkout", "-q", "-b", "feat")
    pat = make_fake("github-pat", "ghost")
    repo.write("ghost.txt", f"token {pat.reveal()}\n")
    ghost = repo.commit(msg("ghost"), "ghost.txt")
    repo.git("update-ref", "refs/remotes/origin/ghost", ghost)
    out["prepush-ghost"] = repo.guards("prepush", "origin", input=f"refs/heads/feat {ghost} refs/heads/feat {ZERO}\n")
    out["range-ghost"] = repo.guards("range", "origin", ZERO, "HEAD")
    repo.git("update-ref", "-d", "refs/remotes/origin/ghost")
    repo.git("checkout", "-q", "main")

    # M1(b): the candidate config drops the default rules; the base config still sees the fake.
    weak = make_fake("github-pat", "weak")
    weakened = (repo.root / ".gitleaks.toml").read_text().replace("useDefault = true", "useDefault = false")
    repo.write(".gitleaks.toml", weakened)
    repo.write("weak.txt", f"token {weak.reveal()}\n")
    repo.git("add", ".gitleaks.toml", "weak.txt")
    out["staged-weak"] = repo.guards("staged")
    tip = repo.commit(msg("weaken"))
    out["prepush-weak"] = repo.guards("prepush", "origin", input=f"refs/heads/main {tip} refs/heads/main {base}\n")
    out["history-weak"] = repo.guards("history")  # HEAD config only: the lint still flags it
    return out


def test_history_skips_commit_format(pushed: dict[str, RunResult]) -> None:
    assert pushed["history-bump"].rc == 0, pushed["history-bump"]


def test_prepush_enforces_commit_format(pushed: dict[str, RunResult]) -> None:
    res = pushed["prepush-bump"]
    assert res.rc == 1
    assert _pairs(res) == {("msg-format", ":message")}


@pytest.mark.parametrize("key", ["prepush-ghost", "range-ghost"])
def test_forged_remote_ref_does_not_hide_commits(pushed: dict[str, RunResult], key: str) -> None:
    res = pushed[key]
    assert res.rc == 1, res
    assert ("github-pat", "ghost.txt") in _pairs(res)


@pytest.mark.parametrize("key", ["staged-weak", "prepush-weak"])
def test_weakened_config_is_scanned_with_the_base_config_too(pushed: dict[str, RunResult], key: str) -> None:
    res = pushed[key]
    assert res.rc == 1, res
    assert ("github-pat", "weak.txt") in _pairs(res)
    assert ("gitleaks-config-extend", ".gitleaks.toml") in _pairs(res)


def test_history_flags_a_weakened_head_config(pushed: dict[str, RunResult]) -> None:
    res = pushed["history-weak"]
    assert res.rc == 1
    assert ("gitleaks-config-extend", ".gitleaks.toml") in _pairs(res)


# --------------------------------------------------------------------------- M2: path-ignored

IGNORED = ["x.sav", "x.parquet", "notebook.ipynb", "docker-compose.override.yml"]
CASE_VARIANTS = {"CLAUDE.MD": "path-local-artifact", ".Claude/x.json": "path-local-artifact", "Data/y.txt": "path-data"}


@pytest.fixture(scope="module")
def forced(tmp_path_factory: pytest.TempPathFactory) -> dict[str, RunResult]:
    require_gitleaks()
    repo = git_repo(tmp_path_factory.mktemp("forced"))
    repo.write("README.md", "# demo\n")
    base = repo.commit(msg("init"), "README.md", add_gitleaks_config(repo), add_real_gitignore(repo))
    names = [*IGNORED, *CASE_VARIANTS, ".env.example"]
    for rel in names:
        repo.write(rel, "N=1\n" if rel == ".env.example" else "x\n")
    repo.git("add", "-f", "--", *names)
    out = {"staged": repo.guards("staged")}
    repo.commit(msg("forced"))
    out["range"] = repo.guards("range", "origin", base, "HEAD")
    return out


@pytest.mark.parametrize("mode", ["staged", "range"])
def test_forced_ignored_paths_are_findings(forced: dict[str, RunResult], mode: str) -> None:
    res = forced[mode]
    pairs = _pairs(res)
    assert res.rc == 1
    for rel in IGNORED:
        assert ("path-ignored", rel) in pairs, rel
    for rel, rule in CASE_VARIANTS.items():
        assert (rule, rel) in pairs, rel
        assert ("path-ignored", rel) in pairs, rel  # .gitignore is matched case-insensitively too
    assert not [p for p in pairs if p[1] == ".env.example"]


# --------------------------------------------------------------------------- L4, L6, L3


def test_missing_staged_blob_is_rc2(tmp_path: Path) -> None:
    require_gitleaks()
    repo = git_repo(tmp_path)
    repo.write("README.md", "# demo\n")
    repo.commit(msg("init"), "README.md", add_gitleaks_config(repo))
    repo.git("update-index", "--add", "--cacheinfo", "100644," + "12345678" * 5 + ",ghost.txt")
    res = repo.guards("staged")
    assert res.rc == 2
    assert "blob" in res.stderr


def test_tree_scans_symlink_targets(tmp_path: Path) -> None:
    require_gitleaks()
    repo = git_repo(tmp_path)
    add_gitleaks_config(repo)
    (repo.root / "link").symlink_to(email("someone", "gmail.com"))
    res = repo.guards("tree")
    assert res.rc == 1
    assert ("content-email", "link") in _pairs(res)


def test_unmapped_stdin_hit_is_reported() -> None:
    batch = [(1, 2, ":message", "c1")]
    hits = [("github-pat", 2), ("github-pat", 9)]
    assert _map_text_hits(hits, batch, "tip") == [
        Finding("github-pat", ":message", 2, "c1"),
        Finding("github-pat", ":unmapped", 9, "tip"),
    ]


def test_invalid_range_ref_is_rc2(tmp_path: Path) -> None:
    require_gitleaks()
    repo, base = _base_repo(tmp_path)
    assert repo.guards("range", "origin", base, "no-such-ref").rc == 2
