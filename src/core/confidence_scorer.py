"""
Confidence Scoring Module for Product ID Agent

Implements sophisticated confidence scoring based on:
- Match strength (exact vs fuzzy, single vs multiple products)
- Disambiguation penalties (multiple candidates, generic terms, fallback logic)
- Contextual boosts (platform keywords, session history, model numbers)

Score Range: 0.00 - 1.00 (two decimal precision)
"""

from typing import Dict, List, Any, Optional, Set
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
        'z/os', 'zos', 'z os',
        'cloud', 'saas', 'paas', 'iaas',
        'on-premises', 'on premises', 'onprem',
        'hybrid', 'multicloud', 'multi-cloud',
        'kubernetes', 'k8s', 'openshift',
        'linux', 'windows', 'unix', 'aix'
    }
    
    # Generic terms that reduce confidence
    GENERIC_TERMS = {
        'software', 'product', 'solution', 'system', 'tool',
        'application', 'platform', 'service', 'program',
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
            Confidence score (0.00-1.00) with two decimal precision
        """
        # Step 1: Determine base score from match strength
        base_score = self._get_base_score(
            match_type=match_type,
            match_score=match_score,
            matched_alias=matched_alias,
            product_count=product_count
        )
        
        # Step 2: Apply disambiguation penalties
        penalties = self._calculate_penalties(
            query=query,
            matched_alias=matched_alias,
            product_count=product_count,
            candidate_count=candidate_count,
            used_fallback=used_fallback
        )
        
        # Step 3: Apply contextual boosts
        boosts = self._calculate_boosts(
            query=query,
            matched_alias=matched_alias,
            product_code=product_code
        )
        
        # Step 4: Calculate final score
        confidence = base_score - penalties + boosts
        
        # Step 5: Cap at 1.00 and format to 2 decimal places
        confidence = min(confidence, 1.00)
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
        
        Base Score Guidelines:
        - Exact dictionary key match, single product: 0.90
        - Exact match, multiple products: 0.75
        - Substring match (long key): 0.70
        - Token overlap only: 0.50
        """
        # Exact match scenarios
        if match_type in ['exact_full', 'exact_phrase', 'exact_phrase_ac']:
            if product_count == 1:
                # Single product exact match
                return 0.90
            else:
                # Multiple products exact match
                return 0.75
        
        # Fuzzy match scenarios
        elif match_type in ['fuzzy', 'fuzzy_bm25', 'fuzzy_ngram']:
            # Long alias substring match (>= 10 chars)
            if len(matched_alias) >= 10 and match_score >= 0.85:
                return 0.70
            
            # Token overlap with good score
            elif match_score >= 0.80:
                return 0.60
            
            # Basic token overlap
            else:
                return 0.50
        
        # Fallback: use raw match score scaled appropriately
        return min(match_score * 0.9, 0.70)
    
    def _calculate_penalties(
        self,
        query: str,
        matched_alias: str,
        product_count: int,
        candidate_count: int,
        used_fallback: bool
    ) -> float:
        """
        Calculate disambiguation penalties.
        
        Penalties:
        - Multiple candidate products remain: -0.10
        - Match relied on generic terms: -0.10
        - Fallback token logic was required: -0.15
        """
        total_penalty = 0.0
        
        # Penalty 1: Multiple candidate products
        if candidate_count > 1:
            total_penalty += 0.10
        
        # Penalty 2: Generic terms in match
        if self._contains_generic_terms(query, matched_alias):
            total_penalty += 0.10
        
        # Penalty 3: Fallback logic used
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
        
        Returns True if >50% of matched tokens are generic.
        """
        query_lower = query.lower()
        alias_lower = matched_alias.lower()
        
        # Get tokens from both query and alias
        query_tokens = set(re.findall(r'\b\w+\b', query_lower))
        alias_tokens = set(re.findall(r'\b\w+\b', alias_lower))
        
        # Find overlapping tokens
        overlap = query_tokens & alias_tokens
        
        if not overlap:
            return False
        
        # Count generic terms in overlap
        generic_count = sum(1 for token in overlap if token in self.GENERIC_TERMS)
        
        # If more than 50% of overlap is generic, penalize
        return generic_count > len(overlap) * 0.5
    
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
    
    def get_confidence_explanation(
        self,
        match_type: str,
        base_score: float,
        penalties: float,
        boosts: float,
        final_score: float
    ) -> Dict[str, Any]:
        """
        Generate detailed explanation of confidence score calculation.
        
        Useful for debugging and transparency.
        """
        return {
            "match_type": match_type,
            "base_score": round(base_score, 2),
            "penalties": round(penalties, 2),
            "boosts": round(boosts, 2),
            "final_score": round(final_score, 2),
            "calculation": f"{base_score:.2f} - {penalties:.2f} + {boosts:.2f} = {final_score:.2f}"
        }


# Made with Bob