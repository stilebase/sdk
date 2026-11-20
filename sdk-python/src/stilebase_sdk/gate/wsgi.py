"""WSGI middleware (Flask, Bottle, Pyramid…).

from stilebase_sdk.gate.wsgi import GateMiddleware
app.wsgi_app = GateMiddleware(app.wsgi_app)   # Flask; the key comes from STILEBASE_KEY
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Iterable, Iterator
from typing import Any
from urllib.parse import quote

from ._core import Capture, Copy, GateOptions, Pass, Respond, body_allowed, holdable, merge_headers, not_payable, reason
from ._request import GateRequest, address_from, headers_of
from ._sync import SyncGate

StartResponse = Callable[..., Any]
WSGIApp = Callable[[dict[str, Any], StartResponse], Iterable[bytes]]


class GateMiddleware:
    """Forwards each request to Stilebase and does what it says: AI agents pay per request with x402, everyone else
    reaches your app.

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
        request = request_from_environ(environ, self.ip_header, self.client_ip)
        decision = self.gate.check(request)
        if isinstance(decision, Respond):
            return self._gate_answer(environ, start_response, decision, request.payment is not None)
        if decision.hold is not None:
            return self._held(environ, start_response, decision)
        if not decision.response_headers and decision.capture is None:
            return self.app(environ, start_response)
        return self._passing(environ, start_response, decision)

    def _passing(self, environ: dict[str, Any], start_response: StartResponse, decision: Pass) -> Iterable[bytes]:
        """The app's answer, with the answer's headers merged in and copied as it goes out (never delayed)."""
        order = decision.capture
        copy = Copy(order.max_bytes) if order is not None else None
        started = time.monotonic()
        seen: dict[str, Any] = {}

        def wrapped_start(status: str, headers: list[tuple[str, str]], *args: Any) -> Any:
            merged = merge_headers(headers, decision.response_headers) if decision.response_headers else list(headers)
            seen["status"], seen["headers"] = status, merged
            seen["ttfb"] = int((time.monotonic() - started) * 1000)
            write = start_response(status, merged, *args)
            if copy is None:
                return write

            def copying_write(data: bytes) -> Any:
                _add(self.gate, copy, data)
                return write(data)

            return copying_write

        result = self.app(environ, wrapped_start)
        if order is None or copy is None:
            return result
        wrapper = environ.get("wsgi.file_wrapper")
        if isinstance(wrapper, type) and isinstance(result, wrapper):
            return result  # the server's sendfile path: kept, the capture skipped rather than lose it
        if hasattr(result, "__len__"):
            return _SizedCopying(result, self.gate, order, copy, seen)
        return _Copying(result, self.gate, order, copy, seen)

    def _held(self, environ: dict[str, Any], start_response: StartResponse, decision: Pass) -> Iterable[bytes]:
        """A paid request: the app's answer is held, settled, and only then sent (SPEC.md, section 7)."""
        hold = decision.hold
        assert hold is not None
        started = time.monotonic()
        held: dict[str, Any] = {"parts": [], "size": 0, "cut": False}

        def keep(data: bytes) -> None:
            if held["cut"]:
                raise _NotPayable
            held["size"] += len(data)
            if held["size"] > hold.max_bytes:
                held["cut"], held["parts"] = True, []  # too large to be paid for: nothing more is read
                raise _NotPayable
            held["parts"].append(bytes(data))

        def holding_start(status: str, headers: list[tuple[str, str]], exc_info: Any = None) -> Callable[[bytes], None]:
            held["status"], held["headers"] = status, list(headers)
            held["ttfb"] = int((time.monotonic() - started) * 1000)
            if not holdable(_code(status)):  # a protocol switch can't be held or paid for
                held["cut"] = True
                raise _NotPayable
            return keep

        result: Iterable[bytes] | None = None
        try:
            try:
                result = self.app(environ, holding_start)  # an app that raises: nothing was settled, nothing sent
                for chunk in result:
                    keep(chunk)
            except _NotPayable:
                pass
            if held["cut"] or "status" not in held:  # can't be held, or the app answered nothing: never settled
                return self._gate_answer(environ, start_response, not_payable(), True)
            status = _code(held["status"])
            body = b"".join(held["parts"])
            outcome = self.gate.settle(hold, status, body)
            if isinstance(outcome, Respond):
                return self._gate_answer(environ, start_response, outcome, True)
            headers = merge_headers(held["headers"], decision.response_headers, outcome.response_headers)
            start_response(held["status"], headers)
            if decision.capture is not None:
                copy = Copy(decision.capture.max_bytes)
                copy.add(body)
                self.gate.capture(decision.capture, status, headers, copy, held["ttfb"])
            return [body] if body_allowed(status) else []
        finally:
            # After the answer is built: an error closing the app's iterable never costs the visitor a paid page.
            close = getattr(result, "close", None)
            if close is not None:
                try:
                    close()
                except Exception as error:
                    self.gate.report(error)

    def _gate_answer(
        self, environ: dict[str, Any], start_response: StartResponse, decision: Respond, payment: bool
    ) -> Iterable[bytes]:
        """Start an answer the gate made (SPEC.md, section 10). If the server refuses it, it's reported, and the
        replacement starts with that error's `exc_info` (PEP 3333 lets a server take new headers then): without a
        payment the site answers; with one, a bare 503, never the page. Headers already sent: the server's error."""
        try:
            return _respond(start_response, decision)
        except Exception as error:
            self.gate.report(error)
            refused = sys.exc_info()
        if payment:
            start_response("503 Service Unavailable", [], refused)
            return []
        first = [True]

        def again(status: str, headers: list[tuple[str, str]], exc_info: Any = None) -> Any:
            if first[0]:
                first[0] = False
                return start_response(status, headers, exc_info or refused)
            return start_response(status, headers, exc_info)

        return self.app(environ, again)


class _NotPayable(Exception):
    """Raised into an app (from `write` or `start_response`) whose paid answer the gate can't hold: it stops being
    read."""


class _Copying:
    """The app's iterable, copied chunk by chunk as the server sends it; the copy goes to Stilebase when it's closed."""

    def __init__(self, result: Iterable[bytes], gate: SyncGate, order: Capture, copy: Copy, seen: dict[str, Any]) -> None:
        self._result = result
        self._gate = gate
        self._order = order
        self._copy = copy
        self._seen = seen

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._result:
            _add(self._gate, self._copy, chunk)
            yield chunk

    def close(self) -> None:
        try:
            close = getattr(self._result, "close", None)
            if close is not None:
                close()
        finally:
            seen = self._seen
            if "status" in seen:
                try:
                    self._gate.capture(self._order, _code(seen["status"]), seen["headers"], self._copy, seen["ttfb"])
                except Exception as error:
                    self._gate.report(error)


class _SizedCopying(_Copying):
    """As `_Copying`, for an iterable with a length: the server still sees it (wsgiref sets Content-Length from a
    single chunk), so wrapping changes nothing it sends."""

    def __len__(self) -> int:
        return len(self._result)  # type: ignore[arg-type]  # only made for an iterable that has one


def _add(gate: SyncGate, copy: Copy, data: bytes) -> None:
    try:
        copy.add(bytes(data))
    except Exception as error:
        gate.report(error)


def _code(status: str) -> int:
    try:
        return int(status.split(" ", 1)[0])
    except ValueError:
        return 0


def _respond(start_response: StartResponse, decision: Respond) -> list[bytes]:
    """An answer built by the gate's helpers (`respond`, `unknown_payment`, `not_payable`), as it is, its status line
    always "NNN Phrase" ("520 Unknown")."""
    headers = list(decision.headers.items())  # never a Content-Length of Stilebase's: valid_headers drops it
    if body_allowed(decision.status):  # the real one; none on 204, 205, 304
        headers.append(("content-length", str(len(decision.body))))
    start_response(f"{decision.status} {reason(decision.status)}", headers)
    return [decision.body] if body_allowed(decision.status) else []


def request_from_environ(
    environ: dict[str, Any], ip_header: str | None = None, client_ip: Callable[[dict[str, Any]], str | None] | None = None
) -> GateRequest:
    """The request as the gate forwards it, from a WSGI environ."""
    pairs = [(k[5:].replace("_", "-").lower(), str(v)) for k, v in environ.items() if k.startswith("HTTP_")]
    pairs += [(k.replace("_", "-").lower(), str(environ[k])) for k in ("CONTENT_TYPE", "CONTENT_LENGTH") if environ.get(k)]
    try:
        ip = client_ip(environ) if client_ip else address_from(headers_of(pairs), ip_header, environ.get("REMOTE_ADDR"))
    except Exception:
        ip = None  # a client_ip that raises: no address
    # PATH_INFO is the path's bytes decoded as latin-1 (PEP 3333): encode those same bytes again.
    decoded = str(environ.get("SCRIPT_NAME") or "") + str(environ.get("PATH_INFO") or "/")
    path = quote(decoded.encode("latin-1", "replace"), safe="/:@!$&'()*+,;=-._~")
    query = str(environ.get("QUERY_STRING") or "")
    target = f"{path}?{query}" if query else path
    scheme = str(environ.get("wsgi.url_scheme", "http"))
    return GateRequest(str(environ.get("REQUEST_METHOD", "GET")), target, tuple(pairs), scheme=scheme, ip=ip)
