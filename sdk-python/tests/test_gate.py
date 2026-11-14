"""The gate's decisions, for both drivers: every test runs with SyncGate and with AsyncGate."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from stilebase_sdk.gate import AsyncGate, GateOptions, GateRequest, Pass, Respond, SyncGate, pass_token
from stilebase_sdk.gate._signing import MAX_PASS_CANDIDATES
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
SCRIPT = {"user-agent": CHROME, "accept": "*/*"}


def request(
    path: str, headers: dict[str, str] | None = None, ip: str | None = "203.0.113.7", url: str | None = None
) -> GateRequest:
    return GateRequest("GET", url or f"https://{DOMAIN}{path}", dict(headers or {}), ip=ip)


class Site:
    """A gate of either kind, with a clock and the errors it reported."""

    def __init__(self, kind: str, stilebase: FakeStilebase, **options: Any) -> None:
        self.kind = kind
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
            tasks = list(self.gate._tasks)
            if tasks:
                self.loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
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
def kind(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def make(kind: str, stilebase: FakeStilebase) -> Iterator[Any]:
    sites: list[Site] = []

    def build(**options: Any) -> Site:
        site = Site(kind, stilebase, **options)
        sites.append(site)
        return site

    yield build
    for site in sites:
        site.close()


def decoded(decision: Pass | Respond) -> dict[str, Any]:
    assert isinstance(decision, Respond)
    return dict(json.loads(base64.b64decode(decision.headers["PAYMENT-REQUIRED"])))


# --- People and settings ------------------------------------------------------------------------------------------


def test_people_pass_without_stilebase_being_asked_about_them(make: Any, stilebase: FakeStilebase) -> None:
    site = make()
    assert site.check(request("/listings", BROWSER)) == Pass()
    assert site.check(request("/listings", BROWSER)) == Pass()
    assert [c.path for c in stilebase.calls] == [CONFIG_PATH, "/v1/x402/agent-ranges"]


def test_without_a_key_every_request_passes_and_it_says_so(kind: str, stilebase: FakeStilebase) -> None:
    errors: list[Exception] = []
    gate_class = AsyncGate if kind == "async" else SyncGate
    gate = gate_class(GateOptions(key="", transport=stilebase.transport, on_error=errors.append))
    asked = request("/", {"user-agent": GPTBOT})
    decision = asyncio.run(gate.check(asked)) if isinstance(gate, AsyncGate) else gate.check(asked)
    assert decision == Pass() and "is not set" in str(errors[0])
    gate_class(GateOptions(key="sbsec_old", on_error=errors.append))
    assert "is not a site key" in str(errors[1])
    assert stilebase.calls == []


def test_settings_are_fetched_signed_saying_which_package_this_is(make: Any, stilebase: FakeStilebase) -> None:
    make().check(request("/", BROWSER))
    call = stilebase.calls[0]
    client = call.headers["stilebase-client"]
    assert client.startswith("stilebase-sdk/") and client.endswith(" gate/test")
    assert call.headers["stilebase-timestamp"] == str(int(NOW))
    assert call.headers["stilebase-signature"].startswith("v2=")


def test_a_refused_key_lets_everyone_through(make: Any, stilebase: FakeStilebase) -> None:
    stilebase.config = (401, None, {})
    site = make()
    assert site.check(request("/listings", {"user-agent": GPTBOT})) == Pass()
    assert "refused the site key" in str(site.errors[0])


def test_when_stilebase_is_down_at_start_requests_dont_each_wait(make: Any, stilebase: FakeStilebase) -> None:
    stilebase.config = httpx.ConnectError("refused")
    site = make()
    assert site.check(request("/listings", {"user-agent": GPTBOT})) == Pass()
    site.clock += 10
    assert site.check(request("/listings", {"user-agent": GPTBOT})) == Pass()
    assert len(stilebase.to(CONFIG_PATH)) == 1
    stilebase.config = (200, live_config(), {})
    site.clock += 30
    site.check(request("/listings", {"user-agent": GPTBOT}))  # starts a retry in the background
    site.settle()
    assert isinstance(site.check(request("/listings", {"user-agent": GPTBOT})), Respond)


def test_settings_refresh_in_the_background_and_keep_the_last_good_ones(make: Any, stilebase: FakeStilebase) -> None:
    site = make()
    site.check(request("/", BROWSER))
    site.clock += 299
    site.check(request("/", BROWSER))
    assert len(stilebase.to(CONFIG_PATH)) == 1
    stilebase.config = (500, None, {})
    site.clock += 2
    site.check(request("/", BROWSER))
    site.check(request("/", BROWSER))  # one refresh at a time
    site.settle()
    assert len(stilebase.to(CONFIG_PATH)) == 2
    assert isinstance(site.check(request("/listings", {"user-agent": GPTBOT})), Respond)  # still gated


def test_the_verification_file_is_served_until_the_domain_is_proven(make: Any, stilebase: FakeStilebase) -> None:
    gateway = {"id": GATEWAY_ID, "domain": DOMAIN, "live": False, "reason": "domain_not_verified"}
    verification = {"path": "/.well-known/stilebase-verify.txt", "value": "stilebase-verify=tok"}
    stilebase.config = (200, live_config(gateway=gateway, verification=verification), {})
    site = make()
    assert site.check(request("/.well-known/stilebase-verify.txt")) == Respond(
        200, {"content-type": "text/plain; charset=utf-8", "cache-control": "no-store"}, b"stilebase-verify=tok\n"
    )
    assert site.check(request("/listings", {"user-agent": GPTBOT})) == Pass()  # not live: nobody is asked
    assert stilebase.to("/v1/x402/requirements") == []


# --- AI agents ----------------------------------------------------------------------------------------------------


def test_agents_get_the_402_and_the_visit_is_reported(make: Any, stilebase: FakeStilebase) -> None:
    site = make()
    decision = site.check(request("/listings?city=Haifa", {"user-agent": GPTBOT}, ip="198.51.100.4"))
    assert isinstance(decision, Respond) and decision.status == 402
    assert decoded(decision) == REQUIRED and json.loads(decision.body) == REQUIRED
    assert stilebase.to("/v1/x402/requirements")[0].query == {
        "host": DOMAIN,
        "url": f"https://{DOMAIN}/listings?city=Haifa",
        "ua": GPTBOT,
        "ip": "198.51.100.4",
    }
    site.settle()
    visit = stilebase.to("/v1/x402/visits")[0]
    assert visit.body == {
        "host": DOMAIN,
        "request": {"method": "GET", "path": "/listings?city=Haifa"},
        "agent": {"ip": "198.51.100.4", "user_agent": GPTBOT, "headers": {}},
    }
    assert visit.headers["stilebase-signature"].startswith("v2=")


def test_a_free_pass_reads_for_free_and_is_reported_as_one(make: Any, stilebase: FakeStilebase) -> None:
    stilebase.requirements = lambda q: (204, None, {"cache-control": "public, max-age=300"})
    site = make()
    assert site.check(request("/listings", {"user-agent": GPTBOT})) == Pass()
    site.settle()
    assert stilebase.to("/v1/x402/visits")[0].body["free_pass"] is True


def test_requirements_are_asked_once_for_five_minutes(make: Any, stilebase: FakeStilebase) -> None:
    site = make()
    site.check(request("/listings", {"user-agent": GPTBOT}))
    site.check(request("/listings", {"user-agent": GPTBOT}))
    assert len(stilebase.to("/v1/x402/requirements")) == 1
    site.clock += 301
    site.check(request("/listings", {"user-agent": GPTBOT}))
    assert len(stilebase.to("/v1/x402/requirements")) == 2


def test_agents_pass_when_stilebase_has_no_live_gateway_or_cant_be_reached(make: Any, stilebase: FakeStilebase) -> None:
    stilebase.requirements = lambda q: (404, None, {})
    assert make().check(request("/listings", {"user-agent": GPTBOT})) == Pass()
    stilebase.requirements = lambda q: httpx.ReadTimeout("slow")
    assert make().check(request("/listings", {"user-agent": GPTBOT})) == Pass()


def test_a_raw_url_with_a_forged_host_is_never_free(make: Any) -> None:
    site = make(free=["/", "/healthz"])
    for url in ("http://x?/premium/report", "http://x#/premium/report", f"http://{DOMAIN}?/premium/report"):
        assert isinstance(site.check(request("/", {"user-agent": GPTBOT}, url=url)), Respond), url


def test_free_entries_that_can_never_match_are_reported(make: Any) -> None:
    site = make(free=["healthz", "/robots.txt?x", "/a/../b", "/a#b", "/%68ealthz", "/healthz"])
    assert site.check(request("/healthz", {"user-agent": GPTBOT})) == Pass()
    reports = [str(e) for e in site.errors if "can never match" in str(e)]
    assert len(reports) == 1
    listed = reports[0].split(" can never match")[0]
    for entry in ("'healthz'", "'/robots.txt?x'", "'/a/../b'", "'/a#b'", "'/%68ealthz'"):
        assert entry in listed
    assert "'/healthz'" not in listed


def test_every_path_is_gated_except_the_free_paths_listed_exactly(make: Any) -> None:
    site = make(free=["/healthz", "/robots.txt"])
    assert site.check(request("/healthz", {"user-agent": GPTBOT})) == Pass()
    assert site.check(request("/robots.txt", {"user-agent": GPTBOT}, url=f"https://{DOMAIN}/robots.txt?x=1")) == Pass()
    for path in ("/listings/2", "/healthz/", "/HEALTHZ", "/healthz/../listings/2", "/x/../healthz", "//healthz", "/%68ealthz"):
        assert isinstance(site.check(request(path, {"user-agent": GPTBOT})), Respond), path
    assert isinstance(make(free="/healthz").check(request("/h", {"user-agent": GPTBOT})), Respond)  # one path, not chars


def test_in_test_mode_only_stilebases_test_agent_is_asked(make: Any) -> None:
    site = make(mode="test")
    assert site.check(request("/listings", {"user-agent": GPTBOT})) == Pass()
    assert site.check(request("/listings", {"user-agent": "curl/8.4.0"})) == Pass()
    assert isinstance(site.check(request("/listings", {"user-agent": "Mozilla/5.0 (compatible; StilebaseCheck/1.0)"})), Respond)


# --- Hidden agents and scripts ------------------------------------------------------------------------------------


def test_scripts_pay_unless_let_through(make: Any) -> None:
    assert isinstance(make().check(request("/listings", {"user-agent": "curl/8.4.0"})), Respond)
    assert isinstance(make().check(request("/listings", {})), Respond)  # no user agent at all
    assert make(catch_http_clients=False).check(request("/listings", {"user-agent": "curl/8.4.0"})) == Pass()


def test_hidden_agents_pay_by_address_or_web_bot_auth(make: Any, stilebase: FakeStilebase) -> None:
    assert isinstance(make().check(request("/listings", BROWSER, ip="20.15.240.70")), Respond)
    assert isinstance(make().check(request("/listings", BROWSER, ip="::ffff:20.15.240.70")), Respond)  # dual-stack servers
    assert isinstance(make().check(request("/listings", {**BROWSER, "signature-agent": '"https://chatgpt.com"'})), Respond)
    assert make().check(request("/listings", BROWSER, ip="20.15.240.80")) == Pass()
    assert make(catch_hidden_agents=False).check(request("/listings", BROWSER, ip="20.15.240.70")) == Pass()


# --- Browser-looking requests that don't act like a browser -------------------------------------------------------


def test_suspects_are_only_counted_while_strict_detection_is_off(make: Any, stilebase: FakeStilebase) -> None:
    stilebase.requirements = lambda q: (204, None, {}) if q.get("suspect") == "true" else (200, REQUIRED, {})
    site = make()
    assert site.check(request("/listings", SCRIPT)) == Pass()
    site.settle()
    signals = ["no_accept_language", "no_sec_fetch", "chrome_without_client_hints", "accept_any"]
    assert stilebase.to("/v1/x402/visits")[0].body["signals"] == signals


def test_suspects_get_a_page_that_lets_real_browsers_through_under_strict_detection(make: Any) -> None:
    decision = make().check(request("/listings", {**SCRIPT, "accept": "text/html"}))
    assert isinstance(decision, Respond) and decision.status == 402
    assert decision.headers["content-type"].startswith("text/html")
    assert b"stilebase_pass=" in decision.body and decision.headers["PAYMENT-REQUIRED"]


def test_the_browser_checks_cookie_passes_without_asking_stilebase(make: Any, stilebase: FakeStilebase) -> None:
    token = pass_token(SECRET, CHROME, int(NOW) - 60)
    site = make()
    assert site.check(request("/listings", {**SCRIPT, "cookie": f"a=1; stilebase_pass={token}"})) == Pass()
    assert stilebase.to("/v1/x402/requirements") == []
    forged = f"{int(NOW)}.{'0' * 32}"
    assert isinstance(site.check(request("/listings", {**SCRIPT, "cookie": f"stilebase_pass={forged}"})), Respond)


def test_suspects_are_checked_when_the_app_sees_http_unless_the_site_really_is_http(make: Any) -> None:
    over_http = request("/listings", SCRIPT, url=f"http://{DOMAIN}/listings")
    assert isinstance(make().check(over_http), Respond)  # a proxy ended TLS: the visitor came over HTTPS
    assert make(assume_https=False).check(over_http) == Pass()
    assert make(check_browsers=False).check(request("/listings", SCRIPT)) == Pass()


# --- Payments -----------------------------------------------------------------------------------------------------


def test_a_settled_payment_lets_the_request_through_with_payment_response(make: Any, stilebase: FakeStilebase) -> None:
    assert make().check(request("/listings/2", PAID, ip="198.51.100.4")) == Pass(SETTLED_HEADER)
    assert stilebase.to("/v1/x402/settle")[0].body == {
        "host": DOMAIN,
        "payment": PAID["payment-signature"],
        "request": {"method": "GET", "path": "/listings/2"},
        "agent": {"ip": "198.51.100.4", "user_agent": GPTBOT, "headers": {}},
    }


def test_x402_v1s_x_payment_header_too(make: Any, stilebase: FakeStilebase) -> None:
    make().check(request("/listings/2", {"user-agent": GPTBOT, "x-payment": "v1payment"}))
    assert stilebase.to("/v1/x402/settle")[0].body["payment"] == "v1payment"


def test_a_refused_payment_gets_the_402_again_with_the_reason(make: Any, stilebase: FakeStilebase) -> None:
    refused = {"success": False, "errorReason": "invalid_exact_evm_payload_signature", "header": "eyJmYWlsIjp0cnVlfQ=="}
    stilebase.settle = (200, refused, {})
    decision = make().check(request("/listings/2", PAID))
    assert isinstance(decision, Respond) and decision.status == 402
    assert json.loads(decision.body)["error"] == "invalid_exact_evm_payload_signature"
    assert decision.headers["PAYMENT-RESPONSE"] == "eyJmYWlsIjp0cnVlfQ=="


@pytest.mark.parametrize(
    "answer",
    [
        (502, None, {}),
        httpx.ReadTimeout("slow"),
        (200, None, {}),
        (200, [], {}),
        (200, 1, {}),
        (200, {}, {}),
        (200, {"success": "false"}, {}),
    ],
    ids=["502", "timeout", "null", "list", "number", "no-success", "string-success"],
)
def test_a_payment_that_cant_be_settled_gets_503(make: Any, stilebase: FakeStilebase, answer: Any) -> None:
    stilebase.settle = answer
    decision = make().check(request("/listings/2", PAID))
    assert isinstance(decision, Respond) and decision.status == 503 and decision.headers["retry-after"] == "5"


# --- Never breaking the site --------------------------------------------------------------------------------------


def test_a_failing_error_handler_still_lets_requests_through(make: Any, stilebase: FakeStilebase) -> None:
    def boom(error: Exception) -> None:
        raise RuntimeError("boom")

    stilebase.config = (401, None, {})
    assert make(on_error=boom).check(request("/", {"user-agent": GPTBOT})) == Pass()


def test_an_agent_cant_dodge_the_gate_with_a_url_the_parser_rejects(make: Any) -> None:
    site = make()
    for url in ("http://[bad", "https://[::::]/listings/1", "https://[1.2.3.4]/x", "not a url"):
        assert isinstance(site.check(GateRequest("GET", url, {"user-agent": GPTBOT})), Respond), url


def test_it_warns_through_logging_once_in_a_while(make: Any, stilebase: FakeStilebase, caplog: pytest.LogCaptureFixture) -> None:
    stilebase.config = (401, None, {})
    site = make(on_error=None)
    with caplog.at_level(logging.WARNING, logger="stilebase_sdk.gate"):
        site.check(request("/", {"user-agent": GPTBOT}))
        site.clock += 31
        site.check(request("/", {"user-agent": GPTBOT}))
        site.settle()
    assert len([r for r in caplog.records if "refused the site key" in r.getMessage()]) == 1


# --- The review's cases (cycle 1) ---------------------------------------------------------------------------------


def test_long_values_are_cut_so_stilebases_call_cant_be_refused(make: Any, stilebase: FakeStilebase) -> None:
    site = make()
    decision = site.check(request(f"/a?pad={'x' * 5000}", {"user-agent": f"GPTBot/1.2 {'y' * 3000}"}))
    assert isinstance(decision, Respond)
    asked = stilebase.to("/v1/x402/requirements")[0].query
    assert (len(asked["url"]), len(asked["ua"])) == (2048, 512)
    site.settle()
    visit = stilebase.to("/v1/x402/visits")[0].body
    assert (len(visit["request"]["path"]), len(visit["agent"]["user_agent"])) == (2048, 512)


def test_settings_of_another_version_let_everyone_through(make: Any, stilebase: FakeStilebase) -> None:
    stilebase.config = (200, live_config(config_version=2), {})
    site = make()
    assert site.check(request("/listings", {"user-agent": GPTBOT})) == Pass()
    assert "can't read" in str(site.errors[0])


# --- The review's cases (cycle 2) ---------------------------------------------------------------------------------


def test_scripts_pay_from_loopback_too_and_owners_exempt_their_health_checks(make: Any) -> None:
    curl = {"user-agent": "curl/8.4.0"}
    assert isinstance(make().check(request("/", curl, ip="127.0.0.1")), Respond)  # a same-host proxy looks local
    assert make(free=["/healthz"]).check(request("/healthz", curl, ip="127.0.0.1")) == Pass()
    assert make(catch_http_clients=False).check(request("/", curl, ip="127.0.0.1")) == Pass()


def test_a_forwarded_list_gives_its_last_entry_the_one_the_proxy_added() -> None:
    from stilebase_sdk.gate._core import address_from

    assert address_from({"x-forwarded-for": "20.15.240.70, 198.51.100.9"}, "X-Forwarded-For", None) == "198.51.100.9"
    assert address_from({}, "x-forwarded-for", "10.0.0.1") is None
    assert address_from({}, None, "10.0.0.1") == "10.0.0.1"


def test_a_sync_refresh_isnt_stuck_behind_slow_reports(stilebase: FakeStilebase) -> None:
    import threading

    release = threading.Event()
    handle = stilebase.handle

    def slow_visits(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/x402/visits":
            release.wait(10)
        return handle(req)

    clock = [NOW]
    gate = SyncGate(
        GateOptions(
            key=KEY, api_url=API, transport=httpx.MockTransport(slow_visits), now=lambda: clock[0], on_error=lambda e: None
        )
    )
    try:
        assert isinstance(gate.check(request("/listings", {"user-agent": GPTBOT})), Respond)  # its report is now stuck
        clock[0] += 301
        gate.check(request("/", BROWSER))  # settings due: refreshed on their own thread
        for _ in range(100):
            if len(stilebase.to(CONFIG_PATH)) == 2:
                break
            threading.Event().wait(0.02)
        assert len(stilebase.to(CONFIG_PATH)) == 2
    finally:
        release.set()
        gate.close()


# --- The review's cases (cycle 3) ---------------------------------------------------------------------------------


def test_the_page_is_named_by_the_gateways_domain_not_the_host_header(make: Any, stilebase: FakeStilebase) -> None:
    make().check(GateRequest("GET", "http://app:3000/listings/1?a=b", {"user-agent": GPTBOT}))
    assert stilebase.to("/v1/x402/requirements")[0].query["url"] == f"https://{DOMAIN}/listings/1?a=b"
    make().check(GateRequest("GET", "//evil.example/x", {"user-agent": GPTBOT}))
    assert stilebase.to("/v1/x402/requirements")[1].query["url"] == f"https://{DOMAIN}/evil.example/x"


def test_the_price_lookup_stays_under_proxies_8_kb_limit(make: Any, stilebase: FakeStilebase) -> None:
    from urllib.parse import urlencode

    make().check(request(f"/listings/1?{'/' * 2048}", {"user-agent": f"GPTBot/1.2 {'é' * 512}"}))
    assert len(urlencode(stilebase.to("/v1/x402/requirements")[0].query)) < 6000


def test_a_refused_lookup_is_answered_with_the_last_price(make: Any, stilebase: FakeStilebase) -> None:
    site = make()
    site.check(request("/listings/1", {"user-agent": GPTBOT}))
    stilebase.requirements = lambda q: (414, None, {})
    decision = site.check(request("/listings/2", {"user-agent": GPTBOT}))
    assert isinstance(decision, Respond) and decision.status == 402
    assert json.loads(decision.body)["resource"]["url"] == f"https://{DOMAIN}/listings/2"
    assert make().check(request("/listings/2", {"user-agent": GPTBOT})) == Pass()  # no price known yet: never break the site


def test_a_receipt_that_cant_be_a_header_is_dropped(make: Any, stilebase: FakeStilebase) -> None:
    stilebase.settle = (200, {"success": True, "transaction": "0xabc", "header": "bad\r\nvalue"}, {})
    assert make().check(request("/listings/2", PAID)) == Pass()
    stilebase.settle = (200, {"success": False, "errorReason": "insufficient_funds", "header": "bad\r\nvalue"}, {})
    refused = make().check(request("/listings/2", PAID))
    assert isinstance(refused, Respond) and refused.status == 402 and "PAYMENT-RESPONSE" not in refused.headers


def test_an_address_that_isnt_a_string_is_none_and_agents_still_pay(make: Any) -> None:
    odd = GateRequest("GET", f"https://{DOMAIN}/listings/2", {"user-agent": GPTBOT}, ip={"hostname": "1.2.3.4"})  # type: ignore[arg-type]
    decision = make().check(odd)
    assert isinstance(decision, Respond) and decision.status == 402


def test_a_rate_limited_lookup_is_answered_with_the_last_price_too(make: Any, stilebase: FakeStilebase) -> None:
    site = make()
    site.check(request("/listings/1", {"user-agent": GPTBOT}))
    stilebase.requirements = lambda q: (429, None, {})
    decision = site.check(request("/listings/2?x=1", {"user-agent": GPTBOT}))
    assert isinstance(decision, Respond) and decision.status == 402  # many unique URLs mustn't read free


@pytest.mark.parametrize("refresh", [None, "soon", 0, -5, True], ids=["null", "text", "zero", "negative", "bool"])
def test_settings_with_an_odd_refresh_run_on_the_default(make: Any, stilebase: FakeStilebase, refresh: Any) -> None:
    stilebase.config = (200, live_config(refresh_seconds=refresh), {})
    assert isinstance(make().check(request("/listings/1", {"user-agent": GPTBOT})), Respond)  # the settings were used


def test_the_lookup_is_form_encoded_as_javascript_does() -> None:
    from stilebase_sdk.gate._detect import fit_encoded, form_quote

    assert form_quote("a b~*é") == "a+b%7E*%C3%A9"  # URLSearchParams: "a+b%7E*%C3%A9"
    assert fit_encoded("~" * 10, 9) == "~~~"  # each ~ is 3 encoded characters, as in the JS SDK


def test_a_pass_cookie_with_a_huge_number_is_refused_not_a_crash(make: Any) -> None:
    script = {"user-agent": CHROME, "accept": "text/html", "cookie": f"stilebase_pass={'9' * 5000}.{'0' * 32}"}
    assert isinstance(make().check(request("/listings", script)), Respond)


# --- The review's cases (cycle 4) ---------------------------------------------------------------------------------


def test_an_over_long_address_counts_as_none(make: Any, stilebase: FakeStilebase) -> None:
    make().check(request("/listings", {"user-agent": GPTBOT}, ip="198.51.100.4, " + "1.1.1.1, " * 500))
    assert stilebase.to("/v1/x402/requirements")[0].query["ip"] == ""


# --- The review's cases (cycle 7) ---------------------------------------------------------------------------------


def test_a_refused_refresh_keeps_asking_agents_to_pay(make: Any, stilebase: FakeStilebase) -> None:
    site = make()
    site.check(request("/listings", {"user-agent": GPTBOT}))
    stilebase.config = (401, None, {})
    site.clock += 301
    site.check(request("/", BROWSER))  # the refresh is refused (a rotated key, or this server's clock off)
    site.settle()
    assert isinstance(site.check(request("/listings", {"user-agent": GPTBOT})), Respond)  # never a free read
    assert any("refused the site key" in str(e) for e in site.errors)


def test_a_removed_gateway_stops_the_asking(make: Any, stilebase: FakeStilebase) -> None:
    site = make()
    site.check(request("/listings", {"user-agent": GPTBOT}))
    stilebase.config = (404, None, {})
    site.clock += 301
    site.check(request("/", BROWSER))
    site.settle()
    assert site.check(request("/listings", {"user-agent": GPTBOT})) == Pass()


# --- The review's cases (cycles 13 to 17 of @stilebase/sdk) -------------------------------------------------------------


def test_nothing_after_a_fragment_is_free(make: Any) -> None:
    """A forged Host can put a "#" in the URL; a router reads past it, so /healthz#/listings/1 is no free /healthz."""
    site = make(free=["/healthz", "/"])
    for url in (f"https://{DOMAIN}/healthz#/listings/1", f"https://{DOMAIN}/#/listings/1", f"https://{DOMAIN}/healthz?a#/x"):
        assert isinstance(site.check(request("/", {"user-agent": GPTBOT}, url=url)), Respond), url
    assert site.check(request("/", {"user-agent": GPTBOT}, url=f"https://{DOMAIN}/healthz?a=1")) == Pass()


def test_the_pass_cookie_is_found_when_cookie_fields_are_joined_with_a_comma(make: Any) -> None:
    token = pass_token(SECRET, CHROME, int(NOW) - 60)
    assert make().check(request("/listings", {**SCRIPT, "cookie": f"a=1, stilebase_pass={token}"})) == Pass()


def test_a_page_with_a_hash_is_named_by_the_part_before_it_and_never_free(make: Any, stilebase: FakeStilebase) -> None:
    site = make(free=["/premium"])
    assert isinstance(site.check(request("/", {"user-agent": GPTBOT}, url=f"https://{DOMAIN}/premium#/cheap")), Respond)
    assert stilebase.to("/v1/x402/requirements")[0].query["url"] == f"https://{DOMAIN}/premium"


def test_a_planted_pass_cookie_doesnt_hide_the_real_one(make: Any) -> None:
    token = pass_token(SECRET, CHROME, int(NOW) - 60)
    cookie = f"prefs=x,stilebase_pass={int(NOW)}.{'0' * 32}; stilebase_pass={token}"
    assert make().check(request("/listings", {**SCRIPT, "cookie": cookie})) == Pass()


def test_the_verification_file_at_a_free_path_is_served_on_the_first_request(make: Any, stilebase: FakeStilebase) -> None:
    """A free request waits for the settings like any other: it may be the verification file, on a cold instance."""
    path = "/.well-known/stilebase-verify.txt"
    stilebase.config = (200, live_config(verification={"path": path, "value": "stilebase-verify=tok"}), {})
    assert isinstance(make(free=[path]).check(request(path)), Respond)


def test_at_most_a_few_pass_cookies_are_checked_newest_first(make: Any) -> None:
    """A Cookie header stuffed with candidates can't cost hundreds of HMACs. Tried from the last: the real one, set just
    before the check page reloads, comes after one planted earlier; repeated, malformed or stale ones don't count."""
    token = pass_token(SECRET, CHROME, int(NOW) - 60)
    real = f"stilebase_pass={token}"
    decoys = [f"stilebase_pass={int(NOW) - i}.{'0' * 32}" for i in range(MAX_PASS_CANDIDATES)]
    site = make()
    assert isinstance(site.check(request("/listings", {**SCRIPT, "cookie": "; ".join([real, *decoys])})), Respond)
    assert site.check(request("/listings", {**SCRIPT, "cookie": "; ".join([real, *decoys[1:]])})) == Pass()
    assert site.check(request("/listings", {**SCRIPT, "cookie": "; ".join([*decoys, real])})) == Pass()
    for other in (decoys[0], "stilebase_pass=junk", f"stilebase_pass={int(NOW) - 99_999}.{'0' * 32}"):
        assert site.check(request("/listings", {**SCRIPT, "cookie": "; ".join([real, *[other] * 9])})) == Pass()


def test_the_check_page_is_the_same_as_the_javascript_sdks(make: Any) -> None:
    """Byte for byte the page @stilebase/sdk sends (its tests run the script: one reload, then a request for cookies)."""
    decision = make().check(request("/listings", {**SCRIPT, "accept": "text/html"}))
    assert isinstance(decision, Respond)
    body = decision.body.decode()
    assert 'var k="stilebase-check",n=Date.now(),t=0;' in body
    assert "&&!(n-t<30000)){try{sessionStorage.setItem(k,String(n))}catch(e){}location.reload()}" in body
    assert "Please allow cookies for this site, then reload the page." in body
