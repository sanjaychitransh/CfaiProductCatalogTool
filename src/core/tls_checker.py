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
        response body.
        """
        if slc_code not in self._slc_to_assistant:
            return None

        return {
            "tls_product": True,
            "message": "This is a TLS product. Redirected to TLS agent.",
            "slc_code": slc_code,
            "product_name": self._slc_to_product.get(slc_code, ""),
            "assistant": self._slc_to_assistant[slc_code],
        }

    def check_results(self, results: list) -> Optional[dict]:
        """
        Inspect the first (highest-confidence) result in *results*.

        If its ``product_code`` maps to a TLS product, return the TLS
        redirect payload.  Otherwise return ``None``.

        Only the top result is checked: if the best match is a TLS
        product the query is considered TLS-owned.
        """
        if not results:
            return None
        top_code = results[0].get("product_code", "")
        return self.check(top_code)


# Made with Bob
