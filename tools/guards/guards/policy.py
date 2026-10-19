"""The versioned identity policy, ``.guards.toml`` at the repo root (D-0017).

It holds only public data: the owner's GitHub login and public alias, and the
commits that are knowingly exempt from ``identity-owner-name``. It exists so
that CI, which has no private markers, still catches the owner's GitHub
noreply address paired with any name other than the alias (a web or ``gh``
merge writes the account's profile *Name* as committer)::

    [identity]
    owner_login = "<login>"
    owner_name = "<public alias>"
    exempt_commits = ["<40-hex sha>"]

The file is required by every mode that checks identities and by ``lint``: a
missing, unreadable or malformed file is rc 2 (fail closed). Unknown keys are
rejected. An exemption is an exact lowercase 40-hex commit sha, never a
pattern. Names are never printed; errors name the file and the key only.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from guards.findings import GuardError
from guards.markers import normalize_name

POLICY_FILE = ".guards.toml"
# Used with fullmatch: "$" alone would accept a trailing newline.
LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
EXEMPT_SHA_RE = re.compile(r"[0-9a-f]{40}")
NOREPLY_DOMAIN = "@users.noreply.github.com"
_TOP_KEYS = frozenset({"identity"})
_IDENTITY_KEYS = frozenset({"owner_login", "owner_name", "exempt_commits"})


@dataclass(frozen=True)
class OwnerPolicy:
    owner_login: str
    owner_name: str = field(repr=False)
    exempt_commits: frozenset[str] = frozenset()

    def owns(self, email: str) -> bool:
        """True for the owner's GitHub noreply address, ``[[<digits>]+]<login>@users.noreply.github.com``.

        An empty digit part (``+<login>@``) still counts as the owner's: fail closed.
        """
        addr = email.strip().strip("<>").strip().lower()
        if not addr.endswith(NOREPLY_DOMAIN):
            return False
        local = addr[: -len(NOREPLY_DOMAIN)]
        digits, plus, login = local.rpartition("+")
        if digits and not (digits.isascii() and digits.isdigit()):
            return False
        return login == self.owner_login.lower()

    def name_ok(self, name: str) -> bool:
        return normalize_name(name) == normalize_name(self.owner_name)

    def flags(self, name: str, email: str, sha: str | None) -> bool:
        """``identity-owner-name``: the owner's address with a name other than the alias."""
        if sha is not None and sha in self.exempt_commits:
            return False
        return self.owns(email) and not self.name_ok(name)


def any_flags(policies: Iterable[OwnerPolicy], name: str, email: str, sha: str | None) -> bool:
    """Stricter of several policies (work tree and published base): one flag is enough."""
    return any(p.flags(name, email, sha) for p in policies)


def is_exempt(policies: Sequence[OwnerPolicy], email: str, sha: str | None) -> bool:
    """The owner-noreply identity of a commit exempt by sha (D-0017, pending history rewrite).

    Every policy must exempt ``sha`` and own ``email``. Only the identity rules
    use this (``identity-owner-name``, and ``identity-name`` for that same
    identity); messages, content and other identities of the commit are still
    checked.
    """
    return (
        sha is not None
        and bool(policies)
        and all(sha in p.exempt_commits and p.owns(email) for p in policies)
    )


def parse_policy(data: bytes, where: str = POLICY_FILE) -> OwnerPolicy:
    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        # The decoder's message is not echoed: it could quote the content.
        raise GuardError(f"invalid TOML in {where}") from None
    if set(doc) - _TOP_KEYS:
        raise GuardError(f"{where}: unknown top-level key (only [identity] is allowed)")
    table = doc.get("identity")
    if not isinstance(table, dict):
        raise GuardError(f"{where}: [identity] table is required")
    if set(table) - _IDENTITY_KEYS:
        raise GuardError(f"{where}: unknown key in [identity]")
    login = table.get("owner_login")
    if not isinstance(login, str) or not LOGIN_RE.fullmatch(login):
        raise GuardError(f"{where}: [identity].owner_login must be a GitHub login")
    name = table.get("owner_name")
    if not isinstance(name, str) or not normalize_name(name):
        raise GuardError(f"{where}: [identity].owner_name must be a non-empty string")
    exempt = table.get("exempt_commits", [])
    if not isinstance(exempt, list) or not all(isinstance(s, str) for s in exempt):
        raise GuardError(f"{where}: [identity].exempt_commits must be a list of strings")
    if not all(EXEMPT_SHA_RE.fullmatch(s) for s in exempt):
        raise GuardError(f"{where}: [identity].exempt_commits entries must be full 40-hex lowercase shas")
    return OwnerPolicy(login, name, frozenset(exempt))


def load_policy(repo_root: Path) -> OwnerPolicy:
    """The work-tree ``.guards.toml``; missing or invalid is rc 2."""
    path = repo_root / POLICY_FILE
    if path.is_symlink() or not path.is_file():
        raise GuardError(f"{POLICY_FILE} is missing (required: owner identity policy)")
    try:
        data = path.read_bytes()
    except OSError:
        raise GuardError(f"cannot read {POLICY_FILE}") from None
    return parse_policy(data)
