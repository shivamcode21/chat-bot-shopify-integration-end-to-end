"""
Lightweight LangSmith span helpers.

These helpers are fail-safe:
- If LangSmith is unavailable/misconfigured, they no-op.
- If no parent trace exists (default), they no-op to avoid noisy root traces.
"""

from __future__ import annotations

from contextlib import contextmanager
import logging
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

try:
    from langsmith import trace, get_current_run_tree
except Exception:  # pragma: no cover - optional dependency behavior
    trace = None
    get_current_run_tree = None


@contextmanager
def traced_operation(
    name: str,
    *,
    run_type: str = "tool",
    metadata: Optional[Dict[str, Any]] = None,
    require_parent: bool = True,
) -> Iterator[Any]:
    """
    Create a nested LangSmith span for an operation.

    Args:
        name: Span name shown in LangSmith.
        run_type: LangSmith run type (e.g., tool/chain/llm).
        metadata: Optional metadata for filtering.
        require_parent: If True, only emits when inside an existing trace.
    """
    if trace is None:
        yield None
        return

    if require_parent:
        try:
            current = get_current_run_tree() if get_current_run_tree else None
        except Exception:
            current = None
        if current is None:
            yield None
            return

    # A @contextmanager generator must yield exactly once. The previous
    # implementation wrapped the inner `with trace(...)` in try/except and
    # yielded again on failure, which caused
    # `RuntimeError: generator didn't stop after throw()` whenever the inner
    # span's __exit__ raised. Attempt to open the span; if construction itself
    # fails, fall back to a no-op single yield. Once yielded, never yield again.
    try:
        span_cm = trace(name=name, run_type=run_type, metadata=metadata or {})
    except Exception as span_err:  # pragma: no cover
        logger.debug(f"LangSmith span '{name}' construction failed (ignored): {span_err}")
        yield None
        return

    try:
        with span_cm as run:
            yield run
    except Exception:
        # Let caller-side exceptions propagate unchanged; the inner CM will
        # already have handled its own cleanup. We do NOT yield again here.
        raise


def set_trace_io(
    run: Any,
    *,
    inputs: Optional[Dict[str, Any]] = None,
    outputs: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Best-effort attachment of inputs/outputs to a LangSmith run object.

    Compatible with multiple SDK variants where run objects may expose either
    `add_inputs`/`add_outputs` or assignable `inputs`/`outputs` attributes.
    """
    if run is None:
        return
    if inputs is not None:
        try:
            if hasattr(run, "add_inputs"):
                run.add_inputs(inputs)
            else:
                run.inputs = inputs
        except Exception:
            pass
    if outputs is not None:
        try:
            if hasattr(run, "add_outputs"):
                run.add_outputs(outputs)
            else:
                run.outputs = outputs
        except Exception:
            pass


# ---------------------------------------------------------------------------
# State snapshot for explicit LangSmith ingestion
# ---------------------------------------------------------------------------

_SCALAR_STATE_KEYS: List[str] = [
    "client_id", "phone_number", "trace_id", "session_id",
    "parent_intent", "has_unknown", "customer_message", "type", "response_type",
    "is_order_query", "is_frustrated", "needs_escalation", "needs_human_agent",
    "selected_order_id", "pincode", "payment_mode", "draft_order_id",
    "current_page_type", "current_product_handle", "current_product_title",
    "current_page_url", "_skip_final_answer", "_streaming_enabled",
    "waiting_for_order_confirmation", "waiting_for_cancellation_reason",
    "waiting_for_update_confirmation",
    "active_return_exchange_flow", "waiting_for_return_exchange_order_id",
    "waiting_for_phone_validation", "phone_validation_order_id",
    "waiting_for_gender", "waiting_for_category",
    "customer_gender", "customer_category",
    "degraded_mode", "_last_pdp_handle",
    "continuity_closure_shown", "last_message_at",
]


def snapshot_state_for_trace(
    state: Any,
    *,
    max_messages: int = 5,
    content_preview_len: int = 300,
) -> Dict[str, Any]:
    """
    Build a JSON-safe, size-bounded snapshot of SupportState for LangSmith.

    The snapshot is designed to appear under the ``state`` key in trace
    inputs / outputs so every conversation turn is fully inspectable.
    """
    if not isinstance(state, dict):
        return {"_error": "state is not a dict"}

    try:
        snap: Dict[str, Any] = {}

        # --- scalar / flag fields ---
        for key in _SCALAR_STATE_KEYS:
            val = state.get(key)
            if val is not None:
                snap[key] = val

        # --- messages (count + tail previews) ---
        messages = state.get("messages")
        if isinstance(messages, list):
            snap["messages_count"] = len(messages)
            tail: List[Dict[str, Any]] = []
            for m in messages[-max_messages:]:
                raw = str(getattr(m, "content", "") or "")
                tail.append({
                    "type": type(m).__name__,
                    "content": raw[:content_preview_len] + ("…" if len(raw) > content_preview_len else ""),
                })
            snap["messages_tail"] = tail

        # --- detected intents / tags ---
        if state.get("detected_intents"):
            snap["detected_intents"] = state["detected_intents"]
        if state.get("detected_tags"):
            snap["detected_tags"] = state["detected_tags"]

        # --- conversation_context (summary) ---
        ctx = state.get("conversation_context")
        if isinstance(ctx, dict):
            cc: Dict[str, Any] = {
                "topic_count": len(ctx.get("topics") or []),
                "active_topic_id": ctx.get("active_topic_id"),
                "focal_entity": ctx.get("focal_entity"),
                "entity_count": len(ctx.get("entities") or []),
            }
            topics = ctx.get("topics")
            if isinstance(topics, list):
                cc["topics"] = [
                    {
                        "topic_id": t.get("topic_id"),
                        "topic_type": t.get("topic_type"),
                        "status": t.get("status"),
                        "summary": (t.get("summary") or "")[:200],
                    }
                    for t in topics[-3:]
                ]
            snap["conversation_context"] = cc

        # --- inquiry_product_info ---
        ipi = state.get("inquiry_product_info")
        if isinstance(ipi, dict):
            snap["inquiry_product_info"] = {
                k: ipi.get(k) for k in ("handle", "title", "id", "product_type") if ipi.get(k)
            }

        # --- scratchpad ---
        sp = state.get("scratchpad")
        if sp:
            sp_str = str(sp)
            snap["scratchpad"] = sp_str[:500] + ("…" if len(sp_str) > 500 else "")

        # --- collection counts for large lists ---
        for key, label in [
            ("product_selection_matches", "product_selection_matches_count"),
            ("known_orders", "known_orders_count"),
            ("recent_products", "recent_products_count"),
            ("tool_call_trace", "tool_call_trace_count"),
            ("degraded_components", "degraded_components"),
        ]:
            val = state.get(key)
            if isinstance(val, list):
                if label.endswith("_count"):
                    snap[label] = len(val)
                else:
                    snap[label] = val

        # --- page_context ---
        pc = state.get("page_context")
        if isinstance(pc, dict):
            snap["page_context"] = {
                k: pc.get(k) for k in ("url", "pageType", "productHandle", "productTitle") if pc.get(k)
            }

        return snap

    except Exception as exc:
        logger.debug(f"snapshot_state_for_trace failed (ignored): {exc}")
        return {"_error": f"snapshot failed: {exc}"}
