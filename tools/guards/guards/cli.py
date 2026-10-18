"""Command line, contract KG (PLAN-CA-0005 §6.3).

    python -m guards staged
    python -m guards range <remote-name> <from> <to>
    python -m guards tree
    python -m guards message <file>
    python -m guards identity <range>
    python -m guards lint
    python -m guards prepush <remote-name>      # git pre-push stdin
    python -m guards history                    # every commit on every ref
    python -m guards doctor                     # local setup health (ok/warn/FAIL)

stdout: one ``rule<TAB>path<TAB>line[<TAB>commit]`` line per finding, no value
(``doctor`` prints one ``ok``/``warn``/``FAIL`` line per check instead).
Message findings use the pseudo-paths ``:message`` / ``:tag-message`` (never
the message file's real path); a gitleaks hit that cannot be mapped back to a
file or message is reported as ``:unmapped`` rather than dropped.
stderr: a short human summary, without values.
Exit: 0 clean, 1 findings, 2 error or missing tool (fail closed).

Arguments are never echoed back: a usage error only names the mode.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from guards import doctor, gitutil
from guards.findings import (
    EXIT_CLEAN,
    EXIT_ERROR,
    EXIT_FINDINGS,
    Finding,
    GuardError,
    format_tsv,
)
from guards import gitleaks as gl_mod
from guards.gitleaks import BlobItem, Gitleaks
from guards.markers import Markers, load_markers
from guards.rules import content, identity, lint, messages, paths

USAGE = """usage: python -m guards MODE [ARGS]
modes:
  staged                         index: paths, path-ignored, content, gitleaks on the staged blobs
  range <remote> <from> <to>     every commit in from..to (merges included)
  tree                           files that are or could be versioned
  message <file>                 commit or tag message
  identity <range>               author/committer/tagger of a rev-list range
  lint                           Makefiles, .gitignore files, workflows
  prepush <remote>               git pre-push hook (reads refs on stdin)
  history                        all commits on all refs (merges via -m),
                                 commit and annotated-tag messages, identities;
                                 the commit-message format is not enforced
  doctor                         local setup health: hooks, git config, gitleaks
"""

MAIN_REF = "refs/heads/main"
HISTORY_REMOTE = "origin"


@dataclass
class Context:
    repo: Path
    markers: Markers


def _decode(data: bytes) -> str:
    return data.decode("utf-8", "replace")


# --------------------------------------------------------------------------- file sets


def candidate_files(repo: Path) -> list[str]:
    """Tracked files plus untracked files that are not ignored."""
    out = gitutil.git(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], repo)
    return sorted(set(gitutil.split_z(out)))


def _read_worktree(repo: Path, rel: str) -> bytes | None:
    """Bytes of a work-tree file; a symlink yields its target string (what git stores)."""
    p = repo / rel
    try:
        if p.is_symlink():
            return os.readlink(p).encode("utf-8", "surrogateescape")
        if not p.is_file():
            return None  # deleted in the work tree or not a regular file
        return p.read_bytes()
    except OSError:
        raise GuardError(f"cannot read {rel}") from None


def _scan_lint(rel: str, data: bytes, commit: str | None) -> list[Finding]:
    """Lint of the files that steer the scanners (``.gitattributes``, ``.gitleaks.toml``)."""
    if not lint.is_scan_lint_target(rel):
        return []
    return [f.with_commit(commit) for f in lint.lint_file(rel, _decode(data))]


def _file_findings(
    ctx: Context, rel: str, data: bytes, commit: str | None = None, *, policy: bool = True
) -> list[Finding]:
    """Path and content rules on one file version; ``policy`` adds the scan-lint."""
    found = [f.with_commit(commit) for f in paths.check_path(rel, size=len(data))]
    found += content.check_content(rel, _decode(data), ctx.markers, commit=commit)
    if policy:
        found += _scan_lint(rel, data, commit)
    return found


def _gitlink_findings(rel: str, commit: str | None) -> list[Finding]:
    """A gitlink (submodule entry) has no content here: path rules only, as a directory."""
    return [f.with_commit(commit) for f in paths.check_path(rel, is_dir=True)]


def _ignored(ctx: Context, first_commit: dict[str, str | None], dirs: frozenset[str] = frozenset()) -> list[Finding]:
    """``path-ignored``: ``.gitignore`` is the single source for what must never be versioned."""
    return paths.ignored_findings(gitutil.ignored_paths(list(first_commit), ctx.repo, dirs), first_commit)


def _scan_items(
    ctx: Context,
    gl: Gitleaks,
    items: list[BlobItem],
    cfgs: list[bytes],
    gitlinks: dict[str, str | None] | None = None,
    *,
    policy: bool = True,
) -> list[Finding]:
    """Python rules and gitleaks over file versions; ``policy`` adds scan-lint and ``path-ignored``.

    ``gitlinks`` (path -> first commit) get path rules and, with ``policy``, ``path-ignored``.
    """
    found: list[Finding] = []
    first: dict[str, str | None] = {}
    for item in items:
        found += _file_findings(ctx, item.path, item.data, item.commit, policy=policy)
        first.setdefault(item.path, item.commit)
    for rel, commit in (gitlinks or {}).items():
        found += _gitlink_findings(rel, commit)
        first.setdefault(rel, commit)
    if policy:
        found += _ignored(ctx, first, frozenset(gitlinks or ()))
    found += gl.blobs(items, cfgs)
    return found


# --------------------------------------------------------------------------- modes


def mode_staged(ctx: Context) -> list[Finding]:
    gl = Gitleaks(ctx.repo)
    candidate = gl_mod.config_bytes(ctx.repo)
    head = gitutil.resolve_ref("HEAD", ctx.repo)
    cfgs = gl_mod.configs(candidate, gl_mod.base_config(ctx.repo, head))
    raw = gitutil.git(
        ["diff", "--cached", "--raw", "-z", "--no-abbrev", "--no-renames", "--no-ext-diff", "--diff-filter=ACMRT"],
        ctx.repo,
    )
    all_entries = gitutil.parse_raw_z(raw)
    entries = [e for e in all_entries if not e.gitlink]
    gitlinks: dict[str, str | None] = {e.path: None for e in all_entries if e.gitlink}
    blobs = gitutil.read_blobs([e.sha for e in entries], ctx.repo)
    items = [BlobItem(e.path, None, blobs[e.sha]) for e in entries]
    return _scan_items(ctx, gl, items, cfgs, gitlinks)


def mode_tree(ctx: Context) -> list[Finding]:
    gl = Gitleaks(ctx.repo)
    cfgs = [gl_mod.config_bytes(ctx.repo)]
    found: list[Finding] = []
    files = candidate_files(ctx.repo)
    with tempfile.TemporaryDirectory(prefix="guards-tree-") as tmp:
        root = Path(tmp)
        for rel in files:
            data = _read_worktree(ctx.repo, rel)
            if data is None:
                continue
            found += _file_findings(ctx, rel, data)
            try:
                gl_mod.write_tree(root, rel, data)
            except gl_mod.PathCollision:
                raise GuardError(f"cannot write a scan copy of {gl_mod.disk_name(rel)}") from None
        found += gl.directory(root, cfgs)
    found += _ignored(ctx, dict.fromkeys(files))
    return found


def mode_lint(ctx: Context) -> list[Finding]:
    found: list[Finding] = []
    for rel in candidate_files(ctx.repo):
        if not lint.is_lint_target(rel):
            continue
        p = ctx.repo / rel
        if p.is_symlink():
            continue
        data = _read_worktree(ctx.repo, rel)
        if data is not None:
            found += lint.lint_file(rel, _decode(data))
    return found


def _map_text_hits(hits: list[tuple[str, int]], batch: list[tuple[int, int, str, str | None]], fallback: str | None) -> list[Finding]:
    """Map stdin hits back to (pseudo-path, commit); an unmappable hit is ``:unmapped``, never dropped."""
    out = []
    for rule, line in hits:
        for first, n, path, commit in batch:
            if first <= line < first + n:
                out.append(Finding(rule, path, line - first + 1, commit))
                break
        else:
            out.append(Finding(rule, ":unmapped", line, fallback))
    return out


def mode_message(ctx: Context, file: str) -> list[Finding]:
    """Findings use the pseudo-path ``:message``: the file's real path is never reported."""
    gl = Gitleaks(ctx.repo)
    try:
        data = Path(file).read_bytes()
    except OSError:
        raise GuardError("cannot read the message file") from None
    found = messages.check_message(_decode(data), ":message", ctx.markers)
    n = max(1, data.count(b"\n") + (0 if data.endswith(b"\n") else 1))
    found += _map_text_hits(gl.text(data, [gl_mod.config_bytes(ctx.repo)]), [(1, n, ":message", None)], None)
    return found


# --------------------------------------------------------------------------- history


def _commit_identity(ctx: Context, sha: str) -> list[Finding]:
    out = gitutil.git(["log", "-1", "--format=%an%x00%ae%x00%cn%x00%ce", sha, "--"], ctx.repo)
    an, ae, cn, ce = (_decode(out).rstrip("\n").split("\0") + ["", "", "", ""])[:4]
    return identity.check_identity("author", an, ae, ctx.markers, sha) + identity.check_identity(
        "committer", cn, ce, ctx.markers, sha
    )


@dataclass
class TagInfo:
    sha: str
    tagger_name: str
    tagger_email: str
    message: bytes


def _annotated_tags(ctx: Context, commits: set[str]) -> list[TagInfo]:
    out = gitutil.git(
        ["for-each-ref", "--format=%(objecttype) %(objectname) %(*objectname)", "refs/tags"], ctx.repo
    )
    tags = []
    for line in _decode(out).splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[0] != "tag" or parts[2] not in commits:
            continue
        raw = gitutil.git(["cat-file", "tag", parts[1]], ctx.repo)
        header, _, body = raw.partition(b"\n\n")
        name = email = ""
        for h in _decode(header).splitlines():
            if h.startswith("tagger "):
                who = h[len("tagger ") :]
                name, _, rest = who.partition(" <")
                email = rest.split(">", 1)[0]
        tags.append(TagInfo(parts[1], name, email, body))
    return tags


def _identity_checks(ctx: Context, commits: list[str]) -> list[Finding]:
    found: list[Finding] = []
    for sha in commits:
        found += _commit_identity(ctx, sha)
    for tag in _annotated_tags(ctx, set(commits)):
        found += identity.check_identity("tagger", tag.tagger_name, tag.tagger_email, ctx.markers, tag.sha)
    return found


def _changed_entries(ctx: Context, sha: str) -> list[gitutil.Entry]:
    """Added or modified entries of ``sha`` against each parent (merges via ``-m``); deletions excluded."""
    raw = gitutil.git(
        ["diff-tree", "-m", "-r", "--root", "--no-commit-id", "--raw", "-z", "--no-abbrev",
         "--no-renames", "--no-ext-diff", "--diff-filter=d", sha],
        ctx.repo,
    )  # fmt: skip
    return gitutil.parse_raw_z(raw)


def range_checks(
    ctx: Context,
    gl: Gitleaks,
    commits: list[str],
    cfgs: list[bytes],
    tip: str,
    *,
    check_format: bool,
    policy_tips: list[str] | None = None,
) -> list[Finding]:
    """Identities, messages (format only when ``check_format``) and every file version in ``commits``.

    Each (path, blob) pair is scanned once, attributed to the oldest commit in
    the range that introduced it (``commits`` must be in ``--topo-order``).
    Leak rules (content, gitleaks, explicit path rules) run on every version.
    Policy rules (scan-lint of ``.gitattributes``/``.gitleaks.toml`` and
    ``path-ignored``) follow today's rules: on every version when
    ``policy_tips`` is None (range, prepush: everything in the range is new),
    else only on the trees of ``policy_tips`` (history: an old version that a
    later commit fixed is not judged by today's policy).
    """
    found = _identity_checks(ctx, commits)
    batch: list[tuple[int, int, str, str | None]] = []  # (first line, n lines, path, commit)
    chunks: list[bytes] = []
    cursor = 1

    def queue(text: bytes, path: str, commit: str) -> None:
        nonlocal cursor
        body = text if text.endswith(b"\n") else text + b"\n"
        n = body.count(b"\n")
        batch.append((cursor, n, path, commit))
        chunks.append(body)
        cursor += n

    seen: dict[tuple[str, str], str] = {}  # (path, blob sha) -> commit
    links: dict[str, str] = {}  # gitlink path -> commit
    for sha in commits:
        raw = gitutil.git(["log", "-1", "--format=%B", sha, "--"], ctx.repo)
        found += messages.check_message(_decode(raw), ":message", ctx.markers, commit=sha, check_format=check_format)
        queue(raw, ":message", sha)
    for sha in reversed(commits):  # oldest first, so a blob is attributed to the commit that added it
        for entry in _changed_entries(ctx, sha):
            if entry.gitlink:
                links.setdefault(entry.path, sha)
            else:
                seen.setdefault((entry.path, entry.sha), sha)
    for tag in _annotated_tags(ctx, set(commits)):
        found += messages.check_message(_decode(tag.message), ":tag-message", ctx.markers, commit=tag.sha, check_format=False)
        queue(tag.message, ":tag-message", tag.sha)

    # History mode holds every distinct blob in memory and on disk: O(history) by design.
    blobs = gitutil.read_blobs([blob_sha for _path, blob_sha in seen], ctx.repo)
    items = [BlobItem(path, commit, blobs[blob_sha]) for (path, blob_sha), commit in seen.items()]
    found += _scan_items(ctx, gl, items, cfgs, dict(links), policy=policy_tips is None)
    if policy_tips is not None:
        found += _tip_policy(ctx, policy_tips)
    if chunks:
        found += _map_text_hits(gl.text(b"".join(chunks), cfgs), batch, tip)
    return found


def _tip_policy(ctx: Context, tips: list[str]) -> list[Finding]:
    """Scan-lint and ``path-ignored`` on what the ref tips still contain (``ls-files -ci`` per tip)."""
    found: list[Finding] = []
    first: dict[str, str | None] = {}
    dirs: set[str] = set()
    lint_blobs: dict[tuple[str, str], str] = {}  # (path, blob sha) -> tip
    for tip in tips:
        for entry in gitutil.tree_entries(tip, ctx.repo):
            first.setdefault(entry.path, tip)
            if entry.gitlink:
                dirs.add(entry.path)
            elif lint.is_scan_lint_target(entry.path):
                lint_blobs.setdefault((entry.path, entry.sha), tip)
    data = gitutil.read_blobs([sha for _path, sha in lint_blobs], ctx.repo)
    for (path, sha), tip in lint_blobs.items():
        found += _scan_lint(path, data[sha], tip)
    found += _ignored(ctx, first, frozenset(dirs))
    return found


def _main_ref(remote: str) -> str:
    return f"refs/remotes/{remote}/main"


def _range_commits(ctx: Context, remote: str, base: str | None, tip: str) -> list[str]:
    """Commits in ``base..tip``; with no base, ``tip`` minus what ``<remote>/main`` already has.

    Only ``refs/remotes/<remote>/main`` is excluded: any other remote-tracking
    ref (even a forged one) could otherwise hide commits from the scan.
    """
    if base is not None:
        return gitutil.rev_list(["--topo-order", f"{base}..{tip}"], ctx.repo)
    main = gitutil.resolve_ref(_main_ref(remote), ctx.repo)
    return _commits_not_in(ctx, tip, main)


def _commits_not_in(ctx: Context, tip: str, exclude: str | None) -> list[str]:
    spec = [tip] if exclude is None else [tip, "--not", exclude]
    return gitutil.rev_list(["--topo-order", *spec], ctx.repo)


def _range_configs(ctx: Context, remote: str, base: str | None, tip: str) -> list[bytes]:
    """Tip config, plus the base one (remote sha, or merge-base with ``<remote>/main``) when it differs."""
    candidate = gl_mod.config_bytes(ctx.repo, tip)
    if base is None:
        main = gitutil.resolve_ref(_main_ref(remote), ctx.repo)
        base = gitutil.merge_base(main, tip, ctx.repo) if main else None
    return gl_mod.configs(candidate, gl_mod.base_config(ctx.repo, base))


def _remote(name: str) -> str:
    if not gitutil.REMOTE_NAME_RE.match(name):
        raise GuardError("invalid remote name (pass the remote's name, never its URL)")
    return name


def mode_range(ctx: Context, remote: str, base: str, tip: str) -> list[Finding]:
    remote = _remote(remote)
    gl = Gitleaks(ctx.repo)
    tip_sha = gitutil.resolve_commit(tip, ctx.repo)
    base_sha = None if gitutil.ZERO_SHA_RE.match(base) else gitutil.resolve_commit(base, ctx.repo)
    commits = _range_commits(ctx, remote, base_sha, tip_sha)
    cfgs = _range_configs(ctx, remote, base_sha, tip_sha)
    return range_checks(ctx, gl, commits, cfgs, tip_sha, check_format=True)


def mode_history(ctx: Context) -> list[Finding]:
    """Full-history scan: every commit reachable from any ref, merges included.

    Every file version (merges via ``-m``) is scanned with the config committed
    at ``HEAD`` united with the one at ``refs/remotes/origin/main`` when that
    ref exists (in CI it comes from GitHub), so a two-step weakening of
    ``HEAD`` cannot lower detection. Leak rules run on every version; policy
    rules (scan-lint, ``path-ignored``) only on what the ref tips contain. The
    commit-message format is not enforced here (Dependabot and web merges
    write their own); message leak rules still run. A repository with no
    commits has no history: only the binary and ``.gitleaksignore`` checks
    run, and the result is clean.
    """
    gl = Gitleaks(ctx.repo)
    commits = gitutil.rev_list(["--topo-order", "--all"], ctx.repo)
    if not commits:
        return []
    try:
        tip = gitutil.resolve_commit("HEAD", ctx.repo)
    except GuardError:
        raise GuardError("HEAD does not point at a commit: check out a branch first") from None
    origin_main = gitutil.resolve_ref(_main_ref(HISTORY_REMOTE), ctx.repo)
    cfgs = gl_mod.configs(gl_mod.config_bytes(ctx.repo, tip), gl_mod.base_config(ctx.repo, origin_main))
    return range_checks(ctx, gl, commits, cfgs, tip, check_format=False, policy_tips=gitutil.ref_tips(ctx.repo))


def mode_identity(ctx: Context, rng: str) -> list[Finding]:
    gitutil.safe_arg(rng, "range")
    commits = gitutil.rev_list(["--end-of-options", rng], ctx.repo)
    return _identity_checks(ctx, commits)


def mode_prepush(ctx: Context, remote: str, stdin: bytes) -> list[Finding]:
    remote = _remote(remote)
    found: list[Finding] = []
    updates: list[tuple[str, str]] = []  # (local sha, remote sha or "")
    for line in _decode(stdin).splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 4:
            raise GuardError("malformed pre-push input")
        _local_ref, local_sha, remote_ref, remote_sha = fields
        if not (gitutil.SHA_RE.match(local_sha) and gitutil.SHA_RE.match(remote_sha)):
            raise GuardError("malformed pre-push input")
        if not remote_ref.startswith("refs/heads/") or remote_ref == "refs/heads/":
            found.append(Finding("push-ref-not-allowed", remote_ref, 0))
            continue
        if gitutil.ZERO_SHA_RE.match(local_sha):
            if remote_ref == MAIN_REF:
                found.append(Finding("push-delete-main", remote_ref, 0))
            continue
        if gitutil.ZERO_SHA_RE.match(remote_sha):
            updates.append((local_sha, ""))
        elif not gitutil.has_object(remote_sha, ctx.repo):
            raise GuardError("the remote has commits this clone does not: run git fetch first")
        else:
            updates.append((local_sha, remote_sha))

    if updates:
        gl = Gitleaks(ctx.repo)
        remote_main: list[str | None] = []  # ls-remote once, only when a new branch needs it
        for local_sha, remote_sha in updates:
            if remote_sha:
                commits = _range_commits(ctx, remote, remote_sha, local_sha)
                cfgs = _range_configs(ctx, remote, remote_sha, local_sha)
            else:
                if not remote_main:
                    remote_main.append(_remote_main(ctx, remote))
                commits, cfgs = _new_branch_scope(ctx, remote_main[0], local_sha)
            found += range_checks(ctx, gl, commits, cfgs, local_sha, check_format=True)
    return found


def _remote_main(ctx: Context, remote: str) -> str | None:
    """``main`` as the remote has it now (``ls-remote``), never the local tracking ref (forgeable).

    None for an empty remote (bootstrap). A sha this clone does not have is rc 2.
    """
    sha = gitutil.remote_branch(remote, MAIN_REF, ctx.repo)
    if sha is not None and not gitutil.has_object(sha, ctx.repo):
        raise GuardError("the remote has commits this clone does not: run git fetch first")
    return sha


def _new_branch_scope(ctx: Context, main: str | None, tip: str) -> tuple[list[str], list[bytes]]:
    """A new branch: commits not on the remote's ``main``, scanned with its config too.

    With an empty remote (bootstrap) every commit is scanned with the
    candidate config only; that config is lint-frozen.
    """
    candidate = gl_mod.config_bytes(ctx.repo, tip)
    if main is None:
        return _commits_not_in(ctx, tip, None), [candidate]
    return _commits_not_in(ctx, tip, main), gl_mod.configs(candidate, gl_mod.base_config(ctx.repo, main))


# --------------------------------------------------------------------------- entry point


def _dispatch(argv: Sequence[str], stdin: Callable[[], bytes]) -> tuple[str, list[Finding]]:
    if not argv:
        raise GuardError("missing mode")
    mode, args = argv[0], list(argv[1:])
    arity = {
        "staged": 0, "tree": 0, "lint": 0, "history": 0,
        "message": 1, "identity": 1, "prepush": 1, "range": 3,
    }
    if mode not in arity:
        raise GuardError("unknown mode")
    if len(args) != arity[mode]:
        raise GuardError(f"wrong number of arguments for {mode}")
    repo = gitutil.toplevel(Path.cwd())
    ctx = Context(repo, load_markers(repo))
    if mode == "staged":
        return mode, mode_staged(ctx)
    if mode == "tree":
        return mode, mode_tree(ctx)
    if mode == "lint":
        return mode, mode_lint(ctx)
    if mode == "history":
        return mode, mode_history(ctx)
    if mode == "message":
        return mode, mode_message(ctx, args[0])
    if mode == "identity":
        return mode, mode_identity(ctx, args[0])
    if mode == "prepush":
        return mode, mode_prepush(ctx, args[0], stdin())
    return mode, mode_range(ctx, args[0], args[1], args[2])


def _doctor(argv: Sequence[str]) -> int:
    """``doctor`` has its own output (one ok/warn/FAIL line per check)."""
    if len(argv) != 1:
        print("guards: error: wrong number of arguments for doctor", file=sys.stderr)
        return EXIT_ERROR
    try:
        return doctor.run(gitutil.toplevel(Path.cwd()), sys.stdout)
    except GuardError as exc:
        print(f"guards: error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - fail closed without echoing details
        print(f"guards: doctor internal error ({type(exc).__name__}); failing closed", file=sys.stderr)
        return EXIT_ERROR


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["doctor"]:
        return _doctor(argv)
    try:
        mode, found = _dispatch(argv, lambda: sys.stdin.buffer.read())
    except GuardError as exc:
        print(f"guards: error: {exc}", file=sys.stderr)
        if str(exc) in ("missing mode", "unknown mode") or str(exc).startswith("wrong number"):
            print(USAGE, file=sys.stderr, end="")
        return EXIT_ERROR
    except KeyboardInterrupt:
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - fail closed without echoing details
        print(f"guards: internal error ({type(exc).__name__}); failing closed", file=sys.stderr)
        return EXIT_ERROR
    text = format_tsv(found)
    sys.stdout.write(text)
    n = text.count("\n")
    rules = sorted({f.rule for f in found})
    if n:
        print(f"guards {mode}: {n} finding(s) [{', '.join(rules)}]", file=sys.stderr)
        return EXIT_FINDINGS
    print(f"guards {mode}: clean", file=sys.stderr)
    return EXIT_CLEAN
