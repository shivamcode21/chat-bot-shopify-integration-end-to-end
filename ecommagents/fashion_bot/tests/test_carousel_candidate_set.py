"""Regression tests for the web-chat carousel candidate set.

Two prod failure modes, same root cause — the carousel resolver only covered
``recent_products`` (this turn's tool output) while the LLM is allowed to emit
handles from THIS turn's tools OR the conversation's product entities:

  A) "show bestsellers" answered from context, no tool ran → recent_products empty
     → only a single focal product resolved → 1 card instead of N.
  B) a repeated bestseller search returned ONE product (Raging Bull Tank), but the
     model re-listed 3 products from entities → candidate set was just
     ['ranging-bull-tank'] → "matched no candidate — showing nothing" → 0 cards.

Fix: ``_products_for_webchat_carousel`` returns the UNION of recent_products +
product_selection_matches + conversation entities (+ focal), deduped by handle, so
every handle the model may legitimately emit resolves. ``_match_by_handles`` still
surfaces only LLM-emitted handles (+ pins), so a wider pool adds no unwanted cards.

``fashion_bot.websocket_chat`` pulls heavy deps, so guard with importorskip.
"""

import pytest


def _funcs():
    pytest.importorskip("pydantic")
    pytest.importorskip("fastapi")
    from fashion_bot.websocket_chat import (
        _products_for_webchat_carousel,
        _match_carousel_products_to_reply,
    )
    return _products_for_webchat_carousel, _match_carousel_products_to_reply


def _ent(handle, title=None):
    return {"entity_type": "product", "full_data": {"handle": handle, "title": title or handle}}


def _handles(products):
    return [(p.get("handle") or "").lower() for p in products]


# ---- Mode B: this turn's tool returned 1 product, LLM listed 3 from entities ----

def test_prod_repro_entity_handles_resolve_when_tool_returned_one():
    build, match = _funcs()
    state = {
        # this turn's repeated bestseller search yielded a single product
        "recent_products": [{"handle": "ranging-bull-tank", "title": "Raging Bull Tank"}],
        "conversation_context": {"entities": [
            _ent("silent-reckoning-long-sleeve"),
            _ent("the-viofuel-varsity"),
            _ent("rebel-club-jersey"),
        ]},
    }
    emitted = ["silent-reckoning-long-sleeve", "the-viofuel-varsity", "rebel-club-jersey"]
    carousel = build(state)
    # union must cover the tool product AND all three entity handles
    assert set(_handles(carousel)) == {"ranging-bull-tank", *emitted}
    out = match(state, carousel, "reply", show_product_handles=emitted)
    assert _handles(out) == emitted  # 3 cards, in the LLM's order (was 0 in prod)


# ---- Mode A: context-only discovery turn, no tool ran ----

def test_context_only_turn_resolves_all_entities():
    build, match = _funcs()
    five = ["no-care-hoodie", "the-viofuel-varsity", "the-bloomstate-hoodie",
            "rhythm-of-fear-denim", "value-protocol-long-sleeve"]
    state = {
        "recent_products": [],
        "inquiry_product_info": {"handle": "no-care-hoodie", "title": "No Care Hoodie"},
        "conversation_context": {"entities": [_ent(h) for h in five]},
    }
    out = match(state, build(state), "reply", show_product_handles=five)
    assert _handles(out) == five


# ---- Dedupe & fallbacks ----

def test_dedupe_by_handle_keeps_one_per_handle():
    build, _ = _funcs()
    state = {
        "recent_products": [{"handle": "dup", "title": "fresh"}],
        "conversation_context": {"entities": [_ent("dup"), _ent("other")]},
    }
    got = _handles(build(state))
    assert got.count("dup") == 1
    assert set(got) == {"dup", "other"}


def test_single_focal_fallback_when_only_ipi():
    build, _ = _funcs()
    state = {
        "recent_products": [],
        "inquiry_product_info": {"handle": "no-care-hoodie", "title": "No Care Hoodie"},
        "conversation_context": {"entities": []},
    }
    assert _handles(build(state)) == ["no-care-hoodie"]


def test_recent_products_included_in_union():
    build, _ = _funcs()
    state = {
        "recent_products": [{"handle": "fresh-a"}, {"handle": "fresh-b"}],
        "conversation_context": {"entities": [_ent("entity-x")]},
    }
    got = set(_handles(build(state)))
    assert {"fresh-a", "fresh-b", "entity-x"} == got


# ---- Top-up: pad an under-filled carousel with this turn's remaining results ----
# Prod RCA: search returned 5 gym tops, the answer model emitted only 2
# ###SHOW_PRODUCTS### handles, so only 2 cards rendered. When the model was
# selecting from this turn's fresh results, top the carousel up from the leftover
# recent_products (reranker order) so a terse selection doesn't shrink a good set.

def _p(handle, pinned=False, title=None):
    d = {"handle": handle, "title": title or handle}
    if pinned:
        d["pinned"] = True
    return d


def test_topup_pads_from_recent_when_llm_underemits():
    build, match = _funcs()
    five = [_p(f"gym-top-{i}") for i in range(5)]
    state = {"recent_products": five}
    carousel = build(state)
    # model emitted only 2 of the 5 fresh results
    out = match(state, carousel, "reply", show_product_handles=["gym-top-0", "gym-top-1"])
    # LLM's picks stay first (order preserved), the other 3 fresh results append
    assert _handles(out)[:2] == ["gym-top-0", "gym-top-1"]
    assert set(_handles(out)) == {f"gym-top-{i}" for i in range(5)}
    assert len(out) == 5


def test_topup_preserves_llm_order_and_respects_max_results():
    build, match = _funcs()
    state = {"recent_products": [_p(f"g{i}") for i in range(10)]}
    out = match(state, build(state), "r", show_product_handles=["g0"], max_results=3)
    assert _handles(out)[0] == "g0"          # LLM pick first
    assert len(out) == 3                       # capped at max_results


def test_topup_skipped_when_picks_are_entities_not_this_turn_results():
    # Guard: model listed entity handles the tool did NOT return this turn; the
    # single unrelated recent product must NOT be injected (was the prod repro).
    build, match = _funcs()
    state = {
        "recent_products": [_p("ranging-bull-tank")],
        "conversation_context": {"entities": [_ent("silent-reckoning-long-sleeve"),
                                              _ent("the-viofuel-varsity")]},
    }
    emitted = ["silent-reckoning-long-sleeve", "the-viofuel-varsity"]
    out = match(state, build(state), "r", show_product_handles=emitted)
    assert _handles(out) == emitted           # exactly the 2 emitted, no top-up


def test_topup_noop_when_no_recent_products():
    build, match = _funcs()
    state = {"conversation_context": {"entities": [_ent("a"), _ent("b")]}}
    out = match(state, build(state), "r", show_product_handles=["a"])
    assert _handles(out) == ["a"]             # nothing to pad from


def test_topup_dedupes_pin_already_shown():
    # A pinned recent product the LLM also emitted isn't double-added by top-up.
    build, match = _funcs()
    state = {"recent_products": [_p("a"), _p("b"), _p("c")]}
    out = match(state, build(state), "r", show_product_handles=["a", "b"])
    assert _handles(out) == ["a", "b", "c"]
    assert len(out) == len(set(_handles(out)))


# ---- Upstash handle-resolve fallback: row mapping + internal flag propagation ----

def _card_funcs():
    pytest.importorskip("pydantic")
    pytest.importorskip("fastapi")
    from fashion_bot.websocket_chat import (
        format_product_for_carousel,
        _upstash_row_to_card_product,
    )
    return format_product_for_carousel, _upstash_row_to_card_product


def test_upstash_row_maps_to_card_product():
    fmt, mp = _card_funcs()
    row = {
        "content": {"title": "Rhythm of Fear Denim", "handle": "rhythm-of-fear-denim", "price_min": 2499},
        "metadata": {"handle": "rhythm-of-fear-denim", "image_url": "https://cdn/x.jpg",
                     "product_url": "https://groovee.in/products/rhythm-of-fear-denim",
                     "variants": [{"id": "v1", "available": True, "option1": "30"}]},
    }
    prod = mp(row)
    assert prod["handle"] == "rhythm-of-fear-denim"
    assert prod["title"] == "Rhythm of Fear Denim"
    assert prod["image_url"].startswith("http")
    # maps into a renderable card
    assert fmt(prod) is not None


def test_searchdb_provenance_propagates_to_card_json():
    fmt, mp = _card_funcs()
    prod = mp({"content": {"title": "X", "handle": "x-handle", "price_min": 100},
               "metadata": {"handle": "x-handle", "image_url": "https://cdn/y.jpg"}})
    prod["handle_resolve"] = "searchdb"  # tagged by the Upstash fallback
    card = fmt(prod)
    assert card.get("handle_resolve") == "searchdb"  # provenance reaches the UI payload


def test_normal_card_marked_internal():
    fmt, _ = _card_funcs()
    card = fmt({"handle": "x", "title": "X", "image_url": "https://cdn/y.jpg", "price_min": 100})
    assert card.get("handle_resolve") == "internal"
