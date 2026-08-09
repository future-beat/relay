#!/usr/bin/env bash
# End-to-end demo: create a ticket, then watch the agent work it via SSE.
# Requires the server running: uvicorn relay.main:app --reload
#
# Both endpoints now require an API key. The demo key is published openly — on
# the /dashboard page and in the README — so this defaults to the .env.example
# dev value and works against production once RELAY_DEMO_KEY is exported.
set -euo pipefail

BASE="${RELAY_URL:-http://127.0.0.1:8000}"

TICKET_ID=$(curl -s -X POST "$BASE/tickets" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${RELAY_DEMO_KEY:-dev-demo-key}" \
  -d '{
    "customer_email": "liam@brightco.io",
    "subject": "API rate limits",
    "body": "Hi, I keep getting 429 errors from your API. What are the rate limits on my plan, and can I raise them?"
  }' | python3 -c "import sys, json; print(json.load(sys.stdin)['id'])")

echo "Created ticket #$TICKET_ID — streaming agent run:"
echo
curl -N -X POST "$BASE/tickets/$TICKET_ID/process" \
  -H "X-API-Key: ${RELAY_DEMO_KEY:-dev-demo-key}"
