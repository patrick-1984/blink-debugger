#!/bin/sh
set -eu
version=$(python3 -c 'from bdbg import __version__; print(__version__)')
export SOURCE_DATE_EPOCH
SOURCE_DATE_EPOCH=$(git log -1 --format=%ct)
mkdir -p dist
python3 -m pip wheel --no-deps --wheel-dir dist .
archive="blink-debugger-$version.zip"
wheel="blink_debugger-$version-py3-none-any.whl"
git archive --format=zip -o "dist/$archive" HEAD
(cd dist && sha256sum "$archive" "$wheel" > SHA256SUMS)
