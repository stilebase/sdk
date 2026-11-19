# Changelog

## 0.2.0

The thin gate (SPEC.md): the gate forwards each request to Stilebase (`/v1/gate/sites/{id}/decide`) and does what it
says; every rule (agents, scripts, browser checks, prices) now runs at Stilebase. A paid page is held until the payment
is settled (`/settle`), so an agent is charged only for a page it got; Stilebase can ask for a copy of an answer
(`capture`, AgentView). New options: `disabled` (`STILEBASE_DISABLED`), `assume_https`. Removed: `catch_http_clients`,
`catch_hidden_agents`, `check_browsers` (Stilebase decides). `GateRequest` takes the path and header pairs, never a URL.
Needs a Stilebase with the `/v1/gate` API.

## 0.1.0

First release: the gate for ASGI (FastAPI, Starlette…), WSGI (Flask…) and Django. It does what Stilebase's
Cloudflare Worker does, run from one site key, with its settings fetched from Stilebase.
