"""
LangSmith Prompt Management for Fashion Bot

This module handles fetching and managing prompts from LangSmith cloud using the client API.
"""

import os
from typing import Optional
from langsmith import Client


class LangSmithPromptManager:
    """Manages prompts stored in LangSmith cloud using client API"""
    
    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize the LangSmith prompt manager.
        
        Args:
            api_key: LangSmith API key. If None, will try to get from environment.
        """
        self.api_key = api_key or os.getenv("LANGSMITH_API_KEY")
        self.environment = os.getenv("ENVIRONMENT", "production").lower()
        self.client = Client(api_key=self.api_key) if self.api_key else None
        
        # Map environment to tag
        self.env_to_tag = {
            "development": "dev",
            "dev": "dev", 
            "staging": "staging",
            "production": "prod",
            "prod": "prod"
        }
        
    def get_tag_from_environment(self) -> str:
        """
        Get the appropriate tag based on current environment.
        
        Returns:
            Tag string (dev, staging, prod)
        """
        return self.env_to_tag.get(self.environment, "prod")
    
    def fetch_prompt_template(self, prompt_name: str, tag: Optional[str] = None) -> Optional[str]:
        """
        Fetch a prompt template from LangSmith using client API.
        
        Args:
            prompt_name: Name of the prompt in LangSmith
            tag: Optional tag to override environment-based tag
            
        Returns:
            The prompt template string or None if not found
        """
        if not self.client:
            print("⚠️  LangSmith client not initialized. Check LANGSMITH_API_KEY.")
            return None
            
        try:
            # Use environment-based tag if not specified
            if not tag:
                tag = self.get_tag_from_environment()
            
            print(f"🔍 Fetching prompt '{prompt_name}' with tag '{tag}'")
            
            # Get all prompts and filter by name and tag
            prompts = self.client.list_prompts()
            matching_prompts = []
            
            for prompt in prompts:
                if prompt.name == prompt_name:
                    # Check if prompt has the specified tag
                    prompt_tags = getattr(prompt, 'tags', []) or []
                    if tag in prompt_tags:
                        matching_prompts.append(prompt)
            
            if matching_prompts:
                # Sort by creation date to get the latest
                latest_prompt = sorted(matching_prompts, key=lambda p: p.created_at, reverse=True)[0]
                print(f"✅ Successfully fetched prompt '{prompt_name}' with tag '{tag}' (created: {latest_prompt.created_at})")
                return latest_prompt.prompt.template
            else:
                print(f"❌ Prompt '{prompt_name}' with tag '{tag}' not found in LangSmith")
                return None
                
        except Exception as e:
            print(f"⚠️  Error fetching prompt from LangSmith: {e}")
            return None
    
    def get_fashion_bot_prompt(self) -> str:
        """
        Get the fashion bot support prompt template.
        Automatically uses environment-based tag.
        """
        tag = self.get_tag_from_environment()
        print(f"🔧 Environment: {self.environment} -> Tag: {tag}")
        
        # Try to fetch from LangSmith with environment-specific tag
        langsmith_prompt = self.fetch_prompt_template("fashion_bot_support_prompt", tag=tag)
        
        if langsmith_prompt:
            print(f"✅ Successfully fetched prompt from LangSmith with tag '{tag}'")
            return langsmith_prompt
        
        # Fallback: try without tag (latest version)
        print(f"🔄 Tag '{tag}' not found, trying latest version...")
        try:
            prompts = self.client.list_prompts()
            matching_prompts = [p for p in prompts if p.name == "fashion_bot_support_prompt"]
            
            if matching_prompts:
                latest_prompt = sorted(matching_prompts, key=lambda p: p.created_at, reverse=True)[0]
                print("✅ Successfully fetched latest prompt from LangSmith")
                return latest_prompt.prompt.template
        except Exception as e:
            print(f"⚠️  Error fetching latest prompt: {e}")
        
        # Final fallback to local template
        print("🔄 Using local fallback prompt template")
        return self.get_local_fashion_bot_prompt()
    
    def get_local_fashion_bot_prompt(self) -> str:
        """Get the local fallback prompt template"""
        return """You are an intelligent and empathetic support assistant for an online fashion store.

Responsibilities:
- Answer product availability, sizing, fabric, customization, discounts, delivery, and order-related queries.
- Detect customer frustration and respond with empathy.
- If a shipment is delayed 5+ days, apologize sincerely and inform the user you are escalating it.
- If a user requests a delayed delivery (custom order), acknowledge and inform the operations team will help.
- If a customer expresses anger like "you are fooling me" or "you are not genuine", respond with politeness, empathy, and reassurance.

Product Info:
{product_info}

{order_info}

Conversation History:
{history}

Respond like a professional human support agent: empathetic, helpful, and conversational."""


# Global instance
prompt_manager = LangSmithPromptManager()


def fetch_langsmith_prompt(prompt_name: str = "fashion_bot_support_prompt", tag: Optional[str] = None) -> str:
    """
    Convenience function to fetch prompts from LangSmith using client API.
    
    Args:
        prompt_name: The name of the prompt in LangSmith
        tag: Optional tag to override environment-based tag
        
    Returns:
        The prompt template string
    """
    return prompt_manager.fetch_prompt_template(prompt_name, tag=tag) or prompt_manager.get_local_fashion_bot_prompt()


def get_environment_info() -> dict:
    """
    Get current environment information for debugging.
    
    Returns:
        Dictionary with environment details
    """
    manager = LangSmithPromptManager()
    return {
        "environment": manager.environment,
        "tag": manager.get_tag_from_environment(),
        "handle": os.getenv("LANGSMITH_HANDLE", "default"),
        "api_key_set": bool(manager.api_key)
    }


def setup_langsmith_prompts():
    """Setup function to show instructions for creating prompts in LangSmith"""
    print("🚀 Setting up LangSmith prompts...")
    
    manager = LangSmithPromptManager()
    tag = manager.get_tag_from_environment()
    handle = os.getenv("LANGSMITH_HANDLE", "default")
    
    print(f"🔧 Current Environment: {manager.environment}")
    print(f"🏷️  Tag: {tag}")
    print(f"👤 Handle: {handle}")
    
    # Get the main fashion bot prompt
    fashion_bot_template = manager.get_local_fashion_bot_prompt()
    
    print(f"\n📝 Instructions for creating prompts in LangSmith:")
    print(f"1. Go to https://smith.langchain.com/prompts")
    print(f"2. Click 'Create Prompt'")
    print(f"3. Use this template:")
    print(f"   Name: fashion_bot_support_prompt")
    print(f"   Tag: {tag}")
    print(f"   Template:\n{fashion_bot_template}")
    print(f"\n4. The prompt will be accessible with tag: {tag}")
    
    print(f"\n🎯 For different environments, create prompts with these tags:")
    print(f"   - Development: fashion_bot_support_prompt (tag: dev)")
    print(f"   - Staging: fashion_bot_support_prompt (tag: staging)") 
    print(f"   - Production: fashion_bot_support_prompt (tag: prod)")


if __name__ == "__main__":
    # Run setup if this file is executed directly
    setup_langsmith_prompts() 