# Demo Plugin — AI Shopping Assistant for Shopify & E-commerce

> **Run the included browser demo and put an AI shopping assistant on an e-commerce website without building the chat experience from scratch.**

This demo is part of **EcommAgents**, the open-source AI commerce agent for Shopify.

It provides a **Chrome extension + standalone FastAPI backend** that can read the current product/page context, understand shopper questions, fetch public Shopify runtime data, and return product recommendations and product cards.

## 🎯 Use case

The demo answers a simple question:

> **What if a shopper could open an AI assistant directly on an online store and ask about the products they are looking at?**

A shopper can visit a Shopify or other e-commerce site and ask:

- “What is this product?”
- “What is the price?”
- “Is this available in size M?”
- “Show me similar products.”
- “Show me something under ₹2,500.”
- “What are the best deals?”
- “What’s new?”
- “Show me the men's collection.”
- “What is the return policy?”

The extension sends the page context to the backend. For Shopify stores, the backend can also query public Shopify endpoints for current product and collection data.

## ✨ What is included

### Shopify runtime data

The demo can use public Shopify endpoints for:

- Products
- Individual product details
- Collections
- Products inside collections
- Predictive/search suggestions

This means the demo can work with **live store data** without requiring a custom Shopify backend integration for the basic read-only shopping experience.

### Smart product recommendations

The recommendation logic supports:

| Use case | Example | Approach |
|---|---|---|
| Similar products | “Show me similar products” | Product type, tags, collection |
| Price range | “More options around ₹2,500” | Price-band filtering |
| Deals | “What is on sale?” | Compare-at-price / discount data |
| New arrivals | “What’s new?” | Created/published date |
| Collection browsing | “Show me men's products” | Collection data |
| General discovery | “What should I buy?” | Context + available catalog |

The goal is to provide a useful baseline that developers can customize for their own store, catalog, ranking logic, or AI workflow.

### Universal page understanding

On non-Shopify or custom e-commerce sites, the Chrome extension can extract useful page information such as:

- Product name
- Price
- Description
- Images
- Open Graph metadata
- JSON-LD structured data
- Current URL and page title

This lets the demo work as a general **AI product assistant**, while Shopify stores receive additional runtime catalog capabilities.

### Chat widget

The extension includes a browser chat experience with:

- Product-aware conversations
- Product cards
- Quick actions
- Minimize/maximize controls
- SPA navigation support
- Context-aware questions

---

## 🏗️ Architecture

```text
                    Shopper
                       │
                       ▼
              Chrome Extension
                       │
             ┌─────────┴─────────┐
             │                   │
        Page Scraping       Page Context
             │                   │
             └─────────┬─────────┘
                       ▼
               FastAPI Backend
                       │
              ┌────────┴────────┐
              ▼                 ▼
        Intent Detection   Shopify Runtime
              │                 │
              └────────┬────────┘
                       ▼
                 LLM Response
                       │
                       ▼
              Product Cards / Reply
```

The standalone demo backend is:

`backend/demo_chat_backend.py`

The Chrome extension is under:

`chrome_extension/`

---

## 🚀 Run the demo

### Prerequisites

- Python 3.10+
- Google Chrome
- An OpenAI API key for LLM-powered responses
- Internet access to the target store

### 1. Start the backend

From the demo backend directory:

```bash
cd ecommagents/fashion_bot/demo_plugin/backend

pip install -r requirements.txt

export OPENAI_API_KEY="your-key"
uvicorn demo_chat_backend:app --port 8001 --reload
```

On Windows PowerShell:

```powershell
$env:OPENAI_API_KEY="your-key"
uvicorn demo_chat_backend:app --port 8001 --reload
```

The demo backend will run at:

```text
http://localhost:8001
```

The backend source also supports loading a `.env` file from the demo/project locations.

### 2. Install the Chrome extension

1. Open Chrome.
2. Go to `chrome://extensions/`.
3. Enable **Developer mode**.
4. Select **Load unpacked**.
5. Choose:

```text
ecommagents/fashion_bot/demo_plugin/chrome_extension/
```

6. Open the extension settings.
7. Point the backend URL to your running demo backend.
8. Enable the widget.

### 3. Open an e-commerce website

Visit a Shopify store or another supported e-commerce website.

Open the chat widget and try:

```text
What's the price of this product?
```

Then try:

```text
Show me similar products
```

or:

```text
Show me products under ₹2500
```

---

## 🧪 Example shopper flow

### Product page

The shopper is viewing a product.

```text
Shopper
  ↓
"What is this product?"
  ↓
Extension reads page context
  ↓
Backend understands intent
  ↓
AI generates response
```

### Shopify catalog discovery

```text
Shopper
  ↓
"Show me similar sunglasses"
  ↓
Backend detects recommendation intent
  ↓
Fetch Shopify catalog data
  ↓
Filter/rank products
  ↓
AI response + product cards
```

### Price-based discovery

```text
"Show me something under ₹2,000"
  ↓
Detect max-price intent
  ↓
Fetch catalog
  ↓
Filter products
  ↓
Return matching product cards
```

---

## 🔌 API

### POST `/demo/chat`

Main chat endpoint.

Example request:

```json
{
  "message": "Show me products under ₹2000",
  "context": {
    "url": "https://store.example.com/products/example",
    "domain": "store.example.com",
    "title": "Example Product",
    "product": {
      "name": "Example Product",
      "price": "₹1,999",
      "description": "Example description"
    }
  },
  "history": [
    {"role": "user", "content": "Hi"},
    {"role": "assistant", "content": "Hello!"}
  ]
}
```

A typical response contains the assistant reply, detected intent, metadata, and optional product cards.

### GET `/demo/fetch-products`

Fetch Shopify products for a store.

```text
GET /demo/fetch-products?store_url=https://example.com&limit=10
```

### GET `/demo/fetch-collections`

Fetch Shopify collections.

```text
GET /demo/fetch-collections?store_url=https://example.com
```

### DELETE `/demo/cache`

Clear the in-memory runtime cache.

```text
DELETE /demo/cache
```

Check the backend source for the current request/response schemas and available routes.

---


## 🛒 Try the demo with any Shopify store

You can test the product-discovery flow with **any Shopify storefront that exposes its public JSON catalog endpoints**. You do not need to use the original store from this repository.

For a Shopify storefront such as:

```text
https://your-store.com
```

try:

```text
https://your-store.com/products.json
https://your-store.com/collections.json
```

These endpoints can provide public product and collection data that you can use to build a searchable catalog for the demo.

### Recommended demo flow

```text
Shopify storefront
       │
       ├── /products.json
       └── /collections.json
              │
              ▼
       Load product catalog
              │
              ▼
     Clean + normalize products
              │
              ▼
       Create embeddings
              │
              ▼
   Vector / search engine
   (Upstash, pgvector, Qdrant,
    Pinecone, Elasticsearch, etc.)
              │
              ▼
       EcommAgents search
              │
              ▼
       AI shopping assistant
```

### 1. Load products

Example:

```bash
curl "https://your-store.com/products.json?limit=250"
curl "https://your-store.com/collections.json?limit=250"
```

The exact pagination/availability of these public endpoints depends on the storefront and Shopify configuration. Treat the returned catalog as **public storefront data**, not as a replacement for the Shopify Admin API.

### 2. Index the catalog

Convert each product into a searchable document containing useful fields such as:

```json
{
  "id": "shopify-product-id",
  "title": "Product name",
  "description": "Product description",
  "product_type": "Shirts",
  "tags": ["black", "cotton"],
  "variants": [],
  "price": 1999,
  "url": "https://your-store.com/products/product-handle",
  "image_url": "https://cdn.shopify.com/..."
}
```

Create an embedding from the product title, description, product type, tags, and other useful attributes, then store the vector together with the product metadata.

You can use **Upstash Vector** or another vector/search engine. The important part is that your search layer can return the product metadata needed to render product cards.

### 3. Connect the search layer to the agent

The included demo already has a product-search abstraction and its main implementation can use the project's Upstash search integration.

If you use another vector database, keep the same contract:

```text
User query
   ↓
Embedding / semantic search
   ↓
Top matching products
   ↓
Product metadata
   ↓
LLM
   ↓
Product recommendations + answer
```

For another search provider, adapt the product-search service rather than changing the Chrome extension.

### 4. Run the demo

Configure your LLM and search credentials in the environment, start the backend, and load the Chrome extension.

```bash
cd ecommagents/fashion_bot/demo_plugin/backend
pip install -r requirements.txt
uvicorn demo_chat_backend:app --port 8001 --reload
```

Then load:

```text
ecommagents/fashion_bot/demo_plugin/chrome_extension/
```

as an unpacked Chrome extension and point it at:

```text
http://localhost:8001
```

Now open the Shopify storefront and ask questions such as:

```text
Show me black shirts
Show me products under ₹2,000
Find something similar to this product
Show me products from the men's collection
What would you recommend for a party?
```

### Important

The `products.json` and `collections.json` endpoints are useful for a **public-catalog demo**. They do not provide private customer, order, inventory, or Admin API data.

For production Shopify integrations, use the appropriate Shopify APIs and authentication for the data and actions your application needs.

## 🧠 Intent-driven behavior

The demo maps natural-language questions to useful commerce operations.

| Intent | Example | Action |
|---|---|---|
| Product search | “Find sunglasses” | Search/fetch products |
| Price | “How much is this?” | Read product pricing |
| Availability | “Is this available?” | Inspect variants/page context |
| Recommendation | “Show similar products” | Run recommendation logic |
| Collection | “Show women's products” | Fetch collection/catalog data |
| Policy | “What's your return policy?” | Use page/context information |
| Order | “I want to order this” | Demo/mock action flow |

These behaviors are intended as an **example implementation**. Developers can change the intent detection, tools, ranking, prompts, and actions for their own application.

---

## ⚙️ Configuration

The demo uses environment variables for its LLM and optional integrations.

Example:

```env
OPENAI_API_KEY=your-key
LANGSMITH_API_KEY=optional
LANGSMITH_PROJECT=demo-chat
```

Do not commit real API keys or customer/store credentials.

---

## ⚡ Performance

The demo uses lightweight in-process caching for Shopify runtime requests.

Current behavior includes:

- Short TTL caching for Shopify responses
- Product list limits
- Shopify request timeouts
- Graceful fallback to page context when runtime fetching fails
- Lazy policy fetching based on intent

This is a **demo/reference implementation**. For production use, add the authentication, rate limiting, observability, caching, privacy controls, and deployment architecture appropriate for your application.

---

## 🧩 Customize it

The most important reason this demo is included in EcommAgents is that you can **start with working code and change it for your own use case**.

You can customize:

### UI

Edit:

```text
chrome_extension/
├── content.js
├── widget.css
├── popup.html
└── popup.js
```

Use this to change the widget, branding, quick actions, product cards, and page behavior.

### Backend

Edit:

```text
backend/demo_chat_backend.py
```

Use this to change:

- Intent detection
- Shopify fetching
- Recommendation logic
- LLM prompts
- API responses
- Custom business actions

### Recommendations

Replace the baseline ranking logic with your own:

- Vector search
- Semantic product search
- Inventory-aware ranking
- Customer history
- Merchandising rules
- Bestseller data
- Personalized recommendations

### Integrations

You can extend the backend with your own:

- Shopify Admin API
- Product database
- Search engine
- CRM
- Order system
- Inventory service
- Analytics platform
- Custom agent tools

---

## 🔒 Security and privacy

The demo is designed primarily as a read-oriented shopping-assistant example.

Before using it with real customers:

- Add authentication between the extension and backend.
- Restrict CORS to trusted origins.
- Add rate limiting.
- Validate and sanitize incoming page context.
- Avoid sending unnecessary customer information to the LLM.
- Do not expose private Shopify Admin API credentials in the extension.
- Store secrets only on the server.
- Add logging/monitoring appropriate for your deployment.

**Never put Shopify Admin API access tokens or LLM API keys inside the Chrome extension.**

---

## 📁 File structure

```text
demo_plugin/
├── README.md
├── CLIENT_LOCATION.md
│
├── backend/
│   ├── demo_chat_backend.py
│   └── requirements.txt
│
└── chrome_extension/
    ├── manifest.json
    ├── content.js
    ├── widget.css
    ├── popup.html
    ├── popup.js
    ├── background.js
    └── icons/
```

---

## 🌍 Who can use this?

This demo is useful for:

- Shopify store owners
- Shopify agencies
- AI developers
- E-commerce startups
- Product recommendation projects
- Customer-support experiments
- AI shopping-assistant prototypes
- Developers learning Shopify + LLM integrations
- Developers building browser-based commerce assistants

You can use the demo as-is, fork it, or use it as a starting point for your own EcommAgents implementation.

---

## 🔗 Part of EcommAgents

This demo is one component of the larger **EcommAgents open-source AI commerce agent**.

Start here:

```text
ecommagents/
└── fashion_bot/
    ├── fashion_bot/       # Main AI commerce agent
    └── demo_plugin/       # Browser shopping assistant demo
```

For the complete system, see the [EcommAgents README](../../README.md).

## License

See the repository's [LICENSE.md](../../LICENSE.md) for the project license and usage terms.
