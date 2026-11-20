"""ASGI middleware (FastAPI, Starlette, Litestar, Quart…).

from stilebase_sdk.gate.asgi import GateMiddleware
app.add_middleware(GateMiddleware)          # FastAPI / Starlette; the key comes from STILEBASE_KEY
app = GateMiddleware(app)                   # any ASGI app
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, MutableMapping
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

from ._async import AsyncGate
from ._core import Copy, GateOptions, Pass, Respond, body_allowed, holdable, merge_headers, not_payable
from ._request import GateRequest, address_from, headers_of

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
# Loose on purpose: frameworks type their apps their own way (Starlette's add_middleware must accept this class).
ASGIApp = Callable[[Any, Any, Any], Awaitable[None]]
# Server features that send a body around `send`: off for a held request, so the gate sees the whole body.
SIDE_SENDS = ("http.response.pathsend", "http.response.zerocopysend")


class _TooLarge(Exception):
    """Raised into an app whose paid answer the gate can't hold (too large, a protocol switch): it stops being read."""


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
        request = self.request(scope)
        decision = await self.gate.check(request)
        if isinstance(decision, Respond):
            if not await _send_gate_answer(send, decision, request.payment is not None, self.gate):
                await self.app(scope, receive, send)  # the gate's answer couldn't be sent, and nothing was paid: the site
            return
        if decision.hold is not None:
            await self._held(scope, receive, send, decision)
            return
        if not decision.response_headers and decision.capture is None:
            await self.app(scope, receive, send)
            return
        await self.app(scope, receive, self._passing(send, decision))

    def _passing(self, send: Send, decision: Pass) -> Send:
        """`send`, with the answer's headers merged in and the site's answer copied as it goes out (never delayed)."""
        order = decision.capture
        copy = Copy(order.max_bytes) if order is not None else None
        started = time.monotonic()
        seen: dict[str, Any] = {}

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                if decision.response_headers:
                    merged = merge_headers(_text(message.get("headers", [])), decision.response_headers)
                    message = {**message, "headers": _raw(merged)}
                seen["status"], seen["headers"] = int(message.get("status", 200)), _text(message.get("headers", []))
                seen["ttfb"] = int((time.monotonic() - started) * 1000)
            elif message["type"] == "http.response.body" and order is not None and copy is not None and "status" in seen:
                try:
                    copy.add(bytes(message.get("body", b"")))
                    if not message.get("more_body", False) and not seen.get("sent"):
                        seen["sent"] = True
                        self.gate.capture(order, seen["status"], seen["headers"], copy, seen["ttfb"])
                except Exception as error:
                    self.gate.report(error)
            await send(message)

        return wrapped

    async def _held(self, scope: Scope, receive: Receive, send: Send, decision: Pass) -> None:
        """A paid request: the site's answer is held, and settled and sent as soon as it's complete (its last body
        message), whatever the app does afterwards (SPEC.md, section 7)."""
        hold = decision.hold
        assert hold is not None
        started = time.monotonic()
        held: dict[str, Any] = {"parts": [], "size": 0, "done": False}

        async def finish(answer: Respond | None) -> None:
            held["done"] = True
            if answer is not None:
                await _send_gate_answer(send, answer, True, self.gate)
                return
            status = int(held["start"].get("status", 200))
            body = b"".join(held["parts"])
            outcome = await self.gate.settle(hold, status, body)
            if isinstance(outcome, Respond):
                await _send_gate_answer(send, outcome, True, self.gate)
                return
            headers = merge_headers(_text(held["start"].get("headers", [])), decision.response_headers, outcome.response_headers)
            await send({**held["start"], "headers": _raw(headers)})
            await send({"type": "http.response.body", "body": body if body_allowed(status) else b"", "more_body": False})
            if decision.capture is not None:
                copy = Copy(decision.capture.max_bytes)
                copy.add(body)
                self.gate.capture(decision.capture, status, headers, copy, held["ttfb"])

        async def holding(message: Message) -> None:
            if held["done"]:
                if held.get("cut"):
                    raise _TooLarge  # stop the app: what it sends next is never read
                return  # the paid answer has gone; what the app sends afterwards isn't the visitor's
            if message["type"] == "http.response.start":
                held["start"] = message
                held["ttfb"] = int((time.monotonic() - started) * 1000)
                if not holdable(int(message.get("status", 200))):  # a protocol switch can't be held or paid for
                    held["cut"] = True
                    await finish(not_payable())
                    raise _TooLarge
            elif message["type"] == "http.response.body" and "start" in held:
                chunk = bytes(message.get("body", b""))
                held["size"] += len(chunk)
                if held["size"] > hold.max_bytes:  # too large to be paid for: nothing more is read
                    held["parts"], held["cut"] = [], True
                    await finish(not_payable())
                    raise _TooLarge
                held["parts"].append(chunk)
                if not message.get("more_body", False):
                    await finish(None)

        try:
            await self.app(_without_side_sends(scope), receive, holding)
        except Exception as error:
            if not held["done"]:
                raise  # the app failed before its answer was complete: nothing was settled, nothing sent
            if not held.get("cut"):
                self.gate.report(error)  # after the paid answer went out: it stays sent
            return
        if not held["done"] and "start" in held:
            await finish(None)  # the app returned without a last body message: what it sent is its answer

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


def _raw(headers: Iterable[tuple[str, str]]) -> list[tuple[bytes, bytes]]:
    return [(k.lower().encode("latin-1", "replace"), v.encode("latin-1", "replace")) for k, v in headers]


def _text(headers: Any) -> list[tuple[str, str]]:
    return [(bytes(k).decode("latin-1"), bytes(v).decode("latin-1")) for k, v in headers]


async def _send_gate_answer(send: Send, decision: Respond, payment: bool, gate: AsyncGate) -> bool:
    """Send an answer the gate made (SPEC.md, section 10). If the server refuses it: False for a request without a
    payment (it goes to the site); a request with a payment gets whatever bare 503 can still be sent, never the page."""
    started = False

    async def tracking(message: Message) -> None:
        nonlocal started
        await send(message)
        started = started or message["type"] == "http.response.start"

    try:
        await _send_response(tracking, decision)
        return True
    except Exception as error:
        gate.report(error)
        if started:
            return True  # the answer began going out: nothing else can be sent
        if not payment:
            return False
        try:
            await send({"type": "http.response.start", "status": 503, "headers": []})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except Exception as again:
            gate.report(again)
        return True


async def _send_response(send: Send, decision: Respond) -> None:
    """An answer built by the gate's helpers (`respond`, `unknown_payment`, `not_payable`), as it is."""
    headers = _raw(decision.headers.items())
    if body_allowed(decision.status):
        headers.append((b"content-length", str(len(decision.body)).encode()))
    try:
        reason = HTTPStatus(decision.status).phrase
    except ValueError:
        reason = ""
    await send({"type": "http.response.start", "status": decision.status, "headers": headers, "reason": reason})
    await send({"type": "http.response.body", "body": decision.body})
