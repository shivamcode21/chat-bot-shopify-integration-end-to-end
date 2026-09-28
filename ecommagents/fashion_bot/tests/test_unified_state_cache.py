"""
Unit tests for UnifiedStateCache - Redis-backed multi-channel state management.

Tests use mocking for Redis operations to ensure isolated, deterministic testing.

Run with: python -m pytest tests/test_unified_state_cache.py -v
"""

import json
import os
import sys
from datetime import datetime, timezone
from typing import Dict
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from fashion_bot.state_cache import (
    # Enums and dataclasses
    Channel,
    ThreadConfig,
    # Main class
    UnifiedStateCache,
    # Serialization helpers
    serialize_state,
    deserialize_state,
    serialize_message,
    deserialize_message,
    # Backward-compatible functions
    get_or_create_state,
    update_state,
    get_cache_stats,
    list_conversations,
    get_state_by_numbers,
    cleanup_expired_states,
    # Factory
    get_unified_cache,
)
from fashion_bot.schema import SupportState


# ==================== FIXTURES ====================

@pytest.fixture
def mock_redis_client():
    """Create a mock Redis client."""
    mock = MagicMock()
    mock.ping.return_value = True
    mock.get.return_value = None
    mock.setex.return_value = True
    mock.delete.return_value = 1
    mock.sadd.return_value = 1
    mock.srem.return_value = 1
    mock.smembers.return_value = set()
    mock.scard.return_value = 0
    mock.expire.return_value = True
    mock.info.return_value = {"used_memory_human": "1M"}
    return mock


@pytest.fixture
def unified_cache(mock_redis_client):
    """Create UnifiedStateCache with mocked Redis."""
    cache = UnifiedStateCache(ttl_hours=24)
    cache._redis_client = mock_redis_client
    cache._redis_initialized = True
    return cache


@pytest.fixture
def sample_state() -> SupportState:
    """Create a sample SupportState for testing."""
    return SupportState(
        messages=[
            HumanMessage(content="What is my order status?"),
            AIMessage(content="Let me check that for you."),
        ],
        phone_number="919876543210",
        gupshup_source_phone_number="15557872987",
        trace_id="trace123",
        selected_order_id="GV1234",
        last_message_at=datetime.now(timezone.utc).isoformat(),
        is_frustrated=False,
        needs_escalation=False,
    )


# ==================== CHANNEL ENUM TESTS ====================

class TestChannelEnum:
    """Test Channel enum values and properties."""
    
    def test_whatsapp_channel_value(self):
        """Test WhatsApp channel value."""
        assert Channel.WHATSAPP.value == "whatsapp"
    
    def test_web_channel_value(self):
        """Test Web channel value."""
        assert Channel.WEB.value == "web"
    
    def test_instagram_channel_value(self):
        """Test Instagram channel value."""
        assert Channel.INSTAGRAM.value == "instagram"
    
    def test_streamlit_channel_value(self):
        """Test Streamlit channel value."""
        assert Channel.STREAMLIT.value == "streamlit"
    
    def test_channel_from_string(self):
        """Test creating Channel from string value."""
        assert Channel("whatsapp") == Channel.WHATSAPP
        assert Channel("web") == Channel.WEB
        assert Channel("instagram") == Channel.INSTAGRAM
        assert Channel("streamlit") == Channel.STREAMLIT
    
    def test_invalid_channel_raises_error(self):
        """Test that invalid channel string raises ValueError."""
        with pytest.raises(ValueError):
            Channel("invalid_channel")


# ==================== THREAD CONFIG TESTS ====================

class TestThreadConfig:
    """Test ThreadConfig dataclass."""
    
    def test_thread_id_generation(self):
        """Test thread_id property generates correct format."""
        config = ThreadConfig(
            channel=Channel.WHATSAPP,
            tenant_id="15557872987",
            user_id="919876543210"
        )
        assert config.thread_id == "whatsapp:15557872987:919876543210"
    
    def test_thread_id_web_channel(self):
        """Test thread_id for web channel."""
        config = ThreadConfig(
            channel=Channel.WEB,
            tenant_id="client-uuid-123",
            user_id="session-456"
        )
        assert config.thread_id == "web:client-uuid-123:session-456"
    
    def test_from_thread_id_whatsapp(self):
        """Test parsing WhatsApp thread_id."""
        thread_id = "whatsapp:15557872987:919876543210"
        config = ThreadConfig.from_thread_id(thread_id)
        
        assert config.channel == Channel.WHATSAPP
        assert config.tenant_id == "15557872987"
        assert config.user_id == "919876543210"
    
    def test_from_thread_id_web(self):
        """Test parsing web channel thread_id."""
        thread_id = "web:client-abc:session-xyz"
        config = ThreadConfig.from_thread_id(thread_id)
        
        assert config.channel == Channel.WEB
        assert config.tenant_id == "client-abc"
        assert config.user_id == "session-xyz"
    
    def test_from_thread_id_with_colon_in_user_id(self):
        """Test parsing thread_id when user_id contains colons."""
        # User ID might contain colons (e.g., session:extra:info)
        thread_id = "web:client-abc:session:with:colons"
        config = ThreadConfig.from_thread_id(thread_id)
        
        assert config.channel == Channel.WEB
        assert config.tenant_id == "client-abc"
        assert config.user_id == "session:with:colons"
    
    def test_from_thread_id_invalid_format(self):
        """Test that invalid thread_id raises ValueError."""
        with pytest.raises(ValueError, match="Invalid thread_id format"):
            ThreadConfig.from_thread_id("invalid-no-colons")
    
    def test_from_thread_id_too_few_parts(self):
        """Test that thread_id with too few parts raises ValueError."""
        with pytest.raises(ValueError):
            ThreadConfig.from_thread_id("whatsapp:only-two")
    
    def test_roundtrip_thread_id(self):
        """Test that thread_id -> config -> thread_id preserves data."""
        original_thread_id = "instagram:business123:user456"
        config = ThreadConfig.from_thread_id(original_thread_id)
        regenerated = config.thread_id
        
        assert regenerated == original_thread_id


# ==================== SERIALIZATION TESTS ====================

class TestMessageSerialization:
    """Test message serialization/deserialization."""
    
    def test_serialize_human_message(self):
        """Test serializing HumanMessage."""
        msg = HumanMessage(content="Hello, world!")
        result = serialize_message(msg)
        
        assert result["type"] == "HumanMessage"
        assert result["content"] == "Hello, world!"
        assert "additional_kwargs" in result
    
    def test_serialize_ai_message(self):
        """Test serializing AIMessage."""
        msg = AIMessage(content="I can help with that.")
        result = serialize_message(msg)
        
        assert result["type"] == "AIMessage"
        assert result["content"] == "I can help with that."
    
    def test_serialize_system_message(self):
        """Test serializing SystemMessage."""
        msg = SystemMessage(content="You are a helpful assistant.")
        result = serialize_message(msg)
        
        assert result["type"] == "SystemMessage"
        assert result["content"] == "You are a helpful assistant."
    
    def test_serialize_message_with_additional_kwargs(self):
        """Test serializing message with additional_kwargs."""
        msg = AIMessage(content="Result", additional_kwargs={"tool_calls": []})
        result = serialize_message(msg)
        
        assert result["additional_kwargs"] == {"tool_calls": []}
    
    def test_deserialize_human_message(self):
        """Test deserializing HumanMessage."""
        msg_dict = {"type": "HumanMessage", "content": "Test", "additional_kwargs": {}}
        msg = deserialize_message(msg_dict)
        
        assert isinstance(msg, HumanMessage)
        assert msg.content == "Test"
    
    def test_deserialize_ai_message(self):
        """Test deserializing AIMessage."""
        msg_dict = {"type": "AIMessage", "content": "Response", "additional_kwargs": {}}
        msg = deserialize_message(msg_dict)
        
        assert isinstance(msg, AIMessage)
        assert msg.content == "Response"
    
    def test_deserialize_system_message(self):
        """Test deserializing SystemMessage."""
        msg_dict = {"type": "SystemMessage", "content": "System prompt", "additional_kwargs": {}}
        msg = deserialize_message(msg_dict)
        
        assert isinstance(msg, SystemMessage)
        assert msg.content == "System prompt"
    
    def test_deserialize_unknown_type_defaults_to_human(self):
        """Test that unknown message type defaults to HumanMessage."""
        msg_dict = {"type": "UnknownMessage", "content": "Unknown", "additional_kwargs": {}}
        msg = deserialize_message(msg_dict)
        
        assert isinstance(msg, HumanMessage)
        assert msg.content == "Unknown"
    
    def test_message_roundtrip(self):
        """Test serialize -> deserialize preserves message."""
        original = AIMessage(content="Test roundtrip", additional_kwargs={"key": "value"})
        serialized = serialize_message(original)
        deserialized = deserialize_message(serialized)
        
        assert isinstance(deserialized, AIMessage)
        assert deserialized.content == original.content


class TestStateSerialization:
    """Test SupportState serialization/deserialization."""
    
    def test_serialize_state_with_messages(self, sample_state):
        """Test serializing state with messages."""
        json_str = serialize_state(sample_state)
        data = json.loads(json_str)
        
        assert data["phone_number"] == "919876543210"
        assert data["trace_id"] == "trace123"
        assert len(data["messages"]) == 2
        assert data["messages"][0]["type"] == "HumanMessage"
        assert data["messages"][1]["type"] == "AIMessage"
    
    def test_serialize_state_with_none_values(self):
        """Test serializing state with None values."""
        state = SupportState(
            messages=[],
            phone_number=None,
            trace_id=None,
        )
        json_str = serialize_state(state)
        data = json.loads(json_str)
        
        assert data["phone_number"] is None
        assert data["trace_id"] is None
    
    def test_serialize_state_with_conversation_context(self):
        """Test serializing state with conversation_context."""
        state = SupportState(
            messages=[HumanMessage(content="Test")],
            phone_number="919876543210",
            conversation_context={
                "topic": "order_status",
                "topic_status": "open",
                "focal_entity": {
                    "entity_type": "order",
                    "entity_id": "GV1234",
                },
                "entities": [],
            },
        )
        json_str = serialize_state(state)
        data = json.loads(json_str)
        
        assert data["conversation_context"]["topic"] == "order_status"
        assert data["conversation_context"]["focal_entity"]["entity_id"] == "GV1234"
    
    def test_deserialize_state_with_messages(self):
        """Test deserializing state with messages."""
        json_str = json.dumps({
            "messages": [
                {"type": "HumanMessage", "content": "Hello", "additional_kwargs": {}},
                {"type": "AIMessage", "content": "Hi there!", "additional_kwargs": {}},
            ],
            "phone_number": "919876543210",
            "trace_id": "test123",
        })
        state = deserialize_state(json_str)
        
        assert len(state["messages"]) == 2
        assert isinstance(state["messages"][0], HumanMessage)
        assert isinstance(state["messages"][1], AIMessage)
        assert state["phone_number"] == "919876543210"
    
    def test_deserialize_state_invalid_json_raises_error(self):
        """Test that invalid JSON raises JSONDecodeError."""
        with pytest.raises(json.JSONDecodeError):
            deserialize_state("not valid json")
    
    def test_state_roundtrip(self, sample_state):
        """Test serialize -> deserialize preserves state."""
        json_str = serialize_state(sample_state)
        restored = deserialize_state(json_str)
        
        assert restored["phone_number"] == sample_state["phone_number"]
        assert restored["trace_id"] == sample_state["trace_id"]
        assert restored["selected_order_id"] == sample_state["selected_order_id"]
        assert len(restored["messages"]) == len(sample_state["messages"])


# ==================== UNIFIED STATE CACHE TESTS ====================

class TestPhoneNormalization:
    """Test phone number normalization."""
    
    def test_normalize_phone_with_country_code(self):
        """Test normalization removes country code prefix."""
        assert UnifiedStateCache.normalize_phone("919876543210") == "9876543210"
        assert UnifiedStateCache.normalize_phone("15557872987") == "5557872987"
    
    def test_normalize_phone_with_plus(self):
        """Test normalization handles + prefix."""
        assert UnifiedStateCache.normalize_phone("+919876543210") == "9876543210"
        assert UnifiedStateCache.normalize_phone("+15557872987") == "5557872987"
    
    def test_normalize_phone_already_10_digits(self):
        """Test normalization keeps 10-digit phone as-is."""
        assert UnifiedStateCache.normalize_phone("9876543210") == "9876543210"
    
    def test_normalize_phone_with_spaces_and_dashes(self):
        """Test normalization removes non-digit characters."""
        assert UnifiedStateCache.normalize_phone("+91 98765 43210") == "9876543210"
        assert UnifiedStateCache.normalize_phone("987-654-3210") == "9876543210"
    
    def test_normalize_phone_short_number(self):
        """Test normalization handles short numbers."""
        assert UnifiedStateCache.normalize_phone("12345") == "12345"
    
    def test_normalize_phone_empty(self):
        """Test normalization handles empty string."""
        assert UnifiedStateCache.normalize_phone("") == ""
        assert UnifiedStateCache.normalize_phone(None) is None


class TestUnifiedStateCacheThreadIdHelpers:
    """Test UnifiedStateCache thread ID helper methods."""
    
    def test_generate_thread_id_whatsapp_normalizes_phones(self, unified_cache):
        """Test thread ID generation for WhatsApp normalizes phone numbers."""
        # Input: 11-digit and 12-digit phones
        # Output: Normalized to last 10 digits
        thread_id = unified_cache.generate_thread_id(
            Channel.WHATSAPP, "15557872987", "919876543210"
        )
        assert thread_id == "whatsapp:5557872987:9876543210"
    
    def test_generate_thread_id_whatsapp_already_normalized(self, unified_cache):
        """Test thread ID generation when phones are already 10 digits."""
        thread_id = unified_cache.generate_thread_id(
            Channel.WHATSAPP, "5557872987", "9876543210"
        )
        assert thread_id == "whatsapp:5557872987:9876543210"
    
    def test_generate_thread_id_whatsapp_with_plus(self, unified_cache):
        """Test thread ID generation handles + prefix."""
        thread_id = unified_cache.generate_thread_id(
            Channel.WHATSAPP, "+15557872987", "+919876543210"
        )
        assert thread_id == "whatsapp:5557872987:9876543210"
    
    def test_generate_thread_id_web_no_normalization(self, unified_cache):
        """Test thread ID generation for web doesn't normalize (not phone numbers)."""
        thread_id = unified_cache.generate_thread_id(
            Channel.WEB, "client-abc", "session-123"
        )
        assert thread_id == "web:client-abc:session-123"
    
    def test_parse_thread_id(self, unified_cache):
        """Test parsing thread ID."""
        config = unified_cache.parse_thread_id("whatsapp:5557872987:9876543210")
        
        assert config.channel == Channel.WHATSAPP
        assert config.tenant_id == "5557872987"
        assert config.user_id == "9876543210"
    
    def test_get_state_key(self, unified_cache):
        """Test state key generation."""
        key = unified_cache._get_state_key("whatsapp:5557872987:9876543210")
        assert key == "state:whatsapp:5557872987:9876543210"
    
    def test_get_index_key_global(self, unified_cache):
        """Test global index key."""
        key = unified_cache._get_index_key()
        assert key == "thread_index:all"
    
    def test_get_index_key_by_channel(self, unified_cache):
        """Test channel-specific index key."""
        key = unified_cache._get_index_key(Channel.WHATSAPP)
        assert key == "thread_index:whatsapp"
    
    def test_get_index_key_by_channel_and_tenant(self, unified_cache):
        """Test tenant-specific index key."""
        key = unified_cache._get_index_key(Channel.WHATSAPP, "5557872987")
        assert key == "thread_index:whatsapp:5557872987"
    
    def test_get_session_phone_key(self, unified_cache):
        """Test session-phone mapping key."""
        key = unified_cache._get_session_phone_key("client-abc", "session-123")
        assert key == "session_phone:client-abc:session-123"


class TestUnifiedStateCacheGetState:
    """Test UnifiedStateCache.get_state() method."""
    
    def test_get_state_hit(self, unified_cache, mock_redis_client, sample_state):
        """Test getting state when it exists in Redis."""
        mock_redis_client.get.return_value = serialize_state(sample_state)
        
        result = unified_cache.get_state("whatsapp:15557872987:919876543210")
        
        assert result is not None
        assert result["phone_number"] == "919876543210"
        mock_redis_client.get.assert_called_once_with("state:whatsapp:15557872987:919876543210")
        mock_redis_client.expire.assert_called_once()  # TTL refresh
    
    def test_get_state_miss(self, unified_cache, mock_redis_client):
        """Test getting state when it doesn't exist."""
        mock_redis_client.get.return_value = None
        
        result = unified_cache.get_state("whatsapp:15557872987:919876543210")
        
        assert result is None
    
    def test_get_state_redis_not_available(self, unified_cache):
        """Test get_state returns None when Redis is unavailable."""
        unified_cache._redis_client = None
        
        result = unified_cache.get_state("whatsapp:15557872987:919876543210")
        
        assert result is None
    
    def test_get_state_redis_error_returns_none(self, unified_cache, mock_redis_client):
        """Test get_state handles Redis errors gracefully."""
        mock_redis_client.get.side_effect = Exception("Redis connection error")
        
        result = unified_cache.get_state("whatsapp:15557872987:919876543210")
        
        assert result is None


class TestUnifiedStateCacheSetState:
    """Test UnifiedStateCache.set_state() method."""
    
    def test_set_state_success(self, unified_cache, mock_redis_client, sample_state):
        """Test setting state successfully."""
        result = unified_cache.set_state("whatsapp:15557872987:919876543210", sample_state)
        
        assert result is True
        mock_redis_client.setex.assert_called_once()
        # Verify TTL is correct (24 hours = 86400 seconds)
        call_args = mock_redis_client.setex.call_args
        assert call_args[0][0] == "state:whatsapp:15557872987:919876543210"
        assert call_args[0][1] == 86400
    
    def test_set_state_updates_indices(self, unified_cache, mock_redis_client, sample_state):
        """Test that set_state updates thread indices."""
        unified_cache.set_state("whatsapp:15557872987:919876543210", sample_state)
        
        # Should call sadd for indices
        assert mock_redis_client.sadd.called
    
    def test_set_state_redis_not_available(self, unified_cache, sample_state):
        """Test set_state fail-open behavior when Redis is unavailable."""
        unified_cache._redis_client = None
        
        result = unified_cache.set_state("whatsapp:15557872987:919876543210", sample_state)
        
        assert result is True
    
    def test_set_state_redis_error_returns_true(self, unified_cache, mock_redis_client, sample_state):
        """Test set_state fail-open behavior on Redis write error."""
        mock_redis_client.setex.side_effect = Exception("Redis write error")
        
        result = unified_cache.set_state("whatsapp:15557872987:919876543210", sample_state)
        
        assert result is True


class TestUnifiedStateCacheDeleteState:
    """Test UnifiedStateCache.delete_state() method."""
    
    def test_delete_state_success(self, unified_cache, mock_redis_client):
        """Test deleting state successfully."""
        result = unified_cache.delete_state("whatsapp:15557872987:919876543210")
        
        assert result is True
        mock_redis_client.delete.assert_called_once_with("state:whatsapp:15557872987:919876543210")
    
    def test_delete_state_removes_from_indices(self, unified_cache, mock_redis_client):
        """Test that delete_state removes from indices."""
        unified_cache.delete_state("whatsapp:15557872987:919876543210")
        
        # Should call srem for indices
        assert mock_redis_client.srem.called
    
    def test_delete_state_redis_not_available(self, unified_cache):
        """Test delete_state fail-open behavior when Redis unavailable."""
        unified_cache._redis_client = None
        
        result = unified_cache.delete_state("whatsapp:15557872987:919876543210")
        
        assert result is True


class TestUnifiedStateCacheGetOrCreateState:
    """Test UnifiedStateCache.get_or_create_state() method."""
    
    def test_create_new_state_whatsapp(self, unified_cache, mock_redis_client):
        """Test creating new state for WhatsApp."""
        mock_redis_client.get.return_value = None
        
        # Patch where generate_trace_id is called from (inside the module's lazy import)
        with patch("fashion_bot.graph_meta.generate_trace_id", return_value="new-trace-123"):
            state, is_new = unified_cache.get_or_create_state(
                channel=Channel.WHATSAPP,
                tenant_id="15557872987",
                user_id="919876543210",
                initial_message="What is my order status?",
            )
        
        assert is_new is True
        assert state["phone_number"] == "919876543210"
        assert state["gupshup_source_phone_number"] == "15557872987"
        assert len(state["messages"]) == 1
        assert state["messages"][0].content == "What is my order status?"
    
    def test_create_new_state_web(self, unified_cache, mock_redis_client):
        """Test creating new state for web channel."""
        mock_redis_client.get.return_value = None
        
        with patch("fashion_bot.graph_meta.generate_trace_id", return_value="new-trace-456"):
            state, is_new = unified_cache.get_or_create_state(
                channel=Channel.WEB,
                tenant_id="client-abc",
                user_id="session-xyz",
                initial_message="Hello",
                client_id="client-abc",
                session_id="session-xyz",
            )
        
        assert is_new is True
        assert state.get("session_id") == "session-xyz"
        assert state["phone_number"] is None  # Not WhatsApp
    
    def test_get_existing_state(self, unified_cache, mock_redis_client, sample_state):
        """Test getting existing state."""
        mock_redis_client.get.return_value = serialize_state(sample_state)
        
        state, is_new = unified_cache.get_or_create_state(
            channel=Channel.WHATSAPP,
            tenant_id="15557872987",
            user_id="919876543210",
            initial_message="Follow up question",
        )
        
        assert is_new is False
        # Should have one more message than original
        assert len(state["messages"]) == len(sample_state["messages"]) + 1
    
    def test_force_new_creates_fresh_state(self, unified_cache, mock_redis_client, sample_state):
        """Test force_new=True creates fresh state even if one exists."""
        mock_redis_client.get.return_value = serialize_state(sample_state)
        
        with patch("fashion_bot.graph_meta.generate_trace_id", return_value="forced-new-trace"):
            state, is_new = unified_cache.get_or_create_state(
                channel=Channel.WHATSAPP,
                tenant_id="15557872987",
                user_id="919876543210",
                initial_message="Fresh start",
                force_new=True,
            )
        
        assert is_new is True
        assert state["trace_id"] == "forced-new-trace"
    
    def test_no_initial_message_creates_empty_messages(self, unified_cache, mock_redis_client):
        """Test creating state without initial message."""
        mock_redis_client.get.return_value = None
        
        with patch("fashion_bot.graph_meta.generate_trace_id", return_value="trace-no-msg"):
            state, is_new = unified_cache.get_or_create_state(
                channel=Channel.WHATSAPP,
                tenant_id="15557872987",
                user_id="919876543210",
                initial_message=None,
            )
        
        assert is_new is True
        assert state["messages"] == []


class TestUnifiedStateCacheUpdateState:
    """Test UnifiedStateCache.update_state() method."""
    
    def test_update_state_calls_set_state(self, unified_cache, mock_redis_client, sample_state):
        """Test that update_state saves to Redis."""
        sample_state["selected_order_id"] = "GV9999"
        
        unified_cache.update_state("whatsapp:15557872987:919876543210", sample_state)
        
        mock_redis_client.setex.assert_called_once()


class TestUnifiedStateCacheSessionMigration:
    """Test session-to-phone migration for web chat."""
    
    def test_migrate_session_to_phone_new_user(self, unified_cache, mock_redis_client, sample_state):
        """Test migrating session when user is new (no existing phone state)."""
        # No existing phone-based state
        mock_redis_client.get.side_effect = [serialize_state(sample_state), None]
        
        new_thread_id, was_migrated = unified_cache.migrate_session_to_phone(
            client_id="client-abc",
            session_id="session-123",
            phone_number="919876543210"
        )
        
        assert new_thread_id == "web:client-abc:919876543210"
        # Note: was_migrated depends on session state existing
    
    def test_migrate_session_to_phone_existing_user(self, unified_cache, mock_redis_client, sample_state):
        """Test migration when user already has phone-based state."""
        # Existing phone-based state found
        mock_redis_client.get.return_value = serialize_state(sample_state)
        
        new_thread_id, was_migrated = unified_cache.migrate_session_to_phone(
            client_id="client-abc",
            session_id="session-123",
            phone_number="919876543210"
        )
        
        assert new_thread_id == "web:client-abc:919876543210"
        assert was_migrated is False
    
    def test_link_session_to_phone(self, unified_cache, mock_redis_client):
        """Test linking session to phone number."""
        unified_cache.link_session_to_phone("client-abc", "session-123", "919876543210")
        
        mock_redis_client.setex.assert_called_once()
        call_args = mock_redis_client.setex.call_args
        assert call_args[0][0] == "session_phone:client-abc:session-123"
        assert call_args[0][2] == "919876543210"
    
    def test_get_phone_for_session(self, unified_cache, mock_redis_client):
        """Test getting linked phone for session."""
        mock_redis_client.get.return_value = "919876543210"
        
        phone = unified_cache.get_phone_for_session("client-abc", "session-123")
        
        assert phone == "919876543210"
        mock_redis_client.get.assert_called_with("session_phone:client-abc:session-123")
    
    def test_get_phone_for_session_not_found(self, unified_cache, mock_redis_client):
        """Test getting phone when session not linked."""
        mock_redis_client.get.return_value = None
        
        phone = unified_cache.get_phone_for_session("client-abc", "session-123")
        
        assert phone is None


class TestUnifiedStateCacheListingAndStats:
    """Test list_threads, list_conversations, and get_stats methods."""
    
    def test_list_threads_all(self, unified_cache, mock_redis_client):
        """Test listing all threads."""
        mock_redis_client.smembers.return_value = {
            "whatsapp:15557872987:919876543210",
            "web:client-abc:session-123",
        }
        
        threads = unified_cache.list_threads()
        
        assert len(threads) == 2
        mock_redis_client.smembers.assert_called_with("thread_index:all")
    
    def test_list_threads_by_channel(self, unified_cache, mock_redis_client):
        """Test listing threads by channel."""
        mock_redis_client.smembers.return_value = {"whatsapp:15557872987:919876543210"}
        
        threads = unified_cache.list_threads(channel=Channel.WHATSAPP)
        
        assert len(threads) == 1
        mock_redis_client.smembers.assert_called_with("thread_index:whatsapp")
    
    def test_list_threads_by_channel_and_tenant(self, unified_cache, mock_redis_client):
        """Test listing threads by channel and tenant."""
        # User IDs returned from Redis are already normalized (last 10 digits stored)
        mock_redis_client.smembers.return_value = {"9876543210", "8765432109"}
        
        threads = unified_cache.list_threads(
            channel=Channel.WHATSAPP,
            tenant_id="15557872987"  # Will be normalized to 5557872987
        )
        
        assert len(threads) == 2
        # Should construct full thread_ids with normalized tenant_id
        assert "whatsapp:5557872987:9876543210" in threads
        assert "whatsapp:5557872987:8765432109" in threads
    
    def test_list_conversations(self, unified_cache, mock_redis_client, sample_state):
        """Test listing conversations with details."""
        # Thread IDs in Redis index are already normalized (phone numbers are last 10 digits)
        mock_redis_client.smembers.return_value = {"whatsapp:5557872987:9876543210"}
        mock_redis_client.get.return_value = serialize_state(sample_state)
        
        conversations = unified_cache.list_conversations()
        
        assert len(conversations) == 1
        conv = conversations[0]
        assert conv["thread_id"] == "whatsapp:5557872987:9876543210"
        assert conv["channel"] == "whatsapp"
        # user_id is extracted from thread_id, which is already normalized
        assert conv["user_id"] == "9876543210"
    
    def test_get_stats(self, unified_cache, mock_redis_client):
        """Test getting cache statistics."""
        mock_redis_client.scard.return_value = 5
        mock_redis_client.info.return_value = {"used_memory_human": "2M"}
        
        stats = unified_cache.get_stats()
        
        assert stats["redis_connected"] is True
        assert stats["total_threads"] == 5
        assert stats["memory_used"] == "2M"
        assert "threads_by_channel" in stats
    
    def test_get_stats_redis_not_available(self, unified_cache):
        """Test get_stats when Redis is unavailable."""
        unified_cache._redis_client = None
        
        stats = unified_cache.get_stats()
        
        assert stats["redis_connected"] is False
        assert stats["total_threads"] == 0


# ==================== BACKWARD COMPATIBLE FUNCTION TESTS ====================

class TestBackwardCompatibleFunctions:
    """Test backward-compatible API functions."""
    
    @patch("fashion_bot.config_manager.get_default_client_id")
    @patch("fashion_bot.state_cache.get_unified_cache")
    def test_get_or_create_state_backward_compat(self, mock_get_cache, mock_get_client_id):
        """Test backward-compatible get_or_create_state."""
        mock_get_client_id.return_value = "default-client"
        mock_cache = MagicMock()
        mock_cache.get_or_create_state.return_value = (SupportState(messages=[]), True)
        mock_get_cache.return_value = mock_cache
        
        state, is_new = get_or_create_state(
            from_phone_number="919876543210",
            to_phone_number="15557872987",
            question="Test question",
        )
        
        mock_cache.get_or_create_state.assert_called_once()
        call_kwargs = mock_cache.get_or_create_state.call_args[1]
        assert call_kwargs["channel"] == Channel.WHATSAPP
        assert call_kwargs["tenant_id"] == "15557872987"
        assert call_kwargs["user_id"] == "919876543210"
    
    @patch("fashion_bot.state_cache.get_unified_cache")
    def test_update_state_backward_compat(self, mock_get_cache):
        """Test backward-compatible update_state."""
        mock_cache = MagicMock()
        # Make generate_thread_id return a real string (with normalized phones)
        mock_cache.generate_thread_id.return_value = "whatsapp:5557872987:9876543210"
        mock_get_cache.return_value = mock_cache
        
        state = SupportState(messages=[], selected_order_id="GV123")
        update_state("919876543210", "15557872987", state)
        
        mock_cache.update_state.assert_called_once()
        call_args = mock_cache.update_state.call_args[0]
        assert call_args[0] == "whatsapp:5557872987:9876543210"
    
    @patch("fashion_bot.state_cache.get_unified_cache")
    def test_get_state_by_numbers_backward_compat(self, mock_get_cache):
        """Test backward-compatible get_state_by_numbers."""
        mock_cache = MagicMock()
        # Make generate_thread_id return a real string (with normalized phones)
        mock_cache.generate_thread_id.return_value = "whatsapp:5557872987:9876543210"
        mock_cache.get_state.return_value = SupportState(messages=[])
        mock_get_cache.return_value = mock_cache
        
        result = get_state_by_numbers("919876543210", "15557872987")
        
        mock_cache.get_state.assert_called_once_with("whatsapp:5557872987:9876543210")
    
    @patch("fashion_bot.state_cache.get_unified_cache")
    def test_get_cache_stats_backward_compat(self, mock_get_cache):
        """Test backward-compatible get_cache_stats."""
        mock_cache = MagicMock()
        mock_cache.get_stats.return_value = {"total_threads": 10, "redis_connected": True}
        mock_get_cache.return_value = mock_cache
        
        stats = get_cache_stats()
        
        # Should add backward-compatible fields
        assert stats["total_conversations"] == 10
        assert stats["redis_state_count"] == 10
    
    @patch("fashion_bot.state_cache.get_unified_cache")
    def test_list_conversations_backward_compat(self, mock_get_cache):
        """Test backward-compatible list_conversations."""
        mock_cache = MagicMock()
        mock_cache.list_conversations.return_value = [{"thread_id": "test"}]
        mock_get_cache.return_value = mock_cache
        
        result = list_conversations()
        
        mock_cache.list_conversations.assert_called_once()
        assert len(result) == 1
    
    def test_cleanup_expired_states_is_noop(self):
        """Test that cleanup_expired_states is now a no-op."""
        # Should return 0 and not raise errors
        result = cleanup_expired_states()
        assert result == 0


# ==================== GLOBAL CACHE INSTANCE TESTS ====================

class TestGlobalCacheInstance:
    """Test global cache instance management."""
    
    def test_get_unified_cache_returns_instance(self):
        """Test that get_unified_cache returns an instance."""
        cache = get_unified_cache()
        assert isinstance(cache, UnifiedStateCache)
    
    def test_get_unified_cache_returns_same_instance(self):
        """Test that get_unified_cache returns the same singleton."""
        cache1 = get_unified_cache()
        cache2 = get_unified_cache()
        assert cache1 is cache2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
