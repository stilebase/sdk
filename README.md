# Stilebase SDKs

The official [Stilebase](https://stilebase.com) SDKs for JavaScript/TypeScript and Python.

Their first module is **the gate**. It answers AI agents (GPTBot, ClaudeBot, PerplexityBot…) with
`402 Payment Required` in the open [x402](https://x402.org) format. An agent that pays (USDC on Base) gets the page.
People, search engines and link previews see no change. Stilebase supplies the price, checks each payment and settles
it, and the site's owner sees every payment and agent on their dashboard.

If Stilebase can't be reached, everyone gets the site as usual: the gate never breaks it.

> **Not published yet.** The packages are built and tested here; they reach npm and PyPI with their first release.
> Until then, Stilebase's gateway page offers its Cloudflare Worker and copy-paste code instead.

| Package | Install | Works with |
| --- | --- | --- |
| [`@stilebase/sdk`](sdk-js) | `npm i @stilebase/sdk` | Express, Cloudflare Workers, Hono, Bun, Deno (Node 20+, no dependencies) |
| [`stilebase-sdk`](sdk-python) | `pip install stilebase-sdk` | FastAPI, Starlette, Litestar, Django, Flask, any ASGI or WSGI app (Python 3.10+) |

## Quick start

1. Create a gateway for your site at [stilebase.com](https://stilebase.com) and copy its **site key**.
2. Set it on your server: `STILEBASE_KEY=sbk_…`.
3. Add one line:

```ts
// Express
import { gate } from "@stilebase/sdk/gate/express";
app.use(gate());
```

```python
# FastAPI / Starlette
from stilebase_sdk.gate.asgi import GateMiddleware
app.add_middleware(GateMiddleware)
```

The gate fetches its settings (which agents to ask, which checks to run) from Stilebase, so new agents reach your
site without a redeploy. Each package's README has every framework and option: test mode, paths that stay free, the
visitor's address behind a proxy.

## In this repository

| Path | What |
| --- | --- |
| [`sdk-js/`](sdk-js) | `@stilebase/sdk` on npm |
| [`sdk-python/`](sdk-python) | `stilebase-sdk` on PyPI |
| [`conformance/`](conformance) | The test cases every gate must pass, shared with Stilebase's server |
| [`SPEC.md`](SPEC.md) | How a gate behaves: the spec for these SDKs and for a gate in any other language |

## Contributing and security

Issues and pull requests are welcome; see [CLAUDE.md](CLAUDE.md) for how the code is laid out and checked. Please
report security problems privately, as [SECURITY.md](SECURITY.md) describes, not in a public issue.

[MIT](LICENSE)
