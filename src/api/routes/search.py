"""
Search endpoints for product matching.
"""

from fastapi import APIRouter, HTTPException, Query
from typing import Literal
import time

from ..models.response import SearchResponse, LegacySearchResponse

router = APIRouter(prefix="/products", tags=["Search"])

# Global matcher instance (will be set by main app)
matcher = None


def set_matcher(matcher_instance):
    """Set the global matcher instance."""
    global matcher
    matcher = matcher_instance


@router.get("/search", response_model=SearchResponse)
def search_products(
    query: str = Query(
        ...,
        description="Search query (product code, name, or description)",
        min_length=1,
        max_length=1000,
        example="IBM Cloud Pak for Data"
    ),
    limit: int = Query(
        10,
        ge=1,
        le=100,
        description="Maximum number of results to return"
    ),
    threshold: float = Query(
        0.70,
        ge=0.0,
        le=1.0,
        description="Minimum fuzzy match score (0.0-1.0)"
    ),
    fuzzy_limit: int = Query(
        30,
        ge=10,
        le=100,
        description="Maximum fuzzy candidates to evaluate"
    ),
    format: Literal["new", "legacy"] = Query(
        "new",
        description="Response format: 'new' (enhanced) or 'legacy' (backward compatible)"
    )
):
    """
    Search for products using exact and fuzzy matching.
    
    **Matching Strategy:**
    - **Exact Match**: Full alias equality or phrase containment (score = 1.0)
    - **Fuzzy Match**: Similarity-based matching with configurable threshold
    
    **Smart Heuristics:**
    - Machine codes (e.g., "5724-A12") skip fuzzy matching
    - Very short queries (< 5 chars) skip fuzzy matching
    - Exact matches always ranked higher than fuzzy matches
    
    **Response Formats:**
    - **new** (default): Enhanced format with execution time, match types, query info
    - **legacy**: Backward compatible format with support_desc, support_alias fields
    
    **Performance:**
    - Token-based candidate filtering for fast fuzzy matching
    - Inverted index for O(1) exact lookups
    - Optimized for large dictionaries (10K+ aliases)
    
    **Examples:**
    - New format: `/products/search?query=qni` or `/products/search?query=qni&format=new`
    - Legacy format: `/products/search?query=qni&format=legacy`
    """
    global matcher
    
    if matcher is None:
        raise HTTPException(status_code=500, detail="Matcher not initialized")
    
    start_time = time.time()
    
    normalized_query = matcher.clean_string(query)
    
    results = matcher.identify_products(
        query=query,
        fuzzy_threshold=threshold,
        return_count=limit,
        fuzzy_limit=fuzzy_limit
    )
    
    execution_time = (time.time() - start_time) * 1000  # Convert to ms
    
    # Return format based on parameter
    if format == "legacy":
        # Legacy format: support_desc, support_alias (with optional confidence)
        legacy_results = []
        for result in results:
            legacy_results.append({
                "score": result["score"],
                "confidence": result.get("confidence", result["score"]),
                "product_code": result["product_code"],
                "support_desc": result["product_name"],
                "support_alias": result["matched_aliases"]
            })
        return {"results": legacy_results}
    else:
        # New format: enhanced with execution time, match types, confidence, etc.
        return {
            "query": query,
            "normalized_query": normalized_query,
            "results": results,
            "execution_time_ms": round(execution_time, 2),
            "result_count": len(results)
        }


# Made with Bob