from rapidfuzz import fuzz, process
from collections import defaultdict
from typing import Dict, List, Any, Optional, Tuple, Set, TypedDict
import re
from .confidence_scorer import ConfidenceScorer

# Optional enhanced imports - graceful fallback if not available
try:
    import ahocorasick
    from rank_bm25 import BM25Okapi
    ENHANCED_AVAILABLE = True
except ImportError:
    ENHANCED_AVAILABLE = False
    ahocorasick = None
    BM25Okapi = None


class ProductGroup(TypedDict):
    """Type definition for grouped product results."""
    score: float
    product_code: Optional[str]
    product_name: Optional[str]
    matched_aliases: List[str]
    match_types: Set[str]


class ProductMatcher:
    """
    High-performance product matcher with optional enhanced features.
    
    Features:
    - Exact match (full equality + phrase containment)
    - Fuzzy match with candidate pre-filtering
    - Optional Aho-Corasick for faster exact matching
    - Optional BM25 for weighted candidate retrieval
    - Optional N-gram index for typo tolerance
    - Advanced text normalization with delimiter handling
    - Machine code detection to skip fuzzy matching
    - Token-based inverted index for fast candidate retrieval
    - Grouped results by product code with score aggregation
    """

    def __init__(
        self,
        match_dictionary: Dict[str, Any],
        delimiter_dict: Optional[Dict[str, str]] = None,
        use_enhanced: bool = True,
        enable_confidence_scoring: bool = True
    ):
        """
        Initialize the matcher with dictionaries.
        
        Args:
            match_dictionary: Dict with 'exact_match' and 'fuzzy_match' sections
            delimiter_dict: Optional dict for term normalization (e.g., {'tcp ip': '_'})
            use_enhanced: Enable enhanced features (Aho-Corasick, BM25, N-gram) if available
            enable_confidence_scoring: Enable confidence scoring for matches
        """
        self.delimiter_dict = delimiter_dict or {}
        self.match_dictionary = match_dictionary or {"exact_match": {}, "fuzzy_match": {}}
        self.use_enhanced = use_enhanced and ENHANCED_AVAILABLE
        self.enable_confidence_scoring = enable_confidence_scoring
        
        # Initialize confidence scorer
        self.confidence_scorer = ConfidenceScorer() if enable_confidence_scoring else None
        
        # Exact matching structures
        self.exact_index: Dict[str, List[Dict[str, str]]] = {}
        self.exact_phrases: List[Tuple[str, List[Dict[str, str]]]] = []
        
        # Fuzzy matching structures
        self.fuzzy_aliases: List[str] = []
        self.fuzzy_routes: List[List[Dict[str, str]]] = []
        
        # Inverted token index for fast candidate filtering
        self.token_to_fuzzy_ids: Dict[str, Set[int]] = defaultdict(set)
        
        # Enhanced features (optional)
        self.ac_automaton = None
        self.bm25_index = None
        self.ngram_index: Dict[str, Set[int]] = defaultdict(set)
        self.ngram_size = 3
        
        # Build all indexes at initialization
        self._build_indexes()

    def _build_indexes(self) -> None:
        """
        Build lookup indexes once during startup for O(1) exact lookups
        and fast fuzzy candidate filtering.
        """
        exact_match = self.match_dictionary.get("exact_match", {})
        fuzzy_match = self.match_dictionary.get("fuzzy_match", {})
        
        # Build exact match indexes
        for alias, products in exact_match.items():
            norm_alias = self.clean_string(alias)
            if not norm_alias:
                continue
            
            # Full match index
            self.exact_index[norm_alias] = products
            
            # Phrase containment index (sorted by length for specificity)
            self.exact_phrases.append((norm_alias, products))
        
        # Sort exact phrases by length (longer = more specific)
        self.exact_phrases.sort(key=lambda x: len(x[0]), reverse=True)
        
        # Build Aho-Corasick automaton if enhanced mode enabled
        if self.use_enhanced and ahocorasick:
            self.ac_automaton = ahocorasick.Automaton()
            for alias, products in exact_match.items():
                norm_alias = self.clean_string(alias)
                if norm_alias:
                    self.ac_automaton.add_word(norm_alias, (norm_alias, products))
            self.ac_automaton.make_automaton()
        
        # Build fuzzy match indexes with token inverted index
        tokenized_corpus = []
        for alias, products in fuzzy_match.items():
            norm_alias = self.clean_string(alias)
            if not norm_alias:
                continue
            
            idx = len(self.fuzzy_aliases)
            self.fuzzy_aliases.append(norm_alias)
            self.fuzzy_routes.append(products)
            
            # Tokenize for BM25
            tokens = self.tokenize(norm_alias)
            tokenized_corpus.append(tokens)
            
            # Build inverted index: token -> alias IDs
            token_set = set(tokens)
            for token in token_set:
                if len(token) >= 2:  # Skip single chars
                    self.token_to_fuzzy_ids[token].add(idx)
            
            # Build n-gram index if enhanced mode enabled
            if self.use_enhanced:
                ngrams = self._generate_ngrams(norm_alias, self.ngram_size)
                for ngram in ngrams:
                    self.ngram_index[ngram].add(idx)
        
        # Initialize BM25 if enhanced mode enabled
        if self.use_enhanced and BM25Okapi and tokenized_corpus:
            self.bm25_index = BM25Okapi(tokenized_corpus)

    def buffer(self, text: str) -> str:
        """Add space padding for phrase matching."""
        return f" {text} "

    def is_small_query(self, text: str, n: int = 5) -> bool:
        """Check if query is too small for fuzzy matching."""
        return len(text.strip()) <= n

    def is_machine_code(self, text: str) -> bool:
        """
        Detect machine/product codes (alphanumeric with >50% digits).
        Skip fuzzy matching for these to avoid false positives.
        """
        stripped = text.replace("-", "").replace(" ", "").replace("_", "")
        if not stripped:
            return False
        
        numeric_count = sum(c.isdigit() for c in stripped)
        return numeric_count >= len(stripped) / 2

    def replace_delimiter_terms(self, query: str) -> str:
        """
        Normalize multi-word terms with custom delimiters.
        
        Example:
            'tcp ip' -> 'tcp_ip'
            'cloud pak' -> 'cloud_pak'
            'check sorter' -> 'check_sorter'
        """
        for term, delimiter in self.delimiter_dict.items():
            parts = term.split()
            if len(parts) != 2:
                continue
            
            # Match with flexible separators: space, hyphen, slash
            pattern = rf"(\b|\.|\?| ){re.escape(parts[0])}( |/|-|){re.escape(parts[1])}(\b|\.|\?| )"
            replacement = rf"\g<1>{parts[0]}{delimiter}{parts[1]}\g<3>"
            query = re.sub(pattern, replacement, query, flags=re.IGNORECASE)
        
        return query

    def clean_string(self, query: Any) -> str:
        """
        Advanced text normalization pipeline:
        1. Handle None/empty/non-string inputs
        2. URL extraction and validation
        3. Possessive form normalization
        4. Special character removal
        5. ASCII encoding
        6. Delimiter term normalization
        7. Whitespace collapse
        """
        if query is None:
            return ""
        
        if not isinstance(query, str):
            query = str(query)
        
        query = query.lower().strip()
        
        if not query:
            return ""
        
        # URL handling: extract product info from IBM URLs
        if query.startswith("http") and len(query.split()) == 1:
            query = query.split("?")[0].replace("-", " ")
            
            # Only keep valid IBM product URLs
            if not any(path in query for path in ["/topic/", "ibm.com/products/", "ibm.com/cloud/"]):
                return ""
        
        # Normalize possessive forms: "IBM's" -> "IBMs"
        query = re.sub(r"(\w+)'s", r"\1s", query)
        
        # Keep alphanumeric + limited punctuation
        query = re.sub(r"[^a-zA-Z0-9.,;:!?#/\s-]", "", query)
        
        # Force ASCII encoding (remove accents, special chars)
        query = query.encode("ascii", errors="ignore").decode()
        
        # Normalize all punctuation/separators to spaces (including hyphens)
        query = re.sub(r"[.,;:!?#/\s-]+", " ", query)
        
        # Apply custom delimiter normalization
        query = self.replace_delimiter_terms(query)
        
        # Collapse multiple spaces
        query = re.sub(r"\s+", " ", query).strip()
        
        return query

    def tokenize(self, text: str) -> List[str]:
        """Split text into tokens (min length 2 for indexing)."""
        return [token for token in text.split() if len(token) >= 2]
    def _generate_ngrams(self, text: str, n: int) -> Set[str]:
        """
        Generate character n-grams for typo tolerance.
        
        Example: "ibm" with n=3 -> {"#ib", "ibm", "bm#"}
        """
        padded = f"#{text}#"
        ngrams = set()
        for i in range(len(padded) - n + 1):
            ngrams.add(padded[i:i+n])
        return ngrams


    def exact_match(self, query: str) -> List[Tuple[str, float, str, List[Dict[str, str]]]]:
        """
        Perform exact matching with Aho-Corasick (if available) or fallback strategies.
        Filters out short matches that don't have word boundaries.
        
        Returns:
            List of (alias, score, match_type, products)
        """
        query_norm = self.clean_string(query)
        query_buf = self.buffer(query_norm)
        matches: List[Tuple[str, float, str, List[Dict[str, str]]]] = []
        seen = set()
        
        # Use Aho-Corasick if available (much faster)
        if self.use_enhanced and self.ac_automaton:
            for end_index, (alias, products) in self.ac_automaton.iter(query_norm):
                if alias not in seen:
                    # For short aliases (<=4 chars), require word boundaries
                    # This prevents "pda" from matching in "update" or "c" in "controller"
                    if len(alias) <= 4:
                        # Check if alias appears as a complete word (with spaces around it)
                        alias_buf = self.buffer(alias)
                        if alias_buf not in query_buf:
                            continue
                    
                    seen.add(alias)
                    matches.append((alias, 1.0, "exact_phrase", products))
        else:
            # Fallback to original logic with word boundary check
            # Strategy 1: Full exact match (O(1))
            if query_norm in self.exact_index:
                matches.append((query_norm, 1.0, "exact_full", self.exact_index[query_norm]))
                seen.add(query_norm)
            
            # Strategy 2: Phrase containment (alias in query)
            for alias, products in self.exact_phrases:
                if alias not in seen:
                    # For short aliases, require word boundaries
                    if len(alias) <= 4:
                        alias_buf = self.buffer(alias)
                        if alias_buf not in query_buf:
                            continue
                    elif self.buffer(alias) not in query_buf:
                        continue
                    
                    seen.add(alias)
                    matches.append((alias, 1.0, "exact_phrase", products))
        
        # Sort by length (longer = more specific)
        matches.sort(key=lambda x: len(x[0]), reverse=True)
        return matches

    def get_fuzzy_candidates(
        self,
        query_norm: str,
        max_candidates: int = 500
    ) -> List[Tuple[int, str]]:
        """
        Fast candidate filtering using BM25 (if available) or token overlap.
        
        Args:
            query_norm: Normalized query string
            max_candidates: Maximum candidates to return
            
        Returns:
            List of (alias_index, alias_text) tuples
        """
        # Use BM25 if available and enabled
        if self.use_enhanced and self.bm25_index:
            query_tokens = self.tokenize(query_norm)
            if query_tokens:
                scores = self.bm25_index.get_scores(query_tokens)
                top_indices = sorted(
                    range(len(scores)),
                    key=lambda i: scores[i],
                    reverse=True
                )[:max_candidates]
                candidates = [(idx, self.fuzzy_aliases[idx]) for idx in top_indices if scores[idx] > 0]
                
                # Add n-gram candidates if BM25 returns few results
                if len(candidates) < 20 and self.ngram_index:
                    ngram_candidates = self._get_ngram_candidates(query_norm, max_candidates)
                    existing_ids = {idx for idx, _ in candidates}
                    for idx, text in ngram_candidates:
                        if idx not in existing_ids:
                            candidates.append((idx, text))
                
                return candidates[:max_candidates]
        
        # Fallback to token overlap
        tokens = self.tokenize(query_norm)
        candidate_ids = set()
        
        # Find all aliases that share at least one token
        for token in tokens:
            candidate_ids.update(self.token_to_fuzzy_ids.get(token, set()))
        
        # Fallback: if no token overlap, use first N aliases
        if not candidate_ids:
            fallback_count = min(max_candidates, len(self.fuzzy_aliases))
            return [(i, self.fuzzy_aliases[i]) for i in range(fallback_count)]
        
        candidates = [(i, self.fuzzy_aliases[i]) for i in candidate_ids]
        
        # Prefer longer aliases (more specific)
        candidates.sort(key=lambda x: len(x[1]), reverse=True)
        return candidates[:max_candidates]

    def _get_ngram_candidates(
        self,
        query_norm: str,
        top_k: int = 100,
        min_overlap: float = 0.3
    ) -> List[Tuple[int, str]]:
        """
        Retrieve candidates using n-gram overlap (typo tolerance).
        
        Returns:
            List of (doc_index, alias_text) tuples
        """
        query_ngrams = self._generate_ngrams(query_norm, self.ngram_size)
        if not query_ngrams:
            return []
        
        # Count n-gram overlaps
        overlap_counts: Dict[int, int] = defaultdict(int)
        for ngram in query_ngrams:
            for doc_idx in self.ngram_index.get(ngram, set()):
                overlap_counts[doc_idx] += 1
        
        # Calculate overlap scores
        scored_candidates = []
        for doc_idx, overlap_count in overlap_counts.items():
            doc_text = self.fuzzy_aliases[doc_idx]
            doc_ngrams = self._generate_ngrams(doc_text, self.ngram_size)
            
            # Jaccard similarity
            union_size = len(query_ngrams | doc_ngrams)
            if union_size > 0:
                score = overlap_count / union_size
                if score >= min_overlap:
                    scored_candidates.append((doc_idx, doc_text))
        
        return scored_candidates[:top_k]

    def fuzzy_match(
        self,
        query: str,
        threshold: float = 0.70,
        limit: int = 30,
        scorer=fuzz.partial_ratio
    ) -> List[Tuple[str, float, str, List[Dict[str, str]]]]:
        """
        Fuzzy matching with candidate pre-filtering.
        
        Performance optimization:
        - Only fuzzy match against pre-filtered candidates (token overlap)
        - Use rapidfuzz's optimized C++ implementation
        - Configurable scorer (partial_ratio, token_sort_ratio, etc.)
        
        Args:
            query: Raw query string
            threshold: Minimum similarity score (0.0-1.0)
            limit: Maximum matches to return
            scorer: Fuzzy matching algorithm
            
        Returns:
            List of (alias, score, match_type, products)
        """
        query_norm = self.clean_string(query)
        
        # Get pre-filtered candidates
        candidates = self.get_fuzzy_candidates(query_norm)
        
        if not candidates:
            return []
        
        # Extract candidate texts and build reverse lookup
        candidate_texts = [candidate_text for _, candidate_text in candidates]
        candidate_map = {candidate_text: idx for idx, candidate_text in candidates}
        
        # Perform fuzzy matching (rapidfuzz is highly optimized)
        extracted = process.extract(
            query_norm,
            candidate_texts,
            scorer=scorer,
            score_cutoff=int(threshold * 100),  # rapidfuzz uses 0-100 scale
            limit=limit
        )
        
        # Convert to standard format
        matches = []
        for alias, score, _ in extracted:
            idx = candidate_map[alias]
            matches.append((alias, score / 100.0, "fuzzy", self.fuzzy_routes[idx]))
        
        # Sort by score, then length
        matches.sort(key=lambda x: (x[1], len(x[0])), reverse=True)
        return matches

    def get_all_matches(
        self,
        query: str,
        fuzzy_threshold: float = 0.70,
        fuzzy_limit: int = 30
    ) -> List[Tuple[str, float, str, List[Dict[str, str]]]]:
        """
        Combine exact and fuzzy matching with smart heuristics.
        
        Logic:
        1. Always perform exact matching
        2. Skip fuzzy for machine codes (e.g., "5724-A12")
        3. Skip fuzzy for very short queries (< 5 chars)
        4. Rank exact matches higher than fuzzy
        
        Returns:
            Combined and ranked list of matches
        """
        query_norm = self.clean_string(query)
        
        # Always do exact matching
        matches = self.exact_match(query_norm)
        
        # Conditionally add fuzzy matching
        should_fuzzy = (
            not self.is_machine_code(query_norm) and 
            not self.is_small_query(query_norm)
        )
        
        if should_fuzzy:
            matches.extend(
                self.fuzzy_match(query_norm, threshold=fuzzy_threshold, limit=fuzzy_limit)
            )
        
        # Ranking: exact > fuzzy, then by score, then by length
        def rank_key(item):
            alias, score, match_type, _ = item
            exact_priority = 1 if match_type.startswith("exact") else 0
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
        Main API method: identify products from query.
        
        Process:
        1. Truncate query to char_limit
        2. Get all matches (exact + fuzzy)
        3. Group by product code (SLC_CODE)
        4. Aggregate scores (max), aliases (list), match types (set)
        5. Calculate confidence scores if enabled
        6. Rank and return top N results
        
        Args:
            query: User search query
            fuzzy_threshold: Minimum fuzzy score (0.0-1.0)
            return_count: Maximum results to return
            fuzzy_limit: Maximum fuzzy candidates to match
            char_limit: Maximum query length to process
            
        Returns:
            List of product dicts with scores, codes, names, aliases, confidence
        """
        # Truncate long queries
        query = str(query)[:char_limit]
        
        # Get all matches
        matches = self.get_all_matches(
            query=query,
            fuzzy_threshold=fuzzy_threshold,
            fuzzy_limit=fuzzy_limit
        )
        
        # Group by product code
        def _default_group() -> Dict[str, Any]:
            return {
                "score": 0.0,
                "product_code": None,
                "product_name": None,
                "matched_aliases": [],
                "match_types": set(),
                "best_match_alias": None,
                "best_match_type": None
            }
        
        grouped: Dict[str, Dict[str, Any]] = defaultdict(_default_group)
        
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
                
                # Collect unique aliases
                if alias not in item["matched_aliases"]:
                    item["matched_aliases"].append(alias)
                
                # Collect match types
                item["match_types"].add(match_type)
        
        # Convert to list and calculate confidence scores
        results = []
        total_candidates = len(grouped)
        
        for _, item in grouped.items():
            item["match_types"] = sorted(list(item["match_types"]))
            
            # Calculate confidence score if enabled
            if self.enable_confidence_scoring and self.confidence_scorer:
                # Determine if fallback was used (fuzzy with low score)
                used_fallback = (
                    "fuzzy" in item["best_match_type"] and
                    item["score"] < 0.75
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
        
        # Final ranking: exact-backed > confidence > score > alias count
        def result_rank(item):
            has_exact = any(mt.startswith("exact") for mt in item["match_types"])
            exact_priority = 1 if has_exact else 0
            return (exact_priority, item["confidence"], item["score"], len(item["matched_aliases"]))
        
        results.sort(key=result_rank, reverse=True)
        return results[:return_count]

    def wml_product_identification(
        self,
        query: str,
        threshold: Optional[float] = None,
        return_count: Optional[int] = None,
        scorer=fuzz.partial_ratio,
        char_limit: int = 1000
    ) -> List[Dict[str, Any]]:
        """
        Legacy API compatibility method (matches original FuzzyMatch interface).
        
        Maps to identify_products with renamed fields for backward compatibility.
        """
        results = self.identify_products(
            query=query,
            fuzzy_threshold=threshold or 0.70,
            return_count=return_count or 10,
            char_limit=char_limit
        )
        
        # Rename fields to match legacy format
        legacy_results = []
        for result in results:
            legacy_results.append({
                "score": result["score"],
                "product_code": result["product_code"],
                "support_desc": result["product_name"],
                "support_alias": result["matched_aliases"]
            })
        
        return legacy_results

# Made with Bob
