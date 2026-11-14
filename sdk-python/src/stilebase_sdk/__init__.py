"""stilebase-sdk: Stilebase for Python. Each module is its own subpackage:

stilebase_sdk.gate   what a site runs so AI agents pay per request with x402 (USDC on Base), settled through
                     Stilebase: stilebase_sdk.gate.asgi, .wsgi, .django, or AsyncGate / SyncGate for anything else.
"""

from ._version import VERSION

__all__ = ["VERSION"]
