#!/usr/bin/env bash
# The package as users get it: the built wheel installed into a clean environment of the oldest supported Python, every
# adapter imported, a keyless gate asked (it must let the request through). Run after `uv build`.
#   scripts/smoke.sh [python-version]     (default 3.10)
set -euo pipefail
cd "$(dirname "$0")/.."
python_version="${1:-3.10}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
uv venv --quiet --python "$python_version" "$work/venv"
py="$work/venv/bin/python"
[ -x "$py" ] || py="$work/venv/Scripts/python.exe"
uv pip install --quiet --python "$py" dist/stilebase_sdk-*.whl django starlette flask
cd "$work"
"$py" -I - <<'PY'
import asyncio, sys
import stilebase_sdk.gate
from stilebase_sdk.gate import AsyncGate, GateOptions, GateRequest, Pass, SyncGate
from stilebase_sdk.gate.asgi import GateMiddleware as Asgi
from stilebase_sdk.gate.wsgi import GateMiddleware as Wsgi
from django.conf import settings
settings.configure()
from stilebase_sdk.gate.django import GateMiddleware as Django  # noqa: F401
quiet = GateOptions(key="", on_error=lambda e: None)
request = GateRequest("GET", "https://a.example/", {"user-agent": "GPTBot"})
assert SyncGate(quiet).check(request) == Pass()
assert asyncio.run(AsyncGate(quiet).check(request)) == Pass()
Asgi(lambda *a: None, key="", on_error=lambda e: None)
Wsgi(lambda *a: [], key="", on_error=lambda e: None)
print("ok", stilebase_sdk.gate.VERSION, "on Python", sys.version.split()[0])
PY
