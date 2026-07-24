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

from src.core.matcher import ProductMatcher
from src.core.tls_checker import TLSChecker

from .routes import search_router, products_router, health_router, search_v0_router
from .routes import search, products, health, search_v0

# Allowed CORS origins — override via CORS_ALLOWED_ORIGINS (comma-separated).
_ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "https://cfaiproducts.2b06dgt5gcrj.us-south.codeengine.appdomain.cloud",
    ).split(",")
    if o.strip()
]

app = FastAPI(
    title="Product Catalog API",
    description="""
    High-performance API for exact + fuzzy product identification.

    **Authentication:** All endpoints (except `/health`) require a Bearer token.
    Click **Authorize** and enter your token to use the interactive docs.

    **Search Pipeline (v5):**
    1. **Normalization** — lowercase, punctuation → space, delimiter joining, noise-word removal
    2. **BM25 Search** — weighted inverted-index retrieval of Top-20 candidates
    3. **N-gram Augmentation** — typo-tolerant candidate expansion when BM25 returns < 20 hits
    4. **RapidFuzz Re-score** — `token_sort_ratio` re-ranks the short candidate list
    5. **Confidence Calculation** — base score ± penalties ± boosts, floored at 0.00
    6. **TLS Routing** — top result checked against TLS SLC-code mappings

    **Coverage:** typos, aliases, partial names, extra words, case-insensitive.
    **Pros:** no external LLM, deterministic, explainable, low-latency, scales to 10 K+ aliases.

    **Fallback endpoint:** `/v0/products/search` — identical pipeline, TLS check bypassed.
    """,
    version="5.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

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
    for path, path_item in schema.get("paths", {}).items():
        if path == "/health":
            continue
        for operation in path_item.values():
            if isinstance(operation, dict):
                operation["security"] = [{"BearerAuth": []}]
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi

matcher = None

_LOCAL_DICT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),  # src/api/
    "..", "..",                                   # → project root
    "data", "product_match_dictionary.json",
)


def _load_match_dictionary() -> dict:
    """Load the product-match dictionary from the local JSON file."""
    local_path = os.path.normpath(_LOCAL_DICT_PATH)
    with open(local_path, encoding="utf-8") as fh:
        doc = json.load(fh)
    match_dictionary = doc.get("match_dictionary")
    if match_dictionary is None:
        raise KeyError("'match_dictionary' key not found in local JSON file")
    print(f"✓ Match dictionary loaded from: {local_path}")
    return match_dictionary


@app.on_event("startup")
def startup_event():
    """Initialize the matcher and TLS checker on application startup."""
    global matcher

    delimiter_dict = {
        "tcp ip": "_",
        "cloud pak": "_",
        "check sorter": "_",
        "web sphere": "_",
        "data stage": "_",
        "z os": "_",
    }

    use_enhanced = os.getenv("USE_ENHANCED_MATCHER", "true").lower() == "true"

    try:
        match_dictionary = _load_match_dictionary()
    except Exception as e:
        print(f"✗ Failed to load match dictionary: {e}")
        print("  ⚠ Starting with empty matcher — search endpoints will return no results.")
        match_dictionary = {"exact_match": {}, "fuzzy_match": {}}

    matcher = ProductMatcher(
        match_dictionary=match_dictionary,
        delimiter_dict=delimiter_dict,
        use_enhanced=use_enhanced
    )

    tls_checker = TLSChecker("data/tls_assistant_slc_code_mappings.json")

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


app.include_router(health_router)
app.include_router(search_router)
app.include_router(products_router)
app.include_router(search_v0_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)


# Made with Bob
