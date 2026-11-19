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
