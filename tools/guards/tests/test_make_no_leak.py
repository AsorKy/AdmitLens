"""The root Makefile never leaks an environment secret, and stays 3.81-portable.

* Dynamic: a distinct random canary in every KE variable name (and a few more
  secret-shaped names) is present in the environment; ``make -n <target>`` for
  every target parsed from the Makefile must not print any of them. ``make -p``
  is never used: it dumps the whole environment by design.
* Static: ``guards lint`` passes on the real Makefile, every recipe line starts
  with ``@``, and the target list is not vacuous.
"""

from __future__ import annotations

import random
import re
import string
from pathlib import Path

import pytest
from support.fakes import REPO_ROOT, Secret, assert_absent_all, run
from support.wired import WiredRepo, bare_wired_repo

pytestmark = [pytest.mark.make, pytest.mark.git]

MAKEFILE = REPO_ROOT / "Makefile"
TARGET_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_-]*)\s*:(?!=)")

EXPECTED_TARGETS = {
    "help", "setup", "hooks", "doctor", "secrets-scan", "guards-test", "lint", "parity", "datagen",
    "hook-pre-commit", "hook-commit-msg", "hook-pre-push",
}

# KE names (PLAN-CA-0005 §6.3) plus generic secret-shaped names.
CANARY_NAMES = (
    "POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PASSWORD", "ADMITLENS_DB_PORT",
    "ADMITLENS_DATABASE_URL", "ADMITLENS_TEST_DATABASE_URL",
    "GITHUB_TOKEN", "API_KEY", "SECRET_KEY",
    "PUSH_URL",  # set by the pre-push hook: read as $$PUSH_URL, never expanded by make
)


def parse_targets(text: str) -> list[str]:
    return sorted({m.group(1) for line in text.splitlines() if (m := TARGET_RE.match(line))})


def _canaries(seed: str) -> dict[str, Secret]:
    rng = random.Random(seed)  # noqa: S311 - canaries, not crypto
    alphabet = string.ascii_letters + string.digits
    return {name: Secret(name, "cn" + "ry" + "".join(rng.choice(alphabet) for _ in range(24))) for name in CANARY_NAMES}


@pytest.fixture
def repo(tmp_path: Path, uv_cache: Path) -> WiredRepo:
    return bare_wired_repo(tmp_path, uv_cache)


def test_target_list_is_not_vacuous() -> None:
    targets = parse_targets(MAKEFILE.read_text(encoding="utf-8"))
    assert EXPECTED_TARGETS <= set(targets)
    assert len(targets) >= len(EXPECTED_TARGETS)


def test_dry_run_of_every_target_prints_no_canary(repo: WiredRepo, request: pytest.FixtureRequest) -> None:
    canaries = _canaries(request.node.nodeid)
    env = {name: s.reveal() for name, s in canaries.items()}
    env.update({"REMOTE": "origin", "MSG_FILE": str(repo.root / ".git" / "MSG")})
    targets = parse_targets(MAKEFILE.read_text(encoding="utf-8"))
    for target in targets:
        res = repo.make("-n", target, env=env)
        assert res.rc == 0, target
        assert_absent_all(res, list(canaries.values()), f"make-n-{target}")


def test_guards_lint_passes_on_the_real_makefile(repo: WiredRepo) -> None:
    res = repo.guards("lint")
    assert res.rc == 0, res
    assert res.stdout == ""


def test_every_recipe_line_starts_with_at() -> None:
    continued = False
    bad = []
    for no, line in enumerate(MAKEFILE.read_text(encoding="utf-8").splitlines(), start=1):
        if line.startswith("\t") and not continued and not line.startswith("\t@"):
            bad.append(no)
        continued = line.startswith("\t") and line.endswith("\\")
    assert bad == []


def test_makefile_never_includes_env_or_uses_dump_flags() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"^\s*-?s?include\b", code, re.MULTILINE)
    assert not re.search(r"\bmake\b[^\n]*\s-p\b", code)
    assert "$(shell" not in code


def test_tool_paths_ignore_the_environment_but_honour_the_command_line(repo: WiredRepo, tmp_path: Path) -> None:
    """GUARDS_DIR and GITLEAKS are ``:=``: an env value is ignored, a command-line one wins."""
    marker = "envmark" + "x7q"
    env_res = run(["make", "-n", "secrets-scan"], repo.root, home=repo.home,
                  extra_env={**repo.env, "GUARDS_DIR": f"/{marker}", "GITLEAKS": f"/{marker}"})
    assert env_res.rc == 0, env_res
    assert marker not in env_res.stdout
    assert "tools/guards" in env_res.stdout and ".tools/bin/gitleaks" in env_res.stdout
    cli_res = run(["make", "-n", f"GITLEAKS=/{marker}", "secrets-scan"], repo.root, home=repo.home, extra_env=repo.env)
    assert cli_res.rc == 0, cli_res
    assert marker in cli_res.stdout
