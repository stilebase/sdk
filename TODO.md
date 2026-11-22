# TODO

What's known and not yet done, each with why it waits. Review findings that aren't blocking
(`.claude/rules/development-workflow.md`) go here.

## Released 0.2.1 (the thin gate; 0.2.0 was never released)

Nothing open. Stilebase's production API serves the thin gate's calls (`/v1/gate/sites/{id}/settings`, `/decide`,
`/settle`): checked November 22, 2026 (an unknown site answers "No Stilebase gateway for this site", not a missing
route). Also done: Stilebase's payment journal; the end-to-end run on dev through every adapter (November 20, 2026; its
first run found a decision cut off at 300 ms aborted the connection it was opening, so after 5 s idle every agent passed
free: fixed with a 1 s budget, calls not aborted, connections kept; the rerun passed); AgentView's collector among
agents in flight, end to end against the API (never sharing a decision, never a visit). Not blocking: the capture
case, which Stilebase only asks for from AgentView's continuous mode; until then no gate captures.

## Both SDKs

- **Late decisions, from the budget fix (November 20, 2026; review findings, none blocking):** a call past its budget
  runs on up to 5 s and holds a pool connection (Python: 1000): with Stilebase half slow, the pool can fill and new
  calls pass uncounted (`NOT_ASKED`); a cap on calls running late, or counting pool waits while some run late. JS
  reports a late call twice (late, then its 5 s abort); Python reports only the first. Requests waiting on a late
  leader: JS may still get its late answer, Python passes at the leader's budget. A late plain `pass` with `remember`
  could still be remembered (it's dropped). The sync gate aborts a call at its budget (per step: a request can wait
  several budgets when Stilebase is slow); a small executor would give it an overall deadline and let calls run on. The ASGI middleware never closes its gate, so a late call at shutdown is
  destroyed pending. No test proves a late call's connection is pooled (the fakes have no pool). *Waits:* seen in
  production.

- **No local skip option:** a site's own health check or uptime probe, sent by a script's user agent (curl,
  Python-urllib), is asked about like any request and gets the 402: the container is then marked unhealthy. Today the
  site answers such paths before the gate (mounting the gate on the rest). An option such as `skip_paths` on the
  middleware, matched as SPEC.md section 5 does, would do it for everyone. *Waits:* a design for how it combines with
  Stilebase's `skip_paths` (a local one must never be able to free a paid page by accident).
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

Server-side findings from these reviews (pricing unusual page URLs conservatively, a `__Host-` name for the pass
cookie) are tracked at Stilebase, not here.

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
  payment holds one through two calls (`decide` and `settle`, each up to `settle_timeout_ms`, 65 s) plus the view: over
  two minutes at worst. It needs an async middleware (`async_capable`). *Waits:* sync Django, the common case, is fine.
- **The ASGI middleware doesn't gate WebSocket connections** (it passes every scope that isn't `http`). *Waits:* few
  sites serve their content over WebSockets.
