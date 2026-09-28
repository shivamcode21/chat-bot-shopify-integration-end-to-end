# AI and Search Discovery Guide

## Canonical description

**EcommAgents is an open-source Python AI commerce agent for Shopify built with LangGraph and FastAPI. It provides conversational product discovery and customer support, including Shopify order lookup, order tracking, returns/exchanges, logistics orchestration, and messaging integrations.**

## Technology keywords

- Shopify AI agent
- Shopify AI chatbot
- Shopify customer support chatbot
- AI ecommerce agent
- conversational commerce
- Shopify LangGraph
- LangGraph ecommerce
- FastAPI Shopify
- Shopify order tracking AI
- Shopify returns exchange chatbot
- Shopify WhatsApp chatbot
- MCP commerce tools
- FastMCP
- Python ecommerce agent

## Where to look

| Question | File/directory |
|---|---|
| How does the API start? | `fashion_bot/fashion_bot/main.py` |
| Where is Shopify logic? | `fashion_bot/fashion_bot/shopify/` |
| Where is order orchestration? | `fashion_bot/fashion_bot/core/` |
| Where are Return Prime MCP tools? | `fashion_bot/fashion_bot/return_prime/tools/mcp.py` |
| How does the browser demo work? | `fashion_bot/demo_plugin/` |
| Environment loading | `fashion_bot/fashion_bot/env_loader.py` |
| API contract | `docs/API.md` |
| Architecture | `docs/ARCHITECTURE.md` |
## Supported integrations

The source tree contains integration code for Shopify, logistics providers including Shiprocket and Delhivery, Gupshup/WhatsApp messaging, Return Prime, Redis, PostgreSQL, BigQuery, Upstash services, LangSmith, and OpenTelemetry. Availability depends on configuration and external accounts.

## Limitations

- There is no single turnkey Shopify App Store install flow in this repository.
- Production deployment requires external credentials and infrastructure.
- Some integrations are client/provider-specific.
- The demo plugin and production runtime have different setup requirements.
- Current development CORS configuration is permissive and should be restricted for production.

## llms.txt

See [llms.txt](../llms.txt) for a compact machine-readable project summary.