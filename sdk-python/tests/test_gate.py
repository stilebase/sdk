"""The gate's decisions, for both drivers: every test runs with SyncGate and with AsyncGate."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from stilebase_sdk.gate import AsyncGate, GateConfig, GateOptions, GateRequest, Pass, Respond, SyncGate, pass_token
from tests.fake_stilebase import (
    API,
    BROWSER,
    CHROME,
    DOMAIN,
    GATEWAY_ID,
    GPTBOT,
    KEY,
    NOW,
    REQUIRED,
    SECRET,
    SETTLED_HEADER,
    FakeStilebase,
    live_config,
)

CONFIG_PATH = f"/v1/x402/sites/{GATEWAY_ID}/config"
PAID = {"user-agent": GPTBOT, "payment-signature": "eyJ4NDAyVmVyc2lvbiI6Mn0="}
SCRIPT = {"user-agent": CHROME, "accept": "*/*"}  # a script posing as Chrome: a suspect


def request(
    path: str, headers: dict[str, str] | None = None, ip: str | None = "203.0.113.7", url: str | None = None
) -> GateRequest:
    return GateRequest("GET", url or f"https://{DOMAIN}{path}", dict(headers or {}), ip=ip)


class Site:
    """A gate of either kind, with a clock and the errors it reported."""

    def __init__(self, kind: str, stilebase: FakeStilebase, **options: Any) -> None:
        self.clock = NOW
        self.errors: list[Exception] = []
        settings: dict[str, Any] = {
            "key": KEY,
            "api_url": API,
            "transport": stilebase.transport,
            "now": lambda: self.clock,
            "on_error": self.errors.append,
            **options,
        }
        self.loop = asyncio.new_event_loop() if kind == "async" else None
        self.gate: SyncGate | AsyncGate = (AsyncGate if kind == "async" else SyncGate)(GateOptions(**settings), adapter="test")

    def check(self, req: GateRequest) -> Pass | Respond:
        if isinstance(self.gate, AsyncGate):
            assert self.loop is not None
            return self.loop.run_until_complete(self.gate.check(req))
        return self.gate.check(req)

    def settle(self) -> None:
        """Let background reports and refreshes finish."""
        if isinstance(self.gate, AsyncGate):
            assert self.loop is not None
            if self.gate._tasks:
                self.loop.run_until_complete(asyncio.wait(set(self.gate._tasks)))
        else:
            self.gate.wait()

    def close(self) -> None:
        if isinstance(self.gate, AsyncGate):
            assert self.loop is not None
            self.loop.run_until_complete(self.gate.aclose())
            self.loop.close()
        else:
            self.gate.close()


@pytest.fixture
def stilebase() -> FakeStilebase:
    return FakeStilebase()


@pytest.fixture(params=["sync", "async"])
def make(request: pytest.FixtureRequest, stilebase: FakeStilebase) -> Iterator[Any]:
    sites: list[Site] = []

    def build(**options: Any) -> Site:
        site = Site(str(request.param), stilebase, **options)
        sites.append(site)
        return site

    yield build
    for site in sites:
        site.close()


def decoded(decision: Pass | Respond) -> dict[str, Any]:
    assert isinstance(decision, Respond)
    return dict(json.loads(base64.b64decode(decision.headers["PAYMENT-REQUIRED"])))


# --- People -------------------------------------------------------------------------------------------------------


def test_people_pass_and_stilebase_is_asked_only_for_settings_and_lists_once(make: Any, stilebase: FakeStilebase) -> None:
    site = make()
    assert site.check(request("/listings", BROWSER)) == Pass()
    assert site.check(request("/listings", BROWSER)) == Pass()
    assert [c.path for c in stilebase.calls] == [CONFIG_PATH, "/v1/x402/agent-ranges"]


@pytest.mark.parametrize("kind", ["sync", "async"])
def test_without_a_key_everyone_passes_and_it_says_so(kind: str, stilebase: FakeStilebase) -> None:
    errors: list[Exception] = []
    gate_type = AsyncGate if kind == "async" else SyncGate
    gate = gate_type(GateOptions(key="", transport=stilebase.transport, on_error=errors.append))
    decision = (
        asyncio.run(gate.check(request("/", {"user-agent": GPTBOT})))
        if isinstance(gate, AsyncGate)
        else gate.check(request("/", {"user-agent": GPTBOT}))
    )
    assert decision == Pass() and "not set" in str(errors[0]) and stilebase.calls == []


# --- AI agents ----------------------------------------------------------------------------------------------------


def test_agents_get_the_402_with_the_price_and_the_visit_is_reported(make: Any, stilebase: FakeStilebase) -> None:
    site = make()
    decision = site.check(request("/listings?city=Haifa", {"user-agent": GPTBOT}))
    assert isinstance(decision, Respond) and decision.status == 402
    assert json.loads(decision.body) == REQUIRED and decoded(decision) == REQUIRED
    assert decision.headers["cache-control"] == "no-store"
    lookup = stilebase.to("/v1/x402/requirements")[0].query
    assert lookup == {"host": DOMAIN, "url": f"https://{DOMAIN}/listings?city=Haifa", "ua": GPTBOT, "ip": "203.0.113.7"}
    site.settle()
    assert stilebase.to("/v1/x402/visits")[0].body == {
        "host": DOMAIN,
        "request": {"method": "GET", "path": "/listings?city=Haifa"},
        "agent": {"ip": "203.0.113.7", "user_agent": GPTBOT, "headers": {}},
    }


def test_a_free_pass_reads_free_and_is_reported_as_one(make: Any, stilebase: FakeStilebase) -> None:
    stilebase.requirements = lambda q: (204, None, {})
    site = make()
    assert site.check(request("/listings", {"user-agent": GPTBOT})) == Pass()
    site.settle()
    assert stilebase.to("/v1/x402/visits")[0].body["free_pass"] is True


def test_the_price_is_asked_once_while_stilebase_says_it_holds(make: Any, stilebase: FakeStilebase) -> None:
    site = make()
    site.check(request("/listings", {"user-agent": GPTBOT}))
    site.check(request("/listings", {"user-agent": GPTBOT}))
    assert len(stilebase.to("/v1/x402/requirements")) == 1
    site.clock += 301
    site.check(request("/listings", {"user-agent": GPTBOT}))
    assert len(stilebase.to("/v1/x402/requirements")) == 2


@pytest.mark.parametrize("answer", [(500, None, {}), httpx.ConnectError("refused")], ids=["500", "down"])
def test_agents_pass_when_the_price_lookup_fails(make: Any, stilebase: FakeStilebase, answer: Any) -> None:
    stilebase.requirements = lambda q: answer
    assert make().check(request("/listings", {"user-agent": GPTBOT})) == Pass()


def test_the_url_and_user_agent_are_cut_to_stilebases_limits(make: Any, stilebase: FakeStilebase) -> None:
    make().check(request("/" + "x" * 5000, {"user-agent": "GPTBot/1.0 " + "(" * 2000}))
    lookup = stilebase.to("/v1/x402/requirements")[0].query
    assert len(lookup["url"]) <= 2048 and len(lookup["ua"]) <= 1536 // 3 + 20


def test_the_page_is_named_by_the_gateways_domain(make: Any, stilebase: FakeStilebase) -> None:
    make().check(request("/a", {"user-agent": GPTBOT}, url="http://app:3000/a?b=1"))
    assert stilebase.to("/v1/x402/requirements")[0].query["url"] == f"https://{DOMAIN}/a?b=1"


# --- Scripts and hidden agents ------------------------------------------------------------------------------------


def test_scripts_pay_unless_let_through(make: Any) -> None:
    assert isinstance(make().check(request("/", {"user-agent": "curl/8.4.0"})), Respond)
    assert isinstance(make().check(request("/", {})), Respond)
    assert make(catch_http_clients=False).check(request("/", {"user-agent": "curl/8.4.0"})) == Pass()


def test_hidden_agents_pay_by_address_or_web_bot_auth(make: Any) -> None:
    assert isinstance(make().check(request("/", BROWSER, ip="20.15.240.70")), Respond)
    assert isinstance(make().check(request("/", BROWSER, ip="::ffff:20.15.240.70")), Respond)
    assert isinstance(make().check(request("/", {**BROWSER, "signature-agent": "https://chatgpt.com"})), Respond)
    assert make(catch_hidden_agents=False).check(request("/", BROWSER, ip="20.15.240.70")) == Pass()


# --- Browser-looking requests that don't act like a browser --------------------------------------------------------


def test_suspects_are_counted_while_strict_detection_is_off(make: Any, stilebase: FakeStilebase) -> None:
    stilebase.requirements = lambda q: (204, None, {})
    site = make()
    assert site.check(request("/listings", SCRIPT)) == Pass()
    assert stilebase.to("/v1/x402/requirements")[0].query["suspect"] == "true"
    site.settle()
    assert stilebase.to("/v1/x402/visits")[0].body["signals"] == [
        "no_accept_language",
        "no_sec_fetch",
        "chrome_without_client_hints",
        "accept_any",
    ]


def test_suspects_get_a_page_under_strict_detection_and_its_cookie_lets_them_pass(make: Any) -> None:
    site = make()
    decision = site.check(request("/listings", {**SCRIPT, "accept": "text/html"}))
    assert isinstance(decision, Respond) and decision.status == 402
    assert decision.headers["content-type"].startswith("text/html") and decision.headers["PAYMENT-REQUIRED"]
    token = pass_token(SECRET, CHROME, int(NOW))
    assert f"stilebase_pass={token}".encode() in decision.body
    assert site.check(request("/listings", {**SCRIPT, "cookie": f"a=1; stilebase_pass={token}"})) == Pass()
    forged = f"{int(NOW)}.{'0' * 32}"
    assert isinstance(site.check(request("/listings", {**SCRIPT, "cookie": f"stilebase_pass={forged}"})), Respond)
    site.clock += 43201  # older than max_age_seconds
    assert isinstance(site.check(request("/listings", {**SCRIPT, "cookie": f"stilebase_pass={token}"})), Respond)


def test_suspects_are_never_checked_over_http_or_with_the_checks_off(make: Any) -> None:
    assert make().check(request("/", SCRIPT, url=f"http://{DOMAIN}/")) == Pass()
    assert make(check_browsers=False).check(request("/", SCRIPT)) == Pass()


# --- Payments -----------------------------------------------------------------------------------------------------


def test_a_settled_payment_lets_the_request_through_with_payment_response(make: Any, stilebase: FakeStilebase) -> None:
    assert make().check(request("/listings", PAID)) == Pass(SETTLED_HEADER)
    settle = stilebase.to("/v1/x402/settle")[0]
    assert settle.body["payment"] == PAID["payment-signature"] and settle.body["request"] == {
        "method": "GET",
        "path": "/listings",
    }
    assert settle.headers["stilebase-signature"].startswith("v2=")


def test_x402_v1s_x_payment_too_and_an_empty_payment_signature_is_none(make: Any, stilebase: FakeStilebase) -> None:
    make().check(request("/", {"user-agent": GPTBOT, "x-payment": "v1"}))
    make().check(request("/", {"user-agent": GPTBOT, "payment-signature": "", "x-payment": "v1"}))
    assert [c.body["payment"] for c in stilebase.to("/v1/x402/settle")] == ["v1", "v1"]


def test_a_refused_payment_gets_the_402_again_with_the_reason(make: Any, stilebase: FakeStilebase) -> None:
    stilebase.settle = (200, {"success": False, "errorReason": "insufficient_funds", "header": "eyJzdWNjZXNzIjpmYWxzZX0="}, {})
    decision = make().check(request("/listings", PAID))
    assert isinstance(decision, Respond) and decision.status == 402
    assert (
        decoded(decision)["error"] == "insufficient_funds" and decision.headers["PAYMENT-RESPONSE"] == "eyJzdWNjZXNzIjpmYWxzZX0="
    )


@pytest.mark.parametrize(
    "answer",
    [httpx.ConnectError("refused"), (502, None, {}), (200, {"transaction": "0xabc"}, {})],
    ids=["down", "502", "no success"],
)
def test_a_payment_stilebase_doesnt_confirm_gets_503(make: Any, stilebase: FakeStilebase, answer: Any) -> None:
    stilebase.settle = answer
    decision = make().check(request("/listings", PAID))
    assert isinstance(decision, Respond) and decision.status == 503 and decision.headers["retry-after"] == "5"


def test_a_payment_without_a_price_to_meet_passes_without_settling(make: Any, stilebase: FakeStilebase) -> None:
    stilebase.requirements = lambda q: (204, None, {})
    assert make().check(request("/listings", PAID)) == Pass()
    assert stilebase.to("/v1/x402/settle") == []


# --- Settings -----------------------------------------------------------------------------------------------------


def test_settings_are_fetched_signed_saying_which_package_this_is(make: Any, stilebase: FakeStilebase) -> None:
    make().check(request("/", BROWSER))
    call = stilebase.calls[0]
    assert call.headers["stilebase-client"].startswith("stilebase-sdk/") and call.headers["stilebase-client"].endswith(
        " gate/test"
    )
    assert call.headers["stilebase-signature"].startswith("v2=")


def test_the_verification_file_is_served_even_before_the_gateway_is_live(make: Any, stilebase: FakeStilebase) -> None:
    gateway = {"id": GATEWAY_ID, "domain": DOMAIN, "live": False, "reason": "domain_not_verified"}
    verification = {"path": "/.well-known/stilebase-verify.txt", "value": "stilebase-verify=tok"}
    stilebase.config = (200, live_config(gateway=gateway, verification=verification), {})
    site = make()
    answer = site.check(request("/.well-known/stilebase-verify.txt"))
    assert isinstance(answer, Respond) and (answer.status, answer.body) == (200, b"stilebase-verify=tok\n")
    assert site.check(request("/listings", {"user-agent": GPTBOT})) == Pass()  # not live: nobody is asked


def test_settings_refresh_in_the_background_keeping_the_last_good_ones(make: Any, stilebase: FakeStilebase) -> None:
    site = make()
    site.check(request("/", BROWSER))
    stilebase.config = (500, None, {})
    site.clock += 301
    site.check(request("/", BROWSER))
    site.settle()
    assert len(stilebase.to(CONFIG_PATH)) == 2
    assert isinstance(site.check(request("/listings", {"user-agent": GPTBOT})), Respond)  # still gated


def test_a_removed_gateway_404_stops_the_asking(make: Any, stilebase: FakeStilebase) -> None:
    site = make()
    site.check(request("/", BROWSER))
    stilebase.config = (404, None, {})
    site.clock += 301
    site.check(request("/", BROWSER))
    site.settle()
    assert site.check(request("/listings", {"user-agent": GPTBOT})) == Pass()


def test_when_stilebase_is_down_at_start_everyone_passes_and_it_is_tried_again(make: Any, stilebase: FakeStilebase) -> None:
    stilebase.config = httpx.ConnectError("refused")
    site = make()
    assert site.check(request("/listings", {"user-agent": GPTBOT})) == Pass()
    stilebase.config = (200, live_config(), {})
    site.clock += 30
    site.check(request("/listings", {"user-agent": GPTBOT}))
    site.settle()
    assert isinstance(site.check(request("/listings", {"user-agent": GPTBOT})), Respond)


# --- The review's cases (cycle 1) ---------------------------------------------------------------------------------


def test_the_last_address_lists_are_kept_when_an_answer_has_none(make: Any, stilebase: FakeStilebase) -> None:
    site = make()
    site.check(request("/", BROWSER, ip="20.15.240.70"))
    stilebase.ranges = (200, {"nothing": True}, {})
    site.clock += 3601
    site.check(request("/", BROWSER, ip="20.15.240.70"))
    site.settle()
    assert isinstance(site.check(request("/", BROWSER, ip="20.15.240.70")), Respond)


# --- The review's cases (cycle 3) ---------------------------------------------------------------------------------


def test_a_path_starting_with_two_slashes_is_named_as_the_js_sdk_names_it() -> None:
    from stilebase_sdk.gate._detect import path_of

    assert path_of("//premium/article.html") == "//premium/article.html"
    assert path_of("https://shop.example.com//premium/a?b=1") == "//premium/a?b=1"


def test_a_header_is_trimmed_as_javascript_trims_it() -> None:
    from stilebase_sdk.gate._detect import is_script
    from tests.fake_stilebase import EXAMPLE

    config = GateConfig.parse(EXAMPLE)
    assert config is not None
    assert is_script("\x85", config) is False and is_script(" \t", config) is True


def test_the_402_is_written_as_json_stringify_writes_it(make: Any, stilebase: FakeStilebase) -> None:
    stilebase.requirements = lambda q: (200, {**REQUIRED, "extra": {"name": "caf\u00e9"}}, {})
    decision = make().check(request("/listings", {"user-agent": GPTBOT}))
    assert isinstance(decision, Respond)
    assert (
        decision.body
        == json.dumps({**REQUIRED, "extra": {"name": "caf\u00e9"}}, separators=(",", ":"), ensure_ascii=False).encode()
    )


# --- The review's cases (cycle 4) ---------------------------------------------------------------------------------


def test_a_lone_surrogate_in_the_price_is_escaped_as_json_stringify_escapes_it() -> None:
    """A price cut in the middle of an emoji: the 402 is still written (JSON.stringify escapes it), never an error."""
    from stilebase_sdk.gate._core import _payment_required

    decision = _payment_required({**REQUIRED, "extra": "\ud83d"})
    assert decision.status == 402 and b'"extra":"\\ud83d"' in decision.body


def test_addresses_and_relative_paths_are_read_as_the_js_sdk_reads_them() -> None:
    from stilebase_sdk.gate._detect import normalize_ip, path_of

    assert normalize_ip("20.15.240.70\x85") == "20.15.240.70\x85"
    assert path_of("premium/x") == "/premium/x"


# --- The review's cases (cycle 5) ---------------------------------------------------------------------------------


def test_a_lone_surrogate_in_the_verification_value_is_written_as_u_fffd(make: Any, stilebase: FakeStilebase) -> None:
    from stilebase_sdk.gate._core import _respond

    assert _respond(200, {}, "tok\ud83d\n").body == "tok�\n".encode()


def test_a_future_from_client_ip_is_left_to_its_owner() -> None:
    from stilebase_sdk.gate.asgi import GateMiddleware

    async def app(scope: Any, receive: Any, send: Any) -> None: ...

    async def run() -> None:
        shared: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        middleware = GateMiddleware(app, client_ip=lambda scope: shared, key=KEY, api_url=API)  # type: ignore[arg-type,return-value]
        assert middleware.request({"type": "http", "path": "/", "headers": []}).ip is None
        assert not shared.cancelled()  # the app may be waiting on it too
        shared.set_result("198.51.100.9")

    asyncio.run(run())
