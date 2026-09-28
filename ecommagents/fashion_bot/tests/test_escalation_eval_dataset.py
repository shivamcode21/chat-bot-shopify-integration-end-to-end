"""
CI guard for the resolution-first escalation eval set
(fashion_bot/evals/escalation_resolution_first.py).

Runs the deterministic layer (the code-enforced dimension — actionability and
the gate soft-block it drives) against the golden labels, and asserts basic
coverage so the dataset can't silently rot.
"""

from fashion_bot.evals import escalation_resolution_first as E


def test_deterministic_layer_is_green():
    summary = E.run_deterministic(verbose=False)
    assert summary["failed"] == 0, f"golden labels disagree with the code: {summary['failures']}"
    assert summary["total"] >= 18


def test_dataset_covers_resolve_and_escalate():
    splits = {ex["metadata"].get("split") for ex in E.GOLDEN_EXAMPLES}
    assert {"resolve", "escalate", "core"} <= splits
    should = [ex["outputs"]["should_escalate"] for ex in E.GOLDEN_EXAMPLES]
    assert any(should) and not all(should), "need both escalate and resolve cases"


def test_dataset_covers_all_actionabilities():
    acts = {ex["outputs"].get("expected_actionability") for ex in E.GOLDEN_EXAMPLES}
    assert {"actionable", "unfulfillable", "mandatory"} <= acts


def test_three_incidents_present():
    incidents = {ex["metadata"].get("incident") for ex in E.GOLDEN_EXAMPLES}
    assert {"A", "B", "C"} <= incidents


def test_example_ids_unique():
    ids = [ex["id"] for ex in E.GOLDEN_EXAMPLES]
    assert len(ids) == len(set(ids))


def test_evaluators_score_expected_directions():
    # over-escalation: escalated a should-not-escalate case
    ref = {"should_escalate": False}
    assert E.eval_should_escalate({"escalated": True}, ref)["score"] == 0
    assert E.eval_no_over_escalation({"escalated": True}, ref)["score"] == 0
    assert E.eval_no_missed_escalation({"escalated": True}, ref)["score"] == 1
    # correct resolve
    assert E.eval_should_escalate({"escalated": False}, ref)["score"] == 1
    # missed escalation: did not escalate a must-escalate case
    ref2 = {"should_escalate": True}
    assert E.eval_no_missed_escalation({"escalated": False}, ref2)["score"] == 0
