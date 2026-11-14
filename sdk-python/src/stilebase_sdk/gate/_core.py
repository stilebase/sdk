"""The gate: what Stilebase's Cloudflare Worker does, for Python sites. People pass straight through; AI agents get 402
Payment Required with the price (x402), and a payment is settled through Stilebase. When Stilebase can't be reached,
everyone passes: the gate never breaks the site. SPEC.md is the spec.

The decision (`decide`) is written once, as a generator that asks its driver for what it needs from Stilebase
(`GetConfig`, `GetRequirements`, …), so the async driver (ASGI) and the sync one (WSGI, Django) share every rule.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from .._version import PACKAGE, VERSION
from ._detect import (
    GateConfig,
    GateRequest,
    Network,
    fit_encoded,
    form_quote,
    in_networks,
    is_ai_agent,
    is_script,
    normalize_ip,
    parse_networks,
    path_of,
    suspect_signals,
)
from ._signing import b64, has_pass, parse_site_key, pass_token, signed_headers

DEFAULT_API_URL = "https://stilebase.com"
CONFIG_TIMEOUT = 3.0
REQUIREMENTS_TIMEOUT = 5.0
SETTLE_TIMEOUT = 65.0  # longer than Stilebase takes at most: giving up first would answer 503 for a payment that then settles
REPORT_TIMEOUT = 10.0
RETRY_WITHOUT_CONFIG = 30.0
MIN_REFRESH = 30.0
RANGES_MAX_AGE = 3600.0
MAX_PATH = 2048  # Stilebase's limits for a URL or path, and a user agent
MAX_USER_AGENT = 512
MAX_CACHED = 1000
MAX_PENDING_REPORTS = 100
WEB_BOT_AUTH_HEADERS = ("signature", "signature-input", "signature-agent")
RECEIPT_CHARACTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=_-")

logger = logging.getLogger("stilebase_sdk.gate")


@dataclass
class GateOptions:
    """How the gate runs. Everything has a default; most sites set nothing (the key comes from STILEBASE_KEY)."""

    key: str | None = field(default=None, repr=False)  # the site key from your gateway page; default: STILEBASE_KEY
    api_url: str | None = None  # Stilebase's API; default: STILEBASE_API_URL, else https://stilebase.com
    catch_http_clients: bool = True  # scripts with their default user agent (curl, python-requests…) pay too
    catch_hidden_agents: bool = True  # agents behind a browser's user agent (operators' addresses, Web Bot Auth) too
    check_browsers: bool = True  # browser-looking requests that don't act like one: counted, or checked under strict detection
    on_error: Callable[[Exception], None] | None = None  # default: a warning on this logger, at most once a minute each
    now: Callable[[], float] = field(default=time.time)  # for tests: the time in seconds
    transport: Any = None  # for tests: an httpx transport


@dataclass(frozen=True)
class Pass:
    """Let the request through; set PAYMENT-RESPONSE on the response when given (a settled payment)."""

    payment_response: str | None = None


@dataclass(frozen=True)
class Respond:
    """Answer the request with this instead of the site."""

    status: int
    headers: dict[str, str]
    body: bytes


Decision = Pass | Respond
PASS = Pass()


# What the decision asks its driver for.
@dataclass(frozen=True)
class GetConfig:
    pass


@dataclass(frozen=True)
class GetRanges:
    pass


@dataclass(frozen=True)
class GetRequirements:
    query: str


@dataclass(frozen=True)
class Report:
    """In the background: a visit for the owner's statistics."""

    payload: dict[str, Any]


@dataclass(frozen=True)
class Settle:
    payload: dict[str, Any]


Effect = GetConfig | GetRanges | GetRequirements | Report | Settle
Flow = Generator[Effect, Any, Decision]


def decide(request: GateRequest, secret: str, options: GateOptions) -> Flow:
    """What to do with this request: the Worker's flow (SPEC.md, "Each request")."""
    config: GateConfig | None = yield GetConfig()
    if config is None:
        return PASS
    path = path_of(request.url)
    if config.verification_path and config.verification_value and path.split("?")[0] == config.verification_path:
        return _respond(200, {"content-type": "text/plain; charset=utf-8"}, f"{config.verification_value}\n")
    if not config.live:
        return PASS

    payment = request.header("payment-signature") or request.header("x-payment") or None  # an empty header is none
    user_agent = request.header("user-agent")
    ip = normalize_ip(request.ip) if isinstance(request.ip, str) else None
    signals: list[str] = []  # a browser-looking request that failed the header checks: which ones
    if payment is None and not (yield from _is_agent(request, user_agent, ip, config, options)):
        signals = suspect_signals(request, config) if options.check_browsers else []
        if not signals:
            return PASS
        if has_pass(request.header("cookie"), config.pass_cookie, secret, user_agent, options.now(), config.pass_max_age):
            return PASS

    # Cut to Stilebase's limits: an over-long URL or user agent must never make its call fail (that would serve the page).
    host = config.domain
    site = {"method": request.method.upper(), "path": path[:MAX_PATH]}
    given_user_agent = request.headers.get("user-agent")
    agent_ip = ip if ip and len(ip) <= 45 else None
    agent_user_agent = given_user_agent[:MAX_USER_AGENT] if given_user_agent is not None else None
    agent = {
        "ip": agent_ip,
        "user_agent": agent_user_agent,
        "headers": {h: request.header(h) for h in WEB_BOT_AUTH_HEADERS if request.header(h)},
    }
    # The page by the gateway's domain (the Host header is the visitor's to write), encoded well under proxies' 8 KB.
    page = f"https://{host}{path}"[:MAX_PATH]
    query = {
        "host": host,
        "url": fit_encoded(page, 4096),
        "ua": fit_encoded(agent_user_agent or "", 1536),
        "ip": agent_ip or "",
    }
    if signals:
        query["suspect"] = "true"

    try:
        status, required = yield GetRequirements(urlencode(query, quote_via=form_quote))
    except Exception:
        return PASS  # Stilebase unreachable: never break the site (the driver reported it)
    if status == 204 and payment is None:
        # A free pass: this agent reads for free. Or a suspect while strict detection is off: counted, served.
        extra: dict[str, Any] = {"signals": signals} if signals else {"free_pass": True}
        yield Report({"host": host, **extra, "request": site, "agent": agent})
        return PASS
    if status != 200 or not isinstance(required, dict):
        return PASS  # no live gateway for this domain (yet)

    if payment is None:
        visit: dict[str, Any] = {"host": host, "signals": signals} if signals else {"host": host}
        yield Report({**visit, "request": site, "agent": agent})
        if signals and "text/html" in request.header("accept"):
            # The whole user agent, as has_pass checks it (agent.user_agent is cut for Stilebase's limits).
            return _browser_check(required, pass_token(secret, user_agent, int(options.now())), config)
        return _payment_required(required)

    try:
        status, settlement = yield Settle({"host": host, "payment": payment, "request": site, "agent": agent})
        if status != 200 or not isinstance(settlement, dict) or not isinstance(settlement.get("success"), bool):
            raise GateError(f"Stilebase couldn't confirm the settlement ({status})")
    except Exception:
        # The outcome is unknown (the payment may have settled): never serve the page, never say nothing was charged.
        headers = {"content-type": "application/json", "retry-after": "5"}
        return _respond(503, headers, _json({"error": "Payment settlement unavailable"}))
    receipt = settlement.get("header")
    receipt = receipt if isinstance(receipt, str) and 0 < len(receipt) <= 8192 and set(receipt) <= RECEIPT_CHARACTERS else None
    if not settlement["success"]:
        reason = settlement.get("errorReason")
        return _payment_required({**required, "error": reason if reason is not None else "payment_rejected"}, receipt)
    return Pass(receipt)


def _is_agent(
    request: GateRequest, user_agent: str, ip: str | None, config: GateConfig, options: GateOptions
) -> Generator[Effect, Any, bool]:
    """An AI agent by its user agent; a script by its default user agent (or none); an agent behind a browser's user
    agent by Web Bot Auth's header or the address it comes from."""
    if is_ai_agent(user_agent, config):
        return True
    if options.catch_http_clients and is_script(user_agent, config):
        return True
    if not options.catch_hidden_agents:
        return False
    if request.header("signature-agent"):
        return True
    if not ip:
        return False
    networks: list[Network] = yield GetRanges()
    return in_networks(ip, networks)


def _payment_required(required: dict[str, Any], payment_response: str | None = None) -> Respond:
    """The 402 for an agent: the x402 PaymentRequired as the body and, base64-encoded, in PAYMENT-REQUIRED."""
    text = _json(required)
    headers = {"content-type": "application/json", "PAYMENT-REQUIRED": b64(text)}
    if payment_response:
        headers["PAYMENT-RESPONSE"] = payment_response
    return _respond(402, headers, text)


def _browser_check(required: dict[str, Any], token: str, config: GateConfig) -> Respond:
    """The 402 for a suspect that asks for HTML: a page whose script lets a real browser through, the payment request
    in the header for everyone else."""
    cookie = f"{config.pass_cookie}={token}; Max-Age={config.pass_max_age}; Path=/; SameSite=Lax; Secure"
    page = (
        '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        "<title>One moment…</title></head>\n"
        '<body style="font-family:system-ui,sans-serif;margin:3rem auto;max-width:32rem;padding:0 1rem;color:#334">\n'
        '<p id="m">Checking your browser…</p>\n'
        "<noscript><p>This page needs JavaScript. Automated clients: the price is in the PAYMENT-REQUIRED header (x402)."
        "</p></noscript>\n"
        f"<script>document.cookie={json.dumps(cookie)};if(document.cookie.indexOf({json.dumps(token)})>=0)location.reload();"
        'else document.getElementById("m").textContent="Please allow cookies for this site to continue.";</script>\n'
        "</body></html>"
    )
    return _respond(402, {"content-type": "text/html; charset=utf-8", "PAYMENT-REQUIRED": b64(_json(required))}, page)


def _json(value: Any) -> str:
    """JSON as JavaScript's JSON.stringify writes it (compact, raw UTF-8): the same bytes from both SDKs."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _respond(status: int, headers: dict[str, str], body: str) -> Respond:
    """An answer the gate makes itself, never kept by a cache in front of the site."""
    return Respond(status, {**headers, "cache-control": "no-store"}, body.encode())


def address_from(headers: dict[str, str], name: str | None, fallback: str | None) -> str | None:
    """The visitor's address from a header your proxy sets: from a list (X-Forwarded-For) its last entry, the one your
    proxy added; the ones before it are the visitor's to write. Else the server's own address for the connection."""
    if name:
        return headers.get(name.lower(), "").split(",")[-1].strip() or None
    return fallback or None


class GateError(Exception):
    """Something the gate couldn't do; it let the request through (or answered 503 for a payment)."""


def json_body(content: bytes) -> Any:
    try:
        return json.loads(content) if content else None
    except ValueError:
        return None


class Engine:
    """What both drivers share: the site key, the options, the settings, the address lists and the price cache."""

    def __init__(self, options: GateOptions, adapter: str) -> None:
        self.options = options
        self.client = f"{PACKAGE}/{VERSION} gate/{adapter}"  # e.g. "stilebase-sdk/0.1.0 gate/asgi"
        self.api = (options.api_url or os.environ.get("STILEBASE_API_URL") or DEFAULT_API_URL).rstrip("/")
        key = options.key if options.key is not None else os.environ.get("STILEBASE_KEY", "")
        self.site = parse_site_key(key)
        self.config: GateConfig | None = None
        self.config_at = 0.0  # when the settings were last loaded (or tried); 0: never
        self.ranges: list[Network] | None = None
        self.ranges_at = 0.0
        self._cache: OrderedDict[str, tuple[float, int, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._warned: dict[str, float] = {}
        if self.site is None:
            self.report(GateError(f"STILEBASE_KEY {'is not a site key (sbk_…)' if key else 'is not set'}: every request passes"))

    def flow(self, request: GateRequest) -> Flow:
        assert self.site is not None
        return decide(request, self.site[1], self.options)

    def config_path(self) -> str:
        assert self.site is not None
        return f"/v1/x402/sites/{self.site[0]}/config"

    def config_headers(self) -> dict[str, str]:
        assert self.site is not None
        return {
            **signed_headers(self.site[1], "GET", self.config_path(), b"", self.options.now()),
            "Stilebase-Client": self.client,
        }

    def config_due(self) -> bool:
        every = max(self.config.refresh_seconds, MIN_REFRESH) if self.config else RETRY_WITHOUT_CONFIG
        return self.options.now() - self.config_at >= every

    def take_config(self, status: int, body: Any) -> None:
        """What the settings call answered. Any failure keeps the last good settings (a 401 after a rotated key: agents
        are still asked to pay), except a 404: the gateway was removed, so everyone passes."""
        try:
            if status == 404:
                self.config = None
                raise GateError("Stilebase has no gateway for this site key (404): every request passes")
            if status != 200:
                raise GateError(f"Stilebase answered the settings {status}")
            config = GateConfig.parse(body)
            if config is None:
                raise GateError("Stilebase sent settings this version can't read")
            self.config = config
        finally:
            self.config_at = self.options.now()

    def ranges_due(self) -> bool:
        return self.options.now() - self.ranges_at > RANGES_MAX_AGE

    def take_ranges(self, status: int, body: Any) -> None:
        """The AI operators' address ranges; on a failure keep what we had (or none) and try again in an hour."""
        try:
            if status != 200:
                raise GateError(f"Stilebase answered the agent ranges {status}")
            prefixes = body.get("prefixes") if isinstance(body, dict) else None
            if not isinstance(prefixes, list):
                raise GateError("Stilebase's agent ranges have no prefixes list")  # keep the last good ones
            self.ranges = parse_networks(prefixes)
        finally:
            if self.ranges is None:
                self.ranges = []
            self.ranges_at = self.options.now()

    def cached_requirements(self, key: str) -> tuple[int, Any] | None:
        with self._lock:
            cached = self._cache.get(key)
            if cached is None or cached[0] <= self.options.now():
                return None
            return cached[1], cached[2]

    def store_requirements(self, key: str, status: int, body: Any, cache_control: str) -> None:
        """Cached as long as Stilebase says, at most 5 minutes, as the Worker's edge cache does."""
        max_age = 0
        for part in cache_control.split(","):
            name, _, value = part.strip().partition("=")
            if name == "max-age" and value.isascii() and value.isdigit():
                max_age = min(int(value[:6]), 300)
        if max_age <= 0 or not (status == 204 or (status == 200 and isinstance(body, dict))):
            return
        with self._lock:
            self._cache.pop(key, None)
            while len(self._cache) >= MAX_CACHED:
                self._cache.popitem(last=False)
            self._cache[key] = (self.options.now() + max_age, status, body)

    def signed_post(self, path: str, payload: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
        assert self.site is not None
        body = json.dumps(payload).encode()
        return body, {"content-type": "application/json", **signed_headers(self.site[1], "POST", path, body, self.options.now())}

    def report(self, error: BaseException) -> None:
        try:
            if self.options.on_error is not None:
                self.options.on_error(error if isinstance(error, Exception) else GateError(str(error)))
                return
            message = str(error) or type(error).__name__
            now = time.monotonic()
            if now - self._warned.get(message, -1e9) < 60:
                return
            if len(self._warned) >= 100:
                self._warned.clear()
            self._warned[message] = now
            logger.warning("stilebase-sdk: %s", message)
        except Exception:
            pass  # a failing error handler mustn't break the site either
