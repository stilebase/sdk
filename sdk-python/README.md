# stilebase-sdk

Stilebase for Python. Its first module is **the gate** (`stilebase_sdk.gate`): AI agents pay per request to read your
site, and people pass straight through. Each module is its own subpackage.

The gate answers AI agents (GPTBot, ClaudeBot, PerplexityBot…) with `402 Payment Required` in the open
[x402](https://x402.org) format. An agent that pays (USDC on Base) gets the page. [Stilebase](https://stilebase.com)
supplies the price, checks the payment and settles it, and you see every payment and agent on your dashboard.

People, search engines and link previews see no change. If Stilebase can't be reached, everyone gets your site as
usual: the gate never breaks it.

## Install

```bash
pip install stilebase-sdk
```

Set one environment variable: the **site key** from your gateway page in Stilebase (**Reveal secret**). Keep it on
your server only.

```bash
STILEBASE_KEY=sbk_…
```

Python 3.10 or later. The only dependency is `httpx`.

### FastAPI, Starlette (any ASGI app)

```python
from stilebase_sdk.gate.asgi import GateMiddleware

app.add_middleware(GateMiddleware)  # behind a proxy, run uvicorn with --proxy-headers
```

### Django

```python
# settings.py
MIDDLEWARE = [
    "stilebase_sdk.gate.django.GateMiddleware",  # first, before anything that renders pages
    # …
]
```

### Flask (any WSGI app)

```python
from stilebase_sdk.gate.wsgi import GateMiddleware

app.wsgi_app = GateMiddleware(app.wsgi_app)  # behind a proxy, wrap it in Werkzeug's ProxyFix first
```

### Anything else

```python
from stilebase_sdk.gate import GateRequest, Respond, SyncGate  # or AsyncGate, with `await gate.check(...)`

gate = SyncGate()
decision = gate.check(GateRequest(method, url, headers, ip=client_ip))  # headers: lowercased names
if isinstance(decision, Respond):
    ...  # send decision.status, decision.headers, decision.body instead
elif decision.payment_response:
    ...  # serve the request, with the header PAYMENT-RESPONSE: decision.payment_response
```

## Check it

On your gateway page, press **Test now**. Stilebase visits your site as its test agent and checks that it gets your
402 with the right price, and that a forged payment is refused. By hand:

```bash
curl -i -A GPTBot https://your-site.example/        # 402 with a PAYMENT-REQUIRED header
curl -i -A "Mozilla/5.0" https://your-site.example/  # your page, as before
```

When the gate first runs it fetches its settings from Stilebase, and the gateway page then shows it as **Connected**.

## What it does

- **AI agents** named by their user agent get the 402. Agents you gave a free pass on the gateway page read for free,
  but only from their operator's official addresses.
- **Payments.** A request with a payment (`PAYMENT-SIGNATURE`, or x402 v1's `X-PAYMENT`) is settled through Stilebase.
  Once settled, your page is served with `PAYMENT-RESPONSE`. A refused payment gets the 402 again, with the reason.
  If Stilebase doesn't confirm the settlement (it can't be reached, or its answer is lost), the agent gets `503` and
  the page isn't served. The payment may have settled anyway, so the gate never says it wasn't charged; sending the
  same payment again can't charge twice.
- **Agents hiding behind a browser's user agent** are asked too: requests from the addresses AI operators publish for
  their agents, or signed with Web Bot Auth's `Signature-Agent`.
- **Scripts** with their default user agent (curl, python-requests, Go-http-client…) are asked too, since no person
  browses with one.
- **Browser-looking requests that don't act like a browser** are counted on your Statistics page. With **Strict
  detection** on, they get the 402 as a page that lets real browsers straight through.
- **Domain verification.** Until you prove your domain, the gate serves `/.well-known/stilebase-verify.txt`, so you
  can verify without touching DNS.
- **Your own health checks** are scripts too (curl, wget, urllib): keep them free with `free=["/healthz"]`, a user
  agent of your own on the check, or `catch_http_clients=False`. An instance's first requests, free ones too, wait
  for the gate's settings (at most a few seconds, only while Stilebase is slow): give readiness probes a timeout
  above 4 seconds.
- **Free paths are exact and literal.** `free=["/healthz"]` keeps `GET /healthz` (and HEAD) free and nothing else:
  not `/healthz/`, `/HEALTHZ`, a POST, or any path that only resolves to it. Under WSGI and Django the server decodes
  the path before the app sees it, so `/%68ealthz` arrives as `/healthz` there, and your routes read it the same way. Use letters, digits,
  `- . _ ~` and `/`; full paths from the site's root, even for a gate mounted under a path. Every other path is gated. (There's no option to gate only some sections: each
  way of predicting how routers and file servers read a path left some spelling of a gated page free.)

The agent lists and checks come from Stilebase. The gate refreshes them every five minutes, so new agents are caught
without a redeploy. Price changes and free passes on the gateway page apply within five minutes too.

## Options

Every adapter takes the same keywords. In Django, put them in `STILEBASE_GATE = {...}` in settings.

```python
GateMiddleware(
    app,
    key="sbk_…",  # default: STILEBASE_KEY
    mode="test",  # only Stilebase's test agent is asked to pay: try it with nobody affected
    free=["/healthz", "/robots.txt"],  # exact paths that stay free for everyone; every other path is gated
    ip_header="cf-connecting-ip",  # the visitor's address from a header your proxy sets
    catch_http_clients=False,  # let curl & co. through (e.g. your own monitoring)
    catch_hidden_agents=False,  # only ask agents that name themselves
    check_browsers=False,  # no browser checks
    assume_https=False,  # the site is really served over plain http (default: visitors come over HTTPS, even when a
    #                      proxy in front ends TLS and the app sees http)
    on_error=lambda error: log.warning(error),  # default: a warning on the "stilebase_sdk.gate" logger, once in a while
)
```

**The visitor's address** is used to recognise agents and to verify free passes:

- ASGI: the connection's address.
- WSGI and Django: `REMOTE_ADDR`.

Behind a proxy, make those the visitor's (`--proxy-headers`, `ProxyFix`), or name the header your proxy sets with
`ip_header`. Trust only your own proxies: uvicorn's `--forwarded-allow-ips` should list their addresses (its default,
`127.0.0.1`, suits a proxy on the same machine), never `*`, and `ProxyFix(x_for=1)` should count your proxies. With
every hop trusted, the address is the first `X-Forwarded-For` entry, which the visitor writes, and a spoofed
operator address would get a free pass. For a list such as `X-Forwarded-For` the gate takes the last entry, the one your proxy added. Never use a
header the visitor can set themselves. Under WSGI and Django, `ip_header` is read from the environ, where
`CF-Connecting-IP` and a visitor's `CF_Connecting_IP` are the same key: use a server that drops headers with
underscores (gunicorn 22 or later, Werkzeug, waitress, mod_wsgi), or have your proxy strip them.

## Undo

Remove the middleware. Your site is exactly as before. Or press **Pause** on the gateway page: the gate then lets
everyone through.

## Licence

MIT
