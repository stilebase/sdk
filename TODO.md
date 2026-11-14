# TODO

What's known and not yet done, each with why it waits. Review findings that aren't blocking
(`.claude/rules/development-workflow.md`), and findings the Cloudflare Worker shares (CLAUDE.md, "A connector, not a
library"), go here.

## Both SDKs

- **More frameworks and languages:** Next.js, Fastify and Koa adapters for `@stilebase/sdk` (Hono works today through
  the fetch adapter); Go, PHP and Ruby gates on [SPEC.md](SPEC.md) and `conformance/`. *Waits:* demand.
- **Shared cases for the whole flow** (a 402, a free pass, a settlement, the visit reported), not only for the checks.
  *Waits:* each SDK tests the flow against a fake Stilebase.
- **An end-to-end test against a running Stilebase API.** *Waits:* both SDKs pass the same conformance cases.
- **Some unusual paths are named differently by the two SDKs:** the JS SDK reads the path through `new URL` (it
  resolves `..` and `%2e%2e`, turns `\` into `/`, encodes `"` `<` `>` `{` `}` `|`); the Python SDK keeps the path as the
  server gave it. Only the page URL sent to Stilebase differs. *Waits:* rare; no page is freed by it.
- **The address lists are scanned prefix by prefix** on each request with an address, as the Worker does. An index by
  prefix length would be faster. *Waits:* the lists are short today.
- **Python: the timeouts are per network step**, not a total deadline (httpx applies 3, 5 and 65 seconds to each of
  connect, read and write), so a trickling answer can hold a request longer than in JS. *Waits:* the far end is
  Stilebase's own API, which answers or fails fast; the Worker has no timeouts at all.
- **Python: the sync and async drivers repeat their effect loop** and the request code. *Waits:* no behaviour changes.

## At Stilebase, not in the SDKs

- **Price unusual page URLs conservatively** (percent-encoding, dot segments, a `#`): a router may serve another page
  than the one the gate names. *Waits:* a server-side change.
- **Name the pass cookie `__Host-stilebase_pass`** in the settings, so a sibling subdomain can't plant one. *Waits:* a
  settings change; strict detection is off by default.

## @stilebase/sdk

- **The test tooling's dependencies** (vitest 2 and its vite and esbuild) have open advisories. Bump them in a
  `chore/` pull request. *Waits:* development only; nothing ships with them (the package has no runtime dependencies).
