#!/usr/bin/env bash
# Assemble Node.js and pnpm into $QQ_PREFIX. Inputs as in toolchains/python/build.sh.
set -euo pipefail

tar -xJf "$QQ_SOURCES/node-v$QQ_VERSION-linux-x64.tar.xz" -C "$QQ_PREFIX" --strip-components=1 --no-same-owner

# pnpm goes where `npm install -g` would put it, with the same relative bin links.
pnpm_dir="$QQ_PREFIX/lib/node_modules/pnpm"
mkdir -p "$pnpm_dir"
tar -xzf "$QQ_SOURCES"/pnpm-*.tgz -C "$pnpm_dir" --strip-components=1 --no-same-owner
ln -s ../lib/node_modules/pnpm/bin/pnpm.cjs "$QQ_PREFIX/bin/pnpm"
ln -s ../lib/node_modules/pnpm/bin/pnpx.cjs "$QQ_PREFIX/bin/pnpx"

# Corepack would let a repo swap in another pnpm at run time, bypassing the pin.
rm -f "$QQ_PREFIX/bin/corepack"
rm -rf "$QQ_PREFIX/lib/node_modules/corepack"
