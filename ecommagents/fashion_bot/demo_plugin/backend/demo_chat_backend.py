"""
Demo Chat Backend - Standalone server for the Product Chat Demo extension

This is a completely standalone FastAPI server that:
1. Receives page context and user messages from the Chrome extension
2. Uses OpenAI GPT to answer questions about the scraped page content
3. Provides mock e-commerce actions (order, add to cart, etc.)

Run with: uvicorn demo_chat_backend:app --port 8001 --reload
"""

import os
import json
import logging
import secrets
from typing import Dict, List, Optional, Any
from datetime import datetime
from pathlib import Path

# Load environment variables from .env file
from dotenv import load_dotenv

# Try to load from multiple possible .env locations
env_paths = [
    Path(__file__).parent / ".env",  # demo_plugin/backend/.env
    Path(__file__).parent.parent / ".env",  # demo_plugin/.env
    Path(__file__).parent.parent.parent / ".env",  # fashion_bot/.env (project root)
    Path(__file__).parent.parent.parent / "fashion_bot" / ".env",  # fashion_bot/fashion_bot/.env
]

env_loaded = False
for env_path in env_paths:
    if env_path.exists():
        load_dotenv(env_path)
        print(f"✅ Loaded .env from: {env_path}")
        env_loaded = True
        break

if not env_loaded:
    # Try default load_dotenv which searches up the directory tree
    load_dotenv()
    print("⚠️ No .env file found in expected locations, using default search")

import sys
# Add project root to path so fashion_bot package is importable
_project_root = str(Path(__file__).parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

UPSTASH_SEARCH_AVAILABLE = False
try:
    from fashion_bot.services.recommendation.recommendation_service import search_products_pipeline
    from fashion_bot.utils.product_utils import format_products_for_llm
    UPSTASH_SEARCH_AVAILABLE = True
except ImportError as e:
    print(f"⚠️ Upstash Search modules not available: {e}")

STORE_LOCATIONS_AVAILABLE = False
try:
    from fashion_bot.utils.store_locations import afind_nearest_store, aget_all_stores
    STORE_LOCATIONS_AVAILABLE = True
except ImportError as e:
    print(f"⚠️ Store location modules not available: {e}")

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("demo_chat")

# Initialize FastAPI app
app = FastAPI(
    title="Product Chat Demo Backend",
    description="Backend for the Chrome extension demo chat widget",
    version="1.0.0"
)

# Enable CORS for Chrome extension
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== MODELS ====================

class ProductInfo(BaseModel):
    name: Optional[str] = None
    price: Optional[str] = None
    originalPrice: Optional[str] = None
    discount: Optional[str] = None
    description: Optional[str] = None
    image: Optional[str] = None
    imageAlt: Optional[str] = None
    imageTitle: Optional[str] = None
    imageDescriptions: Optional[List[str]] = None  # From alt/title of multiple images
    attributes: Optional[Dict[str, str]] = None

class PageContext(BaseModel):
    url: str
    domain: str
    title: Optional[str] = None
    metaDescription: Optional[str] = None
    product: Optional[ProductInfo] = None
    pageText: Optional[str] = None
    structuredData: Optional[List[Dict]] = None
    openGraph: Optional[Dict[str, str]] = None
    timestamp: Optional[str] = None

class ChatMessage(BaseModel):
    role: str  # 'user' or 'assistant'
    content: str

class ChatRequest(BaseModel):
    message: str
    context: PageContext
    history: Optional[List[ChatMessage]] = None
    clientId: Optional[str] = None

class ChatResponse(BaseModel):
    reply: str
    action: Optional[str] = None
    metadata: Optional[Dict] = None
    products: Optional[List[Dict]] = None

# ==================== MOCK E-COMMERCE ACTIONS ====================

class DemoEcommerceActions:
    """Mock e-commerce actions for demo purposes"""
    
    @staticmethod
    def check_order_intent(message: str) -> Optional[str]:
        """
        Return a keyword only when the user wants to order the CURRENT product,
        not when they're browsing for a category ("buy shoes", "buy bags").
        """
        message_lower = message.lower()

        # Phrases that clearly refer to the current product
        direct_keywords = [
            'want this', 'get this', 'order this', 'buy this',
            'purchase this', 'checkout', 'place order', 'add to cart',
            'order it', 'buy it', 'purchase it', 'i\'ll take it',
        ]
        for keyword in direct_keywords:
            if keyword in message_lower:
                return keyword

        # "buy" / "purchase" / "order" followed by a generic noun = browsing
        # Only match if not followed by more words (i.e. "I want to buy" alone)
        import re
        browse_pattern = re.compile(
            r'\b(buy|purchase|order|get|want)\b\s+\b(a|an|some|the|me|any)?\s*\b\w+',
            re.IGNORECASE,
        )
        if browse_pattern.search(message_lower):
            return None

        return None
    
    @staticmethod
    def mock_order_response(product_name: str, price: str = None) -> str:
        """Generate mock order confirmation"""
        order_id = f"DEMO-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        
        response = f"""🎉 **Order Placed Successfully!** (Demo)

**Order ID:** {order_id}
**Product:** {product_name}
"""
        if price:
            response += f"**Amount:** {price}\n"
        
        response += """
**Status:** Processing
**Estimated Delivery:** 3-5 business days

⚠️ *This is a demo order. No actual transaction has been made.*

Would you like to:
- Track this order
- Continue shopping
- Ask about return policy"""
        
        return response
    
    @staticmethod
    def mock_add_to_cart_response(product_name: str) -> str:
        """Generate mock add to cart response"""
        return f"""✅ **Added to Cart!** (Demo)

**{product_name}** has been added to your cart.

Your cart now has **1 item**.

Would you like to:
- Proceed to checkout
- Continue shopping
- View cart"""

# ==================== PRODUCT SEARCH SERVICE (Upstash Search) ====================

class ProductSearchService:
    """AI-powered product search using the shared search_products_pipeline."""

    def __init__(self):
        self._fallback_client_id = os.getenv("DEMO_CLIENT_ID")
        if UPSTASH_SEARCH_AVAILABLE:
            logger.info("✅ Upstash Search modules loaded")
            if self._fallback_client_id:
                logger.info(f"   Fallback DEMO_CLIENT_ID: {self._fallback_client_id[:8]}...")
        else:
            logger.warning("⚠️ Upstash Search not available - product search disabled")

    def resolve_client_id(self, request_client_id: Optional[str] = None) -> Optional[str]:
        return request_client_id or self._fallback_client_id

    def is_available(self, client_id: Optional[str] = None) -> bool:
        return UPSTASH_SEARCH_AVAILABLE and bool(self.resolve_client_id(client_id))

    @staticmethod
    def tool_definition() -> dict:
        """OpenAI function-calling tool schema."""
        return {
            "type": "function",
            "function": {
                "name": "search_products",
                "description": (
                    "Search the store's product catalog for products. "
                    "Use when the user asks for product recommendations, similar or complementary products, "
                    "wants to browse/explore products, or searches for specific items by name, category, "
                    "style, color, or price range."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Natural language search query describing what the user wants "
                                "(e.g. 'black hoodies under 2000', 'casual T-shirts', 'party dresses')"
                            ),
                        }
                    },
                    "required": ["query"],
                },
            },
        }

    async def search_products(
        self,
        query: str,
        client_id: Optional[str] = None,
        product_context: Optional[ProductInfo] = None,
        history: Optional[List[ChatMessage]] = None,
    ) -> tuple:
        """
        Run the shared search pipeline: QU -> Search -> Rerank.

        Returns (formatted_text, products) where *products* is a list of
        merged content+metadata dicts (same shape as ``tool_registry``).
        """
        effective_id = self.resolve_client_id(client_id)
        if not UPSTASH_SEARCH_AVAILABLE or not effective_id:
            return None, []

        try:
            conv_history = []
            if history:
                for msg in history[-20:]:
                    conv_history.append({"role": msg.role, "content": msg.content[:300]})

            exclude_handle = None
            focal_product_context = None
            if product_context and product_context.name:
                import re as _re
                url = getattr(product_context, "url", "") or ""
                handle_match = _re.search(r'/products/([^/?]+)', url)
                if handle_match:
                    exclude_handle = handle_match.group(1)
                attrs = product_context.attributes or {}
                focal_product_context = {
                    "name": product_context.name,
                    "subcategory": attrs.get("subcategory") or attrs.get("product_type") or attrs.get("type"),
                    "category": attrs.get("category"),
                    "color": attrs.get("color"),
                    "style": attrs.get("style"),
                    "price": product_context.price,
                }
                focal_product_context = {k: v for k, v in focal_product_context.items() if v}

            pipeline = await search_products_pipeline(
                query=query,
                client_id=effective_id,
                conversation_history=conv_history,
                user_profile={},
                exclude_handle=exclude_handle,
                max_results=5,
                product_context=focal_product_context,
            )

            if not pipeline.products:
                return None, []

            products = []
            for p in pipeline.products:
                merged = {}
                merged.update(p.get("content", {}))
                merged.update(p.get("metadata", {}))
                products.append(merged)

            formatted_text = format_products_for_llm(products, pipeline.follow_up)
            for prod in products:
                logger.info(
                    f"  Card: {prod.get('title','?')[:40]}... "
                    f"image_url={prod.get('image_url','EMPTY')[:80]}"
                )
            return formatted_text, products

        except Exception as e:
            logger.error(f"Product search error: {e}", exc_info=True)
            return None, []


product_search_service = ProductSearchService()


# ==================== NEAREST STORE SERVICE ====================

class NearestStoreService:
    """Thin wrapper around the shared store_locations utilities."""

    def __init__(self):
        self._fallback_client_id = os.getenv("DEMO_CLIENT_ID")
        if STORE_LOCATIONS_AVAILABLE:
            logger.info("✅ Store location modules loaded")
        else:
            logger.warning("⚠️ Store location modules not available")

    def resolve_client_id(self, request_client_id: Optional[str] = None) -> Optional[str]:
        return request_client_id or self._fallback_client_id

    async def is_available(self, client_id: Optional[str] = None) -> bool:
        if not STORE_LOCATIONS_AVAILABLE:
            return False
        cid = self.resolve_client_id(client_id)
        if not cid:
            return False
        stores = await aget_all_stores(cid)
        return bool(stores)

    @staticmethod
    def tool_definition() -> dict:
        return {
            "type": "function",
            "function": {
                "name": "get_nearest_store",
                "description": (
                    "Find the nearest physical/offline store for this brand based on a "
                    "6-digit Indian pincode. Use when: a product/variant is "
                    "out of stock, the customer asks about store/office/warehouse location "
                    "or brand authenticity, or the customer is confused about size/fit/material "
                    "and might benefit from an in-store visit."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pincode": {
                            "type": "string",
                            "description": "Customer's 6-digit Indian pincode (e.g. '411038')",
                        }
                    },
                    "required": ["pincode"],
                },
            },
        }

    async def find_store(self, pincode: str, client_id: Optional[str] = None) -> str:
        cid = self.resolve_client_id(client_id)
        if not cid:
            return json.dumps({"success": False, "error": "Client not identified"})

        results = await afind_nearest_store(
            cid,
            city=None,
            pincode=pincode.strip(),
            limit=2,
        )
        if not results:
            return json.dumps({"success": True, "message": "No stores found within 30 km of this location."})

        return json.dumps({
            "success": True,
            "nearest_store": results[0],
            "other_stores": results[1:] if len(results) > 1 else [],
            "presentation_hint": (
                "ALWAYS include the google_maps_url link in your response "
                "so the customer can navigate to the store directly."
            ),
        }, default=str)


nearest_store_service = NearestStoreService()

# ==================== LLM SERVICE ====================

class LLMService:
    """Service for handling LLM interactions"""
    
    def __init__(self):
        self.client = None
        openrouter_key = os.getenv("OPENROUTER_API_KEY")
        openai_key = os.getenv("OPENAI_API_KEY")

        if openrouter_key:
            self.api_key = openrouter_key
            self.model = os.getenv("DEMO_PLUGIN_MODEL", "openai/gpt-4o")
            self.client = OpenAI(
                api_key=openrouter_key,
                base_url="https://openrouter.ai/api/v1",
            )
            logger.info(f"✅ OpenRouter client initialized (model={self.model})")
        elif openai_key:
            self.api_key = openai_key
            self.model = os.getenv("DEMO_PLUGIN_MODEL", "gpt-4o")
            self.client = OpenAI(api_key=openai_key)
            logger.info(f"✅ OpenAI client initialized (model={self.model})")
        else:
            self.api_key = None
            self.model = "gpt-4o"
            logger.warning("⚠️ Neither OPENROUTER_API_KEY nor OPENAI_API_KEY set - using fallback responses")
    
    @staticmethod
    def _smart_truncate_page_text(raw: str, budget: int = 8000) -> str:
        """Keep the head (product info) and tail (JSON-LD) of pageText within *budget* chars."""
        if len(raw) <= budget:
            return raw
        jsonld_marker = "=== PRODUCT STRUCTURED DATA (JSON-LD) ==="
        idx = raw.find(jsonld_marker)
        if idx != -1:
            tail = raw[idx:]
            head_budget = budget - len(tail) - 40
            if head_budget > 500:
                return raw[:head_budget] + "\n... (trimmed) ...\n" + tail
        half = budget // 2
        return raw[:half] + "\n... (trimmed) ...\n" + raw[-half:]

    def build_system_prompt(self, context: PageContext) -> str:
        """Build system prompt with page context"""

        prompt = """You are a fashion e-commerce assistant on a website. You answer product questions from page context and help discover products via the search_products tool.

QUERY TYPES:
A) PRODUCT DETAILS — answer from PAGE CONTEXT below. Do NOT call search_products.
B) DISCOVERY / RECOMMENDATIONS — MUST call search_products with the user's raw query. Never invent products or URLs.

PRODUCT DETAIL RULES:
- Answer only what was asked, concisely (1-3 sentences).
- State info directly ("Delivery takes 3-5 days"), never reference the page.
- No discount or save Rs.0 → "No active discounts right now." Never say "you save Rs.0".
- Unknown info → say so briefly, share what you know.
- Never invent specs. Use only what's in the context.

Fabric hints (use only when fabric appears in product data): Suede=premium napped leather; Bamboo Lycra=eco, breathable; Canvas=sturdy twill; Leather=ages well; Cotton=breathable; Denim=durable twill.

SEARCH / RECOMMENDATION RULES:
- Pass the user's query as-is to search_products. The tool handles query understanding internally.
- Curate and present the TOP 3 most relevant products from the tool results.
- Only show products from the same type family the user asked for. Discard mismatches.
  Type families: T-shirts/Tees/Polos | Shirts/Formal/Casual | Hoodies/Sweatshirts | Jackets/Blazers/Outerwear | Jeans/Denim | Trousers/Pants/Chinos/Joggers | Shorts/Jorts | Dresses | Co-ord Sets
- If none match, tell the user and offer what was found.
- Climate awareness: tropical→lightweight; cold→layers; formal→trousers+shirts; party→statement pieces. Exclude climate-inappropriate results.
- If the tool returns a suggested follow-up question, ask it to the user at the end of your response.

NEAREST STORE RULES (only when get_nearest_store tool is available):
- When a product/variant is out of stock, size/fit/material is confusing, or the customer asks about a physical store/office/warehouse/authenticity, ask for their pincode and call get_nearest_store.
- Only ask for pincode (6-digit), not city.
- Present the store with: name, address, phone, manager contact, hours, and the google_maps_url from the tool result.
- CRITICAL: You MUST always include the google_maps_url link from the tool result so the customer can navigate directly. Never omit this link or say you don't have it.
- If no store is found within range, say so and offer to help online instead.

OUTPUT: concise, conversational, plain URLs (no markdown links), numbered lists for multiple products, emojis sparingly. Never fabricate data.
"""

        prompt += f"\n--- PAGE CONTEXT ---\nURL: {context.url}\nDomain: {context.domain}\n"

        if context.title:
            prompt += f"Page Title: {context.title}\n"

        if context.product:
            prompt += "\n--- PRODUCT INFORMATION ---\n"
            if context.product.name:
                prompt += f"Name: {context.product.name}\n"
            if context.product.price:
                prompt += f"Price: {context.product.price}\n"
            if context.product.originalPrice:
                prompt += f"Original Price: {context.product.originalPrice}\n"
            if context.product.discount:
                prompt += f"Discount: {context.product.discount}\n"
            if context.product.description:
                prompt += f"Description: {context.product.description}\n"
            if context.product.attributes:
                prompt += "Attributes: " + "; ".join(f"{k}: {v}" for k, v in context.product.attributes.items()) + "\n"
            if context.product.imageAlt:
                prompt += f"Image Context: {context.product.imageAlt}\n"
            if context.product.imageDescriptions:
                prompt += f"Image Info: {', '.join(context.product.imageDescriptions)}\n"

        if context.pageText:
            page_text = self._smart_truncate_page_text(context.pageText, budget=8000)
            prompt += f"\n--- PAGE TEXT ---\n{page_text}\n--- END PAGE TEXT ---\n"
            logger.info(f"📄 Page text: {len(context.pageText)} raw → {len(page_text)} sent")

        if context.structuredData:
            prompt += f"\n--- STRUCTURED DATA ---\n{json.dumps(context.structuredData, indent=2)[:1000]}\n"

        prompt += "\n--- END CONTEXT ---\nRespond to the user using the above context. Be helpful and concise."

        logger.info(f"📤 System prompt: {len(prompt)} chars")
        return prompt
    
    async def get_response(
        self, message: str, context: PageContext,
        history: List[ChatMessage] = None, client_id: Optional[str] = None,
    ) -> tuple:
        """
        Get LLM response for user message.

        Uses OpenAI function calling: the LLM decides whether to invoke the
        Upstash Search tool for product discovery / recommendations.

        Returns (reply_text, recent_products) where recent_products is a list of
        dicts with title/handle/price/image_url/url from Upstash Search, or
        an empty list when no search was performed.
        """
        # Only short-circuit to mock order when we're on an actual product
        # page (URL has /products/) and the user explicitly refers to THIS
        # product.  Everything else goes through the LLM + search tool.
        is_product_page = '/products/' in (context.url or '') or '/product/' in (context.url or '')
        order_intent = DemoEcommerceActions.check_order_intent(message)
        if order_intent and is_product_page and context.product and context.product.name:
            product_name = context.product.name
            price = context.product.price
            
            if 'cart' in message.lower():
                return DemoEcommerceActions.mock_add_to_cart_response(product_name), []
            else:
                return DemoEcommerceActions.mock_order_response(product_name, price), []
        
        if not self.client:
            return self.get_fallback_response(message, context), []

        try:
            messages = [
                {"role": "system", "content": self.build_system_prompt(context)}
            ]

            if history:
                for msg in history[-20:]:
                    messages.append({"role": msg.role, "content": msg.content})

            messages.append({"role": "user", "content": message})

            # Build tools list
            tools = []
            if product_search_service.is_available(client_id):
                tools.append(product_search_service.tool_definition())
            if await nearest_store_service.is_available(client_id):
                tools.append(nearest_store_service.tool_definition())
            tools = tools or None

            # ── First LLM call (may trigger a tool call) ──
            create_kwargs = dict(
                model=self.model,
                messages=messages,
                max_tokens=400,
                temperature=0.7,
            )
            if tools:
                create_kwargs["tools"] = tools

            response = self.client.chat.completions.create(**create_kwargs)
            choice = response.choices[0]

            # ── Handle tool call if the LLM decided to search ──
            search_recent_products: List[Dict] = []

            if choice.message.tool_calls:
                messages.append(choice.message)

                for tool_call in choice.message.tool_calls:
                    args = json.loads(tool_call.function.arguments)

                    if tool_call.function.name == "search_products":
                        logger.info(f"🔧 LLM invoked search_products(query='{args.get('query')}')")
                        result, cards = await product_search_service.search_products(
                            query=args.get("query", message),
                            client_id=client_id,
                            product_context=context.product,
                            history=history,
                        )
                        search_recent_products = cards
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": result or "No products found matching your criteria.",
                        })

                    elif tool_call.function.name == "get_nearest_store":
                        logger.info(f"🏬 LLM invoked get_nearest_store(pincode='{args.get('pincode')}')")
                        store_result = await nearest_store_service.find_store(
                            pincode=args.get("pincode", ""),
                            client_id=client_id,
                        )
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": store_result,
                        })

                # ── Second LLM call with tool results ──
                final = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=400,
                    temperature=0.7,
                )
                return final.choices[0].message.content, search_recent_products

            return choice.message.content, []

        except Exception as e:
            logger.error(f"OpenAI API error: {e}")
            return self.get_fallback_response(message, context), []
    
    def get_fallback_response(self, message: str, context: PageContext) -> str:
        """Fallback responses when LLM is not available"""
        
        message_lower = message.lower()
        product = context.product
        
        # Product name queries
        if any(word in message_lower for word in ['what is', 'tell me about', 'what product', 'this product']):
            if product and product.name:
                response = f"This is **{product.name}**"
                if product.description:
                    response += f"\n\n{product.description[:300]}..."
                return response
            return f"This page is from {context.domain}. {context.title or 'I can help you understand this product.'}"
        
        # Price queries
        if any(word in message_lower for word in ['price', 'cost', 'how much', 'rate']):
            if product and product.price:
                response = f"💰 The price is **{product.price}**"
                if product.originalPrice:
                    response += f" (Original: {product.originalPrice})"
                if product.discount:
                    response += f"\n🏷️ **{product.discount}**"
                return response
            return "I couldn't find pricing information on this page. Please check the product page directly."
        
        # Discount queries
        if any(word in message_lower for word in ['discount', 'offer', 'sale', 'deal']):
            if product and product.discount and "0%" not in product.discount and "₹0" not in str(product.originalPrice):
                return f"🎉 Yes! There's a **{product.discount}** on this product!\n\nCurrent price: {product.price}\nOriginal: {product.originalPrice}"
            elif product and product.price:
                return f"There are no active discounts on this product right now.\n\nBut **{product.name or 'this product'}** at **{product.price}** is a premium offering with excellent craftsmanship! Would you like to know more about its features?"
            return "I don't see any active discounts on this page. Is there anything else I can help you with?"
        
        # Material/specification queries
        if any(word in message_lower for word in ['material', 'made of', 'fabric', 'specification', 'specs', 'ingredients', 'contains', 'environment', 'eco', 'sustainable']):
            response = ""
            if product and product.attributes:
                response = "📋 **Here's what I know about this product:**\n"
                for key, value in product.attributes.items():
                    response += f"• **{key.title()}:** {value}\n"
                response += "\n✨ This product has been crafted with attention to detail and quality!"
            elif product and product.name:
                response = f"✨ **{product.name}** is one of our premium offerings!\n\n"
                if product.description:
                    response += f"{product.description[:200]}...\n\n"
                if product.price:
                    response += f"💰 Available at **{product.price}**\n\n"
                response += "For specific material details, you can check the product page or our team would be happy to help with any questions!"
            else:
                response = "✨ Great question! This product is crafted with quality materials. For detailed specifications, I'd recommend checking the product page - but I can tell you it's a fantastic choice!"
            return response
        
        # Default response
        return f"""I understand you're asking about "{message}"

Based on what I can see on this page ({context.domain}):
{f'• Product: {product.name}' if product and product.name else ''}
{f'• Price: {product.price}' if product and product.price else ''}

Feel free to ask me specific questions about:
• Product details
• Pricing & discounts
• Specifications
• How to order"""

# Initialize LLM service
llm_service = LLMService()

# Optional: set DEMO_CHAT_X_API_KEY in env to require X-API-Key on POST /chat
DEMO_CHAT_X_API_KEY = os.getenv("DEMO_CHAT_X_API_KEY", "").strip()

# ==================== API ENDPOINTS ====================

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "Product Chat Demo Backend",
        "status": "running",
        "version": "1.0.0"
    }

@app.get("/health")
async def health_check():
    """Health check endpoint for the Chrome extension"""
    return {
        "status": "healthy",
        "llm_available": llm_service.client is not None,
        "product_search_available": product_search_service.is_available(),
        "timestamp": datetime.now().isoformat()
    }

@app.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    x_api_key: Optional[str] = Header(None)
):
    """
    Main chat endpoint
    
    Receives user message with page context and returns AI response.
    The LLM decides autonomously (via function calling) whether to invoke
    the Upstash Search tool for product discovery / recommendations.
    """
    if DEMO_CHAT_X_API_KEY:
        if not x_api_key:
            raise HTTPException(status_code=401, detail="Missing X-API-Key")
        if not secrets.compare_digest(
            x_api_key.encode("utf-8"),
            DEMO_CHAT_X_API_KEY.encode("utf-8"),
        ):
            raise HTTPException(status_code=401, detail="Invalid X-API-Key")

    logger.info(f"📩 Chat request from {request.context.domain}: {request.message[:50]}...")
    
    try:
        reply, products = await llm_service.get_response(
            message=request.message,
            context=request.context,
            history=request.history,
            client_id=request.clientId,
        )
        
        # Determine if there was an action
        action = None
        if "Order Placed" in reply:
            action = "order_placed"
        elif "Added to Cart" in reply:
            action = "add_to_cart"
        
        logger.info(f"✅ Response generated ({len(reply)} chars), {len(products)} product cards")
        
        return ChatResponse(
            reply=reply,
            action=action,
            products=products or None,
            metadata={
                "product_detected": request.context.product is not None,
                "domain": request.context.domain
            }
        )
        
    except Exception as e:
        logger.error(f"❌ Chat error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/scrape-context")
async def scrape_context(url: str):
    """
    Optional: Server-side page scraping
    (For cases where client-side scraping doesn't work)
    """
    # This would use something like playwright or requests+beautifulsoup
    # For now, return a placeholder
    return {
        "status": "not_implemented",
        "message": "Server-side scraping is not implemented. Use client-side scraping via the extension."
    }

# ==================== MAIN ====================

if __name__ == "__main__":
    import uvicorn
    
    port = int(os.getenv("DEMO_CHAT_PORT", "8001"))
    
    print(f"""
╔══════════════════════════════════════════════════════════╗
║           Product Chat Demo Backend                       ║
╠══════════════════════════════════════════════════════════╣
║  Starting server on port {port}                             ║
║                                                          ║
║  Endpoints:                                              ║
║  • GET  /         - Service info                         ║
║  • GET  /health   - Health check                         ║
║  • POST /chat     - Chat endpoint                        ║
║                                                          ║
║  OpenAI: {'✅ Configured' if os.getenv('OPENAI_API_KEY') else '❌ Not configured (fallback mode)'}                            ║
║  Product Search: {'✅ Upstash Search ready' if UPSTASH_SEARCH_AVAILABLE else '❌ Disabled (missing modules)'}               ║
╚══════════════════════════════════════════════════════════╝
    """)
    
    uvicorn.run(app, host="0.0.0.0", port=port)

