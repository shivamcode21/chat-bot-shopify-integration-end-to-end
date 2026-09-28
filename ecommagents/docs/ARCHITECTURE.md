# EcommAgents Architecture

## Runtime layers

~~~mermaid
flowchart TB
    Channels[Web widget / WhatsApp / Gupshup]
    API[FastAPI application]
    Graph[LangGraph stateful agent]
    Tools[Commerce tools]
    Shopify[Shopify services]
    Logistics[Logistics registry + partners]
    Returns[Return Prime]
    LLM[LLM provider]
    State[(PostgreSQL / checkpoints / caches)]

    Channels --> API
    API --> Graph
    Graph --> Tools
    Tools --> Shopify
    Tools --> Logistics
    Tools --> Returns
    Graph --> LLM
    Graph --> State
~~~

## Entry point

`fashion_bot/fashion_bot/main.py` creates the FastAPI application and registers WhatsApp, Gupshup, WebSocket, static widget, health, and support routes.

## Agent orchestration

The runtime uses LangGraph and LangChain packages. Agent state is passed through graph execution, while service factories and registries keep Shopify and logistics integrations behind application-level interfaces.

The order-status flow is Shopify-first: fetch the primary order, return Shopify data for new or terminal orders, resolve connected logistics partners for fulfilled orders, enrich when a partner returns valid data, and fall back to Shopify data when no partner is available.

## Shopify integration

Shopify-specific code is organized under `fashion_bot/shopify/`, including product handlers, order APIs, order creation/editing/cancellation, order tracking, webhooks, formatting, and enrichment.

## MCP/tool layer

The project includes FastMCP and a Return Prime MCP tool module. MCP tools are an integration boundary: validate inputs, keep credentials server-side, and return bounded customer-facing data rather than raw internal records.

## Messaging

WhatsApp and Gupshup webhook modules translate external events into the internal conversation runtime. The browser demo is an additional integration surface.

## Data and infrastructure

The application can integrate with PostgreSQL, Redis, BigQuery, Upstash services, LangSmith, OpenTelemetry, and logistics/returns providers. Some settings are required at application startup.

## Security boundaries

- Credentials belong in environment variables or a deployment secret manager.
- Customer/order data should not be committed to Git.
- Debug and load-test endpoints should not be exposed publicly without controls.
- Production CORS should be restricted to known origins.