"""The adapters in real frameworks: Starlette (ASGI), Flask (WSGI), Django."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from flask import Flask
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from stilebase_sdk.gate.asgi import GateMiddleware as AsgiGate
from stilebase_sdk.gate.wsgi import GateMiddleware as WsgiGate
from tests.fake_stilebase import API, BROWSER, GPTBOT, KEY, REQUIRED, SETTLED_HEADER, FakeStilebase

PAID = {"user-agent": GPTBOT, "payment-signature": "eyJ4NDAyVmVyc2lvbiI6Mn0="}


@pytest.fixture
def stilebase() -> FakeStilebase:
    return FakeStilebase()


def options(stilebase: FakeStilebase, **extra: Any) -> dict[str, Any]:
    return {"key": KEY, "api_url": API, "transport": stilebase.transport, **extra}


# --- ASGI ---------------------------------------------------------------------------------------------------------


def starlette(stilebase: FakeStilebase, **extra: Any) -> TestClient:
    app = Starlette(routes=[Route("/listings", lambda request: PlainTextResponse("the listings"))])
    app.add_middleware(AsgiGate, **options(stilebase, **extra))
    return TestClient(app, base_url="https://shop.example.com")


def test_asgi_answers_agents_serves_people_and_paid_requests(stilebase: FakeStilebase) -> None:
    with starlette(stilebase) as client:
        asked = client.get("/listings", headers={"user-agent": GPTBOT})
        assert asked.status_code == 402
        assert json.loads(base64.b64decode(asked.headers["payment-required"])) == REQUIRED
        person = client.get("/listings", headers=BROWSER)
        assert (person.status_code, person.text) == (200, "the listings")
        paid = client.get("/listings", headers=PAID)
        assert (paid.status_code, paid.text, paid.headers["payment-response"]) == (200, "the listings", SETTLED_HEADER)
    assert stilebase.to("/v1/x402/requirements")[0].query["url"] == "https://shop.example.com/listings"
    assert stilebase.calls[0].headers["stilebase-client"].endswith(" gate/asgi")


def test_asgi_takes_the_address_from_the_connection_or_the_header_you_name(stilebase: FakeStilebase) -> None:
    with starlette(stilebase) as client:
        client.get("/listings", headers={"user-agent": GPTBOT, "cf-connecting-ip": "198.51.100.9"})
    assert stilebase.to("/v1/x402/requirements")[0].query["ip"] == "testclient"  # a header anyone can send isn't trusted
    stilebase.calls.clear()
    with starlette(stilebase, ip_header="cf-connecting-ip") as client:
        client.get("/listings", headers={"user-agent": GPTBOT, "cf-connecting-ip": "198.51.100.9"})
    assert stilebase.to("/v1/x402/requirements")[0].query["ip"] == "198.51.100.9"


# --- WSGI (Flask) -------------------------------------------------------------------------------------------------


def flask(stilebase: FakeStilebase, **extra: Any) -> Any:
    app = Flask(__name__)
    app.add_url_rule("/listings", "listings", lambda: "the listings")
    app.wsgi_app = WsgiGate(app.wsgi_app, **options(stilebase, **extra))  # type: ignore[method-assign]
    return app.test_client()


def test_wsgi_answers_agents_serves_people_and_paid_requests(stilebase: FakeStilebase) -> None:
    client = flask(stilebase)
    base = "https://shop.example.com"
    asked = client.get(f"{base}/listings?city=Haifa", headers={"user-agent": GPTBOT})
    assert asked.status_code == 402 and asked.headers["content-length"] == str(len(asked.data))
    person = client.get(f"{base}/listings", headers=BROWSER)
    assert (person.status_code, person.text) == (200, "the listings")
    paid = client.get(f"{base}/listings", headers=PAID)
    assert (paid.status_code, paid.text, paid.headers["PAYMENT-RESPONSE"]) == (200, "the listings", SETTLED_HEADER)
    query = stilebase.to("/v1/x402/requirements")[0].query
    assert query["url"] == f"{base}/listings?city=Haifa" and query["ip"] == "127.0.0.1"


# --- Django -------------------------------------------------------------------------------------------------------


def _django(stilebase: FakeStilebase, monkeypatch: pytest.MonkeyPatch, **gate: Any) -> Any:
    """Django set up once per process; a new test Client loads the middleware again, with these STILEBASE_GATE."""
    from django.conf import settings

    if not settings.configured:
        settings.configure(
            DEBUG=False,
            ALLOWED_HOSTS=["shop.example.com"],
            ROOT_URLCONF=__name__,
            SECRET_KEY="test",
            MIDDLEWARE=["stilebase_sdk.gate.django.GateMiddleware"],
        )
        import django

        django.setup()
    monkeypatch.setattr(settings, "STILEBASE_GATE", {**options(stilebase), **gate}, raising=False)  # undone after
    from django.test import Client

    return Client(HTTP_HOST="shop.example.com")


def test_django_answers_agents_serves_people_and_paid_requests(stilebase: FakeStilebase, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _django(stilebase, monkeypatch)
    asked = client.get("/listings", HTTP_USER_AGENT=GPTBOT, secure=True)
    assert asked.status_code == 402 and asked["PAYMENT-REQUIRED"]
    person = client.get("/listings", secure=True, **{f"HTTP_{k.upper().replace('-', '_')}": v for k, v in BROWSER.items()})
    assert (person.status_code, person.content) == (200, b"the listings")
    paid = client.get("/listings", secure=True, HTTP_USER_AGENT=GPTBOT, HTTP_PAYMENT_SIGNATURE=PAID["payment-signature"])
    assert (paid.status_code, paid["PAYMENT-RESPONSE"]) == (200, SETTLED_HEADER)
    assert stilebase.to("/v1/x402/requirements")[0].query["url"] == "https://shop.example.com/listings"


def _listings(request: Any) -> Any:
    from django.http import HttpResponse

    return HttpResponse("the listings")


def _premium(request: Any, slug: str) -> Any:
    from django.http import HttpResponse

    return HttpResponse(f"PREMIUM {slug}")


def __getattr__(name: str) -> Any:  # Django's ROOT_URLCONF: this module, its urlpatterns made on first use
    if name == "urlpatterns":
        from django.urls import path, re_path

        return [path("listings", _listings), re_path(r"^premium/(?P<slug>[\w-]+)/", _premium)]
    raise AttributeError(name)


def test_django_never_frees_a_path_it_routes_elsewhere(stilebase: FakeStilebase, monkeypatch: pytest.MonkeyPatch) -> None:
    """Django routes /premium/article/../../healthz to the premium view; the gate must not read it as /healthz."""
    client = _django(stilebase, monkeypatch, free=["/healthz"])
    for path in ("/premium/article/%2E%2E/%2E%2E/healthz", "/premium/article/../../healthz"):
        answer = client.get(path, HTTP_USER_AGENT=GPTBOT, secure=True)
        assert answer.status_code == 402, (path, answer.content)


def test_django_takes_a_client_ip_given_the_request(stilebase: FakeStilebase, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _django(stilebase, monkeypatch, client_ip=lambda request: request.META.get("HTTP_X_TEST_IP"))
    client.get("/listings", HTTP_USER_AGENT=GPTBOT, HTTP_X_TEST_IP="198.51.100.9", secure=True)
    assert stilebase.to("/v1/x402/requirements")[0].query["ip"] == "198.51.100.9"


def test_django_names_a_non_ascii_page_as_the_other_adapters_do(
    stilebase: FakeStilebase, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _django(stilebase, monkeypatch)
    for path in ("/caf%C3%A9", "/x%FF"):
        client.get(path, HTTP_USER_AGENT=GPTBOT, secure=True)
    urls = [c.query["url"] for c in stilebase.to("/v1/x402/requirements")]
    assert urls == ["https://shop.example.com/caf%C3%A9", "https://shop.example.com/x%FF"]


# --- The review's cases (cycle 1) ---------------------------------------------------------------------------------


def test_asgi_uses_the_path_the_visitor_sent_under_a_mount_and_encoded(stilebase: FakeStilebase) -> None:
    from starlette.routing import Mount

    blog = Starlette(routes=[Route("/{rest:path}", lambda request: PlainTextResponse("a post"))])
    app = Starlette(routes=[Mount("/blog", app=AsgiGate(blog, **options(stilebase, free=["/blog/about"])))])
    with TestClient(app, base_url="https://shop.example.com") as client:
        assert client.get("/blog/premium/x", headers={"user-agent": GPTBOT}).status_code == 402
        assert client.get("/blog/about", headers={"user-agent": GPTBOT}).status_code == 200  # free: the full path
        assert client.get("/blog/a%3Fb.pdf", headers={"user-agent": GPTBOT}).status_code == 402  # encoded: never free
    urls = [c.query["url"] for c in stilebase.to("/v1/x402/requirements")]
    # The visitor's full path (not /premium/x), and an encoded one as sent.
    assert urls == ["https://shop.example.com/blog/premium/x", "https://shop.example.com/blog/a%3Fb.pdf"]


def test_asgi_a_host_header_with_a_question_mark_doesnt_move_the_path(stilebase: FakeStilebase) -> None:
    with starlette(stilebase, free=["/about"]) as client:
        assert client.get("/listings", headers={"user-agent": GPTBOT, "host": "shop.example.com?"}).status_code == 402


def test_wsgi_encodes_a_non_ascii_path_once(stilebase: FakeStilebase) -> None:
    client = flask(stilebase)
    client.get("https://shop.example.com/café", headers={"user-agent": GPTBOT})
    assert stilebase.to("/v1/x402/requirements")[0].query["url"] == "https://shop.example.com/caf%C3%A9"


def test_asgi_closes_the_gate_at_shutdown(stilebase: FakeStilebase) -> None:
    app = Starlette(routes=[Route("/listings", lambda request: PlainTextResponse("the listings"))])
    gate = AsgiGate(app, **options(stilebase))
    with TestClient(gate, base_url="https://shop.example.com") as client:
        assert client.get("/listings", headers={"user-agent": GPTBOT}).status_code == 402
        assert gate.gate._http is not None
    assert gate.gate._http is None  # pending reports sent, the HTTP client closed
    assert len(stilebase.to("/v1/x402/visits")) == 1


def test_a_client_ip_that_raises_doesnt_break_the_request(stilebase: FakeStilebase) -> None:
    def broken(_: Any) -> str:
        raise RuntimeError("boom")

    with starlette(stilebase, client_ip=broken) as client:
        assert client.get("/listings", headers={"user-agent": GPTBOT}).status_code == 402  # asked by user agent
        assert client.get("/listings", headers=BROWSER).status_code == 200
    assert flask(stilebase, client_ip=broken).get("/listings", headers=BROWSER).status_code == 200


def test_asgi_keeps_only_the_exact_free_path_free_however_another_page_is_spelt(stilebase: FakeStilebase, tmp_path: Any) -> None:
    from starlette.staticfiles import StaticFiles

    (tmp_path / "listings").mkdir()
    (tmp_path / "listings" / "1.html").write_text("the listing")
    (tmp_path / "robots.txt").write_text("the robots file")
    app = Starlette()
    app.mount("/", StaticFiles(directory=str(tmp_path)))
    app.add_middleware(AsgiGate, **options(stilebase, free=["/robots.txt"]))
    spellings = [
        "/listings/1.html",
        "/robots.txt/../listings/1.html",
        "/robots.txt%2F..%2Flistings/1.html",
        "/%2F/listings/1.html",
        "/x//../listings/1.html",
    ]
    with TestClient(app, base_url="https://shop.example.com") as client:
        assert client.get("/robots.txt", headers={"user-agent": GPTBOT}).text == "the robots file"
        for path in spellings:
            answer = client.get(path, headers={"user-agent": GPTBOT})
            assert (path, answer.status_code == 200 and answer.text == "the listing") == (path, False)  # never free


def test_django_on_asgi_names_the_page_by_the_raw_path(stilebase: FakeStilebase, monkeypatch: pytest.MonkeyPatch) -> None:
    """Under ASGI the server has decoded the path (uvicorn turns a broken byte into U+FFFD): the scope's raw path counts."""
    from types import SimpleNamespace

    from stilebase_sdk.gate.django import GateMiddleware

    _django(stilebase, monkeypatch)
    scope = {"raw_path": b"/x%FF", "path": "/x\ufffd", "root_path": ""}
    meta = {"PATH_INFO": "/x\ufffd", "SCRIPT_NAME": "", "HTTP_HOST": "shop.example.com", "REQUEST_METHOD": "GET"}
    middleware = GateMiddleware(lambda request: None)
    assert middleware.request(SimpleNamespace(scope=scope, META=meta, scheme="https")).url == "https://shop.example.com/x%FF"
    middleware.gate.close()
