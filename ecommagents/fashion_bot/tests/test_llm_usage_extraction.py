"""
Tests for LLM usage/cost extraction across the streaming and non-streaming
LangChain paths (``_extract_llm_usage`` + ``OTelLLMCallbackHandler``).

Background
----------
``on_llm_end`` used to read token usage *only* from
``response.llm_output["token_usage"]``. That dict is populated when a model is
invoked via ``ainvoke`` — but it is ``None`` when the model is streamed
(``astream``), which is how the whole conversational agent graph runs
(``core/streaming_service.py`` drives ``graph.astream_events`` /
``llm.astream``).

Consequence in production: ~31% of all calls (order_status, product_details,
recommendations, escalation, place_order, …) recorded ``model="unknown"`` with
zero prompt tokens, zero completion tokens and zero cost. That spend was
invisible, not absent — `llm_cost_usd_total` under-reported by roughly a third.

Two fixes are locked down here:

1. **Streaming usage is captured.** Model comes off
   ``message.response_metadata["model_name"]`` and tokens off the normalized
   ``message.usage_metadata``, so streamed calls stop reporting as unknown/zero.

2. **Real cost is preferred over the price table.** OpenRouter always returns
   ``usage.cost`` — the actual USD charged — and LangChain passes that block
   through verbatim on the non-streaming path. We bill from it when present and
   fall back to ``_estimate_llm_cost_usd`` only when the provider reported
   nothing (which includes every streamed call, since LangChain's
   ``usage_metadata`` normalization drops non-standard keys like ``cost``).

The ``cost_source`` label on ``llm.cost.usd`` records which of the two applied.
"""

import asyncio
import types

import pytest

from fashion_bot.monitoring.otel_metrics import (
    _coerce_cost_usd,
    _coerce_token_count,
    _estimate_llm_cost_usd,
    _extract_llm_usage,
)

# An OpenRouter usage block. `cost` is the actual amount charged to the account.
OPENROUTER_USAGE = {
    "prompt_tokens": 194,
    "completion_tokens": 2,
    "total_tokens": 196,
    "cost": 0.00042,
    "cost_details": {"upstream_inference_cost": 0.00039},
}


# ---------------------------------------------------------------------------
# Helpers — build LLMResult-shaped objects without needing a live provider
# ---------------------------------------------------------------------------

def _message(usage_metadata=None, response_metadata=None):
    return types.SimpleNamespace(
        usage_metadata=usage_metadata,
        response_metadata=response_metadata or {},
    )


def _result(llm_output=None, generations=None):
    return types.SimpleNamespace(
        llm_output=llm_output,
        generations=generations if generations is not None else [],
    )


# ---------------------------------------------------------------------------
# Non-streaming: real cost is available and must win over the estimate
# ---------------------------------------------------------------------------

def test_non_streaming_reads_real_cost_from_llm_output():
    usage = _extract_llm_usage(
        _result(
            llm_output={
                "model_name": "openai/gpt-4o-mini",
                "token_usage": OPENROUTER_USAGE,
            }
        )
    )

    assert usage.model == "openai/gpt-4o-mini"
    assert usage.prompt_tokens == 194
    assert usage.completion_tokens == 2
    assert usage.actual_cost_usd == pytest.approx(0.00042)


def test_real_cost_differs_from_price_table_estimate():
    """The whole point of fix #2 — the estimate is not the billed number."""
    estimate = _estimate_llm_cost_usd("openai/gpt-4o-mini", 194, 2)
    assert estimate != pytest.approx(OPENROUTER_USAGE["cost"])


def test_free_model_zero_cost_is_actual_not_missing():
    """cost=0.0 is a real answer (free model), not 'provider said nothing'."""
    usage = _extract_llm_usage(
        _result(
            llm_output={
                "model_name": "some/free-model",
                "token_usage": {"prompt_tokens": 10, "completion_tokens": 1, "cost": 0.0},
            }
        )
    )
    assert usage.actual_cost_usd == 0.0  # not None


def test_missing_cost_key_falls_back_to_estimate():
    """A provider that reports no cost must leave actual_cost_usd unset."""
    usage = _extract_llm_usage(
        _result(
            llm_output={
                "model_name": "openai/gpt-4o-mini",
                "token_usage": {"prompt_tokens": 194, "completion_tokens": 2},
            }
        )
    )
    assert usage.actual_cost_usd is None
    assert usage.prompt_tokens == 194


# ---------------------------------------------------------------------------
# Streaming: llm_output is None — this is the ~31% blind spot
# ---------------------------------------------------------------------------

def test_streaming_recovers_model_and_tokens():
    usage = _extract_llm_usage(
        _result(
            llm_output=None,
            generations=[[
                types.SimpleNamespace(
                    message=_message(
                        usage_metadata={"input_tokens": 194, "output_tokens": 2},
                        response_metadata={"model_name": "openai/gpt-4o-mini"},
                    )
                )
            ]],
        )
    )

    # Previously: ("unknown", 0, 0) — the bug.
    assert usage.model == "openai/gpt-4o-mini"
    assert usage.prompt_tokens == 194
    assert usage.completion_tokens == 2
    # LangChain drops non-standard keys when normalizing usage_metadata, so the
    # provider's real cost is genuinely unavailable here; caller estimates.
    assert usage.actual_cost_usd is None


def test_tokens_from_llm_output_are_not_double_counted():
    """llm_output already aggregates across generations — don't add both."""
    usage = _extract_llm_usage(
        _result(
            llm_output={"model_name": "openai/gpt-4o-mini", "token_usage": OPENROUTER_USAGE},
            generations=[[types.SimpleNamespace(
                message=_message(usage_metadata={"input_tokens": 194, "output_tokens": 2})
            )]],
        )
    )
    assert (usage.prompt_tokens, usage.completion_tokens) == (194, 2)


def test_model_recovered_from_generations_when_llm_output_lacks_it():
    usage = _extract_llm_usage(
        _result(
            llm_output={"token_usage": {"prompt_tokens": 5, "completion_tokens": 1}},
            generations=[[types.SimpleNamespace(
                message=_message(response_metadata={"model_name": "google/gemini-3.1-flash-lite"})
            )]],
        )
    )
    assert usage.model == "google/gemini-3.1-flash-lite"
    assert (usage.prompt_tokens, usage.completion_tokens) == (5, 1)


def test_streaming_sums_tokens_across_generations():
    gen = lambda p, c: types.SimpleNamespace(  # noqa: E731 - table-style fixture
        message=_message(
            usage_metadata={"input_tokens": p, "output_tokens": c},
            response_metadata={"model_name": "openai/gpt-4o-mini"},
        )
    )
    usage = _extract_llm_usage(_result(llm_output=None, generations=[[gen(10, 1), gen(20, 3)]]))

    assert usage.prompt_tokens == 30
    assert usage.completion_tokens == 4


# ---------------------------------------------------------------------------
# Robustness — a callback must never break the caller's LLM request
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "response",
    [
        _result(),                                            # nothing at all
        _result(llm_output={}),                               # empty dict
        _result(llm_output={"token_usage": None}),            # null usage
        _result(llm_output={"token_usage": "not-a-dict"}),    # wrong type
        _result(llm_output=None, generations=[[]]),           # empty batch
        _result(llm_output=None, generations=[[object()]]),   # no .message
        _result(llm_output=None, generations=[[types.SimpleNamespace(message=None)]]),
        types.SimpleNamespace(),                              # not an LLMResult
    ],
)
def test_malformed_responses_degrade_quietly(response):
    usage = _extract_llm_usage(response)
    assert usage.model == "unknown"
    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 0
    assert usage.actual_cost_usd is None


@pytest.mark.parametrize(
    "value,expected",
    [(None, 0), (True, 0), (False, 0), ("12", 12), (-5, 0), ("abc", 0), (7.9, 7)],
)
def test_coerce_token_count(value, expected):
    assert _coerce_token_count(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [(None, None), (True, None), ("abc", None), (-1.0, None), ("0.5", 0.5), (0, 0.0)],
)
def test_coerce_cost_usd(value, expected):
    assert _coerce_cost_usd(value) == expected


def test_coerce_cost_usd_rejects_nan():
    assert _coerce_cost_usd(float("nan")) is None


# ---------------------------------------------------------------------------
# End-to-end through real LangChain, with a faked OpenRouter transport.
# This is what actually proves the streaming path behaves as claimed.
# ---------------------------------------------------------------------------

def _as_sdk_object(payload):
    """dict -> attribute tree mimicking the openai SDK response models."""
    if isinstance(payload, dict):
        obj = types.SimpleNamespace(**{k: _as_sdk_object(v) for k, v in payload.items()})
        obj.model_dump = lambda _p=payload, **_kw: _p
        return obj
    if isinstance(payload, list):
        return [_as_sdk_object(item) for item in payload]
    return payload


_COMPLETION = {
    "id": "gen-test", "model": "openai/gpt-4o-mini", "object": "chat.completion",
    "created": 1785900000, "usage": OPENROUTER_USAGE,
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "hello"}}],
}

_CHUNKS = [
    {"id": "gen-test", "model": "openai/gpt-4o-mini", "object": "chat.completion.chunk",
     "created": 1785900000,
     "choices": [{"index": 0, "finish_reason": None,
                  "delta": {"role": "assistant", "content": "hel"}}]},
    {"id": "gen-test", "model": "openai/gpt-4o-mini", "object": "chat.completion.chunk",
     "created": 1785900000,
     "choices": [{"index": 0, "finish_reason": "stop", "delta": {"content": "lo"}}]},
    # OpenRouter sends usage in the final SSE message, with no choices.
    {"id": "gen-test", "model": "openai/gpt-4o-mini", "object": "chat.completion.chunk",
     "created": 1785900000, "choices": [], "usage": OPENROUTER_USAGE},
]


def _patched_openrouter_llm():
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(model="openai/gpt-4o-mini", api_key="sk-test",
                     base_url="https://openrouter.ai/api/v1")

    async def _create(*_args, stream=False, **_kwargs):
        if stream:
            class _Stream:
                async def __aenter__(self):
                    async def gen():
                        for chunk in _CHUNKS:
                            yield _as_sdk_object(chunk)
                    return gen()

                async def __aexit__(self, *_exc):
                    return False
            return _Stream()
        return _as_sdk_object(_COMPLETION)

    class _RawResponse:
        async def create(self, *args, **kwargs):
            parsed = await _create(*args, **kwargs)
            return types.SimpleNamespace(parse=lambda _p=parsed: _p)

    llm.root_async_client.chat.completions.create = _create
    llm.root_async_client.chat.completions.with_raw_response = _RawResponse()
    return llm


class _CaptureHandler:
    """Records what _extract_llm_usage sees for each completed call."""

    def __init__(self):
        self.seen = []

    def __call__(self, response):
        self.seen.append(_extract_llm_usage(response))


@pytest.fixture
def _langchain_available():
    pytest.importorskip("langchain_openai")


def test_handler_labels_actual_vs_estimated_cost(monkeypatch):
    """The cost counter must carry cost_source so the two are separable."""
    from fashion_bot.monitoring import otel_metrics

    recorded = []
    monkeypatch.setattr(
        otel_metrics, "llm_cost_usd_counter",
        types.SimpleNamespace(add=lambda value, labels: recorded.append((value, labels))),
    )

    handler = otel_metrics.OTelLLMCallbackHandler()

    # Non-streaming -> provider reported cost -> billed verbatim.
    handler.on_llm_end(
        _result(llm_output={"model_name": "openai/gpt-4o-mini", "token_usage": OPENROUTER_USAGE}),
        run_id="r1",
    )
    # Streaming -> no cost reported -> price-table estimate.
    handler.on_llm_end(
        _result(llm_output=None, generations=[[types.SimpleNamespace(
            message=_message(
                usage_metadata={"input_tokens": 194, "output_tokens": 2},
                response_metadata={"model_name": "openai/gpt-4o-mini"},
            )
        )]]),
        run_id="r2",
    )

    assert len(recorded) == 2
    actual_value, actual_labels = recorded[0]
    est_value, est_labels = recorded[1]

    assert actual_labels["cost_source"] == "actual"
    assert actual_value == pytest.approx(OPENROUTER_USAGE["cost"])
    assert est_labels["cost_source"] == "estimated"
    assert est_value == pytest.approx(_estimate_llm_cost_usd("openai/gpt-4o-mini", 194, 2))
    # Both keep the pre-existing labels the dashboards group by.
    for labels in (actual_labels, est_labels):
        assert {"client_id", "client_name", "model", "caller"} <= labels.keys()


def test_handler_never_raises_on_malformed_response():
    """A callback exception must not surface into the caller's LLM request."""
    from fashion_bot.monitoring import otel_metrics

    handler = otel_metrics.OTelLLMCallbackHandler()
    handler.on_llm_end(types.SimpleNamespace(), run_id="r-malformed")


def test_end_to_end_non_streaming_yields_actual_cost(_langchain_available):
    from langchain_core.callbacks import BaseCallbackHandler

    capture = _CaptureHandler()

    class _Probe(BaseCallbackHandler):
        def on_llm_end(self, response, *, run_id, **kwargs):
            capture(response)

    llm = _patched_openrouter_llm()
    asyncio.run(llm.ainvoke("hi", config={"callbacks": [_Probe()]}))

    assert len(capture.seen) == 1
    usage = capture.seen[0]
    assert usage.model == "openai/gpt-4o-mini"
    assert (usage.prompt_tokens, usage.completion_tokens) == (194, 2)
    assert usage.actual_cost_usd == pytest.approx(0.00042)


def test_end_to_end_streaming_yields_tokens_and_model(_langchain_available):
    from langchain_core.callbacks import BaseCallbackHandler

    capture = _CaptureHandler()

    class _Probe(BaseCallbackHandler):
        def on_llm_end(self, response, *, run_id, **kwargs):
            capture(response)

    llm = _patched_openrouter_llm()

    async def _drain():
        async for _ in llm.astream("hi", config={"callbacks": [_Probe()]}):
            pass

    asyncio.run(_drain())

    assert len(capture.seen) == 1
    usage = capture.seen[0]
    # The regression this whole change exists to prevent.
    assert usage.model != "unknown"
    assert usage.prompt_tokens == 194
    assert usage.completion_tokens == 2
    # Cost unavailable on this path -> handler estimates from the price table.
    assert usage.actual_cost_usd is None
    assert _estimate_llm_cost_usd(usage.model, 194, 2) > 0
