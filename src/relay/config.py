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
    demo_process_limit: str = "5/hour"
    owner_process_limit: str = "60/hour"
    demo_create_limit: str = "20/hour"
    owner_create_limit: str = "120/hour"
    demo_read_limit: str = "120/hour"
    owner_read_limit: str = "600/hour"

    # Guardrails (phase 2). Prices default to Claude Sonnet 5 per-MTok rates.
    max_run_cost_usd: float = 0.50
    price_in_per_mtok: float = 3.0
    price_out_per_mtok: float = 15.0


settings = Settings()
