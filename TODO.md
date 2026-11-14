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
- **A page with `..` in its path is named differently:** the JS SDK resolves it (`new URL`), the Python SDK keeps it.
  Only the price lookup's page URL differs. *Waits:* rare; no page is freed by it.

## At Stilebase, not in the SDKs

- **Price unusual page URLs conservatively** (percent-encoding, dot segments, a `#`): a router may serve another page
  than the one the gate names. *Waits:* a server-side change.
- **Name the pass cookie `__Host-stilebase_pass`** in the settings, so a sibling subdomain can't plant one. *Waits:* a
  settings change; strict detection is off by default.

## @stilebase/sdk

- **The test tooling's dependencies** (vitest 2 and its vite and esbuild) have open advisories. Bump them in a
  `chore/` pull request. *Waits:* development only; nothing ships with them (the package has no runtime dependencies).
