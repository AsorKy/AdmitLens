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


@pytest.fixture
def default_only(tmp_path: Path) -> Path:
    path = tmp_path / "default-only.toml"
    path.write_text("[extend]\nuseDefault = true\n", encoding="utf-8")
    return path


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


# ------------------------------------------------------------------ default rules


def test_default_rules_still_active_jwt(ws: Workspace) -> None:
    jwt = make_jwt(ws)
    ws.write("notes.txt", f"value: {jwt.reveal()}\n")
    findings, _ = ws.scan()
    rules = rules_for(findings, "notes.txt")
    assert "jwt" in rules, sorted(rules)
    assert not any(r.startswith("admitlens-") for r in rules)


def test_jwt_via_stdin(ws: Workspace) -> None:
    jwt = make_jwt(ws)
    findings, _ = ws.scan_stdin(f"x = {jwt.reveal()}\n")
    assert "jwt" in {f.rule for f in findings}


# ------------------------------------------------------------------ dsn-password


@pytest.mark.parametrize(
    "scheme",
    ["postgresql", "postgresql+psycopg", "postgres+asyncpg", "redis", "rediss", "amqps", "mongodb+srv", "mysql"],
)
def test_dsn_password_positive(ws: Workspace, scheme: str) -> None:
    dsn = make_dsn(ws, scheme)
    ws.write("notes.txt", f"first line\nurl is {dsn.reveal()} ok\n")
    findings, _ = ws.scan()
    assert Finding("admitlens-dsn-password", "notes.txt", 2) in findings


def test_dsn_password_positive_via_stdin(ws: Workspace) -> None:
    dsn = make_dsn(ws, "postgresql+psycopg")
    findings, _ = ws.scan_stdin(f"url = {dsn.reveal()}\n")
    assert "admitlens-dsn-password" in {f.rule for f in findings}


@pytest.mark.parametrize(
    "line",
    [
        "postgresql://app@db.test:5432/x",
        "postgresql+psycopg://app@localhost/admitlens_test",
        "postgresql://app:${POSTGRES_PASSWORD}@db:5432/x",
        "postgresql://app:<password>@db:5432/x",
        "postgresql://app:__GENERATE__@db:5432/x",
        "redis://:__REQUIRED__@cache:6379/0",
        "redis://cache:6379/0",
        "https://example.test/a:b@c",
    ],
)
def test_dsn_password_negative(ws: Workspace, line: str) -> None:
    ws.write("notes.txt", line + "\n")
    findings, _ = ws.scan()
    assert findings == []


def test_dsn_attribution_default_rules_miss_it(ws: Workspace, default_only: Path) -> None:
    dsn = make_dsn(ws, "postgresql")
    ws.write("notes.txt", f"{dsn.reveal()}\n")
    assert ws.scan(default_only)[0] == []
    assert "admitlens-dsn-password" in custom_rules(ws.scan()[0])


# ------------------------------------------------------------------ secret-assignment (config)


def _name(*frags: str) -> str:
    return "".join(frags)


@pytest.mark.parametrize(
    ("path", "template"),
    [
        ("settings/app.yml", "  {name}: {value}\n"),
        ("settings/app.yaml", "{name}: \"{value}\"\n"),
        ("pyproject.toml", "{name} = \"{value}\"\n"),
        ("docker-compose.yml", "    environment:\n      {name}: {value}\n"),
        ("setup.cfg", "{name} = {value}\n"),
        ("conf.ini", "{name}={value}\n"),
        (".env.local", "{name}={value}\n"),
        (".env", "{name}={value}\n"),
    ],
)
def test_secret_assignment_positive(ws: Workspace, path: str, template: str) -> None:
    value = ws.word()
    name = _name("DB_", "PASS", "WORD")
    text = template.format(name=name, value=value.reveal())
    ws.write(path, text)
    line = 1 + text.splitlines().index(next(ln for ln in text.splitlines() if name in ln))
    findings, _ = ws.scan()
    assert Finding("admitlens-secret-assignment", path, line) in findings


@pytest.mark.parametrize("word", ["TOK" + "EN", "SEC" + "RET", "API_" + "KEY", "DS" + "N", "PRIVATE_" + "KEY"])
def test_secret_assignment_name_variants(ws: Workspace, word: str) -> None:
    value = ws.word()
    ws.write("a.yml", f"svc_{word}: {value.reveal()}\n")
    findings, _ = ws.scan()
    assert Finding("admitlens-secret-assignment", "a.yml", 1) in findings


@pytest.mark.parametrize(
    ("path", "text"),
    [
        (".env.example", "X_PASSWORD=__GENERATE__\nX_TOKEN=\nY_SECRET=__REQUIRED__\nZ_DSN=${Z_DSN}\n"),
        (".env.example", "A_TOKEN=<paste-a-token-here>\nPOSTGRES_USER=admitlens\n"),
        ("ci.yml", "env:\n  X_TOKEN: ${{ secrets.X_TOKEN }}\n  Y_PASSWORD: $$Y_PASSWORD\n"),
        ("docker-compose.yml", "    POSTGRES_PASSWORD_FILE: /run/secrets/db_password\n    secrets:\n      - db_password\n"),
        ("app.yml", "ACCESS_TOKEN_TTL: 900\nsecret_rotation_days: 30\nuse_secret: true\n"),
        ("app.yml", "secrets:\n  db_password:\n    file: ./x\n"),
        ("data.json", "{\"user_password\": \"abcdefgh\"}\n"),
        ("notes.md", "user_password = abcdefgh\n"),
    ],
)
def test_secret_assignment_negative(ws: Workspace, path: str, text: str) -> None:
    ws.write(path, text)
    findings, _ = ws.scan()
    assert custom_rules(findings) == set(), findings


def test_secret_assignment_attribution(ws: Workspace, default_only: Path) -> None:
    value = ws.word()
    ws.write("a.yml", f"{_name('DB_', 'PASS', 'WORD')}: {value.reveal()}\n")
    assert ws.scan(default_only)[0] == []
    assert "admitlens-secret-assignment" in custom_rules(ws.scan()[0])


# ------------------------------------------------------------------ secret-assignment (python)


def test_python_literal_positive(ws: Workspace) -> None:
    v1, v2 = ws.word(), ws.word()
    n1, n2 = _name("DB_", "PASS", "WORD"), _name("api_", "TOK", "EN")
    ws.write(
        "settings.py",
        f"import os\n\n{n1} = \"{v1.reveal()}\"\nclass C:\n    {n2}: str = '{v2.reveal()}'\n",
    )
    findings, _ = ws.scan()
    assert Finding("admitlens-secret-assignment-py", "settings.py", 3) in findings
    assert Finding("admitlens-secret-assignment-py", "settings.py", 5) in findings


@pytest.mark.parametrize(
    "line",
    [
        'X_PASSWORD = os.environ["X_PASSWORD"]',
        'db_password = os.environ.get("DB_PASSWORD")',
        "db_password: str = Field(...)",
        'db_password: SecretStr = Field(default="")',
        "token = settings.api_token",
        'if x_token == "abc":',
        "password = None",
        'DB_DSN = f"{scheme}://host/db"',
        'password = ""',
    ],
)
def test_python_literal_negative(ws: Workspace, line: str) -> None:
    ws.write("config.py", f"import os\n{line}\n")
    findings, _ = ws.scan()
    assert custom_rules(findings) == set(), findings


# ------------------------------------------------------------------ ssn


@pytest.mark.parametrize(
    ("area", "group", "serial"),
    [("123", "45", "6789"), ("001", "01", "0001"), ("665", "99", "9999"), ("667", "10", "1000"), ("899", "50", "5050")],
)
def test_ssn_positive(ws: Workspace, area: str, group: str, serial: str) -> None:
    ssn = make_ssn(ws, area, group, serial)
    ws.write("notes.txt", f"id\nvalue {ssn.reveal()} end\n")
    findings, _ = ws.scan()
    assert Finding("admitlens-ssn", "notes.txt", 2) in findings


@pytest.mark.parametrize(
    ("area", "group", "serial"),
    [("000", "12", "3456"), ("666", "12", "3456"), ("900", "12", "3456"), ("999", "12", "3456"), ("123", "00", "6789"), ("123", "45", "0000")],
)
def test_ssn_invalid_ranges_negative(ws: Workspace, area: str, group: str, serial: str) -> None:
    ssn = make_ssn(ws, area, group, serial)
    ws.write("notes.txt", f"value {ssn.reveal()} end\n")
    findings, _ = ws.scan()
    assert "admitlens-ssn" not in {f.rule for f in findings}


@pytest.mark.parametrize(
    "text",
    ["2026-10-17T13:24:05.120Z", "2026-10-17", "123-456-7890", "call 555-123-4567 now", "1234-56-78901"],
)
def test_ssn_lookalikes_negative(ws: Workspace, text: str) -> None:
    ws.write("notes.txt", text + "\n")
    findings, _ = ws.scan()
    assert findings == []


def test_ssn_attribution(ws: Workspace, default_only: Path) -> None:
    ssn = make_ssn(ws, "123", "45", "6789")
    ws.write("notes.txt", f"{ssn.reveal()}\n")
    assert ws.scan(default_only)[0] == []


# ------------------------------------------------------------------ local-path


@pytest.mark.parametrize("kind", ["mac", "linux", "win", "slug", "claude", "folders"])
def test_local_path_positive(ws: Workspace, kind: str) -> None:
    path = user_path(ws, kind)
    ws.write("notes.txt", f"x\nsee {path.reveal()}\n")
    findings, _ = ws.scan()
    assert Finding("admitlens-local-path", "notes.txt", 2) in findings


def test_local_path_attribution(ws: Workspace, default_only: Path) -> None:
    path = user_path(ws, "mac")
    ws.write("notes.txt", f"{path.reveal()}\n")
    assert ws.scan(default_only)[0] == []


@pytest.mark.parametrize(
    "text",
    [
        "/home/runner/work/admitlens/admitlens",
        "./src/users/models.py",
        "/usr/local/bin/python",
        "/opt/app/data",
        "users-table",
        "/var/run/postgresql",
    ],
)
def test_local_path_negative(ws: Workspace, text: str) -> None:
    ws.write("notes.txt", text + "\n")
    findings, _ = ws.scan()
    assert findings == []


# ------------------------------------------------------------------ vault-marker


@pytest.mark.parametrize(
    "marker",
    [("Obsid", "ian Va", "ult/Notes"), ("[", "[proj", "ects/AdmitLens]]")],
)
def test_vault_marker_positive(ws: Workspace, marker: tuple[str, ...]) -> None:
    fake = ws.fake("vault", "".join(marker))
    ws.write("README.txt", f"a\nb\nsee {fake.reveal()}\n")
    findings, _ = ws.scan()
    assert Finding("admitlens-vault-marker", "README.txt", 3) in findings


@pytest.mark.parametrize("text", ["an obsidian vault is a folder", "projects/ is a dir", "[link](projects/x)", "[[wikilink]]"])
def test_vault_marker_negative(ws: Workspace, text: str) -> None:
    ws.write("README.txt", text + "\n")
    findings, _ = ws.scan()
    assert findings == []


# ------------------------------------------------------------------ path-only rules


@pytest.mark.parametrize(
    ("path", "rule"),
    [
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
    ],
)
def test_path_rules_positive(ws: Workspace, path: str, rule: str) -> None:
    ws.write(path, "N=1\n")
    findings, _ = ws.scan()
    assert rule in rules_for(findings, path), findings


@pytest.mark.parametrize(
    "path",
    [
        ".env.example",
        "level-1/backend/.env.example",
        "datagen/readme.txt",
        "src/database.py",
        "docs/keys.md",
        "env.txt",
        "notes/idea_rsa.md",
        "dbconfig.txt",
    ],
)
def test_path_rules_negative(ws: Workspace, path: str) -> None:
    ws.write(path, "N=1\n")
    findings, _ = ws.scan()
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
