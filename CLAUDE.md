# Stilebase SDKs

The public SDKs for [Stilebase](https://stilebase.com): `@stilebase/sdk` (npm) and `stilebase-sdk` (PyPI). Each is
its own project with modules inside; the first module is **the gate**, which asks AI agents to pay per request with
x402 v2 (USDC on Base) and lets people through. [SPEC.md](SPEC.md) is how a gate behaves; read it before changing
one.

How changes are built (branches, build/test/review cycles, merging, releases): `.claude/rules/development-workflow.md`.
Never commit or push to `main` directly: every change goes on its own branch and reaches `main` by a squash-merged pull
request once all CI checks are green. A ruleset enforces it.

This repository is public. Nothing in it may mention Stilebase's private code, infrastructure, hosts, keys or
customers. The SDKs talk only to Stilebase's public API.

## Layout

- `sdk-js/`: `@stilebase/sdk`, TypeScript, tsup, vitest, Node 20+, **no runtime dependencies** (WebCrypto, `fetch`).
  Entries `.` (the `gate` namespace, `VERSION`), `/gate` (`createGate`, framework-neutral), `/gate/express`,
  `/gate/fetch` (Workers, Hono, Bun, Deno). No Next.js adapter yet (TODO.md).
- `sdk-python/`: `stilebase-sdk`, Python 3.10+, depends only on `httpx`, hatchling, typed (`py.typed`, mypy strict).
  `stilebase_sdk.gate`: `_core.decide` is one sans-IO decision generator that yields effects; `AsyncGate` and
  `SyncGate` drive it. Adapters `asgi`, `wsgi`, `django`.
- `conformance/`: `vectors.json` and `gate-config.example.json`, the cases every gate must pass. Both SDKs test
  against them, and so does Stilebase. They describe what Stilebase does: change them only together with it
  (`conformance/README.md`).

## Build & test

```bash
cd sdk-js && npm ci && npm run typecheck && npm run lint && npm test && npm run build && npm run smoke
cd sdk-python && uv sync && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

`npm run smoke` and `sdk-python/scripts/smoke.sh 3.10` install the built package the way users get it (a packed
tarball in an empty project; the wheel in a clean Python 3.10) and import every entry. CI (`.github/workflows/ci.yml`)
runs all of it, the Python suite on 3.10 to 3.13.

## Rules the gate keeps

These are what the reviews check first. Breaking one is a blocking finding.

- **Never break the site.** When Stilebase can't be reached or errors, everyone gets the site. The only exceptions
  are the three in SPEC.md, each because the alternative lets agents read free.
- **Never tell an agent it wasn't charged when the outcome is unknown** (a settlement without an answer).
- **Never trust a header the visitor can set** for their address (SPEC.md, "The visitor's address"). A spoofed
  operator address would get a free pass.
- **Cut what's sent to Stilebase to its limits** (SPEC.md, "The price"): a refused call would serve the page unpaid.
- **Both SDKs behave the same.** A change to how a gate behaves is made to both in the same pull request, with a
  conformance case when it's observable. Changes to one package only (an adapter, its docs) are its own pull request.
- **Secrets stay on the server.** The site key never reaches an agent, a browser, a log line or an error message.
- **No new runtime dependencies** without a reason written in the pull request. The JS package has none.

## Releases

`.github/workflows/release.yml`, on tags `sdk-js-v<version>` and `sdk-py-v<version>`. Only admins can create those tags
(a ruleset). A build job checks that the tag is the package's version and on `main`, runs the checks and builds; the
publish job gets only the built files and waits for approval in the `release` environment, then publishes through
trusted publishing (no stored tokens), with provenance. Bump the version and `CHANGELOG.md` in a pull request first.

The first npm release can't use trusted publishing yet (npm configures it on an existing package). Push the
`sdk-js-v0.1.0` tag and let the workflow build, check and smoke-test it; its publish step fails. Download that run's
`npm-package` artifact, check its SHA-256 against the build job's output, and publish exactly that file by hand:
`npm publish stilebase-sdk-0.1.0.tgz --access public --ignore-scripts` (logged in with 2FA). Then add the trusted
publisher (`stilebase/sdk`, `release.yml`, environment `release`) and require 2FA with tokens disallowed. Every later
release goes through the workflow.

Approving a release in the `release` environment: check that the run's commit is the head of `main` (or on it) and
that `release.yml` there is unchanged. The workflow checks it too, but that check lives in the tagged commit's own copy
of the workflow.

Each package sends its version in `Stilebase-Client`. The JS version is `package.json`'s (built into
`sdk-js/src/version.ts`); the Python version is `sdk-python/src/stilebase_sdk/_version.py`, which `pyproject.toml`
reads. The tests check both.

## TODO

`TODO.md` lists what's known and not yet done, each with why it waits.
