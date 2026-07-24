"""
API route modules.
"""

from .search import router as search_router
from .search_v0 import router as search_v0_router
from .products import router as products_router
from .health import router as health_router

__all__ = [
    "search_router",
    "search_v0_router",
    "products_router",
    "health_router",
]

# Made with Bob
