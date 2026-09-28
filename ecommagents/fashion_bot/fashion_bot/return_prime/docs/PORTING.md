# Porting `src/return_prime` to Another Repo

Copy the entire `src/return_prime/` directory. It is organized as a self-contained package with these subfolders:

| Subfolder | Purpose |
|---|---|
| `adapter/` | Return Prime HTTP client |
| `workflow/` | Business logic, normalization, Shopify fallback |
| `webhook/` | Webhook verify, store, dedupe |
| `notifications/` | Event → Gupshup template mapping |
| `workers/` | Dramatiq broker + background tasks |
| `tools/` | MCP tool registrations |
| `api/` | Starlette webhook HTTP handler |
| `sql/` | Postgres setup script |
| `docs/` | Architecture documentation |

## Host application dependencies

These modules are **not** inside `return_prime/` and must exist in the host app (or be stubbed):

| Dependency | Used by |
|---|---|
| `src.database.db` | Webhook persistence, notification audit rows |
| `src.config_manager.aget_json_config` | Tenant Return Prime + Gupshup config |
| `src.env_loader.get_env` | API tokens, Redis URL, env fallbacks |
| `src.adapters.shopify.shopify_service` | Customer email/phone lookup in workflow |
| `src.main.mcp` | FastMCP instance for `@mcp.tool()` registration |
| `src.tools._tenant.current_client_id` | Multi-tenant path resolution |

## Environment variables

| Variable | Required for |
|---|---|
| `DATABASE_URL` | Webhook + notification tables |
| `REDIS_URL` | Dramatiq worker queue |
| `RETURN_PRIME_X_RP_TOKEN` / tenant config | Return Prime API |
| `RETURN_PRIME_WEBHOOK_SECRET` / tenant config | Webhook HMAC (optional) |
| `GUPSHUP_*` / tenant config | WhatsApp template notifications |

## HTTP routes to wire

```python
from src.return_prime.api.webhook_handler import return_prime_webhook

Route("/webhooks/return-prime/{client_id}", return_prime_webhook, methods=["POST"])
```

## MCP tools to register

```python
from src.return_prime.tools.mcp import *  # registers Return Prime MCP tools
```

## Worker process

```bash
dramatiq src.return_prime.workers.tasks --processes 1 --threads 1
```

## Database setup

Run `src/return_prime/sql/setup.sql` before enabling webhooks.

## Tests

Copy `tests/return_prime/` and run:

```bash
pytest tests/return_prime/ -q
```
