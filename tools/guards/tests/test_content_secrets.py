"""D-0001(b): the Python mirrors of the secret-shaped ``admitlens-*`` gitleaks rules.

gitleaks' built-in global allowlist ``(?i)^true|false|null$`` cannot be removed
and is unanchored, so it drops passwords that start with ``true``, contain
``false`` or end with ``null``. ``content-dsn-password``,
``content-secret-assignment`` and ``content-secret-assignment-py`` use the same
regexes (kept identical by ``test_regexes_match_the_gitleaks_config``) with only
the repo's placeholder allowlist and exact flag values.

Fakes are built at runtime from short fragments; only rule and line are read.
"""

from __future__ import annotations

import random
import string
import tomllib
from pathlib import Path

import pytest
from support.fakes import REPO_ROOT, git_repo, gitleaks_available, require_gitleaks

from guards.gitleaks import Gitleaks
from guards.markers import Markers
from guards.rules import content

CONFIG = REPO_ROOT / ".gitleaks.toml"
SHAPES = ("starts-true", "contains-false", "ends-null")


def _word(seed: str, n: int = 9) -> str:
    rng = random.Random(seed)  # noqa: S311 - deterministic fakes
    return "".join(rng.choice(string.ascii_lowercase) for _ in range(n))


def _evasive(shape: str, seed: str) -> str:
    w = _word(f"{shape}:{seed}")
    return {"starts-true": "tr" + "ue" + w, "contains-false": w[:4] + "fal" + "se" + w[4:], "ends-null": w + "nu" + "ll"}[shape]


def _files(seed: str) -> dict[str, tuple[str, str]]:
    """path -> (text, expected rule) for every evasion shape x {DSN, YAML, Python}."""
    name = "DB_" + "PASS" + "WORD"
    out = {}
    for shape in SHAPES:
        v = _evasive(shape, seed)
        out[f"dsn-{shape}.txt"] = ("url = pos" + "tgres://app:" + v + "@db.test:5432/x\n", "content-dsn-password")
        out[f"conf-{shape}.yml"] = (f"svc:\n  {name}: {v}\n", "content-secret-assignment")
        out[f"set-{shape}.py"] = (f'import os\n{name} = "{v}"\n', "content-secret-assignment-py")
    return out


def _rules(path: str, text: str) -> list[tuple[str, int]]:
    return [(f.rule, f.line) for f in content.check_content(path, text, Markers.empty()) if f.rule.startswith("content-")]


def _line_of(text: str, needle: str) -> int:
    return next(i for i, ln in enumerate(text.splitlines(), start=1) if needle in ln)


# --------------------------------------------------------------------------- sync with .gitleaks.toml


def test_regexes_match_the_gitleaks_config() -> None:
    data = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    by_id = {r["id"]: r for r in data["rules"]}
    for _rule, gl_rule, path, regex, extra in content.SECRET_RULES:
        rule = by_id[gl_rule]
        assert rule["regex"] == regex, gl_rule
        assert rule.get("path") == path, gl_rule
        rule_allow = [x for a in rule.get("allowlists", []) for x in a["regexes"]]
        assert rule_allow == list(extra), gl_rule
    global_allow = [x for a in data["allowlists"] for x in a["regexes"]]
    assert global_allow == list(content.GITLEAKS_PLACEHOLDERS)


# --------------------------------------------------------------------------- unit


@pytest.mark.parametrize("path", sorted(_files("unit")))
def test_evasive_values_are_found(path: str) -> None:
    text, rule = _files("unit")[path]
    assert _rules(path, text) == [(rule, _line_of(text, "://" if path.startswith("dsn-") else "WORD"))]


@pytest.mark.parametrize(
    ("path", "text"),
    [
        (".env.example", "X_PASSWORD=__GENERATE__\nY_SECRET=__REQUIRED__\nZ_DSN=${Z_DSN}\nA_TOKEN=<paste>\nB_TOKEN=$B\n"),
        ("app.yml", "use_secret: true\nx_token: False\ny_password: null\nz_secret: ~\nw_token: None\n"),
        ("compose.yml", "    POSTGRES_PASSWORD_FILE: /run/secrets/db_password\n"),
        ("ci.yml", "env:\n  X_TOKEN: ${{ secrets.X_TOKEN }}\n  Y_PASSWORD: $$Y_PASSWORD\n"),
        ("notes.txt", "pos" + "tgresql://app:${POSTGRES_PASSWORD}@db/x\nredis://:__REQUIRED__@cache:6379/0\n"),
        ("settings.py", 'x_password = os.environ["X_PASSWORD"]\ndb_password: str = Field(...)\n'),
        ("notes.md", "user_password = abcdefgh\n"),
    ],
)
def test_placeholders_and_flags_are_not_found(path: str, text: str) -> None:
    assert _rules(path, text) == []


# --------------------------------------------------------------------------- canary


@pytest.mark.gitleaks
def test_canary_gitleaks_misses_what_the_python_rules_find(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Documents D-0001(b): gitleaks returns nothing on these shapes; the Python rules do.

    If gitleaks ever starts finding them, this canary fails on purpose: revisit
    whether the Python mirrors are still needed.
    """
    require_gitleaks()
    local = REPO_ROOT / ".tools" / "bin" / "gitleaks"
    if local.is_file():
        monkeypatch.setenv("GUARDS_GITLEAKS", str(local))
    assert gitleaks_available()
    repo = git_repo(tmp_path)
    scan = tmp_path / "scan"
    files = _files("canary")
    for path, (text, _rule) in files.items():
        (scan / path).parent.mkdir(parents=True, exist_ok=True)
        (scan / path).write_text(text, encoding="utf-8")
    assert Gitleaks(repo.root).directory(scan, [CONFIG.read_bytes()]) == []
    for path, (text, rule) in files.items():
        assert [r for r, _ in _rules(path, text)] == [rule], path
