from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import os
from pydantic import BaseModel
from typing import Optional
from langchain_core.messages import HumanMessage
from fashion_bot.state_cache import timestamped_human_message
from fashion_bot.env_loader import bootstrap_environment, require_settings

bootstrap_environment()
require_settings("DATABASE_URL")
from fashion_bot.graph_context_meta import graph

from fashion_bot.product_data import PRODUCT_DATA
from fashion_bot.whatsapp_webhook import router as whatsapp_router
from fashion_bot.gupshup_webhook import router as gupshup_router
from fashion_bot.websocket_chat import websocket_router

app = FastAPI()

# Add CORS middleware for web widget
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify exact origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(whatsapp_router, prefix="/whatsapp")
app.include_router(gupshup_router, prefix="/gupshup")
app.include_router(websocket_router)  # WebSocket chat widget

# Serve static files for chat widget
static_path = os.path.join(os.path.dirname(__file__), "..", "static")
if os.path.exists(static_path):
    app.mount("/static", StaticFiles(directory=static_path), name="static")

@app.get("/test")
async def test_chat_widget():
    """Serve test page for chat widget"""
    test_file = os.path.join(static_path, "test-chat-widget.html")
    if os.path.exists(test_file):
        return FileResponse(test_file)
    return {"error": "Test page not found"}

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "service": "fashion_bot"}

class Query(BaseModel):
    product: str
    question: str
    thread_id: Optional[str] = "user-thread"

@app.post("/support-response")
async def handle(query: Query):
    state = {
        "messages": [timestamped_human_message(query.question)],
        "product_info": query.product or f"Default Product - Sizes: {', '.join(PRODUCT_DATA['sizes_available'])}",
        "phone_number": None,
        "selected_order_id": None,
        "known_orders": None,
        "order_status_by_id": {}
    }
    result = await graph.ainvoke(state, config={"configurable": {"thread_id": query.thread_id}})
    return {"response": result["messages"][-1].content}

@app.get("/debug/load_test_metrics")
async def load_test_metrics():
    """Expose server-side metrics for Locust to poll."""
    from fashion_bot.utils.tiered_cache import get_cache_stats
    from fashion_bot.monitoring.websocket_metrics import get_metrics_collector

    cache = get_cache_stats()
    ws = get_metrics_collector().get_metrics()

    # DB pool stats
    pool_stats = {}
    try:
        from fashion_bot.database_manager import _async_pool
        if _async_pool is not None:
            pool_stats = {
                "db_pool_size": _async_pool.get_stats().get("pool_size", 0) if hasattr(_async_pool, "get_stats") else 0,
                "db_pool_available": _async_pool._pool.qsize() if hasattr(_async_pool, "_pool") else 0,
            }
    except Exception:
        pass

    return {
        "cache": cache,
        "websocket": {
            "active_connections": ws.get("active_connections", 0),
            "total_messages": ws.get("total_messages", 0),
            "total_errors": ws.get("total_errors", 0),
            "messages_per_second": ws.get("messages_per_second", 0),
        },
        "db_pool": pool_stats,
    }


@app.get("/")
def root():
    return {
        "status": "Fashion bot is live",
        "endpoints": {
            "whatsapp": "/whatsapp/webhook",
            "gupshup": "/gupshup/webhook",
            "websocket_chat": "/ws/chat/{client_id}/{session_id}",
            "test_widget": "/test",
            "health": "/health"
        }
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("fashion_bot.main:app", host="0.0.0.0", port=8000, reload=True)
