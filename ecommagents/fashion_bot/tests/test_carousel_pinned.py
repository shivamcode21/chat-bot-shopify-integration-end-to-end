"""Unit tests for pinned-promo force-inclusion in the web-chat carousel.

A pinned promo (configured via ``pinned_bestseller_products``, flagged
``pinned=True`` by the search / top-selling tools) carries no inventory, so it
normalizes as out-of-stock and the LLM tends to omit it from
``###SHOW_PRODUCTS###``. The carousel must therefore FORCE-include any pinned
candidate (pins-first), regardless of the LLM's selection — the exact prod gap
where Groovee's configured pin (Evolve: The Cosmic Shacket) was in the
get_top_selling tool output but never rendered.

``fashion_bot.websocket_chat`` pulls heavy deps, so guard with importorskip.
"""

import pytest


def _p(handle, pinned=False, title=None):
    d = {"handle": handle, "title": title or handle, "name": title or handle}
    if pinned:
        d["pinned"] = True
    return d


def _matcher():
    pytest.importorskip("pydantic")
    pytest.importorskip("fastapi")
    from fashion_bot.websocket_chat import _match_carousel_products_to_reply
    return _match_carousel_products_to_reply


def _handles(products):
    return [p.get("handle") for p in products]


def test_pin_forced_in_when_llm_omits_its_handle():
    # The prod scenario: pin is a candidate but the LLM only emitted the live
    # top-sellers' handles. The pin must still render, first.
    match = _matcher()
    carousel = [_p("cosmic", pinned=True), _p("a"), _p("b")]
    out = match({}, carousel, "reply", show_product_handles=["a", "b"])
    assert _handles(out) == ["cosmic", "a", "b"]
    assert out[0].get("pinned") is True


def test_pin_wins_shared_handle_slot():
    match = _matcher()
    carousel = [_p("dup", pinned=True), _p("dup"), _p("b")]
    out = match({}, carousel, "r", show_product_handles=["dup", "b"])
    assert _handles(out) == ["dup", "b"]
    assert out[0].get("pinned") is True  # pinned object, deduped against the live dup


def test_no_pins_behaviour_unchanged():
    match = _matcher()
    carousel = [_p("a"), _p("b")]
    out = match({}, carousel, "r", show_product_handles=["a"])
    assert _handles(out) == ["a"]


def test_pin_shows_even_without_show_products_block():
    match = _matcher()
    out = match({}, [_p("cosmic", pinned=True)], "r", show_product_handles=None)
    assert _handles(out) == ["cosmic"]


def test_no_block_and_no_pin_suppressed():
    match = _matcher()
    out = match({}, [_p("a"), _p("b")], "r", show_product_handles=None)
    assert out == []


def test_pin_sourced_from_recent_products_when_candidates_shadowed():
    # carousel candidates came from product_selection_matches (no pin), but the
    # pin lives in recent_products → still force-included.
    match = _matcher()
    state = {"recent_products": [_p("cosmic", pinned=True)]}
    out = match(state, [_p("a")], "r", show_product_handles=["a"])
    assert _handles(out) == ["cosmic", "a"]


def test_pins_are_additive_never_displace_live():
    # max_results live products are kept AND the pin is added on top (additive),
    # so forcing a pin in never drops a real top-seller from the cards.
    match = _matcher()
    carousel = [_p("pin1", pinned=True)] + [_p(f"live{i}") for i in range(5)]
    out = match(
        {}, carousel, "r",
        show_product_handles=[f"live{i}" for i in range(5)], max_results=5,
    )
    assert _handles(out) == ["pin1", "live0", "live1", "live2", "live3", "live4"]
    assert len(out) == 6  # 5 live + 1 pin


def test_pin_already_in_matched_not_double_counted():
    # When the LLM DID emit the pin's handle, it's deduped — total stays at the
    # live cap, pin in slot #1.
    match = _matcher()
    carousel = [_p("pin1", pinned=True)] + [_p(f"live{i}") for i in range(4)]
    out = match(
        {}, carousel, "r",
        show_product_handles=["pin1", "live0", "live1", "live2", "live3"], max_results=5,
    )
    assert _handles(out) == ["pin1", "live0", "live1", "live2", "live3"]
    assert len(out) == 5
