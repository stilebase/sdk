"""conformance/vectors.json: what Stilebase's server, its Cloudflare Worker and both SDKs must agree on."""

from typing import Any

import pytest

from stilebase_sdk.gate import (
    VERSION,
    GateConfig,
    GateOptions,
    GateRequest,
    Pass,
    Respond,
    SyncGate,
    headers_of,
    normalize_ip,
    parse_site_key,
    pass_token,
    signed_headers,
)
from stilebase_sdk.gate._detect import in_networks, is_ai_agent, is_script, parse_networks, suspect_signals
from stilebase_sdk.gate._signing import has_pass
from tests.fake_stilebase import API, DOMAIN, EXAMPLE, GPTBOT, KEY, VECTORS, FakeStilebase

CONFIG = GateConfig.parse(EXAMPLE)


def test_the_example_settings_parse() -> None:
    assert CONFIG is not None and CONFIG.verification_path == "/.well-known/stilebase-verify.txt"
    assert GateConfig.parse({"gateway": {}}) is None and GateConfig.parse(None) is None


@pytest.mark.parametrize("vector", VECTORS["signing"], ids=lambda v: f"{v['method']} {v['path']}")
def test_signing(vector: dict[str, Any]) -> None:
    body = vector["body"].encode()
    assert signed_headers(vector["secret"], vector["method"], vector["path"], body, vector["timestamp"]) == vector["headers"]


def test_site_keys() -> None:
    for valid in VECTORS["site_keys"]["valid"]:
        assert parse_site_key(valid["key"]) == (valid["gateway_id"], valid["secret"])
    for invalid in VECTORS["site_keys"]["invalid"]:
        assert parse_site_key(invalid) is None, invalid


@pytest.mark.parametrize("vector", VECTORS["pass_tokens"], ids=lambda v: v["user_agent"][:20] or "empty")
def test_pass_tokens(vector: dict[str, Any]) -> None:
    assert pass_token(vector["secret"], vector["user_agent"], vector["timestamp"]) == vector["token"]


@pytest.mark.parametrize("case", VECTORS["pass_cookies"]["cases"], ids=lambda c: c["cookie"][:40])
def test_pass_cookies(case: dict[str, Any]) -> None:
    c = VECTORS["pass_cookies"]
    found = has_pass(case["cookie"], c["cookie_name"], c["secret"], c["user_agent"], c["now"], c["max_age_seconds"])
    assert found is case["has_pass"]


@pytest.mark.parametrize("case", VECTORS["agent_matching"]["cases"], ids=lambda c: c["user_agent"][:30] or "empty")
def test_agent_matching(case: dict[str, Any]) -> None:
    assert CONFIG is not None
    assert is_ai_agent(case["user_agent"], CONFIG) is case["agent"]
    assert is_ai_agent(case["user_agent"], CONFIG, test_mode=True) is case["test_mode"]
    assert is_script(case["user_agent"], CONFIG) is case["script"]


@pytest.mark.parametrize("case", VECTORS["suspect_signals"]["cases"], ids=lambda c: c["name"])
def test_suspect_signals(case: dict[str, Any]) -> None:
    assert CONFIG is not None
    headers = {**case["headers"], "user-agent": case["user_agent"]}
    request = GateRequest(case["method"], case["url"], headers, http_protocol=case["http_protocol"], asn=case["asn"])
    assert suspect_signals(request, CONFIG) == case["signals"]


def test_ip_ranges() -> None:
    networks = parse_networks(VECTORS["ip_ranges"]["prefixes"])
    assert len(networks) == len(VECTORS["ip_ranges"]["prefixes"])
    for case in VECTORS["ip_ranges"]["cases"]:
        assert in_networks(case["ip"], networks) is case["in"], case["ip"]


@pytest.mark.parametrize("case", VECTORS["client_ips"]["cases"], ids=lambda c: c["ip"] or "empty")
def test_client_ips(case: dict[str, Any]) -> None:
    assert normalize_ip(case["ip"]) == case["normalized"]


def test_the_version_is_the_packages() -> None:
    from importlib.metadata import version

    assert VERSION == version("stilebase-sdk")


def test_an_index_of_the_ranges_agrees_with_the_plain_list() -> None:
    from stilebase_sdk.gate._detect import NetworkIndex

    index = NetworkIndex(parse_networks(VECTORS["ip_ranges"]["prefixes"]))
    for case in VECTORS["ip_ranges"]["cases"]:
        assert in_networks(case["ip"], index) is case["in"], case["ip"]


def test_the_request_url_doesnt_let_the_host_header_move_the_path() -> None:
    from urllib.parse import urlsplit

    from stilebase_sdk.gate._detect import request_url

    assert request_url("https", "shop.example.com", "/premium/x", "a=1") == "https://shop.example.com/premium/x?a=1"
    assert urlsplit(request_url("https", "shop.example.com?", "/premium/x")).path == "/premium/x"
    assert urlsplit(request_url("https", "a.example#", "/premium/x")).path == "/premium/x"
    assert request_url("http", "[::1]:8080", "/") == "http://[::1]:8080/"


def test_repeated_cookie_headers_are_joined_as_cookies() -> None:
    assert headers_of([("Cookie", "a=1"), ("cookie", "stilebase_pass=t"), ("Accept", "a"), ("accept", "b")]) == {
        "cookie": "a=1; stilebase_pass=t",
        "accept": "a, b",
    }


def test_settings_of_another_version_arent_read() -> None:
    assert GateConfig.parse({**EXAMPLE, "config_version": 2}) is None


# --- The gate's decisions -------------------------------------------------------------------------------------------


def _gate(stilebase: Any, **options: Any) -> Any:
    return SyncGate(GateOptions(key=KEY, api_url=API, transport=stilebase.transport, on_error=lambda e: None, **options))


@pytest.mark.parametrize("vector", VECTORS["free_paths"]["cases"], ids=lambda v: f"{v['free']} {v['path']}")
def test_free_paths(vector: dict[str, Any]) -> None:
    gate = _gate(FakeStilebase(), free=vector["free"])
    try:
        url = vector.get("url") or f"https://{DOMAIN}{vector['path']}"
        decision = gate.check(GateRequest(vector.get("method", "GET"), url, {"user-agent": GPTBOT}, ip=None))
        if vector["free_request"]:
            assert decision == Pass()
        else:
            assert isinstance(decision, Respond) and decision.status == 402
    finally:
        gate.close()


@pytest.mark.parametrize("vector", VECTORS["payment_headers"]["cases"], ids=lambda v: str(v["headers"]))
def test_payment_headers(vector: dict[str, Any]) -> None:
    stilebase = FakeStilebase()
    gate = _gate(stilebase)
    try:
        gate.check(GateRequest("GET", f"https://{DOMAIN}/listings/1", {"user-agent": GPTBOT, **vector["headers"]}, ip=None))
        settled = stilebase.to("/v1/x402/settle")
        assert (settled[0].body["payment"] if settled else None) == vector["payment"]
    finally:
        gate.close()
