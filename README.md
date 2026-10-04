# toolchains

Part of **quirq infra** ("qq"), quirq-ai's CI/CD system for repos in any language. This repo builds
the toolchains every qq build uses, publishes them, pins them by digest, and promotes them from
staging by reviewed pull request.

**Chromium counterpart:** CIPD packages built by 3pp, and clang's upload-then-promote flow. A
toolchain is built once by CI, stored by content digest, and a build refers to it only by that
digest, never by a moving version.

## How it works (v0)

- **Transport.** OCI artifacts on `ghcr.io/quirq-ai/toolchains/<name>`, addressed by digest (plan
  decision D6). v0 has no containers, so each artifact is one tarball layer that `qq sync`
  unpacks without a container runtime. Linux x86_64 only.
- **Staging.** A change to a toolchain's spec is built in CI and pushed with a `staging-…` tag.
- **Promotion.** A reviewed PR moves the staged digest into `promoted.toml`. That file is what the
  roller reads to update pins in product repos.

## Adding or moving a toolchain

1. Edit `toolchains/<name>/toolchain.toml` (version, source URL and sha256) and its `build.sh`.
   Open a PR. The `build` check builds it, smoke-tests it, and builds its pinned consumer repos.
2. When it lands, `build` on `main` stages it as `ghcr.io/quirq-ai/toolchains/<name>:staging-…`
   and prints a ready-made record in the run summary. Staging happens before the consumer builds
   run, so a staged digest is promotable only if that run's `consumers` job is green.
3. Promote it in its own PR: `python tools/qqtc.py promote <name> --record staged.json`, which
   rewrites `promoted.toml`. That PR may change only `promoted.toml` and `README.md`. The
   `promotion-gate` check pulls the digest, checks that a staging build of a commit on `main`
   produced exactly those bytes, and re-runs the smoke tests and consumer builds on them. It runs
   main's workflow, `qqtc` and specs, never the PR's (not yet a required check: V0-ORG-03). A reviewer approves; then it merges.
4. The toolchain roller (quirq-ai/rollers, V0-ROL-01) reads `promoted.toml` and opens pin-update
   PRs in product repos.

Plan and all v0 items: [quirq-ai/infra-config](https://github.com/quirq-ai/infra-config),
`docs/plan.md` and `docs/v0.md`.

## v0 status

| Item | What | PR | State |
|---|---|---|---|
| V0-TCH-01 | Python toolchain (CPython 3.14.8) | #2, #5 | Built by CI, promoted by digest; xo-space's suite passes on it in CI (1713 collected; an independent run on 3.14.8 got 1705 passed, 8 skipped) and its route parity check passes. Not done yet: pinning it in xo-space's `infra/repo.toml` waits on xo-space's onboarding (V0-ONB-01), and on the packages below being public. |
| V0-TCH-02 | Node.js 24.21.0 LTS and pnpm 11.28.2 | #3, #5 | Built and promoted by digest; innernet installs, typechecks and builds with it in CI. |
| V0-TCH-03 | Staging and promotion | #4, #5 | Promotion PR #5 passed the promotion gate and landed. "Through the gate" in the merge-queue sense waits on V0-ORG-03 (merge queue and rulesets). |

Promoted pins live in [`promoted.toml`](promoted.toml); the roller (V0-ROL-01) reads them from there.

**Needs suraj (org admin):**
- Make these ghcr packages public: `ghcr.io/quirq-ai/toolchains/python` and
  `ghcr.io/quirq-ai/toolchains/node`. New packages start private, so until then `qq sync`, recipes
  and product-repo CI cannot pull the pins. Set it in each package's settings, under "Change
  visibility". Every new toolchain added here adds a package that needs the same step.
- Make `ci` and `promotion-gate` required checks on `main`, with the merge queue (V0-ORG-03).
  Until then nothing enforces that a promoted pin passed its consumer builds.
- Name owners for `.github/`, `tools/`, `toolchains/` and `promoted.toml`, with required code-owner
  review (V0-ORG-02).

## Working here

See [AGENTS.md](AGENTS.md). Run the checks with `python -m pytest`.
