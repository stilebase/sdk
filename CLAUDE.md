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
  `/gate/fetch` (Workers, Hono, Bun, Deno). `protocol.ts` builds and reads the calls; `core.ts` is the gate.
- `sdk-python/`: `stilebase-sdk`, Python 3.10+, depends only on `httpx`, hatchling, typed (`py.typed`, mypy strict).
  `stilebase_sdk.gate`: `_core.py` is the gate without IO (no httpx; `_http.py` holds the client limits) (reading settings and answers, skipped paths, remember keys,
  the decide body, the breaker, the remembered answers); `AsyncGate` (`_async.py`) and `SyncGate` (`_sync.py`) make the
  calls to Stilebase. Adapters `asgi`, `wsgi`, `django` hold a paid answer until it's settled and copy one for capture.
- `conformance/`: `vectors.json` and `settings.example.json`, the cases every gate must pass. Both SDKs test
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

- **Forward, obey, fail open.** A gate has no logic of its own (SPEC.md): it forwards each request to Stilebase, does
  what the answer says, and passes the request when anything goes wrong. Who's an agent, the browser checks, prices
  and payments are Stilebase's: none of it goes into an SDK. The only things a gate decides are mechanical: skipped
  paths, remembered answers, the circuit breaker, holding a paid answer and copying a captured one.
- **Never break the site.** When Stilebase can't be reached, errors or answers late, everyone gets the site. The one
  exception is in SPEC.md: a request carrying a payment gets `503` rather than a page whose payment can't be told.
- **Never delay or change the visitor's answer** for a capture (SPEC.md section 9).
- **Never tell an agent it wasn't charged when the outcome is unknown** (a settlement without an answer).
- **Never trust a header the visitor can set** for their address (SPEC.md, "The visitor's address"). A spoofed
  operator address would get a free pass.
- **Never send the request body, cookies other than `forward_cookies`, or `strip_headers`** to Stilebase.
- **Both SDKs behave the same.** A change to how a gate behaves is made to both in the same pull request, with a
  conformance case when it's observable. Changes to one package only (an adapter, its docs) are its own pull request.
- **Secrets stay on the server.** The site key never reaches an agent, a browser, a log line or an error message.
- **No new runtime dependencies** without a reason written in the pull request. The JS package has none.

## Releases

`.github/workflows/release.yml`, on tags `sdk-js-v<version>` and `sdk-py-v<version>`. Only admins can create those tags
(a ruleset). A build job checks that the tag is the package's version and on `main`, runs the checks and builds; the
publish job gets only the built files and publishes them through trusted publishing (no stored tokens), with
provenance. Pushing the tag is the release: there's no approval step after it. There's no deployment environment (a
choice): the trusted publishers on npm and PyPI accept this repository's `release.yml` from any ref, so anything that
can push a commit changing `.github/workflows/` to a branch here (people, GitHub Apps, tokens, write deploy keys)
could publish from an edited copy, and the tag ruleset doesn't stop it. Keep write access to the admins: no app with
contents:write, no write deploy key. Dependabot is the exception: its branches change dependency versions (lockfiles,
action SHAs in the workflows) and never triggers; review its workflow edits as code. Never push a branch that changes
`release.yml`'s triggers, its `if:` conditions or its "on main" check: pushing it can publish. The publishers must
be registered without an environment: one that names an environment matches no run. Bump the version
and `CHANGELOG.md` in a pull request first. **Don't push a tag while any item in `TODO.md` under "Before releasing <that
version>" is open**: they're what has to be true at Stilebase for the release to work (a gate whose calls fail lets
everyone through).

Before pushing a tag, check that the tagged commit is on `main` and that its `release.yml` is the same as on the head
of `main` (or that you understand every difference): the tagged commit's copy is the one that runs, its "on main"
check included. To abandon a release whose run didn't publish, delete the run (a re-run would publish it).

npm: 0.2.1 was published by hand (npm configures trusted publishing only on an existing package): the tarball the
`sdk-js-v0.2.1` run built, checked against its SHA-256. The trusted publisher (`stilebase/sdk`, `release.yml`, no
environment) is set, with 2FA required and tokens disallowed, so every later release goes through the workflow. (By
hand, the `./` in `npm publish ./<file>.tgz` matters: npm reads `dir/file.tgz` as a GitHub repository.) The package
also lists `0.0.0-stage`, a placeholder npm created when the package was set up ("Temporary package placeholder for
staged publishing"); it isn't `latest`; deprecate it (`npm deprecate @stilebase/sdk@0.0.0-stage "placeholder"`).
If npm ever holds a workflow publish for approval, approve it there after checking it came from a tag's run.

PyPI: the `sdk-py-v0.2.1` run failed (`invalid-publisher`: no publisher matched owner `stilebase`, repo `sdk`, workflow
`release.yml`), so `stilebase-sdk` doesn't exist yet. The tag points at `c0f5662`, whose `release.yml` predates this
one, and nothing was published from it. In this order:

1. Delete the failed `sdk-py-v0.2.1` run (a re-run would publish 0.2.1 from the old `release.yml`).
2. Delete the tag.
3. Register a pending trusted publisher for project `stilebase-sdk` (owner `stilebase`, repo `sdk`, workflow
   `release.yml`, environment left empty). It doesn't reserve the name, so go straight on.
4. Push the tag again on a commit of `main` whose `sdk-python/` is the 0.2.1 that was reviewed (`git diff c0f5662
   <commit> -- sdk-python conformance` is empty), or bump the version if it isn't. Its run creates the project.

Each package sends its version in `Stilebase-Client`. The JS version is `package.json`'s (built into
`sdk-js/src/version.ts`); the Python version is `sdk-python/src/stilebase_sdk/_version.py`, which `pyproject.toml`
reads. The tests check both.

## TODO

`TODO.md` lists what's known and not yet done, each with why it waits.
