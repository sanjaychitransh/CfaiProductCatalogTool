"""
AEO (Answer Engine Optimization) Structured Answer Formatter

Pipeline position:
    BM25 + RapidFuzz  →  LLM Reranking  →  Evidence/Confidence Validation
                                                         ↓
                                              AEO Structured Answer

Responsibilities (additive — zero changes to upstream pipeline):
  1. Evidence Validation
     - Inspect each result produced by identify_products() + LLM reranking.
     - Classify every candidate as CONFIRMED / CANDIDATE / AMBIGUOUS / REJECTED
       using a deterministic rule-set applied to the existing confidence score
       and match_types already calculated by ConfidenceScorer and the matcher.

  2. Structured Answer Assembly
     - Build a single AEOAnswer dict that is consumed directly by the
       /products/answer API endpoint.
     - The "answer" section is intentionally formatted for AI agent / AEO
       consumption: direct, attributable, and machine-readable.

Evidence classification thresholds (read-only — never alter confidence values):
  ≥ 0.85  → CONFIRMED   (high-confidence, exact or near-exact signal)
  ≥ 0.65  → CANDIDATE   (strong fuzzy or multi-alias convergence)
  ≥ 0.40  → AMBIGUOUS   (weak signal, multiple plausible products)
  < 0.40  → REJECTED    (noise — omitted from answer, included in evidence list)

These thresholds are constants here and nowhere else.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Evidence classification thresholds
# ---------------------------------------------------------------------------
_CONFIRMED_THRESHOLD: float = 0.85
_CANDIDATE_THRESHOLD: float = 0.65
_AMBIGUOUS_THRESHOLD: float = 0.40


def _classify_evidence(confidence: float) -> str:
    """
    Map a confidence score to an evidence tier.

    Parameters
    ----------
    confidence : float
        Confidence score already computed by ConfidenceScorer (0.00–1.00).

    Returns
    -------
    str — one of: "CONFIRMED", "CANDIDATE", "AMBIGUOUS", "REJECTED"
    """
    if confidence >= _CONFIRMED_THRESHOLD:
        return "CONFIRMED"
    if confidence >= _CANDIDATE_THRESHOLD:
        return "CANDIDATE"
    if confidence >= _AMBIGUOUS_THRESHOLD:
        return "AMBIGUOUS"
    return "REJECTED"


def _match_quality_label(match_types: List[str], confidence: float) -> str:
    """
    Produce a human-readable one-line match quality summary for AEO output.

    Uses only the match_types list and confidence score — both already
    available from the upstream pipeline.
    """
    has_exact = any(mt.startswith("exact") for mt in match_types)
    if has_exact and confidence >= _CONFIRMED_THRESHOLD:
        return "Exact match — high confidence"
    if has_exact:
        return "Exact match — moderate confidence (multiple products share this alias)"
    if confidence >= _CANDIDATE_THRESHOLD:
        return "Fuzzy match — strong signal"
    if confidence >= _AMBIGUOUS_THRESHOLD:
        return "Fuzzy match — weak signal"
    return "Fuzzy match — insufficient signal"


def _build_evidence_item(result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert a single identify_products() result into an evidence item.

    No fields are mutated on the original result dict.
    """
    confidence: float = result.get("confidence", 0.0)
    match_types: List[str] = result.get("match_types", [])

    return {
        "product_code": result.get("product_code"),
        "product_name": result.get("product_name"),
        "confidence": confidence,
        "score": result.get("score", 0.0),
        "evidence_tier": _classify_evidence(confidence),
        "match_quality": _match_quality_label(match_types, confidence),
        "match_types": match_types,
        "matched_aliases": result.get("matched_aliases", []),
        "reranked_by_llm": result.get("reranked_by_llm", False),
    }


def _derive_answer_status(evidence_items: List[Dict[str, Any]]) -> str:
    """
    Derive the top-level answer status from the evidence list.

    Rules (evaluated in order — first match wins):
      DIRECT_ANSWER   — top item is CONFIRMED
      BEST_GUESS      — top item is CANDIDATE
      NEEDS_CLARIFICATION — top item is AMBIGUOUS and ≥ 2 items present
      LOW_CONFIDENCE  — top item is AMBIGUOUS with only 1 item
      NO_MATCH        — no evidence items at all
    """
    if not evidence_items:
        return "NO_MATCH"
    top_tier = evidence_items[0]["evidence_tier"]
    if top_tier == "CONFIRMED":
        return "DIRECT_ANSWER"
    if top_tier == "CANDIDATE":
        return "BEST_GUESS"
    if top_tier == "AMBIGUOUS":
        return "NEEDS_CLARIFICATION" if len(evidence_items) >= 2 else "LOW_CONFIDENCE"
    return "LOW_CONFIDENCE"


def _build_primary_answer(
    top_item: Dict[str, Any],
    query: str,
    status: str,
    reranked_by_llm: bool,
) -> Dict[str, Any]:
    """
    Build the primary answer block consumed by AI agents / AEO surfaces.

    Keeps the language factual and attribution-explicit so that language
    models re-using this response can cite the source correctly.
    """
    code = top_item.get("product_code", "")
    name = top_item.get("product_name") or code
    confidence = top_item.get("confidence", 0.0)
    tier = top_item.get("evidence_tier", "")
    quality = top_item.get("match_quality", "")
    aliases = top_item.get("matched_aliases", [])
    primary_alias = aliases[0] if aliases else query

    if status == "DIRECT_ANSWER":
        text = (
            f"The IBM product matching \"{query}\" is "
            f"**{name}** (product code: `{code}`). "
            f"Confidence: {confidence:.0%}."
        )
    elif status == "BEST_GUESS":
        text = (
            f"The most likely IBM product for \"{query}\" is "
            f"**{name}** (product code: `{code}`) with {confidence:.0%} confidence. "
            f"Verify if this matches your intent."
        )
    elif status == "NEEDS_CLARIFICATION":
        text = (
            f"Multiple IBM products could match \"{query}\". "
            f"The closest match is **{name}** (`{code}`) at {confidence:.0%} confidence — "
            f"please provide more context to narrow the result."
        )
    else:  # LOW_CONFIDENCE / NO_MATCH
        text = (
            f"No high-confidence match found for \"{query}\". "
            f"The closest candidate is **{name}** (`{code}`) at {confidence:.0%} confidence."
        ) if code else f"No IBM product found matching \"{query}\"."

    return {
        "text": text,
        "product_code": code,
        "product_name": name,
        "primary_alias": primary_alias,
        "confidence": confidence,
        "evidence_tier": tier,
        "match_quality": quality,
        "reranked_by_llm": reranked_by_llm,
    }


def _build_attribution(
    evidence_items: List[Dict[str, Any]],
    reranked_by_llm: bool,
    execution_time_ms: float,
) -> Dict[str, Any]:
    """
    Build an attribution block that describes how the answer was produced.

    This is the AEO "provenance" section — allows downstream agents and
    audit tools to understand which retrieval signals fired.
    """
    retrieval_signals: List[str] = []

    has_exact = any(
        any(mt.startswith("exact") for mt in item.get("match_types", []))
        for item in evidence_items
    )
    has_fuzzy = any(
        any("fuzzy" in mt for mt in item.get("match_types", []))
        for item in evidence_items
    )

    if has_exact:
        retrieval_signals.append("Aho-Corasick exact phrase matching")
    if has_fuzzy:
        retrieval_signals.append("BM25 candidate retrieval")
        retrieval_signals.append("RapidFuzz similarity scoring")
    if reranked_by_llm:
        retrieval_signals.append("LLM reranking (top-5 candidates)")

    tier_counts: Dict[str, int] = {}
    for item in evidence_items:
        t = item["evidence_tier"]
        tier_counts[t] = tier_counts.get(t, 0) + 1

    return {
        "pipeline": retrieval_signals,
        "llm_reranked": reranked_by_llm,
        "candidates_evaluated": len(evidence_items),
        "tier_distribution": tier_counts,
        "execution_time_ms": round(execution_time_ms, 2),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def format_aeo_answer(
    query: str,
    normalized_query: str,
    pipeline_results: List[Dict[str, Any]],
    execution_time_ms: float,
    reranked_by_llm: bool,
) -> Dict[str, Any]:
    """
    Convert the full identify_products() output into an AEO structured answer.

    This function is the **only** public surface of this module.
    It is purely transformational — it reads pipeline_results and builds
    a new dict; it never modifies the input list.

    Parameters
    ----------
    query : str
        The original user query (pre-normalization).
    normalized_query : str
        The noise-stripped query produced by matcher.clean_string().
    pipeline_results : list
        Output of ProductMatcher.identify_products() after LLM reranking.
        Each item must carry: product_code, product_name, confidence, score,
        match_types, matched_aliases, reranked_by_llm.
    execution_time_ms : float
        Wall-clock time for the full pipeline (BM25 → LLM → here).
    reranked_by_llm : bool
        Whether the LLM reranker succeeded this request.

    Returns
    -------
    dict — AEOAnswer structure (see model in src/api/models/response.py).
    """
    # Step 1 — Build evidence items (read-only view over pipeline results)
    evidence_items: List[Dict[str, Any]] = [
        _build_evidence_item(r) for r in pipeline_results
    ]

    # Step 2 — Filter out REJECTED items from the visible evidence list
    # (they remain in raw_results for full transparency)
    visible_evidence = [e for e in evidence_items if e["evidence_tier"] != "REJECTED"]

    # Step 3 — Derive top-level answer status
    status = _derive_answer_status(visible_evidence)

    # Step 4 — Build primary answer (uses the top visible evidence item)
    if visible_evidence:
        primary = _build_primary_answer(
            top_item=visible_evidence[0],
            query=query,
            status=status,
            reranked_by_llm=reranked_by_llm,
        )
    else:
        primary = {
            "text": f"No IBM product found matching \"{query}\".",
            "product_code": None,
            "product_name": None,
            "primary_alias": None,
            "confidence": 0.0,
            "evidence_tier": "REJECTED",
            "match_quality": "No match",
            "reranked_by_llm": reranked_by_llm,
        }

    # Step 5 — Attribution / provenance block
    attribution = _build_attribution(
        evidence_items=visible_evidence,
        reranked_by_llm=reranked_by_llm,
        execution_time_ms=execution_time_ms,
    )

    return {
        "query": query,
        "normalized_query": normalized_query,
        "status": status,
        "answer": primary,
        "evidence": visible_evidence,
        "attribution": attribution,
    }


# Made with Bob
