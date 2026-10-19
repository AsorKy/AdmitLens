"""The real root ``.gitignore``: exact ``git check-ignore`` codes and negations.

``git check-ignore -q --no-index`` returns 0 when a path is ignored, 1 when it
is not and 128 on error; the test asserts the exact code so an error never
passes as "not ignored". ``--no-index`` tests the patterns, not the index.

The checks run in a throwaway repository holding only a copy of the real
``.gitignore``: the real clone's ``.git/info/exclude`` and the machine's global
excludes are private to this machine and must not decide the result.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from support.fakes import REPO_ROOT, git_repo, run

from guards.rules import lint

pytestmark = pytest.mark.git

GITIGNORE = REPO_ROOT / ".gitignore"

IGNORED = [
    ".env",
    ".env.local",
    ".env.production",
    "level-1/backend/.env",
    "svc/prod.env",
    ".envrc",
    "data/seeds/facilities.csv",
    "level-1/backend/data/x.json",
    "deploy/server.pem",
    "id_ed25519",
    "secrets/db_password",
    ".pgpass",
    ".netrc",
    ".guards.local.toml",
    ".claude/settings.json",
    "level-1/backend/.claude/agents/x.md",
    "CLAUDE.md",
    "level-1/backend/CLAUDE.md",
    "CLAUDE.local.md",
    ".mcp.json",
    ".omc/state/x.json",
    ".tools/bin/gitleaks",
    "app.db",
    "pgdata/PG_VERSION",
    "scan.sarif",
    "gitleaks-report.json",
    ".secrets.baseline",
    "tools/guards/.venv/bin/python",
    "dump.sql",
    "dump-2026-10-17.sql",
    "backups/admitlens_dump.sql",
    "level-1/backend/prod_dump.sql",
    "export.sql.gz",
    "export.sql.bz2",
    "export.sql.xz",
    "coverage/index.html",
    "level-1/frontend/coverage/lcov.info",
]

NOT_IGNORED = [
    ".env.example",
    "level-1/backend/.env.example",
    ".githooks/pre-commit",
    ".gitleaks.toml",
    ".guards.toml",
    "tools/guards/uv.lock",
    "tools/guards/pyproject.toml",
    ".github/workflows/guards.yml",
    "Makefile",
    "README.md",
    "level-1/backend/ops/postgres/ensure-test-db.sql",
    "level-1/backend/alembic/versions/0001_x.py",
    "level-1/backend/migrations/0001_init.sql",
]


@pytest.fixture(scope="module")
def copy_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    repo = git_repo(tmp_path_factory.mktemp("gitignore"))
    # Only the real .gitignore decides here: drop the default policy and its exclude line.
    (repo.root / ".guards.toml").unlink()
    (repo.root / ".git" / "info" / "exclude").unlink()
    repo.write(".gitignore", GITIGNORE.read_bytes())
    return repo.root


def _check_ignore(root: Path, rel: str) -> int:
    return run(["git", "check-ignore", "-q", "--no-index", "--", rel], root, home=root.parent / ".home").rc


@pytest.mark.parametrize("rel", IGNORED)
def test_ignored(copy_repo: Path, rel: str) -> None:
    assert _check_ignore(copy_repo, rel) == 0


@pytest.mark.parametrize("rel", NOT_IGNORED)
def test_not_ignored(copy_repo: Path, rel: str) -> None:
    assert _check_ignore(copy_repo, rel) == 1


def test_copy_repo_has_no_private_excludes(copy_repo: Path) -> None:
    exclude = copy_repo / ".git" / "info" / "exclude"
    assert not exclude.exists() or exclude.read_text(encoding="utf-8").strip() == ""


def test_only_allowed_negation() -> None:
    text = GITIGNORE.read_text(encoding="utf-8")
    negations = [line.strip() for line in text.splitlines() if line.strip().startswith("!")]
    assert negations == ["!.env.example"]
    assert lint.check_gitignore(".gitignore", text) == []
