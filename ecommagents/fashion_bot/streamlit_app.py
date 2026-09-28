#!/usr/bin/env python3
"""
Streamlit UI for testing the Meta Customer Support Graph.
Provides an interactive web interface to test the customer support system.
"""

import streamlit as st
import logging
import asyncio
import nest_asyncio

# Patch asyncio so run_until_complete() works even when Streamlit's own
# event loop is already active (avoids intermittent "This event loop is
# already running" RuntimeError).
nest_asyncio.apply()

# Reuse a single event loop across Streamlit re-runs so that cached async
# singletons (DB pool, Redis clients) remain valid.  asyncio.run() would
# create and *destroy* a new loop every interaction, invalidating them.
# NOTE: module-level globals are reset on every Streamlit re-run (the main
# script is re-executed via exec()).  st.session_state survives re-runs.


def _run_async(coro):
    """Run a coroutine on a persistent event loop (survives Streamlit re-runs)."""
    loop = st.session_state.get("_event_loop")
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        st.session_state["_event_loop"] = loop
    return loop.run_until_complete(coro)
import socket
import time
from typing import Dict, Any, List, Tuple, Optional
from fashion_bot.env_loader import (
    bootstrap_environment,
    get_bool,
    get_env,
    require_settings,
)

settings = bootstrap_environment()
if settings.environment == "development":
    if settings.env_file_loaded:
        print(f"🔧 Development mode: Loaded .env from {settings.env_file_path}")
    else:
        print("🔧 Development mode: No .env file found, using process environment")
else:
    print(f"🚀 {settings.environment.title()} mode: Using system environment variables")
require_settings("DATABASE_URL")
from langchain_core.messages import HumanMessage, AIMessage

from fashion_bot.graph_context_meta import graph

from fashion_bot.schema import SupportState
from fashion_bot.state_cache import aget_or_create_state, aupdate_state, aget_state_by_numbers, get_unified_cache
from fashion_bot.database_manager import get_postgres_connection
from fashion_bot.core.runtime_presets import build_single_flight_failopen_runtime
from fashion_bot.core.message_persistence import ensure_assistant_message_for_skip_final
from fashion_bot.core.streaming_service import stream_graph_response
from fashion_bot.rollbar_config import report_error
from fashion_bot.history.conversation_handler import (
    astore_message_event_with_conversation_resolution,
)
import json
from datetime import datetime
import sys

DEFAULT_STREAMLIT_CLIENT_ID = get_env("STREAMLIT_CLIENT_ID")
_ENV_CLIENT_ID = DEFAULT_STREAMLIT_CLIENT_ID

def _should_skip_final_answer_for_streamlit() -> bool:
    """Resolve per-channel final_answer bypass flag for Streamlit UI."""
    return get_bool("SKIP_FINAL_ANSWER_STREAMLIT", get_bool("SKIP_FINAL_ANSWER", True))

# Configure logging to show in terminal
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),  # Explicitly use stdout
    ],
    force=True  # Force reconfiguration
)

# Set up root logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

# Ensure all fashion_bot loggers show their output
logging.getLogger('fashion_bot').setLevel(logging.INFO)
logging.getLogger('meta_graph').setLevel(logging.INFO)
logging.getLogger('meta_nodes').setLevel(logging.INFO)

# Create logger for this module
logger = logging.getLogger(__name__)
_streamlit_runtime = None


def _get_streamlit_runtime():
    global _streamlit_runtime
    if _streamlit_runtime is None:
        cache = get_unified_cache()
        _streamlit_runtime = build_single_flight_failopen_runtime(
            log_fn=lambda trace_id, message, level="info", *_args, **_kwargs: getattr(
                logger,
                level if level in ("info", "warning", "error", "debug") else "info",
            )(f"[TRACE_ID={trace_id}] {message}"),
            get_redis_client_fn=cache._get_async_redis_client,
            redis_guard=cache._redis_guard,
        )
    return _streamlit_runtime


def _list_clients() -> List[Dict[str, str]]:
    """Fetch available clients for Streamlit testing dropdown."""
    try:
        with get_postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        c.id::text AS client_id,
                        COALESCE(
                            NULLIF(to_jsonb(c)->>'name', ''),
                            NULLIF(to_jsonb(c)->>'client_name', ''),
                            NULLIF(to_jsonb(c)->>'display_name', ''),
                            NULLIF(to_jsonb(c)->>'shopify_domain_name', ''),
                            NULLIF(to_jsonb(c)->>'gupshup_source_number', ''),
                            c.id::text
                        ) AS client_name
                    FROM clients c
                    ORDER BY 2 ASC
                    """
                )
                rows = cur.fetchall() or []
                clients: List[Dict[str, str]] = []
                for row in rows:
                    cid = row.get("client_id")
                    cname = row.get("client_name") or cid
                    if cid:
                        clients.append({"id": str(cid), "name": str(cname)})
                return clients
    except Exception as exc:
        logger.warning(f"⚠️ Could not load clients list for Streamlit dropdown: {exc}")
        try:
            db_url = get_env("DATABASE_URL") or ""
            if "@" in db_url and "://" in db_url:
                host_part = db_url.split("@", 1)[1]
                host = host_part.split("/", 1)[0].split("?", 1)[0].split(":", 1)[0]
                socket.getaddrinfo(host, 5432)
                logger.warning(f"🔎 DB DNS check: host '{host}' resolved; likely connectivity/auth issue")
            else:
                logger.warning("🔎 DB DNS check skipped: DATABASE_URL host not parseable")
        except Exception as dns_exc:
            logger.warning(f"🔎 DB DNS check failed: {dns_exc}")
        return []


def _effective_client_id() -> str | None:
    """
    Resolve client_id with strict priority:
    1) Sidebar-selected client_id in session
    2) Existing support_state.client_id
    3) STREAMLIT_CLIENT_ID from env
    """
    selected = _active_client_id()
    if selected:
        return selected
    state_client = (st.session_state.get("support_state") or {}).get("client_id")
    if state_client:
        return state_client
    return DEFAULT_STREAMLIT_CLIENT_ID or None


def _load_or_create_state_for(phone_number: str, client_id: str | None) -> None:
    resolved_client_id = client_id or _effective_client_id()
    existing_state = _run_async(aget_state_by_numbers(phone_number, resolved_client_id))
    if existing_state:
        if resolved_client_id:
            existing_state["client_id"] = resolved_client_id
        else:
            resolved_client_id = existing_state.get("client_id")
        st.session_state.support_state = existing_state
        st.session_state.selected_client_id = resolved_client_id
        if existing_state.get("messages"):
            st.session_state.messages = [
                {
                    "role": "user" if hasattr(m, 'content') and m.__class__.__name__ == 'HumanMessage' else "assistant",
                    "content": m.content if hasattr(m, 'content') else str(m),
                }
                for m in existing_state.get("messages", [])
            ]
        else:
            st.session_state.messages = []
        logger.info(f"[REDIS_STATE] Loaded existing state for phone={phone_number}, client_id={resolved_client_id}")
        return

    state, _ = _run_async(aget_or_create_state(
        from_phone_number=phone_number,
        tenant_id=resolved_client_id,
    ))
    if resolved_client_id:
        state["client_id"] = resolved_client_id
    st.session_state.support_state = state
    st.session_state.selected_client_id = resolved_client_id
    st.session_state.messages = []
    logger.info(f"[REDIS_STATE] Created new state for phone={phone_number}, client_id={resolved_client_id}")

def _active_client_id() -> str:
    """Return the client_id currently selected in the UI (falls back to env var)."""
    return st.session_state.get("selected_client_id") or _ENV_CLIENT_ID


def initialize_session_state():
    """Initialize session state variables."""
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "selected_client_id" not in st.session_state:
        st.session_state.selected_client_id = DEFAULT_STREAMLIT_CLIENT_ID or None
    
    # Default phone number for testing
    default_phone = "9012345678"
    client_id = _active_client_id()

    if "support_state" not in st.session_state:
        _load_or_create_state_for(default_phone, _active_client_id())
    
    if "thread_id" not in st.session_state:
        st.session_state.thread_id = f"streamlit_thread_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        logger.info(f"📊 Session initialized with thread_id: {st.session_state.thread_id}")

def process_user_message(user_input: str) -> str:
    """Process user input through the customer support graph."""
    try:
        resolved_client_id = _effective_client_id()
        if not resolved_client_id:
            msg = (
                "No client_id resolved. Select a client in sidebar or enter one manually. "
                "Graph prompt/config lookups require client_id."
            )
            logger.error(f"❌ {msg}")
            st.error(msg)
            return msg
        logger.info(f"💬 User input: {user_input}")
        st.session_state.support_state["client_id"] = resolved_client_id
        st.session_state.selected_client_id = resolved_client_id
        st.session_state.support_state["_skip_final_answer"] = _should_skip_final_answer_for_streamlit()
        logger.info(f"🏢 Streamlit effective client_id={resolved_client_id}")
        logger.info(
            f"⚙️ Streamlit _skip_final_answer={st.session_state.support_state.get('_skip_final_answer')} "
            f"(env SKIP_FINAL_ANSWER_STREAMLIT/SKIP_FINAL_ANSWER)"
        )
        
        # Ensure client_id is current in state and ContextVar
        client_id = _active_client_id()
        st.session_state.support_state["client_id"] = client_id
        from fashion_bot.client_context import set_client_id
        set_client_id(client_id)

        # Add user message to state
        st.session_state.support_state["messages"] = st.session_state.support_state.get("messages", []) + [HumanMessage(content=user_input)]
        
        trace_id = st.session_state.get("thread_id", f"streamlit_{datetime.now().strftime('%Y%m%d%H%M%S')}")
        runtime = _get_streamlit_runtime()
        response_placeholder = st.empty()
        metric_placeholder = st.empty()

        with st.spinner("Processing your request..."):
            logger.info("🔄 Invoking runtime.run_turn_stream()...")
            queued, streamed_reply, result, ttft_ms = _run_async(
                _run_streamlit_turn_stream(
                    runtime=runtime,
                    client_id=resolved_client_id or "streamlit",
                    user_id=st.session_state.support_state.get("phone_number", "9012345678"),
                    user_input=user_input,
                    trace_id=trace_id,
                    on_update=lambda txt: response_placeholder.markdown(txt or " "),
                    on_ttft=lambda ms: metric_placeholder.caption(f"TTFT: {ms} ms"),
                )
            )
            logger.info("✅ Stream turn completed")
            if ttft_ms is not None:
                logger.info(f"⏱️ Streamlit TTFT={ttft_ms}ms")

        if queued:
            queued_msg = streamed_reply or "I am still processing your previous message. This message has been queued."
            logger.info(f"⏳ Turn queued by single-flight (suppressed for customer): {queued_msg}")
            return ""
        
        # Update state with result
        _apply_stream_result_to_streamlit_support_state(
            support_state=st.session_state.support_state,
            result=result,
        )

        # Persist updated state to Redis
        _persist_streamlit_state_for_ui(
            client_id=resolved_client_id,
            support_state=st.session_state.support_state,
        )

        # Persist messages to conversations/messages DB tables
        phone_number = st.session_state.support_state.get("phone_number", "9012345678")
        conv_id = st.session_state.support_state.get("conversation_id")
        reply_text = _extract_streamlit_reply_text(result)
        try:
            conv_id = _run_async(astore_message_event_with_conversation_resolution(
                client_id=resolved_client_id,
                phone=phone_number,
                sender="customer",
                text=user_input,
                channel_type="whatsapp",
                started_by="customer",
                conversation_id=conv_id,
            ))
            if conv_id:
                st.session_state.support_state["conversation_id"] = conv_id
            _run_async(astore_message_event_with_conversation_resolution(
                client_id=resolved_client_id,
                phone=phone_number,
                sender="bot",
                text=reply_text or "",
                channel_type="whatsapp",
                conversation_id=conv_id,
            ))
        except Exception as db_err:
            logger.warning(f"⚠️ Failed to persist streamlit messages to DB: {db_err}")

        return reply_text
        
    except Exception as e:
        st.error(f"Error processing request: {str(e)}")
        logger.error(f"❌ Error in process_user_message: {str(e)}")
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")
        has_session_state = hasattr(st, "session_state")
        support_state = st.session_state.get("support_state", {}) if has_session_state else {}
        selected_client_id = st.session_state.get("selected_client_id") if has_session_state else None
        thread_id = st.session_state.get("thread_id") if has_session_state else None
        report_error(
            "Error in streamlit process_user_message",
            level="error",
            exc_info=(type(e), e, e.__traceback__),
            trace_id=thread_id,
            client_id=support_state.get("client_id") or selected_client_id,
            channel="streamlit",
        )
        return f"I'm sorry, there was an error processing your request: {str(e)}. Please try again."


def _is_state_only_stream_event(event: Dict[str, Any]) -> bool:
    """Guardrail: never render internal state/meta events as customer-visible text."""
    event_type = str((event or {}).get("type") or "").strip().lower()
    if event_type in {"state_update", "context_update", "runtime_metrics", "metadata"}:
        return True
    if event_type:
        return False
    # Untyped events with state payload are internal by default.
    return bool((event or {}).get("state_snapshot") or (event or {}).get("result"))


def _persist_streamlit_state_for_ui(
    *,
    client_id: str,
    support_state: Dict[str, Any],
) -> None:
    """Persist streamlit support state with centralized logging."""
    phone_number = support_state.get("phone_number", "9012345678")
    _run_async(aupdate_state(phone_number, client_id, support_state))
    logger.info(f"[REDIS_STATE] Saved state to Redis for phone {phone_number}, client_id={client_id}")


def _extract_streamlit_reply_text(result: Dict[str, Any]) -> str:
    """Best-effort final reply extraction from graph result.

    Only returns content from AIMessage objects to prevent echoing
    a user's HumanMessage back as the bot reply.
    """
    if "messages" in result and result["messages"]:
        final_message = result["messages"][-1]
        if isinstance(final_message, AIMessage):
            response = str(final_message.content or "")
            logger.info(f"📤 Response: {response[:100]}...")
            return response
        logger.warning("⚠️ Last message is not AIMessage, falling back to customer_message")
    if "customer_message" in result:
        response = str(result.get("customer_message") or "")
        logger.info(f"📤 Customer message: {response[:100]}...")
        return response
    logger.warning("⚠️ No response generated")
    return "I apologize, but I couldn't generate a response. Please try again."


def _apply_stream_result_to_streamlit_support_state(
    *,
    support_state: Dict[str, Any],
    result: Dict[str, Any],
) -> None:
    """Apply runtime stream result to Streamlit support state."""
    if "messages" in result and result["messages"]:
        support_state["messages"] = result["messages"]
    for key, value in result.items():
        if key == "messages":
            continue
        support_state[key] = value


async def _execute_turn_run_graph_and_update_state_for_streamlit(
    _ctx,
    *,
    support_state: Dict[str, Any],
    user_input: str,
    client_id: str,
):
    """Run graph stream for Streamlit and apply end-event state result in-place."""
    async for event in stream_graph_response(support_state, user_input, client_id):
        if str((event or {}).get("type") or "") == "end":
            result_obj = event.get("result")
            if isinstance(result_obj, dict):
                _apply_stream_result_to_streamlit_support_state(
                    support_state=support_state,
                    result=result_obj,
                )
        yield event


async def _consume_streamlit_runtime_events(
    *,
    runtime,
    client_id: str,
    user_id: str,
    user_input: str,
    trace_id: str,
    execute_stream_fn,
    on_update,
    on_ttft,
    turn_start: float,
) -> Tuple[bool, str, Dict[str, Any], Optional[int]]:
    """Consume runtime stream events and return a normalized turn result."""
    final_reply = ""
    final_result: Dict[str, Any] = {}
    queued = False
    ttft_ms: Optional[int] = None

    def _mark_ttft_if_needed() -> None:
        nonlocal ttft_ms
        if ttft_ms is None:
            ttft_ms = int((time.perf_counter() - turn_start) * 1000)
            try:
                on_ttft(ttft_ms)
            except Exception:
                pass

    async for event in runtime.run_turn_stream(
        channel="streamlit",
        client_id=client_id,
        user_id=user_id,
        inbound_payload={"_runtime_message_text": user_input, "streaming": True},
        execute_stream_fn=execute_stream_fn,
        trace_id=trace_id,
    ):
        if _is_state_only_stream_event(event):
            continue

        event_type = str((event or {}).get("type") or "")
        if event_type == "queued":
            queued = True
            final_reply = ""
            _mark_ttft_if_needed()
            continue
        if event_type == "token":
            token = str(event.get("content") or "")
            if token:
                _mark_ttft_if_needed()
                final_reply += token
                on_update(final_reply)
            continue
        if event_type == "end":
            final_reply = str(event.get("full_response") or final_reply or "").strip()
            _mark_ttft_if_needed()
            final_result = event.get("result") if isinstance(event.get("result"), dict) else final_result
            on_update(final_reply)
            continue
        if event_type == "error":
            err_msg = str(event.get("message") or "I apologize, but I encountered an error. Please try again.")
            _mark_ttft_if_needed()
            final_reply = err_msg
            on_update(final_reply)

    return queued, final_reply, final_result, ttft_ms


async def _run_streamlit_turn_stream(
    *,
    runtime,
    client_id: str,
    user_id: str,
    user_input: str,
    trace_id: str,
    on_update,
    on_ttft,
) -> Tuple[bool, str, Dict[str, Any], Optional[int]]:
    """Run one Streamlit turn via runtime streaming and return (queued, reply, result)."""
    turn_start = time.perf_counter()

    queued, final_reply, final_result, ttft_ms = await _consume_streamlit_runtime_events(
        runtime=runtime,
        client_id=client_id,
        user_id=user_id,
        user_input=user_input,
        trace_id=trace_id,
        execute_stream_fn=lambda ctx: _execute_turn_run_graph_and_update_state_for_streamlit(
            ctx,
            support_state=st.session_state.support_state,
            user_input=user_input,
            client_id=client_id,
        ),
        on_update=on_update,
        on_ttft=on_ttft,
        turn_start=turn_start,
    )

    if not final_result:
        final_result = dict(st.session_state.support_state or {})
        if final_reply:
            final_result = ensure_assistant_message_for_skip_final(
                state_before_invoke=st.session_state.support_state,
                result=final_result,
            )
    return queued, final_reply, final_result, ttft_ms

def display_chat_message(message: Dict[str, str], is_user: bool = True):
    """Display a chat message with appropriate styling."""
    if is_user:
        with st.chat_message("user"):
            st.write(message["content"])
    else:
        with st.chat_message("assistant"):
            st.write(message["content"])

def display_state_info():
    """Display current state information in the sidebar."""
    st.sidebar.header("🔍 Current State")
    
    state = st.session_state.support_state
    
    # Basic info
    st.sidebar.write(f"**Client ID:** {(state.get('client_id') or 'N/A')[:12]}...")
    st.sidebar.write(f"**Thread ID:** {st.session_state.thread_id[:20]}...")
    st.sidebar.write(f"**Messages:** {len(state.get('messages', []))}")
    st.sidebar.write(f"**Client ID:** {state.get('client_id') or 'None'}")
    product_info = state.get('product_info') or 'N/A'
    st.sidebar.write(f"**Product:** {product_info[:30]}...")
    
    # Customer info
    if state.get('phone_number'):
        st.sidebar.write(f"**Phone:** {state['phone_number']}")
    if state.get('selected_order_id'):
        st.sidebar.write(f"**Order ID:** {state['selected_order_id']}")
    
    # Status flags
    if state.get('is_order_query'):
        st.sidebar.write("🔍 **Order Query Detected**")
    if state.get('is_frustrated'):
        st.sidebar.write("😤 **Customer Frustrated**")
    if state.get('needs_escalation'):
        st.sidebar.write("🚨 **Needs Escalation**")
    if state.get('needs_human_agent'):
        st.sidebar.write("👨‍💼 **Needs Human Agent**")
    
    # Orders info
    known_orders = state.get('known_orders', [])
    if known_orders:
        st.sidebar.write(f"**Known Orders:** {len(known_orders)}")
    
    order_status = state.get('order_status_by_id', {})
    if order_status:
        st.sidebar.write(f"**Order Status Cache:** {len(order_status)}")

def main():
    """Main Streamlit application."""
    st.set_page_config(
        page_title="Bloomerce Customer Support Agent",
        page_icon="🤖",
        layout="wide"
    )
    
    # Initialize session state
    initialize_session_state()
    
    # Header
    st.title("🤖 Bloomerce Customer Support Agent")
    st.markdown("Welcome to the Bloomerce E-commerce Customer Support System!")
    
    # Sidebar with state info and controls
    st.sidebar.header("⚙️ Controls")

    # Explicit client selector for test context
    st.sidebar.subheader("🏢 Client")
    clients = _list_clients()
    client_options = ["__none__"] + [c["id"] for c in clients]
    label_by_id = {"__none__": "No client selected"}
    label_by_id.update({c["id"]: f"{c['name']} ({c['id']})" for c in clients})

    current_client_id = _effective_client_id()
    selected_option = current_client_id if current_client_id in client_options else "__none__"
    chosen_option = st.sidebar.selectbox(
        "Choose client context",
        options=client_options,
        index=client_options.index(selected_option),
        format_func=lambda opt: label_by_id.get(opt, opt),
        help="Prompts/configs are fetched for this selected client context.",
    )
    new_client_id = None if chosen_option == "__none__" else chosen_option
    # Keep session state in sync on every run (not only on change).
    st.session_state.selected_client_id = new_client_id
    if new_client_id != current_client_id:
        current_phone = st.session_state.support_state.get("phone_number", "9012345678")
        _load_or_create_state_for(current_phone, new_client_id)
        st.session_state.thread_id = f"streamlit_thread_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        st.rerun()

    manual_client_id = st.sidebar.text_input(
        "Or enter client_id manually",
        value=st.session_state.get("selected_client_id") or "",
        help="Use this when DB dropdown cannot load due connectivity/DNS issues.",
    ).strip()
    if manual_client_id and manual_client_id != st.session_state.get("selected_client_id"):
        current_phone = st.session_state.support_state.get("phone_number", "9012345678")
        _load_or_create_state_for(current_phone, manual_client_id)
        st.session_state.thread_id = f"streamlit_thread_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        st.rerun()
    
    # Phone number input
    st.sidebar.subheader("📱 Phone Number")
    phone_number = st.sidebar.text_input(
        "Enter phone number:",
        value=st.session_state.support_state.get("phone_number", "9012345678"),
        max_chars=10,
        help="Enter the customer's phone number (10 digits)"
    )

    client_id = _active_client_id()

    # Update phone number in state if changed
    if phone_number != st.session_state.support_state.get("phone_number"):
        _load_or_create_state_for(phone_number, _active_client_id())
        st.rerun()
    
    st.sidebar.markdown("---")
    
    # Clear chat button
    if st.sidebar.button("🗑️ Clear Chat"):
        current_phone = st.session_state.support_state.get("phone_number", "9012345678")
        st.session_state.messages = []
        # Create fresh state and save to Redis
        fresh_state, _ = _run_async(aget_or_create_state(
            from_phone_number=current_phone,
            tenant_id=_effective_client_id(),
            force_new=True  # Force create new state
        ))
        fresh_state["client_id"] = _effective_client_id()
        st.session_state.support_state = fresh_state
        # Update Redis with empty state
        _run_async(aupdate_state(current_phone, _effective_client_id(), fresh_state))
        logger.info(f"[REDIS_STATE] Cleared and reset state for phone {current_phone}, client_id={_effective_client_id()}")
        st.session_state.thread_id = f"streamlit_thread_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        st.rerun()
    
    # Display current state
    display_state_info()
    
    # Main chat interface
    st.subheader("💬 Chat with Support Agent")
    
    # Example queries
    st.markdown("**Try these example queries:**")
    example_queries = [
        "Where is my order 132749?",
        "How long will delivery take?",
        "What's your return policy?",
        "Show me iphone 17 pro max case",
        "How can I exchange my product?",
        "Cash on delivery allowed?",
        "I want to update my delivery address in my order",
        "I want to change product in my order. Need gray color",
        "I want orange color case for iphone 16"
    ]
    
    cols = st.columns(3)
    for i, query in enumerate(example_queries):
        if cols[i % 3].button(query, key=f"example_{i}"):
            # Process the example query
            response = process_user_message(query)
            
            # Add to chat history
            st.session_state.messages.append({"role": "user", "content": query})
            if response:
                st.session_state.messages.append({"role": "assistant", "content": response})
            
            st.rerun()
    
    # Display chat history
    chat_container = st.container()
    with chat_container:
        for message in st.session_state.messages:
            if message["role"] == "user":
                with st.chat_message("user"):
                    st.write(message["content"])
            else:
                with st.chat_message("assistant"):
                    st.write(message["content"])
    
    # Chat input
    if user_input := st.chat_input("Type your message here..."):
        # Add user message to chat history
        st.session_state.messages.append({"role": "user", "content": user_input})
        
        # Process through the support graph
        response = process_user_message(user_input)
        
        # Add assistant response to chat history
        if response:
            st.session_state.messages.append({"role": "assistant", "content": response})
        
        # Rerun to update the display
        st.rerun()
    
    # Debug section (collapsible)
    with st.expander("🐛 Debug Information", expanded=False):
        st.json(st.session_state.support_state)

if __name__ == "__main__":
    main()
