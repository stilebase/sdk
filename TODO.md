# TODO

What's known and not yet done, each with why it waits. Review findings that aren't blocking
(`.claude/rules/development-workflow.md`), and findings the Cloudflare Worker shares (CLAUDE.md, "A connector, not a
library"), go here.

## Both SDKs

- **More frameworks and languages:** Next.js, Fastify and Koa adapters for `@stilebase/sdk` (Hono works today through
  the fetch adapter); Go, PHP and Ruby gates on [SPEC.md](SPEC.md) and `conformance/`. *Waits:* demand.
- **An end-to-end test against a running Stilebase API.** *Waits:* both SDKs pass the same conformance cases.
- **Some unusual paths are named differently by the two SDKs:** the JS SDK reads the path through `new URL` (it
  resolves `..` and `%2e%2e`, turns `\` into `/`, encodes `"` `<` `>` `{` `}` `|`); the Python SDK keeps the path as the
  server gave it. Only the page URL sent to Stilebase differs. *Waits:* rare; no page is freed by it.
- **Cookie fields joined with ", "** (the Fetch API joins HTTP/2's separate Cookie fields so, on Deno and Bun): a
  forwarded cookie is found only in the first. The Worker splits at ";" only, and the conformance cases say nothing about it.
  *Waits:* strict detection is off by default; a shared case first.
- **Python: the timeouts are per network step**, not a total deadline (httpx applies 3, 5 and 65 seconds to each of
  connect, read and write), so a trickling answer can hold a request longer than in JS. *Waits:* the far end is
  Stilebase's own API, which answers or fails fast; the Worker has no timeouts at all.
- **An absolute-form target under uvicorn's h11** keeps its whole target as the path (`//host/x`), where Express names
  `/x`. *Waits:* Starlette doesn't route it either; no page is freed.
- **Python: the sync and async drivers repeat their effect loop** and the request code. *Waits:* no behaviour changes.

- **A rewrite before the gate:** Express names the visitor's page (originalUrl), while WSGI, and ASGI without
  `raw_path`, name the path after the rewrite. *Waits:* WSGI and ASGI hand the app no original URL to read; mount the
  gate before rewrites.
- **Tests that could be sharper:** concurrent address-list loads (the generation check), a verification value with a
  lone surrogate sent through the gate rather than `_respond`, and a `client_ip` Future that fails later (its error
  retrieved). *Waits:* the behaviour is covered at one level below.

## Releases

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

- **`_respond` scans every body for lone surrogates**, though only the verification value can hold one (the 402 is
  escaped already). *Waits:* a few KB per 402.
