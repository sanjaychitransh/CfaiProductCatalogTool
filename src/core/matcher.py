"""
matcher.py — Hybrid Product Matcher
=====================================

Implements the full hybrid product-matching pipeline described in the solution
design:

    User Text / Support Case
        │
        ▼
    1. Dictionary Normalisation  (clean_string)
        │  Lowercase, punctuation→space, part-number hyphen merge,
        │  delimiter joining (e.g. "cloud pak"→"cloud_pak"),
        │  noise-word / stopword removal, bigram protection
        ▼
    2. Exact Match  (exact_match)
        │  Aho-Corasick multi-pattern scan over the exact_match section
        │  of the product dictionary.  O(n+m) complexity.
        │  Fallback: hash-lookup + phrase-containment scan.
        ▼
    3. BM25 Candidate Retrieval  (get_fuzzy_candidates)
        │  rank_bm25.BM25Okapi scores every fuzzy-match alias against
        │  the tokenised query.  Top-20 candidates are forwarded.
        │  N-gram augmentation fills gaps when BM25 returns < 20 hits.
        ▼
    4. RapidFuzz Re-scoring  (fuzzy_match)
        │  process.extract with token_sort_ratio re-ranks the short
        │  candidate list with C-speed string similarity.
        ▼
    5. SLC Grouping + Confidence  (identify_products)
        │  Matches grouped by SLC_CODE; per-product confidence computed
        │  from base score ± penalties ± contextual boosts.
        ▼
    6. Optional SBERT Re-ranking  (SBERTReranker.rerank)
        │  Dense bi-encoder + cross-encoder re-order of the top-N list.
        │  Only active when USE_SBERT_RERANKER=true.
        ▼
    7. Result  →  product_code (SLC), product_name, confidence, aliases

Coverage: typos, aliases, partial names, embedded references, case variation,
support-case context words.
"""

from rapidfuzz import fuzz, process
from collections import defaultdict
from typing import Dict, List, Any, Optional, Tuple, Set, TypedDict
import re
from .confidence_scorer import ConfidenceScorer

# ---------------------------------------------------------------------------
# Optional heavy imports — graceful fallback when packages are absent.
# When sentence-transformers is not installed the SBERT re-ranker is simply
# not loaded; the rest of the pipeline is unaffected.
# ---------------------------------------------------------------------------

# Sentence-BERT + Cross-Encoder re-ranker (optional — loaded on demand)
try:
    from .sbert_reranker import SBERTReranker
    _SBERT_MODULE_AVAILABLE = True
except ImportError:
    SBERTReranker = None  # type: ignore
    _SBERT_MODULE_AVAILABLE = False

# Aho-Corasick (pyahocorasick) and BM25 (rank_bm25).
# Both are required for enhanced mode.  If either is absent the matcher
# falls back to the token-inverted-index path which is slower but correct.
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
# Module-level word lists
# ---------------------------------------------------------------------------

# Conversational / question words stripped from queries before matching.
# These carry no product-identification signal; removing them improves
# BM25 scores and RapidFuzz similarity against product alias keys.
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

# Generic product-category words that also trigger a confidence penalty
# when they make up the majority of a query.
#
# NOTE: 'application' is intentionally EXCLUDED.
# "Application Server" is a meaningful product discriminator — e.g.
# "WebSphere Application Server for IBM i" would lose its entire identity
# if 'application' were stripped.  The ConfidenceScorer.GENERIC_TERMS set
# follows the same rule for the same reason.
_GENERIC_PRODUCT_WORDS: frozenset = frozenset({
    'software', 'product', 'solution', 'system', 'tool',
    'platform', 'service', 'program',
    'suite', 'package', 'bundle', 'offering',
})

# Bigrams protected from per-token noise stripping.
#
# When both tokens of a listed pair appear consecutively in the (post-
# normalisation) query they are treated as a single meaningful unit.
# Neither token is removed even if it individually appears in _STOPWORDS
# or _GENERIC_PRODUCT_WORDS.
#
# Rationale for each entry:
#   ('ibm', 'i')         "IBM i" = iSeries/AS400 platform.  Stripping either
#                         token collapses "WebSphere Application Server for IBM i"
#                         to a generic WAS query, causing wrong SLC ranking.
#   ('ibm', 'z')         "IBM Z" mainframe family — same argument.
#   ('z', 'os')          After slash→space normalisation "z/os" becomes "z os".
#                         Both tokens must survive so replace_delimiter_terms
#                         can later rejoin them as "z_os".
#   ('was', 'for')       "WAS for z/OS" — "was" is in _STOPWORDS as an
#                         auxiliary verb, but here it is the WAS acronym.
#                         Stripping it loses the product context entirely.
#   ('m365', 'platform') "M365 Platform" — stripping 'platform' leaves "m365"
#                         (4 chars), which is below the fuzzy threshold.
#   ('power', 'platform') "Power Platform Family" — same pattern.
#
# Using frozenset gives O(1) membership test regardless of how many entries
# are added in future (compared with a tuple which is O(n)).
_PROTECTED_BIGRAMS: frozenset = frozenset({
    ('ibm', 'i'),
    ('ibm', 'z'),
    ('z', 'os'),
    ('was', 'for'),
    ('m365', 'platform'),
    ('power', 'platform'),
})

# Combined noise-word set applied to user queries (never to alias keys).
_QUERY_NOISE_WORDS: frozenset = _STOPWORDS | _GENERIC_PRODUCT_WORDS


class ProductMatcher:
    """
    High-performance product matcher implementing the hybrid pipeline.

    Search pipeline (all stages):
    ─────────────────────────────
    Stage 1  Aho-Corasick exact phrase scan (exact_match dictionary section)
    Stage 2  Fuzzy-section key promotion  (exact equality or noise-stripped match)
    Stage 3  BM25 candidate retrieval     (top-20 from fuzzy_match section)
    Stage 4  N-gram augmentation          (typo-tolerant gap-fill when BM25 < 20)
    Stage 5  RapidFuzz token_sort_ratio   (re-score candidate list, 0–100 scale)
    Stage 6  SLC grouping + confidence    (aggregate, score, rank)
    Stage 7  Optional SBERT re-ranking    (semantic bi-encoder + cross-encoder)

    All indexes are built once at __init__ time so per-request work is minimal.

    Features:
    - Exact match (full equality + phrase containment)
    - Fuzzy match with BM25 candidate pre-filtering
    - Aho-Corasick for O(n+m) exact phrase detection
    - BM25 (rank_bm25) for weighted lexical candidate retrieval
    - N-gram character index for typo tolerance
    - Delimiter normalisation (e.g. "cloud pak" → "cloud_pak")
    - Noise-word / stopword removal with bigram protection
    - Machine-code detection to skip fuzzy matching
    - Token inverted index as non-enhanced fallback
    - SLC_CODE grouping with score aggregation
    - ConfidenceScorer for routing-grade confidence output
    - Optional SBERT + Cross-Encoder semantic re-ranking
    """

    def __init__(
        self,
        match_dictionary: Dict[str, Any],
        delimiter_dict: Optional[Dict[str, str]] = None,
        use_enhanced: bool = True,
        enable_confidence_scoring: bool = True,
        sbert_reranker: Optional[Any] = None,
    ):
        """
        Initialise the matcher and build all search indexes.

        Args:
            match_dictionary:
                Dict with two top-level keys:
                  "exact_match"  — high-confidence aliases (acronyms, canonical
                                   names) that Aho-Corasick indexes for exact
                                   phrase detection.
                  "fuzzy_match"  — broader aliases (partial names, historical
                                   names, support-case terminology) that form the
                                   BM25 + RapidFuzz corpus.
                Both sections map alias strings to a list of
                {"SLC_CODE": ..., "PRODUCT_NAME": ...} dicts.

            delimiter_dict:
                Maps two-word compound terms to the joining character used
                internally (always "_").  Loaded from the "delimiter_dict"
                key in product_match_dictionary.json so operators can extend
                the list without a code change.
                Example: {"cloud pak": "_", "z os": "_"}

            use_enhanced:
                When True (default) and both pyahocorasick and rank_bm25 are
                installed, the Aho-Corasick, BM25, and N-gram indexes are
                activated.  If the packages are absent the matcher silently
                falls back to pure-Python hash + token-inverted-index paths.

            enable_confidence_scoring:
                When True (default) each result carries a confidence score
                computed by ConfidenceScorer.  When False the raw match score
                is used as confidence directly.

            sbert_reranker:
                Optional SBERTReranker instance.  When provided,
                identify_products() passes its top-N result list through the
                reranker as a final post-processing step.  The reranker adds
                a "sbert_score" field to each result and re-sorts by a
                weighted blend of confidence and semantic score.
                Activate via USE_SBERT_RERANKER=true in the environment.
        """
        self.delimiter_dict = delimiter_dict or {}
        self.match_dictionary = match_dictionary or {"exact_match": {}, "fuzzy_match": {}}

        # use_enhanced is False when the optional packages are absent, so
        # downstream code can safely check self.use_enhanced without
        # re-testing for package availability.
        self.use_enhanced = use_enhanced and ENHANCED_AVAILABLE
        self.enable_confidence_scoring = enable_confidence_scoring

        # Optional Sentence-BERT + Cross-Encoder semantic re-ranker.
        # None when USE_SBERT_RERANKER=false (the default).
        self.sbert_reranker = sbert_reranker

        # ConfidenceScorer is stateless per-request; the session_history set
        # inside it is intentionally not used here (see note in scorer).
        self.confidence_scorer = ConfidenceScorer() if enable_confidence_scoring else None

        # ── Exact-match indexes ───────────────────────────────────────────
        # exact_index:   normalised alias → product list  (O(1) hash lookup)
        # exact_phrases: sorted list of (alias, products) for phrase-containment
        #                scan; sorted longest-first so more specific aliases
        #                are checked before generic substrings.
        self.exact_index: Dict[str, List[Dict[str, str]]] = {}
        self.exact_phrases: List[Tuple[str, List[Dict[str, str]]]] = []

        # ── Fuzzy-match indexes ───────────────────────────────────────────
        # fuzzy_aliases: flat list of normalised alias strings (BM25 corpus)
        # fuzzy_routes:  parallel list of product lists; fuzzy_routes[i]
        #                is the product list for fuzzy_aliases[i]
        self.fuzzy_aliases: List[str] = []
        self.fuzzy_routes: List[List[Dict[str, str]]] = []

        # token_to_fuzzy_ids: token string → set of alias indices that
        # contain that token.  Used as a fast pre-filter when BM25 is
        # unavailable (non-enhanced mode or BM25 returns nothing).
        self.token_to_fuzzy_ids: Dict[str, Set[int]] = defaultdict(set)

        # ── Enhanced indexes (optional) ───────────────────────────────────
        # ac_automaton:  Aho-Corasick automaton over the exact_match section
        # bm25_index:    BM25Okapi instance built from the fuzzy_match corpus
        # ngram_index:   char-trigram → set of alias indices
        self.ac_automaton = None
        self.bm25_index = None
        self.ngram_index: Dict[str, Set[int]] = defaultdict(set)
        self.ngram_size = 3  # trigrams balance specificity and recall

        # Build everything now so startup cost is paid once, not per request.
        self._build_indexes()

    # =========================================================================
    # Index construction
    # =========================================================================

    def _build_indexes(self) -> None:
        """
        Build all search indexes from the match dictionary.

        Called once from __init__.  After this method returns, every lookup
        structure used by exact_match(), get_fuzzy_candidates(), and
        fuzzy_match() is fully populated.

        Index construction order:
        1. exact_index + exact_phrases   — from exact_match section
        2. Aho-Corasick automaton        — from exact_match section (enhanced)
        3. fuzzy_aliases + fuzzy_routes  — from fuzzy_match section
        4. token_to_fuzzy_ids            — from fuzzy_match section
        5. ngram_index                   — from fuzzy_match section (enhanced)
        6. BM25Okapi                     — from tokenised fuzzy_match corpus
        7. fuzzy_alias_index             — normalised form → list position
        8. fuzzy_alias_noisestripped_index — noise-stripped form → list position
        """
        exact_match = self.match_dictionary.get("exact_match", {})
        fuzzy_match = self.match_dictionary.get("fuzzy_match", {})

        # ── Step 1–2: Exact-match section ────────────────────────────────
        for alias, products in exact_match.items():
            norm_alias = self.clean_string(alias)
            if not norm_alias:
                continue

            # Hash-lookup index for O(1) full-equality checks.
            self.exact_index[norm_alias] = products

            # Phrase-containment list (checked longest-first).
            self.exact_phrases.append((norm_alias, products))

        # Sort longest alias first — longer alias == more specific match.
        self.exact_phrases.sort(key=lambda x: len(x[0]), reverse=True)

        # Aho-Corasick: add every normalised exact alias as a pattern word.
        # make_automaton() compiles the Aho-Corasick failure links so that
        # a single O(n) scan over the query finds all matching aliases.
        if self.use_enhanced and ahocorasick:
            self.ac_automaton = ahocorasick.Automaton()
            for alias, products in exact_match.items():
                norm_alias = self.clean_string(alias)
                if norm_alias:
                    self.ac_automaton.add_word(norm_alias, (norm_alias, products))
            self.ac_automaton.make_automaton()

        # ── Step 3–8: Fuzzy-match section ────────────────────────────────
        #
        # fuzzy_alias_index:
        #   normalised alias text → list position in fuzzy_aliases.
        #   Used in get_all_matches Step 2 to promote fuzzy-section keys to
        #   exact_full when the full query string equals a fuzzy key exactly.
        #
        # fuzzy_alias_noisestripped_index:
        #   noise-stripped alias text → list position.
        #   Example: query "websphere application server for z/os" strips to
        #   "web_sphere application server z_os"; the alias key has the same
        #   noise-stripped form → exact_full promotion fires.
        #   First-writer-wins: the longest alias for a given noise-stripped
        #   form wins because insertion order is preserved in Python dicts.
        self.fuzzy_alias_index: Dict[str, int] = {}
        self.fuzzy_alias_noisestripped_index: Dict[str, int] = {}
        tokenized_corpus = []  # parallel list of token lists for BM25

        for alias, products in fuzzy_match.items():
            norm_alias = self.clean_string(alias)
            if not norm_alias:
                continue

            idx = len(self.fuzzy_aliases)
            self.fuzzy_aliases.append(norm_alias)
            self.fuzzy_routes.append(products)
            self.fuzzy_alias_index[norm_alias] = idx

            # Noise-stripped index — first writer wins.
            noise_stripped = self.clean_string(alias, remove_noise=True)
            if noise_stripped and noise_stripped not in self.fuzzy_alias_noisestripped_index:
                self.fuzzy_alias_noisestripped_index[noise_stripped] = idx

            # Tokenise for BM25 and the token inverted index.
            tokens = self.tokenize(norm_alias)
            tokenized_corpus.append(tokens)

            # Token inverted index: each unique token in the alias points back
            # to this alias index.  Single-char tokens are excluded — they add
            # noise without meaningful retrieval signal.
            token_set = set(tokens)
            for token in token_set:
                if len(token) >= 2:
                    self.token_to_fuzzy_ids[token].add(idx)

            # N-gram index — only built in enhanced mode.
            # Character trigrams tolerate single-character typos, transpositions,
            # and missing characters that would otherwise score 0 in BM25.
            if self.use_enhanced:
                ngrams = self._generate_ngrams(norm_alias, self.ngram_size)
                for ngram in ngrams:
                    self.ngram_index[ngram].add(idx)

        # BM25Okapi: built from the full tokenised corpus of fuzzy aliases.
        # BM25 provides term-frequency weighting and document-length
        # normalisation — both important for a catalog where product names
        # range from 1 token ("db2") to 10+ tokens
        # ("websphere application server for ibm i").
        if self.use_enhanced and BM25Okapi and tokenized_corpus:
            self.bm25_index = BM25Okapi(tokenized_corpus)

    # =========================================================================
    # Helper utilities
    # =========================================================================

    def buffer(self, text: str) -> str:
        """
        Wrap text in leading and trailing spaces.

        Used for word-boundary checks: testing ` alias ` in ` query ` ensures
        the alias is not a substring of a longer word.
        """
        return f" {text} "

    def is_small_query(self, text: str, n: int = 5) -> bool:
        """
        Return True when the normalised query is too short for fuzzy matching.

        Queries of 5 characters or fewer (e.g. "db2", "mq") are handled
        exclusively by exact matching to avoid high false-positive rates from
        RapidFuzz on very short strings.
        """
        return len(text.strip()) <= n

    def is_machine_code(self, text: str) -> bool:
        """
        Return True when the text looks like a machine/part code.

        Detection rule: after stripping hyphens, spaces, and underscores,
        if ≥ 50% of the remaining characters are digits the text is treated
        as a numeric code (e.g. "2805mc5", "5724a12").  Fuzzy matching is
        skipped for these to avoid false positives against alphanumeric
        product names in the catalog.
        """
        stripped = text.replace("-", "").replace(" ", "").replace("_", "")
        if not stripped:
            return False

        numeric_count = sum(c.isdigit() for c in stripped)
        return numeric_count >= len(stripped) / 2

    def replace_delimiter_terms(self, query: str) -> str:
        """
        Join two-word compound terms with a custom delimiter character.

        Reads from self.delimiter_dict (loaded from the dictionary JSON).
        The joined form is used throughout internal matching but is reversed
        by _display_alias() before appearing in API responses.

        Examples (with delimiter "_"):
            "cloud pak for data"  → "cloud_pak for data"
            "db2 for z os luw"    → "db2 for z_os luw"
            "websphere / appsvr"  → "web_sphere appsvr"   (if 'web sphere' in dict)

        Pattern accepts space, hyphen, or slash as the separator between the
        two parts so that common variants like "tcp/ip" or "tcp-ip" are all
        normalised to "tcp_ip".
        """
        for term, delimiter in self.delimiter_dict.items():
            parts = term.split()
            if len(parts) != 2:
                # Only two-word terms are supported; skip anything else
                continue

            # Flexible separator: space, forward-slash, or hyphen between
            # the two parts.  The leading/trailing boundary group (group 1
            # and group 3) preserves surrounding whitespace or punctuation.
            pattern = rf"(\b|\.|\?| ){re.escape(parts[0])}( |/|-|){re.escape(parts[1])}(\b|\.|\?| )"
            replacement = rf"\g<1>{parts[0]}{delimiter}{parts[1]}\g<3>"
            query = re.sub(pattern, replacement, query, flags=re.IGNORECASE)

        return query

    def _display_alias(self, alias: str) -> str:
        """
        Convert an internal normalised alias back to human-readable form.

        Reverses only the underscore joins introduced by replace_delimiter_terms
        for underscore-delimiter entries (e.g. "cloud_pak" → "cloud pak").
        Underscores that were already present in the original dictionary key
        are not touched because this replacement is targeted (term-by-term),
        not a global underscore→space substitution.

        This is applied to matched_aliases before they are stored in the result
        dict so that the API response shows readable names, not internal codes.
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
        Advanced text normalisation pipeline.

        Converts any raw text (support case title, user query, dictionary key)
        into a canonical lowercase form suitable for exact and fuzzy matching.

        Pipeline steps
        ──────────────
        1. Type guard — None / non-string inputs are coerced to str or "".
        2. Lowercase + strip.
        3. URL handling — IBM product/documentation URLs are kept and the path
           is extracted; non-IBM URLs are discarded (return "").
        4. Possessive normalisation — "IBM's" → "IBMs".
        5. Character whitelist — keep alphanumeric, limited punctuation
           (.,;:!?#/-) and whitespace; strip everything else (e.g. brackets,
           quotes, underscores not introduced by step 7).
        6. ASCII encoding — removes accented characters.
        7. Part-number hyphen merge — "2805-MC5" → "2805mc5".
           Hyphens flanked by alphanumerics on both sides are concatenated so
           the part number is treated as a single token.  Hyphens used as word
           separators (flanked by spaces) fall through unchanged and are
           converted to spaces in step 8.
        8. Punctuation→space — all remaining separators become single spaces.
        9. Delimiter joining — replace_delimiter_terms joins compound terms
           (e.g. "cloud pak" → "cloud_pak") using the delimiter_dict.
        10. Whitespace collapse — multiple spaces → single space.
        11. Noise-word removal (only when remove_noise=True).
            Strips _STOPWORDS and _GENERIC_PRODUCT_WORDS from the token list.
            Protected bigrams are shielded: both tokens in a recognised pair
            are kept even if individually they are noise words.
            Only applied to user queries — NEVER to dictionary alias keys,
            because noise-stripping an alias key would break the fuzzy_alias_index
            and fuzzy_alias_noisestripped_index lookups.

        Args:
            query:        Input text (any type — coerced to str).
            remove_noise: When True, apply stopword/generic-word removal
                          (step 11).  Default False.

        Returns:
            Normalised string.  Empty string when input is None, empty, or a
            non-IBM URL.
        """
        # Step 1: Type guard
        if query is None:
            return ""
        if not isinstance(query, str):
            query = str(query)

        # Step 2: Lowercase + strip
        query = query.lower().strip()
        if not query:
            return ""

        # Step 3: URL handling
        # A lone URL token starting with "http" is treated as a product reference.
        # Only IBM product/documentation paths are useful; everything else is
        # discarded because the URL path carries no reliable product signal.
        if query.startswith("http") and len(query.split()) == 1:
            query = query.split("?")[0].replace("-", " ")  # strip query string, expand hyphens
            if not any(path in query for path in ["/topic/", "ibm.com/products/", "ibm.com/cloud/"]):
                return ""  # non-IBM URL — no usable signal

        # Step 4: Possessive normalisation — "IBM's" → "IBMs"
        query = re.sub(r"(\w+)'s", r"\1s", query)

        # Step 5: Character whitelist — keep only alphanumeric and safe punctuation.
        # Brackets, quotes, asterisks, underscores from raw input, etc. are stripped.
        query = re.sub(r"[^a-zA-Z0-9.,;:!?#/\s-]", "", query)

        # Step 6: ASCII encoding — drops accented/multi-byte characters.
        query = query.encode("ascii", errors="ignore").decode()

        # Step 7: Part-number hyphen merge.
        # "2805-MC5" → "2805mc5"   (the whole part number becomes one token)
        # "red - hat" → unchanged  (space-padded hyphens are word separators)
        # The lookbehind/lookahead ensure only alphanumeric-flanked hyphens are merged.
        query = re.sub(r"(?<=[a-zA-Z0-9])-(?=[a-zA-Z0-9])", "", query)

        # Step 8: Remaining punctuation/separators → single space.
        # This converts remaining hyphens, slashes, and dots to spaces so that
        # e.g. "z/os" becomes "z os" (ready for the delimiter join in step 9).
        query = re.sub(r"[.,;:!?#/\s-]+", " ", query)

        # Step 9: Delimiter joining.
        # Replaces recognised two-word compound terms with a joined form using
        # underscore so that "cloud pak for data" → "cloud_pak for data".
        # This is a prerequisite for correct BM25 tokenisation: "cloud_pak" is
        # one token and will score higher against the alias key "cloud_pak for data"
        # than two separate tokens "cloud" and "pak" would.
        query = self.replace_delimiter_terms(query)

        # Step 10: Whitespace collapse
        query = re.sub(r"\s+", " ", query).strip()

        # Step 11: Noise-word removal (user queries only).
        # Removes tokens in _QUERY_NOISE_WORDS but preserves any token that is
        # part of a _PROTECTED_BIGRAMS pair.  The bigram check is index-aware:
        # token[i] and token[i+1] are checked as a pair; if the pair is
        # protected, both indices are added to the protected_indices set and
        # neither token is removed.
        if remove_noise and query:
            tokens = query.split()
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
            # Safety: if stripping removed every token (e.g. query was pure
            # stopwords) fall back to the pre-stripped form so the caller
            # always gets a non-empty string for a non-empty input.
            query = " ".join(cleaned) if cleaned else query

        return query

    def tokenize(self, text: str) -> List[str]:
        """
        Split normalised text into tokens for BM25 and inverted-index lookups.

        Single-character tokens are excluded because they add noise to BM25
        IDF calculations without meaningful retrieval signal.  The character
        "i" in "ibm i" is protected at a higher level (bigram guard) rather
        than being indexed individually.
        """
        return [token for token in text.split() if len(token) >= 2]

    def _generate_ngrams(self, text: str, n: int) -> Set[str]:
        """
        Generate character n-grams for typo-tolerant candidate retrieval.

        The text is padded with "#" sentinels on both ends so that edge
        characters are covered by n-grams.

        Example (n=3, text="ibm"):
            padded = "#ibm#"
            n-grams = {"#ib", "ibm", "bm#"}

        Jaccard similarity between query n-grams and alias n-grams provides
        a fast approximation of edit distance, surfacing candidates that BM25
        misses due to OOV tokens (e.g. a misspelled product name shares no
        whole tokens with the alias but does share many trigrams).
        """
        padded = f"#{text}#"
        ngrams = set()
        for i in range(len(padded) - n + 1):
            ngrams.add(padded[i:i + n])
        return ngrams

    # =========================================================================
    # Matching stages
    # =========================================================================

    def exact_match(self, query: str) -> List[Tuple[str, float, str, List[Dict[str, str]]]]:
        """
        Stage 1: Exact phrase matching.

        Runs either the Aho-Corasick multi-pattern scan (enhanced mode) or
        a two-strategy Python fallback (non-enhanced mode).

        Aho-Corasick path (enhanced):
            A single O(n + m) scan over the query finds all alias patterns
            simultaneously.  n = query length, m = total alias characters.
            This is substantially faster than linear substring checks for
            catalogs with thousands of aliases.

        Fallback strategies (non-enhanced / Aho-Corasick unavailable):
            Strategy 1: O(1) hash lookup — if the full normalised query is
                        an exact key in exact_index, it is returned immediately
                        with score 1.0 and type "exact_full".
            Strategy 2: Phrase containment — iterate over exact_phrases
                        (sorted longest-first) and check whether the alias
                        appears as a complete phrase in the query.

        Filters applied to all paths:
        - Short aliases (≤ 4 chars) require word boundaries (space-padded
          check) to prevent "vm" or "db" matching inside longer tokens.
        - Phrase-containment aliases must cover at least MIN_COVERAGE_RATIO
          (40%) of the query length.  This stops a short generic alias like
          "z os" from dominating results when the query is a long specific
          product name.  A full exact match (alias == query) always passes.

        Returns:
            List of (alias, score=1.0, match_type, products), sorted by alias
            length descending (longest = most specific first).
        """
        query_norm = self.clean_string(query)
        query_buf = self.buffer(query_norm)    # " <query_norm> " for boundary checks
        query_len = max(len(query_norm), 1)
        matches: List[Tuple[str, float, str, List[Dict[str, str]]]] = []
        seen = set()  # deduplicate aliases across strategies

        # An alias must cover at least 40% of the query length to be accepted
        # as a phrase-containment match, OR it must appear as a whole word
        # (word-boundary match) in the query.  Full equality is always accepted.
        #
        # Rationale: a short specific acronym like "ilmt" or "db2" embedded in
        # a long question sentence ("How do I generate a PVU report in ILMT?")
        # is a valid product reference even though it covers < 40% of the query.
        # The word-boundary check (alias_buf in query_buf) already ensures we
        # are not matching a fragment inside a longer token, so coverage ratio
        # is not needed for those cases.
        MIN_COVERAGE_RATIO = 0.40

        def _coverage_ok(alias: str, word_boundary_confirmed: bool = False) -> bool:
            """True when alias satisfies the coverage ratio or is a confirmed word-boundary match."""
            if alias == query_norm:
                return True
            if word_boundary_confirmed:
                return True
            return len(alias) / query_len >= MIN_COVERAGE_RATIO

        # ── Aho-Corasick path ────────────────────────────────────────────
        if self.use_enhanced and self.ac_automaton:
            for end_index, (alias, products) in self.ac_automaton.iter(query_norm):
                if alias not in seen:
                    alias_buf = self.buffer(alias)
                    # Word-boundary check: alias must appear as a whole word in
                    # the query (not as a substring of a longer token).
                    # Applied to all aliases — short ones (≤ 4 chars) to avoid
                    # "vm" matching inside "WebSphere Virtual Machine", and longer
                    # ones to confirm the alias is a discrete phrase in the query.
                    if alias_buf not in query_buf:
                        continue
                    # Coverage ratio skipped when word boundary is confirmed:
                    # a short acronym like "ilmt" in a long sentence is valid.
                    if not _coverage_ok(alias, word_boundary_confirmed=True):
                        continue
                    seen.add(alias)
                    matches.append((alias, 1.0, "exact_phrase", products))
        else:
            # ── Fallback Strategy 1: O(1) full equality check ────────────
            if query_norm in self.exact_index:
                matches.append((query_norm, 1.0, "exact_full", self.exact_index[query_norm]))
                seen.add(query_norm)

            # ── Fallback Strategy 2: Phrase containment (longest-first) ──
            for alias, products in self.exact_phrases:
                if alias not in seen:
                    alias_buf = self.buffer(alias)
                    if alias_buf not in query_buf:
                        continue
                    # Coverage ratio skipped when word boundary is confirmed.
                    if not _coverage_ok(alias, word_boundary_confirmed=True):
                        continue
                    seen.add(alias)
                    matches.append((alias, 1.0, "exact_phrase", products))

        # Sort by alias length — the most specific (longest) alias leads.
        matches.sort(key=lambda x: len(x[0]), reverse=True)
        return matches

    def get_fuzzy_candidates(
        self,
        query_norm: str,
        max_candidates: int = 500,
        bm25_top_k: int = 20,
    ) -> Tuple[List[Tuple[int, str]], bool]:
        """
        Stage 3+4: BM25 candidate retrieval with N-gram augmentation.

        Returns a short list of (alias_index, alias_text) pairs that are then
        handed to RapidFuzz for accurate similarity re-scoring.  The goal is
        to narrow ~thousands of aliases down to ~20 strong candidates cheaply,
        so RapidFuzz's O(k·n) work stays bounded.

        Retrieval paths (tried in order):
        ─────────────────────────────────
        1. BM25 (enhanced mode):
           BM25Okapi.get_scores() weights each alias by term frequency,
           inverse document frequency, and document length normalisation.
           The top-20 aliases by BM25 score are selected.
           If fewer than 20 are found, N-gram augmentation fills the gap
           (handles misspelled tokens that BM25 would score 0).

        2. Token inverted index (non-enhanced or BM25 signal = 0):
           For each query token, the set of alias indices containing that
           token is unioned.  Candidates are sorted longest-first and capped
           at max_candidates.

        3. Scan-all fallback (no signal at all):
           When neither BM25 nor the token index finds any candidate the
           matcher scans all aliases.  This is the last resort for completely
           out-of-vocabulary queries.  used_fallback=True is returned to
           trigger a −0.15 confidence penalty downstream.

        Args:
            query_norm:     Already-normalised query string.
            max_candidates: Hard cap on returned candidates (safety limit).
            bm25_top_k:     Number of BM25 candidates to retrieve.
                            Default 20 mirrors the described pipeline spec.

        Returns:
            (candidates, used_fallback)
            candidates:    List of (alias_index, alias_text) tuples.
            used_fallback: True only when the scan-all fallback was used,
                           signalling the −0.15 confidence penalty.
        """
        # ── Path 1: BM25 ─────────────────────────────────────────────────
        if self.use_enhanced and self.bm25_index:
            query_tokens = self.tokenize(query_norm)
            if query_tokens:
                scores = self.bm25_index.get_scores(query_tokens)
                # Select top-k by score, filter out zero-score aliases
                # (those share no tokens with the query at all).
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

                # N-gram augmentation: when BM25 returns fewer candidates than
                # bm25_top_k (because the query has OOV tokens due to typos),
                # supplement with n-gram-overlap candidates.
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

        # ── Path 2: Token inverted index ─────────────────────────────────
        # Walk each query token through the inverted index and union all
        # matching alias IDs.  Handles the case where BM25 is not available
        # or the BM25 corpus is empty.
        tokens = self.tokenize(query_norm)
        candidate_ids: Set[int] = set()
        for token in tokens:
            candidate_ids.update(self.token_to_fuzzy_ids.get(token, set()))

        if candidate_ids:
            candidates = [(i, self.fuzzy_aliases[i]) for i in candidate_ids]
            # Sort longest-first as a heuristic: longer aliases tend to be
            # more specific and should be tried before generic short ones.
            candidates.sort(key=lambda x: len(x[1]), reverse=True)
            return candidates[:max_candidates], False

        # ── Path 3: Last-resort scan-all ─────────────────────────────────
        # The query shares zero tokens with any alias.  Scan all aliases so
        # RapidFuzz has something to work with, but signal the fallback so
        # the confidence scorer applies the −0.15 penalty.
        fallback_count = min(max_candidates, len(self.fuzzy_aliases))
        return [(i, self.fuzzy_aliases[i]) for i in range(fallback_count)], True

    def _get_ngram_candidates(
        self,
        query_norm: str,
        top_k: int = 100,
        min_overlap: float = 0.3,
    ) -> List[Tuple[int, str]]:
        """
        Retrieve candidates using character n-gram Jaccard overlap.

        Complements BM25 for typo-tolerant retrieval.  A misspelled token
        (e.g. "qraddar" for "qradar") shares no whole tokens with any alias,
        so BM25 scores it 0.  But "qraddar" and "qradar" share many trigrams
        (#qr, qra, rad, add/ada, dda/dar, ar#) giving a Jaccard score > 0.

        Args:
            query_norm:  Normalised query string.
            top_k:       Maximum candidates to return.
            min_overlap: Minimum Jaccard score (query∩alias / query∪alias)
                         required to include a candidate.  Default 0.3.

        Returns:
            List of (alias_index, alias_text) tuples sorted by Jaccard score.
        """
        query_ngrams = self._generate_ngrams(query_norm, self.ngram_size)
        if not query_ngrams:
            return []

        # Count how many query n-grams each alias shares.
        overlap_counts: Dict[int, int] = defaultdict(int)
        for ngram in query_ngrams:
            for doc_idx in self.ngram_index.get(ngram, set()):
                overlap_counts[doc_idx] += 1

        # Convert raw counts to Jaccard similarity and apply min_overlap filter.
        scored_candidates = []
        for doc_idx, overlap_count in overlap_counts.items():
            doc_text = self.fuzzy_aliases[doc_idx]
            doc_ngrams = self._generate_ngrams(doc_text, self.ngram_size)
            union_size = len(query_ngrams | doc_ngrams)
            if union_size > 0:
                jaccard = overlap_count / union_size
                if jaccard >= min_overlap:
                    scored_candidates.append((doc_idx, doc_text))

        return scored_candidates[:top_k]

    def fuzzy_match(
        self,
        query: str,
        threshold: float = 0.70,
        limit: int = 30,
        scorer=None,
    ) -> Tuple[List[Tuple[str, float, str, List[Dict[str, str]]]], bool]:
        """
        Stage 5: RapidFuzz similarity re-scoring.

        Implements the core of the described pipeline:
            User Query → Normalisation → BM25 Top-20 Candidates → RapidFuzz Re-score

        The scorer used by default is ``fuzz.token_sort_ratio`` which:
        - Sorts the tokens of both strings alphabetically before comparing.
        - Handles word-order variations: "Server Application WebSphere" scores
          identically to "WebSphere Application Server".
        - Is more robust than partial_ratio for multi-token IBM product names
          where the full phrase should match, not just a substring.

        The legacy ``wml_product_identification`` method passes
        ``fuzz.partial_ratio`` instead — that scorer is better for single-
        keyword substring queries and is kept for backward compatibility.

        Args:
            query:     Normalised query string (pre-processed by callers).
            threshold: Minimum similarity score (0.0–1.0).  Candidates below
                       this score are discarded.  Default 0.70.
            limit:     Maximum number of matches to return.  Default 30.
            scorer:    RapidFuzz scorer callable.  Defaults to token_sort_ratio.

        Returns:
            (matches, used_fallback)
            matches:      List of (alias, score 0.0–1.0, "fuzzy", products).
            used_fallback: Propagated from get_fuzzy_candidates; True when the
                           scan-all fallback was used.
        """
        if scorer is None:
            scorer = fuzz.token_sort_ratio

        query_norm = self.clean_string(query)

        # Retrieve BM25/token/n-gram candidate list.
        # used_fallback=True means no BM25 or token signal → −0.15 penalty.
        candidates, used_fallback = self.get_fuzzy_candidates(query_norm)

        if not candidates:
            return [], False

        # Build a flat text list and a reverse-lookup map for routing.
        # candidate_map: alias_text → index in fuzzy_routes
        candidate_texts = [candidate_text for _, candidate_text in candidates]
        candidate_map = {candidate_text: idx for idx, candidate_text in candidates}

        # RapidFuzz process.extract runs the scorer in C++ over the short
        # candidate list.  score_cutoff is on the 0–100 integer scale that
        # RapidFuzz uses internally; the threshold parameter is on 0.0–1.0.
        extracted = process.extract(
            query_norm,
            candidate_texts,
            scorer=scorer,
            score_cutoff=int(threshold * 100),
            limit=limit,
        )

        # Convert from RapidFuzz's 0–100 scale back to 0.0–1.0.
        matches = []
        for alias, score, _ in extracted:
            idx = candidate_map[alias]
            matches.append((alias, score / 100.0, "fuzzy", self.fuzzy_routes[idx]))

        # Primary sort: score DESC.  Secondary: alias length DESC so that
        # longer (more specific) aliases win ties over short generic ones.
        matches.sort(key=lambda x: (x[1], len(x[0])), reverse=True)
        return matches, used_fallback

    def get_all_matches(
        self,
        query: str,
        fuzzy_threshold: float = 0.70,
        fuzzy_limit: int = 30,
    ) -> Tuple[List[Tuple[str, float, str, List[Dict[str, str]]]], bool]:
        """
        Orchestrate exact + fuzzy matching with smart heuristics.

        Combines all matching stages into a single ranked list:

        Step 1 — Exact-match section hits (Aho-Corasick / fallback).
        Step 2 — Fuzzy-section key promotion.
            a. Full query or individual tokens equal to a fuzzy-section key
               → promoted to "exact_full" (highest confidence).
               Handles: "cognos" as a standalone query; "cloud_pak for data"
               as the full normalised form.
            b. Noise-stripped query matches a noise-stripped fuzzy alias key
               → promoted to "exact_full".
               Handles: "websphere application server for z/os" where the
               stopword "for" is stripped from both query and alias key.
               Guard: only applied when noise-stripped query has ≥ 3 tokens
               (short queries like "z/os" must use the standard exact path).
            c. Non-noise-stripped full form checked against fuzzy_alias_index.
               Handles: "cloud pak for data" where the stopword "for" is part
               of the key and noise-stripping would break the lookup.
        Step 3 — Fuzzy scoring (BM25 → N-gram → RapidFuzz).
            Skipped for machine codes (numeric-dominated strings).
            Skipped for very short queries (≤ 5 chars) to avoid noise.
        Step 4 — Unified ranking: exact > fuzzy, then score, then alias length.

        Returns:
            (matches, used_fallback)
        """
        # Compute two normalised forms of the query:
        #   query_norm       — noise-stripped (used for exact-section and fuzzy scoring)
        #   query_norm_full  — not noise-stripped (used for full-phrase key lookups)
        query_norm = self.clean_string(query, remove_noise=True)
        query_norm_full = self.clean_string(query, remove_noise=False)

        # ── Step 1: Exact-match section ───────────────────────────────────
        matches = self.exact_match(query_norm)
        exact_aliases_seen = {alias for alias, _, _, _ in matches}

        # ── Step 2: Fuzzy-section key promotion ───────────────────────────
        # Build the set of terms to check against fuzzy_alias_index:
        #   - noise-stripped form
        #   - non-noise-stripped full form   (Step 2c)
        #   - individual tokens              (e.g. "cognos" from "how to install cognos")
        candidates_for_exact = {query_norm, query_norm_full} | set(self.tokenize(query_norm))
        for term in candidates_for_exact:
            if term in self.fuzzy_alias_index and term not in exact_aliases_seen:
                idx = self.fuzzy_alias_index[term]
                matches.append((term, 1.0, "exact_full", self.fuzzy_routes[idx]))
                exact_aliases_seen.add(term)

        # Step 2b: Noise-stripped alias lookup.
        # The noise-stripped query is looked up in fuzzy_alias_noisestripped_index.
        # Minimum token guard (_NS_MIN_TOKENS=3) prevents this path from
        # hijacking short queries like "z/os" (1 token) which must surface
        # the correct z/OS SLC code via the standard exact path, not via a
        # noise-stripped match that could resolve to a different product.
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

        # ── Step 3: Fuzzy scoring ─────────────────────────────────────────
        # Skip for machine codes (part numbers like "5724A12") — fuzzy scoring
        # would produce many false positives against short product codes.
        # Skip for very short queries (≤ 5 chars) — RapidFuzz scores for short
        # strings are noisy and exact matching is sufficient.
        should_fuzzy = (
            not self.is_machine_code(query_norm) and
            not self.is_small_query(query_norm)
        )

        used_fallback = False
        if should_fuzzy:
            fuzzy_results, used_fallback = self.fuzzy_match(
                query_norm, threshold=fuzzy_threshold, limit=fuzzy_limit
            )
            # Only append fuzzy results whose alias was not already captured
            # in an exact-match step — avoid duplicate confidence calculations.
            for alias, score, match_type, products in fuzzy_results:
                if alias not in exact_aliases_seen:
                    matches.append((alias, score, match_type, products))

        # ── Step 4: Ranking ───────────────────────────────────────────────
        # Exact matches always rank above fuzzy matches.
        # Within each tier: higher score first, then longer alias first.
        def rank_key(item):
            alias, score, match_type, _ = item
            exact_priority = 1 if match_type.startswith("exact") else 0
            return (exact_priority, score, len(alias))

        matches.sort(key=rank_key, reverse=True)
        return matches, used_fallback

    # =========================================================================
    # Main public API
    # =========================================================================

    def identify_products(
        self,
        query: str,
        fuzzy_threshold: float = 0.70,
        return_count: int = 10,
        fuzzy_limit: int = 30,
        char_limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """
        Identify IBM products from free text and return ranked results with SLC codes.

        This is the primary entry point for the matching pipeline.  It runs
        all matching stages, groups results by SLC_CODE, computes confidence
        scores, and returns the top-ranked products.

        Full pipeline
        ─────────────
        1. Truncate query to char_limit (safety guard for very long support cases).
        2. Normalisation + alias expansion via clean_string().
        3. Exact matching (Aho-Corasick / hash / phrase-containment).
        4. Fuzzy-section key promotion to exact_full.
        5. BM25 → N-gram → RapidFuzz fuzzy matching.
        6. Group matches by SLC_CODE — aggregate scores, collect aliases.
        7. Compute tie-break signals (_best_alias_len, _name_overlap).
        8. Calculate ConfidenceScorer scores (base ± penalties ± boosts).
        9. Compute alias_similarity (fuzz.ratio of query vs each alias,
           using internal normalised form to avoid underscore/space mismatch).
        10. Multi-level ranking: exact > confidence > score > alias_similarity
            > alias length > product-name token overlap.
        11. Trim to return_count, clean up internal fields.
        12. Optional SBERT re-ranking (if USE_SBERT_RERANKER=true).

        Args:
            query:          Raw user query or support case text.
            fuzzy_threshold: Minimum RapidFuzz score to accept (0.0–1.0).
                             Default 0.70.
            return_count:   Maximum number of products to return.  Default 10.
            fuzzy_limit:    Maximum fuzzy candidates to score.  Default 30.
            char_limit:     Maximum characters to process from the query.
                            Truncates very long support-case bodies.  Default 1000.

        Returns:
            List of dicts, each representing one identified product:
            {
              "score":           float,       # Raw match score (0.0–1.0)
              "confidence":      float,       # Confidence score (0.00–1.00)
              "product_code":    str,         # SLC_CODE
              "product_name":    str | None,  # PRODUCT_NAME
              "matched_aliases": List[str],   # Human-readable aliases
              "match_types":     List[str],   # e.g. ["exact_full", "fuzzy"]
              "alias_similarity": float,      # Best ratio() vs any matched alias
            }
            Sorted by confidence descending.  Empty list if no matches.
        """
        # Safety truncation — prevents pathologically long support-case text
        # from dominating BM25 token weights.
        query = str(query)[:char_limit]

        # Run all matching stages; used_fallback=True when the scan-all
        # fallback was triggered (→ −0.15 penalty in confidence scorer).
        matches, used_fallback = self.get_all_matches(
            query=query,
            fuzzy_threshold=fuzzy_threshold,
            fuzzy_limit=fuzzy_limit,
        )

        # ── Group matches by SLC_CODE ─────────────────────────────────────
        # Each alias may map to multiple products; each product contributes
        # to exactly one group keyed by its SLC_CODE.  Within a group:
        #   score    — maximum score across all aliases that resolved to it
        #   best_match_alias/type — alias and match_type for the best score
        #              (used by ConfidenceScorer; cleaned up before return)
        #   matched_aliases — ordered list: exact aliases first, fuzzy after
        #   match_types     — union of all match types seen for this product
        def _default_group() -> Dict[str, Any]:
            return {
                "score": 0.0,
                "product_code": None,
                "product_name": None,
                "matched_aliases": [],
                "match_types": set(),
                "best_match_alias": None,
                "best_match_type": None,
                # product_count: how many distinct SLC codes the best alias key
                # resolves to.  >1 means the alias is shared → ambiguity signal
                # used by ConfidenceScorer.
                "best_match_alias_product_count": 1,
            }

        grouped: Dict[str, Dict[str, Any]] = defaultdict(_default_group)

        for alias, score, match_type, products in matches:
            for product in products:
                code = product.get("SLC_CODE")
                name = product.get("PRODUCT_NAME")

                if not code:
                    # Skip malformed dictionary entries (caught by startup
                    # validation, but guard here for safety).
                    continue

                item = grouped[code]
                item["product_code"] = code
                item["product_name"] = name

                # Update best-match tracking.
                # Prefer exact over fuzzy when scores are tied: a score of 1.0
                # from an exact match is qualitatively different from 1.0 via
                # fuzzy (which can happen for very short aliases).
                current_is_exact = (
                    item["best_match_type"] and
                    item["best_match_type"].startswith("exact")
                )
                new_is_exact = match_type.startswith("exact")
                if (
                    (score > item["score"]) or
                    (score == item["score"] and new_is_exact and not current_is_exact)
                ):
                    item["score"] = round(score, 2)
                    item["best_match_alias"] = alias
                    item["best_match_type"] = match_type
                    item["best_match_alias_product_count"] = len(products)

                # Accumulate unique matched aliases for the response.
                # Internal delimiter-joined forms (e.g. "cloud_pak") are
                # converted to display form ("cloud pak") via _display_alias
                # before being stored, so the API response is human-readable.
                # Exact-match aliases are prepended (highest signal first);
                # fuzzy aliases are appended.
                display = self._display_alias(alias)
                if display not in item["matched_aliases"]:
                    if new_is_exact:
                        item["matched_aliases"].insert(0, display)
                    else:
                        item["matched_aliases"].append(display)

                item["match_types"].add(match_type)

        # ── Per-product confidence + tie-break signals ────────────────────
        results = []
        total_candidates = len(grouped)

        # Pre-compute query token set once — shared by name_overlap below.
        query_tokens_for_overlap = set(self.clean_string(query).split())

        for _, item in grouped.items():
            item["match_types"] = sorted(list(item["match_types"]))

            # Tie-break signal 1: best_match_alias length.
            # A longer matching alias indicates a more specific hit.
            # e.g. "web_sphere application server for ibm i" (39 chars) is
            # more specific than "ibm i" (5 chars) at equal score.
            best_alias_len = len(item.get("best_match_alias") or "")

            # Tie-break signal 2: product-name token overlap.
            # Last-resort for products that share the exact same best alias
            # (e.g. SCPF9 and SCPL5 both map to "web_sphere application server
            # for ibm i").  The product whose full name overlaps more with the
            # query wins: "WebSphere Application Server for IBM i" (5 overlap
            # tokens) beats "IBM i" (2 overlap tokens).
            name_tokens = set(self.clean_string(item.get("product_name") or "").split())
            name_overlap = len(query_tokens_for_overlap & name_tokens)

            # ConfidenceScorer: base score ± penalties ± contextual boosts.
            # See confidence_scorer.py for the full scoring specification.
            if self.enable_confidence_scoring and self.confidence_scorer:
                confidence = self.confidence_scorer.calculate_confidence(
                    match_type=item["best_match_type"],
                    match_score=item["score"],
                    query=query,
                    matched_alias=item["best_match_alias"],
                    product_count=item["best_match_alias_product_count"],
                    candidate_count=total_candidates,
                    product_code=item["product_code"],
                    used_fallback=used_fallback,
                )
                item["confidence"] = confidence
            else:
                # Confidence scoring disabled — fall back to raw match score.
                item["confidence"] = round(item["score"], 2)

            # Remove internal tracking fields — they must not appear in the
            # API response, but ConfidenceScorer needed them above.
            del item["best_match_alias"]
            del item["best_match_type"]
            del item["best_match_alias_product_count"]

            # Store tie-break signals so the sort key below is a pure reader.
            # These are explicitly popped after sorting.
            item["_best_alias_len"] = best_alias_len
            item["_name_overlap"] = name_overlap

            results.append(item)

        # ── alias_similarity ──────────────────────────────────────────────
        # Compute the highest fuzz.ratio() between the normalised query and
        # any of the product's matched aliases.
        #
        # Why re-normalise each alias here:
        #   matched_aliases stores display-form values (spaces, no underscores)
        #   because _display_alias() was applied when the alias was inserted.
        #   query_norm_for_rank retains internal delimiter-joined underscores
        #   (e.g. "cloud_pak").  Comparing "cloud_pak for data" against
        #   "cloud pak for data" (spaces) would give a lower ratio than
        #   comparing against the same canonical form.  Re-normalising via
        #   clean_string() restores the underscore form for a fair comparison.
        #
        # Usage:
        #   - Tie-breaking in the ranking key below.
        #   - TLSChecker.check_results() uses it for the tolerance window.
        query_norm_for_rank = self.clean_string(query)

        for item in results:
            item["alias_similarity"] = round(
                max(
                    (
                        fuzz.ratio(query_norm_for_rank, self.clean_string(a))
                        for a in item["matched_aliases"]
                    ),
                    default=0.0,
                ),
                2,
            )

        # ── Final multi-level ranking ─────────────────────────────────────
        # Priority (descending):
        #   1. exact_priority   — products with any exact match beat all fuzzy-only
        #   2. confidence       — primary routing signal
        #   3. score            — raw match score (exact = 1.0, fuzzy < 1.0)
        #   4. alias_similarity — fine-grained: "storage fusion" (sim=100) beats
        #                          "storage fusion hci physical appliance" (sim=50)
        #   5. best_alias_len   — longer alias = more specific
        #   6. name_overlap     — product-name token overlap as last resort
        #
        # The sort key is a pure function — tie-break signals are already on
        # each item dict and will be popped after the sort.
        def result_rank(item):
            has_exact = any(mt.startswith("exact") for mt in item["match_types"])
            exact_priority = 1 if has_exact else 0
            return (
                exact_priority,
                item["confidence"],
                item["score"],
                item["alias_similarity"],
                item["_best_alias_len"],
                item["_name_overlap"],
            )

        results.sort(key=result_rank, reverse=True)

        # Remove tie-break signals — they are internal and must not leak into
        # the API response.
        for item in results:
            item.pop("_best_alias_len", None)
            item.pop("_name_overlap", None)

        # Trim to the requested return count AFTER sorting so the top-N are
        # the highest-ranked, not the first-grouped.
        results = results[:return_count]

        # ── Optional SBERT re-ranking ─────────────────────────────────────
        # Runs after all lexical/fuzzy logic and confidence scoring are done.
        # The re-ranker re-sorts the already-trimmed list using a weighted
        # blend of the existing confidence score and a cross-encoder relevance
        # score.  It adds "sbert_score" to each result for transparency.
        # Enabled via USE_SBERT_RERANKER=true in the environment.
        if self.sbert_reranker is not None and self.sbert_reranker.loaded:
            results = self.sbert_reranker.rerank(query, results)

        return results

    def wml_product_identification(
        self,
        query: str,
        threshold: Optional[float] = None,
        return_count: Optional[int] = None,
        scorer=fuzz.partial_ratio,
        char_limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """
        Legacy API compatibility method.

        Provides the same interface as the original FuzzyMatch class so that
        older callers do not need to be updated when the new pipeline is
        deployed.  The only behavioural difference is that this now calls the
        full hybrid pipeline internally instead of plain rapidfuzz.

        Field mapping (legacy → current):
            support_desc  ← product_name
            support_alias ← matched_aliases
            score         ← score   (unchanged)
            product_code  ← product_code (unchanged)
        """
        results = self.identify_products(
            query=query,
            fuzzy_threshold=threshold or 0.70,
            return_count=return_count or 10,
            char_limit=char_limit,
        )

        # Rename fields to match the legacy response format expected by
        # downstream consumers that were built against the original API.
        legacy_results = []
        for result in results:
            legacy_results.append({
                "score": result["score"],
                "product_code": result["product_code"],
                "support_desc": result["product_name"],
                "support_alias": result["matched_aliases"],
            })

        return legacy_results


# Made with Bob
