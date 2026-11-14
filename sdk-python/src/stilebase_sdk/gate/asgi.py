"""ASGI middleware (FastAPI, Starlette, Litestar, Quart…).

from stilebase_sdk.gate.asgi import GateMiddleware
app.add_middleware(GateMiddleware)          # FastAPI / Starlette; the key comes from STILEBASE_KEY
app = GateMiddleware(app)                   # any ASGI app
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, MutableMapping, Sequence
from http import HTTPStatus
from typing import Any, Literal
from urllib.parse import quote

from ._async import AsyncGate
from ._core import GateOptions, Respond, address_from
from ._detect import PATH_SAFE, GateRequest, encode_path, encode_query, headers_of, request_url

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
# Loose on purpose: frameworks type their apps their own way (Starlette's add_middleware must accept this class).
ASGIApp = Callable[[Any, Any, Any], Awaitable[None]]


class GateMiddleware:
    """AI agents pay per request with x402, settled through Stilebase; everyone else reaches your app.

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
        mode: Literal["live", "test"] = "live",
        free: Sequence[str] | None = None,
        catch_http_clients: bool = True,
        catch_hidden_agents: bool = True,
        check_browsers: bool = True,
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
            key=key,
            api_url=api_url,
            mode=mode,
            free=free,
            catch_http_clients=catch_http_clients,
            catch_hidden_agents=catch_hidden_agents,
            check_browsers=check_browsers,
            assume_https=assume_https,
            on_error=on_error,
            transport=transport,
        )
        self.gate = AsyncGate(options, adapter="asgi")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, self._closing_at_shutdown(receive), send)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        try:
            request: GateRequest | None = self.request(scope)
        except Exception as error:  # a scope this can't read (a nonconforming server): the request goes through
            self.gate.report(error)
            request = None
        if request is None:  # the app runs outside the except: its own errors are never chained to the gate's
            await self.app(scope, receive, send)
            return
        decision = await self.gate.check(request)
        if isinstance(decision, Respond):
            reason = HTTPStatus(decision.status).phrase
            headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in decision.headers.items()]
            headers.append((b"content-length", str(len(decision.body)).encode()))
            await send({"type": "http.response.start", "status": decision.status, "headers": headers, "reason": reason})
            await send({"type": "http.response.body", "body": decision.body})
            return
        paid = decision.payment_response
        if not paid:
            await self.app(scope, receive, send)
            return

        async def with_payment_response(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), (b"payment-response", paid.encode("latin-1"))]}
            await send(message)

        await self.app(scope, receive, with_payment_response)

    def _closing_at_shutdown(self, receive: Receive) -> Receive:
        """The app's lifespan messages, unchanged; at shutdown the gate first sends its pending reports and closes."""

        async def wrapped() -> Message:
            message = await receive()
            if message.get("type") == "lifespan.shutdown":
                try:
                    await asyncio.wait_for(self.gate.aclose(), timeout=5)
                except Exception:  # noqa: S110 - shutting down: nothing left to tell
                    pass
            return message

        return wrapped

    def request(self, scope: Scope) -> GateRequest:
        headers = headers_of((k.decode("latin-1"), v.decode("latin-1")) for k, v in scope.get("headers", []))
        client = scope.get("client")
        if self.client_ip is not None:
            try:
                ip = self.client_ip(scope)
            except Exception:
                ip = None  # a client_ip that raises: no address (agents are still asked to pay by user agent)
        else:
            ip = address_from(headers, self.ip_header, client[0] if client else None)
        server = scope.get("server")
        host = headers.get("host") or (f"{server[0]}:{server[1]}" if server else None)
        query = encode_query(bytes(scope.get("query_string") or b""))
        url = request_url(scope.get("scheme", "http"), host, visitor_path(scope), query)
        return GateRequest(method=scope.get("method", "GET"), url=url, headers=headers, ip=ip)


def visitor_path(scope: Scope) -> str:
    """The path as the visitor sent it, percent-encoded, from the site's root: raw_path when the server gives it, else
    the decoded path encoded again; root_path in front unless it's in already (uvicorn puts it in both, Daphne and
    Hypercorn in neither), by Starlette's rule: it's in when the path is the root or goes on from it with a "/"."""
    raw = scope.get("raw_path")
    if isinstance(raw, (bytes, bytearray)) and raw:
        # As sent, but percent-encoded where a URL must be (a raw byte over 0x7F, a space, `"`, `<`…), as JavaScript's
        # new URL writes it; the visitor's own %XX stay.
        path = quote(bytes(raw), safe=PATH_SAFE + "%")
    else:
        path = quote(encode_path(scope.get("path") or "/"), safe=PATH_SAFE)
    prefix = quote(encode_path(scope.get("root_path") or ""), safe=PATH_SAFE)
    if not prefix:
        return path
    mount = prefix.rstrip("/")
    if path == prefix or (path.startswith(prefix) and path[len(prefix)] == "/"):
        return (mount + path[len(prefix) :]) or "/"
    return mount + path
