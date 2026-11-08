# Conformance

The cases every gate must agree on, whatever language it's written in:

- Stilebase's server, which signs, verifies and builds the settings;
- Stilebase's Cloudflare Worker;
- `@stilebase/sdk` ([`sdk-js`](../sdk-js));
- `stilebase-sdk` ([`sdk-python`](../sdk-python));
- a gate in any other language.

| File | What it is | Checked by |
| --- | --- | --- |
| `vectors.json` | Call signing, site keys, the browser check's pass token, agent and script matching, the browser-header checks, address ranges, address normalization | `sdk-js/test/conformance.test.ts`, `sdk-python/tests/test_conformance.py`, and Stilebase's server and Worker tests |
| `gate-config.example.json` | The settings a gate fetches (`GET /v1/x402/sites/{id}/config`, [SPEC.md](../SPEC.md)), exactly as the server builds them | Both SDKs' tests use it as the settings a fake Stilebase serves. The server's tests fail when it no longer matches what the server sends |

Every key and secret here is made up: the site keys and secrets are derived from the server test suite's signing key
for a gateway id that doesn't exist. None of them works against Stilebase.

## Changing them

These files describe what Stilebase's server does, so they change only together with it:

1. Change the cases here, in a pull request to this repository.
2. Make both SDKs pass them, in the same pull request.
3. Stilebase's server pins this repository at a commit. Moving the pin runs the server's and the Worker's tests
   against the new cases, and the server changes with it.

A case that only one implementation passes is a bug in that implementation, or in the case: never in neither.
