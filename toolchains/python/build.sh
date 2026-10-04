#!/usr/bin/env bash
# Build CPython into $QQ_PREFIX. Called by `qqtc build` with:
#   QQ_SOURCES  directory holding the verified source tarballs
#   QQ_WORK     empty scratch directory
#   QQ_PREFIX   empty directory that becomes the toolchain root (bin/, lib/, ...)
#   QQ_VERSION  the toolchain version, e.g. 3.14.8
set -euo pipefail

minor="${QQ_VERSION%.*}"
# The configure prefix only matters inside the build: CPython finds its prefix from the
# executable's location at run time, so the unpacked toolchain works from any directory.
build_prefix="/opt/qq/python"

cd "$QQ_WORK"
tar -xf "$QQ_SOURCES/Python-$QQ_VERSION.tar.xz"
cd "Python-$QQ_VERSION"
# TODO(expert): --enable-optimizations (PGO) and --with-lto make a faster interpreter but add
# about 20 minutes per build. Off in v0 so toolchain PRs stay quick.
./configure --prefix="$build_prefix" --with-ensurepip=install
make -j"$(nproc)"
make install DESTDIR="$QQ_WORK/dest"
cp -a "$QQ_WORK/dest$build_prefix/." "$QQ_PREFIX/"

cd "$QQ_PREFIX"
# The test suite is about a third of the install and no build needs it.
rm -rf "lib/python$minor/test" "lib/python$minor/idlelib/idle_test"

# Scripts that pip and the installer wrote point at the build prefix. Rewrite them to find the
# interpreter next to themselves (the same trick pip uses for long shebang lines).
for f in bin/*; do
  [ -f "$f" ] && [ ! -L "$f" ] || continue
  if head -c 64 "$f" | grep -q "^#!$build_prefix/bin/python"; then
    {
      printf '#!/bin/sh\n'
      printf '%s\n' "'''exec' \"\$(dirname -- \"\$(realpath -- \"\$0\")\")/python$minor\" \"\$0\" \"\$@\""
      printf "' '''\n"
      tail -n +2 "$f"
    } > "$f.qq" && chmod --reference="$f" "$f.qq" && mv "$f.qq" "$f"
  fi
done

# Bytecode that does not depend on file timestamps, so it stays valid after unpacking (tar
# mtimes are fixed) and is the same on every build of the same sources.
# PYTHONDONTWRITEBYTECODE keeps compileall's own imports from leaving timestamp .pyc files that
# -f would otherwise race with; the check after it fails the build if any survive.
find "lib/python$minor" -name __pycache__ -type d -prune -exec rm -rf {} +
PYTHONDONTWRITEBYTECODE=1 bin/python3 -m compileall -f -q -j0 --invalidation-mode unchecked-hash "lib/python$minor"
PYTHONDONTWRITEBYTECODE=1 bin/python3 - "lib/python$minor" <<'PY'
import pathlib, sys
bad = [p for p in pathlib.Path(sys.argv[1]).rglob("*.pyc") if p.read_bytes()[4:8] != b"\x01\x00\x00\x00"]
if bad:
    sys.exit(f"{len(bad)} .pyc file(s) are not unchecked-hash, e.g. {bad[0]}")
PY
