"""
API authentication — Bearer token dependency.

Token(s) are loaded from the API_ACCESS_TOKEN environment variable.
Multiple tokens may be separated by commas, allowing per-consumer
tokens that can be rotated or revoked individually.

Usage:
    from .auth import require_token
    @router.get("/endpoint", dependencies=[Depends(require_token)])
"""

import os

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer(auto_error=True)


def _get_valid_tokens() -> set:
    """Read valid tokens from environment variable."""
    raw = os.getenv("API_ACCESS_TOKEN", "")
    return {t.strip() for t in raw.split(",") if t.strip()}


def require_token(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> None:
    """
    FastAPI dependency — validates the Bearer token in the Authorization header.

    Raises:
        503: If API_ACCESS_TOKEN env var is not configured (fail-safe).
        401: If the supplied token is missing or does not match.
    """
    valid = _get_valid_tokens()

    if not valid:
        # No token configured → deny all requests (fail-safe, never open)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication not configured on this server",
        )

    if creds.credentials not in valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing access token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# Made with Bob
