"""
Configuration management utilities.
"""

import os
from typing import Optional
from pydantic import BaseModel


class Settings(BaseModel):
    """Application settings."""

    # Application
    app_name: str = "Product Catalog API"
    app_version: str = "3.0.0"
    debug: bool = False

    # Matcher
    use_enhanced_matcher: bool = True
    fuzzy_threshold: float = 0.70
    default_search_limit: int = 10
    default_fuzzy_limit: int = 30

    # Security
    api_access_token: str = ""  # Comma-separated valid tokens

    # Cloudant / local CouchDB
    cloudant_url: str = ""           # CouchDB / Cloudant service URL
    cloudant_username: str = ""      # Basic Auth username (local CouchDB)
    cloudant_password: str = ""      # Basic Auth password (local CouchDB)
    cloudant_apikey: str = ""        # IAM API key (IBM Cloud hosted Cloudant)
    cloudant_db: str = "ibmproductdtool"
    cloudant_doc_id: str = "ICR_Chat_Product_Match_Dictionary"

    # LLM Reranking (watsonx — primary)
    watsonx_url: str = ""            # e.g. https://us-south.ml.cloud.ibm.com
    watsonx_apikey: str = ""         # IAM API key for watsonx
    watsonx_project_id: str = ""     # watsonx.ai project ID
    watsonx_model_id: str = "ibm/granite-13b-chat-v2"

    # LLM Reranking (OpenAI — fallback if watsonx not configured)
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    # LLM behaviour
    llm_rerank_enabled: bool = True  # Master switch; also overridable per-request
    llm_rerank_top_n: int = 5        # How many top candidates to send to the LLM
    llm_timeout_seconds: int = 15    # Hard timeout for LLM calls

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 1

    # Logging
    log_level: str = "INFO"
    
    class Config:
        env_prefix = ""  # No prefix for environment variables


def get_settings() -> Settings:
    """
    Get application settings from environment variables.
    
    Environment variables override default values:
    - USE_ENHANCED_MATCHER: Enable/disable enhanced matcher
    - LOG_LEVEL: Logging level (DEBUG, INFO, WARNING, ERROR)
    - DEFAULT_SEARCH_LIMIT: Default number of search results
    - etc.
    
    Returns:
        Settings instance
    
    Example:
        >>> from src.utils import get_settings
        >>> settings = get_settings()
        >>> print(settings.app_name)
        'Product Catalog API'
    """
    return Settings(
        use_enhanced_matcher=os.getenv("USE_ENHANCED_MATCHER", "true").lower() == "true",
        fuzzy_threshold=float(os.getenv("DEFAULT_FUZZY_THRESHOLD", "0.70")),
        default_search_limit=int(os.getenv("DEFAULT_SEARCH_LIMIT", "10")),
        default_fuzzy_limit=int(os.getenv("DEFAULT_FUZZY_LIMIT", "30")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        debug=os.getenv("DEBUG", "false").lower() == "true",
        api_access_token=os.getenv("API_ACCESS_TOKEN", ""),
        cloudant_url=os.getenv("CLOUDANT_URL", ""),
        cloudant_username=os.getenv("CLOUDANT_USERNAME", ""),
        cloudant_password=os.getenv("CLOUDANT_PASSWORD", ""),
        cloudant_apikey=os.getenv("CLOUDANT_APIKEY", ""),
        cloudant_db=os.getenv("CLOUDANT_DB", "ibmproductdtool"),
        cloudant_doc_id=os.getenv("CLOUDANT_DOC_ID", "ICR_Chat_Product_Match_Dictionary"),
        # LLM reranking — watsonx
        watsonx_url=os.getenv("WATSONX_URL", ""),
        watsonx_apikey=os.getenv("WATSONX_APIKEY", ""),
        watsonx_project_id=os.getenv("WATSONX_PROJECT_ID", ""),
        watsonx_model_id=os.getenv("WATSONX_MODEL_ID", "ibm/granite-13b-chat-v2"),
        # LLM reranking — OpenAI fallback
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        # LLM behaviour
        llm_rerank_enabled=os.getenv("LLM_RERANK_ENABLED", "true").lower() == "true",
        llm_rerank_top_n=int(os.getenv("LLM_RERANK_TOP_N", "5")),
        llm_timeout_seconds=int(os.getenv("LLM_TIMEOUT_SECONDS", "15")),
    )


# Made with Bob