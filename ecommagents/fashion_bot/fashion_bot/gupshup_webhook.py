from fastapi import APIRouter, Request, BackgroundTasks
import contextvars
import re
import os
import json
import logging
import uuid
import datetime
import asyncio
from typing import Any, AsyncGenerator, Dict, Optional
from fashion_bot.repository import bot_user_agent_mode
from fashion_bot.agent_config import aget_agent_phone_number
from fashion_bot.state_cache import (
    get_cache_stats,
    cleanup_expired_states,
    aget_or_create_state,
    aupdate_state,
    aget_state_by_numbers,
)
# from fashion_bot.history.bigquery_logger import log_conversation_to_bigquery  # DISABLED: BigQuery logging
from fashion_bot.history.conversation_handler import (
    astore_message_event_with_conversation_resolution,
)
from fashion_bot.config_manager import aresolve_client_id, aget_gupshup_config_by_client_id, aget_gupshup_config_by_source, aget_config
from fashion_bot.analytics.cancellation_aversion_tracker import analyze_turn as _track_cancellation_aversion
from fashion_bot.utils.media_storage import MEDIA_MESSAGE_TYPES, aprepare_inbound_media

# Langsmith imports for tracing
from langsmith.run_helpers import traceable
from langsmith import Client
from fashion_bot.langsmith_config import get_langsmith_config, setup_langsmith_for_service
from fashion_bot.utils.utils import get_current_environment
from fashion_bot.utils.outbound_guard import sanitize_outbound_text, TOOL_CALL_DIRECTIVE, enrich_after_tool_call_directive
from fashion_bot.env_loader import bootstrap_environment, get_bool, get_env
from fashion_bot.utils.redis_guard import RedisGuard
from fashion_bot.core.conversation_runtime import ConversationRuntime, RuntimeResult, RuntimeContext
from fashion_bot.core.gupshup_runtime_support import GupshupRuntimeSupport
from fashion_bot.core.message_persistence import ensure_assistant_message_for_skip_final
from fashion_bot.utils.langsmith_tracing import traced_operation, set_trace_io, snapshot_state_for_trace
from fashion_bot.utils.http_client import get_shared_async_http_client

bootstrap_environment()

# Ensure logs directory exists and configure absolute log file path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOGS_DIR, "gupshup_webhook.log")


from fashion_bot.rollbar_config import report_error


# Configure logging — trace_id injected by TraceIdFilter via %(trace_id)s
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - [%(trace_id)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
from fashion_bot.trace_context import install_trace_filter
install_trace_filter()
logger = logging.getLogger("gupshup_webhook")


# Initialize Langsmith tracing for Gupshup service
LANGSMITH_CONFIG = get_langsmith_config("gupshup")
LANGSMITH_ENABLED = setup_langsmith_for_service("gupshup")

# Async Redis comes from utils.redis_client (one shared process-wide
# client). The sync ``redis`` library is no longer used here.
from fashion_bot.utils.redis_client import get_shared_async_redis_client

REDIS_URL = get_env("REDIS_URL") or get_env("REDIS_CONNECTION_STRING") or "redis://localhost:6379/0"
CHANNEL_CLIENT_PREFIX = "gupshup:inbound:"

_redis_client = None
# Async Redis client comes from utils.redis_client (single process-wide
# instance shared with config_manager / utils). The legacy alias below
# keeps existing call sites unchanged.
_get_async_redis_client = get_shared_async_redis_client
_redis_guard = RedisGuard()


def _should_skip_final_answer_for_gupshup() -> bool:
    """Resolve per-channel final_answer bypass flag for WhatsApp/Gupshup."""
    return get_bool("SKIP_FINAL_ANSWER_GUPSHUP", get_bool("SKIP_FINAL_ANSWER", True))


def _build_langsmith_run_url(run_id: Optional[str]) -> Optional[str]:
    """Best-effort LangSmith run URL for quick debugging in logs."""
    rid = str(run_id or "").strip()
    if not rid:
        return None
    project_name = getattr(LANGSMITH_CONFIG, "project_name", None) or "fashion-bot-general-dev"
    return f"https://smith.langchain.com/projects/p/{project_name}/r/{rid}"


def _replace_trace_io(
    run: Any,
    *,
    inputs: Optional[Dict[str, Any]] = None,
    outputs: Optional[Dict[str, Any]] = None,
) -> None:
    """Replace LangSmith run IO instead of merging, for cleaner table rendering."""
    if run is None:
        return
    if inputs is not None:
        try:
            run.inputs = dict(inputs)
            if hasattr(run, "extra") and isinstance(run.extra, dict):
                run.extra["inputs_is_truthy"] = False
        except Exception:
            set_trace_io(run, inputs=inputs)
    if outputs is not None:
        try:
            run.outputs = dict(outputs)
        except Exception:
            set_trace_io(run, outputs=outputs)


async def _mark_degraded_state_flag(sender_phone: str, client_id: str, component: str, flag_key: str):
    try:
        state = await aget_state_by_numbers(sender_phone, client_id) or {}
        degraded_components = list(state.get("degraded_components") or [])
        if component not in degraded_components:
            degraded_components.append(component)
        state["degraded_mode"] = True
        state["degraded_components"] = degraded_components
        state["last_redis_error_at"] = datetime.datetime.now().isoformat()
        state[flag_key] = True
        await aupdate_state(sender_phone, client_id, state)
    except Exception as _state_flag_err:
        logger.warning(f"Failed to set degraded state flag {flag_key}: {_state_flag_err}")


_runtime_support: GupshupRuntimeSupport | None = None


def _get_runtime_support() -> GupshupRuntimeSupport:
    global _runtime_support
    if _runtime_support is None:
        _runtime_support = GupshupRuntimeSupport(
            get_redis_client_fn=_get_async_redis_client,
            redis_guard=_redis_guard,
            log_fn=log_with_trace_id,
            mark_degraded_state_fn=_mark_degraded_state_flag,
            get_state_fn=aget_state_by_numbers,
            update_state_fn=aupdate_state,
            generate_trace_id_fn=generate_trace_id,
        )
    else:
        # Keep bindings hot-swappable for tests/patches without restarting process.
        _runtime_support._get_redis_client = _get_async_redis_client
        _runtime_support._redis_guard = _redis_guard
        _runtime_support._get_state = aget_state_by_numbers
        _runtime_support._update_state = aupdate_state
        _runtime_support._generate_trace_id = generate_trace_id
    return _runtime_support


async def _try_acquire_processing_lock(client_id: str, sender_phone: str, trace_id: str) -> tuple[bool, bool]:
    return await _get_runtime_support().try_acquire_processing_lock(client_id, sender_phone, trace_id)


async def _release_processing_lock(client_id: str, sender_phone: str, trace_id: str):
    await _get_runtime_support().release_processing_lock(client_id, sender_phone, trace_id)


async def _enqueue_pending_message(client_id: str, sender_phone: str, trace_id: str, payload_data: dict) -> bool:
    return await _get_runtime_support().enqueue_pending_message(client_id, sender_phone, trace_id, payload_data)


async def _drain_pending_queue(client_id: str, sender_phone: str) -> list[dict]:
    return await _get_runtime_support().drain_pending_queue(client_id, sender_phone)


def _build_merged_payload(base_data: dict, pending_entries: list[dict]) -> tuple[dict, int, int]:
    return _get_runtime_support().build_merged_payload(base_data, pending_entries)


def _should_trigger_summary(state_snapshot: dict, reply_text: str = "") -> tuple[bool, str]:
    # Temporarily disabled in-process summarization.
    # Plan: move summarization trigger/processing to external event queue worker.
    return False, "disabled_external_queue"


def _enqueue_summary_job(sender_phone: str, client_id: str, state_snapshot: dict, trigger_reason: str, trace_id: str):
    # No-op while summary pipeline is externalized.
    log_with_trace_id(
        trace_id,
        "📝 Summary enqueue skipped (disabled in-process; expected via external queue)",
        "debug",
        sender_phone,
        client_id=client_id,
    )


_conversation_runtime: ConversationRuntime | None = None


def _create_detached_task(coro: Any, *, name: Optional[str] = None) -> asyncio.Task[Any]:
    """Start a task in a fresh context so it cannot inherit a parent LangSmith run."""
    fresh_context = contextvars.Context()
    if name:
        try:
            return fresh_context.run(asyncio.create_task, coro, name=name)
        except TypeError:
            pass
    return fresh_context.run(asyncio.create_task, coro)


def _redispatch_payload(merged_payload: dict, merged_trace_id: str, client_id: str) -> None:
    _create_detached_task(
        process_webhook_payload(merged_payload, merged_trace_id, client_id),
        name=f"gupshup-redispatch-{merged_trace_id}",
    )


def _get_conversation_runtime() -> ConversationRuntime:
    global _conversation_runtime
    if _conversation_runtime is None:
        _conversation_runtime = ConversationRuntime(
            log_fn=log_with_trace_id,
            mark_degraded_state_fn=_mark_degraded_state_flag,
            try_acquire_lock_fn=_try_acquire_processing_lock,
            enqueue_pending_fn=_enqueue_pending_message,
            release_lock_fn=_release_processing_lock,
            drain_pending_fn=_drain_pending_queue,
            build_merged_payload_fn=_build_merged_payload,
            should_trigger_summary_fn=_should_trigger_summary,
            enqueue_summary_job_fn=_enqueue_summary_job,
            redispatch_fn=_redispatch_payload,
        )
    return _conversation_runtime

def _sanitize_redis_url(url: str) -> str:
    """Sanitizes a Redis URL for logging by removing sensitive information."""
    if "://" in url:
        parts = url.split("://", 1)
        return f"{parts[0]}://***"
    return url

async def publish_inbound_to_redis(phone: str, text: str, sender: str = "customer", client_id: str = None):
    try:
        client = await _get_async_redis_client()
        if not client:
            return
        # Use provided client_id directly for Redis channel
        if not client_id:
            return
        client_channel = f"{CHANNEL_CLIENT_PREFIX}{client_id}"
        client_payload = json.dumps({
            "direction": "inbound",
            "text": text,
            "sender": sender,
            "phone": phone
        })
        await client.publish(client_channel, client_payload)
    except Exception as ex:
        logger.error(f"Failed to publish inbound to Redis: {ex}")


async def publish_outbound_to_redis(phone: str, text: str, sender: str = "bot", client_id: str = None):
    try:
        client = await _get_async_redis_client()
        if not client:
            return
        # Use provided client_id directly for Redis channel
        if not client_id:
            return
        client_channel = f"{CHANNEL_CLIENT_PREFIX}{client_id}"
        client_payload = json.dumps({
            "direction": "outbound",
            "text": text,
            "sender": sender,
            "phone": phone
        })
        await client.publish(client_channel, client_payload)
    except Exception as ex:
        logger.error(f"Failed to publish outbound to Redis: {ex}")

# Trace ID helpers — delegate to shared trace_context module
from fashion_bot.trace_context import generate_trace_id, set_trace_id

def log_with_trace_id(trace_id: str, message: str, level: str = "info", phone: str = None, client_id: str = None):
    """Log message. Trace ID auto-injected by TraceIdFilter in formatter."""
    getattr(logger, level, logger.info)(message)


router = APIRouter()

# Gupshup configuration - loaded dynamically per-request from database with Redis caching
# These are fallback values only used when dynamic lookup fails
GUPSHUP_API_KEY = "sk_532ed59de7f3414c96e96a3de5bbe24b"
GUPSHUP_SOURCE = "15557872987"
GUPSHUP_URL = "https://api.gupshup.io/wa/api/v1/msg"
APP_NAME = "Ecommagents"

logger.debug(f"gupshup fallback config loaded")

# In-memory session for user phone numbers
user_sessions = {}

def is_quota_exceeded(response_text: str) -> bool:
    """Check if response contains OpenAI quota exceeded error"""
    return "You exceeded your current quota, please check your plan and billing details" in response_text

async def send_message(to, message, trace_id=None, gupshup_source=None, client_id=None):
    """Send message via Gupshup API.
    
    Args:
        to: Destination phone number
        message: Message text to send
        trace_id: Trace ID for logging
        gupshup_source: The Gupshup source number (business phone) - used for multi-client config lookup (DEPRECATED, use client_id)
        client_id: The client UUID - preferred method for config lookup
    """
    # Check if quota is exceeded - block message sending
    quota_exceeded = is_quota_exceeded(message)
    agent_phone = None
    if quota_exceeded:
        agent_phone = await aget_agent_phone_number(client_id=client_id)
        log_with_trace_id(trace_id, f"🚫 LLM quota exceeded. Would send to {to}: {message[:50]}...", "warning", agent_phone)

    # Multi-version routing: tenants configured for the newer Gupshup enterprise
    # GatewayAPI are served by that transport; everyone else falls through to the
    # existing (legacy) code path below, unchanged.
    from fashion_bot.utils.whatsapp_api_version import (
        WHATSAPP_API_VERSION_ENTERPRISE,
        aget_whatsapp_api_version,
    )

    if await aget_whatsapp_api_version(client_id, trace_id) == WHATSAPP_API_VERSION_ENTERPRISE:
        from fashion_bot.utils.whatsapp_enterprise_client import asend_enterprise_text_message

        # Mirror the legacy quota guard: the raw "quota exceeded" error must go to
        # the support agent, never to the customer.
        enterprise_to = (agent_phone or to) if quota_exceeded else to
        return await asend_enterprise_text_message(
            enterprise_to, message, client_id=client_id, trace_id=trace_id
        )

    # Get client-specific Gupshup config
    # Priority: client_id > gupshup_source > defaults
    client_config = None

    # First try to get config by client_id (preferred method)
    if client_id:
        client_config = await aget_gupshup_config_by_client_id(client_id)
        if client_config:
            log_with_trace_id(trace_id, f"✅ Got Gupshup config via client_id: {client_id}", "info", to)

    # Fall back to gupshup_source if client_id lookup failed
    if not client_config and gupshup_source:
        client_config = await aget_gupshup_config_by_source(gupshup_source)
        if client_config:
            log_with_trace_id(trace_id, f"✅ Got Gupshup config via source: {gupshup_source}", "info", to)
    
    if client_config:
        api_key = client_config.get("GUPSHUP_API_KEY", GUPSHUP_API_KEY)
        api_url = client_config.get("GUPSHUP_URL", GUPSHUP_URL)
        app_name = client_config.get("APP_NAME", APP_NAME)
        source_number = client_config.get("GUPSHUP_SOURCE", GUPSHUP_SOURCE)
    else:
        api_key = GUPSHUP_API_KEY
        api_url = GUPSHUP_URL
        app_name = APP_NAME
        source_number = GUPSHUP_SOURCE
    
    if not api_key:
        log_with_trace_id(trace_id, f"Gupshup not configured. Would send to {to}: {message}", "warning", to)
        report_error('Gupshup is not configured, critical error', level='error', to=to, trace_id=trace_id)
        return

    # Normalize phone number (remove +91 if present, ensure it's 10 digits)
    to_clean = re.sub(r'^\+91', '', str(to))
    if len(to_clean) == 10:
         if is_quota_exceeded(message):
            to_clean = await aget_agent_phone_number(client_id=client_id) or to_clean
         else:
            to_clean = f"91{to_clean}"
    
    payload = {
        "channel": "whatsapp",
        "source": source_number,
        "destination": to_clean,
        "message": json.dumps({
            "type": "text",
            "text": message
        }),
        "src.name": app_name,
        "disablePreview": False,
        "encode": False
    }
    
    headers = {
        "apikey": api_key,
        "accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    try:
        log_with_trace_id(trace_id, f"Sending to ***{to_clean[-4:]}", "debug", to)
        client = await get_shared_async_http_client()
        import time as _t
        _t0 = _t.monotonic()
        response = await client.post(api_url, data=payload, headers=headers, timeout=30)
        _elapsed = int((_t.monotonic() - _t0) * 1000)

        if response.status_code in (401, 403):
            log_with_trace_id(
                trace_id,
                f"[GUPSHUP] POST elapsed_ms={_elapsed} status={response.status_code} "
                f"auth_failed client_id={client_id or 'unknown'}",
                "error",
                to,
            )
            return None
        elif response.status_code in [200, 202]:
            log_with_trace_id(trace_id, f"[GUPSHUP] POST elapsed_ms={_elapsed} status={response.status_code} to=***{to_clean[-4:]}", "info", to)
            return response.json()
        else:
            log_with_trace_id(
                trace_id,
                f"[GUPSHUP] POST elapsed_ms={_elapsed} status={response.status_code} "
                f"client_id={client_id or 'unknown'}",
                "error",
                to,
            )
            return None
            
    except Exception as e:
        log_with_trace_id(trace_id, f"Exception occurred while sending message from {source_number} to {to_clean}: {str(e)}", "error", to)
        report_error(
            'Exception sending Gupshup message',
            level='error',
            exc_info=(type(e), e, e.__traceback__),
            source_number=source_number,
            to=to_clean,
            trace_id=trace_id,
        )
        return None
@traceable(
    name="gupshup-fashion-bot-conversation",
    project_name=LANGSMITH_CONFIG.project_name,
    metadata=LANGSMITH_CONFIG.get_traceable_metadata("gupshup_webhook", endpoint="conversation")
)
async def call_main_bot(question, phone_number, trace_id, client_id=None, gupshup_source=None):

    """
    Call the main_meta.py graph directly with the user's question and phone number as trace_id.
    
    Args:
        question (str): The user's message/question
        phone_number (str): The user's phone number to use as trace_id
        trace_id (str): Trace ID for logging
        client_id (str): The client ID (derived from 'app' parameter in webhook)
        gupshup_source (str): The actual Gupshup source number from the message (for config lookup)
    
    Returns:
        tuple: (response, langsmith_trace_id)
    """
    # Use provided client_id or fall back to lookup from source (for backward compatibility)
    if not client_id and gupshup_source:
        client_id = await aresolve_client_id(gupshup_source)
    
    # Debug logging
    log_with_trace_id(trace_id, f"🔍 call_main_bot: client_id={client_id}, gupshup_source={gupshup_source}", "info", phone_number)
    
    # Initialize langsmith_trace_id at the start
    langsmith_trace_id = None
    
    try:
        # Get LangSmith trace ID from the @traceable decorator context
        logger.info(f"🔍 LANGSMITH_ENABLED={LANGSMITH_ENABLED}")
        
        if LANGSMITH_ENABLED:
            from langsmith import get_current_run_tree
            current_run = get_current_run_tree()
            
            # Detailed logging for debugging
            logger.info(f"🔍 get_current_run_tree() returned: {type(current_run).__name__ if current_run else 'None'}")
            
            if current_run:
                logger.info(f"🔍 current_run has trace_id: {hasattr(current_run, 'trace_id')}, id: {hasattr(current_run, 'id')}")
                if hasattr(current_run, 'trace_id'):
                    langsmith_trace_id = str(current_run.trace_id)
                    logger.info(f"✅ LANGSMITH_TRACE_ID CAPTURED: {langsmith_trace_id}")
                elif hasattr(current_run, 'id'):
                    langsmith_trace_id = str(current_run.id)
                    logger.info(f"✅ LANGSMITH_ID CAPTURED (from .id): {langsmith_trace_id}")
                else:
                    logger.warning(f"⚠️ current_run exists but has no trace_id or id attribute")
            else:
                logger.warning(f"⚠️ get_current_run_tree() returned None - @traceable decorator context may not be active")
            
            logger.info(f"📞 Tracing conversation for phone: {phone_number[-4:]}**** with trace_id: {trace_id}, langsmith_trace_id: {langsmith_trace_id}")
            log_with_trace_id(trace_id, f"LangSmith trace_id: {langsmith_trace_id}", "info", phone_number)
            
            if current_run:
                current_run.extra = {
                    "trace_id": trace_id,
                    "langsmith_trace_id": langsmith_trace_id,
                    "phone_number": phone_number,
                    "question": question[:100] if question else "",
                    "service": "gupshup_call_main_bot"
                }
        else:
            logger.warning(f"⚠️ LANGSMITH_ENABLED is False - no LangSmith tracing")
        
        # Get or create state using the state cache module
        # phone_number is the sender (from), client_id is used as tenant_id for Redis key
        with traced_operation(
            "gupshup.state_get_or_create",
            metadata={"client_id": client_id, "phone_suffix": str(phone_number)[-4:]},
        ):
            state, is_new_state = await aget_or_create_state(phone_number, client_id, question, None)
        
        # Inject trace_id and client_id into state
        state['trace_id'] = trace_id
        state["_skip_final_answer"] = _should_skip_final_answer_for_gupshup()
        
        # Ensure client_id is in state for multi-client support
        if not state.get('client_id'):
            state['client_id'] = client_id
            state['gupshup_source_phone_number'] = gupshup_source  # Store source number for sending messages
            log_with_trace_id(trace_id, f"✅ Set client_id in state: {client_id}", "info", phone_number)
            log_with_trace_id(
                trace_id,
                f"⚙️ _skip_final_answer={state.get('_skip_final_answer')} (env SKIP_FINAL_ANSWER_GUPSHUP/SKIP_FINAL_ANSWER)",
                "info",
                phone_number,
            )
            # Persist the updated state
            with traced_operation(
                "gupshup.state_update_bootstrap",
                metadata={"client_id": client_id, "phone_suffix": str(phone_number)[-4:]},
            ):
                await aupdate_state(phone_number, client_id, state)
            log_with_trace_id(trace_id, f"✅ Persisted client_id to state cache", "info", phone_number)
        else:
            log_with_trace_id(trace_id, f"✅ client_id already in state: {state.get('client_id')}", "info", phone_number)
            log_with_trace_id(
                trace_id,
                f"⚙️ _skip_final_answer={state.get('_skip_final_answer')} (env SKIP_FINAL_ANSWER_GUPSHUP/SKIP_FINAL_ANSWER)",
                "info",
                phone_number,
            )

        # Try to invoke the graph with checkpointing first
        try:
            # Configure graph execution with phone number as thread_id
            config = {
                "configurable": {
                    "thread_id": phone_number,  # Use phone number as thread_id for conversation persistence
                    "checkpoint_ns": "gupshup_support"
                },
                # Add tracing metadata to config
                "metadata": {
                    "trace_id": trace_id,  # Primary trace_id for filtering
                    "user_phone": phone_number,
                    "question": question,
                    "service": "gupshup",
                    "is_new_conversation": is_new_state
                }
            }
            
            # Set client_id in context before invoking graph (for tools to access)
            from fashion_bot.client_context import set_client_id
            set_client_id(state.get('client_id'))
            from fashion_bot.monitoring.otel_metrics import set_request_client_id
            set_request_client_id(state.get('client_id') or client_id)
            log_with_trace_id(trace_id, f"🔧 Set client_id in ContextVar: {state.get('client_id')}", "info", phone_number)
            
            # Invoke the graph (lazy import to avoid circular dependency)
            from fashion_bot.graph_context_meta import graph
            logger.info(f"🚀 Invoking graph for phone {phone_number[-4:]}**** with question: {question[:50]}...")
            with traced_operation(
                "gupshup.graph_invoke_with_checkpoint",
                run_type="chain",
                metadata={"client_id": state.get("client_id")},
            ) as gupshup_graph_run:
                set_trace_io(gupshup_graph_run, inputs={"state": snapshot_state_for_trace(state)})
                result = await graph.ainvoke(state, config=config)
                set_trace_io(gupshup_graph_run, outputs={"state": snapshot_state_for_trace(result)})
            logger.info(f"✅ Graph execution completed for phone {phone_number[-4:]}****")
            
        except Exception as checkpoint_error:
            log_with_trace_id(trace_id, f"Checkpoint error, trying without checkpointing: {checkpoint_error}", "warning", phone_number)
            report_error(
                "Checkpoint error in call_main_bot",
                level='warning',
                exc_info=(type(checkpoint_error), checkpoint_error, checkpoint_error.__traceback__),
                phone_number=phone_number,
                trace_id=trace_id,
                client_id=client_id,
            )
            # Fallback: invoke without checkpointing
            from fashion_bot.client_context import set_client_id
            set_client_id(state.get('client_id'))
            log_with_trace_id(trace_id, f"🔧 Set client_id in ContextVar (fallback): {state.get('client_id')}", "info", phone_number)
            from fashion_bot.graph_context_meta import graph
            with traced_operation(
                "gupshup.graph_invoke_no_checkpoint",
                run_type="chain",
                metadata={"client_id": state.get("client_id")},
            ) as gupshup_graph_fallback_run:
                set_trace_io(gupshup_graph_fallback_run, inputs={"state": snapshot_state_for_trace(state)})
                result = await graph.ainvoke(state)
                set_trace_io(gupshup_graph_fallback_run, outputs={"state": snapshot_state_for_trace(result)})

        # If final_answer is skipped (streaming/explicit flag), persist customer_message as AI turn.
        result = ensure_assistant_message_for_skip_final(
            state_before_invoke=state,
            result=result,
        )
        
        # Log result debug info
        log_with_trace_id(trace_id, f"🔍 DEBUG - Raw result keys: {list(result.keys()) if result else 'None'}", "info", phone_number)
        
        # ============= DEBUG: Log conversation_context from result =============
        if result:
            ctx = result.get("conversation_context")
            if ctx:
                log_with_trace_id(trace_id, f"📦 [CONTEXT_DEBUG] conversation_context FOUND in result!", "info", phone_number)
                log_with_trace_id(trace_id, f"📦 [CONTEXT_DEBUG] Entities: {len(ctx.get('entities') or [])}", "info", phone_number)
                focal_entity = ctx.get('focal_entity') or {}
                log_with_trace_id(trace_id, f"📦 [CONTEXT_DEBUG] Focal: {focal_entity.get('entity_value') or 'None'}", "info", phone_number)
                log_with_trace_id(trace_id, f"📦 [CONTEXT_DEBUG] Topic: {ctx.get('topic') or 'None'}", "info", phone_number)
            else:
                log_with_trace_id(trace_id, f"⚠️ [CONTEXT_DEBUG] conversation_context NOT found in result keys: {list(result.keys()) if result else 'None'}", "warning", phone_number)
        
        # ============= DEBUG: Log tool execution trace =============
        if result:
            tool_trace = result.get("tool_call_trace")
            if tool_trace:
                tool_names = [t.get("tool", "unknown") for t in tool_trace]
                log_with_trace_id(trace_id, f"🧰 [TOOL_TRACE] Tools executed: {tool_names}", "info", phone_number)
                for t in tool_trace:
                    tool_name = t.get("tool", "unknown")
                    tool_input = str(t.get("input", ""))[:200]
                    tool_obs = str(t.get("observation_preview", ""))[:200]
                    log_with_trace_id(trace_id, f"🧰 [TOOL_TRACE] {tool_name}: input={tool_input} | output={tool_obs}", "info", phone_number)
            else:
                log_with_trace_id(trace_id, f"🧰 [TOOL_TRACE] No tools executed in this turn", "info", phone_number)

        # NOTE: Tags are now generated asynchronously AFTER reply is sent (async_tag_generator.py)
        # No tag extraction from graph result needed here.
        
        # Update the cached state with the result, preserving conversation_id
        if result:
            # Get existing state to preserve conversation_id
            try:
                with traced_operation(
                    "gupshup.state_read_for_conversation_id",
                    metadata={"client_id": client_id, "phone_suffix": str(phone_number)[-4:]},
                ):
                    existing_state = await aget_state_by_numbers(phone_number, client_id)
                existing_conversation_id = existing_state.get("conversation_id") if existing_state else None
                
                # Preserve conversation_id when updating state
                if existing_conversation_id:
                    result["conversation_id"] = existing_conversation_id
                    log_with_trace_id(trace_id, f"🔍 Preserving conversation_id {existing_conversation_id} in updated state", "info", phone_number)
                
                with traced_operation(
                    "gupshup.state_update_result",
                    metadata={"client_id": client_id, "phone_suffix": str(phone_number)[-4:]},
                ):
                    await aupdate_state(phone_number, client_id, result)
                log_with_trace_id(trace_id, f"✅ Updated state and preserved conversation_id", "info", phone_number)
            except Exception as e:
                log_with_trace_id(trace_id, f"⚠️ Error preserving conversation_id: {e}", "warning", phone_number)
                report_error(
                    "Error preserving conversation_id",
                    level='warning',
                    exc_info=(type(e), e, e.__traceback__),
                    phone_number=phone_number,
                    trace_id=trace_id,
                    client_id=client_id,
                )
                # Fallback: just update with result
                with traced_operation(
                    "gupshup.state_update_result_fallback",
                    metadata={"client_id": client_id, "phone_suffix": str(phone_number)[-4:]},
                ):
                    await aupdate_state(phone_number, client_id, result)
        
        # Extract the final response from the result
        if "messages" in result and result["messages"]:
            final_message = result["messages"][-1]
            if hasattr(final_message, 'content'):
                response = final_message.content
            else:
                response = str(final_message)
        elif "customer_message" in result and result["customer_message"]:
            response = result["customer_message"]
        else:
            response = "I apologize, but I couldn't generate a response. Please try again."
        
        # Log successful response
        logger.info(f"💬 Generated response for phone {phone_number[-4:]}****: {response[:100]}...")
        logger.info(f"🔗 Returning langsmith_trace_id: {langsmith_trace_id}")
        return response, langsmith_trace_id
            
    except Exception as e:
        import traceback
        log_with_trace_id(trace_id, f"Error in call_main_bot: {str(e)}", "error", phone_number)
        log_with_trace_id(trace_id, f"Traceback: {traceback.format_exc()}", "error", phone_number)
        report_error(
            "Error in call_main_bot",
            level='error',
            exc_info=(type(e), e, e.__traceback__),
            phone_number=phone_number,
            trace_id=trace_id,
            client_id=client_id,
        )
        return f"Sorry, there was an error processing your request: {str(e)}", langsmith_trace_id

def extract_10_digit_phone(phone):
    """Extract last 10 digits from phone number"""
    match = re.search(r'(\d{10})$', phone)
    return match.group(1) if match else phone


# Deduplication cache: Redis-based per-client deduplication with 6 hour TTL
# Key format: dedup:gupshup:{client_id}:{message_id}
DEDUP_TTL_SECONDS = 21600  # 6 hours

async def _amark_message_processed(client_id: str, message_id: str) -> bool:
    """Async variant of _mark_message_processed using the async Redis client."""
    if not message_id or not client_id:
        return True
    try:
        redis_client = await _get_async_redis_client()
        if not redis_client:
            logger.warning("Async Redis not available for deduplication")
            return True
        key = f"dedup:gupshup:{client_id}:{message_id}"
        guard_result = await _redis_guard.execute_async(
            op_name="gupshup_dedup_setnx",
            fn=lambda: redis_client.set(key, "1", nx=True, ex=DEDUP_TTL_SECONDS),
            fallback=True,
        )
        if not guard_result.ok:
            logger.warning(
                f"Async dedup degraded for message_id={message_id}, client_id={client_id}. "
                f"Allowing processing (fail-open). error={guard_result.error}"
            )
            return True
        return guard_result.value is True
    except Exception as e:
        logger.error(f"Async Redis deduplication failed for message_id={message_id}: {e}")
        return True

@router.post("/gupshup/webhook")
# NOTE: @traceable removed - only conversation traces should go to LangSmith, not webhook handlers
async def webhook(request: Request, background_tasks: BackgroundTasks):
    """Gupshup webhook endpoint with immediate ack, deduplication, and background processing"""
    trace_id = generate_trace_id()
    set_trace_id(trace_id)
    log_with_trace_id(trace_id, f"Webhook started", "info")
    
    try:
        data = await request.json()
        log_with_trace_id(trace_id, f"Received Gupshup webhook: {data}", "info")

        # Extract message ID for deduplication
        message_id = (
            data.get("payload", {}).get("id")
            or data.get("id")
        )
        
        # Extract 'app' parameter and source for validation
        payload = data.get('payload', {})
        source = payload.get('source', '')
        incoming_app_name = data.get('app', '')

        # 🔑 FAST DEDUP: App-scoped check BEFORE any DB calls.
        # Uses app_name + message_id so it runs before client_id resolution
        # but still isolates different clients (different Gupshup apps).
        # This closes the race window where two concurrent requests both
        # spend time resolving client_id before reaching the SETNX check.
        
        if message_id and incoming_app_name:
            try:
                rc = await _get_async_redis_client()
                if rc:
                    fast_key = f"dedup:msg:{incoming_app_name}:{message_id}"
                    fast_result = await _redis_guard.execute_async(
                        op_name="gupshup_fast_dedup_setnx",
                        fn=lambda: rc.set(fast_key, "1", nx=True, ex=DEDUP_TTL_SECONDS),
                        fallback=True,
                    )
                    if not fast_result.ok:
                        logger.warning(
                            f"Fast dedup degraded for message_id={message_id}, app={incoming_app_name}. "
                            f"Allowing processing (fail-open). error={fast_result.error}"
                        )
                    elif not fast_result.value:
                        log_with_trace_id(trace_id, f"⚡ Duplicate message rejected (fast dedup): {message_id}", "warning")
                        return {"status": "duplicate", "trace_id": trace_id}
            except Exception as _fast_err:
                logger.warning(f"Fast dedup check failed (non-blocking): {_fast_err}")
        
        # VALIDATION: Get client_id using 'app' parameter (primary method).
        # Enterprise sends app id; legacy sends APP_NAME. Keep legacy as the
        # fallback branch so existing tenants continue through the old flow.
        from fashion_bot.config_manager import (
            aget_client_id_by_app_name,
            aget_client_id_by_enterprise_app,
            avalidate_gupshup_app_for_client,
            avalidate_gupshup_app_name,
        )
        
        is_enterprise_app = False
        client_id = None
        if incoming_app_name:
            client_id = await aget_client_id_by_enterprise_app(incoming_app_name)
            if client_id:
                is_enterprise_app = True
            else:
                client_id = await aget_client_id_by_app_name(incoming_app_name)
        
        if not client_id:
            log_with_trace_id(trace_id, f"❌ App '{incoming_app_name}' not found. Rejecting webhook.", "error", client_id=client_id)
            report_error(
                'Gupshup webhook rejected: unknown app',
                level='error',
                incoming_app_name=incoming_app_name,
                trace_id=trace_id,
            )
            return {"status": "error", "message": "Unknown app name", "trace_id": trace_id}
        
        log_with_trace_id(trace_id, f"✅ App '{incoming_app_name}' validated - client_id: {client_id}", "info", client_id=client_id)
        
        from fashion_bot.utils.client_id_utils import is_client_blocklisted
        if is_client_blocklisted(client_id):
            log_with_trace_id(trace_id, f"🚫 Blocked webhook for blocklisted client_id: {client_id}", "info", client_id=client_id)
            return {"status": "blocked", "message": "Client is blocklisted", "trace_id": trace_id}
        
        # Optional: Validate that source matches the expected source for legacy
        # apps. Enterprise app ids are validated against
        # gupshup_enterprise_app_details instead.
        if is_enterprise_app:
            if not await avalidate_gupshup_app_for_client(client_id, incoming_app_name):
                log_with_trace_id(trace_id, f"⚠️ Enterprise app mismatch for client_id {client_id}", "warning", client_id=client_id)
        elif not await avalidate_gupshup_app_name(source, incoming_app_name):
            log_with_trace_id(trace_id, f"⚠️ App name mismatch for source {source}, but proceeding with client_id from app", "warning", client_id=client_id)

        message_id = payload.get('id', '')
        source = payload.get('source', '')
        message_type = payload.get('type', '')
        
        # 🔑 EARLY DEDUPLICATION CHECK - Redis-based per-client deduplication with 6hr TTL
        # This prevents race conditions where multiple webhook calls process the same message
        if message_id and client_id:
            # Atomic check-and-set: returns False if message was already processed
            if not await _amark_message_processed(client_id, message_id):
                log_with_trace_id(trace_id, f"Duplicate message ignored (Redis check): {message_id}", "warning", client_id=client_id)
                return {"status": "duplicate", "trace_id": trace_id}



        if message_type == 'text':
            message_content = payload.get('payload', {}).get('text', '').lower()
        elif message_type in MEDIA_MESSAGE_TYPES:
            # Same placeholder the background path writes, so the agent-mode
            # media backfill below can match the row it needs to update.
            message_content = _build_media_placeholder_text(message_type, payload)
        else:
            message_content = f"[{message_type} message]"

        sender_info = payload.get('sender', {})
        sender_phone = sender_info.get('phone', source)
        # Publish inbound message to Redis as early as possible using client_id
        try:
            phone_for_channel = (sender_phone or source or "").strip()
            if phone_for_channel:
                await publish_inbound_to_redis(phone_for_channel, message_content, sender="customer", client_id=client_id)
        except Exception as _pub_err:
            logger.warning(f"Redis inbound publish failed: {_pub_err}")

        state_user_bot = await bot_user_agent_mode.aget_conversation_state(sender_phone, client_id)
        current_mode = state_user_bot["mode"]
        last_activity_raw = state_user_bot.get("last_activity")
        
        # Normalize last_activity to tz-aware UTC datetime
        last_activity_dt = None
        if isinstance(last_activity_raw, datetime.datetime):
            last_activity_dt = last_activity_raw
        elif isinstance(last_activity_raw, str):
            s = last_activity_raw.replace('Z', '+00:00')
            try:
                last_activity_dt = datetime.datetime.fromisoformat(s)
            except Exception:
                last_activity_dt = None

        if last_activity_dt is not None:
            if last_activity_dt.tzinfo is None:
                last_activity_dt = last_activity_dt.replace(tzinfo=datetime.timezone.utc)
            else:
                last_activity_dt = last_activity_dt.astimezone(datetime.timezone.utc)
        
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        idle = (last_activity_dt is None) or ((now_utc - last_activity_dt) > datetime.timedelta(minutes=10))
        
        # --- Check idle timeout (10 minutes) ---
        if current_mode == "agent":
            if idle:
                # Auto switch back to bot
                await bot_user_agent_mode.aset_conversation_mode(sender_phone, "bot", client_id=client_id)
                current_mode = "bot"
                log_with_trace_id(trace_id, f"Idle timeout: switching {sender_phone} back to bot", "info", sender_phone, client_id=client_id)

        # --- Routing ---
        if current_mode == "agent":
            # Persist latest user message into in-memory conversation state for continuity
            try:
                # Ensure conversation state exists and append the latest user message
                state, _ = await aget_or_create_state(sender_phone, client_id, message_content, None)
                # Ensure client_id is set in state
                if not state.get('client_id'):
                    state['client_id'] = client_id
                    state['gupshup_source_phone_number'] = source
                    await aupdate_state(sender_phone, client_id, state)
                # Refresh last_activity to keep agent mode active
                await bot_user_agent_mode.aset_conversation_mode(sender_phone, "agent", client_id=client_id)
            except Exception as _state_err:
                logger.warning(f"State update failed in agent mode: {str(_state_err)}")
                report_error(
                    "State update failed in agent mode",
                    level='warning',
                    exc_info=(type(_state_err), _state_err, _state_err.__traceback__),
                    phone=sender_phone,
                    client_id=client_id,
                    trace_id=trace_id,
                )

            # Store inbound customer message in Postgres (agent-mode transcript)
            try:
                conv_hint = None
                try:
                    existing_state = await aget_state_by_numbers(sender_phone, client_id)
                    conv_hint = existing_state.get("conversation_id") if existing_state else None
                except Exception:
                    conv_hint = None
                # Get tags from state if available
                conversation_tags = []
                try:
                    existing_state = await aget_state_by_numbers(sender_phone, client_id)
                    if existing_state and "conversation_tags" in existing_state:
                        conversation_tags = existing_state["conversation_tags"]
                except Exception:
                    conversation_tags = []
                    
                conv_id = await astore_message_event_with_conversation_resolution(
                    client_id=client_id,
                    phone=sender_phone,
                    sender="customer",
                    text=message_content,
                    channel_type="whatsapp",
                    started_by="customer",
                    tags=conversation_tags if conversation_tags else None,
                    conversation_id=conv_hint,
                )
                # Customers send media in agent mode too. Upload after the ACK
                # (BackgroundTasks runs post-response) so Gupshup never waits on
                # Cloudinary, then backfill the link onto the row just written.
                if message_type in MEDIA_MESSAGE_TYPES and conv_id:
                    background_tasks.add_task(
                        _astore_inbound_media_and_backfill,
                        data=data,
                        conversation_id=conv_id,
                        placeholder_text=message_content,
                        sender_phone=sender_phone,
                        client_id=client_id,
                        trace_id=trace_id,
                    )
                # Attach conversation_id and client_id into state for continuity
                try:
                    # NOTE: Pass None for initial_message to avoid duplicate HumanMessage appends
                    # The message was already added in the first get_or_create_state call above
                    state, _ = await aget_or_create_state(sender_phone, client_id, None, None)
                    state["conversation_id"] = conv_id
                    # Ensure client_id is set in state
                    if not state.get('client_id'):
                        state['client_id'] = client_id
                        state['gupshup_source_phone_number'] = source
                    await aupdate_state(sender_phone, client_id, state)
                except Exception as state_err:
                    logger.warning(f"⚠️ Failed to update state with conversation_id: {state_err}")
            except Exception as _pg_err:
                logger.warning(f"Postgres conversation store failed (agent mode): {_pg_err}")
                report_error(
                    "Postgres conversation store failed (agent mode)",
                    level='warning',
                    exc_info=(type(_pg_err), _pg_err, _pg_err.__traceback__),
                    phone=sender_phone,
                    client_id=client_id,
                    trace_id=trace_id,
                )

            # DISABLED: BigQuery logging
            # await log_conversation_to_bigquery(
            #     user_question=message_content,
            #     bot_reply="",
            #     thread_id=trace_id,
            #     from_phone=sender_phone,
            #     to_phone=GUPSHUP_SOURCE,
            #     message_type="webhook-human-agent-chat",
            #     session_phone=sender_phone,
            #     extracted_order="",
            #     product_type=None,
            #     backend_url=None,
            #     backend_status=None,
            #     processing_time_ms=None,
            #     metadata={"message_id": message_id, "transcript": ""}
            # )

            log_with_trace_id(
                trace_id,
                f"GBQ insert ok [webhook]: thread_id={trace_id}, from={sender_phone}, to={GUPSHUP_SOURCE}, message_id={message_id}",
                "info",
                sender_phone,
                client_id=client_id
            )
            return None

        # NOTE: Deduplication check moved to early in webhook() - happens before any state modifications
        # This prevents race conditions where multiple webhook calls process the same message
        # ✅ Immediate ACK to Gupshup
        log_with_trace_id(trace_id, f"📤 Scheduling background task for processing", "info", client_id=client_id)
        background_tasks.add_task(process_webhook_payload, data, trace_id, client_id)
        log_with_trace_id(trace_id, f"✅ Background task scheduled, returning 200 OK", "info", client_id=client_id)
        return {"status": "ok", "trace_id": trace_id}

    except Exception as e:
        log_with_trace_id(trace_id, f"Gupshup webhook error: {e}", "error")
        report_error(
            "Gupshup webhook error",
            level='error',
            exc_info=(type(e), e, e.__traceback__),
            trace_id=trace_id,
        )
        return {"error": str(e), "trace_id": trace_id}


async def _astore_inbound_media_and_backfill(
    *,
    data: dict,
    conversation_id: str,
    placeholder_text: str,
    sender_phone: str,
    client_id: str,
    trace_id: str,
) -> None:
    """Store an inbound media attachment and backfill its link onto the row.

    Always runs *behind* the customer's reply — the transcript row is written
    first with the placeholder, and this fills in the durable link once the
    upload lands. Nothing a customer waits on is allowed to await it. Fail-open:
    on any error the row keeps the placeholder text it was inserted with.
    """
    try:
        new_text, metadata = await aprepare_inbound_media(
            data,
            placeholder_text=placeholder_text,
            client_id=client_id,
            phone=sender_phone,
            trace_id=trace_id,
        )
        if not metadata:
            return  # nothing stored — leave the row exactly as inserted

        from fashion_bot.history.postgres_conversations import aattach_media_to_message

        await aattach_media_to_message(
            conversation_id,
            "customer",
            placeholder_text,
            new_text,
            metadata,
        )
    except Exception as media_err:
        log_with_trace_id(
            trace_id,
            f"Agent-mode media storage failed: {media_err}",
            "warning",
            sender_phone,
            client_id=client_id,
        )
        report_error(
            "Agent-mode media storage failed",
            level='warning',
            exc_info=(type(media_err), media_err, media_err.__traceback__),
            phone=sender_phone,
            client_id=client_id,
            trace_id=trace_id,
        )


_MEDIA_EMOJI = {"image": "🖼️", "video": "🎥", "audio": "🎙️", "file": "📎"}


def _build_media_placeholder_text(message_type: str, payload: dict) -> str:
    """Transcript line for a media message, before any stored link is appended."""
    inner = payload.get("payload", {}) or {}
    caption = (inner.get("caption") or "").strip()
    if caption:
        return f"[{message_type.capitalize()} message]: {caption}"
    # Documents carry a filename, which names the attachment better than
    # "Customer sent file" does.
    filename = (inner.get("filename") or "").strip()
    if filename:
        return f"[{message_type.capitalize()} message]: {filename}"
    return f"[{message_type.capitalize()} message]: Customer sent {message_type}"


def _extract_media_url_from_payload(data: dict, trace_id: str = "", client_id: str = None) -> Optional[str]:
    """Pull the Gupshup temporary media URL from the webhook payload."""
    try:
        inner = (data.get("payload", {}) or {}).get("payload", {}) or {}
        return (inner.get("url") or "").strip() or None
    except Exception as url_err:
        log_with_trace_id(
            trace_id,
            f"Could not read media URL from inbound payload: {url_err}",
            "warning",
            client_id=client_id,
        )
        return None


def _extract_caption_from_payload(data: dict, trace_id: str = "", client_id: str = None) -> Optional[str]:
    """Pull the user's caption text from a media message payload."""
    try:
        inner = (data.get("payload", {}) or {}).get("payload", {}) or {}
        caption = (inner.get("caption") or "").strip()
        return caption or None
    except Exception as caption_err:
        log_with_trace_id(
            trace_id,
            f"Could not read caption from inbound payload: {caption_err}",
            "warning",
            client_id=client_id,
        )
        return None


async def _aanalyze_inbound_image_message(
    data: dict,
    message_type: str,
    message_content: str,
    trace_id: str,
    client_id: str,
    sender_phone: str,
) -> tuple[str, str]:
    """Rewrite an inbound image as a text message when analysis finds content.

    Runs combined OCR + product vision, for image messages only. When the image
    carries readable text, or shows a recognizable product, the message is
    rewritten as text so it enters the agent graph; OCR text wins when the image
    has both. Video, audio and documents are returned untouched.

    Fail-open: a disabled flag, a timeout, or any error returns the original
    ``(message_type, message_content)``, so the caller falls through to the
    existing media short-circuit.
    """
    if message_type != "image":
        return message_type, message_content

    try:
        from fashion_bot.utils.inbound_image_ocr import aanalyze_inbound_image

        gupshup_image_url = _extract_media_url_from_payload(data, trace_id, client_id)
        if not gupshup_image_url:
            log_with_trace_id(
                trace_id,
                "🔍 Inbound image carried no media URL; skipping analysis",
                "warning", sender_phone, client_id=client_id,
            )
            return message_type, message_content

        analysis = await aanalyze_inbound_image(
            image_url=gupshup_image_url,
            client_id=client_id,
            trace_id=trace_id,
        )
        if not analysis:
            return message_type, message_content

        caption = _extract_caption_from_payload(data, trace_id, client_id)
        if analysis.prefer_ocr():
            if caption:
                message_content = f"{caption}\n[Text from image]: {analysis.ocr_text}"
            else:
                message_content = f"[Text from image]: {analysis.ocr_text}"
            log_with_trace_id(
                trace_id,
                f"🔍 Inbound image OCR extracted {len(analysis.ocr_text)} chars, proceeding as text message",
                "info", sender_phone, client_id=client_id,
            )
            return "text", message_content

        if analysis.is_product_image and analysis.product_summary:
            # The description leads, because that is what the catalog can be
            # searched with. Text printed on the item rides along as context so
            # the agent can still use it, without it becoming the query.
            summary = analysis.product_summary
            ocr_context = analysis.ocr_context()
            if ocr_context:
                summary = f'{summary} (text on the item: "{ocr_context}")'
            if caption:
                message_content = f"{caption}\n[Product from image]: {summary}"
            else:
                message_content = f"[Product from image]: I'm looking for {summary}"
            log_with_trace_id(
                trace_id,
                f"🔍 Inbound image product detected: {analysis.product_summary}"
                f"{' (+text on item)' if ocr_context else ''}, proceeding as text message",
                "info", sender_phone, client_id=client_id,
            )
            return "text", message_content
    except Exception as ocr_err:
        log_with_trace_id(
            trace_id,
            f"🔍 Inbound image analysis attempt failed (non-fatal): {ocr_err}",
            "warning", sender_phone, client_id=client_id,
        )

    return message_type, message_content


async def _extract_runtime_message_content(data: dict, trace_id: str, client_id: str, sender_phone: str) -> tuple[str, str]:
    """Extract message_type + normalized text/caption for the runtime core flow."""
    payload = data.get("payload", {}) or {}
    message_type = payload.get("type", "") or ""
    if message_type == "text":
        return message_type, (payload.get("payload", {}) or {}).get("text", "") or ""
    if message_type in ("quick_reply", "button"):
        inner_payload = payload.get("payload", {}) or {}
        return message_type, inner_payload.get("text", "") or inner_payload.get("postbackText", "") or f"[{message_type}]"
    if message_type in MEDIA_MESSAGE_TYPES:
        # Voice notes are not transcribed. Whisper ran with language
        # auto-detection, and Hindi speech came back written in Urdu script, so
        # the transcript was misleading rather than useful — and it ran in front
        # of the reply. The stored recording is the better artefact: an agent
        # can play it. Audio therefore behaves exactly like image, video and
        # document.
        #
        # No storage work here either: the customer is waiting on the reply that
        # follows this call. Durable storage runs behind it, via
        # _astore_inbound_media_and_backfill.
        msg = _build_media_placeholder_text(message_type, payload)
        try:
            await publish_inbound_to_redis(sender_phone, f"{_MEDIA_EMOJI.get(message_type, '📎')} {msg}", sender="customer", client_id=client_id)
        except Exception:
            pass
        return message_type, msg
    return message_type, f"[{message_type or 'unknown'} message]"


_DEFAULT_IMAGE_UNSUPPORTED_REPLY = (
    "I'm not able to view images yet. "
    "Could you please type your message instead, "
    "or share the product link or order id? I'll be happy to help!"
)
_DEFAULT_VIDEO_UNSUPPORTED_REPLY = (
    "I'm not able to view videos yet. "
    "Could you please type your message instead, "
    "or share the product link or order id? I'll be happy to help!"
)
_DEFAULT_AUDIO_UNSUPPORTED_REPLY = (
    "I'm not able to listen to voice notes yet. "
    "Could you please type your message instead, "
    "or share the product link or order id? I'll be happy to help!"
)
_DEFAULT_FILE_UNSUPPORTED_REPLY = (
    "I'm not able to open documents yet. "
    "Could you please type your message instead, "
    "or share the product link or order id? I'll be happy to help!"
)
_MEDIA_CONFIG_KEYS = {
    "image": "media_unsupported_reply_image",
    "video": "media_unsupported_reply_video",
    "audio": "media_unsupported_reply_audio",
    "file": "media_unsupported_reply_file",
}
_MEDIA_DEFAULTS = {
    "image": _DEFAULT_IMAGE_UNSUPPORTED_REPLY,
    "video": _DEFAULT_VIDEO_UNSUPPORTED_REPLY,
    "audio": _DEFAULT_AUDIO_UNSUPPORTED_REPLY,
    "file": _DEFAULT_FILE_UNSUPPORTED_REPLY,
}


async def _get_media_unsupported_reply(message_type: str, client_id: Optional[str]) -> str:
    """Per-client media-unsupported reply, falling back to hardcoded default."""
    config_key = _MEDIA_CONFIG_KEYS.get(message_type)
    default = _MEDIA_DEFAULTS.get(message_type, "I'm unable to process that media type. Please type your message instead.")
    if not config_key or not client_id:
        return default
    return await aget_config(config_key, default=default, client_id=client_id) or default


async def _execute_runtime_turn_core(
    ctx: RuntimeContext,
    data: dict,
    source: str,
    sender_phone: str,
    trace_id: str,
    client_id: str,
) -> RuntimeResult:
    payload = data.get("payload", {}) or {}
    message_id = payload.get("id", "")
    sender_name = (payload.get("sender", {}) or {}).get("name", "Unknown")
    message_type, message_content = await _extract_runtime_message_content(data, trace_id, client_id, sender_phone)

    # What arrived, captured before the analysis can rewrite it. Whether the
    # attachment gets stored is decided by this, not by the post-analysis type:
    # an image whose text we recovered is still an image the agent dashboard
    # needs to show.
    inbound_media_type = message_type if message_type in MEDIA_MESSAGE_TYPES else None

    # ─── Inbound image analysis (OCR + product vision) ─────────────────────
    # Runs before the transcript row, the graph state and the trace are built,
    # so every one of them sees the same text. Rewriting after state creation
    # left the graph reading the "[Image message]" placeholder while the OCR
    # result went only to a trace label.
    message_type, message_content = await _aanalyze_inbound_image_message(
        data, message_type, message_content, trace_id, client_id, sender_phone,
    )

    log_with_trace_id(trace_id, f"Customer ({sender_phone}) wrote: {message_content}", "info", sender_phone, client_id=client_id)

    conv_id = None
    # Persist inbound transcript
    try:
        with traced_operation(
            "gupshup.io.store_inbound_transcript",
            metadata={"client_id": client_id, "phone_suffix": str(sender_phone)[-4:]},
            require_parent=True,
        ):
            existing_state = await aget_state_by_numbers(sender_phone, client_id) or {}
            conv_hint = existing_state.get("conversation_id")
            conversation_tags = existing_state.get("conversation_tags") or None
            conv_id = await astore_message_event_with_conversation_resolution(
                client_id=client_id,
                phone=sender_phone,
                sender="customer",
                text=message_content,
                channel_type="whatsapp",
                started_by="customer",
                tags=conversation_tags,
                conversation_id=conv_hint,
            )
            state, _ = await aget_or_create_state(sender_phone, client_id, None, None)
            state["conversation_id"] = conv_id
            state["client_id"] = client_id
            state["gupshup_source_phone_number"] = source
            await aupdate_state(sender_phone, client_id, state)
    except Exception as inbound_err:
        log_with_trace_id(trace_id, f"Postgres conversation store failed (inbound): {inbound_err}", "warning", sender_phone, client_id=client_id)
        report_error(
            "Postgres conversation store failed (inbound)",
            level='warning',
            exc_info=(type(inbound_err), inbound_err, inbound_err.__traceback__),
            phone=sender_phone,
            client_id=client_id,
            trace_id=trace_id,
        )

    # Durable media storage. Detached, so the reply is never behind the upload.
    # Scheduled here rather than inside the media short-circuit below, because a
    # successfully analysed image leaves that branch entirely — gating on the
    # rewritten type silently stopped storing exactly the images the analysis
    # worked on. `placeholder_text` is the text the transcript row was written
    # with, which the backfill matches on exactly.
    if inbound_media_type and conv_id:
        _create_detached_task(
            _astore_inbound_media_and_backfill(
                data=data,
                conversation_id=conv_id,
                placeholder_text=message_content,
                sender_phone=sender_phone,
                client_id=client_id,
                trace_id=trace_id,
            ),
            name=f"inbound-media-{trace_id}",
        )

    # ─── Media short-circuit ─────────────────────────────────────────────────
    # Images only reach here when analysis found nothing actionable (or was disabled/failed).
    if message_type in MEDIA_MESSAGE_TYPES:
        reply = await _get_media_unsupported_reply(message_type, client_id)
        log_with_trace_id(trace_id, f"📷 Media message ({message_type}), short-circuiting with guidance reply", "info", sender_phone, client_id=client_id)
        await send_message(sender_phone, reply, trace_id, gupshup_source=source, client_id=client_id)
        try:
            state_existing = await aget_state_by_numbers(sender_phone, client_id) or {}
            await astore_message_event_with_conversation_resolution(
                client_id=client_id,
                phone=sender_phone,
                sender="bot",
                text=reply,
                channel_type="whatsapp",
                conversation_id=state_existing.get("conversation_id"),
            )
        except Exception:
            pass
        return RuntimeResult(handled=True, reply_text=reply, state_snapshot=await aget_state_by_numbers(sender_phone, client_id))

    # Langsmith metadata
    if LANGSMITH_ENABLED:
        from langsmith import get_current_run_tree
        current_run = get_current_run_tree()
        if current_run:
            current_run.extra = {
                "trace_id": trace_id,
                "sender_phone": sender_phone,
                "sender_name": sender_name,
                "message_type": message_type,
                "message_id": message_id,
                "message_content": message_content[:100],
            }

    with traced_operation(
        "gupshup.call_main_bot",
        run_type="chain",
        metadata={"client_id": client_id, "phone_suffix": str(sender_phone)[-4:]},
        require_parent=True,
    ):
        reply, langsmith_trace_id = await call_main_bot(
            message_content,
            sender_phone,
            trace_id,
            client_id=client_id,
            gupshup_source=source,
        )

    # Agent-mode auto-switch after escalation is disabled — bot continues replying.

    # Persist bot transcript
    try:
        with traced_operation(
            "gupshup.io.store_bot_transcript",
            metadata={"client_id": client_id, "phone_suffix": str(sender_phone)[-4:]},
            require_parent=True,
        ):
            state_existing = await aget_state_by_numbers(sender_phone, client_id) or {}
            conv_id = state_existing.get("conversation_id")
            await astore_message_event_with_conversation_resolution(
                client_id=client_id,
                phone=sender_phone,
                sender="bot",
                text=reply or "",
                channel_type="whatsapp",
                conversation_id=conv_id,
                started_by="bot" if not conv_id else None,
                langsmith_id=langsmith_trace_id,
            )
    except Exception as bot_store_err:
        log_with_trace_id(trace_id, f"Postgres conversation store failed (bot reply): {bot_store_err}", "warning", sender_phone, client_id=client_id)
        report_error(
            "Postgres conversation store failed (bot reply)",
            level='warning',
            exc_info=(type(bot_store_err), bot_store_err, bot_store_err.__traceback__),
            phone=sender_phone,
            client_id=client_id,
            trace_id=trace_id,
        )

    await _publish_outbound_to_redis_for_ui(
        sender_phone=sender_phone,
        text=reply or "",
        sender="bot",
        client_id=client_id,
        trace_id=trace_id,
        operation_name="gupshup.io.publish_outbound_redis",
    )

    await _send_whatsapp_message_to_customer(
        sender_phone=sender_phone,
        text=reply,
        trace_id=trace_id,
        client_id=client_id,
        langsmith_trace_id=langsmith_trace_id,
        operation_name="gupshup.io.send_whatsapp_message",
    )

    try:
        from fashion_bot.async_tag_generator import generate_tags_async
        tag_state = await aget_state_by_numbers(sender_phone, client_id) or {}
        generate_tags_async(
            client_id=client_id,
            conversation_id=tag_state.get("conversation_id"),
            phone_number=sender_phone,
            user_message=message_content,
            bot_reply=reply or "",
            trace_id=trace_id,
        )
    except Exception as async_tag_err:
        log_with_trace_id(trace_id, f"⚠️ Failed to start async tagging: {async_tag_err}", "warning", sender_phone, client_id=client_id)

    snapshot = await aget_state_by_numbers(sender_phone, client_id) or {}
    log_with_trace_id(trace_id, f"Webhook completed. Response: {(reply or '')[:50]}... | LangSmith trace_id: {langsmith_trace_id}", "info", sender_phone, client_id=client_id)
    return RuntimeResult(handled=True, reply_text=reply or "", state_snapshot=snapshot)


def _is_state_only_stream_event(event: Dict[str, Any]) -> bool:
    event_type = str((event or {}).get("type") or "").strip().lower()
    if event_type in {"state_update", "context_update", "runtime_metrics", "metadata"}:
        return True
    if event_type:
        return False
    return bool((event or {}).get("state_snapshot") or (event or {}).get("result"))


def _extract_stream_text(event: Dict[str, Any], *, include_end: bool = True) -> str:
    event_type = str((event or {}).get("type") or "")
    if event_type == "queued":
        return str(event.get("message") or "").strip()
    if event_type == "token":
        return str(event.get("content") or "")
    if include_end and event_type == "end":
        return str(event.get("full_response") or "").strip()
    if event_type == "error":
        return str(event.get("message") or "").strip()
    return ""


def _attach_queue_debug_meta_to_state(state: Dict[str, Any], queue_debug_meta: Any) -> None:
    """Attach queued-processing debug metadata to runtime state when present."""
    if not isinstance(queue_debug_meta, dict):
        return
    queued_entries = queue_debug_meta.get("queued_entries") if isinstance(queue_debug_meta.get("queued_entries"), list) else []
    state["_queued_processing"] = True
    state["_queued_processing_count"] = int(queue_debug_meta.get("merged_count") or len(queued_entries))
    state["_queued_oldest_ts"] = _get_oldest_queued_ts(queued_entries)
    queued_trace_ids = [
        str((e or {}).get("trace_id"))
        for e in queued_entries
        if isinstance(e, dict) and (e or {}).get("trace_id")
    ]
    if queued_trace_ids:
        state["_queued_trace_ids"] = queued_trace_ids[:20]


def _log_queue_debug_dump(
    *,
    trace_id: str,
    sender_phone: str,
    client_id: str,
    queue_debug_meta: Any,
) -> None:
    """Log merged queue-debug context at queued redispatch start."""
    if not isinstance(queue_debug_meta, dict):
        return
    oldest_queued_ts = _get_oldest_queued_ts(queue_debug_meta.get("queued_entries"))
    log_with_trace_id(
        trace_id,
        "🚚 Queued processing run started: "
        f"scheduled_from={queue_debug_meta.get('scheduled_from_trace_id') or '-'}, "
        f"merged_trace_id={queue_debug_meta.get('merged_trace_id') or '-'}, "
        f"merged_count={queue_debug_meta.get('merged_count') or 0}, "
        f"oldest_queued_ts={oldest_queued_ts or '-'}, "
        f"considered={_format_queued_entries_for_log(queue_debug_meta.get('queued_entries'))}",
        "info",
        sender_phone,
        client_id=client_id,
    )


async def _publish_outbound_to_redis_for_ui(
    *,
    sender_phone: str,
    text: str,
    sender: str,
    client_id: str,
    trace_id: str,
    operation_name: str,
) -> None:
    """Best-effort outbound UI publish with local error handling."""
    try:
        with traced_operation(
            operation_name,
            metadata={"client_id": client_id, "phone_suffix": str(sender_phone)[-4:]},
            require_parent=True,
        ):
            await publish_outbound_to_redis(sender_phone, text or "", sender=sender, client_id=client_id)
    except Exception as pub_err:
        log_with_trace_id(
            trace_id,
            f"Redis outbound publish failed ({operation_name}): {pub_err}",
            "warning",
            sender_phone,
            client_id=client_id,
        )


async def _send_whatsapp_message_to_customer(
    *,
    sender_phone: str,
    text: str,
    trace_id: str,
    client_id: str,
    langsmith_trace_id: Optional[str],
    operation_name: str,
) -> None:
    """Send final customer-facing WhatsApp message with consistent tracing/logs."""
    with traced_operation(
        operation_name,
        metadata={"client_id": client_id, "phone_suffix": str(sender_phone)[-4:]},
        require_parent=True,
    ):
        effective_langsmith_id = langsmith_trace_id or trace_id
        if not langsmith_trace_id:
            log_with_trace_id(
                trace_id,
                "⚠️ Missing LangSmith run id for this turn; using local trace_id fallback in URL. "
                "Check LANGSMITH_WORKSPACE_ID / tracing upload settings.",
                "warning",
                sender_phone,
                client_id=client_id,
            )

        # Backstop for the shared guard in stream_graph_response: catches replies
        # that reach WhatsApp without passing through it (canned/orchestrator
        # sends). Idempotent, so double-guarding an agent reply is a no-op.
        guarded = sanitize_outbound_text(text or "")
        if guarded.blocked:
            log_with_trace_id(
                trace_id,
                f"🛡️ [outbound_guard] blocked {','.join(guarded.kinds)} before WhatsApp send: "
                f"{[v.matched for v in guarded.violations]}",
                "warning",
                sender_phone,
                client_id=client_id,
            )
            report_error(
                "Outbound guard stripped leaked content before WhatsApp send",
                level="warning",
                trace_id=trace_id,
                client_id=client_id,
                violations=[{"kind": v.kind, "matched": v.matched} for v in guarded.violations],
            )
            text = guarded.text
            if TOOL_CALL_DIRECTIVE in guarded.kinds:
                text = await enrich_after_tool_call_directive(text, client_id, violations=guarded.violations)

        langsmith_url = _build_langsmith_run_url(effective_langsmith_id)
        log_with_trace_id(
            trace_id,
            f"📤 Sending customer message: {(text or '')[:220]}... | langsmith_url={langsmith_url}",
            "info",
            sender_phone,
            client_id=client_id,
        )
        await send_message(sender_phone, text, trace_id, client_id=client_id)


async def _resolve_post_stream_state_for_ui(
    *,
    sender_phone: str,
    client_id: str,
    trace_id: str,
    runtime_state_snapshot: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Resolve state for post-stream actions with a single best-effort state read."""
    resolved: Dict[str, Any] = dict(runtime_state_snapshot or {})
    required_keys = {
        "conversation_id",
        "_langsmith_trace_id",
        "needs_escalation",
        "needs_human_agent",
        "parent_intent",
    }
    needs_fetch = (not resolved) or any(key not in resolved for key in required_keys)
    if not needs_fetch:
        return resolved
    try:
        persisted = await aget_state_by_numbers(sender_phone, client_id) or {}
        for key, value in persisted.items():
            resolved.setdefault(key, value)
    except Exception as state_err:
        log_with_trace_id(
            trace_id,
            f"⚠️ Failed to fetch persisted state for post-stream actions: {state_err}",
            "warning",
            sender_phone,
            client_id=client_id,
        )
    return resolved


def _should_switch_to_agent_mode_based_on_parent_intent(state_snapshot: Dict[str, Any]) -> bool:
    """Return True when state indicates escalation mode should be active."""
    if not isinstance(state_snapshot, dict):
        return False
    needs_escalation = bool(state_snapshot.get("needs_escalation") or state_snapshot.get("needs_human_agent"))
    parent_intent = str(state_snapshot.get("parent_intent") or "").strip().lower()
    return needs_escalation or parent_intent == "escalation"


async def _apply_agent_mode_switch_if_parent_intent_requires_escalation(
    *,
    sender_phone: str,
    client_id: str,
    trace_id: str,
    state_snapshot: Dict[str, Any],
) -> None:
    """No-op — agent-mode auto-switch after escalation is disabled.

    The bot continues replying on subsequent turns.  Manual dashboard
    toggle (``/dashboard/api/set-mode-agent``) still works.
    """
    return


async def _persist_final_bot_transcript_once_for_ui(
    *,
    sender_phone: str,
    client_id: str,
    trace_id: str,
    final_reply: str,
    state_snapshot: Dict[str, Any],
) -> None:
    """Persist final bot transcript once per turn using provided state snapshot."""
    try:
        with traced_operation(
            "gupshup.io.store_bot_transcript_stream",
            metadata={"client_id": client_id, "phone_suffix": str(sender_phone)[-4:]},
            require_parent=True,
        ):
            conv_id = state_snapshot.get("conversation_id")
            await astore_message_event_with_conversation_resolution(
                client_id=client_id,
                phone=sender_phone,
                sender="bot",
                text=final_reply or "",
                channel_type="whatsapp",
                conversation_id=conv_id,
                started_by="bot" if not conv_id else None,
                langsmith_id=state_snapshot.get("_langsmith_trace_id") or trace_id,
            )
    except Exception as bot_store_err:
        log_with_trace_id(
            trace_id,
            f"Postgres conversation store failed (bot reply): {bot_store_err}",
            "warning",
            sender_phone,
            client_id=client_id,
        )
        report_error(
            "Postgres conversation store failed (bot reply stream)",
            level='warning',
            exc_info=(type(bot_store_err), bot_store_err, bot_store_err.__traceback__),
            phone=sender_phone,
            client_id=client_id,
            trace_id=trace_id,
        )


def _start_async_tag_generation_from_final_response_for_ui(
    *,
    data: Dict[str, Any],
    sender_phone: str,
    client_id: str,
    trace_id: str,
    final_reply: str,
    state_snapshot: Dict[str, Any],
) -> None:
    """Fire-and-forget async tag generation using existing state snapshot."""
    try:
        from fashion_bot.async_tag_generator import generate_tags_async

        payload_message = (data.get("payload", {}) or {}).get("payload", {})
        user_message = payload_message.get("text", "") or "[non-text message]"
        generate_tags_async(
            client_id=client_id,
            conversation_id=state_snapshot.get("conversation_id"),
            phone_number=sender_phone,
            user_message=user_message,
            bot_reply=final_reply or "",
            trace_id=trace_id,
        )
    except Exception as async_tag_err:
        log_with_trace_id(
            trace_id,
            f"⚠️ Failed to start async tagging: {async_tag_err}",
            "warning",
            sender_phone,
            client_id=client_id,
        )


async def _consume_runtime_stream_events(
    *,
    runtime: ConversationRuntime,
    client_id: str,
    runtime_lock_user_id: str,
    data: Dict[str, Any],
    source: str,
    sender_phone: str,
    trace_id: str,
    trace_run: Any = None,
) -> tuple[bool, str, Dict[str, Any]]:
    """Consume runtime stream events and return normalized turn outcome."""
    queued = False
    final_reply = ""
    final_result: Dict[str, Any] = {}

    async for event in runtime.run_turn_stream(
        channel="whatsapp",
        client_id=client_id,
        user_id=runtime_lock_user_id,
        inbound_payload=data,
        execute_stream_fn=lambda ctx: _execute_turn_run_graph_and_update_state_for_gupshup(
            ctx,
            data,
            source,
            sender_phone,
            trace_id,
            client_id,
            trace_run=trace_run,
        ),
        trace_id=trace_id,
    ):
        if _is_state_only_stream_event(event):
            continue

        event_type = str((event or {}).get("type") or "")

        if event_type == "queued":
            queued = True
            queued_text = _extract_stream_text(event)
            if queued_text:
                # Queue notice is internal-only; never send to end customer.
                log_with_trace_id(
                    trace_id,
                    f"⏳ Internal queued notice suppressed for customer: {queued_text}",
                    "info",
                    sender_phone,
                    client_id=client_id,
                )
            continue

        if event_type == "token":
            token_txt = _extract_stream_text(event, include_end=False)
            if token_txt:
                final_reply += token_txt
            continue

        if event_type == "end":
            final_reply = _extract_stream_text(event) or final_reply
            if isinstance(event.get("result"), dict):
                final_result = event.get("result") or {}
            continue

        if event_type == "error":
            error_text = _extract_stream_text(event, include_end=False)
            if error_text:
                final_reply = error_text

    return queued, final_reply, final_result


def _format_queued_entries_for_log(entries: Any, max_items: int = 10) -> str:
    rows = entries if isinstance(entries, list) else []
    if not rows:
        return "none"
    parts = []
    for idx, entry in enumerate(rows[:max_items], start=1):
        if not isinstance(entry, dict):
            continue
        preview = str(entry.get("preview") or "").replace("\n", " ").strip()
        if len(preview) > 120:
            preview = f"{preview[:120]}..."
        parts.append(
            f"{idx}|{entry.get('trace_id') or '-'}|{entry.get('chars') or 0}c|{preview}"
        )
    if len(rows) > max_items:
        parts.append(f"...(+{len(rows) - max_items} more)")
    return " || ".join(parts) if parts else "none"


def _get_oldest_queued_ts(entries: Any) -> Optional[str]:
    """Return oldest queue entry timestamp (ISO-ish string) from debug entries."""
    rows = entries if isinstance(entries, list) else []
    parsed: list[tuple[datetime.datetime, str]] = []
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        ts_raw = str(entry.get("ts") or "").strip()
        if not ts_raw:
            continue
        try:
            dt = datetime.datetime.fromisoformat(ts_raw)
            parsed.append((dt, ts_raw))
        except Exception:
            continue
    if not parsed:
        return None
    parsed.sort(key=lambda x: x[0])
    return parsed[0][1]


async def _execute_turn_run_graph_and_update_state_for_gupshup(
    ctx: RuntimeContext,
    data: dict,
    source: str,
    sender_phone: str,
    trace_id: str,
    client_id: str,
    trace_run: Any = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Run Gupshup graph stream and persist state before/after stream end."""
    payload = data.get("payload", {}) or {}
    message_id = payload.get("id", "")
    sender_name = (payload.get("sender", {}) or {}).get("name", "Unknown")
    message_type, message_content = await _extract_runtime_message_content(data, trace_id, client_id, sender_phone)
    queue_debug_meta = data.get("_runtime_queue_debug") if isinstance(data, dict) else None

    # What arrived, captured before the analysis can rewrite it. Whether the
    # attachment gets stored is decided by this, not by the post-analysis type:
    # an image whose text we recovered is still an image the agent dashboard
    # needs to show.
    inbound_media_type = message_type if message_type in MEDIA_MESSAGE_TYPES else None

    # ─── Inbound image analysis (OCR + product vision) ─────────────────────
    # Runs before the transcript row, the graph state and the trace are built,
    # so every one of them sees the same text. Rewriting after state creation
    # left the graph reading the "[Image message]" placeholder while the OCR
    # result went only to a trace label.
    message_type, message_content = await _aanalyze_inbound_image_message(
        data, message_type, message_content, trace_id, client_id, sender_phone,
    )

    log_with_trace_id(trace_id, f"Customer ({sender_phone}) wrote: {message_content}", "info", sender_phone, client_id=client_id)

    state: Dict[str, Any] = {}
    conv_id = None
    # Persist inbound transcript and initialize state for graph streaming.
    try:
        with traced_operation(
            "gupshup.io.store_inbound_transcript_stream",
            metadata={"client_id": client_id, "phone_suffix": str(sender_phone)[-4:]},
            require_parent=True,
        ):
            existing_state = await aget_state_by_numbers(sender_phone, client_id) or {}
            conv_hint = existing_state.get("conversation_id")
            conversation_tags = existing_state.get("conversation_tags") or None
            conv_id = await astore_message_event_with_conversation_resolution(
                client_id=client_id,
                phone=sender_phone,
                sender="customer",
                text=message_content,
                channel_type="whatsapp",
                started_by="customer",
                tags=conversation_tags,
                conversation_id=conv_hint,
            )
            state, _ = await aget_or_create_state(sender_phone, client_id, message_content, None)
            state["conversation_id"] = conv_id
            state["client_id"] = client_id
            state["gupshup_source_phone_number"] = source
            state["trace_id"] = trace_id
            state["_skip_final_answer"] = _should_skip_final_answer_for_gupshup()
            state["_streaming_enabled"] = True
            state["_langsmith_service"] = "gupshup"
            _attach_queue_debug_meta_to_state(state, queue_debug_meta)
            await aupdate_state(sender_phone, client_id, state)
    except Exception as inbound_err:
        log_with_trace_id(trace_id, f"Postgres conversation store failed (inbound): {inbound_err}", "warning", sender_phone, client_id=client_id)
        report_error(
            "Postgres conversation store failed (inbound stream)",
            level='warning',
            exc_info=(type(inbound_err), inbound_err, inbound_err.__traceback__),
            phone=sender_phone,
            client_id=client_id,
            trace_id=trace_id,
        )
        state, _ = await aget_or_create_state(sender_phone, client_id, message_content, None)
        state["trace_id"] = trace_id
        state["_skip_final_answer"] = _should_skip_final_answer_for_gupshup()
        state["_streaming_enabled"] = True
        state["_langsmith_service"] = "gupshup"
        _attach_queue_debug_meta_to_state(state, queue_debug_meta)

    # Durable media storage. Detached, so the reply is never behind the upload.
    # Scheduled here rather than inside the media short-circuit below, because a
    # successfully analysed image leaves that branch entirely — gating on the
    # rewritten type silently stopped storing exactly the images the analysis
    # worked on. `placeholder_text` is the text the transcript row was written
    # with, which the backfill matches on exactly.
    if inbound_media_type and conv_id:
        _create_detached_task(
            _astore_inbound_media_and_backfill(
                data=data,
                conversation_id=conv_id,
                placeholder_text=message_content,
                sender_phone=sender_phone,
                client_id=client_id,
                trace_id=trace_id,
            ),
            name=f"inbound-media-{trace_id}",
        )

    # LangSmith metadata for top-level message
    if LANGSMITH_ENABLED:
        from langsmith import get_current_run_tree
        current_run = get_current_run_tree()
        if current_run:
            current_run.extra = {
                "trace_id": trace_id,
                "sender_phone": sender_phone,
                "sender_name": sender_name,
                "message_type": message_type,
                "message_id": message_id,
                "message_content": message_content[:100],
                "streaming_turn": True,
            }

    from fashion_bot.client_context import set_client_id
    from fashion_bot.core.streaming_service import stream_graph_response

    set_client_id(state.get("client_id") or client_id)
    final_reply = ""
    yielded_end = False
    if trace_run is not None and hasattr(trace_run, "id"):
        state["_langsmith_trace_id"] = str(trace_run.id)
    _replace_trace_io(
        trace_run,
        inputs={
            "input": str(message_content or "")[:2000],
        },
    )

    # ─── Media short-circuit ─────────────────────────────────────────────────
    # Video, audio, and file can never be acted on. Images only reach here
    # when analysis found nothing actionable (or was disabled/failed) — fall through
    # to the canned media-unsupported reply.
    if message_type in MEDIA_MESSAGE_TYPES:
        reply = await _get_media_unsupported_reply(message_type, client_id)
        log_with_trace_id(trace_id, f"📷 Media message ({message_type}), short-circuiting with guidance reply", "info", sender_phone, client_id=client_id)
        _replace_trace_io(trace_run, outputs={"output": reply, "short_circuit": "media"})
        snapshot = await aget_state_by_numbers(sender_phone, client_id) or {}
        yield {
            "type": "end",
            "full_response": reply,
            "result": snapshot,
            "reply_text": reply,
            "state_snapshot": snapshot,
        }
        return

    async for event in stream_graph_response(
        state,
        message_content,
        client_id,
        langsmith_service="gupshup",
        trace_graph_internally=False,
    ):
        event_type = str((event or {}).get("type") or "")
        if event_type == "token":
            final_reply += _extract_stream_text(event, include_end=False)
            yield event
            continue

        if event_type == "end":
            yielded_end = True
            final_reply = str(event.get("full_response") or final_reply or "").strip()
            result_obj = event.get("result")
            if isinstance(result_obj, dict):
                existing_state = await aget_state_by_numbers(sender_phone, client_id) or {}
                existing_conv_id = existing_state.get("conversation_id")
                if existing_conv_id:
                    result_obj["conversation_id"] = existing_conv_id
                try:
                    with traced_operation(
                        "gupshup.state_update_result_stream",
                        metadata={"client_id": client_id, "phone_suffix": str(sender_phone)[-4:]},
                        require_parent=True,
                    ):
                        await aupdate_state(sender_phone, client_id, result_obj)
                except Exception as update_err:
                    log_with_trace_id(trace_id, f"State update failed after stream end: {update_err}", "warning", sender_phone, client_id=client_id)
                    report_error(
                        "State update failed after stream end",
                        level='warning',
                        exc_info=(type(update_err), update_err, update_err.__traceback__),
                        phone=sender_phone,
                        client_id=client_id,
                        trace_id=trace_id,
                    )

                end_event = dict(event)
                end_event["reply_text"] = final_reply
                end_event["state_snapshot"] = result_obj
                yield end_event
                continue

            snapshot = await aget_state_by_numbers(sender_phone, client_id) or {}
            yield {
                "type": "end",
                "full_response": final_reply,
                "result": snapshot,
                "reply_text": final_reply,
                "state_snapshot": snapshot,
            }
            continue

        yield event

    if not yielded_end:
        snapshot = await aget_state_by_numbers(sender_phone, client_id) or {}
        yield {
            "type": "end",
            "full_response": final_reply,
            "result": snapshot,
            "reply_text": final_reply,
            "state_snapshot": snapshot,
        }

        # 📊 Cancellation aversion tracking — fire-and-forget, never blocks the reply
        _create_detached_task(
            _track_cancellation_aversion(
                user_message=message_content,
                bot_response=final_reply or "",
                phone_number=sender_phone,
                client_id=client_id,
                trace_id=trace_id,
            ),
            name=f"cancel-aversion-{trace_id}",
        )

        # Agent-mode auto-switch after escalation is disabled — bot continues replying.


async def process_webhook_payload(data: dict, trace_id: str, client_id: str):
    """Runs heavy processing logic in the background through centralized runtime."""
    import random
    try:
        log_with_trace_id(trace_id, "🚀 Starting background processing", "info", client_id=client_id)
        if random.randint(1, 100) == 1:
            cleanup_expired_states()

        event = data.get('event') or data.get('payload', {}).get('event') or data.get("payload", {}).get("type")
        if event and str(event).lower() in {"failed", "ack", "sent", "delivered", "read", "optin", "optout"}:
            log_with_trace_id(trace_id, f"Handling Gupshup event: {event}", "info", client_id=client_id)
            return

        payload = data.get("payload", {}) or {}
        source = payload.get("source", "")
        incoming_app_name = data.get("app", "")
        sender_phone = (payload.get("sender", {}) or {}).get("phone", "") or source
        queue_debug_meta = data.get("_runtime_queue_debug") if isinstance(data, dict) else None
        _log_queue_debug_dump(
            trace_id=trace_id,
            sender_phone=sender_phone,
            client_id=client_id,
            queue_debug_meta=queue_debug_meta,
        )

        from fashion_bot.config_manager import (
            aget_client_id_by_enterprise_app,
            avalidate_gupshup_app_for_client,
            avalidate_gupshup_app_name,
        )
        is_enterprise_app = (
            bool(incoming_app_name)
            and await aget_client_id_by_enterprise_app(incoming_app_name) == client_id
        )
        app_is_valid = (
            await avalidate_gupshup_app_for_client(client_id, incoming_app_name)
            if is_enterprise_app
            else await avalidate_gupshup_app_name(source, incoming_app_name)
        )
        if not data or not app_is_valid:
            log_with_trace_id(trace_id, f"Invalid webhook data or app name mismatch for source {source}", "error", client_id=client_id)
            return

        runtime = _get_conversation_runtime()
        runtime_lock_user_id = extract_10_digit_phone(sender_phone or "")
        with traced_operation(
            "gupshup.graph_invoke_stream",
            run_type="chain",
            metadata={"client_id": client_id, "phone_suffix": str(sender_phone)[-4:]},
            require_parent=False,
        ) as turn_run:
            queued, final_reply, final_result = await _consume_runtime_stream_events(
                runtime=runtime,
                client_id=client_id,
                runtime_lock_user_id=runtime_lock_user_id,
                data=data,
                source=source,
                sender_phone=sender_phone,
                trace_id=trace_id,
                trace_run=turn_run,
            )

            if queued:
                _replace_trace_io(
                    turn_run,
                    outputs={
                        "output": "",
                        "queued": True,
                    },
                )
                log_with_trace_id(trace_id, f"⏳ In-flight request exists. Queued message for merged follow-up (phone={sender_phone})", "info", sender_phone, client_id=client_id)
                return

            if not final_reply and isinstance(final_result, dict):
                try:
                    final_reply = ConversationRuntime._extract_reply_from_result(final_result)
                except Exception:
                    final_reply = ""
            if not final_reply:
                final_reply = "I apologize, but I couldn't generate a response. Please try again."

            state_snapshot = await _resolve_post_stream_state_for_ui(
                sender_phone=sender_phone,
                client_id=client_id,
                trace_id=trace_id,
                runtime_state_snapshot=final_result if isinstance(final_result, dict) else None,
            )
            _replace_trace_io(
                turn_run,
                outputs={
                    "output": str(final_reply or "")[:2000],
                    "queued": False,
                },
            )
            await _apply_agent_mode_switch_if_parent_intent_requires_escalation(
                sender_phone=sender_phone,
                client_id=client_id,
                trace_id=trace_id,
                state_snapshot=state_snapshot,
            )
            await _send_whatsapp_message_to_customer(
                sender_phone=sender_phone,
                text=final_reply,
                trace_id=trace_id,
                client_id=client_id,
                langsmith_trace_id=state_snapshot.get("_langsmith_trace_id"),
                operation_name="gupshup.io.send_whatsapp_message_stream",
            )
            await _persist_final_bot_transcript_once_for_ui(
                sender_phone=sender_phone,
                client_id=client_id,
                trace_id=trace_id,
                final_reply=final_reply,
                state_snapshot=state_snapshot,
            )

            await _publish_outbound_to_redis_for_ui(
                sender_phone=sender_phone,
                text=final_reply or "",
                sender="bot",
                client_id=client_id,
                trace_id=trace_id,
                operation_name="gupshup.io.publish_outbound_redis_stream",
            )

            _start_async_tag_generation_from_final_response_for_ui(
                data=data,
                sender_phone=sender_phone,
                client_id=client_id,
                trace_id=trace_id,
                final_reply=final_reply,
                state_snapshot=state_snapshot,
            )

            log_with_trace_id(
                trace_id,
                f"✅ Runtime stream finished: final_chars={len(final_reply)}",
                "info",
                sender_phone,
                client_id=client_id,
            )
    except Exception as e:
        # Hard-failure reporting is centralized in ConversationRuntime for turn execution.
        # Keep webhook-level catch as logging-only to avoid duplicate Rollbar events.
        log_with_trace_id(trace_id, f"Gupshup webhook processing error: {e}", "error", client_id=client_id)



@router.get("/gupshup/webhook/health")
async def webhook_health():
    """Gupshup webhook health check"""
    cache_stats = get_cache_stats()
    
    # Get Langsmith status from centralized config
    langsmith_status = LANGSMITH_CONFIG.get_status()
    
    return {
        "status": "ok", 
        "service": "gupshup-webhook",
        "app_name": APP_NAME,
        "configured": bool(GUPSHUP_API_KEY),
        "cache_stats": cache_stats,
        "langsmith_tracing": langsmith_status
    }

@router.post("/gupshup/send")
async def send_message_endpoint(request: Request):
    """Direct message sending endpoint for testing"""
    try:
        data = await request.json()
        to = data.get('to')
        message = data.get('message')
        trace_id = data.get('trace_id') or generate_trace_id()
        set_trace_id(trace_id)
        gupshup_source = data.get('gupshup_source')  # Optional: for multi-client support (DEPRECATED)
        client_id = data.get('client_id')  # Preferred: for multi-client support
        if not to or not message:
            return {"error": "Missing 'to' or 'message' parameters"}
        result = await send_message(to, message, trace_id=trace_id, gupshup_source=gupshup_source, client_id=client_id)
        return {"status": "ok", "result": result, "trace_id": trace_id}
    except Exception as e:
        report_error(
            "Gupshup send endpoint error",
            level='error',
            exc_info=(type(e), e, e.__traceback__),
        )
        return {"error": str(e)}

@router.get("/")
async def root():
    return {"status": "Gupshup Webhook API is running", "message": "Use POST /gupshup/webhook to receive messages"}

@router.get("/health")
async def health_check():
    return {"status": "healthy", "service": "gupshup-webhook"}
