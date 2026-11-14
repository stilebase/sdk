"""Django middleware.

    # settings.py
    MIDDLEWARE = ["stilebase_sdk.gate.django.GateMiddleware", ...]   # first, before anything that renders pages
    STILEBASE_GATE = {"ip_header": "cf-connecting-ip"}           # optional: GateOptions, plus ip_header or client_ip

The key comes from STILEBASE_KEY (the environment, or STILEBASE_GATE["key"]).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.http import HttpResponse

from ._core import GateOptions, Respond
from ._detect import GateRequest
from ._sync import SyncGate
from .wsgi import request_from_environ


class GateMiddleware:
    """AI agents pay per request with x402, settled through Stilebase; everyone else reaches your views.

    The visitor's address is REMOTE_ADDR, unless STILEBASE_GATE["ip_header"] names a header your proxy or CDN sets
    (e.g. "cf-connecting-ip"). Never a header anyone can set."""

    def __init__(self, get_response: Callable[[Any], Any]) -> None:
        self.get_response = get_response
        options = dict(getattr(settings, "STILEBASE_GATE", {}))
        self.ip_header: str | None = options.pop("ip_header", None)
        self.client_ip: Callable[[Any], str | None] | None = options.pop("client_ip", None)  # given the Django request
        self.gate = SyncGate(GateOptions(**options), adapter="django")

    def __call__(self, request: Any) -> Any:
        decision = self.gate.check(self.request(request))
        if isinstance(decision, Respond):
            response = HttpResponse(decision.body, status=decision.status)
            for name, value in decision.headers.items():
                response[name] = value
            return response
        response = self.get_response(request)
        if decision.payment_response:
            response["PAYMENT-RESPONSE"] = decision.payment_response
        return response

    def request(self, request: Any) -> GateRequest:
        """The request as the WSGI adapter reads it, from META: the path as Django routes it (PATH_INFO), never resolved.
        build_absolute_uri() resolves "/premium/x/../../healthz" to "/healthz", a free path, while Django routes the
        premium page."""
        try:
            scheme = request.scheme  # honours SECURE_PROXY_SSL_HEADER
        except Exception:
            scheme = "http"
        environ = {**request.META, "wsgi.url_scheme": scheme}
        client_ip = self.client_ip
        return request_from_environ(environ, self.ip_header, (lambda _environ: client_ip(request)) if client_ip else None)
