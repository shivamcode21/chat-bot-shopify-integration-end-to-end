import logging
import os
import sys
import asyncio
import json
# OpenTelemetry Imports
from opentelemetry import trace, metrics, _logs
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor

# Configure logging — trace_id injected by TraceIdFilter via %(trace_id)s
# Use force=True to ensure this configuration takes effect even if logging was configured elsewhere
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - [%(trace_id)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    force=True  # Override any existing configuration
)
from fashion_bot.trace_context import install_trace_filter
install_trace_filter()

# Environment check - send telemetry to Grafana in production or when explicitly enabled
ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()
IS_PRODUCTION = ENVIRONMENT in ["production", "prod"]
# Allow local testing of OTel metrics with ENABLE_OTEL_LOCAL=true
ENABLE_OTEL_LOCAL = os.getenv("ENABLE_OTEL_LOCAL", "false").lower() == "true"
ENABLE_OTEL = IS_PRODUCTION or ENABLE_OTEL_LOCAL

# Setup OpenTelemetry Resources
OTEL_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "fashion-bot-agent")
resource = Resource.create({
    "service.name": OTEL_SERVICE_NAME,
    "service.instance.id": os.uname().nodename,
    "deployment.environment": ENVIRONMENT,
})

# Setup Tracing (only in production)
if IS_PRODUCTION:
    tracer_provider = TracerProvider(resource=resource)
    otlp_trace_exporter = OTLPSpanExporter()
    tracer_provider.add_span_processor(BatchSpanProcessor(otlp_trace_exporter))
    trace.set_tracer_provider(tracer_provider)
    logging.info(f"OpenTelemetry Tracing ENABLED (environment: {ENVIRONMENT})")
else:
    logging.info(f"OpenTelemetry Tracing DISABLED (environment: {ENVIRONMENT})")

# Setup Metrics (in production or when explicitly enabled for local testing)
if ENABLE_OTEL:
    metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)
    logging.info(f"OpenTelemetry Metrics ENABLED (environment: {ENVIRONMENT})")
else:
    logging.info(f"OpenTelemetry Metrics DISABLED (environment: {ENVIRONMENT})")
    logging.info(f"To enable locally: export ENABLE_OTEL_LOCAL=true")

# Setup Logging to Grafana (only in production)
# Store reference for proper shutdown
_otel_logger_provider = None

if IS_PRODUCTION:
    _otel_logger_provider = LoggerProvider(resource=resource)
    _logs.set_logger_provider(_otel_logger_provider)
    otlp_log_exporter = OTLPLogExporter()
    _otel_logger_provider.add_log_record_processor(BatchLogRecordProcessor(otlp_log_exporter))

    # Attach OTLP Logging Handler to root logger
    handler = LoggingHandler(level=logging.INFO, logger_provider=_otel_logger_provider)
    logging.getLogger().addHandler(handler)

    # The OTLP HTTP exporter occasionally fails with RemoteDisconnected when
    # Grafana Cloud closes a keep-alive connection. It already retries the
    # batch internally, so the transient ERROR it logs is purely noise —
    # exporting it back to Grafana on the next batch creates a feedback loop.
    # Cap that logger at CRITICAL so transient export hiccups don't pollute
    # the very pipeline they're failing to reach.
    logging.getLogger("opentelemetry.sdk._logs._internal.export").setLevel(logging.CRITICAL)
    logging.getLogger("opentelemetry.exporter.otlp.proto.http._log_exporter").setLevel(logging.CRITICAL)

    # Instrument standard logging to include trace_id and span_id in logs
    LoggingInstrumentor().instrument(set_logging_format=True)

    # Re-install trace filter on all handlers (including the newly added OTLP handler)
    # This ensures %(trace_id)s is available in log records sent to Grafana
    install_trace_filter()

    logging.info(f"OpenTelemetry Logs to Grafana ENABLED (environment: {ENVIRONMENT})")
else:
    logging.info(f"OpenTelemetry Logs to Grafana DISABLED (environment: {ENVIRONMENT}) - logs stay local only")

# --- Traceloop / OpenLLMetry auto-instrumentation for LLM calls ---
# Reuses the global TracerProvider + MeterProvider configured above so the
# standard `gen_ai.*` spans/metrics flow through the existing OTLP pipeline.
# Per-request `client_id` labels are attached via Traceloop association
# properties — see fashion_bot.monitoring.otel_metrics.set_request_client_id.
if ENABLE_OTEL:
    try:
        from traceloop.sdk import Traceloop
        Traceloop.init(
            app_name=OTEL_SERVICE_NAME,
            disable_batch=False,
            should_enrich_metrics=True,
        )
        logging.info("Traceloop / OpenLLMetry instrumentation ENABLED")
    except Exception as _exc:
        logging.warning(f"Traceloop init failed; falling back to manual instrumentors: {_exc}")
        for _name, _import_path in (
            ("OpenAI", "opentelemetry.instrumentation.openai:OpenAIInstrumentor"),
            ("LangChain", "opentelemetry.instrumentation.langchain:LangchainInstrumentor"),
        ):
            try:
                _module, _cls = _import_path.split(":")
                _Instrumentor = getattr(__import__(_module, fromlist=[_cls]), _cls)
                _Instrumentor().instrument()
                logging.info(f"{_name} auto-instrumentation ENABLED (fallback)")
            except Exception as _inner:
                logging.warning(f"{_name} instrumentation unavailable: {_inner}")

# Ensure all relevant loggers are set to INFO level
for logger_name in ['context_graph', 'meta_graph', 'meta_nodes', 'fashion_bot', 'generic_skill_node']:
    logging.getLogger(logger_name).setLevel(logging.INFO)


from fastapi import BackgroundTasks, FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

from fashion_bot.security.cors_settings import get_cors_allowed_origins
from fashion_bot.security.max_body_middleware import MaxBodySizeMiddleware
from fashion_bot.security.rate_limit_middleware import SlidingWindowRateLimitMiddleware

# Widget CDN support
from fashion_bot.widget_config import widget_router
from fashion_bot.cache_static import CachedStaticFiles
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel
from typing import Dict, Any, List, Optional
from fashion_bot.schema import SupportState
from fashion_bot.state_cache import timestamped_human_message
from fashion_bot.gupshup_webhook import router as gupshup_router
from fashion_bot.gupshup_events_webhook import router as gupshup_events_router
from fashion_bot.dashboard import router as dashboard_router
from fashion_bot.shopify.webhook.shopify_webhook import router as shopify_webhook_router
from fashion_bot.shopify.webhook.templates_controller import router as templates_router
from fashion_bot.shopify.webhook.abandoned_checkout_webhook import router as abandoned_checkout_router
from fashion_bot.shopify.webhook.product_webhook import router as product_webhook_router  # Product sync webhooks
from fashion_bot.shiprocket.webhook.shiprocket_webhook import (
    abandoncart_router,
    router as shiprocket_webhook_router,
)
from fashion_bot.delhivery.webhook import delhivery_webhook_router
from fashion_bot.return_prime.webhook.router import router as return_prime_webhook_router
from fashion_bot.monitoring.websocket_metrics import get_metrics_collector
from fashion_bot.cron_jobs.scheduler import start_cron_scheduler, stop_cron_scheduler, get_job_status
from fashion_bot.websocket_chat import websocket_router  # Web chat widget
from fashion_bot.demo_chat_router import router as demo_router  # Demo plugin for client demos
from fashion_bot.attribution_router import router as attribution_router, ensure_tables as ensure_attribution_tables  # Attribution tracking API
from fashion_bot.attribution_verify_router import router as attribution_verify_router  # Attribution verification (internal, LLM second-opinion)
from fashion_bot.database_manager import awith_retry, get_async_postgres_connection
from fashion_bot.utils.http_client import close_shared_async_http_client, get_shared_async_http_client
from fashion_bot.utils.client_id_utils import encode_client_id

# Disable LangSmith warnings to keep logs clean
logging.getLogger("langsmith").setLevel(logging.ERROR)
logging.getLogger("langsmith.client").setLevel(logging.ERROR)

app = FastAPI(title="Fashion Bot Agent Controller", description="Clean API for Fashion Bot with WhatsApp Integration")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int, min_value: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
        return value if value >= min_value else default
    except Exception:
        return default


# Instrument FastAPI with OpenTelemetry (only in production).
# `webhook_request_hook` enriches the auto-generated server span with
# webhook.type / webhook.topic so collector-side span-to-metrics processors
# can derive per-webhook RED metrics without per-handler decorators.
if IS_PRODUCTION:
    from fashion_bot.monitoring.otel_metrics import webhook_request_hook
    FastAPIInstrumentor.instrument_app(app, server_request_hook=webhook_request_hook)

# Webhook RED metrics with business dimensions (Shopify store / Shiprocket
# client_id). Added first so it sits innermost — closest to the route handler —
# giving handler-accurate latency and final status. No-op for non-webhook paths.
if ENABLE_OTEL:
    from fashion_bot.monitoring.otel_metrics import WebhookMetricsMiddleware
    app.add_middleware(WebhookMetricsMiddleware)

# CORS: set CORS_ALLOWED_ORIGINS (comma-separated) in production; legacy fallback is "*"
_cors_origins = get_cors_allowed_origins()
_cors_credentials = True
if not _cors_origins:
    _cors_origins = ["*"]
    _cors_credentials = False  # browsers forbid credentials with wildcard origin

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Outermost runs first: body size, then rate limit, then CORS/app
app.add_middleware(SlidingWindowRateLimitMiddleware)
app.add_middleware(MaxBodySizeMiddleware)

# Startup event: Start cron scheduler
@app.on_event("startup")
async def startup_event():
    """Initialize services on application startup."""
    for uvi_logger in ["uvicorn", "uvicorn.access", "uvicorn.error"]:
        ulog = logging.getLogger(uvi_logger)
        ulog.handlers.clear()
        ulog.setLevel(logging.INFO)
        ulog.propagate = True

    logging.info("[STARTUP] Starting Fashion Bot Agent Controller...")

    # Ensure attribution tables/indexes (incl. schema migrations) exist
    # before the app starts serving traffic - store_event/store_events_batch
    # rely on the uq_attribution_session_started partial unique index for
    # their ON CONFLICT clause, so this can't be a manual, easily-forgotten
    # post-deploy step.
    try:
        ensure_attribution_tables()
    except Exception as e:
        logging.error(f"[STARTUP] ❌ Failed to ensure attribution tables: {e}")

    # Start cron scheduler for background jobs
    # Guard: with multiple UVicorn workers, default to disabling cron to avoid duplicate jobs.
    worker_count = _env_int("UVICORN_WORKERS", 1, min_value=1)
    enable_cron = _env_bool("ENABLE_CRON_SCHEDULER", default=(worker_count == 1))
    if enable_cron:
        try:
            await start_cron_scheduler()
            logging.info("[STARTUP] ✅ Cron scheduler started successfully")
        except Exception as e:
            logging.error(f"[STARTUP] ❌ Failed to start cron scheduler: {e}")
    else:
        logging.info(
            f"[STARTUP] ⏭️ Cron scheduler disabled (ENABLE_CRON_SCHEDULER=false or UVICORN_WORKERS={worker_count})"
        )

# Shutdown event: Stop cron scheduler
@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on application shutdown."""
    logging.info("[SHUTDOWN] Stopping Fashion Bot Agent Controller...")
    
    # Stop cron scheduler
    worker_count = _env_int("UVICORN_WORKERS", 1, min_value=1)
    enable_cron = _env_bool("ENABLE_CRON_SCHEDULER", default=(worker_count == 1))
    if enable_cron:
        try:
            await stop_cron_scheduler()
            logging.info("[SHUTDOWN] ✅ Cron scheduler stopped successfully")
        except Exception as e:
            logging.error(f"[SHUTDOWN] ❌ Error stopping cron scheduler: {e}")
    else:
        logging.info("[SHUTDOWN] ⏭️ Cron scheduler stop skipped (not enabled in this process)")

    try:
        await close_shared_async_http_client()
        logging.info("[SHUTDOWN] ✅ Shared async HTTP client closed")
    except Exception as e:
        logging.error(f"[SHUTDOWN] ❌ Error closing shared async HTTP client: {e}")

    # Flush and shutdown OpenTelemetry logger provider to ensure all logs are sent
    if _otel_logger_provider is not None:
        try:
            _otel_logger_provider.force_flush()
            _otel_logger_provider.shutdown()
            logging.info("[SHUTDOWN] ✅ OpenTelemetry logger provider shut down")
        except Exception as e:
            logging.error(f"[SHUTDOWN] ❌ Error shutting down OpenTelemetry logger: {e}")



# Pydantic models
class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"  # Optional session ID for state management


class ChatResponse(BaseModel):
    response: str
    session_id: str


class ExecuteChatRequest(BaseModel):
    """Request model for execute-chat endpoint."""
    message: str
    phone_number: Optional[str] = None  # Optional phone number for context
    client_id: Optional[str] = None  # Optional client ID for context
    conversation_history: list = []  # Optional (not used currently)


class ExecuteChatResponse(BaseModel):
    """Response model for execute-chat endpoint."""
    response: str
    success: bool
    error: Optional[str] = None
    tool_call_trace: Optional[List[Dict[str, Any]]] = None


class WidgetScriptRequest(BaseModel):
    """Request model for generating a self-serve widget embed snippet."""
    clientId: str
    clientName: Optional[str] = None
    position: Optional[str] = None
    theme: Optional[str] = None
    baseUrl: Optional[str] = None


def _normalize_widget_base_url(base_url: Optional[str]) -> str:
    return str(base_url or "").strip().rstrip("/")


def _resolve_widget_embed_base_url(request: Request, requested_base_url: Optional[str] = None) -> str:
    configured_base_url = (
        requested_base_url
        or os.getenv("WIDGET_EMBED_BASE_URL")
        or os.getenv("WIDGET_API_ORIGIN")
        or ""
    )
    base_url = _normalize_widget_base_url(configured_base_url)
    if base_url:
        return base_url
    return str(request.base_url).strip().rstrip("/")


def _build_widget_script_payload(
    *,
    raw_client_id: str,
    client_name: Optional[str],
    position: Optional[str],
    theme: Optional[str],
    base_url: str,
) -> Dict[str, Any]:
    client_id = str(raw_client_id or "").strip()
    if not client_id:
        raise ValueError("clientId is required")

    encoded_client_id = encode_client_id(client_id)
    script_url = f"{base_url}/static/chat-widget.js"
    config: Dict[str, Any] = {"clientId": encoded_client_id}
    if client_name:
        config["clientName"] = str(client_name)
    if position:
        config["position"] = str(position)
    if theme:
        config["theme"] = str(theme)

    pretty_config = json.dumps(config, ensure_ascii=False, indent=2)
    compact_config = json.dumps(config, ensure_ascii=False, separators=(",", ":"))
    script = (
        "<script>\n"
        f"  window.FashionBotWidgetConfig = {pretty_config};\n"
        "</script>\n"
        f'<script src="{script_url}" async defer></script>'
    )
    one_liner = (
        f"<script>window.FashionBotWidgetConfig={compact_config};</script>\n"
        f'<script src="{script_url}" async defer></script>'
    )

    return {
        "success": True,
        "clientId": client_id,
        "encodedClientId": encoded_client_id,
        "scriptUrl": script_url,
        "config": config,
        "script": script,
        "oneLiner": one_liner,
    }


# Global state storage for maintaining conversation context
user_sessions = {}

# Global state storage for execute-chat endpoint (like Streamlit session_state)
execute_chat_sessions = {}


def create_initial_state() -> SupportState:
    """Create initial state for the conversation."""
    return SupportState(
        messages=[],
        product_info="Premium cotton t-shirt with eco-friendly printing",
        phone_number=None,
        selected_order_id=None,
        known_orders=None,
        order_status_by_id=None,
        is_order_query=None,
        is_frustrated=None,
        needs_escalation=None,
        needs_human_agent=None,
        scratchpad=None
    )


def get_or_create_session(session_id: str) -> SupportState:
    """Get existing session or create new one."""
    if session_id not in user_sessions:
        user_sessions[session_id] = create_initial_state()
    return user_sessions[session_id]


def get_or_create_execute_chat_session(phone_number: str, client_id: str) -> dict:
    """Get or create session for execute-chat endpoint (like Streamlit session_state)."""
    session_key = f"{phone_number}_{client_id}" if phone_number and client_id else (phone_number or client_id or "default")
    
    if session_key not in execute_chat_sessions:
        # Initialize state like Streamlit does
        execute_chat_sessions[session_key] = {
            "messages": [],
            "product_info": "",
            "phone_number": phone_number,
            "client_id": client_id,
            "selected_order_id": None,
            "known_orders": None,
            "order_status_by_id": None,
            "is_order_query": None,
            "is_frustrated": None,
            "needs_escalation": None,
            "needs_human_agent": None,
            "scratchpad": None
        }
        logging.info(f"📊 Created new execute-chat session: {session_key}")
    else:
        logging.info(f"📊 Using existing execute-chat session: {session_key}")
    
    return execute_chat_sessions[session_key]


# Core bot processing function - import graph only when needed
async def process_message(message: str) -> str:
    """Process a message through the bot graph and return response"""
    try:
        from fashion_bot.graph_context_meta import graph

        # Create simple state
        state = SupportState(
            messages=[],
            product_info="Premium cotton t-shirt with eco-friendly printing",
            phone_number=None,
            selected_order_id=None,
            known_orders=None,
            order_status_by_id=None,
            is_order_query=None,
            is_frustrated=None,
            needs_escalation=None,
            needs_human_agent=None,
            scratchpad=None
        )

        # Add user message to state
        from fashion_bot.state_cache import timestamped_human_message
        state["messages"] = [timestamped_human_message(message)]

        # Process through graph
        result = await graph.ainvoke(state)

        messages = result.get("messages", [])
        if messages:
            last_message = messages[-1]
            if isinstance(last_message, AIMessage):
                return last_message.content
            elif isinstance(last_message, dict):
                return last_message.get("content", "No response")
        return str(result.get("customer_message") or "Sorry, I couldn't generate a response.")

    except Exception as e:
        return f"Error: {str(e)}"


async def process_user_input(state: SupportState, user_input: str) -> SupportState:
    """Process user input and return updated state."""
    try:
        from fashion_bot.graph_context_meta import graph
        from fashion_bot.trace_context import generate_trace_id, set_trace_id
        # Initialize trace ID if not present
        if 'trace_id' not in state:
            state['trace_id'] = generate_trace_id()
        set_trace_id(state['trace_id'])

        # Add user message to state
        state["messages"] = state.get("messages", []) + [timestamped_human_message(user_input)]

        # Invoke the graph with required checkpoint configuration
        config = {
            "configurable": {
                "thread_id": "main_thread",
                "checkpoint_ns": "customer_support"
            }
        }
        result = await graph.ainvoke(state, config=config)

        # Update state with result (persist bot replies)
        if "messages" in result and result["messages"]:
            # Replace state's message list with full list from graph result
            state["messages"] = result["messages"]

        for key, value in result.items():
            if key == "messages":
                continue  # already handled above
            state[key] = value

        # Debug: Log what's in the result
        print(f"🔍 DEBUG: Result keys: {list(result.keys())}")
        if "messages" in result:
            print(f"🔍 DEBUG: Messages count: {len(result['messages'])}")
            if result["messages"]:
                print(f"🔍 DEBUG: Last message: {result['messages'][-1]}")
        if "customer_message" in result:
            print(f"🔍 DEBUG: Customer message: {result['customer_message']}")

        # Get the final response - prioritize messages over customer_message
        if "messages" in result and result["messages"]:
            final_message = result["messages"][-1]
            if hasattr(final_message, 'content'):
                print(f"\n🤖 Assistant: {final_message.content}")
            else:
                print(f"\n🤖 Assistant: {final_message}")
        elif "customer_message" in result and result["customer_message"]:
            # Only use customer_message if no messages are present
            print(f"\n🤖 Assistant: {result['customer_message']}")
        else:
            print("\n🤖 Assistant: I apologize, but I couldn't generate a response.")

        return state

    except Exception as e:
        print(f"\n❌ Error processing input: {str(e)}")
        logging.error(f"Error in process_user_input: {str(e)}")
        return state

# API Endpoints
@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Chat endpoint with state management - mimics main_meta.py behavior"""
    try:
        # Get or create session state
        state = get_or_create_session(request.session_id)
        
        # Process user input and get updated state
        updated_state = await process_user_input(state, request.message)
        
        # Update session with new state
        user_sessions[request.session_id] = updated_state
        
        messages = updated_state.get("messages", [])
        response = "I apologize, but I couldn't generate a response."
        
        if messages:
            last_message = messages[-1]
            if isinstance(last_message, AIMessage):
                response = last_message.content
            elif isinstance(last_message, dict):
                response = last_message.get("content", response)
        
        return ChatResponse(response=response, session_id=request.session_id)
        
    except Exception as e:
        logging.error(f"Error in chat endpoint: {str(e)}")
        return ChatResponse(response=f"Error: {str(e)}", session_id=request.session_id)


@app.post("/execute-chat", response_model=ExecuteChatResponse)
async def execute_chat(request: ExecuteChatRequest):
    """
    Execute chat endpoint - mimics Streamlit behavior.
    
    Takes a chat message as input, executes it through the LLM graph,
    and returns the response. This is a stateless endpoint that processes
    each request independently, similar to how Streamlit works.
    
    Args:
        request: ExecuteChatRequest containing:
            - message: The user message to process
            - phone_number: Optional phone number for context
            - client_id: Optional client ID for multi-client support
            - conversation_history: Not used (kept for backward compatibility)
    
    Returns:
        ExecuteChatResponse with the bot's response
    """
    try:
        from fashion_bot.graph_context_meta import graph
        
        logging.info(f"💬 Execute-chat received message: {request.message} [CLIENT={request.client_id or 'unknown'}] [PHONE={request.phone_number or 'unknown'}]")
        if request.client_id:
            logging.info(f"👤 Client ID: {request.client_id} [CLIENT={request.client_id}] [PHONE={request.phone_number or 'unknown'}]")
        
        # Get or create session state (like Streamlit does with st.session_state.support_state)
        state = get_or_create_execute_chat_session(request.phone_number, request.client_id)
        
        # Add attributes to current span for filtering in Grafana Tempo
        current_span = trace.get_current_span()
        if current_span:
            current_span.set_attribute("client_id", request.client_id or "unknown")
            current_span.set_attribute("phone_number", request.phone_number or "unknown")

        # Add user message to EXISTING state (like Streamlit line 83)
        state["messages"] = state.get("messages", []) + [timestamped_human_message(request.message)]
        
        logging.info(f"📊 Current message count: {len(state['messages'])}")
        logging.info("🔄 Invoking graph.ainvoke()...")
        
        # Invoke the graph (without checkpointer config like Streamlit)
        result = await graph.ainvoke(state)
        
        logging.info("✅ Graph invocation completed")
        
        # Update state with result (like Streamlit lines 93-99)
        if "messages" in result and result["messages"]:
            state["messages"] = result["messages"]
        
        for key, value in result.items():
            if key == "messages":
                continue
            state[key] = value
        
        logging.info(f"📊 Updated message count: {len(state.get('messages', []))}")
        
        # Extract the response from the result
        response = None
        
        # First try to get response from messages
        if "messages" in result and result["messages"]:
            final_message = result["messages"][-1]
            if hasattr(final_message, 'content'):
                response = final_message.content
            else:
                response = str(final_message)
        
        # Fallback to customer_message if available
        elif "customer_message" in result:
            response = result['customer_message']
        
        # Default response if nothing found
        if not response:
            response = "I apologize, but I couldn't generate a response. Please try again."
            logging.warning("⚠️ No response generated from graph")
        
        logging.info(f"📤 Response generated: {response[:100]}...")
        
        return ExecuteChatResponse(
            response=response,
            success=True,
            error=None,
            tool_call_trace=state.get("tool_call_trace"),
        )
        
    except Exception as e:
        error_msg = f"Error processing chat request: {str(e)}"
        logging.error(f"❌ {error_msg}")
        import traceback
        logging.error(f"Full traceback: {traceback.format_exc()}")
        
        return ExecuteChatResponse(
            response="I'm sorry, there was an error processing your request. Please try again.",
            success=False,
            error=error_msg
        )


@app.get("/health")
async def health():
    """Health check endpoint"""
    return {"status": "ok", "service": "fashion-bot-agent-controller"}


@app.get("/robots.txt")
async def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /")


@app.post("/api/v1/widget/script")
async def generate_widget_script(request: Request, payload: WidgetScriptRequest):
    """Generate self-serve widget embed script using encoded clientId."""
    try:
        base_url = _resolve_widget_embed_base_url(request, payload.baseUrl)
        result = _build_widget_script_payload(
            raw_client_id=payload.clientId,
            client_name=payload.clientName,
            position=payload.position,
            theme=payload.theme,
            base_url=base_url,
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except ValueError as exc:
        return JSONResponse(
            {"success": False, "error": str(exc)},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )


@app.get("/api/v1/widget/script")
async def generate_widget_script_get(
    request: Request,
    clientId: str,
    clientName: Optional[str] = None,
    position: Optional[str] = None,
    theme: Optional[str] = None,
    baseUrl: Optional[str] = None,
):
    """GET variant for simple admin/self-serve UI integrations."""
    try:
        base_url = _resolve_widget_embed_base_url(request, baseUrl)
        result = _build_widget_script_payload(
            raw_client_id=clientId,
            client_name=clientName,
            position=position,
            theme=theme,
            base_url=base_url,
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except ValueError as exc:
        return JSONResponse(
            {"success": False, "error": str(exc)},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )


@app.get("/metrics/websocket")
async def websocket_metrics():
    """Real-time WebSocket connection metrics for monitoring"""
    collector = get_metrics_collector()
    metrics_data = collector.get_metrics()
    
    return {
        "metrics": metrics_data,
        "health_status": collector.get_health_status(),
        "connections_detail": collector.get_connection_details(limit=50),
    }


@app.post("/session/reset")
async def reset_session(session_id: str = "default"):
    """Reset a session to initial state"""
    if session_id in user_sessions:
        user_sessions[session_id] = create_initial_state()
        return {"status": "ok", "message": f"Session {session_id} reset", "session_id": session_id}
    else:
        return {"status": "ok", "message": f"Session {session_id} created", "session_id": session_id}


@app.post("/execute-chat/reset")
async def reset_execute_chat_session(phone_number: str = None, client_id: str = None):
    """Reset an execute-chat session (clears conversation history)"""
    session_key = f"{phone_number}_{client_id}" if phone_number and client_id else (phone_number or client_id or "default")
    
    if session_key in execute_chat_sessions:
        del execute_chat_sessions[session_key]
        return {"status": "ok", "message": f"Execute-chat session {session_key} reset"}
    else:
        return {"status": "ok", "message": f"No session found for {session_key}"}


@app.get("/session/{session_id}/state")
async def get_session_state(session_id: str):
    """Get current state of a session"""
    if session_id in user_sessions:
        state = user_sessions[session_id]
        return {
            "session_id": session_id,
            "messages_count": len(state.get("messages", [])),
            "phone_number": state.get("phone_number"),
            "selected_order_id": state.get("selected_order_id"),
            "is_frustrated": state.get("is_frustrated"),
            "needs_escalation": state.get("needs_escalation"),
            "trace_id": state.get("trace_id")
        }
    else:
        return {"error": f"Session {session_id} not found"}


@app.get("/")
async def root():
    """Root endpoint with API information."""
    return {
        "status": "Fashion Bot Agent Controller is running",
        "endpoints": {
            "test_chat_widget": "GET /test - Test the web chat widget",
            "websocket_chat": "WS /ws/chat/{client_id}/{session_id} - WebSocket chat endpoint",
            "chat": "POST /chat - Send a message to the bot (with state management)",
            "execute_chat": "POST /execute-chat - Execute a chat message (Streamlit-like, stateless, with client_id support)",
            "session_reset": "POST /session/reset - Reset a session to initial state",
            "session_state": "GET /session/{session_id}/state - Get current session state",
            "websocket_metrics": "GET /metrics/websocket - Real-time WebSocket connection metrics",
            "webhook": "POST /webhook - WhatsApp webhook endpoint",
            "gupshup_webhook": "POST /gupshup/webhook - Gupshup webhook endpoint",
            "gupshup_events_webhook": "POST /gupshup/events/webhook - Gupshup delivery/failure event logger",
            "gupshup_send": "POST /gupshup/send - Send message via Gupshup",
            "shopify_webhook": "POST /order/webhook - Shopify order webhook endpoint",
            "product_webhook": "POST /product/webhook/products - Shopify product webhook (create/update/delete for vector DB sync)",
            "shiprocket_webhook": "POST /shipping/event/webhook - Shiprocket webhook endpoint",
            "return_prime_webhook": "POST /return-prime/webhook - Return Prime webhook event logger",
            "abandoncart_webhook": "POST /abandoncart/event/webhook/{encoded_client_id} - Abandon cart webhook endpoint",
            "instagram_webhook": "GET/POST /instagram/webhook - Instagram webhook verification and event endpoint",
            "health": "GET /health - Health check"
        },
        "features": {
            "web_chat_widget": "Embeddable chat widget for any website",
            "state_management": "Maintains conversation context across requests",
            "session_based": "Each session_id maintains separate conversation state",
            "threading": "Uses LangGraph checkpointing for conversation persistence",
            "stateless_execution": "Execute-chat endpoint for stateless message processing",
            "product_vector_sync": "Real-time product sync via Shopify webhooks (products/create, products/update, products/delete)"
        }
    }


@awith_retry
async def _ais_valid_webhook_verify_token(provider: str, channel: str, verify_token: str) -> bool:
    """Validate a plaintext webhook verify token stored in DB."""
    if not verify_token:
        return False

    sql = """
        SELECT 1
        FROM webhook_verification_tokens
        WHERE provider = %s
          AND channel = %s
          AND verify_token = %s
          AND is_active = TRUE
        LIMIT 1
    """
    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (provider, channel, verify_token))
            row = await cur.fetchone()
            return row is not None


async def _asend_instagram_text_reply(recipient_id: str, text: str) -> bool:
    """Send a simple Instagram DM reply using the Meta Graph API."""
    access_token = (
        os.getenv("INSTAGRAM_ACCESS_TOKEN")
        or os.getenv("META_PAGE_ACCESS_TOKEN")
        or os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN")
    )
    if not access_token:
        logging.warning("Instagram reply skipped: missing INSTAGRAM_ACCESS_TOKEN/META_PAGE_ACCESS_TOKEN")
        return False

    graph_version = os.getenv("META_GRAPH_API_VERSION", "v20.0")
    url = f"https://graph.facebook.com/{graph_version}/me/messages"
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text},
    }
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        client = await get_shared_async_http_client()
        response = await client.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code >= 400:
            logging.warning(
                "Instagram reply failed: status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
            return False
        logging.info("Instagram reply sent to sender_id=%s", recipient_id)
        return True
    except Exception as exc:
        logging.warning("Instagram reply failed: %s", exc, exc_info=True)
        return False


@app.get("/instagram/webhook")
async def instagram_webhook_verify(request: Request):
    """Meta/Instagram webhook verification callback."""
    params = request.query_params
    mode = params.get("hub.mode")
    verify_token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode != "subscribe" or not challenge:
        raise HTTPException(status_code=400, detail="Invalid Instagram webhook verification request")

    is_valid = await _ais_valid_webhook_verify_token("meta", "instagram", verify_token or "")
    if not is_valid:
        logging.warning("Instagram webhook verification failed: invalid verify token")
        raise HTTPException(status_code=403, detail="Invalid verify token")

    logging.info("Instagram webhook verification succeeded")
    return PlainTextResponse(content=str(challenge), status_code=200)


@app.post("/instagram/webhook")
async def receive_instagram_message(request: Request):
    """Instagram webhook event receiver for smoke-testing inbound messages."""
    try:
        raw_body = await request.body()
        raw_text = raw_body.decode("utf-8", errors="replace")
        logging.info(
            "Instagram webhook raw body received: bytes=%s body=%s",
            len(raw_body),
            raw_text[:4000],
        )
        payload = json.loads(raw_text or "{}")
        logging.info("Instagram webhook raw payload parsed: %s", payload)
    except Exception as exc:
        logging.warning("Instagram webhook received invalid JSON: %s", exc)
        return {"status": "ok"}

    if not isinstance(payload, dict):
        logging.warning("Instagram webhook payload was not an object")
        return {"status": "ok"}

    print("Webhook payload:", payload, flush=True)

    try:
        message_count = 0
        for entry in payload.get("entry", []) or []:
            if not isinstance(entry, dict):
                continue

            # Instagram Messaging webhooks commonly arrive as entry.changes[].value.
            for change in entry.get("changes", []) or []:
                if not isinstance(change, dict) or change.get("field") != "messages":
                    continue
                value = change.get("value") or {}
                if not isinstance(value, dict):
                    continue

                sender = value.get("sender") or {}
                sender_id = sender.get("id") if isinstance(sender, dict) else None
                logging.debug("Instagram webhook sender.id=%s", sender_id)

                message = value.get("message") or {}
                text = message.get("text") if isinstance(message, dict) else None
                if sender_id and text:
                    print("User:", sender_id)
                    print("Message:", text)
                    logging.info("Instagram message received from sender_id=%s", sender_id)
                    message_count += 1
                    await _asend_instagram_text_reply(sender_id, "Hello")

        logging.info(
            "Instagram webhook event received: object=%s entries=%s text_messages=%s",
            payload.get("object"),
            len(payload.get("entry") or []),
            message_count,
        )
    except Exception as exc:
        print("Error:", exc)
        logging.exception("Instagram webhook parse error")

    return {"status": "ok"}

# Include Gupshup webhook router
app.include_router(gupshup_router, prefix="")
app.include_router(gupshup_events_router, prefix="")
# Include Dashboard router
app.include_router(dashboard_router, prefix="")
app.include_router(shopify_webhook_router, prefix="/order")
app.include_router(abandoned_checkout_router, prefix="")
app.include_router(shiprocket_webhook_router, prefix="/shipping")
app.include_router(abandoncart_router, prefix="/abandoncart")
app.include_router(delhivery_webhook_router, prefix="/shipping/delhivery")
app.include_router(return_prime_webhook_router, prefix="/return-prime")
app.include_router(templates_router, prefix="")
# Include Product webhook router for vector DB sync (products/create, update, delete)
app.include_router(product_webhook_router, prefix="/product")
# Include WebSocket chat widget router
app.include_router(websocket_router)

# Include Widget config router (CDN versioning)
app.include_router(widget_router)

# Include Demo chat router for client demos (Chrome extension)
app.include_router(demo_router, prefix="/demo", tags=["Demo Chat"])

# Include Attribution router for chatbot conversion tracking
app.include_router(attribution_router)

# Include Attribution verification router (internal, LLM second-opinion on borderline conversions)
app.include_router(attribution_verify_router)

# Include Slack slash-command router (on-demand PR review trigger)
from fashion_bot.slack_commands_router import router as slack_commands_router
app.include_router(slack_commands_router, prefix="")

# Include Admin router (cache management, operational controls)
from fashion_bot.admin_router import router as admin_router
app.include_router(admin_router)

# Serve static files for chat widget with CDN-friendly cache headers
static_path = os.path.join(os.path.dirname(__file__), "..", "static")
if os.path.exists(static_path):
    # Use CachedStaticFiles for proper cache headers:
    # - chat-widget.js (loader): 5 min cache
    # - chat-widget.v*.js (bundles): 1 year immutable
    # - chat-widget-frame.html: 5 min cache
    app.mount("/static", CachedStaticFiles(directory=static_path), name="static")


def _widget_dev_test_routes_allowed() -> bool:
    """Local-only widget test pages; block production and staging (same env names as env_loader)."""
    for key in ("ENVIRONMENT", "NODE_ENV", "DEPLOY_ENV"):
        raw = os.getenv(key)
        if raw:
            e = raw.strip().lower()
            if e in {"production", "prod", "staging", "stage"}:
                return False
            return True
    return True


def _parse_widget_version_number(version: str) -> Optional[int]:
    if not version or not isinstance(version, str) or not version.startswith("v"):
        return None
    try:
        return int(version[1:])
    except ValueError:
        return None


def _supported_widget_test_versions(limit: int = 3) -> list[str]:
    if not os.path.isdir(static_path):
        return []

    available: list[tuple[int, str]] = []
    for name in os.listdir(static_path):
        if not (name.startswith("chat-widget.v") and name.endswith(".js")):
            continue
        version = name[len("chat-widget."):-len(".js")]
        version_number = _parse_widget_version_number(version)
        if version_number is None:
            continue
        test_file = os.path.join(static_path, f"test-widget-{version}.html")
        if os.path.exists(test_file):
            available.append((version_number, version))

    available.sort()
    return [version for _, version in available[-limit:]]


def _serve_widget_test_version(version: str):
    if not _widget_dev_test_routes_allowed():
        raise HTTPException(status_code=404, detail="Not Found")

    supported_versions = _supported_widget_test_versions()
    if version not in supported_versions:
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"Widget test page for {version} is not maintained.",
                "supported_versions": supported_versions,
            },
        )

    test_file = os.path.join(static_path, f"test-widget-{version}.html")
    if os.path.exists(test_file):
        return FileResponse(test_file)
    return {"error": f"Test page not found. Make sure static/test-widget-{version}.html exists."}


def _register_widget_test_routes() -> None:
    for version in _supported_widget_test_versions():
        async def _handler(version=version):
            return _serve_widget_test_version(version)

        async def _suggestions_handler(version=version):
            return _serve_widget_test_version(version)

        app.add_api_route(
            f"/test-{version}",
            _handler,
            methods=["GET"],
            name=f"test_chat_widget_{version}",
        )
        app.add_api_route(
            f"/test-{version}-suggestions",
            _suggestions_handler,
            methods=["GET"],
            name=f"test_chat_widget_{version}_suggestions",
        )


@app.get("/test")
async def test_chat_widget():
    """Serve test page for chat widget (disabled in production/staging)."""
    if not _widget_dev_test_routes_allowed():
        raise HTTPException(status_code=404, detail="Not Found")
    test_file = os.path.join(static_path, "test-chat-widget.html")
    if os.path.exists(test_file):
        return FileResponse(test_file)
    return {"error": "Test page not found. Make sure static/test-chat-widget.html exists."}

_register_widget_test_routes()



# Override webhook endpoint to pass process_message function

@app.post("/webhook")
async def webhook(request: Request):
    """WhatsApp webhook endpoint - routes through existing whatsapp_webhook"""
    try:
        # Import and use the existing webhook function
        from fashion_bot.whatsapp_webhook import webhook as whatsapp_webhook_func
        return await whatsapp_webhook_func(request)
    except Exception as e:
        print(f"❌ Webhook error: {e}")
        return {"error": str(e)}

@app.get("/cron/status")
async def cron_status():
    """
    Get status of all cron jobs.
    
    Returns information about:
    - Whether scheduler is running
    - List of scheduled jobs
    - Next run times
    """
    try:
        status = get_job_status()
        return {
            "success": True,
            **status
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": "Failed to get cron job status"
        }

@app.post("/cron/trigger-agent-switch")
async def trigger_agent_switch():
    """
    Manually trigger the agent mode auto-switch cron job.
    
    This will immediately switch all inactive agent modes to bot mode
    without waiting for the scheduled run.
    """
    try:
        from fashion_bot.cron_jobs.agent_mode_auto_switch import auto_switch_inactive_agent_modes
        result = await auto_switch_inactive_agent_modes()
        return result
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": "Failed to trigger agent mode auto-switch"
        }


@app.post("/cron/trigger-conversation-analytics")
async def trigger_conversation_analytics(request: Request, background_tasks: BackgroundTasks):
    """
    Manually trigger the conversation analytics cron job in the background.

    Security:
    - Requires `X-Internal-Token` header to match `CRON_TRIGGER_INTERNAL_TOKEN`.
    """
    expected_token = (os.getenv("CRON_TRIGGER_INTERNAL_TOKEN") or "").strip()
    provided_token = (request.headers.get("X-Internal-Token") or "").strip()

    if not expected_token:
        logging.error("[CRON_TRIGGER] CRON_TRIGGER_INTERNAL_TOKEN is not configured")
        return JSONResponse(
            {
                "success": False,
                "accepted": False,
                "message": "Cron trigger token not configured on server",
            },
            status_code=503,
        )

    if not provided_token:
        return JSONResponse(
            {
                "success": False,
                "accepted": False,
                "message": "Missing X-Internal-Token header",
            },
            status_code=401,
        )

    if provided_token != expected_token:
        return JSONResponse(
            {
                "success": False,
                "accepted": False,
                "message": "Invalid X-Internal-Token",
            },
            status_code=403,
        )

    import uuid
    import datetime as dt
    run_id = str(uuid.uuid4())
    accepted_at = dt.datetime.now(dt.timezone.utc).isoformat()

    async def _run():
        try:
            from fashion_bot.cron_jobs.conversation_analytics_job import analyze_pending_conversations

            result = await analyze_pending_conversations()
            logging.info(
                "[CRON_TRIGGER] Conversation analytics run completed: run_id=%s result=%s",
                run_id,
                json.dumps(result, default=str),
            )
        except Exception as exc:
            logging.error(
                "[CRON_TRIGGER] Conversation analytics run failed: run_id=%s error=%s",
                run_id,
                exc,
                exc_info=True,
            )

    background_tasks.add_task(_run)

    return {
        "success": True,
        "accepted": True,
        "job": "conversation_analytics",
        "run_id": run_id,
        "accepted_at": accepted_at,
        "message": "Conversation analytics trigger accepted",
    }


@app.post("/cron/invalidate-template-cache")
async def invalidate_template_cache(request: Request):
    """
    Manually invalidate template cache.

    Body:
    - client_id (required)
    - channel (optional)
    - event_key (optional)

    Security:
    - Requires `X-Internal-Token` header matching `CRON_TRIGGER_INTERNAL_TOKEN`.
    """
    expected_token = (os.getenv("CRON_TRIGGER_INTERNAL_TOKEN") or "").strip()
    provided_token = (request.headers.get("X-Internal-Token") or "").strip()

    if not expected_token:
        logging.error("[CACHE_INVALIDATION] CRON_TRIGGER_INTERNAL_TOKEN is not configured")
        return JSONResponse(
            {"success": False, "message": "Cron trigger token not configured on server"},
            status_code=503,
        )

    if not provided_token:
        return JSONResponse(
            {"success": False, "message": "Missing X-Internal-Token header"},
            status_code=401,
        )

    if provided_token != expected_token:
        return JSONResponse(
            {"success": False, "message": "Invalid X-Internal-Token"},
            status_code=403,
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"success": False, "message": "Invalid JSON body"},
            status_code=400,
        )

    client_id = str((body or {}).get("client_id") or "").strip()
    channel = str((body or {}).get("channel") or "").strip() or None
    event_key = str((body or {}).get("event_key") or "").strip() or None

    if not client_id:
        return JSONResponse(
            {"success": False, "message": "client_id is required"},
            status_code=400,
        )

    try:
        from fashion_bot.shopify.webhook.templates_db import ainvalidate_client_template_cache

        result = await ainvalidate_client_template_cache(
            client_id=client_id,
            channel=channel,
            event_key=event_key,
        )
        return {
            "success": True,
            "message": "Template cache invalidated",
            "input": {
                "client_id": client_id,
                "channel": channel,
                "event_key": event_key,
            },
            "result": result,
        }
    except Exception as exc:
        logging.error("[CACHE_INVALIDATION] Failed to invalidate template cache: %s", exc, exc_info=True)
        return JSONResponse(
            {"success": False, "message": f"Failed to invalidate cache: {exc}"},
            status_code=500,
        )


# ==================== PRODUCT INGESTION ENDPOINTS ====================

@app.post("/api/v1/products/ingest")
async def ingest_products(request: Request, background_tasks: BackgroundTasks):
    """
    Ingest products from Shopify or /products.json to Upstash Search.

    The ingestion runs in the background. The response immediately returns a
    ``run_id`` and ``poll_path`` that can be polled for progressive status.

    Request Body:
        - client_id (str, required): The client ID
        - source (str, optional): "shopify", "json", or "auto" (default: "auto")
        - force_refresh (bool, optional): Clear existing vectors before ingestion (default: false)
        - max_products (int, optional): Cap products (0 = unlimited)

    Returns:
        - success (bool)
        - client_id (str)
        - run_id (str): Unique run identifier
        - poll_path (str): GET endpoint to poll for status
        - message (str)
    """
    try:
        import uuid
        from fashion_bot.services.product_ingestion import (
            ProductIngestionOrchestrator,
            ProductIngestionRequest,
        )
        from fashion_bot.services.product_ingestion.sync_logger import (
            IngestionStatusTracker,
            FULL_INGESTION_STAGES,
        )

        body = await request.json()

        client_id = body.get("client_id")
        if not client_id:
            return {
                "success": False,
                "error": "client_id is required",
                "message": "Please provide a client_id in the request body"
            }

        ingestion_request = ProductIngestionRequest(
            client_id=client_id,
            source=body.get("source", "auto"),
            force_refresh=body.get("force_refresh", False)
        )
        max_products = int(body.get("max_products", 0))
        run_id = str(uuid.uuid4())

        tracker = IngestionStatusTracker(run_id=run_id, client_id=client_id)
        await tracker.acreate_run(
            sync_source="manual_api",
            sync_type="full_ingestion",
            stages=FULL_INGESTION_STAGES,
        )

        async def _run():
            try:
                orchestrator = ProductIngestionOrchestrator()
                await orchestrator.ingest_products(
                    client_id=ingestion_request.client_id,
                    source=ingestion_request.source,
                    force_refresh=ingestion_request.force_refresh,
                    max_products=max_products,
                    trace_id=run_id,
                    tracker=tracker,
                )
            except Exception as exc:
                logging.error(f"❌ Background ingestion failed: {exc}", exc_info=True)

        background_tasks.add_task(_run)

        return {
            "success": True,
            "client_id": client_id,
            "run_id": run_id,
            "poll_path": f"/api/v1/products/ingest/status/{client_id}/{run_id}",
            "message": "Ingestion started in background. Poll the poll_path for progress.",
        }

    except ValueError as e:
        return {
            "success": False,
            "error": str(e),
            "message": "Invalid configuration. Check client_id and ensure Shopify credentials or website URL are configured."
        }
    except Exception as e:
        logging.error(f"❌ Product ingestion error: {e}")
        return {
            "success": False,
            "error": str(e),
            "message": "Failed to ingest products. Check logs for details."
        }


@app.get("/api/v1/products/ingest/status/{client_id}")
async def get_ingestion_status(client_id: str):
    """
    Get ingestion status for a client.
    
    Returns:
        - client_id (str): The client ID
        - last_ingestion (str): ISO timestamp of last ingestion
        - product_count (int): Number of products in vector index
        - source_used (str): Source used for last ingestion
        - index_health (str): Health status of the vector index
    """
    try:
        from fashion_bot.services.product_ingestion import ProductIngestionOrchestrator
        
        orchestrator = ProductIngestionOrchestrator()
        status = orchestrator.get_status(client_id)
        
        return status.model_dump()
        
    except Exception as e:
        logging.error(f"❌ Error getting ingestion status: {e}")
        return {
            "success": False,
            "error": str(e),
            "message": "Failed to get ingestion status"
        }


@app.get("/api/v1/products/ingest/status/{client_id}/{run_id}")
async def get_ingestion_run_status(client_id: str, run_id: str):
    """
    Poll progressive ingestion/sync status for a specific run.

    Returns per-step progress, overall status, and product counters.

    Path Parameters:
        - client_id (str): The client ID
        - run_id (str): The run ID returned from the ingest/delta-sync endpoint

    Returns:
        - success (bool)
        - status (pending | in_progress | completed | completed_with_warnings | failed)
        - progress (total_steps, completed_steps, current_step)
        - steps (per-step JSONB)
        - products_added, products_updated, products_deleted, products_unchanged, products_failed
        - started_at, updated_at, completed_at, duration_seconds
    """
    try:
        from fashion_bot.services.product_ingestion.sync_logger import get_ingestion_run

        result = await get_ingestion_run(client_id, run_id)
        if not result:
            return {
                "success": False,
                "client_id": client_id,
                "run_id": run_id,
                "status": "not_found",
                "message": f"No ingestion run found for run_id={run_id}",
            }
        return result
    except Exception as e:
        logging.error(f"❌ Ingestion status poll error: {e}")
        return {
            "success": False,
            "client_id": client_id,
            "run_id": run_id,
            "status": "failed",
            "error": str(e),
            "message": "Failed to fetch ingestion status. Check logs for details.",
        }


@app.get("/api/v1/products/ingest/history/{client_id}")
async def get_ingestion_history_endpoint(client_id: str, request: Request):
    """
    List product ingestion/sync run history for a client, newest first.

    Query Parameters:
        - limit (int, optional): Max rows to return (default: 20)
        - offset (int, optional): Pagination offset (default: 0)

    Returns:
        - success (bool)
        - client_id (str)
        - total (int): Total tracked runs for this client
        - items (list): Run summaries with status, counters, and timestamps
        - limit, offset (int): Pagination metadata
    """
    try:
        from fashion_bot.services.product_ingestion.sync_logger import get_ingestion_history

        limit = int(request.query_params.get("limit", 20))
        offset = int(request.query_params.get("offset", 0))

        result = await get_ingestion_history(client_id, limit=limit, offset=offset)
        return JSONResponse(
            {"success": True, "client_id": client_id, **result},
            headers={"Cache-Control": "no-store, no-cache"},
        )
    except Exception as e:
        logging.error(f"❌ Ingestion history error: {e}")
        return JSONResponse(
            {
                "success": False,
                "client_id": client_id,
                "total": 0,
                "items": [],
                "error": str(e),
                "message": "Failed to fetch ingestion history. Check logs for details.",
            },
            headers={"Cache-Control": "no-store, no-cache"},
        )


@app.post("/api/v1/products/delta-sync")
async def delta_sync_products(request: Request, background_tasks: BackgroundTasks):
    """
    Delta sync products to Upstash Search.

    Only updates changed products (ADD / UPDATE / DELETE / UNCHANGED).
    Runs in the background; returns a ``run_id`` and ``poll_path``.

    Request Body:
        - client_id (str, required): The client ID
        - source (str, optional): "shopify", "json", or "auto" (default: "auto")
        - hours (int, optional): Look-back window in hours (default: 168 = 7 days)

    Returns:
        - success (bool)
        - client_id (str)
        - run_id (str): Unique run identifier
        - poll_path (str): GET endpoint to poll for status
        - message (str)
    """
    try:
        import uuid
        from fashion_bot.services.product_ingestion import ProductIngestionOrchestrator
        from fashion_bot.services.product_ingestion.sync_logger import (
            IngestionStatusTracker,
            DELTA_SYNC_STAGES,
        )

        body = await request.json()

        client_id = body.get("client_id")
        if not client_id:
            return {
                "success": False,
                "error": "client_id is required",
                "message": "Please provide a client_id in the request body"
            }

        source = body.get("source", "auto")
        hours = int(body.get("hours", 168))
        run_id = str(uuid.uuid4())

        tracker = IngestionStatusTracker(run_id=run_id, client_id=client_id)
        await tracker.acreate_run(
            sync_source="manual_api",
            sync_type="delta_sync",
            stages=DELTA_SYNC_STAGES,
        )

        async def _run():
            try:
                orchestrator = ProductIngestionOrchestrator()
                await orchestrator.delta_sync_products(
                    client_id=client_id,
                    source=source,
                    hours=hours,
                    tracker=tracker,
                )
            except Exception as exc:
                logging.error(f"❌ Background delta sync failed: {exc}", exc_info=True)

        background_tasks.add_task(_run)

        return {
            "success": True,
            "client_id": client_id,
            "run_id": run_id,
            "poll_path": f"/api/v1/products/ingest/status/{client_id}/{run_id}",
            "message": "Delta sync started in background. Poll the poll_path for progress.",
        }

    except ValueError as e:
        return {
            "success": False,
            "error": str(e),
            "message": "Invalid configuration. Check client_id and ensure Shopify credentials are configured."
        }
    except Exception as e:
        logging.error(f"❌ Delta sync error: {e}")
        return {
            "success": False,
            "error": str(e),
            "message": "Failed to delta sync products. Check logs for details."
        }


@app.post("/api/v1/products/delta-sync/all")
async def delta_sync_all_clients():
    """
    Trigger the weekly product delta sync for all configured clients.

    Mirrors the Sunday cron: this is a producer that fans out one Dramatiq
    job per client onto the ``cron.product.sync`` lane (consumed by
    queue-background-worker). The heavy ingestion runs on the worker, never
    inline on this service. If the lane is disabled / broker unreachable the
    clients are skipped (logged), not run inline.

    Returns:
        - success (bool): Whether dispatch completed
        - clients_processed (int): Number of clients considered
        - clients_queued (int): Jobs enqueued to the worker
        - clients_skipped (int): Clients skipped (lane off / broker down)
        - duration_seconds (float): Dispatch time
    """
    try:
        from fashion_bot.cron_jobs.product_vector_sync_job import product_vector_delta_sync

        result = await product_vector_delta_sync()
        return result

    except Exception as e:
        logging.error(f"❌ Delta sync all clients error: {e}")
        return {
            "success": False,
            "error": str(e),
            "message": "Failed to delta sync all clients. Check logs for details."
        }


@app.post("/cron/trigger-product-vector-sync")
async def trigger_product_vector_sync():
    """
    Manually trigger the product vector delta sync (weekly cron).

    Fans out one delta-sync job per client to the queue-background-worker
    instead of waiting for the scheduled Sunday run. Returns the dispatch
    summary (clients_queued / clients_skipped); per-client product counts are
    produced on the worker.
    """
    try:
        from fashion_bot.cron_jobs.product_vector_sync_job import product_vector_delta_sync
        result = await product_vector_delta_sync()
        return result
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": "Failed to trigger product vector delta sync"
        }


# ==================== SYNC LOGS & STATS ENDPOINTS ====================


@app.get("/api/v1/products/sync-logs/{client_id}")
async def get_sync_logs(
    client_id: str,
    limit: int = 50,
    offset: int = 0,
    sync_source: Optional[str] = None,
    status: Optional[str] = None,
):
    """
    Paginated sync log entries for a client.

    Query params:
        - limit (int): Page size (default 50)
        - offset (int): Offset (default 0)
        - sync_source (str, optional): Filter by source — "webhook", "cron", "manual_api"
        - status (str, optional): Filter by status — "success", "partial_failure", "failure"
    """
    try:
        from fashion_bot.services.product_ingestion.sync_logger import ProductSyncLogger

        result = await ProductSyncLogger.aget_sync_logs(
            client_id=client_id,
            limit=min(limit, 200),
            offset=max(offset, 0),
            sync_source=sync_source,
            status=status,
        )
        return {"success": True, "client_id": client_id, **result}
    except Exception as e:
        logging.error(f"❌ Error getting sync logs: {e}")
        return {"success": False, "error": str(e)}


@app.get("/api/v1/products/sync-stats/{client_id}")
async def get_sync_stats(client_id: str, days: int = 7):
    """
    Aggregated sync statistics for a client over the last N days.
    """
    try:
        from fashion_bot.services.product_ingestion.sync_logger import ProductSyncLogger

        result = await ProductSyncLogger.aget_sync_stats(client_id, days=days)
        return {"success": True, **result}
    except Exception as e:
        logging.error(f"❌ Error getting sync stats: {e}")
        return {"success": False, "error": str(e)}


# ==================== SINGLE PRODUCT RE-INGEST ====================


@app.post("/api/v1/products/ingest/single")
async def ingest_single_product(request: Request):
    """
    Re-ingest a single product into Upstash Search.

    Fetches the product from Shopify by ID, runs LLM attribute extraction,
    and upserts the search document.  If the product has manually edited
    attributes in Postgres, those are used instead of LLM extraction.

    Request Body:
        - client_id (str, required)
        - product_id (str, required): Shopify numeric product ID
    """
    try:
        from fashion_bot.services.product_ingestion import ProductIngestionOrchestrator

        body = await request.json()
        client_id = body.get("client_id")
        product_id = body.get("product_id")

        if not client_id:
            return {"success": False, "error": "client_id is required"}
        if not product_id:
            return {"success": False, "error": "product_id is required"}

        orchestrator = ProductIngestionOrchestrator()
        result = await orchestrator.ingest_single_product(
            client_id=client_id,
            product_id=str(product_id),
        )
        return result

    except Exception as e:
        logging.error(f"❌ Single product ingest error: {e}")
        return {"success": False, "error": str(e)}


@app.post("/api/v1/products/re-extract")
async def re_extract_product_attributes(request: Request):
    """
    Force LLM attribute re-extraction for specific products.

    LLM extraction is gated on the product's *identity* fields (title,
    product_type, description) so that merchandising churn — tag sweeps, price
    changes, a throttled metafield enrich — does not re-derive attributes that
    cannot have changed. The trade-off is that an attribute inferred only from
    tags or metafields can go stale until an identity field changes. This
    endpoint is the escape hatch for that case.

    Product IDs must be listed explicitly: there is no "whole catalog" switch,
    because that would be an unbounded LLM spend behind a single request. To
    rebuild an entire catalog use ``/api/v1/products/ingest``.

    Request Body:
        - client_id (str, required)
        - product_ids (list[str], required): Shopify numeric product IDs,
          at most 200 per call.
    """
    from fashion_bot.services.product_ingestion.re_extract import (
        ReExtractRequestError,
        arun_product_re_extraction,
    )

    try:
        body = await request.json()
        return await arun_product_re_extraction(
            body.get("client_id"), body.get("product_ids"),
        )
    except ReExtractRequestError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logging.error(f"❌ Product re-extract error: {e}")
        return {"success": False, "error": str(e)}


# ==================== BULK FIELD UPDATE ====================


@app.post("/api/v1/products/bulk-update-fields")
async def bulk_update_fields(request: Request):
    """
    Update specific fields on existing Upstash Search documents.

    Fetches existing docs, patches the requested fields, and re-upserts.

    Request Body:
        - client_id (str, required)
        - updates (list, required): Each entry is::

            {
                "product_id": "12345",
                "content_fields": {"bestseller": true, "in_stock": false},
                "metadata_fields": {}
            }
    """
    try:
        from fashion_bot.services.product_ingestion.upstash_search_service import (
            UpstashSearchService,
        )

        body = await request.json()
        client_id = body.get("client_id")
        updates = body.get("updates")

        if not client_id:
            return {"success": False, "error": "client_id is required"}
        if not updates or not isinstance(updates, list):
            return {"success": False, "error": "updates list is required"}

        service = UpstashSearchService()
        result = await service.abulk_update_fields(client_id, updates)

        return {
            "success": True,
            "client_id": client_id,
            "updated_count": result.get("updated_count", 0),
            "failed_count": len(result.get("failed", [])),
            "failed": result.get("failed", []),
        }
    except Exception as e:
        logging.error(f"❌ Bulk update fields error: {e}")
        return {"success": False, "error": str(e)}


# ==================== PRODUCT ATTRIBUTES CRUD ====================


@app.get("/api/v1/products/attributes/{client_id}")
async def list_product_attributes(
    client_id: str,
    limit: int = 50,
    offset: int = 0,
    manually_edited_only: bool = False,
):
    """Paginated listing of extracted product attributes stored in Postgres."""
    try:
        from fashion_bot.services.product_ingestion.product_attributes_store import (
            alist_product_attributes,
        )

        result = await alist_product_attributes(
            client_id=client_id,
            limit=min(limit, 200),
            offset=max(offset, 0),
            manually_edited_only=manually_edited_only,
        )
        return {"success": True, "client_id": client_id, **_serialise_pg_result(result)}
    except Exception as e:
        logging.error(f"❌ List product attributes error: {e}")
        return {"success": False, "error": str(e)}


@app.get("/api/v1/products/attributes/{client_id}/{product_id}")
async def get_product_attributes(client_id: str, product_id: str):
    """Get extracted attributes for a single product."""
    try:
        from fashion_bot.services.product_ingestion.product_attributes_store import (
            aget_product_attributes,
        )

        row = await aget_product_attributes(client_id, product_id)
        if not row:
            return {"success": False, "error": "Product attributes not found"}
        return {"success": True, "client_id": client_id, "attributes": _serialise_row(row)}
    except Exception as e:
        logging.error(f"❌ Get product attributes error: {e}")
        return {"success": False, "error": str(e)}


@app.put("/api/v1/products/attributes/{client_id}/{product_id}")
async def update_product_attributes(client_id: str, product_id: str, request: Request):
    """
    Manually edit extracted attributes for a product.

    Sets ``is_manually_edited = true`` so future ingestion runs preserve
    the manual overrides instead of re-running LLM extraction.

    Request Body: flat dict of attribute fields, e.g.::

        {"category": "topwear", "subcategory": "t-shirt", "fit": "slim"}
    """
    try:
        from fashion_bot.services.product_ingestion.product_attributes_store import (
            aupsert_product_attributes,
        )

        body = await request.json()
        row_id = await aupsert_product_attributes(
            client_id=client_id,
            product_id=product_id,
            attrs=body,
            product_title=body.get("product_title"),
            is_manually_edited=True,
        )
        if row_id is None:
            return {"success": False, "error": "Failed to update attributes"}
        return {"success": True, "client_id": client_id, "product_id": product_id, "id": row_id}
    except Exception as e:
        logging.error(f"❌ Update product attributes error: {e}")
        return {"success": False, "error": str(e)}


@app.post("/api/v1/products/attributes/{client_id}/sync-to-search")
async def sync_attributes_to_search(client_id: str, request: Request):
    """
    Push manually edited attributes from Postgres back to Upstash Search.

    Request Body (optional):
        - product_ids (list[str]): Specific products to sync.
          If omitted, syncs ALL manually edited products for this client.
    """
    try:
        from fashion_bot.services.product_ingestion.product_attributes_store import (
            aget_manually_edited_product_ids,
            aget_product_attributes,
        )
        from fashion_bot.services.product_ingestion.upstash_search_service import (
            UpstashSearchService,
        )

        body = await request.json() if await request.body() else {}
        requested_ids = body.get("product_ids")

        if requested_ids:
            edits = {}
            for pid in requested_ids:
                row = await aget_product_attributes(client_id, pid)
                if row and row.get("is_manually_edited"):
                    edits[pid] = row
        else:
            edits = await aget_manually_edited_product_ids(client_id)

        if not edits:
            return {"success": True, "synced_count": 0, "message": "No manually edited products to sync"}

        # Build bulk updates: map Postgres attribute columns → Upstash content fields
        attr_to_content = {
            "category": "category",
            "subcategory": "subcategory",
            "base_product_name": "base_product_name",
            "color": "extracted_color",
            "material": "material",
            "segment": "segment",
            "color_family": "color_family",
            "pattern": "pattern",
            "fit": "fit",
        }
        list_attrs = {"occasion", "style", "vibe", "pairing_tags"}

        updates = []
        for pid, row in edits.items():
            content_fields = {}
            for db_col, search_col in attr_to_content.items():
                val = row.get(db_col)
                if val is not None:
                    content_fields[search_col] = val
            for la in list_attrs:
                val = row.get(la)
                if val is not None:
                    content_fields[la] = val if isinstance(val, list) else []
            if row.get("product_line"):
                content_fields["product_line"] = row["product_line"]
                content_fields["product_line_normalized"] = row["product_line"].strip().lower()
            updates.append({"product_id": pid, "content_fields": content_fields})

        service = UpstashSearchService()
        result = await service.abulk_update_fields(client_id, updates)

        return {
            "success": True,
            "client_id": client_id,
            "synced_count": result.get("updated_count", 0),
            "failed_count": len(result.get("failed", [])),
            "failed": result.get("failed", []),
        }
    except Exception as e:
        logging.error(f"❌ Sync attributes to search error: {e}")
        return {"success": False, "error": str(e)}


# ==================== TAXONOMY CONFIG CRUD ====================


@app.get("/api/v1/taxonomy/{client_id}")
async def get_taxonomy_config(client_id: str):
    """
    Get the taxonomy configuration for a client.

    Returns the client-specific taxonomy if set, otherwise returns the
    built-in defaults.
    """
    try:
        from fashion_bot.services.product_ingestion.taxonomy_config_store import (
            aget_taxonomy_config,
            get_defaults,
        )

        config = await aget_taxonomy_config(client_id)
        is_custom = config is not None
        if not config:
            config = get_defaults()
            config["client_id"] = client_id
        return {"success": True, "is_custom": is_custom, "config": _serialise_row(config)}
    except Exception as e:
        logging.error(f"❌ Get taxonomy config error: {e}")
        return {"success": False, "error": str(e)}


@app.put("/api/v1/taxonomy/{client_id}")
async def update_taxonomy_config(client_id: str, request: Request):
    """
    Create or update taxonomy configuration for a client.

    Request Body: any subset of::

        {
            "categories": ["topwear", "bottomwear", ...],
            "subcategory_mapping": {"topwear": ["t-shirt", "blouse", ...]},
            "attribute_schema": {"jeans": ["fit", "wash", "rise"]},
            "occasions": [...], "styles": [...], "vibes": [...],
            "segments": [...], "color_families": [...], "patterns": [...]
        }
    """
    try:
        from fashion_bot.services.product_ingestion.taxonomy_config_store import (
            aupsert_taxonomy_config,
        )

        body = await request.json()
        ok = await aupsert_taxonomy_config(client_id, body)
        if not ok:
            return {"success": False, "error": "Failed to update taxonomy config"}
        return {"success": True, "client_id": client_id, "message": "Taxonomy config updated"}
    except Exception as e:
        logging.error(f"❌ Update taxonomy config error: {e}")
        return {"success": False, "error": str(e)}


@app.delete("/api/v1/taxonomy/{client_id}")
async def delete_taxonomy_config(client_id: str):
    """Delete custom taxonomy config, reverting to built-in defaults."""
    try:
        from fashion_bot.services.product_ingestion.taxonomy_config_store import (
            adelete_taxonomy_config,
        )

        ok = await adelete_taxonomy_config(client_id)
        return {"success": ok, "client_id": client_id}
    except Exception as e:
        logging.error(f"❌ Delete taxonomy config error: {e}")
        return {"success": False, "error": str(e)}


# ==================== Serialisation helpers ====================


def _serialise_row(row: dict) -> dict:
    """Make a Postgres row JSON-safe (datetimes, Decimals, UUIDs)."""
    import uuid
    from datetime import datetime as _dt
    from decimal import Decimal

    out = {}
    for k, v in row.items():
        if isinstance(v, _dt):
            out[k] = v.isoformat()
        elif isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, uuid.UUID):
            out[k] = str(v)
        else:
            out[k] = v
    return out


def _serialise_pg_result(result: dict) -> dict:
    """Serialise paginated result whose ``items`` are Postgres rows."""
    result["items"] = [_serialise_row(r) for r in result.get("items", [])]
    return result


# ==================== RECOMMENDATION ENGINE ENDPOINT ====================

@app.post("/api/v1/recommend")
async def recommend_products(request: Request):
    """
    AI-powered product recommendations using Upstash Search.

    Pipeline: Query Understanding (LLM) → Upstash Search → Business Rules Reranker → Response (LLM)

    Request Body:
        - client_id (str, required): The client ID
        - user_query (str, required): User's natural language query
        - session_id (str, optional): Session ID for tracking
        - user_profile (dict, optional): {segment, size, budget_max, preferred_fits, preferred_colors}
        - context (dict, optional): {conversation_history: [...]}
    """
    try:
        from fashion_bot.services.recommendation import RecommendationService

        body = await request.json()

        client_id = body.get("client_id")
        user_query = body.get("user_query")

        if not client_id:
            return {"success": False, "error": "client_id is required"}
        if not user_query:
            return {"success": False, "error": "user_query is required"}

        service = RecommendationService()
        result = await service.recommend(
            client_id=client_id,
            user_query=user_query,
            session_id=body.get("session_id"),
            user_profile=body.get("user_profile"),
            conversation_history=body.get("context", {}).get("conversation_history"),
        )

        return {"success": True, **result}

    except Exception as e:
        logging.error(f"Recommendation error: {e}")
        import traceback
        logging.error(f"Traceback: {traceback.format_exc()}")
        return {
            "success": False,
            "error": str(e),
            "message": "Failed to generate recommendations.",
        }


# ==================== CLIENT ONBOARDING ENDPOINT ====================


class OnboardClientRequest(BaseModel):
    """Request model for the client onboarding endpoint."""
    client_id: str
    skip_client_configs: bool = False
    skip_agents_config: bool = False
    skip_product_ingestion: bool = False


@app.post("/api/v1/client/onboard")
async def onboard_client_endpoint(request: OnboardClientRequest):
    """
    Onboard a new client by auto-generating client_configs and agents_config.

    Copies configuration from the reference client (Groovee), scrapes the new
    client's domain to extract policies/contact info, and uses GPT-4o to adapt
    agent prompts for the new business type.

    Existing configs and agents are NOT overwritten (idempotent).

    Request Body:
        - client_id (str, required): UUID of the client to onboard (must already
          exist in the `clients` table with name and domain populated)

    Returns:
        - success (bool)
        - status (success | success_with_warnings | failed)
        - message (str)
        - client_name, domain, business_type, categories
        - configs_created / configs_skipped
        - agents_created / agents_skipped
        - steps (per-step onboarding status details)
        - errors (list of strings)
        - warnings (list of strings)
        - products_ingestion (background status string)
        - ingestion_run_id (str)
        - ingestion_poll_path (str)
        - duration_seconds (float)
    """
    try:
        from fashion_bot.client_onboarding import onboard_client

        result = await onboard_client(
            request.client_id,
            skip_client_configs=request.skip_client_configs,
            skip_agents_config=request.skip_agents_config,
            skip_product_ingestion=request.skip_product_ingestion,
        )
        return result

    except Exception as e:
        logging.error(f"❌ Client onboarding error: {e}")
        import traceback
        logging.error(f"Traceback: {traceback.format_exc()}")
        return {
            "success": False,
            "error": str(e),
            "message": "Failed to onboard client. Check logs for details.",
        }


@app.get("/api/v1/client/onboard/status/{client_id}/{run_id}")
async def onboard_client_status_endpoint(client_id: str, run_id: str):
    """
    Poll unified onboarding status for a specific run.

    Returns per-step progress (sync + async), overall status, and counters.
    Falls back to the legacy product_sync_logs lookup if the run predates
    the onboarding_runs table.

    Returns:
        - success (bool)
        - client_id, run_id (str)
        - status (pending | in_progress | completed | completed_with_warnings | failed | unknown)
        - message (str)
        - progress (total_steps, completed_steps, current_step)
        - steps (per-step status JSONB)
        - started_at, completed_at, duration_seconds
    """
    try:
        from fashion_bot.client_onboarding import get_onboarding_ingestion_status

        return await get_onboarding_ingestion_status(client_id, run_id)
    except Exception as e:
        logging.error(f"❌ Client onboarding status error: {e}")
        import traceback
        logging.error(f"Traceback: {traceback.format_exc()}")
        return {
            "success": False,
            "client_id": client_id,
            "run_id": run_id,
            "status": "failed",
            "error": str(e),
            "message": "Failed to fetch onboarding status. Check logs for details.",
        }


@app.get("/api/v1/client/onboard/history/{client_id}")
async def onboard_client_history_endpoint(client_id: str):
    """
    List all onboarding runs for a client, newest first.

    Returns:
        - success (bool)
        - client_id (str)
        - runs (list of run summaries)
    """
    try:
        from fashion_bot.client_onboarding import get_onboarding_history

        runs = await get_onboarding_history(client_id)
        return JSONResponse(
            {"success": True, "client_id": client_id, "runs": runs},
            headers={"Cache-Control": "no-store, no-cache"},
        )
    except Exception as e:
        logging.error(f"❌ Client onboarding history error: {e}")
        import traceback
        logging.error(f"Traceback: {traceback.format_exc()}")
        return JSONResponse(
            {
                "success": False,
                "client_id": client_id,
                "runs": [],
                "error": str(e),
                "message": "Failed to fetch onboarding history. Check logs for details.",
            },
            headers={"Cache-Control": "no-store, no-cache"},
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn_host = os.getenv("UVICORN_HOST", "0.0.0.0")
    uvicorn_port = _env_int("UVICORN_PORT", 8000, min_value=1)
    uvicorn_workers = _env_int("UVICORN_WORKERS", 2, min_value=1)
    uvicorn_reload = _env_bool("UVICORN_RELOAD", False)
    uvicorn_log_level = os.getenv("UVICORN_LOG_LEVEL", "info")

    # Detect an attached debugger so we can force single-worker startup — uvicorn
    # multi-worker mode spawns child processes that don't survive under debugpy
    # ("Child process died"). NOTE: on Python 3.12+, debugpy uses sys.monitoring
    # (PEP 669) instead of sys.settrace, so sys.gettrace() is None under the
    # debugger; rely on the presence of the debugpy/pydevd modules instead.
    debug_session_active = (
        bool(os.getenv("DEBUGPY_LAUNCHER_PORT"))
        or bool(os.getenv("VSCODE_DEBUGPY_ADAPTER_ENDPOINTS"))
        or sys.gettrace() is not None        # legacy settrace-based debuggers
        or "debugpy" in sys.modules          # VS Code / Cursor debugpy launcher (3.12+ safe)
        or "pydevd" in sys.modules           # PyCharm / pydevd
    )
    if debug_session_active and uvicorn_workers > 1:
        print("⚠️ Debug session detected (debugpy); forcing UVICORN_WORKERS=1 for stable startup")
        uvicorn_workers = 1

    # if uvicorn_reload and uvicorn_workers > 1:
    #     print("⚠️ UVICORN_RELOAD=true with UVICORN_WORKERS>1 is not supported; forcing workers=1")
    #     uvicorn_workers = 1

    print("🚀 Starting Fashion Bot Agent Controller...")
    print("📱 Chat endpoint (with state): http://localhost:8000/chat")
    print("⚡ Execute-chat endpoint (Streamlit-like, stateless): http://localhost:8000/execute-chat")
    print("📱 Session reset: http://localhost:8000/session/reset")
    print("📱 Session state: http://localhost:8000/session/{session_id}/state")
    print("📱 WhatsApp webhook endpoint: http://localhost:8000/webhook")
    print("📱 Gupshup webhook endpoint: http://localhost:8000/gupshup/webhook")
    print("📱 Gupshup send endpoint: http://localhost:8000/gupshup/send")
    print("📱 Shopify webhook endpoint: http://localhost:8000/order/webhook")
    print("📱 Shiprocket webhook endpoint: http://localhost:8000/shipping/event/webhook")
    print("📱 Return Prime webhook endpoint: http://localhost:8000/return-prime/webhook")
    print("🏥 Health check: http://localhost:8000/health")
    print("=" * 60)
    print("💡 /chat endpoint: Maintains conversation state (use session_id)")
    print("⚡ /execute-chat: Stateless Streamlit-like execution (with client_id support)")
    print(f"⚙️ Uvicorn host={uvicorn_host} port={uvicorn_port} workers={uvicorn_workers} reload={uvicorn_reload}")
    print("⚙️ Asyncio runtime uses native async I/O; no custom default threadpool configured")
    print("=" * 60)
    print("\n" + "=" * 60)
    print("🚀 Starting Fashion Bot Agent Controller")
    print("=" * 60)
    print("\n💬 WEB CHAT WIDGET:")
    print("   🧪 Test page: http://localhost:8000/test")
    print("   🔌 WebSocket: ws://localhost:8000/ws/chat/{client_id}/{session_id}")
    print("\n📱 API ENDPOINTS:")
    print("   Chat endpoint (with state): http://localhost:8000/chat")
    print("   Session reset: http://localhost:8000/session/reset")
    print("   Session state: http://localhost:8000/session/{session_id}/state")
    print("\n📨 WEBHOOKS:")
    print("   WhatsApp: http://localhost:8000/webhook")
    print("   Gupshup: http://localhost:8000/gupshup/webhook")
    print("   Shopify: http://localhost:8000/order/webhook")
    print("   Shiprocket: http://localhost:8000/shipping/event/webhook")
    print("   Return Prime: http://localhost:8000/return-prime/webhook")
    print("\n🏥 Health check: http://localhost:8000/health")
    print("\n" + "=" * 60)
    print("💡 The /chat endpoint maintains conversation state")
    print("💡 Use session_id to maintain separate conversations")
    print("🎉 Web chat widget now available at /test!")
    print("=" * 60 + "\n")
    app_target = "fashion_bot.agent_controller:app" if uvicorn_workers > 1 or uvicorn_reload else app
    uvicorn.run(
        app_target,
        host=uvicorn_host,
        port=uvicorn_port,
        workers=uvicorn_workers,
        reload=uvicorn_reload,
        log_level=uvicorn_log_level,
    )
