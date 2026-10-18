"""Lint for the two files that steer the scanners: ``.gitattributes`` and ``.gitleaks.toml``."""

from __future__ import annotations

import tomllib

import pytest
from support.fakes import REPO_ROOT

from guards.rules import lint

pytestmark = pytest.mark.unit

REAL_CONFIG = (REPO_ROOT / ".gitleaks.toml").read_text(encoding="utf-8")
REAL_ATTRIBUTES = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")


def _rules(path: str, text: str) -> list[tuple[str, int]]:
    return [(f.rule, f.line) for f in lint.lint_file(path, text)]


# --------------------------------------------------------------------------- .gitattributes


def test_real_gitattributes_passes() -> None:
    assert _rules(".gitattributes", REAL_ATTRIBUTES) == []


@pytest.mark.parametrize(
    "line",
    [
        "*.png binary",
        "*.PDF -diff",
        "assets/**/*.woff2 binary",
        "**/*.whl -diff",
        "*.png diff=exif",
        "*.txt text eol=lf",
        "*.py diff",
        "* text=auto eol=lf",
        "# *.txt -diff",
    ],
)
def test_gitattributes_negative(line: str) -> None:
    assert _rules("sub/.gitattributes", line + "\n") == []


@pytest.mark.parametrize(
    ("text", "rule", "line"),
    [
        ("*.txt -diff\n", "gitattributes-diff", 1),
        ("x\n*.txt binary\n", "gitattributes-diff", 2),
        ("*.py diff=foo\n", "gitattributes-diff", 1),
        ("* -diff\n", "gitattributes-diff", 1),
        ("secret.png.txt binary\n", "gitattributes-diff", 1),
        ("*.p[n]g binary\n", "gitattributes-diff", 1),
        ("[attr]quiet -diff\n", "gitattributes-macro", 1),
        ("*.csv binary\n", "gitattributes-diff", 1),
    ],
)
def test_gitattributes_positive(text: str, rule: str, line: int) -> None:
    assert _rules(".gitattributes", text) == [(rule, line)]
    assert _rules(".GitAttributes", text) == [(rule, line)]


# --------------------------------------------------------------------------- .gitleaks.toml


def test_real_config_passes() -> None:
    assert _rules(".gitleaks.toml", REAL_CONFIG) == []


def test_required_rules_equal_the_real_custom_rules() -> None:
    ids = {r["id"] for r in tomllib.loads(REAL_CONFIG)["rules"]}
    assert ids == lint.REQUIRED_GITLEAKS_RULES


def test_only_the_root_config_is_linted() -> None:
    assert lint.is_lint_target(".gitleaks.toml")
    assert not lint.is_lint_target("sub/.gitleaks.toml")  # a shadow: path rule, not lint


def _drop_rule(text: str, rule_id: str) -> str:
    start = text.index(f'id = "{rule_id}"')
    block = text.rindex("[[rules]]", 0, start)
    end = text.find("[[rules]]", start)
    return text[:block] + (text[end:] if end >= 0 else "")


WEAKENINGS = [
    ("use-default-off", REAL_CONFIG.replace("useDefault = true", "useDefault = false"), "gitleaks-config-extend"),
    ("disabled-rules", REAL_CONFIG.replace("useDefault = true", 'useDefault = true\ndisabledRules = ["x"]'), "gitleaks-config-extend"),
    (
        "global-paths",
        REAL_CONFIG.replace('regexTarget = "secret"\nregexes = [', "regexTarget = \"secret\"\npaths = ['''x''']\nregexes = [", 1),
        "gitleaks-config-allowlist",
    ),
    (
        "global-stopwords",
        REAL_CONFIG + "\n[[allowlists]]\nstopwords = ['''tok''']\n",
        "gitleaks-config-allowlist",
    ),
    (
        "new-regex-allowlist",
        REAL_CONFIG + "\n[[allowlists]]\nregexes = ['''^gh''']\n",
        "gitleaks-config-allowlist",
    ),
    (
        "regex-target-line",
        REAL_CONFIG.replace("regexTarget = \"match\"", "regexTarget = \"line\""),
        "gitleaks-config-allowlist",
    ),
    ("commits", REAL_CONFIG + "\n[allowlist]\ncommits = ['''abc''']\n", "gitleaks-config-allowlist"),
    ("rule-missing", _drop_rule(REAL_CONFIG, "admitlens-ssn"), "gitleaks-config-rule-missing"),
    (
        "override-default-rule",
        REAL_CONFIG + "\n[[rules]]\nid = \"github-pat\"\nregex = '''x^'''\n",
        "gitleaks-config-rule-id",
    ),
    (
        "duplicate-id",
        REAL_CONFIG + "\n[[rules]]\nid = \"admitlens-ssn\"\nregex = '''x^'''\n",
        "gitleaks-config-rule-id",
    ),
    (
        "mirrored-regex-changed",
        REAL_CONFIG.replace("secretGroup = 1\nkeywords = [\"postgres\"", "secretGroup = 2\nkeywords = [\"postgres\"").replace(
            "rediss?|amqps?", "rediss?", 1
        ),
        "gitleaks-config-rule-changed",
    ),
    ("skip-report", REAL_CONFIG.replace('id = "admitlens-ssn"', 'id = "admitlens-ssn"\nskipReport = true'), "gitleaks-config-key"),
    ("not-toml", "[[rules]\n", "gitleaks-config-invalid"),
]


@pytest.mark.parametrize(("case", "text", "rule"), WEAKENINGS, ids=[w[0] for w in WEAKENINGS])
def test_weakened_config_is_a_finding(case: str, text: str, rule: str) -> None:
    assert text != REAL_CONFIG, case
    assert rule in {r for r, _ in _rules(".gitleaks.toml", text)}, case
