"""Tests for inbound WhatsApp image analysis (OCR + product vision).

Covers:
- The analysis utility (``aanalyze_inbound_image``) in isolation
- The backward-compatible ``aextract_text_from_inbound_image`` wrapper
- ``InboundImageAnalysis`` dataclass construction
- JSON response parsing with various LLM output shapes
- Feature flag gating
- Timeout and error fail-open behaviour
- Priority: OCR text > product vision
- The webhook integration helpers (``_extract_media_url_from_payload``,
  ``_extract_caption_from_payload``)
- The shared webhook analysis step (``_aanalyze_inbound_image_message``)
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

inbound_ocr = pytest.importorskip("fashion_bot.utils.inbound_image_ocr")


# ---------------------------------------------------------------------------
# Test payloads
# ---------------------------------------------------------------------------

IMAGE_PAYLOAD = {
    "app": "TestApp",
    "payload": {
        "id": "msg-1",
        "type": "image",
        "source": "919999999999",
        "sender": {"phone": "919999999999", "name": "Tester"},
        "payload": {
            "url": "https://filemanager.gupshup.io/fm/wamedia/testapp/abc123",
            "contentType": "image/jpeg",
            "caption": "my order screenshot",
            "urlExpiry": 1580832279000,
        },
    },
}

IMAGE_PAYLOAD_NO_CAPTION = {
    "app": "TestApp",
    "payload": {
        "id": "msg-2",
        "type": "image",
        "source": "919999999999",
        "sender": {"phone": "919999999999", "name": "Tester"},
        "payload": {
            "url": "https://filemanager.gupshup.io/fm/wamedia/testapp/def456",
            "contentType": "image/jpeg",
        },
    },
}


# ---------------------------------------------------------------------------
# _parse_response
# ---------------------------------------------------------------------------

class TestParseResponse:
    def test_valid_json(self):
        raw = '{"text": "Order #12345", "has_text": true}'
        result = inbound_ocr._parse_response(raw)
        assert result["text"] == "Order #12345"
        assert result["has_text"] is True

    def test_markdown_fenced_json(self):
        raw = '```json\n{"text": "Hello", "has_text": true}\n```'
        result = inbound_ocr._parse_response(raw)
        assert result["text"] == "Hello"
        assert result["has_text"] is True

    def test_single_line_fenced_json(self):
        raw = '```json {"text": "Test", "has_text": true} ```'
        result = inbound_ocr._parse_response(raw)
        assert result["text"] == "Test"

    def test_empty_string(self):
        assert inbound_ocr._parse_response("") == {}
        assert inbound_ocr._parse_response(None) == {}

    def test_no_text_response(self):
        raw = '{"text": "", "has_text": false}'
        result = inbound_ocr._parse_response(raw)
        assert result["has_text"] is False
        assert result["text"] == ""

    def test_garbage_returns_empty(self):
        assert inbound_ocr._parse_response("not json at all") == {}

    def test_product_response(self):
        raw = json.dumps({
            "has_text": False,
            "text": "",
            "is_product_image": True,
            "product_description": {
                "category": "topwear",
                "subcategory": "t-shirt",
                "color": "black",
                "segment": "men",
                "summary": "black oversized cotton t-shirt for men",
            },
        })
        result = inbound_ocr._parse_response(raw)
        assert result["is_product_image"] is True
        assert result["product_description"]["summary"] == "black oversized cotton t-shirt for men"


# ---------------------------------------------------------------------------
# _coerce_bool_flag
# ---------------------------------------------------------------------------

class TestCoerceBoolFlag:
    def test_none(self):
        assert inbound_ocr._coerce_bool_flag(None) is False

    def test_bool(self):
        assert inbound_ocr._coerce_bool_flag(True) is True
        assert inbound_ocr._coerce_bool_flag(False) is False

    def test_dict_with_enabled(self):
        assert inbound_ocr._coerce_bool_flag({"enabled": True}) is True
        assert inbound_ocr._coerce_bool_flag({"enabled": False}) is False

    def test_string(self):
        assert inbound_ocr._coerce_bool_flag("true") is True
        assert inbound_ocr._coerce_bool_flag("false") is False
        assert inbound_ocr._coerce_bool_flag("1") is True
        assert inbound_ocr._coerce_bool_flag("0") is False


# ---------------------------------------------------------------------------
# InboundImageAnalysis dataclass
# ---------------------------------------------------------------------------

class TestInboundImageAnalysis:
    def test_defaults(self):
        a = inbound_ocr.InboundImageAnalysis()
        assert a.ocr_text is None
        assert a.is_product_image is False
        assert a.product_summary is None
        assert a.product_attributes is None

    def test_ocr_only(self):
        a = inbound_ocr.InboundImageAnalysis(ocr_text="Order #123")
        assert a.ocr_text == "Order #123"
        assert a.is_product_image is False

    def test_product_only(self):
        a = inbound_ocr.InboundImageAnalysis(
            is_product_image=True,
            product_summary="black t-shirt",
            product_attributes={"category": "topwear", "color": "black"},
        )
        assert a.product_summary == "black t-shirt"
        assert a.product_attributes["category"] == "topwear"

    def test_both_ocr_and_product(self):
        a = inbound_ocr.InboundImageAnalysis(
            ocr_text="50% OFF",
            is_product_image=True,
            product_summary="red dress",
        )
        assert a.ocr_text == "50% OFF"
        assert a.is_product_image is True


# ---------------------------------------------------------------------------
# aanalyze_inbound_image
# ---------------------------------------------------------------------------

def _mock_llm_response(payload: dict) -> MagicMock:
    """Build a mock LLM response with the given JSON payload."""
    mock = MagicMock()
    mock.content = json.dumps(payload)
    return mock


def _patch_prompt_and_taxonomy():
    """Patch _aget_prompt and _abuild_taxonomy_addendum to avoid DB calls in tests."""
    return (
        patch.object(inbound_ocr, "_aget_prompt", new_callable=AsyncMock, return_value=inbound_ocr._DEFAULT_IMAGE_ANALYSIS_PROMPT),
        patch.object(inbound_ocr, "_abuild_taxonomy_addendum", new_callable=AsyncMock, return_value=""),
    )


class TestAanalyzeInboundImage:
    @pytest.mark.asyncio
    async def test_returns_none_when_flag_off(self):
        with patch.object(inbound_ocr, "aget_config", new_callable=AsyncMock, return_value=None):
            result = await inbound_ocr.aanalyze_inbound_image(
                image_url="https://example.com/img.jpg",
                client_id="test-client",
                trace_id="t1",
            )
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_empty_url(self):
        result = await inbound_ocr.aanalyze_inbound_image(
            image_url="",
            client_id="test-client",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_empty_client_id(self):
        result = await inbound_ocr.aanalyze_inbound_image(
            image_url="https://example.com/img.jpg",
            client_id="",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_ocr_text_on_success(self):
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=_mock_llm_response({
            "has_text": True,
            "text": "Order #99887",
            "is_product_image": False,
            "product_description": None,
        }))
        p1, p2 = _patch_prompt_and_taxonomy()

        with (
            patch.object(inbound_ocr, "aget_config", new_callable=AsyncMock, return_value="true"),
            patch.object(inbound_ocr, "_get_llm", return_value=mock_llm),
            p1, p2,
        ):
            result = await inbound_ocr.aanalyze_inbound_image(
                image_url="https://example.com/img.jpg",
                client_id="test-client",
                trace_id="t2",
            )
            assert result is not None
            assert result.ocr_text == "Order #99887"
            assert result.is_product_image is False

    @pytest.mark.asyncio
    async def test_returns_product_on_success(self):
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=_mock_llm_response({
            "has_text": False,
            "text": "",
            "is_product_image": True,
            "product_description": {
                "category": "topwear",
                "subcategory": "t-shirt",
                "color": "black",
                "segment": "men",
                "summary": "black oversized cotton t-shirt for men",
            },
        }))
        p1, p2 = _patch_prompt_and_taxonomy()

        with (
            patch.object(inbound_ocr, "aget_config", new_callable=AsyncMock, return_value="true"),
            patch.object(inbound_ocr, "_get_llm", return_value=mock_llm),
            p1, p2,
        ):
            result = await inbound_ocr.aanalyze_inbound_image(
                image_url="https://example.com/img.jpg",
                client_id="test-client",
                trace_id="t3",
            )
            assert result is not None
            assert result.is_product_image is True
            assert result.product_summary == "black oversized cotton t-shirt for men"
            assert result.product_attributes["category"] == "topwear"
            assert result.product_attributes["color"] == "black"
            assert result.ocr_text is None

    @pytest.mark.asyncio
    async def test_ocr_takes_priority_over_product(self):
        """When image has both OCR text and is a product, OCR text is set."""
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=_mock_llm_response({
            "has_text": True,
            "text": "50% OFF SALE",
            "is_product_image": True,
            "product_description": {
                "category": "topwear",
                "summary": "red dress on sale",
            },
        }))
        p1, p2 = _patch_prompt_and_taxonomy()

        with (
            patch.object(inbound_ocr, "aget_config", new_callable=AsyncMock, return_value="true"),
            patch.object(inbound_ocr, "_get_llm", return_value=mock_llm),
            p1, p2,
        ):
            result = await inbound_ocr.aanalyze_inbound_image(
                image_url="https://example.com/img.jpg",
                client_id="test-client",
                trace_id="t-both",
            )
            assert result is not None
            assert result.ocr_text == "50% OFF SALE"
            assert result.is_product_image is True
            assert result.product_summary == "red dress on sale"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_actionable_content(self):
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=_mock_llm_response({
            "has_text": False,
            "text": "",
            "is_product_image": False,
            "product_description": None,
        }))
        p1, p2 = _patch_prompt_and_taxonomy()

        with (
            patch.object(inbound_ocr, "aget_config", new_callable=AsyncMock, return_value="true"),
            patch.object(inbound_ocr, "_get_llm", return_value=mock_llm),
            p1, p2,
        ):
            result = await inbound_ocr.aanalyze_inbound_image(
                image_url="https://example.com/img.jpg",
                client_id="test-client",
                trace_id="t4",
            )
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_timeout(self):
        async def slow_llm(*args, **kwargs):
            await asyncio.sleep(10)
            return _mock_llm_response({"has_text": True, "text": "late"})

        mock_llm = AsyncMock()
        mock_llm.ainvoke = slow_llm
        p1, p2 = _patch_prompt_and_taxonomy()

        with (
            patch.object(inbound_ocr, "aget_config", new_callable=AsyncMock, return_value="true"),
            patch.object(inbound_ocr, "_get_llm", return_value=mock_llm),
            p1, p2,
        ):
            result = await inbound_ocr.aanalyze_inbound_image(
                image_url="https://example.com/img.jpg",
                client_id="test-client",
                trace_id="t5",
                timeout_seconds=0.1,
            )
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_llm_exception(self):
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=RuntimeError("LLM down"))
        p1, p2 = _patch_prompt_and_taxonomy()

        with (
            patch.object(inbound_ocr, "aget_config", new_callable=AsyncMock, return_value="true"),
            patch.object(inbound_ocr, "_get_llm", return_value=mock_llm),
            p1, p2,
        ):
            result = await inbound_ocr.aanalyze_inbound_image(
                image_url="https://example.com/img.jpg",
                client_id="test-client",
                trace_id="t6",
            )
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_llm_is_none(self):
        p1, p2 = _patch_prompt_and_taxonomy()

        with (
            patch.object(inbound_ocr, "aget_config", new_callable=AsyncMock, return_value="true"),
            patch.object(inbound_ocr, "_get_llm", return_value=None),
            p1, p2,
        ):
            result = await inbound_ocr.aanalyze_inbound_image(
                image_url="https://example.com/img.jpg",
                client_id="test-client",
                trace_id="t7",
            )
            assert result is None

    @pytest.mark.asyncio
    async def test_product_with_empty_summary_returns_none(self):
        """Product image detected but summary is empty — treated as non-actionable."""
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=_mock_llm_response({
            "has_text": False,
            "text": "",
            "is_product_image": True,
            "product_description": {"summary": "", "category": "topwear"},
        }))
        p1, p2 = _patch_prompt_and_taxonomy()

        with (
            patch.object(inbound_ocr, "aget_config", new_callable=AsyncMock, return_value="true"),
            patch.object(inbound_ocr, "_get_llm", return_value=mock_llm),
            p1, p2,
        ):
            result = await inbound_ocr.aanalyze_inbound_image(
                image_url="https://example.com/img.jpg",
                client_id="test-client",
            )
            assert result is None


# ---------------------------------------------------------------------------
# aextract_text_from_inbound_image (backward-compat wrapper)
# ---------------------------------------------------------------------------

class TestAextractTextFromInboundImage:
    @pytest.mark.asyncio
    async def test_returns_ocr_text_string(self):
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=_mock_llm_response({
            "has_text": True,
            "text": "Order #99887",
            "is_product_image": False,
            "product_description": None,
        }))
        p1, p2 = _patch_prompt_and_taxonomy()

        with (
            patch.object(inbound_ocr, "aget_config", new_callable=AsyncMock, return_value="true"),
            patch.object(inbound_ocr, "_get_llm", return_value=mock_llm),
            p1, p2,
        ):
            result = await inbound_ocr.aextract_text_from_inbound_image(
                image_url="https://example.com/img.jpg",
                client_id="test-client",
                trace_id="compat-1",
            )
            assert result == "Order #99887"

    @pytest.mark.asyncio
    async def test_returns_none_for_product_only(self):
        """Backward-compat wrapper returns None when only product is detected."""
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=_mock_llm_response({
            "has_text": False,
            "text": "",
            "is_product_image": True,
            "product_description": {"summary": "blue jeans", "category": "bottomwear"},
        }))
        p1, p2 = _patch_prompt_and_taxonomy()

        with (
            patch.object(inbound_ocr, "aget_config", new_callable=AsyncMock, return_value="true"),
            patch.object(inbound_ocr, "_get_llm", return_value=mock_llm),
            p1, p2,
        ):
            result = await inbound_ocr.aextract_text_from_inbound_image(
                image_url="https://example.com/img.jpg",
                client_id="test-client",
            )
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_flag_off(self):
        with patch.object(inbound_ocr, "aget_config", new_callable=AsyncMock, return_value=None):
            result = await inbound_ocr.aextract_text_from_inbound_image(
                image_url="https://example.com/img.jpg",
                client_id="test-client",
            )
            assert result is None


# ---------------------------------------------------------------------------
# Webhook helpers (_extract_media_url_from_payload, _extract_caption_from_payload)
# ---------------------------------------------------------------------------

gupshup_webhook = pytest.importorskip("fashion_bot.gupshup_webhook")


class TestExtractMediaUrlFromPayload:
    def test_extracts_image_url(self):
        url = gupshup_webhook._extract_media_url_from_payload(IMAGE_PAYLOAD)
        assert url == "https://filemanager.gupshup.io/fm/wamedia/testapp/abc123"

    def test_returns_none_for_empty_url(self):
        data = {"payload": {"type": "image", "payload": {"url": ""}}}
        assert gupshup_webhook._extract_media_url_from_payload(data) is None

    def test_returns_none_for_missing_payload(self):
        assert gupshup_webhook._extract_media_url_from_payload({}) is None

    def test_returns_none_for_none(self):
        assert gupshup_webhook._extract_media_url_from_payload({"payload": None}) is None


class TestExtractCaptionFromPayload:
    def test_extracts_caption(self):
        caption = gupshup_webhook._extract_caption_from_payload(IMAGE_PAYLOAD)
        assert caption == "my order screenshot"

    def test_returns_none_for_no_caption(self):
        caption = gupshup_webhook._extract_caption_from_payload(IMAGE_PAYLOAD_NO_CAPTION)
        assert caption is None

    def test_returns_none_for_empty_caption(self):
        data = {"payload": {"type": "image", "payload": {"caption": "   "}}}
        assert gupshup_webhook._extract_caption_from_payload(data) is None

    def test_returns_none_for_missing_payload(self):
        assert gupshup_webhook._extract_caption_from_payload({}) is None


# ---------------------------------------------------------------------------
# Webhook image-analysis step (_aanalyze_inbound_image_message)
# ---------------------------------------------------------------------------


def _patch_analysis(result):
    """Patch the analysis entry point the webhook step imports lazily."""
    return patch.object(
        inbound_ocr, "aanalyze_inbound_image", new_callable=AsyncMock, return_value=result
    )


class TestAnalyzeInboundImageMessage:
    @pytest.mark.asyncio
    async def test_skips_non_image_message_types(self):
        with _patch_analysis(None) as mocked:
            msg_type, content = await gupshup_webhook._aanalyze_inbound_image_message(
                IMAGE_PAYLOAD, "audio", "[Audio message]: Customer sent audio",
                "trace-1", "test-client", "919999999999",
            )
        assert (msg_type, content) == ("audio", "[Audio message]: Customer sent audio")
        mocked.assert_not_called()

    @pytest.mark.asyncio
    async def test_ocr_text_with_caption(self):
        analysis = inbound_ocr.InboundImageAnalysis(ocr_text="Order #1234 shipped")
        with _patch_analysis(analysis):
            msg_type, content = await gupshup_webhook._aanalyze_inbound_image_message(
                IMAGE_PAYLOAD, "image", "[Image message]: my order screenshot",
                "trace-1", "test-client", "919999999999",
            )
        assert msg_type == "text"
        assert content == "my order screenshot\n[Text from image]: Order #1234 shipped"

    @pytest.mark.asyncio
    async def test_ocr_text_without_caption(self):
        analysis = inbound_ocr.InboundImageAnalysis(ocr_text="Order #1234 shipped")
        with _patch_analysis(analysis):
            msg_type, content = await gupshup_webhook._aanalyze_inbound_image_message(
                IMAGE_PAYLOAD_NO_CAPTION, "image", "[Image message]: Customer sent image",
                "trace-1", "test-client", "919999999999",
            )
        assert msg_type == "text"
        assert content == "[Text from image]: Order #1234 shipped"

    @pytest.mark.asyncio
    async def test_product_image_with_caption(self):
        analysis = inbound_ocr.InboundImageAnalysis(
            is_product_image=True, product_summary="black oversized cotton t-shirt for men",
        )
        with _patch_analysis(analysis):
            msg_type, content = await gupshup_webhook._aanalyze_inbound_image_message(
                IMAGE_PAYLOAD, "image", "[Image message]: my order screenshot",
                "trace-1", "test-client", "919999999999",
            )
        assert msg_type == "text"
        assert content == (
            "my order screenshot\n[Product from image]: black oversized cotton t-shirt for men"
        )

    @pytest.mark.asyncio
    async def test_product_image_without_caption(self):
        analysis = inbound_ocr.InboundImageAnalysis(
            is_product_image=True, product_summary="black oversized cotton t-shirt for men",
        )
        with _patch_analysis(analysis):
            msg_type, content = await gupshup_webhook._aanalyze_inbound_image_message(
                IMAGE_PAYLOAD_NO_CAPTION, "image", "[Image message]: Customer sent image",
                "trace-1", "test-client", "919999999999",
            )
        assert msg_type == "text"
        assert content == (
            "[Product from image]: I'm looking for black oversized cotton t-shirt for men"
        )

    @pytest.mark.asyncio
    async def test_ocr_wins_when_the_image_is_not_a_product(self):
        analysis = inbound_ocr.InboundImageAnalysis(
            ocr_text="Order #1234 shipped on 12 Aug, arriving Thursday",
        )
        with _patch_analysis(analysis):
            _, content = await gupshup_webhook._aanalyze_inbound_image_message(
                IMAGE_PAYLOAD_NO_CAPTION, "image", "[Image message]: Customer sent image",
                "trace-1", "test-client", "919999999999",
            )
        assert content == (
            "[Text from image]: Order #1234 shipped on 12 Aug, arriving Thursday"
        )

    @pytest.mark.asyncio
    async def test_brand_wordmark_does_not_displace_product_summary(self):
        """A product photo with the brand printed on it is still a product photo."""
        analysis = inbound_ocr.InboundImageAnalysis(
            ocr_text="GROOVEE",
            is_product_image=True,
            product_summary="black oversized cotton t-shirt for men",
        )
        with _patch_analysis(analysis):
            _, content = await gupshup_webhook._aanalyze_inbound_image_message(
                IMAGE_PAYLOAD_NO_CAPTION, "image", "[Image message]: Customer sent image",
                "trace-1", "test-client", "919999999999",
            )
        assert content == (
            "[Product from image]: I'm looking for black oversized cotton t-shirt for men "
            '(text on the item: "GROOVEE")'
        )

    @pytest.mark.asyncio
    async def test_slogan_covered_garment_still_searches_as_a_product(self):
        """The production case: a tee covered in text is not an OCR message."""
        analysis = inbound_ocr.InboundImageAnalysis(
            ocr_text="ONE WITH ALL EXISTENCE.\n\u6230\n\u611b\nTHIS IS THE END OF THE CHAIN",
            is_product_image=True,
            product_summary="red oversized t-shirt with abstract print and japanese text",
        )
        with _patch_analysis(analysis):
            msg_type, content = await gupshup_webhook._aanalyze_inbound_image_message(
                IMAGE_PAYLOAD_NO_CAPTION, "image", "[Image message]: Customer sent image",
                "trace-1", "test-client", "919999999999",
            )
        assert msg_type == "text"
        assert content.startswith(
            "[Product from image]: I'm looking for "
            "red oversized t-shirt with abstract print and japanese text"
        )
        # the printed text still reaches the agent, as context rather than query
        assert "ONE WITH ALL EXISTENCE." in content

    @pytest.mark.asyncio
    async def test_short_ocr_still_used_when_there_is_no_product(self):
        analysis = inbound_ocr.InboundImageAnalysis(ocr_text="gv17685")
        with _patch_analysis(analysis):
            msg_type, content = await gupshup_webhook._aanalyze_inbound_image_message(
                IMAGE_PAYLOAD_NO_CAPTION, "image", "[Image message]: Customer sent image",
                "trace-1", "test-client", "919999999999",
            )
        assert (msg_type, content) == ("text", "[Text from image]: gv17685")

    @pytest.mark.asyncio
    async def test_returns_unchanged_when_analysis_is_none(self):
        with _patch_analysis(None):
            msg_type, content = await gupshup_webhook._aanalyze_inbound_image_message(
                IMAGE_PAYLOAD, "image", "[Image message]: my order screenshot",
                "trace-1", "test-client", "919999999999",
            )
        assert (msg_type, content) == ("image", "[Image message]: my order screenshot")

    @pytest.mark.asyncio
    async def test_returns_unchanged_when_no_media_url(self):
        data = {"payload": {"type": "image", "payload": {"caption": "hi"}}}
        with _patch_analysis(None) as mocked:
            msg_type, content = await gupshup_webhook._aanalyze_inbound_image_message(
                data, "image", "[Image message]: hi",
                "trace-1", "test-client", "919999999999",
            )
        assert (msg_type, content) == ("image", "[Image message]: hi")
        mocked.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_unchanged_when_product_summary_missing(self):
        analysis = inbound_ocr.InboundImageAnalysis(is_product_image=True, product_summary=None)
        with _patch_analysis(analysis):
            msg_type, content = await gupshup_webhook._aanalyze_inbound_image_message(
                IMAGE_PAYLOAD, "image", "[Image message]: my order screenshot",
                "trace-1", "test-client", "919999999999",
            )
        assert (msg_type, content) == ("image", "[Image message]: my order screenshot")

    @pytest.mark.asyncio
    async def test_fails_open_on_analysis_error(self):
        with patch.object(
            inbound_ocr, "aanalyze_inbound_image", new_callable=AsyncMock,
            side_effect=RuntimeError("vision model exploded"),
        ):
            msg_type, content = await gupshup_webhook._aanalyze_inbound_image_message(
                IMAGE_PAYLOAD, "image", "[Image message]: my order screenshot",
                "trace-1", "test-client", "919999999999",
            )
        assert (msg_type, content) == ("image", "[Image message]: my order screenshot")


# ---------------------------------------------------------------------------
# Default prompt: vertical neutrality
# ---------------------------------------------------------------------------


class TestDefaultPromptIsVerticalAgnostic:
    """The default prompt must not gate product detection on apparel.

    A client in another vertical gets the default prompt until someone seeds an
    ``agents_config`` row, so an apparel-only gate here silently sends every one
    of their product photos to the media short-circuit instead.
    """

    def test_product_gate_names_more_than_one_vertical(self):
        prompt = inbound_ocr._DEFAULT_IMAGE_ANALYSIS_PROMPT
        gate = next(
            line for line in prompt.splitlines() if line.startswith('- "is_product_image"')
        )
        assert "physical product a retailer could sell" in gate
        for vertical in ("apparel", "electronics", "home goods"):
            assert vertical in gate, f"{vertical} missing from the product gate"

    def test_garment_only_fields_are_marked_optional(self):
        prompt = inbound_ocr._DEFAULT_IMAGE_ANALYSIS_PROMPT
        assert "never stretch a garment term to cover a non-garment product" in prompt
        for field in ("pattern", "style", "fit"):
            assert f'"{field}"' in prompt

    def test_summary_examples_span_verticals(self):
        prompt = inbound_ocr._DEFAULT_IMAGE_ANALYSIS_PROMPT
        assert "black oversized cotton t-shirt for men" in prompt
        assert "water bottle" in prompt
        assert "headphones" in prompt


# ---------------------------------------------------------------------------
# OCR-vs-product priority and the analysis budget
# ---------------------------------------------------------------------------


class TestPreferOcr:
    def test_no_ocr_text_never_prefers_ocr(self):
        a = inbound_ocr.InboundImageAnalysis(is_product_image=True, product_summary="a tee")
        assert a.prefer_ocr() is False

    def test_a_describable_product_is_never_an_ocr_message(self):
        """However much text is printed on it — the production regression."""
        a = inbound_ocr.InboundImageAnalysis(
            ocr_text="x" * 500, is_product_image=True, product_summary="a tee",
        )
        assert a.prefer_ocr() is False

    def test_product_image_without_a_summary_falls_back_to_ocr(self):
        a = inbound_ocr.InboundImageAnalysis(ocr_text="x" * 500, is_product_image=True)
        assert a.prefer_ocr() is True

    def test_short_ocr_wins_when_no_product_summary(self):
        a = inbound_ocr.InboundImageAnalysis(ocr_text="short", is_product_image=True)
        assert a.prefer_ocr() is True

    def test_short_ocr_wins_when_not_a_product_image(self):
        a = inbound_ocr.InboundImageAnalysis(ocr_text="short")
        assert a.prefer_ocr() is True


class TestEffectiveTimeoutSeconds:
    """A bad env value must never widen or disable the budget silently."""

    def _with_env(self, value):
        mod = MagicMock()
        mod.get_env = MagicMock(return_value=value)
        return patch.dict("sys.modules", {"fashion_bot.env_loader": mod})

    def test_uses_fallback_when_unset(self):
        with self._with_env(None):
            assert inbound_ocr._effective_timeout_seconds(10.0) == 10.0

    def test_override_is_applied(self):
        with self._with_env("7.5"):
            assert inbound_ocr._effective_timeout_seconds(10.0) == 7.5

    def test_unparseable_override_falls_back(self):
        with self._with_env("soon"):
            assert inbound_ocr._effective_timeout_seconds(10.0) == 10.0

    def test_non_positive_override_falls_back(self):
        with self._with_env("0"):
            assert inbound_ocr._effective_timeout_seconds(10.0) == 10.0

    def test_default_budget_exceeds_observed_warm_latency(self):
        """Production measured ~3.5s warm; the budget must leave real headroom."""
        assert inbound_ocr._DEFAULT_TIMEOUT_SECONDS >= 8.0


# ---------------------------------------------------------------------------
# Ordering invariant: analysis must run before anything reads message_content
# ---------------------------------------------------------------------------


class TestAnalysisRunsBeforeStateIsBuilt:
    """The rewritten message must exist before the turn is built around it.

    In production the analysis ran *after* ``aget_or_create_state``, which
    appends its ``initial_message`` to ``state["messages"]``. The graph reads
    ``state``, and ``stream_graph_response`` deliberately does not re-add its
    ``message`` argument — so a successful OCR reached only a trace label while
    the agent kept answering from the "[Image message]" placeholder. The same
    ordering also decides whether the transcript row and the media-backfill
    ``placeholder_text`` agree, since the backfill matches on exact text.

    These assert on source order because that is precisely what regressed;
    a behavioural test would need the whole webhook turn stood up.
    """

    import inspect as _inspect

    def _order(self, func):
        src = self._inspect.getsource(func)
        pos = {
            name: src.find(name)
            for name in (
                "_aanalyze_inbound_image_message",
                "aget_or_create_state",
                "astore_message_event_with_conversation_resolution",
                "placeholder_text=message_content",
            )
        }
        return src, pos

    @pytest.mark.parametrize(
        "func_name",
        ["_execute_runtime_turn_core", "_execute_turn_run_graph_and_update_state_for_gupshup"],
    )
    def test_analysis_precedes_state_and_transcript(self, func_name):
        func = getattr(gupshup_webhook, func_name)
        src, pos = self._order(func)
        analysis = pos["_aanalyze_inbound_image_message"]
        assert analysis != -1, f"{func_name} no longer runs the image analysis step"

        for later in (
            "aget_or_create_state",
            "astore_message_event_with_conversation_resolution",
            "placeholder_text=message_content",
        ):
            if pos[later] == -1:
                continue
            assert analysis < pos[later], (
                f"{func_name}: image analysis must run before {later}, "
                "otherwise the rewritten message never reaches it"
            )

    @pytest.mark.parametrize(
        "func_name",
        ["_execute_runtime_turn_core", "_execute_turn_run_graph_and_update_state_for_gupshup"],
    )
    def test_analysis_runs_exactly_once(self, func_name):
        src, _ = self._order(getattr(gupshup_webhook, func_name))
        assert src.count("await _aanalyze_inbound_image_message(") == 1

    @pytest.mark.parametrize(
        "func_name",
        ["_execute_runtime_turn_core", "_execute_turn_run_graph_and_update_state_for_gupshup"],
    )
    def test_media_storage_is_not_gated_on_the_rewritten_type(self, func_name):
        """A successfully analysed image is still an image that must be stored.

        The durable-media task used to live inside the ``message_type in
        MEDIA_MESSAGE_TYPES`` short-circuit. Once analysis rewrites the type to
        "text" that branch is skipped, so the attachment was silently dropped
        for exactly the images the analysis worked on — and widening the
        timeout makes that the common case, not the rare one.
        """
        src, pos = self._order(getattr(gupshup_webhook, func_name))

        capture = src.find("inbound_media_type = message_type")
        assert capture != -1, f"{func_name} no longer captures the arriving media type"
        assert capture < pos["_aanalyze_inbound_image_message"], (
            "the arriving media type must be captured before analysis can rewrite it"
        )

        schedule = src.find("_astore_inbound_media_and_backfill(")
        assert schedule != -1, f"{func_name} no longer stores inbound media"
        gate = src.rfind("if inbound_media_type and conv_id:", 0, schedule)
        assert gate != -1, (
            "media storage must be gated on the arriving type, not the rewritten one"
        )
        assert src.count("_astore_inbound_media_and_backfill(") == 1, "scheduled more than once"


class TestOcrContext:
    def test_none_when_the_ocr_text_is_the_message(self):
        a = inbound_ocr.InboundImageAnalysis(ocr_text="a receipt")
        assert a.ocr_context() is None

    def test_none_without_ocr_text(self):
        a = inbound_ocr.InboundImageAnalysis(is_product_image=True, product_summary="a tee")
        assert a.ocr_context() is None

    def test_collapses_whitespace(self):
        a = inbound_ocr.InboundImageAnalysis(
            ocr_text="ONE WITH\n\n  ALL   EXISTENCE",
            is_product_image=True, product_summary="a tee",
        )
        assert a.ocr_context() == "ONE WITH ALL EXISTENCE"

    def test_truncates_a_slogan_covered_garment(self):
        a = inbound_ocr.InboundImageAnalysis(
            ocr_text="y" * 900, is_product_image=True, product_summary="a tee",
        )
        ctx = a.ocr_context()
        assert len(ctx) == inbound_ocr.MAX_OCR_CONTEXT_CHARS + 1  # + the ellipsis
        assert ctx.endswith("…")

    def test_whitespace_only_ocr_yields_no_context(self):
        a = inbound_ocr.InboundImageAnalysis(
            ocr_text="   \n  ", is_product_image=True, product_summary="a tee",
        )
        assert a.ocr_context() is None


class TestImageSourcedQueryPromptGuidance:
    """Both product-facing defaults must scope the honest-absence wording.

    Without this, the relevance-check block — written for category misses — fires
    on a machine-read photo and asserts the customer's item is unavailable.
    """

    def _defaults(self):
        from fashion_bot.utils.context_helpers import get_default_prompt_for_agent
        return {name: get_default_prompt_for_agent(name)
                for name in ("product_details", "recommendations")}

    def test_both_handlers_carry_the_rule(self):
        for name, prompt in self._defaults().items():
            assert "IMAGE-SOURCED QUERIES" in prompt, f"{name} missing the rule"

    def test_rule_forbids_asserting_absence(self):
        for name, prompt in self._defaults().items():
            assert "NEVER tell the customer that the specific item" in prompt, name

    def test_rule_asks_for_a_name_or_link(self):
        for name, prompt in self._defaults().items():
            assert "product name or link" in prompt, name

    def test_rule_names_both_image_prefixes(self):
        for name, prompt in self._defaults().items():
            assert "[Product from image]:" in prompt, name
            assert "[Text from image]:" in prompt, name


# ---------------------------------------------------------------------------
# Observability: a failed analysis must never be silent in production
# ---------------------------------------------------------------------------


class TestFailurePathsAreVisible:
    """Production ships INFO and WARN only — DEBUG is dropped on the floor.

    A customer sent a product photo, got the "I'm not able to view images yet"
    reply, and the logs held nothing at all: the analysis had run, returned no
    actionable content, and said so at DEBUG. Every dead end below must be
    visible at WARNING or above.
    """

    def test_unparseable_response_is_logged(self, caplog):
        with caplog.at_level("WARNING", logger="inbound_image_ocr"):
            assert inbound_ocr._parse_response("I cannot see an image here.") == {}
        # _parse_response itself stays quiet; the caller owns the logging
        assert inbound_ocr._parse_response("not json") == {}

    def test_no_actionable_content_logs_at_warning_not_debug(self):
        import inspect
        src = inspect.getsource(inbound_ocr.aanalyze_inbound_image)
        marker = "no actionable content"
        assert marker in src
        before = src[: src.index(marker)]
        call = before[before.rindex("logger.") :]
        assert call.startswith("logger.warning"), (
            "the no-content outcome must be visible in production; "
            "DEBUG is dropped and this failure then has no log at all"
        )

    def test_run_analysis_reports_why_it_gave_up(self):
        import inspect
        src = inspect.getsource(inbound_ocr._arun_analysis)
        assert "unparseable model response" in src
        assert "no actionable content" in src

    def test_webhook_logs_a_missing_media_url(self):
        import inspect
        src = inspect.getsource(gupshup_webhook._aanalyze_inbound_image_message)
        assert "carried no media URL" in src
