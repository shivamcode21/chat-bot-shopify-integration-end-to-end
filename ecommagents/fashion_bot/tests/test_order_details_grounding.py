"""
Unit tests for the stateless order line-item grounding check in tool_factory.

`_validate_line_item_variant_id` guards mutating order tools (variant/size
change, product change) against a line_item_variant_id the LLM inferred from
the product catalog / search results instead of one of the order's real line
items — the cause of "No line item with variant_id X in order Y" failures.

The helper is a pure function (input -> output, no state access or mutation,
per AGENTS.md §2), so these tests need no live services or state fixtures.
"""

from fashion_bot.tool_factory import _validate_line_item_variant_id


REAL_VARIANT_ID = "44781434388722"
CATALOG_VARIANT_ID = "99999999999999"  # hallucinated / from product search
LINE_ITEMS = [
    {"name": "Sunfire Denim - 32", "variant_id": REAL_VARIANT_ID, "title": "Sunfire Denim"},
]


class TestValidateLineItemVariantId:
    def test_no_variant_id_supplied_is_not_gated(self):
        # When no id is supplied the tool falls back to old_variant matching
        # against these same real line items, which is already grounded.
        assert _validate_line_item_variant_id(LINE_ITEMS, "", "G37792") is None
        assert _validate_line_item_variant_id(LINE_ITEMS, None, "G37792") is None

    def test_real_variant_id_passes(self):
        assert _validate_line_item_variant_id(LINE_ITEMS, REAL_VARIANT_ID, "G37792") is None

    def test_matches_int_vs_str(self):
        items = [{"name": "x", "variant_id": int(REAL_VARIANT_ID)}]
        assert _validate_line_item_variant_id(items, REAL_VARIANT_ID, "G1") is None

    def test_hallucinated_variant_id_rejected(self):
        err = _validate_line_item_variant_id(LINE_ITEMS, CATALOG_VARIANT_ID, "G37792")
        assert err is not None
        assert err["success"] is False
        assert err["error"] == "variant_id_not_in_order"
        assert err["phone_validated"] is True

    def test_rejection_lists_real_variant_ids(self):
        err = _validate_line_item_variant_id(LINE_ITEMS, CATALOG_VARIANT_ID, "G37792")
        assert err["valid_variant_ids"] == [REAL_VARIANT_ID]
        assert err["available_items"] == [
            {"name": "Sunfire Denim - 32", "variant_id": REAL_VARIANT_ID}
        ]

    def test_rejection_message_is_actionable(self):
        err = _validate_line_item_variant_id(LINE_ITEMS, CATALOG_VARIANT_ID, "G37792")
        msg = err["message"].lower()
        assert "get_order_details" in msg
        assert CATALOG_VARIANT_ID in err["message"]

    def test_empty_order_with_supplied_id_is_rejected(self):
        err = _validate_line_item_variant_id([], REAL_VARIANT_ID, "G37792")
        assert err is not None
        assert err["valid_variant_ids"] == []

    def test_available_items_falls_back_to_title(self):
        items = [{"title": "Only Title", "variant_id": REAL_VARIANT_ID}]
        err = _validate_line_item_variant_id(items, CATALOG_VARIANT_ID, "G1")
        assert err["available_items"][0]["name"] == "Only Title"
