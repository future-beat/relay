from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # Guardrails (phase 2). Prices default to Claude Sonnet 5 per-MTok rates.
    max_run_cost_usd: float = 0.50
    price_in_per_mtok: float = 3.0
    price_out_per_mtok: float = 15.0

    # Shutdown drain (phase 2 remaster). How long the lifespan waits for in-flight
    # SSE runs before closing the database. Innermost of three nested windows: it
    # nests inside uvicorn's --timeout-graceful-shutdown (20s), which nests inside
    # fly.toml's kill_timeout (30s). Overshooting any of them means SIGKILL instead.
    shutdown_drain_seconds: float = 5.0


settings = Settings()
