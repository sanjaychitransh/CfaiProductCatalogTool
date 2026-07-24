"""
Response models for the Product Catalog API.
"""

from pydantic import BaseModel, Field
from typing import List, Optional, Union


class ProductResult(BaseModel):
    """Single product match result."""
    score: float = Field(..., description="Raw match score from matcher (0.0-1.0)")
    confidence: float = Field(..., description="Confidence score with contextual adjustments (0.00-1.00)")
    product_code: str = Field(..., description="Unique product code (SLC_CODE)")
    product_name: Optional[str] = Field(None, description="Product name")
    matched_aliases: List[str] = Field(..., description="Aliases that matched the query")
    match_types: List[str] = Field(..., description="Types of matches (exact_full, exact_phrase, fuzzy)")


class SearchResponse(BaseModel):
    """Search API response."""
    query: str = Field(..., description="Original search query")
    normalized_query: str = Field(..., description="Cleaned/normalized query")
    results: List[ProductResult] = Field(..., description="Matched products")
    execution_time_ms: float = Field(..., description="Query execution time in milliseconds")
    result_count: int = Field(..., description="Number of results returned")


class LegacyProductResult(BaseModel):
    """Legacy API format for backward compatibility."""
    score: float
    confidence: Optional[float] = None
    product_code: str
    support_desc: Optional[str]
    support_alias: List[str]


class LegacySearchResponse(BaseModel):
    """Legacy API response format."""
    results: List[LegacyProductResult]


class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    matcher_loaded: bool
    exact_aliases: int
    fuzzy_aliases: int


class ProductInfo(BaseModel):
    """Product information."""
    product_code: str
    product_name: Optional[str]


class ProductListResponse(BaseModel):
    """Product list response."""
    count: int
    total_available: int
    products: List[ProductInfo]


class TLSRedirectResponse(BaseModel):
    """
    Returned when the top search result belongs to a TLS-owned product.

    Instead of product details the caller receives a redirect notice so
    the conversation can be handed off to the appropriate TLS agent.
    """
    tls_product: bool = Field(True, description="Always True — signals a TLS product intercept")
    message: str = Field(
        "This is a TLS product. Redirected to TLS agent.",
        description="Human-readable redirect notice"
    )
    slc_code: str = Field(..., description="Matched SLC code")
    product_name: Optional[str] = Field(None, description="TLS product name")
    assistant: str = Field(..., description="Name of the TLS assistant to redirect to")


# Made with Bob
