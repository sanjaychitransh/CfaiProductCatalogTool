"""
TLS product checker.

Loads the TLS SLC-code mappings once at startup and exposes a fast
set-based lookup so the search route can intercept TLS products before
returning results to the caller.
"""

import json
import os
from typing import Optional


class TLSChecker:
    """
    Checks whether a matched SLC code belongs to a TLS-owned product.

    Usage
    -----
    checker = TLSChecker("data/tls_assistant_slc_code_mappings.json")
    result  = checker.check("SCPA0")
    # result is None  → not a TLS product, proceed normally
    # result is dict  → TLS product, return the dict as the response
    """

    def __init__(self, mappings_path: str):
        """
        Parameters
        ----------
        mappings_path:
            Path to ``tls_assistant_slc_code_mappings.json``.  Only
            entries whose ``owner`` field equals ``"TLS"`` are loaded
            (the file should already be pre-filtered, but the guard
            stays for safety).
        """
        self._slc_to_assistant: dict[str, str] = {}
        self._slc_to_product: dict[str, str] = {}

        if not os.path.exists(mappings_path):
            print(f"⚠  TLS mappings file not found: {mappings_path} — TLS check disabled")
            return

        with open(mappings_path, "r", encoding="utf-8") as fh:
            entries = json.load(fh)

        for entry in entries:
            if entry.get("owner") != "TLS":
                continue
            slc = entry.get("slc_code", "").strip()
            if not slc:
                continue
            self._slc_to_assistant[slc] = entry.get("assistant", "TLS Agent")
            self._slc_to_product[slc] = entry.get("product_name", "")

        print(f"✓ TLS checker loaded — {len(self._slc_to_assistant)} SLC codes")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def loaded(self) -> bool:
        """True when at least one TLS SLC code is loaded."""
        return bool(self._slc_to_assistant)

    def is_tls(self, slc_code: str) -> bool:
        """Return True if *slc_code* belongs to a TLS product."""
        return slc_code in self._slc_to_assistant

    def check(self, slc_code: str) -> Optional[dict]:
        """
        Return a TLS redirect payload if *slc_code* is a TLS product,
        otherwise return ``None``.

        The returned dict is ready to be used directly as the API
        response body (matches ``TLSRedirectResponse`` shape).
        ``message`` is intentionally omitted — callers receive only the
        routing signal (``tls_product: true``), the SLC code, the product
        name, and the target assistant.
        """
        if slc_code not in self._slc_to_assistant:
            return None

        return {
            "tls_product": True,
            "slc_code": slc_code,
            "product_name": self._slc_to_product.get(slc_code, ""),
            "assistant": self._slc_to_assistant[slc_code],
        }

    def check_results(self, results: list, sim_tolerance: float = 0.0) -> Optional[dict]:
        """
        Check whether the query should be redirected to a TLS agent.

        Uses ALIAS_SIMILARITY (not raw score or confidence) for the tolerance
        window.  alias_similarity is the best ratio() score between the query
        and the product's matched aliases — it is the most precise signal for
        "this result is actually about the query term" and is already computed
        by identify_products() and stored on each result dict.

        Rules (in order):
        1. If the top-ranked result is a TLS product → redirect.
        2. If a lower-ranked result is a TLS product AND its alias_similarity
           is within ``sim_tolerance`` of the top result's alias_similarity
           (i.e. a genuine tie at the alias level) → redirect.
        3. Otherwise → return None (proceed normally).

        Why alias_similarity
        --------------------
        - Raw score: too coarse — short aliases inflate it.
        - Confidence: still tied when many products share same penalties.
        - alias_similarity: fine-grained — "cics transaction server" (sim=100)
          vs "wca4z cics transaction server exec…" (sim=56) → clear separation.

        Default tolerance is 0.0 (exact tie only).

        Parameters
        ----------
        results        : ranked list from identify_products (highest first)
        sim_tolerance  : max alias_similarity gap within which a TLS hit still
                         triggers the intercept (default 0.0 = exact sim tie)
        """
        if not results:
            return None

        top_sim = results[0].get("alias_similarity", 0.0)

        for result in results:
            result_sim = result.get("alias_similarity", 0.0)

            # Stop scanning once alias_similarity drops below the top
            if top_sim - result_sim > sim_tolerance:
                break

            hit = self.check(result.get("product_code", ""))
            if hit is not None:
                return hit

        return None


# Made with Bob
