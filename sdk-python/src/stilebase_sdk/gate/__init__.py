"""stilebase-sdk: AI agents pay per request with x402 (USDC on Base), settled through Stilebase; people pass.

Adapters: `stilebase_sdk.gate.asgi` (FastAPI, Starlette…), `stilebase_sdk.gate.wsgi` (Flask…), `stilebase_sdk.gate.django`. For
anything else, `AsyncGate` / `SyncGate`: `check(GateRequest(...))` returns `Pass` or `Respond`.
"""

from .._version import VERSION
from ._async import AsyncGate
from ._core import DEFAULT_API_URL, Decision, GateError, GateOptions, Pass, Respond
from ._detect import GateConfig, GateRequest, headers_of, normalize_ip
from ._signing import parse_site_key, pass_token, signed_headers
from ._sync import SyncGate

__all__ = [
    "DEFAULT_API_URL",
    "VERSION",
    "AsyncGate",
    "Decision",
    "GateConfig",
    "GateError",
    "GateOptions",
    "GateRequest",
    "Pass",
    "Respond",
    "SyncGate",
    "headers_of",
    "normalize_ip",
    "parse_site_key",
    "pass_token",
    "signed_headers",
]
