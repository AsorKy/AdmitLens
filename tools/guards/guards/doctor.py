"""``make doctor``: is this clone set up so the leak guards actually run?

One line per check, ``ok``, ``warn`` or ``FAIL`` plus the check's name. No
value is ever printed: not the e-mail, not the name, not a config value. The
exit code is 1 when any check fails, else 0 (warnings do not fail).

What each check protects (PLAN-CA-0005 §6.4, "Configuración de git"):

* ``core.hooksPath`` and the three hooks: without them nothing runs locally.
* ``user.useConfigOnly``, ``push.default``, ``push.followTags``: no guessed
  identity, no accidental multi-branch or tag pushes.
* ``status.showUntrackedFiles``, ``core.autocrlf``: hygiene (warn only).
* gitleaks at ``$GUARDS_GITLEAKS`` whose sha256 matches the versioned
  extracted-binary pin for this platform (``tools/guards/bin/gitleaks.sha256``).
* no ``.gitleaksignore`` anywhere.
* no non-empty ``.git/info/attributes``: it is local, unreviewed and outranks
  every ``.gitattributes`` file (it could mark text as binary for diff tools).
* ``user.email`` is a GitHub noreply address; ``user.name`` is not a private
  real name from ``.guards.local.toml``. An absent ``.guards.local.toml`` is a
  warning (CI has none); so is an empty ``real_names``: an operator who stays
  anonymous behind a public alias has no real name to protect (D-0012).
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import TextIO

from guards import gitutil
from guards.findings import EXIT_CLEAN, EXIT_FINDINGS, GuardError
from guards.gitleaks import BINARY_ENV, PINNED_VERSION, binary_ok, ensure_no_gitleaksignore
from guards.markers import MARKERS_FILE, load_markers
from guards.rules.identity import NOREPLY_SUFFIX

HOOKS_DIR = ".githooks"
HOOKS = ("pre-commit", "commit-msg", "pre-push")
MAKE_VERSION_ENV = "GUARDS_MAKE_VERSION"
MIN_MAKE = (3, 81)

# (key, expected value, severity when it differs)
GIT_CONFIG = (
    ("core.hooksPath", HOOKS_DIR, "FAIL"),
    ("user.useConfigOnly", "true", "FAIL"),
    ("push.default", "simple", "FAIL"),
    ("push.followTags", "false", "FAIL"),
    ("status.showUntrackedFiles", "all", "warn"),
    ("core.autocrlf", "false", "warn"),
)
OPTIONAL_TOOLS = ("actionlint", "shellcheck")


def _config(repo: Path, key: str) -> str | None:
    """Effective value of ``key``; None when unset. Never printed."""
    if not gitutil.git_ok(["config", "--get", key], repo):
        return None
    return gitutil.git(["config", "--get", key], repo).decode("utf-8", "replace").strip()


def _gitleaks_status(repo: Path) -> str:
    raw = os.environ.get(BINARY_ENV, "") or ".tools/bin/gitleaks"
    p = Path(raw) if Path(raw).is_absolute() else repo / raw
    if not (p.is_file() and os.access(p, os.X_OK)):
        return "FAIL"
    return "ok" if binary_ok(p) else "FAIL"


def _info_attributes_status(repo: Path) -> str:
    try:
        p = gitutil.git_path("info/attributes", repo)
    except GuardError:
        return "FAIL"
    try:
        return "FAIL" if p.exists() and p.read_bytes().strip() else "ok"
    except OSError:
        return "FAIL"


def _make_version_ok(text: str) -> bool:
    m = re.match(r"^(\d+)\.(\d+)", text.strip())
    return bool(m) and (int(m.group(1)), int(m.group(2))) >= MIN_MAKE


def checks(repo: Path) -> list[tuple[str, str]]:
    """(status, check name) pairs, in a stable order."""
    out: list[tuple[str, str]] = []

    for key, expected, severity in GIT_CONFIG:
        out.append(("ok" if _config(repo, key) == expected else severity, f"git config {key}"))

    for name in HOOKS:
        hook = repo / HOOKS_DIR / name
        if not hook.is_file():
            out.append(("FAIL", f"hook {name} present"))
        elif not os.access(hook, os.X_OK):
            out.append(("FAIL", f"hook {name} executable"))
        else:
            out.append(("ok", f"hook {name}"))

    out.append((_gitleaks_status(repo), f"gitleaks {PINNED_VERSION} at the configured path"))

    try:
        ensure_no_gitleaksignore(repo)
        out.append(("ok", "no .gitleaksignore"))
    except GuardError:
        out.append(("FAIL", "no .gitleaksignore"))

    out.append((_info_attributes_status(repo), "no .git/info/attributes"))

    email = (_config(repo, "user.email") or "").lower()
    local = email[: -len(NOREPLY_SUFFIX)] if email.endswith(NOREPLY_SUFFIX) else ""
    noreply = bool(local) and "@" not in local
    out.append(("ok" if noreply else "FAIL", "user.email is a GitHub noreply address"))

    name = _config(repo, "user.name") or ""
    try:
        markers = load_markers(repo)
    except GuardError:
        out.append(("FAIL", f"{MARKERS_FILE} readable"))
        markers = None
    if markers is not None:
        if not (repo / MARKERS_FILE).is_file():
            out.append(("warn", f"{MARKERS_FILE} present (real-name checks are off)"))
        elif not markers.real_names:
            out.append(("warn", f"{MARKERS_FILE} real_names filled in (real-name checks are off)"))
    if not name:
        out.append(("FAIL", "user.name set"))
    elif markers is not None and markers.name_hit(name):
        out.append(("FAIL", "user.name is not a private real name"))
    else:
        out.append(("ok", "user.name is not a private real name"))

    for tool in OPTIONAL_TOOLS:
        out.append(("ok" if shutil.which(tool) else "warn", f"{tool} installed (optional)"))

    make_version = os.environ.get(MAKE_VERSION_ENV, "")
    out.append(("ok" if _make_version_ok(make_version) else "warn", "GNU Make >= 3.81"))
    return out


def run(repo: Path, stream: TextIO) -> int:
    results = checks(repo)
    for status, name in results:
        stream.write(f"{status:<5} {name}\n")
    failed = sum(1 for status, _ in results if status == "FAIL")
    stream.write(f"doctor: {failed} check(s) failed\n" if failed else "doctor: all required checks passed\n")
    return EXIT_FINDINGS if failed else EXIT_CLEAN
