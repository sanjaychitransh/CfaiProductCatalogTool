"""
Product Catalog API - Main Application

High-performance FastAPI service for product identification using 
advanced multi-stage search pipeline.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import json
import os

# Import the enhanced ProductMatcher
from src.core.matcher import ProductMatcher

# Import route modules
from .routes import search_router, products_router, health_router
from .routes import search, products, health

# Create FastAPI application
app = FastAPI(
    title="Product Catalog API",
    description="""
    High-performance API for exact + fuzzy product identification.
    
    **Enhanced Search Stack:**
    - Aho-Corasick: Fast exact phrase matching
    - BM25: Weighted candidate retrieval
    - RapidFuzz: Accurate reranking
    - N-gram: Typo tolerance
    - SLC_CODE grouping: Deduplicated results
    """,
    version="3.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# Add CORS middleware for web applications
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global matcher instance
matcher = None


@app.on_event("startup")
def startup_event():
    """Initialize the matcher on application startup."""
    global matcher
    
    try:
        with open("data/product_match_dictionary.json", "r", encoding="utf-8") as file:
            raw_data = json.load(file)
        
        match_dictionary = raw_data.get("match_dictionary", {})
        
        # Delimiter normalization dictionary
        delimiter_dict = {
            "tcp ip": "_",
            "cloud pak": "_",
            "check sorter": "_",
            "web sphere": "_",
            "data stage": "_"
        }
        
        # Initialize matcher with enhanced features
        use_enhanced = os.getenv("USE_ENHANCED_MATCHER", "true").lower() == "true"
        
        matcher = ProductMatcher(
            match_dictionary=match_dictionary,
            delimiter_dict=delimiter_dict,
            use_enhanced=use_enhanced
        )
        
        # Set matcher in route modules
        search.set_matcher(matcher)
        products.set_matcher(matcher)
        health.set_matcher(matcher)
        
        print(f"✓ Matcher initialized")
        print(f"  - Mode: {'Enhanced' if matcher.use_enhanced else 'Legacy'}")
        print(f"  - Exact aliases: {len(matcher.exact_index)}")
        print(f"  - Fuzzy aliases: {len(matcher.fuzzy_aliases)}")
        
        if matcher.use_enhanced:
            print(f"  - Aho-Corasick: {'✓' if matcher.ac_automaton else '✗'}")
            print(f"  - BM25: {'✓' if matcher.bm25_index else '✗'}")
            print(f"  - N-gram index: {len(matcher.ngram_index)} entries")
        
    except FileNotFoundError:
        print("✗ Error: product_match_dictionary.json not found")
        raise
    except Exception as e:
        print(f"✗ Error initializing matcher: {e}")
        raise


# Include routers
app.include_router(health_router)
app.include_router(search_router)
app.include_router(products_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)


# Made with Bob
