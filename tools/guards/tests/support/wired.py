"""Throwaway repositories wired like the real one.

A wired repo holds copies of the real root ``Makefile``, ``.githooks/``,
``.gitleaks.toml`` and ``.gitignore``. The Makefile's defaults resolve inside
it: ``tools/guards`` is a symlink to the real guards project (excluded in
``.git/info/exclude``) and ``.tools/bin/`` holds a copy of the real pinned
gitleaks, if any (``.tools/`` is ignored; its pin is versioned).
``GUARDS_DIR`` and ``GITLEAKS`` are ``:=`` in the Makefile, so the environment
cannot override them; ``WiredRepo.make`` turns an ``env`` entry for either
into a command-line assignment, the only override make honours. ``HOME`` and the uv cache are temporary; ``MAKEFLAGS`` is never
inherited.

Building one costs a few hooked git commands, so tests copy a template that
is built once per session (see ``conftest.py``). The remote URL is relative
(``../remote.git``) and the symlink is absolute, so a copied tree stays
self-contained.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from support.fakes import GUARDS_ROOT, NOREPLY_EMAIL, REPO_ROOT, GitRepo, RunResult, msg, run

REAL_GITLEAKS = REPO_ROOT / ".tools" / "bin" / "gitleaks"
WIRED_FILES = ("Makefile", ".gitleaks.toml", ".gitignore")
HOOKS = ("pre-commit", "commit-msg", "pre-push")
ZERO = "0" * 40
# Makefile variables that only a command-line assignment can override.
MAKE_VARS = ("GUARDS_DIR", "GITLEAKS")


class WiredRepo(GitRepo):
    def make(self, *args: str, input: str | None = None, env: dict[str, str] | None = None) -> RunResult:  # noqa: A002
        extra = {**self.env, **(env or {})}
        assigns = [f"{k}={extra.pop(k)}" for k in MAKE_VARS if k in extra]
        return run(["make", "-s", *assigns, *args], self.root, input, home=self.home, extra_env=extra, timeout=120)

    def push(self, *args: str, env: dict[str, str] | None = None) -> RunResult:
        return self.git("push", "-q", *args, extra_env=env, check=False)

    def remote_head(self, ref: str = "refs/heads/main") -> str:
        res = run(["git", "--git-dir", str(self.root.parent / "remote.git"), "rev-parse", "--verify", "-q", ref],
                  self.root, home=self.home)
        return res.stdout.strip() if res.rc == 0 else ""

    def output(self, res: RunResult) -> str:
        return res.stdout + res.stderr


def wired_env(uv_cache: Path) -> dict[str, str]:
    return {"UV_CACHE_DIR": str(uv_cache)}


def copy_wiring(root: Path) -> None:
    for name in WIRED_FILES:
        shutil.copy2(REPO_ROOT / name, root / name)
    shutil.copytree(REPO_ROOT / ".githooks", root / ".githooks")  # copy2 keeps the +x bits


def link_tools(root: Path) -> None:
    """Point the Makefile defaults at the real guards and gitleaks (call after ``git init``)."""
    (root / "tools").mkdir(exist_ok=True)
    (root / "tools" / "guards").symlink_to(GUARDS_ROOT, target_is_directory=True)
    # Copies, never symlinks: an installer writing to the temp repo's .tools/bin
    # must not be able to overwrite the real binary or its install record.
    bindir = root / ".tools" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    if REAL_GITLEAKS.is_file():
        shutil.copy2(REAL_GITLEAKS, bindir / REAL_GITLEAKS.name)
    (root / ".git" / "info").mkdir(exist_ok=True)  # GIT_TEMPLATE_DIR is empty: no info/ by default
    with (root / ".git" / "info" / "exclude").open("a", encoding="utf-8") as fh:
        fh.write("/tools/guards\n")


def bare_wired_repo(base: Path, uv_cache: Path) -> WiredRepo:
    """Initialised, wiring copied, nothing committed, no hooks, no remote."""
    root = base / "repo"
    root.mkdir(parents=True)
    repo = WiredRepo(root, base / ".home", wired_env(uv_cache))
    repo.git("init", "-q", "-b", "main")
    repo.git("config", "user.name", "Test Dev")
    repo.git("config", "user.email", NOREPLY_EMAIL)
    repo.git("config", "commit.gpgsign", "false")
    repo.git("config", "tag.gpgsign", "false")
    copy_wiring(root)
    link_tools(root)
    return repo


def build_template(base: Path, uv_cache: Path) -> None:
    """Hooks on, one clean commit made and pushed through the hooks."""
    repo = bare_wired_repo(base, uv_cache)
    res = repo.make("hooks")
    if res.rc != 0:
        raise AssertionError(f"make hooks failed: {res!r}")
    repo.write("README.md", "# demo\n")
    repo.git("add", "--", *WIRED_FILES, ".githooks", "README.md")
    repo.git("commit", "-q", "-F", "-", input=msg("init"))
    repo.git("init", "-q", "--bare", str(base / "remote.git"))
    repo.git("remote", "add", "origin", "../remote.git")
    pushed = repo.push("-u", "origin", "main")
    if pushed.rc != 0:
        raise AssertionError(f"clean template push failed: {pushed!r}")


def clone_template(template: Path, dest: Path, uv_cache: Path) -> WiredRepo:
    shutil.copytree(template, dest, symlinks=True, dirs_exist_ok=True)
    home = dest / ".home"
    shutil.rmtree(home, ignore_errors=True)
    return WiredRepo(dest / "repo", home, wired_env(uv_cache))
