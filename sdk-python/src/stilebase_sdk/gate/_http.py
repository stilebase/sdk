"""The HTTP client settings both gates share (the core stays free of httpx)."""

from __future__ import annotations

import httpx

# The connections each gate keeps to Stilebase: enough not to queue under load, bounded so a flood can't exhaust the
# host. A wait for a free one that runs out (PoolTimeout) is the gate's own, never counted against Stilebase.
LIMITS = httpx.Limits(max_connections=1000, max_keepalive_connections=100)
POOL_MARGIN = 0.05  # seconds: a wait for a connection gives up this much before the call's whole budget


def budget(seconds: float) -> httpx.Timeout:
    """httpx's timeouts for a call with this budget. The wait for a pool connection ends first (a PoolTimeout: it never
    reached Stilebase, so it counts nothing), before the overall deadline could: a queued request is never mistaken for
    a slow Stilebase."""
    return httpx.Timeout(seconds, pool=max(seconds - POOL_MARGIN, seconds / 2))
