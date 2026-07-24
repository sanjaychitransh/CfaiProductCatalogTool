"""
Search endpoints for product matching (primary — includes TLS intercept).

When the top-ranked result belongs to a TLS-owned product the endpoint
returns a TLSRedirectResponse instead of the normal product list.
The original behaviour without any TLS check is preserved at
/v0/products/search (see search_v0.py).
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Literal, Union
import time

from ..auth import require_token
from ..models.response import LegacySearchResponse, SearchResponse, TLSRedirectResponse

router = APIRouter(prefix="/products", tags=["Search"])

# Global instances (set by main app at startup)
matcher = None
tls_checker = None


def set_matcher(matcher_instance):
    global matcher
    matcher = matcher_instance


def set_tls_checker(tls_checker_instance):
    global tls_checker
    tls_checker = tls_checker_instance


@router.get(
    "/search",
    response_model=Union[TLSRedirectResponse, SearchResponse, LegacySearchResponse],
    dependencies=[Depends(require_token)],
    responses={
        200: {
            "description": (
                "Product search result.  When the best match is a TLS-owned product "
                "the response body is a **TLSRedirectResponse** (``tls_product: true``). "
                "Otherwise it is the standard **SearchResponse** or **LegacySearchResponse**."
            )
        }
    },
)
def search_products(
    query: str = Query(
        ...,
        description="Search query (product code, name, or description)",
        min_length=1,
        max_length=1000,
        examples=["IBM Cloud Pak for Data"]
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
    ),
):
    """
    Search for products using exact and fuzzy matching.

    **TLS Intercept:**
    If the highest-confidence result belongs to a TLS-owned product the
    endpoint returns a `TLSRedirectResponse` — no product names included.
    Use `/v0/products/search` to bypass the TLS check.

    **Matching Strategy:**
    - **Exact Match**: Full alias equality or phrase containment (score = 1.0)
    - **Fuzzy Match**: Similarity-based matching with configurable threshold

    **Response Formats (non-TLS products):**
    - **new** (default): Enhanced format with execution time, match types, confidence
    - **legacy**: Backward compatible format with support_desc, support_alias fields

    **Examples:**
    - `GET /products/search?query=AIX`  → TLSRedirectResponse
    - `GET /products/search?query=qni`  → SearchResponse
    - `GET /products/search?query=qni&format=legacy`  → LegacySearchResponse
    """
    global matcher, tls_checker

    if matcher is None:
        raise HTTPException(status_code=500, detail="Matcher not initialized")

    start_time = time.time()

    normalized_query = matcher.clean_string(query, remove_noise=True)

    results = matcher.identify_products(
        query=query,
        fuzzy_threshold=threshold,
        return_count=limit,
        fuzzy_limit=fuzzy_limit,
    )

    # TLS intercept — check top result before building the full response
    if tls_checker is not None and tls_checker.loaded:
        tls_hit = tls_checker.check_results(results)
        if tls_hit is not None:
            return tls_hit

    execution_time = (time.time() - start_time) * 1000

    if format == "legacy":
        legacy_results = [
            {
                "score": r["score"],
                "confidence": r.get("confidence", r["score"]),
                "product_code": r["product_code"],
                "support_desc": r["product_name"],
                "support_alias": r["matched_aliases"],
            }
            for r in results
        ]
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
