from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# The value D-02 publishes for the hosted demo, in one place so the README, the demo
# script and `fly secrets set RELAY_DEMO_KEY=...` cannot drift into disagreeing — the
# README used to carry a literal that no test looked at, and a visitor running
# scripts/demo.sh against a different one got a 401 they could not diagnose.
#
# Deliberately not the default for `demo_key` below. Auth fails closed when no key is
# configured, and a default here would make every unconfigured deployment accept a key
# published on the internet. It documents what the hosted instance is deployed with;
# .env.example still ships empty so local dev generates its own.
PUBLISHED_DEMO_KEY = "relay-demo-2026"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RELAY_", env_file=".env", extra="ignore")

    # Read without the RELAY_ prefix so the same variable works for the SDK's own lookup.
    anthropic_api_key: str | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    model: str = "claude-sonnet-5"
    db_path: Path = Path("relay.db")
    kb_dir: Path = Path("kb")
    max_agent_steps: int = 10
    max_tokens: int = 16000

    # MCP server (phase 5). Writes are opt-in: set RELAY_MCP_ALLOW_WRITES=true to enable them.
    mcp_allow_writes: bool = False

    # Security perimeter (phase 1). Both keys unset means auth fails closed.
    api_key: str | None = None
    demo_key: str | None = None
    max_daily_cost_usd: float = 5.0
    # Only read a proxy-supplied client IP when the deployment is actually behind one.
    # Aliased so the short RELAY_TRUST_PROXY name works as well as the attribute-derived one.
    trust_proxy_header: bool = Field(
        default=False,
        validation_alias=AliasChoices("RELAY_TRUST_PROXY", "RELAY_TRUST_PROXY_HEADER"),
    )
    # Charged before the presented key is known good, so guessing costs the same
    # allowance a correct key does. Deliberately well above every tier's own limit:
    # this is the outer bound on an anonymous caller, not the working ceiling for an
    # authenticated one.
    anon_auth_limit: str = "60/minute"
    demo_process_limit: str = "5/hour"
    owner_process_limit: str = "60/hour"
    demo_create_limit: str = "20/hour"
    owner_create_limit: str = "120/hour"
    demo_read_limit: str = "120/hour"
    owner_read_limit: str = "600/hour"
    # Charged only by requests the daily ceiling refuses. Without it, an exhausted
    # budget made /process an unthrottled endpoint — the tiered window below is never
    # reached once the 503 raises, so the anon 60/minute was the only remaining cap and
    # every one of those requests still ran a SUM over the runs table. Tier-independent
    # and per-IP, because during an outage nobody is doing paid work: this only has to
    # keep a refusal cheaper than a retry loop. It is a separate bucket rather than the
    # tiered one so a global outage still does not spend the caller's own allowance.
    outage_process_limit: str = "10/minute"
    # The public live feed's own per-IP bucket (phase 5). /events resolves no credential,
    # so there is no tier to key on and this is the only per-caller bound on it. A
    # separate bucket rather than the shared anon one because a viewer reconnecting on
    # its idle ceiling must not spend the allowance that meters key guessing, and a feed
    # flood must be visible in the logs as itself. Generous against real use — an
    # EventSource reconnects once per idle close (5 min) — and small against a
    # reconnect loop, which is the connection-holding attack that defeats scale-to-zero.
    anon_events_limit: str = "30/minute"

    # Guardrails (phase 2). Prices default to Claude Sonnet 5 per-MTok rates.
    max_run_cost_usd: float = 0.50
    price_in_per_mtok: float = 3.0
    price_out_per_mtok: float = 15.0

    # Shutdown drain (phase 2 remaster). How long the lifespan waits for in-flight
    # SSE runs before closing the database. Innermost of three nested windows: it
    # nests inside uvicorn's --timeout-graceful-shutdown (20s), which nests inside
    # fly.toml's kill_timeout (30s). Overshooting any of them means SIGKILL instead.
    shutdown_drain_seconds: float = 5.0

    # Semantic retrieval (phase 3).
    #
    # Read without the RELAY_ prefix, same escape hatch as anthropic_api_key above.
    # The prefix would rename this to RELAY_VOYAGE_API_KEY, so a correctly-named
    # VOYAGE_API_KEY in the environment or a Fly secret would be ignored and the
    # agent would fall back to keyword search — working, cheaper, quietly worse,
    # and with nothing in the logs to say so.
    #
    # Defaults to None on purpose: unset key IS the keyword-fallback baseline, so
    # CI and local dev run the retrieval path without a credential. Never logged,
    # never in a span attribute, never in a query string — it leaves the process
    # only as an Authorization header to the Voyage endpoint.
    voyage_api_key: str | None = Field(default=None, validation_alias="VOYAGE_API_KEY")
    voyage_model: str = "voyage-4-lite"
    # Must match the output_dimension the committed kb index was built with. Changing
    # it without rebuilding the index yields vectors of two different widths.
    voyage_dim: int = 512
    # Calibrated against the golden set (plan 03-06), not chosen by intuition. Measured
    # cosines against the committed index: the uncovered topic tops out at 0.2543
    # ("Salesforce ..." queries, 0.1951–0.2543) and unrelated control queries lower
    # still (0.1756–0.2000), while a query about a covered topic scores 0.34–0.63 on its
    # own doc. 0.30 sits in that gap with ~0.05 of margin on the off-topic side, so an
    # uncovered query yields no semantic hit and — absent keyword hits — `results: []`,
    # which is what makes the model escalate (D-03/D-04).
    #
    # Two covered queries measured below this ("uptime SLA guarantee" 0.2659, and
    # sub-0.30 second-place docs): they are not starved, because the keyword half of the
    # hybrid union still returns their doc (D-05). The floor drops the near-uniform
    # low-similarity noise docs, not the answer.
    #
    # The 0.55 this shipped with as a placeholder was far too high: only one of the
    # twelve golden queries cleared it, so the phase's semantic ranking would have been
    # silently inert and every case keyword-ranked.
    retrieval_floor: float = 0.30

    # Run-event live feed (phase 5). All defaulted: /events is public and projection-only,
    # so this layer adds no key and nothing here needs to be configured to deploy.
    #
    # Per-subscriber bounded queue. Above this the broker drops the oldest frame rather
    # than awaiting the slow subscriber — a stalled dashboard tab must not backpressure
    # the paid run that is publishing to it.
    events_queue_maxsize: int = 256
    # SSE comment keep-alive, so a quiet feed does not look dead to a proxy or a browser
    # that would otherwise time the connection out mid-idle.
    events_heartbeat_seconds: float = 15.0
    # Close an idle /events stream, so a forgotten tab cannot hold the Fly machine awake
    # and defeat min_machines_running=0 (D-09). The deadline resets on real frames only,
    # never on heartbeats — otherwise the server's own keep-alive would keep it alive
    # forever. EventSource reconnects by itself when the viewer comes back.
    events_idle_seconds: float = 300.0
    # Hard ceiling on concurrent live viewers. publish() is O(subscribers) and runs on
    # the loop that answers the container HEALTHCHECK, and each subscriber holds up to
    # events_queue_maxsize frames on a 512MB machine — so an uncapped subscriber set is
    # an attacker-chosen cost charged to every paid run. Over the cap /events refuses
    # with a 503 rather than growing: a viewer turned away costs nothing, a viewer
    # admitted costs every run that publishes afterwards.
    events_max_subscribers: int = 50
    # How long a run's per-step rows are kept before the startup sweep deletes them.
    #
    # `run_events.payload` is stored RAW by design (D-01) — it is the full-fidelity
    # record phase 6 drills into — so it holds customer emails, ticket bodies, reply
    # text and every tool argument. Without a window that is unbounded personal data
    # accumulating on the Fly volume for the life of the deployment, on a demo anyone
    # can drive, plus a disk-exhaustion path on a 512MB machine (WR-05).
    #
    # 30 days: long enough that the drill-down is useful for any run a visitor or an
    # operator would still be looking at, short enough that the demo is not a long-term
    # store of other people's support tickets. The `runs` summary table is NOT swept —
    # it carries no message content and /metrics is built from it.
    events_retention_days: int = 30


settings = Settings()
