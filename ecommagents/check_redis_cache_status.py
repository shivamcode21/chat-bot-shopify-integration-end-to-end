#!/usr/bin/env python3
"""
Redis cache status checker for agents_config
"""

import sys
import os
import json
from dotenv import load_dotenv

# Load environment variables
load_dotenv(os.path.join(os.path.dirname(__file__), 'fashion_bot', 'fashion_bot', '.env'))

sys.path.append(os.path.join(os.path.dirname(__file__), 'fashion_bot'))

from fashion_bot.utils.utils import _get_redis_client
from fashion_bot.config_manager import get_default_client_id

def check_redis_cache_status():
    """Check Redis cache status for agents_config"""
    
    print("🔍 Redis Cache Status Check")
    print("=" * 50)
    
    # Get Redis client
    client = _get_redis_client()
    if not client:
        print("❌ Redis client not available")
        return
    
    print("✅ Redis client connected")
    
    try:
        # Get client_id
        client_id = get_default_client_id()
        print(f"📋 Client ID: {client_id}")
        
        # Check cache key
        cache_key = f"agents_config:{client_id}"
        print(f"🔑 Cache key: {cache_key}")
        
        # Check if key exists
        exists = client.exists(cache_key)
        print(f"📦 Key exists: {exists}")
        
        if exists:
            # Get TTL
            ttl = client.ttl(cache_key)
            print(f"⏰ TTL (seconds): {ttl}")
            
            # Get cached data
            cached_data = client.get(cache_key)
            if cached_data:
                try:
                    agents_config = json.loads(cached_data)
                    print(f"📊 Cached agents count: {len(agents_config)}")
                    
                    # Show agent names
                    agent_names = [agent.get('agent_name') for agent in agents_config]
                    print(f"🏷️  Agent names: {agent_names}")
                    
                    # Check for cancellation_handler specifically
                    cancellation_agent = next(
                        (agent for agent in agents_config if agent.get('agent_name') == 'cancellation_handler'),
                        None
                    )
                    if cancellation_agent:
                        prompt_length = len(cancellation_agent.get('agent_prompt', ''))
                        print(f"🚫 Cancellation handler prompt length: {prompt_length} chars")
                    else:
                        print("⚠️ Cancellation handler not found in cache")
                        
                except json.JSONDecodeError as e:
                    print(f"❌ JSON decode error: {e}")
            else:
                print("❌ No cached data found")
        else:
            print("📭 No cache data exists")
            
        # Additional Redis info
        info = client.info('memory')
        used_memory = info.get('used_memory_human', 'unknown')
        print(f"💾 Redis memory usage: {used_memory}")
        
    except Exception as e:
        print(f"❌ Error checking cache status: {e}")
    
    print("\n" + "=" * 50)
    print("✅ Cache status check completed")

if __name__ == "__main__":
    check_redis_cache_status()