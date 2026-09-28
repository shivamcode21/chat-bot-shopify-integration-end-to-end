import re
import json
import os
import logging
from typing import List, Dict, Optional, Any, Union
import httpx
from fashion_bot.utils.http_client import get_shared_async_http_client

logger = logging.getLogger(__name__)


def _build_product_url(
    handle: Optional[str],
    shop_url: Optional[str],
    website_url: Optional[str] = None,
) -> Optional[str]:
    """Build a customer-facing product URL from a handle.

    Prefers the client's canonical ``website_url`` (e.g. https://groovee.in)
    and only falls back to the ``*.myshopify.com`` admin/staging domain when no
    website URL is configured — this keeps the internal Shopify domain out of
    customer-facing replies. Mirrors ``ShopifyProductService._build_product_url``.
    """
    if not handle:
        return None
    if website_url:
        return f"{website_url.rstrip('/')}/products/{handle}"
    if shop_url:
        return f"https://{shop_url}/products/{handle}"
    return None


def _html_to_plain_text(html_content: str) -> str:
    """
    Convert HTML content to clean plain text for LLM consumption.
    Extracts text from tables, lists, paragraphs while preserving structure.
    """
    if not html_content:
        return ""
    
    try:
        text = str(html_content)
        
        # Replace table cells with tabs and rows with newlines
        text = re.sub(r'</td>\s*<td[^>]*>', '\t', text, flags=re.IGNORECASE)
        text = re.sub(r'</tr>\s*<tr[^>]*>', '\n', text, flags=re.IGNORECASE)
        
        # Replace list items with bullet points
        text = re.sub(r'<li[^>]*>', '• ', text, flags=re.IGNORECASE)
        text = re.sub(r'</li>', '\n', text, flags=re.IGNORECASE)
        
        # Replace paragraph/div/br with newlines
        text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'</p>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'</div>', '\n', text, flags=re.IGNORECASE)
        
        # Remove all remaining HTML tags
        text = re.sub(r'<[^>]+>', '', text)
        
        # Decode common HTML entities
        text = text.replace('&nbsp;', ' ')
        text = text.replace('\xa0', ' ')
        text = text.replace('&amp;', '&')
        text = text.replace('&lt;', '<')
        text = text.replace('&gt;', '>')
        text = text.replace('&quot;', '"')
        
        # Clean up whitespace
        text = re.sub(r'[ \t]+', ' ', text)  # Multiple spaces/tabs to single space
        text = re.sub(r'\n\s*\n', '\n', text)  # Multiple newlines to single
        text = text.strip()
        
        return text
    except Exception:
        return str(html_content)


def extract_handle_from_url(msg: str) -> Optional[str]:
    """Extract product handle from Groovee URL"""
    match = re.search(r"groovee\.in(?:/[^\s]*)?/products/([a-zA-Z0-9\-]+)", msg)
    return match.group(1) if match else None

def load_preorder_variants() -> Dict[str, List[str]]:
    """Load pre-order variants configuration"""
    preorder_path = os.path.join(os.path.dirname(__file__), "..", "..", "preorder_variants.json")
    try:
        with open(preorder_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Could not load preorder_variants.json: {e}")
        return {}

def process_product_sizes(product: Dict, preorder_variants: Dict[str, List[str]]) -> str:
    """Process product sizes with pre-order logic"""
    title = product.get("title", "No Title")
    preorder_sizes = set(preorder_variants.get(title, []))
    
    available_sizes = []
    preorder_list = []
    in_stock_list = []
    
    for v in product.get("variants", []):
        size = v["title"]
        inv = v.get("inventory_quantity", 0)
        if inv > 0 or size in preorder_sizes:
            if size in preorder_sizes:
                preorder_list.append(size)
            else:
                in_stock_list.append(size)
    
    # Build the response with separate sections
    response_parts = []
    
    if preorder_list:
        preorder_sizes_str = ", ".join(preorder_list)
        response_parts.append(f"Available for Pre-Order: {preorder_sizes_str}")
    
    if in_stock_list:
        in_stock_sizes_str = ", ".join(in_stock_list)
        response_parts.append(f"In Stock: {in_stock_sizes_str}")
    
    if not response_parts:
        return "Out of stock"
    
    return "\n".join(response_parts)

def clean_html_description(body_html: str) -> str:
    """Clean HTML tags from product description"""
    import re
    # Remove HTML tags
    clean_description = re.sub(r'<[^>]+>', '', body_html)
    # Replace HTML entities
    clean_description = clean_description.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    # Clean up extra whitespace
    clean_description = re.sub(r'\s+', ' ', clean_description).strip()
    return clean_description

def _resolve_metafield_reference(metafield_node: Dict) -> str:
    """Resolve inline Metaobject / TaxonomyValue references to display names.

    Works with the ``reference`` (single) and ``references`` (list) fragments
    returned by the GraphQL query.  Returns the resolved string value, or
    empty string if nothing could be resolved.
    """
    if not isinstance(metafield_node, dict):
        return ""
    mtype = metafield_node.get("type", "")
    is_list = mtype.startswith("list.")

    if is_list:
        refs_container = metafield_node.get("references")
        refs = refs_container.get("edges", []) if isinstance(refs_container, dict) else []
        names = []
        for ref_edge in refs:
            ref_node = ref_edge.get("node") if isinstance(ref_edge, dict) else None
            # A referenced object that Shopify deleted, unpublished, or that
            # falls outside the token's API scope comes back as ``{"node": null}``
            # — the key is present with a None value, so ``.get("node", {})``
            # would yield None (not the default) and the ``.get`` below would
            # raise ``AttributeError: 'NoneType' object has no attribute 'get'``.
            # Skip those broken edges and keep resolving the rest of the list.
            if not isinstance(ref_node, dict):
                continue
            name = ref_node.get("name") or ref_node.get("displayName") or ref_node.get("handle", "")
            if name:
                names.append(name)
        return json.dumps(names) if names else ""
    else:
        ref_data = metafield_node.get("reference")
        if isinstance(ref_data, dict) and ref_data:
            name = ref_data.get("name") or ref_data.get("displayName") or ref_data.get("handle", "")
            return name or ""
    return ""


async def ashopify_get_product_metafields(
    product_id: str,
    access_token: str,
    shop_url: str,
    api_version: str = "2024-04",
) -> Dict[str, Any]:
    url = f"https://{shop_url}/admin/api/{api_version}/products/{product_id}/metafields.json"
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json",
    }
    client = await get_shared_async_http_client()
    try:
        response = await client.get(url, headers=headers, timeout=15)
        if response.status_code == 200:
            return {"success": True, "metafields": response.json().get("metafields", [])}
            try:
                error_json = response.json()
            except Exception:
                error_json = {}
            return {"success": False, "error": f"Shopify REST error: {response.status_code}", "details": error_json or response.text}
    except httpx.TimeoutException:
        return {"success": False, "error": "Request timed out while fetching product metafields."}
    except httpx.ConnectError:
        return {"success": False, "error": "Network connection error while fetching product metafields."}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def ashopify_get_variant_inventory(
    variant_id: str,
    access_token: str,
    shop_url: str,
    api_version: str = "2024-04",
) -> Dict[str, Any]:
    url = f"https://{shop_url}/admin/api/{api_version}/variants/{variant_id}.json"
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json",
    }
    client = await get_shared_async_http_client()
    try:
        response = await client.get(url, headers=headers, timeout=15)
        if response.status_code == 200:
            variant_data = response.json().get("variant", {})
            return {
                "success": True, 
                "variant": {
                    "id": variant_data.get("id"),
                    "inventory_quantity": variant_data.get("inventory_quantity", 0),
                    "inventory_management": variant_data.get("inventory_management"),
                    "inventory_policy": variant_data.get("inventory_policy"),
                    "inventory_item_id": variant_data.get("inventory_item_id"),
                    "title": variant_data.get("title"),
                    "sku": variant_data.get("sku"),
                    "available": variant_data.get("inventory_quantity", 0) > 0
                    or variant_data.get("inventory_policy") == "continue",
                },
                }
            try:
                error_json = response.json()
            except Exception:
                error_json = {}
            return {"success": False, "error": f"Shopify REST error: {response.status_code}", "details": error_json or response.text}
    except httpx.TimeoutException:
        return {"success": False, "error": "Request timed out while fetching variant inventory."}
    except httpx.ConnectError:
        return {"success": False, "error": "Network connection error while fetching variant inventory."}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}

def fuzzy_search_products(query: str, products: List[Dict]) -> List[Dict]:
    """
    Fuzzy search for products by title or product_type (case-insensitive, partial match).
    Returns a list of matching product dicts.
    """
    q = query.lower()
    # Extract keywords (words with at least 3 letters)
    tokens = [w.lower() for w in re.findall(r'\w{3,}', query)]
    if not tokens:
        return []
    matches = []
    for p in products:
        title = p.get('title', '').lower()
        ptype = p.get('product_type', '').lower()
        if any(token in title or token in ptype for token in tokens):
            matches.append(p)
    return matches 
def _transform_graphql_product_response(product_data: Dict) -> Dict:
    """
    Transform GraphQL Admin API product response into a more convenient format.
    Adds computed fields and flattens nested structures.
    """
    try:
        # Extract metafields and resolve taxonomy/metaobject references
        metafields = []
        raw_mf = product_data.get("metafields")
        if isinstance(raw_mf, dict) and "edges" in raw_mf:
            for edge in raw_mf["edges"]:
                node = edge.get("node") if isinstance(edge, dict) else None
                if not isinstance(node, dict):
                    continue
                mtype = node.get("type", "")
                if "metaobject_reference" in mtype or "taxonomy_value" in mtype:
                    resolved = _resolve_metafield_reference(node)
                    if resolved:
                        node = {**node, "value": resolved}
                metafields.append(node)
        
        # Extract and enhance variants
        variants = []
        raw_vars = product_data.get("variants")
        if isinstance(raw_vars, dict) and "edges" in raw_vars:
            for edge in raw_vars["edges"]:
                variant = edge.get("node") if isinstance(edge, dict) else None
                if not isinstance(variant, dict):
                    continue
                
                # Add computed fields
                inventory_qty = variant.get("inventoryQuantity", 0)
                inventory_policy = variant.get("inventoryPolicy", "DENY")
                
                # In Admin API, we need to check inventory differently
                variant["is_available"] = inventory_qty > 0 or inventory_policy == "CONTINUE"
                
                # Extract size from selectedOptions
                size_value = None
                for option in variant.get("selectedOptions", []):
                    option_name = option.get("name", "").lower()
                    option_value = option.get("value", "").lower()
                    
                    # Check if this looks like a size option
                    if option_name in ["size", "sizes"] or option_value in [
                        "xs", "s", "m", "l", "xl", "xxl", "xxxl",
                        "small", "medium", "large", "extra small", "extra large"
                    ] or any(size in option_value for size in ["28", "30", "32", "34", "36", "38", "40", "42"]):
                        size_value = option.get("value")
                        break
                        
                variant["size"] = size_value
                
                # Extract variant metafields
                variant_metafields = []
                raw_vmf = variant.get("metafields")
                if isinstance(raw_vmf, dict) and "edges" in raw_vmf:
                    variant_metafields = [
                        e.get("node") for e in raw_vmf["edges"]
                        if isinstance(e, dict) and isinstance(e.get("node"), dict)
                    ]
                variant["metafields"] = variant_metafields
                
                variants.append(variant)
        
        # Extract options
        options = product_data.get("options", [])
        
        # Extract images
        images = []
        raw_imgs = product_data.get("images")
        if isinstance(raw_imgs, dict) and "edges" in raw_imgs:
            images = [
                e.get("node") for e in raw_imgs["edges"]
                if isinstance(e, dict) and isinstance(e.get("node"), dict)
            ]
        
        # Extract colors from variant options
        colors = []
        _color_opt_names = {"color", "colors", "colour", "colours"}
        for opt in options:
            if (opt.get("name") or "").lower() in _color_opt_names:
                colors = opt.get("values", [])
                break

        # Enrich from metafields when variant options have no color.
        # Shopify stores category-level color in metafields like
        # "color_pattern", "color", "colour" — the ingestion pipeline
        # already extracts these into Upstash Search, but the live
        # tool fetch was missing them.
        if not colors and metafields:
            _color_mf_keys = {"color_pattern", "color", "colour", "colors", "colours"}
            for mf in metafields:
                bare_key = (mf.get("key") or "").lower().replace("-", "_")
                if bare_key in _color_mf_keys:
                    raw_val = mf.get("value", "")
                    if raw_val:
                        try:
                            parsed = json.loads(raw_val)
                            if isinstance(parsed, list):
                                colors = [str(v) for v in parsed if v and not str(v).startswith("gid://")]
                            elif isinstance(parsed, str) and not parsed.startswith("gid://"):
                                colors = [parsed]
                        except (json.JSONDecodeError, TypeError):
                            if not raw_val.startswith("gid://"):
                                colors = [raw_val]
                    if colors:
                        break

        # Build enhanced product with computed fields
        enhanced_product = {
            **product_data,  # Include all original fields
            "metafields": metafields,
            "variants": variants,
            "options": options,
            "images": images,
            "colors": colors,
            
            # Add convenience fields
            "total_variants": len(variants),
            "available_variants": len([v for v in variants if v.get("is_available", False)]),
            "sizes_available": list(set([v.get("size") for v in variants if v.get("size") and v.get("is_available", False)])),
            "price_range": {
                "min": min([float(v.get("price", "0")) for v in variants]) if variants else 0,
                "max": max([float(v.get("price", "0")) for v in variants]) if variants else 0
            },
            
            # Add availability status
            "has_available_variants": any(v.get("is_available", False) for v in variants),
            "total_inventory": product_data.get("totalInventory", 0),
            
            # Extract all unique sizes (not just available ones)
            "all_sizes": list(set([v.get("size") for v in variants if v.get("size")])),
            
            # Extract option names for reference
            "option_names": [opt.get("name") for opt in options] if options else []
        }
        
        return enhanced_product
        
    except Exception as e:
        # Surface the *real* failure (was a bare print() → stdout, invisible in
        # Loki/OTel). The raw product_data we return here still has GraphQL-shaped
        # nested fields (e.g. metafields = {"edges": [...]}), so downstream
        # formatting must defend against that — see _format_graphql_product_for_tools.
        logger.exception(
            "Error transforming GraphQL product response for handle=%s: %s",
            product_data.get("handle") if isinstance(product_data, dict) else None,
            e,
        )
        return product_data

def _parse_shopify_gid(gid_value: str) -> Dict[str, str]:
    """
    Parse a Shopify Global ID (GID) to extract resource type and ID.
    
    Example: "gid://shopify/OnlineStorePage/148405027138"
    Returns: {"resource_type": "OnlineStorePage", "id": "148405027138", "numeric_id": "148405027138"}
    """
    try:
        if not gid_value.startswith("gid://shopify/"):
            return {}
        
        # Remove the gid://shopify/ prefix
        path = gid_value.replace("gid://shopify/", "")
        parts = path.split("/")
        
        if len(parts) >= 2:
            return {
                "resource_type": parts[0],
                "id": parts[1],
                "numeric_id": parts[1],
                "full_gid": gid_value
            }
        return {}
    except Exception:
        return {}


# Test function - can be run directly
async def amain():
    """
    Main function to test GraphQL product API implementation.
    Update these credentials with your actual Shopify store details.
    """
    
    # Using config manager to get Shopify credentials
    from fashion_bot.config_manager import aget_shopify_config
    shopify_config = await aget_shopify_config()
    
    if not shopify_config.get('access_token') or not shopify_config.get('shop_url'):
        print("Error: Shopify configuration not found in database")
        return
    
    TEST_CONFIG = {
        "shop_url": shopify_config.get('shop_url'),
        "access_token": shopify_config.get('access_token'),
        "product_handle": "sunfire-denim", # Replace with an actual product handle
        "api_version": shopify_config.get('api_version', '2024-04')
    }
    
    print("=== Shopify GraphQL Product API Test ===\n")
    print(f"🏪 Store: {TEST_CONFIG['shop_url']}")
    print(f"🔑 API Version: {TEST_CONFIG['api_version']}")
    print(f"🧪 Testing product handle: '{TEST_CONFIG['product_handle']}'")
    print("=" * 60)
    
    # Test GraphQL API implementation
    print("\n🚀 Testing GraphQL API...")
    import time
    start_time = time.time()
    
    graphql_result = await ashopify_get_product_by_handle_graphql(
        TEST_CONFIG["product_handle"],
        TEST_CONFIG["access_token"],
        TEST_CONFIG["shop_url"],
        TEST_CONFIG["api_version"],
        formatted_response=True
    )
    
    response_time = time.time() - start_time
    print(f"GraphQL Result: Success={graphql_result.get('success')} (Response time: {response_time:.3f}s)")
    
    # Log full GraphQL response
    print("\n📄 FULL GRAPHQL RESPONSE:")
    print("-" * 40)
    print(json.dumps(graphql_result, indent=2, default=str))
    print("-" * 40)
    
    if graphql_result.get("success"):
        product = graphql_result["product"]
        print(f"\n✅ GRAPHQL API SUCCESS:")
        print(f"   📦 Product: {product.get('title')}")
        print(f"   🏷️  Vendor: {product.get('vendor', 'N/A')}")
        print(f"   📂 Product Type: {product.get('productType', 'N/A')}")
        print(f"   🔗 Handle: {product.get('handle')}")
        print(f"   📊 Total Variants: {len(product.get('variants', []))}")
        print(f"   ✅ Available Variants: {product.get('available_variants', 0)}")
        print(f"   📋 Metafields: {len(product.get('metafields', []))}")
        print(f"   📏 Available Sizes: {product.get('sizes_available', [])}")
        print(f"   📦 Total Inventory: {product.get('total_inventory', 0)}")
        print(f"   🖼️  Images Count: {len(product.get('images', []))}")
        
        price_range = product.get('price_range', {})
        if price_range:
            print(f"   💰 Price Range: ${price_range.get('min', 0)} - ${price_range.get('max', 0)}")
            
        # Show metafield namespaces and resolved references
        metafields = product.get('metafields', [])
        if metafields:
            print(f"   📋 METAFIELDS DETAILS:")
            
            reference_metafields = []
            failed_references = []
            regular_metafields = []
            
            for mf in metafields:
                if mf.get('resolved_reference'):
                    reference_metafields.append(mf)
                elif mf.get('reference_error'):
                    failed_references.append(mf)
                else:
                    regular_metafields.append(mf)
            
            # Show resolved references first (these are the important ones)
            if reference_metafields:
                print(f"     🔗 RESOLVED REFERENCES ({len(reference_metafields)}):")
                for mf in reference_metafields:
                    key = mf.get('key', 'unknown')
                    namespace = mf.get('namespace', 'unknown')
                    ref_type = mf.get('reference_type', 'Unknown')
                    ref_title = mf.get('resolved_reference', {}).get('title', 'No title')
                    
                    print(f"       • {namespace}.{key} -> {ref_type}: '{ref_title}'")
                    
                    # Special handling for pages (like size guides)
                    if ref_type == "OnlineStorePage":
                        page_content = mf.get('page_content', '')
                        if page_content:
                            # Show first 100 characters of content
                            content_preview = page_content[:100] + "..." if len(page_content) > 100 else page_content
                            print(f"         📄 Content: {content_preview}")
            
            # Show failed reference resolutions
            if failed_references:
                print(f"     ⚠️  FAILED REFERENCES ({len(failed_references)}):")
                for mf in failed_references:
                    key = mf.get('key', 'unknown')
                    namespace = mf.get('namespace', 'unknown')
                    error = mf.get('reference_error', 'Unknown error')
                    original_gid = mf.get('value', '')
                    
                    print(f"       • {namespace}.{key}: {error}")
                    print(f"         GID: {original_gid}")
                    
                    # Show permission guidance
                    if mf.get('reference_suggestion'):
                        print(f"         💡 Fix: {mf.get('reference_suggestion')}")
            
            # Show regular metafields (collapsed)
            if regular_metafields:
                print(f"     💾 REGULAR METAFIELDS ({len(regular_metafields)}):")
                for mf in regular_metafields:
                    key = mf.get('key', 'unknown')
                    namespace = mf.get('namespace', 'unknown')
                    value = mf.get('value', '')
                    mf_type = mf.get('type', 'unknown')
                    
                    # Show regular value (truncated if too long)
                    if len(str(value)) > 50:
                        print(f"       • {namespace}.{key} ({mf_type}): {str(value)[:50]}...")
                    else:
                        print(f"       • {namespace}.{key} ({mf_type}): {value}")
            
            # Show convenient access fields
            if product.get('size_guide_content'):
                print(f"   📏 SIZE GUIDE QUICK ACCESS:")
                print(f"       Title: '{product.get('size_guide_title')}'")
                content_length = len(product.get('size_guide_content', ''))
                print(f"       Content length: {content_length} characters")
                print(f"       Access via: product.size_guide_content")
            elif failed_references and any(mf.get('key') == 'size_guide' for mf in failed_references):
                print(f"   📏 SIZE GUIDE REFERENCE FOUND (but not resolved):")
                size_guide_mf = next(mf for mf in failed_references if mf.get('key') == 'size_guide')
                print(f"       GID: {size_guide_mf.get('value', 'N/A')}")
                print(f"       Error: {size_guide_mf.get('reference_error', 'Unknown')}")
                print(f"       💡 Add 'read_content' permission to resolve this reference")
        
        # Show first few variants
        variants = product.get('variants', [])[:3]
        if variants:
            print(f"   🏷️  FIRST {min(3, len(variants))} VARIANTS:")
            for i, variant in enumerate(variants, 1):
                title = variant.get('title', f'Variant {i}')
                inventory = variant.get('inventoryQuantity', 0)
                available = variant.get('is_available', False)
                size = variant.get('size', 'N/A')
                price = variant.get('price', 'N/A')
                sku = variant.get('sku', 'N/A')
                print(f"     {i}. {title}")
                print(f"        💰 Price: ${price} | 📏 Size: {size} | 📦 Stock: {inventory}")
                print(f"        🔖 SKU: {sku} | ✅ Available: {available}")
                
        # Show product tags and SEO info
        tags = product.get('tags', [])
        if tags:
            print(f"   🏷️  TAGS: {', '.join(tags[:5])}{'...' if len(tags) > 5 else ''}")
            
        seo = product.get('seo', {})
        if seo:
            print(f"   🔍 SEO Title: {seo.get('title', 'N/A')}")
            
    else:
        print(f"\n❌ GRAPHQL API ERROR:")
        print(f"   Error: {graphql_result.get('error')}")
        if graphql_result.get('details'):
            print(f"   Details: {json.dumps(graphql_result.get('details'), indent=4)}")
    
    print("\n" + "=" * 60)
    print("🎉 GraphQL Test Complete!")
    print("\n💡 Summary:")
    if graphql_result.get("success"):
        product = graphql_result["product"]
        print(f"✅ Successfully fetched product '{product.get('title')}'")
        print(f"📊 Data includes: {len(product.get('variants', []))} variants, {len(product.get('metafields', []))} metafields, {len(product.get('images', []))} images")
        print(f"⏱️  Response Time: {response_time:.3f} seconds")
        print(f"💾 Data Size: {len(str(graphql_result))} characters")
    else:
        print("❌ GraphQL API test failed")
        
    print("\n💡 Debugging Tips:")
    print("• Check the full JSON response above for detailed error information")
    print("• Ensure your access token has 'read_products' permission")
    print("• For metafield references, add 'read_content' permission")
    print("• Verify the product handle exists in your store")
    print("• GraphQL errors often indicate permission or query issues")
    print(f"• Test URL: https://{TEST_CONFIG['shop_url']}/admin/products (to verify store access)")


if __name__ == "__main__":
    import asyncio

    asyncio.run(amain())

def _as_node_list(value: Any) -> List[Dict[str, Any]]:
    """Coerce a connection-shaped or list-shaped field into a list of dict nodes.

    The transform step can fall back to the raw GraphQL response (e.g. when
    ``_transform_graphql_product_response`` hits an exception and returns the
    untransformed ``product_data``). In that shape ``metafields`` / ``images``
    are connections like ``{"edges": [{"node": {...}}]}`` rather than the
    flattened lists the formatter expects. Iterating that dict directly yields
    its string keys (``"edges"``), and calling ``.get()`` on a ``str`` raised
    ``'str' object has no attribute 'get'`` — the symptom that masked the real
    transform error. Normalising here makes the formatter safe regardless of
    which shape it receives, and drops any non-dict entries defensively.
    """
    if not value:
        return []
    if isinstance(value, dict):
        edges = value.get("edges")
        if isinstance(edges, list):
            value = [edge.get("node") for edge in edges if isinstance(edge, dict)]
        else:
            return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _format_graphql_product_for_tools(
    enhanced_product: Dict[str, Any],
    shop_url: str,
    api_method: str,
    website_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the formatted tool-facing product payload from enhanced product data.

    The customer-facing ``url`` is built from the client's canonical
    ``website_url`` (e.g. https://groovee.in) when available, falling back to
    the ``*.myshopify.com`` admin domain only when no website URL is known.
    Mirrors ``ShopifyProductService`` so the internal staging domain never
    leaks into a customer reply.
    """
    raw_product_id = enhanced_product.get("id")
    numeric_product_id = ""
    if isinstance(raw_product_id, str) and raw_product_id.startswith("gid://shopify/Product/"):
        numeric_product_id = raw_product_id.replace("gid://shopify/Product/", "")
    elif raw_product_id is not None:
        numeric_product_id = str(raw_product_id)

    size_guide_content_raw = enhanced_product.get("size_guide_content")
    size_guide_content = _html_to_plain_text(size_guide_content_raw) if size_guide_content_raw else None

    _image_nodes = _as_node_list(enhanced_product.get("images"))

    cleaned_metafields = []
    for mf in _as_node_list(enhanced_product.get("metafields")):
        if mf.get("namespace") == "judgeme":
            continue

        value = mf.get("value", "")
        if isinstance(value, str) and value.startswith('["gid://shopify/'):
            continue
        if isinstance(value, str) and ("<" in value or "&" in value):
            value = _html_to_plain_text(value)
        if value and str(value).strip():
            cleaned_metafields.append({"key": mf.get("key"), "value": value})

    return {
        "id": raw_product_id,
        "product_id": numeric_product_id,
        "handle": enhanced_product.get("handle"),
        "name": enhanced_product.get("title"),
        "title": enhanced_product.get("title"),
        "price": enhanced_product.get("price_range"),
        "image_url": (enhanced_product.get("featuredImage") or {}).get("url")
        or (_image_nodes[0].get("url") if _image_nodes else None),
        "url": _build_product_url(enhanced_product.get("handle"), shop_url, website_url),
        "total_sizes": enhanced_product.get("all_sizes", []),
        "available_sizes": enhanced_product.get("sizes_available", []),
        "colors": enhanced_product.get("colors", []),
        "availability": enhanced_product.get("availability", {}),
        "total_variants": enhanced_product.get("total_variants", 0),
        "variants": enhanced_product.get("variants", []),
        "images_count": len(_image_nodes),
        "metafields_count": len(cleaned_metafields),
        "size_guide": {
            "has_size_guide": size_guide_content is not None,
            "title": enhanced_product.get("size_guide_title"),
            "content": size_guide_content,
            "content_html": size_guide_content_raw,
        },
        "all_metafields": cleaned_metafields,
        "fabric": enhanced_product.get("fabric", ""),
        "care_instructions": enhanced_product.get("care_instructions", ""),
        "fit_type": enhanced_product.get("fit_type", ""),
        "vendor": enhanced_product.get("vendor"),
        "product_type": enhanced_product.get("productType"),
        "tags": enhanced_product.get("tags", []),
        "description": enhanced_product.get("description", ""),
        "api_method": api_method,
    }


async def _afetch_page_via_rest(page_id: str, access_token: str, shop_url: str, api_version: str) -> Dict[str, Any]:
    """Async version of page fetch used for metafield reference resolution."""
    url = f"https://{shop_url}/admin/api/{api_version}/pages/{page_id}.json"
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Content-Type": "application/json",
    }
    client = await get_shared_async_http_client()

    try:
        response = await client.get(url, headers=headers, timeout=10)

        if response.status_code == 200:
            data = response.json()
            page_data = data.get("page", {})
            if page_data:
                transformed_page = {
                    "id": f"gid://shopify/OnlineStorePage/{page_data.get('id')}",
                    "title": page_data.get("title"),
                    "handle": page_data.get("handle"),
                    "body": page_data.get("body_html"),
                    "bodySummary": page_data.get("summary_html", ""),
                    "createdAt": page_data.get("created_at"),
                    "updatedAt": page_data.get("updated_at"),
                    "publishedAt": page_data.get("published_at"),
                }
                return {"success": True, "object": transformed_page, "resource_type": "OnlineStorePage"}
            return {"success": False, "error": "Page data not found in response"}

        if response.status_code == 403:
            return {
                "success": False,
                "error": "Permission denied - missing 'read_content' scope",
                "details": "Your access token needs the 'read_content' permission to access pages. Please update your private app permissions.",
                "suggestion": "Add 'read_content' permission to your private app in the Shopify admin",
            }
        if response.status_code == 404:
            return {
                "success": False,
                "error": f"Page not found (ID: {page_id})",
                "details": "The referenced page may have been deleted or is not accessible",
            }

        try:
            error_json = response.json()
            return {"success": False, "error": f"REST API error {response.status_code}", "details": error_json}
        except Exception:
            return {"success": False, "error": f"REST API error {response.status_code}", "details": response.text[:200]}

    except httpx.TimeoutException:
        return {"success": False, "error": "Timeout while fetching page via REST"}
    except httpx.ConnectError:
        return {"success": False, "error": "Connection error while fetching page via REST"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error fetching page: {str(e)}"}


async def _afetch_referenced_object(gid_info: Dict[str, str], access_token: str, shop_url: str, api_version: str) -> Dict[str, Any]:
    """Async version of metafield GID resolution."""
    if not gid_info or not gid_info.get("resource_type"):
        return {"success": False, "error": "Invalid GID info"}

    resource_type = gid_info["resource_type"]
    gid = gid_info["full_gid"]
    client = await get_shared_async_http_client()

    if resource_type in ["Page", "OnlineStorePage"]:
        query = """
        query getPage($id: ID!) {
            page(id: $id) {
                id
                title
                handle
                body
                bodySummary
                createdAt
                updatedAt
            }
        }
        """
        url = f"https://{shop_url}/admin/api/{api_version}/graphql.json"
        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": access_token,
        }
        payload = {"query": query, "variables": {"id": gid}}

        try:
            response = await client.post(url, headers=headers, json=payload, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if "errors" in data:
                    return await _afetch_page_via_rest(gid_info["numeric_id"], access_token, shop_url, api_version)
                page_data = data.get("data", {}).get("page")
                if page_data:
                    return {"success": True, "object": page_data, "resource_type": "Page"}
                return await _afetch_page_via_rest(gid_info["numeric_id"], access_token, shop_url, api_version)
            return await _afetch_page_via_rest(gid_info["numeric_id"], access_token, shop_url, api_version)
        except Exception:
            return await _afetch_page_via_rest(gid_info["numeric_id"], access_token, shop_url, api_version)

    if resource_type == "Product":
        query = """
        query getProductById($id: ID!) {
            product(id: $id) {
                id
                title
                handle
                description
                productType
                vendor
                createdAt
                updatedAt
            }
        }
        """
    elif resource_type == "Collection":
        query = """
        query getCollectionById($id: ID!) {
            collection(id: $id) {
                id
                title
                handle
                description
                createdAt
                updatedAt
            }
        }
        """
    else:
        query = """
        query getNodeById($id: ID!) {
            node(id: $id) {
                id
                ... on Product {
                    title
                    handle
                    description
                }
                ... on Collection {
                    title
                    handle
                    description
                }
            }
        }
        """

    url = f"https://{shop_url}/admin/api/{api_version}/graphql.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": access_token,
    }
    payload = {"query": query, "variables": {"id": gid}}

    try:
        response = await client.post(url, headers=headers, json=payload, timeout=10)
        if response.status_code != 200:
            error_text = response.text[:200] + "..." if len(response.text) > 200 else response.text
            return {"success": False, "error": f"HTTP {response.status_code}", "details": error_text}

        data = response.json()
        if "errors" in data:
            return {"success": False, "error": "GraphQL errors", "details": data["errors"]}

        if resource_type == "Product":
            object_data = data.get("data", {}).get("product")
        elif resource_type == "Collection":
            object_data = data.get("data", {}).get("collection")
        else:
            object_data = data.get("data", {}).get("node")

        if object_data:
            return {"success": True, "object": object_data, "resource_type": resource_type}
        return {"success": False, "error": f"Referenced {resource_type} not found or not accessible"}

    except httpx.TimeoutException:
        return {"success": False, "error": "Timeout while fetching referenced object"}
    except httpx.ConnectError:
        return {"success": False, "error": "Connection error while fetching referenced object"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def _aenhance_metafields_with_references(metafields: List[Dict], access_token: str, shop_url: str, api_version: str) -> List[Dict]:
    """Async metafield reference resolver for important GID-backed fields."""
    # Keep in sync with the size_guide_keys lookup further down — any key
    # we look up by must also be in this resolve set, otherwise a metafield
    # whose value is a ``gid://shopify/Page/…`` (page_reference) gets stored
    # as the raw GID instead of the actual Page body, and the Shopify
    # fallback path returns a "size guide" whose content is just a GID.
    resolve_references_for = {
        "size_guide", "size_chart", "sizing_chart", "sizechart", "sizeguide",
        "care_guide", "materials_guide",
    }
    enhanced_metafields = []

    for metafield in metafields:
        if not isinstance(metafield, dict):
            continue
        enhanced_field = metafield.copy()
        key = metafield.get("key", "").lower()
        value = metafield.get("value", "")
        should_resolve = key in resolve_references_for and isinstance(value, str) and value.startswith("gid://shopify/")

        if should_resolve:
            gid_info = _parse_shopify_gid(value)
            if gid_info:
                ref_result = await _afetch_referenced_object(gid_info, access_token, shop_url, api_version)
                if ref_result.get("success"):
                    enhanced_field["resolved_reference"] = ref_result["object"]
                    enhanced_field["reference_type"] = ref_result["resource_type"]
                    enhanced_field["original_gid"] = value
                    if ref_result["resource_type"] in ["OnlineStorePage", "Page"]:
                        page_data = ref_result["object"]
                        enhanced_field["page_title"] = page_data.get("title")
                        enhanced_field["page_content"] = page_data.get("body")
                        enhanced_field["page_summary"] = page_data.get("bodySummary")
                        enhanced_field["page_handle"] = page_data.get("handle")
                else:
                    enhanced_field["reference_error"] = ref_result.get("error")
                    enhanced_field["reference_details"] = ref_result.get("details")
                    enhanced_field["reference_suggestion"] = ref_result.get("suggestion")

        enhanced_metafields.append(enhanced_field)

    return enhanced_metafields


async def _atransform_graphql_product_response_with_references(
    product_data: Dict[str, Any],
    access_token: str,
    shop_url: str,
    api_version: str,
) -> Dict[str, Any]:
    """Async version of the GraphQL product transformer with reference resolution."""
    try:
        enhanced_product = _transform_graphql_product_response(product_data)

        metafields = enhanced_product.get("metafields", [])
        if metafields:
            enhanced_metafields = await _aenhance_metafields_with_references(metafields, access_token, shop_url, api_version)
            enhanced_product["metafields"] = enhanced_metafields

            metafield_dict = {}
            for mf in enhanced_metafields:
                key = mf.get("key", "")
                namespace = mf.get("namespace", "")
                full_key = f"{namespace}.{key}" if namespace else key
                metafield_dict[full_key] = mf
                metafield_dict[key] = mf

            enhanced_product["metafields_by_key"] = metafield_dict

            size_guide_keys = [
                "size_guide",
                "size_chart",
                "sizing_chart",
                "sizechart",
                "sizeguide",
                "custom.size_guide",
                "custom.size_chart",
                "shopify.size_chart",
                "product.size_guide",
                "product.size_chart",
            ]

            size_guide = None
            for key in size_guide_keys:
                if key in metafield_dict:
                    size_guide = metafield_dict[key]
                    break

            if size_guide:
                size_guide_value = size_guide.get("value", "")
                size_guide_type = size_guide.get("type", "")

                if size_guide.get("resolved_reference"):
                    enhanced_product["size_guide_content"] = size_guide.get("page_content")
                    enhanced_product["size_guide_title"] = size_guide.get("page_title")
                    enhanced_product["size_guide"] = {
                        "content": size_guide.get("page_content"),
                        "title": size_guide.get("page_title"),
                    }
                elif size_guide_type in ["json", "json_string"]:
                    try:
                        parsed = json.loads(size_guide_value) if isinstance(size_guide_value, str) else size_guide_value
                        enhanced_product["size_guide"] = parsed
                        enhanced_product["size_guide_content"] = str(parsed)
                    except Exception:
                        enhanced_product["size_guide"] = {"content": size_guide_value}
                        enhanced_product["size_guide_content"] = size_guide_value
                elif size_guide_type in ["multi_line_text_field", "single_line_text_field", "rich_text_field"]:
                    enhanced_product["size_guide"] = {"content": size_guide_value}
                    enhanced_product["size_guide_content"] = size_guide_value
                else:
                    enhanced_product["size_guide"] = {"content": size_guide_value}
                    enhanced_product["size_guide_content"] = size_guide_value

            fabric_keys = ["fabric", "material", "fabric_content", "custom.fabric", "product.fabric"]
            care_keys = ["care", "care_instructions", "wash_care", "custom.care", "product.care_instructions"]
            fit_keys = ["fit", "fit_type", "fitting", "custom.fit", "product.fit_type"]

            for key in fabric_keys:
                if key in metafield_dict:
                    enhanced_product["fabric"] = metafield_dict[key].get("value", "")
                    break
            for key in care_keys:
                if key in metafield_dict:
                    enhanced_product["care_instructions"] = metafield_dict[key].get("value", "")
                    break
            for key in fit_keys:
                if key in metafield_dict:
                    enhanced_product["fit_type"] = metafield_dict[key].get("value", "")
                    break

        for variant in enhanced_product.get("variants", []):
            if not isinstance(variant, dict):
                continue
            variant_metafields = variant.get("metafields", [])
            if not isinstance(variant_metafields, list) or not variant_metafields:
                continue
            has_important_metafields = any(
                isinstance(mf, dict) and mf.get("key", "").lower() in ["size_guide", "care_guide", "materials_guide"]
                for mf in variant_metafields
            )
            if has_important_metafields:
                variant["metafields"] = await _aenhance_metafields_with_references(
                    variant_metafields, access_token, shop_url, api_version
                )

        return enhanced_product

    except Exception as e:
        logger.exception(
            "Reference-enhanced transform failed for handle=%s, falling back to plain transform: %s",
            product_data.get("handle") if isinstance(product_data, dict) else None,
            e,
        )
        return _transform_graphql_product_response(product_data)


async def ashopify_get_product_by_id_graphql(
    product_id: Union[str, int],
    access_token: str,
    shop_url: str,
    api_version: str = "2024-04",
    formatted_response: bool = False,
    website_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Async GraphQL product fetch by Shopify product ID."""
    if isinstance(product_id, int) or (isinstance(product_id, str) and product_id.isdigit()):
        product_gid = f"gid://shopify/Product/{product_id}"
    elif isinstance(product_id, str) and product_id.startswith("gid://"):
        product_gid = product_id
    else:
        return {"success": False, "error": f"Invalid product_id format: {product_id}"}

    graphql_query = """
    query getProductById($id: ID!) {
        product(id: $id) {
            id
            title
            description
            descriptionHtml
            handle
            vendor
            productType
            tags
            createdAt
            updatedAt
            publishedAt
            status
            totalInventory
            seo {
                title
                description
            }
            metafields(first: 100) {
                edges {
                    node {
                        id
                        namespace
                        key
                        value
                        type
                        description
                        createdAt
                        updatedAt
                        reference {
                            ... on Metaobject { displayName handle type }
                            ... on TaxonomyValue { name }
                        }
                        references(first: 20) {
                            edges {
                                node {
                                    ... on Metaobject { displayName handle type }
                                    ... on TaxonomyValue { name }
                                }
                            }
                        }
                    }
                }
            }
            options {
                id
                name
                values
                position
            }
            variants(first: 100) {
                edges {
                    node {
                        id
                        title
                        sku
                        barcode
                        price
                        compareAtPrice
                        inventoryQuantity
                        inventoryPolicy
                        taxable
                        selectedOptions {
                            name
                            value
                        }
                        image {
                            id
                            url
                            altText
                        }
                        metafields(first: 50) {
                            edges {
                                node {
                                    id
                                    namespace
                                    key
                                    value
                                    type
                                }
                            }
                        }
                    }
                }
            }
            images(first: 10) {
                edges {
                    node {
                        id
                        url
                        altText
                        width
                        height
                    }
                }
            }
            featuredImage {
                id
                url
                altText
                width
                height
            }
        }
    }
    """

    url = f"https://{shop_url}/admin/api/{api_version}/graphql.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": access_token,
    }
    payload = {
        "query": graphql_query,
        "variables": {"id": product_gid},
    }
    client = await get_shared_async_http_client()

    try:
        response = await client.post(url, headers=headers, json=payload, timeout=15)
        if response.status_code != 200:
            try:
                error_json = response.json()
            except Exception:
                error_json = {}
            return {
                "success": False,
                "error": f"GraphQL API error: {response.status_code}",
                "details": error_json or response.text,
            }

        data = response.json()
        if "errors" in data:
            return {"success": False, "error": "GraphQL errors", "details": data["errors"]}

        product_data = data.get("data", {}).get("product")
        if not product_data:
            return {
                "success": False,
                "error": "Product not found",
                "details": f"No product found with ID {product_gid}",
            }

        enhanced_product = await _atransform_graphql_product_response_with_references(
            product_data, access_token, shop_url, api_version
        )

        if formatted_response:
            return {
                "success": True,
                "product": _format_graphql_product_for_tools(
                    enhanced_product,
                    shop_url,
                    "GraphQL Admin API (by ID)",
                    website_url=website_url,
                ),
                "source": "graphql",
                "formatted": True,
            }
        return {"success": True, "product": enhanced_product, "source": "graphql", "formatted": False}

    except httpx.TimeoutException:
        return {"success": False, "error": "Request timed out while fetching product via GraphQL."}
    except httpx.ConnectError:
        return {"success": False, "error": "Network connection error while fetching product via GraphQL."}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def ashopify_get_product_by_handle_graphql(
    handle: str,
    access_token: str,
    shop_url: str,
    api_version: str = "2024-04",
    formatted_response: bool = False,
    website_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Async GraphQL product fetch by handle."""
    graphql_query = """
    query getProductByHandle($handle: String!) {
        products(first: 1, query: $handle) {
            edges {
                node {
                    id
                    title
                    description
                    descriptionHtml
                    handle
                    vendor
                    productType
                    tags
                    createdAt
                    updatedAt
                    publishedAt
                    status
                    totalInventory
                    seo {
                        title
                        description
                    }
                    metafields(first: 100) {
                        edges {
                            node {
                                id
                                namespace
                                key
                                value
                                type
                                description
                                createdAt
                                updatedAt
                                reference {
                                    ... on Metaobject { displayName handle type }
                                    ... on TaxonomyValue { name }
                                }
                                references(first: 20) {
                                    edges {
                                        node {
                                            ... on Metaobject { displayName handle type }
                                            ... on TaxonomyValue { name }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    options {
                        id
                        name
                        values
                        position
                    }
                    variants(first: 100) {
                        edges {
                            node {
                                id
                                title
                                sku
                                barcode
                                price
                                compareAtPrice
                                inventoryQuantity
                                inventoryPolicy
                                taxable
                                selectedOptions {
                                    name
                                    value
                                }
                                image {
                                    id
                                    url
                                    altText
                                }
                                metafields(first: 50) {
                                    edges {
                                        node {
                                            id
                                            namespace
                                            key
                                            value
                                            type
                                        }
                                    }
                                }
                            }
                        }
                    }
                    images(first: 10) {
                        edges {
                            node {
                                id
                                url
                                altText
                                width
                                height
                            }
                        }
                    }
                    featuredImage {
                        id
                        url
                        altText
                        width
                        height
                    }
                }
            }
        }
    }
    """

    url = f"https://{shop_url}/admin/api/{api_version}/graphql.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": access_token,
    }
    payload = {"query": graphql_query, "variables": {"handle": f"handle:{handle}"}}
    client = await get_shared_async_http_client()

    try:
        response = await client.post(url, headers=headers, json=payload, timeout=15)
        if response.status_code != 200:
            try:
                error_json = response.json()
            except Exception:
                error_json = {}
            return {"success": False, "error": f"GraphQL API error: {response.status_code}", "details": error_json or response.text}

        data = response.json()
        if "errors" in data:
            return {"success": False, "error": "GraphQL errors", "details": data["errors"]}

        products_data = data.get("data", {}).get("products", {}).get("edges", [])
        if not products_data:
            return {"success": False, "error": "Product not found", "details": "No product found with the given handle"}

        product_data = products_data[0]["node"]
        enhanced_product = await _atransform_graphql_product_response_with_references(
            product_data, access_token, shop_url, api_version
        )

        if formatted_response:
            return {
                "success": True,
                "product": _format_graphql_product_for_tools(
                    enhanced_product,
                    shop_url,
                    "GraphQL Admin API",
                    website_url=website_url,
                ),
                "source": "graphql",
                "formatted": True,
            }
        return {"success": True, "product": enhanced_product, "source": "graphql", "formatted": False}

    except httpx.TimeoutException:
        return {"success": False, "error": "Request timed out while fetching product via GraphQL."}
    except httpx.ConnectError:
        return {"success": False, "error": "Network connection error while fetching product via GraphQL."}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def ashopify_graphql_product_search(
    title_query: str,
    access_token: str,
    shop_url: str,
    api_version: str = "2023-04",
    first: int = 10,
    website_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Async Shopify product name search with full product hydration."""
    url = f"https://{shop_url}/admin/api/{api_version}/graphql.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": access_token,
    }
    graphql_query = {
        "query": f'{{ products(first: {first}, query: "title:{title_query}* AND status:ACTIVE") {{ edges {{ node {{ id title handle }} }} }} }}'
    }
    client = await get_shared_async_http_client()

    try:
        response = await client.post(url, headers=headers, json=graphql_query, timeout=15)
        if response.status_code != 200:
            try:
                error_json = response.json()
            except Exception:
                error_json = {}
            return {"success": False, "error": f"Shopify GraphQL error: {response.status_code}", "details": error_json or response.text}

        search_data = response.json().get("data", {})
        products_edges = search_data.get("products", {}).get("edges", [])
        if not products_edges:
            return {"success": True, "products": [], "total_found": 0, "query": title_query}

        complete_products = []
        errors = []
        for edge in products_edges:
            node = edge.get("node", {})
            handle = node.get("handle")
            title = node.get("title", "Unknown")

            if not handle:
                errors.append(f"No handle found for product: {title}")
                continue

            product_result = await ashopify_get_product_by_handle_graphql(
                handle=handle,
                access_token=access_token,
                shop_url=shop_url,
                api_version=api_version,
                formatted_response=True,
                website_url=website_url,
            )

            if product_result.get("success"):
                complete_products.append(product_result.get("product", {}))
            else:
                errors.append(
                    f"Failed to fetch details for '{title}' (handle: {handle}): {product_result.get('error', 'Unknown error')}"
                )

        result = {
            "success": True,
            "products": complete_products,
            "total_found": len(products_edges),
            "successful_fetches": len(complete_products),
            "query": title_query,
            "search_method": "GraphQL with complete product details",
        }
        if errors:
            result["errors"] = errors
            result["partial_success"] = True
        return result

    except httpx.TimeoutException:
        return {"success": False, "error": "Request timed out while searching products (GraphQL)."}
    except httpx.ConnectError:
        return {"success": False, "error": "Network connection error while searching products (GraphQL)."}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def ashopify_get_top_selling_products_rest(
    top_n: int = 5,
    access_token: str = None,
    shop_url: str = None,
    api_version: str = "2024-04",
    website_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Async top-selling products fetch based on recent Shopify orders."""
    try:
        from datetime import datetime, timedelta
        
        since = (datetime.utcnow() - timedelta(days=7)).isoformat() + "Z"
        base_url = f"https://{shop_url}/admin/api/{api_version}"
        headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json",
        }
        orders_url = f"{base_url}/orders.json?status=any&created_at_min={since}&fields=id,line_items&limit=250"
        sales_count = {}
        total_orders = 0
        client = await get_shared_async_http_client()
        
        while orders_url:
            response = await client.get(orders_url, headers=headers, timeout=15)
            if response.status_code != 200:
                return {
                    "success": False,
                    "error": f"Failed to fetch orders: HTTP {response.status_code}",
                    "details": response.text[:500],
                }
            
            data = response.json()
            orders = data.get("orders", [])
            total_orders += len(orders)
            
            for order in orders:
                for item in order.get("line_items", []):
                    product_id = item.get("product_id")
                    quantity = item.get("quantity", 0)
                    if product_id:
                        sales_count[product_id] = sales_count.get(product_id, 0) + quantity
            
            orders_url = None
            link_header = response.headers.get("Link", "")
            if 'rel="next"' in link_header:
                next_match = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
                if next_match:
                    orders_url = next_match.group(1)
        
        if not sales_count:
            return {
                "success": True,
                "message": "No sales data found in the last 7 days",
                "products": [],
                "total_orders_analyzed": total_orders,
                "date_range": f"Last 7 days (since {since})",
            }
        
        top_product_sales = sorted(sales_count.items(), key=lambda x: x[1], reverse=True)[:top_n]
        
        products = []
        for product_id, sales_qty in top_product_sales:
            try:
                product_url = f"{base_url}/products/{product_id}.json"
                product_response = await client.get(product_url, headers=headers, timeout=10)
                if product_response.status_code != 200:
                    continue

                product_data = product_response.json().get("product", {})
                if not product_data:
                    continue

                variants = product_data.get("variants", [])
                price = variants[0].get("price") if variants else None
                images = product_data.get("images", [])
                image_url = images[0].get("src") if images else None

                handle = product_data.get("handle")
                store_url = _build_product_url(handle, shop_url, website_url)

                options = product_data.get("options", [])
                all_sizes = []
                available_sizes = []
                colors = []
                size_names = {"size", "sizes"}
                color_names = {"color", "colors", "colour", "colours"}
                for opt in options:
                    opt_name = (opt.get("name") or "").lower()
                    if opt_name in size_names:
                        all_sizes = opt.get("values", [])
                    elif opt_name in color_names:
                        colors = opt.get("values", [])

                compact_variants = []
                total_inventory = 0
                for variant in variants:
                    inv_qty = variant.get("inventory_quantity", 0)
                    total_inventory += max(inv_qty, 0)
                    size_val = variant.get("option1")
                    if inv_qty > 0 and size_val and size_val not in available_sizes:
                        available_sizes.append(size_val)
                    compact_variants.append(
                        {
                            "id": variant.get("id"),
                            "title": variant.get("title"),
                            "price": variant.get("price"),
                            "sku": variant.get("sku"),
                            "inventory_quantity": inv_qty,
                            "available": inv_qty > 0 or variant.get("inventory_policy") == "continue",
                            "option1": variant.get("option1"),
                            "option2": variant.get("option2"),
                            "option3": variant.get("option3"),
                        }
                    )

                description = ""
                body_html = product_data.get("body_html") or ""
                if body_html:
                    description = clean_html_description(body_html)

                products.append(
                    {
                        "id": product_data.get("id"),
                        "title": product_data.get("title"),
                        "handle": handle,
                        "vendor": product_data.get("vendor"),
                        "category": product_data.get("product_type"),
                        "tags": product_data.get("tags", []),
                        "price": price,
                        "image": image_url,
                        "url": store_url,
                        "sales": sales_qty,
                        "created_at": product_data.get("created_at"),
                        "updated_at": product_data.get("updated_at"),
                        "status": product_data.get("status"),
                        "description": description,
                        "all_sizes": all_sizes,
                        "available_sizes": available_sizes,
                        "colors": colors,
                        "variants": compact_variants,
                        "total_inventory": total_inventory,
                        "in_stock": total_inventory > 0,
                    }
                )
            except Exception:
                continue
        
        return {
            "success": True,
            "products": products,
            "total_products_analyzed": len(sales_count),
            "total_orders_analyzed": total_orders,
            "method": "REST API Sales Analysis",
            "note": "Based on actual sales data from last 7 days",
            "date_range": f"Last 7 days (since {since})",
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": "Unexpected error in sales analysis",
            "details": str(e),
        }
