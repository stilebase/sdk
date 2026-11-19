"""The gate, sans IO (SPEC.md): forward each request to Stilebase, do what the answer says, let everything through when
anything goes wrong. Everything here is shared by the async driver (ASGI) and the sync one (WSGI, Django); they only
do the calls."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeGuard

from .._version import PACKAGE, VERSION
from ._request import JS_WHITESPACE, GateRequest, normalize_ip
from ._signing import parse_site_key, signed_headers

DEFAULT_API_URL = "https://stilebase.com"
SETTINGS_VERSION = 1
SETTINGS_TIMEOUT = 3.0  # the first request waits at most this long for the settings
MIN_REFRESH = 30.0
RETRY_WITHOUT_SETTINGS = 30.0
MAX_HEADERS = 100
UNKNOWN_RETRY = 5
CAPTURE_QUEUE = 20
CAPTURE_TIMEOUT = 10.0

logger = logging.getLogger("stilebase_sdk.gate")


@dataclass
class GateOptions:
    """How the gate runs. Everything has a default; most sites set nothing (the key comes from STILEBASE_KEY)."""

    key: str | None = field(default=None, repr=False)  # the site key from your gateway page; default: STILEBASE_KEY
    api_url: str | None = None  # Stilebase's API; default: STILEBASE_API_URL, else https://stilebase.com
    disabled: bool | None = None  # every request passes, nothing is asked; default: STILEBASE_DISABLED
    assume_https: bool = True  # send scheme https whatever the app sees (behind a TLS-terminating proxy)
    on_error: Callable[[Exception], None] | None = None  # default: a warning on this logger, at most once a minute each
    now: Callable[[], float] = field(default=time.time)  # for tests: the time in seconds
    transport: Any = None  # for tests: an httpx transport


@dataclass(frozen=True)
class Settings:
    """What a gate runs on: GET /v1/gate/sites/{id}/settings (SPEC.md, section 3; conformance `settings`)."""

    enabled: bool = True
    refresh_seconds: int = 300
    timeout_ms: int = 300
    settle_timeout_ms: int = 65_000
    breaker_failures: int = 5
    breaker_open_seconds: int = 30
    remember_by: tuple[str, ...] = ("client_ip", "user-agent", "path")
    cache_max_entries: int = 10_000
    strip_headers: tuple[str, ...] = ()
    forward_cookies: tuple[str, ...] = ()
    skip_prefixes: tuple[str, ...] = ()
    skip_extensions: tuple[str, ...] = ()
    max_bytes: int = 10 * 1024 * 1024

    @property
    def timeout(self) -> float:
        return self.timeout_ms / 1000

    @property
    def settle_timeout(self) -> float:
        return self.settle_timeout_ms / 1000

    @classmethod
    def parse(cls, value: Any) -> Settings | None:
        """Settings as Stilebase sent them: None (everyone passes) unless `settings_version` is 1 and `enabled` a
        boolean; any other field missing, of the wrong type or (a number) not a positive integer takes its default."""
        if not isinstance(value, dict) or value.get("settings_version") != SETTINGS_VERSION:
            return None
        if not isinstance(value.get("enabled"), bool):
            return None
        d = cls()
        breaker = _object(value.get("breaker"))
        skip = _object(value.get("skip_paths"))
        settle = _object(value.get("settle_after"))
        return cls(
            enabled=value["enabled"],
            refresh_seconds=positive_integer(value.get("refresh_seconds"), d.refresh_seconds),
            timeout_ms=positive_integer(value.get("timeout_ms"), d.timeout_ms),
            settle_timeout_ms=positive_integer(value.get("settle_timeout_ms"), d.settle_timeout_ms),
            breaker_failures=positive_integer(breaker.get("failures"), d.breaker_failures),
            breaker_open_seconds=positive_integer(breaker.get("open_seconds"), d.breaker_open_seconds),
            remember_by=_strings(value.get("remember_by"), d.remember_by),
            cache_max_entries=positive_integer(value.get("cache_max_entries"), d.cache_max_entries),
            strip_headers=_strings(value.get("strip_headers"), ()),
            forward_cookies=_strings(value.get("forward_cookies"), ()),
            skip_prefixes=_strings(skip.get("prefixes"), ()),
            skip_extensions=_strings(skip.get("extensions"), ()),
            max_bytes=positive_integer(settle.get("max_bytes"), d.max_bytes),
        )

    def read(self) -> dict[str, Any]:
        """The settings as conformance `settings` writes them, every default filled in."""
        return {
            "settings_version": SETTINGS_VERSION,
            "enabled": self.enabled,
            "refresh_seconds": self.refresh_seconds,
            "timeout_ms": self.timeout_ms,
            "settle_timeout_ms": self.settle_timeout_ms,
            "breaker": {"failures": self.breaker_failures, "open_seconds": self.breaker_open_seconds},
            "remember_by": list(self.remember_by),
            "cache_max_entries": self.cache_max_entries,
            "strip_headers": list(self.strip_headers),
            "forward_cookies": list(self.forward_cookies),
            "skip_paths": {"prefixes": list(self.skip_prefixes), "extensions": list(self.skip_extensions)},
            "settle_after": {"max_bytes": self.max_bytes},
        }


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _integral(value: Any) -> TypeGuard[int | float]:
    """An integer as JSON carries one (600 or 600.0, as JavaScript reads both); never a boolean."""
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, float) and value.is_integer())


def positive_integer(value: Any, default: int) -> int:
    """A positive integer, or `default`."""
    return int(value) if _integral(value) and value > 0 else default


def _strings(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    """A list of strings, or `default` (a list holding anything else is of the wrong type)."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return default
    return tuple(value)


# Decisions an adapter acts on.


@dataclass(frozen=True)
class Capture:
    """Copy the site's answer to this request and send it to Stilebase (SPEC.md, section 9)."""

    token: str
    max_bytes: int


@dataclass(frozen=True)
class Hold:
    """Hold the site's answer, then `settle` it (SPEC.md, section 7)."""

    body: dict[str, Any]  # the decide body, sent again with the delivery
    max_bytes: int


@dataclass(frozen=True)
class Pass:
    """Let the request through: set `response_headers` on the site's answer; `hold` it for settling; `capture` it."""

    response_headers: Mapping[str, str] = field(default_factory=dict)
    hold: Hold | None = None
    capture: Capture | None = None


@dataclass(frozen=True)
class Respond:
    """Answer the request with this instead of the site."""

    status: int
    headers: dict[str, str]
    body: bytes


Decision = Pass | Respond
PASS = Pass()


def respond(status: int, headers: Mapping[str, str], body: str | bytes) -> Respond:
    """An answer the gate sends itself, never kept by a cache in front of the site."""
    data = body.encode("utf-8", "replace") if isinstance(body, str) else body
    kept = {k: v for k, v in headers.items() if k.lower() != "cache-control"}
    return Respond(status, {**kept, "cache-control": "no-store"}, data)


def unknown_payment(retry_after: int = UNKNOWN_RETRY) -> Respond:
    """A payment whose outcome isn't known: the agent may retry; it's never told it wasn't charged."""
    body = json.dumps({"error": "Payment outcome unknown, try again"}, separators=(",", ":"))
    return respond(503, {"content-type": "application/json", "retry-after": str(retry_after)}, body)


def too_large() -> Respond:
    """A paid answer bigger than the gate can hold: not settled, so the agent isn't charged."""
    body = json.dumps({"error": "This answer is too large to be paid for through this gate"}, separators=(",", ":"))
    return respond(502, {"content-type": "application/json"}, body)


# The rules (SPEC.md); pure, and checked against conformance/vectors.json.


_FORBIDDEN = ("%", ";", "\\", "..")
# Never sent, whatever the settings say: they add to this list, never take from it.
ALWAYS_STRIPPED = frozenset({"cookie", "authorization", "proxy-authorization"})


def skipped(path: str, settings: Settings) -> bool:
    """Whether a path is passed without asking (SPEC.md, section 5): matched as received, never decoded, and never
    when it holds anything a router might read as another page."""
    raw = path.split("#", 1)[0].split("?", 1)[0]
    if any(token in raw for token in _FORBIDDEN) or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        return False
    lowered = raw.lower()
    if any(prefix and lowered.startswith(prefix.lower()) for prefix in settings.skip_prefixes):
        return True
    segment = lowered.rsplit("/", 1)[-1]
    return any(ext and segment.endswith(ext.lower()) and len(segment) > len(ext) for ext in settings.skip_extensions)


def remember_key(request: GateRequest, settings: Settings) -> str:
    """The SHA-256 (lowercase hex) of the `remember_by` fields joined with "\\n"; a missing value is empty."""
    values = {"client_ip": normalize_ip(request.ip) or "", "path": request.path}
    joined = "\n".join(values[name] if name in values else request.header(name) for name in settings.remember_by)
    return hashlib.sha256(joined.encode("utf-8", "replace")).hexdigest()


def decide_body(request: GateRequest, settings: Settings, assume_https: bool) -> dict[str, Any]:
    """What `decide` is sent (SPEC.md, section 6, step 4): never the host, the body, cookies or credentials."""
    stripped = {name.lower() for name in settings.strip_headers} | ALWAYS_STRIPPED
    headers = [[name.lower(), value] for name, value in request.headers if name.lower() not in stripped]
    cookies: dict[str, str] = {}
    credentials = False
    for name, value in request.headers:
        lowered = name.lower()
        if lowered in ("cookie", "authorization"):
            credentials = True
        if lowered == "cookie":
            for part in value.split(";"):
                cookie, sep, content = part.partition("=")
                cookie = cookie.strip(JS_WHITESPACE)
                if sep and cookie in settings.forward_cookies and cookie not in cookies:
                    cookies[cookie] = content.strip(JS_WHITESPACE)
    path = request.path if request.path.startswith("/") else f"/{request.path}"
    body: dict[str, Any] = {
        "request": {
            "method": request.method.upper(),
            "scheme": "https" if assume_https else (request.scheme or "http").lower(),
            "path": path,
        },
        "client_ip": normalize_ip(request.ip),
        "headers": headers[:MAX_HEADERS],
        "cookies": cookies,
        "credentials": credentials,
    }
    edge: dict[str, Any] = {k: v for k, v in (("asn", request.asn), ("http_protocol", request.http_protocol)) if v is not None}
    if edge:
        body["edge"] = edge
    return body


@dataclass(frozen=True)
class Answer:
    """`decide`'s answer, read (SPEC.md, section 6, step 5). `usable` False: anything the gate doesn't understand."""

    usable: bool
    decision: Decision = PASS
    remember: float | None = None


UNUSABLE = Answer(False)


def read_answer(status: int, body: Any) -> Answer:
    """What a `decide` (or `settle`) answer tells the gate to do (SPEC.md, section 6 step 5, "Reading an answer";
    conformance `answers`). Fields it doesn't know are ignored."""
    if status != 200 or not isinstance(body, dict):
        return UNUSABLE
    action = body.get("action")
    if action == "respond":
        code = body.get("status")
        headers = {} if body.get("headers") is None else body["headers"]
        content = body.get("body", "")
        if not (_integral(code) and 200 <= code <= 599) or not _string_map(headers) or not isinstance(content, str):
            return UNUSABLE
        return Answer(True, respond(int(code), headers, content))
    if action == "unknown_payment":
        retry = body.get("retry_after")
        return Answer(True, unknown_payment(int(retry) if _integral(retry) and 1 <= retry <= 3600 else UNKNOWN_RETRY))
    if action != "pass":
        return UNUSABLE
    response_headers = body.get("response_headers", {})
    if not _string_map(response_headers):
        return UNUSABLE
    hold: Hold | None = None
    if "settle_after" in body:
        settle = body["settle_after"]
        size = settle.get("max_bytes") if isinstance(settle, dict) else None
        if not (_integral(size) and size > 0):
            return UNUSABLE
        hold = Hold({}, int(size))
    capture: Capture | None = None
    order = body.get("capture")
    if isinstance(order, dict):
        token, size = order.get("token"), order.get("max_bytes")
        if isinstance(token, str) and _integral(size) and size >= 0:
            capture = Capture(token, int(size))
    remember = body.get("remember")
    seconds = remember.get("seconds") if isinstance(remember, dict) else None
    keep = float(seconds) if _integral(seconds) and seconds > 0 else None
    return Answer(True, Pass(dict(response_headers), hold, capture), keep)


def _string_map(value: Any) -> bool:
    return isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())


def delivery(status: int, body: bytes) -> dict[str, Any]:
    """What the site answered a paid request (SPEC.md, section 7, step 3)."""
    return {"status": status, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}


def capture_body(
    token: str, status: int, headers: Sequence[tuple[str, str]], body: bytes, size: int, truncated: bool, ttfb_ms: int
) -> dict[str, Any]:
    """A copy of the site's answer for Stilebase (SPEC.md, section 9)."""
    return {
        "token": token,
        "status": status,
        "headers": [[n, v] for n, v in headers],
        "body": base64.b64encode(body).decode(),
        "size": size,
        "truncated": truncated,
        "ttfb_ms": ttfb_ms,
    }


class Copy:
    """The first `limit` bytes of a body as it goes by, its whole size, and whether it was cut."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.size = 0
        self.parts: list[bytes] = []
        self._kept = 0

    def add(self, chunk: bytes) -> None:
        self.size += len(chunk)
        room = self.limit - self._kept
        if room > 0 and chunk:
            piece = chunk[:room]
            self.parts.append(piece)
            self._kept += len(piece)

    @property
    def body(self) -> bytes:
        return b"".join(self.parts)

    @property
    def truncated(self) -> bool:
        return self.size > self._kept


class RememberCache:
    """Remembered `pass` answers: at most `size`, the oldest going first, each until it expires. Thread-safe."""

    def __init__(self) -> None:
        self._entries: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str, now: float) -> bool:
        with self._lock:
            expires = self._entries.get(key)
            if expires is None:
                return False
            if expires <= now:
                del self._entries[key]
                return False
            return True

    def put(self, key: str, until: float, size: int) -> None:
        with self._lock:
            self._entries.pop(key, None)
            while self._entries and len(self._entries) >= size:
                self._entries.popitem(last=False)
            self._entries[key] = until


class Breaker:
    """SPEC.md, section 6 step 6. `failures` failed decisions in a row open it for `open_seconds` (nothing is asked);
    then it's half open: exactly one request is asked, and its outcome closes the breaker or opens it again."""

    def __init__(self) -> None:
        self._failures = 0
        self._open_until = 0.0
        self._probing = False
        self._lock = threading.Lock()

    def is_open(self, now: float) -> bool:
        """Open, or half open with its one request already being asked: nothing more is asked."""
        with self._lock:
            return self._open_until > 0 and (now < self._open_until or self._probing)

    def admit(self, now: float, settings: Settings) -> Asking | None:
        """Whether this request may be asked: as usual while closed; half open, only the first (the probe)."""
        with self._lock:
            if self._open_until == 0:
                return Asking(probe=False)
            if now < self._open_until or self._probing:
                return None
            self._probing = True
            return Asking(probe=True)

    def record(self, ok: bool, now: float, settings: Settings, asking: Asking | None = None) -> None:
        with self._lock:
            if asking is not None and asking.probe:
                self._probing = False
            if ok:
                self._failures = 0
                self._open_until = 0.0
                return
            self._failures += 1
            if self._failures >= settings.breaker_failures:
                self._open_until = now + settings.breaker_open_seconds


@dataclass(frozen=True)
class Asking:
    """A request the breaker lets be asked; `probe`: the one request asked while it's half open."""

    probe: bool


class GateError(Exception):
    """Something the gate couldn't do; it let the request through (or answered 503 for a payment)."""


def json_body(content: bytes) -> Any:
    try:
        return json.loads(content) if content else None
    except ValueError:
        return None


def _disabled_by_env() -> bool:
    return os.environ.get("STILEBASE_DISABLED", "").strip().lower() in ("1", "true", "yes", "on")


class Engine:
    """What both drivers share: the site key, the options, the settings, the remembered answers and the breaker."""

    def __init__(self, options: GateOptions, adapter: str) -> None:
        self.options = options
        self.client = f"{PACKAGE}/{VERSION} gate/{adapter}"  # e.g. "stilebase-sdk/0.2.0 gate/asgi"
        self.api = (options.api_url or os.environ.get("STILEBASE_API_URL") or DEFAULT_API_URL).rstrip("/")
        key = options.key if options.key is not None else os.environ.get("STILEBASE_KEY", "")
        self.site = parse_site_key(key)
        self.disabled = options.disabled if options.disabled is not None else _disabled_by_env()
        self.settings: Settings | None = None
        self.settings_at = 0.0  # when the settings were last loaded (or tried); 0: never
        self.remembered = RememberCache()
        self.breaker = Breaker()
        self._warned: dict[str, float] = {}
        if self.site is None and not self.disabled:
            self.report(GateError(f"STILEBASE_KEY {'is not a site key (sbk_…)' if key else 'is not set'}: every request passes"))

    @property
    def active(self) -> bool:
        return self.site is not None and not self.disabled

    def path(self, name: str) -> str:
        assert self.site is not None
        return f"/v1/gate/sites/{self.site[0]}/{name}"

    def settings_headers(self) -> dict[str, str]:
        assert self.site is not None
        signed = signed_headers(self.site[1], "GET", self.path("settings"), b"", self.options.now())
        return {**signed, "Stilebase-Client": self.client}

    def settings_due(self) -> bool:
        every = max(self.settings.refresh_seconds, MIN_REFRESH) if self.settings else RETRY_WITHOUT_SETTINGS
        return self.options.now() - self.settings_at >= every

    def take_settings(self, status: int, body: Any) -> None:
        """What the settings call answered. Any failure keeps the last good settings (a 401 after a rotated key), except
        a 404: the gateway was removed, so everyone passes."""
        try:
            if status == 404:
                self.settings = None
                raise GateError("Stilebase has no gateway for this site key (404): every request passes")
            if status != 200:
                raise GateError(f"Stilebase answered the settings {status}")
            if not isinstance(body, dict):  # not a JSON object: a failed fetch, the last good settings stay
                raise GateError("Stilebase's settings answer isn't a JSON object")
            settings = Settings.parse(body)
            if settings is None:
                self.settings = None
                raise GateError("Stilebase sent settings this version can't read: every request passes")
            self.settings = settings
        finally:
            self.settings_at = self.options.now()

    def signed_post(self, name: str, payload: dict[str, Any]) -> tuple[str, bytes, dict[str, str]]:
        assert self.site is not None
        path = self.path(name)
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8", "replace")
        headers = {
            "content-type": "application/json",
            **signed_headers(self.site[1], "POST", path, body, self.options.now()),
            "Stilebase-Client": self.client,
        }
        return self.api + path, body, headers

    def before_asking(self, request: GateRequest, settings: Settings | None) -> Decision | Asking:
        """A decision made without asking (SPEC.md, section 6, steps 1-3 and 6), or `Asking`: ask."""
        payment = request.payment is not None
        now = self.options.now()
        if settings is None or not settings.enabled:
            return PASS
        if self.breaker.is_open(now):
            return unknown_payment() if payment else PASS
        if not payment and skipped(request.path, settings):
            return PASS
        if not payment and self.remembered.get(remember_key(request, settings), now):
            return PASS
        asking = self.breaker.admit(now, settings)
        if asking is None:  # half open, and another request is the one being asked
            return unknown_payment() if payment else PASS
        return asking

    def after_asking(
        self, request: GateRequest, settings: Settings, body: dict[str, Any], answer: Answer, asking: Asking | None = None
    ) -> Decision:
        """What the gate does with `decide`'s answer (SPEC.md, section 6, steps 5-6)."""
        payment = request.payment is not None
        self.breaker.record(answer.usable, self.options.now(), settings, asking)
        if not answer.usable:
            return unknown_payment() if payment else PASS
        decision = answer.decision
        if isinstance(decision, Pass):
            if decision.hold is not None:
                limit = min(decision.hold.max_bytes, settings.max_bytes)  # the settings cap what any answer asks to hold
                decision = Pass(decision.response_headers, Hold(body, limit), decision.capture)
            elif answer.remember and not payment and not decision.response_headers and decision.capture is None:
                # Only a plain pass is remembered: one with headers, a capture or a hold is about this request alone.
                key = remember_key(request, settings)
                self.remembered.put(key, self.options.now() + answer.remember, settings.cache_max_entries)
        return decision

    def gave_up(self, settings: Settings | None, asking: Asking | None) -> None:
        """A request that was being asked failed before its answer was read: the breaker counts it (and frees its
        probe)."""
        if settings is not None and asking is not None:
            self.breaker.record(False, self.options.now(), settings, asking)

    def settled(self, status: int, body: Any) -> Decision:
        """What `settle` answered (SPEC.md, section 7, step 4): `Pass` (send the held answer with its headers), or what
        to send instead."""
        answer = read_answer(status, body)
        if not answer.usable:
            return unknown_payment()
        decision = answer.decision
        if isinstance(decision, Pass):
            return Pass(decision.response_headers)
        return decision

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
