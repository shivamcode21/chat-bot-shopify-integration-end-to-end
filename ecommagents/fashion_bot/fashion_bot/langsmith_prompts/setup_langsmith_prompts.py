#!/usr/bin/env python3
"""
Setup script for LangSmith prompts in Fashion Bot

This script helps you set up prompts in LangSmith cloud for the fashion bot using the client API.
"""

import os
import sys
from pathlib import Path

# Add the fashion_bot directory to the path
sys.path.append(str(Path(__file__).parent))

from fashion_bot.fashion_bot.langsmith_integration.langsmith_prompts import setup_langsmith_prompts, prompt_manager, get_environment_info


def main():
    """Main setup function"""
    print("🚀 Fashion Bot LangSmith Prompt Setup")
    print("=" * 50)
    
    # Check if LangSmith API key is available
    api_key = os.getenv("LANGSMITH_API_KEY")
    if not api_key:
        print("❌ LANGSMITH_API_KEY not found in environment variables")
        print("\nTo set up LangSmith prompts:")
        print("1. Get your LangSmith API key from https://smith.langchain.com/")
        print("2. Set the environment variable:")
        print("   export LANGSMITH_API_KEY='your-api-key-here'")
        print("3. Run this script again")
        return
    
    print(f"✅ LangSmith API key found: {api_key[:10]}...")
    
    # Get environment information
    env_info = get_environment_info()
    print(f"🔧 Environment: {env_info['environment']}")
    print(f"🏷️  Tag: {env_info['tag']}")
    print(f"👤 Handle: {env_info['handle']}")
    print()
    
    # Setup prompts
    setup_langsmith_prompts()
    
    print("\n" + "=" * 50)
    print("📋 Next Steps:")
    print("1. Go to https://smith.langchain.com/prompts")
    print("2. Create prompts with the templates shown above")
    print("3. Use appropriate tags for different environments")
    print("4. Set ENVIRONMENT variable in your app")
    print("5. Your fashion bot will automatically fetch the right prompt!")
    
    print("\n🔧 Environment Configuration:")
    print("Set one of these environment variables:")
    print("   ENVIRONMENT=development  # Uses @dev tag")
    print("   ENVIRONMENT=staging      # Uses @staging tag")
    print("   ENVIRONMENT=production   # Uses @prod tag")
    
    print("\n🎯 Benefits of using LangSmith client API:")
    print("- Automatic environment-based tag selection")
    print("- Version control with tags")
    print("- Easy rollback to previous versions")
    print("- Centralized prompt management")
    print("- Performance monitoring and analytics")


if __name__ == "__main__":
    main() 