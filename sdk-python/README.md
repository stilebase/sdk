# stilebase-sdk

Connect your site to [Stilebase](https://stilebase.com) from Python. **The gate** (`stilebase_sdk.gate`) does what
Stilebase's Cloudflare Worker does, from inside your app: AI agents (GPTBot, ClaudeBot, PerplexityBot…) get
`402 Payment Required` in the open [x402](https://x402.org) format, an agent that pays (USDC on Base) gets the page, and
Stilebase supplies the price and settles the payment. People, search engines and link previews see no change. If
Stilebase can't be reached, everyone gets your site as usual.

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
from stilebase_sdk.gate import GateRequest, Respond, SyncGate  # or AsyncGate, with `await gate.check(...)`

gate = SyncGate()
decision = gate.check(GateRequest(method, url, headers, ip=client_ip))  # headers: lowercased names (headers_of)
if isinstance(decision, Respond):
    ...  # send decision.status, decision.headers, decision.body instead
# else serve the request; add PAYMENT-RESPONSE: decision.payment_response when it's set
```

## Check it

On your gateway page, press **Test now**. By hand: `curl -i -A GPTBot https://your-site.example/` gets a 402.

## What it does

Exactly what the Worker does, with its settings (agent lists, checks, price) fetched from Stilebase and refreshed
every five minutes: agents, scripts with their default user agent and agents behind a browser's user agent get the
402; a payment is settled through Stilebase and the page served with `PAYMENT-RESPONSE` (`503` if Stilebase doesn't
confirm it); browser-looking requests that don't act like a browser are counted, or checked under **Strict
detection**; the domain-verification file is served until your domain is verified.

Your own health checks and webhooks are scripts too: give them a user agent of your own, or set
`catch_http_clients=False`.

## Options

```python
GateMiddleware(
    app,
    key="sbk_…",  # default: STILEBASE_KEY
    ip_header="cf-connecting-ip",  # the visitor's address from a header your proxy or CDN sets
    catch_http_clients=False,  # let curl & co. through
    catch_hidden_agents=False,  # only ask agents that name themselves
    check_browsers=False,  # no browser checks
    on_error=lambda error: log.warning(error),  # default: a warning on the "stilebase_sdk.gate" logger
)
```

**The visitor's address:** the connection's (ASGI) or `REMOTE_ADDR` (WSGI, Django); behind a proxy, make those the
visitor's (`--proxy-headers`, `ProxyFix`), or name the header your proxy sets with `ip_header` (from a list such as
`X-Forwarded-For`, its last entry). Never a header the visitor can set. The same settings tell the gate that
visitors came over https behind a proxy that ends TLS (Django: `SECURE_PROXY_SSL_HEADER`): the browser checks run only
for https.

## Undo

Remove the middleware, or press **Pause** on the gateway page.

## Licence

MIT
