"""How a gate proves its calls come from the site (SPEC.md, "Signing calls to Stilebase"):
HMAC-SHA256 with the gateway's secret, over "v2\\n<timestamp>\\n<METHOD>\\n<path>\\n<hex SHA-256 of the body>"."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re

SITE_KEY = re.compile(r"^sbk_([0-9a-f]{32})_([0-9a-f]{64})$")
# A bounded timestamp (int() refuses over 4300 digits), with no leading zeros: pass_token never makes one.
PASS_TOKEN = re.compile(r"^[1-9][0-9]{0,11}\.[0-9a-f]{32}$")  # ASCII digits: \d matches any script's


def parse_site_key(key: str) -> tuple[str, str] | None:
    """The gateway's id and signing secret in a site key (`sbk_<32 hex>_<64 hex>`), or None when it isn't one."""
    match = SITE_KEY.match(key.strip())
    if match is None:
        return None
    h = match.group(1)
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}", f"sbsec_{match.group(2)}"


def signed_headers(secret: str, method: str, path: str, body: bytes, now: float) -> dict[str, str]:
    """The headers that prove a call to Stilebase's x402 API comes from your site."""
    timestamp = str(int(now))
    canonical = "\n".join(("v2", timestamp, method.upper(), path, hashlib.sha256(body).hexdigest()))
    mac = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return {"Stilebase-Timestamp": timestamp, "Stilebase-Signature": f"v2={mac}"}


def pass_token(secret: str, user_agent: str, timestamp: int) -> str:
    """The browser check's proof: when it was issued, and an HMAC of that time and the user agent with your secret."""
    mac = hmac.new(secret.encode(), f"stilebase-pass\n{timestamp}\n{user_agent}".encode(), hashlib.sha256).hexdigest()
    return f"{timestamp}.{mac[:32]}"


MAX_PASS_CANDIDATES = 8  # distinct times among one request's pass cookies checked at most (SPEC.md, section 5, step 7)


def has_pass(cookie_header: str, cookie_name: str, secret: str, user_agent: str, now: float, max_age: float) -> bool:
    """A browser that passed the check within `max_age` seconds carries the cookie it earned (its time no more than a
    minute ahead)."""
    # Split at "," too: a runtime may join several Cookie fields (HTTP/2 sends each cookie as its own) with ", ". Every
    # piece with the cookie's name is a candidate, tried from the last: the check page sets the real cookie just before
    # it reloads, so browsers send it after any planted earlier or on a longer path (another cookie's value holding
    # commas, a sibling subdomain's cookie). Well-formed, in-time ones are grouped by their time, and only the last few
    # distinct times get an HMAC (one each), so a Cookie header stuffed with them can't make one request cost hundreds.
    candidates: dict[int, set[str]] = {}  # its time -> the tokens with that time
    for part in reversed(re.split(r"[;,]", cookie_header)):
        name, _, token = part.strip().partition("=")
        if name != cookie_name or not PASS_TOKEN.match(token):
            continue
        timestamp = int(token.split(".")[0])
        age = int(now) - timestamp
        if age < -60 or age > max_age:
            continue
        if timestamp in candidates:
            candidates[timestamp].add(token)
        elif len(candidates) < MAX_PASS_CANDIDATES:
            candidates[timestamp] = {token}
    for timestamp, tokens in candidates.items():
        expected = pass_token(secret, user_agent, timestamp)
        if any(hmac.compare_digest(token, expected) for token in tokens):
            return True
    return False


def b64(text: str) -> str:
    """Base64 of a string's UTF-8 bytes (the PAYMENT-REQUIRED header)."""
    return base64.b64encode(text.encode()).decode()
