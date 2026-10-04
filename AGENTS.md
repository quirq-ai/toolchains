# Agent guide

How an agent changes this repo safely. Read `README.md` first.

- Every change is a pull request, titled with its work item id (for example `V0-TCH-01: ...`).
  It lands only when the `ci` and `promotion-gate` checks are green on the merge result.
- Toolchains are referenced only by digest. Never point a consumer at a tag.
- Promoting a toolchain is a reviewed PR that edits `promoted.toml` and nothing else except
  `README.md`; `promotion-gate` refuses anything more. Change the gate (`.github/`, `tools/`,
  `toolchains/`) in its own PR. Never promote a digest that CI did not build from this repo.
- Leave `.github/CODEOWNERS` and any `owners` list empty; suraj assigns people.
- Mark a decision you cannot make with a one-line `TODO(suraj):` or `TODO(expert):`.
- This repo is public: no secrets, tokens or internal hostnames.
- GitHub-specific code stays behind a `backend` field (`github` now, `launchpad` later).
- Use other qq repos by pinned commit, never by copying their code.
