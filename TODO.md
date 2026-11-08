# TODO

What's known and not yet done, each with why it waits. Findings from code reviews that aren't blocking
(`.claude/rules/development-workflow.md`) go here.

## Both SDKs

- **More frameworks:** Fastify, Koa and Hono-native adapters for `@stilebase/sdk` (Hono works today through
  `withGate(app.fetch)`); Django's async middleware for `stilebase-sdk`. *Waits:* the adapters that exist cover the
  common stacks.
- **More languages**, each on `conformance/` and [SPEC.md](SPEC.md): Go (`net/http`, Gin, Echo, Caddy and Traefik
  plugins), PHP (Laravel, Symfony), Ruby (Rack), Java/Kotlin, .NET, Rust. *Waits:* demand decides the order.
