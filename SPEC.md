# How a gate behaves

The spec for `@stilebase/sdk`, `stilebase-sdk`, and a gate in any other language. A gate is a thin connector to
Stilebase: it does what Stilebase's Cloudflare Worker does, from inside a site (middleware, an edge function). It lets
people through, asks AI agents to pay with [x402](https://x402.org) v2, and has Stilebase settle the payments. Check an
implementation against [`conformance/`](conformance).

`<stilebase>` below is the API address, `https://stilebase.com` unless the owner sets `STILEBASE_API_URL`.

## The one rule: never break the site

When Stilebase can't be reached, answers with an error, or has no live gateway for the site, **everyone gets the site
as usual**. Requests never wait on Stilebase longer than the timeouts below. Two exceptions, each because the
alternative lets agents read without paying:

- **A failed settings fetch** keeps the last good settings (a 401 after a rotated key, a 5xx, a timeout). Only a 404
  drops them (section 3).
- **A settlement without an answer** answers 503 with `Retry-After` (section 5, step 7). The page isn't served, since
  the payment may have settled.

## 1. The key

The gate reads `STILEBASE_KEY`, the **site key**: `sbk_` + the gateway's id (32 lowercase hex) + `_` + 64 lowercase
hex. Surrounding whitespace is ignored; anything else is not a site key. The signing secret is `sbsec_` + those 64 hex.
Without a valid key every request passes, and the gate warns. The key belongs on the site's server only: it never goes
to agents, browsers, logs or error messages.

## 2. Signing calls to Stilebase

`/v1/x402/settle`, `/v1/x402/visits` and `/v1/x402/sites/{id}/config` accept only calls signed with the secret:

| Header | Value |
| --- | --- |
| `Stilebase-Timestamp` | Unix seconds now. Stilebase refuses calls more than five minutes off |
| `Stilebase-Signature` | `v2=` + lowercase hex HMAC-SHA256 of the canonical string, keyed with the whole secret (`sbsec_…`) |

The canonical string is five lines joined with `\n`:

```
v2
<Stilebase-Timestamp>
<METHOD, e.g. POST>
<the API path, e.g. /v1/x402/settle>
<lowercase hex SHA-256 of the exact body bytes sent; of the empty string for a GET>
```

## 3. The settings

The Worker has its settings written in when it's downloaded; a gate fetches them. It sends
`GET <stilebase>/v1/x402/sites/<gateway id>/config`, signed over an empty body, with
`Stilebase-Client: <package>/<version> gate/<adapter>` (the gateway's page shows the site as **Connected** from it).
The answer ([`conformance/gate-config.example.json`](conformance/gate-config.example.json) is an example):

- `config_version`: a gate runs only on the version it knows (1).
- `gateway`: `id`, `domain`, `live`.
- `verification`: the file's `path` and `value` while the domain is unverified, otherwise null.
- `agents`, `http_clients`, `known_bots`: tokens matched case-insensitively anywhere in the user agent.
- `browser_checks`: the signal `weights`, `suspect_score`, `cloud_asns`.
- `pass`: the browser check's `cookie` name and `max_age_seconds`.
- `refresh_seconds`: how often to fetch them again (300 when missing; never more often than every 30 seconds).

On the first request, with a 3-second timeout; then in the background every `refresh_seconds`. With no settings yet
it tries again at most every 30 seconds and lets everyone through meanwhile. A **404** (the gateway was removed) drops
the settings; any other failure keeps the last good ones.

## 4. The visitor's address

The gate **never trusts a header the visitor can set**: a spoofed operator address would catch the wrong visitor or
skip a hidden agent. It uses the address the framework gives (Express's `req.ip`, an ASGI server's client address,
WSGI's `REMOTE_ADDR`; owners configure their own proxies there), `CF-Connecting-IP` on Cloudflare Workers, or the
header the owner names (`ipHeader`, for a header their own proxy or CDN sets). From a list such as `X-Forwarded-For` it
takes the **last** entry, the one the owner's proxy added. `::ffff:1.2.3.4` becomes `1.2.3.4`.

## 5. Each request

1. **The verification file.** While `verification` is set, a request for its `path` gets its `value` as plain text.
2. **Not live:** everyone passes.
3. **A request with a payment** (`PAYMENT-SIGNATURE`, else `X-PAYMENT`; an empty header is none) skips steps 4 and 5.
4. **Who is an agent:** a user agent matching `agents`; one matching `http_clients`, or none at all (unless the owner
   lets scripts through); a request carrying Web Bot Auth's `Signature-Agent`; or one from an address on the AI
   operators' lists (`GET <stilebase>/v1/x402/agent-ranges`, public, refreshed hourly). Anyone else is a person.
5. **Scripts posing as browsers.** A GET or HEAD over https whose user agent starts with `Mozilla/` and doesn't match
   `known_bots` is scored with `browser_checks`: `no_accept_language`, `no_sec_fetch`, `chrome_without_client_hints`,
   `accept_any`, and, where the edge reports them (Cloudflare's `request.cf`), `http1` and `cloud_asn`. At
   `suspect_score` or more it's a *suspect*; a suspect with a valid pass cookie passes, other people pass straight away.
   The **pass cookie** is `<cookie>=<unix time>.<first 32 hex of HMAC-SHA256(secret, "stilebase-pass\n<unix
   time>\n<User-Agent>")>`, at most `max_age_seconds` old and at most a minute ahead.
6. **The price.** `GET <stilebase>/v1/x402/requirements?host=<domain>&url=<page URL>&ua=<user agent>&ip=<address>`
   (public), plus `suspect=true` for a suspect. The page URL is `https://<domain>` + the request's path and query. The
   URL is cut to 2,048 characters and the user agent to 512, then both to fit 4,096 and 1,536 characters once
   form-encoded: a call Stilebase refused would serve the page unpaid. Answers are cached as `Cache-Control` says, at
   most 5 minutes.
   - **200**: answer **402** with the x402 `PaymentRequired` as the JSON body and, base64-encoded, in
     `PAYMENT-REQUIRED`. For a suspect that asked for HTML, the body is instead a small page whose script sets the pass
     cookie and reloads, so a real browser goes straight through.
   - **204**: a free pass, or a suspect while the owner's strict detection is off. The request passes.
   - Anything else, or no answer within 5 seconds: the request passes.
7. **A payment** goes to a signed `POST <stilebase>/v1/x402/settle` (up to 65 seconds):
   `{"host", "payment", "request": {"method", "path"}, "agent": {"ip", "user_agent", "headers"}}`, where
   `agent.headers` carries the Web Bot Auth headers that are present.
   - `"success": true`: the request goes to the site, with `PAYMENT-RESPONSE: <header>`.
   - `"success": false`: 402 again, with `error` set to the `errorReason`, and `PAYMENT-RESPONSE`.
   - No answer, or an answer without a boolean `success`: 503 with `Retry-After: 5`. **The outcome is unknown**, so a
     gate never tells the agent it wasn't charged; sending the same payment again can never charge twice.
8. **Visits.** Every 402, free pass and counted suspect is reported with a signed `POST <stilebase>/v1/x402/visits`
   (the settle body without `payment`, plus `free_pass` or `signals`), in the background.

Every answer the gate makes itself carries `Cache-Control: no-store`.

## 6. Options every gate offers

| Option | Default | What it does |
| --- | --- | --- |
| key | `STILEBASE_KEY` | The site key |
| API URL | `STILEBASE_API_URL`, else `https://stilebase.com` | Where Stilebase is |
| IP header / client IP | per platform (section 4) | Where the visitor's real address is |
| catch HTTP clients | on | Off lets scripts with default user agents through (e.g. your own health checks) |
| catch hidden agents | on | Off skips the address lists and `Signature-Agent` |
| check browsers | on | Off skips the browser checks |
| on error | warn | Where the gate reports what went wrong (it never throws into the app) |
