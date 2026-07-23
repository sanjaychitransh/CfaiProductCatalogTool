"""
LLM Reranking Module for Product Search Pipeline.

Pipeline position:
    BM25 + RapidFuzz → Top 10 candidates → take Top 5 → LLM rerank → reranked results
                                                              ↓ (failure)
                                                     BM25 + RapidFuzz results

Design principles:
  - The LLM ONLY reorders candidates by relevance — it never assigns or invents
    confidence scores.  Confidence is always computed by ConfidenceScorer using
    the fixed rule-set:
        Base score  →  exact single-product : 0.90
                       exact multi-product  : 0.75
                       substring (long key) : 0.70
                       token overlap only   : 0.50
        Penalties   →  -0.10 multiple candidates remain
                       -0.10 generic terms
                       -0.15 fallback token logic
        Boosts      →  +0.05 platform/version keywords
                       +0.05 session-confirmed product
                       +0.05 model number mentioned
        Cap         →  1.00
  - The LLM is ONLY allowed to return product_id (SLC_CODE) values that were
    supplied in the candidate list. This prevents hallucination of new codes.
  - Exact-match results always retain their top position regardless of LLM order.
  - Any product_id not present in the original candidate set is silently dropped.
  - On any failure (network, timeout, JSON parse, empty response) the caller
    receives (original_results, False) — the BM25+RapidFuzz results pass through
    unchanged.

Conditional confidence boost rule (applied in matcher.py, NOT here):
  A +0.05 boost is applied to the top result ONLY when ALL of these hold:
    1. LLM reranking succeeded (reranked_by_llm = True)
    2. The LLM's #1 pick agrees with BM25+RapidFuzz's #1 pick
       (both retrieval signals independently converge on the same product)
    3. The top result is a fuzzy match (exact matches are already 0.90+ and
       do not need further inflation)
    4. After the boost the score is capped at 1.00
  This is NOT a blanket "LLM ran → +0.10" rule.  The boost is earned only
  when the LLM provides strong discriminating evidence that agrees with
  the lexical retrieval signal.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider availability probing (graceful fallback if SDK not installed)
# ---------------------------------------------------------------------------
try:
    from ibm_watsonx_ai import Credentials
    from ibm_watsonx_ai.foundation_models import ModelInference
    _WATSONX_AVAILABLE = True
except ImportError:
    _WATSONX_AVAILABLE = False
    Credentials = None
    ModelInference = None

try:
    import openai as _openai_module
    _OPENAI_AVAILABLE = True
except ImportError:
    _OPENAI_AVAILABLE = False
    _openai_module = None


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

_RERANK_PROMPT = """You are an IBM product-search reranking assistant. Your only job is to reorder the candidate list from most relevant to least relevant for the given user query.

USER QUERY: {query}

CANDIDATES (you must ONLY use these product_ids):
{candidates_json}

Rules:
1. Reply with ONLY valid JSON — no markdown, no explanation, no scores.
2. The JSON must have exactly one key: "ranking".
3. "ranking" must be an ordered list of product_id strings, most relevant first.
4. Only include product_ids from the CANDIDATES list above — do not invent new ones.
5. You may omit a candidate if it is clearly irrelevant, but include all plausible ones.
6. DO NOT generate, invent, or output any confidence score — ordering only.

Example reply format:
{{"ranking": ["PROD-001", "PROD-002", "PROD-003"]}}
"""


class LLMReranker:
    """
    Wraps an LLM (watsonx or OpenAI) to rerank product candidates.

    Usage
    -----
    reranker = LLMReranker()
    reranked, success = reranker.rerank(query, top5_candidates)
    if not success:
        # fall back to original BM25 + RapidFuzz ordering
        ...
    """

    def __init__(
        self,
        # watsonx settings (read from env if not provided)
        watsonx_url: Optional[str] = None,
        watsonx_apikey: Optional[str] = None,
        watsonx_project_id: Optional[str] = None,
        watsonx_model_id: Optional[str] = None,
        # OpenAI settings (read from env if not provided)
        openai_api_key: Optional[str] = None,
        openai_model: Optional[str] = None,
        # Behaviour
        timeout_seconds: int = 15,
        candidates_to_rerank: int = 5,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.candidates_to_rerank = candidates_to_rerank

        # Resolve watsonx config
        self._wx_url = watsonx_url or os.getenv("WATSONX_URL", "")
        self._wx_apikey = watsonx_apikey or os.getenv("WATSONX_APIKEY", "")
        self._wx_project_id = watsonx_project_id or os.getenv("WATSONX_PROJECT_ID", "")
        self._wx_model_id = watsonx_model_id or os.getenv(
            "WATSONX_MODEL_ID", "ibm/granite-13b-chat-v2"
        )

        # Resolve OpenAI config
        self._openai_api_key = openai_api_key or os.getenv("OPENAI_API_KEY", "")
        self._openai_model = openai_model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")

        # Determine which backend to use
        self._backend: Optional[str] = self._detect_backend()

        if self._backend is None:
            logger.warning(
                "LLMReranker: no LLM credentials configured "
                "(set WATSONX_APIKEY / WATSONX_URL / WATSONX_PROJECT_ID "
                "or OPENAI_API_KEY). Reranking will be skipped."
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        """True when an LLM backend is configured and its SDK is installed."""
        return self._backend is not None

    def rerank(
        self,
        query: str,
        candidates: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], bool, Optional[str]]:
        """
        Rerank the top-N candidates using the LLM.

        Parameters
        ----------
        query:
            The original user query.
        candidates:
            List of product result dicts (as returned by identify_products).
            Each dict MUST contain a "product_code" key (SLC_CODE).

        Returns
        -------
        (results, success, llm_top1_id):
            results      — reranked list on success; original list on failure.
            success      — True when the LLM reranked successfully.
            llm_top1_id  — product_code the LLM ranked #1, or None on failure.
                           The caller uses this to detect LLM ↔ BM25 agreement
                           and apply a conditional confidence boost.
        """
        if not self.is_available:
            return candidates, False, None

        # Take only the top N for reranking
        to_rerank = candidates[: self.candidates_to_rerank]
        passthrough = candidates[self.candidates_to_rerank :]

        # Build the candidate payload the LLM will see.
        # Only expose product_id (SLC_CODE) and product_name — no raw scores.
        candidate_payload = [
            {
                "product_id": c["product_code"],
                "product_name": c.get("product_name") or c["product_code"],
            }
            for c in to_rerank
            if c.get("product_code")
        ]

        if not candidate_payload:
            return candidates, False, None

        # Build a lookup so we can reassemble result dicts by product_id
        id_to_result: Dict[str, Dict[str, Any]] = {
            c["product_code"]: c for c in to_rerank if c.get("product_code")
        }
        valid_ids = set(id_to_result.keys())

        try:
            raw_response = self._call_llm(query, candidate_payload)
            ranking = self._parse_ranking(raw_response, valid_ids)
        except Exception as exc:
            logger.warning("LLMReranker: LLM call failed (%s). Using original order.", exc)
            return candidates, False, None

        if not ranking:
            logger.warning("LLMReranker: empty ranking returned. Using original order.")
            return candidates, False, None

        # The product_id the LLM placed first — returned to the caller so it can
        # check whether this agrees with the BM25 #1 pick before any boost.
        llm_top1_id: str = ranking[0]

        # Reassemble in LLM order.
        # Confidence scores are NEVER touched here — they were already calculated
        # by ConfidenceScorer in identify_products() and must not be overwritten.
        #
        # After assembling the LLM order we do a stable sort that guarantees
        # exact-match results always lead (rule: "give preference to exact_match").
        reranked_top: List[Dict[str, Any]] = []
        seen: set = set()
        for pid in ranking:
            if pid in id_to_result and pid not in seen:
                reranked_top.append(id_to_result[pid])
                seen.add(pid)

        # Candidates the LLM omitted — append after, confidence intact
        omitted = [r for r in to_rerank if r["product_code"] not in seen]
        reranked_top.extend(omitted)

        # Stable sort: exact-match items always precede fuzzy items.
        # Within each group the LLM's relative order is preserved.
        def _exact_first(item: Dict[str, Any]) -> int:
            return 0 if any(mt.startswith("exact") for mt in item.get("match_types", [])) else 1

        reranked_top.sort(key=_exact_first)

        return reranked_top + passthrough, True, llm_top1_id

    # ------------------------------------------------------------------
    # Backend detection
    # ------------------------------------------------------------------

    def _detect_backend(self) -> Optional[str]:
        if _WATSONX_AVAILABLE and self._wx_apikey and self._wx_url and self._wx_project_id:
            return "watsonx"
        if _OPENAI_AVAILABLE and self._openai_api_key:
            return "openai"
        return None

    # ------------------------------------------------------------------
    # LLM call dispatch
    # ------------------------------------------------------------------

    def _call_llm(
        self, query: str, candidate_payload: List[Dict[str, str]]
    ) -> str:
        prompt = _RERANK_PROMPT.format(
            query=query,
            candidates_json=json.dumps(candidate_payload, indent=2),
        )
        if self._backend == "watsonx":
            return self._call_watsonx(prompt)
        if self._backend == "openai":
            return self._call_openai(prompt)
        raise RuntimeError("No LLM backend available")

    def _call_watsonx(self, prompt: str) -> str:
        credentials = Credentials(
            url=self._wx_url,
            api_key=self._wx_apikey,
        )
        model = ModelInference(
            model_id=self._wx_model_id,
            credentials=credentials,
            project_id=self._wx_project_id,
        )
        params = {
            "max_tokens": 256,
            "temperature": 0.0,
        }
        messages = [{"role": "user", "content": prompt}]
        response = model.chat(messages=messages, params=params)
        # chat() returns a dict with choices list (OpenAI-compatible shape)
        if isinstance(response, dict):
            return (
                response.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
        return str(response)

    def _call_openai(self, prompt: str) -> str:
        client = _openai_module.OpenAI(
            api_key=self._openai_api_key,
            timeout=self.timeout_seconds,
        )
        completion = client.chat.completions.create(
            model=self._openai_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=256,
            temperature=0.0,
        )
        return completion.choices[0].message.content or ""

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_ranking(self, raw: str, valid_ids: set) -> List[str]:
        """
        Parse the LLM response and return a validated ordered list of product_id strings.

        Robustness:
        - Strips markdown code fences if present.
        - Falls back to extracting the first JSON object found in the text.
        - Filters out any product_id not in valid_ids (hallucination guard).
        """
        text = raw.strip()

        # Strip markdown code fences
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(
                line for line in lines if not line.startswith("```")
            ).strip()

        # Attempt direct JSON parse
        try:
            data = json.loads(text)
            return self._extract_valid_ids(data, valid_ids)
        except json.JSONDecodeError:
            pass

        # Fallback: find the first {...} block
        start = text.find("{")
        end = text.rfind("}") + 1
        if start != -1 and end > start:
            try:
                data = json.loads(text[start:end])
                return self._extract_valid_ids(data, valid_ids)
            except json.JSONDecodeError:
                pass

        logger.warning("LLMReranker: could not parse JSON from LLM response: %.200s", raw)
        return []

    @staticmethod
    def _extract_valid_ids(data: Any, valid_ids: set) -> List[str]:
        """
        Extract the 'ranking' list and return only known product_id strings.
        Any entry that is not a plain string or not in valid_ids is silently dropped.
        """
        if not isinstance(data, dict):
            return []
        ranking = data.get("ranking", [])
        if not isinstance(ranking, list):
            return []
        seen: set = set()
        result: List[str] = []
        for pid in ranking:
            if isinstance(pid, str) and pid in valid_ids and pid not in seen:
                result.append(pid)
                seen.add(pid)
        return result


# ---------------------------------------------------------------------------
# Module-level singleton (lazily initialized)
# ---------------------------------------------------------------------------

_reranker_instance: Optional[LLMReranker] = None


def get_reranker() -> LLMReranker:
    """Return the module-level LLMReranker singleton (creates on first call)."""
    global _reranker_instance
    if _reranker_instance is None:
        _reranker_instance = LLMReranker()
    return _reranker_instance


# Made with Bob
