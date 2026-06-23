"""
Product endpoints for listing and retrieving product information.
"""

from fastapi import APIRouter, HTTPException, Query, Path

from ..models.response import ProductListResponse, ProductInfo

router = APIRouter(prefix="/products", tags=["Products"])

# Global matcher instance (will be set by main app)
matcher = None


def set_matcher(matcher_instance):
    """Set the global matcher instance."""
    global matcher
    matcher = matcher_instance


@router.get("", response_model=ProductListResponse)
def get_products(
    limit: int = Query(
        100, 
        ge=1, 
        le=1000,
        description="Maximum number of products to return"
    ),
    offset: int = Query(
        0,
        ge=0,
        description="Number of products to skip (for pagination)"
    )
):
    """
    List all unique products from the dictionary.
    
    Returns deduplicated products from both exact and fuzzy match sections.
    Supports pagination via limit and offset parameters.
    """
    global matcher
    
    if matcher is None:
        raise HTTPException(status_code=500, detail="Matcher not initialized")
    
    unique_products = {}
    match_dictionary = matcher.match_dictionary
    
    # Collect unique products from both sections
    for section in ["exact_match", "fuzzy_match"]:
        entries = match_dictionary.get(section, {})
        for _, products in entries.items():
            for product in products:
                code = product.get("SLC_CODE")
                name = product.get("PRODUCT_NAME")
                if code and code not in unique_products:
                    unique_products[code] = {
                        "product_code": code,
                        "product_name": name
                    }
    
    # Convert to list and apply pagination
    all_products = list(unique_products.values())
    total_available = len(all_products)
    
    paginated_products = all_products[offset:offset + limit]
    
    return {
        "count": len(paginated_products),
        "total_available": total_available,
        "products": paginated_products
    }


@router.get("/{product_code}", response_model=ProductInfo)
def get_product_by_code(
    product_code: str = Path(..., description="Product code (SLC_CODE)")
):
    """
    Get product information by product code.
    
    Searches through both exact and fuzzy match dictionaries.
    """
    global matcher
    
    if matcher is None:
        raise HTTPException(status_code=500, detail="Matcher not initialized")
    
    match_dictionary = matcher.match_dictionary
    
    # Search in both sections
    for section in ["exact_match", "fuzzy_match"]:
        entries = match_dictionary.get(section, {})
        for _, products in entries.items():
            for product in products:
                if product.get("SLC_CODE") == product_code:
                    return {
                        "product_code": product_code,
                        "product_name": product.get("PRODUCT_NAME")
                    }
    
    raise HTTPException(status_code=404, detail=f"Product code '{product_code}' not found")


# Made with Bob