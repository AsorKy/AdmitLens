"""Fail-closed wrapper for the pinned gitleaks binary.

* Binary: ``$GUARDS_GITLEAKS`` when set (``make`` passes ``$(GITLEAKS)``; a
  missing file there is rc 2, there is no fallback), else
  ``<repo>/.tools/bin/gitleaks``, else ``gitleaks`` on PATH. Its sha256 must
  equal the extracted-binary pin for the current platform in the versioned
  ``tools/guards/bin/gitleaks.sha256`` (the same file pins the release
  tarballs); otherwise, or on an unsupported platform, rc 2.
* Engine: gitleaks never runs in ``git`` mode. That mode reads ``git log -p``,
  which ``.gitattributes`` (``-diff``, ``binary``, textconv), ``color.*`` and
  any ``git -c`` override can blind (measured in the W9 review). Instead the
  guards enumerate the blobs themselves and scan them with ``gitleaks dir``
  (:meth:`Gitleaks.blobs`), and messages with ``gitleaks stdin``.
* Config (fail closed): read from the index (staged, tree and message scans) or
  from a commit (range and history scans) into a ``mkstemp`` file, never from
  the working tree, so a weakened config in the working tree cannot lower
  detection. A config missing from the index or from that commit is rc 2. The
  single exception is bootstrap: a repository with no commits yet may use the
  working-tree ``.gitleaks.toml`` for index-side scans. When the candidate
  config differs from the base one, every scan runs once per config and the
  findings are united (:func:`configs`).
* Output: ``--redact`` and a JSON report in a 0600 temp file, deleted in
  ``finally``. Only ``RuleID``, ``File`` and ``StartLine`` are read. gitleaks'
  own stdout/stderr are captured and never echoed.
* No suppressions: ``--ignore-gitleaks-allow`` always, ``-i`` points at an empty
  temp dir, and a ``.gitleaksignore`` anywhere is a hard error (rc 2).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import shutil
import unicodedata
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from guards import gitutil
from guards.findings import Finding, GuardError

PINNED_VERSION = "8.30.1"
TIMEOUT = 300
CONFIG_NAME = ".gitleaks.toml"
BINARY_ENV = "GUARDS_GITLEAKS"
VERSION_ERROR = f"gitleaks {PINNED_VERSION} is required (run make setup)"


def find_binary(repo: Path) -> str:
    override = os.environ.get(BINARY_ENV, "")
    if override:
        p = Path(override)
        p = p if p.is_absolute() else repo / p
        if not (p.is_file() and os.access(p, os.X_OK)):
            raise GuardError("gitleaks not found at the configured path (run make setup)")
        return str(p)
    local = repo / ".tools" / "bin" / "gitleaks"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    found = shutil.which("gitleaks")
    if not found:
        raise GuardError("gitleaks not found (run make setup)")
    return found


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- pins

PIN_FILE = Path(__file__).resolve().parents[1] / "bin" / "gitleaks.sha256"
PLATFORMS = {("Darwin", "arm64"): "darwin_arm64", ("Linux", "x86_64"): "linux_x64"}


def platform_key() -> str:
    """``darwin_arm64`` or ``linux_x64``; any other platform is rc 2."""
    key = PLATFORMS.get((platform.system(), platform.machine()))
    if key is None:
        raise GuardError("unsupported platform for the pinned gitleaks")
    return key


def binary_pin_name(plat: str) -> str:
    """Name of the extracted-binary line in the pin file (the binary's path inside its tarball)."""
    return f"gitleaks_{PINNED_VERSION}_{plat}/gitleaks"


def load_pins(pin_file: Path = PIN_FILE) -> dict[str, str]:
    """``name -> sha256`` from the versioned pin file (``sha256sum`` format, ``#`` comments)."""
    pins: dict[str, str] = {}
    try:
        lines = pin_file.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError):
        raise GuardError("the gitleaks pin file cannot be read") from None
    for line in lines:
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2 or len(parts[0]) != 64 or parts[1] in pins:
            raise GuardError("the gitleaks pin file is malformed")
        pins[parts[1]] = parts[0].lower()
    return pins


def binary_pin(plat: str | None = None, pin_file: Path = PIN_FILE) -> str:
    """The pinned sha256 of the extracted binary for ``plat`` (default: this platform)."""
    pin = load_pins(pin_file).get(binary_pin_name(plat or platform_key()))
    if pin is None:
        raise GuardError("no pinned gitleaks binary checksum for this platform")
    return pin


def binary_ok(binary: str | Path, plat: str | None = None) -> bool:
    """The binary's sha256 equals the versioned pin for ``plat`` (default: this platform)."""
    try:
        expected = binary_pin(plat)
        actual = file_sha256(Path(binary))
    except (GuardError, OSError):
        return False
    return hmac.compare_digest(expected, actual)


def check_version(binary: str) -> None:
    platform_key()  # an unsupported platform is its own rc 2, not "run make setup"
    if not binary_ok(binary):
        raise GuardError(VERSION_ERROR)


def config_bytes(repo: Path, commit: str | None = None) -> bytes:
    """The committed config, failing closed.

    ``commit`` given (range and history scans): ``<commit>:.gitleaks.toml`` or
    rc 2. ``commit`` absent (staged, tree, message): the index copy; with no
    commits at all, the working-tree file (bootstrap); otherwise rc 2.
    """
    if commit is not None:
        data = gitutil.blob(f"{commit}:{CONFIG_NAME}", repo)
        if data is None:
            raise GuardError(f"{CONFIG_NAME} is missing from the scanned commit: refusing to scan without it")
        return data
    data = gitutil.blob(f":{CONFIG_NAME}", repo)
    if data is not None:
        return data
    if not gitutil.has_commits(repo):
        p = repo / CONFIG_NAME
        if p.is_file() and not p.is_symlink():
            try:
                return p.read_bytes()
            except OSError:
                raise GuardError(f"cannot read {CONFIG_NAME}") from None
    raise GuardError(f"{CONFIG_NAME} is not in the index (git add it): refusing to scan without it")


def configs(candidate: bytes, base: bytes | None) -> list[bytes]:
    """Configs to scan with: the candidate, plus the base one when it differs.

    A weakened candidate config can then never hide what the base config finds.
    ``base`` is None at bootstrap (no base commit, or no config there).
    """
    return [candidate] if base is None or base == candidate else [base, candidate]


def base_config(repo: Path, base: str | None) -> bytes | None:
    """``<base>:.gitleaks.toml`` or None (no base commit, or no config there yet)."""
    return None if base is None else gitutil.blob(f"{base}:{CONFIG_NAME}", repo)


@contextmanager
def _private_file(data: bytes | None, suffix: str) -> Iterator[str]:
    fd, path = tempfile.mkstemp(prefix="guards-", suffix=suffix)
    try:
        os.fchmod(fd, 0o600)
        if data:
            os.write(fd, data)
        os.close(fd)
        fd = -1
        yield path
    finally:
        if fd >= 0:
            os.close(fd)
        with suppress(FileNotFoundError):
            os.unlink(path)


@dataclass(frozen=True)
class BlobItem:
    """One file version to scan: repository path, commit (None = index/tree) and bytes."""

    path: str
    commit: str | None
    data: bytes


def _safe_rel(rel: str) -> PurePosixPath:
    p = PurePosixPath(rel)
    if p.is_absolute() or not p.parts or any(part in ("", ".", "..") for part in p.parts):
        raise GuardError("refusing to scan an unsafe path")
    return p


class PathCollision(Exception):  # noqa: N818 - internal control flow, never reaches the CLI
    """``rel`` cannot coexist with an already written path (case or Unicode-normalisation twin)."""


def disk_name(rel: str) -> str:
    """``rel`` as a valid UTF-8 file name: undecodable bytes become ``\\xNN`` text."""
    raw = rel.encode("utf-8", "surrogateescape")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", "backslashreplace")
    return rel


def write_tree(root: Path, rel: str, data: bytes) -> None:
    """Write ``data`` at ``root/rel``, never overwriting or following links.

    :class:`PathCollision` when the file system already holds a twin of ``rel``
    (case-insensitive or normalisation-insensitive volumes); any other
    ``OSError`` is rc 2.
    """
    dest = root / _safe_rel(rel)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except (FileExistsError, NotADirectoryError, IsADirectoryError):
        raise PathCollision(rel) from None
    except OSError:
        raise GuardError(f"cannot write a scan copy of {disk_name(rel)}") from None


def _fold(rel: str) -> str:
    return unicodedata.normalize("NFD", rel).casefold()


class Gitleaks:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        ensure_no_gitleaksignore(repo)
        self.binary = find_binary(repo)
        check_version(self.binary)

    def _run(self, args: list[str], *, cwd: Path, config: bytes, stdin: bytes | None = None) -> list[dict[str, object]]:
        # No GITLEAKS_* overrides; git config overrides dropped and colour off in case
        # any gitleaks code path shells out to git.
        env = gitutil.git_env({k: v for k, v in os.environ.items() if not k.startswith("GITLEAKS_")})
        with (
            _private_file(config, ".toml") as cfg,
            _private_file(None, ".json") as report,
            tempfile.TemporaryDirectory(prefix="guards-noignore-") as noignore,
        ):
            argv = [
                self.binary, *args,
                "--config", cfg,
                "--gitleaks-ignore-path", noignore,
                "--ignore-gitleaks-allow",
                "--redact",
                "--no-banner",
                "--no-color",
                "--log-level", "error",
                "--exit-code", "1",
                "--report-format", "json",
                "--report-path", report,
            ]  # fmt: skip
            try:
                proc = subprocess.run(  # noqa: S603
                    argv,
                    cwd=cwd,
                    env=env,
                    input=stdin,
                    stdin=None if stdin is not None else subprocess.DEVNULL,
                    capture_output=True,
                    timeout=TIMEOUT,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise GuardError("gitleaks timed out") from None
            if proc.returncode not in (0, 1):
                raise GuardError(f"gitleaks failed (rc {proc.returncode})")
            try:
                with open(report, "rb") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                raise GuardError("gitleaks report could not be read") from None
            if not isinstance(data, list) or (proc.returncode == 1 and not data):
                raise GuardError("gitleaks report is inconsistent")
            return data

    @staticmethod
    def _items(items: list[dict[str, object]]) -> list[tuple[str, str, int]]:
        out = []
        for item in items:
            rule = str(item.get("RuleID", "gitleaks"))
            path = str(item.get("File", "") or "").removeprefix("./")
            line = item.get("StartLine", 0)
            out.append((rule, path, int(line) if isinstance(line, int) else 0))
        return out

    def _dir(self, root: Path, cfgs: list[bytes]) -> set[tuple[str, str, int]]:
        found: set[tuple[str, str, int]] = set()
        for cfg in cfgs:
            found.update(self._items(self._run(["dir", "."], cwd=root, config=cfg)))
        return found

    def blobs(self, items: list[BlobItem], cfgs: list[bytes]) -> list[Finding]:
        """Scan file versions with ``gitleaks dir``, in bucket directories per commit.

        A commit's files share a bucket unless two paths collide on this file
        system (case or Unicode-normalisation twins, a file where another needs
        a directory): the later one opens a new bucket of the same commit.
        Paths are reported back as repository paths with their commit. A hit
        whose path cannot be mapped back is reported as ``:unmapped``.
        """
        if not items:
            return []
        # Materialises every blob on disk at once: O(scanned bytes) of temp space.
        buckets: list[tuple[str | None, dict[str, str], set[str]]] = []  # (commit, disk -> repo path, folded)
        open_bucket: dict[str | None, list[int]] = {}
        with tempfile.TemporaryDirectory(prefix="guards-blobs-") as tmp:
            root = Path(tmp)
            for item in items:
                name = disk_name(item.path)
                for idx in [*open_bucket.get(item.commit, []), None]:
                    if idx is None:
                        buckets.append((item.commit, {}, set()))
                        idx = len(buckets) - 1
                        open_bucket.setdefault(item.commit, []).append(idx)
                    _commit, paths, folded = buckets[idx]
                    if _fold(name) in folded:
                        continue
                    try:
                        write_tree(root, f"b{idx:06d}/{name}", item.data)
                    except PathCollision:
                        if not paths:
                            raise GuardError(f"cannot write a scan copy of {name}") from None
                        continue
                    paths[name] = item.path
                    folded.add(_fold(name))
                    break
            hits = self._dir(root, cfgs)
        out = []
        for rule, path, line in hits:
            bucket, _, rel = path.partition("/")
            idx = int(bucket[1:]) if len(bucket) == 7 and bucket[0] == "b" and bucket[1:].isdigit() else -1
            if 0 <= idx < len(buckets) and rel in buckets[idx][1]:
                out.append(Finding(rule, buckets[idx][1][rel], line, buckets[idx][0]))
            else:
                out.append(Finding(rule, ":unmapped", line))
        return out

    def directory(self, root: Path, cfgs: list[bytes]) -> list[Finding]:
        return [Finding(rule, path, line) for rule, path, line in self._dir(root, cfgs)]

    def text(self, data: bytes, cfgs: list[bytes]) -> list[tuple[str, int]]:
        """(rule, line) for each hit in ``data`` scanned through stdin, united over ``cfgs``."""
        found: set[tuple[str, int]] = set()
        for cfg in cfgs:
            found.update((rule, line) for rule, _, line in self._items(self._run(["stdin"], cwd=self.repo, config=cfg, stdin=data)))
        return sorted(found)


def ensure_no_gitleaksignore(repo: Path) -> None:
    """A ``.gitleaksignore`` anywhere (tracked or not ignored) is rc 2."""
    listed = gitutil.split_z(gitutil.git(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], repo))
    for rel in sorted(set(listed)):
        if Path(rel).name.lower() == ".gitleaksignore":
            raise GuardError(f".gitleaksignore present at {rel}: suppression files are not allowed")
    if (repo / ".gitleaksignore").exists():
        raise GuardError(".gitleaksignore present at the repo root: suppression files are not allowed")
