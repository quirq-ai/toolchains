#!/usr/bin/env bash
# Assemble Node.js and pnpm into $QQ_PREFIX. Inputs as in toolchains/python/build.sh, plus
# QQ_PLATFORM (the platform being built, always this machine's) and QQ_PYTHON (the Python running
# qqtc, used on Windows to unzip).
set -euo pipefail

pnpm_rel=lib/node_modules/pnpm   # where `npm install -g` would put it on Linux and macOS
case "$QQ_PLATFORM" in
  linux-x86_64 | macos-arm64)
    arch=linux-x64
    [ "$QQ_PLATFORM" = macos-arm64 ] && arch=darwin-arm64
    tar -xJf "$QQ_SOURCES/node-v$QQ_VERSION-$arch.tar.xz" -C "$QQ_PREFIX" --strip-components=1 --no-same-owner
    ;;
  windows-x86_64)
    # The Windows zip keeps node.exe, npm, npx and their .cmd files next to node_modules/; npm.cmd
    # finds node.exe beside itself, so the whole tree goes into bin/ unchanged.
    "$QQ_PYTHON" -I -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' \
      "$QQ_SOURCES/node-v$QQ_VERSION-win-x64.zip" "$QQ_WORK"
    mv "$QQ_WORK/node-v$QQ_VERSION-win-x64" "$QQ_PREFIX/bin"
    pnpm_rel=bin/node_modules/pnpm
    ;;
  *)
    echo "node: no build for $QQ_PLATFORM" >&2
    exit 1
    ;;
esac

# pnpm, unpacked where npm would install it, with the same relative bin entries.
pnpm_dir="$QQ_PREFIX/$pnpm_rel"
mkdir -p "$pnpm_dir"
tar -xzf "$QQ_SOURCES"/pnpm-*.tgz -C "$pnpm_dir" --strip-components=1 --no-same-owner
for cmd in pnpm pnpx; do
  if [ "$QQ_PLATFORM" = windows-x86_64 ]; then
    # Windows has no symlinks without admin rights: a .cmd for cmd and PowerShell, and an sh
    # script for Git Bash, the way npm installs global commands there.
    printf '@"%%~dp0node.exe" "%%~dp0node_modules\\pnpm\\bin\\%s.cjs" %%*\r\n' "$cmd" > "$QQ_PREFIX/bin/$cmd.cmd"
    printf '#!/bin/sh\nexec "$(dirname "$0")/node.exe" "$(dirname "$0")/node_modules/pnpm/bin/%s.cjs" "$@"\n' \
      "$cmd" > "$QQ_PREFIX/bin/$cmd"
  else
    ln -s "../$pnpm_rel/bin/$cmd.cjs" "$QQ_PREFIX/bin/$cmd"
  fi
done

# Corepack would let a repo swap in another pnpm at run time, bypassing the pin.
rm -f "$QQ_PREFIX"/bin/corepack "$QQ_PREFIX"/bin/corepack.cmd
rm -rf "$QQ_PREFIX/lib/node_modules/corepack" "$QQ_PREFIX/bin/node_modules/corepack"
