"""stilebase-sdk's gate: forward each request to Stilebase, do what it says, let everything through when anything goes
wrong (SPEC.md). AI agents pay per request with x402 (USDC on Base); people pass.

Adapters: `stilebase_sdk.gate.asgi` (FastAPI, Starlette…), `stilebase_sdk.gate.wsgi` (Flask…), `stilebase_sdk.gate.django`.
For anything else, `AsyncGate` / `SyncGate`: `check(GateRequest(...))` returns `Pass` or `Respond`; a `Pass` may ask to
`hold` the site's answer (then `settle(...)`) or to `capture` a copy of it (then `capture(...)`).
"""

from .._version import VERSION
from ._async import AsyncGate
from ._core import DEFAULT_API_URL, Capture, Copy, Decision, GateError, GateOptions, Hold, Pass, Respond, Settings
from ._request import GateRequest, headers_of, normalize_ip
from ._signing import parse_site_key, signed_headers
from ._sync import SyncGate

__all__ = [
    "DEFAULT_API_URL",
    "VERSION",
    "AsyncGate",
    "Capture",
    "Copy",
    "Decision",
    "GateError",
    "GateOptions",
    "GateRequest",
    "Hold",
    "Pass",
    "Respond",
    "Settings",
    "SyncGate",
    "headers_of",
    "normalize_ip",
    "parse_site_key",
    "signed_headers",
]
