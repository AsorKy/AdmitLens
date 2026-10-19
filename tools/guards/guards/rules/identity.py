"""Author, committer and tagger identity (PLAN-CA-0005 §6.4).

E-mail must end in ``@users.noreply.github.com`` or be ``noreply@github.com``
(web merges, Dependabot). The name must not contain a private real name from
``.guards.local.toml`` (``identity-name``, local only), and the owner's GitHub
noreply address must carry the public alias from the versioned
``.guards.toml`` (``identity-owner-name``, CI-safe, D-0017). Findings use the
pseudo-path ``:<role>`` and line 0; the name is never part of a finding.

A commit listed in ``exempt_commits`` (by every policy in force) is exempt by
sha (pending history rewrite): its owner-noreply identity skips both name
rules. Its other identities, its message and its files are still checked.
"""

from __future__ import annotations

from collections.abc import Sequence

from guards.findings import NONE_DISABLED, Disabled, Finding
from guards.markers import Markers
from guards.policy import OwnerPolicy, any_flags, is_exempt

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
    *,
    owners: Sequence[OwnerPolicy] = (),
) -> list[Finding]:
    if role not in ROLES:
        raise ValueError(f"unknown role {role}")
    found: list[Finding] = []
    if "identity-email" not in disabled and not email_ok(email):
        found.append(Finding("identity-email", f":{role}", 0, commit))
    exempt = is_exempt(owners, email, commit)
    if "identity-name" not in disabled and markers.name_hit(name) and not exempt:
        found.append(Finding("identity-name", f":{role}", 0, commit))
    if "identity-owner-name" not in disabled and any_flags(owners, name, email, commit):
        found.append(Finding("identity-owner-name", f":{role}", 0, commit))
    return found
