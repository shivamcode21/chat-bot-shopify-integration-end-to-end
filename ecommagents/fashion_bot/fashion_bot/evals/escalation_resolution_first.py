"""
Escalation resolution-first — LangSmith eval set (design go-live step 6).

This is the golden dataset + harness that gates the resolution-first escalation
work (design_docs/ESCALATION_RESOLUTION_FIRST_FRAMEWORK.md). It exists so we can
measure, before rolling the behavioural changes to all tenants, that the bot:

  * resolves / clarifies / de-escalates the cases it should, and
  * still escalates the cases a human genuinely must handle.

Two layers, one dataset:

1. **Deterministic layer (runs now, no LLM, no network).** Scores the two
   decisions this PR enforces in code — ``classify_escalation_actionability``
   (and the ``aevaluate_escalation_gate`` soft-block it drives) and
   ``frustration_should_escalate`` (the first-turn routing rule). Run it in CI:

       python -m fashion_bot.evals.escalation_resolution_first --check

   (``tests/test_escalation_eval_dataset.py`` also asserts it stays green.)

2. **Full-agent behavioural layer (staging).** The same examples carry a
   ``should_escalate`` gold label and an ``expected_behavior`` note. Point a
   target that runs the real graph at the uploaded dataset and score it with the
   evaluators below. Upload the dataset with:

       LANGSMITH_API_KEY=... python -m fashion_bot.evals.escalation_resolution_first --upload

   then, in a staging harness::

       from langsmith import Client
       from fashion_bot.evals.escalation_resolution_first import (
           DATASET_NAME, EVALUATORS,
       )
       Client().evaluate(my_agent_target, data=DATASET_NAME, evaluators=EVALUATORS)

   where ``my_agent_target(inputs) -> {"escalated": bool, "reply": str, ...}``
   runs the conversation graph on ``inputs["message"]``.

Every example is drawn from (or modelled closely on) a real production
conversation from the last 10 days.
"""

from __future__ import annotations

from typing import Any, Dict, List

from fashion_bot.agent_config import (
    classify_escalation_actionability,
    frustration_should_escalate,
)

DATASET_NAME = "escalation-resolution-first"
DATASET_DESCRIPTION = (
    "Golden set for resolution-first escalation handling: the bot should resolve/"
    "clarify/de-escalate what it can and escalate only when a human must act. "
    "Covers the three RCA incidents plus siblings (vague, emotional, unfulfillable, "
    "lead-capture, mandatory hand-offs)."
)

# ---------------------------------------------------------------------------
# Golden examples
# ---------------------------------------------------------------------------
# Each example:
#   inputs:
#     message           customer's message (verbatim / close to real)
#     channel           whatsapp | web-chat
#     context           one-line situation
#     detected_intents  intents the router would emit (for the frustration rule)
#     is_frustrated     router frustration flag
#     escalation        {category, reason} the agent WOULD pass to escalate_to_agent
#   outputs (gold labels):
#     should_escalate       ideal END behaviour (for the full-agent eval)
#     expected_actionability  actionable | unfulfillable | mandatory  (gate layer)
#     frustration_route_escalates  None, or bool for frustration router examples
#     expected_behavior     short description of the ideal bot action
#     lever                 which mechanism should produce it
#   metadata: incident, source, split
GOLDEN_EXAMPLES: List[Dict[str, Any]] = [
    # ── RESOLVE (should_escalate = False) ──────────────────────────────────
    {
        "id": "incident-a-vague-complaint",
        "inputs": {
            "message": "Complaint",
            "channel": "whatsapp",
            "context": "one word, no issue stated; customer has recent orders",
            "detected_intents": ["escalation"],
            "is_frustrated": False,
            "escalation": {"category": "Product Complaint",
                           "reason": "General product complaint from customer."},
        },
        "outputs": {
            "should_escalate": False,
            "frustration_route_escalates": None,
            "expected_actionability": "actionable",
            "expected_behavior": "Ask one clarifying question (what went wrong + which order) before escalating.",
            "lever": "prompt-clarify",
        },
        "metadata": {"incident": "A", "source": "conv e001007d", "split": "core"},
    },
    {
        "id": "incident-b-fraud-objection",
        "inputs": {
            "message": "It means you are doing fraud",
            "channel": "web-chat",
            "context": "reacting to the wallet-only refund policy just explained",
            "detected_intents": ["return_exchange_policy"],
            "is_frustrated": True,
            "escalation": {"category": "Frustration",
                           "reason": "Customer called the refund policy fraud."},
        },
        "outputs": {
            "should_escalate": False,
            "frustration_route_escalates": False,
            "expected_actionability": "actionable",
            "expected_behavior": "De-escalate: acknowledge, explain why refunds go to the wallet and how to use it. Escalate only if they then ask for a human.",
            "lever": "frustration-router + prompt-deescalate",
        },
        "metadata": {"incident": "B", "source": "session fbw_ms5zjby8y8qfdmqbi", "split": "core"},
    },
    {
        "id": "incident-c-nonexistent-variant",
        "inputs": {
            "message": "I buy pack of 3 16g",
            "channel": "whatsapp",
            "context": "only Pack of 1 (8g) and Pack of 2 (16g) exist",
            "detected_intents": ["cancel_or_update_order"],
            "is_frustrated": False,
            # LLM sets human_can_resolve=False: the "Pack of 3" doesn't exist, so
            # a human can't fulfil it either. (No keyword matching of the reason.)
            "escalation": {"category": "Order Update",
                           "reason": "Product variant not available as requested.",
                           "human_can_resolve": False},
        },
        "outputs": {
            "should_escalate": False,
            "frustration_route_escalates": None,
            "expected_actionability": "unfulfillable",
            "expected_behavior": "Offer the two real pack sizes and ask which they'd like; do not escalate.",
            "lever": "actionability-gate (LLM: human_can_resolve=False)",
        },
        "metadata": {"incident": "C", "source": "conv 48cc849b", "split": "core"},
    },
    {
        "id": "order-status-question",
        "inputs": {
            "message": "Where is my order gv16798?",
            "channel": "whatsapp",
            "context": "order In Transit with a tracking link",
            "detected_intents": ["order_status"],
            "is_frustrated": False,
            "escalation": {"category": "Order Status Query",
                           "reason": "Customer asked for order status."},
        },
        "outputs": {
            "should_escalate": False,
            "frustration_route_escalates": None,
            "expected_actionability": "actionable",
            "expected_behavior": "Look up tracking and answer with status + link.",
            "lever": "agent-resolves",
        },
        "metadata": {"incident": None, "source": "conv 2f50b5ea", "split": "resolve"},
    },
    {
        "id": "refund-status-question",
        "inputs": {
            "message": "Where's my 200 rupee refund?",
            "channel": "whatsapp",
            "context": "advance payment on a delayed order",
            "detected_intents": ["order_status"],
            "is_frustrated": False,
            "escalation": {"category": "Payment/Refund Status",
                           "reason": "Customer asking where their refund is."},
        },
        "outputs": {
            "should_escalate": False,
            "frustration_route_escalates": None,
            "expected_actionability": "actionable",
            "expected_behavior": "Check wallet/refund status, explain the 6-7 day SLA; escalate only a genuine breach.",
            "lever": "prompt-attempt-first",
        },
        "metadata": {"incident": None, "source": "conv 2f50b5ea", "split": "resolve"},
    },
    {
        "id": "frustration-with-order-intent",
        "inputs": {
            "message": "kaha hai mera order, bakwaas service hai",
            "channel": "whatsapp",
            "context": "frustrated but the ask is an order-status lookup",
            "detected_intents": ["order_status"],
            "is_frustrated": True,
            "escalation": {"category": "Frustration",
                           "reason": "Customer frustrated about delivery."},
        },
        "outputs": {
            "should_escalate": False,
            "frustration_route_escalates": False,
            "expected_actionability": "actionable",
            "expected_behavior": "Route to order_status and answer the tracking; the answer de-escalates.",
            "lever": "prompt-deescalate",
        },
        "metadata": {"incident": None, "source": "pattern (Delivery Query storm)", "split": "resolve"},
    },
    {
        "id": "return-policy-question",
        "inputs": {
            "message": "Can I return this top?",
            "channel": "whatsapp",
            "context": "policy question, no order referenced",
            "detected_intents": ["return_exchange_policy"],
            "is_frustrated": False,
            "escalation": {"category": "Return Request",
                           "reason": "Customer asking about returns."},
        },
        "outputs": {
            "should_escalate": False,
            "frustration_route_escalates": None,
            "expected_actionability": "actionable",
            "expected_behavior": "Answer the return policy and share the self-serve Return Prime link.",
            "lever": "agent-resolves",
        },
        "metadata": {"incident": None, "source": "policy", "split": "resolve"},
    },
    {
        "id": "cancel-out-for-delivery",
        "inputs": {
            "message": "How can I cancel my order",
            "channel": "whatsapp",
            "context": "order gv16892 is out for delivery",
            "detected_intents": ["cancel_or_update_order"],
            "is_frustrated": False,
            "escalation": None,
        },
        "outputs": {
            "should_escalate": False,
            "frustration_route_escalates": None,
            "expected_actionability": None,
            "expected_behavior": "Explain it's out for delivery so can't be cancelled; offer return/exchange after delivery.",
            "lever": "agent-resolves (eligibility)",
        },
        "metadata": {"incident": None, "source": "conv 28a1ef04", "split": "resolve"},
    },
    # ── ESCALATE (should_escalate = True) ──────────────────────────────────
    {
        "id": "restocking-lead",
        "inputs": {
            "message": "Notify me when Neo Tribe Denim is back in stock",
            "channel": "web-chat",
            "context": "sold-out product",
            "detected_intents": ["product_details"],
            "is_frustrated": False,
            "escalation": {"category": "Restocking Query",
                           "reason": "Customer wants a restock notification."},
        },
        "outputs": {
            "should_escalate": True,
            "frustration_route_escalates": None,
            "expected_actionability": "actionable",
            "expected_behavior": "Escalate immediately as a lead; the bot can't restock.",
            "lever": "prompt-escalate-now",
        },
        "metadata": {"incident": None, "source": "conv bf580765", "split": "escalate"},
    },
    {
        "id": "bulk-order-lead",
        "inputs": {
            "message": "I want to place a bulk corporate order",
            "channel": "whatsapp",
            "context": "wholesale request",
            "detected_intents": ["discount"],
            "is_frustrated": False,
            "escalation": {"category": "Bulk Order Discount",
                           "reason": "Customer wants a bulk order."},
        },
        "outputs": {
            "should_escalate": True,
            "frustration_route_escalates": None,
            "expected_actionability": "actionable",
            "expected_behavior": "Escalate immediately to sales; the bot can't price a bulk deal.",
            "lever": "prompt-escalate-now",
        },
        "metadata": {"incident": None, "source": "Bulk Order Discount escalations", "split": "escalate"},
    },
    {
        "id": "cancellation-nonfixable",
        "inputs": {
            "message": "Cancel my order, I found it cheaper elsewhere",
            "channel": "whatsapp",
            "context": "eligible order, non-fixable reason",
            "detected_intents": ["cancel_or_update_order"],
            "is_frustrated": False,
            "escalation": {"category": "Cancellation Requests",
                           "reason": "Customer found it cheaper; wants to cancel."},
        },
        "outputs": {
            "should_escalate": True,
            "frustration_route_escalates": None,
            "expected_actionability": "mandatory",
            "expected_behavior": "Bot can't cancel directly; a human performs it. Never gated.",
            "lever": "mandatory hand-off",
        },
        "metadata": {"incident": None, "source": "cancellation_handler prompt", "split": "escalate"},
    },
    {
        "id": "callback-request",
        "inputs": {
            "message": "Please call me back tomorrow",
            "channel": "whatsapp",
            "context": "explicit callback ask",
            "detected_intents": ["escalation"],
            "is_frustrated": False,
            "escalation": {"category": "Callback Request",
                           "reason": "Customer requested a callback."},
        },
        "outputs": {
            "should_escalate": True,
            "frustration_route_escalates": None,
            "expected_actionability": "mandatory",
            "expected_behavior": "Schedule the callback and hand off.",
            "lever": "mandatory hand-off",
        },
        "metadata": {"incident": None, "source": "Callback Request escalations", "split": "escalate"},
    },
    {
        "id": "explicit-human-request",
        "inputs": {
            "message": "I want to talk to a human agent",
            "channel": "whatsapp",
            "context": "explicit human ask",
            "detected_intents": ["escalation"],
            "is_frustrated": True,
            "escalation": {"category": "General",
                           "reason": "Customer explicitly asked for a human."},
        },
        "outputs": {
            "should_escalate": True,
            "frustration_route_escalates": True,
            "expected_actionability": "actionable",
            "expected_behavior": "Escalate — an explicit human request is honoured immediately.",
            "lever": "prompt-escalate-now",
        },
        "metadata": {"incident": None, "source": "policy", "split": "escalate"},
    },
    {
        "id": "pure-anger-no-intent",
        "inputs": {
            "message": "You useless bots, worst service ever",
            "channel": "whatsapp",
            "context": "pure anger, nothing resolvable stated",
            "detected_intents": ["escalation"],
            "is_frustrated": True,
            "escalation": {"category": "Frustration",
                           "reason": "Customer venting; no resolvable request."},
        },
        "outputs": {
            "should_escalate": True,
            "frustration_route_escalates": True,
            "expected_actionability": "actionable",
            "expected_behavior": "Standalone frustration with nothing to resolve → escalate.",
            "lever": "prompt-escalate-now",
        },
        "metadata": {"incident": None, "source": "pattern", "split": "escalate"},
    },
    {
        "id": "system-error-tool-failed",
        "inputs": {
            "message": "Cancel order #71130",
            "channel": "whatsapp",
            "context": "the cancel tool errored out",
            "detected_intents": ["cancel_or_update_order"],
            "is_frustrated": False,
            "escalation": {"category": "System Error - Order Update/Cancel Failed",
                           "reason": "Cancel tool failed; needs manual handling."},
        },
        "outputs": {
            "should_escalate": True,
            "frustration_route_escalates": None,
            "expected_actionability": "actionable",
            "expected_behavior": "A tool failed — escalate immediately, nothing for the bot to retry.",
            "lever": "prompt-escalate-now",
        },
        "metadata": {"incident": None, "source": "System Error escalations", "split": "escalate"},
    },
    {
        "id": "missing-item-complaint",
        "inputs": {
            "message": "The lip massager is missing from my order #70977",
            "channel": "whatsapp",
            "context": "specific, verified order; item genuinely missing",
            "detected_intents": ["escalation"],
            "is_frustrated": False,
            "escalation": {"category": "Product Complaint",
                           "reason": "Missing item from order #70977."},
        },
        "outputs": {
            "should_escalate": True,
            "frustration_route_escalates": None,
            "expected_actionability": "actionable",
            "expected_behavior": "Gather the details, then escalate for fulfilment to investigate. T1 can still escalate when a human must act.",
            "lever": "prompt-attempt-then-escalate",
        },
        "metadata": {"incident": None, "source": "Product Complaint escalations", "split": "escalate"},
    },
    {
        "id": "unfulfillable-false-positive-guard",
        "inputs": {
            "message": "My order isn't showing anywhere",
            "channel": "whatsapp",
            "context": "the order genuinely can't be found — needs a human, NOT a dead-end",
            "detected_intents": ["order_status"],
            "is_frustrated": False,
            # Same "does not exist" words as Incident C, but the LLM correctly
            # sets human_can_resolve=True — a human CAN find/fix a missing order.
            # This is exactly why the signal is LLM-driven, not keyword-matched.
            "escalation": {"category": "General",
                           "reason": "Customer's order does not exist in our system.",
                           "human_can_resolve": True},
        },
        "outputs": {
            "should_escalate": True,
            "frustration_route_escalates": None,
            "expected_actionability": "actionable",
            "expected_behavior": "'order does not exist' must NOT be treated as an unfulfillable product request — it needs a human. The LLM sets human_can_resolve=True so the gate does not soft-block it.",
            "lever": "gate-guard (LLM: human_can_resolve=True)",
        },
        "metadata": {"incident": None, "source": "false-positive guard", "split": "escalate"},
    },
    {
        "id": "warranty-claim",
        "inputs": {
            "message": "The zip broke, I want to claim warranty",
            "channel": "whatsapp",
            "context": "warranty needs human assessment",
            "detected_intents": ["escalation"],
            "is_frustrated": False,
            "escalation": {"category": "Warranty Claim",
                           "reason": "Customer raising a warranty claim."},
        },
        "outputs": {
            "should_escalate": True,
            "frustration_route_escalates": None,
            "expected_actionability": "actionable",
            "expected_behavior": "Escalate — a warranty claim is a human judgement call.",
            "lever": "prompt-escalate-now",
        },
        "metadata": {"incident": None, "source": "Warranty Claim escalations", "split": "escalate"},
    },
    {
        "id": "exchange-unconfigured-partner",
        "inputs": {
            "message": "Change my size to X-Large",
            "channel": "whatsapp",
            "context": "delivered order; automated exchange partner not configured",
            "detected_intents": ["after_delivery_return_exchange"],
            "is_frustrated": False,
            "escalation": {"category": "Exchange Request",
                           "reason": "Automated size change failed; partner not configured."},
        },
        "outputs": {
            "should_escalate": True,
            "frustration_route_escalates": None,
            "expected_actionability": "actionable",
            "expected_behavior": "Offer the self-serve exchange first; escalate the residual (unconfigured partner) — a real hand-off.",
            "lever": "prompt-attempt-then-escalate",
        },
        "metadata": {"incident": None, "source": "conv a9b25b27", "split": "escalate"},
    },
    {
        "id": "offline-store-lead",
        "inputs": {
            "message": "Is there a store near me in Hyderabad?",
            "channel": "web-chat",
            "context": "system suggests a store visit → logged as a lead",
            "detected_intents": ["product_details"],
            "is_frustrated": False,
            "escalation": {"category": "Offline Store Suggestion",
                           "reason": "Store visit suggested: GANT HYDERABAD."},
        },
        "outputs": {
            "should_escalate": True,
            "frustration_route_escalates": None,
            "expected_actionability": "mandatory",
            "expected_behavior": "System lead — a human follows up; never gated.",
            "lever": "mandatory hand-off (system lead)",
        },
        "metadata": {"incident": None, "source": "Offline Store Suggestion escalations", "split": "escalate"},
    },
]


# ---------------------------------------------------------------------------
# Deterministic scorer — runs the shipped pure functions, no LLM / no network
# ---------------------------------------------------------------------------
def score_example_deterministic(example: Dict[str, Any]) -> Dict[str, Any]:
    """Check the dimensions this PR enforces in code for one example.

    Returns ``{"checks": {name: {"ok", "expected", "actual"}}, "ok": bool}``.
    The actionability gate and the first-turn frustration routing rule are
    code-enforced; every other expectation in the dataset (clarify, de-escalate,
    attempt-then-escalate) is prompt-driven and belongs to the full-agent layer.
    """
    inp, out = example["inputs"], example["outputs"]
    checks: Dict[str, Dict[str, Any]] = {}
    esc = inp.get("escalation") or {}
    category = esc.get("category")
    # LLM-driven signal the escalating agent would pass on escalate_to_agent
    # (default True = a human can act). No keyword matching of the reason text.
    human_can_resolve = esc.get("human_can_resolve", True)

    if category and out.get("expected_actionability") is not None:
        actual = classify_escalation_actionability(
            category, human_can_resolve=human_can_resolve
        )
        checks["actionability"] = {
            "ok": actual == out["expected_actionability"],
            "expected": out["expected_actionability"],
            "actual": actual,
        }
        # The soft-block is a pure consequence of the actionability label: with
        # the gate on by default, an unfulfillable request is blocked and
        # nothing else is.
        expect_softblock = out["expected_actionability"] == "unfulfillable"
        actual_softblock = actual == "unfulfillable"
        checks["gate_softblock"] = {
            "ok": actual_softblock == expect_softblock,
            "expected": expect_softblock,
            "actual": actual_softblock,
        }

    if inp.get("is_frustrated") and out.get("frustration_route_escalates") is not None:
        actual_fr = frustration_should_escalate(
            [{"intent": i} for i in (inp.get("detected_intents") or [])]
        )
        checks["frustration_route"] = {
            "ok": actual_fr == out["frustration_route_escalates"],
            "expected": out["frustration_route_escalates"],
            "actual": actual_fr,
        }

    return {"checks": checks, "ok": all(c["ok"] for c in checks.values())}


def run_deterministic(verbose: bool = True) -> Dict[str, Any]:
    """Score every example on the code-enforced dimensions. Returns a summary."""
    passed, failed = 0, 0
    failures: List[str] = []
    for ex in GOLDEN_EXAMPLES:
        res = score_example_deterministic(ex)
        if res["ok"]:
            passed += 1
        else:
            failed += 1
            bad = {k: v for k, v in res["checks"].items() if not v["ok"]}
            failures.append(f"{ex['id']}: {bad}")
        if verbose:
            mark = "PASS" if res["ok"] else "FAIL"
            print(f"  [{mark}] {ex['id']}")
    if verbose:
        print(f"\nDeterministic layer: {passed} passed, {failed} failed, {len(GOLDEN_EXAMPLES)} total")
        for f in failures:
            print(f"  ✗ {f}")
    return {"passed": passed, "failed": failed, "total": len(GOLDEN_EXAMPLES), "failures": failures}


# ---------------------------------------------------------------------------
# LangSmith upload + evaluators (full-agent behavioural layer)
# ---------------------------------------------------------------------------
def _to_langsmith_rows() -> List[Dict[str, Any]]:
    rows = []
    for ex in GOLDEN_EXAMPLES:
        rows.append({
            "inputs": ex["inputs"],
            "outputs": ex["outputs"],
            "metadata": {**ex.get("metadata", {}), "example_id": ex["id"]},
        })
    return rows


def upload(dataset_name: str = DATASET_NAME, client: Any = None) -> str:
    """Create/refresh the dataset in LangSmith and add the golden examples.

    Requires ``LANGSMITH_API_KEY`` in the environment (or a passed ``client``).
    Idempotent on the dataset name: reuses the dataset if it already exists.
    Returns the dataset id.
    """
    from langsmith import Client

    client = client or Client()
    if client.has_dataset(dataset_name=dataset_name):
        ds = client.read_dataset(dataset_name=dataset_name)
    else:
        ds = client.create_dataset(dataset_name=dataset_name, description=DATASET_DESCRIPTION)

    rows = _to_langsmith_rows()
    # Current LangSmith SDK takes a list of {inputs, outputs, metadata} dicts.
    client.create_examples(dataset_id=ds.id, examples=rows)
    print(f"Uploaded {len(rows)} examples to dataset '{dataset_name}' ({ds.id})")
    return str(ds.id)


# Evaluators for the full-agent run. Target contract:
#   target(inputs: dict) -> {"escalated": bool, "reply": str}
# LangSmith calls each evaluator with (outputs, reference_outputs) where
# ``outputs`` is the target's return and ``reference_outputs`` is the gold label.
def eval_should_escalate(outputs: dict, reference_outputs: dict) -> dict:
    """Did the agent escalate exactly when it should? The headline metric."""
    predicted = bool(outputs.get("escalated"))
    expected = bool(reference_outputs.get("should_escalate"))
    return {"key": "escalates_correctly", "score": int(predicted == expected)}


def eval_no_over_escalation(outputs: dict, reference_outputs: dict) -> dict:
    """Penalise ONLY the over-escalation direction (escalated when it shouldn't)."""
    over = bool(outputs.get("escalated")) and not bool(reference_outputs.get("should_escalate"))
    return {"key": "no_over_escalation", "score": int(not over)}


def eval_no_missed_escalation(outputs: dict, reference_outputs: dict) -> dict:
    """Penalise the dangerous direction (did NOT escalate when it should have)."""
    missed = (not bool(outputs.get("escalated"))) and bool(reference_outputs.get("should_escalate"))
    return {"key": "no_missed_escalation", "score": int(not missed)}


EVALUATORS = [eval_should_escalate, eval_no_over_escalation, eval_no_missed_escalation]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Escalation resolution-first eval set")
    parser.add_argument("--check", action="store_true",
                        help="Run the deterministic scorer locally (no LLM / no network).")
    parser.add_argument("--upload", action="store_true",
                        help="Upload the golden dataset to LangSmith (needs LANGSMITH_API_KEY).")
    parser.add_argument("--dataset-name", default=DATASET_NAME)
    args = parser.parse_args()

    if not args.check and not args.upload:
        args.check = True  # default action

    rc = 0
    if args.check:
        summary = run_deterministic(verbose=True)
        rc = 0 if summary["failed"] == 0 else 1
    if args.upload:
        upload(dataset_name=args.dataset_name)
    return rc


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
