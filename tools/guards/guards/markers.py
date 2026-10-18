"""Private markers from the ignored ``.guards.local.toml``.

The file holds the operator's real name, personal e-mail and private paths so
that versioned rules never contain them (I8). Values are only ever compared,
never printed: errors name the file, not its content. CI has no such file, so
these checks are local-only by design.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from guards.findings import GuardError

MARKERS_FILE = ".guards.local.toml"


@dataclass(frozen=True)
class Markers:
    real_names: tuple[str, ...] = field(default=(), repr=False)
    personal_emails: tuple[str, ...] = field(default=(), repr=False)
    paths: tuple[str, ...] = field(default=(), repr=False)

    def __repr__(self) -> str:
        return (
            f"Markers(real_names={len(self.real_names)}, "
            f"personal_emails={len(self.personal_emails)}, paths={len(self.paths)})"
        )

    @classmethod
    def empty(cls) -> Markers:
        return cls()

    def name_hit(self, text: str) -> bool:
        low = " ".join(text.lower().split())
        return any(n and " ".join(n.lower().split()) in low for n in self.real_names)

    def email_hit(self, text: str) -> bool:
        low = text.lower()
        return any(e and e.lower() in low for e in self.personal_emails)

    def path_hit(self, text: str) -> bool:
        return any(p and p in text for p in self.paths)

    def any_hit(self, text: str) -> bool:
        return self.name_hit(text) or self.email_hit(text) or self.path_hit(text)


def _strings(table: dict[str, object], key: str) -> tuple[str, ...]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise GuardError(f"{MARKERS_FILE}: [private].{key} must be a list of strings")
    return tuple(v.strip() for v in value if v.strip())


def load_markers(repo_root: Path) -> Markers:
    path = repo_root / MARKERS_FILE
    if not path.exists():
        return Markers.empty()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise GuardError(f"cannot read {MARKERS_FILE}") from None
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        # The decoder's message is not echoed: it could quote the content.
        raise GuardError(f"invalid TOML in {MARKERS_FILE}") from None
    private = data.get("private", {})
    if not isinstance(private, dict):
        raise GuardError(f"{MARKERS_FILE}: [private] must be a table")
    return Markers(
        real_names=_strings(private, "real_names"),
        personal_emails=_strings(private, "personal_emails"),
        paths=_strings(private, "paths"),
    )
