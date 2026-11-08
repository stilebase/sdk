# Conformance

The cases every gate must agree on, whatever language it's written in:

- Stilebase's server and its Cloudflare Worker;
- `@stilebase/sdk` ([`sdk-js`](../sdk-js));
- `stilebase-sdk` ([`sdk-python`](../sdk-python));
- a gate in any other language.

| File | What it is | Checked by |
| --- | --- | --- |
| `vectors.json` | Call signing, site keys, the browser check's pass token, agent and script matching, the browser-header checks, address ranges, address normalization, `paths` matching, which header is the payment | Each SDK's conformance tests, and Stilebase's own |
| `gate-config.example.json` | The settings a gate fetches (`GET /v1/x402/sites/{id}/config`, [SPEC.md](../SPEC.md)), as Stilebase sends them | Each SDK's tests serve it from a fake Stilebase. Stilebase's tests fail when it no longer matches what Stilebase sends |

Every key and secret here is made up, for a gateway that doesn't exist. None of them works against Stilebase.

The browser-check cases in `vectors.json` state their own settings (a case "over plain http" is checked with *assume
HTTPS* off); run each case with the settings it names, not the gate's defaults.

## Changing them

These files describe what Stilebase does, so they change only together with it:

1. Change the cases here, in a pull request to this repository.
2. Make both SDKs pass them, in the same pull request.
3. Stilebase is tested against the new cases and deploys the matching change. A release of either SDK that depends on
   new cases waits for that deploy.

A case that only one implementation passes is a bug in that implementation, or in the case: never in neither.
