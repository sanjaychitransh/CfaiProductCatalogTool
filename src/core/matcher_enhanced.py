"""
matcher_enhanced.py — compatibility shim
=========================================

The ``EnhancedProductMatcher`` class that previously lived here has been
consolidated into ``ProductMatcher`` (matcher.py).  ``ProductMatcher`` already
implements the full enhanced stack (Aho-Corasick, BM25, N-gram, RapidFuzz,
confidence scoring) and is the only matcher instantiated by the application.

This module is kept as a thin re-export so any external code that imported
``EnhancedProductMatcher`` by name continues to work without modification.
New code should import ``ProductMatcher`` directly.
"""

from .matcher import ProductMatcher as EnhancedProductMatcher  # noqa: F401

__all__ = ["EnhancedProductMatcher"]

# Made with Bob
