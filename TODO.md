# TODO

What's known and not yet done, each with why it waits. Review findings that aren't blocking
(`.claude/rules/development-workflow.md`) go here.

## Before releasing 0.2.0 (the thin gate)

- **Stilebase's payment journal is live** (its settle records every payment whose outcome isn't known, and settles it
  from the chain). *Waits:* its own change, at Stilebase.
- **An end-to-end run on dev** (Base Sepolia, real test USDC) through every adapter: Express, Fetch (a Worker), ASGI,
  WSGI and Django: a person served, an agent's 402, a paid page settled and delivered, a site error not charged, a
  capture received, and Stilebase down (everyone served, a payment 503). *Waits:* the journal, then this run, by hand,
  before tagging.

## Both SDKs

- **More frameworks and languages:** Next.js, Fastify and Koa adapters for `@stilebase/sdk` (Hono works today through
  the fetch adapter); Go, PHP and Ruby gates on [SPEC.md](SPEC.md) and `conformance/`. *Waits:* demand.
- **An end-to-end test against a running Stilebase API.** *Waits:* both SDKs pass the same conformance cases.
- **Some unusual paths are named differently by the two SDKs:** the JS SDK reads the path through `new URL` (it
  resolves `..` and `%2e%2e`, turns `\` into `/`, encodes `"` `<` `>` `{` `}` `|`); the Python SDK keeps the path as the
  server gave it. Only the path sent to Stilebase differs. *Waits:* rare; no page is freed by it.
- **Cookie fields joined with ", "** (the Fetch API joins HTTP/2's separate Cookie fields so, on Deno and Bun): a
  forwarded cookie is found only in the first. The Worker splits at ";" only, and the conformance cases say nothing about it.
  *Waits:* strict detection is off by default; a shared case first.
- **Python: the sync gate's budgets are per network step.** httpx's sync client has no overall deadline: `decide`
  applies `timeout_ms` to each of connect, write and read, and the first settings load splits its 3 s across the steps
  (1, 0.5, 1, 0.5 for the pool). A read that trickles in can still run longer. The async gate enforces totals with
  `asyncio.wait_for`. *Waits:* a true deadline costs a thread per call or a watchdog closing the connection;
  Stilebase's API answers or fails fast.
- **An absolute-form target under uvicorn's h11** keeps its whole target as the path (`//host/x`), where Express names
  `/x`. *Waits:* Starlette doesn't route it either; no page is freed.
- **Python: the sync and async gates repeat their calls** (settings, decide, settle, captures) around one core without
  IO (`_core.py`). *Waits:* every rule is in the core; the drivers differ only in how they wait (asyncio or threads),
  and sharing in-flight decisions exists only in the async one.

- **A rewrite before the gate:** Express names the visitor's page (originalUrl), while WSGI, and ASGI without
  `raw_path`, name the path after the rewrite. *Waits:* WSGI and ASGI hand the app no original URL to read; mount the
  gate before rewrites.
- **One environment for both packages:** `release` admits both tag patterns, so the npm and PyPI trusted publishers
  are tied to the same environment, and only `release.yml`'s `if:` conditions keep an `sdk-js-v*` run from publishing
  to PyPI. Two environments (`release-npm` for `sdk-js-v*`, `release-pypi` for `sdk-py-v*`), each publisher registered
  to its own, would tie each package to its own tags. *Waits:* the tagged commit's `release.yml` is checked before
  every tag (CLAUDE.md, "Releases"); register the publishers first.

## At Stilebase, not in the SDKs

- **Price unusual page URLs conservatively** (percent-encoding, dot segments, a `#`): a router may serve another page
  than the one the gate names. *Waits:* a server-side change.
- **Name the pass cookie `__Host-stilebase_pass`** in the settings, so a sibling subdomain can't plant one. *Waits:* a
  settings change; strict detection is off by default.

## @stilebase/sdk

- **The test tooling's dependencies** (vitest 2 and its vite and esbuild) have open advisories. Bump them in a
  `chore/` pull request. *Waits:* development only; nothing ships with them (the package has no runtime dependencies).
- **Express: `url.parse` on every request.** Node 24 can print DEP0169 once when the SDK isn't under `node_modules`
  (bundled or linked), and a plain `/...` target is parsed twice (here and in the core). Express itself uses
  `url.parse` only for unusual targets; doing the same would avoid both. *Waits:* a warning at most, no wrong page.
- **The build's `external: ["node:url"]` is redundant:** tsup strips the `node:` prefix and ships a bare `url` import.
  *Waits:* Node, Bun and Deno resolve `url` to the built-in; only a bundler that polyfills it would differ.

## stilebase-sdk

- **Django's middleware is sync only.** Under Django on ASGI every request hops to a thread, and a request with a
  payment holds one for up to `settle_timeout_ms` (65 s). It needs an async middleware (`async_capable`). *Waits:*
  sync Django, the common case, is fine.
- **The ASGI middleware doesn't gate WebSocket connections** (it passes every scope that isn't `http`). *Waits:* few
  sites serve their content over WebSockets.
