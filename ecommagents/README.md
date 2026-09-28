# EcommAgents — Open-source AI Agent for Shopify

> **Shopify AI chatbot · Shopify AI agent · AI ecommerce agent · Shopify LangGraph · Shopify order tracking · Shopify returns & exchanges · Shopify WhatsApp chatbot**

EcommAgents is a Python-based AI commerce and customer-support agent built around **Shopify**, **LangGraph**, **FastAPI**, LLMs, and tool-driven workflows. It connects conversational AI with product discovery, order lookup, delivery status, returns/exchanges, and messaging integrations.

**Repository location:** `ecommagents/` in [chat-bot-shopify-integration-end-to-end](https://github.com/shivamcode21/chat-bot-shopify-integration-end-to-end)

## ⭐ What it does

- 🛍️ **Product discovery and recommendations**
- 🧾 **Order support** — order lookup, status, delivery timelines, and order workflows.
- 🚚 **Logistics orchestration** — Shopify-first order handling with pluggable partners such as Shiprocket and Delhivery.
- ↩️ **Returns & exchanges** — Return Prime integrations and customer-facing workflows.
- 💬 **Conversational channels** — WebSocket chat plus WhatsApp/Gupshup webhook integrations.
- 🧠 **LangGraph orchestration** — stateful graph-based agent workflows and checkpoints.
- 🔧 **Tool/MCP integrations** — FastMCP-based tools for selected commerce operations.
- 🧩 **Demo experiences** — a Shopify-aware browser extension and demo chat widget are included.

> **Important:** Some integrations require external accounts, credentials, databases, and partner APIs. This is not a one-command Shopify App Store installation.

## Architecture

~~~mermaid
flowchart LR
    U[Shopper] --> C[Web Chat / WhatsApp / Gupshup]
    C --> F[FastAPI]
    F --> G[LangGraph Agent]
    G --> T[Commerce Tools]
    T --> S[Shopify]
    T --> L[Logistics Partners]
    T --> R[Returns / Exchanges]
    G --> M[LLM Provider]
    G --> D[(State / Checkpoints)]
~~~

The main runtime lives in `fashion_bot/`. The FastAPI entry point is `fashion_bot/fashion_bot/main.py`.

## Quick start

### Prerequisites

- Python 3.12
- PostgreSQL for `DATABASE_URL`
- An LLM provider API key for model-backed conversations
- Shopify and/or partner credentials for the integrations you intend to use

### Local setup

~~~bash
cd ecommagents/fashion_bot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env and set DATABASE_URL plus your LLM/provider credentials.
python -m fashion_bot.main
~~~

The API starts on http://localhost:8000.

| Endpoint | Purpose |
|---|---|
| GET /health | Health check |
| GET / | Runtime endpoint summary |
| POST /support-response | Product/customer-support request |
| WS /ws/chat/{client_id}/{session_id} | WebSocket chat |

Smoke test:

~~~bash
curl http://localhost:8000/health
~~~

### Environment

Start from [`.env.example`](fashion_bot/.env.example). The application requires `DATABASE_URL` at startup. Model, Shopify, messaging, analytics, cache, and partner settings depend on the features you enable.

**Never commit `.env`, API keys, service-account material, production database URLs, or customer data.**

## Docker

A development-oriented container definition is provided in `fashion_bot/Dockerfile`.

~~~bash
cd ecommagents/fashion_bot
docker build -t ecommagents .
docker run --rm -p 8000:8000 --env-file .env ecommagents
~~~

For local PostgreSQL + API:

~~~bash
docker compose up --build
~~~

Review the environment before using Docker with real credentials.

## API documentation

See [API.md](docs/API.md) for request/response examples and [ARCHITECTURE.md](docs/ARCHITECTURE.md) for the runtime architecture.

## Supported commerce capabilities

| Capability | Implementation area |
|---|---|
| Shopify products | `fashion_bot/shopify/modules/product_handlers.py` |
| Shopify orders | `fashion_bot/shopify/modules/order_apis.py` |
| Order tracking | `fashion_bot/shopify/modules/order_tracking_apis.py` |
| Order cancellation/editing | Shopify order modules |
| Returns/exchanges | Shopify return APIs + Return Prime tooling |
| Logistics | Logistics registry + partner adapters |
| Web chat | FastAPI WebSocket routes + static widget |
| WhatsApp / Gupshup | Webhook routers |
| MCP | Return Prime MCP module + FastMCP |

Exact availability depends on configuration, credentials, and the connected Shopify store/partners.

## Demo experience

The [demo plugin](fashion_bot/demo_plugin/README.md) includes a Chrome extension + FastAPI backend for product chat and Shopify runtime data fetching.

After starting the backend, the widget test page is available at `/test`.

## Project map

~~~text
ecommagents/
├── README.md
├── docs/
├── site/
├── llms.txt
└── fashion_bot/
    ├── Dockerfile
    ├── docker-compose.yml
    ├── .env.example
    ├── requirements.txt
    ├── fashion_bot/
    │   ├── main.py
    │   ├── core/
    │   ├── shopify/
    │   └── return_prime/
    └── demo_plugin/
~~~

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Please keep secrets, customer data, production configuration, and generated databases out of commits.

## Security

If you find a credential or sensitive-data exposure, **do not open a public issue with the secret**. See [SECURITY.md](SECURITY.md).

If credentials have ever been committed, rotate/revoke them and rewrite Git history as appropriate before treating this repository as a clean public open-source distribution.

## License

See [LICENSE.md](LICENSE.md) for the intended scoped EcommAgents MIT license.

## Search terms

Shopify AI chatbot · Shopify AI agent · Shopify customer support chatbot · AI ecommerce agent · Shopify LangGraph · Shopify WhatsApp chatbot · Shopify order tracking AI · Shopify returns exchange chatbot · FastAPI Shopify · LangGraph ecommerce · MCP Shopify · conversational commerce