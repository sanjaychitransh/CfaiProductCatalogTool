"""
confidence_scorer.py — Product Match Confidence Scoring
=========================================================

Computes a calibrated confidence score (0.00–1.00) for each product match
produced by the hybrid matching pipeline.  The score is used by downstream
workflows to:

  - Decide whether to route automatically to an SLC without human review
  - Signal low-confidence matches for disambiguation or clarification
  - Support TLS intercept logic (alias_similarity threshold check)

Scoring model
─────────────
The confidence score is the sum of three components, clamped to [0.00, 1.00]:

    confidence = base_score − penalties + boosts

  base_score  (0.50–0.90)
    Derived from the match type and alias specificity.
    Exact dictionary match on a specific alias           → 0.90
    Fuzzy match on a long alias (≥10 chars)              → 0.70
    Fuzzy match on a short alias (< 10 chars)            → 0.50

  penalties   (0.00–0.35)
    Applied when disambiguation signals are weak:
    −0.10  Multiple candidate products in the result set
           (suppressed when a strong exact match with a long alias was found)
    −0.10  Query consists mostly (>50%) of generic category words
    −0.15  Matcher fell back to scan-all (no BM25/token signal at all)

  boosts      (0.00–0.15)
    Applied when contextual signals strengthen the match:
    +0.05  A platform/deployment keyword appears in both query and alias
           (e.g. "z/os", "cloud", "kubernetes")
    +0.05  This product was confirmed earlier in the session
    +0.05  The query explicitly mentions a model or version number

Score precision: two decimal places throughout.
Floor: 0.00 — penalties cannot make the score negative.
"""

from typing import Dict, List, Any, Optional, Set, Tuple
import re


class ConfidenceScorer:
    """
    Calculate confidence scores for product matches based on multiple factors.

    Scoring Components:
    1. Base Score: Match strength (0.50–0.90)
    2. Penalties:  Disambiguation issues (−0.10 to −0.15 each)
    3. Boosts:     Contextual signals (+0.05 each)
    4. Clamp:      Result floored at 0.00, capped at 1.00

    Public API:
        calculate_confidence(...)  → float           (used by ProductMatcher)
        explain(...)               → Dict[str, Any]  (for audit / debugging)
        add_to_session_history(product_code)
        clear_session_history()
    """

    # ── Platform/deployment keywords for contextual boost ─────────────────
    # A boost is applied when the SAME keyword appears in both the user query
    # and the matched alias, confirming that the deployment context is aligned.
    # e.g. query "db2 for z/os performance" + alias "db2 for z_os" → +0.05
    PLATFORM_KEYWORDS = {
        'z/os', 'zos', 'z os', 'z_os',   # z/OS — internal form after delimiter join
        'ibm i', 'ibm z',                  # IBM i (iSeries/AS400) and IBM Z mainframe
        'cloud', 'saas', 'paas', 'iaas',
        'on-premises', 'on premises', 'onprem',
        'hybrid', 'multicloud', 'multi-cloud',
        'kubernetes', 'k8s', 'openshift',
        'linux', 'windows', 'unix', 'aix',
    }

    # ── Generic category words that dilute confidence ──────────────────────
    # A penalty is applied when the MAJORITY (>50%) of the full query token
    # set consists of these words, indicating the query names a category
    # rather than a specific product.
    #
    # NOTE: 'application' is intentionally EXCLUDED.
    # "Application" is a meaningful discriminator in IBM product names:
    #   "WebSphere Application Server" (SAIM8)
    #   "Maximo Application Suite"     (SBIK2)
    #   "Cloud Pak for Applications"   (various)
    # Including it would incorrectly penalise highly specific product queries.
    GENERIC_TERMS = {
        'software', 'product', 'solution', 'system', 'tool',
        'platform', 'service', 'program',
        'suite', 'package', 'bundle', 'offering',
    }

    def __init__(self, session_history: Optional[Set[str]] = None):
        """
        Initialise the confidence scorer.

        Args:
            session_history:
                Set of SLC_CODE strings that have been confirmed in the
                current conversation session.  Matching a previously
                confirmed product earns a +0.05 boost.

                Note: the ConfidenceScorer instance in ProductMatcher is
                a singleton created at startup and shared across all requests.
                If per-session boosts are needed, the caller must manage the
                session_history set externally (e.g. per-request context) and
                pass it in at construction time or via add_to_session_history().
        """
        self.session_history = session_history or set()

    # =========================================================================
    # Primary public method
    # =========================================================================

    def calculate_confidence(
        self,
        match_type: str,
        match_score: float,
        query: str,
        matched_alias: str,
        product_count: int,
        candidate_count: int,
        product_code: str,
        used_fallback: bool = False,
    ) -> float:
        """
        Calculate the final confidence score for one product match.

        Orchestrates the three-component model:
            Step 1  → base_score  (_get_base_score)
            Step 2  → penalties   (_calculate_penalties)
            Step 3  → boosts      (_calculate_boosts)
            Step 4  → clamp to [0.00, 1.00] and round to 2 dp

        Args:
            match_type:
                Type of match as produced by the matcher:
                "exact_full"       — full query equals a dictionary key
                "exact_phrase"     — alias phrase found within query (AC path)
                "exact_phrase_ac"  — same, Aho-Corasick variant
                "fuzzy"            — BM25+RapidFuzz similarity match
                "fuzzy_bm25"       — legacy: same as "fuzzy" from enhanced path
                "fuzzy_ngram"      — legacy: n-gram-only path (not currently used)

            match_score:
                Raw match score from the matcher.
                1.0 for exact matches; 0.0–1.0 for fuzzy matches.

            query:
                Original (un-normalised) user query or support case text.
                Used for generic-term detection and boost evaluation.

            matched_alias:
                The dictionary alias that produced the best match for this
                product.  Used for length-based scoring and platform keyword
                alignment.

            product_count:
                Number of distinct SLC codes that the best-match alias key
                resolves to in the dictionary.  >1 signals an ambiguous alias
                (e.g. "websphere" maps to several WebSphere products).

            candidate_count:
                Total number of distinct SLC codes in the current result set.
                >1 triggers the multi-candidate penalty unless the match is
                a strong, specific exact match.

            product_code:
                The SLC_CODE for this product, used for session-history lookup.

            used_fallback:
                True when the matcher found no BM25 or token signal and fell
                back to scanning all aliases.  Triggers the −0.15 penalty.

        Returns:
            Confidence score in [0.00, 1.00] rounded to 2 decimal places.
        """
        # Step 1: Base score from match strength
        base_score = self._get_base_score(
            match_type=match_type,
            match_score=match_score,
            matched_alias=matched_alias,
            product_count=product_count,
        )

        # Step 2: Disambiguation penalties
        penalties = self._calculate_penalties(
            query=query,
            matched_alias=matched_alias,
            product_count=product_count,
            candidate_count=candidate_count,
            used_fallback=used_fallback,
            match_type=match_type,
            match_score=match_score,
        )

        # Step 3: Contextual boosts
        boosts = self._calculate_boosts(
            query=query,
            matched_alias=matched_alias,
            product_code=product_code,
        )

        # Step 4: Combine, clamp, round
        confidence = base_score - penalties + boosts
        confidence = max(0.00, min(confidence, 1.00))
        confidence = round(confidence, 2)

        return confidence

    # =========================================================================
    # Component helpers
    # =========================================================================

    def _get_base_score(
        self,
        match_type: str,
        match_score: float,
        matched_alias: str,
        product_count: int,
    ) -> float:
        """
        Determine the base score from match type and alias specificity.

        Base score table:
        ─────────────────────────────────────────────────────────────────
        Match type          Conditions                         Base score
        ─────────────────────────────────────────────────────────────────
        exact_*             product_count == 1                 0.90
        exact_*             product_count > 1,
                            alias length ≥ 10 chars            0.90
                            (long alias = specific enough)
        exact_*             product_count > 1,
                            alias length < 10 chars            0.75
        fuzzy / fuzzy_*     alias length ≥ 10 chars            0.70
        fuzzy / fuzzy_*     alias length < 10 chars            0.50
        (other)             fallback: min(score × 0.9, 0.70)
        ─────────────────────────────────────────────────────────────────

        Rationale for the long-alias multi-product rule:
        Aliases with ≥ 10 characters are specific product phrases
        (e.g. "instana observability", "guardium data protection").
        When such an alias exactly matches the user query the match is
        unambiguous at the alias level even when the alias string happens to
        resolve to more than one SLC code.  The multi-candidate penalty in
        _calculate_penalties still fires for such cases, so the total
        confidence is still reduced when the result set is genuinely ambiguous.
        """
        # ── Exact match ──────────────────────────────────────────────────
        if match_type in ('exact_full', 'exact_phrase', 'exact_phrase_ac'):
            if product_count == 1:
                return 0.90   # single product — completely unambiguous
            # Multiple products: differentiate by alias specificity.
            # A long alias (≥10 chars) is a meaningful phrase; short aliases
            # like "db2" or "mq" are inherently ambiguous.
            return 0.90 if len(matched_alias) >= 10 else 0.75

        # ── Fuzzy match ───────────────────────────────────────────────────
        elif match_type in ('fuzzy', 'fuzzy_bm25', 'fuzzy_ngram'):
            # Long alias → meaningful multi-token phrase match; higher signal.
            # Short alias → single-token or code match; lower signal.
            return 0.70 if len(matched_alias) >= 10 else 0.50

        # ── Unknown match type (future-proofing) ─────────────────────────
        # Scale the raw match score conservatively rather than returning 0.
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
        Compute the total disambiguation penalty for one product match.

        Penalty rules:
        ──────────────────────────────────────────────────────────────────────
        −0.10  Multiple candidates in the result set
               Signals that the query was ambiguous and the correct product
               has not been uniquely identified.
               SUPPRESSED when the match is a "strong exact":
                 • match_type starts with "exact"
                 • match_score == 1.0  (lexically perfect)
                 • alias length ≥ 10   (specific phrase, not a short code)
               Rationale: when a user types exactly "guardium data protection"
               (score=1.0, alias=20 chars) the presence of other Guardium
               products in the result set is expected and does NOT indicate
               ambiguity at the query level.

        −0.10  Query is majority generic terms
               Fires when >50% of all query tokens are in GENERIC_TERMS.
               Denominator is the FULL query token count, not the overlap,
               so that product names like "Maximo Application Suite" (one
               generic token "suite" out of three) do not trigger the penalty.
               Only fires for queries like "software solution tool" where
               generic words dominate.

        −0.15  Fallback scan-all was used
               Fires when the BM25 index and the token inverted index both
               returned zero candidates, forcing the matcher to scan all
               aliases.  This indicates an out-of-vocabulary query where
               the confidence in any returned match is inherently low.
        ──────────────────────────────────────────────────────────────────────
        """
        total_penalty = 0.0

        # ── Penalty 1: multiple candidates ───────────────────────────────
        # A "strong exact" match is:
        #   - an exact match type (not fuzzy)
        #   - perfect score (1.0)
        #   - long alias (≥10 chars, specific product phrase)
        # For strong exact matches, other results in the set are expected
        # (e.g. product family members) and do NOT signal ambiguity.
        is_exact_match = match_type.startswith("exact")
        is_strong_exact = (
            is_exact_match
            and match_score >= 1.0
            and len(matched_alias) >= 10
        )
        if candidate_count > 1 and not is_strong_exact:
            total_penalty += 0.10

        # ── Penalty 2: generic-term-dominated query ───────────────────────
        # Check the full query token set (not just the overlap tokens) so
        # that product names with one generic suffix are not penalised.
        if self._contains_generic_terms(query, matched_alias):
            total_penalty += 0.10

        # ── Penalty 3: fallback scan-all was required ─────────────────────
        # −0.15 signals very low confidence: the query had no BM25 or token
        # overlap with any alias; the returned match is best-effort only.
        if used_fallback:
            total_penalty += 0.15

        return total_penalty

    def _calculate_boosts(
        self,
        query: str,
        matched_alias: str,
        product_code: str,
    ) -> float:
        """
        Compute the total contextual boost for one product match.

        Boost rules (+0.05 each, independently applicable):
        ──────────────────────────────────────────────────────────────────────
        +0.05  Platform/deployment keyword alignment
               A keyword from PLATFORM_KEYWORDS appears in BOTH the query
               and the matched alias.  This confirms the deployment context
               matches, e.g. query "db2 z/os query" + alias "db2 for z_os".

        +0.05  Previously confirmed in session
               This product_code appears in session_history (confirmed by
               the user or the system in a prior turn).  Recurrence in the
               same session is a strong signal.

        +0.05  Model or version number explicitly mentioned
               The query contains a pattern matching IBM product codes
               (e.g. "5724-A12"), semantic version numbers ("v11.5"),
               or model identifiers ("z15", "p9").
        ──────────────────────────────────────────────────────────────────────
        """
        total_boost = 0.0

        # Boost 1: platform keyword alignment
        if self._has_platform_keywords(query, matched_alias):
            total_boost += 0.05

        # Boost 2: session history — previously confirmed product
        if product_code in self.session_history:
            total_boost += 0.05

        # Boost 3: explicit model/version number in query
        if self._has_model_number(query):
            total_boost += 0.05

        return total_boost

    # =========================================================================
    # Predicate helpers
    # =========================================================================

    def _contains_generic_terms(self, query: str, matched_alias: str) -> bool:
        """
        Return True when the query is dominated by generic category words.

        Threshold: >50% of the FULL query token count must be generic.

        The denominator is the full query token count (not the token overlap
        between query and alias) so that specific product names containing
        one generic suffix token do not trigger the penalty.

        Examples:
            "software solution tool"                → 3/3 generic → True  → −0.10
            "maximo application suite"              → 1/3 generic → False
            "IBM cloud pak integration software"    → 1/5 generic → False
            "product service offering"              → 3/3 generic → True  → −0.10
        """
        query_lower = query.lower()
        # Note: matched_alias is accepted as a parameter for potential future
        # use (e.g. overlap-based check) but is not currently used in this
        # implementation.  Keeping it in the signature preserves the API.
        _ = matched_alias  # unused; retained for interface stability

        # Tokenise the raw query (before normalisation) to count all words.
        query_tokens = list(re.findall(r'\b\w+\b', query_lower))
        if not query_tokens:
            return False

        generic_count = sum(1 for token in query_tokens if token in self.GENERIC_TERMS)

        # Penalty fires only when the clear majority of the query is generic noise.
        return generic_count > len(query_tokens) * 0.5

    def _has_platform_keywords(self, query: str, matched_alias: str) -> bool:
        """
        Return True when a PLATFORM_KEYWORDS entry appears in both the
        query string and the matched alias string.

        This bi-directional check ensures the boost fires only when the
        platform context is explicitly present in both the user's intent
        (query) and the matched product's alias, not just one of them.
        """
        query_lower = query.lower()
        alias_lower = matched_alias.lower()

        for keyword in self.PLATFORM_KEYWORDS:
            if keyword in query_lower and keyword in alias_lower:
                return True

        return False

    def _has_model_number(self, query: str) -> bool:
        """
        Return True when the query explicitly mentions a model or version number.

        Recognised patterns:
        ──────────────────────────────────────────────────────────────────
        Regex pattern (raw)       Example       Description
        ──────────────────────────────────────────────────────────────────
        r'\\d{4}-[A-Z]\\d{2}'    5724-A12      IBM product code
        r'v?\\d+\\.\\d+'         v11.5 / 3.2   Semantic version
        r'[a-z]\\d+'             z15, p9, k8s  Model identifier
        ──────────────────────────────────────────────────────────────────

        Note: the model-identifier pattern (r'[a-z]\\d+') is intentionally broad
        to cover IBM Z/Power hardware model names (z15, z14, p9, e980) and
        platform codes (k8s → Kubernetes, v2 → version 2).
        """
        # IBM part / product code: four digits, hyphen, letter + two digits
        if re.search(r'\b\d{4}-[A-Z]\d{2}\b', query, re.IGNORECASE):
            return True

        # Semantic version number (e.g. v11.5, 3.2, 10.0.1)
        if re.search(r'\bv?\d+\.\d+\b', query, re.IGNORECASE):
            return True

        # Model identifier: a single letter followed immediately by digits
        # (e.g. z15, z14, p9, e980, m1)
        if re.search(r'\b[a-z]\d+\b', query, re.IGNORECASE):
            return True

        return False

    # =========================================================================
    # Session management
    # =========================================================================

    def add_to_session_history(self, product_code: str) -> None:
        """
        Record that a product has been confirmed in the current session.

        Call this when the user explicitly confirms a product so that future
        queries about the same product receive the +0.05 session boost.

        Args:
            product_code: SLC_CODE of the confirmed product.
        """
        self.session_history.add(product_code)

    def clear_session_history(self) -> None:
        """
        Reset the session history.

        Call at the start of a new conversation or when a session expires.
        """
        self.session_history.clear()

    # =========================================================================
    # Explanation / audit API
    # =========================================================================

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
        Return a full breakdown of the confidence calculation.

        Calls the same internal helpers as calculate_confidence() so the
        numbers in the returned dict are guaranteed to match the actual score.
        Intended for debugging, audit trails, and Swagger/documentation use.

        Returns:
            {
              "match_type":   str,   # as passed in
              "base_score":   float, # result of _get_base_score()
              "penalties":    float, # result of _calculate_penalties()
              "boosts":       float, # result of _calculate_boosts()
              "final_score":  float, # clamped result
              "calculation":  str,   # human-readable formula string
            }
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

    def get_confidence_explanation(
        self,
        match_type: str,
        base_score: float,
        penalties: float,
        boosts: float,
        final_score: float,
    ) -> Dict[str, Any]:
        """
        Format a pre-computed confidence breakdown as a dict.

        .. deprecated::
            Prefer :meth:`explain` which re-computes all components from
            raw inputs so the values are guaranteed to be consistent.
            This method exists for backward compatibility only and will be
            removed in a future version.
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
