"""Bracketed IN/NOT IN list normalization in ``_validate_filter``.

Reproduces the production incident where the query-understanding LLM emitted an
Upstash filter with a JSON/Python-style bracket list::

    category IN ['Bras', 'Shapewear'] AND in_stock = true AND variant_availability_pct > 10

Upstash's filter DSL expects parentheses, so the raw ``[`` triggered
``Invalid filter. At line 1:12 token recognition error at: '['`` and the search
returned nothing (the pipeline silently fell back to ``in_stock = true``,
dropping the intended category scope).

``_validate_filter`` now rewrites the brackets to Upstash's ``IN (...)`` syntax
before the search is issued. Pure-logic tests; guard on pydantic like the sibling
recommendation tests so a bare dev container degrades gracefully.
"""

import pytest


def test_bracket_in_list_is_rewritten_to_parens():
    pytest.importorskip("pydantic")
    from fashion_bot.services.recommendation import query_understanding as qu

    # The exact filter string from the production trace (client 37c47134).
    raw = "category IN ['Bras', 'Shapewear'] AND in_stock = true AND variant_availability_pct > 10"
    fixed = qu._validate_filter(raw, qu.DEFAULT_FILTERABLE_FIELDS)

    # No bracket survives to reach Upstash's tokenizer.
    assert "[" not in fixed and "]" not in fixed
    # Members are preserved (single-quoted) inside parentheses.
    assert "category IN ('Bras', 'Shapewear')" in fixed
    # Sibling clauses are untouched / still valid.
    assert "in_stock = true" in fixed
    assert "variant_availability_pct > 10" in fixed


def test_not_in_bracket_list_is_rewritten():
    pytest.importorskip("pydantic")
    from fashion_bot.services.recommendation import query_understanding as qu

    fixed = qu._validate_filter("segment NOT IN ['men', 'unisex']", qu.DEFAULT_FILTERABLE_FIELDS)
    assert "segment NOT IN ('men', 'unisex')" in fixed
    assert "[" not in fixed and "]" not in fixed


def test_unquoted_bracket_members_get_quoted():
    pytest.importorskip("pydantic")
    from fashion_bot.services.recommendation import query_understanding as qu

    fixed = qu._validate_filter("category IN [Bras, Shapewear]", qu.DEFAULT_FILTERABLE_FIELDS)
    assert "category IN ('Bras', 'Shapewear')" in fixed


def test_already_parenthesized_in_list_is_unchanged():
    pytest.importorskip("pydantic")
    from fashion_bot.services.recommendation import query_understanding as qu

    # A correctly-formed filter must pass through without corruption.
    raw = "category IN ('Bras', 'Shapewear') AND in_stock = true"
    fixed = qu._validate_filter(raw, qu.DEFAULT_FILTERABLE_FIELDS)
    assert "category IN ('Bras', 'Shapewear')" in fixed
    assert "in_stock = true" in fixed


def test_plain_equality_filter_still_validates():
    pytest.importorskip("pydantic")
    from fashion_bot.services.recommendation import query_understanding as qu

    # Existing single-value behaviour is unaffected: bare string values are quoted
    # and in_stock is guaranteed.
    fixed = qu._validate_filter("category = Bras", qu.DEFAULT_FILTERABLE_FIELDS)
    assert "category = 'Bras'" in fixed
    assert "in_stock = true" in fixed
