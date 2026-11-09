# How a gate behaves

The spec for `@stilebase/sdk`, `stilebase-sdk`, and a gate in any other language. A gate sits in front of a site
(middleware, an edge function, a plugin). It lets people through, asks AI agents to pay with
[x402](https://x402.org) v2, and has Stilebase settle the payments. Check an implementation against
[`conformance/`](conformance).

`<stilebase>` below is the API address, `https://stilebase.com` unless the owner sets `STILEBASE_API_URL`.

## The one rule: never break the site

When Stilebase can't be reached, answers with an error, or has no live gateway for the site, **everyone gets the site
as usual**. A gate keeps working on the last settings it fetched (section 3) and on the price lookups it has cached
(at most 5 minutes); once those run out during an outage, agents pass too. Requests never wait on Stilebase for longer than the timeouts below. A person's request waits on it only
in three cases: the gate's very first request waits for the settings (at most 3 seconds); the first request with an
address waits for the operators' address lists (at most 5 seconds) while hidden agents are caught; and a browser that
looks like a script (a *suspect*, section 5, step 5) waits on the price lookup (at most 5 seconds) unless it carries a valid
pass cookie or that answer is cached.

Three exceptions to "everyone gets the site", each because the alternative lets agents read without paying:

- **A refused price lookup** (400, 413, 414, 422, 429, 431) is answered with 402 at the last price the gate saw for any
  page, with this page's URL. Only when it has none yet does the request pass. A visitor can try to provoke these
  (many unique URLs, a request built to be refused), so passing would let agents read free.
- **A failed settings fetch** keeps the last good settings, whatever the failure (a 401 after a rotated key, a 5xx, a
  timeout, an unknown `config_version`). Only a 404 drops them (section 3).
- **A settlement without an answer** answers 503 with `Retry-After` (section 5, step 8). The page isn't served, since the
  payment may not have settled.

## 1. The key

The gate reads `STILEBASE_KEY`, the **site key**: `sbk_` + the gateway's id (32 lowercase hex) + `_` + 64 lowercase
hex. Surrounding whitespace is ignored; anything else (uppercase, a dashed id, a missing part) is not a site key. The signing secret is `sbsec_` + those 64 hex. Without a valid key every request
passes, and the gate warns once. The key belongs on the site's server only: it never goes to agents or browsers.

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

Every hex value is lowercase: Stilebase refuses an uppercase signature or body hash.

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

- `config_version`: a gate runs only on the version it knows (1). Any other is a failed fetch: the gate warns and keeps
  the last good settings, and with none yet everyone passes.
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
- `refresh_seconds`: how often to fetch them again. A gate uses 300 when it's missing, not a number or not positive,
  and never less than 30.

**When.** On the first request, waiting at most 3 seconds. Then in the background every `refresh_seconds`, keeping
the last good settings on failure. With no settings yet it retries at most every 30 seconds and lets everyone through
meanwhile. A **404** (the gateway was removed) drops the settings, and everyone passes. A **401** (the key was
rotated, or this server's clock is more than 5 minutes off) keeps the last settings: agents are still asked to pay,
and only their payments fail until the key or the clock is fixed. Dropping them would let every agent read free.

## 4. The visitor's address

The gate normalizes it (`::ffff:1.2.3.4` becomes `1.2.3.4`) and **never trusts a header the visitor can set**. A
spoofed operator address with an agent's user agent would otherwise get a free pass or that agent's price. By
default it uses:

- the address the framework gives, where the framework works it out from proxies the owner configured (Express's
  `req.ip`, an ASGI server's client address, WSGI's `REMOTE_ADDR`). Owners configure their actual proxies, never
  "trust every hop": then the first `X-Forwarded-For` entry wins, and the visitor wrote it. Where the gate can tell
  that every hop is trusted (Express's `trust proxy: true`), it takes the last entry instead and warns;
- on platforms that set a header themselves and overwrite the visitor's: `CF-Connecting-IP` on Cloudflare Workers,
  `x-real-ip` on Vercel;
- elsewhere (Bun, Deno, self-hosted Next.js, Hono on Node) no address, unless the owner names the header their proxy
  sets. Without an address, free passes can't verify, per-agent prices don't apply, and hidden agents aren't caught by
  address, so the packages' READMEs tell owners to name one.

From a header holding a list, such as `X-Forwarded-For`, it takes the **last** entry, the one the owner's proxy added:
the entries before it are the visitor's to write. An address over 45 characters counts as none.

## 5. Each request

1. **The verification file.** While `verification` is set, the gate answers requests for `path` with `value` as plain
   text.
2. **Not live:** everyone passes.
3. **A request with a payment** (`PAYMENT-SIGNATURE`, x402 v2, else `X-PAYMENT`, v1; an empty header is no payment)
   skips steps 4 and 5: whoever
   sends a payment is treated as an agent. It still goes through the price lookup (step 6), which names the requirement
   the payment must meet, and then to settlement (step 8). When the lookup brings no requirement (204, or an answer that
   lets the request pass), the request passes without settling.
4. **Who is an agent.** A request is asked to pay when:
   - its user agent matches `agents` (in test mode, only `test_mode_agents`);
   - it matches `http_clients`, or has no user agent, unless the owner lets scripts through;
   - it comes from an address on the AI operators' published lists: `GET <stilebase>/v1/x402/agent-ranges` (public),
     cached for an hour and checked locally; or
   - it carries Web Bot Auth's `Signature-Agent` header.

   Scripts pay from anywhere, the machine itself included: a proxy on the same host makes every visitor look local.
   Owners exempt their own health checks with `free`, a user agent of their own, or by letting scripts through.
5. **Scripts posing as browsers.** A GET or HEAD whose user agent starts with `Mozilla/` (case-sensitive) and doesn't
   match `known_bots` is scored on what a browser would send, with `browser_checks.weights`:

   | Signal | Weight in the example |
   | --- | --- |
   | `no_accept_language`: no `Accept-Language` | 2 |
   | `no_sec_fetch`: none of `Sec-Fetch-Mode`, `Sec-Fetch-Site`, `Sec-Fetch-Dest` | 2 |
   | `chrome_without_client_hints`: a `Chrome/` user agent without `Sec-CH-UA` | 2 |
   | `accept_any`: `Accept` missing or `*/*` | 1 |
   | `http1`: the visitor's connection was HTTP/1.x | 1 |
   | `cloud_asn`: the visitor's network is on `cloud_asns` | 1 |

   `http1` and `cloud_asn` count only where the edge that met the visitor reports them (Cloudflare's `request.cf`),
   never from the app's own connection: behind a proxy, that's the proxy's hop, and every person would score.

   At `suspect_score` or more it's a *suspect*. The checks assume the visitor came over HTTPS, whatever scheme the app
   sees (behind a proxy that ends TLS, the app sees plain http); owners of a site really served over http turn that
   off. A suspect with a valid pass cookie passes (below). Other suspects go to the price lookup with `suspect=true`.
6. **The price.** `GET <stilebase>/v1/x402/requirements?host=<domain>&url=<page URL>&ua=<user agent>&ip=<address>`
   (public). The URL is cut to 2,048 characters and the user agent to 512, then both are cut to fit 4,096 and 1,536
   characters once form-encoded as `application/x-www-form-urlencoded` (the WHATWG URL standard's serializer): a call
   Stilebase refused would serve the page unpaid. Answers:
   - **200** with a JSON object: the x402 `PaymentRequired`. Answer **402** with it as the JSON body and, base64-encoded, in
     `PAYMENT-REQUIRED`. Report the visit (below).
   - **204**: a free pass (the owner lets this agent read free, and its address verified), or a suspect while the
     owner's strict detection is off. The request passes; report it with `"free_pass": true`, or with `signals` for a
     suspect.
   - **200 for a suspect** (strict detection on): report it with `signals`, then answer 402. When the request asked for
     HTML (`Accept` contains `text/html`), the body is a small page whose script sets the pass cookie and reloads, so
     a real browser goes straight through.
   - **400, 413, 414, 422, 429, 431**: 402 with the last price (the rule at the top).
   - Anything else (a 200 that isn't a JSON object included), or no answer within 5 seconds: the request passes.

   200 and 204 answers are cached for as long as their `Cache-Control` says, at most 5 minutes, at most 1,000 entries.
7. **The pass cookie.** `<cookie>=<unix time>.<first 32 hex of HMAC-SHA256(secret, "stilebase-pass\n<unix time>\n<User-Agent>")>`,
   for `max_age_seconds`, `Path=/; SameSite=Lax; Secure`. The gate checks it itself, with no call to Stilebase: the
   time at most `max_age_seconds` old and at most a minute ahead, the HMAC for this user agent, compared in constant
   time.
8. **A payment.** A request with `PAYMENT-SIGNATURE` (x402 v2) or `X-PAYMENT` (v1) goes to a signed
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
   `SettlementResponse` plus `header`, read only when it's a 200 with a JSON object whose `success` is a boolean:
   - `"success": true`: the request goes to the site, and the response gets `PAYMENT-RESPONSE: <header>`.

   `header` is used only when it can be a header: a base64 string (`A-Z a-z 0-9 + / = _ -`) of at most 8,192
   characters. Otherwise the gate leaves `PAYMENT-RESPONSE` out (and still serves a paid request, or answers the 402).
   - `"success": false`: answer 402 again, with `error` set to the `errorReason` and `PAYMENT-RESPONSE`.
   - No answer, or any other answer (Stilebase down, a timeout, a body that isn't a JSON object, a `success` that isn't
     a boolean): 503 with
     `Retry-After: 5`, and the page isn't served. **The outcome is unknown**: the payment may have settled after all
     (an answer lost on the way), so a gate never tells the agent it wasn't charged. Sending the same payment again
     can never charge twice: each authorization is spent once, on-chain.
9. **Visits** are reported with a signed `POST <stilebase>/v1/x402/visits`, the same body as settle without
   `payment` (plus `free_pass` or `signals`), in the background: a request never waits on it. A gate keeps a bounded
   number of reports in flight and drops the rest.

Every answer the gate makes itself (402, the browser check's page, 503, the verification file) carries
`Cache-Control: no-store`, so no cache in front of the site keeps it and serves it to people.

## 6. The page's name

The page is named to Stilebase and to agents by the gateway's domain over https, with the request's path and query:
the Host header is the visitor's to write, and behind a proxy it's an internal name. The request URL is built from
the Host header only when it's a host name (anything else becomes `localhost`), so a forged Host can't change the path
the gate reads. An absolute-form request target (`GET http://host/path`) keeps its path as written: it's cut as text,
never resolved by a URL parser. A URL the adapter can't parse is still gated.

**Free paths.** A gate covers every path. The owner may list exact paths that stay free for everyone (`free`, for
example `/healthz` and `/robots.txt`; full paths from the site's root). A request is free only when it's a GET or HEAD
and its URL is **literal**:

- `http` or `https`, a plain host name (letters, digits, `-`, `.`, an optional port), then `/`;
- a path of letters, digits, `-` `.` `_` `~` and `/` only, with no empty, `.` or `..` segment (a trailing slash is
  fine); a query may follow;

and that path is one of the free paths **character for character**. There's no decoding and no resolving: `/HEALTHZ`,
`/healthz/`, `/%68ealthz`, `//healthz`, `/x/../healthz` and a URL with a host like `x?` are all gated. Some router or
file server reads each unusual spelling differently, so only spellings every parser reads the same way can be free. A
free entry that isn't literal is reported once and never matches. There's no option to gate only some paths: every way
of predicting how routers and file servers read a path left some spelling of a gated page free.

## 7. Options every gate offers

| Option | Default | What it does |
| --- | --- | --- |
| key | `STILEBASE_KEY` | The site key |
| API URL | `STILEBASE_API_URL`, else `https://stilebase.com` | Where Stilebase is |
| mode | live | `test`: only `test_mode_agents` are asked to pay |
| free | none | Exact paths that stay free for everyone (section 6) |
| client IP / IP header | per platform (section 4) | Where the visitor's real address is: a header the owner's proxy sets, or a function (each package's README lists what its adapters take) |
| catch HTTP clients | on | Off lets scripts with default user agents through |
| catch hidden agents | on | Off skips the address lists and `Signature-Agent` |
| check browsers | on | Off skips the browser checks |
| assume HTTPS | on | Off for a site really served over plain http |
| on error | warn | Where the gate reports what went wrong (it never throws into the app) |
