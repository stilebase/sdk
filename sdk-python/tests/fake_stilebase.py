"""A stand-in for Stilebase's gate API, as an httpx transport (sync and async): serves the settings, answers decide
and settle as told, records every call (checking its signature)."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

CONFORMANCE = Path(__file__).resolve().parents[2] / "conformance"
SETTINGS: dict[str, Any] = json.loads((CONFORMANCE / "settings.example.json").read_text(encoding="utf-8"))
VECTORS: dict[str, Any] = json.loads((CONFORMANCE / "vectors.json").read_text(encoding="utf-8"))

GATEWAY_ID = "8f14e45f-ceea-467a-9575-1e2a5e4c8a01"
SECRET = "sbsec_" + "ab" * 32
KEY = f"sbk_{GATEWAY_ID.replace('-', '')}_{'ab' * 32}"
API = "https://stilebase.test"
DOMAIN = "shop.example.com"
NOW = 1_792_756_800.0
GPTBOT = "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.2; +https://openai.com/gptbot)"
CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
PAYMENT = "eyJ4NDAyVmVyc2lvbiI6Mn0="
RECEIPT = "eyJzdWNjZXNzIjp0cnVlfQ=="
REQUIRED_402 = {
    "action": "respond",
    "status": 402,
    "headers": {"content-type": "application/json", "payment-required": "e30="},
    "body": "{}",
}
PASS = {"action": "pass"}
REMEMBER = {"action": "pass", "remember": {"seconds": 600}}
SETTLE_AFTER = {"action": "pass", "settle_after": {"max_bytes": 1000}}
PAID = {"action": "pass", "response_headers": {"payment-response": RECEIPT}}
Answer = tuple[int, Any]


def settings(**overrides: Any) -> dict[str, Any]:
    return {**copy.deepcopy(SETTINGS), **overrides}


@dataclass
class Call:
    method: str
    path: str
    headers: httpx.Headers
    body: Any
    timeout: dict[str, Any] = field(default_factory=dict)


def _signed(request: httpx.Request) -> bool:
    ts = request.headers.get("stilebase-timestamp", "")
    canonical = "\n".join(["v2", ts, request.method, request.url.path, hashlib.sha256(request.content).hexdigest()])
    mac = hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return bool(request.headers.get("stilebase-signature") == f"v2={mac}")


@dataclass
class FakeStilebase:
    """`decide` / `settle`: the answer (status, JSON body), or a function of the body sent. `down`: every call fails
    to connect. `delay`: seconds every decide takes (for budgets)."""

    settings_answer: Answer = (200, None)
    decide: Answer | Callable[[dict[str, Any]], Answer] = (200, PASS)
    settle: Answer | Callable[[dict[str, Any]], Answer] = (200, PAID)
    down: bool = False
    delay: float = 0.0
    raw_decide: bytes | None = None
    raw_settings: bytes | None = None
    calls: list[Call] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.settings_answer == (200, None):
            self.settings_answer = (200, settings())

    def to(self, name: str) -> list[Call]:
        return [c for c in self.calls if c.path.endswith(f"/{name}")]

    def _answer(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("Stilebase down", request=request)
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        timeout = dict(request.extensions.get("timeout", {}))
        self.calls.append(Call(request.method, request.url.path, request.headers, body, timeout))
        if not _signed(request):
            return httpx.Response(401, json={"error": "Invalid site signature"})
        name = request.url.path.rsplit("/", 1)[-1]
        if name == "settings":
            if self.raw_settings is not None:
                return httpx.Response(200, content=self.raw_settings)
            return httpx.Response(self.settings_answer[0], json=self.settings_answer[1])
        if name == "decide":
            if self.raw_decide is not None:
                return httpx.Response(200, content=self.raw_decide)
            status, answer = self.decide(body) if callable(self.decide) else self.decide
            return httpx.Response(status, json=answer)
        if name == "settle":
            status, answer = self.settle(body) if callable(self.settle) else self.settle
            return httpx.Response(status, json=answer)
        if name == "captures":
            return httpx.Response(204)
        return httpx.Response(404)

    @property
    def transport(self) -> httpx.MockTransport:
        fake = self

        def handler(request: httpx.Request) -> httpx.Response:
            if fake.delay and request.url.path.endswith("/decide"):
                time.sleep(fake.delay)
            return fake._answer(request)

        return httpx.MockTransport(handler)

    @property
    def async_transport(self) -> httpx.MockTransport:
        fake = self

        async def handler(request: httpx.Request) -> httpx.Response:
            if fake.delay and request.url.path.endswith("/decide"):
                import asyncio

                await asyncio.sleep(fake.delay)
            return fake._answer(request)

        return httpx.MockTransport(handler)
