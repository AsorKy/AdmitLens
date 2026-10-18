"""Tests for the repository ``.gitleaks.toml``.

Self-contained on purpose: the helpers below do not depend on the guards package.

Safety rules followed here:

* No secret-shaped literal lives in this file. Fakes are assembled at runtime from
  fragments of three characters or fewer.
* Fakes are written to temp files or sent through stdin, never through argv.
* gitleaks always runs with ``--redact``; only RuleID, File and StartLine are read
  from its JSON report, and the report text itself is checked with ``assert_absent``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.gitleaks

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG = REPO_ROOT / ".gitleaks.toml"
PINNED_VERSION = "8.30.1"
TIMEOUT = 20


# --------------------------------------------------------------------------- helpers


class Fake:
    """A runtime-assembled fake. Its repr and str never show the value."""

    def __init__(self, kind: str, value: str, sensitive: str | None = None) -> None:
        self._kind = kind
        self._value = value
        # The part that must never be echoed (e.g. the password inside a DSN).
        # The rest (scheme, host) is allowed to appear in a redacted Match.
        self._sensitive = sensitive if sensitive is not None else value

    def reveal(self) -> str:
        return self._value

    def sensitive(self) -> str:
        return self._sensitive

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self._value.encode()).hexdigest()[:8]

    def __repr__(self) -> str:
        return f"<fake {self._kind} {self.fingerprint}>"

    __str__ = __repr__


def assert_absent(text: str, fake: Fake, channel: str) -> None:
    """Fail without printing the value if ``fake`` (or a 10-char window) is in ``text``."""
    if fake.reveal() in text:
        pytest.fail(f"fake {fake!r} leaked in {channel}", pytrace=False)
    value = fake.sensitive()
    if value in text:
        pytest.fail(f"fake {fake!r} leaked in {channel}", pytrace=False)
    if len(value) > 10:
        for i in range(len(value) - 9):
            if value[i : i + 10] in text:
                pytest.fail(f"fragment of fake {fake!r} leaked in {channel}", pytrace=False)


@dataclass(frozen=True)
class RunResult:
    rc: int
    stdout: str
    stderr: str

    def __repr__(self) -> str:
        return f"RunResult(rc={self.rc}, stdout_len={len(self.stdout)}, stderr_len={len(self.stderr)})"


def _gitleaks_bin() -> str:
    local = REPO_ROOT / ".tools" / "bin" / "gitleaks"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    found = shutil.which("gitleaks")
    if found:
        return found
    in_ci = os.environ.get("CI", "") == "true"
    if os.environ.get("GUARDS_ALLOW_SKIP") == "1" and not in_ci:
        pytest.skip("gitleaks not installed (GUARDS_ALLOW_SKIP=1)")
    pytest.fail("gitleaks not found: run the installer in tools/guards/bin", pytrace=False)


def _clean_env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home),
        "LC_ALL": "C",
        "TZ": "UTC",
        "NO_COLOR": "1",
    }


def run(argv: list[str], home: Path, cwd: Path, stdin: str | None = None) -> RunResult:
    """Run a command in a clean env. ``argv`` must never carry a fake."""
    proc = subprocess.run(  # noqa: S603 - argv is built from fixed tokens and temp paths
        argv,
        cwd=cwd,
        env=_clean_env(home),
        input=stdin,
        stdin=None if stdin is not None else subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )
    return RunResult(proc.returncode, proc.stdout, proc.stderr)


@dataclass(frozen=True)
class Finding:
    rule: str
    file: str
    line: int


class Workspace:
    """A temp project: ``src/`` is scanned, ``out/`` holds reports and home."""

    def __init__(self, root: Path, rng: random.Random) -> None:
        self.src = root / "src"
        self.out = root / "out"
        self.home = root / "home"
        for d in (self.src, self.out, self.home):
            d.mkdir()
        self.rng = rng
        self.fakes: list[Fake] = []

    def fake(self, kind: str, value: str, sensitive: str | None = None) -> Fake:
        f = Fake(kind, value, sensitive)
        self.fakes.append(f)
        return f

    def word(self, n: int = 8) -> Fake:
        """Low-entropy lowercase value: the default rules do not flag these."""
        return self.fake("word", "".join(self.rng.choices("abcdefghij", k=n)))

    def write(self, rel: str, content: str) -> None:
        path = self.src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def scan(self, config: Path = CONFIG) -> tuple[list[Finding], RunResult]:
        report = self.out / "report.json"
        argv = [
            _gitleaks_bin(), "dir", ".",
            "--config", str(config),
            "--redact",
            "--no-banner",
            "--log-level", "error",
            "--report-format", "json",
            "--report-path", str(report),
        ]  # fmt: skip
        result = run(argv, self.home, self.src)
        return self._read(report, result)

    def scan_stdin(self, text: str, config: Path = CONFIG) -> tuple[list[Finding], RunResult]:
        report = self.out / "report-stdin.json"
        argv = [
            _gitleaks_bin(), "stdin",
            "--config", str(config),
            "--redact",
            "--no-banner",
            "--log-level", "error",
            "--report-format", "json",
            "--report-path", str(report),
        ]  # fmt: skip
        result = run(argv, self.home, self.src, stdin=text)
        return self._read(report, result)

    def _read(self, report: Path, result: RunResult) -> tuple[list[Finding], RunResult]:
        assert result.rc in (0, 1), result
        findings: list[Finding] = []
        if report.exists():
            raw = report.read_text(encoding="utf-8")
            for fake in self.fakes:
                assert_absent(raw, fake, "json report")
                assert_absent(result.stdout, fake, "stdout")
                assert_absent(result.stderr, fake, "stderr")
            # Only these three fields are ever read from the report.
            for item in json.loads(raw):
                findings.append(Finding(item["RuleID"], item["File"], item["StartLine"]))
        else:
            for fake in self.fakes:
                assert_absent(result.stdout, fake, "stdout")
                assert_absent(result.stderr, fake, "stderr")
        assert (result.rc == 1) == bool(findings), result
        return findings, result


@pytest.fixture
def ws(tmp_path: Path, request: pytest.FixtureRequest) -> Workspace:
    return Workspace(tmp_path, random.Random(request.node.nodeid))


def rules_for(findings: list[Finding], file: str) -> set[str]:
    return {f.rule for f in findings if f.file == file}


def custom_rules(findings: list[Finding]) -> set[str]:
    return {f.rule for f in findings if f.rule.startswith("admitlens-")}


# ------------------------------------------------------------------ fake assemblers


def b64url(obj: object) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def make_jwt(ws: Workspace) -> Fake:
    sig = base64.urlsafe_b64encode(bytes(ws.rng.randrange(256) for _ in range(32))).decode().rstrip("=")
    head = b64url({"alg": "HS256", "typ": "JWT"})
    body = b64url({"sub": "user-1234567890", "name": "Fake Person", "iat": 1700000000})
    return ws.fake("jwt", f"{head}.{body}.{sig}")


def make_dsn(ws: Workspace, scheme: str, user: str = "app", host: str = "db.test:5432/x") -> Fake:
    pw = ws.word(9).reveal()
    return ws.fake("dsn", f"{scheme}" + "://" + f"{user}:{pw}@{host}", sensitive=pw)


def make_ssn(ws: Workspace, area: str, group: str, serial: str) -> Fake:
    return ws.fake("ssn", f"{area}-{group}-{serial}")


def user_path(ws: Workspace, kind: str) -> Fake:
    name = "jd" + "oe"
    parts = {
        "mac": ["/Us", "ers/", name, "/proj/x.py"],
        "linux": ["/ho", "me/", name, "/proj/x.py"],
        "win": ["C:", "\\Us", "ers\\", name, "\\proj\\x.py"],
        "slug": ["-Us", "ers-", name, "-proj"],
        "claude": ["/pri", "vate/", "tmp/", "clau", "de-501/x"],
        "folders": ["/pri", "vate/", "var/", "fold", "ers/ab/T/x"],
    }[kind]
    return ws.fake("local-path", "".join(parts), sensitive=name if kind in {"mac", "linux", "win", "slug"} else None)


# ------------------------------------------------------------------ loading and basics


def test_binary_version_is_exactly_pinned(tmp_path: Path) -> None:
    result = run([_gitleaks_bin(), "version"], tmp_path, tmp_path)
    assert result.rc == 0
    assert result.stdout.strip() == PINNED_VERSION


def test_config_loads_on_empty_dir(ws: Workspace) -> None:
    findings, result = ws.scan()
    assert result.rc == 0
    assert findings == []


def test_config_does_not_flag_itself(ws: Workspace) -> None:
    """The toml and this very test file are free of secret-shaped content."""
    # The default allowlist skips any path containing the config's own file name, so the
    # copies get neutral names; otherwise the scan would be vacuous.
    ws.write("config-copy.toml", CONFIG.read_text(encoding="utf-8"))
    ws.write("test_self_copy.py", Path(__file__).read_text(encoding="utf-8"))
    findings, _ = ws.scan()
    assert findings == []


def test_invalid_config_fails_loudly(ws: Workspace, tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("[[rules]]\nid = 'x'\nregex = '(?!lookahead)'\n", encoding="utf-8")
    result = run(
        [_gitleaks_bin(), "dir", ".", "--config", str(bad), "--no-banner", "--log-level", "error"],
        ws.home,
        ws.src,
    )
    assert result.rc not in (0, 1)


# ------------------------------------------------------------------ the shared corpus
#
# Most cases below are one small file each. Running gitleaks once per case cost
# ~0.2 s x 130 cases (D-0008), so every case is written into its own directory
# (``c0000/``, ``c0001/``, ...) of ONE workspace, scanned once with the repo config
# and once with the default-only config. Each test then reads the findings of its
# own directory, with that prefix stripped: paths and line numbers are exactly
# what a lone scan reports. Every path rule is anchored with ``(?:^|/)``, so the
# prefix cannot change a match, and gitleaks matches file by file. The corpus
# scan also runs ``assert_absent`` for every fake of every case.


DSN_SCHEMES = ["postgresql", "postgresql+psycopg", "postgres+asyncpg", "redis", "rediss", "amqps", "mongodb+srv", "mysql"]
DSN_NEGATIVE = [
    "postgresql://app@db.test:5432/x",
    "postgresql+psycopg://app@localhost/admitlens_test",
    "postgresql://app:${POSTGRES_PASSWORD}@db:5432/x",
    "postgresql://app:<password>@db:5432/x",
    "postgresql://app:__GENERATE__@db:5432/x",
    "redis://:__REQUIRED__@cache:6379/0",
    "redis://cache:6379/0",
    "https://example.test/a:b@c",
]
ASSIGN_POSITIVE = [
    ("settings/app.yml", "  {name}: {value}\n"),
    ("settings/app.yaml", "{name}: \"{value}\"\n"),
    ("pyproject.toml", "{name} = \"{value}\"\n"),
    ("docker-compose.yml", "    environment:\n      {name}: {value}\n"),
    ("setup.cfg", "{name} = {value}\n"),
    ("conf.ini", "{name}={value}\n"),
    (".env.local", "{name}={value}\n"),
    (".env", "{name}={value}\n"),
]
ASSIGN_WORDS = ["TOK" + "EN", "SEC" + "RET", "API_" + "KEY", "DS" + "N", "PRIVATE_" + "KEY"]
ASSIGN_NEGATIVE = [
    (".env.example", "X_PASSWORD=__GENERATE__\nX_TOKEN=\nY_SECRET=__REQUIRED__\nZ_DSN=${Z_DSN}\n"),
    (".env.example", "A_TOKEN=<paste-a-token-here>\nPOSTGRES_USER=admitlens\n"),
    ("ci.yml", "env:\n  X_TOKEN: ${{ secrets.X_TOKEN }}\n  Y_PASSWORD: $$Y_PASSWORD\n"),
    ("docker-compose.yml", "    POSTGRES_PASSWORD_FILE: /run/secrets/db_password\n    secrets:\n      - db_password\n"),
    ("app.yml", "ACCESS_TOKEN_TTL: 900\nsecret_rotation_days: 30\nuse_secret: true\n"),
    ("app.yml", "secrets:\n  db_password:\n    file: ./x\n"),
    ("data.json", "{\"user_password\": \"abcdefgh\"}\n"),
    ("notes.md", "user_password = abcdefgh\n"),
]
PY_NEGATIVE = [
    'X_PASSWORD = os.environ["X_PASSWORD"]',
    'db_password = os.environ.get("DB_PASSWORD")',
    "db_password: str = Field(...)",
    'db_password: SecretStr = Field(default="")',
    "token = settings.api_token",
    'if x_token == "abc":',
    "password = None",
    'DB_DSN = f"{scheme}://host/db"',
    'password = ""',
]
SSN_POSITIVE = [("123", "45", "6789"), ("001", "01", "0001"), ("665", "99", "9999"), ("667", "10", "1000"), ("899", "50", "5050")]
SSN_INVALID = [("000", "12", "3456"), ("666", "12", "3456"), ("900", "12", "3456"), ("999", "12", "3456"), ("123", "00", "6789"), ("123", "45", "0000")]
SSN_LOOKALIKES = ["2026-10-17T13:24:05.120Z", "2026-10-17", "123-456-7890", "call 555-123-4567 now", "1234-56-78901"]
LOCAL_KINDS = ["mac", "linux", "win", "slug", "claude", "folders"]
LOCAL_NEGATIVE = [
    "/home/runner/work/admitlens/admitlens",
    "./src/users/models.py",
    "/usr/local/bin/python",
    "/opt/app/data",
    "users-table",
    "/var/run/postgresql",
]
VAULT_POSITIVE = [("Obsid", "ian Va", "ult/Notes"), ("[", "[proj", "ects/AdmitLens]]")]
VAULT_NEGATIVE = ["an obsidian vault is a folder", "projects/ is a dir", "[link](projects/x)", "[[wikilink]]"]
PATH_POSITIVE = [
    (".env", "admitlens-path-env"),
    (".env.local", "admitlens-path-env"),
    (".env.production", "admitlens-path-env"),
    ("level-1/backend/.env", "admitlens-path-env"),
    (".env.examples", "admitlens-path-env"),
    (".env.exampl", "admitlens-path-env"),
    ("data/a.txt", "admitlens-path-data"),
    ("level-1/data/sub/b.txt", "admitlens-path-data"),
    ("certs/server.pem", "admitlens-path-key"),
    ("a.key", "admitlens-path-key"),
    ("store.p12", "admitlens-path-key"),
    ("bundle.pfx", "admitlens-path-key"),
    ("id_rsa", "admitlens-path-key"),
    ("keys/id_ed25519.pub", "admitlens-path-key"),
    ("local.db", "admitlens-path-db"),
    ("x/y.sqlite3", "admitlens-path-db"),
    ("x.db-wal", "admitlens-path-db"),
]
PATH_NEGATIVE = [
    ".env.example",
    "level-1/backend/.env.example",
    "datagen/readme.txt",
    "src/database.py",
    "docs/keys.md",
    "env.txt",
    "notes/idea_rsa.md",
    "dbconfig.txt",
]


def _name(*frags: str) -> str:
    return "".join(frags)


class Corpus:
    """Every case in its own directory of one workspace; two gitleaks scans in total."""

    def __init__(self, ws: Workspace, default_only: Path) -> None:
        self.ws = ws
        self._dirs: dict[tuple[object, ...], str] = {}
        self._build()
        self._full, _ = ws.scan()
        self._default, _ = ws.scan(default_only)

    def _put(self, key: tuple[object, ...], rel: str, text: str) -> None:
        assert key not in self._dirs, key
        prefix = f"c{len(self._dirs):04d}"
        self._dirs[key] = prefix
        self.ws.write(f"{prefix}/{rel}", text)

    @staticmethod
    def _strip(findings: list[Finding], prefix: str) -> list[Finding]:
        lead = prefix + "/"
        return [Finding(f.rule, f.file[len(lead) :], f.line) for f in findings if f.file.startswith(lead)]

    def findings(self, *key: object) -> list[Finding]:
        """What a lone scan of this case reports with the repo config."""
        return self._strip(self._full, self._dirs[key])

    def default_findings(self, *key: object) -> list[Finding]:
        """What a lone scan of this case reports with gitleaks' default rules only."""
        return self._strip(self._default, self._dirs[key])

    def _build(self) -> None:  # noqa: C901 - one block per case family, mirrors the tests below
        ws = self.ws
        self._put(("jwt",), "notes.txt", f"value: {make_jwt(ws).reveal()}\n")
        for scheme in DSN_SCHEMES:
            self._put(("dsn+", scheme), "notes.txt", f"first line\nurl is {make_dsn(ws, scheme).reveal()} ok\n")
        for line in DSN_NEGATIVE:
            self._put(("dsn-", line), "notes.txt", line + "\n")
        self._put(("dsn-attr",), "notes.txt", f"{make_dsn(ws, 'postgresql').reveal()}\n")
        name = _name("DB_", "PASS", "WORD")
        for path, template in ASSIGN_POSITIVE:
            self._put(("assign+", path, template), path, template.format(name=name, value=ws.word().reveal()))
        for word in ASSIGN_WORDS:
            self._put(("assign-word", word), "a.yml", f"svc_{word}: {ws.word().reveal()}\n")
        for path, text in ASSIGN_NEGATIVE:
            self._put(("assign-", path, text), path, text)
        self._put(("assign-attr",), "a.yml", f"{name}: {ws.word().reveal()}\n")
        v1, v2 = ws.word(), ws.word()
        n2 = _name("api_", "TOK", "EN")
        self._put(("py+",), "settings.py",
                  f"import os\n\n{name} = \"{v1.reveal()}\"\nclass C:\n    {n2}: str = '{v2.reveal()}'\n")
        for line in PY_NEGATIVE:
            self._put(("py-", line), "config.py", f"import os\n{line}\n")
        for parts in SSN_POSITIVE:
            self._put(("ssn+", *parts), "notes.txt", f"id\nvalue {make_ssn(ws, *parts).reveal()} end\n")
        for parts in SSN_INVALID:
            self._put(("ssn-", *parts), "notes.txt", f"value {make_ssn(ws, *parts).reveal()} end\n")
        for text in SSN_LOOKALIKES:
            self._put(("ssn~", text), "notes.txt", text + "\n")
        self._put(("ssn-attr",), "notes.txt", f"{make_ssn(ws, '123', '45', '6789').reveal()}\n")
        for kind in LOCAL_KINDS:
            self._put(("local+", kind), "notes.txt", f"x\nsee {user_path(ws, kind).reveal()}\n")
        self._put(("local-attr",), "notes.txt", f"{user_path(ws, 'mac').reveal()}\n")
        for text in LOCAL_NEGATIVE:
            self._put(("local-", text), "notes.txt", text + "\n")
        for marker in VAULT_POSITIVE:
            self._put(("vault+", marker), "README.txt", f"a\nb\nsee {ws.fake('vault', ''.join(marker)).reveal()}\n")
        for text in VAULT_NEGATIVE:
            self._put(("vault-", text), "README.txt", text + "\n")
        for path, _rule in PATH_POSITIVE:
            self._put(("path+", path), path, "N=1\n")
        for path in PATH_NEGATIVE:
            self._put(("path-", path), path, "N=1\n")


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Corpus:
    root = tmp_path_factory.mktemp("gitleaks-corpus")
    default_only = root / "default-only.toml"
    default_only.write_text("[extend]\nuseDefault = true\n", encoding="utf-8")
    ws_root = root / "ws"
    ws_root.mkdir()
    return Corpus(Workspace(ws_root, random.Random("gitleaks-corpus")), default_only)


def test_corpus_is_not_vacuous(corpus: Corpus) -> None:
    """The aggregated scan found the custom rules and kept every case apart."""
    assert len(corpus._dirs) >= 110
    assert {f.rule for f in corpus._full} >= {
        "jwt", "admitlens-dsn-password", "admitlens-secret-assignment", "admitlens-secret-assignment-py",
        "admitlens-ssn", "admitlens-local-path", "admitlens-vault-marker", "admitlens-path-env",
        "admitlens-path-data", "admitlens-path-key", "admitlens-path-db",
    }
    assert all(f.file.split("/", 1)[0] in set(corpus._dirs.values()) for f in corpus._full)


# ------------------------------------------------------------------ default rules


def test_default_rules_still_active_jwt(corpus: Corpus) -> None:
    rules = rules_for(corpus.findings("jwt"), "notes.txt")
    assert "jwt" in rules, sorted(rules)
    assert not any(r.startswith("admitlens-") for r in rules)


def test_jwt_via_stdin(ws: Workspace) -> None:
    jwt = make_jwt(ws)
    findings, _ = ws.scan_stdin(f"x = {jwt.reveal()}\n")
    assert "jwt" in {f.rule for f in findings}


# ------------------------------------------------------------------ dsn-password


@pytest.mark.parametrize("scheme", DSN_SCHEMES)
def test_dsn_password_positive(corpus: Corpus, scheme: str) -> None:
    assert Finding("admitlens-dsn-password", "notes.txt", 2) in corpus.findings("dsn+", scheme)


def test_dsn_password_positive_via_stdin(ws: Workspace) -> None:
    dsn = make_dsn(ws, "postgresql+psycopg")
    findings, _ = ws.scan_stdin(f"url = {dsn.reveal()}\n")
    assert "admitlens-dsn-password" in {f.rule for f in findings}


@pytest.mark.parametrize("line", DSN_NEGATIVE)
def test_dsn_password_negative(corpus: Corpus, line: str) -> None:
    assert corpus.findings("dsn-", line) == []


def test_dsn_attribution_default_rules_miss_it(corpus: Corpus) -> None:
    assert corpus.default_findings("dsn-attr") == []
    assert "admitlens-dsn-password" in custom_rules(corpus.findings("dsn-attr"))


# ------------------------------------------------------------------ secret-assignment (config)


@pytest.mark.parametrize(("path", "template"), ASSIGN_POSITIVE)
def test_secret_assignment_positive(corpus: Corpus, path: str, template: str) -> None:
    name = _name("DB_", "PASS", "WORD")
    lines = template.format(name=name, value="v").splitlines()
    line = 1 + lines.index(next(ln for ln in lines if name in ln))
    assert Finding("admitlens-secret-assignment", path, line) in corpus.findings("assign+", path, template)


@pytest.mark.parametrize("word", ASSIGN_WORDS)
def test_secret_assignment_name_variants(corpus: Corpus, word: str) -> None:
    assert Finding("admitlens-secret-assignment", "a.yml", 1) in corpus.findings("assign-word", word)


@pytest.mark.parametrize(("path", "text"), ASSIGN_NEGATIVE)
def test_secret_assignment_negative(corpus: Corpus, path: str, text: str) -> None:
    findings = corpus.findings("assign-", path, text)
    assert custom_rules(findings) == set(), findings


def test_secret_assignment_attribution(corpus: Corpus) -> None:
    assert corpus.default_findings("assign-attr") == []
    assert "admitlens-secret-assignment" in custom_rules(corpus.findings("assign-attr"))


# ------------------------------------------------------------------ secret-assignment (python)


def test_python_literal_positive(corpus: Corpus) -> None:
    findings = corpus.findings("py+")
    assert Finding("admitlens-secret-assignment-py", "settings.py", 3) in findings
    assert Finding("admitlens-secret-assignment-py", "settings.py", 5) in findings


@pytest.mark.parametrize("line", PY_NEGATIVE)
def test_python_literal_negative(corpus: Corpus, line: str) -> None:
    findings = corpus.findings("py-", line)
    assert custom_rules(findings) == set(), findings


# ------------------------------------------------------------------ ssn


@pytest.mark.parametrize(("area", "group", "serial"), SSN_POSITIVE)
def test_ssn_positive(corpus: Corpus, area: str, group: str, serial: str) -> None:
    assert Finding("admitlens-ssn", "notes.txt", 2) in corpus.findings("ssn+", area, group, serial)


@pytest.mark.parametrize(("area", "group", "serial"), SSN_INVALID)
def test_ssn_invalid_ranges_negative(corpus: Corpus, area: str, group: str, serial: str) -> None:
    assert "admitlens-ssn" not in {f.rule for f in corpus.findings("ssn-", area, group, serial)}


@pytest.mark.parametrize("text", SSN_LOOKALIKES)
def test_ssn_lookalikes_negative(corpus: Corpus, text: str) -> None:
    assert corpus.findings("ssn~", text) == []


def test_ssn_attribution(corpus: Corpus) -> None:
    assert corpus.default_findings("ssn-attr") == []


# ------------------------------------------------------------------ local-path


@pytest.mark.parametrize("kind", LOCAL_KINDS)
def test_local_path_positive(corpus: Corpus, kind: str) -> None:
    assert Finding("admitlens-local-path", "notes.txt", 2) in corpus.findings("local+", kind)


def test_local_path_attribution(corpus: Corpus) -> None:
    assert corpus.default_findings("local-attr") == []


@pytest.mark.parametrize("text", LOCAL_NEGATIVE)
def test_local_path_negative(corpus: Corpus, text: str) -> None:
    assert corpus.findings("local-", text) == []


# ------------------------------------------------------------------ vault-marker


@pytest.mark.parametrize("marker", VAULT_POSITIVE)
def test_vault_marker_positive(corpus: Corpus, marker: tuple[str, ...]) -> None:
    assert Finding("admitlens-vault-marker", "README.txt", 3) in corpus.findings("vault+", marker)


@pytest.mark.parametrize("text", VAULT_NEGATIVE)
def test_vault_marker_negative(corpus: Corpus, text: str) -> None:
    assert corpus.findings("vault-", text) == []


# ------------------------------------------------------------------ path-only rules


@pytest.mark.parametrize(("path", "rule"), PATH_POSITIVE)
def test_path_rules_positive(corpus: Corpus, path: str, rule: str) -> None:
    findings = corpus.findings("path+", path)
    assert rule in rules_for(findings, path), findings


@pytest.mark.parametrize("path", PATH_NEGATIVE)
def test_path_rules_negative(corpus: Corpus, path: str) -> None:
    findings = corpus.findings("path-", path)
    assert findings == [], findings


# ------------------------------------------------------------------ redaction


def test_redact_never_exposes_values(ws: Workspace) -> None:
    jwt = make_jwt(ws)
    dsn = make_dsn(ws, "postgresql")
    ssn = make_ssn(ws, "123", "45", "6789")
    path = user_path(ws, "mac")
    ws.write("notes.txt", "\n".join(f.reveal() for f in (jwt, dsn, ssn, path)) + "\n")
    findings, result = ws.scan()  # _read() runs assert_absent on report, stdout and stderr
    assert {f.rule for f in findings} >= {"jwt", "admitlens-dsn-password", "admitlens-ssn", "admitlens-local-path"}
    # The report must also carry only the redacted marker in secret-bearing fields.
    raw = json.loads((ws.out / "report.json").read_text(encoding="utf-8"))
    assert raw, result
    for item in raw:
        assert item["Secret"] == "REDACTED"
