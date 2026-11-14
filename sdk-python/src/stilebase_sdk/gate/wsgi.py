"""WSGI middleware (Flask, Bottle, Pyramid…).

from stilebase_sdk.gate.wsgi import GateMiddleware
app.wsgi_app = GateMiddleware(app.wsgi_app)   # Flask; the key comes from STILEBASE_KEY
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

from ._core import GateOptions, Respond, address_from
from ._detect import GateRequest, headers_of
from ._sync import SyncGate

StartResponse = Callable[..., Any]
WSGIApp = Callable[[dict[str, Any], StartResponse], Iterable[bytes]]


class GateMiddleware:
    """AI agents pay per request with x402, settled through Stilebase; everyone else reaches your app.

    `ip_header`: read the visitor's address from this header (one your proxy or CDN sets). Default: REMOTE_ADDR (behind
    a proxy, Werkzeug's ProxyFix makes it the visitor's). Never a header anyone can set. `client_ip`: or work it out
    from the environ yourself. The other keywords: GateOptions.
    """

    def __init__(
        self,
        app: WSGIApp,
        *,
        ip_header: str | None = None,
        client_ip: Callable[[dict[str, Any]], str | None] | None = None,
        **options: Any,
    ) -> None:
        self.app = app
        self.ip_header = ip_header
        self.client_ip = client_ip
        self.gate = SyncGate(GateOptions(**options), adapter="wsgi")

    def __call__(self, environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        decision = self.gate.check(request_from_environ(environ, self.ip_header, self.client_ip))
        if isinstance(decision, Respond):
            status = f"{decision.status} {HTTPStatus(decision.status).phrase}"
            start_response(status, [*decision.headers.items(), ("Content-Length", str(len(decision.body)))])
            return [decision.body]
        receipt = decision.payment_response
        if not receipt:
            return self.app(environ, start_response)

        def with_receipt(status: str, headers: list[tuple[str, str]], *args: Any) -> Any:
            return start_response(status, [*headers, ("PAYMENT-RESPONSE", receipt)], *args)

        return self.app(environ, with_receipt)


def request_from_environ(
    environ: dict[str, Any], ip_header: str | None = None, client_ip: Callable[[dict[str, Any]], str | None] | None = None
) -> GateRequest:
    """The request as the gate reads it, from a WSGI environ."""
    pairs = [(k[5:].replace("_", "-"), str(v)) for k, v in environ.items() if k.startswith("HTTP_")]
    headers = headers_of(pairs)
    try:
        ip = client_ip(environ) if client_ip else address_from(headers, ip_header, environ.get("REMOTE_ADDR"))
    except Exception:
        ip = None  # a client_ip that raises: no address (agents are still asked to pay by user agent)
    # PATH_INFO is the path's bytes decoded as latin-1 (PEP 3333): encode those same bytes again.
    decoded = str(environ.get("SCRIPT_NAME") or "") + str(environ.get("PATH_INFO") or "/")
    path = quote(decoded.encode("latin-1", "replace"), safe="/:@!$&'()*+,;=-._~")
    query = str(environ.get("QUERY_STRING") or "")
    url = f"{environ.get('wsgi.url_scheme', 'http')}://localhost{path}{'?' + query if query else ''}"
    return GateRequest(str(environ.get("REQUEST_METHOD", "GET")), url, headers, ip=ip)
