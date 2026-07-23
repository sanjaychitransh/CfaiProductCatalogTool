"""
Acronym / Abbreviation Expander

Purpose
-------
When the full pipeline (BM25 + RapidFuzz + LLM reranking) returns NO results
for a short query that looks like an acronym or abbreviation (e.g. "p4d",
"cp4d", "icp4d", "hcpud"), this module generates candidate expansions and
re-runs the existing matcher on each candidate until at least one result is
found.

Design constraints (MUST NOT be violated)
------------------------------------------
- Zero changes to ProductMatcher, ConfidenceScorer, or LLMReranker logic.
- The expander is called ONLY when identify_products() returns an empty list.
- Normal (non-empty) pipeline results are never touched.
- Each expansion candidate is passed through the full existing pipeline
  (identify_products) so all scoring, confidence, and reranking rules apply
  unchanged.
- Only queries that are:
    * Pure alphabetic / alphanumeric tokens (no spaces, ≤ MAX_ACRONYM_LEN chars)
    * Do NOT already produce results
  are eligible for expansion.
- MAX_EXPANSIONS caps the total number of pipeline calls to keep latency bounded.

Two expansion strategies (tried in order — earlier strategy has priority):
─────────────────────────────────────────────────────────────────────────────
Strategy 1 — INSERTION  (primary, handles the "p4d" → "cp4d" case)
  Insert one letter (a–z) at every possible position (0 … len(token)).
  This handles acronyms where the user typed a prefix/suffix of the real key.
  Example: "p4d"  →  "ap4d", "bp4d", "cp4d" …  "p4da", "p4db" … "p4dz"
  Each insertion point × 26 letters = (len+1)×26 candidates.

Strategy 2 — SUBSTITUTION  (secondary, handles "hcpud" → "cp4d"-like typos)
  Replace one alphabetic character at a time with every letter a–z.
  Numeric characters are never changed (they are part of product codes).
  Example: "cp4d"  →  "ap4d", "bp4d" …  "ca4d" …  "cp4a", "cp4b" …
  Each alphabetic position × 25 letters (skip original) = up to 25n candidates.

Candidates within each strategy are sorted lexicographically so the search
is deterministic and reproducible.  The original token is never included.
MAX_EXPANSIONS caps the total number of pipeline calls across both strategies.
"""

from __future__ import annotations

import string
from typing import Any, Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants — tune here, nowhere else
# ---------------------------------------------------------------------------

# Only expand queries up to this many characters (longer strings are not acronyms)
MAX_ACRONYM_LEN: int = 8

# Maximum number of expansion candidates to try before giving up
# Default covers (len+1)*26 insertions for a 4-char token = 130, which is safe.
MAX_EXPANSIONS: int = 208  # (8+1)*26 worst-case insertions for MAX_ACRONYM_LEN

_ALPHABET: str = string.ascii_lowercase  # 'a' .. 'z'


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def is_expansion_candidate(query: str) -> bool:
    """
    Return True when a query token is a candidate for acronym expansion.

    Rules:
    - No whitespace (single token only)
    - At least 2 characters, at most MAX_ACRONYM_LEN characters
    - Contains at least one alphabetic character (pure digit strings are skipped)
    - Contains at most one word (no space-separated phrases)
    """
    q = query.strip()
    if not q or " " in q:
        return False
    if len(q) < 2 or len(q) > MAX_ACRONYM_LEN:
        return False
    if not any(c.isalpha() for c in q):
        return False
    return True


def _alpha_positions(token: str) -> List[int]:
    """Return indices of all alphabetic characters in the token."""
    return [i for i, c in enumerate(token) if c.isalpha()]


def _insertion_candidates(token: str) -> List[str]:
    """
    Strategy 1: INSERT one letter (a–z) at every position in *token*.

    For a token of length n there are (n+1) insertion points × 26 letters
    = (n+1)*26 candidates.

    Example: "p4d" (length 3, insertion points 0..3)
      pos 0 → "ap4d", "bp4d", "cp4d", … "zp4d"
      pos 1 → "pа4d", "pb4d", … "pz4d"
      pos 2 → "p4ad", "p4bd", … "p4zd"
      pos 3 → "p4da", "p4db", … "p4dz"

    The original token is excluded from the result.
    Candidates are sorted: insertion-point first, then lexicographic.
    """
    token_lower = token.lower()
    seen: set = set()
    candidates: List[Tuple[int, str]] = []  # (insertion_pos, candidate)

    for pos in range(len(token_lower) + 1):
        for letter in _ALPHABET:
            candidate = token_lower[:pos] + letter + token_lower[pos:]
            if candidate not in seen and candidate != token_lower:
                seen.add(candidate)
                candidates.append((pos, candidate))

    candidates.sort(key=lambda x: (x[0], x[1]))
    return [c for _, c in candidates]


def _substitution_candidates(token: str) -> List[str]:
    """
    Strategy 2: SUBSTITUTE one alphabetic character at a time with every letter a–z.

    Numeric characters are never changed (they carry product-code signal).
    The original character is skipped (no point re-trying it).
    The original token itself is excluded.
    Candidates are sorted lexicographically.
    """
    token_lower = token.lower()
    positions = _alpha_positions(token_lower)
    seen: set = set()
    candidates: List[Tuple[int, str]] = []  # (position, candidate)

    for pos in positions:
        for letter in _ALPHABET:
            if letter == token_lower[pos]:
                continue  # skip identical — it's the original character
            chars = list(token_lower)
            chars[pos] = letter
            candidate = "".join(chars)
            if candidate not in seen and candidate != token_lower:
                seen.add(candidate)
                candidates.append((pos, candidate))

    candidates.sort(key=lambda x: (x[0], x[1]))
    return [c for _, c in candidates]


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

MatcherCallable = Callable[..., List[Dict[str, Any]]]


def expand_and_search(
    query: str,
    identify_products_fn: MatcherCallable,
    fuzzy_threshold: float = 0.70,
    return_count: int = 10,
    fuzzy_limit: int = 30,
    enable_llm_reranking: bool = True,
    max_expansions: int = MAX_EXPANSIONS,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    Try letter-insertion then letter-substitution expansions of *query* until
    results are found.

    Strategy order
    --------------
    1. Insertion  — insert one letter a–z at every position (handles "p4d" → "cp4d")
    2. Substitution — replace one alphabetic character a–z (handles typos like "hcpud")

    Each candidate is fed through the full unmodified identify_products() pipeline.
    The first expansion that returns a non-empty result set wins; the loop stops.

    Parameters
    ----------
    query : str
        The original query that produced zero results from the main pipeline.
    identify_products_fn : callable
        Reference to ``ProductMatcher.identify_products`` — called unmodified.
    fuzzy_threshold, return_count, fuzzy_limit, enable_llm_reranking :
        Forwarded unchanged to ``identify_products_fn``.
    max_expansions : int
        Hard cap on total pipeline calls across both strategies.

    Returns
    -------
    (results, matched_expansion)
        results           — the first non-empty result list found, or [] if
                            no expansion yielded results within the cap.
        matched_expansion — the expansion string that produced results, or
                            None if none succeeded.
    """
    if not is_expansion_candidate(query):
        return [], None

    # Build the ordered candidate list: insertions first, substitutions second.
    # Dedup across strategies so we never call the pipeline twice for the same string.
    seen_global: set = set()
    ordered: List[str] = []

    for candidate in _insertion_candidates(query) + _substitution_candidates(query):
        if candidate not in seen_global:
            seen_global.add(candidate)
            ordered.append(candidate)

    for expansion in ordered[:max_expansions]:
        results = identify_products_fn(
            query=expansion,
            fuzzy_threshold=fuzzy_threshold,
            return_count=return_count,
            fuzzy_limit=fuzzy_limit,
            enable_llm_reranking=enable_llm_reranking,
        )
        if results:
            return results, expansion

    return [], None


# Made with Bob
