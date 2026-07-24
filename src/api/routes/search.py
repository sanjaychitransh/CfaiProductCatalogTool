"""
Search endpoints for product matching (primary — includes TLS intercept).

When the top-ranked result belongs to a TLS-owned product the endpoint
returns a TLSRedirectResponse instead of the normal product list.
The original behaviour without any TLS check is preserved at
/v0/products/search (see search_v0.py).
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Literal, Optional, Union
import time

from ..auth import require_token
from ..models.response import AEOAnswerResponse, LegacySearchResponse, SearchResponse, TLSRedirectResponse
from src.core.aeo_formatter import format_aeo_answer
from src.core.acronym_expander import expand_and_search, is_expansion_candidate

router = APIRouter(prefix="/products", tags=["Search"])

# Global instances (set by main app at startup)
matcher = None
tls_checker = None


def set_matcher(matcher_instance):
    """Set the global matcher instance."""
    global matcher
    matcher = matcher_instance


def set_tls_checker(tls_checker_instance):
    """Set the global TLS checker instance."""
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
                "Otherwise it is the standard **SearchResponse** (new format) or "
                "**LegacySearchResponse** (legacy format)."
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
    llm_rerank: bool = Query(
        True,
        description=(
            "Enable LLM reranking of the top-5 candidates. "
            "Falls back to BM25+RapidFuzz order if LLM is unavailable or fails."
        )
    ),
):
    """
    Search for products using exact and fuzzy matching with optional LLM reranking.

    **TLS Intercept (new in v4):**
    If the highest-confidence result belongs to a TLS-owned product the
    endpoint immediately returns a `TLSRedirectResponse` — *no* product
    names are included.  The caller should hand the conversation off to
    the named TLS assistant.

    To bypass the TLS check (e.g. for debugging or fallback), use the
    secondary endpoint at `/v0/products/search`.

    ---

    **Search Pipeline:**
    1. Normalization + Synonym/Alias Expansion
    2. BM25 + RapidFuzz → Top 10 candidates
    3. Take Top 5 → LLM Reranking
       - **Success** → reranked results (`reranked_by_llm=true`)
       - **Failure** → BM25+RapidFuzz results (`reranked_by_llm=false`)

    **Matching Strategy:**
    - **Exact Match**: Full alias equality or phrase containment (score = 1.0)
    - **Fuzzy Match**: Similarity-based matching with configurable threshold

    **Smart Heuristics:**
    - Machine codes (e.g., "5724-A12") skip fuzzy matching
    - Very short queries (< 5 chars) skip fuzzy matching
    - Exact matches always ranked higher than fuzzy matches

    **LLM Reranking:**
    - Only the Top 5 of the Top 10 BM25+RapidFuzz results are sent to the LLM
    - The LLM is constrained to return only `product_id` values from the candidate list
      (no hallucination of new codes is possible)
    - Disable with `llm_rerank=false` for lower latency

    **Response Formats (non-TLS products):**
    - **new** (default): Enhanced format with execution time, match types, confidence, etc.
    - **legacy**: Backward compatible format with support_desc, support_alias fields

    **Examples:**
    - `GET /products/search?query=AIX`  → TLSRedirectResponse (AIX is a TLS product)
    - `GET /products/search?query=qni`  → SearchResponse (standard product)
    - `GET /products/search?query=qni&format=legacy` → LegacySearchResponse
    - No LLM: `/products/search?query=qni&llm_rerank=false`
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
        enable_llm_reranking=llm_rerank,
    )

    # ── Acronym expansion fallback ─────────────────────────────────────────
    # When the main pipeline finds nothing and the query looks like an acronym
    # (short, no spaces, e.g. "cp4d"), try single-letter a–z substitutions
    # at each alphabetic position until results appear.
    # The expansion is purely additive — all existing scoring rules apply
    # unchanged to every expansion candidate.
    expansion_used: Optional[str] = None
    if not results and is_expansion_candidate(query):
        results, expansion_used = expand_and_search(
            query=query,
            identify_products_fn=matcher.identify_products,
            fuzzy_threshold=threshold,
            return_count=limit,
            fuzzy_limit=fuzzy_limit,
            enable_llm_reranking=llm_rerank,
        )

    # ------------------------------------------------------------------ #
    # TLS intercept — check top result before building the full response  #
    # ------------------------------------------------------------------ #
    if tls_checker is not None and tls_checker.loaded:
        tls_hit = tls_checker.check_results(results)
        if tls_hit is not None:
            return tls_hit   # TLSRedirectResponse payload

    # ------------------------------------------------------------------ #
    # Normal response path                                                 #
    # ------------------------------------------------------------------ #
    execution_time = (time.time() - start_time) * 1000  # Convert to ms

    # Determine whether the LLM actually reranked (all items carry the same flag)
    reranked_by_llm = bool(results and results[0].get("reranked_by_llm", False))

    if format == "legacy":
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
        response: dict = {
            "query": query,
            "normalized_query": normalized_query,
            "results": results,
            "execution_time_ms": round(execution_time, 2),
            "result_count": len(results),
            "reranked_by_llm": reranked_by_llm,
        }
        if expansion_used is not None:
            response["expansion_used"] = expansion_used
        return response


@router.get(
    "/answer",
    response_model=AEOAnswerResponse,
    dependencies=[Depends(require_token)],
    summary="AEO structured answer",
    description="""
Full pipeline: **BM25 + RapidFuzz → LLM Reranking → Evidence Validation → AEO Answer**.

Returns a single structured answer optimised for AI agent / Answer Engine consumption.

**Answer status values:**
- `DIRECT_ANSWER` — top result is CONFIRMED (confidence ≥ 0.85)
- `BEST_GUESS` — top result is CANDIDATE (confidence ≥ 0.65)
- `NEEDS_CLARIFICATION` — top result is AMBIGUOUS (confidence ≥ 0.40) with ≥ 2 candidates
- `LOW_CONFIDENCE` — top result is AMBIGUOUS with only 1 candidate
- `NO_MATCH` — nothing survived the evidence validation thresholds

**Evidence tiers** (applied to every result, read-only — confidence scores are never altered):
- `CONFIRMED` ≥ 0.85 · `CANDIDATE` ≥ 0.65 · `AMBIGUOUS` ≥ 0.40 · `REJECTED` < 0.40

The `attribution.pipeline` field lists which retrieval signals fired
(Aho-Corasick · BM25 · RapidFuzz · LLM reranking).
""",
)
def answer_products(
    query: str = Query(
        ...,
        description="Natural-language or product-code query",
        min_length=1,
        max_length=1000,
        examples=["IBM Cloud Pak for Data"],
    ),
    limit: int = Query(
        10,
        ge=1,
        le=100,
        description="Maximum pipeline candidates before evidence validation",
    ),
    threshold: float = Query(
        0.70,
        ge=0.0,
        le=1.0,
        description="Minimum fuzzy match score passed to BM25+RapidFuzz",
    ),
    fuzzy_limit: int = Query(
        30,
        ge=10,
        le=100,
        description="Maximum fuzzy candidates evaluated by RapidFuzz",
    ),
    llm_rerank: bool = Query(
        True,
        description=(
            "Enable LLM reranking of the top-5 candidates before evidence validation. "
            "Falls back to BM25+RapidFuzz order on failure."
        ),
    ),
):
    """
    Run the full pipeline and return an AEO structured answer.

    Upstream pipeline (unchanged):
      1. BM25 candidate retrieval + RapidFuzz similarity scoring
      2. ConfidenceScorer — base/penalty/boost rule-set
      3. LLM reranking of top-5 (optional)

    AEO layer (additive — reads results, never mutates scores):
      4. Evidence validation — classify each result as CONFIRMED / CANDIDATE /
         AMBIGUOUS / REJECTED using confidence score thresholds.
      5. Structured answer assembly — primary answer text, evidence list,
         attribution / provenance block.
    """
    global matcher

    if matcher is None:
        raise HTTPException(status_code=500, detail="Matcher not initialized")

    start_time = time.time()

    # ── Stage 1-3: existing pipeline (unchanged) ───────────────────────────
    normalized_query = matcher.clean_string(query, remove_noise=True)

    pipeline_results = matcher.identify_products(
        query=query,
        fuzzy_threshold=threshold,
        return_count=limit,
        fuzzy_limit=fuzzy_limit,
        enable_llm_reranking=llm_rerank,
    )

    # ── Acronym expansion fallback ─────────────────────────────────────────
    # When the main pipeline finds nothing and the query looks like an acronym
    # (short, no spaces, e.g. "cp4d"), try single-letter a–z substitutions
    # at each alphabetic position until results appear.
    # The expansion is purely additive — all existing scoring rules apply
    # unchanged to every expansion candidate.
    expansion_used: Optional[str] = None
    if not pipeline_results and is_expansion_candidate(query):
        pipeline_results, expansion_used = expand_and_search(
            query=query,
            identify_products_fn=matcher.identify_products,
            fuzzy_threshold=threshold,
            return_count=limit,
            fuzzy_limit=fuzzy_limit,
            enable_llm_reranking=llm_rerank,
        )

    execution_time_ms = (time.time() - start_time) * 1000

    reranked_by_llm = bool(
        pipeline_results and pipeline_results[0].get("reranked_by_llm", False)
    )

    # ── Stage 4-5: AEO evidence validation + answer assembly ──────────────
    aeo_answer = format_aeo_answer(
        query=query,
        normalized_query=normalized_query,
        pipeline_results=pipeline_results,
        execution_time_ms=round(execution_time_ms, 2),
        reranked_by_llm=reranked_by_llm,
    )

    # Surface the expansion that rescued a zero-result query
    if expansion_used is not None:
        aeo_answer["expansion_used"] = expansion_used

    return aeo_answer


# Made with Bob
