"""
Enhanced Product Matcher with Advanced Search Stack

Architecture:
1. Aho-Corasick: Fast exact phrase matching (O(n+m) complexity)
2. BM25: Weighted inverted index for candidate retrieval
3. RapidFuzz: Reranking of shortlisted candidates
4. N-gram: Typo tolerance and fuzzy fallback
5. SLC_CODE grouping: Aggregate results by product code
6. Confidence Scoring: Advanced confidence calculation

Performance: Optimized for 10K+ aliases with sub-100ms response times
"""

from rapidfuzz import fuzz, process
from collections import defaultdict
from typing import Dict, List, Any, Optional, Tuple, Set
import re
import math
import ahocorasick
from rank_bm25 import BM25Okapi
from .confidence_scorer import ConfidenceScorer


class EnhancedProductMatcher:
    """
    Production-grade product matcher with multi-stage search pipeline.
    
    Search Pipeline:
    1. Aho-Corasick exact phrase detection (instant)
    2. BM25 candidate retrieval (fast)
    3. RapidFuzz reranking (accurate)
    4. N-gram fallback (typo-tolerant)
    5. SLC_CODE grouping (deduplicated)
    """

    def __init__(
        self,
        match_dictionary: Dict[str, Any],
        delimiter_dict: Optional[Dict[str, str]] = None,
        enable_confidence_scoring: bool = True
    ):
        """
        Initialize enhanced matcher with all search indexes.
        
        Args:
            match_dictionary: Dict with 'exact_match' and 'fuzzy_match' sections
            delimiter_dict: Optional dict for term normalization
            enable_confidence_scoring: Enable confidence scoring for matches
        """
        self.delimiter_dict = delimiter_dict or {}
        self.match_dictionary = match_dictionary or {"exact_match": {}, "fuzzy_match": {}}
        self.enable_confidence_scoring = enable_confidence_scoring
        
        # Initialize confidence scorer
        self.confidence_scorer = ConfidenceScorer() if enable_confidence_scoring else None
        
        # Aho-Corasick automaton for exact phrase matching
        self.ac_automaton = ahocorasick.Automaton()
        self.exact_phrase_map: Dict[str, List[Dict[str, str]]] = {}
        
        # BM25 index for candidate retrieval
        self.bm25_corpus: List[str] = []
        self.bm25_routes: List[List[Dict[str, str]]] = []
        self.bm25_index: Optional[BM25Okapi] = None
        
        # N-gram index for typo tolerance
        self.ngram_index: Dict[str, Set[int]] = defaultdict(set)
        self.ngram_size = 3
        
        # Legacy token index (kept for compatibility)
        self.token_to_fuzzy_ids: Dict[str, Set[int]] = defaultdict(set)
        
        # Build all indexes
        self._build_indexes()

    def _build_indexes(self) -> None:
        """Build all search indexes at initialization."""
        exact_match = self.match_dictionary.get("exact_match", {})
        fuzzy_match = self.match_dictionary.get("fuzzy_match", {})
        
        # 1. Build Aho-Corasick automaton for exact matching
        print("Building Aho-Corasick automaton...")
        for alias, products in exact_match.items():
            norm_alias = self.clean_string(alias)
            if not norm_alias:
                continue
            
            # Add to automaton
            self.ac_automaton.add_word(norm_alias, (norm_alias, products))
            self.exact_phrase_map[norm_alias] = products
        
        # Finalize automaton (required for searching)
        self.ac_automaton.make_automaton()
        print(f"✓ Aho-Corasick: {len(self.exact_phrase_map)} exact phrases indexed")
        
        # 2. Build BM25 index for fuzzy matching
        print("Building BM25 index...")
        tokenized_corpus = []
        
        for alias, products in fuzzy_match.items():
            norm_alias = self.clean_string(alias)
            if not norm_alias:
                continue
            
            idx = len(self.bm25_corpus)
            self.bm25_corpus.append(norm_alias)
            self.bm25_routes.append(products)
            
            # Tokenize for BM25
            tokens = self.tokenize(norm_alias)
            tokenized_corpus.append(tokens)
            
            # Build token index (legacy)
            token_set = set(tokens)
            for token in token_set:
                if len(token) >= 2:
                    self.token_to_fuzzy_ids[token].add(idx)
            
            # Build n-gram index for typo tolerance
            ngrams = self._generate_ngrams(norm_alias, self.ngram_size)
            for ngram in ngrams:
                self.ngram_index[ngram].add(idx)
        
        # Initialize BM25
        if tokenized_corpus:
            self.bm25_index = BM25Okapi(tokenized_corpus)
            print(f"✓ BM25: {len(self.bm25_corpus)} documents indexed")
            print(f"✓ N-gram: {len(self.ngram_index)} {self.ngram_size}-grams indexed")
        else:
            print("⚠ Warning: No fuzzy match data for BM25 index")

    def _generate_ngrams(self, text: str, n: int) -> Set[str]:
        """
        Generate character n-grams for typo tolerance.
        
        Example: "ibm" with n=3 -> {"#ib", "ibm", "bm#"}
        """
        # Add padding for edge n-grams
        padded = f"#{text}#"
        ngrams = set()
        
        for i in range(len(padded) - n + 1):
            ngrams.add(padded[i:i+n])
        
        return ngrams

    def buffer(self, text: str) -> str:
        """Add space padding for phrase matching."""
        return f" {text} "

    def is_small_query(self, text: str, n: int = 5) -> bool:
        """Check if query is too small for fuzzy matching."""
        return len(text.strip()) <= n

    def is_machine_code(self, text: str) -> bool:
        """Detect machine/product codes (skip fuzzy for these)."""
        stripped = text.replace("-", "").replace(" ", "").replace("_", "")
        if not stripped:
            return False
        
        numeric_count = sum(c.isdigit() for c in stripped)
        return numeric_count >= len(stripped) / 2

    def replace_delimiter_terms(self, query: str) -> str:
        """Normalize multi-word terms with custom delimiters."""
        for term, delimiter in self.delimiter_dict.items():
            parts = term.split()
            if len(parts) != 2:
                continue
            
            pattern = rf"(\b|\.|\?| ){re.escape(parts[0])}( |/|-|){re.escape(parts[1])}(\b|\.|\?| )"
            replacement = rf"\g<1>{parts[0]}{delimiter}{parts[1]}\g<3>"
            query = re.sub(pattern, replacement, query, flags=re.IGNORECASE)
        
        return query

    def clean_string(self, query: Any) -> str:
        """Advanced text normalization pipeline."""
        if query is None:
            return ""
        
        if not isinstance(query, str):
            query = str(query)
        
        query = query.lower().strip()
        
        if not query:
            return ""
        
        # URL handling
        if query.startswith("http") and len(query.split()) == 1:
            query = query.split("?")[0].replace("-", " ")
            if not any(path in query for path in ["/topic/", "ibm.com/products/", "ibm.com/cloud/"]):
                return ""
        
        # Normalize possessives
        query = re.sub(r"(\w+)'s", r"\1s", query)
        
        # Keep alphanumeric + limited punctuation
        query = re.sub(r"[^a-zA-Z0-9.,;:!?#/\s-]", "", query)
        
        # Force ASCII
        query = query.encode("ascii", errors="ignore").decode()
        
        # Normalize punctuation to spaces
        query = re.sub(r"[.,;:!?#/\s-]+", " ", query)
        
        # Apply delimiter normalization
        query = self.replace_delimiter_terms(query)
        
        # Collapse spaces
        query = re.sub(r"\s+", " ", query).strip()
        
        return query

    def tokenize(self, text: str) -> List[str]:
        """Split text into tokens (min length 2)."""
        return [token for token in text.split() if len(token) >= 2]

    def exact_match_ahocorasick(
        self, 
        query: str
    ) -> List[Tuple[str, float, str, List[Dict[str, str]]]]:
        """
        Fast exact phrase matching using Aho-Corasick automaton.
        
        Complexity: O(n + m) where n=query length, m=number of matches
        Much faster than substring search for large alias sets.
        
        Returns:
            List of (alias, score, match_type, products)
        """
        query_norm = self.clean_string(query)
        matches: List[Tuple[str, float, str, List[Dict[str, str]]]] = []
        seen = set()
        
        # Find all exact phrase matches in query
        for end_index, (alias, products) in self.ac_automaton.iter(query_norm):
            if alias not in seen:
                seen.add(alias)
                matches.append((alias, 1.0, "exact_phrase_ac", products))
        
        # Sort by length (longer = more specific)
        matches.sort(key=lambda x: len(x[0]), reverse=True)
        return matches

    def bm25_retrieve(
        self, 
        query: str, 
        top_k: int = 100
    ) -> List[Tuple[int, float]]:
        """
        Retrieve top-k candidates using BM25 scoring.
        
        BM25 advantages:
        - Term frequency weighting
        - Document length normalization
        - IDF (inverse document frequency) scoring
        
        Returns:
            List of (doc_index, bm25_score) tuples
        """
        if not self.bm25_index:
            return []
        
        query_norm = self.clean_string(query)
        query_tokens = self.tokenize(query_norm)
        
        if not query_tokens:
            return []
        
        # Get BM25 scores for all documents
        scores = self.bm25_index.get_scores(query_tokens)
        
        # Get top-k candidates
        top_indices = sorted(
            range(len(scores)), 
            key=lambda i: scores[i], 
            reverse=True
        )[:top_k]
        
        return [(idx, scores[idx]) for idx in top_indices if scores[idx] > 0]

    def ngram_retrieve(
        self, 
        query: str, 
        top_k: int = 100,
        min_overlap: float = 0.3
    ) -> List[Tuple[int, float]]:
        """
        Retrieve candidates using n-gram overlap (typo tolerance).
        
        Useful for:
        - Misspellings: "ibm clod" -> "ibm cloud"
        - Character transpositions: "teh" -> "the"
        - Missing characters: "datbase" -> "database"
        
        Returns:
            List of (doc_index, overlap_score) tuples
        """
        query_norm = self.clean_string(query)
        query_ngrams = self._generate_ngrams(query_norm, self.ngram_size)
        
        if not query_ngrams:
            return []
        
        # Count n-gram overlaps
        overlap_counts: Dict[int, int] = defaultdict(int)
        
        for ngram in query_ngrams:
            for doc_idx in self.ngram_index.get(ngram, set()):
                overlap_counts[doc_idx] += 1
        
        # Calculate overlap scores (Jaccard-like)
        scored_candidates = []
        for doc_idx, overlap_count in overlap_counts.items():
            doc_text = self.bm25_corpus[doc_idx]
            doc_ngrams = self._generate_ngrams(doc_text, self.ngram_size)
            
            # Jaccard similarity
            union_size = len(query_ngrams | doc_ngrams)
            if union_size > 0:
                score = overlap_count / union_size
                if score >= min_overlap:
                    scored_candidates.append((doc_idx, score))
        
        # Sort by score
        scored_candidates.sort(key=lambda x: x[1], reverse=True)
        return scored_candidates[:top_k]

    def fuzzy_match_enhanced(
        self,
        query: str,
        threshold: float = 0.70,
        limit: int = 30,
        use_bm25: bool = True,
        use_ngram_fallback: bool = True
    ) -> List[Tuple[str, float, str, List[Dict[str, str]]]]:
        """
        Enhanced fuzzy matching with multi-stage pipeline:
        
        1. BM25 candidate retrieval (fast, weighted)
        2. RapidFuzz reranking (accurate)
        3. N-gram fallback (typo-tolerant)
        
        Args:
            query: Search query
            threshold: Minimum similarity score
            limit: Maximum results
            use_bm25: Use BM25 for candidate retrieval
            use_ngram_fallback: Use n-gram if BM25 fails
            
        Returns:
            List of (alias, score, match_type, products)
        """
        query_norm = self.clean_string(query)
        
        # Stage 1: BM25 candidate retrieval
        candidates = []
        if use_bm25 and self.bm25_index:
            bm25_candidates = self.bm25_retrieve(query_norm, top_k=100)
            candidates = [(idx, self.bm25_corpus[idx]) for idx, _ in bm25_candidates]
        
        # Stage 2: N-gram fallback if BM25 returns few results
        if use_ngram_fallback and len(candidates) < 20:
            ngram_candidates = self.ngram_retrieve(query_norm, top_k=100)
            ngram_set = {idx for idx, _ in ngram_candidates}
            existing_set = {idx for idx, _ in candidates}
            
            # Add new candidates from n-gram
            for idx, _ in ngram_candidates:
                if idx not in existing_set:
                    candidates.append((idx, self.bm25_corpus[idx]))
        
        if not candidates:
            return []
        
        # Stage 3: RapidFuzz reranking
        candidate_texts = [text for _, text in candidates]
        candidate_map = {text: idx for idx, text in candidates}
        
        extracted = process.extract(
            query_norm,
            candidate_texts,
            scorer=fuzz.token_sort_ratio,  # Better for word order variations
            score_cutoff=int(threshold * 100),
            limit=limit
        )
        
        # Format results
        matches = []
        for alias, score, _ in extracted:
            idx = candidate_map[alias]
            match_type = "fuzzy_bm25" if use_bm25 else "fuzzy_ngram"
            matches.append((alias, score / 100.0, match_type, self.bm25_routes[idx]))
        
        matches.sort(key=lambda x: (x[1], len(x[0])), reverse=True)
        return matches

    def get_all_matches(
        self,
        query: str,
        fuzzy_threshold: float = 0.70,
        fuzzy_limit: int = 30
    ) -> List[Tuple[str, float, str, List[Dict[str, str]]]]:
        """
        Combined search with smart heuristics.
        
        Pipeline:
        1. Aho-Corasick exact matching (always)
        2. BM25 + RapidFuzz fuzzy matching (conditional)
        3. N-gram fallback (if needed)
        
        Returns:
            Ranked list of all matches
        """
        query_norm = self.clean_string(query)
        
        # Stage 1: Exact matching with Aho-Corasick
        matches = self.exact_match_ahocorasick(query_norm)
        
        # Stage 2: Fuzzy matching (conditional)
        should_fuzzy = (
            not self.is_machine_code(query_norm) and 
            not self.is_small_query(query_norm)
        )
        
        if should_fuzzy:
            fuzzy_matches = self.fuzzy_match_enhanced(
                query_norm, 
                threshold=fuzzy_threshold, 
                limit=fuzzy_limit,
                use_bm25=True,
                use_ngram_fallback=True
            )
            matches.extend(fuzzy_matches)
        
        # Ranking: exact > fuzzy, then by score, then by length
        def rank_key(item):
            alias, score, match_type, _ = item
            exact_priority = 1 if "exact" in match_type else 0
            return (exact_priority, score, len(alias))
        
        matches.sort(key=rank_key, reverse=True)
        return matches

    def identify_products(
        self,
        query: str,
        fuzzy_threshold: float = 0.70,
        return_count: int = 10,
        fuzzy_limit: int = 30,
        char_limit: int = 1000
    ) -> List[Dict[str, Any]]:
        """
        Main API: Identify products with SLC_CODE grouping and confidence scoring.
        
        Process:
        1. Get all matches (exact + fuzzy)
        2. Group by SLC_CODE
        3. Aggregate scores (max), aliases (list), match types (set)
        4. Calculate confidence scores if enabled
        5. Rank and return top N
        
        Returns:
            List of product dicts grouped by SLC_CODE with confidence scores
        """
        query = str(query)[:char_limit]
        
        # Get all matches
        matches = self.get_all_matches(
            query=query,
            fuzzy_threshold=fuzzy_threshold,
            fuzzy_limit=fuzzy_limit
        )
        
        # Group by SLC_CODE
        grouped: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            "score": 0.0,
            "product_code": None,
            "product_name": None,
            "matched_aliases": [],
            "match_types": set(),
            "best_match_alias": None,
            "best_match_type": None
        })
        
        for alias, score, match_type, products in matches:
            for product in products:
                code = product.get("SLC_CODE")
                name = product.get("PRODUCT_NAME")
                
                if not code:
                    continue
                
                item = grouped[code]
                item["product_code"] = code
                item["product_name"] = name
                
                # Track best match for confidence calculation
                if score > item["score"]:
                    item["score"] = round(score, 6)
                    item["best_match_alias"] = alias
                    item["best_match_type"] = match_type
                
                if alias not in item["matched_aliases"]:
                    item["matched_aliases"].append(alias)
                
                item["match_types"].add(match_type)
        
        # Convert to list and calculate confidence scores
        results = []
        total_candidates = len(grouped)
        
        for _, item in grouped.items():
            item["match_types"] = sorted(list(item["match_types"]))
            
            # Calculate confidence score if enabled
            if self.enable_confidence_scoring and self.confidence_scorer:
                # Determine if fallback was used (ngram or low score fuzzy)
                used_fallback = (
                    "ngram" in item["best_match_type"] or
                    ("fuzzy" in item["best_match_type"] and item["score"] < 0.75)
                )
                
                confidence = self.confidence_scorer.calculate_confidence(
                    match_type=item["best_match_type"],
                    match_score=item["score"],
                    query=query,
                    matched_alias=item["best_match_alias"],
                    product_count=len(item["matched_aliases"]),
                    candidate_count=total_candidates,
                    product_code=item["product_code"],
                    used_fallback=used_fallback
                )
                item["confidence"] = confidence
            else:
                # Fallback: use match score as confidence
                item["confidence"] = round(item["score"], 2)
            
            # Clean up temporary fields
            del item["best_match_alias"]
            del item["best_match_type"]
            
            results.append(item)
        
        # Final ranking: exact > confidence > score > alias count
        def result_rank(item):
            has_exact = any("exact" in mt for mt in item["match_types"])
            exact_priority = 1 if has_exact else 0
            return (exact_priority, item["confidence"], item["score"], len(item["matched_aliases"]))
        
        results.sort(key=result_rank, reverse=True)
        return results[:return_count]

    def get_stats(self) -> Dict[str, Any]:
        """Get detailed statistics about the matcher."""
        return {
            "exact_match": {
                "aho_corasick_phrases": len(self.exact_phrase_map),
            },
            "fuzzy_match": {
                "bm25_documents": len(self.bm25_corpus),
                "ngram_index_size": len(self.ngram_index),
                "ngram_size": self.ngram_size,
                "token_index_size": len(self.token_to_fuzzy_ids)
            },
            "search_pipeline": [
                "1. Aho-Corasick (exact phrases)",
                "2. BM25 (candidate retrieval)",
                "3. RapidFuzz (reranking)",
                "4. N-gram (typo fallback)",
                "5. SLC_CODE grouping"
            ]
        }


# Made with Bob