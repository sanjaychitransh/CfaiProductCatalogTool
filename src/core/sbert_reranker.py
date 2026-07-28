"""
Sentence-BERT Retrieval + Cross-Encoder Re-Ranking Module

Architecture:
1. Sentence-BERT (bi-encoder): Encode all product aliases once at startup
   into dense vector embeddings; at query time encode the query and retrieve
   the top-k candidates by cosine similarity (semantic nearest-neighbour).
2. Cross-Encoder Re-ranking: Score each (query, candidate-alias) pair with a
   cross-encoder model that reads both strings jointly — much more accurate
   than bi-encoder dot-product but too slow to run over all aliases.

This module is intentionally isolated:
- It adds a *semantic re-ranking stage* on top of the existing
  Aho-Corasick → BM25 → RapidFuzz pipeline.
- It does NOT modify any existing matching logic.
- The caller (ProductMatcher) passes its current result list in; this module
  re-orders it and returns it.  If the models are unavailable the original
  order is returned unchanged.

Usage (in ProductMatcher.identify_products):
    if self.sbert_reranker:
        results = self.sbert_reranker.rerank(query, results)

Environment variables:
    USE_SBERT_RERANKER  — set to "true" to enable (default: false)
    SBERT_MODEL         — bi-encoder model name (default: all-MiniLM-L6-v2)
    CROSS_ENCODER_MODEL — cross-encoder model name
                          (default: cross-encoder/ms-marco-MiniLM-L-6-v2)
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional heavy imports — graceful fallback when sentence-transformers is
# not installed (e.g. production images that exclude the package).
# ---------------------------------------------------------------------------
try:
    import numpy as np
    from sentence_transformers import SentenceTransformer, CrossEncoder

    _SBERT_AVAILABLE = True
except ImportError:
    _SBERT_AVAILABLE = False
    np = None  # type: ignore
    SentenceTransformer = None  # type: ignore
    CrossEncoder = None  # type: ignore


class SBERTReranker:
    """
    Semantic re-ranking stage using Sentence-BERT + Cross-Encoder.

    Two-phase ranking:
    ──────────────────
    1. **Bi-encoder retrieval** (Sentence-BERT):
       Compute cosine similarity between the query embedding and all
       pre-computed alias embeddings.  Returns top-``bi_top_k`` candidates.
       Fast: O(1) per query once embeddings are cached.

    2. **Cross-encoder re-ranking**:
       Score each (query, alias) pair jointly — the cross-encoder sees both
       strings at once, producing much more accurate relevance scores.
       Slow: O(k) per query; kept manageable by limiting input to bi_top_k.

    The final ordering merges the cross-encoder score with the upstream
    confidence score via a weighted blend so that strong lexical matches
    (exact_phrase, exact_full) are never accidentally pushed down by the
    semantic scorer.
    """

    # Weight given to the cross-encoder score in the blended final score.
    # 1.0 = pure cross-encoder; 0.0 = original confidence unchanged.
    CROSS_ENCODER_WEIGHT: float = 0.35

    def __init__(
        self,
        bi_encoder_model: str = "all-MiniLM-L6-v2",
        cross_encoder_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
        bi_top_k: int = 20,
    ) -> None:
        """
        Initialise and load models.

        Args:
            bi_encoder_model:   Sentence-BERT model name or path.
            cross_encoder_model: Cross-Encoder model name or path.
            bi_top_k:           Number of candidates forwarded to the
                                cross-encoder from the bi-encoder stage.
        """
        self.bi_top_k = bi_top_k
        self._loaded = False

        if not _SBERT_AVAILABLE:
            logger.warning(
                "sentence-transformers is not installed. "
                "SBERTReranker will pass results through unchanged."
            )
            return

        try:
            logger.info("Loading Sentence-BERT model: %s", bi_encoder_model)
            self._bi_encoder: SentenceTransformer = SentenceTransformer(bi_encoder_model)

            logger.info("Loading Cross-Encoder model: %s", cross_encoder_model)
            self._cross_encoder: CrossEncoder = CrossEncoder(cross_encoder_model)

            self._loaded = True
            logger.info("SBERTReranker models loaded successfully.")
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to load SBERT/Cross-Encoder models — reranker disabled. "
                "Error: %s",
                exc,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def loaded(self) -> bool:
        """True if both models were loaded successfully."""
        return self._loaded

    def rerank(
        self,
        query: str,
        results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Re-rank *results* using Sentence-BERT + Cross-Encoder.

        The existing fields of each result dict are preserved.  A new field
        ``sbert_score`` (float 0–1) is added to every result for transparency.

        Args:
            query:   Original (or normalized) user query.
            results: List of product dicts as returned by
                     ``ProductMatcher.identify_products``.  Each dict must
                     contain at least ``matched_aliases`` (list[str]) and
                     ``confidence`` (float).

        Returns:
            Re-ordered result list (same dicts, new ``sbert_score`` field).
            If models are not loaded the original list is returned unchanged
            (``sbert_score`` is set to ``None`` for each item).
        """
        if not results:
            return results

        if not self._loaded:
            for r in results:
                r.setdefault("sbert_score", None)
            return results

        # ── Stage 1: Bi-encoder retrieval ────────────────────────────────
        # Flatten all aliases → keep track of which result they belong to.
        alias_index: List[Tuple[int, str]] = []  # (result_idx, alias_text)
        for result_idx, result in enumerate(results):
            for alias in result.get("matched_aliases", []):
                alias_index.append((result_idx, alias))

        if not alias_index:
            for r in results:
                r.setdefault("sbert_score", None)
            return results

        alias_texts = [a for _, a in alias_index]

        # Encode query and all aliases
        query_emb = self._bi_encoder.encode(query, convert_to_numpy=True)
        alias_embs = self._bi_encoder.encode(
            alias_texts, convert_to_numpy=True, batch_size=64
        )

        # Cosine similarity (L2-normalise for numerical stability)
        query_emb = query_emb / (np.linalg.norm(query_emb) + 1e-9)
        alias_embs = alias_embs / (
            np.linalg.norm(alias_embs, axis=1, keepdims=True) + 1e-9
        )
        cos_sims: np.ndarray = alias_embs @ query_emb  # shape (N,)

        # Pick top bi_top_k alias indices by cosine similarity
        top_k = min(self.bi_top_k, len(alias_texts))
        top_alias_indices = np.argsort(cos_sims)[::-1][:top_k].tolist()

        # ── Stage 2: Cross-encoder re-ranking ────────────────────────────
        ce_pairs = [(query, alias_texts[i]) for i in top_alias_indices]
        ce_scores: List[float] = self._cross_encoder.predict(ce_pairs).tolist()

        # Normalise cross-encoder scores to [0, 1] via sigmoid
        def _sigmoid(x: float) -> float:
            return 1.0 / (1.0 + math.exp(-x))

        ce_scores_norm = [_sigmoid(s) for s in ce_scores]

        # ── Aggregate scores per result ───────────────────────────────────
        # Keep the *best* cross-encoder score across all aliases of each result
        # that appeared in the bi-encoder top-k.
        result_ce_score: Dict[int, float] = {i: 0.0 for i in range(len(results))}

        for alias_list_idx, ce_score in zip(top_alias_indices, ce_scores_norm):
            result_idx, _ = alias_index[alias_list_idx]
            if ce_score > result_ce_score[result_idx]:
                result_ce_score[result_idx] = ce_score

        # Attach sbert_score and compute blended score
        w = self.CROSS_ENCODER_WEIGHT
        for result_idx, result in enumerate(results):
            ce = result_ce_score[result_idx]
            result["sbert_score"] = round(ce, 4)
            conf = result.get("confidence", 0.0)
            # Exact matches keep lexical priority — halve blend weight for them
            has_exact = any(
                mt.startswith("exact") for mt in result.get("match_types", [])
            )
            blend_weight = w * 0.5 if has_exact else w
            result["_blended"] = (1.0 - blend_weight) * conf + blend_weight * ce

        # ── Final sort: blended score descending ─────────────────────────
        results.sort(key=lambda r: r.pop("_blended", 0.0), reverse=True)

        return results


# Made with Bob
