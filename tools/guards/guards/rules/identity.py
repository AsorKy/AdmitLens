"""Author, committer and tagger identity (PLAN-CA-0005 §6.4).

E-mail must end in ``@users.noreply.github.com`` or be ``noreply@github.com``
(web merges, Dependabot). The name must not contain a private real name from
``.guards.local.toml``. Findings use the pseudo-path ``:<role>`` and line 0.
"""

from __future__ import annotations

from guards.findings import NONE_DISABLED, Disabled, Finding
from guards.markers import Markers

NOREPLY_SUFFIX = "@users.noreply.github.com"
NOREPLY_EXACT = "noreply@github.com"
ROLES = ("author", "committer", "tagger")


def email_ok(email: str) -> bool:
    addr = email.strip().strip("<>").strip().lower()
    local = addr[: -len(NOREPLY_SUFFIX)] if addr.endswith(NOREPLY_SUFFIX) else ""
    return addr == NOREPLY_EXACT or (bool(local) and "@" not in local)


def check_identity(
    role: str,
    name: str,
    email: str,
    markers: Markers,
    commit: str | None,
    disabled: Disabled = NONE_DISABLED,
) -> list[Finding]:
    if role not in ROLES:
        raise ValueError(f"unknown role {role}")
    found: list[Finding] = []
    if "identity-email" not in disabled and not email_ok(email):
        found.append(Finding("identity-email", f":{role}", 0, commit))
    if "identity-name" not in disabled and markers.name_hit(name):
        found.append(Finding("identity-name", f":{role}", 0, commit))
    return found
