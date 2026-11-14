"""A stand-in for Stilebase's x402 API, as an httpx transport (sync and async): answers a gate's calls, records them."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx

CONFORMANCE = Path(__file__).resolve().parents[2] / "conformance"
EXAMPLE: dict[str, Any] = json.loads((CONFORMANCE / "gate-config.example.json").read_text(encoding="utf-8"))
VECTORS: dict[str, Any] = json.loads((CONFORMANCE / "vectors.json").read_text(encoding="utf-8"))

GATEWAY_ID = "8f14e45f-ceea-467a-9575-1e2a5e4c8a01"
SECRET = "sbsec_" + "ab" * 32
KEY = f"sbk_{GATEWAY_ID.replace('-', '')}_{'ab' * 32}"
API = "https://stilebase.test"
DOMAIN = "shop.example.com"
NOW = 1_792_756_800.0
REQUIRED = {
    "x402Version": 2,
    "error": "PAYMENT-SIGNATURE header is required",
    "resource": {"url": f"https://{DOMAIN}/listings"},
    "accepts": [{"scheme": "exact", "network": "eip155:8453", "amount": "10000", "payTo": "0xPlatform"}],
}
SETTLED_HEADER = "eyJzdWNjZXNzIjp0cnVlfQ=="
CACHED = "public, max-age=300"
NETWORK = "eip155:8453"
GPTBOT = "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.2; +https://openai.com/gptbot)"
CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
BROWSER = {
    "user-agent": CHROME,
    "accept": "text/html,application/xhtml+xml",
    "accept-language": "en-US,en;q=0.9",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-dest": "document",
    "sec-ch-ua": '"Chromium";v="130"',
}


def live_config(**overrides: Any) -> dict[str, Any]:
    config = copy.deepcopy(EXAMPLE)
    config["gateway"] = {**config["gateway"], "domain": DOMAIN, "live": True, "reason": None}
    config["verification"] = None
    return {**config, **overrides}


@dataclass
class Call:
    method: str
    path: str
    query: dict[str, str]
    headers: httpx.Headers
    body: Any


Answer = tuple[int, Any, dict[str, str]] | Exception


class FakeStilebase:
    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.config: Answer = (200, live_config(), {})
        self.requirements: Callable[[dict[str, str]], Answer] = lambda q: (200, REQUIRED, {"cache-control": CACHED})
        self.settle: Answer = (200, {"success": True, "transaction": "0xabc", "network": NETWORK, "header": SETTLED_HEADER}, {})
        self.ranges: Answer = (200, {"prefixes": ["20.15.240.64/28", "2600:1f28:365:80b0::/60"]}, {})
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode(), keep_blank_values=True).items()}
        content = request.read()
        body = json.loads(content) if content else None
        self.calls.append(Call(request.method, request.url.path, query, request.headers, body))
        path = request.url.path
        answer: Answer
        if path == f"/v1/x402/sites/{GATEWAY_ID}/config":
            answer = self.config
        elif path == "/v1/x402/requirements":
            answer = self.requirements(query)
        elif path == "/v1/x402/agent-ranges":
            answer = self.ranges
        elif path == "/v1/x402/settle":
            answer = self.settle
        elif path == "/v1/x402/visits":
            answer = (204, None, {})
        else:
            answer = (404, None, {})
        if isinstance(answer, Exception):
            raise answer
        status, body, headers = answer
        return httpx.Response(status, json=body if body is not None and status != 204 else None, headers=headers)

    def to(self, path: str) -> list[Call]:
        return [c for c in self.calls if c.path == path]
