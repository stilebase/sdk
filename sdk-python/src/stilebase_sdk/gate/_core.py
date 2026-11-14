"""The gate: what a site runs in front of its pages so AI agents pay per request with x402 and everyone else passes
straight through. The same flow as Stilebase's Cloudflare Worker and @stilebase/sdk; SPEC.md, at the root of this
repository, is the spec.

The decision (`decide`) is written once, as a generator that asks its driver for what it needs from Stilebase
(`GetConfig`, `GetRequirements`, …), so the async driver (ASGI) and the sync one (WSGI, Django) share every rule.

It never breaks the site: without a key, without settings, or when Stilebase can't be reached, every request passes.
Only a payment Stilebase doesn't confirm is refused (503), since serving it would give the page away for a payment that
may not have been taken.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Generator, Iterable
from dataclasses import dataclass, field, replace
from typing import Any, Literal
from urllib.parse import urlencode

from .._version import PACKAGE, VERSION
from ._detect import (
    GateConfig,
    GateRequest,
    NetworkIndex,
    fit_encoded,
    form_quote,
    has_surrogates,
    in_networks,
    is_ai_agent,
    is_literal_path,
    is_script,
    literal_path,
    normalize_ip,
    parse_networks,
    site_path,
    site_url,
    suspect_signals,
    without_surrogates,
)
from ._signing import b64, has_pass, parse_site_key, pass_token, signed_headers

DEFAULT_API_URL = "https://stilebase.com"
CONFIG_TIMEOUT = 3.0
REQUIREMENTS_TIMEOUT = 5.0
# Longer than Stilebase takes at most to settle: giving up first would answer 503 for a payment that then settles.
SETTLE_TIMEOUT = 65.0
REPORT_TIMEOUT = 10.0
RETRY_WITHOUT_CONFIG = 30.0  # no settings yet: try again at most this often, never on every request
MIN_REFRESH = 30.0
RANGES_MAX_AGE = 3600.0
REQUIREMENTS_CACHE_SIZE = 1000
MAX_REQUIREMENTS_AGE = 300
_MAX_AGE = re.compile(r"max-age=([0-9]+)")
WARN_EVERY = 600.0
WEB_BOT_AUTH_HEADERS = ("signature", "signature-input", "signature-agent")
MAX_PATH = 2048  # Stilebase's limits for a URL or path, and a user agent
MAX_USER_AGENT = 512
# The price lookup's query, encoded, stays well under the 8 KB request line proxies allow.
MAX_ENCODED_URL = 4096
MAX_ENCODED_USER_AGENT = 1536
# Stilebase refusing the price lookup (malformed, too long, too many): the gate asks with the last price it had. A
# visitor can provoke each of these (a padded URL, many unique URLs), so letting the request through would serve it free.
REFUSED = frozenset({400, 413, 414, 422, 429, 431})
logger = logging.getLogger("stilebase_sdk.gate")


@dataclass(frozen=True)
class Pass:
    """Serve the request. After a settled payment, add PAYMENT-RESPONSE with this value to your response."""

    payment_response: str | None = None


@dataclass(frozen=True)
class Respond:
    """Answer with this instead: the 402, the browser check, the verification file, or 503."""

    status: int
    headers: dict[str, str]
    body: bytes


Decision = Pass | Respond
PASS = Pass()


@dataclass
class GateOptions:
    """How the gate runs. Everything has a default; most sites set nothing (the key comes from STILEBASE_KEY)."""

    # The site key from your gateway page; default: STILEBASE_KEY. Never in a repr (error pages, log lines).
    key: str | None = field(default=None, repr=False)
    api_url: str | None = None  # Stilebase's API; default: STILEBASE_API_URL, else https://stilebase.com
    mode: Literal["live", "test"] = "live"  # test: only Stilebase's test agent is asked to pay
    free: Iterable[str] | None = None  # exact paths that stay free for everyone (["/healthz"]); all others are gated
    catch_http_clients: bool = True  # scripts with their default user agent (curl, python-requests…) pay too
    catch_hidden_agents: bool = True  # agents behind a browser's user agent (operators' addresses, Web Bot Auth) too
    check_browsers: bool = True  # browser-looking requests that don't act like one: counted, or asked under strict detection
    # Visitors reach the site over HTTPS whatever scheme the app sees (behind a proxy that ends TLS it sees http). The
    # browser checks rely on it (browsers send their tell-tale headers only over HTTPS). False for a site really on http.
    assume_https: bool = True
    on_error: Callable[[Exception], None] | None = None  # default: a logging warning, once in a while
    now: Callable[[], float] = field(default=time.time)  # for tests: the time in seconds
    transport: Any = None  # for tests: an httpx transport


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
    """Fire and forget: a visit for the owner's statistics."""

    path: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class Settle:
    payload: dict[str, Any]


Effect = GetConfig | GetRanges | GetRequirements | Report | Settle
Flow = Generator[Effect, Any, Decision]


class GateError(Exception):
    """Something the gate couldn't do; it let the request through (or answered 503 for a payment)."""


class Engine:
    """What both drivers share: the site key, the options, the cached settings, lists and requirements."""

    def __init__(self, options: GateOptions, adapter: str) -> None:
        self.options = options
        self.client = f"{PACKAGE}/{VERSION} gate/{adapter}"  # e.g. "stilebase-sdk/0.1.0 gate/asgi"
        self.api = (options.api_url or os.environ.get("STILEBASE_API_URL") or DEFAULT_API_URL).rstrip("/")
        key = options.key if options.key is not None else os.environ.get("STILEBASE_KEY", "")
        self.site = parse_site_key(key)
        self.config: GateConfig | None = None
        self.config_checked_at = 0.0
        self.config_tried = False
        self.ranges: NetworkIndex | None = None
        self.ranges_at = 0.0
        self._requirements: OrderedDict[str, tuple[float, int, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._refreshing: set[str] = set()
        self.memory: dict[str, Any] = {}  # "required": the last price Stilebase gave, for refused lookups
        self._warned: dict[str, float] = {}
        if self.site is None:
            self.report(GateError(f"STILEBASE_KEY {'is not a site key (sbk_…)' if key else 'is not set'}: every request passes"))
        # Worked out once (an iterator given as `free` would be used up by a second look).
        usable, unusable = _free_entries(options.free)
        self.free_paths = frozenset(usable)
        if unusable:
            self.report(
                GateError(
                    f"free: {', '.join(repr(p) for p in unusable)} can never match (use literal paths from the site's "
                    'root, like "/healthz": letters, digits, - . _ ~ and /, no query, %, "..", "//")'
                )
            )

    @property
    def secret(self) -> str:
        assert self.site is not None
        return self.site[1]

    def flow(self, request: GateRequest) -> Flow:
        return decide(request, self.secret, self.options, self.report, self.memory, self.free_paths)

    # --- Settings ------------------------------------------------------------------------------------------------

    def config_path(self) -> str:
        assert self.site is not None
        return f"/v1/x402/sites/{self.site[0]}/config"

    def config_headers(self) -> dict[str, str]:
        signed = signed_headers(self.secret, "GET", self.config_path(), b"", self.options.now())
        return {**signed, "Stilebase-Client": self.client}

    def config_due(self) -> bool:
        every = max(self.config.refresh_seconds, MIN_REFRESH) if self.config else RETRY_WITHOUT_CONFIG
        return self.options.now() - self.config_checked_at >= every

    def take_config(self, status: int, body: Any) -> None:
        """What the settings call answered. Raises GateError when it brought no settings (the last good ones stay,
        except after a refused key)."""
        if status == 404:
            self.config = None  # the gateway was removed: stop asking agents to pay
            raise GateError("Stilebase has no gateway for this site key (404): every request passes")
        if status == 401:
            # A rotated key, or this server's clock more than 5 minutes off. Keep the last settings: agents still get the
            # 402 (the price lookup isn't signed) and only payments fail, as with the Worker. Dropping them would let
            # every agent read free until someone notices.
            raise GateError(
                "Stilebase refused the site key (401): was it rotated, or is this server's clock off? Update STILEBASE_KEY. "
                "Agents are still asked to pay, but their payments can't be settled"
            )
        if status != 200:
            raise GateError(f"Stilebase answered the settings {status}")
        config = GateConfig.parse(body)
        if config is None:
            raise GateError("Stilebase sent settings this version can't read")
        self.config = config

    def config_done(self) -> None:
        self.config_tried = True
        self.config_checked_at = self.options.now()

    # --- The AI operators' address lists ---------------------------------------------------------------------------

    def ranges_due(self) -> bool:
        return self.options.now() - self.ranges_at > RANGES_MAX_AGE

    def take_ranges(self, status: int, body: Any) -> None:
        if status != 200 or not isinstance(body, dict) or not isinstance(body.get("prefixes"), list):
            raise GateError(f"Stilebase answered the agent ranges {status}")
        self.ranges = NetworkIndex(parse_networks(body["prefixes"]))

    def ranges_done(self) -> None:
        self.ranges_at = self.options.now()
        if self.ranges is None:
            self.ranges = NetworkIndex([])  # keep what we had (or none) and try again in an hour

    # --- Requirements: cached as long as Stilebase says (5 minutes) ------------------------------------------------

    def cached_requirements(self, key: str) -> tuple[int, Any] | None:
        with self._lock:
            cached = self._requirements.get(key)
            if cached is None:
                return None
            if cached[0] <= self.options.now():
                del self._requirements[key]
                return None
            return cached[1], cached[2]

    def store_requirements(self, key: str, status: int, body: Any, cache_control: str) -> None:
        # As @stilebase/sdk reads it: the first max-age=<ASCII digits>, any length, at most MAX_REQUIREMENTS_AGE (a
        # value too long for int() is over it anyway).
        found = _MAX_AGE.search(cache_control)
        digits = found.group(1) if found else "0"
        max_age = MAX_REQUIREMENTS_AGE if len(digits) > 9 else min(int(digits), MAX_REQUIREMENTS_AGE)
        if status not in (200, 204) or max_age <= 0 or (status == 200 and not isinstance(body, dict)):
            return  # a 200 without a JSON object (cut short, an error page) is asked again next time, as in JS
        with self._lock:
            while len(self._requirements) >= REQUIREMENTS_CACHE_SIZE:
                self._requirements.popitem(last=False)
            self._requirements[key] = (self.options.now() + max_age, status, body)

    def begin_refresh(self, name: str) -> bool:
        """True for the one caller that should start a background refresh of `name` ("config", "ranges")."""
        with self._lock:
            if name in self._refreshing:
                return False
            self._refreshing.add(name)
            return True

    def end_refresh(self, name: str) -> None:
        with self._lock:
            self._refreshing.discard(name)

    # --- Calls -----------------------------------------------------------------------------------------------------

    def signed_post(self, path: str, payload: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
        body = json.dumps(payload).encode()
        headers = {"content-type": "application/json", **signed_headers(self.secret, "POST", path, body, self.options.now())}
        return body, headers

    def report(self, error: BaseException) -> None:
        err = error if isinstance(error, Exception) else GateError(str(error))
        if self.options.on_error is not None:
            try:
                self.options.on_error(err)
            except Exception:  # noqa: S110 - a failing error handler mustn't break the site either
                pass
            return
        message = str(err) or type(err).__name__
        now = self.options.now()
        if now - self._warned.get(message, -WARN_EVERY) < WARN_EVERY:
            return
        self._warned[message] = now
        logger.warning("%s", message)


def json_body(content: bytes) -> Any:
    try:
        return json.loads(content) if content else None
    except ValueError:
        return None


def decide(
    request: GateRequest,
    secret: str,
    options: GateOptions,
    report: Callable[[BaseException], None],
    memory: dict[str, Any] | None = None,
    free: frozenset[str] | None = None,
) -> Flow:
    """What to do with this request (see the module docstring). `memory` keeps the last price between requests; `free`
    is the owner's usable free paths (default: worked out from options.free)."""
    memory = memory if memory is not None else {}
    free = free if free is not None else frozenset(_free_entries(options.free)[0])
    config: GateConfig | None = yield GetConfig()
    if config is None:
        return PASS
    # Only a string is an address: a custom client_ip returning anything else must never raise here, which would let
    # every request through.
    ip = normalize_ip(request.ip) if isinstance(request.ip, str) else None
    # No address is longer than 45 characters: anything longer (a header someone stuffed) counts as none, as Stilebase
    # treats it, and never bloats the price lookup past proxies' limits.
    request = replace(request, ip=ip if ip and len(ip) <= 45 else None, url=without_surrogates(request.url))
    # Lone surrogates (a header decoded with surrogateescape) as U+FFFD, once, here: the lookup, the pass token and
    # the check page all encode these as UTF-8, and an error there would let the request through. Only then is the
    # caller's mapping copied (names lowercased, as headers_of gives them); otherwise it's kept as given (a framework's
    # case-insensitive headers), whatever its values.
    if any(isinstance(value, str) and has_surrogates(value) for value in request.headers.values()):
        cleaned = {
            str(name).lower(): without_surrogates(value) if isinstance(value, str) else value
            for name, value in request.headers.items()
        }
        request = replace(request, headers=cleaned)
    path, page_query = site_path(request.url)
    page = site_url(config.domain, path, page_query)
    if config.verification_path and config.verification_value and path == config.verification_path:
        headers = {"content-type": "text/plain; charset=utf-8", "cache-control": "no-store"}
        return Respond(200, headers, f"{config.verification_value}\n".encode())
    if not config.live or _is_free(request.method, request.url, free):
        return PASS

    payment = request.header("payment-signature") or request.header("x-payment") or None
    user_agent = request.header("user-agent")
    test_mode = options.mode == "test"
    signals: list[str] = []  # a browser-looking request that failed the header checks: which ones
    if payment is None and not (yield from _is_agent(request, user_agent, config, options)):
        check = options.check_browsers and not test_mode
        signals = suspect_signals(request, config, options.assume_https) if check else []
        if not signals:
            return PASS
        if has_pass(request.header("cookie"), config.pass_cookie, secret, user_agent, options.now(), config.pass_max_age):
            return PASS

    # Cut to Stilebase's limits: an over-long URL or user agent must never make its call fail (that would serve the page).
    site_request = {"method": request.method.upper(), "path": (path + (f"?{page_query}" if page_query else ""))[:MAX_PATH]}
    given_user_agent = request.headers.get("user-agent")
    agent = {
        "ip": request.ip,
        "user_agent": given_user_agent[:MAX_USER_AGENT] if given_user_agent is not None else None,
        "headers": {h: request.headers[h] for h in WEB_BOT_AUTH_HEADERS if request.headers.get(h)},
    }
    host = config.domain
    query = {
        "host": host,
        "url": fit_encoded(page[:MAX_PATH], MAX_ENCODED_URL),
        "ua": fit_encoded(user_agent[:MAX_USER_AGENT], MAX_ENCODED_USER_AGENT),
        "ip": request.ip or "",
    }
    if signals:
        query["suspect"] = "true"
    try:
        status, required = yield GetRequirements(urlencode(query, quote_via=form_quote))
    except Exception as error:
        report(error)
        return PASS  # Stilebase unreachable: never break the site
    if status == 204 and payment is None:
        # A free pass: this agent reads for free. Or a suspect while strict detection is off: counted, served.
        extra: dict[str, Any] = {"signals": signals} if signals else {"free_pass": True}
        yield Report("/v1/x402/visits", {"host": host, **extra, "request": site_request, "agent": agent})
        return PASS
    if status == 200 and isinstance(required, dict):
        memory["required"] = required
    elif status in REFUSED and isinstance(memory.get("required"), dict):
        # Stilebase refused the lookup (a request built to be refused?): ask with the last price rather than give the page.
        last: dict[str, Any] = memory["required"]
        required = {**last, "resource": {**(last.get("resource") or {}), "url": page}}
    else:
        return PASS  # no live gateway for this domain (yet), or no price known

    if payment is None:
        flagged: dict[str, Any] = {"signals": signals} if signals else {}
        visit = {"host": host, **flagged, "request": site_request, "agent": agent}
        yield Report("/v1/x402/visits", visit)  # statistics only
        if signals and "text/html" in request.header("accept"):
            return browser_check(required, pass_token(secret, user_agent, int(options.now())), config)
        return payment_required(required)

    try:
        status, settlement = yield Settle({"host": host, "payment": payment, "request": site_request, "agent": agent})
        if status != 200 or not isinstance(settlement, dict) or not isinstance(settlement.get("success"), bool):
            raise GateError(f"Stilebase answered the settlement {status} without a boolean success")
    except Exception as error:
        report(error)
        # The outcome is unknown: the payment may have settled (an answer lost on the way). Never serve the page, and never
        # tell the agent it wasn't charged. Sending the same payment again can't charge twice.
        headers = {"content-type": "application/json", "retry-after": "5", "cache-control": "no-store"}
        return Respond(503, headers, b'{"error": "Payment settlement unavailable"}')
    header = _receipt(settlement.get("header"))
    if not settlement.get("success"):
        reason = settlement.get("errorReason")
        refused = {**required, "error": reason if reason is not None else "payment_rejected"}
        return payment_required(refused, header)
    return Pass(header) if header else PASS


_RECEIPT = re.compile(r"[A-Za-z0-9+/=_-]{1,8192}")


def _receipt(value: Any) -> str | None:
    """Stilebase's PAYMENT-RESPONSE value, only when it can be a header (a base64 string): anything else is dropped, so a
    server never rejects the gate's answer over it."""
    return value if isinstance(value, str) and _RECEIPT.fullmatch(value) else None


def _free_entries(free: Iterable[str] | str | None) -> tuple[set[str], list[Any]]:
    """The owner's free paths that can match (literal), and the ones that never can."""
    listed: list[Any] = [] if free is None else [free] if isinstance(free, str) else list(free)  # one path, not chars
    usable = [p for p in listed if isinstance(p, str) and is_literal_path(p)]
    return set(usable), [p for p in listed if p not in usable]


def _is_free(method: str, url: str, free: frozenset[str]) -> bool:
    """A free path: a GET or HEAD (a POST can run code a free page shares with gated ones) whose URL is literal (no
    encoding, no dot segments, a plain host) and whose path is one of the owner's free paths character for character.
    Anything unusual is gated: no two parsers can read it differently."""
    if not free or method.upper() not in ("GET", "HEAD"):
        return False
    path = literal_path(url)
    return path is not None and path in free


def _is_agent(request: GateRequest, user_agent: str, config: GateConfig, options: GateOptions) -> Generator[Effect, Any, bool]:
    """An AI agent by its user agent; a script by its default user agent (or none); an agent hiding behind a browser's
    user agent by Web Bot Auth's header or the address it comes from. In test mode only Stilebase's test agent."""
    if is_ai_agent(user_agent, config, options.mode == "test"):
        return True
    if options.mode == "test":
        return False
    if options.catch_http_clients and is_script(user_agent, config):
        return True
    if not options.catch_hidden_agents:
        return False
    if request.header("signature-agent"):
        return True
    if not request.ip:
        return False
    networks: NetworkIndex = yield GetRanges()
    return in_networks(request.ip, networks)


def payment_required(required: dict[str, Any], payment_response: str | None = None) -> Respond:
    text = json.dumps(required)
    headers = {"content-type": "application/json", "cache-control": "no-store", "PAYMENT-REQUIRED": b64(text)}
    if payment_response:
        headers["PAYMENT-RESPONSE"] = payment_response
    return Respond(402, headers, text.encode())


def browser_check(required: dict[str, Any], token: str, config: GateConfig) -> Respond:
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
        # It reloads once: checked again within 30 seconds (the gate didn't find the cookie: blocked, stripped by a
        # proxy, or hidden by planted ones), it asks for cookies instead of reloading forever.
        '<script>var k="stilebase-check",n=Date.now(),t=0;try{t=Number(sessionStorage.getItem(k))||0}catch(e){}'
        f"document.cookie={json.dumps(cookie)};if(document.cookie.indexOf({json.dumps(token)})>=0&&!(n-t<30000))"
        "{try{sessionStorage.setItem(k,String(n))}catch(e){}location.reload()}"
        'else document.getElementById("m").textContent="Please allow cookies for this site, then reload the page.";</script>\n'
        "</body></html>"
    )
    headers = {
        "content-type": "text/html; charset=utf-8",
        "cache-control": "no-store",
        "PAYMENT-REQUIRED": b64(json.dumps(required)),
    }
    return Respond(402, headers, page.encode())


def address_from(headers: dict[str, str], ip_header: str | None, fallback: str | None) -> str | None:
    """The visitor's address from a header your proxy sets (`ip_header`), else the adapter's default. A list (as in
    X-Forwarded-For) gives its last entry: the one your proxy added. The ones before it are the visitor's to write."""
    if ip_header:
        return headers.get(ip_header.lower(), "").split(",")[-1].strip() or None
    return fallback or None
