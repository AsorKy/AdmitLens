"""Where ``ls-remote`` looks (D-0011), ``history`` under CI, remote names and the git timeout.

* ``prepush`` and ``range`` with a zero base ask the URL the push really goes to
  (``PUSH_URL``, the hook's ``$2``) what ``main`` is. Without ``PUSH_URL`` the
  remote name is used only when push and fetch URLs are one and the same.
* ``range <remote> 000… <tip>`` never trusts the local tracking ref.
* ``history`` with ``CI=true`` needs ``refs/remotes/origin/main``.
* A git call that times out takes its whole process group with it.

URLs here are temp paths; the tests check they never reach the output.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from support.fakes import (
    GitRepo,
    RunResult,
    Secret,
    add_gitleaks_config,
    assert_absent_all,
    git_repo,
    make_fake,
    msg,
    require_gitleaks,
)

from guards import gitutil
from guards.findings import GuardError

pytestmark = pytest.mark.git

ZERO = "0" * 40


class Scene:
    """A clean ``main`` on two bare remotes; ``feat`` = leak commit + clean tip; the leak sits on the fetch remote's main."""

    def __init__(self, tmp_path: Path) -> None:
        self.repo = repo = git_repo(tmp_path)
        repo.write("README.md", "# demo\n")
        self.base = repo.commit(msg("init"), "README.md", add_gitleaks_config(repo))
        self.fetch_url = str(tmp_path / "fetch.git")
        self.push_url = str(tmp_path / "push.git")
        for url in (self.fetch_url, self.push_url):
            repo.git("init", "-q", "--bare", url)
            repo.git("push", "-q", url, "main")
        repo.git("remote", "add", "origin", self.fetch_url)
        repo.git("checkout", "-q", "-b", "feat")
        self.pat: Secret = make_fake("github-pat", f"push-target-{tmp_path.name}")  # gitleaks-only rule
        repo.write("notes.txt", f"token {self.pat.reveal()}\n")
        self.leak = repo.commit(msg("leak"), "notes.txt")
        repo.write("notes.txt", "clean\n")
        self.tip = repo.commit(msg("clean"), "notes.txt")
        # What the fetch remote's main already has: the leak. The push target never saw it.
        repo.git("push", "-q", self.fetch_url, f"{self.leak}:refs/heads/main")

    def prepush(self, push_url: str | None, remote: str = "origin") -> RunResult:
        env = {"PUSH_URL": push_url} if push_url is not None else None
        return self.repo.guards("prepush", remote, input=f"refs/heads/feat {self.tip} refs/heads/feat {ZERO}\n",
                                extra_env=env)

    def no_url_in(self, res: RunResult) -> None:
        out = res.stdout + res.stderr
        leaked = self.fetch_url in out or self.push_url in out
        assert not leaked


@pytest.fixture
def scene(tmp_path: Path) -> Scene:
    require_gitleaks()
    return Scene(tmp_path)


# --------------------------------------------------------------------------- prepush: PUSH_URL


@pytest.mark.gitleaks
def test_prepush_asks_the_push_url_not_the_fetch_url(scene: Scene) -> None:
    """pushurl != url and the fetch remote's main holds the leak: the leak is still scanned (D-0011)."""
    scene.repo.git("config", "remote.origin.pushurl", scene.push_url)
    res = scene.prepush(scene.push_url)
    assert res.rc == 1, res
    assert ("github-pat", "notes.txt", "1", scene.leak) in res.findings
    assert_absent_all(res, [scene.pat], "prepush-push-url")
    scene.no_url_in(res)


@pytest.mark.gitleaks
def test_the_fetch_url_would_have_hidden_the_leak(scene: Scene) -> None:
    """Not vacuous: asking the fetch URL excludes the leak commit, which is the bug the push URL fixes."""
    res = scene.prepush(scene.fetch_url)
    assert res.rc == 0, res


@pytest.mark.gitleaks
def test_prepush_without_push_url_refuses_a_split_remote(scene: Scene) -> None:
    scene.repo.git("config", "remote.origin.pushurl", scene.push_url)
    res = scene.prepush(None)
    assert res.rc == 2, res
    assert "fetch URL" in res.stderr
    scene.no_url_in(res)


@pytest.mark.gitleaks
def test_prepush_without_push_url_refuses_several_urls(scene: Scene) -> None:
    scene.repo.git("remote", "set-url", "--add", "origin", scene.push_url)
    res = scene.prepush(None)
    assert res.rc == 2, res
    scene.no_url_in(res)


@pytest.mark.gitleaks
def test_prepush_without_push_url_refuses_push_instead_of(scene: Scene) -> None:
    scene.repo.git("config", f"url.{scene.push_url}.pushInsteadOf", scene.fetch_url)
    res = scene.prepush(None)
    assert res.rc == 2, res
    scene.no_url_in(res)


@pytest.mark.gitleaks
def test_prepush_refuses_a_push_url_rewritten_for_reads(scene: Scene) -> None:
    """``ls-remote`` applies ``insteadOf``: it would read somewhere else than the push goes."""
    scene.repo.git("config", f"url.{scene.fetch_url}.insteadOf", scene.push_url)
    res = scene.prepush(scene.push_url)
    assert res.rc == 2, res
    assert "rewritten" in res.stderr
    scene.no_url_in(res)


@pytest.mark.gitleaks
def test_prepush_single_url_remote_without_push_url_still_works(scene: Scene) -> None:
    """A normal remote (one url, no pushurl): the remote name is enough, and the scope is the remote's main."""
    scene.repo.git("remote", "set-url", "origin", scene.push_url)
    res = scene.prepush(None)
    assert res.rc == 1, res
    assert ("github-pat", "notes.txt", "1", scene.leak) in res.findings
    scene.repo.git("push", "-q", scene.push_url, f"{scene.leak}:refs/heads/main")
    assert scene.prepush(None).rc == 0  # the leak is now on the push target's main: out of scope


@pytest.mark.gitleaks
def test_prepush_unreachable_push_url_is_rc2_with_its_cause(scene: Scene, tmp_path: Path) -> None:
    res = scene.prepush(str(tmp_path / "absent.git"))
    assert res.rc == 2, res
    assert "git ls-remote failed (rc" in res.stderr


# --------------------------------------------------------------------------- range with a zero base


@pytest.mark.gitleaks
def test_manual_range_ignores_a_forged_tracking_ref(scene: Scene) -> None:
    """``update-ref refs/remotes/origin/main <leak>`` used to hide the leak from ``range origin 000… tip``."""
    scene.repo.git("remote", "set-url", "origin", scene.push_url)
    scene.repo.git("update-ref", "refs/remotes/origin/main", scene.leak)
    res = scene.repo.guards("range", "origin", ZERO, scene.tip)
    assert res.rc == 1, res
    assert ("github-pat", "notes.txt", "1", scene.leak) in res.findings
    assert_absent_all(res, [scene.pat], "range-forged-ref")


@pytest.mark.gitleaks
def test_manual_range_refuses_a_split_remote(scene: Scene) -> None:
    scene.repo.git("config", "remote.origin.pushurl", scene.push_url)
    res = scene.repo.guards("range", "origin", ZERO, scene.tip)
    assert res.rc == 2, res
    scene.no_url_in(res)


@pytest.mark.gitleaks
def test_remote_with_branches_but_no_main_scans_everything(tmp_path: Path) -> None:
    """No ``main`` on the remote (other branches only): every commit of the tip is scanned."""
    require_gitleaks()
    scene = Scene(tmp_path)
    bare = ["--git-dir", scene.push_url]
    scene.repo.git(*bare, "update-ref", "refs/heads/other", scene.base)
    scene.repo.git(*bare, "update-ref", "-d", "refs/heads/main")
    scene.repo.git("remote", "set-url", "origin", scene.push_url)
    res = scene.prepush(None)
    assert res.rc == 1, res
    assert {f[3] for f in res.findings if f[0] == "github-pat"} == {scene.leak}


# --------------------------------------------------------------------------- history under CI (M3b)


def _history_repo(tmp_path: Path) -> GitRepo:
    repo = git_repo(tmp_path)
    repo.write("README.md", "# demo\n")
    repo.commit(msg("init"), "README.md", add_gitleaks_config(repo))
    return repo


@pytest.mark.gitleaks
def test_history_in_ci_without_origin_main_is_rc2(tmp_path: Path) -> None:
    require_gitleaks()
    repo = _history_repo(tmp_path)
    res = repo.guards("history", extra_env={"CI": "true"})
    assert res.rc == 2, res
    assert "refs/remotes/origin/main is missing" in res.stderr


@pytest.mark.gitleaks
def test_history_locally_without_origin_main_is_unchanged(tmp_path: Path) -> None:
    require_gitleaks()
    repo = _history_repo(tmp_path)
    assert repo.guards("history").rc == 0
    assert repo.guards("history", extra_env={"CI": "false"}).rc == 0


@pytest.mark.gitleaks
def test_history_in_ci_with_origin_main_is_clean(tmp_path: Path) -> None:
    require_gitleaks()
    repo = _history_repo(tmp_path)
    bare = tmp_path / "remote.git"
    repo.git("init", "-q", "--bare", str(bare))
    repo.git("remote", "add", "origin", str(bare))
    repo.git("push", "-q", "origin", "main")  # creates refs/remotes/origin/main
    assert repo.guards("history", extra_env={"CI": "true"}).rc == 0


# --------------------------------------------------------------------------- remote names


@pytest.mark.unit
@pytest.mark.parametrize("name", ["origin", "team/fork", "a/b/c", "up-stream_2", "fork.v2", "0rigin"])
def test_valid_remote_names_are_accepted(name: str) -> None:
    assert gitutil.REMOTE_NAME_RE.match(name)


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    ["", "-x", "--upload-pack=x", "a b", "a\tb", "a\nb", "a..b", "../x", "./x", "/x", "a//b", "a/",
     "https://h.test/x", "git@h.test:x", ".hidden"],
)
def test_invalid_remote_names_are_rejected(name: str) -> None:
    assert not gitutil.REMOTE_NAME_RE.match(name)


@pytest.mark.gitleaks
def test_prepush_through_a_remote_name_with_a_slash(scene: Scene) -> None:
    scene.repo.git("remote", "add", "team/fork", scene.push_url)
    assert scene.prepush(None, remote="team/fork").rc == 1  # scoped against push.git's clean main
    assert scene.repo.guards("prepush", "a..b", input="").rc == 2


# --------------------------------------------------------------------------- timeout kills the group


SLOW_TRANSPORT = """#!/bin/sh
sleep 30 &
echo $! >"$GUARDS_TEST_PIDFILE"
wait
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_ls_remote_timeout_keeps_its_cause_and_kills_the_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = git_repo(tmp_path)
    script = tmp_path / "slow-ssh"
    script.write_text(SLOW_TRANSPORT)
    script.chmod(0o755)
    pidfile = tmp_path / "transport.pid"
    monkeypatch.setenv("GIT_SSH_COMMAND", str(script))
    monkeypatch.setenv("GIT_SSH_VARIANT", "simple")
    monkeypatch.setenv("GUARDS_TEST_PIDFILE", str(pidfile))
    monkeypatch.setattr(gitutil, "TIMEOUT", 2)
    start = time.monotonic()
    with pytest.raises(GuardError) as exc:
        gitutil.remote_branch("ssh://guards-fake.invalid/x", "refs/heads/main", repo.root)
    assert time.monotonic() - start < 15
    text = str(exc.value)
    assert "git ls-remote timed out after 2 s" in text
    assert "cannot tell what the remote already has" in text
    assert "guards-fake" not in text
    pid = int(pidfile.read_text().strip())
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    alive = _alive(pid)
    if alive:  # do not leave it behind even when the assertion fails
        os.kill(pid, 9)
    assert not alive, "the transport's child outlived the timeout"

