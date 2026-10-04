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
    qqtc promote NAME --record FILE    record a staged build as NAME's promoted pin
    qqtc promoted-changed --base FILE  print the promoted entries that differ from FILE, as JSON

A toolchain lives in toolchains/<name>/ as toolchain.toml plus a build script. `build` writes
<name>-<version>-r<revision>-<platform>.tar.gz and <name>.record.json; publishing the tarball
to the registry is the backend's job (see .github/workflows/build.yml for github).

promoted.toml holds one pin per toolchain: the digest a reviewed PR promoted from staging. The
toolchain roller reads it to update product repos; nothing else should pin a toolchain.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import posixpath
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
PROMOTED = ROOT / "promoted.toml"
PROMOTED_SCHEMA = "quirq-toolchains-promoted/1"

BACKENDS = ("github", "launchpad")
PLATFORMS = ("linux-x86_64",)
# Fixed tar mtime (1980-01-01, the earliest zip-safe time) so packing the same tree twice gives
# the same bytes.
PACK_EPOCH = 315532800

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
VERSION_RE = re.compile(r"^[0-9]+(\.[0-9]+)*$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
APT_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")
FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
BUILD_RUN_RE = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+$")
PROMOTED_KEYS = ("name", "version", "revision", "platform", "ref", "layer_sha256", "built_from", "build_run")


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
        _require(FILENAME_RE.match(fname) is not None, w,
                 f"source file name {fname!r} must be a plain file name (set `filename` if the URL has none)")
        _require(fname not in seen, w, f"two sources save to the same file {fname!r}")
        seen.add(fname)

    build = spec.get("build")
    _require(isinstance(build, dict), where, "missing [build] table")
    _check_keys(build, {"script", *BACKENDS}, f"{where} [build]")
    gh = build.get("github", {})
    w = f"{where} [build.github]"
    _require(isinstance(gh, dict), w, "must be a table")
    _check_keys(gh, {"apt_packages"}, w)
    pkgs = gh.get("apt_packages", [])
    _require(isinstance(pkgs, list) and all(isinstance(x, str) and APT_RE.match(x) for x in pkgs), w,
             "apt_packages must be a list of Debian package names")
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


def registry_repo(cfg: dict, name: str) -> str:
    return f"{cfg[cfg['backend']]['registry']}/{name}"


def check_promoted_entry(entry: dict, cfg: dict, where: str) -> None:
    _check_keys(entry, set(PROMOTED_KEYS), where)
    missing = [k for k in PROMOTED_KEYS if k not in entry]
    _require(not missing, where, f"missing key(s) {missing}")
    name = entry["name"]
    _require(isinstance(name, str) and NAME_RE.match(name) is not None, where, "bad name")
    _require(isinstance(entry["version"], str) and VERSION_RE.match(entry["version"]) is not None,
             where, "version must look like 1.2.3")
    _require(type(entry["revision"]) is int and entry["revision"] >= 1, where, "revision must be an integer >= 1")
    _require(entry["platform"] in PLATFORMS, where, f"platform must be one of {PLATFORMS}")
    prefix = f"oci://{registry_repo(cfg, name)}@"
    ref = entry["ref"]
    _require(isinstance(ref, str) and ref.startswith(prefix) and DIGEST_RE.match(ref[len(prefix):]) is not None,
             where, f"ref must be {prefix}sha256:<64 hex> (a digest, never a tag)")
    _require(isinstance(entry["layer_sha256"], str) and SHA256_RE.match(entry["layer_sha256"]) is not None,
             where, "layer_sha256 must be 64 lowercase hex characters")
    _require(isinstance(entry["built_from"], str) and GIT_SHA_RE.match(entry["built_from"]) is not None,
             where, "built_from must be the full commit SHA the staging build ran on")
    _require(isinstance(entry["build_run"], str) and BUILD_RUN_RE.match(entry["build_run"]) is not None,
             where, "build_run must be https://github.com/<owner>/<repo>/actions/runs/<id>")


def load_promoted(path: Path = PROMOTED, cfg: dict | None = None, spec_dir: Path = SPEC_DIR) -> dict[str, dict]:
    """Return promoted entries by name. A missing file means nothing is promoted yet."""
    if not path.exists():
        return {}
    cfg = cfg or load_repo_config()
    data = _load_toml(path)
    where = path.name
    _check_keys(data, {"schema", "toolchain"}, where)
    _require(data.get("schema") == PROMOTED_SCHEMA, where, f"schema must be {PROMOTED_SCHEMA!r}")
    entries = {}
    for i, entry in enumerate(data.get("toolchain", [])):
        w = f"{where} [[toolchain]] #{i + 1}"
        check_promoted_entry(entry, cfg, w)
        _require(entry["name"] not in entries, w, f"{entry['name']!r} is promoted twice")
        _require((spec_dir / entry["name"] / "toolchain.toml").is_file(), w,
                 f"no toolchains/{entry['name']}/toolchain.toml for this entry")
        entries[entry["name"]] = entry
    return entries


def _toml_value(v) -> str:
    if type(v) is int:
        return str(v)
    # Every promoted string is validated to plain ASCII (hex, URLs, versions), where JSON and
    # TOML basic strings agree. write_promoted re-parses its output to make sure.
    _require(isinstance(v, str) and v.isascii() and v.isprintable(), "promoted.toml",
             f"refusing to write value {v!r}")
    return json.dumps(v)


def write_promoted(entries: dict[str, dict], path: Path = PROMOTED) -> None:
    lines = [
        "# Promoted toolchains: one pin per toolchain, by digest. Written by `qqtc promote` in a",
        "# reviewed PR; read by the toolchain roller (quirq-ai/rollers, V0-ROL-01). Do not edit by hand.",
        f'schema = "{PROMOTED_SCHEMA}"',
    ]
    for name in sorted(entries):
        lines += ["", "[[toolchain]]"]
        lines += [f"{k} = {_toml_value(entries[name][k])}" for k in PROMOTED_KEYS]
    text = "\n".join(lines) + "\n"
    tomllib.loads(text)  # never leave a file that later runs cannot read
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def promote(record: dict, path: Path = PROMOTED, spec_dir: Path = SPEC_DIR, cfg: dict | None = None) -> dict:
    """Make a staged build the promoted pin for its toolchain. Returns the new entry."""
    cfg = cfg or load_repo_config()
    _require(isinstance(record, dict), "record", "must be a JSON object")
    entry = {k: record.get(k) for k in PROMOTED_KEYS}
    name = entry["name"]
    _require(isinstance(name, str), "record", "missing name")
    spec = load_spec(name, spec_dir)["toolchain"]
    check_promoted_entry(entry, cfg, "record")
    for k in ("version", "revision", "platform"):
        _require(entry[k] == spec[k], "record",
                 f"{k} is {entry[k]!r} but toolchains/{name}/toolchain.toml says {spec[k]!r}; "
                 "promote only a build of the spec as it is now")
    entries = load_promoted(path, cfg, spec_dir)
    entries[name] = entry
    write_promoted(entries, path)
    return entry


def promoted_changed(base: Path | None, path: Path = PROMOTED, spec_dir: Path = SPEC_DIR) -> list[dict]:
    """Entries that are new or different compared with base (an older promoted.toml, or None)."""
    cfg = load_repo_config()
    now = load_promoted(path, cfg, spec_dir)
    before = load_promoted(base, cfg, spec_dir) if base and base.exists() and base.stat().st_size else {}
    changed = [e for n, e in sorted(now.items()) if before.get(n) != e]
    for e in changed:
        spec = load_spec(e["name"], spec_dir)["toolchain"]
        for k in ("version", "revision", "platform"):
            _require(e[k] == spec[k], path.name,
                     f"{e['name']}: promoted {k} {e[k]!r} does not match its spec ({spec[k]!r})")
    return changed


def validate(spec_dir: Path = SPEC_DIR, repo_config: Path = REPO_CONFIG, promoted: Path = PROMOTED) -> list[str]:
    """Return a list of problems; empty means valid."""
    problems = []
    cfg = None
    try:
        cfg = load_repo_config(repo_config)
    except SpecError as e:
        problems.append(str(e))
    if cfg is not None:
        try:
            load_promoted(promoted, cfg, spec_dir)
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
            elif ti.isdir():
                tar.addfile(ti)
            elif ti.issym() or ti.islnk():
                base = posixpath.dirname(ti.name) if ti.issym() else ""
                target = posixpath.normpath(posixpath.join(base, ti.linkname))
                if ti.linkname.startswith("/") or target == ".." or target.startswith("../"):
                    raise SpecError(f"{p}: link to {ti.linkname!r} points outside the toolchain")
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
    p = sub.add_parser("promote")
    p.add_argument("name")
    p.add_argument("--record", required=True, help="staged record JSON file, or - for stdin")
    p = sub.add_parser("promoted-changed")
    p.add_argument("--base", type=Path, help="promoted.toml before the change (missing or empty: none)")
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
        elif args.cmd == "promote":
            text = sys.stdin.read() if args.record == "-" else Path(args.record).read_text()
            try:
                record = json.loads(text)
            except json.JSONDecodeError as e:
                raise SpecError(f"--record: not valid JSON: {e}") from None
            _require(isinstance(record, dict), "--record", "must be a JSON object")
            _require(record.get("name") == args.name, "record",
                     f"record is for {record.get('name')!r}, not {args.name!r}")
            entry = promote(record)
            print(f"promoted {entry['name']} {entry['version']}-r{entry['revision']}: {entry['ref']}")
        elif args.cmd == "promoted-changed":
            print(json.dumps(promoted_changed(args.base)))
    except SpecError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"error: command failed with exit code {e.returncode}: {e.cmd}", file=sys.stderr)
        return 1
    except (tarfile.TarError, OSError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
