import ast
import contextlib
import importlib
import sys
import time
import types
from pathlib import Path

import pytest

_runtime_mod = pytest.importorskip("fashion_bot.core.conversation_runtime")
ConversationRuntime = _runtime_mod.ConversationRuntime
RuntimeResult = _runtime_mod.RuntimeResult


@pytest.mark.asyncio
async def test_runtime_queues_when_lock_not_acquired():
    rt = ConversationRuntime(
        log_fn=lambda *a, **k: None,
        mark_degraded_state_fn=lambda *a, **k: None,
        try_acquire_lock_fn=lambda c, u, t: (False, False),
        enqueue_pending_fn=lambda c, u, t, p: True,
        release_lock_fn=lambda *a, **k: None,
        drain_pending_fn=lambda *a, **k: [],
        build_merged_payload_fn=lambda p, e: ({}, 0, 0),
        should_trigger_summary_fn=lambda s, r: (False, "none"),
        enqueue_summary_job_fn=lambda *a, **k: None,
        redispatch_fn=lambda *a, **k: None,
    )

    async def _execute(_ctx):
        return RuntimeResult(handled=True)

    result = await rt.run_turn(
        channel="whatsapp",
        client_id="c1",
        user_id="u1",
        inbound_payload={"payload": {}},
        execute_fn=_execute,
        trace_id="t1",
    )
    assert result.queued is True
    assert result.handled is False


@pytest.mark.asyncio
async def test_runtime_executes_and_enqueues_summary():
    summary_calls = []

    rt = ConversationRuntime(
        log_fn=lambda *a, **k: None,
        mark_degraded_state_fn=lambda *a, **k: None,
        try_acquire_lock_fn=lambda c, u, t: (True, False),
        enqueue_pending_fn=lambda c, u, t, p: False,
        release_lock_fn=lambda *a, **k: None,
        drain_pending_fn=lambda *a, **k: [],
        build_merged_payload_fn=lambda p, e: ({}, 0, 0),
        should_trigger_summary_fn=lambda s, r: (True, "periodic"),
        enqueue_summary_job_fn=lambda *a, **k: summary_calls.append("called"),
        redispatch_fn=lambda *a, **k: None,
    )

    async def _execute(_ctx):
        return RuntimeResult(handled=True, reply_text="ok", state_snapshot={"messages": [1, 2]})

    result = await rt.run_turn(
        channel="whatsapp",
        client_id="c1",
        user_id="u1",
        inbound_payload={"payload": {}},
        execute_fn=_execute,
        trace_id="t2",
    )
    assert result.handled is True
    assert summary_calls == ["called"]


@pytest.mark.asyncio
async def test_runtime_fails_open_when_enqueue_pending_fails():
    calls = []

    rt = ConversationRuntime(
        log_fn=lambda *a, **k: None,
        mark_degraded_state_fn=lambda *a, **k: None,
        try_acquire_lock_fn=lambda c, u, t: (False, False),
        enqueue_pending_fn=lambda c, u, t, p: False,
        release_lock_fn=lambda *a, **k: None,
        drain_pending_fn=lambda *a, **k: [],
        build_merged_payload_fn=lambda p, e: ({}, 0, 0),
        should_trigger_summary_fn=lambda s, r: (False, "none"),
        enqueue_summary_job_fn=lambda *a, **k: None,
        redispatch_fn=lambda *a, **k: None,
    )

    async def _execute(_ctx):
        calls.append("execute")
        return RuntimeResult(handled=True, reply_text="processed")

    result = await rt.run_turn(
        channel="whatsapp",
        client_id="c1",
        user_id="u1",
        inbound_payload={"payload": {}},
        execute_fn=_execute,
        trace_id="t2-fail-open",
    )

    assert result.handled is True
    assert result.queued is False
    assert result.reply_text == "processed"
    assert calls == ["execute"]


@pytest.mark.asyncio
async def test_runtime_stream_fails_open_when_enqueue_pending_fails():
    rt = ConversationRuntime(
        log_fn=lambda *a, **k: None,
        mark_degraded_state_fn=lambda *a, **k: None,
        try_acquire_lock_fn=lambda c, u, t: (False, False),
        enqueue_pending_fn=lambda c, u, t, p: False,
        release_lock_fn=lambda *a, **k: None,
        drain_pending_fn=lambda *a, **k: [],
        build_merged_payload_fn=lambda p, e: ({}, 0, 0),
        should_trigger_summary_fn=lambda s, r: (False, "none"),
        enqueue_summary_job_fn=lambda *a, **k: None,
        redispatch_fn=lambda *a, **k: None,
    )

    events = []

    async def _execute_stream(_ctx):
        yield {"type": "token", "content": "hello"}
        yield {"type": "end", "full_response": "hello", "result": {"messages": ["hello"]}}

    async for event in rt.run_turn_stream(
        channel="whatsapp",
        client_id="c1",
        user_id="u1",
        inbound_payload={"payload": {}},
        execute_stream_fn=_execute_stream,
        trace_id="t2-fail-open-stream",
    ):
        events.append(event)

    event_types = [str((event or {}).get("type") or "") for event in events if isinstance(event, dict)]
    assert "queued" not in event_types
    assert event_types[0] == "token"
    assert event_types[-1] == "end"


@pytest.mark.asyncio
async def test_runtime_drain_merge_redispatch():
    redispatched = []

    rt = ConversationRuntime(
        log_fn=lambda *a, **k: None,
        mark_degraded_state_fn=lambda *a, **k: None,
        try_acquire_lock_fn=lambda c, u, t: (True, False),
        enqueue_pending_fn=lambda c, u, t, p: False,
        release_lock_fn=lambda *a, **k: None,
        drain_pending_fn=lambda *a, **k: [{"message_text": "hi"}],
        build_merged_payload_fn=lambda p, e: ({"payload": {"type": "text", "payload": {"text": "hi"}}}, 1, 2),
        should_trigger_summary_fn=lambda s, r: (False, "none"),
        enqueue_summary_job_fn=lambda *a, **k: None,
        redispatch_fn=lambda payload, trace, client: redispatched.append((payload, client)),
    )

    async def _execute(_ctx):
        return RuntimeResult(handled=True, reply_text="ok", state_snapshot={})

    result = await rt.run_turn(
        channel="whatsapp",
        client_id="c1",
        user_id="u1",
        inbound_payload={"payload": {}},
        execute_fn=_execute,
        trace_id="t3",
    )
    assert result.handled is True
    assert len(redispatched) == 1


@pytest.mark.asyncio
async def test_runtime_awaits_async_hooks():
    calls = []

    async def _try_acquire(_client_id, _user_id, _trace_id):
        calls.append("try_acquire")
        return True, False

    async def _enqueue_pending(*_args, **_kwargs):
        calls.append("enqueue_pending")
        return False

    async def _release(*_args, **_kwargs):
        calls.append("release")

    async def _drain(*_args, **_kwargs):
        calls.append("drain")
        return []

    async def _should_summary(*_args, **_kwargs):
        calls.append("should_summary")
        return True, "periodic"

    async def _enqueue_summary(*_args, **_kwargs):
        calls.append("enqueue_summary")

    rt = ConversationRuntime(
        log_fn=lambda *a, **k: None,
        mark_degraded_state_fn=lambda *a, **k: None,
        try_acquire_lock_fn=_try_acquire,
        enqueue_pending_fn=_enqueue_pending,
        release_lock_fn=_release,
        drain_pending_fn=_drain,
        build_merged_payload_fn=lambda p, e: ({}, 0, 0),
        should_trigger_summary_fn=_should_summary,
        enqueue_summary_job_fn=_enqueue_summary,
        redispatch_fn=lambda *a, **k: None,
    )

    async def _execute(_ctx):
        return RuntimeResult(handled=True, reply_text="ok", state_snapshot={"messages": [1]})

    result = await rt.run_turn(
        channel="whatsapp",
        client_id="c1",
        user_id="u1",
        inbound_payload={"payload": {}},
        execute_fn=_execute,
        trace_id="t_async",
    )

    assert result.handled is True
    assert calls == [
        "try_acquire",
        "should_summary",
        "enqueue_summary",
        "drain",
        "release",
    ]


class _FakeRuntime:
    def __init__(self, events):
        self._events = list(events)

    async def run_turn_stream(
        self,
        *,
        channel,
        client_id,
        user_id,
        inbound_payload,
        execute_stream_fn,
        trace_id,
    ):
        for event in self._events:
            yield event


class _FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


@pytest.mark.asyncio
async def test_stream_graph_response_emits_core_events_only(monkeypatch):
    ss = pytest.importorskip("fashion_bot.core.streaming_service")

    # stream_graph_response now drives graph.astream(stream_mode=["custom","values"]):
    # `custom` carries the skill node's writer tokens, `values` the full final state.
    class _FakeStreamGraph:
        async def astream(self, _state, stream_mode=None, config=None):
            yield ("custom", {"type": "token", "content": "hello world"})
            yield (
                "values",
                {
                    "customer_message": "hello world",
                    "messages": [],
                    # These keys are intentionally ignored by stream_graph_response now.
                    "temporary_responses": ["temp message"],
                    "partial_responses": ["partial message"],
                },
            )

    fake_graph_mod = types.ModuleType("fashion_bot.graph_context_meta")
    fake_graph_mod.graph = _FakeStreamGraph()
    monkeypatch.setitem(sys.modules, "fashion_bot.graph_context_meta", fake_graph_mod)

    state = {"trace_id": "t1", "phone_number": "9999999999"}
    events = [event async for event in ss.stream_graph_response(state, "hi", "c1")]
    event_types = [str((event or {}).get("type") or "") for event in events]

    assert event_types[0] == "start"
    assert event_types[-1] == "end"
    assert "token" in event_types
    assert "temporary_response" not in event_types
    assert "partial_response" not in event_types
    assert events[-1].get("full_response") == "hello world"


@pytest.mark.asyncio
async def test_gupshup_stream_consumer_accumulates_tokens_and_end_result():
    gw = pytest.importorskip("fashion_bot.gupshup_webhook")
    runtime = _FakeRuntime(
        [
            {"type": "start"},
            {"type": "token", "content": "hello"},
            {"type": "token", "content": " world"},
            {"type": "end", "full_response": "", "result": {"messages": []}},
        ]
    )

    queued, final_reply, final_result = await gw._consume_runtime_stream_events(
        runtime=runtime,
        client_id="c1",
        runtime_lock_user_id="9999999999",
        data={"payload": {}},
        source="15550001111",
        sender_phone="9999999999",
        trace_id="t2",
    )

    assert queued is False
    assert final_reply == "hello world"
    assert final_result == {"messages": []}


@pytest.mark.asyncio
async def test_gupshup_streaming_sends_single_final_message(monkeypatch):
    gw = pytest.importorskip("fashion_bot.gupshup_webhook")
    atg = pytest.importorskip("fashion_bot.async_tag_generator")
    cfg = pytest.importorskip("fashion_bot.config_manager")

    send_calls = []

    async def _fake_consume(**_kwargs):
        return False, "final bot reply", {"messages": []}

    monkeypatch.setattr(cfg, "validate_gupshup_app_name", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(gw, "_get_conversation_runtime", lambda: object())
    monkeypatch.setattr(gw, "_consume_runtime_stream_events", _fake_consume)
    monkeypatch.setattr(gw, "cleanup_expired_states", lambda: None)
    async def _fake_send_message(to, message, *args, **kwargs):
        send_calls.append((to, message))

    monkeypatch.setattr(gw, "send_message", _fake_send_message)
    monkeypatch.setattr(gw, "publish_outbound_to_redis", lambda *args, **kwargs: None)
    monkeypatch.setattr(gw, "astore_message_event_with_conversation_resolution", lambda **kwargs: "conv1")
    monkeypatch.setattr(gw, "aget_state_by_numbers", lambda *_args, **_kwargs: {"conversation_id": "conv1", "_langsmith_trace_id": "ls1"})
    monkeypatch.setattr(gw.bot_user_agent_mode, "get_conversation_state", lambda *_args, **_kwargs: {"mode": "agent"})
    monkeypatch.setattr(gw.bot_user_agent_mode, "set_conversation_mode", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(atg, "generate_tags_async", lambda **_kwargs: None)
    monkeypatch.setattr(gw, "traced_operation", lambda *_args, **_kwargs: contextlib.nullcontext())

    data = {
        "app": "test-app",
        "payload": {
            "type": "text",
            "source": "15550001111",
            "sender": {"phone": "9999999999", "name": "Tester"},
            "payload": {"text": "hello"},
        },
    }

    await gw.process_webhook_payload(data, "trace-1", "c1")

    assert send_calls == [("9999999999", "final bot reply")]


@pytest.mark.asyncio
async def test_websocket_stream_consumer_streams_tokens_and_end(monkeypatch):
    wc = _import_websocket_chat_module(monkeypatch)
    runtime = _FakeRuntime(
        [
            {"type": "start"},
            {"type": "token", "content": "hi"},
            {"type": "token", "content": " there"},
            {"type": "end", "full_response": "hi there", "result": {"messages": []}},
        ]
    )
    websocket = _FakeWebSocket()

    queued, full_response, result = await wc._consume_websocket_runtime_stream_events(
        runtime=runtime,
        websocket=websocket,
        state={},
        client_id="c1",
        user_identifier="web_u1",
        message="hello",
        trace_id="t1",
        execute_stream_fn=_empty_stream,
    )

    assert queued is False
    assert full_response == "hi there"
    assert result == {"messages": []}
    assert [m.get("type") for m in websocket.sent] == ["stream", "stream", "end"]


@pytest.mark.asyncio
async def test_streamlit_stream_consumer_accumulates_tokens(monkeypatch):
    sa = _import_streamlit_app_module(monkeypatch)
    runtime = _FakeRuntime(
        [
            {"type": "start"},
            {"type": "token", "content": "hello"},
            {"type": "token", "content": " world"},
            {"type": "end", "full_response": "hello world", "result": {"messages": []}},
        ]
    )
    updates = []
    ttft_values = []

    queued, final_reply, final_result, ttft_ms = await sa._consume_streamlit_runtime_events(
        runtime=runtime,
        client_id="c1",
        user_id="u1",
        user_input="hi",
        trace_id="t3",
        execute_stream_fn=_empty_stream,
        on_update=lambda txt: updates.append(txt),
        on_ttft=lambda ms: ttft_values.append(ms),
        turn_start=time.perf_counter() - 0.01,
    )

    assert queued is False
    assert final_reply == "hello world"
    assert final_result == {"messages": []}
    assert ttft_ms is not None
    assert updates[-1] == "hello world"
    assert len(ttft_values) == 1


@pytest.mark.asyncio
async def test_tool_registry_awaits_async_factory(monkeypatch):
    tool_registry = pytest.importorskip("fashion_bot.core.tool_registry")

    async def _fake_factory(**kwargs):
        assert kwargs["client_id"] == "c1"
        return ["async-tool"]

    monkeypatch.setitem(
        tool_registry.TOOL_REGISTRY,
        "async_test_agent",
        {
            "factory": "async_test_factory",
            "params": ["state", "messages_list", "client_id"],
            "topic": "general",
            "entity_type": "general",
            "prompt_name": "async_test_handler",
        },
    )
    monkeypatch.setattr(
        tool_registry,
        "_get_factory_map",
        lambda: {"async_test_factory": _fake_factory},
    )

    tools = await tool_registry.aget_tools_for_agent(
        agent_name="async_test_agent",
        state={},
        messages_list=[],
        client_id="c1",
    )

    assert tools == ["async-tool"]


def test_async_hot_path_source_guards():
    project_root = Path(__file__).resolve().parents[1]
    guarded_files = {
        "fashion_bot/agent_controller.py": ["ThreadPoolExecutor", "graph.invoke("],
        "fashion_bot/core/streaming_service.py": ["run_in_executor("],
        "fashion_bot/core/tool_registry.py": [
            "ThreadPoolExecutor",
            "_run_async_in_thread",
            "ProductOrchestrator.search_products_by_name(",
            "ProductOrchestrator.get_top_selling(",
        ],
        "fashion_bot/gupshup_webhook.py": [
            "requests.post(",
            "_ScheduledAsyncResult",
            "loop.create_task(coro)",
            "return asyncio.run(coro)",
        ],
        "fashion_bot/main.py": ["graph.invoke("],
        "fashion_bot/shopify/webhook/event_processor.py": [
            "asyncio.to_thread(",
            "requests.get(",
        ],
        "fashion_bot/tool_factory.py": [
            "_run_async_in_thread",
            "ProductOrchestrator.get_product_details_from_url(",
            "ProductOrchestrator.search_products_by_name(",
            "ProductOrchestrator.get_top_selling(",
            "ProductOrchestrator.aget_product_info_from_context(",
            "OrderCreationOrchestrator.create_draft_order_in_shopify(",
            "_safe_call(_update_order_shiprocket",
            "_safe_call(_update_order_notes_shopify",
            "_safe_call(_update_order_tags_shopify",
            "_safe_call(_update_order_phone_number",
            "_safe_call(_update_order_email",
            "_safe_call(_update_order_size",
            "_safe_call(_cancel_order_shopify",
            "_safe_call(_cancel_order_shiprocket",
            "OrderUpdateOrchestrator.update_name(",
            "OrderUpdateOrchestrator.update_order(",
            "OrderUpdateOrchestrator.update_phone(",
            "OrderUpdateOrchestrator.update_email(",
            "CancellationOrchestrator.cancel_order(",
            "CancellationOrchestrator.cancel_shipment(",
        ],
    }

    for relative_path, forbidden_tokens in guarded_files.items():
        source = (project_root / relative_path).read_text()
        for token in forbidden_tokens:
            assert token not in source, f"Found forbidden token {token!r} in {relative_path}"


def test_generic_skill_wrappers_await_async_nodes():
    project_root = Path(__file__).resolve().parents[1]
    async_wrapper_expectations = {
        "fashion_bot/graph_context_meta.py": [
            "delivery_intent_with_logging",
            "order_intent_with_logging",
            "return_policy_intent_with_logging",
            "product_details_intent_with_logging",
            "after_delivery_return_intent_with_logging",
            "discount_intent_with_logging",
            "place_order_intent_with_logging",
            "cancel_or_update_order_intent_with_logging",
            "product_change_in_order_intent_with_logging",
            "unknown_handler_with_logging",
            "delivery_policy_intent_with_logging",
            "payment_policy_intent_with_logging",
            "return_exchange_policy_intent_with_logging",
            "vendor_inquiry_intent_with_logging",
            "recommendations_intent_with_logging",
            "feedback_intent_with_logging",
            "escalation_handler_with_logging",
        ],
        "fashion_bot/graph_context_meta.py": [
            "recommendations_intent_with_logging",
        ],
    }

    for relative_path, function_names in async_wrapper_expectations.items():
        source = (project_root / relative_path).read_text()
        tree = ast.parse(source)
        defs = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for function_name in function_names:
            node = defs[function_name]
            assert isinstance(
                node, ast.AsyncFunctionDef
            ), f"{relative_path}:{function_name} must stay async"
            assert any(
                isinstance(subnode, ast.Await)
                and isinstance(subnode.value, ast.Call)
                and isinstance(subnode.value.func, ast.Name)
                and subnode.value.func.id.endswith("_node")
                and "generic" in subnode.value.func.id
                for subnode in ast.walk(node)
            ), f"{relative_path}:{function_name} must await its generic async node"


def test_gupshup_stream_trace_uses_single_graph_dump():
    project_root = Path(__file__).resolve().parents[1]
    source = (project_root / "fashion_bot/gupshup_webhook.py").read_text()

    assert 'trace_graph_internally=False' in source
    assert 'trace_run=turn_run' in source
    assert '_replace_trace_io(' in source
    assert '"input": str(message_content or "")[:2000]' in source
    assert '"output": str(final_reply or "")[:2000]' in source
    assert 'operation_name="gupshup.io.send_whatsapp_message_stream"' in source
    assert '_create_detached_task(' in source
    output_idx = source.rfind('_replace_trace_io(')
    send_idx = source.find('await _send_whatsapp_message_to_customer(', output_idx)
    assert output_idx != -1
    assert send_idx != -1
    assert output_idx < send_idx


def test_async_tag_generator_does_not_emit_langsmith_llm_trace():
    project_root = Path(__file__).resolve().parents[1]
    source = (project_root / "fashion_bot/async_tag_generator.py").read_text()

    assert 'with tracing_context(enabled=False, parent=False):' in source


def test_websocket_trace_records_normalized_message_io():
    project_root = Path(__file__).resolve().parents[1]
    source = (project_root / "fashion_bot/websocket_chat.py").read_text()

    assert 'input_message=message' in source
    assert '"input": clean_input[:2000]' in source
    assert '"output": reply_preview[:2000]' in source
    assert '"customer_message": clean_input[:2000]' not in source
    assert "def _replace_trace_io(" in source
    assert 'with _open_webchat_turn_trace(' in source
    assert 'trace_graph_internally=False' in source


def test_websocket_turn_uses_single_root_trace_with_nested_io_spans():
    project_root = Path(__file__).resolve().parents[1]
    source = (project_root / "fashion_bot/websocket_chat.py").read_text()
    tree = ast.parse(source)

    def _traced_operation_calls(scope):
        for node in ast.walk(scope):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "traced_operation":
                yield node

    traced_require_parent = {}
    for call in _traced_operation_calls(tree):
        # A non-literal span name would make this test blind to the call, which
        # is exactly how the empty-root-span regression hid here before: the
        # shared transcript helpers passed the name in as a parameter, so the
        # require_parent assertions below silently never saw them.
        assert call.args, "traced_operation() needs a positional span name"
        assert isinstance(call.args[0], ast.Constant) and isinstance(
            call.args[0].value, str
        ), "traced_operation() span name must be a literal so this test can see it"
        op_name = call.args[0].value
        for kw in call.keywords:
            if kw.arg == "require_parent" and isinstance(kw.value, ast.Constant):
                traced_require_parent[op_name] = kw.value.value

    assert "with _open_webchat_turn_trace(" in source
    assert 'name="general-streaming-conversation"' in source
    assert "trace_graph_internally=False" in source
    assert "_replace_trace_io(" in source
    assert traced_require_parent["webchat.io.store_bot_transcript"] is True
    assert traced_require_parent["webchat.io.persist_session_state"] is True
    assert traced_require_parent["webchat.io.store_bot_transcript_streaming"] is True
    assert traced_require_parent["webchat.io.persist_session_state_streaming"] is True

    # Every webchat.io.* span is an I/O sub-span of the turn and must nest under
    # the turn's root trace. Opened with require_parent=False outside that trace,
    # it lands in LangSmith as its own empty root run (no inputs/outputs) — one
    # per message — which is pure noise and cost. The turn's actual root is
    # opened by _open_webchat_turn_trace via trace(), not by traced_operation,
    # so this does not constrain legitimate root spans.
    for op_name, require_parent in traced_require_parent.items():
        if op_name.startswith("webchat.io."):
            assert require_parent is True, f"{op_name} must nest under the turn trace"

    # Transcript persistence runs before the turn trace opens, so it can carry
    # no span at all — not even one it names itself.
    helpers = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name
        in {"_webchat_store_inbound_customer_row", "_webchat_store_bot_row"}
    }
    assert set(helpers) == {
        "_webchat_store_inbound_customer_row",
        "_webchat_store_bot_row",
    }
    for name, node in helpers.items():
        assert not list(
            _traced_operation_calls(node)
        ), f"{name} runs before the turn trace opens; it must not open a span"


def test_place_order_factory_imports_typing_names_used_in_runtime_annotations():
    project_root = Path(__file__).resolve().parents[1]
    source = (project_root / "fashion_bot/tool_factory.py").read_text()
    tree = ast.parse(source)

    typing_imports = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "typing":
            typing_imports.update(alias.name for alias in node.names)

    assert {"Any", "Dict"} <= typing_imports
    assert "def _normalize_shopify_numeric_id(raw_id: Any, resource_type: str) -> str:" in source
    assert (
        "def _resolve_variant_id_from_product(product_info: Dict[str, Any], requested_size: str) -> str:"
        in source
    )


def _import_websocket_chat_module(monkeypatch):
    fake_graph_mod = types.ModuleType("fashion_bot.graph_context_meta")
    fake_graph_mod.graph = object()
    fake_graph_mod.generate_trace_id = lambda: "trace-test"
    monkeypatch.setitem(sys.modules, "fashion_bot.graph_context_meta", fake_graph_mod)
    sys.modules.pop("fashion_bot.websocket_chat", None)
    try:
        return importlib.import_module("fashion_bot.websocket_chat")
    except ImportError as exc:
        pytest.skip(f"websocket_chat import skipped due to missing dependency: {exc}")


def _import_streamlit_app_module(monkeypatch):
    fake_graph_mod = types.ModuleType("fashion_bot.graph_context_meta")
    fake_graph_mod.graph = object()
    monkeypatch.setitem(sys.modules, "fashion_bot.graph_context_meta", fake_graph_mod)
    monkeypatch.setenv("DATABASE_URL", "postgresql://tester:tester@localhost:5432/testdb")
    sys.modules.pop("streamlit_app", None)
    try:
        return importlib.import_module("streamlit_app")
    except ImportError as exc:
        pytest.skip(f"streamlit_app import skipped due to missing dependency: {exc}")


async def _empty_stream(_ctx):
    if False:
        yield {}
