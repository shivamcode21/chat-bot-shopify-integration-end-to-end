"""Only tool-sourced URLs may reach the customer from the order_status agent.

Regression cover for the Enamor reply of 2026-08-24 (web chat, phone …4009,
LangSmith trace 01a032a4-2cce-7f72-b249-8e8db82b422a, scored 2/5 and tagged
AGENT_TECHNICAL_ERROR). The customer wrote "Please deliver this order"; the
agent's only tool call that turn was ``annotate_order``, so it held no order
data. It applied the prompt's "Out for Delivery" script from the previous turn's
narrative and filled the tracking slot with an invented waybill:
``https://shiprocket.co/tracking/1234567890``.

The helpers under test are pure functions (input -> output, no state mutation,
per AGENTS.md §2), so these tests need no live services.
"""

from fashion_bot.utils.utils import collect_tool_result_urls, strip_unsourced_urls


REAL_URL = "https://shiprocket.co/tracking/34791189074634"
OTHER_REAL_URL = "https://shiprocket.co/tracking/77138643621"
FAKE_URL = "https://shiprocket.co/tracking/1234567890"

# The reply that actually reached the customer.
INCIDENT_REPLY = (
    "Good news!!! You will get your order today. The delivery person will "
    "contact you soon. You can track the order here: " + FAKE_URL
)


def _step(tool_name, observation):
    """Build one (action, observation) intermediate step."""
    return ({"tool": tool_name}, observation)


class TestCollectToolResultUrls:
    def test_nothing_from_no_steps(self):
        assert collect_tool_result_urls([]) == []
        assert collect_tool_result_urls(None) == []

    def test_get_order_details_shape(self):
        steps = [_step("get_order_details", {"tracking": {"tracking_url": REAL_URL}})]
        assert collect_tool_result_urls(steps) == [REAL_URL]

    def test_get_recent_orders_shape(self):
        steps = [_step("get_recent_orders", {"orders": [{"tracking_url": REAL_URL}]})]
        assert collect_tool_result_urls(steps) == [REAL_URL]

    def test_split_shipment_parcels_all_count(self):
        # Walking by value rather than by named key means a split order's second
        # waybill counts without a per-tool shape list to maintain.
        steps = [_step("get_order_details", {"tracking": {"parcels": [
            {"tracking_url": REAL_URL}, {"tracking_url": OTHER_REAL_URL},
        ]}})]
        assert collect_tool_result_urls(steps) == [REAL_URL, OTHER_REAL_URL]

    def test_url_embedded_in_a_text_field_counts(self):
        # Some tools return prose, not a tidy tracking_url key.
        steps = [_step("get_delivery_partner_information",
                       {"message": f"Track it at {REAL_URL} for updates."})]
        assert collect_tool_result_urls(steps) == [REAL_URL]

    def test_string_observation_counts(self):
        steps = [_step("some_tool", f"see {REAL_URL}")]
        assert collect_tool_result_urls(steps) == [REAL_URL]

    def test_annotate_order_result_yields_nothing(self):
        # The incident turn, exactly: a successful tool call carrying no URL.
        steps = [_step("annotate_order", {"success": True, "note_result": {"order_id": "ENAMOR-234676"}})]
        assert collect_tool_result_urls(steps) == []

    def test_malformed_steps_are_skipped(self):
        assert collect_tool_result_urls([(), ({"tool": "x"},)]) == []


class TestStripUnsourcedUrls:
    def test_the_incident_link_is_removed(self):
        cleaned = strip_unsourced_urls(INCIDENT_REPLY, [])
        assert FAKE_URL not in cleaned
        assert "1234567890" not in cleaned
        assert cleaned.startswith("Good news!!! You will get your order today.")

    def test_a_tool_sourced_link_survives_untouched(self):
        text = f"Your order is on its way. Track it here: {REAL_URL}"
        assert strip_unsourced_urls(text, [REAL_URL]) == text

    def test_trailing_punctuation_still_matches_the_tool_url(self):
        text = f"Track it here: {REAL_URL}."
        assert strip_unsourced_urls(text, [REAL_URL]) == text

    def test_trailing_slash_and_case_still_match(self):
        text = f"Track it here: {REAL_URL}/"
        assert strip_unsourced_urls(text, [REAL_URL]) == text

    def test_prompt_authored_url_survives(self):
        # Rare Rabbit's order_status prompt tells the agent to share its returns
        # portal; Concept Groove's shares an Instagram link. Neither comes from a
        # tool, and stripping them would be a live regression.
        portal = "https://returns.thehouseofrare.com/"
        text = f"You can raise a return here: {portal}"
        assert strip_unsourced_urls(text, [portal]) == text

    def test_punctuation_is_kept_when_the_url_goes(self):
        cleaned = strip_unsourced_urls(f"Track here: {FAKE_URL}. Thanks!", [])
        assert FAKE_URL not in cleaned
        assert cleaned.endswith("Thanks!")

    def test_every_invented_url_is_removed(self):
        text = f"Parcel 1: {FAKE_URL}. Parcel 2: https://shiprocket.co/tracking/9999999999."
        cleaned = strip_unsourced_urls(text, [REAL_URL])
        assert "shiprocket.co" not in cleaned

    def test_it_never_substitutes_a_replacement_link(self):
        # Choosing one would be guessing which parcel the sentence meant.
        cleaned = strip_unsourced_urls(INCIDENT_REPLY, [REAL_URL])
        assert FAKE_URL not in cleaned
        assert REAL_URL not in cleaned

    def test_reply_without_urls_is_returned_byte_identical(self):
        text = "Your order is being packed.  Two   spaces stay as typed."
        assert strip_unsourced_urls(text, []) == text

    def test_mixed_reply_keeps_only_the_sourced_url(self):
        text = f"Track: {FAKE_URL} and shop: {REAL_URL}"
        cleaned = strip_unsourced_urls(text, [REAL_URL])
        assert FAKE_URL not in cleaned
        assert REAL_URL in cleaned

    def test_non_string_input_passes_through(self):
        assert strip_unsourced_urls(None, []) is None
        assert strip_unsourced_urls("", []) == ""
