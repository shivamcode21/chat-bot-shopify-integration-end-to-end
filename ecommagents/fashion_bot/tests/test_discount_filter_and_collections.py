"""discount_pct filter support + collections fetch / embedding-text cap.

Covers the two changes in this PR:

1. ``discount_pct`` is registered as a hard-filter field so the
   query-understanding LLM is allowed to emit ``discount_pct > N`` filters for
   sale / discount / EOSS intent (today it can only ``sort_by`` discount).
2. Collections are fetched with a higher page cap (``first: 55``) so the
   sale/EOSS collection — the newest one, which Shopify orders last — lands in
   the document's ``collections`` list, while the embedding text stays capped at
   ``_SEARCHABLE_TEXT_MAX_COLLECTIONS`` so widening the fetch does not dilute the
   vector (the list changes, the text does not).

Pure-logic tests; they pull pydantic-backed modules, so they guard with
``importorskip`` to degrade gracefully in a bare dev container (mirrors
``test_bestseller_and_newarrivals.py``).
"""

import pytest


def test_discount_pct_and_collections_are_filterable_fields():
    pytest.importorskip("pydantic")
    from fashion_bot.services.recommendation import query_understanding as qu

    names = {f["name"] for f in qu.DEFAULT_FILTERABLE_FIELDS}
    assert "discount_pct" in names
    assert "collections" in names
    # Both must surface in the schema text the QU prompt is rendered from,
    # otherwise the LLM (told to use ONLY listed fields) won't emit them.
    schema = qu._format_schema(qu.DEFAULT_FILTERABLE_FIELDS)
    assert "discount_pct" in schema
    assert "collections" in schema
    # collections must use the CONTAINS operator (it is an array field).
    coll = next(f for f in qu.DEFAULT_FILTERABLE_FIELDS if f["name"] == "collections")
    assert coll["operator"] == "CONTAINS"


def test_default_prompt_no_longer_forbids_collections_filter():
    pytest.importorskip("pydantic")
    from fashion_bot.services.recommendation import query_understanding as qu

    # The old rule forced collections into the query ("... neckline, collections)
    # MUST go into the query"). Now collections is a registered filter, so that
    # exclusion list must no longer contain it (or the prompt contradicts the
    # schema and the LLM won't filter on it).
    assert "neckline, collections) MUST go into" not in qu.DEFAULT_SYSTEM_PROMPT
    assert "collections CONTAINS" in qu.DEFAULT_SYSTEM_PROMPT


def _product(collections):
    from fashion_bot.services.product_ingestion import models as m
    return m.NormalizedProduct(
        id="gid://shopify/Product/1", title="Tee", handle="tee", product_type="T",
        vendor="Iconic", tags=[], colors=[], sizes=[], price_min=9.0, price_max=9.0,
        image_url="", product_url="", in_stock=True, description="",
        collections=collections,
    )


def test_full_collection_list_is_preserved_in_document():
    pytest.importorskip("pydantic")
    # 40 ordinary collections + the EOSS collection LAST (as Shopify orders it).
    cols = [f"Collection {i}" for i in range(40)] + ["End of Season Sale | UPTO 50% OFF"]
    content = _product(cols).to_search_document("c1")["content"]
    assert content["collections"] == cols
    assert "End of Season Sale | UPTO 50% OFF" in content["collections"]


def test_embedding_text_collections_are_capped():
    pytest.importorskip("pydantic")
    from fashion_bot.services.product_ingestion import models as m

    cols = [f"Collection {i}" for i in range(40)] + ["End of Season Sale | UPTO 50% OFF"]
    text = _product(cols)._build_searchable_text()
    coll_part = next(seg for seg in text.split(" | ") if seg.startswith("Collections:"))
    listed = coll_part[len("Collections: "):].split(", ")
    # Only the capped prefix is embedded — the list grew, the vector text did not.
    assert len(listed) == m._SEARCHABLE_TEXT_MAX_COLLECTIONS
    assert "End of Season Sale | UPTO 50% OFF" not in coll_part
