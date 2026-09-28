#!/usr/bin/env python3
"""
Test script for LangSmith prompt integration using client API

This script tests that the LangSmith prompt fetching works correctly with environment-based tags.
"""

import pytest

# Legacy manual integration script; not suitable for automated CI collection.
pytest.skip("Legacy LangSmith integration script; skip in automated pytest runs.", allow_module_level=True)

import os
import sys
from pathlib import Path

# Add the fashion_bot directory to the path
sys.path.append(str(Path(__file__).parent))

from fashion_bot.fashion_bot.langsmith_integration.langsmith_prompts import fetch_langsmith_prompt, prompt_manager, get_environment_info
from fashion_bot.nodes import build_smart_prompt


def test_langsmith_integration():
    """Test the LangSmith prompt integration"""
    print("🧪 Testing LangSmith Prompt Integration")
    print("=" * 50)
    
    # Test 1: Check if LangSmith API key is available
    api_key = os.getenv("LANGSMITH_API_KEY")
    if api_key:
        print(f"✅ LangSmith API key found: {api_key[:10]}...")
    else:
        print("⚠️  No LangSmith API key found - will use fallback prompts")
    
    # Test 2: Check environment configuration
    env_info = get_environment_info()
    print(f"🔧 Environment: {env_info['environment']}")
    print(f"🏷️  Tag: {env_info['tag']}")
    print(f"👤 Handle: {env_info['handle']}")
    
    # Test 3: Test prompt fetching
    print("\n🔍 Testing prompt fetching...")
    try:
        prompt_template = fetch_langsmith_prompt("fashion_bot_support_prompt")
        print("✅ Successfully fetched prompt template")
        print(f"📝 Template length: {len(prompt_template)} characters")
    except Exception as e:
        print(f"❌ Error fetching prompt: {e}")
        return False
    
    # Test 4: Test prompt formatting
    print("\n🔧 Testing prompt formatting...")
    try:
        # Create a mock state
        mock_state = {
            "messages": [
                type('Message', (), {'type': 'human', 'content': 'Hello, I need help with my order'})()
            ],
            "product_info": "Sizes: S, M, L. Colors: Red, Blue. In stock: Yes.",
            "selected_order_id": "ORD123",
            "order_status_by_id": {"ORD123": "Shipped"}
        }
        
        formatted_prompt = build_smart_prompt(mock_state)
        print("✅ Successfully formatted prompt with context")
        print(f"📝 Formatted prompt length: {len(formatted_prompt)} characters")
        
        # Check if the prompt contains expected elements
        if "fashion store" in formatted_prompt.lower():
            print("✅ Prompt contains expected content")
        else:
            print("⚠️  Prompt may not contain expected content")
            
    except Exception as e:
        print(f"❌ Error formatting prompt: {e}")
        return False
    
    # Test 5: Test environment-specific tag fetching
    print("\n🏷️  Testing environment-specific tag fetching...")
    try:
        tag = env_info['tag']
        print(f"🔍 Testing tag: {tag}")
        
        # Test fetching with specific tag
        tagged_prompt = fetch_langsmith_prompt("fashion_bot_support_prompt", tag=tag)
        print(f"✅ Successfully fetched prompt with tag '{tag}'")
        print(f"📝 Tagged prompt length: {len(tagged_prompt)} characters")
        
    except Exception as e:
        print(f"❌ Error testing tagged prompt: {e}")
        return False
    
    # Test 6: Test fallback behavior
    print("\n🔄 Testing fallback behavior...")
    try:
        # Temporarily remove API key to test fallback
        original_key = os.environ.get("LANGSMITH_API_KEY")
        if original_key:
            del os.environ["LANGSMITH_API_KEY"]
        
        # Create a new prompt manager without API key
        fallback_manager = prompt_manager.__class__()
        fallback_prompt = fallback_manager.get_fashion_bot_prompt()
        
        print("✅ Fallback prompt working correctly")
        
        # Restore API key
        if original_key:
            os.environ["LANGSMITH_API_KEY"] = original_key
            
    except Exception as e:
        print(f"❌ Error testing fallback: {e}")
        return False
    
    print("\n🎉 All tests passed!")
    print("✅ LangSmith prompt integration is working correctly")
    return True


def test_environment_specific_prompts():
    """Test environment-specific prompt fetching"""
    print("\n🧪 Testing Environment-Specific Prompts")
    print("=" * 50)
    
    # Test different environments
    environments = ["development", "staging", "production"]
    
    for env in environments:
        print(f"\n🔧 Testing {env} environment...")
        
        # Set environment
        original_env = os.environ.get("ENVIRONMENT")
        os.environ["ENVIRONMENT"] = env
        
        try:
            # Create a new prompt manager for this environment
            env_manager = prompt_manager.__class__()
            prompt_template = env_manager.get_fashion_bot_prompt()
            
            print(f"✅ {env} environment prompt fetched successfully")
            print(f"📝 Template length: {len(prompt_template)} characters")
            
            # Show the tag that would be used
            tag = env_manager.get_tag_from_environment()
            print(f"🏷️  Would fetch with tag: {tag}")
                
        except Exception as e:
            print(f"❌ Error testing {env} environment: {e}")
        finally:
            # Restore original environment
            if original_env:
                os.environ["ENVIRONMENT"] = original_env
            elif "ENVIRONMENT" in os.environ:
                del os.environ["ENVIRONMENT"]


def test_tag_specific_fetching():
    """Test fetching prompts with specific tags"""
    print("\n🏷️  Testing Tag-Specific Prompt Fetching")
    print("=" * 50)
    
    tags = ["dev", "staging", "prod"]
    
    for tag in tags:
        print(f"\n🔍 Testing tag '{tag}'...")
        try:
            prompt_template = fetch_langsmith_prompt("fashion_bot_support_prompt", tag=tag)
            print(f"✅ Successfully fetched prompt with tag '{tag}'")
            print(f"📝 Template length: {len(prompt_template)} characters")
        except Exception as e:
            print(f"❌ Error fetching prompt with tag '{tag}': {e}")


def show_prompt_template():
    """Show the current prompt template"""
    print("\n📋 Current Prompt Template:")
    print("=" * 50)
    
    template = fetch_langsmith_prompt("fashion_bot_support_prompt")
    print(template)
    
    print("\n🔍 Template Variables:")
    print("- {product_info}: Product availability and details")
    print("- {order_info}: Current order status information")
    print("- {history}: Recent conversation history")


def show_environment_info():
    """Show current environment configuration"""
    print("\n🔧 Environment Configuration:")
    print("=" * 50)
    
    env_info = get_environment_info()
    for key, value in env_info.items():
        print(f"   {key}: {value}")
    
    # Show what would be pulled
    handle = env_info['handle']
    tag = env_info['tag']
    pull_string = f"{handle}/fashion_bot_support_prompt@{tag}"
    print(f"\n🎯 Current pull string: {pull_string}")


def main():
    """Main test function"""
    print("🚀 Fashion Bot LangSmith Integration Test")
    print("=" * 50)
    
    # Show environment info
    show_environment_info()
    
    # Run tests
    success = test_langsmith_integration()
    
    if success:
        # Run additional tests
        test_environment_specific_prompts()
        test_tag_specific_fetching()
        
        # Show the prompt template
        show_prompt_template()
        
        print("\n" + "=" * 50)
        print("✅ Integration test completed successfully!")
        print("\nNext steps:")
        print("1. Your fashion bot is ready to use LangSmith client API")
        print("2. Create prompts in LangSmith UI with appropriate tags")
        print("3. Set ENVIRONMENT variable to control which tag is used")
        print("4. Monitor performance in LangSmith dashboard")
    else:
        print("\n❌ Integration test failed!")
        print("Please check the error messages above and fix any issues.")


if __name__ == "__main__":
    main() 
