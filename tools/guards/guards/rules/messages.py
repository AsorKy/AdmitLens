"""Commit and annotated-tag messages (ADR-ADLN-0012 §2.1–§2.2, contract KC).

The first meaningful line must follow the project format, or be a git-made
``Merge …`` / ``Revert "…"`` line. Every line is then checked for AI trailers,
``claude.ai`` URLs, local home paths, non-allowlisted e-mails and private
markers. Secret-shaped content in messages is gitleaks' job (``gitleaks stdin``).
"""

from __future__ import annotations

import re

from guards.findings import NONE_DISABLED, Disabled, Finding
from guards.markers import Markers
from guards.rules.content import CLAUDE_URL_RE, disallowed_email_in

STATES_PLAIN = "Pre-Implementation|Post-Implementation|Rolled-Back"
STATES_STEP = (
    "Implementing|Testing|Debugging|Verifying|Refactoring|Integrating|"
    "Migrating|Step-Applied|Phase-Complete|Halted"
)
FORMAT_RE = re.compile(
    rf"^\[({STATES_PLAIN}|({STATES_STEP}) - \d+)\]"
    r"\{\d{4}-\d{2}-\d{2} (PLAN-(CA|P)-\d{4}|KATA-K\d{2})\}: \S"
)
GIT_MADE_RE = re.compile(r'^(Merge |Revert ")')

COAUTHOR_RE = re.compile(r"(?i)^\s*co-authored-by:.*@([a-z0-9-]+\.)*anthropic\.com\b")
SESSION_RE = re.compile(r"(?i)^\s*claude-session:")
LOCAL_PATH_RE = re.compile(r"(?:/Users|/home)/[^/\s]+/")


def first_line_index(lines: list[str]) -> int | None:
    for i, line in enumerate(lines):
        if line.strip() and not line.startswith("#"):
            return i
    return None


def format_ok(first: str) -> bool:
    return bool(FORMAT_RE.match(first) or GIT_MADE_RE.match(first))


def check_message(
    text: str,
    path: str,
    markers: Markers,
    commit: str | None = None,
    disabled: Disabled = NONE_DISABLED,
    *,
    check_format: bool = True,
) -> list[Finding]:
    found: list[Finding] = []

    def add(rule: str, line: int) -> None:
        if rule not in disabled:
            found.append(Finding(rule, path, line, commit))

    lines = text.splitlines()
    if check_format:
        idx = first_line_index(lines)
        if idx is None or not format_ok(lines[idx]):
            add("msg-format", (idx or 0) + 1)

    for no, line in enumerate(lines, start=1):
        if COAUTHOR_RE.search(line):
            add("msg-claude-coauthor", no)
        if SESSION_RE.search(line):
            add("msg-claude-session", no)
        if CLAUDE_URL_RE.search(line):
            add("msg-claude-url", no)
        if LOCAL_PATH_RE.search(line):
            add("msg-local-path", no)
        if disallowed_email_in(line):
            add("msg-email", no)
        if markers.any_hit(line):
            add("msg-private-marker", no)
    return found
