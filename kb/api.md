# API Access

The Lanekeep REST API is available on Pro and Enterprise plans.

## Authentication

Create an API key under **Settings → Developer → API keys**. Pass it as a
bearer token: `Authorization: Bearer lk_live_...`. Keys can be scoped read-only
or read-write and can be revoked at any time.

## Rate limits

- Pro: 600 requests/minute per workspace.
- Enterprise: 3,000 requests/minute per workspace (higher on request).

A 429 response includes a `Retry-After` header. Exceeding limits repeatedly for
more than 24 hours may result in temporary key suspension.

## Webhooks

Webhooks are Enterprise-only. Endpoints must be HTTPS and respond within 5
seconds; deliveries are retried 3 times with exponential backoff.
