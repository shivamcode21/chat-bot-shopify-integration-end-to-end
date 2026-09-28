# EcommAgents — Open-Source AI Commerce Agent for Shopify

> **A ready-to-run AI commerce and customer-support agent for Shopify. Clone it, configure it, run it, and adapt it to your own use case.**

[![Shopify](https://img.shields.io/badge/Shopify-AI%20Agent-green)](#) [![Python](https://img.shields.io/badge/Python-3.12-blue)](#) [![FastAPI](https://img.shields.io/badge/FastAPI-API-009688)](#) [![LangGraph](https://img.shields.io/badge/LangGraph-Agent-orange)](#)

EcommAgents connects **Shopify + LLMs + LangGraph + FastAPI + commerce tools** to provide an AI agent that can help shoppers and customers with product discovery, recommendations, orders, tracking, returns, exchanges, and conversational support.

**You do not need to build the agent from scratch. It is already here.**

---

## 🚀 What can you use it for?

Use the existing agent for your own Shopify or e-commerce use case:

- 🛍️ **AI shopping assistant**
- 🤖 **Shopify customer support chatbot**
- 🔎 **Product search and recommendations**
- 📦 **Order lookup and tracking**
- ↩️ **Returns and exchanges**
- 💬 **Website conversational AI**
- 📱 **WhatsApp commerce/support**
- 🚚 **Logistics-aware customer support**
- 🧠 **LangGraph agent workflows**
- 🔧 **Tool/MCP-powered commerce actions**

The project can be adapted for fashion, electronics, beauty, D2C, home goods, or other Shopify businesses.

---

# ⚡ Quick Start

## 1. Clone the repository

```bash
git clone https://github.com/shivamcode21/chat-bot-shopify-integration-end-to-end.git
cd chat-bot-shopify-integration-end-to-end
```

The main application is inside:

```text
ecommagents/fashion_bot/
```

---

## 2. Requirements

Before starting, install:

- **Python 3.12**
- **PostgreSQL**
- An LLM API key, such as OpenAI
- Shopify credentials for Shopify-connected features
- Credentials for any optional integrations you want to use

The repository also includes Docker configuration.

---

## 3. Create a Python environment

```bash
cd ecommagents/fashion_bot

python -m venv .venv
source .venv/bin/activate
```

### Windows

```powershell
python -m venv .venv
.venv\Scripts\activate
```

---

## 4. Install dependencies

```bash
pip install -r requirements.txt
```

---

## 5. Configure environment variables

Create your local environment file:

```bash
cp .env.example .env
```

Then open `.env` and configure the services you want to use.

Typical configuration includes:

```env
DATABASE_URL=postgresql://user:password@localhost:5432/ecommagents

LLM_PROVIDER=openai
OPENAI_API_KEY=your-openai-api-key
OPENAI_MODEL=your-model

SHOPIFY_SHOP_DOMAIN=your-store.myshopify.com
SHOPIFY_ACCESS_TOKEN=your-shopify-access-token
```

Additional integrations are documented in:

- `ecommagents/fashion_bot/.env.example`
- [Vendor configuration guide](ecommagents/fashion_bot/VENDOR_CONFIGURATION_GUIDE.md)
- [API documentation](ecommagents/docs/API.md)

**Never commit real API keys, Shopify access tokens, database credentials, service-account files, or customer data.**

---

## 6. Start the application

From `ecommagents/fashion_bot`:

```bash
python -m fashion_bot.main
```

The API will be available at:

```text
http://localhost:8000
```

Check that the application is running:

```bash
curl http://localhost:8000/health
```

You should receive a healthy response from the API.

---


# 🛍️ Run the demo with your own Shopify store

You can try EcommAgents against a Shopify storefront of your choice.

If the storefront exposes Shopify's public catalog JSON endpoints, start with:

```text
https://your-store.com/products.json
https://your-store.com/collections.json
```

Load the products and collections, normalize the catalog, create embeddings, and push the product documents into a vector/search engine such as **Upstash Vector** (or another provider such as pgvector, Qdrant, Pinecone, or Elasticsearch).

Then connect that search layer to the agent:

```text
Shopify /products.json + /collections.json
              ↓
       Product catalog
              ↓
     Embeddings + metadata
              ↓
       Vector / Search DB
              ↓
       EcommAgents tool
              ↓
      AI shopping assistant
```

For example, index metadata such as:

```text
title
description
product_type
tags
price
variants
product URL
image URL
collection
```

The included demo already has a product-search abstraction and an Upstash search integration. If you prefer another vector/search provider, replace the search implementation while keeping the same product-result contract.

See the [Demo Plugin README](ecommagents/fashion_bot/demo_plugin/README.md) for the complete walkthrough.

> **Note:** `products.json` and `collections.json` are public storefront catalog endpoints. They are useful for demonstrating product discovery, but they do not expose private customer/order data or replace authenticated Shopify Admin APIs.

---

# 🧪 What can you try?

Once the application is running, the main API includes:

| Endpoint | Purpose |
|---|---|
| `GET /health` | Health check |
| `GET /` | Runtime/API information |
| `POST /support-response` | AI product/customer-support request |
| `WS /ws/chat/{client_id}/{session_id}` | Real-time conversational chat |

See the [API documentation](ecommagents/docs/API.md) for request examples.

---

# 🛍️ Demo: AI Shopping Assistant

The repository includes a separate **Chrome extension + FastAPI demo plugin**.

It demonstrates an AI shopping assistant that can:

- Understand the product page a shopper is viewing
- Answer product questions
- Read public Shopify product data
- Find products
- Recommend similar products
- Find products within a price range
- Surface deals and new arrivals
- Display product cards
- Work with Shopify and other e-commerce pages

### Demo location

```text
ecommagents/fashion_bot/demo_plugin/
```

Read the complete [Demo Plugin README](ecommagents/fashion_bot/demo_plugin/README.md) for installation and usage.

---

# 🧩 How it works

```text
                    Shopper
                       │
          ┌────────────┴────────────┐
          ▼                         ▼
      Web Chat                  WhatsApp
          │                         │
          └────────────┬────────────┘
                       ▼
                    FastAPI
                       │
                       ▼
                 LangGraph Agent
                       │
             ┌─────────┼─────────┐
             ▼         ▼         ▼
          Shopify   LLM       Commerce
                       │        Tools
             └─────────┼─────────┘
                       ▼
               AI response/action
```

The main application is under:

```text
ecommagents/fashion_bot/
```

Important areas:

```text
fashion_bot/
├── fashion_bot/
│   ├── main.py              # FastAPI application
│   ├── core/                # Agent and orchestration logic
│   ├── shopify/             # Shopify integrations
│   └── return_prime/        # Returns/MCP tooling
│
├── demo_plugin/             # Browser shopping assistant
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── requirements.txt
```

For a deeper explanation, see [Architecture](ecommagents/docs/ARCHITECTURE.md).

---

# 🐳 Run with Docker

A development Docker setup is included.

### Build the application

```bash
cd ecommagents/fashion_bot

docker build -t ecommagents .
```

### Run it

```bash
docker run --rm -p 8000:8000 --env-file .env ecommagents
```

### Or start the local PostgreSQL + application stack

```bash
docker compose up --build
```

---

# 🔌 Integrations

Depending on your configuration, EcommAgents can work with:

- **Shopify**
- **Shiprocket**
- **Delhivery**
- **Return Prime**
- **WhatsApp / Gupshup**
- **OpenAI and other LLM providers**
- **PostgreSQL**
- **Redis**
- **BigQuery**
- **Upstash**
- **LangSmith**
- **MCP / FastMCP**

Not every integration is required. Configure only the services needed for your use case.

---

# 🛠️ Customize it for your business

The main advantage of this project is that the agent is **already implemented**.

You can start from the existing code and customize:

- System prompts
- Agent behavior
- Shopify configuration
- Product recommendation logic
- Customer-support workflows
- Order workflows
- Return/exchange rules
- Logistics integrations
- WhatsApp behavior
- Custom tools
- UI and chat experience
- LLM provider
- Database and infrastructure

### Example

A fashion store could use it for:

```text
Customer:
"Show me black shirts under ₹2,000"

        ↓

AI Agent

        ↓

Shopify product search

        ↓

Product filtering/recommendation

        ↓

AI response + products
```

An electronics store could adapt the same system for product comparisons, warranty questions, order tracking, and support.

---

# 📚 Documentation

| Document | Description |
|---|---|
| [EcommAgents README](ecommagents/README.md) | Detailed project documentation |
| [Demo Plugin](ecommagents/fashion_bot/demo_plugin/README.md) | Browser AI shopping assistant |
| [API Documentation](ecommagents/docs/API.md) | API endpoints and examples |
| [Architecture](ecommagents/docs/ARCHITECTURE.md) | System architecture |
| [AI Discovery Guide](ecommagents/docs/AI-DISCOVERY.md) | Machine-readable project description |
| [llms.txt](ecommagents/llms.txt) | AI/search context |
| [Contributing](ecommagents/CONTRIBUTING.md) | Contribution guide |
| [Security](ecommagents/SECURITY.md) | Security reporting |

---

# 🤝 Open Source

EcommAgents is being released so developers can **run it, learn from it, fork it, customize it, and build their own commerce use cases on top of it.**

You are encouraged to:

- Fork the repository
- Run the existing agent
- Connect your own Shopify store
- Customize the workflows
- Add tools and integrations
- Improve the project
- Submit pull requests

See [CONTRIBUTING.md](ecommagents/CONTRIBUTING.md).

---

# 🔐 Security

This project can connect to Shopify stores, databases, LLM providers, logistics systems, and customer-facing channels.

Before deploying it publicly:

- Keep secrets server-side.
- Never commit `.env` files containing credentials.
- Never expose Shopify Admin API tokens in browser code.
- Configure CORS for your deployment.
- Add authentication and rate limiting where appropriate.
- Protect customer and order information.
- Review third-party integrations before production use.

If credentials were previously committed to Git history, rotate/revoke them and review the repository history before public distribution.

See [SECURITY.md](ecommagents/SECURITY.md).

---

# 📄 License

See [LICENSE.md](ecommagents/LICENSE.md) for the project license.

---

## 🔎 Search

Shopify AI chatbot · Shopify AI agent · Shopify customer support chatbot · AI ecommerce agent · Shopify LangGraph · Shopify WhatsApp chatbot · Shopify order tracking AI · Shopify returns exchange chatbot · open source Shopify AI agent · FastAPI Shopify · LangGraph ecommerce · MCP Shopify

---

## ⭐ Get Started

**Clone the repo → configure your credentials → run the agent → customize it for your use case.**

If you build something interesting with EcommAgents, contributions and improvements are welcome.
