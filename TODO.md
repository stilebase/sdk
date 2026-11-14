# TODO

What's known and not yet done, each with why it waits. Findings from code reviews that aren't blocking
(`.claude/rules/development-workflow.md`) go here.

## Both SDKs

- **Conformance cases for the newer rules:** the cut-to-fit of `url` and `ua` (WHATWG encoding, `~` and `*`), the
  refusals answered at the last price (429 included), `refresh_seconds` defaults, settle answers that aren't a JSON
  object or whose `success` isn't a boolean. *Waits:* each SDK has its own tests for them; shared cases come next.
- **A refused lookup for a suspect** (strict detection off) is answered at the last price, where a served suspect is
  the normal answer; and the last price lives in memory, so a fresh serverless instance has none. Put the gateway's
  base requirement in the signed settings, and serve suspects on a refusal. *Waits:* refusals no longer happen in
  normal operation (the lookup is cut to Stilebase's limits).
- **A failed first fetch of the address lists** leaves them empty for an hour. Retry every 30 seconds while they're
  empty after a failure, as with the settings. *Waits:* it needs Stilebase to fail exactly at a gate's start, and
  agents are still asked to pay by user agent.
- **Any 404 on the settings drops them**, a routing fault at Stilebase's edge included. Drop them only on the API's
  own "no such gateway" answer. *Waits:* the API answers 404 only for removed gateways.
- **The browser check's pass token is in the 402 page**, so a script that reads the page can lift it. A stronger
  challenge is a design change to strict detection. *Waits:* strict detection is off by default.
- **Planted pass cookies can still hide the real one.** Whoever can set a same-named cookie (a sibling subdomain with
  `Domain=`, or the site echoing user input into a cookie value with commas) can put more distinct times than the gate
  checks (8) after the real cookie; a browser keeps a re-set cookie's original place. The check page then asks for
  cookies instead of reloading forever, but that person stays on it until the planted cookies expire. Fix at the
  source: Stilebase's settings name the cookie `__Host-stilebase_pass` (browsers refuse that name from another
  subdomain; the check page already sets `Secure; Path=/` with no `Domain`), and the check page expires the old cookie
  before setting the new one. *Waits:* a Stilebase settings change; strict detection is off by default, and it needs
  control of a sibling subdomain or such an echo.
- **Stilebase should price unusual page URLs conservatively** (percent-encoding, a `%23`, dot segments): the gate
  names a page as it reads it, and some router may serve another page for the same request. A URL with a `#` is named
  by the part before it (Express, `new URL`), while a router reading it as a string (Hono's `getPath`) serves the
  whole thing. The SDKs keep only the rule they must apply on their own (such a URL is never free, decided without a
  call to Stilebase); pricing is Stilebase's. *Waits:* a server-side change; only a forged Host or a raw request
  target puts a `#` in a request URL.
- **A cold start's first requests wait for the settings, free health checks included** (at most about 3.5 seconds
  when Stilebase is slow or unreachable). Two cycles of letting free requests skip the wait each opened a hole (agents
  reading free after a failed background load, a free-listed verification file not served on a cold instance).
  *Waits:* only until an instance's first load ends, bounded; give readiness probes a timeout above 4 seconds.

- **Warn once when the gate has no visitor address** (the fetch adapter outside Cloudflare, without `ipHeader`). Free
  passes then never verify and hidden agents aren't caught by address, silently. *Waits:* the READMEs say to set it;
  agents are still asked to pay by user agent.
- **Python: the timeouts are per network step, not a total deadline.** httpx applies 3, 5 and 65 seconds to each of
  connect, write and read, so a slow or trickling answer can hold a request longer (the JS SDK's are total). Wrap each
  call in an overall deadline. *Waits:* the far end is Stilebase's own API, which answers or fails fast.

- **More frameworks:** Fastify, Koa and Hono-native adapters for `@stilebase/sdk` (Hono works today through
  `withGate(app.fetch)`); Django's async middleware for `stilebase-sdk`. *Waits:* the adapters that exist cover the
  common stacks.
- **More languages**, each on `conformance/` and [SPEC.md](SPEC.md): Go (`net/http`, Gin, Echo, Caddy and Traefik
  plugins), PHP (Laravel, Symfony), Ruby (Rack), Java/Kotlin, .NET, Rust. *Waits:* demand decides the order.
- **A refused price lookup reuses the last price**, which can be another agent's per-agent price; that payment then
  fails settlement and gets the 402 again. *Waits:* it only happens when a proxy refuses a lookup, which cutting the
  lookup to Stilebase's limits now avoids.
- **The price lookup cuts the user agent by encoded length, settle by characters.** A user agent full of non-ASCII
  text before the agent's name can make the two see different agents, so different prices. Cut both the same way.
  *Waits:* it needs a contrived user agent, and the agent only gets the 402 again.
- **Cutting a user agent to 512 characters could split an emoji** where a runtime decodes headers as UTF-8. *Waits:*
  the Fetch API, Node and Python decode headers as Latin-1, so it can't happen there. Cut by code points anyway.
- **The JS and Python SDKs name a `/a/../b` page differently** (JS resolves dot segments through `new URL`). Add a
  conformance case. *Waits:* only the page's name in statistics differs, not whether it's gated.
- **Gating only some sections.** 0.1.0 has no `paths` option: five review cycles each found a spelling of a gated
  page that some router or file server serves but the prefix match missed (doubled or encoded slashes, `..` after an
  empty segment, absolute-form targets, route parameters holding `%2F..`, Next.js locales and basePath, NTFS stream
  names). Bring it back only as its own design, for example integrated with the framework's router (Express routes,
  Next.js matchers) instead of matching paths in the gate. *Waits:* gating everything with exact free paths covers
  the common need (health checks, robots.txt).
- **Test mode is invisible on the gateway page.** A gate in `mode: "test"` passes Stilebase's **Test now**, and the
  page then says agents get a 402 while real ones read free. Report the mode in `Stilebase-Client`. *Waits:* it goes
  with the first release, together with Stilebase showing it.

## @stilebase/sdk

- **The smoke test's transitive dependencies resolve fresh** from the registry (its top-level versions are pinned).
  Install them from a committed lockfile. *Waits:* the smoke test runs in its own release job, which can't touch the
  published tarball (the publish job checks its hash).

- **Types under `moduleResolution: node`** (`"module": "commonjs"` projects): subpath types resolve only through
  `exports`. Add `typesVersions` for `gate`, `gate/express`, `gate/fetch`. *Waits:* NodeNext and bundler
  resolution, the defaults for new projects, work.
- **The fetch adapter finds `waitUntil` only on the third argument** (Workers' `ctx`). Vercel and Netlify Edge pass a
  `context` second, so background reports and refreshes may be cut off there. *Waits:* the settings are still fetched
  on the next request; only statistics can be lost.
- **The price lookup cuts the URL by encoded length, settle by characters**, as for the user agent (above). *Waits:*
  only the statistics' page name differs.
- **Identical concurrent price lookups aren't combined**, and a refused lookup's 402 names the uncut URL. *Waits:*
  performance and cosmetics only.
- **A first load another request started, and the runtime cancelled** (Workers): each request waits on it
  at most its own deadline (3.5 s settings, 5.5 s address lists) until it's replaced after 10 seconds, so for those 10
  seconds every request waits the full bound. Hand the first load to `waitUntil` as well, so it survives the
  disconnect. *Waits:* it needs a cold isolate whose first visitor disconnects mid-fetch.
- **A `clientIp` that throws, or a receipt header that can't be set, is swallowed silently.** Report it through the
  gate's warning, throttled. *Waits:* the gate keeps working (no address, or no receipt header).
- **Re-running a release job whose smoke test failed** may fail at the upload (an artifact name is unique per run);
  release again from a new run. *Waits:* to confirm on the first release.
- **Tests for a superseded load finishing late**, and for the address lists' bound. *Waits:* the code keeps the newer
  load (ownership checks); a test pins it.
- **A superseded load still marks the settings as tried, and a superseded failed address-list load still sets an
  empty list.** Only matters with a custom `fetch` that ignores its abort signal. Gate those writes on ownership too
  (a generation counter). *Waits:* the built-in fetch aborts loads well before they're superseded.
- **The default warning throttle's map is never pruned**, so error messages that vary grow it slowly. Cap it.
  *Waits:* default `onError` only, and slow.
- **Cleanup:** `express.ts` and `fetch.ts` each repeat the warn-once logic from core;
  `ExpressResponse.headersSent` is no longer read; settings and address lists share their single-flight code by copy.
  *Waits:* no behaviour changes.
- **Intervals and cache ages use the wall clock** (`Date.now`): a clock set back (NTP, a restored VM) stops refreshes
  and keeps cached prices and free passes for as long as it jumped. Use `performance.now()` for durations, wall time
  only for signatures. *Waits:* large backward jumps are rare and heal on their own; the injectable `now` the tests
  use would need splitting in two.
- **The agent and client name lists are lowercased on every request** rather than once per settings load. *Waits:*
  about 60 short strings per request from a person; no measurable cost found.
- **Each CommonJS entry carries its own copy of the core** (tsup splits shared code for ESM only), so mixing entries
  in CommonJS gives two `createGate`s. Nothing depends on their identity any more (the address check no longer uses
  `instanceof`). *Waits:* a few KB per entry; ESM, the main path, shares one copy.
- **Express: a hop count larger than the real number of proxies** (`trust proxy` 5 behind one) makes `req.ip` an
  entry the visitor wrote. The gate catches every setting that trusts every hop, but can't tell a wrong count from a
  right one: `req.ip` being the first of several entries is also what a correct count gives when the visitor sent no
  X-Forwarded-For. *Waits:* it's a misconfiguration Express documents; the README says to set the count to your
  proxies.
- **Express: ranges that trust every hop are found through Express's compiled setting** (`trust proxy fn`, Express 4
  and 5, not documented), and a `trust proxy` function is asked about made-up addresses on each request. A future
  Express without that setting would leave only `true` and functions caught. *Waits:* it's been stable since Express
  4; the tests run on the Express version installed, so a change shows up there.

- **A Next.js adapter.** 0.1.0 has none: nine review cycles found Next-specific ways past a middleware gate. Next
  rewrites the URL middleware sees (a trailing `.rsc` stripped, `..` resolved) while it routes the original path;
  `config.matcher` exclusions match prefixes of the raw path (`/healthz/x`, `/healthz/../premium/x`); and Next's image
  optimizer fetches local images through middleware with no user agent, so a gate asks it to pay and breaks every
  optimized image on a gated page, while it can't be told apart safely from an outside request. A Next gate needs
  its own design (inside route handlers and pages, or integrated with Next's routing), and on Vercel's Edge runtime a
  middleware must answer within 25 seconds, less than a settlement can take. *Waits:* Next.js sites can use Stilebase's
  Cloudflare Worker meanwhile.
- **An end-to-end test against a running Stilebase API.** *Waits:* the Python SDK has one in Stilebase's server
  suite, and both SDKs pass the same conformance cases.

## stilebase-sdk

- **Django's middleware is sync only.** Under Django on ASGI every request hops to a thread, and a settlement holds
  one for up to 65 seconds. It needs an async middleware (`async_capable`). *Waits:* sync Django, the common case,
  is fine.
- **The ASGI middleware doesn't gate WebSocket connections.** *Waits:* few sites serve their content over WebSockets.
- **It doesn't follow redirects**, so an API address that redirects (an old name) gives it no settings. *Waits:* the
  default address doesn't redirect.
- **WSGI servers that keep headers with underscores** (uWSGI, gunicorn before 22) let a visitor's `CF_Connecting_IP`
  land on the same environ key as the proxy's `CF-Connecting-IP`, so `ip_header` can read the visitor's. The README
  says which servers drop them. *Waits:* WSGI hides the raw header names; the common servers drop such headers.
- **Cleanup:** the sync and async drivers repeat the effect loop and the load functions; the ASGI and WSGI middlewares
  list GateOptions' keywords by hand (Django takes STILEBASE_GATE as GateOptions plus `ip_header` and `client_ip`).
  *Waits:* no behaviour changes.
