"""conformance/vectors.json: what Stilebase's server, its Cloudflare Worker and both SDKs must agree on."""

from typing import Any

import pytest

from stilebase_sdk.gate import VERSION, GateConfig, GateRequest, parse_site_key, pass_token, signed_headers
from stilebase_sdk.gate._detect import in_networks, is_ai_agent, is_script, normalize_ip, parse_networks, suspect_signals
from tests.fake_stilebase import EXAMPLE, VECTORS

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


@pytest.mark.parametrize("case", VECTORS["agent_matching"]["cases"], ids=lambda c: c["user_agent"][:30] or "empty")
def test_agent_matching(case: dict[str, Any]) -> None:
    assert CONFIG is not None
    assert is_ai_agent(case["user_agent"], CONFIG) is case["agent"]
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


def test_settings_of_another_version_arent_read() -> None:
    assert GateConfig.parse({**EXAMPLE, "config_version": 2}) is None
