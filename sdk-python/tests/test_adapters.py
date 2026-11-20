"""The adapters in real frameworks: Starlette (ASGI), Flask (WSGI), Django. Each obeys, holds a paid answer until
it's settled, and copies an answer without changing it."""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from tests.fake_stilebase import (
    API,
    CHROME,
    GPTBOT,
    KEY,
    PAYMENT,
    RECEIPT,
    REQUIRED_402,
    SETTLE_AFTER,
    FakeStilebase,
)

PAGE = "the listings"
CAPTURE = {"action": "pass", "capture": {"token": "tok", "max_bytes": 5}}


def by_agent(body: dict[str, Any]) -> tuple[int, Any]:
    """What a Stilebase might answer: the 402 for GPTBot, settle_after for a payment, a capture for curl."""
    headers = dict(body["headers"])
    if "payment-signature" in headers:
        return 200, SETTLE_AFTER
    if "GPTBot" in headers.get("user-agent", ""):
        return 200, REQUIRED_402
    if "curl" in headers.get("user-agent", ""):
        return 200, CAPTURE
    return 200, {"action": "pass"}


def captured(stilebase: FakeStilebase) -> dict[str, Any]:
    [sent] = stilebase.to("captures")
    body: dict[str, Any] = sent.body
    return body


def test_asgi() -> None:
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from stilebase_sdk.gate.asgi import GateMiddleware

    stilebase = FakeStilebase(decide=by_agent)
    app = Starlette(routes=[Route("/listings", lambda request: PlainTextResponse(PAGE))])
    app.add_middleware(GateMiddleware, key=KEY, api_url=API, transport=stilebase.async_transport)
    with TestClient(app, base_url="http://shop.example.com") as client:
        asked = client.get("/listings?x=1", headers={"user-agent": GPTBOT})
        assert asked.status_code == 402 and asked.headers["cache-control"] == "no-store"
        person = client.get("/listings", headers={"user-agent": CHROME})
        assert (person.status_code, person.text) == (200, PAGE)
        paid = client.get("/listings", headers={"user-agent": GPTBOT, "payment-signature": PAYMENT})
        assert (paid.status_code, paid.text, paid.headers["payment-response"]) == (200, PAGE, RECEIPT)
        copied = client.get("/listings", headers={"user-agent": "curl/8.4.0"})
        assert copied.text == PAGE
    first = stilebase.to("decide")[0].body
    assert first["request"] == {"method": "GET", "scheme": "https", "path": "/listings?x=1"}
    [settled] = stilebase.to("settle")
    assert settled.body["delivery"]["sha256"] == hashlib.sha256(PAGE.encode()).hexdigest()
    copy = captured(stilebase)
    assert (copy["status"], base64.b64decode(copy["body"]), copy["size"], copy["truncated"]) == (200, b"the l", 12, True)


def test_asgi_withholds_a_page_whose_settlement_failed() -> None:
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from stilebase_sdk.gate.asgi import GateMiddleware

    stilebase = FakeStilebase(decide=(200, SETTLE_AFTER), settle=(500, {}))
    app = Starlette(
        routes=[
            Route("/listings", lambda request: PlainTextResponse(PAGE)),
            Route("/big", lambda request: PlainTextResponse("x" * 2000)),
        ]
    )
    app.add_middleware(GateMiddleware, key=KEY, api_url=API, transport=stilebase.async_transport)
    with TestClient(app) as client:
        refused = client.get("/listings", headers={"payment-signature": PAYMENT})
        assert (refused.status_code, refused.headers["retry-after"]) == (503, "5") and PAGE not in refused.text
        big = client.get("/big", headers={"payment-signature": PAYMENT})
        assert big.status_code == 502
    assert len(stilebase.to("settle")) == 1  # the big one was never settled


def test_asgi_takes_the_address_from_the_header_you_name() -> None:
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from stilebase_sdk.gate.asgi import GateMiddleware

    for ip_header, expected in ((None, "testclient"), ("x-forwarded-for", "198.51.100.9")):
        stilebase = FakeStilebase()
        app = Starlette(routes=[Route("/", lambda request: PlainTextResponse("ok"))])
        app.add_middleware(GateMiddleware, ip_header=ip_header, key=KEY, api_url=API, transport=stilebase.async_transport)
        with TestClient(app) as client:
            client.get("/", headers={"user-agent": GPTBOT, "x-forwarded-for": "20.15.240.70, 198.51.100.9"})
        assert stilebase.to("decide")[0].body["client_ip"] == expected


def test_wsgi() -> None:
    from flask import Flask, Response, stream_with_context

    from stilebase_sdk.gate.wsgi import GateMiddleware

    stilebase = FakeStilebase(decide=by_agent)
    app = Flask(__name__)
    app.add_url_rule("/listings", "listings", lambda: PAGE)
    app.add_url_rule("/stream", "stream", lambda: Response(stream_with_context(iter([b"the ", b"listings"]))))
    app.wsgi_app = GateMiddleware(app.wsgi_app, key=KEY, api_url=API, transport=stilebase.transport)  # type: ignore[method-assign]
    client = app.test_client()
    assert client.get("/listings", headers={"user-agent": GPTBOT}).status_code == 402
    assert client.get("/listings", headers={"user-agent": CHROME}).text == PAGE
    paid = client.get("/listings", headers={"user-agent": GPTBOT, "payment-signature": PAYMENT})
    assert (paid.status_code, paid.text, paid.headers["payment-response"]) == (200, PAGE, RECEIPT)
    copied = client.get("/stream", headers={"user-agent": "curl/8.4.0"})
    assert copied.text == PAGE
    copied.close()  # what a WSGI server does once it has sent the answer: the copy goes then
    app.wsgi_app.gate.wait()
    copy = captured(stilebase)
    assert (base64.b64decode(copy["body"]), copy["size"], copy["truncated"]) == (b"the l", 12, True)


def test_wsgi_withholds_a_page_whose_settlement_failed() -> None:
    from flask import Flask

    from stilebase_sdk.gate.wsgi import GateMiddleware

    stilebase = FakeStilebase(decide=(200, SETTLE_AFTER), settle=(200, REQUIRED_402))
    app = Flask(__name__)
    app.add_url_rule("/listings", "listings", lambda: PAGE)
    app.wsgi_app = GateMiddleware(app.wsgi_app, key=KEY, api_url=API, transport=stilebase.transport)  # type: ignore[method-assign]
    refused = app.test_client().get("/listings", headers={"payment-signature": PAYMENT})
    assert refused.status_code == 402 and PAGE not in refused.text


def test_django() -> None:
    import django
    from django.conf import settings

    if not settings.configured:
        settings.configure(DEBUG=False, ALLOWED_HOSTS=["*"], ROOT_URLCONF=__name__, MIDDLEWARE=[])
        django.setup()
    from django.http import HttpResponse, StreamingHttpResponse
    from django.test import RequestFactory

    from stilebase_sdk.gate.django import GateMiddleware

    stilebase = FakeStilebase(decide=by_agent)
    settings.STILEBASE_GATE = {"key": KEY, "api_url": API, "transport": stilebase.transport}
    middleware = GateMiddleware(lambda request: HttpResponse(PAGE))
    factory = RequestFactory()
    assert middleware(factory.get("/listings", HTTP_USER_AGENT=GPTBOT)).status_code == 402
    assert middleware(factory.get("/listings", HTTP_USER_AGENT=CHROME)).content == PAGE.encode()
    paid = middleware(factory.get("/listings", HTTP_USER_AGENT=GPTBOT, HTTP_PAYMENT_SIGNATURE=PAYMENT))
    assert (paid.status_code, paid.content, paid["payment-response"]) == (200, PAGE.encode(), RECEIPT)

    streaming = GateMiddleware(lambda request: StreamingHttpResponse(iter([b"the ", b"listings"])))
    held = streaming(factory.get("/listings", HTTP_PAYMENT_SIGNATURE=PAYMENT))
    assert held.content == PAGE.encode() and held["payment-response"] == RECEIPT
    copied = streaming(factory.get("/listings", HTTP_USER_AGENT="curl/8.4.0"))
    assert b"".join(copied.streaming_content) == PAGE.encode()
    streaming.gate.wait()
    copy = captured(stilebase)
    assert (base64.b64decode(copy["body"]), copy["size"], copy["truncated"]) == (b"the l", 12, True)


urlpatterns: list[Any] = []


def test_django_holds_a_stream_without_finishing_the_request_early() -> None:
    import django
    from django.conf import settings

    if not settings.configured:
        settings.configure(DEBUG=False, ALLOWED_HOSTS=["*"], ROOT_URLCONF=__name__, MIDDLEWARE=[])
        django.setup()
    from django.core.signals import request_finished
    from django.http import StreamingHttpResponse
    from django.test import RequestFactory

    from stilebase_sdk.gate.django import GateMiddleware

    finished: list[object] = []
    closed: list[bool] = []

    def on_finished(**kwargs: Any) -> None:
        finished.append(kwargs)

    def view(request: Any) -> StreamingHttpResponse:
        response = StreamingHttpResponse(iter([b"the ", b"listings"]), reason="Fine Thanks")
        response.set_cookie("session", "abc")
        response._resource_closers.append(lambda: closed.append(True))
        return response

    stilebase = FakeStilebase(decide=(200, SETTLE_AFTER))
    settings.STILEBASE_GATE = {"key": KEY, "api_url": API, "transport": stilebase.transport}
    request_finished.connect(on_finished)
    try:
        held = GateMiddleware(view)(RequestFactory().get("/listings", HTTP_PAYMENT_SIGNATURE=PAYMENT))
        assert finished == [] and closed == [True]  # the stream's resources freed, the request not "finished" yet
        assert (held.content, held.reason_phrase, held.cookies["session"].value) == (PAGE.encode(), "Fine Thanks", "abc")
        assert held["payment-response"] == RECEIPT
    finally:
        request_finished.disconnect(on_finished)


# Review cycle 2


def _asgi_call(app: Any, path: str, headers: list[tuple[bytes, bytes]], extensions: dict[str, Any]) -> list[dict[str, Any]]:
    import asyncio

    sent: list[dict[str, Any]] = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("203.0.113.7", 1234),
        "server": ("shop.example.com", 443),
        "extensions": extensions,
    }

    async def receive() -> dict[str, Any]:
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(message: Any) -> None:
        sent.append(dict(message))

    asyncio.run(asyncio.wait_for(app(scope, receive, send), 5))
    return sent


def test_asgi_holds_a_file_response_whatever_pathsend_offers(tmp_path: Any) -> None:
    from starlette.applications import Starlette
    from starlette.responses import FileResponse
    from starlette.routing import Route

    from stilebase_sdk.gate.asgi import GateMiddleware

    page = tmp_path / "page.html"
    page.write_bytes(PAGE.encode())
    stilebase = FakeStilebase(decide=(200, SETTLE_AFTER))
    inner = Starlette(routes=[Route("/file", lambda request: FileResponse(page))])
    app = GateMiddleware(inner, key=KEY, api_url=API, transport=stilebase.async_transport)
    sent = _asgi_call(app, "/file", [(b"payment-signature", PAYMENT.encode())], {"http.response.pathsend": {}})
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]  # never a pathsend around the gate
    assert sent[1]["body"] == PAGE.encode()
    assert (b"payment-response", RECEIPT.encode()) in sent[0]["headers"]
    [settled] = stilebase.to("settle")
    assert settled.body["delivery"]["size"] == len(PAGE)


def _endless() -> Any:
    while True:
        yield b"x" * 100


def test_asgi_stops_reading_an_endless_paid_answer() -> None:
    from starlette.applications import Starlette
    from starlette.responses import StreamingResponse
    from starlette.routing import Route

    from stilebase_sdk.gate.asgi import GateMiddleware

    stilebase = FakeStilebase(decide=(200, SETTLE_AFTER))
    inner = Starlette(routes=[Route("/endless", lambda request: StreamingResponse(_endless()))])
    app = GateMiddleware(inner, key=KEY, api_url=API, transport=stilebase.async_transport)
    sent = _asgi_call(app, "/endless", [(b"payment-signature", PAYMENT.encode())], {})
    assert sent[0]["status"] == 502 and stilebase.to("settle") == []


def test_wsgi_stops_reading_an_endless_paid_answer() -> None:
    from flask import Flask, Response

    from stilebase_sdk.gate.wsgi import GateMiddleware

    stilebase = FakeStilebase(decide=(200, SETTLE_AFTER))
    app = Flask(__name__)
    app.add_url_rule("/endless", "endless", lambda: Response(_endless()))
    app.wsgi_app = GateMiddleware(app.wsgi_app, key=KEY, api_url=API, transport=stilebase.transport)  # type: ignore[method-assign]
    assert app.test_client().get("/endless", headers={"payment-signature": PAYMENT}).status_code == 502
    assert stilebase.to("settle") == []


def _django() -> None:
    import django
    from django.conf import settings

    if not settings.configured:
        settings.configure(DEBUG=False, ALLOWED_HOSTS=["*"], ROOT_URLCONF=__name__, MIDDLEWARE=[])
        django.setup()


def test_django_stops_reading_an_endless_paid_answer() -> None:
    _django()
    from django.conf import settings
    from django.http import StreamingHttpResponse
    from django.test import RequestFactory

    from stilebase_sdk.gate.django import GateMiddleware

    stilebase = FakeStilebase(decide=(200, SETTLE_AFTER))
    settings.STILEBASE_GATE = {"key": KEY, "api_url": API, "transport": stilebase.transport}
    middleware = GateMiddleware(lambda request: StreamingHttpResponse(_endless()))
    assert middleware(RequestFactory().get("/", HTTP_PAYMENT_SIGNATURE=PAYMENT)).status_code == 502
    assert stilebase.to("settle") == []


def test_a_held_answer_that_cant_carry_a_body_is_sent_without_one() -> None:
    from flask import Flask
    from starlette.applications import Starlette
    from starlette.responses import Response as StarletteResponse
    from starlette.routing import Route

    from stilebase_sdk.gate.asgi import GateMiddleware as Asgi
    from stilebase_sdk.gate.wsgi import GateMiddleware as Wsgi

    stilebase = FakeStilebase(decide=(200, SETTLE_AFTER))
    inner = Starlette(routes=[Route("/", lambda request: StarletteResponse(status_code=304))])
    sent = _asgi_call(
        Asgi(inner, key=KEY, api_url=API, transport=stilebase.async_transport),
        "/",
        [(b"payment-signature", PAYMENT.encode())],
        {},
    )
    assert (sent[0]["status"], sent[1]["body"]) == (304, b"")

    flask = Flask(__name__)
    flask.add_url_rule("/", "x", lambda: ("", 204))
    flask.wsgi_app = Wsgi(flask.wsgi_app, key=KEY, api_url=API, transport=stilebase.transport)  # type: ignore[method-assign]
    answered = flask.test_client().get("/", headers={"payment-signature": PAYMENT})
    assert (answered.status_code, answered.data) == (204, b"")

    _django()
    from django.conf import settings
    from django.http import HttpResponseNotModified
    from django.test import RequestFactory

    from stilebase_sdk.gate.django import GateMiddleware

    settings.STILEBASE_GATE = {"key": KEY, "api_url": API, "transport": stilebase.transport}
    held = GateMiddleware(lambda request: HttpResponseNotModified())(RequestFactory().get("/", HTTP_PAYMENT_SIGNATURE=PAYMENT))
    assert (held.status_code, held.content, held["payment-response"]) == (304, b"", RECEIPT)
    assert len(stilebase.to("settle")) == 3


# Review cycle 3


NOT_PAYABLE = b'{"error":"answer_not_payable"}'


def test_wsgi_keeps_a_paid_page_whose_iterable_fails_to_close() -> None:
    from stilebase_sdk.gate.wsgi import GateMiddleware

    class Closing:
        def __iter__(self) -> Any:
            return iter([PAGE.encode()])

        def close(self) -> None:
            raise RuntimeError("close failed")

    def app(environ: Any, start_response: Any) -> Any:
        start_response("200 OK", [("Content-Type", "text/plain")])
        return Closing()

    stilebase = FakeStilebase(decide=(200, SETTLE_AFTER))
    errors: list[Exception] = []
    gate = GateMiddleware(app, key=KEY, api_url=API, transport=stilebase.transport, on_error=errors.append)
    started: list[Any] = []
    environ = {"REQUEST_METHOD": "GET", "PATH_INFO": "/", "HTTP_PAYMENT_SIGNATURE": PAYMENT, "wsgi.url_scheme": "https"}
    body = gate(environ, lambda status, headers: started.append((status, headers)))
    assert list(body) == [PAGE.encode()] and started[0][0] == "200 OK"
    assert any("close failed" in str(e) for e in errors)


def test_wsgi_an_app_that_never_answers_gets_not_payable() -> None:
    from stilebase_sdk.gate.wsgi import GateMiddleware

    stilebase = FakeStilebase(decide=(200, SETTLE_AFTER))
    gate = GateMiddleware(lambda environ, start_response: [], key=KEY, api_url=API, transport=stilebase.transport)
    started: list[Any] = []
    environ = {"REQUEST_METHOD": "GET", "PATH_INFO": "/", "HTTP_PAYMENT_SIGNATURE": PAYMENT, "wsgi.url_scheme": "https"}
    assert list(gate(environ, lambda status, headers: started.append(status))) == [NOT_PAYABLE]
    assert started == ["502 Bad Gateway"] and stilebase.to("settle") == []


def test_asgi_sends_the_paid_page_before_background_work_and_keeps_it_if_that_fails() -> None:
    from starlette.applications import Starlette
    from starlette.background import BackgroundTask
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from stilebase_sdk.gate.asgi import GateMiddleware

    order: list[str] = []

    def later() -> None:
        order.append("background")
        raise RuntimeError("background failed")

    stilebase = FakeStilebase(decide=(200, SETTLE_AFTER))
    inner = Starlette(routes=[Route("/", lambda request: PlainTextResponse(PAGE, background=BackgroundTask(later)))])
    errors: list[Exception] = []
    app = GateMiddleware(inner, key=KEY, api_url=API, transport=stilebase.async_transport, on_error=errors.append)
    sent = _asgi_call(app, "/", [(b"payment-signature", PAYMENT.encode())], {})
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"] and sent[1]["body"] == PAGE.encode()
    assert order == ["background"] and len(stilebase.to("settle")) == 1
    assert any("background failed" in str(e) for e in errors)


def test_set_cookie_never_reaches_a_capture() -> None:
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from stilebase_sdk.gate.asgi import GateMiddleware

    def page(request: Any) -> PlainTextResponse:
        response = PlainTextResponse(PAGE)
        response.set_cookie("session", "secret")
        return response

    stilebase = FakeStilebase(decide=(200, CAPTURE))
    app = Starlette(routes=[Route("/", page)])
    app.add_middleware(GateMiddleware, key=KEY, api_url=API, transport=stilebase.async_transport)
    with TestClient(app) as client:
        assert "session=secret" in client.get("/").headers["set-cookie"]  # the visitor still gets it
    names = [name for name, _ in captured(stilebase)["headers"]]
    assert "set-cookie" not in names and "content-type" in names


def test_a_protocol_switch_is_never_held() -> None:
    from stilebase_sdk.gate.asgi import GateMiddleware

    async def switching(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 101, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    stilebase = FakeStilebase(decide=(200, SETTLE_AFTER))
    app = GateMiddleware(switching, key=KEY, api_url=API, transport=stilebase.async_transport)
    sent = _asgi_call(app, "/", [(b"payment-signature", PAYMENT.encode())], {})
    assert (sent[0]["status"], sent[1]["body"]) == (502, NOT_PAYABLE) and stilebase.to("settle") == []


# Final review


def _wsgiref(app: Any, environ_extra: dict[str, Any]) -> bytes:
    import io
    from wsgiref.handlers import SimpleHandler
    from wsgiref.util import setup_testing_defaults

    environ: dict[str, Any] = {}
    setup_testing_defaults(environ)
    environ.update(environ_extra)
    out, err = io.BytesIO(), io.StringIO()
    SimpleHandler(io.BytesIO(), out, err, environ).run(app)
    return out.getvalue()


def test_wsgi_capture_keeps_what_the_server_sends() -> None:
    import io
    from wsgiref.util import FileWrapper

    from stilebase_sdk.gate.wsgi import GateMiddleware

    def single(environ: Any, start_response: Any) -> Any:
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [PAGE.encode()]

    stilebase = FakeStilebase(decide=(200, CAPTURE))
    gate = GateMiddleware(single, key=KEY, api_url=API, transport=stilebase.transport)
    sent = _wsgiref(gate, {"HTTP_USER_AGENT": "curl/8.4.0"})
    assert f"Content-Length: {len(PAGE)}".encode() in sent and sent.endswith(PAGE.encode())  # wsgiref still sets it
    gate.gate.wait()
    assert len(stilebase.to("captures")) == 1

    def file(environ: Any, start_response: Any) -> Any:
        start_response("200 OK", [("Content-Type", "text/plain")])
        return environ["wsgi.file_wrapper"](io.BytesIO(PAGE.encode()))

    stilebase = FakeStilebase(decide=(200, CAPTURE))
    gate = GateMiddleware(file, key=KEY, api_url=API, transport=stilebase.transport)
    environ = {"REQUEST_METHOD": "GET", "PATH_INFO": "/", "wsgi.url_scheme": "https", "wsgi.file_wrapper": FileWrapper}
    environ["HTTP_USER_AGENT"] = "curl/8.4.0"
    result = gate(environ, lambda status, headers: None)
    assert isinstance(result, FileWrapper)  # the server's sendfile path, untouched; the capture skipped
    gate.gate.wait()
    assert stilebase.to("captures") == []


def test_wsgi_gate_answers_carry_one_content_length_and_none_without_a_body() -> None:
    from stilebase_sdk.gate.wsgi import GateMiddleware

    for answer, lengths in (
        ({"action": "respond", "status": 402, "headers": {"Content-Length": "2"}, "body": "{}"}, ["2"]),
        ({"action": "respond", "status": 204, "headers": {}, "body": "x"}, []),
    ):
        stilebase = FakeStilebase(decide=(200, answer))
        gate = GateMiddleware(lambda environ, start_response: [], key=KEY, api_url=API, transport=stilebase.transport)
        started: list[Any] = []
        environ = {"REQUEST_METHOD": "GET", "PATH_INFO": "/", "wsgi.url_scheme": "https", "HTTP_USER_AGENT": GPTBOT}
        body = gate(environ, lambda status, headers: started.append(headers))  # noqa: B023 - called in this iteration
        assert [v for n, v in started[0] if n.lower() == "content-length"] == lengths
        assert b"".join(body) == (b"" if not lengths else b"{}")


def test_django_respond_adds_no_content_type() -> None:
    _django()
    from django.conf import settings
    from django.test import RequestFactory

    from stilebase_sdk.gate.django import GateMiddleware

    stilebase = FakeStilebase(decide=(200, {"action": "respond", "status": 402, "body": "pay"}))
    settings.STILEBASE_GATE = {"key": KEY, "api_url": API, "transport": stilebase.transport}
    answered = GateMiddleware(lambda request: None)(RequestFactory().get("/", HTTP_USER_AGENT=GPTBOT))
    assert answered.status_code == 402 and "Content-Type" not in answered and answered["Cache-Control"] == "no-store"


def test_an_invalid_header_from_stilebase_is_left_out() -> None:
    from starlette.applications import Starlette
    from starlette.routing import Route

    from stilebase_sdk.gate.asgi import GateMiddleware

    answer = {"action": "respond", "status": 402, "headers": {"x-ok": "1", "x-bad": "a\r\nset-cookie: x"}, "body": "pay"}
    stilebase = FakeStilebase(decide=(200, answer))
    app = GateMiddleware(
        Starlette(routes=[Route("/", lambda r: None)]), key=KEY, api_url=API, transport=stilebase.async_transport
    )
    sent = _asgi_call(app, "/", [(b"user-agent", GPTBOT.encode())], {})
    names = [n for n, _ in sent[0]["headers"]]
    assert sent[0]["status"] == 402 and b"x-ok" in names and b"x-bad" not in names


# The last review: fail closed, and headers every server accepts


EVERY_KIND = {
    "x-ok": "a b",
    "x-tab": "a\tb",
    "x-euro": "pay €1",
    "x-ctl": "a\u0001b",
    "x-space": " lead",
    "x-trail": "trail ",
    "x-del": "a\u007fb",
    "connection": "close",
    "keep-alive": "timeout=5",
    "Content-Length": "999",
    "transfer-encoding": "chunked",
    "upgrade": "h2c",
    "set-cookie\n": "x",
}


def test_wsgiref_sends_what_stilebase_asked_for() -> None:
    """wsgiref itself (its start_response refuses hop-by-hop headers): the 402 goes out, with a real Content-Length."""
    from stilebase_sdk.gate.wsgi import GateMiddleware

    answer = {"action": "respond", "status": 402, "headers": EVERY_KIND, "body": "pay"}
    gate = GateMiddleware(lambda e, s: [], key=KEY, api_url=API, transport=FakeStilebase(decide=(200, answer)).transport)
    sent = _wsgiref(gate, {"HTTP_USER_AGENT": GPTBOT})
    head, _, body = sent.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.0 402") and body == b"pay"
    lines = head.lower().split(b"\r\n")
    assert b"x-ok: a b" in lines and b"content-length: 3" in lines
    dropped = (b"connection", b"transfer-encoding", b"x-ctl", b"x-space", b"x-euro", b"x-tab")
    assert not any(line.startswith(dropped) for line in lines)


def test_h11_accepts_what_the_asgi_gate_sends() -> None:
    """h11, uvicorn's HTTP/1.1 library, validates every header it's given: what the gate sends must pass it. (Its own
    validation, rather than a uvicorn server in-process.)"""
    import h11
    from starlette.applications import Starlette
    from starlette.routing import Route

    from stilebase_sdk.gate.asgi import GateMiddleware

    answer = {"action": "respond", "status": 402, "headers": EVERY_KIND, "body": "pay"}
    inner = Starlette(routes=[Route("/", lambda r: None)])
    app = GateMiddleware(inner, key=KEY, api_url=API, transport=FakeStilebase(decide=(200, answer)).async_transport)
    sent = _asgi_call(app, "/", [(b"user-agent", GPTBOT.encode())], {})
    connection = h11.Connection(our_role=h11.SERVER)
    connection.receive_data(b"GET / HTTP/1.1\r\nHost: shop.example.com\r\n\r\n")
    connection.next_event()
    head = connection.send(h11.Response(status_code=sent[0]["status"], headers=sent[0]["headers"]))
    assert head is not None and b"x-ok: a b" in head and head.lower().count(b"content-length") == 1
    assert connection.send(h11.Data(data=sent[1]["body"])) == b"pay"
    # What the gate dropped that h11 itself refuses (it accepts \x01, DEL and raw UTF-8; the strict rule drops those for
    # stricter servers, e.g. Node's).
    for bad in ({"x-space": " lead"}, {"x-trail": "trail "}, {"bad name": "x"}, {"x-nul": "a\u0000b"}):
        try:
            h11.Connection(our_role=h11.CLIENT).send(h11.Request(method="GET", target="/", headers=[("host", "x"), *bad.items()]))
            raise AssertionError(f"h11 accepted {bad}")
        except h11.LocalProtocolError:
            pass


# The last round: fail open, on real servers


class _RefusingHandler:
    """wsgiref's own handler, made to refuse one status the way a server does: after recording it, as wsgiref's own
    header assertions do (so a second start_response needs exc_info, PEP 3333)."""

    @staticmethod
    def make(refused: str) -> Any:
        from wsgiref.handlers import SimpleHandler

        class Refusing(SimpleHandler):
            def start_response(self, status: str, headers: Any, exc_info: Any = None) -> Any:
                write = super().start_response(status, headers, exc_info)
                if status.startswith(refused) and exc_info is None:
                    raise AssertionError(f"this server refuses {status}")
                return write

        return Refusing


def _wsgiref_with(handler: Any, app: Any, environ_extra: dict[str, Any]) -> bytes:
    import io
    from wsgiref.util import setup_testing_defaults

    environ: dict[str, Any] = {}
    setup_testing_defaults(environ)
    environ.update(environ_extra)
    out = io.BytesIO()
    handler(io.BytesIO(), out, io.StringIO(), environ).run(app)
    return out.getvalue()


def test_wsgiref_a_refused_gate_answer_falls_back() -> None:
    """Without a payment the site answers; with one, a bare 503, never the page. Each refusal is reported."""
    from stilebase_sdk.gate.wsgi import GateMiddleware

    def site(environ: Any, start_response: Any) -> Any:
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [PAGE.encode()]

    for payment, answer, expected in (
        (False, REQUIRED_402, b"HTTP/1.0 200"),
        (True, {"action": "unknown_payment"}, b"HTTP/1.0 503"),
    ):
        errors: list[Exception] = []
        stilebase = FakeStilebase(decide=(200, answer))
        gate = GateMiddleware(site, key=KEY, api_url=API, transport=stilebase.transport, on_error=errors.append)
        extra = {"HTTP_USER_AGENT": GPTBOT} | ({"HTTP_PAYMENT_SIGNATURE": PAYMENT} if payment else {})
        refused = "402" if not payment else "503 Service Unavailable"
        sent = _wsgiref_with(_RefusingHandler.make(refused), gate, extra)
        assert sent.startswith(expected) and (PAGE.encode() in sent) is (not payment)
        assert any("refuses" in str(e) for e in errors)


def test_wsgiref_status_line_for_a_status_without_a_phrase() -> None:
    from stilebase_sdk.gate.wsgi import GateMiddleware

    answer = {"action": "respond", "status": 520, "body": "x"}
    gate = GateMiddleware(lambda e, s: [], key=KEY, api_url=API, transport=FakeStilebase(decide=(200, answer)).transport)
    assert _wsgiref(gate, {"HTTP_USER_AGENT": GPTBOT}).startswith(b"HTTP/1.0 520 Unknown\r\n")


def test_asgi_a_refused_gate_answer_falls_back() -> None:
    import asyncio

    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from stilebase_sdk.gate.asgi import GateMiddleware

    for payment in (False, True):
        errors: list[Exception] = []
        answer = (200, {"action": "unknown_payment"}) if payment else (200, REQUIRED_402)
        inner = Starlette(routes=[Route("/", lambda r: PlainTextResponse(PAGE))])
        app = GateMiddleware(
            inner, key=KEY, api_url=API, transport=FakeStilebase(decide=answer).async_transport, on_error=errors.append
        )
        sent: list[dict[str, Any]] = []

        async def send(message: Any) -> None:
            if message["type"] == "http.response.start" and message["status"] in (402, 503) and message["headers"]:
                raise RuntimeError("the server refuses this answer")
            sent.append(dict(message))  # noqa: B023 - called in this iteration

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        headers = [(b"user-agent", GPTBOT.encode())] + ([(b"payment-signature", PAYMENT.encode())] if payment else [])
        scope = {"type": "http", "method": "GET", "scheme": "https", "path": "/", "raw_path": b"/", "query_string": b""}
        scope |= {"headers": headers, "client": ("1.2.3.4", 1), "server": ("x", 443)}
        asyncio.run(app(scope, receive, send))
        body = b"".join(m.get("body", b"") for m in sent)
        assert sent[0]["status"] == (503 if payment else 200) and (PAGE.encode() in body) is (not payment)
        assert any("refuses" in str(e) for e in errors)


def test_asgi_a_refused_paid_answer_is_reported() -> None:
    import asyncio

    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from stilebase_sdk.gate.asgi import GateMiddleware

    errors: list[Exception] = []
    inner = Starlette(routes=[Route("/", lambda r: PlainTextResponse(PAGE))])
    stilebase = FakeStilebase(decide=(200, SETTLE_AFTER))
    app = GateMiddleware(inner, key=KEY, api_url=API, transport=stilebase.async_transport, on_error=errors.append)
    sent: list[dict[str, Any]] = []

    async def send(message: Any) -> None:
        if message["type"] == "http.response.start" and message["status"] == 200:
            raise RuntimeError("the server refuses the paid answer")
        sent.append(dict(message))

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "method": "GET", "scheme": "https", "path": "/", "raw_path": b"/", "query_string": b""}
    scope |= {"headers": [(b"payment-signature", PAYMENT.encode())], "client": ("1.2.3.4", 1), "server": ("x", 443)}
    asyncio.run(app(scope, receive, send))
    assert any("refuses the paid answer" in str(e) for e in errors) and sent[0]["status"] == 503


def test_django_a_gate_answer_that_cant_be_built_falls_back(monkeypatch: Any) -> None:
    _django()
    from django.conf import settings
    from django.http import HttpResponse
    from django.test import RequestFactory

    import stilebase_sdk.gate.django as adapter

    def broken(decision: Any) -> Any:
        raise RuntimeError("can't build it")

    monkeypatch.setattr(adapter, "_response", broken)
    errors: list[Exception] = []
    stilebase = FakeStilebase(decide=(200, REQUIRED_402))
    settings.STILEBASE_GATE = {"key": KEY, "api_url": API, "transport": stilebase.transport, "on_error": errors.append}
    middleware = adapter.GateMiddleware(lambda request: HttpResponse(PAGE))
    assert middleware(RequestFactory().get("/", HTTP_USER_AGENT=GPTBOT)).content == PAGE.encode()
    paid = middleware(RequestFactory().get("/", HTTP_USER_AGENT=GPTBOT, HTTP_PAYMENT_SIGNATURE=PAYMENT))
    assert paid.status_code == 503 and PAGE.encode() not in paid.content
    assert len([e for e in errors if "can't build" in str(e)]) == 2


def test_django_gate_answers_carry_their_length() -> None:
    _django()
    from django.conf import settings
    from django.test import RequestFactory

    from stilebase_sdk.gate.django import GateMiddleware

    for answer, length in (
        ({"action": "respond", "status": 402, "body": "pay"}, "3"),
        ({"action": "respond", "status": 204, "body": "x"}, None),
    ):
        settings.STILEBASE_GATE = {"key": KEY, "api_url": API, "transport": FakeStilebase(decide=(200, answer)).transport}
        answered = GateMiddleware(lambda request: None)(RequestFactory().get("/", HTTP_USER_AGENT=GPTBOT))
        assert answered.get("Content-Length") == length
