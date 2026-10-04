"""Repo-shape checks that hold from the first commit."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_codeowners_has_no_owners_yet():
    # suraj assigns owners (V0-ORG-02); agents must not fill this in.
    lines = (ROOT / ".github" / "CODEOWNERS").read_text().splitlines()
    assert all(not line.strip() or line.lstrip().startswith("#") for line in lines)
