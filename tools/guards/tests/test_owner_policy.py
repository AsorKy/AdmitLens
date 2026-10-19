"""D-0017 hotfix: accent-insensitive names, ``identity-owner-name``, shallow history, skip-ci.

Names are fakes from ``support.fakes`` (or the public test alias); assertions
compare booleans, rule ids and shas, never a name in a diff.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest
from support.fakes import (
    OWNER_LOGIN,
    OWNER_NAME,
    OWNER_NOREPLY,
    REPO_ROOT,
    GitRepo,
    add_gitleaks_config,
    add_policy,
    assert_absent_all,
    email,
    git_repo,
    guards,
    make_fake,
    msg,
    policy_toml,
    require_gitleaks,
    run,
)

from guards.findings import GuardError
from guards.markers import Markers, normalize_name
from guards.policy import OwnerPolicy, any_flags, is_exempt, parse_policy
from guards.rules import content, identity, lint, messages

EXEMPT_SHA = "905939b59152349d71e27be7250a6f50f02fb2c4"


def _accented(name: str) -> str:
    """Same name with an acute accent on every vowel and swapped case (NFC)."""
    marked = "".join(ch + "́" if ch.lower() in "aeiou" else ch for ch in name)
    return unicodedata.normalize("NFC", marked).swapcase()


def _real() -> tuple[Markers, str]:
    name = make_fake("real-name", "accent").reveal()
    return Markers(real_names=(name,)), name


def _policy(*exempt: str) -> OwnerPolicy:
    return OwnerPolicy(OWNER_LOGIN, OWNER_NAME, frozenset(exempt))


# --------------------------------------------------------------------------- 1. accent-insensitive names


@pytest.mark.unit
def test_normalize_name_drops_accents_case_and_spacing() -> None:
    _, name = _real()
    assert normalize_name(_accented(name)) == normalize_name(name)
    assert normalize_name("  AÉ  b\t") == "ae b"


@pytest.mark.unit
def test_accented_variant_is_caught_in_identity() -> None:
    markers, name = _real()
    variant = _accented(name)
    assert variant != name and normalize_name(variant) == normalize_name(name)
    found = identity.check_identity("committer", variant, OWNER_NOREPLY, markers, commit="c1")
    assert [f.rule for f in found] == ["identity-name"]


@pytest.mark.unit
def test_accented_variant_is_caught_in_message_and_content() -> None:
    markers, name = _real()
    text = "intro\n\nby " + _accented(name) + "\n"
    assert [f.rule for f in messages.check_message(text, ":message", markers, check_format=False)] == [
        "msg-private-marker"
    ]
    assert [f.rule for f in content.check_content("NOTICE", text, markers)] == ["content-private-marker"]


@pytest.mark.unit
def test_only_the_full_name_matches_by_default() -> None:
    markers, name = _real()
    first, last = name.split()
    assert markers.name_hit("signed " + _accented(name) + ".")
    assert markers.name_hit(name.upper())
    assert not markers.name_hit("signed " + first)  # a single word is not the full name
    assert not markers.name_hit(last)
    assert not markers.name_hit("x" + name)  # bounded: not inside a longer word
    assert not markers.name_hit(name + "s")


@pytest.mark.unit
def test_particles_and_common_words_do_not_match_full_name_markers() -> None:
    markers = Markers(real_names=("Ana de la Base", "Luis Torre de Naipes"))
    assert not markers.name_hit("la base de datos")
    assert not markers.name_hit("un torre de naipes")
    assert markers.name_hit("by LUIS Torre DE NAIPES")


@pytest.mark.unit
def test_unrelated_names_are_not_caught() -> None:
    markers, _ = _real()
    assert not markers.name_hit(make_fake("real-name", "someone-else").reveal())
    assert not markers.name_hit(OWNER_NAME)


@pytest.mark.unit
def test_real_name_words_match_whole_words_only() -> None:
    _, name = _real()
    word = name.split()[1]
    markers = Markers(real_name_words=(word,))
    assert markers.name_hit("signed " + _accented(word) + ", ok")
    assert not markers.name_hit("signed x" + word + "y")
    assert not markers.name_hit(word[:-1])


@pytest.mark.git
@pytest.mark.parametrize("words", ['["ab"]', '["two words"]', '["abc"]', '"notalist"', "[1]"])
def test_invalid_real_name_words_are_rc2(tmp_path: Path, words: str) -> None:
    repo = git_repo(tmp_path)
    repo.write(".git/info/exclude", "/.guards.toml\n*.local.toml\n")
    repo.write(".guards.local.toml", f"[private]\nreal_names = []\nreal_name_words = {words}\n")
    res = repo.guards("lint")
    assert res.rc == 2
    assert ".guards.local.toml" in res.stderr


@pytest.mark.git
def test_valid_real_name_words_load(tmp_path: Path) -> None:
    repo = git_repo(tmp_path)
    repo.write(".git/info/exclude", "/.guards.toml\n*.local.toml\n")
    repo.write(".guards.local.toml", '[private]\nreal_names = []\nreal_name_words = ["Zqwerty"]\n')
    assert repo.guards("lint").rc == 0


# --------------------------------------------------------------------------- 2. identity-owner-name (unit)


@pytest.mark.unit
@pytest.mark.parametrize(
    "addr",
    [
        OWNER_NOREPLY,
        OWNER_NOREPLY.upper(),
        email(OWNER_LOGIN, "users.noreply.github.com"),
        email("+" + OWNER_LOGIN, "users.noreply.github.com"),
        "<" + OWNER_NOREPLY + ">",
    ],
)
def test_owner_noreply_variants_are_owned(addr: str) -> None:
    # Includes "+<login>@": an empty digit part is still the owner's (fail closed).
    assert _policy().owns(addr)


@pytest.mark.unit
@pytest.mark.parametrize(
    "addr",
    [
        "49699333+dependabot[bot]@users.noreply.github.com",
        email("noreply", "github.com"),
        email("1000+dev", "users.noreply.github.com"),
        email("x+" + OWNER_LOGIN, "users.noreply.github.com"),
        email(OWNER_LOGIN + "x", "users.noreply.github.com"),
        email(OWNER_LOGIN, "users.noreply.github.com.evil.io"),
    ],
)
def test_other_addresses_are_not_owned(addr: str) -> None:
    assert not _policy().owns(addr)


@pytest.mark.unit
@pytest.mark.parametrize("role", ["author", "committer", "tagger"])
def test_owner_address_with_another_name_is_a_finding(role: str) -> None:
    other = make_fake("real-name", "web-merge").reveal()
    found = identity.check_identity(role, other, OWNER_NOREPLY, Markers.empty(), "c1", owners=[_policy()])
    assert [(f.rule, f.path, f.line, f.commit) for f in found] == [("identity-owner-name", f":{role}", 0, "c1")]
    assert identity.check_identity(
        role, other, OWNER_NOREPLY, Markers.empty(), "c1", frozenset({"identity-owner-name"}), owners=[_policy()]
    ) == []


@pytest.mark.unit
@pytest.mark.parametrize("name", [OWNER_NAME, OWNER_NAME.upper(), _accented(OWNER_NAME), " " + OWNER_NAME.lower() + " "])
def test_owner_alias_in_any_case_or_accent_passes(name: str) -> None:
    assert identity.check_identity("author", name, OWNER_NOREPLY, Markers.empty(), "c1", owners=[_policy()]) == []


@pytest.mark.unit
def test_other_contributors_are_not_affected() -> None:
    other = make_fake("real-name", "contributor").reveal()
    for addr in ("49699333+dependabot[bot]@users.noreply.github.com", email("noreply", "github.com"), email("7+dev", "users.noreply.github.com")):
        assert identity.check_identity("author", other, addr, Markers.empty(), "c1", owners=[_policy()]) == []
    assert identity.check_identity("author", "dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com", Markers.empty(), "c1", owners=[_policy()]) == []


@pytest.mark.unit
def test_exempt_commit_skips_only_the_name_rules_for_the_owner_identity() -> None:
    markers, name = _real()
    sha = "a" * 40
    exempt = [_policy(sha)]
    assert identity.check_identity("committer", name, OWNER_NOREPLY, markers, sha, owners=exempt) == []
    # Another commit, or the same commit's non-owner identity, is still checked.
    assert [f.rule for f in identity.check_identity("committer", name, OWNER_NOREPLY, markers, "b" * 40, owners=exempt)] == [
        "identity-name", "identity-owner-name",
    ]
    assert [f.rule for f in identity.check_identity("author", name, email("7+dev", "users.noreply.github.com"), markers, sha, owners=exempt)] == [
        "identity-name"
    ]


@pytest.mark.unit
def test_stricter_policy_wins_when_only_one_exempts() -> None:
    other = make_fake("real-name", "branch-exempt").reveal()
    sha = "c" * 40
    assert not any_flags([_policy(sha)], other, OWNER_NOREPLY, sha)
    assert any_flags([_policy(sha), _policy()], other, OWNER_NOREPLY, sha)
    assert not is_exempt([_policy(sha), _policy()], OWNER_NOREPLY, sha)
    assert not is_exempt([], OWNER_NOREPLY, sha)


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "",
        "not toml = [",
        "[identity]\nowner_name = \"x\"\n",
        "[identity]\nowner_login = \"-bad\"\nowner_name = \"x\"\n",
        "[identity]\nowner_login = \"ok\"\nowner_name = \"  \"\n",
        policy_toml(exempt=("905939b*",)),
        policy_toml(exempt=("*",)),
        policy_toml(exempt=("905939b",)),
        policy_toml(exempt=(EXEMPT_SHA.upper(),)),
        policy_toml() + "extra = 1\n",
        policy_toml() + "[other]\nx = 1\n",
        "[identity]\nowner_login = \"ok\"\nowner_name = \"x\"\nexempt_commits = \"" + EXEMPT_SHA + "\"\n",
        policy_toml(exempt=(EXEMPT_SHA + "\n",)),  # trailing newline: fullmatch, not "$"
        policy_toml(login=OWNER_LOGIN + "\n"),
    ],
)
def test_invalid_policy_is_rejected(text: str) -> None:
    with pytest.raises(GuardError):
        parse_policy(text.encode())


@pytest.mark.unit
def test_real_policy_file_parses_and_lists_full_shas_only() -> None:
    policy = parse_policy((REPO_ROOT / ".guards.toml").read_bytes())
    assert policy.owner_login and policy.owner_name
    assert all(len(sha) == 40 for sha in policy.exempt_commits)


@pytest.mark.git
def test_real_policy_file_is_versionable() -> None:
    """``.guards.toml`` must not be matched by any ignore rule (``*.local.toml`` still is)."""
    res = run(["git", "check-ignore", "-q", "--no-index", ".guards.toml"], REPO_ROOT)
    assert res.rc == 1
    assert run(["git", "check-ignore", "-q", "--no-index", ".guards.local.toml"], REPO_ROOT).rc == 0


# --------------------------------------------------------------------------- 2. identity-owner-name (git)


def _owner_repo(tmp_path: Path) -> tuple[GitRepo, str]:
    repo = git_repo(tmp_path)
    repo.write("README.md", "# demo\n")
    base = repo.commit(msg("init"), "README.md", add_gitleaks_config(repo))
    bare = tmp_path / "remote.git"
    repo.git("init", "-q", "--bare", str(bare))
    repo.git("remote", "add", "origin", str(bare))
    repo.git("push", "-q", "--no-verify", "origin", "main")
    repo.git("fetch", "-q", "origin")
    return repo, base


def _web_merge(repo: GitRepo, name: str) -> str:
    """A commit whose committer is the owner's noreply with ``name`` (what a web merge writes)."""
    repo.write("f.txt", "x\n")
    return repo.commit(msg("web"), "f.txt", extra_env={"GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": OWNER_NOREPLY})


@pytest.mark.git
@pytest.mark.gitleaks
def test_owner_name_mismatch_fails_prepush_range_and_history(tmp_path: Path) -> None:
    require_gitleaks()
    repo, base = _owner_repo(tmp_path)
    secret_name = make_fake("real-name", "web-merge-git")
    sha = _web_merge(repo, secret_name.reveal())
    expected = ("identity-owner-name", ":committer", "0", sha)
    results = {
        "range": repo.guards("range", "origin", base, "HEAD"),
        "prepush": repo.guards("prepush", "origin", input=f"refs/heads/main {sha} refs/heads/main {base}\n"),
        "history": repo.guards("history"),
        "identity": repo.guards("identity", f"{base}..HEAD"),
    }
    for mode, res in results.items():
        assert res.rc == 1, mode
        assert expected in res.findings, mode
        assert_absent_all(res, [secret_name], f"owner-name-{mode}")


@pytest.mark.git
@pytest.mark.gitleaks
def test_exempt_sha_is_clean_and_alias_variants_pass(tmp_path: Path) -> None:
    require_gitleaks()
    repo, base = _owner_repo(tmp_path)
    sha = _web_merge(repo, make_fake("real-name", "exempted").reveal())
    add_policy(repo, policy_toml(exempt=(sha,)))
    assert repo.guards("range", "origin", base, "HEAD").rc == 0
    assert repo.guards("history").rc == 0
    assert repo.guards("lint").rc == 0
    repo.write("g.txt", "y\n")
    repo.commit(msg("alias"), "g.txt", extra_env={"GIT_COMMITTER_NAME": _accented(OWNER_NAME), "GIT_COMMITTER_EMAIL": OWNER_NOREPLY})
    assert repo.guards("range", "origin", base, "HEAD").rc == 0


@pytest.mark.git
@pytest.mark.gitleaks
def test_a_branch_cannot_exempt_its_own_commit_against_the_base(tmp_path: Path) -> None:
    """The base's committed ``.guards.toml`` still flags the commit (stricter of both)."""
    require_gitleaks()
    repo = git_repo(tmp_path)
    repo.write("README.md", "# demo\n")
    repo.write(".guards.toml", policy_toml())
    base = repo.commit(msg("init"), "README.md", ".guards.toml", add_gitleaks_config(repo))
    sha = _web_merge(repo, make_fake("real-name", "self-exempt").reveal())
    repo.write(".guards.toml", policy_toml(exempt=(sha,)))
    repo.commit(msg("exempt myself"), ".guards.toml")
    res = repo.guards("range", "origin", base, "HEAD")
    assert res.rc == 1
    assert ("identity-owner-name", ":committer", "0", sha) in res.findings
    for spec in (f"{base}..HEAD", f"{base[:12]}..main"):
        res = repo.guards("identity", spec)  # an A..B range applies the policy committed at A too
        assert res.rc == 1, spec
        assert ("identity-owner-name", ":committer", "0", sha) in res.findings, spec
    assert repo.guards("identity", "HEAD").rc == 0  # no base: the work-tree policy alone exempts it


@pytest.mark.git
def test_lint_rejects_an_exemption_without_an_owner_identity(tmp_path: Path) -> None:
    repo = git_repo(tmp_path)
    sha = repo.commit(msg("plain"))  # author and committer are the test dev, not the owner
    add_policy(repo, policy_toml(exempt=(sha,)))
    res = repo.guards("lint")
    assert res.rc == 1
    assert res.findings == [("guards-config-exempt", ".guards.toml", "0", sha)]
    add_policy(repo, policy_toml(exempt=("d" * 40,)))  # not in this clone: a warning, not a finding
    res = repo.guards("lint")
    assert res.rc == 0
    assert "warn .guards.toml: 1 exempt_commits sha(s) not in this clone" in res.stderr


@pytest.mark.git
@pytest.mark.parametrize("mode", ["missing", "invalid", "wildcard"])
def test_missing_or_invalid_policy_is_rc2(tmp_path: Path, mode: str) -> None:
    repo = git_repo(tmp_path)
    base = repo.commit(msg("init"))
    if mode == "missing":
        (repo.root / ".guards.toml").unlink()
    elif mode == "invalid":
        add_policy(repo, "[identity\n")
    else:
        add_policy(repo, policy_toml(exempt=("9059*",)))
    for args in (("identity", "HEAD"), ("range", "origin", base, "HEAD"), ("history",), ("lint",)):
        res = repo.guards(*args)
        assert res.rc == 2, (mode, args[0])
        assert ".guards.toml" in res.stderr
    res = repo.guards("prepush", "origin", input=f"refs/heads/main {base} refs/heads/main {base}\n")
    assert res.rc == 2
    assert repo.guards("doctor").stdout.count("FAIL  .guards.toml present and valid") == 1


@pytest.mark.git
def test_doctor_checks_the_local_owner_identity(tmp_path: Path) -> None:
    repo = git_repo(tmp_path)
    check = "user.name is the owner alias for the owner noreply address"

    def status() -> str:
        lines = [line.split(None, 1) for line in repo.guards("doctor").stdout.splitlines()]
        return {name: st for st, name in (p for p in lines if len(p) == 2)}[check]

    assert status() == "ok"  # another contributor's address: not concerned
    repo.git("config", "user.email", OWNER_NOREPLY)
    repo.git("config", "user.name", make_fake("real-name", "doctor-owner").reveal())
    assert status() == "FAIL"
    repo.git("config", "user.name", _accented(OWNER_NAME))
    assert status() == "ok"


@pytest.mark.git
def test_real_repo_identities_and_policy_lint_are_clean(tmp_path: Path) -> None:
    """The exemption keeps the real history clean; ``guards lint`` accepts the real policy."""
    assert guards(["identity", "HEAD"], REPO_ROOT, home=tmp_path / ".home").rc == 0
    assert guards(["lint"], REPO_ROOT, home=tmp_path / ".home").rc == 0


# --------------------------------------------------------------------------- 3. shallow history


@pytest.mark.git
@pytest.mark.parametrize("ci", [True, False])
def test_history_on_a_shallow_clone_is_rc2(tmp_path: Path, ci: bool) -> None:
    src = git_repo(tmp_path, "src")
    src.commit(msg("one"))
    src.commit(msg("two"))
    clone = GitRepo(tmp_path / "shallow", src.home)
    src.git("clone", "-q", "--depth", "1", "file://" + str(src.root), str(clone.root))
    add_policy(clone)
    res = clone.guards("history", extra_env={"CI": "true"} if ci else None)
    assert res.rc == 2
    assert "history needs the full clone (fetch-depth: 0)" in res.stderr


# --------------------------------------------------------------------------- 4. URL is a secret-looking Make name


@pytest.mark.unit
@pytest.mark.parametrize("line", ["\t@echo $(ADMITLENS_DATABASE_URL)", "X = ${DATABASE_URL}", "\t@curl $(PUSH_URL)"])
def test_make_url_variable_is_a_finding(line: str) -> None:
    found = lint.check_makefile("Makefile", line + "\n")
    assert [(f.rule, f.line) for f in found] == [("make-secret-var", 1)]


@pytest.mark.unit
@pytest.mark.parametrize("line", ["\t@$(CURL) -s x", "X = $(CURL_OPTS)", "Y = $(URLENCODE)", "Z = $(MYURL)"])
def test_make_url_inside_another_word_is_fine(line: str) -> None:
    assert lint.check_makefile("Makefile", line + "\n") == []


@pytest.mark.unit
@pytest.mark.parametrize("line", ["\t@echo $(URL)", "\t@echo $(URLS)", "\t@echo $(URL_BASE)"])
def test_make_url_token_is_a_finding(line: str) -> None:
    assert [f.rule for f in lint.check_makefile("Makefile", line + "\n")] == ["make-secret-var"]


@pytest.mark.unit
def test_make_shell_url_variable_is_fine() -> None:
    assert lint.check_makefile("Makefile", '\t@PUSH_URL="$$PUSH_URL" run\n\t@echo "$$ADMITLENS_DATABASE_URL"\n') == []


# --------------------------------------------------------------------------- 5. msg-skip-ci


SKIP_LINES = [
    "[Implementing - 1]{2026-10-17 PLAN-CA-0005}: fix [skip ci]",
    "[ci skip]",
    "body [No CI] here",
    "[SKIP ACTIONS]",
    "[actions skip]",
    "skip-checks: true",
    "Skip-Checks:TRUE",
]


@pytest.mark.unit
@pytest.mark.parametrize("line", SKIP_LINES)
def test_skip_ci_markers_are_findings(line: str) -> None:
    text = msg("x") + line + "\n"
    found = [f for f in messages.check_message(text, ":message", Markers.empty()) if f.rule == "msg-skip-ci"]
    assert [(f.path, f.line) for f in found] == [(":message", len(text.splitlines()))]
    assert [
        f
        for f in messages.check_message(text, ":message", Markers.empty(), disabled=frozenset({"msg-skip-ci"}))
        if f.rule == "msg-skip-ci"
    ] == []


@pytest.mark.unit
@pytest.mark.parametrize("line", ["skip ci in prose", "[skip-ci]", "skip-checks: false", "note: skip-checks: true later"])
def test_skip_ci_lookalikes_are_not_findings(line: str) -> None:
    assert not [f for f in messages.check_message(msg("x") + line + "\n", ":message", Markers.empty()) if f.rule == "msg-skip-ci"]


@pytest.mark.git
@pytest.mark.gitleaks
def test_skip_ci_in_a_message_file_is_a_finding(tmp_path: Path) -> None:
    require_gitleaks()
    repo = git_repo(tmp_path)
    add_gitleaks_config(repo)
    f = repo.write("MSG", msg("x") + "[skip ci]\n")
    res = repo.guards("message", str(f))
    assert res.rc == 1 and res.rules == {"msg-skip-ci"}


@pytest.mark.git
@pytest.mark.gitleaks
def test_skip_ci_fails_range_prepush_history_and_tag_messages(tmp_path: Path) -> None:
    require_gitleaks()
    repo, base = _owner_repo(tmp_path)
    sha = repo.commit(msg("quiet") + "[ci skip]\n")
    for mode, res in {
        "range": repo.guards("range", "origin", base, "HEAD"),
        "prepush": repo.guards("prepush", "origin", input=f"refs/heads/main {sha} refs/heads/main {base}\n"),
        "history": repo.guards("history"),
    }.items():
        assert res.rc == 1, mode
        assert [f for f in res.findings if f[0] == "msg-skip-ci" and f[3] == sha], mode
    (tmp_path / "tag").mkdir()
    repo, base = _owner_repo(tmp_path / "tag")
    repo.commit(msg("tagged"))
    repo.git("tag", "-a", "t0", "-F", "-", input="release\n\nskip-checks: true\n")
    res = repo.guards("history")
    assert res.rc == 1
    assert [f for f in res.findings if f[0] == "msg-skip-ci" and f[1] == ":tag-message"]

