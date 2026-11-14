"""The adapters in real frameworks: Starlette (ASGI), Flask (WSGI), Django."""

from __future__ import annotations

import base64
import json
from typing import Any

from tests.fake_stilebase import API, BROWSER, DOMAIN, GPTBOT, KEY, REQUIRED, SETTLED_HEADER, FakeStilebase

PAID = {"user-agent": GPTBOT, "payment-signature": "eyJ4NDAyVmVyc2lvbiI6Mn0="}


def options(stilebase: FakeStilebase) -> dict[str, Any]:
    return {"key": KEY, "api_url": API, "transport": stilebase.transport}


def test_asgi_answers_agents_serves_people_and_paid_requests() -> None:
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from stilebase_sdk.gate.asgi import GateMiddleware

    stilebase = FakeStilebase()
    app = Starlette(routes=[Route("/listings", lambda request: PlainTextResponse("the listings"))])
    app.add_middleware(GateMiddleware, **options(stilebase))
    client = TestClient(app, base_url=f"https://{DOMAIN}")
    asked = client.get("/listings", headers={"user-agent": GPTBOT})
    assert asked.status_code == 402 and json.loads(base64.b64decode(asked.headers["payment-required"])) == REQUIRED
    person = client.get("/listings", headers=BROWSER)
    assert (person.status_code, person.text) == (200, "the listings")
    paid = client.get("/listings", headers=PAID)
    assert (paid.status_code, paid.text, paid.headers["payment-response"]) == (200, "the listings", SETTLED_HEADER)
    assert stilebase.to("/v1/x402/requirements")[0].query["url"] == f"https://{DOMAIN}/listings"


def test_asgi_takes_the_address_from_the_connection_or_the_header_you_name() -> None:
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from stilebase_sdk.gate.asgi import GateMiddleware

    for ip_header, expected in ((None, "testclient"), ("x-forwarded-for", "198.51.100.9")):
        stilebase = FakeStilebase()
        app = Starlette(routes=[Route("/", lambda request: PlainTextResponse("ok"))])
        app.add_middleware(GateMiddleware, ip_header=ip_header, **options(stilebase))
        TestClient(app).get("/", headers={"user-agent": GPTBOT, "x-forwarded-for": "20.15.240.70, 198.51.100.9"})
        assert stilebase.to("/v1/x402/requirements")[0].query["ip"] == expected


def test_wsgi_answers_agents_serves_people_and_paid_requests() -> None:
    from flask import Flask

    from stilebase_sdk.gate.wsgi import GateMiddleware

    stilebase = FakeStilebase()
    app = Flask(__name__)
    app.add_url_rule("/listings", "listings", lambda: "the listings")
    app.wsgi_app = GateMiddleware(app.wsgi_app, **options(stilebase))  # type: ignore[method-assign]
    client = app.test_client()
    assert client.get("/listings", headers={"user-agent": GPTBOT}, base_url=f"https://{DOMAIN}").status_code == 402
    person = client.get("/listings", headers=BROWSER, base_url=f"https://{DOMAIN}")
    assert (person.status_code, person.text) == (200, "the listings")
    paid = client.get("/listings", headers=PAID, base_url=f"https://{DOMAIN}")
    assert (paid.status_code, paid.headers["PAYMENT-RESPONSE"]) == (200, SETTLED_HEADER)


def test_django_answers_agents_serves_people_and_paid_requests() -> None:
    from django.conf import settings

    if not settings.configured:
        settings.configure(
            DEBUG=False,
            ALLOWED_HOSTS=[DOMAIN],
            ROOT_URLCONF=__name__,
            SECRET_KEY="test",
            MIDDLEWARE=["stilebase_sdk.gate.django.GateMiddleware"],
        )
        import django

        django.setup()
    stilebase = FakeStilebase()
    settings.STILEBASE_GATE = options(stilebase)
    from django.test import Client

    client = Client(HTTP_HOST=DOMAIN)
    asked = client.get("/listings", HTTP_USER_AGENT=GPTBOT, secure=True)
    assert asked.status_code == 402 and asked["PAYMENT-REQUIRED"]
    person = client.get("/listings", secure=True, **{f"HTTP_{k.upper().replace('-', '_')}": v for k, v in BROWSER.items()})
    assert (person.status_code, person.content) == (200, b"the listings")
    paid = client.get("/listings", secure=True, HTTP_USER_AGENT=GPTBOT, HTTP_PAYMENT_SIGNATURE=PAID["payment-signature"])
    assert (paid.status_code, paid["PAYMENT-RESPONSE"]) == (200, SETTLED_HEADER)
    assert stilebase.to("/v1/x402/requirements")[0].query["url"] == f"https://{DOMAIN}/listings"


def _listings(request: Any) -> Any:
    from django.http import HttpResponse

    return HttpResponse("the listings")


def __getattr__(name: str) -> Any:  # Django's ROOT_URLCONF: this module, its urlpatterns made on first use
    if name == "urlpatterns":
        from django.urls import path

        return [path("listings", _listings)]
    raise AttributeError(name)


def test_asgi_takes_an_async_client_ip_as_no_address_and_closes_it() -> None:
    from stilebase_sdk.gate.asgi import GateMiddleware

    made: list[Any] = []

    async def lookup(scope: Any) -> str:
        return "198.51.100.9"

    def client_ip(scope: Any) -> Any:
        made.append(lookup(scope))
        return made[-1]

    async def app(scope: Any, receive: Any, send: Any) -> None: ...

    middleware = GateMiddleware(app, client_ip=client_ip, key=KEY, api_url=API)
    assert middleware.request({"type": "http", "path": "/", "headers": []}).ip is None
    assert made[0].cr_frame is None  # closed, never left unawaited
