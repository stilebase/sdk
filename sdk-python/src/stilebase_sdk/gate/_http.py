"""The HTTP client settings both gates share (the core stays free of httpx)."""

from __future__ import annotations

import httpx

# How long an idle connection to Stilebase is kept (httpx's default is 5 s): a new one costs a TLS handshake, which from
# far away can take more than a decision's whole budget. Stilebase's server keeps them longer.
KEEPALIVE = 60.0
# A decision cut off by its budget isn't cancelled: it runs on, at most this long, so the connection it was opening is
# there for the next request (cancelling it would make every call after an idle spell start, and lose, a new handshake).
KEEP_CONNECTING = 10.0
# The connections each gate keeps to Stilebase: enough not to queue under load, bounded so a flood can't exhaust the
# host. A wait for a free one that runs out (PoolTimeout) is the gate's own, never counted against Stilebase.
LIMITS = httpx.Limits(max_connections=1000, max_keepalive_connections=100, keepalive_expiry=KEEPALIVE)
POOL_MARGIN = 0.05  # seconds: a wait for a connection gives up this much before the call's whole budget


def kept(seconds: float) -> httpx.Timeout:
    """httpx's timeouts for a decision the gate waits `seconds` for but lets run on (KEEP_CONNECTING) once it stops
    waiting; the wait for a pool connection is still the budget's, as in `budget`."""
    return httpx.Timeout(max(seconds, KEEP_CONNECTING), pool=max(seconds - POOL_MARGIN, seconds / 2))


def budget(seconds: float) -> httpx.Timeout:
    """httpx's timeouts for a call with this budget. The wait for a pool connection ends first (a PoolTimeout: it never
    reached Stilebase, so it counts nothing), before the overall deadline could: a queued request is never mistaken for
    a slow Stilebase."""
    return httpx.Timeout(seconds, pool=max(seconds - POOL_MARGIN, seconds / 2))
