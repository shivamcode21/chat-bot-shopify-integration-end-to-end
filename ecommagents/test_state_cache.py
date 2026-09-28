#!/usr/bin/env python3
"""
Test file for state_cache.py functionality.

This test file verifies:
1. State creation and retrieval
2. Conversation history maintenance
3. Unified thread_id pattern
4. Multi-channel support (WhatsApp, Web)
5. Cache statistics
"""

import sys
import os
from datetime import datetime, timedelta
from langchain_core.messages import HumanMessage, AIMessage

# Add the fashion_bot directory to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'fashion_bot'))

from fashion_bot.state_cache import (
    get_or_create_state, 
    update_state, 
    cleanup_expired_states, 
    get_cache_stats,
    get_unified_cache,
    UnifiedStateCache,
    Channel
)

def test_basic_state_creation():
    """Test basic state creation and retrieval."""
    print("🧪 Testing basic state creation...")
    
    from_phone = "919876543210"
    to_phone = "15557872987"
    question1 = "Hello, I need help with my order"
    product_info = "Test Product"
    
    # Create initial state
    state1, is_new1 = get_or_create_state(from_phone, to_phone, question1, product_info)
    
    print(f"✅ First call - Created new state: {is_new1}")
    print(f"✅ State has {len(state1['messages'])} messages")
    print(f"✅ First message: {state1['messages'][0].content}")
    
    assert is_new1 == True, "First call should create new state"
    assert len(state1['messages']) == 1, "Should have one message"
    assert state1['messages'][0].content == question1, "Message content should match"
    assert state1['phone_number'] == from_phone, "Phone number should match"
    assert state1['product_info'] == product_info, "Product info should match"

def test_state_retrieval_and_conversation_history():
    """Test that we get the same state and conversation history is maintained."""
    print("\n🧪 Testing state retrieval and conversation history...")
    
    from_phone = "919876543210"
    to_phone = "15557872987"
    question2 = "What is the status of order #gv12345?"
    product_info = "Test Product"
    
    # Get state again (should be same as before)
    state2, is_new2 = get_or_create_state(from_phone, to_phone, question2, product_info)
    
    print(f"✅ Second call - Created new state: {is_new2}")
    print(f"✅ State now has {len(state2['messages'])} messages")
    print(f"✅ Messages:")
    for i, msg in enumerate(state2['messages']):
        print(f"    {i+1}. {msg.content}")
    
    assert is_new2 == False, "Second call should retrieve existing state"
    assert len(state2['messages']) == 2, "Should have two messages now"
    assert state2['messages'][1].content == question2, "Second message should match"

def test_multiple_customers_same_business():
    """Test that different customers to the same business get separate states."""
    print("\n🧪 Testing multiple customers to same business...")
    
    to_phone = "15557872987"  # Same business
    from_phone1 = "919111111111"  # Customer 1
    from_phone2 = "919222222222"  # Customer 2
    
    question1 = "Customer 1 message"
    question2 = "Customer 2 message"
    product_info = "Test Product"
    
    # Create states for both customers
    state1, is_new1 = get_or_create_state(from_phone1, to_phone, question1, product_info)
    state2, is_new2 = get_or_create_state(from_phone2, to_phone, question2, product_info)
    
    print(f"✅ Customer 1 - New state: {is_new1}, Messages: {len(state1['messages'])}")
    print(f"✅ Customer 2 - New state: {is_new2}, Messages: {len(state2['messages'])}")
    
    assert is_new1 == True, "Customer 1 should get new state"
    assert is_new2 == True, "Customer 2 should get new state"
    assert state1['messages'][0].content == question1, "Customer 1 message should be separate"
    assert state2['messages'][0].content == question2, "Customer 2 message should be separate"

def test_same_customer_multiple_businesses():
    """Test that same customer to different businesses get separate states."""
    print("\n🧪 Testing same customer to multiple businesses...")
    
    from_phone = "919333333333"  # Same customer
    to_phone1 = "15551111111"    # Business 1
    to_phone2 = "15552222222"    # Business 2
    
    question1 = "Message to business 1"
    question2 = "Message to business 2"
    product_info = "Test Product"
    
    # Create states for both businesses
    state1, is_new1 = get_or_create_state(from_phone, to_phone1, question1, product_info)
    state2, is_new2 = get_or_create_state(from_phone, to_phone2, question2, product_info)
    
    print(f"✅ Business 1 - New state: {is_new1}, Messages: {len(state1['messages'])}")
    print(f"✅ Business 2 - New state: {is_new2}, Messages: {len(state2['messages'])}")
    
    assert is_new1 == True, "Business 1 conversation should get new state"
    assert is_new2 == True, "Business 2 conversation should get new state"
    assert state1['messages'][0].content == question1, "Business 1 message should be separate"
    assert state2['messages'][0].content == question2, "Business 2 message should be separate"

def test_state_update():
    """Test state update functionality."""
    print("\n🧪 Testing state update...")
    
    from_phone = "919444444444"
    to_phone = "15557872987"
    question = "Test update"
    product_info = "Test Product"
    
    # Create initial state
    state, is_new = get_or_create_state(from_phone, to_phone, question, product_info)
    print(f"✅ Initial state - Messages: {len(state['messages'])}")
    
    # Add an AI response to simulate processing
    state['messages'].append(AIMessage(content="Bot response to your query"))
    state['scratchpad'] = "Updated by bot processing"
    
    # Update the cache
    update_state(from_phone, to_phone, state)
    
    # Retrieve again to verify update
    updated_state, is_new2 = get_or_create_state(from_phone, to_phone, "Follow up question", product_info)
    
    print(f"✅ After update - Messages: {len(updated_state['messages'])}")
    print(f"✅ Scratchpad: {updated_state.get('scratchpad', 'None')}")
    
    assert is_new2 == False, "Should retrieve existing state"
    assert len(updated_state['messages']) == 3, "Should have 3 messages (original + bot + new)"
    assert updated_state.get('scratchpad') == "Updated by bot processing", "Scratchpad should be updated"

def test_cache_statistics():
    """Test cache statistics functionality."""
    print("\n🧪 Testing cache statistics...")
    
    stats = get_cache_stats()
    print(f"✅ Cache Statistics:")
    print(f"    Redis connected: {stats.get('redis_connected')}")
    print(f"    Total threads: {stats.get('total_threads')}")
    print(f"    Total conversations: {stats.get('total_conversations')}")
    print(f"    TTL hours: {stats.get('ttl_hours')}")
    print(f"    Threads by channel: {stats.get('threads_by_channel', {})}")
    
    # Note: With Redis-backed cache, stats depend on Redis connection
    if stats.get('redis_connected'):
        print("    ✅ Redis connection verified")
    else:
        print("    ⚠️ Redis not connected (using in-memory fallback)")
    
    assert 'total_conversations' in stats, "Should have total_conversations field"
    assert 'ttl_hours' in stats, "Should have ttl_hours field"

def test_custom_cache_instance():
    """Test custom cache instance with different expiry."""
    print("\n🧪 Testing custom cache instance (now uses UnifiedStateCache)...")
    
    # Create custom cache with 1 hour expiry
    custom_cache = UnifiedStateCache(ttl_hours=1)
    
    from_phone = "919555555555"
    to_phone = "15557872987"
    question = "Custom cache test"
    
    # Create state in custom cache using new API
    state, is_new = custom_cache.get_or_create_state(
        channel=Channel.WHATSAPP,
        tenant_id=to_phone,
        user_id=from_phone,
        initial_message=question
    )
    
    print(f"✅ Custom cache - New state: {is_new}")
    print(f"✅ Custom cache stats: {custom_cache.get_stats()}")
    
    assert is_new == True, "Should create new state in custom cache"
    assert custom_cache.ttl_hours == 1, "Custom TTL should be 1 hour"

def test_web_chat_session():
    """Test web chat session management (new unified cache feature)."""
    print("\n🧪 Testing web chat session management...")
    
    cache = get_unified_cache()
    
    client_id = "test-client-uuid"
    session_id = "test-session-uuid"
    phone = "919777777777"
    
    # Create anonymous session (no phone)
    state1, is_new1 = cache.get_or_create_state(
        channel=Channel.WEB,
        tenant_id=client_id,
        user_id=session_id,
        initial_message="Hello from web chat",
        session_id=session_id
    )
    
    print(f"✅ Anonymous session created: {is_new1}")
    
    # Migrate session to phone
    new_thread_id, was_migrated = cache.migrate_session_to_phone(
        client_id=client_id,
        session_id=session_id,
        phone_number=phone
    )
    
    print(f"✅ Session migrated to phone: {was_migrated}")
    print(f"✅ New thread_id: {new_thread_id}")
    
    # Verify phone was linked
    linked_phone = cache.get_phone_for_session(client_id, session_id)
    print(f"✅ Linked phone lookup: {linked_phone}")
    
    assert is_new1 == True, "Should create new session"
    assert "web:" in new_thread_id, "Thread ID should contain channel prefix"

def test_thread_id_generation():
    """Test thread ID generation for different channels."""
    print("\n🧪 Testing thread ID generation...")
    
    cache = get_unified_cache()
    
    # WhatsApp thread ID
    whatsapp_thread = cache.generate_thread_id(Channel.WHATSAPP, "15557872987", "919876543210")
    print(f"✅ WhatsApp thread ID: {whatsapp_thread}")
    assert whatsapp_thread == "whatsapp:15557872987:919876543210"
    
    # Web thread ID
    web_thread = cache.generate_thread_id(Channel.WEB, "client-uuid", "session-uuid")
    print(f"✅ Web thread ID: {web_thread}")
    assert web_thread == "web:client-uuid:session-uuid"
    
    # Instagram thread ID
    ig_thread = cache.generate_thread_id(Channel.INSTAGRAM, "ig-business-id", "ig-user-id")
    print(f"✅ Instagram thread ID: {ig_thread}")
    assert ig_thread == "instagram:ig-business-id:ig-user-id"

def test_cleanup_functionality():
    """Test cleanup functionality."""
    print("\n🧪 Testing cleanup functionality...")
    
    initial_stats = get_cache_stats()
    print(f"✅ Initial conversations: {initial_stats['total_conversations']}")
    
    # Run cleanup
    cleaned_count = cleanup_expired_states()
    print(f"✅ Cleaned up {cleaned_count} expired states")
    
    final_stats = get_cache_stats()
    print(f"✅ Final conversations: {final_stats['total_conversations']}")

def run_all_tests():
    """Run all tests."""
    print("🚀 Starting State Cache Tests...")
    print("=" * 50)
    
    try:
        test_basic_state_creation()
        test_state_retrieval_and_conversation_history()
        test_multiple_customers_same_business()
        test_same_customer_multiple_businesses()
        test_state_update()
        test_cache_statistics()
        test_custom_cache_instance()
        test_web_chat_session()
        test_thread_id_generation()
        test_cleanup_functionality()
        
        print("\n" + "=" * 50)
        print("🎉 All tests passed successfully!")
        
        # Final cache stats
        final_stats = get_cache_stats()
        print(f"\n📊 Final Cache Statistics:")
        for key, value in final_stats.items():
            print(f"    {key}: {value}")
            
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    return True

if __name__ == "__main__":
    success = run_all_tests()
    exit(0 if success else 1) 