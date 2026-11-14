"""Who is asking: an AI agent, a script, a browser that doesn't act like one. A port of the checks in Stilebase's
Cloudflare Worker, driven by the settings Stilebase sends instead of copied lists. conformance/vectors.json holds the
cases every implementation must agree on."""

from __future__ import annotations

import ipaddress
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote_plus

Network = ipaddress.IPv4Network | ipaddress.IPv6Network
CHROME = re.compile(r"Chrome/\d")
HTTP1 = re.compile(r"^HTTP/1", re.IGNORECASE)
CONFIG_VERSION = 1  # the settings this version reads
MAPPED_IPV4 = re.compile(r"^::ffff:(\d{1,3}(?:\.\d{1,3}){3})$", re.IGNORECASE)


@dataclass(frozen=True)
class GateConfig:
    """The settings a gate runs on: GET /v1/x402/sites/{id}/config (SPEC.md, "The settings")."""

    domain: str
    live: bool
    refresh_seconds: int
    verification_path: str | None
    verification_value: str | None
    agents: tuple[str, ...]
    test_mode_agents: tuple[str, ...]
    http_clients: tuple[str, ...]
    known_bots: tuple[str, ...]
    weights: Mapping[str, float]
    suspect_score: float
    cloud_asns: frozenset[float]
    pass_cookie: str
    pass_max_age: float

    @classmethod
    def parse(cls, value: Any) -> GateConfig | None:
        """A settings document from Stilebase, checked enough to run on; None when it isn't one."""
        try:
            if value["config_version"] != CONFIG_VERSION:
                return None  # settings whose meaning changed: run on none rather than misread them
            # As @stilebase/sdk's parseConfig reads them: what a gate can't run without is checked strictly (None),
            # the rest is taken as far as it's usable (an odd cloud_asns entry is dropped, not the whole document).
            gateway, checks, passes = value["gateway"], value["browser_checks"], value["pass"]
            verification = value.get("verification")
            verification = verification if isinstance(verification, dict) else {}
            lists = [_strings(value[name]) for name in ("agents", "test_mode_agents", "http_clients", "known_bots")]
            if not isinstance(gateway["domain"], str) or not isinstance(gateway["live"], bool):
                return None
            if not isinstance(checks["weights"], dict) or not isinstance(passes["cookie"], str):
                return None
            asns = checks.get("cloud_asns")
            return cls(
                domain=gateway["domain"],
                live=gateway["live"],
                refresh_seconds=_refresh_seconds(value.get("refresh_seconds")),
                verification_path=verification.get("path") if isinstance(verification.get("path"), str) else None,
                verification_value=verification.get("value") if isinstance(verification.get("value"), str) else None,
                agents=lists[0],
                test_mode_agents=lists[1],
                http_clients=lists[2],
                known_bots=lists[3],
                weights={str(k): v for k, v in checks["weights"].items() if _is_number(v)},
                suspect_score=_number(checks["suspect_score"]),
                cloud_asns=frozenset(a for a in asns if _is_number(a)) if isinstance(asns, list) else frozenset(),
                pass_cookie=passes["cookie"],
                pass_max_age=_number(passes["max_age_seconds"]),
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            return None


def _is_number(value: Any) -> bool:
    """A finite number, as JavaScript's typeof sees one: never a bool or a numeric string."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _number(value: Any) -> float:
    if not _is_number(value):
        raise TypeError("not a number")
    return float(value) if isinstance(value, float) else int(value)


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError("not a list of strings")
    return tuple(value)


@dataclass(frozen=True)
class GateRequest:
    """What a gate needs to know about a request, whatever the framework."""

    method: str
    url: str  # as the visitor asked for it: its scheme says whether they came over HTTPS
    headers: Mapping[str, str]  # names lowercased (see headers_of)
    ip: str | None = None  # the visitor's address, as far as it can be trusted (the ip_header option)
    http_protocol: str | None = None  # what the edge knows: "HTTP/1.1", "HTTP/2"…
    asn: int | None = None  # the network's AS number, from the edge
    extra: Mapping[str, Any] = field(default_factory=dict)

    def header(self, name: str) -> str:
        return self.headers.get(name, "")


def headers_of(pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Headers keyed by lowercased name, repeated ones joined as HTTP allows: with ", ", and cookies with "; " (HTTP/2
    servers pass each cookie as its own header)."""
    headers: dict[str, str] = {}
    for name, value in pairs:
        key = name.lower()
        separator = "; " if key == "cookie" else ", "
        headers[key] = f"{headers[key]}{separator}{value}" if key in headers else value
    return headers


# A host name (underscores too: Docker Compose service names), or a bracketed IPv6 literal, and an optional port.
HOST = re.compile(r"^(?:[a-z0-9_-]+(?:\.[a-z0-9_-]+)*|\[[0-9a-f:.]+\])(?::\d{1,5})?$", re.IGNORECASE)


def request_url(scheme: str, host: str | None, path: str, query: str = "") -> str:
    """The URL a visitor asked for, from parts a framework gives (`path` percent-encoded). The Host header is the
    visitor's to write, so anything that isn't a host name (a "?" or "#" would change the path the gate reads) becomes
    "localhost"."""
    safe_host = host if host and HOST.match(host) else "localhost"
    path = (path if path.startswith("/") else f"/{path}").replace("?", "%3F").replace("#", "%23")
    return f"{'https' if scheme == 'https' else 'http'}://{safe_host}{path}{'?' + query if query else ''}"


def names(user_agent: str, tokens: Sequence[str]) -> bool:
    """Whether `user_agent` names any of `tokens`, case-insensitively, anywhere in it."""
    lowered = user_agent.lower()
    return any(token and token.lower() in lowered for token in tokens)


def is_ai_agent(user_agent: str, config: GateConfig, test_mode: bool = False) -> bool:
    """An AI agent by its user agent (in test mode, only Stilebase's test agent)."""
    return names(user_agent, config.test_mode_agents if test_mode else config.agents)


def is_script(user_agent: str, config: GateConfig) -> bool:
    """A script by its default user agent, or none at all: no person browses with one."""
    return not user_agent.strip() or names(user_agent, config.http_clients)


def suspect_signals(request: GateRequest, config: GateConfig, assume_https: bool = False) -> list[str]:
    """The header checks a browser-looking GET fails, when they add up to a suspect; [] otherwise."""
    user_agent = request.header("user-agent")
    if request.method.upper() not in ("GET", "HEAD") or not user_agent.startswith("Mozilla/"):
        return []
    if names(user_agent, config.known_bots):
        return []
    # Browsers send Sec-Fetch-* and client hints only over HTTPS: over plain HTTP every real Chrome would fail the checks.
    if not assume_https and not request.url.lower().startswith("https:"):
        return []
    fired: list[str] = []
    if not request.header("accept-language"):
        fired.append("no_accept_language")
    if not any(request.header(h) for h in ("sec-fetch-mode", "sec-fetch-site", "sec-fetch-dest")):
        fired.append("no_sec_fetch")
    if CHROME.search(user_agent) and not request.header("sec-ch-ua"):
        fired.append("chrome_without_client_hints")
    accept = request.header("accept").strip()
    if not accept or accept == "*/*":
        fired.append("accept_any")
    if HTTP1.match(request.http_protocol or ""):
        fired.append("http1")
    if request.asn is not None and request.asn in config.cloud_asns:
        fired.append("cloud_asn")
    score = sum(config.weights.get(signal, 0) for signal in fired)
    return fired if score >= config.suspect_score else []


def normalize_ip(ip: str | None) -> str | None:
    """The visitor's address as Stilebase and the address lists expect it: IPv4-mapped IPv6 ("::ffff:8.8.8.8", how
    dual-stack servers report IPv4 visitors) as plain IPv4, trimmed; None when there is none."""
    trimmed = (ip or "").strip()
    mapped = MAPPED_IPV4.match(trimmed)
    return mapped.group(1) if mapped else (trimmed or None)


LITERAL_PATH = re.compile(r"/[A-Za-z0-9._~/-]*")
LITERAL_URL = re.compile(r"https?://([^/?#]*)(/[^?#]*)(?:\?[^#]*)?", re.IGNORECASE | re.DOTALL)


def is_literal_path(path: str) -> bool:
    """A path every parser reads the same way: from "/", only letters, digits and "-" "." "_" "~" between slashes, no
    empty, "." or ".." segment (a trailing slash is fine). No percent-encoding, so nothing to decode or resolve."""
    if not LITERAL_PATH.fullmatch(path):
        return False
    segments = path[1:].split("/")
    if segments and segments[-1] == "":
        segments.pop()  # a trailing slash
    return all(segment not in ("", ".", "..") for segment in segments)


def literal_path(url: str) -> str | None:
    """The path of a request URL when the whole URL is literal: http(s), a plain host name, then a literal path (a
    query may follow, never a "#": a request URL has none, unless a forged Host put it there). None for anything else
    (a Host like "x?", percent-encoding, dot segments, an odd scheme): such a request is never free, since some router
    may read it differently."""
    match = LITERAL_URL.fullmatch(url)
    if not match or not HOST.match(match.group(1)) or not is_literal_path(match.group(2)):
        return None
    return match.group(2)


SCHEME_AND_AUTHORITY = re.compile(r"^[a-z][a-z0-9+.-]*://[^/?#]*", re.IGNORECASE)


def site_path(raw: str) -> tuple[str, str]:
    """The path and query of the URL an adapter gave, whatever its host part: the gate names the page by the gateway's
    own domain (site_url), never by the Host header, which is the visitor's to write (and behind a proxy an internal
    name like app:3000). "//evil.example/x" stays a path. The page is the part before a "#", as most routers read it
    (a URL with one is never free: literal_path). Never raises."""
    rest = SCHEME_AND_AUTHORITY.sub("", raw, count=1).split("#", 1)[0].replace("\\", "%5C")
    path, _, query = rest.partition("?")
    return "/" + path.lstrip("/"), query


def site_url(domain: str, path: str, query: str) -> str:
    return f"https://{domain}{path}{'?' + query if query else ''}"


def form_quote(value: str | bytes, *_: Any) -> str:
    """application/x-www-form-urlencoded as the WHATWG URL standard serializes it (JavaScript's URLSearchParams): letters,
    digits and `*-._` stay, a space is `+`, everything else is percent-encoded UTF-8. Python's quote_plus differs on two
    characters (it keeps `~` and escapes `*`); both SDKs must cut and send the same lookup. Usable as urlencode's
    quote_via."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "surrogateescape")
    value = _LONE_SURROGATE.sub("\ufffd", value)  # as URLSearchParams writes one (a header decoded with surrogateescape)
    return quote_plus(value, safe="*").replace("~", "%7E")


_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def fit_encoded(value: str, max_encoded: int) -> str:
    """`value` cut so its form-encoding is at most `max_encoded` long: the price lookup goes in a URL, and proxies refuse
    request lines over 8 KB (a refusal must never serve the page)."""
    if len(form_quote(value)) <= max_encoded:
        return value
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if len(form_quote(value[:middle])) <= max_encoded:
            low = middle
        else:
            high = middle - 1
    return value[:low]


class NetworkIndex:
    """Networks grouped by prefix length, so a lookup costs one set probe per distinct length (a few dozen), not one
    comparison per network (thousands): it runs for every request from a person."""

    def __init__(self, networks: Iterable[Network]) -> None:
        groups: dict[tuple[int, int], set[int]] = {}
        for net in networks:
            shift = net.max_prefixlen - net.prefixlen
            groups.setdefault((net.version, shift), set()).add(int(net.network_address) >> shift)
        self._groups = list(groups.items())

    def __contains__(self, address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        value = int(address)
        return any(version == address.version and value >> shift in bases for (version, shift), bases in self._groups)


def parse_networks(prefixes: Iterable[Any]) -> list[Network]:
    networks: list[Network] = []
    for prefix in prefixes:
        try:
            networks.append(ipaddress.ip_network(str(prefix), strict=False))
        except ValueError:
            continue
    return networks


def in_networks(ip: str | None, networks: Sequence[Network] | NetworkIndex) -> bool:
    try:
        address = ipaddress.ip_address(ip or "")
    except ValueError:
        return False
    if isinstance(networks, NetworkIndex):
        return address in networks
    return any(address.version == net.version and address in net for net in networks)


def _refresh_seconds(value: Any) -> int:
    """How often to fetch the settings again: 300 when it's missing, not a number or not positive (as @stilebase/sdk);
    the gate never refreshes more often than every 30 seconds whatever it says."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value <= 0:
        return 300
    return int(value)
