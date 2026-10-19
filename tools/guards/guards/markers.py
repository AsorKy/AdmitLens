"""Private markers from the ignored ``.guards.local.toml``.

The file holds the operator's real name, personal e-mail and private paths so
that versioned rules never contain them (I8). Values are only ever compared,
never printed: errors name the file, not its content. CI has no such file, so
these checks are local-only by design.
"""

from __future__ import annotations

import re
import tomllib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from guards.findings import GuardError

MARKERS_FILE = ".guards.local.toml"
_WORD_SPLIT_RE = re.compile(r"[\W_]+")
_WORD_RE = re.compile(r"[^\W_]{4,}")
MIN_WORD_LETTERS = 4


def normalize_name(text: str) -> str:
    """Compare-form of a name: NFKD, no combining marks, ``casefold``, single spaces.

    Accents and case never decide a match: an accented variant, or a commit
    that upper-cases a surname, still matches the plain marker.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.casefold().split())


def name_words(text: str) -> set[str]:
    """Word tokens (letters and digits) of the normalized text."""
    return {w for w in _WORD_SPLIT_RE.split(normalize_name(text)) if w}


@dataclass(frozen=True)
class Markers:
    real_names: tuple[str, ...] = field(default=(), repr=False)
    real_name_words: tuple[str, ...] = field(default=(), repr=False)
    personal_emails: tuple[str, ...] = field(default=(), repr=False)
    paths: tuple[str, ...] = field(default=(), repr=False)

    def __repr__(self) -> str:
        return (
            f"Markers(real_names={len(self.real_names)}, real_name_words={len(self.real_name_words)}, "
            f"personal_emails={len(self.personal_emails)}, paths={len(self.paths)})"
        )

    @classmethod
    def empty(cls) -> Markers:
        return cls()

    def name_hit(self, text: str) -> bool:
        """A real name after :func:`normalize_name`, whole, bounded by non-alphanumerics.

        Only the full name matches by default: single words of a name
        (particles like "de", or names that are also common words) would
        flag ordinary prose. Single words match only when the operator lists
        them on purpose in ``real_name_words`` (four letters or more), and
        then as whole words only.
        """
        norm = normalize_name(text)
        for raw in self.real_names:
            name = normalize_name(raw)
            if name and re.search(rf"(?<![^\W_]){re.escape(name)}(?![^\W_])", norm):
                return True
        if self.real_name_words:
            words = name_words(text)
            return any(normalize_name(w) in words for w in self.real_name_words)
        return False

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


def _words(table: dict[str, object]) -> tuple[str, ...]:
    """``real_name_words``: single words, each at least four letters or digits once normalized."""
    words = _strings(table, "real_name_words")
    if not all(_WORD_RE.fullmatch(normalize_name(w)) for w in words):
        raise GuardError(
            f"{MARKERS_FILE}: [private].real_name_words entries must be single words of "
            f"{MIN_WORD_LETTERS}+ letters"
        )
    return words


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
        real_name_words=_words(private),
        personal_emails=_strings(private, "personal_emails"),
        paths=_strings(private, "paths"),
    )
