"""WSGI middleware (Flask, Pyramid, Bottle, any WSGI app; Django has its own, stilebase_sdk.gate.django).

from stilebase_sdk.gate.wsgi import GateMiddleware
app.wsgi_app = GateMiddleware(app.wsgi_app)     # Flask; the key comes from STILEBASE_KEY
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from http import HTTPStatus
from typing import Any, Literal
from urllib.parse import quote

from ._core import GateOptions, Respond, address_from
from ._detect import GateRequest, headers_of, request_url
from ._sync import SyncGate

StartResponse = Callable[..., Any]
WSGIApp = Callable[[dict[str, Any], StartResponse], Iterable[bytes]]


class GateMiddleware:
    """AI agents pay per request with x402, settled through Stilebase; everyone else reaches your app.

    `ip_header`: read the visitor's address from this header (one your proxy or CDN sets, e.g. "cf-connecting-ip").
    Default: REMOTE_ADDR (behind a proxy, Werkzeug's ProxyFix makes it the visitor's). Never a header anyone can set.
    `client_ip`: or work it out from the environ yourself. The other keywords: GateOptions.
    """

    def __init__(
        self,
        app: WSGIApp,
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
        client_ip: Callable[[dict[str, Any]], str | None] | None = None,
        transport: Any = None,
    ) -> None:
        # Explicit keywords, not **options: the same as the ASGI middleware, and checked by type checkers.
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
        self.gate = SyncGate(options, adapter="wsgi")

    def __call__(self, environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        decision = self.gate.check(self.request(environ))
        if isinstance(decision, Respond):
            status = f"{decision.status} {HTTPStatus(decision.status).phrase}"
            headers = [*decision.headers.items(), ("Content-Length", str(len(decision.body)))]
            start_response(status, headers)
            return [decision.body]
        paid = decision.payment_response
        if not paid:
            return self.app(environ, start_response)

        def with_payment_response(status: str, headers: list[tuple[str, str]], *rest: Any) -> Any:
            return start_response(status, [*headers, ("PAYMENT-RESPONSE", paid)], *rest)

        return self.app(environ, with_payment_response)

    def request(self, environ: dict[str, Any]) -> GateRequest:
        return request_from_environ(environ, self.ip_header, self.client_ip)


PATH_SAFE = "/:@!$&'()*+,;=-._~"  # what stays as it is when a decoded path is percent-encoded again


def request_from_environ(
    environ: dict[str, Any],
    ip_header: str | None = None,
    client_ip: Callable[[dict[str, Any]], str | None] | None = None,
    *,
    scheme: str | None = None,
    path: str | None = None,
) -> GateRequest:
    """The request from a WSGI environ. `scheme` and `path` (percent-encoded) override environ's, for Django."""
    pairs = [(k[5:].replace("_", "-"), str(v)) for k, v in environ.items() if k.startswith("HTTP_")]
    for key, name in (("CONTENT_TYPE", "content-type"), ("CONTENT_LENGTH", "content-length")):
        if environ.get(key):
            pairs.append((name, str(environ[key])))
    headers = headers_of(pairs)
    try:
        ip = client_ip(environ) if client_ip is not None else address_from(headers, ip_header, environ.get("REMOTE_ADDR"))
    except Exception:
        ip = None  # a client_ip that raises: no address (agents are still asked to pay by user agent)
    host = headers.get("host") or f"{environ.get('SERVER_NAME', 'localhost')}:{environ.get('SERVER_PORT', '80')}"
    if path is None:
        # PATH_INFO is the path's bytes decoded as latin-1 (PEP 3333): encode those same bytes again.
        decoded = environ.get("SCRIPT_NAME", "") + environ.get("PATH_INFO", "/")
        try:
            raw = decoded.encode("latin-1")
        except UnicodeEncodeError:
            raw = decoded.encode()  # a server that went beyond PEP 3333
        path = quote(raw, safe=PATH_SAFE)
    url = request_url(scheme or environ.get("wsgi.url_scheme", "http"), host, path, environ.get("QUERY_STRING", ""))
    return GateRequest(method=environ.get("REQUEST_METHOD", "GET"), url=url, headers=headers, ip=ip)
