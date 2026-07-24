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


# ---------------------------------------------------------------------------
# Words stripped from queries before matching.
# These carry no product-identification signal and only dilute scores.
# ---------------------------------------------------------------------------

# Conversational / question words
_STOPWORDS: frozenset = frozenset({
    'what', 'which', 'who', 'where', 'when', 'why', 'how',
    'is', 'are', 'were', 'be', 'been', 'being',
    'do', 'does', 'did', 'have', 'has', 'had',
    'can', 'could', 'will', 'would', 'shall', 'should', 'may', 'might',
    'to', 'of', 'in', 'on', 'at', 'by', 'for', 'with', 'from', 'about',
    'the', 'a', 'an', 'this', 'that', 'these', 'those',
    'me', 'my', 'we', 'our', 'you', 'your',
    'tell', 'show', 'give', 'help', 'find', 'get', 'list',
    'install', 'download', 'use', 'run', 'start', 'stop', 'set', 'up',
    'ibm', 'need', 'want', 'like', 'know',
})

# Generic product-category words (also penalised by confidence scorer)
# NOTE: 'application' is intentionally excluded — "Application Server" is a
# meaningful product discriminator (e.g. "WebSphere Application Server for IBM i").
_GENERIC_PRODUCT_WORDS: frozenset = frozenset({
    'software', 'product', 'solution', 'system', 'tool',
    'platform', 'service', 'program',
    'suite', 'package', 'bundle', 'offering',
})

# Bigrams that must be protected from per-token noise stripping.
# When both tokens of a bigram appear consecutively in the query they are
# treated as a single meaningful unit and neither token is removed.
# E.g. "ibm i" is the IBM i (AS/400) platform — stripping "ibm" or "i"
# individually destroys the only signal that distinguishes
# "WebSphere Application Server for IBM i" from generic WebSphere products.
_PROTECTED_BIGRAMS: tuple = (
    ('ibm', 'i'),     # IBM i (iSeries / AS/400) platform
    ('ibm', 'z'),     # IBM Z mainframe platform
    ('z', 'os'),      # z/OS — arrives as "z os" after slash→space normalisation;
                      # protected so neither token is noise-stripped before
                      # replace_delimiter_terms rejoins them as "z_os"
    ('was', 'for'),   # WAS = WebSphere Application Server; "was for <platform>"
                      # must not have either token stripped (e.g. "was for z/os")
)

# Combined set removed from queries before matching
_QUERY_NOISE_WORDS: frozenset = _STOPWORDS | _GENERIC_PRODUCT_WORDS


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
    - Noise-word / stopword removal before matching
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
        # fuzzy_alias_index:          normalised alias text -> list position
        # fuzzy_alias_noisestripped_index: noise-stripped alias text -> list position
        #   Used in get_all_matches Step 2 to promote fuzzy-section keys to
        #   exact_full when the noise-stripped query equals the noise-stripped alias.
        #   Example: query "websphere application server for z/os" normalises to
        #   "web_sphere application server z_os" (noise-stripped); the alias key
        #   "websphere application server for z/os" normalises to
        #   "web_sphere application server z_os" (also noise-stripped) — they match.
        self.fuzzy_alias_index: Dict[str, int] = {}
        self.fuzzy_alias_noisestripped_index: Dict[str, int] = {}
        tokenized_corpus = []
        for alias, products in fuzzy_match.items():
            norm_alias = self.clean_string(alias)
            if not norm_alias:
                continue

            idx = len(self.fuzzy_aliases)
            self.fuzzy_aliases.append(norm_alias)
            self.fuzzy_routes.append(products)
            self.fuzzy_alias_index[norm_alias] = idx

            # Build noise-stripped index (first writer wins — longest alias takes
            # priority because fuzzy_match is iterated in insertion order and we
            # do NOT overwrite an existing entry).
            noise_stripped = self.clean_string(alias, remove_noise=True)
            if noise_stripped and noise_stripped not in self.fuzzy_alias_noisestripped_index:
                self.fuzzy_alias_noisestripped_index[noise_stripped] = idx
            
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

        Note: this transformation is applied to the internal matching index only.
        Display aliases are converted back to spaces by _display_alias().
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

    def _display_alias(self, alias: str) -> str:
        """
        Convert an internal normalized alias back to a human-readable form
        for use in matched_aliases output.

        Reverses the underscore-joining applied by replace_delimiter_terms so
        that aliases like 'cloud_pak for data_stage' are displayed as
        'cloud pak for data stage'.

        Only underscores that were introduced by delimiter_dict substitutions
        are reversed — any underscore that was present in the original
        dictionary key is preserved by this targeted replacement.
        """
        result = alias
        for term, delimiter in self.delimiter_dict.items():
            if delimiter == "_":
                parts = term.split()
                if len(parts) == 2:
                    result = result.replace(
                        f"{parts[0]}_{parts[1]}", f"{parts[0]} {parts[1]}"
                    )
        return result

    def clean_string(self, query: Any, remove_noise: bool = False) -> str:
        """
        Advanced text normalization pipeline:
        1. Handle None/empty/non-string inputs
        2. URL extraction and validation
        3. Possessive form normalization
        4. Special character removal
        5. ASCII encoding
        6. Delimiter term normalization
        7. Whitespace collapse
        8. Noise-word removal (only when remove_noise=True)
           Strips conversational stopwords and generic product-category words
           (e.g. "what is", "software", "solution") that carry no product signal.
           Applied to user queries, NOT to dictionary alias keys.
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

        # Step 8: Remove noise words from user queries
        # Only applied when explicitly requested (user queries, not alias keys)
        # Bigram-aware: tokens that form a _PROTECTED_BIGRAM with their neighbour
        # are never stripped (e.g. "ibm i" — removing either token destroys the
        # IBM i platform signal and causes wrong rankings).
        if remove_noise and query:
            tokens = query.split()
            # Build a set of indices that are part of a protected bigram
            protected_indices: set = set()
            for idx in range(len(tokens) - 1):
                pair = (tokens[idx], tokens[idx + 1])
                if pair in _PROTECTED_BIGRAMS:
                    protected_indices.add(idx)
                    protected_indices.add(idx + 1)
            cleaned = [
                t for pos, t in enumerate(tokens)
                if pos in protected_indices or t not in _QUERY_NOISE_WORDS
            ]
            # Preserve original if stripping removed everything meaningful
            query = " ".join(cleaned) if cleaned else query

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

        Filters applied:
        - Short aliases (<=4 chars) require word boundaries to avoid substring noise.
        - Phrase-containment aliases must cover at least MIN_COVERAGE_RATIO of the
          query length. This prevents a very short generic alias (e.g. "vm", "z os")
          from flooding results when the query is a long, specific product name.
          A full exact match (alias == query) always passes regardless of length.

        Returns:
            List of (alias, score, match_type, products)
        """
        query_norm = self.clean_string(query)
        query_buf = self.buffer(query_norm)
        query_len = max(len(query_norm), 1)
        matches: List[Tuple[str, float, str, List[Dict[str, str]]]] = []
        seen = set()

        # Minimum fraction of the query that a phrase-containment alias must cover.
        # e.g. 0.40 means an alias must be at least 40% as long as the query.
        # A full exact match (alias == query) is always accepted.
        MIN_COVERAGE_RATIO = 0.40

        def _coverage_ok(alias: str) -> bool:
            """True when alias is long enough relative to the query."""
            if alias == query_norm:
                return True
            return len(alias) / query_len >= MIN_COVERAGE_RATIO

        # Use Aho-Corasick if available (much faster)
        if self.use_enhanced and self.ac_automaton:
            for end_index, (alias, products) in self.ac_automaton.iter(query_norm):
                if alias not in seen:
                    # For short aliases (<=4 chars), require word boundaries
                    if len(alias) <= 4:
                        alias_buf = self.buffer(alias)
                        if alias_buf not in query_buf:
                            continue
                    # Reject aliases that cover too little of the query
                    if not _coverage_ok(alias):
                        continue
                    seen.add(alias)
                    matches.append((alias, 1.0, "exact_phrase", products))
        else:
            # Strategy 1: Full exact match (O(1)) — always accepted
            if query_norm in self.exact_index:
                matches.append((query_norm, 1.0, "exact_full", self.exact_index[query_norm]))
                seen.add(query_norm)

            # Strategy 2: Phrase containment (alias in query)
            for alias, products in self.exact_phrases:
                if alias not in seen:
                    if len(alias) <= 4:
                        alias_buf = self.buffer(alias)
                        if alias_buf not in query_buf:
                            continue
                    elif self.buffer(alias) not in query_buf:
                        continue
                    if not _coverage_ok(alias):
                        continue
                    seen.add(alias)
                    matches.append((alias, 1.0, "exact_phrase", products))

        # Sort by length (longer = more specific)
        matches.sort(key=lambda x: len(x[0]), reverse=True)
        return matches

    def get_fuzzy_candidates(
        self,
        query_norm: str,
        max_candidates: int = 500,
        bm25_top_k: int = 20,
    ) -> Tuple[List[Tuple[int, str]], bool]:
        """
        Fast candidate filtering using BM25 (if available) or token overlap.

        Args:
            query_norm: Normalized query string
            max_candidates: Maximum candidates to return from token/fallback paths
            bm25_top_k: Number of BM25 candidates to retrieve before RapidFuzz
                        re-scores them.  Keeping this tight (default 20) ensures
                        that BM25 acts as a focused pre-filter — matching the
                        described pipeline: "BM25 → Top 20 Candidates → RapidFuzz
                        Re-score".  N-gram augmentation still kicks in when BM25
                        returns fewer than 20 results.

        Returns:
            Tuple of:
              - List of (alias_index, alias_text) tuples
              - used_fallback: True when no token/BM25 candidates were found and
                the matcher fell back to scanning all aliases (triggers -0.15 penalty)
        """
        # ── BM25 path (enhanced mode) ────────────────────────────────────────
        if self.use_enhanced and self.bm25_index:
            query_tokens = self.tokenize(query_norm)
            if query_tokens:
                scores = self.bm25_index.get_scores(query_tokens)
                top_indices = sorted(
                    range(len(scores)),
                    key=lambda i: scores[i],
                    reverse=True,
                )[:bm25_top_k]
                candidates = [
                    (idx, self.fuzzy_aliases[idx])
                    for idx in top_indices
                    if scores[idx] > 0
                ]

                # Augment with n-gram candidates when BM25 returns few results
                # (handles typos and out-of-vocabulary terms)
                if len(candidates) < bm25_top_k and self.ngram_index:
                    ngram_candidates = self._get_ngram_candidates(
                        query_norm, bm25_top_k
                    )
                    existing_ids = {idx for idx, _ in candidates}
                    for idx, text in ngram_candidates:
                        if idx not in existing_ids:
                            candidates.append((idx, text))

                if candidates:
                    return candidates[:max_candidates], False

        # ── Token-overlap fallback (non-enhanced or BM25 returned nothing) ──
        tokens = self.tokenize(query_norm)
        candidate_ids: Set[int] = set()
        for token in tokens:
            candidate_ids.update(self.token_to_fuzzy_ids.get(token, set()))

        if candidate_ids:
            candidates = [(i, self.fuzzy_aliases[i]) for i in candidate_ids]
            candidates.sort(key=lambda x: len(x[1]), reverse=True)
            return candidates[:max_candidates], False

        # ── Last-resort: no signal at all — scan all aliases ─────────────────
        # Triggers -0.15 confidence penalty (used_fallback=True)
        fallback_count = min(max_candidates, len(self.fuzzy_aliases))
        return [(i, self.fuzzy_aliases[i]) for i in range(fallback_count)], True

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
        scorer=None,
    ) -> List[Tuple[str, float, str, List[Dict[str, str]]]]:
        """
        Fuzzy matching with candidate pre-filtering.

        Pipeline (mirrors the described architecture):
          User Query → Normalization → BM25 Search → Top 20 Candidates
          → RapidFuzz Re-score → return ranked matches

        Scorer selection:
        - ``token_sort_ratio`` is used by default.  It handles word-order
          variations well (e.g. "Server Application WebSphere" still matches
          "WebSphere Application Server") and is more robust than
          ``partial_ratio`` for multi-token IBM product names.
        - Pass an explicit scorer to override (e.g. ``fuzz.partial_ratio``
          for single-keyword queries where substring matching is preferred).

        Args:
            query: Raw query string (already normalised by callers)
            threshold: Minimum similarity score (0.0-1.0)
            limit: Maximum matches to return
            scorer: RapidFuzz scorer to use; defaults to ``fuzz.token_sort_ratio``

        Returns:
            (matches, used_fallback) where matches is a list of
            (alias, score, match_type, products) tuples.
        """
        if scorer is None:
            scorer = fuzz.token_sort_ratio

        query_norm = self.clean_string(query)

        # Retrieve pre-filtered candidates; used_fallback signals -0.15 penalty
        candidates, used_fallback = self.get_fuzzy_candidates(query_norm)

        if not candidates:
            return [], False

        # Build texts list and reverse lookup for routing
        candidate_texts = [candidate_text for _, candidate_text in candidates]
        candidate_map = {candidate_text: idx for idx, candidate_text in candidates}

        # RapidFuzz re-scores the short candidate list (C++ speed, accurate)
        extracted = process.extract(
            query_norm,
            candidate_texts,
            scorer=scorer,
            score_cutoff=int(threshold * 100),  # rapidfuzz uses 0-100 scale
            limit=limit,
        )

        matches = []
        for alias, score, _ in extracted:
            idx = candidate_map[alias]
            matches.append((alias, score / 100.0, "fuzzy", self.fuzzy_routes[idx]))

        # Primary sort: score DESC, secondary: alias length DESC (specificity)
        matches.sort(key=lambda x: (x[1], len(x[0])), reverse=True)
        return matches, used_fallback

    def get_all_matches(
        self,
        query: str,
        fuzzy_threshold: float = 0.70,
        fuzzy_limit: int = 30
    ) -> List[Tuple[str, float, str, List[Dict[str, str]]]]:
        """
        Combine exact and fuzzy matching with smart heuristics.

        Logic:
        1. Always perform exact matching (exact_match section of dictionary)
        2. Also check fuzzy_match section for exact key equality — promotes
           keys that are stored in fuzzy_match but exactly match the query
        3. Skip fuzzy scoring for machine codes (e.g., "5724-A12")
        4. Skip fuzzy scoring for very short queries (< 5 chars)
        5. Rank: exact matches first, then fuzzy, then by score, then by length

        Returns:
            Combined and ranked list of matches
        """
        # Strip noise words from the user query; alias keys are never noise-stripped
        query_norm = self.clean_string(query, remove_noise=True)

        # --- Step 1: exact_match section hits ---
        matches = self.exact_match(query_norm)
        exact_aliases_seen = {alias for alias, _, _, _ in matches}

        # --- Step 2: promote fuzzy-section keys that exactly match any token
        #             or the full query string.
        #
        #   e.g. query "how to install cognos":
        #     - "cognos" is a token AND an exact key in fuzzy_match → exact_full
        #   e.g. query "cognos":
        #     - full query is an exact key in fuzzy_match → exact_full
        #
        #   Step 2b: also promote via noise-stripped index.
        #   Handles cases where the query and alias differ only in stopwords:
        #   query  "websphere application server for z/os"
        #     → noise-stripped: "web_sphere application server z_os"
        #   alias  "websphere application server for z/os"
        #     → noise-stripped: "web_sphere application server z_os"
        #   They match via fuzzy_alias_noisestripped_index → promoted to exact_full.
        candidates_for_exact = {query_norm} | set(self.tokenize(query_norm))
        for term in candidates_for_exact:
            if term in self.fuzzy_alias_index and term not in exact_aliases_seen:
                idx = self.fuzzy_alias_index[term]
                matches.append((term, 1.0, "exact_full", self.fuzzy_routes[idx]))
                exact_aliases_seen.add(term)

        # Step 2b: noise-stripped alias lookup.
        # Only applied when the noise-stripped query has enough tokens to carry
        # specific product context (>= 3 tokens).  Short queries like "z/os"
        # (1 token after normalisation) must go through the standard exact_match
        # path; hijacking them via the noisestripped index would surface wrong
        # products (e.g. "was for z/os" → SAIW1 instead of z/OS → SCZQ9).
        _NS_MIN_TOKENS = 3
        if (
            len(query_norm.split()) >= _NS_MIN_TOKENS
            and query_norm in self.fuzzy_alias_noisestripped_index
        ):
            idx = self.fuzzy_alias_noisestripped_index[query_norm]
            norm_alias = self.fuzzy_aliases[idx]
            if norm_alias not in exact_aliases_seen:
                matches.append((norm_alias, 1.0, "exact_full", self.fuzzy_routes[idx]))
                exact_aliases_seen.add(norm_alias)

        # --- Step 3: fuzzy scoring for remaining candidates ---
        should_fuzzy = (
            not self.is_machine_code(query_norm) and
            not self.is_small_query(query_norm)
        )

        used_fallback = False
        if should_fuzzy:
            fuzzy_results, used_fallback = self.fuzzy_match(
                query_norm, threshold=fuzzy_threshold, limit=fuzzy_limit
            )
            # Exclude aliases already captured as exact
            for alias, score, match_type, products in fuzzy_results:
                if alias not in exact_aliases_seen:
                    matches.append((alias, score, match_type, products))

        # Ranking: exact > fuzzy, then by score, then by alias length
        def rank_key(item):
            alias, score, match_type, _ = item
            exact_priority = 1 if match_type.startswith("exact") else 0
            return (exact_priority, score, len(alias))

        matches.sort(key=rank_key, reverse=True)
        return matches, used_fallback

    def identify_products(
        self,
        query: str,
        fuzzy_threshold: float = 0.70,
        return_count: int = 10,
        fuzzy_limit: int = 30,
        char_limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """
        Main API method: identify products from query.

        Pipeline
        --------
        1. Truncate query to char_limit
        2. Normalization + Synonym / Alias Expansion (clean_string)
        3. BM25 + RapidFuzz → all matches (exact + fuzzy)
        4. Group by product code (SLC_CODE), score aggregation
        5. Calculate confidence scores
        6. Rank → Top 10 candidates, return top ``return_count``

        Args:
            query: User search query
            fuzzy_threshold: Minimum fuzzy score (0.0-1.0)
            return_count: Maximum results to return
            fuzzy_limit: Maximum fuzzy candidates to match
            char_limit: Maximum query length to process

        Returns:
            List of product dicts with scores, codes, names, aliases, and confidence.
        """
        # Truncate long queries
        query = str(query)[:char_limit]

        # Get all matches; used_fallback=True when -0.15 penalty should apply
        matches, used_fallback = self.get_all_matches(
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
                "best_match_type": None,
                # Number of products that the best-match alias key resolves to
                # (used for confidence scoring: >1 means ambiguous alias)
                "best_match_alias_product_count": 1,
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

                # Track best match for confidence calculation.
                # Prefer exact over fuzzy at equal score.
                current_is_exact = item["best_match_type"] and item["best_match_type"].startswith("exact")
                new_is_exact = match_type.startswith("exact")
                if (score > item["score"]) or (score == item["score"] and new_is_exact and not current_is_exact):
                    item["score"] = round(score, 6)
                    item["best_match_alias"] = alias
                    item["best_match_type"] = match_type
                    # How many distinct products does this alias key resolve to?
                    item["best_match_alias_product_count"] = len(products)

                # Collect unique aliases — exact-matched aliases go first.
                # Convert internal underscore-joined forms back to display form
                # (e.g. 'cloud_pak' → 'cloud pak') before storing for output.
                display = self._display_alias(alias)
                if display not in item["matched_aliases"]:
                    if new_is_exact:
                        item["matched_aliases"].insert(0, display)
                    else:
                        item["matched_aliases"].append(display)

                # Collect match types
                item["match_types"].add(match_type)

        # Convert to list and calculate confidence scores
        results = []
        total_candidates = len(grouped)

        for _, item in grouped.items():
            item["match_types"] = sorted(list(item["match_types"]))

            # Preserve tie-breaking signals before temporary fields are deleted.
            #
            # Signal 1 — best_match_alias length:
            #   A longer best-matching alias indicates a more specific match
            #   (e.g. "web_sphere application server for ibm i" (39 chars) is more
            #   specific than "ibm i db2" (9 chars) even if both score 1.0).
            item["_best_alias_len"] = len(item.get("best_match_alias") or "")
            #
            # Signal 2 — product name similarity (final tie-break):
            #   When two products share the exact same best-match alias
            #   (e.g. both SCPF9 and SCPL5 map to "web_sphere application server
            #   for ibm i"), use the token overlap between the query and the
            #   product name as a last-resort tie-break.
            #   SCPL5 "WebSphere Application Server for IBM i" shares 5 tokens
            #   with the query; SCPF9 "IBM i" shares only 2.
            query_tokens = set(self.clean_string(query).split())
            name_tokens  = set(self.clean_string(item.get("product_name") or "").split())
            item["_name_overlap"] = len(query_tokens & name_tokens)

            # Calculate confidence score if enabled
            if self.enable_confidence_scoring and self.confidence_scorer:
                confidence = self.confidence_scorer.calculate_confidence(
                    match_type=item["best_match_type"],
                    match_score=item["score"],
                    query=query,
                    matched_alias=item["best_match_alias"],
                    # product_count: how many products the best alias key maps to
                    product_count=item["best_match_alias_product_count"],
                    # candidate_count: total distinct products in result set
                    # spec: "-0.10 if multiple candidate products remain"
                    candidate_count=total_candidates,
                    product_code=item["product_code"],
                    # used_fallback: True only when no token/BM25 signal was found
                    # and matcher fell back to scanning all aliases
                    used_fallback=used_fallback
                )
                item["confidence"] = confidence
            else:
                # Fallback: use match score as confidence
                item["confidence"] = round(item["score"], 2)

            # Clean up temporary fields
            del item["best_match_alias"]
            del item["best_match_type"]
            del item["best_match_alias_product_count"]

            results.append(item)

        # Compute alias_similarity for every result: highest ratio() between the
        # normalised query and any of the product's matched aliases.
        # This is used both for tie-breaking in final ranking AND by TLSChecker
        # to decide whether a TLS product is "close enough" to the top result.
        query_norm_for_rank = self.clean_string(query)

        for item in results:
            item["alias_similarity"] = max(
                (fuzz.ratio(query_norm_for_rank, a) for a in item["matched_aliases"]),
                default=0.0
            )

        # Final ranking: exact-backed > confidence > score > alias_similarity > alias count
        #
        # alias_similarity tie-breaker resolves cases where score and confidence are equal:
        #   "storage fusion"  → alias "storage fusion" (sim=100) beats
        #                        alias "storage fusion hci physical appliance" (sim=50)
        #   "db2 for z/os"    → alias "db2 for z/os" (sim=100) beats alias "z os" (sim=40)
        def result_rank(item):
            has_exact = any(mt.startswith("exact") for mt in item["match_types"])
            exact_priority = 1 if has_exact else 0
            best_alias_len = item.pop("_best_alias_len", 0)
            name_overlap   = item.pop("_name_overlap", 0)
            return (
                exact_priority,
                item["confidence"],
                item["score"],
                item["alias_similarity"],
                best_alias_len,
                name_overlap,
            )

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
