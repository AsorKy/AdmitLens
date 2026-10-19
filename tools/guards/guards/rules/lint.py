"""Lint for the files that wire the project together.

* Makefiles: no ``include .env``, no Make expansion of secret-looking names,
  no GNU Make >= 3.82 constructs (the operator's Mac ships 3.81).
* ``.gitignore``: the only negation allowed is ``!.env.example``.
* Workflows: ``run:`` is ``make`` only, ``uses:`` pinned to a 40-hex SHA,
  top-level ``permissions:``, no ``pull_request_target``/``workflow_run``,
  no ``${{ github.event.`` in ``run:``, ``guards.yml`` has no paths filter,
  no floating ``*-latest`` runner label (``wf-runner-latest``) and no
  ``runs-on:`` expression other than a bare ``${{ matrix.<key> }}``
  (``wf-runner-dynamic``): the image must not change under the pinned
  toolchain (Make, git), and a computed label cannot be checked statically.
* ``.gitattributes``: ``-diff``/``binary``/``diff=`` only on a closed list of
  binary extensions, and no ``[attr]`` macros (they hide diffs from reviewers
  and from any diff-based scanner).
* ``.gitleaks.toml`` (root): see :func:`check_gitleaks_config`; in short it
  may add detection but never remove it without a matching change here.

YAML errors become a finding with a line number only: PyYAML's error text
quotes the offending source line, so it is never echoed.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import PurePosixPath

import yaml

from guards.findings import NONE_DISABLED, Disabled, Finding
from guards.rules.content import (
    DSN_PASSWORD_REGEX,
    GITLEAKS_PLACEHOLDERS,
    SECRET_ASSIGNMENT_ALLOW,
    SECRET_ASSIGNMENT_PATH,
    SECRET_ASSIGNMENT_PY_PATH,
    SECRET_ASSIGNMENT_PY_REGEX,
    SECRET_ASSIGNMENT_REGEX,
    SECRET_RULES,
)

# --------------------------------------------------------------------------- Makefiles

MAKEFILE_NAMES = frozenset({"Makefile", "makefile", "GNUmakefile"})
INCLUDE_ENV_RE = re.compile(r"^\s*-?s?include\s+(.*\s)?\S*\.env(\s|$)")
# URL only as a whole ``_``-separated token: DATABASE_URL and URLS yes, CURL no.
SECRET_NAME_RE = re.compile(r"(?i)(PASSWORD|SECRET|TOKEN|KEY|DSN|(?:^|_)URLS?(?:_|$))")
GNU_WORD_RE = re.compile(r"\.(ONESHELL|SHELLFLAGS|RECIPEPREFIX)\b")
GNU_FUNC_RE = re.compile(r"(?<!\$)\$[({](file|let)\s")
BANG_ASSIGN_RE = re.compile(r"^[^:=!]*[^\s:=!]\s*!=")
COLON3_RE = re.compile(r":::?=")
GROUPED_RE = re.compile(r"&:")


def is_makefile(path: str) -> bool:
    name = PurePosixPath(path).name
    return name in MAKEFILE_NAMES or name.endswith(".mk")


def _strip_comment(line: str) -> str:
    out = []
    escaped = False
    for ch in line:
        if ch == "#" and not escaped:
            break
        escaped = ch == "\\" and not escaped
        out.append(ch)
    return "".join(out)


def _make_expansion_names(line: str) -> list[str]:
    """Names (first word) of every ``$(…)``/``${…}`` Make expansion; ``$$`` is shell."""
    names = []
    i = 0
    while i < len(line) - 1:
        if line[i] == "$":
            nxt = line[i + 1]
            if nxt == "$":
                i += 2
                continue
            if nxt in "({":
                m = re.match(r"[^\s():{}$,]+", line[i + 2 :])
                if m:
                    names.append(m.group(0))
            i += 2
            continue
        i += 1
    return names


def check_makefile(path: str, text: str, disabled: Disabled = NONE_DISABLED) -> list[Finding]:
    found: list[Finding] = []

    def add(rule: str, line: int) -> None:
        if rule not in disabled:
            found.append(Finding(rule, path, line))

    for no, raw in enumerate(text.splitlines(), start=1):
        recipe = raw.startswith("\t")
        line = raw if recipe else _strip_comment(raw)
        if not recipe and INCLUDE_ENV_RE.search(line):
            add("make-include-env", no)
        if any(SECRET_NAME_RE.search(n) for n in _make_expansion_names(line)):
            add("make-secret-var", no)
        gnu = bool(GNU_FUNC_RE.search(line))
        if not recipe:
            gnu = gnu or bool(
                GNU_WORD_RE.search(line)
                or BANG_ASSIGN_RE.search(line)
                or COLON3_RE.search(line)
                or GROUPED_RE.search(line)
            )
        if gnu:
            add("make-gnu-construct", no)
    return found


# --------------------------------------------------------------------------- .gitignore

ALLOWED_NEGATION = "!.env.example"


def check_gitignore(path: str, text: str, disabled: Disabled = NONE_DISABLED) -> list[Finding]:
    if "gitignore-negation" in disabled:
        return []
    return [
        Finding("gitignore-negation", path, no)
        for no, raw in enumerate(text.splitlines(), start=1)
        if raw.strip().startswith("!") and raw.strip() != ALLOWED_NEGATION
    ]


# --------------------------------------------------------------------------- workflows

PINNED_RE = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
MAKE_SEGMENT_RE = re.compile(r"^make(\s+[A-Za-z0-9_.,/=:@+%-]+)*$")
FORBIDDEN_TRIGGERS = frozenset({"pull_request_target", "workflow_run"})
EVENT_RE = re.compile(r"\$\{\{\s*github\.event\.")
GUARDS_WORKFLOWS = frozenset({"guards.yml", "guards.yaml"})
LATEST_RUNNER_RE = re.compile(r"(?i)-latest\b")
EXPRESSION_RE = re.compile(r"\$\{\{")
MATRIX_REF_RE = re.compile(r"^\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}$")


def is_workflow(path: str) -> bool:
    p = PurePosixPath(path)
    return p.parent == PurePosixPath(".github/workflows") and p.suffix in (".yml", ".yaml")


def run_is_make_only(script: str) -> bool:
    joined = re.sub(r"\\\n", " ", script)
    lines = [ln.strip() for ln in joined.splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    if not lines:
        return False
    for line in lines:
        for segment in line.split("&&"):
            if not MAKE_SEGMENT_RE.match(segment.strip()):
                return False
    return True


def _scalar(node: yaml.Node) -> str | None:
    return node.value if isinstance(node, yaml.ScalarNode) else None


def _line(node: yaml.Node) -> int:
    return node.start_mark.line + 1


def _trigger_nodes(on_value: yaml.Node) -> list[tuple[str, yaml.Node, yaml.Node | None]]:
    """(name, key-or-scalar node, value node) for each trigger."""
    if isinstance(on_value, yaml.ScalarNode):
        return [(on_value.value, on_value, None)]
    if isinstance(on_value, yaml.SequenceNode):
        return [(n.value, n, None) for n in on_value.value if isinstance(n, yaml.ScalarNode)]
    if isinstance(on_value, yaml.MappingNode):
        return [(k.value, k, v) for k, v in on_value.value if isinstance(k, yaml.ScalarNode)]
    return []


def _scalars(node: yaml.Node) -> list[yaml.ScalarNode]:
    """Every scalar under ``node``, keys excluded."""
    if isinstance(node, yaml.ScalarNode):
        return [node]
    if isinstance(node, yaml.SequenceNode):
        return [s for item in node.value for s in _scalars(item)]
    if isinstance(node, yaml.MappingNode):
        return [s for _k, v in node.value for s in _scalars(v)]
    return []


def _mapping_get(node: yaml.Node | None, name: str) -> yaml.Node | None:
    if isinstance(node, yaml.MappingNode):
        for k, v in node.value:
            if _scalar(k) == name:
                return v
    return None


def _runner_labels(runs_on: yaml.Node) -> list[yaml.ScalarNode]:
    """Label scalars of ``runs-on``: a string, a list, or the ``labels`` of a mapping (``group`` is a name, not a label)."""
    if isinstance(runs_on, yaml.MappingNode):
        labels = _mapping_get(runs_on, "labels")
        return _scalars(labels) if labels is not None else []
    return _scalars(runs_on)


def _check_runners(jobs: yaml.Node, add) -> None:  # noqa: ANN001
    """``runs-on`` of each job, plus the matrix keys it references as ``${{ matrix.<key> }}``."""
    if not isinstance(jobs, yaml.MappingNode):
        return
    for _name, job in jobs.value:
        runs_on = _mapping_get(job, "runs-on")
        if runs_on is None:
            continue
        keys: set[str] = set()
        for s in _runner_labels(runs_on):
            ref = MATRIX_REF_RE.match(s.value.strip())
            if ref:
                keys.add(ref.group(1))
            elif EXPRESSION_RE.search(s.value):
                add("wf-runner-dynamic", _line(s))
            elif LATEST_RUNNER_RE.search(s.value):
                add("wf-runner-latest", _line(s))
        matrix = _mapping_get(_mapping_get(job, "strategy"), "matrix")
        if not keys or matrix is None:
            continue
        if not isinstance(matrix, yaml.MappingNode):  # e.g. ${{ fromJSON(...) }}: cannot be checked
            add("wf-runner-dynamic", _line(matrix))
            continue
        values: list[yaml.ScalarNode] = []
        for key in sorted(keys):
            node = _mapping_get(matrix, key)
            if node is not None:
                values += _scalars(node)
        include = _mapping_get(matrix, "include")
        if isinstance(include, yaml.SequenceNode):
            for entry in include.value:
                for key in sorted(keys):
                    node = _mapping_get(entry, key)
                    if node is not None:
                        values += _scalars(node)
        elif include is not None:
            add("wf-runner-dynamic", _line(include))
        for s in values:
            if EXPRESSION_RE.search(s.value):
                add("wf-runner-dynamic", _line(s))
            elif LATEST_RUNNER_RE.search(s.value):
                add("wf-runner-latest", _line(s))


def _walk(node: yaml.Node, add) -> None:  # noqa: ANN001
    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            name = _scalar(key)
            if name == "run" and isinstance(value, yaml.ScalarNode):
                if not run_is_make_only(value.value):
                    add("wf-run-not-make", _line(key))
                if EVENT_RE.search(value.value):
                    add("wf-event-in-run", _line(key))
            elif name == "uses" and isinstance(value, yaml.ScalarNode):
                if not PINNED_RE.match(value.value.strip()):
                    add("wf-uses-unpinned", _line(key))
            _walk(value, add)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            _walk(item, add)


def check_workflow(path: str, text: str, disabled: Disabled = NONE_DISABLED) -> list[Finding]:
    found: list[Finding] = []

    def add(rule: str, line: int) -> None:
        if rule not in disabled:
            found.append(Finding(rule, path, line))

    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
        add("wf-yaml-invalid", (mark.line + 1) if mark is not None else 1)
        return found
    if not isinstance(root, yaml.MappingNode):
        add("wf-yaml-invalid", 1)
        return found

    top = {k.value: (k, v) for k, v in root.value if isinstance(k, yaml.ScalarNode)}
    if "permissions" not in top:
        add("wf-no-permissions", 1)
    if "on" in top:
        is_guards = PurePosixPath(path).name in GUARDS_WORKFLOWS
        for name, node, value in _trigger_nodes(top["on"][1]):
            if name in FORBIDDEN_TRIGGERS:
                add("wf-forbidden-trigger", _line(node))
            if is_guards and isinstance(value, yaml.MappingNode):
                for k, _v in value.value:
                    if _scalar(k) in ("paths", "paths-ignore"):
                        add("wf-guards-paths", _line(k))
    if "jobs" in top:
        _walk(top["jobs"][1], add)
        _check_runners(top["jobs"][1], add)
    return found


# --------------------------------------------------------------------------- .gitattributes

BINARY_EXTENSIONS = frozenset({
    "png", "jpg", "jpeg", "gif", "ico", "webp", "pdf", "woff", "woff2", "ttf", "otf", "eot",
    "zip", "gz", "tgz", "bz2", "xz", "7z", "jar", "whl", "so", "dylib", "dll", "exe",
})
BINARY_PATTERN_RE = re.compile(r"^(?:[^\s\"*?\[\]]*/)?(?:\*\*/)?\*\.([A-Za-z0-9]+)$")


def _diff_attr(attr: str) -> bool:
    """Attributes that change or suppress the textual diff."""
    return attr in ("-diff", "binary") or attr.startswith("diff=")


def check_gitattributes(path: str, text: str, disabled: Disabled = NONE_DISABLED) -> list[Finding]:
    found: list[Finding] = []
    for no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pattern, *attrs = line.split()
        if pattern.startswith("[attr]"):
            if "gitattributes-macro" not in disabled:
                found.append(Finding("gitattributes-macro", path, no))
            continue
        if not any(_diff_attr(a) for a in attrs):
            continue
        m = BINARY_PATTERN_RE.match(pattern)
        if not (m and m.group(1).lower() in BINARY_EXTENSIONS) and "gitattributes-diff" not in disabled:
            found.append(Finding("gitattributes-diff", path, no))
    return found


# --------------------------------------------------------------------------- .gitleaks.toml

GITLEAKS_CONFIG = ".gitleaks.toml"
CUSTOM_RULE_PREFIX = "admitlens-"
_SECRET_WORDS = ["password", "passwd", "secret", "token", "apikey", "api_key", "private_key", "dsn"]
# Full body of every existing custom rule, compared without ``description`` (on
# the rule and on its allowlists). New ``admitlens-*`` rules may be added, but a
# frozen one can only change together with this snapshot (two-file review), so
# no commit can weaken a rule by adding ``keywords``, a ``path``, an
# ``entropy`` or by editing its regex. A test asserts it equals the real file.
FROZEN_RULES: dict[str, dict[str, object]] = {
    "admitlens-dsn-password": {
        "id": "admitlens-dsn-password",
        "regex": DSN_PASSWORD_REGEX,
        "secretGroup": 1,
        "keywords": ["postgres", "redis", "amqp", "mongodb", "mysql"],
    },
    "admitlens-secret-assignment": {
        "id": "admitlens-secret-assignment",
        "path": SECRET_ASSIGNMENT_PATH,
        "regex": SECRET_ASSIGNMENT_REGEX,
        "secretGroup": 1,
        "keywords": _SECRET_WORDS,
        "allowlists": [{"regexTarget": "secret", "regexes": list(SECRET_ASSIGNMENT_ALLOW)}],
    },
    "admitlens-secret-assignment-py": {
        "id": "admitlens-secret-assignment-py",
        "path": SECRET_ASSIGNMENT_PY_PATH,
        "regex": SECRET_ASSIGNMENT_PY_REGEX,
        "secretGroup": 1,
        "keywords": _SECRET_WORDS,
    },
    "admitlens-ssn": {
        "id": "admitlens-ssn",
        "regex": (
            r"\b(?:00[1-9]|0[1-9]\d|[1-5]\d\d|6[0-5]\d|66[0-5]|66[7-9]|6[7-9]\d|[78]\d\d)"
            r"-(?:0[1-9]|[1-9]\d)-(?:000[1-9]|00[1-9]\d|0[1-9]\d\d|[1-9]\d{3})\b"
        ),
    },
    "admitlens-local-path": {
        "id": "admitlens-local-path",
        "regex": (
            r"/Users/([A-Za-z0-9._-]+)/|/home/([A-Za-z0-9._-]+)/|[A-Za-z]:\\Users\\([^\\\s]+)\\"
            r"|-Users-([A-Za-z0-9._]+)-|/private/tmp/claude[-]|/private/var/folder[s]/"
        ),
        "keywords": ["/users/", "/home/", "\\users\\", "-users-", "/private/"],
        "allowlists": [{"regexTarget": "match", "regexes": [r"^/home/runner/$"]}],
    },
    "admitlens-vault-marker": {
        "id": "admitlens-vault-marker",
        "regex": r"(?:Obsidian[ ]Vault/|\[\[projects/)",
        "keywords": ["obsidian", "projects/"],
    },
    "admitlens-path-env": {
        "id": "admitlens-path-env",
        "path": (
            r"(?:^|/)\.env(?:\.(?:[^e/][^/]*|e(?:[^x/][^/]*)?|ex(?:[^a/][^/]*)?|exa(?:[^m/][^/]*)?"
            r"|exam(?:[^p/][^/]*)?|examp(?:[^l/][^/]*)?|exampl(?:[^e/][^/]*)?|example[^/]+))?$"
        ),
    },
    "admitlens-path-data": {"id": "admitlens-path-data", "path": r"(?:^|/)data/"},
    "admitlens-path-key": {
        "id": "admitlens-path-key",
        "path": r"(?i)(?:^|/)(?:[^/]+\.(?:pem|key|p12|pfx)|id_rsa[^/]*|id_ed25519[^/]*)$",
    },
    "admitlens-path-db": {"id": "admitlens-path-db", "path": r"(?i)\.(?:db(?:-[^/]*)?|sqlite[^/]*)$"},
}
# Kept for callers and tests: the frozen ids are the required ones.
REQUIRED_GITLEAKS_RULES = frozenset(FROZEN_RULES)
# Every allowlist the config may carry, by scope ("" = global), compared without
# ``description``. Loosening one means changing this file too (two-file review).
FROZEN_ALLOWLISTS: dict[str, tuple[dict[str, object], ...]] = {
    "": ({"regexTarget": "secret", "regexes": list(GITLEAKS_PLACEHOLDERS)},),
    "admitlens-secret-assignment": ({"regexTarget": "secret", "regexes": list(SECRET_ASSIGNMENT_ALLOW)},),
    "admitlens-local-path": ({"regexTarget": "match", "regexes": [r"^/home/runner/$"]},),
}
# Rules mirrored by guards.rules.content: regex and path must stay identical.
MIRRORED_RULES = {gl_rule: (path, regex) for _rule, gl_rule, path, regex, _allow in SECRET_RULES}
ALLOWED_TOP_KEYS = frozenset({"title", "extend", "allowlist", "allowlists", "rules"})
ALLOWED_RULE_KEYS = frozenset({
    "id", "description", "regex", "secretGroup", "keywords", "path", "entropy", "allowlist", "allowlists", "tags",
})


def _allowlists(table: dict[str, object]) -> list[object]:
    out: list[object] = []
    single = table.get("allowlist")
    if single is not None:
        out.append(single)
    many = table.get("allowlists")
    if many is not None:
        out.extend(many if isinstance(many, list) else [many])
    return out


def rule_body(rule: dict[str, object]) -> dict[str, object]:
    """A rule without ``description``; ``allowlist``/``allowlists`` merged into ``allowlists`` (no descriptions)."""
    body = {k: v for k, v in rule.items() if k not in ("description", "allowlist", "allowlists")}
    allows = [
        {k: v for k, v in a.items() if k != "description"} if isinstance(a, dict) else a for a in _allowlists(rule)
    ]
    if allows:
        body["allowlists"] = allows
    return body


def _allowlist_ok(scope: str, allow: object) -> bool:
    if not isinstance(allow, dict):
        return False
    body = {k: v for k, v in allow.items() if k != "description"}
    return any(body == frozen for frozen in FROZEN_ALLOWLISTS.get(scope, ()))


def check_gitleaks_config(path: str, text: str, disabled: Disabled = NONE_DISABLED) -> list[Finding]:
    """Structural checks against weakening; findings use line 0 (tomllib gives no positions).

    * ``gitleaks-config-invalid``: not TOML.
    * ``gitleaks-config-extend``: ``[extend]`` other than exactly ``useDefault = true``.
    * ``gitleaks-config-key``: an unknown top-level or rule key (e.g. ``skipReport``).
    * ``gitleaks-config-allowlist``: an allowlist not in :data:`FROZEN_ALLOWLISTS`
      (so no ``paths``, ``commits``, ``stopwords`` or new ``regexTarget``).
    * ``gitleaks-config-rule-id``: a rule id outside ``admitlens-*`` (it would
      replace a default rule) or a duplicate id.
    * ``gitleaks-config-rule-missing``: a :data:`REQUIRED_GITLEAKS_RULES` id is absent.
    * ``gitleaks-config-rule-changed``: a frozen rule's body differs from
      :data:`FROZEN_RULES` (any key but ``description``), a mirrored rule's
      regex or path differs from :mod:`guards.rules.content`, or a custom rule
      has neither regex nor path.
    """
    rules: set[str] = set()

    def add(rule: str) -> None:
        if rule not in disabled:
            rules.add(rule)

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return [] if "gitleaks-config-invalid" in disabled else [Finding("gitleaks-config-invalid", path, 0)]

    if data.get("extend") != {"useDefault": True}:
        add("gitleaks-config-extend")
    if set(data) - ALLOWED_TOP_KEYS:
        add("gitleaks-config-key")
    for allow in _allowlists(data):
        if not _allowlist_ok("", allow):
            add("gitleaks-config-allowlist")

    ids: list[str] = []
    raw_rules = data.get("rules", [])
    for rule in raw_rules if isinstance(raw_rules, list) else [raw_rules]:
        if not isinstance(rule, dict):
            add("gitleaks-config-rule-changed")
            continue
        rid = str(rule.get("id", ""))
        ids.append(rid)
        if not rid.startswith(CUSTOM_RULE_PREFIX):
            add("gitleaks-config-rule-id")
        if set(rule) - ALLOWED_RULE_KEYS:
            add("gitleaks-config-key")
        for allow in _allowlists(rule):
            if not _allowlist_ok(rid, allow):
                add("gitleaks-config-allowlist")
        if not (rule.get("regex") or rule.get("path")):
            add("gitleaks-config-rule-changed")
        if rid in MIRRORED_RULES and (rule.get("path"), rule.get("regex")) != MIRRORED_RULES[rid]:
            add("gitleaks-config-rule-changed")
        if rid in FROZEN_RULES and rule_body(rule) != FROZEN_RULES[rid]:
            add("gitleaks-config-rule-changed")
    if len(ids) != len(set(ids)):
        add("gitleaks-config-rule-id")
    if not REQUIRED_GITLEAKS_RULES <= set(ids):
        add("gitleaks-config-rule-missing")
    return [Finding(r, path, 0) for r in sorted(rules)]


# --------------------------------------------------------------------------- dispatch


def is_gitattributes(path: str) -> bool:
    return PurePosixPath(path).name.lower() == ".gitattributes"


def is_lint_target(path: str) -> bool:
    return (
        is_makefile(path)
        or PurePosixPath(path).name == ".gitignore"
        or is_workflow(path)
        or is_gitattributes(path)
        or path == GITLEAKS_CONFIG
    )


def is_scan_lint_target(path: str) -> bool:
    """Files linted inside every scan too (staged, range, history, tree): they steer the scanners."""
    return is_gitattributes(path) or path == GITLEAKS_CONFIG


def lint_file(path: str, text: str, disabled: Disabled = NONE_DISABLED) -> list[Finding]:
    if is_makefile(path):
        return check_makefile(path, text, disabled)
    if PurePosixPath(path).name == ".gitignore":
        return check_gitignore(path, text, disabled)
    if is_workflow(path):
        return check_workflow(path, text, disabled)
    if is_gitattributes(path):
        return check_gitattributes(path, text, disabled)
    if path == GITLEAKS_CONFIG:
        return check_gitleaks_config(path, text, disabled)
    return []
