#!/bin/sh
set -eu
version=$(python3 -c 'from bdbg import __version__; print(__version__)')
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
(cd dist && sha256sum -c SHA256SUMS)
python3 -m zipfile -e "dist/blink-debugger-$version.zip" "$work/plugin"
python3 -m venv "$work/runtime"
"$work/runtime/bin/python" -m pip install --no-deps --disable-pip-version-check "dist/blink_debugger-$version-py3-none-any.whl"
"$work/runtime/bin/bdbg" --version
PATH="$work/runtime/bin" "$work/runtime/bin/python" scripts/verify_install.py "$work/plugin" "$work/runtime/bin/bdbg"

peer_wheel=$(python3 -c 'import json; print(json.load(open("compat.json"))["webBlink"]["wheel"])')
peer_hash=$(python3 -c 'import json; print(json.load(open("compat.json"))["webBlink"]["wheelSha256"])')
peer_version=$(python3 -c 'import json; print(json.load(open("compat.json"))["webBlink"]["version"])')
if test -n "${WEB_BLINK_WHEEL:-}"; then
    cp "$WEB_BLINK_WHEEL" "$work/$peer_wheel"
else
    curl --fail --location --silent --show-error \
        "https://github.com/patrick-1984/web-blink/releases/download/v$peer_version/$peer_wheel" \
        -o "$work/$peer_wheel"
fi
printf '%s  %s\n' "$peer_hash" "$work/$peer_wheel" | sha256sum -c
"$work/runtime/bin/python" -m pip install --disable-pip-version-check "$work/$peer_wheel"
"$work/runtime/bin/blink" --version
"$work/runtime/bin/blink" schemas export "$work/peer-schemas"
"$work/runtime/bin/python" scripts/verify_weblink.py "$work/plugin" "$work/peer-schemas"
