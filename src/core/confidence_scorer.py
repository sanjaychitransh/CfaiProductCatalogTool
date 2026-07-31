"""
Confidence Scoring Module for Product ID Agent

Implements sophisticated confidence scoring based on:
- Match strength (exact vs fuzzy, single vs multiple products)
- Disambiguation penalties (multiple candidates, generic terms, fallback logic)
- Contextual boosts (platform keywords, session history, model numbers)

Score Range: 0.00 - 1.00 (two decimal precision)
Floor: 0.00 — score is always non-negative (penalties cannot push below zero)
"""

from typing import Dict, List, Any, Optional, Set, Tuple
import re


class ConfidenceScorer:
    """
    Calculate confidence scores for product matches based on multiple factors.
    
    Scoring Components:
    1. Base Score: Match strength (0.50 - 0.90)
    2. Penalties: Disambiguation issues (-0.10 to -0.15)
    3. Boosts: Contextual signals (+0.05 each)
    4. Cap: Maximum score of 1.00
    """
    
    # Platform/version keywords for contextual boost
    PLATFORM_KEYWORDS = {
        'z/os', 'zos', 'z os', 'z_os',  # z/OS — internal form after delimiter join
        'ibm i', 'ibm z',                # IBM i (iSeries/AS400) and IBM Z mainframe
        'cloud', 'saas', 'paas', 'iaas',
        'on-premises', 'on premises', 'onprem',
        'hybrid', 'multicloud', 'multi-cloud',
        'kubernetes', 'k8s', 'openshift',
        'linux', 'windows', 'unix', 'aix'
    }
    
    # Generic terms that reduce confidence.
    # NOTE: 'application' is intentionally EXCLUDED — it is a meaningful
    # product discriminator for IBM names such as "WebSphere Application Server",
    # "Maximo Application Suite", and "Cloud Pak for Applications".  Including it
    # would incorrectly penalise highly-specific product queries.
    GENERIC_TERMS = {
        'software', 'product', 'solution', 'system', 'tool',
        'platform', 'service', 'program',
        'suite', 'package', 'bundle', 'offering'
    }
    
    def __init__(self, session_history: Optional[Set[str]] = None):
        """
        Initialize confidence scorer.
        
        Args:
            session_history: Set of previously confirmed product codes in session
        """
        self.session_history = session_history or set()
    
    def calculate_confidence(
        self,
        match_type: str,
        match_score: float,
        query: str,
        matched_alias: str,
        product_count: int,
        candidate_count: int,
        product_code: str,
        used_fallback: bool = False
    ) -> float:
        """
        Calculate confidence score for a product match.
        
        Args:
            match_type: Type of match (exact_full, exact_phrase, fuzzy, etc.)
            match_score: Raw match score from matcher (0.0-1.0)
            query: Original search query
            matched_alias: The alias that matched
            product_count: Number of products for this match
            candidate_count: Total number of candidate products remaining
            product_code: Product code (SLC_CODE)
            used_fallback: Whether fallback token logic was used
            
        Returns:
            Confidence score (0.00-1.00) with two decimal precision.
            Always ≥ 0.00 — penalties cannot push the score below zero.
        """
        # Step 1: Determine base score from match strength
        base_score = self._get_base_score(
            match_type=match_type,
            match_score=match_score,
            matched_alias=matched_alias,
            product_count=product_count,
        )

        # Step 2: Apply disambiguation penalties
        penalties = self._calculate_penalties(
            query=query,
            matched_alias=matched_alias,
            product_count=product_count,
            candidate_count=candidate_count,
            used_fallback=used_fallback,
            match_type=match_type,
            match_score=match_score,
        )

        # Step 3: Apply contextual boosts
        boosts = self._calculate_boosts(
            query=query,
            matched_alias=matched_alias,
            product_code=product_code,
        )

        # Step 4: Calculate final score — floor at 0.00, cap at 1.00
        confidence = base_score - penalties + boosts
        confidence = max(0.00, min(confidence, 1.00))
        confidence = round(confidence, 2)

        return confidence
    
    def _get_base_score(
        self,
        match_type: str,
        match_score: float,
        matched_alias: str,
        product_count: int
    ) -> float:
        """
        Determine base score from match strength.

        Spec:
          Exact dictionary key match, single product              → 0.90
          Exact match, multiple products but long specific alias  → 0.90
          Exact match, multiple products with short alias (≤9)    → 0.75
          Fuzzy match, long alias (≥ 10 chars)                   → 0.70
          Fuzzy match, short alias (< 10 chars)                  → 0.50

        Rationale for promoting long-alias multi-product exact matches:
          Aliases with ≥10 characters are specific product phrases
          (e.g. "instana observability", "guardium data protection").
          When such an alias exactly matches the user query, the match is
          unambiguous at the alias level even if the same alias resolves to
          multiple SLC codes.  The candidate_count penalty still fires when
          there are multiple results in the set.
        """
        # ── Exact match ──────────────────────────────────────────────────
        if match_type in ['exact_full', 'exact_phrase', 'exact_phrase_ac']:
            if product_count == 1:
                return 0.90   # single product — unambiguous
            else:
                # Long aliases are specific even when shared across products
                if len(matched_alias) >= 10:
                    return 0.90
                return 0.75   # short alias shared by multiple products

        # ── Fuzzy match ──────────────────────────────────────────────────
        elif match_type in ['fuzzy', 'fuzzy_bm25', 'fuzzy_ngram']:
            # "Substring match (long key)": alias is long (≥ 10 chars) — the alias
            # is a meaningful phrase, not just a token overlap
            if len(matched_alias) >= 10:
                return 0.70
            # "Token overlap only": short alias, weaker signal
            else:
                return 0.50

        # Fallback
        return min(match_score * 0.9, 0.70)
    
    def _calculate_penalties(
        self,
        query: str,
        matched_alias: str,
        product_count: int,
        candidate_count: int,
        used_fallback: bool,
        match_type: str = "",
        match_score: float = 0.0,
    ) -> float:
        """
        Calculate disambiguation penalties.

        Spec:
          -0.10  if multiple candidate products remain AND the top match is
                 not a high-confidence exact match (score < 1.0 or short alias)
                 Rationale: when the query exactly names a specific product
                 (score=1.0, alias≥10 chars), the presence of other results
                 is expected and does not signal ambiguity.
          -0.10  if match relied on generic terms — applied only when the
                 generic terms make up >50% of ALL query tokens (not just
                 the query-alias token overlap).
          -0.15  if fallback token logic was required
                 (used_fallback = True when BM25/token index returned no
                  candidates and the matcher fell back to scanning all aliases)
        """
        total_penalty = 0.0

        # Penalty 1: multiple candidate products remain in the result set.
        # Suppressed when the match is a strong, specific exact match:
        #   - match type is exact (exact_full / exact_phrase / exact_phrase_ac)
        #   - score is 1.0 (perfect lexical match)
        #   - alias is long (≥10 chars), meaning it is specific enough not
        #     to be considered ambiguous.
        is_exact_match = match_type.startswith("exact") or match_type == "exact_full"
        is_strong_exact = (
            is_exact_match
            and match_score >= 1.0
            and len(matched_alias) >= 10
        )
        if candidate_count > 1 and not is_strong_exact:
            total_penalty += 0.10

        # Penalty 2: match relied on generic terms.
        # Now evaluated against the full query token set rather than only the
        # query-alias overlap, so that product names with one generic suffix
        # token (e.g. "suite" in "Maximo Application Suite") are not penalised
        # unless the majority of the entire query is generic noise.
        if self._contains_generic_terms(query, matched_alias):
            total_penalty += 0.10

        # Penalty 3: fallback token logic was required
        if used_fallback:
            total_penalty += 0.15

        return total_penalty
    
    def _calculate_boosts(
        self,
        query: str,
        matched_alias: str,
        product_code: str
    ) -> float:
        """
        Calculate contextual boosts.
        
        Boosts:
        - Platform/version keywords align: +0.05
        - Previously confirmed product in session: +0.05
        - Model number explicitly mentioned: +0.05
        """
        total_boost = 0.0
        
        # Boost 1: Platform/version keywords
        if self._has_platform_keywords(query, matched_alias):
            total_boost += 0.05
        
        # Boost 2: Previously confirmed in session
        if product_code in self.session_history:
            total_boost += 0.05
        
        # Boost 3: Model number mentioned
        if self._has_model_number(query):
            total_boost += 0.05
        
        return total_boost
    
    def _contains_generic_terms(self, query: str, matched_alias: str) -> bool:
        """
        Check if match relies heavily on generic terms.

        Returns True only when >50% of the FULL query token set is generic.

        Rationale: the original check measured the fraction of generic terms in
        the query-alias token overlap, which fires incorrectly for specific
        product names that contain exactly one generic suffix (e.g. "suite" in
        "Maximo Application Suite" — 2 of 3 overlap tokens are non-generic, yet
        the fraction check counted "suite" as >50% of a 2-token overlap after
        stop-words were excluded).

        Using the full query token count as the denominator means a product
        name must consist *mostly* of generic words to trigger the penalty, which
        is the intended behaviour for queries like "software solution tool".
        """
        query_lower = query.lower()
        alias_lower = matched_alias.lower()

        # All tokens in the query
        query_tokens = list(re.findall(r'\b\w+\b', query_lower))

        if not query_tokens:
            return False

        # Count generic terms in the full query
        generic_count = sum(1 for token in query_tokens if token in self.GENERIC_TERMS)

        # Penalise only when the majority of the FULL query is generic noise
        return generic_count > len(query_tokens) * 0.5
    
    def _has_platform_keywords(self, query: str, matched_alias: str) -> bool:
        """
        Check if platform/version keywords are present and aligned.
        """
        query_lower = query.lower()
        alias_lower = matched_alias.lower()
        
        # Check if any platform keyword appears in both query and alias
        for keyword in self.PLATFORM_KEYWORDS:
            if keyword in query_lower and keyword in alias_lower:
                return True
        
        return False
    
    def _has_model_number(self, query: str) -> bool:
        """
        Detect if query contains a model number pattern.
        
        Patterns:
        - IBM product codes: 5724-A12, 5655-Y04
        - Version numbers: v1.0, version 2.5
        - Model numbers: z15, z14, p9
        """
        # IBM product code pattern
        if re.search(r'\b\d{4}-[A-Z]\d{2}\b', query, re.IGNORECASE):
            return True
        
        # Version number pattern
        if re.search(r'\bv?\d+\.\d+\b', query, re.IGNORECASE):
            return True
        
        # Model number pattern (letter + digits)
        if re.search(r'\b[a-z]\d+\b', query, re.IGNORECASE):
            return True
        
        return False
    
    def add_to_session_history(self, product_code: str) -> None:
        """
        Add a confirmed product to session history.
        
        Args:
            product_code: Product code to add
        """
        self.session_history.add(product_code)
    
    def clear_session_history(self) -> None:
        """Clear session history."""
        self.session_history.clear()
    
    def explain(
        self,
        match_type: str,
        match_score: float,
        query: str,
        matched_alias: str,
        product_count: int,
        candidate_count: int,
        product_code: str,
        used_fallback: bool = False,
    ) -> Dict[str, Any]:
        """
        Return a full breakdown of the confidence calculation for a match.

        Useful for debugging, audit trails, and Swagger exploration.
        Calls the same internal helpers as ``calculate_confidence`` so the
        numbers are always consistent.

        Returns:
            Dict with keys: match_type, base_score, penalties, boosts,
            final_score, calculation (human-readable formula string).
        """
        base_score = self._get_base_score(
            match_type=match_type,
            match_score=match_score,
            matched_alias=matched_alias,
            product_count=product_count,
        )
        penalties = self._calculate_penalties(
            query=query,
            matched_alias=matched_alias,
            product_count=product_count,
            candidate_count=candidate_count,
            used_fallback=used_fallback,
            match_type=match_type,
            match_score=match_score,
        )
        boosts = self._calculate_boosts(
            query=query,
            matched_alias=matched_alias,
            product_code=product_code,
        )
        final_score = max(0.00, min(base_score - penalties + boosts, 1.00))
        final_score = round(final_score, 2)

        return {
            "match_type": match_type,
            "base_score": round(base_score, 2),
            "penalties": round(penalties, 2),
            "boosts": round(boosts, 2),
            "final_score": final_score,
            "calculation": (
                f"{base_score:.2f} - {penalties:.2f} + {boosts:.2f} "
                f"= {final_score:.2f}"
            ),
        }

    # ── Legacy alias kept for backward compatibility ──────────────────────
    def get_confidence_explanation(
        self,
        match_type: str,
        base_score: float,
        penalties: float,
        boosts: float,
        final_score: float,
    ) -> Dict[str, Any]:
        """
        Generate detailed explanation of confidence score calculation.

        .. deprecated::
            Prefer :meth:`explain` — it recomputes all components from the
            raw inputs so the values are guaranteed to be consistent.
        """
        return {
            "match_type": match_type,
            "base_score": round(base_score, 2),
            "penalties": round(penalties, 2),
            "boosts": round(boosts, 2),
            "final_score": round(final_score, 2),
            "calculation": (
                f"{base_score:.2f} - {penalties:.2f} + {boosts:.2f} "
                f"= {final_score:.2f}"
            ),
        }


# Made with Bob