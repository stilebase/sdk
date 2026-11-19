"""What a gate knows about a visitor's request, whatever the framework (SPEC.md, "Each request" and "The visitor's
address"). The gate decides nothing from it: it forwards it to Stilebase."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

# What JavaScript's String.prototype.trim() removes: both SDKs and the Worker read a header the same way.
JS_WHITESPACE = "\t\n\v\f\r \xa0                　﻿"
PAYMENT_HEADERS = ("payment-signature", "x-payment")


@dataclass(frozen=True)
class GateRequest:
    """A visitor's request. `path` is the path with the query, as the framework gives it (never the host); `headers`
    every header as received, in order, names in any case."""

    method: str
    path: str
    headers: tuple[tuple[str, str], ...] = ()
    scheme: str = "http"  # how the request reached the app (behind a TLS-terminating proxy: http; see assume_https)
    ip: str | None = None  # the visitor's address, as far as it can be trusted (the ip_header option)
    asn: int | None = None  # what the edge knows (Cloudflare's request.cf), when there is an edge
    http_protocol: str | None = None
    _index: dict[str, str] = field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", tuple((str(n), str(v)) for n, v in self.headers))
        object.__setattr__(self, "_index", headers_of(self.headers))

    def header(self, name: str) -> str:
        """A header's value by name (any case), repeated ones joined; "" when absent."""
        return self._index.get(name.lower(), "")

    @property
    def payment(self) -> str | None:
        """The agent's payment (PAYMENT-SIGNATURE, else the v1 X-PAYMENT); an empty header is none."""
        return next((self.header(h) for h in PAYMENT_HEADERS if self.header(h).strip(JS_WHITESPACE)), None)


def headers_of(pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Headers keyed by lowercased name, repeated ones joined as HTTP allows (cookies with "; ")."""
    headers: dict[str, str] = {}
    for name, value in pairs:
        key = name.lower()
        separator = "; " if key == "cookie" else ", "
        headers[key] = f"{headers[key]}{separator}{value}" if key in headers else value
    return headers


_MAPPED = re.compile(r"::ffff:(\d{1,3}(?:\.\d{1,3}){3})", re.IGNORECASE)


def normalize_ip(ip: str | None) -> str | None:
    """The visitor's address as Stilebase expects it: trimmed, IPv4-mapped IPv6 ("::ffff:8.8.8.8", how dual-stack
    servers report IPv4 visitors) as plain IPv4; None when there is none."""
    trimmed = (ip or "").strip(JS_WHITESPACE)
    mapped = _MAPPED.fullmatch(trimmed)
    return mapped.group(1) if mapped else (trimmed or None)


def address_from(headers: Mapping[str, str], name: str | None, fallback: str | None) -> str | None:
    """The visitor's address from a header your proxy sets: from a list (X-Forwarded-For) its last entry, the one your
    proxy added; the ones before it are the visitor's to write. Else the server's own address for the connection."""
    if name:
        return headers.get(name.lower(), "").split(",")[-1].strip(JS_WHITESPACE) or None
    return fallback or None
