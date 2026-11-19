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
