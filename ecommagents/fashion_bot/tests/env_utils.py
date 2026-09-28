#!/usr/bin/env python3
"""
Environment utilities for Fashion Bot testing
"""
import os
import sys
from typing import Optional


def setup_environment():
    """
    Set up the environment for testing by:
    1. Adding parent directory to Python path
    2. Loading environment variables from .env file
    3. Disabling LangSmith tracing for tests
    """
    # Add the parent directory to the path to import fashion_bot modules
    parent_dir = os.path.join(os.path.dirname(__file__), '..')
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    
    # Load environment variables from .env file
    try:
        # Load .env file from the fashion_bot directory
        from fashion_bot.langsmith_config import get_current_environment
        from pathlib import Path
        from dotenv import load_dotenv
    
        current_env = get_current_environment()
        if current_env == "development":
            # Load environment variables from .env file for local development
            env_path = Path(__file__).resolve().parent / ".env"
            load_dotenv(dotenv_path=env_path)
        else:
            print(f"🚀 {current_env.title()} mode: Using system environment variables")
        
        # ALWAYS DISABLE LANGSMITH TRACING FOR TESTS
        # This prevents sending test data to LangSmith
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        os.environ["LANGSMITH_TRACING_V2"] = "false"
        os.environ.pop("LANGCHAIN_PROJECT", None)
        os.environ.pop("LANGSMITH_PROJECT", None)
        
        return True
    except ImportError:
        print("⚠️  python-dotenv not installed. Using system environment variables.")
        return False
    except FileNotFoundError:
        print("⚠️  .env file not found. Using system environment variables.")
        return False


def get_api_keys() -> tuple[Optional[str], Optional[str]]:
    """
    Get API keys from environment variables
    
    Returns:
        tuple: (openai_api_key, langsmith_api_key)
    """
    openai_key = os.getenv("OPENAI_API_KEY")
    langsmith_key = os.getenv("LANGSMITH_API_KEY")
    
    return openai_key, langsmith_key


def validate_environment() -> bool:
    """
    Validate that required environment variables are set
    
    Returns:
        bool: True if environment is valid, False otherwise
    """
    openai_key, langsmith_key = get_api_keys()
    
    if not openai_key:
        print("❌ ERROR: OPENAI_API_KEY not found in environment variables")
        print("   Make sure your .env file contains: OPENAI_API_KEY=your_key_here")
        return False
    else:
        print(f"✅ OpenAI API key found: {openai_key[:10]}...")
    
    if not langsmith_key:
        print("⚠️  WARNING: LANGSMITH_API_KEY not found. LangSmith integration will be disabled.")
        print("   To enable LangSmith, add LANGSMITH_API_KEY=your_key_here to your .env file")
    else:
        print(f"✅ LangSmith API key found: {langsmith_key[:10]}...")
    
    return True


def print_environment_status():
    """Print the current environment status"""
    openai_key, langsmith_key = get_api_keys()
    
    print("🔧 Environment Status:")
    print(f"   OpenAI API Key: {'✅ Set' if openai_key else '❌ Not set'}")
    print(f"   LangSmith API Key: {'✅ Set' if langsmith_key else '⚠️  Not set'}")
    
    if openai_key:
        print(f"   OpenAI Key Preview: {openai_key[:10]}...")
    if langsmith_key:
        print(f"   LangSmith Key Preview: {langsmith_key[:10]}...")


# Auto-setup when module is imported
setup_environment() 