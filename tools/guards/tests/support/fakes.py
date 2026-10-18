"""Safe fakes, a clean subprocess runner and a value-free leak assertion.

Rules this module enforces (plan PLAN-CA-0005 §9.3):

* No secret-shaped literal lives in the repository. Fakes are assembled at
  runtime from fragments of three characters or fewer, with a seeded RNG.
* A fake's ``repr``/``str`` never shows its value; ``reveal()`` is only for
  writing it to a file or to a process's stdin, never to ``argv``.
* ``RunResult`` shows only the return code and output lengths.
* ``assert_absent`` fails naming the rule, a fingerprint and the channel,
  never the value. ``assert value not in out`` is forbidden by convention:
  pytest would print the value in the assertion diff.

This file lives outside the ``test_*.py`` pattern, so pytest does not rewrite
its assertions.
"""

from __future__ import annotations

import hashlib
import os
import random
import shutil
import string
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

GUARDS_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = GUARDS_ROOT.parents[1]

_ALNUM = string.ascii_letters + string.digits
_B32 = string.ascii_uppercase + "234567"

# Environment variables that would redirect git to another repository.
FORBIDDEN_GIT_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")

NOREPLY_EMAIL = "1000+dev@users.noreply.github.com"


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:8]


@dataclass(frozen=True)
class Secret:
    """A fake value whose representation never reveals it."""

    rule: str
    _value: str = field(repr=False)

    @property
    def fp(self) -> str:
        return fingerprint(self._value)

    def reveal(self) -> str:
        """Return the value. Only for writing files or feeding stdin."""
        return self._value

    def __repr__(self) -> str:
        return f"<fake {self.rule} {self.fp}>"

    __str__ = __repr__


def _body(rng: random.Random, alphabet: str, n: int) -> str:
    return "".join(rng.choice(alphabet) for _ in range(n))


def make_fake(rule: str, seed: str | int) -> Secret:
    """Assemble a fake for ``rule`` from short fragments and a seeded RNG."""
    rng = random.Random(f"{rule}:{seed}")  # noqa: S311 - deterministic fakes, not crypto
    if rule == "github-pat":
        value = "gh" + "p_" + _body(rng, _ALNUM, 36)
    elif rule == "aws-access-token":
        value = "AK" + "IA" + _body(rng, _B32, 16)
    elif rule == "personal-email":
        local = _body(rng, string.ascii_lowercase, 9)
        value = local + "@" + "zq" + _body(rng, string.ascii_lowercase, 6) + ".i" + "o"
    elif rule == "real-name":
        first = "Zq" + _body(rng, string.ascii_lowercase, 6)
        last = "Xv" + _body(rng, string.ascii_lowercase, 7)
        value = f"{first} {last}"
    elif rule == "private-path":
        value = "/op" + "t/" + "vau" + "lt-" + _body(rng, string.ascii_lowercase, 10) + "/"
    elif rule == "dsn-password":
        scheme = "pos" + "tgr" + "es"
        value = scheme + ":" + "//" + "app" + ":" + _body(rng, _ALNUM, 20) + "@" + "db.te" + "st:5432/x"
    elif rule == "local-path":
        value = "/Us" + "ers/" + _body(rng, string.ascii_lowercase, 8) + "/pr" + "oj"
    else:  # pragma: no cover - programming error in a test
        raise ValueError(f"unknown fake rule {rule!r}")
    return Secret(rule, value)


def email(local: str, domain: str) -> str:
    """Build an e-mail address at runtime so no literal sits in the source."""
    return local + "@" + domain


@dataclass(frozen=True)
class RunResult:
    rc: int
    stdout: str = field(repr=False)
    stderr: str = field(repr=False)

    def __repr__(self) -> str:
        return f"RunResult(rc={self.rc}, stdout={len(self.stdout)}B, stderr={len(self.stderr)}B)"

    @property
    def findings(self) -> list[tuple[str, ...]]:
        return [tuple(line.split("\t")) for line in self.stdout.splitlines() if line]

    @property
    def rules(self) -> set[str]:
        return {f[0] for f in self.findings}


def clean_env(
    home: Path,
    *,
    extra: dict[str, str] | None = None,
    path: str | None = None,
) -> dict[str, str]:
    """A minimal, deterministic environment for git and for the guards."""
    home.mkdir(parents=True, exist_ok=True)
    (home / ".config").mkdir(exist_ok=True)
    if path is None:
        parts = []
        local_tools = REPO_ROOT / ".tools" / "bin"
        if local_tools.is_dir():
            parts.append(str(local_tools))
        parts.append(os.environ.get("PATH", "/usr/bin:/bin"))
        path = os.pathsep.join(parts)
    env = {
        "PATH": path,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TEMPLATE_DIR": "",
        "LC_ALL": "C",
        "TZ": "UTC",
        "PYTHONPATH": str(GUARDS_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for key in ("CI", "GUARDS_ALLOW_SKIP", "TMPDIR"):
        if key in os.environ:
            env[key] = os.environ[key]
    if extra:
        env.update(extra)
    for var in FORBIDDEN_GIT_VARS:
        env.pop(var, None)
    return env


def run(
    argv: list[str],
    cwd: Path,
    input: str | bytes | None = None,  # noqa: A002 - mirrors subprocess
    *,
    home: Path | None = None,
    extra_env: dict[str, str] | None = None,
    path: str | None = None,
    timeout: int = 30,
) -> RunResult:
    """Run a command with a clean env. Fakes go through ``input``, never argv."""
    env = clean_env(home or (cwd.parent / ".home"), extra=extra_env, path=path)
    data = input.encode() if isinstance(input, str) else input
    proc = subprocess.run(  # noqa: S603 - argv is an explicit list
        argv,
        cwd=cwd,
        env=env,
        input=data,
        stdin=None if data is not None else subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return RunResult(
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


def guards(args: list[str], cwd: Path, input: str | None = None, **kw: object) -> RunResult:  # noqa: A002
    return run([sys.executable, "-m", "guards", *args], cwd, input, **kw)  # type: ignore[arg-type]


def assert_absent(text: str, secret: Secret, channel: str) -> None:
    """Fail if ``secret`` (or any 10-char window of it) appears in ``text``."""
    value = secret.reveal()
    windows = {value} | {value[i : i + 10] for i in range(max(0, len(value) - 9))}
    if any(w in text for w in windows):
        pytest.fail(
            f"value leak: rule={secret.rule} fp={secret.fp} channel={channel}",
            pytrace=False,
        )


def assert_absent_all(result: RunResult, secrets: list[Secret], channel: str) -> None:
    for s in secrets:
        assert_absent(result.stdout, s, f"{channel}:stdout")
        assert_absent(result.stderr, s, f"{channel}:stderr")


class GitRepo:
    """A throwaway repository under ``tmp_path`` with a noreply identity."""

    def __init__(self, root: Path, home: Path, env: dict[str, str] | None = None) -> None:
        self.root = root
        self.home = home
        self.env: dict[str, str] = dict(env or {})  # added to every command this repo runs

    def git(
        self,
        *args: str,
        input: str | None = None,  # noqa: A002
        extra_env: dict[str, str] | None = None,
        check: bool = True,
    ) -> RunResult:
        res = run(["git", *args], self.root, input, home=self.home, extra_env={**self.env, **(extra_env or {})})
        if check and res.rc != 0:
            raise AssertionError(f"git {args[0]} failed rc={res.rc}")
        return res

    def write(self, rel: str, content: str | bytes) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")
        return p

    def commit(self, message: str, *paths: str, extra_env: dict[str, str] | None = None) -> str:
        if paths:
            self.git("add", "-f", "--", *paths)
        self.git("commit", "-q", "--allow-empty", "-F", "-", input=message, extra_env=extra_env)
        return self.head()

    def head(self, ref: str = "HEAD") -> str:
        return self.git("rev-parse", ref).stdout.strip()

    def guards(
        self,
        *args: str,
        input: str | None = None,  # noqa: A002
        path: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> RunResult:
        return guards(list(args), self.root, input, home=self.home, path=path, extra_env=extra_env)


GITLEAKS_CONFIG = REPO_ROOT / ".gitleaks.toml"


def add_gitleaks_config(repo: GitRepo) -> str:
    """Copy the real ``.gitleaks.toml`` into ``repo`` (the guards fail closed without it)."""
    repo.write(".gitleaks.toml", GITLEAKS_CONFIG.read_bytes())
    return ".gitleaks.toml"


def add_real_gitignore(repo: GitRepo) -> str:
    """Copy the real root ``.gitignore`` (the single source of ``path-ignored``)."""
    repo.write(".gitignore", (REPO_ROOT / ".gitignore").read_bytes())
    return ".gitignore"


def git_repo(tmp_path: Path, name: str = "repo") -> GitRepo:
    root = tmp_path / name
    root.mkdir()
    repo = GitRepo(root, tmp_path / ".home")
    repo.git("init", "-q", "-b", "main")
    repo.git("config", "user.name", "Test Dev")
    repo.git("config", "user.email", NOREPLY_EMAIL)
    repo.git("config", "commit.gpgsign", "false")
    repo.git("config", "tag.gpgsign", "false")
    return repo


def msg(desc: str = "do a thing", state: str = "Implementing - 1") -> str:
    """A commit message in the ADR-0012 format."""
    return f"[{state}]{{2026-10-17 PLAN-CA-0005}}: {desc}\n\nbody\n\nlogs: EXEC-LOG\n"


def gitleaks_available() -> bool:
    local = REPO_ROOT / ".tools" / "bin" / "gitleaks"
    return local.is_file() or shutil.which("gitleaks") is not None


def require_gitleaks() -> None:
    """Fail (not skip) without gitleaks, unless explicitly allowed outside CI."""
    if gitleaks_available():
        return
    in_ci = os.environ.get("CI", "") == "true"
    if os.environ.get("GUARDS_ALLOW_SKIP") == "1" and not in_ci:
        pytest.skip("gitleaks absent and GUARDS_ALLOW_SKIP=1 (local only)")
    pytest.fail("gitleaks is required for this test (install it with make setup)", pytrace=False)
