"""W9-R2 fixes on real throwaway repositories.

* M1: ``git replace`` / grafts cannot make the local layers read another history.
* M2: ``history`` judges old versions by leak rules only; policy rules
  (scan-lint, ``path-ignored``) apply to what the ref tips still contain.
* M3: every existing ``admitlens-*`` rule body is frozen; ``history`` unites the
  ``origin/main`` config; ``prepush`` on a new branch asks the remote
  (``ls-remote``) instead of trusting the forgeable tracking ref.
* L1: case / Unicode-normalisation twins and non-UTF-8 paths are scanned, not rc 2.
* L2: pathspec magic (a leading ``:``, ``GIT_*_PATHSPECS``) cannot break or bend scans.
* L3: gitlinks get path rules.
* L5: attribution follows ``--topo-order`` even with skewed commit dates.
"""

from __future__ import annotations

import os
import sys
import tomllib
import unicodedata
from pathlib import Path

import pytest
from support.fakes import (
    GITLEAKS_CONFIG,
    GitRepo,
    RunResult,
    add_gitleaks_config,
    add_real_gitignore,
    assert_absent_all,
    git_repo,
    make_fake,
    msg,
    require_gitleaks,
)

from guards.rules import lint

pytestmark = pytest.mark.git

ZERO = "0" * 40


def _repo_with_remote(tmp_path: Path, *, gitignore: bool = False) -> tuple[GitRepo, str]:
    repo = git_repo(tmp_path)
    repo.write("README.md", "# demo\n")
    files = ["README.md", add_gitleaks_config(repo)]
    if gitignore:
        files.append(add_real_gitignore(repo))
    base = repo.commit(msg("init"), *files)
    bare = tmp_path / "remote.git"
    repo.git("init", "-q", "--bare", str(bare))
    repo.git("remote", "add", "origin", str(bare))
    repo.git("push", "-q", "--no-verify", "origin", "main")
    return repo, base


def _pairs(res: RunResult) -> set[tuple[str, str]]:
    return {(f[0], f[1]) for f in res.findings}


def _blob(repo: GitRepo, data: str) -> str:
    return repo.git("hash-object", "-w", "--stdin", input=data).stdout.strip()


def _index_add(repo: GitRepo, mode: str, sha: str, path: str) -> None:
    repo.git("update-index", "--add", "--cacheinfo", f"{mode},{sha},{path}")


def _install_prepush_hook(repo: GitRepo) -> None:
    hook = repo.root / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m guards prepush "$1"\n', encoding="utf-8")
    hook.chmod(0o755)


# --------------------------------------------------------------------------- M1: replace objects


@pytest.fixture(scope="module")
def replaced(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    require_gitleaks()
    tmp = tmp_path_factory.mktemp("replaced")
    repo, base = _repo_with_remote(tmp)
    pat = make_fake("github-pat", "replace")
    repo.write("secret.txt", f"token = {pat.reveal()}\n")
    dirty = repo.commit(msg("plant"), "secret.txt")
    tree = repo.head(f"{base}^{{tree}}")
    clean = repo.git("commit-tree", tree, "-p", base, "-F", "-", input=msg("clean")).stdout.strip()
    repo.git("replace", dirty, clean)
    assert repo.git("ls-tree", "--name-only", "HEAD").stdout.split() == [".gitleaks.toml", "README.md"]  # git is fooled
    out: dict[str, object] = {"pat": pat, "base": base}
    out["range"] = repo.guards("range", "origin", base, "HEAD")
    out["history"] = repo.guards("history")
    _install_prepush_hook(repo)
    out["push"] = repo.git("push", "-q", "origin", "main", check=False)
    out["remote-main"] = repo.git("ls-remote", "origin", "refs/heads/main").stdout.split()[:1]
    return out


@pytest.mark.gitleaks
@pytest.mark.parametrize("mode", ["range", "history"])
def test_replace_objects_do_not_hide_a_commit(replaced: dict[str, object], mode: str) -> None:
    res = replaced[mode]
    assert isinstance(res, RunResult)
    assert res.rc == 1, res
    assert ("github-pat", "secret.txt") in _pairs(res)
    assert_absent_all(res, [replaced["pat"]], f"replace-{mode}")  # type: ignore[list-item]


@pytest.mark.gitleaks
def test_replace_objects_do_not_fool_a_real_push(replaced: dict[str, object]) -> None:
    res = replaced["push"]
    assert isinstance(res, RunResult)
    assert res.rc != 0, res
    assert replaced["remote-main"] == [replaced["base"]]  # nothing reached the remote
    assert_absent_all(res, [replaced["pat"]], "replace-push")  # type: ignore[list-item]


# --------------------------------------------------------------------------- M2: history policy per tip


@pytest.fixture(scope="module")
def policy(tmp_path_factory: pytest.TempPathFactory) -> dict[str, RunResult]:
    require_gitleaks()
    repo, _base = _repo_with_remote(tmp_path_factory.mktemp("policy"))
    out: dict[str, RunResult] = {}
    # A file later deleted and ignored, plus scan-lint files that fail today's lint, later fixed.
    repo.write("out/report.txt", "report\n")
    repo.write(".gitattributes", "*.txt -diff\n")
    old = GITLEAKS_CONFIG.read_text(encoding="utf-8").replace('id = "admitlens-ssn"', 'id = "admitlens-ssn-old"')
    repo.write(".gitleaks.toml", old)
    repo.commit(msg("old policy"), "out/report.txt", ".gitattributes", ".gitleaks.toml")
    repo.git("branch", "keep")  # a tip that still holds all of it
    repo.git("rm", "-q", "--cached", "--", "out/report.txt")
    (repo.root / "out" / "report.txt").unlink()
    repo.write(".gitignore", "out/\n")
    repo.write(".gitattributes", "*.png binary\n")
    add_gitleaks_config(repo)
    repo.commit(msg("fix policy"), ".gitignore", ".gitattributes", ".gitleaks.toml")
    out["kept"] = repo.guards("history")
    repo.git("branch", "-q", "-D", "keep")
    out["fixed"] = repo.guards("history")
    return out


@pytest.mark.gitleaks
def test_history_does_not_judge_old_versions_by_todays_policy(policy: dict[str, RunResult]) -> None:
    res = policy["fixed"]
    assert res.rc == 0, (res, res.findings)


@pytest.mark.gitleaks
def test_history_policy_applies_to_what_a_tip_still_holds(policy: dict[str, RunResult]) -> None:
    res = policy["kept"]
    assert res.rc == 1
    assert _pairs(res) == {
        ("path-ignored", "out/report.txt"),
        ("gitattributes-diff", ".gitattributes"),
        ("gitleaks-config-rule-missing", ".gitleaks.toml"),
    }


@pytest.mark.gitleaks
def test_history_still_runs_leak_rules_on_old_versions(tmp_path: Path) -> None:
    require_gitleaks()
    repo, _ = _repo_with_remote(tmp_path)
    pat = make_fake("github-pat", "old-version")
    repo.write("notes.txt", f"token {pat.reveal()}\n")
    repo.write(".env", "A=1\n")
    repo.commit(msg("leak"), "notes.txt", ".env")
    repo.git("rm", "-q", "--", "notes.txt", ".env")
    repo.commit(msg("remove"))
    res = repo.guards("history")
    assert res.rc == 1
    assert {("github-pat", "notes.txt"), ("path-env", ".env")} <= _pairs(res)
    assert_absent_all(res, [pat], "history-old-version")


# --------------------------------------------------------------------------- M3(a): frozen rule bodies


def _rule_block_insert(text: str, rule_id: str, line: str) -> str:
    anchor = f'id = "{rule_id}"\n'
    assert anchor in text
    return text.replace(anchor, anchor + line + "\n", 1)


REAL_TOML = GITLEAKS_CONFIG.read_text(encoding="utf-8")
WEAKENINGS = {
    "ssn-keywords": _rule_block_insert(REAL_TOML, "admitlens-ssn", 'keywords = ["zzqx"]'),
    "ssn-entropy": _rule_block_insert(REAL_TOML, "admitlens-ssn", "entropy = 4.5"),
    "ssn-path": _rule_block_insert(REAL_TOML, "admitlens-ssn", "path = '''\\.md$'''"),
    "local-path-secret-group": _rule_block_insert(REAL_TOML, "admitlens-local-path", "secretGroup = 4"),
    "vault-marker-keywords": REAL_TOML.replace('keywords = ["obsidian", "projects/"]', 'keywords = ["zzqx"]'),
    "vault-marker-regex": REAL_TOML.replace("Obsidian[ ]Vault/", "Obsidian[ ]Vaultz/"),
    "path-data-path": REAL_TOML.replace("path = '''(?:^|/)data/'''", "path = '''(?:^|/)data/zz/'''"),
    "path-key-keywords": _rule_block_insert(REAL_TOML, "admitlens-path-key", 'keywords = ["zzqx"]'),
}


@pytest.mark.unit
@pytest.mark.parametrize("case", sorted(WEAKENINGS))
def test_frozen_rule_bodies_cannot_change(case: str) -> None:
    text = WEAKENINGS[case]
    assert text != REAL_TOML
    tomllib.loads(text)  # still valid TOML: only the freeze can catch it
    found = lint.check_gitleaks_config(".gitleaks.toml", text)
    assert ("gitleaks-config-rule-changed", ".gitleaks.toml", 0) in [(f.rule, f.path, f.line) for f in found], case


@pytest.mark.unit
def test_frozen_snapshot_equals_the_real_config() -> None:
    real = {r["id"]: lint.rule_body(r) for r in tomllib.loads(REAL_TOML)["rules"]}
    assert real == lint.FROZEN_RULES
    assert lint.check_gitleaks_config(".gitleaks.toml", REAL_TOML) == []


@pytest.mark.unit
def test_new_custom_rules_are_still_allowed() -> None:
    extra = REAL_TOML + '\n[[rules]]\nid = "admitlens-extra"\ndescription = "x"\nregex = \'\'\'zz[0-9]{40}\'\'\'\n'
    assert lint.check_gitleaks_config(".gitleaks.toml", extra) == []


# --------------------------------------------------------------------------- M3(b): history unites origin/main


@pytest.mark.gitleaks
def test_history_unites_the_origin_main_config(tmp_path: Path) -> None:
    require_gitleaks()
    repo, _ = _repo_with_remote(tmp_path)
    repo.git("fetch", "-q", "origin")
    weak = make_fake("github-pat", "two-step")
    repo.write(".gitleaks.toml", REAL_TOML.replace("useDefault = true", "useDefault = false"))
    repo.write("weak.txt", f"token {weak.reveal()}\n")
    repo.commit(msg("weaken"), ".gitleaks.toml", "weak.txt")
    res = repo.guards("history")
    assert res.rc == 1
    assert ("github-pat", "weak.txt") in _pairs(res)
    assert_absent_all(res, [weak], "history-two-step")


# --------------------------------------------------------------------------- M3(c): prepush asks the remote


@pytest.mark.gitleaks
def test_prepush_new_branch_ignores_a_forged_tracking_ref(tmp_path: Path) -> None:
    require_gitleaks()
    repo, _ = _repo_with_remote(tmp_path)
    repo.git("checkout", "-q", "-b", "feat")
    pat = make_fake("github-pat", "forged-main")
    repo.write("feat.txt", f"token {pat.reveal()}\n")
    tip = repo.commit(msg("feat"), "feat.txt")
    repo.git("update-ref", "refs/remotes/origin/main", tip)  # forged: "the remote already has it"
    res = repo.guards("prepush", "origin", input=f"refs/heads/feat {tip} refs/heads/feat {ZERO}\n")
    assert res.rc == 1, res
    assert ("github-pat", "feat.txt") in _pairs(res)
    assert_absent_all(res, [pat], "prepush-forged-main")


@pytest.mark.gitleaks
def test_prepush_to_an_empty_remote_scans_everything(tmp_path: Path) -> None:
    require_gitleaks()
    repo = git_repo(tmp_path)
    repo.write("README.md", "# demo\n")
    first = repo.commit(msg("init"), "README.md", add_gitleaks_config(repo))
    repo.git("init", "-q", "--bare", str(tmp_path / "remote.git"))
    repo.git("remote", "add", "origin", str(tmp_path / "remote.git"))
    assert repo.guards("prepush", "origin", input=f"refs/heads/main {first} refs/heads/main {ZERO}\n").rc == 0
    pat = make_fake("github-pat", "bootstrap")
    repo.write("a.txt", f"token {pat.reveal()}\n")
    leaky = repo.commit(msg("leak"), "a.txt")
    repo.commit(msg("later"))
    res = repo.guards("prepush", "origin", input=f"refs/heads/main {repo.head()} refs/heads/main {ZERO}\n")
    assert res.rc == 1
    assert ("github-pat", "a.txt", leaky) in {(f[0], f[1], f[3]) for f in res.findings}
    assert_absent_all(res, [pat], "prepush-bootstrap")


def test_prepush_ls_remote_failure_is_rc2(tmp_path: Path) -> None:
    repo, _ = _repo_with_remote(tmp_path)
    repo.git("remote", "set-url", "origin", str(tmp_path / "gone.git"))
    repo.git("checkout", "-q", "-b", "feat")
    tip = repo.commit(msg("feat"))
    res = repo.guards("prepush", "origin", input=f"refs/heads/feat {tip} refs/heads/feat {ZERO}\n")
    assert res.rc == 2
    assert "ls-remote" in res.stderr


# --------------------------------------------------------------------------- L1: path twins and non-UTF-8 paths

TWIN_PATHS = {
    "case-upper": "Secret.txt",
    "case-lower": "secret.txt",
    "nfc": unicodedata.normalize("NFC", "café.txt"),
    "nfd": unicodedata.normalize("NFD", "café.txt"),
    "file": "dir",
    "dir-child": "DIR/inner.txt",
    "non-utf8": os.fsdecode(b"bad\xff.txt"),
}


@pytest.mark.gitleaks
@pytest.mark.parametrize("mode", ["staged", "range"])
def test_path_twins_and_non_utf8_paths_are_scanned(tmp_path: Path, mode: str) -> None:
    require_gitleaks()
    repo, base = _repo_with_remote(tmp_path)
    repo.git("config", "core.ignorecase", "false")  # keep the twins distinct in the index
    repo.git("config", "core.precomposeunicode", "false")
    fakes = {key: make_fake("github-pat", f"twin-{key}") for key in TWIN_PATHS}
    for key, rel in TWIN_PATHS.items():
        _index_add(repo, "100644", _blob(repo, f"token {fakes[key].reveal()}\n"), rel)
    if mode == "staged":
        res = repo.guards("staged")
    else:
        repo.commit(msg("twins"))
        res = repo.guards("range", "origin", base, "HEAD")
    assert res.rc == 1, res
    found = {f[1] for f in res.findings if f[0] == "github-pat"}
    expected = {rel.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace") for rel in TWIN_PATHS.values()}
    assert found == expected
    assert_absent_all(res, list(fakes.values()), f"twins-{mode}")


# --------------------------------------------------------------------------- L2: pathspec magic


@pytest.mark.gitleaks
def test_leading_colon_path_is_checked_against_gitignore(tmp_path: Path) -> None:
    require_gitleaks()
    repo, _ = _repo_with_remote(tmp_path, gitignore=True)
    _index_add(repo, "100644", _blob(repo, "x\n"), ":(glob)x.sav")
    res = repo.guards("staged")
    assert res.rc == 1, res
    assert ("path-ignored", ":(glob)x.sav") in _pairs(res)


@pytest.mark.gitleaks
@pytest.mark.parametrize(
    "var", ["GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS"]
)
def test_inherited_pathspec_variables_are_ignored(tmp_path: Path, var: str) -> None:
    require_gitleaks()
    repo, _ = _repo_with_remote(tmp_path, gitignore=True)
    _index_add(repo, "100644", _blob(repo, "x\n"), "x.sav")
    res = repo.guards("staged", extra_env={var: "1"})
    assert res.rc == 1, res
    assert _pairs(res) == {("path-ignored", "x.sav")}


# --------------------------------------------------------------------------- L3: gitlinks


@pytest.mark.gitleaks
@pytest.mark.parametrize("mode", ["staged", "range"])
def test_gitlinks_get_path_rules(tmp_path: Path, mode: str) -> None:
    require_gitleaks()
    repo, base = _repo_with_remote(tmp_path, gitignore=True)
    _index_add(repo, "160000", base, ".claude")
    _index_add(repo, "160000", base, "data/sub")
    if mode == "staged":
        res = repo.guards("staged")
    else:
        repo.commit(msg("gitlinks"))
        res = repo.guards("range", "origin", base, "HEAD")
    assert res.rc == 1, res
    pairs = _pairs(res)
    assert {("path-local-artifact", ".claude"), ("path-data", "data/sub")} <= pairs
    assert {("path-ignored", ".claude"), ("path-ignored", "data/sub")} <= pairs


# --------------------------------------------------------------------------- L5: topo-order attribution


def _dated(when: str) -> dict[str, str]:
    return {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}


@pytest.mark.gitleaks
def test_attribution_follows_topology_not_dates(tmp_path: Path) -> None:
    require_gitleaks()
    repo = git_repo(tmp_path)
    repo.write("README.md", "# demo\n")
    repo.commit(msg("root"), "README.md", add_gitleaks_config(repo), extra_env=_dated("2025-01-01T00:00:00Z"))
    pat = make_fake("github-pat", "topo")
    repo.write("f.txt", f"token {pat.reveal()}\n")
    origin = repo.commit(msg("add f"), "f.txt", extra_env=_dated("2026-01-01T00:00:00Z"))
    repo.git("checkout", "-q", "-b", "side")
    repo.git("rm", "-q", "--", "f.txt")
    repo.commit(msg("drop f"), extra_env=_dated("2030-01-01T00:00:00Z"))
    repo.write("f.txt", f"token {pat.reveal()}\n")
    readd = repo.commit(msg("re-add f"), "f.txt", extra_env=_dated("2000-01-01T00:00:00Z"))
    repo.git("checkout", "-q", "main")
    repo.write("c.txt", "c\n")
    repo.commit(msg("other"), "c.txt", extra_env=_dated("2010-01-01T00:00:00Z"))
    repo.git("merge", "-q", "--no-ff", "-m", msg("merge side"), "side", extra_env=_dated("2026-06-01T00:00:00Z"))
    by_date = repo.git("rev-list", "--all").stdout.split()
    assert by_date.index(origin) < by_date.index(readd)  # date order would credit the re-add
    res = repo.guards("history")
    assert res.rc == 1
    assert {f[3] for f in res.findings if f[0] == "github-pat" and f[1] == "f.txt"} == {origin}
    assert_absent_all(res, [pat], "topo")
