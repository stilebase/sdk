"""How a gate proves its calls come from the site (SPEC.md, "Signing calls to Stilebase"): HMAC-SHA256 with the
gateway's secret, from the site key."""

from __future__ import annotations

import hashlib
import hmac
import re

from ._request import JS_WHITESPACE

SITE_KEY = re.compile(r"^sbk_([0-9a-f]{32})_([0-9a-f]{64})$")


def parse_site_key(key: str) -> tuple[str, str] | None:
    """The gateway's id and signing secret in a site key (`sbk_<32 hex>_<64 hex>`), or None when it isn't one."""
    match = SITE_KEY.match(key.strip(JS_WHITESPACE))
    if not match:
        return None
    h = match.group(1)
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}", f"sbsec_{match.group(2)}"


def signed_headers(secret: str, method: str, path: str, body: bytes, now: float) -> dict[str, str]:
    """The headers that prove a call to Stilebase comes from your site."""
    ts = str(int(now))
    canonical = "\n".join(["v2", ts, method.upper(), path, hashlib.sha256(body).hexdigest()])
    mac = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return {"Stilebase-Timestamp": ts, "Stilebase-Signature": f"v2={mac}"}
