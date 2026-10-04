#!/usr/bin/env python3
"""gate: decide what the promotion gate must verify, from git data only.

promotion-gate.yml runs this from a trusted worktree (main's tip for a PR, the queue's base in the
merge queue, main itself after a push). Everything it reads from the change under test comes out
of git as data; nothing from that change is imported or run.

    gate.py prepare --event E --base B --head H --trusted T --gate DIR --out DIR
        apply the rules below, write DIR/promoted.toml (the pins that will land) and print
        {"changed": [...new or moved pins...], "merged": "<tree or commit the pins land in>"}
    gate.py check-workflows DIR
        check that only promotion-gate.yml defines a job reported as `promotion-gate`, that the
        gate's triggers match main's, and that no workflow can write check runs or statuses

Rules, by event:
- A PR (pull_request_target, or pull_request when this runs as a required workflow) is judged
  against main's tip and by what will actually merge (git merge-tree). A PR that changes
  promoted.toml may change nothing else except README.md. Its workflows must not define another
  check named promotion-gate or change the gate's triggers.
- In the merge queue, a group may not change promoted.toml and .github/ together, and the same
  workflow rules apply to the merged tree.
- After a push to main, main is trusted; only the pins are compared.

Needs PyYAML for the workflow rules (tools/gate-requirements.txt pins it by hash).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# python -I does not put this script's directory on sys.path; this directory is the trusted one.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import qqtc  # noqa: E402

PR_EVENTS = ("pull_request_target", "pull_request")
PROMOTION_FILES = {"promoted.toml", "README.md"}
GATE_WORKFLOW = ".github/workflows/promotion-gate.yml"
GATE_JOB = "promotion-gate"
# When the gate runs. A PR cannot change this: the trusted copy of this constant judges it, so a
# change to the triggers needs an owner to land it outside the gate.
GATE_TRIGGERS = {
    "pull_request_target": {"branches": ["main"], "types": ["opened", "synchronize", "reopened", "edited"]},
    "merge_group": None,
    "push": {"branches": ["main"]},
}


class GateError(Exception):
    """The change breaks a gate rule. The message says which and what to do."""


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if r.returncode != 0:
        raise GateError(f"git {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def show(repo: Path, ref: str, path: str) -> str | None:
    """A file's text at ref, or None when it does not exist there."""
    r = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=repo, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def changed_files(repo: Path, a: str, b: str, merge_base: bool) -> list[str]:
    spec = f"{a}...{b}" if merge_base else f"{a}..{b}"
    return [f for f in git(repo, "diff", "--no-renames", "--name-only", "-z", spec).split("\0") if f]


def merged_tree(repo: Path, trusted: str, head: str) -> str:
    """The tree that merging head into trusted would produce. A conflict is an error."""
    r = subprocess.run(["git", "merge-tree", "--write-tree", "--name-only", trusted, head],
                       cwd=repo, capture_output=True, text=True)
    if r.returncode == 1:
        raise GateError("this PR conflicts with main; update it so the gate can see what will merge")
    if r.returncode != 0:
        raise GateError(f"git merge-tree failed: {r.stderr.strip()}")
    return r.stdout.splitlines()[0]


def _listing(files) -> str:
    # One per line, prefixed and quoted, so no file name can start a workflow command.
    return "\n".join(f"- {f!r}" for f in sorted(files))


def _triggers(doc: dict):
    # YAML 1.1 reads a bare `on` key as true.
    return doc.get("on", doc.get(True))


def _risky_permissions(doc: dict):
    """(where, what) for each grant that lets a token write check runs or commit statuses."""
    scopes = [("the workflow", doc.get("permissions"))]
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):
        scopes += [(f"job {j!r}", job.get("permissions")) for j, job in jobs.items() if isinstance(job, dict)]
    for where, perms in scopes:
        if perms == "write-all" or (isinstance(perms, str) and "${{" in perms):
            yield where, f"permissions: {perms}"
        elif isinstance(perms, dict):
            for scope in ("checks", "statuses"):
                if perms.get(scope) not in (None, "read", "none"):
                    yield where, f"{scope}: {perms.get(scope)}"


def workflow_problems(workflows: dict[str, str]) -> list[str]:
    """Problems with the workflow files {path: text}."""
    import yaml  # only needed here, so qqtc and the other commands stay standard library

    problems = []
    if GATE_WORKFLOW not in workflows:
        problems.append(f"{GATE_WORKFLOW} must not be removed or renamed")
    for path, text in sorted(workflows.items()):
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as e:
            problems.append(f"{path}: not valid YAML ({e.__class__.__name__})")
            continue
        if not isinstance(doc, dict):
            problems.append(f"{path}: not a workflow mapping")
            continue
        if path == GATE_WORKFLOW and _triggers(doc) != GATE_TRIGGERS:
            problems.append(f"{path}: its triggers differ from gate.py's GATE_TRIGGERS; a change to "
                            "when the gate runs needs an owner and lands outside the gate")
        if "permissions" not in doc:
            problems.append(f"{path}: must declare top-level permissions (the repo default may allow writes)")
        problems += [f"{path}: {where} grants {what}; a workflow could then post its own "
                     f"{GATE_JOB} check or status" for where, what in _risky_permissions(doc)]
        jobs = doc.get("jobs") or {}
        if not isinstance(jobs, dict):
            problems.append(f"{path}: jobs is not a mapping")
            continue
        for job_id, job in jobs.items():
            name = job.get("name", job_id) if isinstance(job, dict) else job_id
            if not isinstance(name, str) or "${{" in name:
                problems.append(f"{path}: job {job_id!r} must have a literal name")
                continue
            reported = name.strip().lower()
            if reported == GATE_JOB and not (path == GATE_WORKFLOW and job_id == GATE_JOB and name == job_id):
                problems.append(f"{path}: job {job_id!r} reports a check named {GATE_JOB!r}; "
                                f"only the {GATE_JOB} job in {GATE_WORKFLOW} may")
    return problems


def workflows_at(repo: Path, ref: str) -> dict[str, str]:
    out = {}
    listing = git(repo, "ls-tree", "-r", "-z", "--name-only", ref, "--", ".github/workflows/")
    for path in filter(None, listing.split("\0")):
        if path.endswith((".yml", ".yaml")):
            out[path] = show(repo, ref, path) or ""
    return out


def prepare(repo: Path, event: str, base: str, head: str, trusted: str, gate: Path, out: Path) -> dict:
    """Apply the rules and stage the pins to verify. Returns {"changed": [...], "merged": ref}."""
    if event in PR_EVENTS:
        new_ref = merged_tree(repo, trusted, head)
        old_ref = trusted
        files = changed_files(repo, trusted, head, merge_base=True)
        if "promoted.toml" in files:
            other = set(files) - PROMOTION_FILES
            if other:
                raise GateError("a PR that changes promoted.toml may change only promoted.toml and "
                                "README.md. Move these to their own PR:\n" + _listing(other))
    elif event == "merge_group":
        new_ref, old_ref = head, base
        files = changed_files(repo, base, head, merge_base=False)
        if "promoted.toml" in files and any(f.startswith(".github/") for f in files):
            raise GateError("this merge group changes promoted.toml and .github/ together; "
                            "merge the gate change and the promotion in separate groups")
    elif event == "push":
        new_ref, old_ref = head, base
        git(repo, "cat-file", "-e", f"{base}^{{commit}}")  # fail closed on a new or rewritten branch
    else:
        raise GateError(f"unexpected event {event!r}")

    if event != "push":
        problems = workflow_problems(workflows_at(repo, new_ref))
        if problems:
            raise GateError("workflow rules broken:\n" + _listing(problems))

    out.mkdir(parents=True, exist_ok=True)
    (gate / "promoted.toml").write_text(show(repo, new_ref, "promoted.toml") or "")
    base_toml = out / "base.toml"
    base_toml.write_text(show(repo, old_ref, "promoted.toml") or "")
    changed = qqtc.promoted_changed(base_toml, gate / "promoted.toml", gate / "toolchains")
    return {"changed": changed, "merged": new_ref}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gate", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    for flag in ("--event", "--base", "--head", "--trusted"):
        p.add_argument(flag, required=True)
    p.add_argument("--gate", type=Path, required=True, help="trusted worktree that runs the checks")
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("check-workflows")
    p.add_argument("dir", type=Path)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "prepare":
            print(json.dumps(prepare(Path.cwd(), args.event, args.base, args.head, args.trusted,
                                     args.gate, args.out)))
        else:
            wf = {f".github/workflows/{p.name}": p.read_text()
                  for p in sorted(args.dir.iterdir()) if p.suffix in (".yml", ".yaml")}
            problems = workflow_problems(wf)
            if problems:
                raise GateError("workflow rules broken:\n" + _listing(problems))
            print("PASS")
    except (GateError, qqtc.SpecError) as e:
        lines = str(e).splitlines()
        print(f"::error::{lines[0]}")
        print("\n".join(lines[1:]))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
