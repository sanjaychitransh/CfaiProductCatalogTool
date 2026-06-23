"""
Health and statistics endpoints.
"""

from fastapi import APIRouter

from ..models.response import HealthResponse

router = APIRouter(tags=["System"])

# Global matcher instance (will be set by main app)
matcher = None


def set_matcher(matcher_instance):
    """Set the global matcher instance."""
    global matcher
    matcher = matcher_instance


@router.get("/", include_in_schema=False)
def root():
    """Root endpoint redirect to docs."""
    return {
        "message": "Product Catalog API",
        "version": "3.0.0",
        "docs": "/docs",
        "health": "/health"
    }


@router.get("/health", response_model=HealthResponse)
def health():
    """
    Health check endpoint.
    
    Returns system status and matcher statistics.
    """
    global matcher
    
    return {
        "status": "ok" if matcher else "error",
        "matcher_loaded": matcher is not None,
        "exact_aliases": len(matcher.exact_index) if matcher else 0,
        "fuzzy_aliases": len(matcher.fuzzy_aliases) if matcher else 0
    }


@router.get("/stats")
def get_statistics():
    """
    Get detailed statistics about the matcher.
    
    Returns information about index sizes, search pipeline, etc.
    """
    global matcher
    
    if matcher is None:
        return {"error": "Matcher not initialized"}
    
    token_counts = [len(ids) for ids in matcher.token_to_fuzzy_ids.values()]
    avg_aliases_per_token = sum(token_counts) / len(token_counts) if token_counts else 0
    
    stats = {
        "matcher_type": "enhanced" if matcher.use_enhanced else "legacy",
        "exact_match": {
            "total_aliases": len(matcher.exact_index),
            "phrase_index_size": len(matcher.exact_phrases),
            "aho_corasick_enabled": matcher.ac_automaton is not None
        },
        "fuzzy_match": {
            "total_aliases": len(matcher.fuzzy_aliases),
            "token_index_size": len(matcher.token_to_fuzzy_ids),
            "avg_aliases_per_token": round(avg_aliases_per_token, 2),
            "bm25_enabled": matcher.bm25_index is not None,
            "ngram_index_size": len(matcher.ngram_index),
            "ngram_size": matcher.ngram_size
        },
        "delimiter_terms": len(matcher.delimiter_dict)
    }
    
    if matcher.use_enhanced:
        stats["features"] = [
            "Aho-Corasick exact matching",
            "BM25 candidate retrieval",
            "RapidFuzz reranking",
            "N-gram typo tolerance",
            "SLC_CODE grouping"
        ]
    
    return stats


# Made with Bob