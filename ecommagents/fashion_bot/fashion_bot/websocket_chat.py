"""
WebSocket endpoint for Web Chat Widget
Handles real-time chat connections from embedded web widgets

Now uses unified Redis-backed state cache for:
- Session persistence across server restarts
- Session-to-phone migration when user provides phone
- Multi-channel support (web chat, WhatsApp, etc.)
"""
import json
import logging
import uuid
import asyncio
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, List, Tuple
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, HTTPException
from fashion_bot.env_loader import get_bool, get_int
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from fashion_bot.env_loader import bootstrap_environment

bootstrap_environment()
from fashion_bot.graph_context_meta import graph
from fashion_bot.trace_context import generate_trace_id, set_trace_id
from fashion_bot.utils.utils import extract_webchat_phone_candidate, log_with_trace_id
from fashion_bot.utils.product_utils import find_near_miss_handle
from fashion_bot.rollbar_config import report_error
from fashion_bot.core.conversation_runtime import RuntimeResult
from fashion_bot.core.message_persistence import ensure_assistant_message_for_skip_final
from fashion_bot.utils.langsmith_tracing import traced_operation, set_trace_io, snapshot_state_for_trace
# from fashion_bot.history.bigquery_logger import log_conversation_to_bigquery  # DISABLED: BigQuery logging

# Import unified state cache for Redis-backed session persistence
from fashion_bot.state_cache import (
    get_unified_cache, 
    Channel,
    UnifiedStateCache,
    timestamped_human_message,
    timestamped_ai_message,
)

# Import WebSocket metrics collector
from fashion_bot.monitoring.websocket_metrics import get_metrics_collector
from fashion_bot.security.embed_origins import verify_websocket_embed_for_client
from fashion_bot.security.widget_api_key import verify_widget_api_key_for_client

# LangSmith tracing imports
from langsmith.run_helpers import traceable
from langsmith import get_current_run_tree, trace
from fashion_bot.langsmith_config import get_langsmith_config, setup_langsmith_for_service
from fashion_bot.utils.client_id_utils import decode_client_id, is_encoded_client_id
from fashion_bot.utils.client_location import resolve_client_location_for_web_widget
from fashion_bot.utils.turn_metrics import track_turn_metrics

logger = logging.getLogger(__name__)
_websocket_runtime = None

# Node-emitted custom signals forwarded verbatim to the widget (see generic_skill_node
# _emit_ui_signals and streaming_service._UI_PASSTHROUGH_EVENTS). 'products' is NOT here
# — the carousel is sent separately from state by _webchat_send_stream_end_and_maybe_carousel.
# 'tool' carries the per-tool action label ({"type":"tool","tool":..,"action":..}) the
# widget renders in the typing indicator (see utils/tool_action_names.py).
_UI_SIGNAL_EVENTS = frozenset({"suggestions", "track_order", "phone_captured", "tool", "stream_reset"})

_MAX_WS_BYTES = max(4096, get_int("MAX_WS_MESSAGE_BYTES", 2 * 1024 * 1024))
_WS_SEND_TIMEOUT_SECONDS = max(1, get_int("WS_SEND_TIMEOUT_SECONDS", 5))
_WS_RECEIVE_POLL_SECONDS = max(1, get_int("WS_RECEIVE_POLL_SECONDS", 30))
_WS_SLOW_DISCONNECT_SECONDS = max(5, get_int("WS_SLOW_DISCONNECT_SECONDS", 30))


def _copy_location_to_state(state: Dict[str, Any], location: Dict[str, Any]) -> None:
    """Persist normalized widget location fields where graph/tools already look."""
    if not location or location.get("source") == "unavailable":
        return

    state["user_location"] = location
    state["client_location"] = {
        key: location.get(key)
        for key in (
            "city",
            "pincode",
            "state",
            "state_code",
            "country",
            "country_code",
            "district",
            "building",
            "latitude",
            "longitude",
            "display_name",
            "place_id",
            "place_type",
        )
        if location.get(key) is not None
    }
    if location.get("pincode"):
        state["pincode"] = location.get("pincode")
    if location.get("city"):
        state["city"] = location.get("city")
    if location.get("state"):
        state["state"] = location.get("state")


async def _apply_widget_location_to_state(state: Dict[str, Any], raw_location: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw_location, dict):
        return None
    location = await resolve_client_location_for_web_widget(browser_location=raw_location)
    _copy_location_to_state(state, location)
    return location


def _is_websocket_connected(websocket: WebSocket) -> bool:
    """Best-effort connection-state guard for websocket sends."""
    try:
        client_state_enum = type(websocket.client_state)
        app_state_enum = type(websocket.application_state)
        return (
            websocket.client_state == client_state_enum.CONNECTED
            and websocket.application_state == app_state_enum.CONNECTED
        )
    except Exception:
        return False


async def _receive_json_capped(websocket: WebSocket) -> dict:
    """Parse one WebSocket text frame as JSON; reject payloads over MAX_WS_MESSAGE_BYTES."""
    text = await websocket.receive_text()
    if len(text.encode("utf-8")) > _MAX_WS_BYTES:
        raise ValueError("WebSocket message too large")
    return json.loads(text)


async def _send_json_with_timeout(
    websocket: WebSocket,
    payload: Dict,
    *,
    warning_message: str,
    timeout_seconds: Optional[int] = None,
) -> bool:
    """Best-effort WebSocket send that cannot hang forever on a dead client."""
    if not _is_websocket_connected(websocket):
        logger.debug("%s: websocket not connected; skipping send", warning_message)
        return False

    timeout = timeout_seconds or _WS_SEND_TIMEOUT_SECONDS
    try:
        await asyncio.wait_for(websocket.send_json(payload), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        logger.warning("%s: send timed out after %ss", warning_message, timeout)
    except WebSocketDisconnect:
        logger.warning("%s: client disconnected", warning_message)
    except Exception as ws_err:
        logger.warning("%s: %s", warning_message, ws_err)
    return False


async def _close_websocket_with_timeout(
    websocket: WebSocket,
    *,
    code: int,
    reason: str,
    timeout_seconds: Optional[int] = None,
) -> None:
    """Best-effort WebSocket close that cannot block cleanup forever."""
    try:
        app_state_enum = type(websocket.application_state)
        if websocket.application_state == app_state_enum.DISCONNECTED:
            return
    except Exception as state_err:
        logger.debug(
            "WebSocket close state-check failed (code=%s reason=%s): %s",
            code,
            reason,
            state_err,
        )

    timeout = timeout_seconds or _WS_SEND_TIMEOUT_SECONDS
    try:
        await asyncio.wait_for(websocket.close(code=code, reason=reason), timeout=timeout)
    except Exception as close_err:
        logger.debug("WebSocket close skipped/failed: %s", close_err)


def _replace_trace_io(
    run,
    *,
    inputs: Optional[Dict] = None,
    outputs: Optional[Dict] = None,
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


def _get_websocket_runtime():
    """WebSocket uses passthrough runtime — no Redis single-flight lock.

    WebSocket messages are inherently sequential (one ``await receive_json()``
    at a time per connection), so the distributed lock is unnecessary overhead.
    The passthrough runtime still provides the ``run_turn`` contract with
    per-turn redis/db metrics, just without the lock/queue Redis round-trips.
    """
    global _websocket_runtime
    if _websocket_runtime is None:
        from fashion_bot.core.runtime_presets import build_passthrough_runtime
        _websocket_runtime = build_passthrough_runtime(
            log_fn=lambda trace_id, message, level="info", *_args, **_kwargs: getattr(
                logger,
                level if level in ("info", "warning", "error", "debug") else "info",
            )(f"[TRACE_ID={trace_id}] {message}"),
        )
    return _websocket_runtime


def _should_skip_final_answer_for_webchat() -> bool:
    """Resolve per-channel final_answer bypass flag for webchat."""
    return get_bool("SKIP_FINAL_ANSWER_WEBCHAT", get_bool("SKIP_FINAL_ANSWER", True))

# Initialize LangSmith for webchat service
LANGSMITH_CONFIG = get_langsmith_config("general")  # Use general config for webchat
LANGSMITH_ENABLED = setup_langsmith_for_service("general")


def _webchat_has_verified_phone(state: Dict) -> bool:
    """True when state holds a real 10-digit mobile, not a web_ session placeholder."""
    from fashion_bot.utils.phone_number_utils import normalize_phone_number

    p = state.get("phone_number")
    if not p:
        return False
    s = str(p).strip()
    if s.startswith("web_"):
        return False
    digits = normalize_phone_number(s)
    if len(digits) > 10:
        digits = digits[-10:]
    return len(digits) == 10 and digits.isdigit()


def _normalize_widget_phone(phone: Any) -> Optional[str]:
    """Normalize widget-provided phone input to a 10-digit number."""
    if not phone:
        return None
    from fashion_bot.utils.phone_number_utils import normalize_phone_number

    digits = normalize_phone_number(str(phone))
    if len(digits) > 10:
        digits = digits[-10:]
    if len(digits) != 10 or not digits.isdigit():
        return None
    return digits


async def _webchat_link_phone_number(
    session: Dict,
    *,
    phone: Any,
    client_id: str,
    session_id: str,
    source: str,
) -> Tuple[bool, Optional[str]]:
    """Link a widget session to a real phone in Redis and Postgres."""
    digits = _normalize_widget_phone(phone)
    if not digits:
        return False, None

    state = session["state"]
    already_linked = (
        state.get(WEBCHAT_PHONE_LINKED_KEY) is True
        and str(state.get("phone_number") or "") == digits
    )
    if already_linked:
        return True, digits

    try:
        from fashion_bot.history.postgres_conversations import amigrate_webchat_guest_to_phone

        await amigrate_session_to_phone(session, digits)
        state[WEBCHAT_GUEST_EXCHANGES_KEY] = 0
        state[WEBCHAT_PHONE_LINKED_KEY] = True
        await aupdate_session_state(session)
        conv_id = state.get("conversation_id")
        stats = await amigrate_webchat_guest_to_phone(
            client_id=client_id,
            session_id=session_id,
            new_phone=digits,
            conversation_id=conv_id,
        )
        logger.info(
            "webchat_phone_link: source=%s session=%s digits=***%s conv_hint=%s postgres=%s",
            source,
            session_id[:12],
            digits[-4:],
            str(conv_id)[:12] if conv_id else None,
            stats,
        )
        return True, digits
    except Exception as e:
        logger.error(
            "webchat_phone_link: source=%s migration failed session=%s: %s",
            source,
            session_id[:12],
            e,
            exc_info=True,
        )
        report_error(
            f"webchat phone link failed from {source}: {e}",
            level="error",
            exc_info=(type(e), e, e.__traceback__),
            session_id=session_id,
            client_id=client_id,
        )
        return False, digits


async def _webchat_link_detected_phone_if_present(
    session: Dict,
    *,
    user_message: str,
    client_id: str,
    session_id: str,
) -> bool:
    """
    If the guest gave a phone in this turn, link it before showing the phone gate.
    Uses the same Redis and Postgres migration path as an explicit widget phone_update.
    """
    state = session["state"]
    if _webchat_has_verified_phone(state):
        return True

    # Extract from the message the guest typed THIS turn — do not seed with the
    # existing state phone. Seeding state first meant a stale/corrupted phone
    # already in state was returned and re-linked, shadowing the corrected
    # number the customer just typed.
    phone = extract_webchat_phone_candidate(user_message)
    if not phone:
        return False

    linked, _digits = await _webchat_link_phone_number(
        session,
        phone=phone,
        client_id=client_id,
        session_id=session_id,
        source="message_text",
    )
    return linked


def _webchat_merge_trailing_human_messages(state: Dict) -> str:
    """
    Collapse consecutive trailing HumanMessages into one (resume after phone gate).
    Returns combined text for runtime / tagging.
    """
    msgs = list(state.get("messages") or [])
    if not msgs:
        return ""
    end = len(msgs) - 1
    start = end
    while start >= 0 and isinstance(msgs[start], HumanMessage):
        start -= 1
    start += 1
    if start > end:
        return ""
    trailing = msgs[start : end + 1]
    if len(trailing) == 1:
        c = trailing[0].content
        return str(c) if c is not None else ""
    parts: List[str] = []
    for m in trailing:
        c = m.content if hasattr(m, "content") else str(m)
        parts.append(str(c) if c is not None else "")
    merged = "\n\n".join(p for p in parts if p)
    state["messages"] = msgs[:start] + [timestamped_human_message(merged)]
    return merged


def _webchat_increment_guest_exchange_if_applicable(
    state: Dict, guest_at_start: bool, turn_was_queued: bool
) -> None:
    if not guest_at_start or turn_was_queued:
        return
    n = int(state.get(WEBCHAT_GUEST_EXCHANGES_KEY) or 0)
    state[WEBCHAT_GUEST_EXCHANGES_KEY] = n + 1


async def _aget_webchat_guest_exchange_limit(client_id: str) -> int:
    """Resolve phone-gate exchange limit from client_configs, then env/default."""
    default = max(
        WEBCHAT_GUEST_EXCHANGE_LIMIT_MIN,
        min(WEBCHAT_GUEST_EXCHANGE_LIMIT_DEFAULT, WEBCHAT_GUEST_EXCHANGE_LIMIT_MAX),
    )
    try:
        from fashion_bot.config_manager import aget_config

        raw = await aget_config(
            WEBCHAT_GUEST_EXCHANGE_LIMIT_CONFIG_KEY,
            client_id=client_id,
            default=None,
        )
        if raw is None or str(raw).strip() == "":
            return default
        limit = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(
            "webchat_phone_gate: non-integer %s for client=%s; using default=%d",
            WEBCHAT_GUEST_EXCHANGE_LIMIT_CONFIG_KEY,
            client_id,
            default,
        )
        return default
    except Exception as e:
        logger.warning(
            "webchat_phone_gate: config read failed for client=%s: %s; using default=%d",
            client_id,
            e,
            default,
        )
        return default
    return max(
        WEBCHAT_GUEST_EXCHANGE_LIMIT_MIN,
        min(limit, WEBCHAT_GUEST_EXCHANGE_LIMIT_MAX),
    )


async def _webchat_store_inbound_customer_row(
    *,
    state: Dict,
    client_id: str,
    session_id: str,
    message: str,
) -> Optional[str]:
    """
    Persist inbound customer transcript (Postgres). Updates state conversation_id.
    Returns conv_id on success.
    """
    from fashion_bot.history.postgres_conversations import astore_conversation_event

    phone_number = state.get("phone_number")
    user_identifier = phone_number if phone_number else session_id
    verified = _webchat_has_verified_phone(state)
    page_context = state.get("page_context")
    conversation_tags = state.get("conversation_tags", []) or []
    conv_hint = state.get("conversation_id")

    customer_info_data: Dict = {"is_identified": verified, "source": "web-widget"}
    if page_context:
        customer_info_data["page_context"] = {
            "url": page_context.get("url"),
            "page_type": page_context.get("pageType"),
            "product_handle": page_context.get("productHandle"),
            "product_title": page_context.get("productTitle"),
        }

    conv_id = await astore_conversation_event(
        client_id=client_id,
        phone=user_identifier,
        sender="customer",
        text=message,
        channel_type="web-chat",
        started_by="customer",
        tags=conversation_tags if conversation_tags else None,
        customer_id=user_identifier,
        customer_info=json.dumps(customer_info_data),
        conversation_id=conv_hint,
    )
    state["conversation_id"] = conv_id
    return conv_id


async def _webchat_store_bot_row(
    *,
    state: Dict,
    client_id: str,
    session_id: str,
    response_text: str,
    conv_id: Optional[str],
    trace_id: str,
) -> None:
    """Persist outbound bot transcript (Postgres)."""
    from fashion_bot.history.postgres_conversations import astore_conversation_event

    phone_number = state.get("phone_number")
    user_identifier = phone_number if phone_number else session_id
    verified = _webchat_has_verified_phone(state)

    await astore_conversation_event(
        client_id=client_id,
        phone=user_identifier,
        sender="bot",
        text=response_text,
        channel_type="web-chat",
        started_by="customer",
        customer_id=user_identifier,
        customer_info=json.dumps({"is_identified": verified, "source": "web-widget"}),
        conversation_id=conv_id,
        langsmith_id=trace_id,
    )


from fashion_bot.utils.cart_utils import (
    build_cart_context_messages as _build_cart_context_messages,
    normalize_cart_item as _normalize_cart_item,
    normalize_cart_snapshot as _normalize_cart_snapshot,
    upsert_cart_entity_in_context as _upsert_cart_entity_in_context,
)


async def _flush_pending_widget_actions(websocket: WebSocket, session: Dict) -> None:
    """Send any cart actions queued by the agent's tools out to the widget."""
    state = session.get("state") or {}
    actions = state.get("pending_widget_actions") or []
    if not isinstance(actions, list) or not actions:
        return
    state["pending_widget_actions"] = []
    logger.info(f"🛒 Flushing {len(actions)} pending widget action(s) to client")
    cart_snap = state.get("cart") or {}
    items = cart_snap.get("items") or []

    _MUTATION_ACTIONS = {"add", "remove", "qty"}
    has_mutation = any(
        str(a.get("action") or "").strip().lower() in _MUTATION_ACTIONS
        for a in actions
        if isinstance(a, dict)
    )

    for act in actions:
        if not isinstance(act, dict):
            continue
        action = str(act.get("action") or "").strip().lower()

        if action == "show_cart" and has_mutation:
            logger.info("🛒 Skipping show_cart — mutation action already triggers CART_UPDATED")
            continue

        variant_id = str(act.get("variant_id") or "").strip()
        target_text = str(act.get("product_title") or "").strip()
        if not target_text and variant_id and items:
            for it in items:
                if str(it.get("variant_id") or "").strip() == variant_id:
                    target_text = str(it.get("product_title") or "").strip()
                    break
        payload: Dict = {
            "type": "cart_action",
            "action": action,
            "variant_id": variant_id,
            "target_text": target_text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if act.get("quantity") is not None:
            try:
                payload["quantity"] = int(act["quantity"])
            except (TypeError, ValueError):
                pass
        if act.get("qty_direction"):
            payload["qty_direction"] = str(act["qty_direction"]).lower()
        logger.info(f"🛒 Dispatching cart_action: {action} variant={variant_id}")
        await _send_websocket_event_for_ui(
            websocket=websocket,
            payload=payload,
            warning_message="⚠️ Failed to dispatch cart_action to widget",
        )

    try:
        await aupdate_session_state(session)
    except Exception as e:
        logger.warning(f"⚠️ Failed to persist state after cart flush: {e}")


async def _apply_cart_snapshot_to_state(session: Dict, raw_snapshot: Dict) -> None:
    """Persist a normalized cart snapshot into session state and conversation context."""
    state = session["state"]
    snapshot = _normalize_cart_snapshot(raw_snapshot or {})

    previous = state.get("cart") or {}
    previous_token = previous.get("token") if isinstance(previous, dict) else None
    if (
        snapshot.get("token")
        and previous_token == snapshot["token"]
        and (previous.get("item_count") == snapshot.get("item_count"))
        and not snapshot.get("last_action")
    ):
        return

    state["cart"] = snapshot
    _upsert_cart_entity_in_context(state, snapshot)
    await aupdate_session_state(session)


async def _append_cart_context_to_state_and_transcript(
    *,
    session: Dict,
    client_id: str,
    session_id: str,
    trace_id: str,
    events: List[Dict],
) -> None:
    state = session["state"]
    if not state.get("session_id"):
        state["session_id"] = session_id
    if not state.get("phone_number"):
        state["phone_number"] = session_id
    state["trace_id"] = trace_id
    state["_skip_final_answer"] = _should_skip_final_answer_for_webchat()

    conv_id = state.get("conversation_id")
    from fashion_bot.utils.context_helpers import add_action_to_context

    for event in events:
        texts = _build_cart_context_messages(event)
        if not texts:
            continue
        user_text, bot_text = texts
        try:
            conv_id = await _webchat_store_inbound_customer_row(
                state=state,
                client_id=client_id,
                session_id=session_id,
                message=user_text,
            )
        except Exception as e:
            logger.warning(f"⚠️ Failed to store inbound cart context message: {e}")
            conv_id = state.get("conversation_id")
        state["messages"] = state.get("messages", []) + [timestamped_human_message(user_text)]

        try:
            await _webchat_store_bot_row(
                state=state,
                client_id=client_id,
                session_id=session_id,
                response_text=bot_text,
                conv_id=conv_id,
                trace_id=trace_id,
            )
        except Exception as e:
            logger.warning(f"⚠️ Failed to store bot cart context message: {e}")
        state["messages"] = state.get("messages", []) + [timestamped_ai_message(bot_text)]

        ctx = state.get("conversation_context")
        if isinstance(ctx, dict):
            action_type = str(event.get("action") or "").strip().lower() or "cart_modified"
            add_action_to_context(ctx, {
                "action_type": "cart_modified",
                "action_name": f"Cart {action_type}",
                "performed_at": datetime.now(timezone.utc).isoformat(),
                "parameters": {
                    "action": action_type,
                    "variant_id": str(event.get("variant_id") or event.get("variantId") or "").strip(),
                    "product_title": str(event.get("product_title") or event.get("productTitle") or "").strip(),
                    "quantity": event.get("quantity"),
                    "cart_quantity": event.get("cart_quantity") or event.get("cartQuantity"),
                },
                "success": True,
                "result_summary": bot_text,
                "topic_id": None,
            })

    await aupdate_session_state(session)


@contextmanager
def _open_webchat_turn_trace(
    *,
    name: str,
    trace_id: str,
    user_identifier: str,
    client_id: str,
    input_message: Optional[str],
):
    """Create a single LangSmith root trace for the whole webchat turn."""
    if not (LANGSMITH_ENABLED and LANGSMITH_CONFIG.is_enabled):
        yield None
        return

    try:
        with trace(
            name=name,
            run_type="chain",
            project_name=LANGSMITH_CONFIG.project_name,
            metadata={
                "trace_id": trace_id,
                "user_identifier": user_identifier,
                "client_id": client_id,
                "service": "webchat",
            },
        ) as run:
            clean_input = str(input_message or "").strip()
            _replace_trace_io(
                run,
                inputs={
                    "input": clean_input[:2000],
                },
            )
            yield run
    except Exception as trace_err:
        logger.warning(f"LangSmith webchat root trace error: {trace_err}")
        yield None


async def invoke_graph_with_tracing(
    state: dict,
    trace_id: str,
    user_identifier: str,
    input_message: Optional[str] = None,
    trace_graph_internally: bool = True,
) -> Tuple[dict, str]:
    """
    Invoke the graph within a LangSmith trace context to capture trace ID.
    
    Args:
        state: The conversation state to pass to the graph
        trace_id: Our internal trace ID for logging
        user_identifier: User identifier (phone or session_id)
    
    Returns:
        Tuple of (result dict, langsmith_trace_id string)
    """
    langsmith_trace_id = None
    result = None

    from fashion_bot.monitoring.otel_metrics import set_request_client_id
    set_request_client_id(state.get("client_id"))

    if trace_graph_internally and LANGSMITH_ENABLED and LANGSMITH_CONFIG.is_enabled:
        try:
            with trace(
                name="webchat-conversation",
                run_type="chain",
                project_name=LANGSMITH_CONFIG.project_name,
                metadata={
                    "trace_id": trace_id,
                    "user_identifier": user_identifier,
                    "service": "webchat"
                }
            ) as run:
                if run is not None and hasattr(run, 'id'):
                    langsmith_trace_id = str(run.id)
                    state["_langsmith_trace_id"] = langsmith_trace_id

                clean_input = str(input_message or "").strip()
                if not clean_input:
                    msgs = state.get("messages") if isinstance(state, dict) else None
                    if isinstance(msgs, list):
                        for msg in reversed(msgs):
                            content = getattr(msg, "content", None)
                            if content:
                                clean_input = str(content).strip()
                                if clean_input:
                                    break
                _replace_trace_io(
                    run,
                    inputs={
                        "input": clean_input[:2000],
                        "state": snapshot_state_for_trace(state),
                    },
                )
                
                result = await graph.ainvoke(state)
                result = ensure_assistant_message_for_skip_final(
                    state_before_invoke=state,
                    result=result,
                )
                reply_preview = ""
                if isinstance(result, dict):
                    if result.get("customer_message"):
                        reply_preview = str(result.get("customer_message") or "")
                    elif result.get("messages"):
                        last_msg = result["messages"][-1]
                        reply_preview = str(getattr(last_msg, "content", last_msg) or "")
                    result["_langsmith_trace_id"] = langsmith_trace_id
                _replace_trace_io(
                    run,
                    outputs={
                        "output": reply_preview[:2000],
                        "state": snapshot_state_for_trace(result),
                    },
                )
                
        except Exception as trace_err:
            logger.warning(f"LangSmith trace error: {trace_err}, invoking without tracing")
            result = await graph.ainvoke(state)
            result = ensure_assistant_message_for_skip_final(
                state_before_invoke=state,
                result=result,
            )
    else:
        result = await graph.ainvoke(state)
        result = ensure_assistant_message_for_skip_final(
            state_before_invoke=state,
            result=result,
        )
        try:
            current_run = get_current_run_tree()
        except Exception:
            current_run = None
        if current_run is not None and hasattr(current_run, "id"):
            langsmith_trace_id = str(current_run.id)
    
    # Fallback to custom trace_id if LangSmith trace ID not captured
    if not langsmith_trace_id:
        langsmith_trace_id = trace_id
    state["_langsmith_trace_id"] = langsmith_trace_id
    if isinstance(result, dict):
        result["_langsmith_trace_id"] = langsmith_trace_id
    
    return result, langsmith_trace_id

# Router for WebSocket endpoints
websocket_router = APIRouter()

# ==================== SESSION MANAGEMENT (REDIS-BACKED) ====================
# Sessions are now stored in Redis via UnifiedStateCache for:
# - Persistence across server restarts
# - Session-to-phone migration when user provides phone
# - Unified key pattern: web:{client_id}:{session_id or phone}

# Active WebSocket connections tracking (in-memory only - just for connection management)
# This only tracks which WebSockets are currently connected, not conversation state
active_websocket_connections: Dict[str, WebSocket] = {}

# Session data retention - how long to keep session data in Redis after last activity
# Conversation history and state are preserved for 24 hours (Redis TTL)
SESSION_DATA_RETENTION = timedelta(minutes=30)  # Used for connection management only

# WebSocket idle timeout - close connection if no messages for this duration
# E-commerce support conversations typically last 15-20 minutes
# Close idle connections to free up resources for active users
WEBSOCKET_IDLE_TIMEOUT = timedelta(minutes=20)  # Close after 20 min idle

# Heartbeat interval - widget sends ping every X minutes to keep connection alive
HEARTBEAT_INTERVAL = timedelta(minutes=5)  # Widget should ping every 5 min

# Guest webchat: completed human+bot exchanges allowed before the phone gate.
WEBCHAT_GUEST_EXCHANGE_LIMIT_CONFIG_KEY = "webchat_guest_exchange_limit"
WEBCHAT_GUEST_EXCHANGE_LIMIT_DEFAULT = get_int("WEBCHAT_GUEST_EXCHANGE_LIMIT_DEFAULT", 8)
WEBCHAT_GUEST_EXCHANGE_LIMIT_MIN = 1
WEBCHAT_GUEST_EXCHANGE_LIMIT_MAX = 100
WEBCHAT_GUEST_EXCHANGES_KEY = "webchat_guest_exchanges_completed"
WEBCHAT_PHONE_LINKED_KEY = "webchat_phone_linked"
WEBCHAT_PENDING_RESUME_KEY = "webchat_pending_graph_resume"
WEBCHAT_PHONE_REQUIRED_MESSAGE = (
    "We need your 10-digit mobile number to continue. Please provide it below."
)

# Client name to ID cache (refreshed from database)
_client_name_cache: Dict[str, str] = {}
_cache_last_updated: Optional[datetime] = None
_cache_ttl = timedelta(minutes=10)  # Refresh cache every 10 minutes
_client_name_cache_lock: Optional[asyncio.Lock] = None



def _carousel_discount_pct(product: Dict, price=None) -> Optional[int]:
    """Best-effort rounded discount % for the carousel image badge.

    Returns an int > 0 when the product is genuinely marked down, otherwise
    None. Fully defensive: any unexpected data shape yields None instead of
    raising, so it can never break carousel formatting.

    Uses an explicit ``discount_pct`` when present (bestseller path); otherwise
    derives it from ``compare_at_price_min`` vs the current price (the main
    Upstash search path exposes compare-at but not a precomputed discount).
    """
    try:
        raw_discount = product.get("discount_pct")
        discount_pct = None
        if raw_discount is not None:
            try:
                discount_pct = float(raw_discount)
            except (TypeError, ValueError):
                discount_pct = None
        if discount_pct is None:
            compare_at = float(
                product.get("compare_at_price_min")
                or product.get("compare_at_price")
                or 0
            )
            current = float(
                product.get("price_min")
                or (price if not isinstance(price, dict) else 0)
                or 0
            )
            if compare_at > current > 0:
                discount_pct = (compare_at - current) / compare_at * 100.0
        if discount_pct is not None and discount_pct > 0:
            return round(discount_pct)
    except Exception:
        return None
    return None


# Rating/count extraction lives in utils/product_utils.py, shared with the
# LLM-facing tool result (tool_factory.py._normalize_product) per AGENTS.md
# "Shared Utilities Over Duplication" -- imported under the pre-existing
# local name so every call site below is unchanged.
from fashion_bot.utils.product_utils import extract_product_rating as _extract_product_rating


def format_product_for_carousel(product: Dict, rating_enabled: bool = True) -> Optional[Dict]:
    """
    Format a single product for the widget carousel.
    Ensures image_url and price are strings for the frontend.

    ``rating_enabled`` is the per-tenant kill switch for the rating/count
    display (aget_judgeme_rating_display_enabled() in config_manager.py) --
    plain bool, not looked up here, since this function stays synchronous
    and callers already await the config lookup once per turn rather than
    once per product. Defaults True so every other caller (including the
    existing test suite) keeps today's behavior unchanged.

    Handles product dicts from multiple sources:
      - Shopify adapter (image_url, featured_image, images)
      - Upstash search metadata (image_url, all_images)
      - Search tool rows: { "number", "title", "url", "product_data": { ... } } (handle/image in product_data)
      - Recommendation engine (images: [{"src": ...}])
    
    Args:
        product: Product dictionary from state or adapter
        
    Returns:
        Formatted product dict or None if invalid
    """
    if not product:
        return None

    # Upstash / search_products often nest the catalog dict under product_data; title/url may sit at top level only.
    pd = product.get("product_data")
    if isinstance(pd, dict):
        merged = dict(pd)
        for key in ("title", "url", "name", "product_url", "link", "product_link"):
            v = product.get(key)
            if v is not None and str(v).strip() != "":
                merged[key] = v
        product = merged
    
    # Extract fields (handle various naming conventions)
    title = product.get("title") or product.get("name") or product.get("product_name")
    handle = product.get("handle") or product.get("product_handle")
    url = (
        product.get("url")
        or product.get("product_url")
        or product.get("link")
        or product.get("product_link")
    )
    
    # Robust image extraction — try multiple field formats
    image_url = product.get("image_url")
    if not image_url or not str(image_url).startswith('http'):
        img_obj = (product.get("featured_image") or product.get("image")
                   or product.get("featuredImage") or product.get("featured_media"))
        if isinstance(img_obj, dict):
            image_url = img_obj.get("url") or img_obj.get("src") or img_obj.get("image_url")
        elif isinstance(img_obj, str) and img_obj.startswith('http'):
            image_url = img_obj

    # Upstash catalog: all_images list when image_url missing
    if (not image_url or not str(image_url).startswith('http')) and product.get("all_images"):
        ai = product.get("all_images")
        if isinstance(ai, list) and len(ai) > 0 and isinstance(ai[0], str) and ai[0].startswith('http'):
            image_url = ai[0]
            
    # Check images list (Shopify adapter / recommendation engine)
    if (not image_url or not str(image_url).startswith('http')) and product.get("images"):
        images = product.get("images")
        if isinstance(images, list) and len(images) > 0:
            first_img = images[0]
            if isinstance(first_img, dict):
                image_url = first_img.get("url") or first_img.get("src") or first_img.get("image_url")
            elif isinstance(first_img, str) and first_img.startswith('http'):
                image_url = first_img

    # Handle sometimes only inferable from canonical product URL
    if not handle and url:
        m = re.search(r"/products/([^/?#]+)", str(url))
        if m:
            handle = m.group(1)
    
    if not title or not handle:
        return None
    
    # Resolve variant id from payload-only sources (never URL-derived).
    variant_id = (
        product.get("variantId")
        or product.get("variant_id")
        or product.get("selected_variant_id")
    )
    if not variant_id:
        variant_obj = product.get("variant")
        if isinstance(variant_obj, dict):
            variant_id = variant_obj.get("id")
    if not variant_id and isinstance(product.get("variants"), list):
        variants = [v for v in product.get("variants") if isinstance(v, dict)]
        if variants:
            available = next((v for v in variants if v.get("available") is True), None)
            variant_id = (available or variants[0]).get("id")
    if not variant_id and url:
        try:
            parsed_url = urlparse(str(url))
            variant_qs = parse_qs(parsed_url.query).get("variant")
            if variant_qs and variant_qs[0]:
                variant_id = variant_qs[0]
        except Exception:
            variant_id = variant_id

    # Get price (handle dict or string)
    price = product.get("price") or product.get("price_min")
    if isinstance(price, dict):
        price = price.get("min") or price.get("amount") or price.get("regular")
    
    raw_variants = product.get("variants")
    variants_out = None
    options_out = None
    if isinstance(raw_variants, list) and len(raw_variants) > 1:
        variants_out = []
        for v in raw_variants:
            if not isinstance(v, dict):
                continue
            variants_out.append({
                "id": v.get("id"),
                "title": v.get("title") or "",
                "price": str(v.get("price") or price or ""),
                "available": v.get("available", True),
                "option1": v.get("option1"),
                "option2": v.get("option2"),
                "option3": v.get("option3"),
            })
        raw_options = product.get("options")
        if isinstance(raw_options, list) and raw_options:
            options_out = []
            for opt in raw_options:
                if isinstance(opt, dict):
                    options_out.append({
                        "name": opt.get("name", "Option"),
                        "values": opt.get("values", []),
                    })
                elif isinstance(opt, str):
                    options_out.append({"name": opt, "values": []})
        if not options_out and variants_out:
            # Derive option names from variant selected_options (GraphQL path)
            # e.g. [{"name": "Color", "value": "Eclipse"}, {"name": "Size", "value": "32B"}]
            raw_variants_for_names = product.get("variants") or []
            slot_names: list = []
            for rv in raw_variants_for_names:
                if isinstance(rv, dict) and isinstance(rv.get("selected_options"), list):
                    slot_names = [
                        so.get("name", f"Option {i + 1}")
                        for i, so in enumerate(rv["selected_options"])
                    ]
                    break
            for slot_idx, slot_key in enumerate(["option1", "option2", "option3"]):
                vals = list(dict.fromkeys(
                    str(v[slot_key]) for v in variants_out
                    if v.get(slot_key) is not None
                ))
                if vals:
                    options_out = options_out or []
                    options_out.append({
                        "name": slot_names[slot_idx] if slot_idx < len(slot_names) else f"Option {slot_idx + 1}",
                        "values": vals,
                    })

    result: Dict = {
        "title": title,
        "handle": handle,
        "variantId": str(variant_id) if variant_id else None,
        "price": str(price) if price else None,
        "url": url,
        "image_url": image_url,
    }
    # Internal/debug provenance (forwarded to the UI): how this card's handle was
    # resolved — "internal" (in-memory candidate set, the normal path) or "searchdb"
    # (recovered via the Upstash handle-resolve fallback). Lets the widget / network
    # inspector see when a DB lookup was needed.
    result["handle_resolve"] = product.get("handle_resolve") or "internal"

    # Rating + review count, when Judge.me has published reviews for this
    # product. Absent entirely for a 0-review product -- the frontend renders
    # the pre-existing card unchanged in that case (see chat-widget-frame.html
    # createProductCard).
    rating_info = _extract_product_rating(product) if rating_enabled else None
    if rating_info:
        result["rating"] = rating_info["rating"]
        result["rating_count"] = rating_info["rating_count"]

    if variants_out:
        result["variants"] = variants_out
    if options_out:
        result["options"] = options_out

    # Rounded discount % for the carousel image badge. Computed in a dedicated,
    # fully-defensive helper and guarded again here so a bad value can never
    # break carousel formatting for the rest of the payload.
    try:
        badge_pct = _carousel_discount_pct(product, price)
        if badge_pct:
            result["discount_pct"] = badge_pct
            # Original / MRP (compare-at) price for the strike-through, only when
            # genuinely higher than the current price (a real markdown). Same raw
            # string form as `price`; the widget's formatPrice() adds the symbol.
            try:
                _cmp = float(
                    product.get("compare_at_price_min")
                    or product.get("compare_at_price")
                    or 0
                )
                _cur = float(
                    product.get("price_min")
                    or (price if not isinstance(price, dict) else 0)
                    or 0
                )
                if _cmp > _cur > 0:
                    result["original_price"] = (
                        str(int(_cmp)) if float(_cmp).is_integer() else str(_cmp)
                    )
            except (TypeError, ValueError):
                pass
    except Exception:
        pass

    return result


def _full_data_for_focal(focal: Dict, entities: List) -> Optional[Dict]:
    """
    focal_entity is often a lightweight ref (entity_id, entity_value) without full_data.
    Image URL and price live on the matching entry in conversation_context.entities.
    """
    if not isinstance(focal, dict):
        return None
    embedded = focal.get("full_data")
    if isinstance(embedded, dict) and (
        embedded.get("handle") or embedded.get("title") or embedded.get("name")
    ):
        return embedded
    fid = focal.get("entity_id")
    if fid is None or not isinstance(entities, list):
        return None
    fid_s = str(fid).strip().lower()
    for ent in reversed(entities):
        if not isinstance(ent, dict):
            continue
        et = (ent.get("entity_type") or "").lower()
        if et not in ("product", "selectable_product"):
            continue
        eid = ent.get("entity_id")
        if eid is None:
            continue
        if str(eid).strip().lower() != fid_s:
            continue
        full = ent.get("full_data")
        if isinstance(full, dict) and (full.get("handle") or full.get("title") or full.get("name")):
            return full
    return None


def _merge_entity_full_data_for_carousel(fd: Dict) -> Dict:
    """Flatten selectable_product rows that nest catalog fields under product_data."""
    if not isinstance(fd, dict):
        return {}
    pd = fd.get("product_data")
    if isinstance(pd, dict):
        merged = dict(pd)
        for key in ("title", "url", "name", "product_url", "link", "product_link", "handle"):
            v = fd.get(key)
            if v is not None and str(v).strip():
                merged[key] = v
        return merged
    return fd


def _conversation_entities_product_candidates(
    state: Dict, max_candidates: int = 25
) -> List[Dict]:
    """
    Products from conversation_context.entities (tool + recommendation), deduped by handle.

    When the model answers from context without a search tool (recent_products / product_selection_matches
    empty), focal fallback alone yields one SKU (e.g. All Eyes Denim) while the reply lists other
    products still present in entities — carousel would be wrong and solo idempotency can hide it entirely.
    """
    ctx = state.get("conversation_context") or {}
    entities = ctx.get("entities") or []
    if not isinstance(entities, list):
        return []
    out: List[Dict] = []
    seen: set = set()
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        et = (ent.get("entity_type") or "").lower()
        if et not in ("product", "selectable_product"):
            continue
        raw = ent.get("full_data")
        if not isinstance(raw, dict):
            continue
        fd = _merge_entity_full_data_for_carousel(raw)
        handle = str(fd.get("handle") or "").strip().lower()
        title = (fd.get("title") or fd.get("name") or "").strip()
        if not handle and not title:
            continue
        dedupe_key = handle or re.sub(r"\s+", " ", title.lower())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        out.append(fd)
        if len(out) >= max_candidates:
            break
    return out


def _products_for_webchat_carousel(state: Dict) -> List[Dict]:
    """
    Build the product list used for the widget carousel after a turn.

    Returns the UNION of every product the LLM may legitimately reference this
    turn, deduped by handle (first-wins, so the freshest live rows win). The
    prompt lets the model emit a handle from THIS turn's tool results OR from the
    conversation's product entities, so the carousel resolver must cover the SAME
    universe. If we returned only ``recent_products`` (this turn's tools), a handle
    the model validly picked from an entity — while the tool returned different or
    fewer products (e.g. a repeated bestseller search yielding one item) — matches
    NO candidate and the carousel shows ZERO cards (observed in prod:
    "SHOW_PRODUCTS handles [...] matched no candidate"). Widening the pool is safe:
    ``_match_by_handles`` surfaces ONLY the handles the LLM emitted (plus configured
    pins), so extra candidates never become unwanted cards.

    Sources, in precedence order (earlier wins on a handle collision):
      1. recent_products          — this turn's tool results (live inventory)
      2. product_selection_matches — accumulated search SKUs across the topic
      3. conversation entities     — products carried in conversation_context
      4. inquiry_product_info      — the single focal product, if any
      5. focal_entity full_data    — last-resort focal product
    """
    rp = state.get("recent_products") or []
    if not isinstance(rp, list):
        rp = []
    pm = state.get("product_selection_matches") or []
    if not isinstance(pm, list):
        pm = []

    sources: List[Dict] = []
    sources.extend(rp)
    sources.extend(pm)
    sources.extend(_conversation_entities_product_candidates(state))

    ipi = state.get("inquiry_product_info")
    if isinstance(ipi, dict) and (ipi.get("handle") or ipi.get("title") or ipi.get("name")):
        sources.append(ipi)

    ctx = state.get("conversation_context") or {}
    focal = ctx.get("focal_entity") or {}
    et = (focal.get("entity_type") or "").lower()
    if et in ("product", "selectable_product"):
        entities = ctx.get("entities") or []
        fd = _full_data_for_focal(focal, entities) or {}
        handle = fd.get("handle") or focal.get("entity_id")
        title = fd.get("title") or fd.get("name") or focal.get("entity_value")
        if fd and (handle or title):
            sources.append(fd)
        elif handle or title:
            meta = focal.get("metadata") or {}
            url = meta.get("url") or meta.get("product_link") or state.get("product_link")
            sources.append({
                "handle": handle,
                "title": title,
                "name": title,
                "url": url,
                "image_url": fd.get("image_url") or fd.get("image"),
            })

    # Dedupe by handle (first occurrence wins → freshest live data). Rows without a
    # resolvable handle are kept as-is so they can still match by slugified title.
    deduped: List[Dict] = []
    seen_handles: set = set()
    for p in sources:
        if not isinstance(p, dict):
            continue
        h = _resolve_product_handle(p)
        if h:
            if h in seen_handles:
                continue
            seen_handles.add(h)
        deduped.append(p)
    return deduped


def _resolve_product_handle(product: Dict) -> str:
    """Extract the canonical handle from a product dict (flat or nested)."""
    h = (product.get("handle") or product.get("product_handle") or "").strip().lower()
    if h:
        return h
    pd = product.get("product_data")
    if isinstance(pd, dict):
        h = (pd.get("handle") or pd.get("product_handle") or "").strip().lower()
    return h


def _resolve_product_title(product: Dict) -> str:
    """Extract the product title/name from a product dict (flat or nested)."""
    t = (product.get("title") or product.get("name") or "").strip()
    if t:
        return t
    pd = product.get("product_data")
    if isinstance(pd, dict):
        t = (pd.get("title") or pd.get("name") or "").strip()
    return t


def _slugify_for_handle_match(text: str) -> str:
    """Approximate Shopify's handle slugification so a title can be compared
    against a handle: lowercase, non-alphanumerics → hyphens, collapse/trim.

    e.g. "Anti-Acne Serum" → "anti-acne-serum". Used only as a fallback when an
    LLM-emitted handle doesn't match a candidate's real handle, so a model that
    slugified a title (instead of copying the handle verbatim) still resolves to
    the right card rather than an empty carousel.
    """
    if not text:
        return ""
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")


def _match_by_handles(
    carousel_products: List[Dict],
    handles: List[str],
    max_results: int,
) -> List[Dict]:
    """Return carousel products matching *handles*, preserving the order from
    *handles* (i.e. the LLM's chosen order).

    Primary match is by exact (case/space-normalised) handle. As a resilience
    fallback — for when the model slugified a product's title instead of copying
    its real handle, or when a candidate carries a title but an empty handle
    (e.g. some non-search product tools) — an emitted handle also matches a
    candidate whose slugified title equals it. Exact-handle matches always win.

    Last, an emitted handle that is a NEAR MISS of a candidate's (a dropped or
    appended word, ``tshirt`` vs ``t-shirt``) resolves to that candidate via
    ``find_near_miss_handle`` — the model mangles long slugs when copying them
    by hand, and the product is almost always right there in the candidate set.
    That tier is SKU-anchored and abstains whenever the answer is ambiguous, so
    it can add a card that would otherwise be lost but never substitute a
    different product for one that already matched.
    """
    handle_set = {h.strip().lower() for h in handles if h}
    by_handle: Dict[str, Dict] = {}
    by_title_slug: Dict[str, Dict] = {}
    all_by_handle: Dict[str, Dict] = {}
    for p in carousel_products:
        if not isinstance(p, dict):
            continue
        h = _resolve_product_handle(p)
        if h and h not in all_by_handle:
            all_by_handle[h] = p
        if h and h in handle_set and h not in by_handle:
            by_handle[h] = p
        title_slug = _slugify_for_handle_match(_resolve_product_title(p))
        if title_slug and title_slug in handle_set and title_slug not in by_title_slug:
            by_title_slug.setdefault(title_slug, p)

    ordered: List[Dict] = []
    seen_ids: set = set()
    for raw in handles:
        key = (raw or "").strip().lower()
        if not key:
            continue
        match = by_handle.get(key) or by_title_slug.get(key)
        if match is None:
            # Only reached when exact + title-slug both missed, so this tier can
            # never change a handle that already resolved.
            recovered = find_near_miss_handle(key, list(all_by_handle))
            if recovered:
                match = all_by_handle.get(recovered)
                logger.info(
                    f"🛒 Carousel: near-miss handle {key!r} resolved to {recovered!r}"
                )
        if match is None:
            continue
        # Dedupe in case handle + title-slug resolve to the same product.
        ident = id(match)
        if ident in seen_ids:
            continue
        seen_ids.add(ident)
        ordered.append(match)
    return ordered[:max_results]


def _collect_pinned_products(state: Dict, carousel_products: List[Dict]) -> List[Dict]:
    """Pinned promos (products a store explicitly configured via
    ``pinned_bestseller_products``, flagged ``pinned=True`` by the search /
    top-selling tools).

    A pin is a per-turn promo decision: the search tool only sets ``pinned=True``
    for a bestseller / catalog-wide query THIS turn. So collect pins ONLY from
    this turn's tool output (``recent_products``, extracted fresh from this turn's
    intermediate_steps) — NOT from ``carousel_products``, which is the accumulated
    union (recent_products + ``product_selection_matches`` + entities). That union
    retains ``pinned=True`` on a promo pinned in an EARLIER turn, which would
    otherwise leak the pin into a later unrelated query (e.g. a bestseller pin
    surfacing in a "show me jeans" carousel)."""
    rp = state.get("recent_products")
    if not isinstance(rp, list):
        return []
    return [p for p in rp if isinstance(p, dict) and p.get("pinned")]


def _force_pins_first(pinned: List[Dict], matched: List[Dict], max_results: int) -> List[Dict]:
    """Prepend pinned promos to the carousel selection, deduped by handle (a pin
    wins the slot over a live duplicate).

    Pins are ADDITIVE: the cap is ``max_results + len(pinned)`` so forcing a pin
    in never displaces a live top-seller from the cards (a pin already in
    ``matched`` is deduped, so the total is at most ``max_results`` in that case).
    """
    seen: set = set()
    out: List[Dict] = []
    for p in list(pinned) + list(matched):
        h = (_resolve_product_handle(p) or "").strip().lower()
        if h and h in seen:
            continue
        if h:
            seen.add(h)
        out.append(p)
    return out[: max_results + len(pinned)]



def _match_carousel_products_to_reply(
    state: Dict,
    carousel_products: List[Dict],
    reply: str,
    max_results: int = 7,
    show_product_handles: Optional[List[str]] = None,
) -> List[Dict]:
    """Select which carousel products to display.

    Primarily LLM-driven: a product card is shown for the handles the model
    explicitly emitted in its ``###SHOW_PRODUCTS:[...]###`` block
    (``show_product_handles``). Matching is resilient (exact handle, else
    slugified title — see ``_match_by_handles``). Outside pins, there is
    intentionally no title-substring fallback on the reply text, so incidental
    product mentions never trigger a carousel.

    EXCEPTION — pinned promos: any candidate flagged ``pinned=True`` (a product
    the store explicitly configured) is ALWAYS force-included, pins-first, even
    when the LLM didn't emit its handle. Pin configs carry no inventory, so they
    normalize as out-of-stock and the LLM tends to drop them from
    ``###SHOW_PRODUCTS###`` — force-including is what "pinning" promises.

    ``reply`` is retained for call-site compatibility but no longer drives
    matching.
    """
    if not carousel_products:
        return []

    pinned = _collect_pinned_products(state, carousel_products)

    if not show_product_handles:
        # No LLM block: still surface configured pins (the promo must render);
        # otherwise nothing (no incidental-mention fallback).
        if pinned:
            result = _force_pins_first(pinned, [], max_results)
            log_with_trace_id(
                state,
                f"🛒 Carousel: showing {len(result)} pinned product(s) "
                f"(LLM emitted no SHOW_PRODUCTS block)",
                "info",
            )
            return result
        log_with_trace_id(
            state,
            "🛒 Carousel: suppressed — LLM emitted no SHOW_PRODUCTS block",
            "info",
        )
        return []

    matched = _match_by_handles(carousel_products, show_product_handles, max_results)
    if pinned:
        matched = _force_pins_first(pinned, matched, max_results)
    if matched:
        log_with_trace_id(
            state,
            f"🛒 Carousel: handle-based match — {[_resolve_product_handle(p) for p in matched]}"
            + (f" (incl. {len(pinned)} pinned)" if pinned else ""),
            "info",
        )
        return matched

    candidate_handles = [
        _resolve_product_handle(p) or _slugify_for_handle_match(_resolve_product_title(p))
        for p in carousel_products
        if isinstance(p, dict)
    ]
    log_with_trace_id(
        state,
        f"🛒 Carousel: SHOW_PRODUCTS handles {show_product_handles} matched no candidate "
        f"(available candidates: {candidate_handles}) — showing nothing. "
        f"Likely the LLM emitted handles not grounded in this turn's tool results.",
        "warning",
    )
    return []


# ═══════════════════════════════════════════════════════════════
# TRACK SHOWN PRODUCTS - For context switching
# ═══════════════════════════════════════════════════════════════

# Handles whose carousel cards have been shown this session. Surfaced to the LLM
# (via the "CARDS ALREADY DISPLAYED THIS SESSION" context line) so it avoids
# re-emitting them in ###SHOW_PRODUCTS### unless the customer explicitly asks.
CAROUSEL_SHOWN_HANDLES_KEY = "carousel_shown_handles"
# Upper bound on the per-session shown-handles blocklist. Kept well below the
# search fetch ceiling (PRODUCT_FETCH_LIMIT_MAX) so the pipeline's "show more"
# fetch-widening (which adds this list's length) stays small and can never
# saturate — guaranteeing a full page of net-new products survives the exclusion.
# Most-recent-wins: older handles age out, so a very long session may eventually
# re-show a product seen long ago (acceptable) rather than claim false scarcity.
CAROUSEL_SHOWN_HANDLES_MAX = 50


def track_shown_products(state: Dict, products: List[Dict], max_tracked: int = 10):
    """
    Track products shown in carousel for context switching.
    
    When user says "rebel club wale ka" (the rebel club one), the LLM
    can look up from shown_products to understand which product they mean.
    
    Args:
        state: Session state dict
        products: List of product dicts from carousel
        max_tracked: Maximum products to track (FIFO)
    """
    if not products:
        return
    
    # Initialize if not exists
    if "shown_products" not in state:
        state["shown_products"] = []
    
    # Add new products (avoid duplicates by handle)
    existing_handles = {p.get("handle") for p in state["shown_products"]}
    
    for product in products:
        handle = product.get("handle")
        if handle and handle not in existing_handles:
            state["shown_products"].append({
                "handle": handle,
                "title": product.get("title") or product.get("name", ""),
                "price": product.get("price"),
                "url": product.get("url"),
            })
            existing_handles.add(handle)
    
    # Keep only recent products (FIFO)
    if len(state["shown_products"]) > max_tracked:
        state["shown_products"] = state["shown_products"][-max_tracked:]
    
    logger.info(f"📦 Tracking {len(state['shown_products'])} shown products: {[p.get('title', p.get('handle', '?'))[:20] for p in state['shown_products']]}")


def _carousel_product_dedupe_key(product: Dict) -> Optional[str]:
    """Stable key for webchat carousel idempotency (handle, else /products/{{handle}} from url)."""
    if not isinstance(product, dict):
        return None
    h = product.get("handle")
    if h is not None and str(h).strip():
        return str(h).strip().lower()
    url = product.get("url")
    if url:
        m = re.search(r"/products/([^/?#]+)", str(url))
        if m:
            return m.group(1).lower()
    return None


def _dedupe_carousel_payload(formatted: List[Dict]) -> List[Dict]:
    """Collapse duplicate handles within a single outgoing carousel payload.

    Cross-turn "already shown" suppression is intentionally NOT done here. The LLM
    is the decider across turns: it sees the "CARDS ALREADY DISPLAYED THIS SESSION"
    list in its context and omits those handles from ###SHOW_PRODUCTS### by default,
    re-including one only when the customer explicitly asks to see it again. This
    only guards against the same handle appearing twice in one payload (e.g. the
    LLM repeated a handle in its block).
    """
    if not formatted:
        return formatted
    out: List[Dict] = []
    seen: set = set()
    for f in formatted:
        key = _carousel_product_dedupe_key(f)
        if key:
            if key in seen:
                continue
            seen.add(key)
        out.append(f)
    return out


def _record_sent_carousel_handles(
    state: Dict, formatted: List[Dict]
) -> None:
    """Record shown handles so they can be surfaced to the LLM next turn.

    Feeds the "CARDS ALREADY DISPLAYED THIS SESSION" context line so the LLM can
    avoid re-showing these unless the customer explicitly asks."""
    if not formatted:
        return
    if not state.get(CAROUSEL_SHOWN_HANDLES_KEY):
        state[CAROUSEL_SHOWN_HANDLES_KEY] = []
    shown = state[CAROUSEL_SHOWN_HANDLES_KEY]
    seen = {str(x).strip().lower() for x in shown if x}
    for f in formatted:
        k = _carousel_product_dedupe_key(f)
        if k and k not in seen:
            shown.append(k)
            seen.add(k)
    # Bound the blocklist (most-recent-wins) so it can never grow past the search
    # fetch window and starve "show more". Drop the oldest handles beyond the cap.
    if len(shown) > CAROUSEL_SHOWN_HANDLES_MAX:
        del shown[: len(shown) - CAROUSEL_SHOWN_HANDLES_MAX]


async def afetch_clients_from_database() -> Dict[str, str]:
    """Async variant of fetch_clients_from_database."""
    try:
        from fashion_bot.database_manager import awith_retry, get_async_postgres_connection

        @awith_retry
        async def _read_clients():
            async with get_async_postgres_connection() as conn:
                async with conn.cursor() as cur:
                    query = 'SELECT id, "name" FROM public.clients WHERE "name" IS NOT NULL'
                    await cur.execute(query)
                    return await cur.fetchall()

        logger.info("📡 Connecting to database to fetch clients asynchronously...")
        rows = await _read_clients()

        logger.info(f"📊 Found {len(rows)} rows in clients table (async)")

        mapping = {}
        for row in rows:
            if isinstance(row, dict):
                client_id = row.get('id')
                client_name = row.get('name')
            else:
                client_id, client_name = row[0], row[1]

            if client_name:
                normalized_name = client_name.lower().strip()
                mapping[normalized_name] = str(client_id)
                logger.info(f"  ✓ Loaded async: '{client_name}' → {client_id}")

        logger.info(f"✅ Loaded {len(mapping)} clients from database asynchronously: {list(mapping.keys())}")
        return mapping

    except ImportError as e:
        logger.error(f"❌ Import error: {e}. Make sure psycopg is installed.")
        return {}
    except Exception as e:
        logger.error(f"❌ Failed to fetch clients from database asynchronously: {e}")
        report_error(
            "Failed to fetch clients from database",
            level='error',
            exc_info=(type(e), e, e.__traceback__),
        )
        return {}


async def _alink_attribution_events_to_conversation(
    client_id: str, session_id: str, conversation_id: str
) -> None:
    """
    Backfill conversation_id onto this session's chat_attribution_events rows.

    Lets order-attribution joins to messages.tags to confirm a chat was
    genuine pre-sales talk, since the widget posts events without knowing
    the server-side conversation_id.
    """
    if not session_id or not conversation_id:
        return
    try:
        from fashion_bot.database_manager import get_async_postgres_connection

        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE chat_attribution_events
                    SET conversation_id = %s
                    WHERE session_id = %s AND client_id = %s AND conversation_id IS NULL
                    """,
                    (conversation_id, session_id, client_id),
                )
    except Exception as e:
        logger.error(f"[WEBSOCKET_CHAT] Failed to link attribution events to conversation: {e}")


_CLIENT_NAME_MAPPING_CACHE_KEY = "websocket:client_name_mapping"
_CLIENT_NAME_MAPPING_TTL = int(_cache_ttl.total_seconds())


async def aget_client_name_mapping() -> Dict[str, str]:
    """Resolve the client_name -> client_id mapping with tiered cache.

    Memory -> Redis -> Postgres per AGENTS.md §3. Replaces the previous
    process-local TTL dict so all pods share the Redis tier and cold
    starts don't each pay the full DB scan.
    """
    global _client_name_cache, _cache_last_updated

    from fashion_bot.utils.redis_client import get_shared_async_redis_client
    from fashion_bot.utils.tiered_cache import aget_with_tiered_cache

    async def _load_from_db() -> Optional[Dict[str, str]]:
        logger.info("🔄 Refreshing client name cache from database asynchronously...")
        mapping = await afetch_clients_from_database()
        # afetch_clients_from_database returns {} on failure — preserve
        # that as "no data" rather than caching an empty mapping in
        # Redis (which would mask the upstream error for 10 minutes).
        return mapping or None

    async def _redis_get() -> Optional[Dict[str, str]]:
        client = await get_shared_async_redis_client()
        if not client:
            return None
        raw = await client.get(_CLIENT_NAME_MAPPING_CACHE_KEY)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) and data else None
        except Exception:
            return None

    async def _redis_set(value: Optional[Dict[str, str]]) -> None:
        if not value:
            return
        client = await get_shared_async_redis_client()
        if client:
            await client.setex(
                _CLIENT_NAME_MAPPING_CACHE_KEY,
                _CLIENT_NAME_MAPPING_TTL,
                json.dumps(value),
            )

    mapping, _src = await aget_with_tiered_cache(
        cache_key=_CLIENT_NAME_MAPPING_CACHE_KEY,
        ttl_seconds=_CLIENT_NAME_MAPPING_TTL,
        load_from_source_fn=_load_from_db,
        get_from_redis_fn=_redis_get,
        set_to_redis_fn=_redis_set,
        cache_none=False,
    )
    if mapping:
        _client_name_cache = mapping
        _cache_last_updated = datetime.utcnow()
    return mapping or {}


async def aresolve_client_name_to_id(client_name: str) -> Optional[str]:
    """Async variant of resolve_client_name_to_id."""
    if not client_name:
        logger.error("❌ No client_name provided")
        return None

    normalized_name = client_name.lower().strip()
    logger.info(f"🔍 Resolving client_name async: '{client_name}' (normalized: '{normalized_name}')")

    mapping = await aget_client_name_mapping()
    logger.info(f"📊 Available clients in async cache: {list(mapping.keys())}")

    client_id = mapping.get(normalized_name)

    if client_id:
        logger.info(f"✅ Resolved '{client_name}' → {client_id[:8]}... (async)")
    elif mapping:
        # Unknown client name with a populated mapping is a user-facing
        # condition: caller returns a friendly error to the WS client.
        logger.warning(
            f"⚠️ Client name '{client_name}' (normalized: '{normalized_name}') "
            f"not found among {len(mapping)} known clients"
        )
    else:
        # Empty mapping means the upstream DB fetch failed AND Redis cache
        # is also empty — this is a real server-side outage, not a
        # user-facing condition. Escalate so on-call sees it.
        logger.error(
            f"❌ Client name '{client_name}' lookup failed — cache is empty "
            f"(DB fetch failed and Redis tier returned nothing); incoming "
            f"websocket connections cannot be authorised."
        )
        try:
            report_error(
                "Websocket client_name cache is empty",
                level="error",
                client_name=client_name,
            )
        except Exception:
            pass

    return client_id


async def aresolve_websocket_client_identifier(client_identifier: str) -> Optional[str]:
    """
    Resolve websocket path identifier into canonical client_id.

    Supported path values:
    - Encoded client_id token (preferred)
    - Raw client UUID
    - Legacy client name
    """
    raw = (client_identifier or "").strip()
    if not raw:
        return None

    if is_encoded_client_id(raw):
        try:
            decoded = decode_client_id(raw).strip()
            if decoded:
                logger.info("✅ Resolved websocket identifier via encoded client_id token")
                return decoded
        except ValueError:
            logger.warning("⚠️ Failed to decode websocket encoded client_id token: %s", raw[:24])

    try:
        raw_uuid = str(uuid.UUID(raw))
        logger.info("✅ Resolved websocket identifier as raw client UUID")
        return raw_uuid
    except (ValueError, TypeError):
        pass

    return await aresolve_client_name_to_id(raw)


def create_initial_state(session_id: str, phone_number: Optional[str] = None) -> Dict:
    """
    Create initial state for a new chat session.
    Matches the structure from streamlit_app.py and gupshup_webhook.py
    
    NOTE: This function is kept for backward compatibility.
    The unified cache now creates states directly.
    """
    return {
        "messages": [],
        "product_info": "",
        "phone_number": phone_number or session_id,  # Use session_id as fallback identifier
        "selected_order_id": None,
        "known_orders": None,
        "order_status_by_id": None,
        "is_order_query": None,
        "is_frustrated": None,
        "needs_escalation": None,
        "needs_human_agent": None,
        "scratchpad": None,
        "client_id": None,  # Will be set from client_id parameter
        "trace_id": None,  # Will be set per message
        "session_id": session_id,
        "carousel_shown_handles": [],  # Session-wide carousel idempotency (see CAROUSEL_SHOWN_HANDLES_KEY)
        "session_created_at": datetime.now(timezone.utc).isoformat(),
        "last_activity": datetime.now(timezone.utc).isoformat()
    }


async def aget_or_create_session(session_id: str, client_id: str, phone_number: Optional[str] = None) -> Dict:
    """Async variant of get_or_create_session using async cache methods."""
    cache = get_unified_cache()
    user_id = phone_number if phone_number else session_id

    if not phone_number:
        linked_phone = await cache.aget_phone_for_session(client_id, session_id)
        if linked_phone:
            user_id = linked_phone
            phone_number = linked_phone
            logger.info(f"📱 Found linked phone for session {session_id[:8]}...: {linked_phone}")

    state, is_new = await cache.aget_or_create_state(
        channel=Channel.WEB,
        tenant_id=client_id,
        user_id=user_id,
        client_id=client_id,
        session_id=session_id
    )

    if "session_id" not in state or not state.get("session_id"):
        state["session_id"] = session_id
    if "client_id" not in state or not state.get("client_id"):
        state["client_id"] = client_id
    if phone_number:
        state["phone_number"] = phone_number
    
    # Session-wide carousel idempotency: never repeat a product card in a session
    if CAROUSEL_SHOWN_HANDLES_KEY not in state:
        state[CAROUSEL_SHOWN_HANDLES_KEY] = []
    
    # Build session dict (for backward compatibility with existing code)
    session = {
        "session_id": session_id,
        "client_id": client_id,
        "state": state,
        "thread_id": cache.generate_thread_id(Channel.WEB, client_id, user_id),
        "created_at": state.get("session_created_at", datetime.now(timezone.utc).isoformat()),
        "last_activity": state.get("last_message_at", datetime.now(timezone.utc).isoformat())
    }

    if is_new:
        logger.info(f"✨ Created new async session: {session_id[:8]}... for client: {client_id[:8]}... (Redis-backed)")
    else:
        logger.debug(f"♻️ Retrieved existing async session from Redis: {session_id[:8]}...")

    return session


async def aupdate_session_state(session: Dict) -> None:
    """Async variant of update_session_state."""
    cache = get_unified_cache()
    thread_id = session.get("thread_id")
    if thread_id:
        await cache.aupdate_state(thread_id, session["state"])
    else:
        logger.warning(f"⚠️ No thread_id in session, cannot update Redis")


async def amigrate_session_to_phone(session: Dict, phone_number: str) -> Dict:
    """
    Migrate an anonymous session to a phone-based session (async).

    This enables cross-device continuity: same phone = same conversation history.

    Returns updated session dict with new thread_id.
    """
    cache = get_unified_cache()
    client_id = session["client_id"]
    session_id = session["session_id"]

    new_thread_id, was_migrated = await cache.amigrate_session_to_phone(
        client_id=client_id,
        session_id=session_id,
        phone_number=phone_number
    )

    session["thread_id"] = new_thread_id
    session["state"]["phone_number"] = phone_number

    if was_migrated:
        logger.info(f"📱 Migrated session {session_id[:8]}... to phone {phone_number}")
    else:
        logger.info(f"Linked session {session_id[:8]}... to existing phone thread: {phone_number}")

    return session


def cleanup_expired_sessions():
    """
    Clean up expired session connections.
    
    NOTE: With Redis-backed state, session STATE is automatically expired by Redis TTL.
    This function only cleans up orphaned WebSocket connection tracking.
    """
    # With Redis TTL handling state expiry, this is now mostly a no-op
    # We only track active WebSocket connections, not session state
    logger.debug("🧹 cleanup_expired_sessions called - Redis TTL handles state expiry automatically")


@track_turn_metrics()
async def process_message(
    message: str,
    session: Dict,
    trace_id: str
) -> str:
    """
    Process user message through the graph.
    Follows the same pattern as streamlit_app.py
    
    Now supports page context for context-aware chat:
    - If user is on a product page, LLM has access to product handle/title
    - Can answer questions like "what sizes does this come in?" without asking for URL
    """
    try:
        state = session["state"]
        client_id = session["client_id"]
        session_id = session["session_id"]
        phone_number = state.get("phone_number")  # May be None for anonymous users
        guest_at_start = not _webchat_has_verified_phone(state)
        
        # Determine user identifier: phone if available, else session_id
        user_identifier = phone_number if phone_number else session_id
        is_identified = _webchat_has_verified_phone(state)
        
        # Log page context if available (context-aware chat)
        page_context = state.get("page_context")
        if page_context:
            log_with_trace_id(state, f"📍 Processing with page context: {page_context.get('pageType')} - {page_context.get('productHandle', 'N/A')}", "info")
        
        log_with_trace_id(state, f"💬 Web chat message from {client_id}: {message[:50]}...", "info")
        log_with_trace_id(state, f"👤 User: {'identified (phone)' if is_identified else 'anonymous (session_id)'}", "info")
        
        # Store customer message in database (before processing)
        conv_id = None
        try:
            log_with_trace_id(state, f"💾 Storing customer message to database (conv_hint: {state.get('conversation_id')})", "info")
            conv_id = await _webchat_store_inbound_customer_row(
                state=state,
                client_id=client_id,
                session_id=session_id,
                message=message,
            )
            log_with_trace_id(state, f"✅ Stored customer message, conv_id: {conv_id}", "info")
            
        except Exception as e:
            log_with_trace_id(state, f"⚠️ Failed to store customer message: {e}", "warning")
            report_error(
                "Failed to store customer message (process_message)",
                level='warning',
                exc_info=(type(e), e, e.__traceback__),
                session_id=session.get("session_id"),
                client_id=session.get("client_id"),
            )
        
        # Add user message to state (same as streamlit_app)
        state["messages"] = state.get("messages", []) + [timestamped_human_message(message)]
        state["trace_id"] = trace_id
        state["_skip_final_answer"] = _should_skip_final_answer_for_webchat()
        
        # CRITICAL: Ensure session_id is preserved in state for web chat detection
        # This is needed by escalation_handler to detect web chat vs WhatsApp
        if not state.get("session_id"):
            state["session_id"] = session_id
        if not state.get("phone_number"):
            # For web chat, use session_id as phone_number fallback (starts with "web_")
            state["phone_number"] = user_identifier
        
        # Set client context (same as gupshup_webhook)
        from fashion_bot.client_context import set_client_id
        set_client_id(client_id)
        log_with_trace_id(state, f"🔧 Set client_id in ContextVar: {client_id}", "info")
        
        # Invoke through centralized runtime contract inside one root trace.
        with _open_webchat_turn_trace(
            name="webchat-conversation",
            trace_id=trace_id,
            user_identifier=user_identifier,
            client_id=client_id,
            input_message=message,
        ) as turn_run:
            if turn_run is not None and hasattr(turn_run, "id"):
                state["_langsmith_trace_id"] = str(turn_run.id)

            runtime = _get_websocket_runtime()
            runtime_holder: Dict[str, str] = {}

            async def _execute_turn(_ctx):
                log_with_trace_id(state, "🚀 Invoking graph with LangSmith tracing...", "info")
                result, langsmith_trace_id = await invoke_graph_with_tracing(
                    state,
                    trace_id,
                    user_identifier,
                    input_message=message,
                    trace_graph_internally=False,
                )
                runtime_holder["langsmith_trace_id"] = langsmith_trace_id
                log_with_trace_id(state, f"✅ Graph execution completed, langsmith_trace_id: {langsmith_trace_id}", "info")

                if "messages" in result and result["messages"]:
                    final_message = result["messages"][-1]
                    reply_text = final_message.content if hasattr(final_message, "content") else str(final_message)
                elif "customer_message" in result:
                    reply_text = result["customer_message"]
                else:
                    reply_text = ""

                return RuntimeResult(handled=True, reply_text=reply_text, state_snapshot=state), result

            result_holder: Dict[str, dict] = {}

            async def _execute_wrapper(ctx):
                rr, graph_result = await _execute_turn(ctx)
                result_holder["graph_result"] = graph_result
                return rr

            run_result = await runtime.run_turn(
                channel="web",
                client_id=client_id,
                user_id=user_identifier,
                inbound_payload={"_runtime_message_text": message},
                execute_fn=_execute_wrapper,
                trace_id=trace_id,
            )
            session["_last_turn_queued"] = False
            if run_result.queued:
                queued_msg = run_result.reply_text or "I am still processing your previous message. This message has been queued."
                log_with_trace_id(state, f"⏳ Turn queued by single-flight: {queued_msg}", "info")
                session["_last_turn_queued"] = True
                _replace_trace_io(turn_run, outputs={"output": "", "queued": True})
                # Queue notice is internal-only; do not return customer-visible text.
                return ""

            result = result_holder.get("graph_result") or {}
            langsmith_trace_id = runtime_holder.get("langsmith_trace_id") or trace_id
            log_with_trace_id(state, f"🧮 Runtime metrics: redis_calls={run_result.redis_calls_total}, db_calls={run_result.db_calls_total}, degraded={run_result.degraded_mode}", "info")
            
            # Update session state with result (same as streamlit_app)
            if "messages" in result and result["messages"]:
                state["messages"] = result["messages"]
            
            for key, value in result.items():
                if key == "messages":
                    continue
                state[key] = value

            # NOTE: Tags are now generated asynchronously after reply is sent (async_tag_generator.py)

            # Backfill conversation_id onto this session's attribution events so
            # order attribution can later join to messages.tags and confirm the
            # chat was genuine pre-sales talk, not just a greeting or support query.
            conv_id_for_attribution = state.get("conversation_id")
            if conv_id_for_attribution:
                asyncio.create_task(
                    _alink_attribution_events_to_conversation(
                        client_id=client_id,
                        session_id=session_id,
                        conversation_id=conv_id_for_attribution,
                    )
                )

            # Extract the final response (same as streamlit_app)
            response = None
            if "messages" in result and result["messages"]:
                final_message = result["messages"][-1]
                if hasattr(final_message, 'content'):
                    response = final_message.content
                else:
                    response = str(final_message)
            elif "customer_message" in result:
                response = result['customer_message']
            else:
                response = "I apologize, but I couldn't generate a response. Please try again."
            _replace_trace_io(
                turn_run,
                outputs={
                    "output": str(response or "")[:2000],
                },
            )
            
            log_with_trace_id(state, f"📤 Response: {response[:100]}...", "info")
            
            # Store bot response in database (after processing)
            try:
                from fashion_bot.history.postgres_conversations import astore_conversation_event
                
                log_with_trace_id(state, f"💾 Storing bot response to database (conv_id: {conv_id}, langsmith_id: {langsmith_trace_id})", "info")
                
                with traced_operation(
                    "webchat.io.store_bot_transcript",
                    metadata={"client_id": client_id, "user_suffix": str(user_identifier)[-6:]},
                    require_parent=True,
                ):
                    await astore_conversation_event(
                        client_id=client_id,
                        phone=user_identifier,  # phone or session_id
                        sender="bot",
                        text=response,
                        channel_type="web-chat",  # ✅ Tagged as web-chat
                        started_by="customer",
                        customer_id=user_identifier,
                        customer_info=json.dumps({"is_identified": is_identified, "source": "web-widget"}),
                        conversation_id=conv_id,  # Link to same conversation
                        langsmith_id=langsmith_trace_id,  # Store actual LangSmith trace ID
                    )

                log_with_trace_id(state, "✅ Stored bot response", "info")
                
            except Exception as e:
                log_with_trace_id(state, f"⚠️ Failed to store bot response: {e}", "warning")
                report_error(
                    "Failed to store bot response (process_message)",
                    level='warning',
                    exc_info=(type(e), e, e.__traceback__),
                    session_id=session.get("session_id"),
                    client_id=session.get("client_id"),
                )
            
            _webchat_increment_guest_exchange_if_applicable(
                state, guest_at_start=guest_at_start, turn_was_queued=False
            )

            # Save updated state to Redis (once, after guest exchange update)
            with traced_operation(
                "webchat.io.persist_session_state",
                metadata={"client_id": client_id, "user_suffix": str(user_identifier)[-6:]},
                require_parent=True,
            ):
                await aupdate_session_state(session)
            log_with_trace_id(state, "💾 State saved to Redis", "debug")

            # 🏷️ Async tag generation — fire-and-forget after response is ready
            try:
                from fashion_bot.async_tag_generator import generate_tags_async
                generate_tags_async(
                    client_id=client_id,
                    conversation_id=conv_id,
                    phone_number=user_identifier,
                    user_message=message,
                    bot_reply=response or "",
                    trace_id=trace_id,
                )
            except Exception as async_tag_err:
                log_with_trace_id(state, f"⚠️ Failed to start async tagging: {async_tag_err}", "warning")

        return response
        
    except Exception as e:
        logger.error(f"❌ Error processing message: {str(e)}")
        log_with_trace_id(session["state"], f"Error: {str(e)}", "error")
        report_error(
            "Error processing message",
            level='error',
            exc_info=(type(e), e, e.__traceback__),
            session_id=session.get("session_id"),
            client_id=session.get("client_id"),
            trace_id=trace_id,
        )
        return f"I'm sorry, there was an error processing your request. Please try again."


def _is_state_only_stream_event(event: Dict) -> bool:
    """Guardrail: keep runtime/state metadata events out of websocket UI stream."""
    event_type = str((event or {}).get("type") or "").strip().lower()
    if event_type in {"state_update", "context_update", "runtime_metrics", "metadata"}:
        return True
    if event_type:
        return False
    return bool((event or {}).get("state_snapshot") or (event or {}).get("result"))


async def _send_websocket_event_for_ui(
    *,
    websocket: WebSocket,
    payload: Dict,
    warning_message: str,
) -> bool:
    """Best-effort websocket send with local warning log."""
    return await _send_json_with_timeout(
        websocket,
        payload,
        warning_message=warning_message,
    )


async def _store_webchat_bot_transcript_streaming_for_ui(
    *,
    state: Dict,
    client_id: str,
    user_identifier: str,
    is_identified: bool,
    conv_id: Optional[str],
    full_response: str,
    session_id: str,
) -> None:
    """Persist bot transcript for webchat streaming turns (best effort)."""
    try:
        from fashion_bot.history.postgres_conversations import astore_conversation_event

        langsmith_trace_id = state.get("_langsmith_trace_id")
        with traced_operation(
            "webchat.io.store_bot_transcript_streaming",
            metadata={"client_id": client_id, "user_suffix": str(user_identifier)[-6:]},
            require_parent=True,
        ):
            await astore_conversation_event(
                client_id=client_id,
                phone=user_identifier,
                sender="bot",
                text=full_response,
                channel_type="web-chat",
                started_by="customer",
                customer_id=user_identifier,
                customer_info=json.dumps({"is_identified": is_identified, "source": "web-widget"}),
                conversation_id=conv_id,
                langsmith_id=langsmith_trace_id,
            )
    except Exception as e:
        log_with_trace_id(state, f"⚠️ Failed to store bot response: {e}", "warning")
        report_error(
            "Failed to store bot response (streaming)",
            level='warning',
            exc_info=(type(e), e, e.__traceback__),
            session_id=session_id,
            client_id=client_id,
        )


async def _persist_webchat_session_state_for_ui(
    *,
    session: Dict,
    state: Dict,
    client_id: str,
    user_identifier: str,
) -> None:
    """Persist websocket session state with explicit tracing/log wrapper."""
    with traced_operation(
        "webchat.io.persist_session_state_streaming",
        metadata={"client_id": client_id, "user_suffix": str(user_identifier)[-6:]},
        require_parent=True,
    ):
        await aupdate_session_state(session)
    log_with_trace_id(state, "💾 State saved to Redis", "debug")


def _apply_stream_result_to_webchat_state(
    *,
    state: Dict,
    result: Dict,
) -> None:
    """Apply runtime stream result payload to websocket in-memory state."""
    if "messages" in result and result["messages"]:
        state["messages"] = result["messages"]
    for key, value in result.items():
        if key == "messages":
            continue
        state[key] = value


async def _execute_turn_run_graph_and_update_state_for_webchat(
    _ctx,
    *,
    state: Dict,
    message: str,
    client_id: str,
    trace_graph_internally: bool = True,
):
    """Run graph stream for webchat and apply end-event state result in-place."""
    from fashion_bot.core.streaming_service import stream_graph_response

    async for event in stream_graph_response(
        state,
        message,
        client_id,
        trace_graph_internally=trace_graph_internally,
    ):
        if str((event or {}).get("type") or "") == "end":
            result_obj = event.get("result")
            if isinstance(result_obj, dict):
                _apply_stream_result_to_webchat_state(
                    state=state,
                    result=result_obj,
                )
        yield event


async def _consume_websocket_runtime_stream_events(
    *,
    runtime,
    websocket: WebSocket,
    state: Dict,
    client_id: str,
    user_identifier: str,
    message: str,
    trace_id: str,
    execute_stream_fn,
) -> Tuple[bool, str, Optional[Dict]]:
    """Consume runtime stream events for websocket channel and relay user-visible output."""
    full_response = ""
    result: Optional[Dict] = None
    turn_was_queued = False
    # If the websocket send fails (client disconnected mid-stream) we stop emitting
    # to the UI but keep consuming the runtime stream so the full reply is still
    # accumulated and persisted to the conversation history.
    client_disconnected = False

    async for event in runtime.run_turn_stream(
        channel="web",
        client_id=client_id,
        user_id=user_identifier,
        inbound_payload={"_runtime_message_text": message, "streaming": True},
        execute_stream_fn=execute_stream_fn,
        trace_id=trace_id,
    ):
        if _is_state_only_stream_event(event):
            continue

        event_type = str((event or {}).get("type") or "")
        if event_type == "queued":
            turn_was_queued = True
            queued_msg = str(event.get("message") or "I am still processing your previous message. This message has been queued.")
            log_with_trace_id(state, f"⏳ Streaming turn queued by single-flight: {queued_msg}", "info")
            # Queue notice is internal-only; do not stream text to customer.
            continue

        if event_type == "start":
            continue

        if event_type in _UI_SIGNAL_EVENTS:
            # stream_reset: the agent is retrying after a transient LLM error —
            # discard any partially-streamed text so the retry starts clean.
            if event_type == "stream_reset":
                full_response = ""
            # Forward node-emitted UI signals (suggestions / track_order /
            # phone_captured) verbatim. Best-effort — a failed send must not abort
            # the turn (the customer reply has already streamed).
            await _send_websocket_event_for_ui(
                websocket=websocket,
                payload={**event, "timestamp": datetime.now(timezone.utc).isoformat()},
                warning_message=f"⚠️ Failed to send UI signal '{event_type}'",
            )
            continue

        if event_type == "token":
            token = str(event.get("content") or "")
            if token:
                full_response += token
                if not client_disconnected:
                    sent = await _send_websocket_event_for_ui(
                        websocket=websocket,
                        payload={
                            "type": "stream",
                            "token": token,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        },
                        warning_message="⚠️ Failed to send stream token (client likely disconnected)",
                    )
                    if not sent:
                        client_disconnected = True
            continue

        if event_type == "tool_start":
            tool_name = event.get("tool", "")
            log_with_trace_id(state, f"🔧 Tool started: {tool_name}", "debug")
            continue

        if event_type == "tool_end":
            tool_name = event.get("tool", "")
            log_with_trace_id(state, f"✅ Tool completed: {tool_name}", "debug")
            continue

        if event_type == "end":
            full_response = str(event.get("full_response", full_response) or full_response)
            result = event.get("result") if isinstance(event.get("result"), dict) else result
            if event.get("queued"):
                turn_was_queued = True
                full_response = ""
            if not client_disconnected:
                await _send_websocket_event_for_ui(
                    websocket=websocket,
                    payload={
                        "type": "end",
                        "full_response": full_response,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "queued": bool(event.get("queued")),
                    },
                    warning_message="⚠️ Failed to send end event",
                )
            if not turn_was_queued:
                langsmith_id = state.get("_langsmith_trace_id") or ""
                log_with_trace_id(state, f"✅ Streaming completed: {len(full_response)} chars langsmith={langsmith_id[:12]} reply={full_response[:120]}...", "info")
            continue

        if event_type == "error":
            error_msg = str(event.get("message") or "Unknown error")
            log_with_trace_id(state, f"❌ Streaming error: {error_msg}", "error")
            full_response = "I apologize, but I encountered an error. Please try again."
            if not client_disconnected:
                await _send_websocket_event_for_ui(
                    websocket=websocket,
                    payload={
                        "type": "stream",
                        "token": full_response,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                    warning_message="⚠️ Failed to send streaming error token",
                )

    return turn_was_queued, full_response, result


@track_turn_metrics()
async def process_message_streaming(
    websocket: WebSocket,
    message: str,
    session: Dict,
    trace_id: str
) -> str:
    """
    Process user message with streaming response.
    Sends tokens to websocket as they are generated.
    
    Returns the complete response for logging purposes.
    """
    try:
        state = session["state"]
        client_id = session["client_id"]
        session_id = session["session_id"]
        phone_number = state.get("phone_number")
        guest_at_start = not _webchat_has_verified_phone(state)
        
        user_identifier = phone_number if phone_number else session_id
        is_identified = _webchat_has_verified_phone(state)
        
        log_with_trace_id(state, f"💬 web_chat msg={message[:50]}...", "info")

        # Store customer message in database (before processing)
        conv_id = None
        page_context = state.get("page_context")
        try:
            conv_id = await _webchat_store_inbound_customer_row(
                state=state,
                client_id=client_id,
                session_id=session_id,
                message=message,
            )
            
        except Exception as e:
            log_with_trace_id(state, f"⚠️ Failed to store customer message: {e}", "warning")
            report_error(
                "Failed to store customer message (streaming)",
                level='warning',
                exc_info=(type(e), e, e.__traceback__),
                session_id=session_id,
                client_id=client_id,
            )
        
        # Add user message to state
        state["messages"] = state.get("messages", []) + [timestamped_human_message(message)]
        state["trace_id"] = trace_id
        state["_skip_final_answer"] = _should_skip_final_answer_for_webchat()
        
        # CRITICAL: Ensure session_id is preserved in state for web chat detection
        # This is needed by escalation_handler to detect web chat vs WhatsApp
        if not state.get("session_id"):
            state["session_id"] = session_id
        if not state.get("phone_number"):
            # For web chat, use session_id as phone_number fallback (starts with "web_")
            state["phone_number"] = user_identifier
        
        # Set client context
        from fashion_bot.client_context import set_client_id
        set_client_id(client_id)
        
        with _open_webchat_turn_trace(
            name="general-streaming-conversation",
            trace_id=trace_id,
            user_identifier=user_identifier,
            client_id=client_id,
            input_message=message,
        ) as turn_run:
            if turn_run is not None and hasattr(turn_run, "id"):
                state["_langsmith_trace_id"] = str(turn_run.id)

            runtime = _get_websocket_runtime()

            log_with_trace_id(state, "🚀 Starting streaming response...", "debug")
            turn_was_queued, full_response, result = await _consume_websocket_runtime_stream_events(
                runtime=runtime,
                websocket=websocket,
                state=state,
                client_id=client_id,
                user_identifier=user_identifier,
                message=message,
                trace_id=trace_id,
                execute_stream_fn=lambda ctx: _execute_turn_run_graph_and_update_state_for_webchat(
                    ctx,
                    state=state,
                    message=message,
                    client_id=client_id,
                    trace_graph_internally=False,
                ),
            )

            if turn_was_queued:
                _replace_trace_io(turn_run, outputs={"output": "", "queued": True})
                return ""
            
            # Update session state with result
            if result:
                _apply_stream_result_to_webchat_state(
                    state=state,
                    result=result,
                )
                
                # NOTE: Tags are now generated asynchronously after reply is sent (async_tag_generator.py)
            _replace_trace_io(
                turn_run,
                outputs={
                    "output": str(full_response or "")[:2000],
                },
            )
            
            await _store_webchat_bot_transcript_streaming_for_ui(
                state=state,
                client_id=client_id,
                user_identifier=user_identifier,
                is_identified=is_identified,
                conv_id=conv_id,
                full_response=full_response,
                session_id=session_id,
            )

            # NOTE: Tags are now generated asynchronously after reply is sent (async_tag_generator.py)

            # Backfill conversation_id onto this session's attribution events -
            # mirrors process_message (non-streaming). Streaming is the default
            # (use_streaming = data.get("streaming", True)), so this path
            # handles the vast majority of production traffic; without this
            # call here, chat_attribution_events.conversation_id could only
            # ever be filled in by the insert-time lookup in
            # attribution_router.py, which itself only matches a still-live
            # conversation keyed by session_id-as-phone - leaving it
            # permanently NULL for any resumed chat and for any customer
            # whose identity has since migrated to a verified phone number.
            if conv_id:
                asyncio.create_task(
                    _alink_attribution_events_to_conversation(
                        client_id=client_id,
                        session_id=session_id,
                        conversation_id=conv_id,
                    )
                )

        _webchat_increment_guest_exchange_if_applicable(
            state, guest_at_start=guest_at_start, turn_was_queued=False
        )

        # Save updated state to Redis
        await _persist_webchat_session_state_for_ui(
            session=session,
            state=state,
            client_id=client_id,
            user_identifier=user_identifier,
        )
        
        # 🏷️ Async tag generation — fire-and-forget after streaming response is complete
        try:
            from fashion_bot.async_tag_generator import generate_tags_async
            generate_tags_async(
                client_id=client_id,
                conversation_id=conv_id,
                phone_number=user_identifier,
                user_message=message,
                bot_reply=full_response or "",
                trace_id=trace_id,
            )
        except Exception as async_tag_err:
            log_with_trace_id(state, f"⚠️ Failed to start async tagging: {async_tag_err}", "warning")
        
        return full_response
        
    except Exception as e:
        logger.error(f"❌ Error in streaming message: {str(e)}")
        log_with_trace_id(session["state"], f"Streaming Error: {str(e)}", "error")
        report_error(
            "Error in streaming message",
            level='error',
            exc_info=(type(e), e, e.__traceback__),
            session_id=session.get("session_id"),
            client_id=session.get("client_id"),
            trace_id=trace_id,
        )
        # Send error as final token
        await _send_websocket_event_for_ui(
            websocket=websocket,
            payload={
                "type": "stream",
                "token": "I'm sorry, there was an error processing your request.",
                "timestamp": datetime.now(timezone.utc).isoformat()
            },
            warning_message="⚠️ Failed to send top-level streaming error token",
        )
        return "I'm sorry, there was an error processing your request. Please try again."


async def _webchat_send_suggestions(websocket: WebSocket, session: Dict) -> None:
    """Send LLM-guided suggestion tiles to the widget (consumed once from state).

    Called AFTER the carousel so the tiles render below the product images.
    Popped from state so they never re-appear on a later turn.
    """
    state = session.get("state") or {}
    suggestions = state.pop("suggestions", None)
    if not suggestions:
        return
    await _send_json_with_timeout(
        websocket,
        {
            "type": "suggestions",
            "suggestions": suggestions[:4],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        warning_message="⚠️ Failed to send suggestions for session %s"
        % ((session.get("session_id") or "")[:8]),
    )
    logger.info(f"💡 Suggestions: sent {len(suggestions[:4])} tiles")


def _upstash_row_to_card_product(row: Dict) -> Dict:
    """Map an Upstash Search result ({content, metadata}) to a carousel product
    dict in the same shape format_product_for_carousel expects (handle/title from
    content; image_url/url/variants from metadata)."""
    content = (row.get("content") or {}) if isinstance(row, dict) else {}
    md = (row.get("metadata") or {}) if isinstance(row, dict) else {}
    return {
        "handle": md.get("handle") or content.get("handle"),
        "title": content.get("title"),
        "name": content.get("title"),
        "url": md.get("product_url"),
        "image_url": md.get("image_url"),
        "all_images": md.get("all_images") or [],
        "price_min": content.get("price_min"),
        "price_max": content.get("price_max"),
        "compare_at_price_min": content.get("compare_at_price_min"),
        "in_stock": content.get("in_stock", True),
        "variants": md.get("variants") or [],
        "metafield_attributes": content.get("metafield_attributes") or {},
    }


async def _aresolve_handles_from_upstash(
    state: Dict, handles: List[str], cap: int = 5
) -> List[Dict]:
    """Last-resort carousel resolver.

    When the LLM emits a handle that the in-memory candidate set could not resolve
    (e.g. a product that aged out of the conversation entities), fetch it straight
    from Upstash by handle. Each recovered product is tagged
    ``handle_resolve="searchdb"`` (an internal/debug provenance flag forwarded to
    the UI; normal in-memory matches are tagged ``"internal"``). Bounded (``cap``)
    and fail-open: any error or miss is logged and skipped — it never raises or
    blocks the carousel.

    Log levels track customer impact, not code path: a successful recovery is the
    fallback doing its job and logs at INFO. Only a handle this lookup could NOT
    resolve (the customer loses a card) or a broken/unavailable Upstash client
    logs at ERROR.

    NOTE: the lookup is an EXACT match on ``handle`` (``filter=handle = '...'``),
    so it recovers only handles the model transcribed verbatim. A near-miss — a
    dropped word, ``tshirt`` vs ``t-shirt`` — misses here even though the product
    is indexed; the ``query``/``semantic_weight`` arguments do not rescue it,
    because the filter is evaluated as a hard predicate. NOTE: relies on
    ``content.handle`` being populated (filterable); the one-time backfill +
    schema-version hash guard keep that true.
    """
    client_id = state.get("client_id")
    handles = [h.strip() for h in (handles or []) if isinstance(h, str) and h.strip()]
    if not client_id or not handles:
        return []
    try:
        from fashion_bot.services.product_ingestion.upstash_search_service import (
            get_upstash_search_service,
        )
        svc = get_upstash_search_service()
    except Exception as e:
        log_with_trace_id(state, f"🛒 handle_resolve: Upstash service unavailable: {e}", "error")
        return []

    recovered: List[Dict] = []
    for h in handles[:cap]:
        safe = h.replace("'", "").replace("\\", "")
        try:
            rows = await svc.asearch(
                query=h.replace("-", " "), client_id=client_id,
                filter_str=f"handle = '{safe}'", limit=1, reranking=False,
            )
        except Exception as e:
            log_with_trace_id(state, f"🛒 handle_resolve: Upstash lookup failed for '{h}': {e}", "error")
            continue
        row = rows[0] if rows else None
        if not row:
            log_with_trace_id(
                state,
                f"🛒 handle_resolve: '{h}' not found in Upstash either — cannot render a card",
                "error",
            )
            continue
        prod = _upstash_row_to_card_product(row)
        if _resolve_product_handle(prod):
            prod["handle_resolve"] = "searchdb"
            recovered.append(prod)
    if recovered:
        log_with_trace_id(
            state,
            f"🛒 handle_resolve: recovered {len(recovered)} product(s) from Upstash "
            f"{[_resolve_product_handle(p) for p in recovered]}",
            "info",
        )
    return recovered


async def _webchat_send_stream_end_and_maybe_carousel(
    websocket: WebSocket,
    session: Dict,
    trace_id: str,
    response: str,
) -> bool:
    """Send stream_end then optional product carousel (same contract as main message loop)."""
    payload: Dict[str, Any] = {
        "type": "stream_end",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trace_id": trace_id,
    }
    if response:
        payload["full_response"] = response
    sent_stream_end = await _send_json_with_timeout(
        websocket,
        payload,
        warning_message="⚠️ Failed to send stream_end for session %s"
        % ((session.get("session_id") or "")[:8]),
    )
    if not sent_stream_end:
        return False

    state = session["state"]

    # When the conversation message-limit gate short-circuited the turn, the
    # canned limit reply is terminal and must stand alone. The gate returns a
    # partial state update and does not clear the previous product turn's
    # carousel state (show_product_handles / entities), so without this guard
    # the widget re-renders a stale carousel and suggestion tiles beneath the
    # limit notice (the gate never selected those products this turn).
    if state.get("conversation_limit_reached"):
        return True

    emitted = [h for h in (state.get("show_product_handles") or [])
               if isinstance(h, str) and h.strip()]

    carousel_products = _products_for_webchat_carousel(state)
    matched = (
        _match_carousel_products_to_reply(
            state, carousel_products, response or "",
            show_product_handles=emitted,
        )
        if carousel_products else []
    )

    # Fallback: any handle the LLM emitted that the in-memory candidate set could
    # NOT resolve (e.g. a product that aged out of the conversation entities) is
    # fetched straight from Upstash and appended (tagged handle_resolve="searchdb").
    # Entering this path is INFO — recovery is the expected outcome and costs the
    # customer nothing. ERROR is reserved for a handle that Upstash cannot resolve
    # either, which is the case where a product card is actually lost.
    if emitted:
        resolved = {_resolve_product_handle(p) for p in matched}
        unmatched = [h for h in emitted if h.strip().lower() not in resolved]
        if unmatched:
            log_with_trace_id(
                state,
                f"🛒 handle_resolve: {len(unmatched)} emitted handle(s) {unmatched} "
                f"not in candidate set — falling back to Upstash",
                "info",
            )
            recovered = await _aresolve_handles_from_upstash(state, unmatched)
            if recovered:
                matched = list(matched) + recovered

    formatted = []
    if matched:
        from fashion_bot.config_manager import aget_judgeme_rating_display_enabled
        rating_enabled = await aget_judgeme_rating_display_enabled(state.get("client_id"))
        formatted = [format_product_for_carousel(p, rating_enabled=rating_enabled) for p in matched if p]
        formatted = [f for f in formatted if f]
        formatted = _dedupe_carousel_payload(formatted)
    if formatted:
        await asyncio.sleep(0.3)
        sent = await _send_json_with_timeout(
            websocket,
            {
                "type": "products",
                "products": formatted,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            warning_message="⚠️ Failed to send carousel for session %s"
            % ((session.get("session_id") or "")[:8]),
        )
        if sent:
            _record_sent_carousel_handles(state, formatted)
            await aupdate_session_state(session)
            logger.info(f"🛒 Carousel: Sent {len(formatted)} products")
        else:
            return False
    # Suggestion tiles go LAST — after stream_end and the carousel — so they
    # render below the product images, in order.
    await _webchat_send_suggestions(websocket, session)
    return True


async def _webchat_pause_turn_require_phone(
    websocket: WebSocket,
    session: Dict,
    *,
    user_message: str,
    trace_id: str,
    client_id: str,
    session_id: str,
    guest_exchanges_completed: int,
    guest_exchange_limit: int,
) -> None:
    """
    Store inbound + append human message; do not run graph. Notifies client (no bot transcript row).
    """
    state = session["state"]
    try:
        await _webchat_store_inbound_customer_row(
            state=state,
            client_id=client_id,
            session_id=session_id,
            message=user_message,
        )
    except Exception as e:
        log_with_trace_id(state, f"⚠️ Phone-gate pause: failed to store inbound: {e}", "warning")
        report_error(
            f"Phone-gate pause store inbound failed: {e}",
            level="warning",
            exc_info=(type(e), e, e.__traceback__),
            session_id=session_id,
            client_id=client_id,
        )

    state["messages"] = state.get("messages", []) + [timestamped_human_message(user_message)]
    state["trace_id"] = trace_id
    state["_skip_final_answer"] = _should_skip_final_answer_for_webchat()
    if not state.get("session_id"):
        state["session_id"] = session_id
    uid = state.get("phone_number") or session_id
    if not state.get("phone_number"):
        state["phone_number"] = uid
    state[WEBCHAT_PENDING_RESUME_KEY] = True
    await aupdate_session_state(session)

    await _send_websocket_event_for_ui(
        websocket=websocket,
        payload={
            "type": "typing",
            "is_typing": False,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        warning_message="⚠️ Failed to send typing false for phone gate",
    )
    await _send_websocket_event_for_ui(
        websocket=websocket,
        payload={
            "type": "phone_required",
            "blocked": True,
            "message": WEBCHAT_PHONE_REQUIRED_MESSAGE,
            "guest_exchanges_completed": guest_exchanges_completed,
            "guest_exchanges_max": guest_exchange_limit,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        warning_message="⚠️ Failed to send phone_required",
    )
    log_with_trace_id(
        state,
        f"📵 Webchat phone gate: paused graph, inbound stored (exchanges_done={guest_exchanges_completed})",
        "info",
    )


@track_turn_metrics()
async def process_message_streaming_resume(
    websocket: WebSocket,
    session: Dict,
    trace_id: str,
    resume_inbound_text: str,
) -> str:
    """
    Run one graph turn after phone verification; user message(s) already in state.
    Clears webchat_pending_graph_resume on success.
    """
    state = session["state"]
    client_id = session["client_id"]
    session_id = session["session_id"]
    phone_number = state.get("phone_number")
    user_identifier = phone_number if phone_number else session_id
    is_identified = _webchat_has_verified_phone(state)
    conv_id = state.get("conversation_id")

    from fashion_bot.client_context import set_client_id

    set_client_id(client_id)
    state["trace_id"] = trace_id
    state["_skip_final_answer"] = _should_skip_final_answer_for_webchat()
    if not state.get("session_id"):
        state["session_id"] = session_id

    runtime = _get_websocket_runtime()
    log_with_trace_id(state, "🚀 [RESUME] Streaming after phone gate...", "info")

    turn_was_queued, full_response, result = await _consume_websocket_runtime_stream_events(
        runtime=runtime,
        websocket=websocket,
        state=state,
        client_id=client_id,
        user_identifier=user_identifier,
        message=resume_inbound_text,
        trace_id=trace_id,
        execute_stream_fn=lambda ctx: _execute_turn_run_graph_and_update_state_for_webchat(
            ctx,
            state=state,
            message=resume_inbound_text,
            client_id=client_id,
        ),
    )

    if turn_was_queued:
        return ""

    if result:
        _apply_stream_result_to_webchat_state(state=state, result=result)

    await _store_webchat_bot_transcript_streaming_for_ui(
        state=state,
        client_id=client_id,
        user_identifier=user_identifier,
        is_identified=is_identified,
        conv_id=conv_id,
        full_response=full_response,
        session_id=session_id,
    )

    # Backfill conversation_id onto this session's attribution events - see
    # process_message_streaming for why this call can't be skipped here too.
    if conv_id:
        asyncio.create_task(
            _alink_attribution_events_to_conversation(
                client_id=client_id,
                session_id=session_id,
                conversation_id=conv_id,
            )
        )

    state.pop(WEBCHAT_PENDING_RESUME_KEY, None)
    await _persist_webchat_session_state_for_ui(
        session=session,
        state=state,
        client_id=client_id,
        user_identifier=user_identifier,
    )

    try:
        from fashion_bot.async_tag_generator import generate_tags_async

        generate_tags_async(
            client_id=client_id,
            conversation_id=conv_id,
            phone_number=user_identifier,
            user_message=resume_inbound_text,
            bot_reply=full_response or "",
            trace_id=trace_id,
        )
    except Exception as async_tag_err:
        log_with_trace_id(state, f"⚠️ Failed to start async tagging (resume): {async_tag_err}", "warning")

    return full_response or ""


@websocket_router.websocket("/ws/chat/{client_name}/{session_id}")
async def websocket_chat_endpoint(
    websocket: WebSocket,
    client_name: str,
    session_id: str,
    api_key: Optional[str] = Query(None),
    embed_parent_origin: Optional[str] = Query(None),
    widget_version: Optional[str] = Query(None),
):
    """
    WebSocket endpoint for web chat widget.
    
    Supports TWO identifier formats:
    1. Encoded client_id (recommended): base64-encoded client_id (from widget using encodedClientId)
    2. Client name (legacy): human-readable client name (resolved via database lookup)
    
    Flow:
    1. Accept WebSocket connection
    2. Detect identifier format and resolve to client_id:
       - If encoded client_id: decode directly (no DB lookup)
       - If client name: resolve via database cache
    3. Cache client_id in session object for entire connection lifetime
    4. All messages in this session use the cached client_id (no re-lookup)
    5. Cache persists until WebSocket disconnects
    
    Args:
        client_identifier: Either base64-encoded client_id OR client_name (auto-detected)
        session_id: Unique session identifier from widget
    """
    import time as _t
    _accept_t0 = _t.monotonic()
    _accept_status = "ok"
    try:
        await websocket.accept()
    except Exception:
        _accept_status = "error"
        raise
    finally:
        get_metrics_collector().record_accept_duration(
            (_t.monotonic() - _accept_t0) * 1000.0,
            status=_accept_status,
        )

    # Track WebSocket connection
    metrics_collector = get_metrics_collector()
    metrics_collector.track_connection(session_id)

    # Step 1: Resolve websocket identifier (encoded token, UUID, or legacy client name)
    client_id = await aresolve_websocket_client_identifier(client_name)

    if not client_id:
        # Unknown clients are a user-facing condition, not a server error.
        # aresolve_client_name_to_id already logs the upstream cause.
        logger.warning(f"⚠️ Rejecting websocket: invalid client name '{client_name}'")
        await _send_json_with_timeout(
            websocket,
            {
                "type": "error",
                "message": "We're having trouble connecting right now. Please try again shortly.",
                "timestamp": datetime.now(timezone.utc).isoformat()
            },
            warning_message=f"⚠️ Failed to send client resolution error for session {session_id[:8]}",
        )
        await _close_websocket_with_timeout(websocket, code=1008, reason="client_resolution_failed")
        metrics_collector.disconnect_connection(session_id)
        return

    origin_hdr = websocket.headers.get("origin")
    host_hdr = websocket.headers.get("host")
    referer_hdr = websocket.headers.get("referer")
    user_agent_hdr = websocket.headers.get("user-agent")
    safe_user_agent = (user_agent_hdr[:180] + "...") if user_agent_hdr and len(user_agent_hdr) > 180 else user_agent_hdr
    if not await verify_widget_api_key_for_client(client_id, api_key):
        logger.warning(
            "❌ WebSocket rejected: invalid or missing widget API key client_id=%s identifier=%s session=%s "
            "widget_version=%s origin=%s embed_parent_origin=%s referer=%s host=%s has_api_key=%s user_agent=%s",
            client_id[:8],
            client_name[:32],
            session_id[:12],
            widget_version,
            origin_hdr,
            embed_parent_origin,
            referer_hdr,
            host_hdr,
            bool(api_key),
            safe_user_agent,
        )
        await _send_json_with_timeout(
            websocket,
            {
                "type": "error",
                "message": "Unauthorized: invalid or missing API key.",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            warning_message=f"⚠️ Failed to send unauthorized error for session {session_id[:8]}",
        )
        await _close_websocket_with_timeout(websocket, code=1008, reason="Unauthorized")
        metrics_collector.disconnect_connection(session_id)
        return

    if not await verify_websocket_embed_for_client(client_id, origin_hdr, embed_parent_origin):
        if not origin_hdr:
            rejection_source = "missing_browser_origin_header"
        elif not embed_parent_origin:
            rejection_source = "legacy_bundle_or_direct_frame_missing_embed_parent_origin"
        else:
            rejection_source = "embed_parent_origin_not_allowlisted"
        logger.warning(
            "❌ WebSocket rejected: origin not allowed client_id=%s identifier=%s session=%s "
            "widget_version=%s rejection_source=%s origin=%s embed_parent_origin=%s referer=%s host=%s "
            "has_api_key=%s user_agent=%s",
            client_id[:8],
            client_name[:32],
            session_id[:12],
            widget_version,
            rejection_source,
            origin_hdr,
            embed_parent_origin,
            referer_hdr,
            host_hdr,
            bool(api_key),
            safe_user_agent,
        )
        await _send_json_with_timeout(
            websocket,
            {
                "type": "error",
                "message": "Forbidden: origin not allowed for this client.",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            warning_message=f"⚠️ Failed to send forbidden origin error for session {session_id[:8]}",
        )
        await _close_websocket_with_timeout(websocket, code=1008, reason="Forbidden origin")
        metrics_collector.disconnect_connection(session_id)
        return

    logger.info(f"🔌 WebSocket connected: client_name={client_name}, client_id={client_id[:8]}..., session={session_id[:8]}...")
    
    # Periodic cleanup
    cleanup_expired_sessions()
    
    # Step 2: Get or create session (client_id is cached in session object)
    session = await aget_or_create_session(session_id, client_id)
    
    # Send welcome message
    if not await _send_json_with_timeout(
        websocket,
        {
            "type": "system",
            "message": "Connected to Fashion Bot",
            "session_id": session_id,
            "timestamp": datetime.now(timezone.utc).isoformat()
        },
        warning_message=f"Failed to send welcome message for session {session_id[:8]}",
    ):
        metrics_collector.disconnect_connection(session_id)
        return
    
    # Track last activity for idle timeout
    last_activity = datetime.now(timezone.utc)
    connection_phase = "idle"
    last_client_message_type = None
    last_turn_trace_id = None
    last_customer_message_at = None
    response_started_at = None
    disconnect_context_logged = False

    def _log_disconnect_context(reason: str) -> None:
        nonlocal disconnect_context_logged
        if disconnect_context_logged:
            return
        disconnect_context_logged = True
        now = datetime.now(timezone.utc)
        idle_seconds = int((now - last_activity).total_seconds()) if last_activity else None
        response_elapsed_seconds = (
            round((now - response_started_at).total_seconds(), 2)
            if response_started_at else None
        )
        customer_wait_seconds = (
            round((now - last_customer_message_at).total_seconds(), 2)
            if last_customer_message_at else None
        )
        waiting_for_response = connection_phase in {
            "waiting_for_bot_response",
            "streaming_response",
            "waiting_for_phone_resume_response",
            "streaming_phone_resume_response",
        }
        normal_disconnect_phase = connection_phase in {
            "idle",
            "closed",
            "idle_timeout",
            "heartbeat",
        }
        is_disruptive = waiting_for_response or not normal_disconnect_phase
        if is_disruptive and customer_wait_seconds and customer_wait_seconds >= _WS_SLOW_DISCONNECT_SECONDS:
            logger.warning(
                "slow_turn_disconnect: session=%s client_id=%s phase=%s "
                "customer_wait_seconds=%s response_elapsed_seconds=%s trace_id=%s reason=%s",
                session_id[:12],
                client_id[:8],
                connection_phase,
                customer_wait_seconds,
                response_elapsed_seconds,
                last_turn_trace_id,
                reason,
            )
        log_fn = logger.error if is_disruptive else logger.info
        log_fn(
            "🔌 WebSocket disconnected: session=%s client_id=%s widget_version=%s "
            "phase=%s waiting_for_response=%s last_client_message_type=%s "
            "idle_seconds=%s customer_wait_seconds=%s response_elapsed_seconds=%s trace_id=%s reason=%s",
            session_id[:12],
            client_id[:8],
            widget_version,
            connection_phase,
            waiting_for_response,
            last_client_message_type,
            idle_seconds,
            customer_wait_seconds,
            response_elapsed_seconds,
            last_turn_trace_id,
            reason,
        )
    
    try:
        while True:
            # Check for idle timeout (if enabled)
            if WEBSOCKET_IDLE_TIMEOUT:
                now = datetime.now(timezone.utc)
                idle_time = now - last_activity
                
                if idle_time > WEBSOCKET_IDLE_TIMEOUT:
                    connection_phase = "idle_timeout"
                    logger.info(f"⏰ WebSocket idle timeout ({WEBSOCKET_IDLE_TIMEOUT}): {session_id[:8]}...")
                    await _send_json_with_timeout(
                        websocket,
                        {
                            "type": "system",
                            "message": "Connection closed due to inactivity",
                            "timestamp": now.isoformat()
                        },
                        warning_message=f"⚠️ Failed to send idle timeout notice for session {session_id[:8]}",
                    )
                    await _close_websocket_with_timeout(websocket, code=1000, reason="Idle timeout")
                    break
            
            # Receive message from client (size-capped). Poll with a timeout so
            # idle/dead sockets do not park a handler forever inside receive_text().
            try:
                receive_timeout = _WS_RECEIVE_POLL_SECONDS
                if WEBSOCKET_IDLE_TIMEOUT:
                    idle_remaining = WEBSOCKET_IDLE_TIMEOUT - (datetime.now(timezone.utc) - last_activity)
                    receive_timeout = max(1, min(receive_timeout, int(idle_remaining.total_seconds()) + 1))
                data = await asyncio.wait_for(
                    _receive_json_capped(websocket),
                    timeout=receive_timeout,
                )
            except asyncio.TimeoutError:
                continue
            except ValueError as ve:
                logger.warning("WebSocket message rejected: %s", ve)
                if not await _send_json_with_timeout(
                    websocket,
                    {
                        "type": "error",
                        "message": "Message too large" if "large" in str(ve).lower() else "Invalid message",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                    warning_message=f"⚠️ Failed to send invalid-message error for session {session_id[:8]}",
                ):
                    break
                continue
            except json.JSONDecodeError:
                logger.warning("WebSocket invalid JSON frame")
                continue
            
            # Record message in metrics
            metrics_collector.record_message(session_id)
            
            # Update last activity timestamp
            last_activity = datetime.now(timezone.utc)
            
            message_type = data.get("type", "message")
            last_client_message_type = message_type
            
            if message_type == "ping":
                connection_phase = "heartbeat"
                # Heartbeat
                if not await _send_json_with_timeout(
                    websocket,
                    {
                        "type": "pong",
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    },
                    warning_message=f"⚠️ Failed to send pong for session {session_id[:8]}",
                ):
                    break
                connection_phase = "idle"
                continue
            
            if message_type == "phone_update":
                connection_phase = "phone_update"
                # User provided phone: migrate Redis thread + backfill Postgres rows still keyed by guest session id
                phone = data.get("phone")
                if phone:
                    digits = _normalize_widget_phone(phone)
                    if not digits:
                        if not await _send_json_with_timeout(
                            websocket,
                            {
                                "type": "error",
                                "message": "Invalid phone number. Please enter 10 digits.",
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            },
                            warning_message=f"⚠️ Failed to send invalid phone error for session {session_id[:8]}",
                        ):
                            break
                        continue

                    pending_resume = bool(session["state"].get(WEBCHAT_PENDING_RESUME_KEY))
                    migration_ok, _digits = await _webchat_link_phone_number(
                        session,
                        phone=digits,
                        client_id=client_id,
                        session_id=session_id,
                        source="phone_update",
                    )

                    if migration_ok:
                        if not await _send_json_with_timeout(
                            websocket,
                            {
                                "type": "system",
                                "message": "Phone number linked successfully",
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            },
                            warning_message=f"⚠️ Failed to send phone linked message for session {session_id[:8]}",
                        ):
                            break
                        if pending_resume:
                            merged_text = _webchat_merge_trailing_human_messages(session["state"])
                            session["state"].pop(WEBCHAT_PENDING_RESUME_KEY, None)
                            await aupdate_session_state(session)
                            resume_trace_id = generate_trace_id()
                            last_turn_trace_id = resume_trace_id
                            last_customer_message_at = datetime.now(timezone.utc)
                            connection_phase = "waiting_for_phone_resume_response"
                            if not await _send_json_with_timeout(
                                websocket,
                                {
                                    "type": "typing",
                                    "is_typing": True,
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                },
                                warning_message=f"⚠️ Failed to send typing for resume session {session_id[:8]}",
                            ):
                                break
                            try:
                                response_started_at = datetime.now(timezone.utc)
                                connection_phase = "streaming_phone_resume_response"
                                response = await process_message_streaming_resume(
                                    websocket,
                                    session,
                                    resume_trace_id,
                                    merged_text,
                                )
                                if not await _webchat_send_stream_end_and_maybe_carousel(
                                    websocket,
                                    session,
                                    resume_trace_id,
                                    response or "",
                                ):
                                    break
                                try:
                                    await _flush_pending_widget_actions(websocket, session)
                                except Exception as flush_err:
                                    logger.warning(f"⚠️ Failed to flush widget actions (resume): {flush_err}")
                                connection_phase = "idle"
                                response_started_at = None
                            except Exception as resume_err:
                                logger.error(
                                    "phone_update: resume graph failed session=%s: %s",
                                    session_id[:12],
                                    resume_err,
                                    exc_info=True,
                                )
                                report_error(
                                    f"phone_update resume failed: {resume_err}",
                                    level="error",
                                    exc_info=(type(resume_err), resume_err, resume_err.__traceback__),
                                    session_id=session_id,
                                    client_id=client_id,
                                )
                                session["state"].pop(WEBCHAT_PENDING_RESUME_KEY, None)
                                await aupdate_session_state(session)
                                await _send_websocket_event_for_ui(
                                    websocket=websocket,
                                    payload={
                                        "type": "error",
                                        "message": "Could not resume your message. Please try sending again.",
                                        "timestamp": datetime.now(timezone.utc).isoformat(),
                                    },
                                    warning_message="⚠️ Failed to send resume error to client",
                                )
                connection_phase = "idle"
                continue

            if message_type == "cart_context":
                connection_phase = "cart_context_update"
                trace_id = generate_trace_id()
                set_trace_id(trace_id)
                events = data.get("events") or []
                if not isinstance(events, list) or not events:
                    continue
                page_context = data.get("pageContext")
                if isinstance(page_context, dict):
                    session["state"]["page_context"] = page_context
                    session["state"]["current_page_url"] = page_context.get("url")
                    session["state"]["current_product_handle"] = page_context.get("productHandle")
                    session["state"]["current_product_title"] = page_context.get("productTitle")
                    session["state"]["current_product_type"] = page_context.get("productType")
                    session["state"]["current_collection_handle"] = page_context.get("collectionHandle")
                    session["state"]["current_page_type"] = page_context.get("pageType")
                user_loc = data.get("userLocation") or data.get("clientLocation")
                if isinstance(user_loc, dict) and user_loc.get("latitude") is not None:
                    try:
                        location = await _apply_widget_location_to_state(session["state"], user_loc)
                        if location:
                            await aupdate_session_state(session)
                            logger.info(
                                "📍 widget cart location: city=%s pincode=%s lat=%s lon=%s source=%s",
                                location.get("city"),
                                location.get("pincode"),
                                location.get("latitude"),
                                location.get("longitude"),
                                location.get("city_source"),
                            )
                    except Exception as loc_err:
                        logger.warning(f"⚠️ Failed to resolve widget cart location: {loc_err}")
                try:
                    await _append_cart_context_to_state_and_transcript(
                        session=session,
                        client_id=client_id,
                        session_id=session_id,
                        trace_id=trace_id,
                        events=events,
                    )
                except Exception as e:
                    logger.warning(f"⚠️ Failed to append cart context events: {e}")
                connection_phase = "idle"
                continue

            if message_type == "cart_snapshot":
                connection_phase = "cart_snapshot_update"
                snapshot = data.get("cart") or data.get("snapshot") or {}
                if not isinstance(snapshot, dict):
                    connection_phase = "idle"
                    continue
                try:
                    await _apply_cart_snapshot_to_state(session, snapshot)
                except Exception as e:
                    logger.warning(f"⚠️ Failed to apply cart snapshot: {e}")
                connection_phase = "idle"
                continue

            if message_type == "cart_op_error":
                connection_phase = "cart_op_error"
                # The storefront failed to apply a cart mutation (e.g. /cart/add.js
                # rejected an invalid/unavailable variant). Surfaced here so the
                # failure is captured instead of being swallowed in the browser.
                logger.warning(
                    "🛒❌ Storefront cart op failed: action=%s variant=%s title=%r error=%r diag=%s",
                    data.get("action"),
                    data.get("variant_id"),
                    data.get("product_title"),
                    data.get("error"),
                    data.get("diag"),
                )
                connection_phase = "idle"
                continue

            # Process regular message
            user_message = data.get("message", "")
            if not user_message:
                continue
            
            # Generate trace ID for this interaction
            trace_id = generate_trace_id()
            last_turn_trace_id = trace_id
            last_customer_message_at = datetime.now(timezone.utc)
            connection_phase = "processing_customer_message"
            set_trace_id(trace_id)
            
            # Clear per-turn product state so carousel only shows THIS turn's results
            session["state"]["product_selection_matches"] = []
            
            # Extract page context from widget (context-aware chat)
            page_context = data.get("pageContext")
            if page_context:
                # Store page context in session state for graph nodes to access
                session["state"]["page_context"] = page_context
                session["state"]["current_page_url"] = page_context.get("url")
                session["state"]["current_product_handle"] = page_context.get("productHandle")
                session["state"]["current_product_title"] = page_context.get("productTitle")
                session["state"]["current_product_type"] = page_context.get("productType")
                session["state"]["current_collection_handle"] = page_context.get("collectionHandle")
                session["state"]["current_page_type"] = page_context.get("pageType")
                
                logger.info(f"📍 page_context: type={page_context.get('pageType')} handle={page_context.get('productHandle')} url={str(page_context.get('url', ''))[:60]}")
                
                # 🚀 PRE-FETCH PRODUCT INFO if on product page (WEB CHAT ONLY)
                # This ensures get_product_context tool returns data without LLM needing to call special tool
                # WhatsApp never sends pageContext, so this block is never executed for WhatsApp
                page_type = page_context.get("pageType")
                product_handle = page_context.get("productHandle")
                
                if (isinstance(page_type, str) and 
                    page_type == "product" and 
                    product_handle):
                    
                    new_handle = str(product_handle).lower().strip() if product_handle else ""
                    last_pdp_handle = (session["state"].get("_last_pdp_handle") or "").lower().strip()
                    
                    logger.debug(f"product check: new={new_handle} last_pdp={last_pdp_handle}")
                    
                    should_fetch = False
                    should_reset = False
                    
                    if not last_pdp_handle:
                        should_fetch = True
                        logger.debug(f"first product page visit, fetching: {new_handle}")
                        
                        new_title = page_context.get("productTitle", new_handle)
                        context_init_msg = f"[Context: User is viewing {new_title}]"
                        if "messages" in session["state"]:
                            session["state"]["messages"].append(SystemMessage(content=context_init_msg))
                            logger.debug(f"added initial context for: {new_handle}")
                    elif last_pdp_handle != new_handle:
                        should_fetch = True
                        should_reset = True
                        logger.info(f"🔄 product_changed: {last_pdp_handle} → {new_handle}")
                        
                        # Clear ALL old product context
                        session["state"]["inquiry_product_info"] = None
                        session["state"]["product_link"] = None
                        session["state"]["product_selection_matches"] = []
                        
                        # 🔄 FULL CONVERSATION CONTEXT RESET FOR PRODUCT SWITCH
                        # This ensures generic_skill_node sees correct entities and focal_entity
                        try:
                            conv_ctx = session["state"].get("conversation_context")
                            if not conv_ctx or not isinstance(conv_ctx, dict):
                                conv_ctx = {"topics": [], "entities": []}
                                session["state"]["conversation_context"] = conv_ctx
                            
                            # 1. Clear ALL product entities from entities list (keep order entities)
                            old_entities = conv_ctx.get("entities", [])
                            non_product_entities = [
                                e for e in old_entities
                                if isinstance(e, dict) and e.get("entity_type") != "product"
                            ]
                            removed_count = len(old_entities) - len(non_product_entities)
                            conv_ctx["entities"] = non_product_entities
                            logger.debug(f"cleared {removed_count} product entities, kept {len(non_product_entities)}")
                            
                            # 2. Clear focal_entity if it's a product
                            old_focal = conv_ctx.get("focal_entity")
                            if old_focal and isinstance(old_focal, dict) and old_focal.get("entity_type") == "product":
                                conv_ctx["focal_entity"] = None
                            
                            # 3. Clear product entity_refs from ALL topics
                            topics = conv_ctx.get("topics", [])
                            for topic in topics:
                                if isinstance(topic, dict):
                                    old_refs = topic.get("entity_refs", [])
                                    # Keep only order refs, remove product refs
                                    # entity_refs are strings like "product:handle" or "order:id"
                                    non_product_refs = [
                                        ref for ref in old_refs
                                        if isinstance(ref, str) and ref.startswith("order:")
                                    ]
                                    removed_refs = len(old_refs) - len(non_product_refs)
                                    if removed_refs > 0:
                                        topic["entity_refs"] = non_product_refs
                            
                            # 4. Reset active topic to force fresh context
                            if "active_topic_id" in conv_ctx:
                                conv_ctx["active_topic_id"] = "product_inquiry_" + str(int(datetime.now(timezone.utc).timestamp()))
                            
                        except Exception as e:
                            logger.warning(f"⚠️ Non-critical error resetting conversation_context: {e}")
                        
                        new_title = page_context.get("productTitle", new_handle)
                        context_switch_msg = (
                            f"[SYSTEM] Customer navigated to a new product page: {new_title}\n"
                            f"Product handle: {new_handle}"
                        )
                        if "messages" not in session["state"]:
                            session["state"]["messages"] = []
                        session["state"]["messages"].append(SystemMessage(content=context_switch_msg))
                    else:
                        logger.debug(f"same product ({new_handle}), reusing context")
                    
                    session["state"]["_last_pdp_handle"] = new_handle
                    
                    if should_fetch:
                        logger.debug(f"pre-fetching product: {new_handle} reset={should_reset}")
                        try:
                            from fashion_bot.core.orchestrator import ProductOrchestrator
                            result = await ProductOrchestrator.aget_product_info_from_context(
                                product_handle=new_handle,
                                page_url=page_context.get("url", ""),
                                cached_product=session["state"].get("inquiry_product_info"),
                                state=session["state"],
                            )
                            
                            if result.get("found"):
                                from fashion_bot.tool_factory import _normalize_product
                                fetched_product = _normalize_product(result.get("product", {}))
                                session["state"]["inquiry_product_info"] = fetched_product
                                session["state"]["product_link"] = result.get("product_url", "")
                                product_name = fetched_product.get("name") or fetched_product.get("title") or result.get("name", "Unknown")
                                logger.info(f"pre-fetched product: {product_name}")
                                
                                # 🎯 SET NEW FOCAL ENTITY in conversation_context
                                # This ensures generic_skill_node sees the correct product
                                try:
                                    conv_ctx = session["state"].get("conversation_context")
                                    if not conv_ctx or not isinstance(conv_ctx, dict):
                                        conv_ctx = {"topics": [], "entities": []}
                                        session["state"]["conversation_context"] = conv_ctx
                                    
                                    # Build full_data for context_helpers to use in prompt building
                                    # This is the CRITICAL part - without full_data, the LLM won't see product details
                                    product_full_data = {
                                        "name": product_name,
                                        "price": fetched_product.get("price"),
                                        "description": fetched_product.get("description"),
                                        "sizes": fetched_product.get("sizes_in_stock") or fetched_product.get("sizes") or fetched_product.get("available_sizes"),
                                        "vendor": fetched_product.get("vendor"),
                                        "product_link": fetched_product.get("url") or fetched_product.get("product_url") or session["state"].get("product_link"),
                                        "product_attributes": fetched_product.get("all_metafields") or fetched_product.get("product_attributes") or []
                                    }
                                    
                                    # Create new focal entity from pre-fetched product WITH full_data
                                    new_focal_entity = {
                                        "entity_type": "product",
                                        "entity_id": new_handle,
                                        "entity_value": product_name,
                                        "source": "page_context",
                                        "discovered_at": datetime.now(timezone.utc).isoformat(),
                                        "full_data": product_full_data,  # 🎯 CRITICAL: Include product data for LLM
                                        "metadata": {
                                            "handle": new_handle,
                                            "url": session["state"].get("product_link"),
                                            "from_page_context": True
                                        }
                                    }
                                    
                                    # Set as focal entity
                                    conv_ctx["focal_entity"] = new_focal_entity
                                    if "entities" not in conv_ctx:
                                        conv_ctx["entities"] = []
                                    conv_ctx["entities"].append(new_focal_entity)
                                    logger.debug(f"focal_entity={product_name} entities={len(conv_ctx['entities'])}")
                                    
                                except Exception as ctx_err:
                                    logger.warning(f"⚠️ Non-critical error setting focal_entity: {ctx_err}")
                                
                            else:
                                logger.warning(f"⚠️ Could not pre-fetch product: {result.get('message', result.get('error', 'Unknown'))}")
                        except Exception as e:
                            logger.error(f"❌ Error pre-fetching product: {e}")
                            report_error(
                                "Error pre-fetching product",
                                level='warning',
                                exc_info=(type(e), e, e.__traceback__),
                                session_id=session_id,
                                client_id=client_id,
                            )

                logger.debug(f"state keys: page_context, current_product_handle, current_page_type")
            else:
                logger.warning(f"⚠️ NO pageContext in message data. Keys received: {list(data.keys())}")
            
            # Extract browser geolocation from widget
            user_loc = data.get("userLocation") or data.get("clientLocation")
            if isinstance(user_loc, dict) and user_loc.get("latitude") is not None:
                try:
                    location = await _apply_widget_location_to_state(session["state"], user_loc)
                    if location:
                        await aupdate_session_state(session)
                        logger.info(
                            "📍 widget message location: city=%s pincode=%s lat=%s lon=%s source=%s",
                            location.get("city"),
                            location.get("pincode"),
                            location.get("latitude"),
                            location.get("longitude"),
                            location.get("city_source"),
                        )
                except Exception as loc_err:
                    logger.warning(f"⚠️ Failed to resolve widget message location: {loc_err}")

            # Check for phone number in message payload (from widget)
            phone_in_message = data.get("phone")
            if phone_in_message:
                linked, digits = await _webchat_link_phone_number(
                    session,
                    phone=phone_in_message,
                    client_id=client_id,
                    session_id=session_id,
                    source="message_payload",
                )
                if not linked:
                    logger.warning(
                        "webchat_phone_link: ignored invalid/unlinked message payload phone session=%s digits=%s",
                        session_id[:12],
                        f"***{digits[-4:]}" if digits else None,
                    )
            
            st = session["state"]
            if not _webchat_has_verified_phone(st):
                await _webchat_link_detected_phone_if_present(
                    session,
                    user_message=user_message,
                    client_id=client_id,
                    session_id=session_id,
                )
                st = session["state"]

            guest_done = int(st.get(WEBCHAT_GUEST_EXCHANGES_KEY) or 0)
            guest_exchange_limit = await _aget_webchat_guest_exchange_limit(client_id)
            if not _webchat_has_verified_phone(st) and guest_done >= guest_exchange_limit:
                await _webchat_pause_turn_require_phone(
                    websocket,
                    session,
                    user_message=user_message,
                    trace_id=trace_id,
                    client_id=client_id,
                    session_id=session_id,
                    guest_exchanges_completed=guest_done,
                    guest_exchange_limit=guest_exchange_limit,
                )
                continue

            # Send typing indicator
            connection_phase = "waiting_for_bot_response"
            if not await _send_json_with_timeout(
                websocket,
                {
                    "type": "typing",
                    "is_typing": True,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                },
                warning_message=f"⚠️ Failed to send typing indicator for session {session_id[:8]}",
            ):
                break
            
            # Check if client requests streaming (default: True for web chat)
            use_streaming = data.get("streaming", True)

            if use_streaming:
                # Process with streaming - tokens sent incrementally
                logger.debug(f"streaming mode for message")
                response_started_at = datetime.now(timezone.utc)
                connection_phase = "streaming_response"
                response = await process_message_streaming(websocket, user_message, session, trace_id)
                if not await _webchat_send_stream_end_and_maybe_carousel(
                    websocket, session, trace_id, response or ""
                ):
                    break
                try:
                    await _flush_pending_widget_actions(websocket, session)
                except Exception as flush_err:
                    logger.warning(f"⚠️ Failed to flush widget actions: {flush_err}")
                connection_phase = "idle"
                response_started_at = None
            else:
                # Non-streaming mode (legacy behavior)
                response_started_at = datetime.now(timezone.utc)
                response = await process_message(user_message, session, trace_id)
                try:
                    await _flush_pending_widget_actions(websocket, session)
                except Exception as flush_err:
                    logger.warning(f"⚠️ Failed to flush widget actions: {flush_err}")
                if session.pop("_last_turn_queued", False):
                    logger.info("⏳ Non-stream turn queued; suppressing customer-visible queued message")
                    if not await _send_json_with_timeout(
                        websocket,
                        {
                            "type": "queued",
                            "queued": True,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "trace_id": trace_id,
                        },
                        warning_message=f"⚠️ Failed to send queued control event for session {session_id[:8]}",
                    ):
                        break
                    continue

                response_payload = {
                    "type": "message",
                    "message": response or "",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "trace_id": trace_id
                }

                if not await _send_json_with_timeout(
                    websocket,
                    response_payload,
                    warning_message=f"⚠️ Failed to send response message for session {session_id[:8]}",
                ):
                    break
                connection_phase = "idle"
                response_started_at = None

                carousel_products = _products_for_webchat_carousel(session["state"])
                # Suppress the carousel when the message-limit gate short-circuited
                # the turn — the canned limit reply is terminal and stale carousel
                # state from a prior product turn must not render beneath it.
                if carousel_products and not session["state"].get("conversation_limit_reached"):
                    matched = _match_carousel_products_to_reply(
                        session["state"], carousel_products, response or "",
                        show_product_handles=session["state"].get("show_product_handles"),
                    )
                    formatted = []
                    if matched:
                        from fashion_bot.config_manager import aget_judgeme_rating_display_enabled
                        rating_enabled = await aget_judgeme_rating_display_enabled(session["state"].get("client_id"))
                        formatted = [format_product_for_carousel(p, rating_enabled=rating_enabled) for p in matched if p]
                        formatted = [f for f in formatted if f]
                        formatted = _dedupe_carousel_payload(formatted)
                    if formatted:
                        await asyncio.sleep(0.3)
                        if not await _send_json_with_timeout(
                            websocket,
                            {
                                "type": "products",
                                "products": formatted,
                                "timestamp": datetime.now(timezone.utc).isoformat()
                            },
                            warning_message=f"⚠️ Failed to send carousel for session {session_id[:8]}",
                        ):
                            break
                        _record_sent_carousel_handles(session["state"], formatted)
                        await aupdate_session_state(session)
                        logger.info(f"🛒 Carousel: Sent {len(formatted)} products")
    
    except WebSocketDisconnect:
        _log_disconnect_context("websocket_disconnect")
        metrics_collector.disconnect_connection(session_id)
    except Exception as e:
        logger.error(f"❌ WebSocket error: {str(e)}")
        metrics_collector.record_error(session_id)
        report_error(
            "WebSocket endpoint error",
            level='error',
            exc_info=(type(e), e, e.__traceback__),
            session_id=session_id,
            client_name=client_name,
            client_id=locals().get("client_id"),
        )
        await _close_websocket_with_timeout(websocket, code=1011, reason="Internal server error")
    finally:
        _log_disconnect_context("handler_exit")
        metrics_collector.disconnect_connection(session_id)


@websocket_router.get("/ws/sessions/active")
async def get_active_sessions():
    """
    Get count of active web chat sessions (for monitoring).
    
    Now reads from Redis-backed unified cache instead of in-memory dict.
    """
    cache = get_unified_cache()
    
    # List all web chat conversations from Redis
    conversations = await cache.alist_conversations(channel=Channel.WEB)
    
    return {
        "active_sessions": len(conversations),
        "sessions": [
            {
                "thread_id": conv.get("thread_id", "")[:20] + "...",
                "session_id": conv.get("user_id", "")[:8] + "...",
                "client_id": conv.get("tenant_id", "")[:8] + "...",
                "last_activity": conv.get("last_updated"),
                "message_count": 0,  # Not stored in summary; would need full state fetch
                "channel": conv.get("channel", "web"),
                "source": conv.get("source", "redis")
            }
            for conv in conversations
        ]
    }


@websocket_router.get("/ws/clients/cache")
async def get_client_cache_status():
    """Get status of client name cache (for monitoring)"""
    global _cache_last_updated

    mapping = await aget_client_name_mapping()
    
    return {
        "cache_size": len(mapping),
        "clients": list(mapping.keys()),
        "last_updated": _cache_last_updated.isoformat() if _cache_last_updated else None,
        "cache_ttl_minutes": int(_cache_ttl.total_seconds() / 60)
    }


@websocket_router.post("/ws/clients/cache/refresh")
async def refresh_client_cache():
    """Manually refresh client name cache from database.

    Busts both the in-process memory tier and the Redis tier of the
    tiered cache, then forces a reload via aget_client_name_mapping.
    """
    global _client_name_cache, _cache_last_updated

    logger.info("🔄 Manual cache refresh requested")
    try:
        from fashion_bot.utils.tiered_cache import invalidate_tiered_cache_key
        invalidate_tiered_cache_key(_CLIENT_NAME_MAPPING_CACHE_KEY)
    except Exception as e:
        logger.warning(f"Failed to invalidate memory tier: {e}")
    try:
        from fashion_bot.utils.redis_client import get_shared_async_redis_client
        rc = await get_shared_async_redis_client()
        if rc:
            await rc.delete(_CLIENT_NAME_MAPPING_CACHE_KEY)
    except Exception as e:
        logger.warning(f"Failed to invalidate Redis tier: {e}")

    mapping = await aget_client_name_mapping()
    return {
        "status": "success",
        "cache_size": len(mapping),
        "clients": list(mapping.keys()),
        "refreshed_at": _cache_last_updated.isoformat() if _cache_last_updated else None,
    }
