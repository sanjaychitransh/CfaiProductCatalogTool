"""
Search endpoints — v0 (secondary / fallback).

This file preserves the original search behaviour: **no TLS intercept**.
All matched products are returned exactly as before v4.0.

Available at:  GET /v0/products/search

Use this endpoint to:
- Revert to / compare against the pre-TLS behaviour
- Debug TLS routing decisions
- Support clients that need the raw product list regardless of TLS ownership
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Literal
import time

from ..auth import require_token
from ..models.response import SearchResponse, LegacySearchResponse

router = APIRouter(prefix="/v0/products", tags=["Search (v0 — no TLS check)"])

# Global matcher instance (set by main app at startup)
matcher = None


def set_matcher(matcher_instance):
    """Set the global matcher instance."""
    global matcher
    matcher = matcher_instance


@router.get("/search", response_model=SearchResponse, dependencies=[Depends(require_token)])
def search_products_v0(
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
    Search for products using exact and fuzzy matching **(no TLS check)**.

    This is the **v0 / secondary endpoint** — it behaves exactly as the
    primary `/products/search` did before v4.0.  TLS-owned products are
    **not** intercepted; all matches are returned normally.

    Use `/products/search` (primary) for production traffic.
    Use this endpoint for fallback, regression testing, or comparison.

    ---

    **Matching Strategy:**
    - **Exact Match**: Full alias equality or phrase containment (score = 1.0)
    - **Fuzzy Match**: Similarity-based matching with configurable threshold

    **Response Formats:**
    - **new** (default): Enhanced format with execution time, match types, confidence, etc.
    - **legacy**: Backward compatible format with support_desc, support_alias fields

    **Examples:**
    - `/v0/products/search?query=AIX` → returns AIX product result normally (no TLS redirect)
    - `/v0/products/search?query=qni`
    - `/v0/products/search?query=qni&format=legacy`
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

    execution_time = (time.time() - start_time) * 1000  # ms

    if format == "legacy":
        legacy_results = []
        for result in results:
            legacy_results.append({
                "score": result["score"],
                "confidence": result.get("confidence", result["score"]),
                "product_code": result["product_code"],
                "support_desc": result["product_name"],
                "support_alias": result["matched_aliases"],
            })
        return {"tls_product": False, "results": legacy_results}

    return {
        "tls_product": False,
        "query": query,
        "normalized_query": normalized_query,
        "results": results,
        "execution_time_ms": round(execution_time, 2),
        "result_count": len(results),
    }


# Made with Bob
