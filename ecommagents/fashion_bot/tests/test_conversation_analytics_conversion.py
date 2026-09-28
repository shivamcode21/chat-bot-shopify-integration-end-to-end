import pytest


conversation_analyzer = pytest.importorskip("fashion_bot.analytics.conversation_analyzer")


def _user_message(tags):
    return {
        "message_side": "user_to_system",
        "message": "test",
        "tags": tags,
    }


def test_post_order_support_chat_does_not_count_nearby_order_as_assisted():
    lead = conversation_analyzer._calculate_lead_metadata(
        [
            _user_message(["Order Status Query"]),
            _user_message(["Order Status Query"]),
            _user_message(["Order Status Query"]),
        ],
        {"lead_generation_tags": ["Product Query"], "preorder_lead_tags": []},
        converted=False,
    )

    status = conversation_analyzer._determine_order_conversion_status(
        {"conversion_assisted": False},
        ["gv15471"],
        lead,
    )

    assert status == {
        "conversion_assisted": False,
        "conversion_detected_via": None,
    }


def test_single_presales_tag_does_not_count_nearby_order_as_assisted():
    lead = conversation_analyzer._calculate_lead_metadata(
        [
            _user_message(["Product Query"]),
        ],
        {"lead_generation_tags": ["Product Query"], "preorder_lead_tags": []},
        converted=False,
    )

    status = conversation_analyzer._determine_order_conversion_status(
        {"conversion_assisted": False},
        ["gv15472"],
        lead,
    )

    assert status == {
        "conversion_assisted": False,
        "conversion_detected_via": None,
    }


def test_qualified_presales_chat_counts_nearby_order_as_data_driven_assisted():
    lead = conversation_analyzer._calculate_lead_metadata(
        [
            _user_message(["Product Query"]),
            _user_message([]),
            _user_message(["Size Inquiry"]),
            _user_message(["Pricing Query"]),
        ],
        {
            "lead_generation_tags": ["Product Query", "Size Inquiry", "Pricing Query"],
            "preorder_lead_tags": [],
        },
        converted=False,
    )

    status = conversation_analyzer._determine_order_conversion_status(
        {"conversion_assisted": False},
        ["gv15472"],
        lead,
    )

    assert status == {
        "conversion_assisted": True,
        "conversion_detected_via": "data_driven",
    }


def test_single_presales_signal_with_enough_messages_counts_as_assisted():
    lead = conversation_analyzer._calculate_lead_metadata(
        [
            _user_message(["Product Query"]),
            _user_message([]),
            _user_message([]),
            _user_message(["Order Status Query"]),
        ],
        {"lead_generation_tags": ["Product Query"], "preorder_lead_tags": []},
        converted=False,
    )

    status = conversation_analyzer._determine_order_conversion_status(
        {"conversion_assisted": False},
        ["gv15473"],
        lead,
    )

    assert status == {
        "conversion_assisted": True,
        "conversion_detected_via": "data_driven",
    }


def test_mixed_qualified_presales_and_post_order_tags_count_as_assisted():
    lead = conversation_analyzer._calculate_lead_metadata(
        [
            _user_message(["Product Query"]),
            _user_message(["Size Inquiry"]),
            _user_message(["Pricing Query"]),
            _user_message(["Order Status Query"]),
            _user_message(["Order Status Query"]),
            _user_message(["Order Status Query"]),
        ],
        {
            "lead_generation_tags": ["Product Query", "Size Inquiry", "Pricing Query"],
            "preorder_lead_tags": [],
        },
        converted=False,
    )

    status = conversation_analyzer._determine_order_conversion_status(
        {"conversion_assisted": False},
        ["gv15473"],
        lead,
    )

    assert status == {
        "conversion_assisted": True,
        "conversion_detected_via": "data_driven",
    }


def test_llm_inferred_checkout_help_still_counts_without_post_chat_order():
    status = conversation_analyzer._determine_order_conversion_status(
        {"conversion_assisted": True},
        [],
        {"is_lead": False},
    )

    assert status == {
        "conversion_assisted": True,
        "conversion_detected_via": "llm_inferred",
    }


@pytest.mark.asyncio
async def test_filter_unlinked_conversion_orders_removes_orders_linked_to_prior_conversation(monkeypatch):
    class _Cursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, *_args, **_kwargs):
            return None

        async def fetchall(self):
            return [
                {
                    "order_id": "gv15471",
                    "conversation_id": "prior-conversation",
                }
            ]

    class _Connection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def cursor(self):
            return _Cursor()

    class _ConnectionManager:
        async def __aenter__(self):
            return _Connection()

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(
        conversation_analyzer,
        "get_async_postgres_connection",
        lambda: _ConnectionManager(),
    )

    filtered = await conversation_analyzer._filter_unlinked_conversion_orders(
        client_id="00000000-0000-0000-0000-000000000001",
        conversation_id="later-conversation",
        post_conversation_orders=[
            {"order_id": "gv15471"},
            {"order_id": "gv15472"},
        ],
    )

    assert filtered == [{"order_id": "gv15472"}]
