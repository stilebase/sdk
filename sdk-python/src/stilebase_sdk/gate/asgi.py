"""ASGI middleware (FastAPI, Starlette, Litestar, Quart…).

from stilebase_sdk.gate.asgi import GateMiddleware
app.add_middleware(GateMiddleware)          # FastAPI / Starlette; the key comes from STILEBASE_KEY
app = GateMiddleware(app)                   # any ASGI app
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, MutableMapping
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

from ._async import AsyncGate
from ._core import GateOptions, Respond, address_from
from ._detect import GateRequest, headers_of

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
        catch_http_clients: bool = True,
        catch_hidden_agents: bool = True,
        check_browsers: bool = True,
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
            catch_http_clients=catch_http_clients,
            catch_hidden_agents=catch_hidden_agents,
            check_browsers=check_browsers,
            on_error=on_error,
            transport=transport,
        )
        self.gate = AsyncGate(options, adapter="asgi")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        decision = await self.gate.check(self.request(scope))
        if isinstance(decision, Respond):
            headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in decision.headers.items()]
            headers.append((b"content-length", str(len(decision.body)).encode()))
            reason = HTTPStatus(decision.status).phrase
            await send({"type": "http.response.start", "status": decision.status, "headers": headers, "reason": reason})
            await send({"type": "http.response.body", "body": decision.body})
            return
        receipt = decision.payment_response
        if not receipt:
            await self.app(scope, receive, send)
            return

        async def with_receipt(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), (b"payment-response", receipt.encode())]}
            await send(message)

        await self.app(scope, receive, with_receipt)

    def request(self, scope: Scope) -> GateRequest:
        headers = headers_of((k.decode("latin-1"), v.decode("latin-1")) for k, v in scope.get("headers", []))
        client = scope.get("client")
        try:
            ip = self.client_ip(scope) if self.client_ip else address_from(headers, self.ip_header, client[0] if client else None)
            if inspect.isawaitable(ip):  # an async client_ip: no address (a sync function is what it takes)
                if inspect.iscoroutine(ip):
                    ip.close()
                elif isinstance(ip, asyncio.Future):
                    ip.add_done_callback(lambda done: done.cancelled() or done.exception())  # retrieved, never logged
                ip = None
        except Exception:
            ip = None  # a client_ip that raises: no address (agents are still asked to pay by user agent)
        raw = scope.get("raw_path")
        path = (
            raw.decode("latin-1")
            if isinstance(raw, bytes) and raw
            else quote(scope.get("root_path", "") + scope.get("path", "/"))
        )
        query = scope.get("query_string", b"").decode("latin-1")
        url = f"{scope.get('scheme', 'http')}://localhost{path}{'?' + query if query else ''}"
        return GateRequest(scope.get("method", "GET"), url, headers, ip=ip)
