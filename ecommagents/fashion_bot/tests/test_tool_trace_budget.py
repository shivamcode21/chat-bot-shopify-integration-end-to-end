"""The per-tool tool-trace budget stays scoped to get_product_reviews.

The trace is the only record of a tool call that survives into the next turn,
and it is truncated so it cannot grow without bound. get_product_reviews is the
one tool whose FULL output the next turn needs -- it returns up to 15 reviews
for the model to show five at a time, so a "show me more" turn has to reach
reviews 6-10, which sit well past the default cut. At the default the model was
handed one reviewer and a half-sentence, had nothing to show, and invented three
customers.

These tests pin both halves of that: the review payload survives whole, and
NOTHING ELSE does. Raising the default, or adding tools to the map without
thinking, puts a large payload into every turn's context for every agent -- the
cost the default exists to avoid.
"""
from __future__ import annotations

from fashion_bot.nodes import generic_skill_node as gsn


class _Action:
    """Stand-in for a LangChain AgentAction -- the trace reads .tool/.tool_input."""

    def __init__(self, tool: str):
        self.tool = tool
        self.tool_input = {}


def _trace_for(tool: str, observation) -> str:
    msg = gsn._build_tool_trace_system_message([(_Action(tool), observation)])
    assert msg is not None
    return msg.content


_OVERSIZED = {"success": True, "data": "x" * 9_000}


def test_only_get_product_reviews_has_a_raised_budget():
    """The map is the whole allowlist -- one entry, deliberately."""
    assert set(gsn._TOOL_TRACE_OUTPUT_CHARS_BY_TOOL) == {"get_product_reviews"}


def test_other_tools_still_truncate_at_the_default():
    """A tool absent from the map is unaffected by the review exception."""
    for tool in (
        "search_products",
        "find_product_by_id",
        "find_product_by_url",
        "get_cart",
        "add_to_cart",
        "get_contact_information",
        "get_available_categories",
    ):
        content = _trace_for(tool, _OVERSIZED)
        # Header, tool name and input share the line, so allow modest overhead
        # -- the point is that it is bounded near the default, not near 6000.
        assert len(content) < gsn._TOOL_TRACE_OUTPUT_CHARS + 200, (
            f"{tool} exceeded the default budget: {len(content)} chars"
        )
        assert content.rstrip().endswith("..."), f"{tool} was not truncated"


def test_review_payload_survives_whole():
    """Every reviewer reaches the next turn -- the 15th as surely as the 1st.

    Uses bodies longer than the real ones (Concept Groove's run to a few words)
    so the assertion holds for a wordier store, not just this catalogue.
    """
    reviews = [
        {
            "rating": 5,
            "body": (
                "The fit is exactly what I hoped for and the fabric feels premium, "
                "washed it twice and the print has not faded at all."
            ),
            "created_at": f"2026-0{i % 9 + 1}-1{i % 9}T00:00:00+00:00",
            "reviewer_name": f"Reviewer Number {i}",
            "verified": True,
        }
        for i in range(15)
    ]
    content = _trace_for(
        "get_product_reviews",
        {
            "success": True,
            "reviews": reviews,
            "matched_count": 40,
            "total_published": 40,
            "sort_used": "recent",
            "older_reviews_unfetched": False,
        },
    )

    for review in reviews:
        assert review["reviewer_name"] in content, (
            f"{review['reviewer_name']} was cut off -- a 'show me more' turn "
            "reaching this review has nothing real to show"
        )
    # The last review is the one a third batch needs, and the first casualty of
    # a budget set too low.
    assert reviews[-1]["body"][:40] in content


def test_the_budget_leaves_headroom_over_a_real_payload():
    """Guards the number itself.

    A budget only a little above the real payload silently starts truncating
    the moment a store writes longer reviews, and the failure looks like the
    model inventing customers rather than like a limit being hit.
    """
    budget = gsn._TOOL_TRACE_OUTPUT_CHARS_BY_TOOL["get_product_reviews"]
    # Measured against the live Concept Groove listing: 14 reviews, 3904 chars.
    assert budget >= 5_000
    # Not open-ended either -- this rides in context on every following turn.
    assert budget <= 10_000


# ── one elevated payload per turn, however many calls ─────────────────────
# A turn can call one tool repeatedly: the model retrying, or following a
# sentiment filter that matched nothing with an unfiltered call. Granting each
# call its own elevated budget multiplies what rides into the next turn by
# however many times the model chose to call, which is not a number this code
# controls. Only the LAST call of an elevated tool keeps the raised budget.


def _reviews(tag: str, count: int = 15) -> dict:
    return {
        "success": True,
        "reviews": [
            {
                "rating": 5,
                "body": "The fit is exactly what I hoped for and the fabric feels premium.",
                "created_at": "2026-01-01T00:00:00+00:00",
                "reviewer_name": f"{tag}_Reviewer_{i}",
                "verified": True,
            }
            for i in range(count)
        ],
        "matched_count": 40,
        "total_published": 40,
        "sort_used": "recent",
    }


def _trace_for_steps(steps) -> str:
    msg = gsn._build_tool_trace_system_message(steps)
    assert msg is not None
    return msg.content


def test_repeated_review_calls_do_not_multiply_the_budget():
    """The bound holds by construction, not because turns happen to call once.

    Without this, the worst case is the elevated budget times however many
    calls the model made -- the failure mode is a quietly enormous prompt on
    every following turn, which shows up as cost and as useful context falling
    out of the window rather than as anything obviously broken.
    """
    one = len(_trace_for_steps([(_Action("get_product_reviews"), _reviews("A"))]))
    six = len(
        _trace_for_steps(
            [(_Action("get_product_reviews"), _reviews(f"P{i}")) for i in range(6)]
        )
    )
    budget = gsn._TOOL_TRACE_OUTPUT_CHARS_BY_TOOL["get_product_reviews"]

    # Five extra calls may add five TRUNCATED stubs, never five more full
    # payloads. Each stub costs its 220 characters plus its own line overhead
    # -- the tool name, `input=`, and the carried sort_used field -- so allow
    # per-line slack rather than asserting a bound the format cannot meet.
    _STUB_LINE_OVERHEAD = 200
    assert six < one + 5 * (gsn._TOOL_TRACE_OUTPUT_CHARS + _STUB_LINE_OVERHEAD)

    # The property that actually matters: six calls are nowhere near six times
    # one call. Without the cap this would be ~6x.
    assert six < 2 * one
    assert six < budget + 2_000


def test_the_last_review_call_is_the_one_kept_whole():
    """A follow-up continues from the MOST RECENT list, so that is the payload
    worth carrying; an earlier call in the same turn has been superseded."""
    content = _trace_for_steps(
        [
            (_Action("get_product_reviews"), _reviews("FIRST")),
            (_Action("get_product_reviews"), _reviews("LAST")),
        ]
    )

    assert all(f"LAST_Reviewer_{i}" in content for i in range(15))
    # The superseded call is present as a truncated stub, not carried whole.
    assert sum(f"FIRST_Reviewer_{i}" in content for i in range(15)) < 15


def test_a_single_review_call_is_unaffected_by_the_cap():
    """The normal turn -- and the one the fabrication fix exists for. Every
    reviewer must still reach the next turn, cap or no cap."""
    content = _trace_for_steps([(_Action("get_product_reviews"), _reviews("ONLY"))])
    assert all(f"ONLY_Reviewer_{i}" in content for i in range(15))


def test_a_review_call_alongside_other_tools_keeps_its_full_budget():
    """The mixed turn seen live: a product search followed by a review fetch.
    The search must not consume the review call's allowance."""
    content = _trace_for_steps(
        [
            (_Action("search_products"), {"products": ["x"] * 99}),
            (_Action("get_product_reviews"), _reviews("MIXED")),
        ]
    )
    assert all(f"MIXED_Reviewer_{i}" in content for i in range(15))


def test_trace_never_carries_more_calls_than_the_cap():
    """_TOOL_TRACE_MAX_CALLS is the other half of the bound."""
    steps = [(_Action(f"tool_{i}"), {"n": i}) for i in range(20)]
    content = _trace_for_steps(steps)
    assert f"tool_{gsn._TOOL_TRACE_MAX_CALLS - 1}" in content
    assert f"tool_{gsn._TOOL_TRACE_MAX_CALLS}" not in content
