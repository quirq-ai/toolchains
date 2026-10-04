#!/usr/bin/env python3
"""qqtc: build, pack, check and promote quirq infra toolchains.

Standard library only, so it runs on a bare runner or a fresh machine.

    qqtc validate                      check every spec (and promoted.toml, when it exists)
    qqtc list                          print the toolchain names as a JSON array
    qqtc ci-config NAME                print the backend's build settings for NAME as JSON
    qqtc build NAME --out DIR          fetch and verify sources, build, pack, write a record
    qqtc unpack TARBALL DIR            unpack a toolchain tarball safely
    qqtc smoke NAME ROOT               run NAME's smoke commands in an unpacked toolchain
    qqtc consumers NAME ROOT --work W  build NAME's pinned consumer repos with the toolchain

A toolchain lives in toolchains/<name>/ as toolchain.toml plus a build script. `build` writes
<name>-<version>-r<revision>-<platform>.tar.gz and <name>.record.json; publishing the tarball
to the registry is the backend's job (see .github/workflows/build.yml for github).
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC_DIR = ROOT / "toolchains"
REPO_CONFIG = ROOT / "toolchains.toml"

BACKENDS = ("github", "launchpad")
PLATFORMS = ("linux-x86_64",)
# Fixed tar mtime (1980-01-01, the earliest zip-safe time) so packing the same tree twice gives
# the same bytes.
PACK_EPOCH = 315532800

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
VERSION_RE = re.compile(r"^[0-9]+(\.[0-9]+)*$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class SpecError(Exception):
    """A spec or config file is malformed. The message names the file and the field."""


# --- loading and validation ------------------------------------------------------------------


def _load_toml(path: Path) -> dict:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise SpecError(f"{path}: file not found") from None
    except tomllib.TOMLDecodeError as e:
        raise SpecError(f"{path}: invalid TOML: {e}") from None


def _require(cond: bool, where: str, msg: str) -> None:
    if not cond:
        raise SpecError(f"{where}: {msg}")


def _check_keys(table: dict, allowed: set[str], where: str) -> None:
    extra = sorted(set(table) - allowed)
    _require(not extra, where, f"unknown key(s) {extra}; allowed: {sorted(allowed)}")


def load_repo_config(path: Path = REPO_CONFIG) -> dict:
    cfg = _load_toml(path)
    where = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
    _require(cfg.get("schema") == "quirq-toolchains/1", where, "schema must be 'quirq-toolchains/1'")
    backend = cfg.get("backend")
    _require(backend in BACKENDS, where, f"backend must be one of {BACKENDS}, got {backend!r}")
    _require(isinstance(cfg.get(backend), dict), where, f"missing [{backend}] table for the active backend")
    _require(isinstance(cfg[backend].get("registry"), str) and cfg[backend]["registry"],
             where, f"[{backend}].registry must be a non-empty string")
    return cfg


def load_spec(name: str, spec_dir: Path = SPEC_DIR) -> dict:
    path = spec_dir / name / "toolchain.toml"
    where = f"toolchains/{name}/toolchain.toml"
    spec = _load_toml(path)
    _check_keys(spec, {"toolchain", "source", "build", "smoke", "consumer"}, where)

    tc = spec.get("toolchain")
    _require(isinstance(tc, dict), where, "missing [toolchain] table")
    _check_keys(tc, {"name", "version", "revision", "platform", "description"}, f"{where} [toolchain]")
    _require(tc.get("name") == name, where, f"[toolchain].name must equal the directory name {name!r}")
    _require(NAME_RE.match(name) is not None, where, "name must be lowercase letters, digits and '-'")
    _require(isinstance(tc.get("version"), str) and VERSION_RE.match(tc["version"]) is not None,
             where, "[toolchain].version must look like 1.2.3")
    _require(isinstance(tc.get("revision"), int) and tc["revision"] >= 1, where,
             "[toolchain].revision must be an integer >= 1")
    _require(tc.get("platform") in PLATFORMS, where, f"[toolchain].platform must be one of {PLATFORMS}")

    sources = spec.get("source")
    _require(isinstance(sources, list) and sources, where, "needs at least one [[source]]")
    seen = set()
    for i, src in enumerate(sources):
        w = f"{where} [[source]] #{i + 1}"
        _check_keys(src, {"url", "sha256", "filename"}, w)
        _require(isinstance(src.get("url"), str) and src["url"].startswith("https://"), w,
                 "url must be an https URL")
        _require(isinstance(src.get("sha256"), str) and SHA256_RE.match(src["sha256"]) is not None, w,
                 "sha256 must be 64 lowercase hex characters")
        fname = source_filename(src)
        _require(fname not in seen, w, f"two sources save to the same file {fname!r}")
        seen.add(fname)

    build = spec.get("build")
    _require(isinstance(build, dict), where, "missing [build] table")
    _check_keys(build, {"script", *BACKENDS}, f"{where} [build]")
    script = build.get("script")
    _require(isinstance(script, str) and (spec_dir / name / script).is_file(), where,
             f"[build].script {script!r} must name a file in toolchains/{name}/")

    smoke = spec.get("smoke", {})
    _check_keys(smoke, {"commands"}, f"{where} [smoke]")
    cmds = smoke.get("commands")
    _require(isinstance(cmds, list) and cmds and all(isinstance(c, str) for c in cmds), where,
             "[smoke].commands must be a non-empty list of strings")

    for i, con in enumerate(spec.get("consumer", [])):
        w = f"{where} [[consumer]] #{i + 1}"
        _check_keys(con, {"repo", "commit", "commands"}, w)
        _require(isinstance(con.get("repo"), str) and con["repo"].startswith("https://"), w,
                 "repo must be an https git URL")
        _require(isinstance(con.get("commit"), str) and GIT_SHA_RE.match(con["commit"]) is not None, w,
                 "commit must be a full 40-character git SHA (consumers are pinned)")
        _require(isinstance(con.get("commands"), list) and con["commands"]
                 and all(isinstance(c, str) for c in con["commands"]), w,
                 "commands must be a non-empty list of strings")
    return spec


def toolchain_names(spec_dir: Path = SPEC_DIR) -> list[str]:
    return sorted(p.parent.name for p in spec_dir.glob("*/toolchain.toml"))


def source_filename(src: dict) -> str:
    return src.get("filename") or src["url"].rsplit("/", 1)[-1]


def artifact_basename(spec: dict) -> str:
    tc = spec["toolchain"]
    return f"{tc['name']}-{tc['version']}-r{tc['revision']}-{tc['platform']}"


def validate(spec_dir: Path = SPEC_DIR, repo_config: Path = REPO_CONFIG) -> list[str]:
    """Return a list of problems; empty means valid."""
    problems = []
    try:
        load_repo_config(repo_config)
    except SpecError as e:
        problems.append(str(e))
    names = toolchain_names(spec_dir)
    if not names:
        problems.append("toolchains/: no toolchain.toml found")
    for name in names:
        try:
            load_spec(name, spec_dir)
        except SpecError as e:
            problems.append(str(e))
    return problems


# --- fetching and building -------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_sources(spec: dict, dest: Path) -> None:
    """Download every source into dest and verify it against its pinned sha256."""
    dest.mkdir(parents=True, exist_ok=True)
    for src in spec["source"]:
        target = dest / source_filename(src)
        if not target.exists() or sha256_file(target) != src["sha256"]:
            print(f"fetch {src['url']}", flush=True)
            tmp = target.with_suffix(target.suffix + ".part")
            with urllib.request.urlopen(src["url"], timeout=300) as r, tmp.open("wb") as f:
                shutil.copyfileobj(r, f)
            tmp.replace(target)
        got = sha256_file(target)
        if got != src["sha256"]:
            raise SpecError(
                f"{src['url']}: sha256 mismatch: pinned {src['sha256']}, downloaded {got}. "
                "If the upstream release is the one you meant to pin, verify it independently "
                "before updating the pin."
            )


def pack(tree: Path, out: Path) -> None:
    """Pack tree's contents into a deterministic .tar.gz rooted at the toolchain root."""
    paths = sorted(tree.rglob("*"))
    with out.open("wb") as f, \
            gzip.GzipFile(filename="", mode="wb", fileobj=f, mtime=0, compresslevel=9) as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for p in paths:
            ti = tar.gettarinfo(str(p), arcname=p.relative_to(tree).as_posix())
            ti.mtime = PACK_EPOCH
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            if ti.isreg():
                with p.open("rb") as src:
                    tar.addfile(ti, src)
            elif ti.isdir() or ti.issym() or ti.islnk():
                tar.addfile(ti)
            else:
                raise SpecError(f"{p}: only files, directories and links can be packed")


def unpack(tarball: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball, "r:gz") as tar:
        # "data" refuses absolute paths, links that escape dest, and device files.
        tar.extractall(dest, filter="data")


def build(name: str, out: Path, spec_dir: Path = SPEC_DIR) -> dict:
    spec = load_spec(name, spec_dir)
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"qqtc-{name}-"))
    sources, scratch, prefix = work / "sources", work / "work", work / "prefix"
    scratch.mkdir()
    prefix.mkdir()
    fetch_sources(spec, sources)
    env = dict(os.environ, QQ_SOURCES=str(sources), QQ_WORK=str(scratch), QQ_PREFIX=str(prefix),
               QQ_VERSION=spec["toolchain"]["version"], QQ_NAME=name)
    script = spec_dir / name / spec["build"]["script"]
    subprocess.run(["bash", str(script)], env=env, check=True)

    tarball = out / f"{artifact_basename(spec)}.tar.gz"
    pack(prefix, tarball)
    tc = spec["toolchain"]
    record = {
        "name": name,
        "version": tc["version"],
        "revision": tc["revision"],
        "platform": tc["platform"],
        "file": tarball.name,
        "layer_sha256": sha256_file(tarball),
        "size": tarball.stat().st_size,
        "built_from": os.environ.get("GITHUB_SHA", ""),
    }
    (out / f"{name}.record.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    shutil.rmtree(work, ignore_errors=True)
    print(json.dumps(record, indent=2, sort_keys=True))
    return record


# --- checking a built toolchain --------------------------------------------------------------


def _run_shell(cmd: str, cwd: Path, env: dict) -> None:
    print(f"$ {cmd}", flush=True)
    subprocess.run(["bash", "-euo", "pipefail", "-c", cmd], cwd=cwd, env=env, check=True)


def toolchain_env(root: Path) -> dict:
    env = dict(os.environ)
    env["PATH"] = f"{root / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    return env


def smoke(name: str, root: Path, spec_dir: Path = SPEC_DIR) -> None:
    spec = load_spec(name, spec_dir)
    version = spec["toolchain"]["version"]
    env = toolchain_env(root)
    for cmd in spec["smoke"]["commands"]:
        _run_shell(cmd.replace("{version}", version), root, env)


def consumers(name: str, root: Path, work: Path, spec_dir: Path = SPEC_DIR) -> None:
    """Check out each pinned consumer repo and run its build with only this toolchain on PATH first."""
    spec = load_spec(name, spec_dir)
    env = toolchain_env(root)
    for con in spec.get("consumer", []):
        repo_dir = work / con["repo"].rstrip("/").rsplit("/", 1)[-1]
        if repo_dir.exists():
            shutil.rmtree(repo_dir)
        repo_dir.mkdir(parents=True)
        for git in (["init", "-q"], ["fetch", "-q", "--depth", "1", con["repo"], con["commit"]],
                    ["checkout", "-q", "FETCH_HEAD"]):
            subprocess.run(["git", *git], cwd=repo_dir, check=True)
        print(f"consumer {con['repo']} @ {con['commit']}", flush=True)
        for cmd in con["commands"]:
            _run_shell(cmd, repo_dir, env)


# --- CLI -------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="qqtc", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate")
    sub.add_parser("list")
    p = sub.add_parser("ci-config")
    p.add_argument("name")
    p = sub.add_parser("build")
    p.add_argument("name")
    p.add_argument("--out", type=Path, default=ROOT / "out")
    p = sub.add_parser("unpack")
    p.add_argument("tarball", type=Path)
    p.add_argument("dest", type=Path)
    p = sub.add_parser("smoke")
    p.add_argument("name")
    p.add_argument("root", type=Path)
    p = sub.add_parser("consumers")
    p.add_argument("name")
    p.add_argument("root", type=Path)
    p.add_argument("--work", type=Path, required=True)
    args = ap.parse_args(argv)

    try:
        if args.cmd == "validate":
            problems = validate()
            for prob in problems:
                print(f"FAIL {prob}")
            print("PASS" if not problems else f"{len(problems)} problem(s)")
            return 1 if problems else 0
        if args.cmd == "list":
            print(json.dumps(toolchain_names()))
        elif args.cmd == "ci-config":
            backend = load_repo_config()["backend"]
            spec = load_spec(args.name)
            print(json.dumps(spec["build"].get(backend, {}), sort_keys=True))
        elif args.cmd == "build":
            build(args.name, args.out.resolve())
        elif args.cmd == "unpack":
            unpack(args.tarball, args.dest)
        elif args.cmd == "smoke":
            smoke(args.name, args.root.resolve())
        elif args.cmd == "consumers":
            consumers(args.name, args.root.resolve(), args.work.resolve())
    except SpecError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"error: command failed with exit code {e.returncode}: {e.cmd}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
