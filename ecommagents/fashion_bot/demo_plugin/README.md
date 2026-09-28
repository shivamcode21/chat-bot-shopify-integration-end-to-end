# Product Chat Demo Plugin - Enhanced with Shopify Runtime

A Chrome extension + FastAPI backend for demonstrating AI-powered product chat on ANY website, with **special Shopify runtime data fetching** capabilities.

## ✨ Key Features

### 🛒 Shopify Runtime Data (NEW!)
- **Zero Integration Required**: Works on any Shopify store without backend setup
- **Real-time Product Fetching**: Live access to `/products.json`, `/collections.json`
- **Intent Detection**: Smart understanding of user queries (price, availability, policies)
- **Product Cards**: Visual product recommendations in chat
- **Automatic Caching**: 2-minute TTL for optimal performance

### 🎯 Smart Recommendations (NEW!)
- **Category-based**: "More sunglasses you may like" - same collection, different products
- **Tag Similarity**: "Similar styles" - overlapping tags, same product_type
- **Price-band**: "More options around ₹2500" - price ±20%, same category
- **Discount-driven**: "Best deals right now 🔥" - highest discounts first
- **New Arrivals**: "New arrivals ✨" - sorted by created_at/published_at
- **Collection Popularity**: Prioritizes frontpage, bestsellers, trending collections

### 🔍 Universal Page Scraping
- Extracts product name, price, discount, description, images
- Parses JSON-LD structured data
- Reads Open Graph metadata
- Works on WooCommerce, custom stores, and general e-commerce sites

### 💬 Smart Chat Widget
- Dark theme with cyan accents
- Maximize/minimize support
- Dynamic quick actions based on context
- Product cards for visual shopping
- SPA navigation support

---

## Architecture

```
User → Chrome Extension (Content Script)
                ↓
         Page Scraping
                ↓
    Backend (FastAPI @ /demo)
                ↓
    Intent Detection → Shopify API Fetch → LLM Response
                ↓
    Response with Product Cards
```

---

## Shopify API Endpoints Used

| Intent | Endpoint | Data Retrieved |
|--------|----------|----------------|
| Product Search | `/products.json` | All products (paginated) |
| Single Product | `/products/<handle>.json` | Full product details |
| Collections | `/collections.json` | Store collections |
| Collection Products | `/collections/<handle>/products.json` | Products in collection |
| Search | `/search/suggest.json` | Search results |

**Note**: These are public Shopify endpoints, available on most stores by default.

---

## Recommendation Engine

The demo includes a smart recommendation engine that works without any sales data:

### Recommendation Types

| Type | Trigger | Logic | Best For |
|------|---------|-------|----------|
| **Category-based** | "Similar products", "More like this" | Same collection, different product ID, sorted by price | Product pages |
| **Tag Similarity** | "Similar styles" | Overlapping tags (≥2), same product_type | Fashion/lifestyle |
| **Price Band** | "More options around ₹X" | Price ±20%, same category preferred | Budget shoppers |
| **Deals** | "Best deals", "On sale" | Products with compare_at_price, highest discount first | Deal hunters |
| **New Arrivals** | "New arrivals", "Latest" | Sorted by created_at/published_at | Returning customers |
| **Collection Popularity** | Automatic fallback | Prioritizes frontpage, bestsellers, trending | General browsing |

### How It Works

1. **On Product Page**: When user asks for recommendations, fetches all products and filters by:
   - Same product_type (e.g., "Sunglass")
   - Overlapping tags (e.g., ["Men", "Sunglasses"])
   - Similar price range

2. **On Collection Page**: Returns products from the current collection with smart sorting

3. **General Browsing**: Shows deals or new arrivals based on user intent

### Example Queries

```
"Show me similar products"           → Category + Tag recommendations
"More options under ₹2500"           → Price-filtered products
"Best deals right now"               → Discount-sorted products
"What's new?"                        → New arrivals
"Show me trending sunglasses"        → Collection products with popularity proxy
```

---

## Installation

### 1. Backend Setup (Integrated with Fashion Bot)

The demo backend is now integrated into the main `agent_controller.py` at the `/demo` prefix.

```bash
# Navigate to fashion_bot directory
cd fashion_bot

# Install dependencies (if not already done)
pip install httpx openai python-dotenv

# Run the server
python -m fashion_bot.agent_controller
```

The demo endpoints will be available at:
- `http://localhost:8000/demo/health`
- `http://localhost:8000/demo/chat`
- `http://localhost:8000/demo/fetch-products?store_url=<url>`
- `http://localhost:8000/demo/fetch-collections?store_url=<url>`

### 2. Chrome Extension

1. Open Chrome and go to `chrome://extensions/`
2. Enable "Developer mode" (top right toggle)
3. Click "Load unpacked"
4. Select the `demo_plugin/chrome_extension/` folder
5. Click the extension icon and configure:
   - **Backend URL**: `http://localhost:8000/demo` (local) or your deployed URL
   - **API Key**: Optional, for authentication
   - **Enable Widget**: Toggle the chat widget on/off

---

## Usage

### Basic Flow

1. Install the extension
2. Visit any e-commerce website (e.g., `samandmarshall.com`)
3. Click the chat button (bottom right)
4. Ask questions about products!

### Example Questions

**On a product page:**
- "What's the price?"
- "Is this available in size M?"
- "Tell me about this product"
- "I want to order this"

**On any Shopify store:**
- "Show me all products"
- "What are your best sellers?"
- "Any products under ₹1000?"
- "What's your return policy?"
- "Show me the men's collection"

### Intent Detection

The backend automatically detects user intent:

| Intent | Keywords | Action |
|--------|----------|--------|
| `product_search` | show me, find, recommend | Fetch `/products.json` |
| `price_check` | price, cost, discount | Get pricing data |
| `availability` | available, stock, size | Check variant availability |
| `collection` | collection, category, men, women | Fetch collections |
| `policy` | return, shipping, refund | Fetch policy pages |
| `order` | order, buy, add to cart | Mock order flow |

---

## API Reference

### POST `/demo/chat`

Main chat endpoint with Shopify runtime support.

**Request:**
```json
{
  "message": "Show me products under ₹2000",
  "context": {
    "url": "https://store.myshopify.com/products/example",
    "domain": "store.myshopify.com",
    "title": "Example Product",
    "product": {
      "name": "Example Product",
      "price": "₹1,999",
      "description": "..."
    }
  },
  "history": [
    {"role": "user", "content": "Hi"},
    {"role": "assistant", "content": "Hello!"}
  ]
}
```

**Response:**
```json
{
  "reply": "Here are some products under ₹2000...",
  "action": null,
  "metadata": {
    "intent": "product_search",
    "entities": {"max_price": 2000},
    "shopify_enabled": true,
    "products_count": 15
  },
  "products": [
    {
      "id": 123,
      "title": "Product Name",
      "price": "₹1,499",
      "originalPrice": "₹1,999",
      "discount": "25% OFF",
      "image": "https://...",
      "available": true,
      "url": "/products/product-handle"
    }
  ]
}
```

### GET `/demo/fetch-products`

Debug endpoint to test Shopify product fetching.

```
GET /demo/fetch-products?store_url=https://samandmarshall.com&limit=10
```

### GET `/demo/fetch-collections`

Debug endpoint to test Shopify collection fetching.

```
GET /demo/fetch-collections?store_url=https://samandmarshall.com
```

### DELETE `/demo/cache`

Clear the runtime cache.

```
DELETE /demo/cache
```

---

## Performance

### Caching Strategy
- **TTL**: 2 minutes for all Shopify API responses
- **Key Format**: `products:{store_url}`, `product:{store_url}:{handle}`
- **Memory-based**: Simple in-process cache (no Redis required for demo)

### Timeouts
- Shopify API calls: 5 second timeout
- Graceful fallback to page context if API fails

### Best Practices
- Product list limited to 20 items by default
- Only fetches data relevant to detected intent
- Lazy loading of policies (only when asked)

---

## Demo Script for Clients

### Opening Pitch
> "Our AI shopping assistant works on any Shopify store in real-time — no integration, no data upload, no setup."

### Live Demo Steps

1. **Install Extension** (30 sec)
   - Show Chrome extension installation
   - Point to backend URL

2. **Visit Client's Store** (1 min)
   - Navigate to their Shopify store
   - Show the chat widget appearing automatically

3. **Product Questions** (2 min)
   - "Show me your products"
   - "What's this product's price?"
   - "Is size M available?"
   - Show product cards appearing

4. **Smart Features** (1 min)
   - "Any discounts?"
   - "What's your return policy?"
   - "Order this product" (show mock order)

5. **Close** (30 sec)
   > "All of this works out of the box. For deeper integration, we can customize the responses and add actual order placement."

---

## Troubleshooting

### "Could not fetch products"
- Store might block API access (rare)
- Store might use non-standard Shopify setup
- Check if store is actually on Shopify

### "LLM fallback responses"
- Set `OPENAI_API_KEY` in `.env`
- Check API key is valid

### Widget not appearing
- Check if extension is enabled
- Verify "Enable Widget" is on in popup
- Check console for errors

### Slow responses
- Shopify API might be slow
- Check network tab for bottlenecks
- Clear cache: `DELETE /demo/cache`

---

## Environment Variables

```env
# Required for intelligent responses
OPENAI_API_KEY=sk-...

# Optional
LANGSMITH_API_KEY=...
LANGSMITH_PROJECT=demo-chat
```

---

## File Structure

```
demo_plugin/
├── chrome_extension/
│   ├── manifest.json      # Extension manifest (v3)
│   ├── content.js         # Page scraper + chat widget
│   ├── widget.css         # Chat widget styles
│   ├── popup.html         # Settings popup
│   ├── popup.js           # Popup logic
│   ├── background.js      # Service worker
│   └── icons/             # Extension icons
│
└── README.md              # This file

fashion_bot/
├── demo_chat_router.py    # Backend router (integrated)
└── agent_controller.py    # Main app (includes demo router)
```

---

## What This Proves

✅ **Real-time commerce AI** - No pre-indexing needed  
✅ **Zero manual setup** - Works instantly on Shopify  
✅ **Production-safe** - Read-only API access  
✅ **Extensible** - Easy to add custom features  

---

## Next Steps After Demo

1. **Custom Branding**: Match client's brand colors
2. **Order Integration**: Connect to actual checkout
3. **Analytics**: Track popular questions
4. **Training**: Fine-tune responses for specific products
5. **Multi-channel**: Add WhatsApp, Instagram support

---

## License

Internal use only - Demo purposes.
