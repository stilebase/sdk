"""How a gate proves its calls come from the site (SPEC.md, "Signing calls to Stilebase"), and the browser check's pass
token. HMAC-SHA256 with the gateway's secret."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re

from ._detect import JS_WHITESPACE

SITE_KEY = re.compile(r"^sbk_([0-9a-f]{32})_([0-9a-f]{64})$")
PASS_TOKEN = re.compile(r"^[0-9]{1,12}\.[0-9a-f]{32}$")


def parse_site_key(key: str) -> tuple[str, str] | None:
    """The gateway's id and signing secret in a site key (`sbk_<32 hex>_<64 hex>`), or None when it isn't one."""
    match = SITE_KEY.match(key.strip(JS_WHITESPACE))
    if not match:
        return None
    h = match.group(1)
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}", f"sbsec_{match.group(2)}"


def _hmac_hex(secret: str, message: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def signed_headers(secret: str, method: str, path: str, body: bytes, now: float) -> dict[str, str]:
    """The headers that prove a call to Stilebase's x402 API comes from your site."""
    ts = str(int(now))
    canonical = "\n".join(["v2", ts, method.upper(), path, hashlib.sha256(body).hexdigest()])
    return {"Stilebase-Timestamp": ts, "Stilebase-Signature": f"v2={_hmac_hex(secret, canonical)}"}


def pass_token(secret: str, user_agent: str, timestamp: int) -> str:
    """The browser check's proof: when it was issued (unix seconds), and an HMAC of that time and the user agent."""
    message = "\n".join(["stilebase-pass", str(timestamp), user_agent])
    return f"{timestamp}.{_hmac_hex(secret, message)[:32]}"


def has_pass(cookie_header: str, cookie_name: str, secret: str, user_agent: str, now: float, max_age: float) -> bool:
    """A browser that passed the check within `max_age` seconds carries the cookie it earned (its time at most a minute
    ahead)."""
    token = ""
    for part in cookie_header.split(";"):
        name, _, value = part.strip(JS_WHITESPACE).partition("=")
        if name == cookie_name:
            token = value
            break
    if not PASS_TOKEN.match(token):
        return False
    timestamp = int(token.split(".")[0])
    age = int(now) - timestamp
    if age < -60 or age > max_age:
        return False
    return hmac.compare_digest(token, pass_token(secret, user_agent, timestamp))


def b64(text: str) -> str:
    """Base64 of a string's UTF-8 bytes (the PAYMENT-REQUIRED header)."""
    return base64.b64encode(text.encode("utf-8", "replace")).decode()
