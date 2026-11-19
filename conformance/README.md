# Conformance

The cases every gate must agree on, whatever language it's written in:

- Stilebase's server and its Cloudflare Worker;
- `@stilebase/sdk` ([`sdk-js`](../sdk-js));
- `stilebase-sdk` ([`sdk-python`](../sdk-python));
- a gate in any other language.

| File | What it is | Checked by |
| --- | --- | --- |
| `vectors.json` | Call signing, site keys, address normalization, remember keys, skipped paths, the decide body a gate sends, and what it does with each answer | Each SDK's conformance tests, and Stilebase's own |
| `settings.example.json` | The settings a gate fetches (`GET /v1/gate/sites/{id}/settings`, [SPEC.md](../SPEC.md)), as Stilebase sends them | Each SDK's tests serve it from a fake Stilebase. Stilebase's tests fail when it no longer matches what Stilebase sends |

Every key and secret here is made up, for a gateway that doesn't exist. None of them works against Stilebase.

The decide-body cases are checked against `settings.example.json`'s `strip_headers` and `forward_cookies`; the
skipped-path cases carry their own `skip_paths`.

## Changing them

These files describe what Stilebase does, so they change only together with it:

1. Change the cases here, in a pull request to this repository.
2. Make both SDKs pass them, in the same pull request.
3. Stilebase is tested against the new cases and deploys the matching change. A release of either SDK that depends on
   new cases waits for that deploy.

A case that only one implementation passes is a bug in that implementation, or in the case: never in neither.
