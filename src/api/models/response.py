"""
Response models for the Product Catalog API.
"""

from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional


class ProductResult(BaseModel):
    """Single product match result."""
    score: float = Field(..., description="Raw match score from matcher (0.0-1.0)")
    confidence: float = Field(..., description="Confidence score with contextual adjustments (0.00-1.00)")
    product_code: str = Field(..., description="Unique product code (SLC_CODE)")
    product_name: Optional[str] = Field(None, description="Product name")
    matched_aliases: List[str] = Field(..., description="Aliases that matched the query")
    match_types: List[str] = Field(..., description="Types of matches (exact_full, exact_phrase, fuzzy)")
    reranked_by_llm: bool = Field(False, description="True when the LLM reranker changed the order of this result set")


class SearchResponse(BaseModel):
    """Search API response."""
    query: str = Field(..., description="Original search query")
    normalized_query: str = Field(..., description="Cleaned/normalized query")
    results: List[ProductResult] = Field(..., description="Matched products")
    execution_time_ms: float = Field(..., description="Query execution time in milliseconds")
    result_count: int = Field(..., description="Number of results returned")
    reranked_by_llm: bool = Field(False, description="True when the LLM successfully reranked the top results")


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


# ---------------------------------------------------------------------------
# AEO (Answer Engine Optimization) response models
# ---------------------------------------------------------------------------

class AEOEvidenceItem(BaseModel):
    """A single evidence item produced by the AEO confidence validation stage."""
    product_code: Optional[str] = Field(None, description="Product code (SLC_CODE)")
    product_name: Optional[str] = Field(None, description="Product display name")
    confidence: float = Field(..., description="Confidence score (0.00–1.00) from ConfidenceScorer")
    score: float = Field(..., description="Raw retrieval score (0.00–1.00)")
    evidence_tier: str = Field(
        ...,
        description="Evidence classification: CONFIRMED (≥0.85) | CANDIDATE (≥0.65) | AMBIGUOUS (≥0.40) | REJECTED (<0.40)",
    )
    match_quality: str = Field(..., description="Human-readable one-line match quality label")
    match_types: List[str] = Field(..., description="Match type signals (exact_full, exact_phrase, fuzzy, …)")
    matched_aliases: List[str] = Field(..., description="Aliases that contributed to this match")
    reranked_by_llm: bool = Field(False, description="Whether LLM reranking affected this result set")


class AEOPrimaryAnswer(BaseModel):
    """
    The direct, attributable answer block consumed by AI agents and AEO surfaces.
    """
    text: str = Field(..., description="Natural-language answer sentence ready for agent consumption")
    product_code: Optional[str] = Field(None, description="Best-match product code")
    product_name: Optional[str] = Field(None, description="Best-match product name")
    primary_alias: Optional[str] = Field(None, description="The alias that drove the top match")
    confidence: float = Field(..., description="Confidence score of the top match (0.00–1.00)")
    evidence_tier: str = Field(..., description="Evidence tier of the top match")
    match_quality: str = Field(..., description="One-line match quality label for the top match")
    reranked_by_llm: bool = Field(False, description="True when LLM reranking succeeded on this request")


class AEOAttribution(BaseModel):
    """
    Provenance block — describes which retrieval signals fired and pipeline metrics.
    Allows downstream agents and audit tools to understand answer lineage.
    """
    pipeline: List[str] = Field(..., description="Ordered list of retrieval signals that fired")
    llm_reranked: bool = Field(False, description="Whether LLM reranking ran successfully")
    candidates_evaluated: int = Field(..., description="Number of non-rejected evidence items evaluated")
    tier_distribution: Dict[str, int] = Field(
        ..., description="Count of evidence items per tier (CONFIRMED/CANDIDATE/AMBIGUOUS)"
    )
    execution_time_ms: float = Field(..., description="Full pipeline wall-clock time in milliseconds")


class AEOAnswerResponse(BaseModel):
    """
    Full AEO structured answer response.

    Pipeline: BM25 + RapidFuzz → LLM Reranking → Evidence Validation → AEO Answer
    """
    query: str = Field(..., description="Original user query")
    normalized_query: str = Field(..., description="Noise-stripped query used for retrieval")
    status: str = Field(
        ...,
        description=(
            "Answer status: "
            "DIRECT_ANSWER (top item CONFIRMED) | "
            "BEST_GUESS (top item CANDIDATE) | "
            "NEEDS_CLARIFICATION (top item AMBIGUOUS, ≥2 candidates) | "
            "LOW_CONFIDENCE (top item AMBIGUOUS, 1 candidate) | "
            "NO_MATCH (empty results)"
        ),
    )
    answer: AEOPrimaryAnswer = Field(..., description="Primary structured answer block")
    evidence: List[AEOEvidenceItem] = Field(
        ..., description="Validated evidence items (REJECTED items excluded)"
    )
    attribution: AEOAttribution = Field(..., description="Pipeline provenance and metrics")


# Made with Bob