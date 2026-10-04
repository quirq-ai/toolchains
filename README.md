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
   and prints a ready-made record in the run summary.
3. Promote it in its own PR: `python tools/qqtc.py promote <name> --record staged.json`, which
   rewrites `promoted.toml`. The `promotions` job in `ci` pulls the digest, checks that a staging
   build of a commit on `main` produced exactly those bytes, and re-runs the smoke tests and
   consumer builds on them. A reviewer approves; then it merges.
4. The toolchain roller (quirq-ai/rollers, V0-ROL-01) reads `promoted.toml` and opens pin-update
   PRs in product repos.

Plan and all v0 items: [quirq-ai/infra-config](https://github.com/quirq-ai/infra-config),
`docs/plan.md` and `docs/v0.md`.

## v0 status

| Item | What | PR | State |
|---|---|---|---|
| V0-TCH-01 | Python toolchain (CPython 3.14.8) | #2, #5 | Built by CI, promoted by digest; xo-space's tests and route parity pass on it in CI. Pinning it in xo-space's `infra/repo.toml` waits on V0-SYN-01 (manifest schema). |
| V0-TCH-02 | Node.js 24.21.0 LTS and pnpm 11.28.2 | #3, #5 | Built and promoted by digest; innernet installs, typechecks and builds with it in CI. |
| V0-TCH-03 | Staging and promotion | #4, #5 | Promotion PR #5 passed the `promotions` gate and landed. "Through the gate" in the merge-queue sense waits on V0-ORG-03 (merge queue and rulesets). |

Promoted pins live in [`promoted.toml`](promoted.toml); the roller (V0-ROL-01) reads them from there.

## Working here

See [AGENTS.md](AGENTS.md). Run the checks with `python -m pytest`.
