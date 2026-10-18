"""Content rules. Secret shapes are mostly gitleaks' job (W4), with one exception.

* ``content-email``: any e-mail outside the domain allowlist.
* ``content-claude-url``: any ``claude.ai`` URL (ADR-0012 §2.2).
* ``content-phi-header``: a CSV/TSV header that looks like patient data.
* ``content-private-marker``: a value from ``.guards.local.toml``.
* ``content-dsn-password``, ``content-secret-assignment``,
  ``content-secret-assignment-py``: the same regexes as the ``admitlens-*``
  rules of ``.gitleaks.toml`` (kept identical by a test), applied without
  gitleaks' built-in global allowlist. That allowlist, ``(?i)^true|false|null$``,
  cannot be removed and is unanchored, so gitleaks drops any password that
  starts with ``true``, contains ``false`` or ends with ``null`` (D-0001(b)).
  Here only the repo's placeholder allowlist and exact flag values are skipped.

Every rule reports the line only. Matching happens in memory; nothing matched
is ever returned or printed.
"""

from __future__ import annotations

import re

from guards.findings import NONE_DISABLED, Disabled, Finding
from guards.markers import Markers

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
CLAUDE_URL_RE = re.compile(r"(?i)\bhttps?://(?:[a-z0-9-]+\.)*claude\.ai\b")

ALLOWED_EXACT_EMAILS = frozenset({"noreply@github.com"})
ALLOWED_DOMAINS = ("example.com", "users.noreply.github.com")
ALLOWED_TLDS = (".test",)

PHI_DOB = frozenset({"dob", "date_of_birth", "birth_date", "birthdate", "birthday", "dateofbirth"})
PHI_NAME = frozenset({
    "name", "first_name", "last_name", "full_name", "patient_name", "firstname", "lastname",
    "surname", "given_name", "family_name", "middle_name",
})
PHI_IDS = frozenset({"mrn", "medical_record_number", "member_ref"})
CSV_SUFFIXES = (".csv", ".tsv")

# --- Secret-shaped rules mirrored from .gitleaks.toml (RE2 and Python re agree on
# these patterns). tests/test_content_secrets.py asserts they equal the toml.
GITLEAKS_PLACEHOLDERS = (
    r"^__(?:GENERATE|REQUIRED)__$",
    r"^<",
    r"^\$\{",
    r"^\$\$?[A-Za-z_][A-Za-z0-9_]*$",
)
DSN_PASSWORD_REGEX = (
    r"""(?i)\b(?:postgres(?:ql)?(?:\+[a-z0-9_]+)?|rediss?|amqps?|mongodb(?:\+srv)?|mysql(?:\+[a-z0-9_]+)?)"""
    r"""://[^\s:/@'"<>$]+:([^\s:/@'"<>${}]+)@"""
)
SECRET_ASSIGNMENT_PATH = (
    r"""(?i)(?:^|/)(?:\.env(?:\.[^/]*)?|[^/]+\.ya?ml|[^/]+\.toml|[^/]+\.ini|[^/]+\.cfg|docker-compose[^/]*\.yml)$"""
)
SECRET_ASSIGNMENT_REGEX = (
    r"""(?i)(?:PASSWORD|PASSWD|SECRET|TOKEN|APIKEY|API_KEY|PRIVATE_KEY|DSN)["']?[ \t]*[=:][ \t]*["']?([^\s"'#]+)"""
)
SECRET_ASSIGNMENT_ALLOW = (r"^/run/secrets/", r"(?i)^(?:true|false|null|none|~)$")
SECRET_ASSIGNMENT_PY_PATH = r"\.py$"
SECRET_ASSIGNMENT_PY_REGEX = (
    r"""(?mi)^[ \t]*(?:self\.|cls\.)?[a-z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|APIKEY|API_KEY|PRIVATE_KEY|DSN)"""
    r"""[ \t]*(?::[^=\n]+)?=[ \t]*[rb]{0,2}["']([^"'\n]+)["']"""
)

# (finding rule, gitleaks rule it mirrors, path regex or None, regex, extra allowlist)
SECRET_RULES = (
    ("content-dsn-password", "admitlens-dsn-password", None, DSN_PASSWORD_REGEX, ()),
    (
        "content-secret-assignment", "admitlens-secret-assignment",
        SECRET_ASSIGNMENT_PATH, SECRET_ASSIGNMENT_REGEX, SECRET_ASSIGNMENT_ALLOW,
    ),
    ("content-secret-assignment-py", "admitlens-secret-assignment-py", SECRET_ASSIGNMENT_PY_PATH, SECRET_ASSIGNMENT_PY_REGEX, ()),
)  # fmt: skip
_COMPILED = tuple(
    (
        rule,
        re.compile(path) if path else None,
        re.compile(regex),
        tuple(re.compile(a) for a in (*GITLEAKS_PLACEHOLDERS, *extra)),
    )
    for rule, _gl, path, regex, extra in SECRET_RULES
)


def secret_lines(path: str, text: str) -> list[tuple[str, int]]:
    """(rule, line) of each non-placeholder secret-shaped value. The value is never returned."""
    out: list[tuple[str, int]] = []
    for rule, path_re, regex, allow in _COMPILED:
        if path_re is not None and not path_re.search(path):
            continue
        for m in regex.finditer(text):
            value = m.group(1)
            if not value or any(a.search(value) for a in allow):
                continue
            out.append((rule, text.count("\n", 0, m.start(1)) + 1))
    return out


def email_allowed(address: str) -> bool:
    addr = address.lower()
    if addr in ALLOWED_EXACT_EMAILS:
        return True
    domain = addr.rsplit("@", 1)[-1]
    if any(domain == d or domain.endswith("." + d) for d in ALLOWED_DOMAINS):
        return True
    return any(domain.endswith(t) for t in ALLOWED_TLDS)


def disallowed_email_in(line: str) -> bool:
    return any(not email_allowed(m.group(0)) for m in EMAIL_RE.finditer(line))


def is_binary(text: str) -> bool:
    return "\x00" in text[:8192]


def _normalize(cell: str) -> str:
    cell = cell.strip().strip("\"'").strip().lower()
    return re.sub(r"[\s\-]+", "_", cell)


def phi_header(line: str) -> bool:
    for sep in (",", "\t", ";", "|"):
        if sep in line:
            cells = {_normalize(c) for c in line.split(sep)}
            if cells & PHI_IDS or (cells & PHI_DOB and cells & PHI_NAME):
                return True
    return False


def check_content(
    path: str,
    text: str,
    markers: Markers,
    disabled: Disabled = NONE_DISABLED,
    commit: str | None = None,
) -> list[Finding]:
    if is_binary(text):
        return []
    found: list[Finding] = []

    def add(rule: str, line: int) -> None:
        if rule not in disabled:
            found.append(Finding(rule, path, line, commit))

    lines = text.splitlines()
    for no, line in enumerate(lines, start=1):
        if disallowed_email_in(line):
            add("content-email", no)
        if CLAUDE_URL_RE.search(line):
            add("content-claude-url", no)
        if markers.any_hit(line):
            add("content-private-marker", no)

    for rule, no in secret_lines(path, text):
        add(rule, no)

    if path.lower().endswith(CSV_SUFFIXES):
        header_no = next((i for i, ln in enumerate(lines, start=1) if ln.strip()), None)
        if header_no is not None and phi_header(lines[header_no - 1]):
            add("content-phi-header", header_no)
    return found
