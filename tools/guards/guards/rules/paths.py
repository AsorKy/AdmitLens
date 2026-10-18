"""Forbidden paths: what the root ``.gitignore`` keeps out, re-checked on the index
and on history, so that ``git add -f`` or a missing ignore rule is still caught.

Patterns mirror PLAN-CA-0005 §6.4 and are compared case-insensitively
(``CLAUDE.MD``, ``.Claude/`` and ``Data/`` are hits too). The only per-file
exception is the exact basename ``.env.example``; there is never a
per-directory exception.

``path-ignored`` (any path the ``.gitignore`` files match) is computed by the
CLI with ``git check-ignore``, so ``.gitignore`` stays the single source for
everything these patterns do not spell out.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import PurePosixPath

from guards.findings import NONE_DISABLED, Disabled, Finding

MAX_BYTES = 1024 * 1024  # 1 MiB

ENV_EXCEPTION = ".env.example"

KEY_NAMES = (
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "id_rsa*", "id_ed25519*", "id_ecdsa*",
    "*.age", ".netrc", ".pgpass", ".npmrc", ".pypirc", ".git-credentials",
)
KEY_DIRS = ("secrets",)

LOCAL_NAMES = ("CLAUDE.md", "CLAUDE.local.md", "*.local.md", "*.local.toml", ".mcp.json")
LOCAL_DIRS = (".claude", ".omc", ".tools")

DUMP_NAMES = ("*.db", "*.sqlite*", "*.db-*", "*.dump", "*.sql.gz", "*.bak")
DUMP_DIRS = ("backups", "pgdata")

REPORT_NAMES = ("*.sarif", "gitleaks-report*", ".secrets.baseline")

GITLEAKS_CONFIG = ".gitleaks.toml"


IGNORED_RULE = "path-ignored"


def _any(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(name, p.lower()) for p in patterns)


def check_path(
    path: str, size: int | None = None, disabled: Disabled = NONE_DISABLED, *, is_dir: bool = False
) -> list[Finding]:
    """Findings for one repository-relative POSIX path (line is always 0).

    ``is_dir`` (a gitlink): the last component counts as a directory too, so a
    submodule entry at ``.claude`` or ``data/sub`` is caught like its contents.
    """
    parts = PurePosixPath(path.lower()).parts
    if not parts:
        return []
    name = parts[-1]
    dirs = parts if is_dir else parts[:-1]
    rules: list[str] = []

    if name != ENV_EXCEPTION and (name.startswith(".env") or name.endswith(".env")):
        rules.append("path-env")
    if "data" in dirs:
        rules.append("path-data")
    if _any(name, KEY_NAMES) or any(d in KEY_DIRS for d in dirs):
        rules.append("path-key")
    if _any(name, LOCAL_NAMES) or any(d in LOCAL_DIRS for d in dirs):
        rules.append("path-local-artifact")
    if _any(name, DUMP_NAMES) or any(d in DUMP_DIRS for d in dirs):
        rules.append("path-dump")
    if _any(name, REPORT_NAMES):
        rules.append("path-report")
    if name == ".gitleaksignore":
        rules.append("path-gitleaksignore")
    # gitleaks' default allowlist skips any path matching an unanchored
    # `gitleaks.toml`, so such files would hide content. Only the root config
    # is legitimate.
    if "gitleaks.toml" in path.lower() and path != GITLEAKS_CONFIG:
        rules.append("gitleaks-config-shadow")
    if size is not None and size > MAX_BYTES:
        rules.append("path-too-large")

    return [Finding(r, path, 0) for r in rules if r not in disabled]


def ignored_findings(ignored: set[str], first_commit: dict[str, str | None], disabled: Disabled = NONE_DISABLED) -> list[Finding]:
    """``path-ignored`` for each path in ``ignored``, tagged with the commit that first added it."""
    if IGNORED_RULE in disabled:
        return []
    return [Finding(IGNORED_RULE, p, 0, first_commit.get(p)) for p in sorted(ignored)]
