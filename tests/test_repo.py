"""Repo-shape checks that hold from the first commit."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_codeowners_owns_every_gate_path():
    # suraj names the owners (V0-ORG-02); agents must not change them.
    rules = {}
    for line in (ROOT / ".github" / "CODEOWNERS").read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            path, *owners = line.split()
            rules[path] = owners
    assert "*" not in rules  # code-owner review is required, so a catch-all would gate every PR
    for path in ("/.github/", "/tools/", "/toolchains/", "/toolchains.toml", "/promoted.toml"):
        assert rules.get(path) == ["@sharmasuraj0123"], path
