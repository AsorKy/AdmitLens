"""Range, identity and pre-push checks on real throwaway repositories."""

from __future__ import annotations

from pathlib import Path

import pytest
from support.fakes import (
    GitRepo,
    add_gitleaks_config,
    assert_absent_all,
    email,
    git_repo,
    make_fake,
    msg,
    require_gitleaks,
)

pytestmark = pytest.mark.git

ZERO = "0" * 40


def _repo_with_remote(tmp_path: Path) -> tuple[GitRepo, str]:
    repo = git_repo(tmp_path)
    repo.write("README.md", "# demo\n")
    base = repo.commit(msg("init"), "README.md", add_gitleaks_config(repo))
    bare = tmp_path / "remote.git"
    repo.git("init", "-q", "--bare", str(bare))
    repo.git("remote", "add", "origin", str(bare))
    repo.git("push", "-q", "--no-verify", "origin", "main")
    return repo, base


@pytest.mark.gitleaks
def test_secret_only_in_merge_commit_is_found(tmp_path: Path) -> None:
    require_gitleaks()
    repo, base = _repo_with_remote(tmp_path)
    pat = make_fake("github-pat", "merge")
    repo.git("checkout", "-q", "-b", "side")
    repo.write("side.txt", "side\n")
    repo.commit(msg("side"), "side.txt")
    repo.git("checkout", "-q", "main")
    repo.write("main.txt", "main\n")
    repo.commit(msg("main"), "main.txt")
    repo.git("merge", "-q", "--no-ff", "--no-commit", "side")
    repo.write("config/merged.txt", f"token = {pat.reveal()}\n")
    repo.git("add", "config/merged.txt")
    merge = repo.commit("Merge branch 'side'\n")
    res = repo.guards("range", "origin", base, "HEAD")
    assert res.rc == 1
    hits = [f for f in res.findings if f[0] == "github-pat"]
    assert hits and hits[0][1] == "config/merged.txt" and hits[0][3] == merge
    assert_absent_all(res, [pat], "range-merge")


@pytest.mark.gitleaks
def test_secret_only_in_commit_message_is_found(tmp_path: Path) -> None:
    require_gitleaks()
    repo, base = _repo_with_remote(tmp_path)
    pat = make_fake("aws-access-token", "msg")
    sha = repo.commit(msg("leaky") + f"key {pat.reveal()}\n")
    res = repo.guards("range", "origin", base, "HEAD")
    assert res.rc == 1
    assert [f for f in res.findings if f[0] == "aws-access-token" and f[3] == sha]
    assert_absent_all(res, [pat], "range-message")


@pytest.mark.gitleaks
def test_annotated_tag_message_and_tagger_checked(tmp_path: Path) -> None:
    require_gitleaks()
    repo, base = _repo_with_remote(tmp_path)
    pat = make_fake("github-pat", "tag")
    repo.commit(msg("tagged"))
    repo.git(
        "tag", "-a", "v0", "-F", "-",
        input=f"release {pat.reveal()}\n",
        extra_env={"GIT_COMMITTER_EMAIL": email("rel", "gmail.com")},
    )
    res = repo.guards("range", "origin", base, "HEAD")
    assert res.rc == 1
    assert "github-pat" in res.rules
    assert ("identity-email", ":tagger") in {(f[0], f[1]) for f in res.findings}
    assert_absent_all(res, [pat], "range-tag")


def test_identity_author_and_committer_flagged(tmp_path: Path) -> None:
    repo, base = _repo_with_remote(tmp_path)
    sha = repo.commit(
        msg("bad ids"),
        extra_env={"GIT_AUTHOR_EMAIL": email("a", "gmail.com"), "GIT_COMMITTER_EMAIL": email("c", "corp.io")},
    )
    res = repo.guards("identity", f"{base}..HEAD")
    assert res.rc == 1
    assert sorted(res.findings) == [("identity-email", ":author", "0", sha), ("identity-email", ":committer", "0", sha)]


def test_real_name_marker_flagged(tmp_path: Path) -> None:
    repo, base = _repo_with_remote(tmp_path)
    name = make_fake("real-name", "range")
    repo.write(".git/info/exclude", "*.local.toml\n")
    repo.write(".guards.local.toml", f'[private]\nreal_names = ["{name.reveal()}"]\n')
    sha = repo.commit(msg("by name"), extra_env={"GIT_AUTHOR_NAME": name.reveal()})
    res = repo.guards("identity", f"{base}..HEAD")
    assert res.findings == [("identity-name", ":author", "0", sha)]
    assert_absent_all(res, [name], "identity-name")


@pytest.mark.gitleaks
def test_range_flags_bad_message_and_forced_path(tmp_path: Path) -> None:
    require_gitleaks()
    repo, base = _repo_with_remote(tmp_path)
    repo.write(".claude/settings.json", "{}\n")
    sha = repo.commit("wip\n", ".claude/settings.json")
    assert repo.guards("identity", f"{base}..HEAD").rc == 0
    full = repo.guards("range", "origin", base, "HEAD")
    assert full.rc == 1
    assert ("msg-format", ":message", "1", sha) in full.findings
    assert ("path-local-artifact", ".claude/settings.json", "0", sha) in full.findings


# --------------------------------------------------------------------------- prepush


def _prepush(repo: GitRepo, lines: list[str]):  # noqa: ANN202
    return repo.guards("prepush", "origin", input="".join(line + "\n" for line in lines))


def test_prepush_rejects_tag_ref(tmp_path: Path) -> None:
    repo, base = _repo_with_remote(tmp_path)
    res = _prepush(repo, [f"refs/tags/v1 {base} refs/tags/v1 {ZERO}"])
    assert res.rc == 1
    assert res.findings == [("push-ref-not-allowed", "refs/tags/v1", "0")]


def test_prepush_rejects_notes_and_replace_refs(tmp_path: Path) -> None:
    repo, base = _repo_with_remote(tmp_path)
    res = _prepush(
        repo,
        [f"refs/notes/commits {base} refs/notes/commits {ZERO}", f"refs/replace/{base} {base} refs/replace/{base} {ZERO}"],
    )
    assert res.rc == 1
    assert res.rules == {"push-ref-not-allowed"}
    assert len(res.findings) == 2


def test_prepush_rejects_main_deletion(tmp_path: Path) -> None:
    repo, base = _repo_with_remote(tmp_path)
    res = _prepush(repo, [f"(delete) {ZERO} refs/heads/main {base}"])
    assert res.rc == 1
    assert res.findings == [("push-delete-main", "refs/heads/main", "0")]


def test_prepush_allows_other_branch_deletion(tmp_path: Path) -> None:
    repo, base = _repo_with_remote(tmp_path)
    assert _prepush(repo, [f"(delete) {ZERO} refs/heads/old {base}"]).rc == 0


@pytest.mark.gitleaks
def test_prepush_new_branch_range(tmp_path: Path) -> None:
    require_gitleaks()
    repo, _ = _repo_with_remote(tmp_path)
    repo.git("checkout", "-q", "-b", "feat")
    good = repo.commit(msg("good"))
    assert _prepush(repo, [f"refs/heads/feat {good} refs/heads/feat {ZERO}"]).rc == 0
    bad = repo.commit("not the format\n")
    res = _prepush(repo, [f"refs/heads/feat {bad} refs/heads/feat {ZERO}"])
    assert res.rc == 1
    assert {f[3] for f in res.findings} == {bad}


@pytest.mark.gitleaks
def test_prepush_fast_forward_range_on_main(tmp_path: Path) -> None:
    require_gitleaks()
    repo, base = _repo_with_remote(tmp_path)
    new = repo.commit(msg("next"))
    assert _prepush(repo, [f"refs/heads/main {new} refs/heads/main {base}"]).rc == 0


def test_prepush_remote_ahead_is_rc2(tmp_path: Path) -> None:
    repo, base = _repo_with_remote(tmp_path)
    unknown = "1234567" * 5 + "89abc"
    res = _prepush(repo, [f"refs/heads/main {base} refs/heads/main {unknown}"])
    assert res.rc == 2
    assert "git fetch" in res.stderr


def test_prepush_malformed_stdin_is_rc2(tmp_path: Path) -> None:
    repo, _ = _repo_with_remote(tmp_path)
    assert _prepush(repo, ["only three fields"]).rc == 2


def test_prepush_empty_stdin_is_clean(tmp_path: Path) -> None:
    repo, _ = _repo_with_remote(tmp_path)
    assert _prepush(repo, []).rc == 0


# --------------------------------------------------------------------------- Dependabot sign-off

BOT_SIGNOFF = "Signed-off-by: dependabot[bot] <" + email("support", "github.com") + ">\n"


@pytest.mark.gitleaks
def test_dependabot_signoff_consistent_across_modes(tmp_path: Path) -> None:
    """The exact trailer is clean in message, range, prepush and history; a variant is flagged in all four."""
    require_gitleaks()
    repo, base = _repo_with_remote(tmp_path)
    good_msg = msg("bump x") + BOT_SIGNOFF
    bad_msg = msg("bump y") + "Signed-off-by: dependabot[bot] <" + email("support", "gmail.com") + ">\n"

    repo.write(".git/GOOD", good_msg)
    repo.write(".git/BAD", bad_msg)
    assert repo.guards("message", ".git/GOOD").rc == 0
    bad = repo.guards("message", ".git/BAD")
    assert bad.rc == 1 and bad.findings == [("msg-email", ":message", "6")]

    repo.git("checkout", "-q", "-b", "deps")
    good = repo.commit(good_msg)
    assert repo.guards("range", "origin", base, "HEAD").rc == 0
    assert _prepush(repo, [f"refs/heads/deps {good} refs/heads/deps {ZERO}"]).rc == 0
    assert repo.guards("history").rc == 0

    flagged = repo.commit(bad_msg)
    for res in (
        repo.guards("range", "origin", base, "HEAD"),
        _prepush(repo, [f"refs/heads/deps {flagged} refs/heads/deps {ZERO}"]),
        repo.guards("history"),
    ):
        assert res.rc == 1
        assert res.findings == [("msg-email", ":message", "6", flagged)]
