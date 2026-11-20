# stilebase-sdk

Connect your site to [Stilebase](https://stilebase.com) from Python. **The gate** (`stilebase_sdk.gate`) asks
Stilebase about each request and does what it says: AI agents (GPTBot, ClaudeBot, PerplexityBot…) get
`402 Payment Required` in the open [x402](https://x402.org) format, an agent that pays (USDC on Base) gets the page,
and people, search engines and link previews see no change. The gate has no rules of its own: who's an agent, the
price and the payment are all decided by Stilebase, so they change without a new release. If Stilebase can't be
reached or answers late, everyone gets your site as usual.

## Install

```bash
pip install stilebase-sdk
```

Set the **site key** from your gateway page in Stilebase (**Reveal secret**). Keep it on your server only.

```bash
STILEBASE_KEY=sbk_…
```

Python 3.10 or later. The only dependency is `httpx`.

### FastAPI, Starlette (ASGI)

```python
from stilebase_sdk.gate.asgi import GateMiddleware

app.add_middleware(GateMiddleware)
```

### Flask (WSGI)

```python
from stilebase_sdk.gate.wsgi import GateMiddleware

app.wsgi_app = GateMiddleware(app.wsgi_app)
```

### Django

```python
# settings.py
MIDDLEWARE = ["stilebase_sdk.gate.django.GateMiddleware", ...]  # first
STILEBASE_GATE = {"ip_header": "cf-connecting-ip"}  # optional
```

### Anything else

```python
from stilebase_sdk.gate import GateRequest, Respond, SyncGate  # or AsyncGate, with `await`

gate = SyncGate()
decision = gate.check(GateRequest(method, path_with_query, header_pairs, scheme=scheme, ip=client_ip))
if isinstance(decision, Respond):
    ...  # send decision.status, decision.headers, decision.body instead of your page
else:
    ...  # serve the page, adding decision.response_headers
    # decision.hold: a payment. Hold your answer, then `gate.settle(decision.hold, status, body)`: a Pass means send
    #   it (with that Pass's response_headers), a Respond means send that instead.
    # decision.capture: send a copy with `gate.capture(...)` (AgentView).
```

## Check it

On your gateway page, press **Test now**. By hand: `curl -i -A GPTBot https://your-site.example/` gets a 402.

## What it does

For each request, the gate sends Stilebase the method, path and headers (never the host, the body, your cookies or
`Authorization`) and does what the answer says: let it through (remembering a person for a while, so their next
pages aren't asked about), answer it (a 402, Stilebase's browser check, the domain-verification file), or, for an
agent that pays, hold your page until the payment is settled, so an agent is charged only for a page it got. Static
files (`.css`, `.js`, images, fonts) pass without asking: agents read them without paying. Anything that goes wrong
lets the request through; a payment whose outcome isn't known gets `503` with `Retry-After`, never the page.

## Options

```python
GateMiddleware(
    app,
    key="sbk_…",  # default: STILEBASE_KEY
    ip_header="cf-connecting-ip",  # the visitor's address from a header your proxy or CDN sets
    disabled=True,  # let everyone through and ask nothing (default: STILEBASE_DISABLED=1)
    assume_https=False,  # tell Stilebase the scheme the app sees (default: https, as behind a proxy that ends TLS)
    on_error=lambda error: log.warning(error),  # default: a warning on the "stilebase_sdk.gate" logger
)
```

**The visitor's address:** the connection's (ASGI) or `REMOTE_ADDR` (WSGI, Django); behind a proxy, make those the
visitor's (`--proxy-headers`, `ProxyFix`), or name the header your proxy sets with `ip_header` (from a list such as
`X-Forwarded-For`, its last entry). Never a header the visitor can set.

## Undo

Remove the middleware, set `STILEBASE_DISABLED=1`, or press **Pause** on the gateway page.

## Licence

MIT
