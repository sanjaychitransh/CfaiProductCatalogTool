"""
Configuration management utilities.
"""

import os
from pydantic import BaseModel


class Settings(BaseModel):
    """Application settings."""

    # Application
    app_name: str = "Product Catalog API"
    app_version: str = "4.0.0"
    debug: bool = False

    # Matcher
    use_enhanced_matcher: bool = True
    fuzzy_threshold: float = 0.70
    default_search_limit: int = 10
    default_fuzzy_limit: int = 30

    # Security
    api_access_token: str = ""  # Comma-separated valid tokens

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 1

    # Logging
    log_level: str = "INFO"

    class Config:
        env_prefix = ""


def get_settings() -> Settings:
    """Get application settings from environment variables."""
    return Settings(
        use_enhanced_matcher=os.getenv("USE_ENHANCED_MATCHER", "true").lower() == "true",
        fuzzy_threshold=float(os.getenv("DEFAULT_FUZZY_THRESHOLD", "0.70")),
        default_search_limit=int(os.getenv("DEFAULT_SEARCH_LIMIT", "10")),
        default_fuzzy_limit=int(os.getenv("DEFAULT_FUZZY_LIMIT", "30")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        debug=os.getenv("DEBUG", "false").lower() == "true",
        api_access_token=os.getenv("API_ACCESS_TOKEN", ""),
    )


# Made with Bob
