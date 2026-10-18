"""Unit tests per rule: positives, negatives, exact (rule, path, line), attribution.

Attribution: running the same positive with the rule disabled yields no finding,
which proves the finding comes from that rule and not from a neighbour.
"""

from __future__ import annotations

import pytest
from support.fakes import email, make_fake

from guards.findings import Finding
from guards.markers import Markers
from guards.rules import content, identity, lint, messages, paths

pytestmark = pytest.mark.unit

NO_MARKERS = Markers.empty()


def _rpl(findings: list[Finding]) -> list[tuple[str, str, int]]:
    return [(f.rule, f.path, f.line) for f in findings]


# --------------------------------------------------------------------------- paths

PATH_POSITIVES = [
    (".env", "path-env"),
    ("level-1/backend/.env.local", "path-env"),
    ("svc/prod.env", "path-env"),
    (".envrc", "path-env"),
    ("data/seeds/facilities.csv", "path-data"),
    ("datagen/data/raw.json", "path-data"),
    ("deploy/server.pem", "path-key"),
    ("home/.ssh/id_ed25519.pub", "path-key"),
    ("secrets/db_password", "path-key"),
    (".pgpass", "path-key"),
    (".claude/settings.json", "path-local-artifact"),
    ("level-1/backend/CLAUDE.md", "path-local-artifact"),
    ("CLAUDE.local.md", "path-local-artifact"),
    (".guards.local.toml", "path-local-artifact"),
    (".mcp.json", "path-local-artifact"),
    (".omc/state/x.json", "path-local-artifact"),
    (".tools/bin/gitleaks", "path-local-artifact"),
    ("level-1/backend/app.db", "path-dump"),
    ("x/dump.sqlite3", "path-dump"),
    ("backups/2026.sql", "path-dump"),
    ("ops/pgdata/PG_VERSION", "path-dump"),
    ("db.sql.gz", "path-dump"),
    ("scan.sarif", "path-report"),
    ("gitleaks-report.json", "path-report"),
    (".secrets.baseline", "path-report"),
    (".gitleaksignore", "path-gitleaksignore"),
    ("level-2/.gitleaksignore", "path-gitleaksignore"),
    ("a-gitleaks.toml", "gitleaks-config-shadow"),
    ("docs/gitleaks.toml", "gitleaks-config-shadow"),
    ("level-1/.gitleaks.toml", "gitleaks-config-shadow"),
    ("x/gitleaks.toml.bak", "gitleaks-config-shadow"),
    ("gitleaks.toml.d/rules.txt", "gitleaks-config-shadow"),
    # Case variants (macOS file systems are case-insensitive; the rules are too).
    ("CLAUDE.MD", "path-local-artifact"),
    ("level-1/Claude.md", "path-local-artifact"),
    (".Claude/settings.json", "path-local-artifact"),
    ("Data/seeds/x.txt", "path-data"),
    (".ENV", "path-env"),
    ("Secrets/db_password", "path-key"),
    ("deploy/SERVER.PEM", "path-key"),
    ("docs/GitLeaks.toml", "gitleaks-config-shadow"),
]

PATH_NEGATIVES = [
    ".env.example",
    "level-1/backend/.env.example",
    "README.md",
    "tools/guards/guards/rules/paths.py",
    "level-1/backend/app/database.py",
    ".github/workflows/guards.yml",
    "docs/env.md",
    "datagen/src/datagen/build.py",
    "uv.lock",
    ".gitleaks.toml",
]


@pytest.mark.parametrize(("path", "rule"), PATH_POSITIVES)
def test_path_positive(path: str, rule: str) -> None:
    found = paths.check_path(path, size=10)
    assert (rule, path, 0) in _rpl(found)
    assert paths.check_path(path, size=10, disabled=frozenset({rule})) == [
        f for f in found if f.rule != rule
    ]


@pytest.mark.parametrize("path", PATH_NEGATIVES)
def test_path_negative(path: str) -> None:
    assert paths.check_path(path, size=10) == []


def test_path_too_large() -> None:
    big = 1024 * 1024 + 1
    assert _rpl(paths.check_path("assets/big.bin", size=big)) == [("path-too-large", "assets/big.bin", 0)]
    assert paths.check_path("assets/ok.bin", size=1024 * 1024) == []
    assert paths.check_path("assets/big.bin", size=big, disabled=frozenset({"path-too-large"})) == []


# --------------------------------------------------------------------------- content: emails


@pytest.mark.parametrize(
    "addr",
    [
        email("someone", "gmail.com"),
        email("noreply", "anthropic.com"),
        email("dev", "corp.example.org"),
    ],
)
def test_content_email_positive(addr: str) -> None:
    text = f"line one\ncontact: {addr}\n"
    found = content.check_content("README.md", text, NO_MARKERS)
    assert _rpl(found) == [("content-email", "README.md", 2)]
    assert content.check_content("README.md", text, NO_MARKERS, disabled=frozenset({"content-email"})) == []


@pytest.mark.parametrize(
    "text",
    [
        "author " + email("1000+dev", "users.noreply.github.com") + "\n",
        "bot " + email("noreply", "github.com") + "\n",
        "fixture " + email("ana", "clinic.test") + " and " + email("bo", "example.com") + "\n",
        'created_at = "2026-10-17T13:24:05.120Z"\n',
        "POSTGRES_PASSWORD=__GENERATE__\nADMITLENS_DATABASE_URL=${ADMITLENS_DATABASE_URL}\n",
    ],
)
def test_content_email_negative(text: str) -> None:
    assert content.check_content(".env.example", text, NO_MARKERS) == []


# --------------------------------------------------------------------------- content: claude URLs


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("see https://" + "claude" + ".ai/code/session_x\n", 1),
        ("a\nb\n<" + "http://" + "claude" + ".ai>\n", 3),
        ("docs: https://www." + "claude" + ".ai/chat\n", 1),
    ],
)
def test_content_claude_url_positive(text: str, line: int) -> None:
    found = content.check_content("notes.md", text, NO_MARKERS)
    assert _rpl(found) == [("content-claude-url", "notes.md", line)]
    assert content.check_content("notes.md", text, NO_MARKERS, disabled=frozenset({"content-claude-url"})) == []


@pytest.mark.parametrize(
    "text",
    ["the model is claude, a product name\n", "https://docs.anthropic.com/en/docs\n", "x = 'claude_ai'\n"],
)
def test_content_claude_url_negative(text: str) -> None:
    assert content.check_content("notes.md", text, NO_MARKERS) == []


# --------------------------------------------------------------------------- content: PHI-like CSV headers


@pytest.mark.parametrize(
    "header",
    [
        "id,first_name,last_name,dob,facility_id",
        '"Patient Name";"Date of Birth";"Zip"',
        "facility_id\tmrn\tadmitted_at",
        "ref,member_ref,status",
    ],
)
def test_content_phi_header_positive(header: str) -> None:
    text = header + "\n1,2,3\n"
    found = content.check_content("tests/fixtures/x.csv", text, NO_MARKERS)
    assert _rpl(found) == [("content-phi-header", "tests/fixtures/x.csv", 1)]
    assert content.check_content("tests/fixtures/x.csv", text, NO_MARKERS, disabled=frozenset({"content-phi-header"})) == []


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("seeds/facilities.csv", "facility_id,name,city,state,beds\n"),
        ("seeds/patients.csv", "patient_ref,birth_year,sex\n"),
        ("docs/schema.md", "first_name,last_name,dob\n"),
    ],
)
def test_content_phi_header_negative(path: str, text: str) -> None:
    assert content.check_content(path, text, NO_MARKERS) == []


# --------------------------------------------------------------------------- content: private markers


def _markers() -> tuple[Markers, list[str]]:
    name = make_fake("real-name", "content").reveal()
    mail = make_fake("personal-email", "content").reveal()
    vault = make_fake("private-path", "content").reveal()
    return Markers(real_names=(name,), personal_emails=(mail,), paths=(vault,)), [name, mail, vault]


@pytest.mark.parametrize("idx", [0, 1, 2])
def test_content_private_marker_positive(idx: int) -> None:
    markers, values = _markers()
    text = "intro\n\nwritten by " + values[idx].upper() if idx == 0 else "intro\n\nref " + values[idx]
    found = [f for f in content.check_content("NOTICE", text, markers) if f.rule == "content-private-marker"]
    assert _rpl(found) == [("content-private-marker", "NOTICE", 3)]
    assert [
        f
        for f in content.check_content("NOTICE", text, markers, disabled=frozenset({"content-private-marker"}))
        if f.rule == "content-private-marker"
    ] == []


def test_content_private_marker_negative() -> None:
    markers, _ = _markers()
    assert content.check_content("NOTICE", "Copyright 2026 AsorKy\n", markers) == []
    assert content.check_content("NOTICE", "anything\n", NO_MARKERS) == []


def test_content_binary_is_skipped() -> None:
    assert content.check_content("img.png", "\x00\x01" + email("a", "gmail.com"), NO_MARKERS) == []


# --------------------------------------------------------------------------- identity


@pytest.mark.parametrize("role", ["author", "committer", "tagger"])
def test_identity_email_positive(role: str) -> None:
    addr = make_fake("personal-email", role).reveal()
    found = identity.check_identity(role, "Test Dev", addr, NO_MARKERS, commit="abc")
    assert [(f.rule, f.path, f.line, f.commit) for f in found] == [("identity-email", f":{role}", 0, "abc")]
    assert identity.check_identity(role, "Test Dev", addr, NO_MARKERS, commit="abc", disabled=frozenset({"identity-email"})) == []


@pytest.mark.parametrize(
    "addr",
    [email("1000+dev", "users.noreply.github.com"), email("noreply", "github.com"), "49699333+dependabot[bot]@users.noreply.github.com"],
)
def test_identity_email_negative(addr: str) -> None:
    assert identity.check_identity("author", "Test Dev", addr, NO_MARKERS, commit="abc") == []


def test_identity_rejects_lookalike_domain() -> None:
    addr = email("x", "users.noreply.github.com.evil.io")
    assert [f.rule for f in identity.check_identity("author", "X", addr, NO_MARKERS, commit=None)] == ["identity-email"]


@pytest.mark.parametrize("role", ["author", "committer", "tagger"])
def test_identity_name_positive(role: str) -> None:
    markers, values = _markers()
    found = identity.check_identity(role, values[0].lower(), email("1+x", "users.noreply.github.com"), markers, commit="c1")
    assert [(f.rule, f.path) for f in found] == [("identity-name", f":{role}")]
    assert identity.check_identity(role, values[0], email("1+x", "users.noreply.github.com"), markers, commit="c1", disabled=frozenset({"identity-name"})) == []


def test_identity_name_negative() -> None:
    markers, _ = _markers()
    assert identity.check_identity("author", "AsorKy", email("1+x", "users.noreply.github.com"), markers, commit=None) == []


# --------------------------------------------------------------------------- messages

VALID_FIRST_LINES = [
    "[Pre-Implementation]{2026-10-17 PLAN-CA-0005}: Baseline before work",
    "[Post-Implementation]{2026-10-17 PLAN-P-0012}: Done",
    "[Rolled-Back]{2026-10-17 PLAN-CA-0005}: Undo W3",
    "[Implementing - 3]{2026-10-17 PLAN-CA-0005}: guards",
    "[Halted - 12]{2026-01-02 PLAN-P-0001}: blocked",
    "[Phase-Complete - 1]{2026-10-17 KATA-K07}: kata done",
    "Merge branch 'feature/x'",
    'Revert "[Implementing - 1]{2026-10-17 PLAN-CA-0005}: x"',
]


@pytest.mark.parametrize("first", VALID_FIRST_LINES)
def test_message_format_negative(first: str) -> None:
    text = first + "\n\nbody at 2026-10-17T13:24:05.120Z\n\nlogs: EXEC-LOG · CHANGES\n"
    assert messages.check_message(text, ":message", NO_MARKERS) == []


@pytest.mark.parametrize(
    "first",
    [
        "fix stuff",
        "[Implementing]{2026-10-17 PLAN-CA-0005}: missing step number",
        "[Pre-Implementation]{2026-10-17 PLAN-X-0005}: bad plan id",
        "[Testing - 1]{17-10-2026 PLAN-CA-0005}: bad date",
        "[Testing - 1]{2026-10-17 PLAN-CA-0005}:  ",
        "[Testing - 1]{2026-10-17 KATA-K1}: short kata",
    ],
)
def test_message_format_positive(first: str) -> None:
    found = messages.check_message(first + "\n", ":message", NO_MARKERS)
    assert _rpl(found) == [("msg-format", ":message", 1)]
    assert messages.check_message(first + "\n", ":message", NO_MARKERS, disabled=frozenset({"msg-format"})) == []


GOOD = "[Implementing - 1]{2026-10-17 PLAN-CA-0005}: ok\n\n"


@pytest.mark.parametrize(
    ("body", "rule", "line"),
    [
        ("Co-Authored-By: Claude <" + email("noreply", "anthropic.com") + ">\n", "msg-claude-coauthor", 3),
        ("x\nco-authored-by: Bot <" + email("bot", "mail.anthropic.com") + ">\n", "msg-claude-coauthor", 4),
        ("Claude-Session: abc\n", "msg-claude-session", 3),
        ("x\ny\nclaude-session:https\n", "msg-claude-session", 5),
        ("see https://" + "claude" + ".ai/code/s\n", "msg-claude-url", 3),
        ("path " + "/Us" + "ers/" + "nico/proj/x\n", "msg-local-path", 3),
        ("path " + "/ho" + "me/" + "nico/x\n", "msg-local-path", 3),
        ("ping " + email("someone", "gmail.com") + "\n", "msg-email", 3),
    ],
)
def test_message_leak_positive(body: str, rule: str, line: int) -> None:
    found = messages.check_message(GOOD + body, ":message", NO_MARKERS, commit="c0ffee")
    assert (rule, ":message", line) in _rpl(found)
    without = messages.check_message(GOOD + body, ":message", NO_MARKERS, disabled=frozenset({rule}))
    assert rule not in {f.rule for f in without}


@pytest.mark.parametrize(
    "body",
    [
        "Co-Authored-By: Pal <" + email("1+pal", "users.noreply.github.com") + ">\n",
        "Signed-off-by: Bot <" + email("noreply", "github.com") + ">\n",
        "ran in the runner workspace and /tmp/x\n",
    ],
)
def test_message_leak_negative(body: str) -> None:
    assert messages.check_message(GOOD + body, ":message", NO_MARKERS) == []


# Dependabot's standard sign-off (GitHub's public bot address), built at runtime.
BOT_ADDR = email("support", "github.com")
SIGNOFF = "Signed-off-by: dependabot[bot] <" + BOT_ADDR + ">"


def test_dependabot_signoff_constant_matches() -> None:
    assert messages.DEPENDABOT_SIGNOFF == SIGNOFF


@pytest.mark.parametrize(
    "body",
    [
        SIGNOFF + "\n",
        "Bumps x from 1 to 2.\n\n---\nupdated-dependencies:\n- dependency-name: x\n...\n\n" + SIGNOFF + "\n",
        SIGNOFF,  # no trailing newline
        SIGNOFF + "\r\n",  # CRLF: splitlines drops the terminator
    ],
)
def test_dependabot_signoff_exempt(body: str) -> None:
    assert messages.check_message(GOOD + body, ":message", NO_MARKERS) == []


@pytest.mark.parametrize(
    ("body", "line"),
    [
        ("contact " + BOT_ADDR + "\n", 3),  # same address, another line
        ("x\n" + SIGNOFF + "\ncc " + BOT_ADDR + "\n", 5),  # exempt line, then the address in the body
        ("Signed-off-by: dependabot[bot] <" + email("support", "gmail.com") + ">\n", 3),
        ("Signed-off-by: dependabot[bot] <" + email("dependabot", "github.com") + ">\n", 3),
        ("Signed-off-by: Some Dev <" + email("some.dev", "gmail.com") + ">\n", 3),
        ("Signed-off-by: dependabot <" + BOT_ADDR + ">\n", 3),  # name not exact
        ("signed-off-by: dependabot[bot] <" + BOT_ADDR + ">\n", 3),  # case differs
        (SIGNOFF + " " + email("x", "gmail.com") + "\n", 3),  # trailing text
        (SIGNOFF + " and more\n", 3),  # trailing text, no second address
        (SIGNOFF + " \n", 3),  # trailing space
        (" " + SIGNOFF + "\n", 3),  # leading space
        ("> " + SIGNOFF + "\n", 3),  # quoted
    ],
)
def test_dependabot_signoff_not_exempt(body: str, line: int) -> None:
    found = messages.check_message(GOOD + body, ":message", NO_MARKERS)
    assert ("msg-email", ":message", line) in _rpl(found)
    assert {f.rule for f in found} == {"msg-email"}


def test_dependabot_signoff_does_not_mask_other_rules() -> None:
    markers, values = _markers()
    found = messages.check_message(GOOD + SIGNOFF + "\n", ":message", markers)
    assert found == []
    found = messages.check_message(GOOD + "by " + values[0] + "\n" + SIGNOFF + "\n", ":message", markers)
    assert _rpl(found) == [("msg-private-marker", ":message", 3)]


def test_message_private_marker() -> None:
    markers, values = _markers()
    found = messages.check_message(GOOD + "by " + values[0] + "\n", ":message", markers)
    assert _rpl(found) == [("msg-private-marker", ":message", 3)]
    assert messages.check_message(GOOD + "by " + values[0] + "\n", ":message", markers, disabled=frozenset({"msg-private-marker"})) == []


def test_message_comment_lines_do_not_count_as_first_line() -> None:
    text = "# Please enter the commit message\n" + GOOD
    assert messages.check_message(text, ":message", NO_MARKERS) == []


# --------------------------------------------------------------------------- lint: Makefiles


@pytest.mark.parametrize(
    ("text", "rule", "line"),
    [
        ("all:\n\t@echo hi\ninclude .env\n", "make-include-env", 3),
        ("-include ../.env\n", "make-include-env", 1),
        ("x:\n\t@psql $(POSTGRES_PASSWORD)\n", "make-secret-var", 2),
        ("DSN := ${ADMITLENS_DATABASE_DSN}\n", "make-secret-var", 1),
        ("x:\n\t@curl -H $(shell echo $(API_TOKEN))\n", "make-secret-var", 2),
        (".ONESHELL:\nall:\n\t@true\n", "make-gnu-construct", 1),
        ("SHA != git rev-parse HEAD\n", "make-gnu-construct", 1),
        ("X ::= 1\n", "make-gnu-construct", 1),
        ("a b &: c\n\t@true\n", "make-gnu-construct", 1),
        ("x:\n\t@$(file >out,hi)\n", "make-gnu-construct", 2),
        (".RECIPEPREFIX = >\n", "make-gnu-construct", 1),
    ],
)
def test_makefile_positive(text: str, rule: str, line: int) -> None:
    found = lint.check_makefile("Makefile", text)
    assert (rule, "Makefile", line) in _rpl(found)
    assert rule not in {f.rule for f in lint.check_makefile("Makefile", text, disabled=frozenset({rule}))}


@pytest.mark.parametrize(
    "text",
    [
        "env:\n\t@sh ops/make-env.sh\n",
        'db-up:\n\t@[ -n "$$POSTGRES_PASSWORD" ] && [ "$$a" != "b" ] && echo ok\n',
        "UV ?= uv\nPY := $(UV) run python\nx:\n\t@$(PY) -m guards lint # $(MAKEFILE_LIST)\n",
        "HOOKS := pre-commit commit-msg\n",
    ],
)
def test_makefile_negative(text: str) -> None:
    assert lint.check_makefile("level-1/backend/Makefile", text) == []


# --------------------------------------------------------------------------- lint: .gitignore


def test_gitignore_negations() -> None:
    text = "# c\n.env\n.env.*\n!.env.example\n!data/README.md\n\\!literal\n!keep.csv\n"
    found = lint.check_gitignore(".gitignore", text)
    assert _rpl(found) == [("gitignore-negation", ".gitignore", 5), ("gitignore-negation", ".gitignore", 7)]
    assert lint.check_gitignore(".gitignore", text, disabled=frozenset({"gitignore-negation"})) == []
    assert lint.check_gitignore("level-1/backend/.gitignore", ".env\n!.env.example\n") == []


# --------------------------------------------------------------------------- lint: workflows

SHA = "a" * 40

GOOD_WF = f"""name: guards
on:
  push:
  pull_request:
  workflow_dispatch:
permissions: {{}}
jobs:
  guards:
    runs-on: ubuntu-24.04
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@{SHA} # v4
        with:
          persist-credentials: false
      - run: make guards-test secrets-scan
      - run: |
          make -C level-1/backend lint
          make -C level-1/backend test-unit
"""


def test_workflow_negative() -> None:
    assert lint.check_workflow(".github/workflows/guards.yml", GOOD_WF) == []
    l1 = GOOD_WF.replace("  push:\n", "  push:\n    paths: [level-1/**]\n")
    assert lint.check_workflow(".github/workflows/level-1-ci.yml", l1) == []


@pytest.mark.parametrize(
    ("mutate", "rule", "line"),
    [
        (lambda t: t.replace("      - run: make guards-test secrets-scan", "      - run: echo hi"), "wf-run-not-make", 16),
        (lambda t: t.replace("          make -C level-1/backend test-unit", "          make x | tee out"), "wf-run-not-make", 17),
        (lambda t: t.replace(f"actions/checkout@{SHA}", "actions/checkout@v4"), "wf-uses-unpinned", 13),
        (lambda t: t.replace("permissions: {}\n", ""), "wf-no-permissions", 1),
        (lambda t: t.replace("  pull_request:\n", "  pull_request_target:\n"), "wf-forbidden-trigger", 4),
        (lambda t: t.replace("  workflow_dispatch:\n", "  workflow_run:\n    workflows: [x]\n"), "wf-forbidden-trigger", 5),
        (lambda t: t.replace("make guards-test secrets-scan", "make t T=${{ github.event.head_commit.message }}"), "wf-event-in-run", 16),
        (lambda t: t.replace("  push:\n", "  push:\n    paths: [x/**]\n"), "wf-guards-paths", 4),
    ],
)
def test_workflow_positive(mutate, rule: str, line: int) -> None:  # noqa: ANN001
    text = mutate(GOOD_WF)
    found = lint.check_workflow(".github/workflows/guards.yml", text)
    assert (rule, ".github/workflows/guards.yml", line) in _rpl(found)
    assert rule not in {f.rule for f in lint.check_workflow(".github/workflows/guards.yml", text, disabled=frozenset({rule}))}


def test_workflow_invalid_yaml_is_a_finding() -> None:
    found = lint.check_workflow(".github/workflows/x.yml", "on: [push\njobs: {\n")
    assert [f.rule for f in found] == ["wf-yaml-invalid"]
    assert found[0].line >= 1


def test_lint_dispatch() -> None:
    assert [f.rule for f in lint.lint_file("a/b/rules.mk", "include .env\n")] == ["make-include-env"]
    assert [f.rule for f in lint.lint_file("sub/.gitignore", "!x\n")] == ["gitignore-negation"]
    assert lint.lint_file("docs/workflows/x.yml", "nonsense: [\n") == []
    assert lint.lint_file(".github/workflows/x.yaml", "on: push\njobs: {}\n")[0].rule == "wf-no-permissions"
