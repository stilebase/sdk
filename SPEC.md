# How a gate behaves

The spec for `@stilebase/sdk`, `stilebase-sdk`, and a gate in any other language. A gate sits in front of a site
(middleware, an edge function, a plugin). It lets people through, asks AI agents to pay with
[x402](https://x402.org) v2, and has Stilebase settle the payments. Check an implementation against
[`conformance/`](conformance).

`<stilebase>` below is the API address, `https://stilebase.com` unless the owner sets `STILEBASE_API_URL`.

## The one rule: never break the site

When Stilebase can't be reached, answers with an error, or has no live gateway for the site, **everyone gets the site
as usual**. Requests never wait on Stilebase for longer than the timeouts below, and a person's request never waits on
it at all. Three exceptions, each because the alternative lets agents read without paying:

- **A refused price lookup** (400, 413, 414, 422, 431) is answered with 402 at the last price the gate had, for this
  page. Only when it has none yet does the request pass.
- **A 401 on the settings** keeps the last settings (rule 3 below).
- **A failed settlement** answers 503 with `Retry-After`. Nothing was charged.

## 1. The key

The gate reads `STILEBASE_KEY`, the **site key**: `sbk_` + the gateway's id (32 lowercase hex) + `_` + 64 hex.
Surrounding whitespace is ignored. The signing secret is `sbsec_` + those 64 hex. Without a valid key every request
passes, and the gate warns once. The key belongs on the site's server only: it never goes to agents or browsers.

## 2. Signing calls to Stilebase

`/v1/x402/settle`, `/v1/x402/visits` and `/v1/x402/sites/{id}/config` accept only calls signed with the secret:

| Header | Value |
| --- | --- |
| `Stilebase-Timestamp` | Unix seconds now. Stilebase refuses calls more than five minutes off |
| `Stilebase-Signature` | `v2=` + hex HMAC-SHA256 of the canonical string, keyed with the whole secret (`sbsec_…`) |

The canonical string is five lines joined with `\n`:

```
v2
<Stilebase-Timestamp>
<METHOD, e.g. POST>
<the API path, e.g. /v1/x402/settle>
<hex SHA-256 of the exact body bytes sent; of the empty string for a GET>
```

```python
import hashlib, hmac, json, time


def signed_call(secret: str, path: str, payload: dict) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    canonical = "\n".join(["v2", ts, "POST", path, hashlib.sha256(body).hexdigest()])
    mac = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return body, {"content-type": "application/json", "stilebase-timestamp": ts, "stilebase-signature": f"v2={mac}"}
```

## 3. The settings

The gate sends `GET <stilebase>/v1/x402/sites/<gateway id>/config`, signed over an empty body, with
`Stilebase-Client: <package>/<version> <module>/<adapter>` (for example `@stilebase/sdk/0.1.0 gate/express`). The
gateway's page shows the site as **Connected** from it. The answer
([`conformance/gate-config.example.json`](conformance/gate-config.example.json) is an example):

- `config_version`: a gate runs only on the version it knows (1). On any other it lets everyone through and warns.
- `gateway`: `id`, `domain`, `live`. When `live` is false, `reason` says why: `paused`, `account_switched_off`,
  `domain_not_verified`, `domain_claimed_by_another_gateway`.
- `verification`: the file's `path` and `value` while the domain is unverified, otherwise null.
- The lists, each made of tokens matched case-insensitively anywhere in the user agent:
  - `agents`: AI agents, including two checkers that must get the 402 like an agent (`StilebaseCheck`,
    `CoinbaseBazaarDiscovery`);
  - `test_mode_agents`: the only ones asked to pay in test mode;
  - `http_clients`: scripts' default user agents (curl, python-requests…);
  - `known_bots`: search engines, link previews, monitors and feed readers, never checked as browsers.
- `browser_checks`: the signal `weights`, `suspect_score`, `cloud_asns`.
- `pass`: the browser check's `cookie` name and `max_age_seconds`.
- `refresh_seconds`.

**When.** On the first request, waiting at most 3 seconds. Then in the background every `refresh_seconds`, keeping
the last good settings on failure. With no settings yet it retries at most every 30 seconds and lets everyone through
meanwhile. A **404** (the gateway was removed) drops the settings, and everyone passes. A **401** (the key was
rotated, or this server's clock is more than 5 minutes off) keeps the last settings: agents are still asked to pay,
and only their payments fail until the key or the clock is fixed. Dropping them would let every agent read free.

## 4. The visitor's address

The gate normalizes it (`::ffff:1.2.3.4` becomes `1.2.3.4`) and **never trusts a forwarded header** unless the owner
names one their proxy sets. A spoofed operator address with an agent's user agent would otherwise get a free pass or
that agent's price. From a list such as `X-Forwarded-For` it takes the **last** entry, the one the owner's proxy
added: the entries before it are the visitor's to write. An address over 45 characters counts as none.

## 5. Each request

1. **The verification file.** While `verification` is set, the gate answers `GET <path>` with `value` as plain text.
2. **Not live:** everyone passes.
3. **Who is an agent.** A request is asked to pay when:
   - its user agent matches `agents` (in test mode, only `test_mode_agents`);
   - it matches `http_clients`, or has no user agent, unless the owner lets scripts through;
   - it comes from an address on the AI operators' published lists: `GET <stilebase>/v1/x402/agent-ranges` (public),
     cached for an hour and checked locally; or
   - it carries Web Bot Auth's `Signature-Agent` header.

   Scripts pay from anywhere, the machine itself included: a proxy on the same host makes every visitor look local.
   Owners exempt their own health checks with `paths`, a user agent of their own, or by letting scripts through.
4. **Scripts posing as browsers.** A GET with a browser's user agent that doesn't match `known_bots` is scored on what
   a browser would send, with `browser_checks.weights`:

   | Signal | Weight in the example |
   | --- | --- |
   | `no_accept_language`: no `Accept-Language` | 2 |
   | `no_sec_fetch`: none of the `Sec-Fetch-*` headers | 2 |
   | `chrome_without_client_hints`: a `Chrome/` user agent without `Sec-CH-UA` | 2 |
   | `accept_any`: `Accept` missing or `*/*` | 1 |
   | `http1`: HTTP/1.x, where the runtime reports it | 1 |
   | `cloud_asn`: a network on `cloud_asns`, where the runtime reports it | 1 |

   At `suspect_score` or more it's a *suspect*. The checks assume the visitor came over HTTPS, whatever scheme the app
   sees (behind a proxy that ends TLS, the app sees plain http); owners of a site really served over http turn that
   off. A suspect with a valid pass cookie passes (below). Other suspects go to the price lookup with `suspect=true`.
5. **The price.** `GET <stilebase>/v1/x402/requirements?host=<domain>&url=<page URL>&ua=<user agent>&ip=<address>`
   (public). The URL is cut to 2,048 characters and the user agent to 512, then both are cut to fit 4,096 and 1,536
   characters once form-encoded: a call Stilebase refused would serve the page unpaid. Answers:
   - **200**: the x402 `PaymentRequired`. Answer **402** with it as the JSON body and, base64-encoded, in
     `PAYMENT-REQUIRED`. Report the visit (below).
   - **204**: a free pass (the owner lets this agent read free, and its address verified), or a suspect while the
     owner's strict detection is off. The request passes; report it with `"free_pass": true`, or with `signals` for a
     suspect.
   - **200 for a suspect** (strict detection on): report it with `signals`, then answer 402. When the request asked for
     HTML (`Accept` contains `text/html`), the body is a small page whose script sets the pass cookie and reloads, so
     a real browser goes straight through.
   - **400, 413, 414, 422, 431**: 402 with the last price (the rule at the top).
   - Anything else, or no answer within 5 seconds: the request passes.

   200 and 204 answers are cached for as long as their `Cache-Control` says, at most 5 minutes, at most 1,000 entries.
6. **The pass cookie.** `<cookie>=<unix time>.<first 32 hex of HMAC-SHA256(secret, "stilebase-pass\n<unix time>\n<User-Agent>")>`,
   for `max_age_seconds`, `Path=/; SameSite=Lax; Secure`. The gate checks it itself, with no call to Stilebase: the
   time at most `max_age_seconds` old and at most a minute ahead, the HMAC for this user agent, compared in constant
   time.
7. **A payment.** A request with `PAYMENT-SIGNATURE` (x402 v2) or `X-PAYMENT` (v1) goes to a signed
   `POST <stilebase>/v1/x402/settle`, waiting up to 65 seconds (longer than Stilebase takes at most: giving up first
   would answer 503 for a payment that then settles):

   ```json
   {
     "host": "<the gateway's domain>",
     "payment": "<the PAYMENT-SIGNATURE or X-PAYMENT header, as received>",
     "request": {"method": "GET", "path": "/listings?city=Haifa"},
     "agent": {"ip": "20.15.240.70", "user_agent": "...GPTBot/1.2...", "headers": {"signature": "...", "signature-input": "...", "signature-agent": "..."}}
   }
   ```

   `agent.headers` carries only the Web Bot Auth headers that are present. The answer is the x402
   `SettlementResponse` plus `header`:
   - `"success": true`: the request goes to the site, and the response gets `PAYMENT-RESPONSE: <header>`.
   - `"success": false`: answer 402 again, with `error` set to the `errorReason` and `PAYMENT-RESPONSE`.
   - Anything else: 503 with `Retry-After: 5`. Nothing was charged.
8. **Visits** are reported with a signed `POST <stilebase>/v1/x402/visits`, the same body as settle without
   `payment` (plus `free_pass` or `signals`), in the background: a request never waits on it. A gate keeps a bounded
   number of reports in flight and drops the rest.

## 6. The page's name

The page is named to Stilebase and to agents by the gateway's domain over https, with the request's path and query:
the Host header is the visitor's to write, and behind a proxy it's an internal name. The request URL is built from
the Host header only when it's a host name (anything else becomes `localhost`), so a forged Host can't move the path
out of reach of the `paths` option. A URL the adapter can't parse is still gated.

`paths` (prefixes, or a function) sees the path as routers and static file servers do: percent-decoded, lowercased,
dot segments resolved. So `/LISTINGS/2`, `/%6Cistings/2` and `/x/..%2Flistings/2` are all gated like `/listings/2`.

## 7. Options every gate offers

| Option | Default | What it does |
| --- | --- | --- |
| key | `STILEBASE_KEY` | The site key |
| API URL | `STILEBASE_API_URL`, else `https://stilebase.com` | Where Stilebase is |
| mode | live | `test`: only `test_mode_agents` are asked to pay |
| paths | everything | Gate only these prefixes, or the paths a function accepts |
| client IP / IP header | the connection's address | Where the visitor's real address is, behind a proxy |
| catch HTTP clients | on | Off lets scripts with default user agents through |
| catch hidden agents | on | Off skips the address lists and `Signature-Agent` |
| check browsers | on | Off skips the browser checks |
| assume HTTPS | on | Off for a site really served over plain http |
| on error | warn | Where the gate reports what went wrong (it never throws into the app) |
