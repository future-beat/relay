"""Phase 1 security perimeter: API-key tiers as FastAPI route dependencies.

These controls are route dependencies and never middleware. A StreamingResponse
locks its status line at 200 the moment the generator yields its first chunk, so
a rejection raised from middleware could only ever surface as an in-stream error
on an otherwise successful response. Dependencies resolve before the handler
returns, which is what makes 401/403/503 real HTTP status codes.
"""

import logging
import secrets
from typing import Literal

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

from .config import settings

logger = logging.getLogger("relay.auth")

Tier = Literal["owner", "demo"]

# Errors are raised here, not by the extractor, so this module owns the message
# and can distinguish a missing header from an invalid one.
_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)
_UNAUTHENTICATED = {"WWW-Authenticate": "APIKey"}  # matches FastAPI's own challenge shape


def resolve_tier(presented: str | None) -> Tier | None:
    """Constant-time tier lookup. Pure and non-raising.

    Both comparisons are evaluated unconditionally before branching: an elif on
    the comparisons themselves would leak which configured key was closer.
    """
    if not presented:
        return None
    # Starlette decodes headers as latin-1, so a codepoint > 127 would raise
    # TypeError if the constant-time check ran on str — a 500 on the auth path.
    candidate = presented.encode()
    is_owner = bool(settings.api_key) and secrets.compare_digest(
        candidate, settings.api_key.encode()
    )
    is_demo = bool(settings.demo_key) and secrets.compare_digest(
        candidate, settings.demo_key.encode()
    )
    if is_owner:
        return "owner"
    if is_demo:
        return "demo"
    return None


def require_tier(*allowed: Tier):
    """Dependency factory. 401 = unauthenticated; 403 = authenticated but not
    permitted on this surface; 503 = the deployment configured no keys at all."""

    def _dependency(presented: str | None = Security(_HEADER)) -> Tier:
        # Fail closed. A deploy that forgets `fly secrets set RELAY_API_KEY` would
        # otherwise ship a wide-open paid endpoint. Raised per request, never at
        # import or startup, so /health stays public and the container still boots.
        if not settings.api_key and not settings.demo_key:
            logger.info("auth.rejected", extra={"ctx": {"outcome": "not_configured"}})
            raise HTTPException(503, "auth is not configured on this deployment")
        tier = resolve_tier(presented)
        if tier is None:
            logger.info("auth.rejected", extra={"ctx": {"outcome": "unauthenticated"}})
            raise HTTPException(401, "missing or invalid API key", headers=_UNAUTHENTICATED)
        if tier not in allowed:
            logger.info(
                "auth.rejected",
                extra={"ctx": {"outcome": "forbidden", "tier": tier}},
            )
            raise HTTPException(403, f"the {tier} key may not use this endpoint")
        return tier

    return _dependency
