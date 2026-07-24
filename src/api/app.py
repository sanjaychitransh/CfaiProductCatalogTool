"""
Product Catalog API - Main Application

High-performance FastAPI service for product identification using
advanced multi-stage search pipeline.
"""

import json
import os

from dotenv import load_dotenv
load_dotenv()  # loads .env from the working directory

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

# Import the enhanced ProductMatcher
from src.core.matcher import ProductMatcher
from src.utils.cloudant_client import load_match_dictionary_from_cloudant

# Import TLS checker
from src.core.tls_checker import TLSChecker

# Import route modules
from .routes import search_router, products_router, health_router, search_v0_router
from .routes import search, products, health, search_v0

# Allowed CORS origins — defaults to the public Code Engine URL;
# override at runtime via CORS_ALLOWED_ORIGINS (comma-separated).
_ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "https://frowsy-glareless-kylie.ngrok-free.dev",
    ).split(",")
    if o.strip()
]

# Create FastAPI application
app = FastAPI(
    title="Product Catalog API",
    description="""
    High-performance API for exact + fuzzy product identification.

    **Authentication:** All endpoints (except `/health`) require a Bearer token.
    Click **Authorize** and enter your token to use the interactive docs.

    **Enhanced Search Stack:**
    - Aho-Corasick: Fast exact phrase matching
    - BM25: Weighted candidate retrieval
    - RapidFuzz: Accurate reranking
    - N-gram: Typo tolerance
    - SLC_CODE grouping: Deduplicated results

    **TLS Routing:**
    When the top search result belongs to a TLS-owned product the
    `/products/search` endpoint returns a redirect notice instead of
    product names.  The original behaviour (no TLS check) remains
    available at `/v0/products/search` for fallback / comparison.

    **Full Pipeline (`/products/answer`):**
    BM25 + RapidFuzz → LLM Reranking → Evidence/Confidence Validation → AEO Structured Answer
    """,
    version="4.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# Narrowed CORS — only allow the configured origin(s), read-only methods
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET"],
    allow_headers=["Authorization", "Content-Type"],
)


def _custom_openapi():
    """Attach BearerAuth security scheme so Swagger UI shows the Authorize button."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    schema.setdefault("components", {})["securitySchemes"] = {
        "BearerAuth": {"type": "http", "scheme": "bearer"}
    }
    # Apply BearerAuth to every route except /health (which is unauthenticated)
    for path, path_item in schema.get("paths", {}).items():
        if path == "/health":
            continue
        for operation in path_item.values():
            if isinstance(operation, dict):
                operation["security"] = [{"BearerAuth": []}]
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi

# Global matcher instance
matcher = None


# Path to the local fallback dictionary, relative to the project root
_LOCAL_DICT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),  # src/api/
    "..", "..",                                   # → project root
    "data", "product_match_dictionary.json",
)


def _load_match_dictionary() -> dict:
    """
    Load the product-match dictionary.

    Priority
    --------
    1. CouchDB / Cloudant  (CLOUDANT_URL + credentials)
    2. data/product_match_dictionary.json  (local fallback)

    Required environment variables for CouchDB/Cloudant
    ----------------------------------------------------
    CLOUDANT_URL       CouchDB service URL
    CLOUDANT_USERNAME  Basic Auth username  (or CLOUDANT_APIKEY for IAM)
    CLOUDANT_PASSWORD  Basic Auth password
    """
    # --- Attempt 1: CouchDB / Cloudant ---
    try:
        print("⟳ Loading match dictionary from CouchDB / Cloudant…")
        match_dictionary = load_match_dictionary_from_cloudant()
        print("✓ Match dictionary loaded from CouchDB / Cloudant")
        return match_dictionary
    except Exception as e:
        print(f"⚠ CouchDB / Cloudant unavailable: {e}")
        print("  ↳ Falling back to local file: data/product_match_dictionary.json")

    # --- Attempt 2: local JSON file ---
    local_path = os.path.normpath(_LOCAL_DICT_PATH)
    try:
        with open(local_path, encoding="utf-8") as fh:
            doc = json.load(fh)
        match_dictionary = doc.get("match_dictionary")
        if match_dictionary is None:
            raise KeyError("'match_dictionary' key not found in local JSON file")
        print(f"✓ Match dictionary loaded from local file: {local_path}")
        return match_dictionary
    except Exception as e:
        raise RuntimeError(
            f"Failed to load match dictionary from both CouchDB/Cloudant and "
            f"local file '{local_path}': {e}"
        ) from e


@app.on_event("startup")
def startup_event():
    """Initialize the matcher and TLS checker on application startup."""
    global matcher

    # Delimiter normalization dictionary
    delimiter_dict = {
        "tcp ip": "_",
        "cloud pak": "_",
        "check sorter": "_",
        "web sphere": "_",
        "data stage": "_",
        "z os": "_",      # z/OS — slash is collapsed to space before this runs,
                          # so "z/os" arrives here as "z os" and must be rejoined
    }

    use_enhanced = os.getenv("USE_ENHANCED_MATCHER", "true").lower() == "true"

    try:
        match_dictionary = _load_match_dictionary()
    except Exception as e:
        print(f"✗ Failed to load match dictionary from all sources: {e}")
        print("  ⚠ Starting with empty matcher — search endpoints will return no results.")
        match_dictionary = {"exact_match": {}, "fuzzy_match": {}}

    matcher = ProductMatcher(
        match_dictionary=match_dictionary,
        delimiter_dict=delimiter_dict,
        use_enhanced=use_enhanced
    )

    # Initialize TLS checker
    tls_checker = TLSChecker("data/tls_assistant_slc_code_mappings.json")

    # Set matcher (and TLS checker) in route modules
    search.set_matcher(matcher)
    search.set_tls_checker(tls_checker)
    search_v0.set_matcher(matcher)
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


# Include routers
app.include_router(health_router)
app.include_router(search_router)           # primary  — with TLS intercept
app.include_router(products_router)
app.include_router(search_v0_router)        # secondary — original behaviour (no TLS check)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)


# Made with Bob
