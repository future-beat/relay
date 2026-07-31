from pathlib import Path

from pydantic import Field
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

    # MCP server (phase 5)
    mcp_allow_writes: bool = True

    # Guardrails (phase 2). Prices default to Claude Sonnet 5 per-MTok rates.
    max_run_cost_usd: float = 0.50
    price_in_per_mtok: float = 3.0
    price_out_per_mtok: float = 15.0


settings = Settings()
