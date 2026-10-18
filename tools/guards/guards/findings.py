"""The finding record, its TSV form, exit codes and the value-free error type."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


@dataclass(frozen=True, order=True)
class Finding:
    """One detection. There is deliberately no field for the matched value."""

    rule: str
    path: str
    line: int
    commit: str | None = None

    def with_commit(self, commit: str | None) -> Finding:
        return Finding(self.rule, self.path, self.line, commit)

    def to_tsv(self) -> str:
        fields = [self.rule, _clean(self.path), str(self.line)]
        if self.commit:
            fields.append(self.commit)
        return "\t".join(fields)


def _clean(text: str) -> str:
    # Paths with tabs or newlines would break the one-finding-per-line format, and
    # undecodable bytes (surrogate escapes from a non-UTF-8 path) are shown as \xNN.
    text = text.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")
    return text.replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def format_tsv(findings: Iterable[Finding]) -> str:
    unique = sorted(set(findings), key=lambda f: (f.commit or "", f.path, f.line, f.rule))
    return "".join(f.to_tsv() + "\n" for f in unique)


class GuardError(Exception):
    """A failure that must block (rc 2).

    The message is written by the guards themselves and names paths or tools
    only; it never embeds file content, exception text from parsers, or values.
    """


Disabled = frozenset[str]
NONE_DISABLED: Disabled = frozenset()
