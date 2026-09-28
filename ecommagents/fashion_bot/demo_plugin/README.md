# ⚡ Demo Plugin — Complete Installation Guide

> **Run the EcommAgents AI shopping assistant directly in Chrome.**

This demo contains two pieces:

`text
Chrome Extension
      ↓
FastAPI Demo Backend
      ↓
LLM + Shopify/product-search tools
      ↓
AI shopping assistant
`

The extension reads the current product/page context and sends the shopper's question to the backend. For catalog discovery, the backend can search an indexed Shopify product catalog.

---

## 🚀 Install it locally

### Prerequisites

Install:

- Google Chrome
- Python 3.10+
- Git
- An OpenAI API key or another supported LLM configuration
- Internet access

For semantic product discovery, also prepare a searchable product catalog using **Upstash Vector/Search or another vector/search provider**.

---

## Step 1 — Clone the repository

`bash
git clone https://github.com/shivamcode21/chat-bot-shopify-integration-end-to-end.git
cd chat-bot-shopify-integration-end-to-end
`

The demo is here:

`text
ecommagents/fashion_bot/demo_plugin/
`

---

## Step 2 — Start the demo backend

`bash
cd ecommagents/fashion_bot/demo_plugin/backend

python -m venv .venv
`

### macOS / Linux

`bash
source .venv/bin/activate
`

### Windows

`powershell
.venv\Scripts\activate
`

Install dependencies:

`bash
pip install -r requirements.txt
`

Set your LLM key.

### macOS / Linux

`bash
export OPENAI_API_KEY="your-openai-api-key"
`

### Windows PowerShell

`powershell
$env:OPENAI_API_KEY="your-openai-api-key"
`

Start the backend:

`bash
uvicorn demo_chat_backend:app --port 8001 --reload
`

You should see the FastAPI server running on:

`text
http://localhost:8001
`

### Test the backend

Open:

`text
http://localhost:8001/health
`

or run:

`bash
curl http://localhost:8001/health
`

A healthy response means the demo backend is ready.

---

## Step 3 — Install the Chrome extension

Open Google Chrome and navigate to:

`text
chrome://extensions/
`

Then:

1. Turn on **Developer mode**.
2. Click **Load unpacked**.
3. Select this directory:

`text
ecommagents/fashion_bot/demo_plugin/chrome_extension/
`

Chrome will install **Product Chat Demo** as a local extension.

You do not need to publish it to the Chrome Web Store to run the demo.

---

## Step 4 — Configure the extension

Click the **Product Chat Demo** extension icon in Chrome.

Configure:

### Backend URL

`text
http://localhost:8001
`

### Enable Chat Widget

Turn it **ON**.

### Client ID

If you are using the indexed product-search pipeline, enter the Client ID associated with your searchable product catalog.

If you are only testing page-aware chat, you can start without a catalog Client ID.

### OpenAI API Key

Normally keep this empty when the backend has `OPENAI_API_KEY` configured.

The API key should preferably stay on the backend.

Click:

**Save Settings → Test Connection**

You should see:

`text
✓ Backend connected successfully!
`

---

## Step 5 — Open a Shopify website

Open any Shopify storefront you want to test.

For example:

`text
https://your-store.com
`

The extension runs on the page and opens the AI shopping widget.

Try:

`text
What is this product?
`

Then:

`text
What is the price?
`

Then:

`text
Tell me about this product
`

The extension extracts page information such as the product name, price, description, images, structured data, URL, and other available context.

---

# 🛍️ Step 6 — Enable product discovery with a Shopify catalog

For the full AI shopping experience, index the Shopify store's product catalog.

Many Shopify storefronts expose public catalog endpoints such as:

`text
https://your-store.com/products.json
https://your-store.com/collections.json
`

Try them in your browser first.

You can also fetch them:

`bash
curl "https://your-store.com/products.json?limit=250"
curl "https://your-store.com/collections.json?limit=250"
`

These provide public storefront catalog information when the storefront makes those endpoints available.

---

## Step 7 — Create your searchable product catalog

Use the Shopify catalog as the source:

`text
/products.json
/collections.json
       ↓
Normalize products
       ↓
Create searchable product documents
       ↓
Generate embeddings
       ↓
Upstash Vector/Search
       ↓
EcommAgents product-search tool
`

A useful product document can contain:

`json
{
  "id": "shopify-product-id",
  "title": "Black Cotton Shirt",
  "description": "Regular-fit cotton shirt...",
  "product_type": "Shirts",
  "tags": ["black", "cotton", "casual"],
  "price": 1999,
  "variants": [],
  "url": "https://your-store.com/products/black-cotton-shirt",
  "image_url": "https://cdn.shopify.com/..."
}
`

Embed useful searchable text, for example:

`text
Black Cotton Shirt
Regular-fit cotton shirt
Shirts
black
cotton
casual
`

Store the embedding together with the product metadata.

---

# 🔎 Step 8 — Use Upstash or another vector/search engine

The demo's product-search integration is designed around the project's search pipeline.

You can use:

- **Upstash Vector**
- Upstash Search
- pgvector
- Qdrant
- Pinecone
- Elasticsearch
- Another vector/semantic search system

The important contract is:

`text
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
Product recommendations
`

If you use a different vector database, adapt the search implementation while keeping the product result structure expected by the agent.

---

# 🧪 Step 9 — Test product search

Once the catalog is indexed and the correct Client ID/search configuration is available, try:

`text
Show me black shirts
`

`text
Show me products under ₹2,000
`

`text
Find something similar to this product
`

`text
Show me men's products
`

`text
What would you recommend for a party?
`

The backend can search the catalog and return product data that the extension renders as product recommendations/cards.

---

## 🔄 Complete demo flow

`text
             Shopify Store
                   │
        /products.json
        /collections.json
                   │
                   ▼
          Product ingestion
                   │
                   ▼
        Embeddings + metadata
                   │
                   ▼
       Upstash Vector / Search
                   │
                   ▼
Chrome Extension ──► FastAPI
                   │
                   ▼
             LLM logic
                   │
                   ▼
        Product search results
                   │
                   ▼
          Shopper conversation
`

---

# 🧰 Demo backend endpoints

The standalone backend exposes:

| Endpoint | Purpose |
|---|---|
| `GET /` | Backend information |
| `GET /health` | Health check |
| `POST /chat` | Chat with page context |
| `POST /scrape-context` | Process page context |

The source is:

`text
ecommagents/fashion_bot/demo_plugin/backend/demo_chat_backend.py
`

---

# 🔐 Important security note

The Chrome extension is a **demo/reference implementation**.

Do not put production secrets in the extension.

Do not expose:

- Shopify Admin API tokens
- Private database credentials
- Production API keys
- Customer/order data credentials

Prefer:

`text
Chrome Extension
      ↓
Your authenticated backend
      ↓
Private APIs / Shopify Admin API / Vector DB
`

For production, add authentication, rate limiting, restricted CORS, logging, privacy controls, and appropriate Shopify API permissions.

---

# 🛠️ Customize the demo

The main files are:

`text
demo_plugin/
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
    └── background.js
`

Change the Chrome UI in:

`text
chrome_extension/
`

Change the AI/backend behavior in:

`text
backend/demo_chat_backend.py
`

You can replace the recommendation/search layer with your own vector database, search API, recommendation model, or business logic.

---

# ❓ Troubleshooting

### Extension says it cannot connect

Make sure the backend is running:

`bash
uvicorn demo_chat_backend:app --port 8001 --reload
`

Then test:

`text
http://localhost:8001/health
`

Make sure the extension's **Backend URL** is:

`text
http://localhost:8001
`

### Page chat works but product search returns nothing

The page-aware chat and catalog search are separate capabilities.

For semantic product discovery, verify:

1. Products were loaded from the Shopify catalog.
2. Products were indexed.
3. Embeddings were created.
4. The vector/search service is configured.
5. The correct Client ID is configured.
6. The backend can access the search service.

### Shopify products.json does not work

Not every storefront will expose the same public endpoints or catalog size. Treat these endpoints as a convenient demo ingestion method, not as a guaranteed Shopify API contract.

For production applications, use Shopify's appropriate authenticated APIs.

---

## 🎯 What this demo is for

This demo is designed so developers can go from:

`text
"I have a Shopify store"
        ↓
"I have a product catalog"
        ↓
"I want an AI shopping assistant"
        ↓
"Run EcommAgents"
        ↓
"Index my products"
        ↓
"Open the Chrome extension"
        ↓
"Start talking to my store"
`

You can then customize the agent, search, UI, tools, and business logic for your own use case.

[← Back to EcommAgents](../../README.md)
