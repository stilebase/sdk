# How a gate behaves

The spec for `@stilebase/sdk`, `stilebase-sdk`, Stilebase's Cloudflare Worker and a gate in any other language. Check
an implementation against [`conformance/`](conformance).

A gate has **no logic of its own**. It forwards each request to Stilebase, does what the answer says, and lets the
request through when anything goes wrong. Who is an AI agent, the browser checks, prices and payments are all decided
by Stilebase, so they change without a new release, and every language behaves the same.

`<stilebase>` below is the API address, `https://stilebase.com` unless the owner sets `STILEBASE_API_URL`.

## The one rule: never break the site

When Stilebase can't be reached, answers with an error, answers too late or answers something the gate doesn't
understand, **the request goes to the site as usual**. A gate never throws into the app.

One exception, because the alternative might tell an agent it wasn't charged when it was: a request **carrying a
payment** (`PAYMENT-SIGNATURE`, or the v1 `X-PAYMENT`, not empty) that gets no usable answer is answered `503` with
`Retry-After: 5`, and the site's answer to it (if the gate holds one) is not sent.

## 1. The key

The gate reads `STILEBASE_KEY`, the **site key**: `sbk_` + the gateway's id (32 lowercase hex) + `_` + 64 lowercase
hex. Surrounding whitespace is ignored; anything else is not a site key. The signing secret is `sbsec_` + those 64 hex.
Without a valid key every request passes, and the gate warns once. The key belongs on the site's server only: it never
goes to visitors, logs or error messages.

`STILEBASE_DISABLED=1` (or the option `disabled`): every request passes and the gate calls nothing.

## 2. Signing calls to Stilebase

Every call is signed with the secret:

| Header | Value |
| --- | --- |
| `Stilebase-Timestamp` | Unix seconds now. Stilebase refuses calls more than five minutes off |
| `Stilebase-Signature` | `v2=` + lowercase hex HMAC-SHA256 of the canonical string, keyed with the whole secret (`sbsec_…`) |
| `Stilebase-Client` | `<package>/<version> gate/<adapter>`, e.g. `@stilebase/sdk/0.2.0 gate/express` |

The canonical string is five lines joined with `\n`:

```
v2
<Stilebase-Timestamp>
<METHOD, e.g. POST>
<the API path, e.g. /v1/gate/sites/<id>/decide>
<lowercase hex SHA-256 of the exact body bytes sent; of the empty string for a GET>
```

A gate keeps its connection to Stilebase open and reuses it.

## 3. The settings

`GET <stilebase>/v1/gate/sites/<gateway id>/settings`, signed over an empty body
([`conformance/settings.example.json`](conformance/settings.example.json) is an example):

| Field | What the gate does with it |
| --- | --- |
| `settings_version` | Runs only on a version it knows (1); any other: everyone passes |
| `enabled` | `false`: everyone passes, nothing is asked (Stilebase's kill switch) |
| `refresh_seconds` | Fetches the settings again this often (300 when missing; never more often than every 30 seconds) |
| `timeout_ms` | The whole budget for a decision, connecting included (300) |
| `settle_timeout_ms` | The budget for a call about a payment (65000) |
| `breaker` | `failures` in a row open it; it stays open `open_seconds`, then one request tries again |
| `remember_by` | The fields of the remember key, in order (section 6) |
| `cache_max_entries` | The most remembered answers kept; the oldest go first |
| `strip_headers` | Never sent to Stilebase (`cookie`, `authorization`, `proxy-authorization`) |
| `forward_cookies` | The only cookies sent, by name |
| `skip_paths` | `prefixes` and `extensions` passed without asking (section 5) |
| `settle_after.max_bytes` | The largest paid answer the gate holds (section 7) |

**Reading them** (conformance `settings`): `settings_version` must be the integer 1 (not `true`) and `enabled` a
boolean, or everyone passes.
Any other field that's missing, of the wrong type, or (for a number) not a positive integer takes its default:
`refresh_seconds` 300, `timeout_ms` 300, `settle_timeout_ms` 65000, `breaker` 5 failures / 30 seconds,
`cache_max_entries` 10000, `settle_after.max_bytes` 10485760, `remember_by` `["client_ip", "user-agent", "path"]` (an
empty list takes it too: a key of nothing would let one pass serve everyone), and
empty lists for `strip_headers`, `forward_cookies` and `skip_paths`. `cookie`, `authorization` and
`proxy-authorization` are never sent, whatever `strip_headers` says.

The settings are fetched on the first request (waiting at most 3 seconds in all, however slowly they arrive; meanwhile, and until there are settings,
everyone passes), then in the background every `refresh_seconds`. A failed fetch keeps the last good settings (a 401
after a rotated key, a 5xx, a timeout, a body that isn't a JSON object, `null` included); a **404** (the gateway was removed) drops
them, and everyone passes until a fetch succeeds again.

## 4. The visitor's address

The gate **never trusts a header the visitor can set**: a spoofed address would make an agent look like someone else.
It uses the address the framework gives (Express's `req.ip`, an ASGI server's client address, WSGI's `REMOTE_ADDR`;
owners configure their own proxies there), `CF-Connecting-IP` on Cloudflare Workers, or the header the owner names
(`ipHeader`, for a header their own proxy or CDN sets). From a list such as `X-Forwarded-For` it takes the **last**
entry, the one the owner's proxy added. Surrounding space is trimmed and `::ffff:1.2.3.4` becomes `1.2.3.4`. No usable
address: `null`.

## 5. Skipped paths

A request passes without asking when its path matches `skip_paths`. The path is matched **as the site's router will
read it**, so a paid page can't be reached by dressing it up as a file:

1. Take the path without the query (`?…`) and fragment (`#…`), exactly as received: **no decoding**.
2. If it contains `%` (any percent-encoding), `;` (matrix parameters, which some servers strip before routing), `\`,
   `..` or a control character, **don't skip**: a router may read it as another page.
3. Lowercase it. It's skipped when it starts with one of `prefixes`, or its last segment ends with one of
   `extensions` and is longer than the extension (`/.css` isn't skipped).

Owners are told that agents read skipped paths without paying, and can remove an extension a route of theirs
answers with content (a framework that renders `/articles/1.js` as a page, say).

## 6. Each request

1. **Disabled, no key, no settings, `enabled: false`, or the breaker open:** pass.
2. **Skipped path** (section 5), for a request without a payment: pass.
3. **Remembered:** the remember key is the lowercase hex SHA-256 of the `remember_by` fields joined with `\n`
   (`client_ip`: the address or empty; `user-agent`: the header or empty; `path`: the path with the query). A request
   **without a payment** whose key has an unexpired remembered `pass`: pass.
4. **Ask Stilebase:** `POST <stilebase>/v1/gate/sites/<gateway id>/decide`, signed:

   ```json
   {
     "request": {"method": "GET", "scheme": "https", "path": "/products/1?x=2"},
     "client_ip": "203.0.113.7",
     "headers": [["user-agent", "…"], ["accept", "…"]],
     "cookies": {"stilebase_pass": "…"},
     "edge": {"asn": 16509, "http_protocol": "HTTP/2"}
   }
   ```

   - `scheme`: how the visitor reached the site. Behind a TLS-terminating proxy that's `https`, whatever the app sees
     (the option `assumeHttps`, on by default).
   - `path`: the path with the query, as the framework gives it, with a leading `/` added if it has none; **never the
     host**.
   - `headers`: every header but `strip_headers`, names lowercased, in order, values as received; at most 100.
   - `cookies`: only the `forward_cookies` found in the `Cookie` header (split at `;`, names trimmed).
   - `edge`: only on a platform that reports them (Cloudflare's `request.cf.asn`, `request.cf.httpProtocol`), with only
     the fields known; otherwise left out.
   - `credentials`: `true` when the request carried a `Cookie` or `Authorization` header (which are never sent), so
     Stilebase never asks to capture a signed-in visitor's page (section 9).
   - **Never the request body.**

   The budget is `timeout_ms`, or `settle_timeout_ms` for a request with a payment. Requests with the same remember
   key and no payment that arrive while one is being asked **wait for that answer** instead of asking again (within
   their own budget), and do what it says.
5. **Do what the answer says** (an HTTP 200 with a JSON object):

   | `action` | The gate |
   | --- | --- |
   | `pass` | Lets the request through. With `remember: {"seconds": n}`, stores `pass` under its remember key for `n` seconds (never for a request with a payment) |
   | `pass` + `response_headers` | Lets it through and sets these headers on the site's answer |
   | `pass` + `settle_after` | Holds the site's answer and settles (section 7) |
   | `respond` | Answers with exactly `status`, `headers` and `body` (a UTF-8 string); the site isn't called |
   | `unknown_payment` | Answers `503` with `Retry-After: <retry_after>` |

   Any `pass` may also carry `capture` (section 9).

   **Reading an answer** (conformance `answers`): `respond` needs `status` an integer from 200 to 599; `headers`
   missing or `null` is none, otherwise an object of strings; `body` missing is `""`, otherwise a string.
   `settle_after.max_bytes` must be a positive integer, and a `settle_after` that isn't an object makes the answer
   unusable. `response_headers` must be an object of strings when present (it applies to a `settle_after` too).
   **Headers from Stilebase** (`headers`, `response_headers`) are kept only when every server accepts them: the name
   an HTTP token (all of it), the value printable ASCII and tabs only (`\t`, `0x20`–`0x7e`), without leading or
   trailing spaces or tabs. Any other header, and always `connection`, `keep-alive`, `proxy-connection`,
   `transfer-encoding`, `upgrade`, `te`, `trailer` and `content-length` (the adapter sets the real length), **is left
   out**: the rest of the answer stands. `remember.seconds` that isn't a positive integer is
   ignored, as is a malformed `capture` (its `token` a string, its
   `max_bytes` a positive integer). `retry_after` that isn't an integer from 1 to 3600 is 5.

   Anything else (an unknown `action`, a field of the wrong type, an error status, a body that isn't JSON, no answer
   within the budget): the request passes, **except** a request with a payment, which gets `503` with `Retry-After: 5`.
   Fields a gate doesn't know are ignored.
   A decision shared while in flight that's been waiting longer than its budget is forgotten (the runtime may have
   cancelled it), and the next request asks again. A request that waited on another's decision does what it says but
   never remembers it and never captures: only the request that asked does.
6. **The breaker:** a decision that failed (no answer, an error status, a malformed answer) counts one failure; any
   usable answer resets the count. A failure is Stilebase's (no answer, an error status, a malformed answer), never the
   gate's own (waiting for a free connection in its pool, a cancelled request): those count nothing. Times are
   measured on a clock that never goes back (a monotonic clock), not the time of day. At `breaker.failures` in a row the breaker **opens** for `breaker.open_seconds`:
   everyone passes, requests with a payment get `503` with `Retry-After: 5`. Then it's **half open**: one request is
   asked (the others pass as while open); a usable answer closes it, a failure opens it again. A probe that hasn't
   finished within its budget (plus a second) is forgotten, and the next request probes. Only Stilebase's failures
   count: a request the server or runtime cancels (the visitor left) counts nothing, for itself or for requests
   waiting on its decision.

Every answer the gate makes itself (`respond`, the 503s, the 502) carries `Cache-Control: no-store`.

## 7. A payment: settled after the site answers

On `pass` + `settle_after`, the agent is charged only for a page the site delivered:

1. Let the request through to the site, and **hold its answer**: status, headers and body, sending nothing to the
   agent yet.
2. A body larger than `settle_after.max_bytes` (the answer's, capped by the settings') can't be held: the gate stops
   reading it, answers `502` and doesn't settle (the agent isn't charged). Nor can a protocol switch (`101`, a
   WebSocket): `502`, not settled.
   A server feature that sends a body around the gate (ASGI's `http.response.pathsend` or `zerocopysend`) is turned off
   for a held request, so the gate sees the whole body.
3. `POST <stilebase>/v1/gate/sites/<gateway id>/settle`, signed: the same body as `decide` (the payment header
   included) plus `"delivery": {"status": <the site's status>, "size": <the body's bytes>, "sha256": "<lowercase hex>"}`.
   The budget is `settle_timeout_ms`.
4. The answer:
   - `pass` (with `response_headers`, e.g. `PAYMENT-RESPONSE`): send the held answer (section 10). A `settle_after`
     in a settle answer is ignored.
   - `respond`: send that instead; the held answer is dropped.
   - `unknown_payment`, anything else, or no answer: `503` with `Retry-After`; the held answer is dropped.

## 8. Options every gate offers

| Option | Default | What it does |
| --- | --- | --- |
| key | `STILEBASE_KEY` | The site key |
| API URL | `STILEBASE_API_URL`, else `https://stilebase.com` | Where Stilebase is |
| disabled | `STILEBASE_DISABLED` | Every request passes, nothing is asked |
| IP header / client IP | per platform (section 4) | Where the visitor's real address is |
| assume https | on | The scheme sent is `https` whatever the app sees (behind a TLS-terminating proxy) |
| on error | warn | Where the gate reports what went wrong (it never throws into the app) |

## 9. Capture (AgentView)

A `pass` may carry `capture: {"token": "…", "max_bytes": n}`: Stilebase wants a copy of the site's answer to this
request (AgentView: how the site answers people and AI agents). The gate:

1. Serves the request exactly as it would without `capture`: the answer to the visitor is never delayed, changed or
   cut short.
2. Copies the answer as it goes out: status, headers **without `Set-Cookie`** (cookies the site sets are never sent to
   Stilebase), and the first `max_bytes` bytes of the body (as sent, still
   compressed if the site compressed it), plus the body's total size and whether the copy was cut (`truncated`).
3. Sends the copy in the background: `POST <stilebase>/v1/gate/sites/<gateway id>/captures`, signed:
   `{"token", "status", "headers": [[name, value]…], "body": "<base64>", "size", "truncated", "ttfb_ms"}`, at most
   one at a time per gate and at most 20 waiting; the rest are dropped. A failure is dropped too: never retried,
   never in the way.

A gate that doesn't support `capture` ignores it (unknown fields are ignored); Stilebase then sees no copies.

## 10. The answers a gate sends (conformance `answers_sent`)

Every adapter builds what it sends the same way:

- **Headers added to the site's answer** (`response_headers` from decide, then from settle): each **replaces** a header
  of the same name (case-insensitive), never adds a second one; settle's win over decide's, and both win over any the
  site sets, whenever it sets them (a same-name header the site gives as the headers go out is dropped).
- **No body** with `204`, `205` or `304`, whoever answered (the site, held; or Stilebase's `respond`), and no
  `Content-Length` added to those.
- **The gate's own answers**, each with `Content-Type: application/json` and `Cache-Control: no-store`:
  - a payment whose outcome isn't known: `503`, `Retry-After: <n>`, body `{"error":"payment_outcome_unknown"}`;
  - a paid answer that can't be held: `502`, body `{"error":"answer_not_payable"}`.
- **`respond`**: Stilebase's status, headers and body, plus `Cache-Control: no-store` (replacing any); no other header
  is added (no default `Content-Type`).
- **The gate can't send its own answer** anyway (the server refuses it): an error, as the server reports it, never the
  page: a refused 402 is not a reason to serve the paid page.
