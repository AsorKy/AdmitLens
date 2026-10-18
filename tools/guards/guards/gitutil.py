"""Thin, fail-closed wrappers around ``git``.

Every call uses an explicit argv list, ``stdin=DEVNULL`` unless input is fed,
and a timeout. On failure a :class:`GuardError` names the git subcommand only;
git's own stderr is not echoed (it could quote file content or a remote URL).

The inherited environment is kept on purpose (inside hooks git sets
``GIT_DIR``/``GIT_INDEX_FILE`` and the guards must see the same index), with
these exceptions (see :func:`git_env`), so a caller cannot reshape the guards'
own git output:

* command-line config overrides (``git -c …`` reaches a hook as
  ``GIT_CONFIG_PARAMETERS``/``GIT_CONFIG_COUNT``) are dropped and replaced by a
  fixed set;
* replace objects and grafts are disabled: ``git replace`` or a graft file
  would make every local layer read a different history from the one a push
  transmits;
* pathspec-magic variables (``GIT_LITERAL_PATHSPECS`` and friends) are dropped,
  so path arguments mean exactly what the guards wrote;
* ``GIT_TERMINAL_PROMPT=0``: a network call (``ls-remote``) never prompts.

Every git process runs in its own session (process group). On a timeout or an
interrupt the whole group is killed, so a transport child (``ssh``, a remote
helper) spawned by ``ls-remote`` is never left running.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from guards.findings import GuardError

TIMEOUT = 30
BATCH_TIMEOUT = 300
ZERO_SHA_RE = re.compile(r"^0{40}$|^0{64}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
# A remote *name* (never a URL): starts with an alphanumeric, may contain "/"
# (``team/fork``), never "..", "//", whitespace, ":" or a trailing "/".
REMOTE_NAME_RE = re.compile(r"^(?!.*\.\.)(?!.*//)[A-Za-z0-9][A-Za-z0-9._/-]*(?<!/)$")

GITLINK_MODE = "160000"
SYMLINK_MODE = "120000"

# Config forced on every git call the guards make (GIT_CONFIG_COUNT form).
FORCED_CONFIG = (
    ("color.ui", "never"),
    ("color.diff", "never"),
    ("log.showSignature", "false"),
    ("core.attributesFile", os.devnull),
)


def _is_config_override(key: str) -> bool:
    return key in ("GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT") or key.startswith(
        ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
    )


# Inherited variables that change how git parses pathspecs.
PATHSPEC_VARS = frozenset({
    "GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS",
})
# Never created by anyone: /dev/null is an empty graft file (no grafts) and cannot be written.
NO_GRAFT_FILE = os.devnull


def git_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """``base`` (default: ``os.environ``) without config overrides or pathspec magic, plus forced settings."""
    env = {
        k: v
        for k, v in (os.environ if base is None else base).items()
        if not _is_config_override(k) and k not in PATHSPEC_VARS
    }
    env["GIT_CONFIG_COUNT"] = str(len(FORCED_CONFIG))
    for i, (key, value) in enumerate(FORCED_CONFIG):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    env["GIT_ATTR_NOSYSTEM"] = "1"
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_GRAFT_FILE"] = NO_GRAFT_FILE
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _subcommand(args: list[str]) -> str:
    """The git subcommand in ``args`` (skipping ``-c key=value`` pairs), for error messages."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] == "-c" else 1
    return args[i] if i < len(args) else "command"


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """Kill ``proc``'s whole process group (its own session) and reap it."""
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with suppress(subprocess.TimeoutExpired, OSError, ValueError):
        proc.communicate(timeout=5)


def _run(args: list[str], cwd: Path, input: bytes | None, timeout: int) -> subprocess.CompletedProcess[bytes]:  # noqa: A002
    try:
        proc = subprocess.Popen(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=cwd,
            env=git_env(),
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError:
        raise GuardError("git is not installed") from None
    try:
        out, err = proc.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        raise GuardError(f"git {_subcommand(args)} timed out after {timeout} s") from None
    except BaseException:
        _kill_group(proc)
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def git(args: list[str], cwd: Path, *, input: bytes | None = None, check: bool = True) -> bytes:  # noqa: A002
    proc = _run(args, cwd, input, TIMEOUT)
    if check and proc.returncode != 0:
        raise GuardError(f"git {args[0]} failed (rc {proc.returncode})")
    return proc.stdout


def git_ok(args: list[str], cwd: Path) -> bool:
    try:
        return _run(args, cwd, None, TIMEOUT).returncode == 0
    except GuardError:
        return False


def toplevel(cwd: Path) -> Path:
    try:
        out = git(["rev-parse", "--show-toplevel"], cwd)
    except GuardError:
        raise GuardError("not inside a git work tree") from None
    return Path(out.decode().strip())


def git_path(name: str, cwd: Path) -> Path:
    """``git rev-parse --git-path``: honours worktrees and ``GIT_DIR``."""
    out = git(["rev-parse", "--git-path", name], cwd).decode("utf-8", "surrogateescape").strip()
    p = Path(out)
    return p if p.is_absolute() else cwd / p


def split_z(data: bytes) -> list[str]:
    return [p.decode("utf-8", "surrogateescape") for p in data.split(b"\0") if p]


def safe_arg(value: str, what: str) -> str:
    """Refuse values that git would parse as options."""
    if not value or value.startswith("-") or "\n" in value or "\0" in value:
        raise GuardError(f"invalid {what}")
    return value


def resolve_commit(rev: str, cwd: Path) -> str:
    safe_arg(rev, "revision")
    try:
        out = git(["rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}"], cwd)
    except GuardError:
        raise GuardError("unknown revision (run git fetch first?)") from None
    return out.decode().strip()


def resolve_ref(ref: str, cwd: Path) -> str | None:
    """Commit a full ref name points at, or None when the ref does not exist (one git call)."""
    proc = _run(["rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}"], cwd, None, TIMEOUT)
    sha = proc.stdout.decode("ascii", "replace").strip()
    return sha if proc.returncode == 0 and SHA_RE.match(sha) else None


def has_object(sha: str, cwd: Path) -> bool:
    return git_ok(["cat-file", "-e", f"{sha}^{{commit}}"], cwd)


def has_commits(cwd: Path) -> bool:
    """True when any ref points at a commit (an unborn repo has none)."""
    return bool(git(["rev-list", "-n", "1", "--all"], cwd).strip())


def rev_list(args: list[str], cwd: Path) -> list[str]:
    """Commits of a rev-list spec; an unknown ref is rc 2 (git exits non-zero)."""
    out = git(["rev-list", *args], cwd)
    return [line for line in out.decode().splitlines() if line]


def blob(spec: str, cwd: Path) -> bytes | None:
    """Content of ``<rev>:<path>`` or ``:<path>``; None if it is missing or not a blob.

    One ``cat-file --batch`` call: the header names the type and size.
    """
    if "\n" in spec or "\0" in spec:
        raise GuardError("invalid object name")
    proc = _run(["cat-file", "--batch"], cwd, spec.encode("utf-8", "surrogateescape") + b"\n", TIMEOUT)
    if proc.returncode != 0:
        raise GuardError(f"git cat-file failed (rc {proc.returncode})")
    data = proc.stdout
    nl = data.find(b"\n")
    header = data[:nl].decode("ascii", "replace").split() if nl >= 0 else []
    if len(header) != 3 or not SHA_RE.match(header[0]):
        return None  # "<spec> missing" (or ambiguous): no such object
    if header[1] != "blob":
        return None
    size = int(header[2])
    body = data[nl + 1 : nl + 1 + size]
    if len(body) != size:
        raise GuardError("unexpected git cat-file output")
    return body


@dataclass(frozen=True)
class Entry:
    """One added or modified path from a ``--raw -z`` diff."""

    path: str
    mode: str
    sha: str

    @property
    def gitlink(self) -> bool:
        return self.mode == GITLINK_MODE


def parse_raw_z(data: bytes) -> list[Entry]:
    """Entries of ``--raw -z --no-renames --no-abbrev`` output (deletions excluded by the caller)."""
    fields = data.split(b"\0")
    out: list[Entry] = []
    i = 0
    while i < len(fields):
        head = fields[i]
        if not head:
            i += 1
            continue
        if not head.startswith(b":") or i + 1 >= len(fields):
            raise GuardError("unexpected git diff output")
        parts = head[1:].decode("ascii", "replace").split()
        if len(parts) != 5 or not SHA_RE.match(parts[3]):
            raise GuardError("unexpected git diff output")
        path = fields[i + 1].decode("utf-8", "surrogateescape")
        if not parts[4].startswith("D"):
            out.append(Entry(path, parts[1], parts[3]))
        i += 2
    return out


def read_blobs(shas: list[str], cwd: Path) -> dict[str, bytes]:
    """Every blob in ``shas`` through one ``cat-file --batch``; anything missing is rc 2."""
    unique = list(dict.fromkeys(shas))
    if not unique:
        return {}
    proc = _run(["cat-file", "--batch"], cwd, "".join(s + "\n" for s in unique).encode(), BATCH_TIMEOUT)
    if proc.returncode != 0:
        raise GuardError(f"git cat-file failed (rc {proc.returncode})")
    out: dict[str, bytes] = {}
    data = proc.stdout
    pos = 0
    for sha in unique:
        nl = data.find(b"\n", pos)
        if nl < 0:
            raise GuardError("unexpected git cat-file output")
        header = data[pos:nl].decode("ascii", "replace").split()
        if len(header) != 3 or header[0] != sha or header[1] != "blob":
            raise GuardError("an expected blob is missing from the repository")
        size = int(header[2])
        start = nl + 1
        out[sha] = data[start : start + size]
        pos = start + size + 1
    return out


@dataclass(frozen=True)
class CommitInfo:
    """Identity and message of one commit, as ``git log`` formats them (re-encoded to UTF-8)."""

    sha: str
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str
    message: bytes  # ``%B`` plus the newline ``git log -1 --format=%B`` ends with


COMMIT_FIELDS = 6


def commit_infos(shas: list[str], cwd: Path) -> dict[str, CommitInfo]:
    """``CommitInfo`` for every commit in ``shas`` through one ``git log --no-walk --stdin -z``."""
    unique = list(dict.fromkeys(shas))
    if not unique:
        return {}
    for sha in unique:
        if not SHA_RE.match(sha):
            raise GuardError("invalid commit id")
    args = ["log", "--no-walk=unsorted", "--stdin", "-z", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B"]
    proc = _run(args, cwd, "".join(s + "\n" for s in unique).encode(), BATCH_TIMEOUT)
    if proc.returncode != 0:
        raise GuardError(f"git log failed (rc {proc.returncode})")
    # -z terminates every record with NUL (in place of the newline -1 --format=%B ends with).
    fields = proc.stdout.split(b"\0")
    if len(fields) != COMMIT_FIELDS * len(unique) + 1 or fields[-1] != b"":
        raise GuardError("unexpected git log output")
    out: dict[str, CommitInfo] = {}
    for i, sha in enumerate(unique):
        rec = fields[i * COMMIT_FIELDS : (i + 1) * COMMIT_FIELDS]
        if rec[0].decode("ascii", "replace") != sha:
            raise GuardError("unexpected git log output")
        an, ae, cn, ce = (f.decode("utf-8", "replace") for f in rec[1:5])
        out[sha] = CommitInfo(sha, an, ae, cn, ce, rec[5] + b"\n")
    return out


def changed_entries(shas: list[str], cwd: Path) -> dict[str, list[Entry]]:
    """Added or modified entries of each commit against each parent (merges via ``-m``).

    One ``git diff-tree --stdin`` call for every commit. A commit with no
    change has no output and maps to ``[]``. In ``-z`` output a token that
    starts with ``:`` is an entry (its path is the next token); any other
    token where an entry may start is the next commit's id.
    """
    unique = list(dict.fromkeys(shas))
    out: dict[str, list[Entry]] = {sha: [] for sha in unique}
    if not unique:
        return out
    for sha in unique:
        if not SHA_RE.match(sha):
            raise GuardError("invalid commit id")
    args = ["diff-tree", "--stdin", "-m", "-r", "--root", "--raw", "-z", "--no-abbrev",
            "--no-renames", "--no-ext-diff", "--diff-filter=d"]  # fmt: skip
    proc = _run(args, cwd, "".join(s + "\n" for s in unique).encode(), BATCH_TIMEOUT)
    if proc.returncode != 0:
        raise GuardError(f"git diff-tree failed (rc {proc.returncode})")
    fields = proc.stdout.split(b"\0")
    current: str | None = None
    i = 0
    while i < len(fields):
        tok = fields[i]
        if not tok:
            i += 1
            continue
        if tok.startswith(b":"):
            if current is None or i + 1 >= len(fields):
                raise GuardError("unexpected git diff-tree output")
            out[current].extend(parse_raw_z(tok + b"\0" + fields[i + 1] + b"\0"))
            i += 2
            continue
        sha = tok.decode("ascii", "replace")
        if sha not in out:
            raise GuardError("unexpected git diff-tree output")
        current = sha
        i += 1
    return out


def _check_ignore_arg(path: str) -> str:
    """``check-ignore`` parses its input as pathspecs: a leading ``:`` would be magic."""
    return "./" + path if path.startswith(":") else path


def ignored_paths(paths: list[str], cwd: Path, dirs: frozenset[str] = frozenset()) -> set[str]:
    """Paths the work tree's ignore rules match, tracked or not.

    ``check-ignore --no-index`` also reports tracked (``git add -f``) paths.
    Matching is case-insensitive everywhere (so CI on Linux agrees with macOS)
    and the user-global ``core.excludesFile`` is ignored: only ``.gitignore``
    files and ``.git/info/exclude`` count. Paths in ``dirs`` (gitlinks) are
    also queried with a trailing ``/`` so directory-only patterns apply.
    """
    unique = sorted(set(paths))
    if not unique:
        return set()
    queries: dict[str, str] = {}  # what check-ignore echoes back -> repository path
    for p in unique:
        queries[_check_ignore_arg(p)] = p
        if p in dirs:
            queries[_check_ignore_arg(p) + "/"] = p
    data = b"".join(q.encode("utf-8", "surrogateescape") + b"\0" for q in queries)
    args = [
        "-c", "core.ignoreCase=true", "-c", f"core.excludesFile={os.devnull}",
        "check-ignore", "--no-index", "-z", "--stdin",
    ]  # fmt: skip
    proc = _run(args, cwd, data, BATCH_TIMEOUT)
    if proc.returncode not in (0, 1):
        raise GuardError(f"git check-ignore failed (rc {proc.returncode})")
    out = set()
    for echoed in split_z(proc.stdout):
        if echoed not in queries:
            raise GuardError("unexpected git check-ignore output")
        out.add(queries[echoed])
    return out


def tree_entries(commit: str, cwd: Path) -> list[Entry]:
    """Every blob, symlink and gitlink in ``commit``'s tree (``ls-tree -r``)."""
    out: list[Entry] = []
    for record in _split_records(git(["ls-tree", "-r", "-z", "--full-tree", "--end-of-options", commit], cwd)):
        head, sep, path = record.partition(b"\t")
        parts = head.decode("ascii", "replace").split()
        if not sep or len(parts) != 3 or not SHA_RE.match(parts[2]):
            raise GuardError("unexpected git ls-tree output")
        out.append(Entry(path.decode("utf-8", "surrogateescape"), parts[0], parts[2]))
    return out


def _split_records(data: bytes) -> list[bytes]:
    return [p for p in data.split(b"\0") if p]


def ref_tips(cwd: Path) -> list[str]:
    """Commits that refs (and ``HEAD``) point at, annotated tags peeled; what ``--all`` starts from."""
    out = git(["for-each-ref", "--format=%(objecttype) %(objectname) %(*objecttype) %(*objectname)"], cwd)
    tips: list[str] = []
    for line in out.decode("ascii", "replace").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "commit":
            tips.append(parts[1])
        elif len(parts) == 4 and parts[2] == "commit":
            tips.append(parts[3])
    head = resolve_ref("HEAD", cwd)
    if head is not None:
        tips.append(head)
    return list(dict.fromkeys(tips))


def remote_urls(remote: str, cwd: Path, *, push: bool) -> list[str]:
    """``git remote get-url --all [--push] <remote>``: rewrites applied, never printed."""
    safe_arg(remote, "remote")
    args = ["remote", "get-url", "--all", *(["--push"] if push else []), remote]
    try:
        out = git(args, cwd)
    except GuardError:
        raise GuardError("git remote get-url failed: unknown remote?") from None
    return [line for line in out.decode("utf-8", "surrogateescape").splitlines() if line]


def read_url(location: str, cwd: Path) -> str:
    """Where ``ls-remote <location>`` would connect (``ls-remote --get-url``), without connecting.

    It resolves a remote name to its URL and applies ``url.*.insteadOf``.
    """
    safe_arg(location, "remote")
    return git(["ls-remote", "--get-url", location], cwd).decode("utf-8", "surrogateescape").rstrip("\n")


def remote_branch(remote: str, ref: str, cwd: Path) -> str | None:
    """The sha ``ref`` has on ``remote`` (a name or a URL) right now, None when it does not exist.

    Any failure (network, timeout, unknown remote) is rc 2: the caller cannot
    tell what the remote already has. The message keeps the cause (failed with
    an rc, or timed out) but never the remote's URL.
    """
    safe_arg(remote, "remote")
    try:
        out = git(["ls-remote", "--refs", remote, ref], cwd)
    except GuardError as exc:
        raise GuardError(f"{exc}: cannot tell what the remote already has") from None
    for line in out.decode("ascii", "replace").splitlines():
        sha, _, name = line.partition("\t")
        if name == ref:
            if not SHA_RE.match(sha):
                raise GuardError("unexpected git ls-remote output")
            return sha
    return None
