"""Who is asking: an AI agent, a script, a browser that doesn't act like one. The checks in Stilebase's Cloudflare
Worker, driven by the settings Stilebase sends (GateConfig) instead of lists baked in. conformance/vectors.json holds
the cases every implementation must agree on."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus, urlsplit

CONFIG_VERSION = 1  # the settings this version reads; another means they changed meaning


@dataclass(frozen=True)
class GateConfig:
    """The settings a gate runs on: GET /v1/x402/sites/{id}/config (SPEC.md, "The settings")."""

    domain: str
    live: bool
    refresh_seconds: float
    verification_path: str | None
    verification_value: str | None
    agents: tuple[str, ...]
    http_clients: tuple[str, ...]
    known_bots: tuple[str, ...]
    weights: Mapping[str, Any]
    suspect_score: float
    cloud_asns: frozenset[Any]
    pass_cookie: str
    pass_max_age: float

    @classmethod
    def parse(cls, value: Any) -> GateConfig | None:
        """A settings document from Stilebase, checked enough to run on; None when it isn't one."""
        try:
            if value["config_version"] != CONFIG_VERSION:
                return None
            gateway, checks, passes = value["gateway"], value["browser_checks"], value["pass"]
            if not isinstance(gateway["domain"], str) or not isinstance(gateway["live"], bool):
                return None
            if not isinstance(checks["weights"], dict) or not isinstance(passes["cookie"], str):
                return None
            verification = value.get("verification") or {}
            refresh = value.get("refresh_seconds")
            return cls(
                domain=gateway["domain"],
                live=gateway["live"],
                refresh_seconds=refresh if _is_number(refresh) and refresh > 0 else 300,
                verification_path=verification.get("path"),
                verification_value=verification.get("value"),
                agents=_strings(value["agents"]),
                http_clients=_strings(value["http_clients"]),
                known_bots=_strings(value["known_bots"]),
                weights=checks["weights"],
                suspect_score=_number(checks["suspect_score"]),
                cloud_asns=frozenset(checks.get("cloud_asns") or ()),
                pass_cookie=passes["cookie"],
                pass_max_age=_number(passes["max_age_seconds"]),
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            return None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _number(value: Any) -> float:
    if not _is_number(value):
        raise TypeError("not a number")
    return int(value) if isinstance(value, float) and value.is_integer() else value  # 43200.0 is 43200, as in JSON


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError("not a list of strings")
    return tuple(value)


@dataclass(frozen=True)
class GateRequest:
    """What a gate needs to know about a request, whatever the framework."""

    method: str
    url: str  # as the visitor asked for it: its scheme says whether they came over HTTPS
    headers: Mapping[str, str]  # names lowercased (headers_of)
    ip: str | None = None  # the visitor's address, as far as it can be trusted (the ip_header option)
    http_protocol: str | None = None  # what the edge knows: "HTTP/1.1", "HTTP/2"…
    asn: int | None = None  # the network's AS number, from the edge

    def header(self, name: str) -> str:
        return self.headers.get(name, "")


def headers_of(pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Headers keyed by lowercased name, repeated ones joined as HTTP allows (cookies with "; ")."""
    headers: dict[str, str] = {}
    for name, value in pairs:
        key = name.lower()
        separator = "; " if key == "cookie" else ", "
        headers[key] = f"{headers[key]}{separator}{value}" if key in headers else value
    return headers


def _names(user_agent: str, tokens: Sequence[str]) -> bool:
    """Whether `user_agent` names any of `tokens`, case-insensitively, anywhere in it."""
    lowered = user_agent.lower()
    return any(token and token.lower() in lowered for token in tokens)


def is_ai_agent(user_agent: str, config: GateConfig) -> bool:
    """An AI agent by its user agent."""
    return _names(user_agent, config.agents)


def is_script(user_agent: str, config: GateConfig) -> bool:
    """A script by its default user agent, or none at all: no person browses with one."""
    return not user_agent.strip() or _names(user_agent, config.http_clients)


def suspect_signals(request: GateRequest, config: GateConfig) -> list[str]:
    """The header checks a browser-looking GET fails, when they add up to a suspect; [] otherwise."""
    user_agent = request.header("user-agent")
    if request.method.upper() not in ("GET", "HEAD") or not user_agent.startswith("Mozilla/"):
        return []
    if _names(user_agent, config.known_bots):
        return []
    # Browsers send Sec-Fetch-* and client hints only over HTTPS: over plain HTTP every real Chrome would fail the checks.
    if not request.url.lower().startswith("https:"):
        return []
    fired = []
    if not request.header("accept-language"):
        fired.append("no_accept_language")
    if not any(request.header(h) for h in ("sec-fetch-mode", "sec-fetch-site", "sec-fetch-dest")):
        fired.append("no_sec_fetch")
    if re.search(r"Chrome/\d", user_agent) and not request.header("sec-ch-ua"):
        fired.append("chrome_without_client_hints")
    accept = request.header("accept").strip()
    if not accept or accept == "*/*":
        fired.append("accept_any")
    if (request.http_protocol or "").upper().startswith("HTTP/1"):
        fired.append("http1")
    if request.asn is not None and request.asn in config.cloud_asns:
        fired.append("cloud_asn")
    score = sum(config.weights.get(signal, 0) for signal in fired)
    return fired if score >= config.suspect_score else []


Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_networks(prefixes: Iterable[Any]) -> list[Network]:
    """The CIDR prefixes that are networks ("20.15.240.64/28"); anything else is left out."""
    networks: list[Network] = []
    for prefix in prefixes:
        try:
            if isinstance(prefix, str) and re.fullmatch(r"[0-9a-fA-F:.]+(/[0-9]{1,3})?", prefix):
                networks.append(ipaddress.ip_network(prefix, strict=False))
        except ValueError:
            continue
    return networks


def in_networks(ip: str | None, networks: Sequence[Network]) -> bool:
    try:
        address = ipaddress.ip_address(ip or "")
    except ValueError:
        return False
    return any(address.version == net.version and address in net for net in networks)


def normalize_ip(ip: str | None) -> str | None:
    """The visitor's address as Stilebase and the address lists expect it: IPv4-mapped IPv6 ("::ffff:8.8.8.8", how
    dual-stack servers report IPv4 visitors) as plain IPv4, trimmed; None when there is none."""
    trimmed = (ip or "").strip()
    mapped = re.fullmatch(r"::ffff:(\d{1,3}(?:\.\d{1,3}){3})", trimmed, re.IGNORECASE)
    return mapped.group(1) if mapped else (trimmed or None)


def path_of(url: str) -> str:
    """The request's path and query ("/listings?city=Haifa"); "/" when the URL can't be read."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "/"
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def form_quote(value: str | bytes, *_: Any) -> str:
    """application/x-www-form-urlencoded as JavaScript's URLSearchParams writes it (`*-._` stay, `~` is encoded): both
    SDKs must cut and send the same lookup. Usable as urlencode's quote_via."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return quote_plus(value.encode("utf-8", "replace"), safe="*").replace("~", "%7E")


def fit_encoded(value: str, max_encoded: int) -> str:
    """`value` cut so its form encoding is at most `max_encoded` long: the price lookup goes in a URL, and proxies refuse
    request lines over 8 KB (a refused lookup would serve the page)."""
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
