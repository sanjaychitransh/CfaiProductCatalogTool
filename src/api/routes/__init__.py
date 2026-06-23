"""
API route modules.
"""

from .search import router as search_router
from .products import router as products_router
from .health import router as health_router

__all__ = [
    "search_router",
    "products_router",
    "health_router",
]

# Made with Bob