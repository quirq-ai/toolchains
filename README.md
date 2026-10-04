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

Plan and all v0 items: [quirq-ai/infra-config](https://github.com/quirq-ai/infra-config),
`docs/plan.md` and `docs/v0.md`.

## v0 status

| Item | What | PR | State |
|---|---|---|---|
| V0-TCH-01 | Python toolchain (CPython 3.14.x) | | not started |
| V0-TCH-02 | Node.js 24 LTS and pnpm toolchain | | not started |
| V0-TCH-03 | Staging and promotion | | not started |

## Working here

See [AGENTS.md](AGENTS.md). Run the checks with `python -m pytest`.
