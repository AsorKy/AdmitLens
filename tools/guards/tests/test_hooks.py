"""The real git hooks, end to end: .githooks/* -> make hook-* -> guards.

Every test works on a copy of a wired template repository (see
``support/wired.py``): the real root Makefile, hooks and gitleaks config, hooks
switched on, one clean commit already pushed to a local bare remote.

Commits that only set the scene for a pre-push test use ``--no-verify``: each
hooked commit costs two gitleaks runs, and only the step under test needs them.

The hooks must not trust the environment they inherit: ``MAKEFILES``,
``MAKEFLAGS``, an env ``GUARDS_DIR``/``GITLEAKS`` and a stray ``GNUmakefile``
are all ignored, and a missing ``make``, ``uv`` or gitleaks fails closed (rc 2).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from support.fakes import RunResult, Secret, assert_absent_all, email, make_fake, msg, run
from support.wired import WIRED_FILES, WiredRepo, bare_wired_repo

pytestmark = [pytest.mark.git, pytest.mark.make, pytest.mark.gitleaks]

# The exact text guards/gitleaks.py raises when the configured binary is missing.
GITLEAKS_MISSING = "gitleaks not found at the configured path"


def try_commit(
    repo: WiredRepo,
    message: str,
    *paths: str,
    env: dict[str, str] | None = None,
    no_verify: bool = False,
) -> RunResult:
    if paths:
        repo.git("add", "-f", "--", *paths)
    args = ["commit", "-q", "--allow-empty", "-F", "-", *(["--no-verify"] if no_verify else [])]
    return repo.git(*args, input=message, extra_env=env, check=False)


def assert_blocked(repo: WiredRepo, res: RunResult, before: str, rule: str | None = None) -> None:
    assert res.rc != 0, res
    assert repo.head() == before
    if rule is not None:
        assert rule in repo.output(res), rule


def assert_push_blocked(repo: WiredRepo, res: RunResult, rule: str, remote_before: str) -> None:
    assert res.rc != 0, res
    assert rule in repo.output(res), rule
    assert repo.remote_head() == remote_before


# --------------------------------------------------------------------------- pre-commit


def test_pre_commit_blocks_staged_env_file(wired: WiredRepo) -> None:
    before = wired.head()
    wired.write(".env", "APP_MODE=dev\n")
    assert_blocked(wired, try_commit(wired, msg("env"), ".env"), before, "path-env")


def test_pre_commit_blocks_forced_claude_artifact(wired: WiredRepo) -> None:
    before = wired.head()
    wired.write("CLAUDE.md", "# local notes\n")
    assert_blocked(wired, try_commit(wired, msg("notes"), "CLAUDE.md"), before, "path-local-artifact")


def test_pre_commit_blocks_staged_fake_dsn(wired: WiredRepo) -> None:
    before = wired.head()
    dsn = make_fake("dsn-password", "pre-commit")
    wired.write("config/settings.txt", f"url = {dsn.reveal()}\n")
    res = try_commit(wired, msg("settings"), "config/settings.txt")
    assert_blocked(wired, res, before, "admitlens-dsn-password")
    assert_absent_all(res, [dsn], "pre-commit")


def test_pre_commit_blocks_gitleaksignore(wired: WiredRepo) -> None:
    before = wired.head()
    wired.write(".gitleaksignore", "x\n")
    res = try_commit(wired, msg("ignore"), ".gitleaksignore")
    assert_blocked(wired, res, before, ".gitleaksignore")
    assert "Error 2" in wired.output(res)


def test_pre_commit_lets_env_example_with_placeholders_pass(wired: WiredRepo) -> None:
    wired.write(".env.example", "POSTGRES_USER=admitlens\nPOSTGRES_PASSWORD=__GENERATE__\nADMITLENS_DATABASE_URL=\n")
    wired.git("add", ".env.example")
    res = try_commit(wired, msg("env example"))
    assert res.rc == 0, res


def test_inherited_makeflags_dry_run_does_not_neuter_the_hook(wired: WiredRepo) -> None:
    before = wired.head()
    wired.write(".env", "APP_MODE=dev\n")
    res = try_commit(wired, msg("env"), ".env", env={"MAKEFLAGS": "-n", "MFLAGS": "-n", "GNUMAKEFLAGS": "-n"})
    assert_blocked(wired, res, before, "path-env")


def test_missing_gitleaks_blocks_the_commit(wired: WiredRepo) -> None:
    before = wired.head()
    (wired.root / ".tools" / "bin" / "gitleaks").unlink()
    wired.write("app.txt", "hello\n")
    res = try_commit(wired, msg("clean"), "app.txt")
    assert_blocked(wired, res, before, GITLEAKS_MISSING)
    assert "Error 2" in wired.output(res)


# --------------------------------------------------------------------------- inherited environment


def _env_makefiles(repo: WiredRepo, tmp: Path) -> dict[str, str]:
    evil = tmp / "evil.mk"
    evil.write_text("override GUARDS = true\n", encoding="utf-8")
    return {"MAKEFILES": str(evil)}


def _env_tool_paths(repo: WiredRepo, tmp: Path) -> dict[str, str]:
    """A gitleaks that reports the pinned version and finds nothing, and a guards dir that does not exist."""
    fake = tmp / "fake-gitleaks"
    fake.write_text('#!/bin/sh\n[ "$1" = version ] && echo 8.30.1\nexit 0\n', encoding="utf-8")
    fake.chmod(0o755)
    return {"GITLEAKS": str(fake), "GUARDS_DIR": str(tmp / "no-guards")}


def _stray_gnumakefile(repo: WiredRepo, tmp: Path) -> dict[str, str]:
    repo.write("GNUmakefile", "hook-pre-commit hook-commit-msg hook-pre-push:\n\t@true\n")
    return {}


ATTACKS = [("makefiles", _env_makefiles), ("env-tool-paths", _env_tool_paths), ("gnumakefile", _stray_gnumakefile)]


@pytest.mark.parametrize(("case", "attack"), ATTACKS, ids=[a[0] for a in ATTACKS])
def test_inherited_make_environment_does_not_neuter_pre_commit(wired: WiredRepo, tmp_path: Path, case: str, attack) -> None:  # noqa: ANN001
    """The fake token is visible only to gitleaks, so its rule ID proves the real guards and gitleaks ran."""
    before = wired.head()
    env = attack(wired, tmp_path)
    pat = make_fake("github-pat", f"inherited-{case}")
    wired.write("notes.txt", f"see {pat.reveal()}\n")
    res = try_commit(wired, msg("notes"), "notes.txt", env=env)
    assert_blocked(wired, res, before, "github-pat")
    assert_absent_all(res, [pat], f"inherited-{case}")


@pytest.mark.parametrize(("case", "attack"), ATTACKS, ids=[a[0] for a in ATTACKS])
def test_inherited_make_environment_does_not_neuter_pre_push(wired: WiredRepo, tmp_path: Path, case: str, attack) -> None:  # noqa: ANN001
    remote = wired.remote_head()
    env = attack(wired, tmp_path)
    pat = make_fake("github-pat", f"inherited-push-{case}")
    wired.write("notes.txt", f"token {pat.reveal()}\n")
    assert try_commit(wired, msg("sneaky"), "notes.txt", no_verify=True).rc == 0
    res = wired.push("origin", "main", env=env)
    assert_push_blocked(wired, res, "github-pat", remote)
    assert_absent_all(res, [pat], f"inherited-push-{case}")


# --------------------------------------------------------------------------- missing tools (PATH trimmed)

BASE_TOOLS = ("git", "make", "uv", "dirname", "basename", "mktemp", "cat", "rm")


@pytest.fixture(scope="module")
def trimmed_paths(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """PATH values holding every tool the hooks need except one (``make`` or ``uv``)."""
    paths = {}
    for missing in ("make", "uv"):
        d = tmp_path_factory.mktemp(f"no-{missing}")
        for tool in BASE_TOOLS:
            if tool == missing:
                continue
            src = shutil.which(tool)
            assert src, f"{tool} not found on PATH"
            (d / tool).symlink_to(src)
        paths[missing] = str(d)
    return paths


def _hook_call(repo: WiredRepo, hook: str) -> tuple[list[str], str | None]:
    if hook == "commit-msg":
        repo.write(".git/MSG_TRIMMED", msg("x"))
        return [".git/MSG_TRIMMED"], None
    if hook == "pre-push":
        sha = repo.head()
        return ["origin", "../remote.git"], f"refs/heads/main {sha} refs/heads/main {sha}\n"
    return [], None


@pytest.mark.parametrize("missing", ["make", "uv"])
@pytest.mark.parametrize("hook", ["pre-commit", "commit-msg", "pre-push"])
def test_hook_fails_closed_when_a_tool_is_missing(
    wired: WiredRepo, trimmed_paths: dict[str, str], hook: str, missing: str
) -> None:
    args, stdin = _hook_call(wired, hook)
    res = run(["/bin/sh", f".githooks/{hook}", *args], wired.root, stdin, home=wired.home,
              extra_env=wired.env, path=trimmed_paths[missing])
    assert res.rc == 2, (hook, missing, res)
    if missing == "make":
        assert f"{hook}: make not found" in res.stderr


def test_commit_msg_hook_fails_closed_when_cd_fails(wired: WiredRepo) -> None:
    """A message path whose directory does not exist stops the hook before make runs."""
    res = run(["/bin/sh", ".githooks/commit-msg", "no-such-dir/MSG"], wired.root, home=wired.home, extra_env=wired.env)
    assert res.rc == 2, res
    assert "guards" not in res.stderr


def test_commit_without_make_on_path_is_blocked(wired: WiredRepo, trimmed_paths: dict[str, str]) -> None:
    before = wired.head()
    wired.write("app.txt", "hello\n")
    wired.git("add", "--", "app.txt")
    res = run(["git", "commit", "-q", "-F", "-"], wired.root, msg("clean"), home=wired.home,
              extra_env=wired.env, path=trimmed_paths["make"])
    assert_blocked(wired, res, before, "pre-commit: make not found")


# --------------------------------------------------------------------------- commit-msg


def test_commit_msg_rejects_bad_format(wired: WiredRepo) -> None:
    before = wired.head()
    assert_blocked(wired, try_commit(wired, "wip\n"), before, "msg-format")


def test_commit_msg_rejects_session_trailer(wired: WiredRepo) -> None:
    before = wired.head()
    trailer = "Claude" + "-Session" + ": abc\n"
    assert_blocked(wired, try_commit(wired, msg("x") + trailer), before, "msg-claude-session")


def test_commit_msg_rejects_fake_in_message(wired: WiredRepo) -> None:
    before = wired.head()
    pat = make_fake("github-pat", "commit-msg")
    res = try_commit(wired, msg("x") + f"token {pat.reveal()}\n")
    assert_blocked(wired, res, before, "github-pat")
    assert_absent_all(res, [pat], "commit-msg")


@pytest.mark.parametrize("first", ["[Implementing - 1]{2026-10-17 PLAN-CA-0005}: x", "Merge branch 'side'"])
def test_commit_msg_accepts_valid_messages(wired: WiredRepo, first: str) -> None:
    before = wired.head()
    res = try_commit(wired, first + "\n")
    assert res.rc == 0, res
    assert wired.head() != before


# --------------------------------------------------------------------------- pre-push


def test_pre_push_rejects_non_noreply_author(wired: WiredRepo) -> None:
    remote = wired.remote_head()
    assert try_commit(wired, msg("x"), env={"GIT_AUTHOR_EMAIL": email("dev", "corp.example.org")}, no_verify=True).rc == 0
    assert_push_blocked(wired, wired.push("origin", "main"), "identity-email", remote)


def test_pre_push_rejects_non_noreply_committer(wired: WiredRepo) -> None:
    """Author is noreply; only the committer is not."""
    remote = wired.remote_head()
    env = {"GIT_COMMITTER_EMAIL": email("dev", "corp.example.org")}
    assert try_commit(wired, msg("x"), env=env, no_verify=True).rc == 0
    res = wired.push("origin", "main")
    assert_push_blocked(wired, res, "identity-email", remote)
    assert "identity-email\t:committer" in wired.output(res)


def test_pre_push_rejects_real_name_marker(wired: WiredRepo) -> None:
    remote = wired.remote_head()
    name = make_fake("real-name", "pre-push")
    wired.write(".guards.local.toml", f'[private]\nreal_names = ["{name.reveal()}"]\n')
    assert try_commit(wired, msg("x"), env={"GIT_AUTHOR_NAME": name.reveal()}, no_verify=True).rc == 0
    res = wired.push("origin", "main")
    assert_push_blocked(wired, res, "identity-name", remote)
    assert_absent_all(res, [name], "pre-push-name")


def _plant_then_remove(wired: WiredRepo, fake: Secret) -> str:
    wired.write("config/settings.txt", f"url = {fake.reveal()}\n")
    assert try_commit(wired, msg("plant"), "config/settings.txt", no_verify=True).rc == 0
    planted = wired.head()
    wired.write("config/settings.txt", "url = <set me>\n")
    assert try_commit(wired, msg("remove"), "config/settings.txt", no_verify=True).rc == 0
    return planted


def test_pre_push_rejects_fake_in_intermediate_commit(wired: WiredRepo) -> None:
    remote = wired.remote_head()
    dsn = make_fake("dsn-password", "intermediate")
    planted = _plant_then_remove(wired, dsn)
    res = wired.push("origin", "main")
    assert_push_blocked(wired, res, "admitlens-dsn-password", remote)
    assert planted in res.stdout + res.stderr
    assert_absent_all(res, [dsn], "pre-push-intermediate")


def test_no_verify_commit_is_caught_at_push(wired: WiredRepo) -> None:
    remote = wired.remote_head()
    pat = make_fake("github-pat", "no-verify")
    wired.write("notes.txt", f"token {pat.reveal()}\n")
    assert try_commit(wired, msg("sneaky"), "notes.txt", no_verify=True).rc == 0
    res = wired.push("origin", "main")
    assert_push_blocked(wired, res, "github-pat", remote)
    assert_absent_all(res, [pat], "pre-push-no-verify")


def test_pre_push_rejects_fake_only_in_merge(wired: WiredRepo) -> None:
    remote = wired.remote_head()
    pat = make_fake("github-pat", "merge-push")
    wired.git("checkout", "-q", "-b", "side")
    wired.write("side.txt", "side\n")
    assert try_commit(wired, msg("side"), "side.txt", no_verify=True).rc == 0
    wired.git("checkout", "-q", "main")
    wired.write("main.txt", "main\n")
    assert try_commit(wired, msg("main"), "main.txt", no_verify=True).rc == 0
    wired.git("merge", "-q", "--no-ff", "--no-commit", "side")
    wired.write("merged.txt", f"token {pat.reveal()}\n")
    assert try_commit(wired, "Merge branch 'side'\n", "merged.txt", no_verify=True).rc == 0
    res = wired.push("origin", "main")
    assert_push_blocked(wired, res, "github-pat", remote)
    assert_absent_all(res, [pat], "pre-push-merge")


def test_pre_push_rejects_tag(wired: WiredRepo) -> None:
    wired.git("tag", "-a", "v1", "-F", "-", input="release\n")
    res = wired.push("origin", "v1")
    assert res.rc != 0
    assert "push-ref-not-allowed" in wired.output(res)
    assert wired.remote_head("refs/tags/v1") == ""


def test_pre_push_rejects_notes_ref(wired: WiredRepo) -> None:
    wired.git("notes", "add", "-m", "note", "HEAD")
    res = wired.push("origin", "refs/notes/commits")
    assert res.rc != 0
    assert "push-ref-not-allowed" in wired.output(res)
    assert wired.remote_head("refs/notes/commits") == ""


def test_pre_push_rejects_main_deletion(wired: WiredRepo) -> None:
    remote = wired.remote_head()
    assert_push_blocked(wired, wired.push("origin", ":main"), "push-delete-main", remote)


def test_pre_push_remote_ahead_is_rc2(wired: WiredRepo) -> None:
    bare = ["--git-dir", str(wired.root.parent / "remote.git")]
    ident = {
        "GIT_AUTHOR_NAME": "Other", "GIT_AUTHOR_EMAIL": "9+o@users.noreply.github.com",
        "GIT_COMMITTER_NAME": "Other", "GIT_COMMITTER_EMAIL": "9+o@users.noreply.github.com",
    }
    tree = wired.git(*bare, "rev-parse", "main^{tree}").stdout.strip()
    ahead = wired.git(*bare, "commit-tree", tree, "-p", "main", "-m", "remote only", extra_env=ident).stdout.strip()
    wired.git(*bare, "update-ref", "refs/heads/main", ahead)
    assert try_commit(wired, msg("local"), no_verify=True).rc == 0
    res = wired.push("--force", "origin", "main")
    out = wired.output(res)
    assert res.rc != 0
    assert "git fetch" in out and "Error 2" in out
    assert wired.remote_head() == ahead


def test_pre_push_runs_level_checks_and_warns_without_them(wired: WiredRepo) -> None:
    wired.write("level-1/backend/Makefile", "check:\n\t@echo level-one-check-ran\n")
    wired.write("level-2/backend/Makefile", "all:\n\t@true\n")
    assert try_commit(wired, msg("levels"), "level-1/backend/Makefile", "level-2/backend/Makefile", no_verify=True).rc == 0
    res = wired.push("origin", "main")
    out = wired.output(res)
    assert res.rc == 0, res
    assert "level-one-check-ran" in out
    assert "warn  level-2/backend" in out
    assert wired.remote_head() == wired.head()


def test_pre_push_failing_level_check_blocks(wired: WiredRepo) -> None:
    remote = wired.remote_head()
    wired.write("level-1/backend/Makefile", "check:\n\t@exit 3\n")
    assert try_commit(wired, msg("broken level"), "level-1/backend/Makefile", no_verify=True).rc == 0
    res = wired.push("origin", "main")
    assert res.rc != 0
    assert wired.remote_head() == remote


# --------------------------------------------------------------------------- first push (W10)


@pytest.fixture(scope="module")
def first_push(tmp_path_factory: pytest.TempPathFactory, uv_cache: Path) -> tuple[WiredRepo, str, Secret]:
    """Hooks on, never pushed, an EMPTY bare remote, and a fake only gitleaks sees in an intermediate commit.

    Blocked pushes change nothing, so the tests below can share one repository.
    """
    base = tmp_path_factory.mktemp("first-push")
    repo = bare_wired_repo(base, uv_cache)
    assert repo.make("hooks").rc == 0
    repo.write("README.md", "# demo\n")
    assert try_commit(repo, msg("init"), *WIRED_FILES, ".githooks", "README.md", no_verify=True).rc == 0
    pat = make_fake("github-pat", "first-push")  # a gitleaks built-in rule; the Python guard has none
    planted = _plant_then_remove(repo, pat)
    repo.git("init", "-q", "--bare", str(base / "remote.git"))
    repo.git("remote", "add", "origin", "../remote.git")
    return repo, planted, pat


@pytest.mark.parametrize("branch", ["main", "feature-x"])
def test_first_push_of_new_branch_to_empty_remote_is_blocked(first_push, branch: str) -> None:  # noqa: ANN001
    repo, planted, pat = first_push
    res = repo.push("-u", "origin", f"HEAD:refs/heads/{branch}")
    assert_push_blocked(repo, res, "github-pat", "")
    assert planted in repo.output(res)
    assert repo.remote_head(f"refs/heads/{branch}") == ""
    assert repo.git("--git-dir", str(repo.root.parent / "remote.git"), "for-each-ref").stdout == ""
    assert_absent_all(res, [pat], f"first-push-{branch}")
