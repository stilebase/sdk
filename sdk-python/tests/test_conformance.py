"""The shared cases (conformance/vectors.json): Stilebase, its Worker and both SDKs agree on every one."""

from __future__ import annotations

from typing import Any

import pytest

from stilebase_sdk.gate import GateRequest, Pass, Respond, Settings, normalize_ip, parse_site_key, signed_headers
from stilebase_sdk.gate._core import (
    Asking,
    Capture,
    Engine,
    GateOptions,
    body_allowed,
    capture_headers,
    decide_body,
    merge_headers,
    not_payable,
    read_answer,
    remember_key,
    respond,
    skipped,
    unknown_payment,
)
from tests.fake_stilebase import KEY, SETTINGS, VECTORS


@pytest.mark.parametrize("case", VECTORS["signing"])
def test_signing(case: dict[str, Any]) -> None:
    headers = signed_headers(case["secret"], case["method"], case["path"], case["body"].encode(), case["timestamp"])
    assert headers == case["headers"]


def test_site_keys() -> None:
    for case in VECTORS["site_keys"]["valid"]:
        assert parse_site_key(case["key"]) == (case["gateway_id"], case["secret"])
    for key in VECTORS["site_keys"]["invalid"]:
        assert parse_site_key(key) is None


@pytest.mark.parametrize("case", VECTORS["client_ips"]["cases"])
def test_client_ips(case: dict[str, Any]) -> None:
    assert normalize_ip(case["ip"]) == case["normalized"]


@pytest.mark.parametrize("case", VECTORS["remember_keys"]["cases"])
def test_remember_keys(case: dict[str, Any]) -> None:
    headers = (("User-Agent", case["user_agent"]),) if case["user_agent"] is not None else ()
    request = GateRequest("GET", case["path"], headers, ip=case["client_ip"])
    assert remember_key(request, Settings()) == case["key"]


SKIP = Settings(
    skip_prefixes=tuple(p.lower() for p in VECTORS["skip_paths"]["skip_paths"]["prefixes"]),
    skip_extensions=tuple(e.lower() for e in VECTORS["skip_paths"]["skip_paths"]["extensions"]),
)


@pytest.mark.parametrize("case", VECTORS["skip_paths"]["cases"], ids=lambda c: c["path"])
def test_skip_paths(case: dict[str, Any]) -> None:
    assert skipped(case["path"], SKIP) is case["skip"], case["why"]


@pytest.mark.parametrize("case", VECTORS["decide_bodies"]["cases"], ids=lambda c: c["name"])
def test_decide_bodies(case: dict[str, Any]) -> None:
    given = case["request"]
    edge = given.get("edge") or {}
    request = GateRequest(
        given["method"],
        given["path"],
        tuple((n, v) for n, v in given["headers"]),
        scheme=given["scheme"],
        ip=given["client_ip"],
        asn=edge.get("asn"),
        http_protocol=edge.get("http_protocol"),
    )
    settings = Settings.parse(SETTINGS)
    assert settings is not None
    assert decide_body(request, settings, given["assume_https"]) == case["body"]


@pytest.mark.parametrize("case", VECTORS["settings"]["cases"], ids=lambda c: c["name"])
def test_settings(case: dict[str, Any]) -> None:
    read = Settings.parse(case["body"])
    assert (read.read() if read is not None else None) == case["read"]


def outcome(decision: Pass | Respond) -> str:
    if isinstance(decision, Respond):
        return "unknown_payment" if decision.status == 503 and "retry-after" in decision.headers else "respond"
    return "settle_after" if decision.hold is not None else "pass"


@pytest.mark.parametrize("case", VECTORS["answers"]["cases"], ids=lambda c: c["name"])
def test_answers(case: dict[str, Any]) -> None:
    settings = Settings.parse(SETTINGS)
    assert settings is not None
    for payment, expected in ((False, case["no_payment"]), (True, case["with_payment"])):
        engine = Engine(GateOptions(key=KEY, on_error=lambda e: None), "test")
        headers = (("payment-signature", "x"),) if payment else ()
        request = GateRequest("GET", "/listings/2", headers, ip="203.0.113.7")
        asking = Asking(probe=False, key=remember_key(request, settings))
        decision = engine.after_asking(request, settings, {"b": 1}, read_answer(case["http_status"], case["body"]), asking)
        assert outcome(decision) == expected, (case["name"], payment)
        if "response_headers" in case:
            assert isinstance(decision, Pass) and dict(decision.response_headers) == case["response_headers"]
        if "respond" in case:
            assert isinstance(decision, Respond)
            assert (decision.status, decision.body.decode()) == (case["respond"]["status"], case["respond"]["body"])
        if "capture" in case:
            assert isinstance(decision, Pass)
            expected = case["capture"]
            assert decision.capture == (Capture(expected["token"], expected["max_bytes"]) if expected else None)
        if "headers" in case:
            assert isinstance(decision, Respond) and decision.headers == case["headers"]
        if "retry_after" in case:
            assert isinstance(decision, Respond) and decision.headers["retry-after"] == str(case["retry_after"])
        if case.get("remember_seconds") and not payment:
            assert engine.remembered.get(remember_key(request, settings), engine.options.clock())
        elif not payment:
            assert not engine.remembered.get(remember_key(request, settings), engine.options.clock())


def test_the_settings_example_is_read() -> None:
    settings = Settings.parse(SETTINGS)
    assert settings is not None
    assert (settings.timeout, settings.settle_timeout, settings.breaker_failures) == (1.0, 65.0, 5)
    assert settings.forward_cookies == ("stilebase_pass",) and ".png" in settings.skip_extensions
    assert settings.read()["strip_headers"] == ["cookie", "authorization", "proxy-authorization"]
    assert Settings.parse({**SETTINGS, "settings_version": 2}) is None
    assert Settings.parse({**SETTINGS, "enabled": "yes"}) is None


SENT = VECTORS["answers_sent"]


@pytest.mark.parametrize("case", SENT["merges"])
def test_merges(case: dict[str, Any]) -> None:
    merged = merge_headers([(n, v) for n, v in case["site"]], *case["overlays"])
    assert [list(pair) for pair in merged] == case["sent"]


@pytest.mark.parametrize("case", SENT["body_allowed"])
def test_body_allowed(case: dict[str, Any]) -> None:
    assert body_allowed(case["status"]) is case["allowed"]


@pytest.mark.parametrize("case", SENT["capture_headers"])
def test_capture_headers(case: dict[str, Any]) -> None:
    assert [list(pair) for pair in capture_headers([(n, v) for n, v in case["headers"]])] == case["kept"]


def test_gate_answers() -> None:
    for built, expected in (
        (unknown_payment(SENT["gate_answers"]["unknown_payment"]["retry_after"]), SENT["gate_answers"]["unknown_payment"]),
        (not_payable(), SENT["gate_answers"]["not_payable"]),
    ):
        assert (built.status, built.headers, built.body.decode()) == (expected["status"], expected["headers"], expected["body"])


def test_respond_replaces_cache_control_and_drops_bodies_that_cant_be_sent() -> None:
    answer = respond(402, {"Cache-Control": "max-age=60", "X-A": "1"}, "{}")
    assert (answer.headers, answer.body) == ({"cache-control": "no-store", "x-a": "1"}, b"{}")
    assert respond(304, {}, "ignored").body == b""
