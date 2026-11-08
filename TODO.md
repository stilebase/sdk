# TODO

What's known and not yet done, each with why it waits. Findings from code reviews that aren't blocking
(`.claude/rules/development-workflow.md`) go here.

## Both SDKs

- **Warn once when the gate has no visitor address** (the fetch adapter outside Cloudflare, Next.js outside Vercel,
  without `ipHeader`). Free passes then never verify and hidden agents aren't caught by address, silently. *Waits:* the
  READMEs say to set it; agents are still asked to pay by user agent.
- **Python: the timeouts are per network step, not a total deadline.** httpx applies 3, 5 and 65 seconds to each of
  connect, write and read, so a slow or trickling answer can hold a request longer (the JS SDK's are total). Wrap each
  call in an overall deadline. *Waits:* the far end is Stilebase's own API, which answers or fails fast.

- **More frameworks:** Fastify, Koa and Hono-native adapters for `@stilebase/sdk` (Hono works today through
  `withGate(app.fetch)`); Django's async middleware for `stilebase-sdk`. *Waits:* the adapters that exist cover the
  common stacks.
- **More languages**, each on `conformance/` and [SPEC.md](SPEC.md): Go (`net/http`, Gin, Echo, Caddy and Traefik
  plugins), PHP (Laravel, Symfony), Ruby (Rack), Java/Kotlin, .NET, Rust. *Waits:* demand decides the order.
