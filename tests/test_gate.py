"""Tests for tools/gate.py: the promotion gate's rules, on throwaway git repos. No network."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import gate  # noqa: E402
import qqtc  # noqa: E402

SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64


def sh(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, files: dict[str, str | None], msg: str = "change") -> str:
    for path, text in files.items():
        p = repo / path
        if text is None:
            sh(repo, "rm", "-q", path)
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        sh(repo, "add", path)
    sh(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg)
    return sh(repo, "rev-parse", "HEAD")


def pin(version: str = "1.2.3", digest: str = DIGEST) -> dict:
    return {"name": "demo", "version": version, "revision": 1, "platform": "linux-x86_64",
            "ref": f"oci://ghcr.io/quirq-ai/toolchains/demo@{digest}", "layer_sha256": "c" * 64,
            "built_from": SHA, "build_run": "https://github.com/quirq-ai/toolchains/actions/runs/1"}


def spec(version: str = "1.2.3") -> str:
    return textwrap.dedent(f"""\
        [toolchain]
        name = "demo"
        version = "{version}"
        revision = 1
        platform = "linux-x86_64"

        [[source]]
        url = "https://example.org/demo-{version}.tar.gz"
        sha256 = "{'d' * 64}"

        [build]
        script = "build.sh"

        [smoke]
        commands = ["demo --version"]
        """)


GATE_YML = (ROOT / ".github/workflows/promotion-gate.yml").read_text()
CI_YML = (ROOT / ".github/workflows/ci.yml").read_text()


@pytest.fixture
def repo(tmp_path: Path):
    """A main branch with tools, one spec, a gate workflow and one promoted pin."""
    r = tmp_path / "repo"
    r.mkdir()
    sh(r, "init", "-q", "-b", "main")
    files = {
        "toolchains.toml": (ROOT / "toolchains.toml").read_text(),
        "toolchains/demo/toolchain.toml": spec(),
        "toolchains/demo/build.sh": "#!/bin/sh\n",
        "tools/qqtc.py": (ROOT / "tools/qqtc.py").read_text(),
        "tools/gate.py": (ROOT / "tools/gate.py").read_text(),
        ".github/workflows/promotion-gate.yml": GATE_YML,
        ".github/workflows/ci.yml": CI_YML,
        "promoted.toml": qqtc.render_promoted({"demo": pin()}),
        "README.md": "readme\n",
    }
    commit(r, files, "main")
    return r


def branch(repo: Path, name: str = "pr", start: str = "main") -> None:
    sh(repo, "checkout", "-q", "-b", name, start)


def run_prepare(repo: Path, head: str, event: str = "pull_request", base: str | None = None):
    main = sh(repo, "rev-parse", "main")
    gate_dir = repo.parent / f"gate-{head[:8]}-{event}"
    if not gate_dir.exists():
        sh(repo, "worktree", "add", "-q", "--detach", str(gate_dir), base or main)
    return gate.prepare(repo, event, base or main, head, base or main, gate_dir, repo.parent / "out")


def test_promotion_only_pr_passes_and_lists_the_moved_pin(repo):
    branch(repo)
    head = commit(repo, {"promoted.toml": qqtc.render_promoted({"demo": pin(digest="sha256:" + "e" * 64)}),
                         "README.md": "now promoted\n"})
    plan = run_prepare(repo, head)
    assert [e["ref"].rsplit("@", 1)[1] for e in plan["changed"]] == ["sha256:" + "e" * 64]


@pytest.mark.parametrize("extra", ["tools/qqtc.py", ".github/workflows/ci.yml", "toolchains/demo/build.sh"])
def test_promotion_with_any_other_file_is_refused(repo, extra):
    branch(repo)
    head = commit(repo, {"promoted.toml": qqtc.render_promoted({"demo": pin(digest="sha256:" + "e" * 64)}),
                         extra: "# changed\n"})
    with pytest.raises(gate.GateError, match="may change only promoted.toml"):
        run_prepare(repo, head)


def test_rename_onto_readme_still_names_the_source(repo):
    branch(repo)
    sh(repo, "mv", "-f", "tools/qqtc.py", "README.md")
    head = commit(repo, {"promoted.toml": qqtc.render_promoted({"demo": pin(digest="sha256:" + "e" * 64)})})
    with pytest.raises(gate.GateError, match="tools/qqtc.py"):
        run_prepare(repo, head)


def test_stale_tools_only_pr_is_not_mistaken_for_a_promotion(repo):
    branch(repo)
    head = commit(repo, {"tools/notes.txt": "x\n"})
    sh(repo, "checkout", "-q", "main")
    commit(repo, {"promoted.toml": qqtc.render_promoted({"demo": pin(digest="sha256:" + "f" * 64)})})
    assert run_prepare(repo, head)["changed"] == []


def test_judges_the_merged_promoted_toml(repo):
    branch(repo)
    head = commit(repo, {"promoted.toml": qqtc.render_promoted({"demo": pin(digest="sha256:" + "e" * 64)})})
    sh(repo, "checkout", "-q", "main")
    commit(repo, {"promoted.toml": qqtc.render_promoted({"demo": pin(digest="sha256:" + "f" * 64)})})
    with pytest.raises(gate.GateError, match="conflicts with main"):
        run_prepare(repo, head)


def test_non_canonical_promoted_toml_is_refused(repo):
    branch(repo)
    text = qqtc.render_promoted({"demo": pin(digest="sha256:" + "e" * 64)}) + "# hand edit\n"
    head = commit(repo, {"promoted.toml": text})
    with pytest.raises(qqtc.SpecError, match="canonical"):
        run_prepare(repo, head)


def test_pin_must_match_the_spec(repo):
    branch(repo)
    head = commit(repo, {"promoted.toml": qqtc.render_promoted({"demo": pin(version="9.9.9")})})
    with pytest.raises(qqtc.SpecError, match="does not match its spec"):
        run_prepare(repo, head)


@pytest.mark.parametrize("yml", [
    # another job reporting the check name
    "name: x\non: pull_request\npermissions: {}\njobs:\n  promotion-gate:\n    runs-on: ubuntu-24.04\n    steps: [{run: 'true'}]\n",
    "name: x\non: pull_request\npermissions: {}\njobs:\n  fake:\n    name: Promotion-Gate\n    runs-on: ubuntu-24.04\n    steps: [{run: 'true'}]\n",
    "name: x\non: pull_request\npermissions: {}\njobs:\n  fake:\n    name: \"${{ 'promotion-gate' }}\"\n    runs-on: ubuntu-24.04\n    steps: [{run: 'true'}]\n",
    # a token that could post the check or a status itself
    "name: x\non: pull_request\npermissions: {checks: write}\njobs:\n  a:\n    runs-on: ubuntu-24.04\n    steps: [{run: 'true'}]\n",
    "name: x\non: pull_request\npermissions: {}\njobs:\n  a:\n    permissions: write-all\n    runs-on: ubuntu-24.04\n    steps: [{run: 'true'}]\n",
    "name: x\non: pull_request\npermissions: {}\njobs:\n  a:\n    permissions: {statuses: write}\n    runs-on: ubuntu-24.04\n    steps: [{run: 'true'}]\n",
    # no top-level permissions: the repo default applies
    "name: x\non: pull_request\njobs:\n  a:\n    runs-on: ubuntu-24.04\n    steps: [{run: 'true'}]\n",
    "not: [valid\n",
    # a merge key, which the strict loader refuses
    "name: x\non: pull_request\npermissions: {}\nbase: &b {runs-on: ubuntu-24.04}\njobs:\n  a:\n    <<: *b\n    steps: [{run: 'true'}]\n",
    # an empty job-level permissions value
    "name: x\non: pull_request\npermissions: {}\njobs:\n  a:\n    permissions:\n    runs-on: ubuntu-24.04\n    steps: [{run: 'true'}]\n",
    # permissions present but empty, so the repo default applies
    "name: x\non: pull_request\npermissions:\njobs:\n  a:\n    runs-on: ubuntu-24.04\n    steps: [{run: 'true'}]\n",
    # a duplicate key would otherwise let the last one win
    "name: x\non: pull_request\npermissions: {}\njobs:\n  a:\n    runs-on: ubuntu-24.04\n    steps: [{run: 'true'}]\n  a:\n    name: promotion-gate\n    runs-on: ubuntu-24.04\n    steps: [{run: 'true'}]\n",
])
@pytest.mark.parametrize("fname", ["sneaky.yml", "sneaky.YML", "sub/sneaky.Yaml"])
def test_workflow_that_could_fake_the_gate_is_refused(repo, yml, fname):
    branch(repo)
    head = commit(repo, {f".github/workflows/{fname}": yml})
    with pytest.raises(gate.GateError, match="workflow rules broken"):
        run_prepare(repo, head)


def test_gate_triggers_declared_twice_is_refused(repo):
    branch(repo)
    yml = GATE_YML.replace("\non:\n", '\n"on": {push: {branches: [main]}}\non:\n', 1)
    assert yml != GATE_YML
    head = commit(repo, {".github/workflows/promotion-gate.yml": yml})
    with pytest.raises(gate.GateError, match="twice|duplicate"):
        run_prepare(repo, head)


def test_lone_carriage_returns_are_not_canonical(repo):
    branch(repo)
    text = qqtc.render_promoted({"demo": pin(digest="sha256:" + "e" * 64)})
    head = commit(repo, {"promoted.toml": text.replace("\n[[toolchain]]", "\r[[toolchain]]")})
    with pytest.raises(qqtc.SpecError):
        run_prepare(repo, head)


def test_merge_inside_the_pr_cannot_hide_a_change(repo):
    """The file list comes from what lands, not from the PR's own diff."""
    branch(repo, "side")
    commit(repo, {"tools/qqtc.py": "# replaced\n"})
    sh(repo, "checkout", "-q", "main")
    branch(repo)
    sh(repo, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "-q", "--no-ff", "-m", "m", "side")
    head = commit(repo, {"promoted.toml": qqtc.render_promoted({"demo": pin(digest="sha256:" + "e" * 64)})})
    with pytest.raises(gate.GateError, match="tools/qqtc.py"):
        run_prepare(repo, head)


def test_changing_the_gate_triggers_is_refused(repo):
    branch(repo)
    yml = GATE_YML.replace("  merge_group:\n", "  pull_request_target:\n  merge_group:\n")
    assert yml != GATE_YML
    head = commit(repo, {".github/workflows/promotion-gate.yml": yml})
    with pytest.raises(gate.GateError, match="triggers differ"):
        run_prepare(repo, head)


@pytest.mark.parametrize("extra", [
    "concurrency:\n  group: g\n  cancel-in-progress: true\n",
    "concurrency: g\n",
])
def test_gate_concurrency_is_refused(repo, extra):
    branch(repo)
    yml = GATE_YML.replace("jobs:\n", extra + "jobs:\n", 1)
    assert yml != GATE_YML
    head = commit(repo, {".github/workflows/promotion-gate.yml": yml})
    with pytest.raises(gate.GateError, match="must not set concurrency"):
        run_prepare(repo, head)


def test_gate_job_concurrency_is_refused(repo):
    branch(repo)
    yml = GATE_YML.replace("    runs-on: ubuntu-24.04\n", "    runs-on: ubuntu-24.04\n    concurrency: g\n", 1)
    assert yml != GATE_YML
    head = commit(repo, {".github/workflows/promotion-gate.yml": yml})
    with pytest.raises(gate.GateError, match="must not set concurrency"):
        run_prepare(repo, head)


@pytest.mark.parametrize("value", ["41", "45", "0", "true", "40.0", "'30'", "${{ 30 }}", None])
def test_gate_timeout_over_the_queue_limit_is_refused(repo, value):
    branch(repo)
    line = "    timeout-minutes: 35"
    assert line in GATE_YML
    yml = GATE_YML.replace(line, "" if value is None else f"    timeout-minutes: {value}", 1)
    head = commit(repo, {".github/workflows/promotion-gate.yml": yml})
    with pytest.raises(gate.GateError, match="timeout-minutes"):
        run_prepare(repo, head)


@pytest.mark.parametrize("job", [
    "  second:\n    runs-on: ubuntu-24.04\n    steps:\n      - run: true\n",
    "  second:\n    uses: ./.github/workflows/ci.yml\n",
    "  second: 3\n",
    "  second:\n",
])
def test_every_gate_job_needs_a_timeout(repo, job):
    branch(repo)
    head = commit(repo, {".github/workflows/promotion-gate.yml": GATE_YML + job})
    with pytest.raises(gate.GateError, match="job 'second'"):
        run_prepare(repo, head)


def test_pull_request_target_is_no_longer_a_gate_event(repo):
    branch(repo)
    head = commit(repo, {"README.md": "changed\n"})
    with pytest.raises(gate.GateError, match="unexpected event"):
        run_prepare(repo, head, event="pull_request_target")


def _pick_tools(repo: Path, event: str, base: str, head: str) -> subprocess.CompletedProcess:
    """Run the workflow's own `case` that picks the trusted tools commit, as the step does."""
    step = next(st["run"] for st in yaml.safe_load(GATE_YML)["jobs"]["promotion-gate"]["steps"]
                if st.get("id") == "changed")
    case = step[step.index('case "$GITHUB_EVENT_NAME" in'):step.index("esac") + len("esac")]
    env = {"PATH": os.environ["PATH"], "GITHUB_EVENT_NAME": event, "BASE": base, "HEAD": head}
    return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c",
                           case + '\necho "$tools"'], cwd=repo, env=env, capture_output=True, text=True)


def test_the_step_picks_trusted_tools_for_each_event(repo):
    main = sh(repo, "rev-parse", "main")
    sh(repo, "update-ref", "refs/remotes/origin/main", main)
    branch(repo)
    head = commit(repo, {"README.md": "changed\n"})
    picked = {e: _pick_tools(repo, e, base="b" * 40, head=head) for e in
              ("pull_request", "merge_group", "push", "pull_request_target", "workflow_dispatch")}
    assert picked["pull_request"].stdout.strip() == main  # main's tip, never the PR head
    assert picked["merge_group"].stdout.strip() == "b" * 40  # the queue's base
    assert picked["push"].stdout.strip() == head  # main itself after a push
    for event in ("pull_request_target", "workflow_dispatch"):
        assert picked[event].returncode != 0 and "does not handle" in picked[event].stdout


def test_prepare_from_the_trusted_worktree_ignores_the_checkouts_gitattributes(repo):
    # The step runs prepare from inside $GATE. An untracked .gitattributes in the PR checkout
    # (merge=union) must not turn a conflicting promoted.toml into a clean merge.
    sh(repo, "checkout", "-q", "-b", "side", "main")
    side = commit(repo, {"promoted.toml": qqtc.render_promoted({"demo": pin(digest="sha256:" + "c" * 64)})})
    sh(repo, "checkout", "-q", "main")
    commit(repo, {"promoted.toml": qqtc.render_promoted({"demo": pin(digest="sha256:" + "d" * 64)})})
    (repo / ".gitattributes").write_text("promoted.toml merge=union\n")
    main = sh(repo, "rev-parse", "main")
    gate_dir = repo.parent / "gate-wt"
    sh(repo, "worktree", "add", "-q", "--detach", str(gate_dir), main)
    with pytest.raises(gate.GateError, match="conflicts with main"):
        gate.prepare(gate_dir, "pull_request", main, side, main, gate_dir, repo.parent / "out")


def test_removing_the_gate_workflow_is_refused(repo):
    branch(repo)
    head = commit(repo, {".github/workflows/promotion-gate.yml": None})
    with pytest.raises(gate.GateError, match="must not be removed"):
        run_prepare(repo, head)


def test_ordinary_workflow_change_passes(repo):
    branch(repo)
    head = commit(repo, {".github/workflows/ci.yml": CI_YML + "# comment\n"})
    assert run_prepare(repo, head)["changed"] == []


def test_merge_group_refuses_gate_and_promotion_together(repo):
    base = sh(repo, "rev-parse", "main")
    branch(repo)
    head = commit(repo, {"promoted.toml": qqtc.render_promoted({"demo": pin(digest="sha256:" + "e" * 64)}),
                         ".github/workflows/ci.yml": CI_YML + "# x\n"})
    with pytest.raises(gate.GateError, match="separate groups"):
        run_prepare(repo, head, event="merge_group", base=base)


def test_merge_group_promotion_passes(repo):
    base = sh(repo, "rev-parse", "main")
    branch(repo)
    head = commit(repo, {"promoted.toml": qqtc.render_promoted({"demo": pin(digest="sha256:" + "e" * 64)})})
    assert len(run_prepare(repo, head, event="merge_group", base=base)["changed"]) == 1


def test_push_with_unknown_before_fails_closed(repo):
    head = sh(repo, "rev-parse", "main")
    with pytest.raises(gate.GateError):
        gate.prepare(repo, "push", "0" * 40, head, head, repo, repo.parent / "out")


def test_this_repos_workflows_pass_the_rules():
    wf = {f".github/workflows/{p.name}": p.read_bytes() for p in (ROOT / ".github/workflows").glob("*.yml")}
    assert gate.workflow_problems(wf) == []


def test_error_output_cannot_start_a_workflow_command(capsys, tmp_path):
    d = tmp_path / "wf"
    d.mkdir()
    (d / "promotion-gate.yml").write_text(GATE_YML)
    (d / "x.yml").write_text("name: x\non: push\njobs: {}\n")
    assert gate.main(["check-workflows", str(d)]) == 1
    out = capsys.readouterr().err.splitlines()
    assert out[0].startswith("::error::")
    assert all(line.startswith("- ") for line in out[1:])
