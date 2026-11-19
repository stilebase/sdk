"""ASGI middleware (FastAPI, Starlette, Litestar, Quart…).

from stilebase_sdk.gate.asgi import GateMiddleware
app.add_middleware(GateMiddleware)          # FastAPI / Starlette; the key comes from STILEBASE_KEY
app = GateMiddleware(app)                   # any ASGI app
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

from ._async import AsyncGate
from ._core import Capture, Copy, GateOptions, Pass, Respond, too_large
from ._request import GateRequest, address_from, headers_of

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
# Loose on purpose: frameworks type their apps their own way (Starlette's add_middleware must accept this class).
ASGIApp = Callable[[Any, Any, Any], Awaitable[None]]
BODILESS = frozenset({204, 205, 304})
# Server features that send a body around `send`: off for a held request, so the gate sees the whole body.
SIDE_SENDS = ("http.response.pathsend", "http.response.zerocopysend")


class _TooLarge(Exception):
    """Raised into an app whose paid answer outgrew what the gate holds: it stops being read."""


def _without_side_sends(scope: Scope) -> Scope:
    extensions = scope.get("extensions")
    if not isinstance(extensions, Mapping) or not any(name in extensions for name in SIDE_SENDS):
        return scope
    return {**scope, "extensions": {k: v for k, v in extensions.items() if k not in SIDE_SENDS}}


class GateMiddleware:
    """Forwards each request to Stilebase and does what it says: AI agents pay per request with x402, everyone else
    reaches your app.

    `ip_header`: read the visitor's address from this header (one your proxy or CDN sets, e.g. "cf-connecting-ip").
    Default: the connection's address (behind a proxy, run uvicorn with --proxy-headers so it's the visitor's). Never a
    header anyone can set. `client_ip`: or work it out from the scope yourself. The other keywords: GateOptions.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        key: str | None = None,
        api_url: str | None = None,
        disabled: bool | None = None,
        assume_https: bool = True,
        on_error: Callable[[Exception], None] | None = None,
        ip_header: str | None = None,
        client_ip: Callable[[Scope], str | None] | None = None,
        transport: Any = None,
    ) -> None:
        # Explicit keywords, not **options: type checkers then accept the class in add_middleware (Starlette).
        self.app = app
        self.ip_header = ip_header
        self.client_ip = client_ip
        options = GateOptions(
            key=key, api_url=api_url, disabled=disabled, assume_https=assume_https, on_error=on_error, transport=transport
        )
        self.gate = AsyncGate(options, adapter="asgi")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        decision = await self.gate.check(self.request(scope))
        if isinstance(decision, Respond):
            await _send_response(send, decision)
            return
        if decision.hold is not None:
            await self._held(scope, receive, send, decision)
            return
        if not decision.response_headers and decision.capture is None:
            await self.app(scope, receive, send)
            return
        await self.app(scope, receive, self._passing(send, decision))

    def _passing(self, send: Send, decision: Pass) -> Send:
        """`send`, adding the answer's headers and copying the site's answer as it goes out (never delaying it)."""
        added = _raw(decision.response_headers)
        order = decision.capture
        copy = Copy(order.max_bytes) if order is not None else None
        started = time.monotonic()
        seen: dict[str, Any] = {}

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                if added:
                    message = {**message, "headers": [*message.get("headers", []), *added]}
                seen["status"], seen["headers"] = message.get("status", 200), message.get("headers", [])
                seen["ttfb"] = int((time.monotonic() - started) * 1000)
            elif message["type"] == "http.response.body" and order is not None and copy is not None and "status" in seen:
                try:
                    copy.add(bytes(message.get("body", b"")))
                    if not message.get("more_body", False) and not seen.get("sent"):
                        seen["sent"] = True
                        self.gate.capture(order, int(seen["status"]), _text(seen["headers"]), copy, seen["ttfb"])
                except Exception as error:
                    self.gate.report(error)
            await send(message)

        return wrapped

    async def _held(self, scope: Scope, receive: Receive, send: Send, decision: Pass) -> None:
        """A paid request: the site's answer is held, settled, and only then sent (SPEC.md, section 7)."""
        hold = decision.hold
        assert hold is not None
        started = time.monotonic()
        held: dict[str, Any] = {"parts": [], "size": 0, "over": False}

        async def holding(message: Message) -> None:
            if held["over"]:
                raise _TooLarge  # stop the app: what it sends next is never read
            if message["type"] == "http.response.start":
                held["start"] = message
                held["ttfb"] = int((time.monotonic() - started) * 1000)
            elif message["type"] == "http.response.body":
                chunk = bytes(message.get("body", b""))
                held["size"] += len(chunk)
                if held["size"] > hold.max_bytes:
                    held["over"], held["parts"] = True, []  # too large to be paid for: nothing more is read
                    raise _TooLarge
                held["parts"].append(chunk)

        try:
            await self.app(_without_side_sends(scope), receive, holding)
        except Exception:
            if not held["over"]:
                raise  # the app failed: nothing was settled, nothing sent
        if held["over"]:
            await _send_response(send, too_large())
            return
        start = held.get("start")
        if start is None:
            return
        status = int(start.get("status", 200))
        body = b"".join(held["parts"])
        outcome = await self.gate.settle(hold, status, body)
        if isinstance(outcome, Respond):
            await _send_response(send, outcome)
            return
        headers = [*start.get("headers", []), *_raw(decision.response_headers), *_raw(outcome.response_headers)]
        await send({**start, "headers": headers})
        # A status that can't carry a body is sent without one.
        await send({"type": "http.response.body", "body": b"" if status in BODILESS else body, "more_body": False})
        if decision.capture is not None:
            _capture_whole(self.gate, decision.capture, status, _text(headers), body, held["ttfb"])

    def request(self, scope: Scope) -> GateRequest:
        pairs = tuple((k.decode("latin-1"), v.decode("latin-1")) for k, v in scope.get("headers", []))
        client = scope.get("client")
        try:
            fallback = client[0] if client else None
            ip = self.client_ip(scope) if self.client_ip else address_from(headers_of(pairs), self.ip_header, fallback)
            if inspect.isawaitable(ip):  # an async client_ip: no address (a sync function is what it takes)
                if inspect.iscoroutine(ip):
                    ip.close()
                elif isinstance(ip, asyncio.Future):
                    ip.add_done_callback(lambda done: done.cancelled() or done.exception())  # retrieved, never logged
                ip = None
        except Exception:
            ip = None  # a client_ip that raises: no address
        raw = scope.get("raw_path")
        if isinstance(raw, bytes) and raw:
            path = raw.decode("latin-1")
        else:
            path = quote(scope.get("root_path", "") + scope.get("path", "/"))
        query = scope.get("query_string", b"").decode("latin-1")
        target = f"{path}?{query}" if query else path
        return GateRequest(scope.get("method", "GET"), target, pairs, scheme=scope.get("scheme", "http"), ip=ip)


def _raw(headers: Mapping[str, str]) -> list[tuple[bytes, bytes]]:
    return [(k.lower().encode("latin-1", "replace"), v.encode("latin-1", "replace")) for k, v in headers.items()]


def _text(headers: Any) -> list[tuple[str, str]]:
    return [(bytes(k).decode("latin-1"), bytes(v).decode("latin-1")) for k, v in headers]


def _capture_whole(gate: AsyncGate, order: Capture, status: int, headers: list[tuple[str, str]], body: bytes, ttfb: int) -> None:
    try:
        copy = Copy(order.max_bytes)
        copy.add(body)
        gate.capture(order, status, headers, copy, ttfb)
    except Exception as error:
        gate.report(error)


async def _send_response(send: Send, decision: Respond) -> None:
    headers = [(k.lower().encode("latin-1", "replace"), v.encode("latin-1", "replace")) for k, v in decision.headers.items()]
    headers.append((b"content-length", str(len(decision.body)).encode()))
    try:
        reason = HTTPStatus(decision.status).phrase
    except ValueError:
        reason = ""
    await send({"type": "http.response.start", "status": decision.status, "headers": headers, "reason": reason})
    await send({"type": "http.response.body", "body": decision.body})
