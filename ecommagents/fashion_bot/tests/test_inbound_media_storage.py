"""Tests for durable storage of inbound WhatsApp media.

Covers the Cloudinary helper in isolation and its wiring into the Gupshup
webhook: the transcript line carries the stored link, ``message_metadata``
carries the structured descriptor, and every failure path stays fail-open.
"""

import pytest

media_storage = pytest.importorskip("fashion_bot.utils.media_storage")

InboundMedia = media_storage.InboundMedia
StoredMedia = media_storage.StoredMedia


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
            "caption": "damaged item",
            "urlExpiry": 1580832279000,
        },
    },
}

STORED = StoredMedia(
    url="https://res.cloudinary.com/demo/image/upload/v1/whatsapp-inbound/deadbeef.jpg",
    public_id="whatsapp-inbound/client/9999/deadbeef",
    resource_type="image",
    provider="cloudinary",
    size_bytes=1234,
    content_type="image/jpeg",
)


# ---------------------------------------------------------------------------
# extract_inbound_media
# ---------------------------------------------------------------------------

def test_extract_inbound_media_reads_gupshup_image_payload():
    media = media_storage.extract_inbound_media(IMAGE_PAYLOAD)

    assert media is not None
    assert media.media_type == "image"
    assert media.url.endswith("abc123")
    assert media.content_type == "image/jpeg"
    assert media.caption == "damaged item"
    assert media.url_expiry == 1580832279000


def _payload(media_type, inner):
    return {"payload": {"id": f"msg-{media_type}", "type": media_type,
                        "sender": {"phone": "919999999999"}, "payload": inner}}


AUDIO_PAYLOAD = _payload("audio", {
    "url": "https://filemanager.gupshup.io/fm/wamedia/testapp/voice",
    "contentType": "audio/ogg",
})
VIDEO_PAYLOAD = _payload("video", {
    "url": "https://filemanager.gupshup.io/fm/wamedia/testapp/clip",
    "contentType": "video/mp4",
})
FILE_PAYLOAD = _payload("file", {
    "url": "https://filemanager.gupshup.io/fm/wamedia/testapp/doc",
    "contentType": "application/pdf",
    "filename": "invoice-GV1234.pdf",
})


@pytest.mark.parametrize(
    "payload,media_type,resource_type",
    [
        (IMAGE_PAYLOAD, "image", "image"),
        (VIDEO_PAYLOAD, "video", "video"),
        (AUDIO_PAYLOAD, "audio", "video"),   # Cloudinary serves audio as video
        (FILE_PAYLOAD, "file", "raw"),       # documents are raw assets
    ],
)
def test_every_media_kind_is_extracted_and_routed_to_a_resource_type(
    payload, media_type, resource_type
):
    media = media_storage.extract_inbound_media(payload)

    assert media is not None
    assert media.media_type == media_type
    assert media_storage._RESOURCE_TYPE_BY_MEDIA_TYPE[media_type] == resource_type


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,expected_endpoint",
    [
        (VIDEO_PAYLOAD, "https://api.cloudinary.com/v1_1/demo/video/upload"),
        (AUDIO_PAYLOAD, "https://api.cloudinary.com/v1_1/demo/video/upload"),
        (FILE_PAYLOAD, "https://api.cloudinary.com/v1_1/demo/raw/upload"),
    ],
)
async def test_video_audio_and_documents_upload_to_the_right_endpoint(
    monkeypatch, enabled_cloudinary, payload, expected_endpoint
):
    http = _FakeHttpClient()
    _install_http_client(monkeypatch, http)

    stored = await media_storage.astore_inbound_media(
        media_storage.extract_inbound_media(payload),
        client_id="client-1",
        phone="919999999999",
        trace_id="t1",
    )

    assert stored is not None
    assert http.post_calls[0][0] == expected_endpoint


@pytest.mark.asyncio
async def test_document_upload_keeps_its_filename(monkeypatch, enabled_cloudinary):
    http = _FakeHttpClient()
    _install_http_client(monkeypatch, http)

    await media_storage.astore_inbound_media(
        media_storage.extract_inbound_media(FILE_PAYLOAD),
        client_id="client-1",
        phone="919999999999",
        trace_id="t1",
    )

    assert http.post_calls[0][1]["files"]["file"][0] == "invoice-GV1234.pdf"


def test_extract_inbound_media_ignores_text_and_urlless_media():
    text_payload = {"payload": {"type": "text", "payload": {"text": "hello"}}}
    urlless = {"payload": {"type": "image", "payload": {"caption": "no url"}}}

    assert media_storage.extract_inbound_media(text_payload) is None
    assert media_storage.extract_inbound_media(urlless) is None
    assert media_storage.extract_inbound_media({}) is None


# ---------------------------------------------------------------------------
# metadata + transcript text
# ---------------------------------------------------------------------------

def test_build_media_metadata_describes_the_stored_asset():
    media = media_storage.extract_inbound_media(IMAGE_PAYLOAD)

    stored_meta = media_storage.build_media_metadata(media, STORED)["media"]
    assert stored_meta["stored"] is True
    assert stored_meta["url"] == STORED.url
    assert stored_meta["provider"] == "cloudinary"
    assert stored_meta["caption"] == "damaged item"
    assert stored_meta["source_url_expiry"] == 1580832279000


def test_append_media_link_is_a_noop_without_a_stored_asset():
    assert media_storage.append_media_link("[Image message]: hi", None) == "[Image message]: hi"


def test_append_media_link_appends_once():
    once = media_storage.append_media_link("[Image message]: hi", STORED)
    assert once.endswith(STORED.url)
    assert media_storage.append_media_link(once, STORED) == once


# ---------------------------------------------------------------------------
# astore_inbound_media
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, *, content=b"", json_body=None, status=200):
        self.content = content
        self._json = json_body or {}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class _FakeHttpClient:
    """Records the download GET and the Cloudinary upload POST."""

    def __init__(self, *, content=b"jpeg-bytes", upload_body=None, upload_status=200):
        self.content = content
        self.upload_body = upload_body if upload_body is not None else {
            "secure_url": STORED.url,
            "public_id": STORED.public_id,
            "resource_type": "image",
            "bytes": 1234,
        }
        self.upload_status = upload_status
        self.get_calls = []
        self.post_calls = []

    async def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return _FakeResponse(content=self.content)

    async def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return _FakeResponse(json_body=self.upload_body, status=self.upload_status)


@pytest.fixture
def enabled_cloudinary(monkeypatch):
    """Feature flag on with credentials present."""
    async def _enabled(_client_id):
        return True

    monkeypatch.setattr(media_storage, "ais_media_storage_enabled", _enabled)
    monkeypatch.setattr(
        media_storage,
        "_cloudinary_credentials",
        lambda: {"cloud_name": "demo", "api_key": "key", "api_secret": "secret"},
    )


def _install_http_client(monkeypatch, client):
    async def _get_client():
        return client

    monkeypatch.setattr(media_storage, "get_shared_async_http_client", _get_client)


@pytest.mark.asyncio
async def test_astore_inbound_media_uploads_and_returns_link(monkeypatch, enabled_cloudinary):
    http = _FakeHttpClient()
    _install_http_client(monkeypatch, http)

    media = media_storage.extract_inbound_media(IMAGE_PAYLOAD)
    stored = await media_storage.astore_inbound_media(
        media, client_id="client-1", phone="919999999999", trace_id="t1"
    )

    assert stored is not None
    assert stored.url == STORED.url
    assert stored.provider == "cloudinary"

    # Downloaded the Gupshup URL, uploaded to the image endpoint of the cloud.
    assert http.get_calls[0][0] == media.url
    upload_url, upload_kwargs = http.post_calls[0]
    assert upload_url == "https://api.cloudinary.com/v1_1/demo/image/upload"
    assert upload_kwargs["files"]["file"][1] == b"jpeg-bytes"

    form = upload_kwargs["data"]
    assert form["api_key"] == "key"
    assert form["signature"], "upload must be signed"
    # Only the last 4 phone digits reach the object path.
    assert form["folder"].endswith("/client-1/9999")
    assert "919999999999" not in form["folder"]


@pytest.mark.asyncio
async def test_upload_is_idempotent_across_a_webhook_redelivery(monkeypatch, enabled_cloudinary):
    """Same message twice must overwrite one asset, not leave a duplicate."""
    http = _FakeHttpClient()
    _install_http_client(monkeypatch, http)

    media = media_storage.extract_inbound_media(IMAGE_PAYLOAD)
    for _ in range(2):
        await media_storage.astore_inbound_media(
            media, client_id="client-1", phone="919999999999", trace_id="t1"
        )

    first, second = (call[1]["data"] for call in http.post_calls)
    assert first["public_id"] == second["public_id"]
    assert first["overwrite"] == "true"
    # Derived from the message id, not derivable from it alone.
    assert IMAGE_PAYLOAD["payload"]["id"] not in first["public_id"]


@pytest.mark.asyncio
async def test_public_id_is_tenant_scoped(monkeypatch, enabled_cloudinary):
    media = media_storage.extract_inbound_media(IMAGE_PAYLOAD)
    assert media_storage._public_id_for(media, "client-1") != media_storage._public_id_for(
        media, "client-2"
    )


@pytest.mark.asyncio
async def test_astore_inbound_media_signature_matches_signed_params(monkeypatch, enabled_cloudinary):
    http = _FakeHttpClient()
    _install_http_client(monkeypatch, http)

    media = media_storage.extract_inbound_media(IMAGE_PAYLOAD)
    await media_storage.astore_inbound_media(
        media, client_id="client-1", phone="919999999999", trace_id="t1"
    )

    form = dict(http.post_calls[0][1]["data"])
    signature = form.pop("signature")
    form.pop("api_key")
    assert signature == media_storage._sign_params(form, "secret")


@pytest.mark.asyncio
async def test_astore_inbound_media_skips_when_disabled(monkeypatch):
    async def _disabled(_client_id):
        return False

    monkeypatch.setattr(media_storage, "ais_media_storage_enabled", _disabled)
    http = _FakeHttpClient()
    _install_http_client(monkeypatch, http)

    media = media_storage.extract_inbound_media(IMAGE_PAYLOAD)
    stored = await media_storage.astore_inbound_media(
        media, client_id="client-1", phone="919999999999", trace_id="t1"
    )

    assert stored is None
    assert http.get_calls == []


@pytest.mark.asyncio
async def test_storage_is_enabled_by_default_and_overridable(monkeypatch):
    """Default on; env or a per-client config row can still turn it off."""
    seen = {}

    def _fake_get_bool(key, default):
        seen[key] = default
        return default

    monkeypatch.setattr(media_storage, "get_bool", _fake_get_bool)

    # No client id: falls straight through to the global default.
    assert await media_storage.ais_media_storage_enabled(None) is True
    assert seen["INBOUND_MEDIA_STORAGE_ENABLED"] is True

    # A per-client row wins over the default, in both directions.
    async def _config_says(value):
        async def _aget_config(_key, default=None, client_id=None):
            return value
        import fashion_bot.config_manager as cfg
        monkeypatch.setattr(cfg, "aget_config", _aget_config)
        return await media_storage.ais_media_storage_enabled("client-1")

    assert await _config_says("false") is False
    assert await _config_says("true") is True


@pytest.mark.asyncio
async def test_enabled_without_credentials_still_changes_nothing(monkeypatch):
    """Default-on must be inert until Cloudinary is actually provisioned."""
    monkeypatch.setattr(media_storage, "_cloudinary_credentials", lambda: None)
    monkeypatch.setattr(media_storage, "get_bool", lambda key, default: default)
    http = _FakeHttpClient()
    _install_http_client(monkeypatch, http)

    text, metadata = await media_storage.aprepare_inbound_media(
        IMAGE_PAYLOAD,
        placeholder_text="[Image message]: damaged item",
        client_id=None,
        phone="919999999999",
    )

    assert (text, metadata) == ("[Image message]: damaged item", None)
    assert http.get_calls == []


@pytest.mark.asyncio
async def test_astore_inbound_media_skips_when_credentials_missing(monkeypatch):
    async def _enabled(_client_id):
        return True

    monkeypatch.setattr(media_storage, "ais_media_storage_enabled", _enabled)
    monkeypatch.setattr(media_storage, "_cloudinary_credentials", lambda: None)
    http = _FakeHttpClient()
    _install_http_client(monkeypatch, http)

    media = media_storage.extract_inbound_media(IMAGE_PAYLOAD)
    stored = await media_storage.astore_inbound_media(
        media, client_id="client-1", phone="919999999999", trace_id="t1"
    )

    assert stored is None
    assert http.get_calls == []


@pytest.mark.asyncio
async def test_astore_inbound_media_fails_open_on_upload_error(monkeypatch, enabled_cloudinary):
    http = _FakeHttpClient(upload_status=500)
    _install_http_client(monkeypatch, http)

    media = media_storage.extract_inbound_media(IMAGE_PAYLOAD)
    stored = await media_storage.astore_inbound_media(
        media, client_id="client-1", phone="919999999999", trace_id="t1"
    )

    assert stored is None


@pytest.mark.asyncio
async def test_timeout_budget_is_sized_for_video_not_photos(monkeypatch, enabled_cloudinary):
    """Storage runs behind the reply, so the budget covers the biggest upload."""
    _install_http_client(monkeypatch, _FakeHttpClient())
    seen = {}

    real_get_int = media_storage.get_int

    def _record(key, default):
        seen[key] = default
        return real_get_int(key, default)

    monkeypatch.setattr(media_storage, "get_int", _record)

    await media_storage.astore_inbound_media(
        media_storage.extract_inbound_media(VIDEO_PAYLOAD),
        client_id="client-1",
        phone="919999999999",
        trace_id="t1",
    )

    assert seen["INBOUND_MEDIA_TOTAL_TIMEOUT_SECONDS"] == 60
    # Per-phase values sit under the ceiling but must not cut a healthy
    # transfer short before it.
    assert seen["INBOUND_MEDIA_DOWNLOAD_TIMEOUT_SECONDS"] == 30
    assert seen["INBOUND_MEDIA_UPLOAD_TIMEOUT_SECONDS"] == 45
    assert (
        seen["INBOUND_MEDIA_DOWNLOAD_TIMEOUT_SECONDS"]
        + seen["INBOUND_MEDIA_UPLOAD_TIMEOUT_SECONDS"]
        >= seen["INBOUND_MEDIA_TOTAL_TIMEOUT_SECONDS"]
    ), "phase timeouts must not undercut the total budget"


@pytest.mark.asyncio
async def test_astore_inbound_media_bounds_a_hanging_config_read(monkeypatch):
    """The feature-flag lookup reads config, so it must sit inside the budget."""
    import asyncio

    async def _hangs(_client_id):
        await asyncio.sleep(30)
        return True

    monkeypatch.setattr(media_storage, "ais_media_storage_enabled", _hangs)
    monkeypatch.setattr(
        media_storage,
        "get_int",
        lambda key, default: 1 if key == "INBOUND_MEDIA_TOTAL_TIMEOUT_SECONDS" else default,
    )

    media = media_storage.extract_inbound_media(IMAGE_PAYLOAD)
    stored = await media_storage.astore_inbound_media(
        media, client_id="client-1", phone="919999999999", trace_id="t1"
    )

    assert stored is None


# ---------------------------------------------------------------------------
# aprepare_inbound_media — the invariant every inbound path relies on
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prepare_is_zero_diff_when_storage_is_disabled(monkeypatch):
    async def _disabled(_client_id):
        return False

    monkeypatch.setattr(media_storage, "ais_media_storage_enabled", _disabled)

    text, metadata = await media_storage.aprepare_inbound_media(
        IMAGE_PAYLOAD,
        placeholder_text="[Image message]: damaged item",
        client_id="client-1",
        phone="919999999999",
        trace_id="t1",
    )

    # Disabled must be byte-for-byte the pre-feature behaviour: unchanged text,
    # and message_metadata stays NULL rather than gaining a blob.
    assert text == "[Image message]: damaged item"
    assert metadata is None


@pytest.mark.asyncio
async def test_prepare_leaves_non_media_payloads_untouched(monkeypatch, enabled_cloudinary):
    http = _FakeHttpClient()
    _install_http_client(monkeypatch, http)

    text_payload = {"payload": {"type": "text", "payload": {"text": "hi"}}}
    text, metadata = await media_storage.aprepare_inbound_media(
        text_payload,
        placeholder_text="hi",
        client_id="client-1",
        phone="919999999999",
    )

    assert (text, metadata) == ("hi", None)
    assert http.get_calls == []


@pytest.mark.asyncio
async def test_prepare_returns_link_and_metadata_together(monkeypatch, enabled_cloudinary):
    _install_http_client(monkeypatch, _FakeHttpClient())

    text, metadata = await media_storage.aprepare_inbound_media(
        IMAGE_PAYLOAD,
        placeholder_text="[Image message]: damaged item",
        client_id="client-1",
        phone="919999999999",
    )

    assert text.endswith(STORED.url)
    assert metadata["media"]["url"] == STORED.url


@pytest.mark.asyncio
async def test_astore_inbound_media_rejects_oversized_asset(monkeypatch, enabled_cloudinary):
    http = _FakeHttpClient(content=b"x" * 2048)
    _install_http_client(monkeypatch, http)
    monkeypatch.setattr(
        media_storage,
        "get_int",
        lambda key, default: 512 if key == "INBOUND_MEDIA_MAX_BYTES" else default,
    )

    media = media_storage.extract_inbound_media(IMAGE_PAYLOAD)
    stored = await media_storage.astore_inbound_media(
        media, client_id="client-1", phone="919999999999", trace_id="t1"
    )

    assert stored is None
    assert http.post_calls == [], "oversized assets must not be uploaded"


# ---------------------------------------------------------------------------
# Webhook wiring
# ---------------------------------------------------------------------------

gw = pytest.importorskip("fashion_bot.gupshup_webhook")


@pytest.fixture
def _silent_redis_publish(monkeypatch):
    async def _noop(*_a, **_kw):
        return None

    monkeypatch.setattr(gw, "publish_inbound_to_redis", _noop)


def _patch_prepare(monkeypatch, module, *, stored):
    """Stub the shared entry point with its real contract."""
    async def _prepare(_data, *, placeholder_text, client_id, phone, trace_id=None):
        if not stored:
            return placeholder_text, None
        return (
            media_storage.append_media_link(placeholder_text, STORED),
            media_storage.build_media_metadata(
                media_storage.extract_inbound_media(IMAGE_PAYLOAD), STORED
            ),
        )

    monkeypatch.setattr(module, "aprepare_inbound_media", _prepare)


@pytest.mark.asyncio
async def test_extraction_never_stores_media_on_the_reply_path(monkeypatch, _silent_redis_publish):
    """The customer is waiting on the reply after this call — no upload here."""
    called = []

    async def _must_not_run(*_a, **_kw):
        called.append(True)
        return "", None

    monkeypatch.setattr(gw, "aprepare_inbound_media", _must_not_run)

    message_type, text = await gw._extract_runtime_message_content(
        IMAGE_PAYLOAD, "t1", "client-1", "919999999999"
    )

    assert (message_type, text) == ("image", "[Image message]: damaged item")
    assert called == [], "storage must run behind the reply, not inside extraction"


@pytest.mark.asyncio
async def test_extract_runtime_message_content_leaves_text_messages_alone(monkeypatch):
    text_payload = {"payload": {"type": "text", "payload": {"text": "where is my order"}}}

    message_type, text = await gw._extract_runtime_message_content(
        text_payload, "t1", "client-1", "919999999999"
    )

    assert (message_type, text) == ("text", "where is my order")


@pytest.mark.asyncio
async def test_documents_short_circuit_like_other_media(monkeypatch):
    """A document must get a canned reply, not fall through to the agent graph."""
    from fashion_bot.utils.media_storage import MEDIA_MESSAGE_TYPES

    assert set(MEDIA_MESSAGE_TYPES) == {"image", "video", "audio", "file"}
    assert gw._MEDIA_CONFIG_KEYS["file"] == "media_unsupported_reply_file"

    reply = await gw._get_media_unsupported_reply("file", client_id=None)
    assert reply == gw._DEFAULT_FILE_UNSUPPORTED_REPLY
    assert "documents" in reply


@pytest.mark.asyncio
async def test_document_placeholder_names_the_attachment(monkeypatch, _silent_redis_publish):
    message_type, text = await gw._extract_runtime_message_content(
        FILE_PAYLOAD, "t1", "client-1", "919999999999"
    )

    assert message_type == "file"
    assert text == "[File message]: invoice-GV1234.pdf"


@pytest.mark.asyncio
async def test_voice_notes_are_not_transcribed(monkeypatch, _silent_redis_publish):
    """Audio behaves like every other media kind: placeholder, no Whisper."""
    message_type, text = await gw._extract_runtime_message_content(
        AUDIO_PAYLOAD, "t1", "client-1", "919999999999"
    )

    assert message_type == "audio"
    assert text == "[Audio message]: Customer sent audio"
    # The transcription helper must not be reachable from the webhook at all.
    assert not hasattr(gw, "gupshup_audio_to_text")


@pytest.mark.asyncio
async def test_audio_backfill_targets_its_placeholder(monkeypatch):
    _patch_prepare(monkeypatch, gw, stored=True)
    calls = []

    async def _attach(conversation_id, sender, expected_text, new_text, metadata):
        calls.append((expected_text, new_text))
        return True

    import fashion_bot.history.postgres_conversations as pgc

    monkeypatch.setattr(pgc, "aattach_media_to_message", _attach)

    placeholder = "[Audio message]: Customer sent audio"
    await gw._astore_inbound_media_and_backfill(
        data=AUDIO_PAYLOAD,
        conversation_id="conv-1",
        placeholder_text=placeholder,
        sender_phone="919999999999",
        client_id="client-1",
        trace_id="t1",
    )

    expected_text, new_text = calls[0]
    assert expected_text == placeholder
    assert new_text == f"{placeholder}\n{STORED.url}"


def test_media_placeholder_matches_between_agent_and_bot_paths():
    payload = IMAGE_PAYLOAD["payload"]
    assert gw._build_media_placeholder_text("image", payload) == "[Image message]: damaged item"

    no_caption = {"type": "image", "payload": {"url": "https://x/y"}}
    assert (
        gw._build_media_placeholder_text("image", no_caption)
        == "[Image message]: Customer sent image"
    )


@pytest.mark.asyncio
async def test_media_backfill_writes_link_and_metadata(monkeypatch):
    _patch_prepare(monkeypatch, gw, stored=True)

    calls = []

    async def _attach(conversation_id, sender, expected_text, new_text, metadata):
        calls.append((conversation_id, sender, expected_text, new_text, metadata))
        return True

    import fashion_bot.history.postgres_conversations as pgc

    monkeypatch.setattr(pgc, "aattach_media_to_message", _attach)

    await gw._astore_inbound_media_and_backfill(
        data=IMAGE_PAYLOAD,
        conversation_id="conv-1",
        placeholder_text="[Image message]: damaged item",
        sender_phone="919999999999",
        client_id="client-1",
        trace_id="t1",
    )

    assert len(calls) == 1
    conversation_id, sender, expected_text, new_text, metadata = calls[0]
    assert conversation_id == "conv-1"
    assert sender == "customer"
    assert expected_text == "[Image message]: damaged item"
    assert new_text.endswith(STORED.url)
    assert metadata["media"]["url"] == STORED.url


@pytest.mark.asyncio
async def test_queued_media_keeps_its_link_through_the_merge(monkeypatch):
    """A second image arriving mid-turn is merged as plain text — keep the link."""
    support_mod = pytest.importorskip("fashion_bot.core.gupshup_runtime_support")
    support = support_mod.GupshupRuntimeSupport(
        get_redis_client_fn=lambda: None,
        redis_guard=None,
        log_fn=lambda *a, **k: None,
        mark_degraded_state_fn=lambda *a, **k: None,
        get_state_fn=lambda *a, **k: {},
        update_state_fn=lambda *a, **k: None,
        generate_trace_id_fn=lambda: "t1",
    )
    _patch_prepare(monkeypatch, media_storage, stored=True)

    merged = await support._amerge_text_with_stored_media(
        "client-1", "919999999999", "t1", IMAGE_PAYLOAD
    )

    assert merged.startswith("[image message]")
    assert merged.endswith(STORED.url)


@pytest.mark.asyncio
async def test_queued_media_merge_text_unchanged_when_nothing_stored(monkeypatch):
    support_mod = pytest.importorskip("fashion_bot.core.gupshup_runtime_support")
    support = support_mod.GupshupRuntimeSupport(
        get_redis_client_fn=lambda: None,
        redis_guard=None,
        log_fn=lambda *a, **k: None,
        mark_degraded_state_fn=lambda *a, **k: None,
        get_state_fn=lambda *a, **k: {},
        update_state_fn=lambda *a, **k: None,
        generate_trace_id_fn=lambda: "t1",
    )
    _patch_prepare(monkeypatch, media_storage, stored=False)

    merged = await support._amerge_text_with_stored_media(
        "client-1", "919999999999", "t1", IMAGE_PAYLOAD
    )

    assert merged == "[image message]"


@pytest.mark.asyncio
async def test_media_backfill_skipped_when_nothing_stored(monkeypatch):
    _patch_prepare(monkeypatch, gw, stored=False)

    calls = []

    async def _attach(*args, **kwargs):
        calls.append(args)
        return True

    import fashion_bot.history.postgres_conversations as pgc

    monkeypatch.setattr(pgc, "aattach_media_to_message", _attach)

    await gw._astore_inbound_media_and_backfill(
        data=IMAGE_PAYLOAD,
        conversation_id="conv-1",
        placeholder_text="[Image message]: damaged item",
        sender_phone="919999999999",
        client_id="client-1",
        trace_id="t1",
    )

    assert calls == [], "no row rewrite when nothing was stored"
