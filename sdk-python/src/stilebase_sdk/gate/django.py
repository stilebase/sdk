"""Django middleware.

    # settings.py
    MIDDLEWARE = ["stilebase_sdk.gate.django.GateMiddleware", ...]   # first, before anything that renders pages
    STILEBASE_GATE = {"ip_header": "cf-connecting-ip"}           # optional: GateOptions' keywords, plus ip_header

The key comes from STILEBASE_KEY (the environment, or STILEBASE_GATE["key"]).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from django.conf import settings
from django.http import HttpResponse

from ._core import Capture, Copy, GateOptions, Pass, Respond, body_allowed, holdable, merge_headers, not_payable
from ._request import GateRequest, address_from, headers_of
from ._sync import SyncGate


class GateMiddleware:
    """Forwards each request to Stilebase and does what it says: AI agents pay per request with x402, everyone else
    reaches your views.

    The visitor's address is REMOTE_ADDR, unless STILEBASE_GATE["ip_header"] names a header your proxy or CDN sets
    (e.g. "cf-connecting-ip"). Never a header anyone can set. An async streaming response (Django's async iterators)
    is never held or copied: a paid one is answered 502, not charged."""

    def __init__(self, get_response: Callable[[Any], Any]) -> None:
        self.get_response = get_response
        options = dict(getattr(settings, "STILEBASE_GATE", {}))
        self.ip_header: str | None = options.pop("ip_header", None)
        self.gate = SyncGate(GateOptions(**options), adapter="django")

    def __call__(self, request: Any) -> Any:
        decision = self.gate.check(self.request(request))
        if isinstance(decision, Respond):
            return _response(decision)
        started = time.monotonic()
        response = self.get_response(request)
        ttfb = int((time.monotonic() - started) * 1000)
        if decision.hold is not None:
            return self._held(response, decision, ttfb)
        _overlay(response, decision.response_headers)
        if decision.capture is not None:
            self._copy(response, decision.capture, ttfb)
        return response

    def _held(self, response: Any, decision: Pass, ttfb: int) -> Any:
        """A paid request: the view's answer is held, settled, and only then sent (SPEC.md, section 7)."""
        hold = decision.hold
        assert hold is not None
        if getattr(response, "is_async", False) or not holdable(response.status_code):
            return _response(not_payable())  # an async stream, or a protocol switch: never held, never settled
        if getattr(response, "streaming", False):
            parts: list[bytes] = []
            size = 0
            stream = iter(response.streaming_content)
            try:
                for chunk in stream:
                    size += len(chunk)
                    if size > hold.max_bytes:
                        parts = []
                        break  # too large to be paid for: nothing more is read
                    parts.append(bytes(chunk))
            finally:
                close = getattr(stream, "close", None)
                if close is not None:
                    close()
                _release(response)
            if size > hold.max_bytes:
                return _response(not_payable())
            body = b"".join(parts)
            held = HttpResponse(body, status=response.status_code, reason=response.reason_phrase)
            for name, value in response.items():
                if name.lower() != "content-length":
                    held[name] = value
            held.cookies = response.cookies  # Set-Cookie lives here, not among the headers
            response = held
        else:
            body = bytes(response.content)
            if len(body) > hold.max_bytes:
                return _response(not_payable())
        outcome = self.gate.settle(hold, response.status_code, body)
        if isinstance(outcome, Respond):
            return _response(outcome)
        if not body_allowed(response.status_code):
            response.content = b""  # a status that can't carry a body is sent without one
        _overlay(response, decision.response_headers, outcome.response_headers)
        if decision.capture is not None:
            self._copy(response, decision.capture, ttfb)
        return response

    def _copy(self, response: Any, order: Capture, ttfb: int) -> None:
        """A copy of the view's answer for Stilebase, never delaying or changing it."""
        try:
            if getattr(response, "is_async", False):
                return
            headers = list(response.items())
            copy = Copy(order.max_bytes)
            if not getattr(response, "streaming", False):
                copy.add(bytes(response.content))
                self.gate.capture(order, response.status_code, headers, copy, ttfb)
                return
            original = response.streaming_content
            gate = self.gate

            def copying() -> Iterator[bytes]:
                try:
                    for chunk in original:
                        try:
                            copy.add(bytes(chunk))
                        except Exception as error:
                            gate.report(error)
                        yield chunk
                finally:
                    gate.capture(order, response.status_code, headers, copy, ttfb)

            response.streaming_content = copying()
        except Exception as error:
            self.gate.report(error)

    def request(self, request: Any) -> GateRequest:
        pairs = tuple((str(k), str(v)) for k, v in request.headers.items())
        ip = address_from(headers_of(pairs), self.ip_header, request.META.get("REMOTE_ADDR"))
        return GateRequest(request.method, request.get_full_path(), pairs, scheme=request.scheme, ip=ip)


def _release(response: Any) -> None:
    """Free what a streamed response holds open (a file, say) without `close()`, which would also send Django's
    request_finished before the request is done: the response sent instead is closed by Django as usual."""
    for closer in list(getattr(response, "_resource_closers", ())):
        try:
            closer()
        except Exception:
            pass
    resource_closers = getattr(response, "_resource_closers", None)
    if isinstance(resource_closers, list):
        resource_closers.clear()


def _overlay(response: Any, *overlays: Mapping[str, str]) -> None:
    """The answer's headers on a view's response: merged by the gate's helper (each replaces its namesake; Django's
    headers compare names case-insensitively, as the helper does)."""
    for name, value in merge_headers([], *overlays):
        response[name] = value


def _response(decision: Respond) -> HttpResponse:
    """An answer built by the gate's helpers (`respond`, `unknown_payment`, `not_payable`), as it is."""
    response = HttpResponse(decision.body, status=decision.status)
    for name, value in decision.headers.items():
        response[name] = value
    return response
