#!/usr/bin/env bash
# End-to-end demo: create a ticket, then watch the agent work it via SSE.
# Requires the server running: uvicorn relay.main:app --reload
#
# Both endpoints now require an API key. The demo key is published openly — on
# the /dashboard page and in the README — so exporting RELAY_DEMO_KEY is all
# this needs, locally or against production.
set -euo pipefail

BASE="${RELAY_URL:-http://127.0.0.1:8000}"

# Fail loudly rather than sending a placeholder the server will 401: a silent
# fallback here reads as "the perimeter is broken" instead of "set your key".
if [ -z "${RELAY_DEMO_KEY:-}" ]; then
  echo "RELAY_DEMO_KEY is not set." >&2
  echo "Set it to the demo key shown on $BASE/dashboard (or in the README):" >&2
  echo "  export RELAY_DEMO_KEY=..." >&2
  exit 1
fi

TICKET_ID=$(curl -s -X POST "$BASE/tickets" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $RELAY_DEMO_KEY" \
  -d '{
    "customer_email": "liam@brightco.io",
    "subject": "API rate limits",
    "body": "Hi, I keep getting 429 errors from your API. What are the rate limits on my plan, and can I raise them?"
  }' | python3 -c "import sys, json; print(json.load(sys.stdin)['id'])")

echo "Created ticket #$TICKET_ID — streaming agent run:"
echo
curl -N -X POST "$BASE/tickets/$TICKET_ID/process" \
  -H "X-API-Key: $RELAY_DEMO_KEY"
