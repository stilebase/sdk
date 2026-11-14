"""Django middleware.

    # settings.py
    MIDDLEWARE = ["stilebase_sdk.gate.django.GateMiddleware", ...]   # first, before anything that renders pages
    STILEBASE_GATE = {"ip_header": "cf-connecting-ip"}           # optional: GateOptions, plus ip_header

The key comes from STILEBASE_KEY (the environment, or STILEBASE_GATE["key"]).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.http import HttpResponse

from ._core import GateOptions, Respond, address_from
from ._detect import GateRequest, headers_of
from ._sync import SyncGate


class GateMiddleware:
    """AI agents pay per request with x402, settled through Stilebase; everyone else reaches your views.

    The visitor's address is REMOTE_ADDR, unless STILEBASE_GATE["ip_header"] names a header your proxy or CDN sets
    (e.g. "cf-connecting-ip"). Never a header anyone can set."""

    def __init__(self, get_response: Callable[[Any], Any]) -> None:
        self.get_response = get_response
        options = dict(getattr(settings, "STILEBASE_GATE", {}))
        self.ip_header: str | None = options.pop("ip_header", None)
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
        headers = headers_of(request.headers.items())
        ip = address_from(headers, self.ip_header, request.META.get("REMOTE_ADDR"))
        try:
            url = request.build_absolute_uri()  # validates the host against ALLOWED_HOSTS
        except Exception:
            url = f"{request.scheme}://localhost{request.get_full_path()}"
        return GateRequest(method=request.method, url=url, headers=headers, ip=ip)
