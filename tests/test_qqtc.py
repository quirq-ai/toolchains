"""Tests for tools/qqtc.py. Standard library plus pytest; no network."""

import hashlib
import io
import json
import sys
import tarfile
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import qqtc  # noqa: E402

PAYLOAD = b"upstream source bytes\n"
PAYLOAD_SHA = hashlib.sha256(PAYLOAD).hexdigest()

BUILD_SH = """\
#!/usr/bin/env bash
set -euo pipefail
mkdir -p "$QQ_PREFIX/bin" "$QQ_PREFIX/share"
cp "$QQ_SOURCES/demo-$QQ_VERSION.tar.gz" "$QQ_PREFIX/share/source"
printf '#!/bin/sh\\necho demo %s\\n' "$QQ_VERSION" > "$QQ_PREFIX/bin/demo"
chmod +x "$QQ_PREFIX/bin/demo"
ln -s demo "$QQ_PREFIX/bin/demo-alias"
"""


def write_spec(spec_dir: Path, name: str = "demo", body: str | None = None, script: str = BUILD_SH) -> Path:
    d = spec_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "build.sh").write_text(script)
    if body is None:
        body = f"""
            [toolchain]
            name = "{name}"
            version = "1.2.3"
            revision = 1
            platform = "linux-x86_64"

            [[source]]
            url = "https://example.invalid/demo-1.2.3.tar.gz"
            sha256 = "{PAYLOAD_SHA}"

            [build]
            script = "build.sh"
            [build.github]
            runs_on = "ubuntu-24.04"

            [smoke]
            commands = ["bin/demo | grep -qx 'demo {{version}}'", "test -L bin/demo-alias"]
        """
    (d / "toolchain.toml").write_text(textwrap.dedent(body))
    return d


@pytest.fixture
def spec_dir(tmp_path):
    d = tmp_path / "toolchains"
    write_spec(d)
    return d


@pytest.fixture
def fake_download(monkeypatch):
    calls = []

    def urlopen(url, timeout=None):
        calls.append(url)
        return io.BytesIO(PAYLOAD)

    monkeypatch.setattr(qqtc.urllib.request, "urlopen", urlopen)
    return calls


# --- the real repo ---------------------------------------------------------------------------


def test_repo_specs_are_valid():
    assert qqtc.validate() == []


def test_repo_lists_python():
    assert "python" in qqtc.toolchain_names()


def test_repo_consumers_are_pinned():
    for name in qqtc.toolchain_names():
        for con in qqtc.load_spec(name).get("consumer", []):
            assert qqtc.GIT_SHA_RE.match(con["commit"])


# --- validation ------------------------------------------------------------------------------


def test_valid_spec_loads(spec_dir):
    spec = qqtc.load_spec("demo", spec_dir)
    assert qqtc.artifact_basename(spec) == "demo-1.2.3-r1-linux-x86_64"


@pytest.mark.parametrize(
    "old, new, message",
    [
        ('name = "demo"', 'name = "other"', "directory name"),
        ('version = "1.2.3"', 'version = "latest"', "version"),
        ("revision = 1", "revision = 0", "revision"),
        ('platform = "linux-x86_64"', 'platform = "windows"', "platform"),
        ("https://example.invalid", "http://example.invalid", "https"),
        (PAYLOAD_SHA, "abc", "sha256"),
        ('script = "build.sh"', 'script = "missing.sh"', "script"),
        ("[smoke]", "[smoke]\nextra = 1", "unknown key"),
    ],
)
def test_invalid_spec_is_rejected(spec_dir, old, new, message):
    path = spec_dir / "demo" / "toolchain.toml"
    path.write_text(path.read_text().replace(old, new, 1))
    with pytest.raises(qqtc.SpecError, match=message):
        qqtc.load_spec("demo", spec_dir)


def test_unpinned_consumer_is_rejected(spec_dir):
    path = spec_dir / "demo" / "toolchain.toml"
    path.write_text(path.read_text() + textwrap.dedent("""
        [[consumer]]
        repo = "https://github.com/quirq-ai/xo-space"
        commit = "main"
        commands = ["true"]
    """))
    with pytest.raises(qqtc.SpecError, match="40-character"):
        qqtc.load_spec("demo", spec_dir)


def test_repo_config_requires_active_backend_table(tmp_path):
    cfg = tmp_path / "toolchains.toml"
    cfg.write_text('schema = "quirq-toolchains/1"\nbackend = "launchpad"\n[github]\nregistry = "x"\n')
    with pytest.raises(qqtc.SpecError, match=r"\[launchpad\]"):
        qqtc.load_repo_config(cfg)


# --- fetch, pack, unpack ---------------------------------------------------------------------


def test_fetch_rejects_a_mismatched_download(spec_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(qqtc.urllib.request, "urlopen", lambda url, timeout=None: io.BytesIO(b"tampered"))
    with pytest.raises(qqtc.SpecError, match="sha256 mismatch"):
        qqtc.fetch_sources(qqtc.load_spec("demo", spec_dir), tmp_path / "src")


def make_tree(root: Path) -> Path:
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "tool").write_text("#!/bin/sh\necho hi\n")
    (root / "bin" / "tool").chmod(0o755)
    (root / "bin" / "alias").symlink_to("tool")
    (root / "lib").mkdir()
    (root / "lib" / "data.txt").write_text("data\n")
    return root


def test_pack_is_deterministic(tmp_path):
    a = make_tree(tmp_path / "a")
    b = make_tree(tmp_path / "b")
    (b / "lib" / "data.txt").touch()  # a different mtime must not change the bytes
    qqtc.pack(a, tmp_path / "a.tar.gz")
    qqtc.pack(b, tmp_path / "b.tar.gz")
    assert (tmp_path / "a.tar.gz").read_bytes() == (tmp_path / "b.tar.gz").read_bytes()


def test_pack_unpack_round_trip(tmp_path):
    tree = make_tree(tmp_path / "tree")
    qqtc.pack(tree, tmp_path / "t.tar.gz")
    with tarfile.open(tmp_path / "t.tar.gz") as tar:
        names = tar.getnames()
        assert all(not n.startswith(("/", "./")) for n in names)
        assert all(m.uid == 0 and m.mtime == qqtc.PACK_EPOCH for m in tar.getmembers())
    out = tmp_path / "out"
    qqtc.unpack(tmp_path / "t.tar.gz", out)
    assert (out / "bin" / "alias").is_symlink()
    assert (out / "bin" / "tool").stat().st_mode & 0o111
    assert (out / "lib" / "data.txt").read_text() == "data\n"


def test_unpack_refuses_paths_outside_dest(tmp_path):
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        ti = tarfile.TarInfo("../escape")
        ti.size = 1
        tar.addfile(ti, io.BytesIO(b"x"))
    with pytest.raises(tarfile.TarError):
        qqtc.unpack(evil, tmp_path / "dest")
    assert not (tmp_path / "escape").exists()


# --- build, smoke, consumers -----------------------------------------------------------------


def test_build_then_smoke(spec_dir, tmp_path, fake_download):
    record = qqtc.build("demo", tmp_path / "out", spec_dir)
    tarball = tmp_path / "out" / "demo-1.2.3-r1-linux-x86_64.tar.gz"
    assert fake_download == ["https://example.invalid/demo-1.2.3.tar.gz"]
    assert record["layer_sha256"] == qqtc.sha256_file(tarball)
    assert json.loads((tmp_path / "out" / "demo.record.json").read_text()) == record

    root = tmp_path / "root"
    qqtc.unpack(tarball, root)
    qqtc.smoke("demo", root, spec_dir)
    assert (root / "share" / "source").read_bytes() == PAYLOAD


def test_build_twice_gives_same_layer(spec_dir, tmp_path, fake_download):
    first = qqtc.build("demo", tmp_path / "one", spec_dir)
    second = qqtc.build("demo", tmp_path / "two", spec_dir)
    assert first["layer_sha256"] == second["layer_sha256"]


def test_smoke_failure_is_reported(spec_dir, tmp_path, fake_download):
    qqtc.build("demo", tmp_path / "out", spec_dir)
    root = tmp_path / "root"
    qqtc.unpack(tmp_path / "out" / "demo-1.2.3-r1-linux-x86_64.tar.gz", root)
    (root / "bin" / "demo").write_text("#!/bin/sh\necho wrong\n")
    with pytest.raises(qqtc.subprocess.CalledProcessError):
        qqtc.smoke("demo", root, spec_dir)


def test_consumers_use_the_toolchain_on_path(spec_dir, tmp_path, fake_download, monkeypatch):
    # A local git repo stands in for the consumer. Validation only allows https repos, so the
    # test hands consumers() an already-loaded spec.
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    run = lambda *a: qqtc.subprocess.run(["git", *a], cwd=upstream, check=True, capture_output=True, text=True)
    run("init", "-q")
    (upstream / "README").write_text("consumer\n")
    run("add", "README")
    run("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "c")
    sha = run("rev-parse", "HEAD").stdout.strip()

    qqtc.build("demo", tmp_path / "out", spec_dir)
    root = tmp_path / "root"
    qqtc.unpack(tmp_path / "out" / "demo-1.2.3-r1-linux-x86_64.tar.gz", root)

    spec = qqtc.load_spec("demo", spec_dir)
    spec["consumer"] = [{"repo": upstream.as_uri(), "commit": sha,
                         "commands": ["demo > used.txt", "test -f README"]}]
    monkeypatch.setattr(qqtc, "load_spec", lambda name, spec_dir=None: spec)
    qqtc.consumers("demo", root, tmp_path / "work", spec_dir)
    assert (tmp_path / "work" / "upstream" / "used.txt").read_text() == "demo 1.2.3\n"


def test_cli_validate_passes_on_repo(capsys):
    assert qqtc.main(["validate"]) == 0
    assert "PASS" in capsys.readouterr().out
