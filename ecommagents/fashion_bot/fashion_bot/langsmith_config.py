#!/usr/bin/env python3
"""
LangSmith configuration and setup utilities for Fashion Bot
Supports environment-based project separation (dev/staging/prod)
"""
import logging
from typing import Optional, Dict, Any, Literal
from fashion_bot.env_loader import bootstrap_environment, get_bool, get_env, set_env

bootstrap_environment()

logger = logging.getLogger(__name__)

# Environment type definitions
Environment = Literal["development", "staging", "production"]

def get_current_environment() -> Environment:
    """
    Detect current environment from various sources
    
    Priority order:
    1. ENVIRONMENT env var
    2. NODE_ENV env var (for Node.js compatibility)
    3. DEPLOY_ENV env var
    4. Default to 'development' if none found
    
    Returns:
        Environment: Current environment
    """
    env_vars = ["ENVIRONMENT", "NODE_ENV", "DEPLOY_ENV"]
    
    for var in env_vars:
        env_value = (get_env(var, "") or "").lower()
        if env_value in ["production", "prod"]:
            return "production"
        elif env_value in ["staging", "stage"]:
            return "staging"
        elif env_value in ["development", "dev", "local"]:
            return "development"
    
    # Default to development
    logger.info("No environment specified, defaulting to 'development'")
    return "development"

def get_environment_suffix(env: Environment) -> str:
    """Get short suffix for environment"""
    suffixes = {
        "development": "dev",
        "staging": "staging", 
        "production": "prod"
    }
    return suffixes[env]

class LangSmithConfig:
    """
    Environment-aware LangSmith configuration and setup
    
    Automatically separates traces by environment:
    - development -> fashion-bot-gupshup-webhook-dev
    - staging -> fashion-bot-gupshup-webhook-staging  
    - production -> fashion-bot-gupshup-webhook-prod
    """
    
    def __init__(self, base_project_name: str = "fashion-bot", service: str = None):
        self.api_key = get_env("LANGSMITH_API_KEY")
        self.environment = get_current_environment()
        self.env_suffix = get_environment_suffix(self.environment)
        
        # Build environment-specific project name
        if service:
            self.project_name = f"{base_project_name}-{service}-{self.env_suffix}"
        else:
            self.project_name = f"{base_project_name}-{self.env_suffix}"
            
        self.base_project_name = base_project_name
        self.service = service
        self.is_enabled = bool(self.api_key)
        
        logger.info(f"🏗️  LangSmith Config: Environment='{self.environment}', Project='{self.project_name}'")
        
    def setup_tracing(self, project_name: Optional[str] = None) -> bool:
        """
        Setup LangSmith tracing environment variables
        
        Args:
            project_name: Optional project name override (will still get env suffix)
            
        Returns:
            bool: True if tracing was enabled, False otherwise
        """
        if not self.api_key:
            logger.warning("⚠️  LANGSMITH_API_KEY not found. LangSmith tracing disabled.")
            return False

        # Org-scoped keys require workspace id to ingest runs.
        workspace_id = get_env("LANGSMITH_WORKSPACE_ID")
        if self.api_key.startswith("lsv2_") and not workspace_id:
            logger.warning(
                "⚠️ LANGSMITH_WORKSPACE_ID not set for org-scoped LANGSMITH_API_KEY. "
                "Tracing uploads may fail with HTTP 403."
            )
            
        # Use provided project name or default, ensure it has environment suffix
        if project_name:
            if not project_name.endswith(f"-{self.env_suffix}"):
                final_project_name = f"{project_name}-{self.env_suffix}"
            else:
                final_project_name = project_name
        else:
            final_project_name = self.project_name
        
        # Set environment variables for tracing
        set_env("LANGCHAIN_PROJECT", final_project_name)
        set_env("LANGSMITH_PROJECT", final_project_name)
        set_env("LANGSMITH_TRACING_V2", "true")
        set_env("LANGCHAIN_TRACING_V2", "true")
        
        # Add environment-specific tags
        existing_tags = get_env("LANGCHAIN_TAGS", "") or ""
        env_tags = f"environment:{self.environment},env:{self.env_suffix}"
        if existing_tags:
            all_tags = f"{existing_tags},{env_tags}"
        else:
            all_tags = env_tags
        set_env("LANGCHAIN_TAGS", all_tags)
        
        logger.info(f"🔗 LangSmith tracing enabled")
        logger.info(f"   Project: {final_project_name}")
        logger.info(f"   Environment: {self.environment}")
        logger.info(f"   API Key: {self.api_key[:10]}...")
        if workspace_id:
            logger.info(f"   Workspace: {workspace_id}")
        
        return True
    
    def get_status(self) -> Dict[str, Any]:
        """
        Get current LangSmith configuration status
        
        Returns:
            dict: Configuration status information with environment details
        """
        return {
            "enabled": self.is_enabled,
            "api_key_configured": bool(self.api_key),
            "environment": self.environment,
            "environment_suffix": self.env_suffix,
            "project_name": get_env("LANGCHAIN_PROJECT", self.project_name),
            "base_project_name": self.base_project_name,
            "service": self.service,
            "tracing_v2_enabled": get_bool("LANGCHAIN_TRACING_V2", False),
            "langsmith_v2_enabled": get_bool("LANGSMITH_TRACING_V2", False),
            "tags": get_env("LANGCHAIN_TAGS", "") or ""
        }
    
    def get_traceable_metadata(self, service: str, **extra_metadata) -> Dict[str, Any]:
        """
        Get standardized metadata for traceable functions with environment info
        
        Args:
            service: Service name (e.g., 'gupshup_webhook', 'whatsapp_webhook')
            **extra_metadata: Additional metadata fields
            
        Returns:
            dict: Metadata dictionary for traceable decorators
        """
        metadata = {
            "service": service,
            "bot_type": "fashion_bot",
            "environment": self.environment,
            "env_suffix": self.env_suffix,
            "project": self.project_name,
            "base_project": self.base_project_name
        }
        metadata.update(extra_metadata)
        return metadata
    
    def validate_setup(self) -> bool:
        """
        Validate that LangSmith is properly configured
        
        Returns:
            bool: True if valid, False otherwise
        """
        if not self.api_key:
            logger.warning("⚠️  LANGSMITH_API_KEY not found in environment variables")
            logger.warning("   To enable LangSmith, add LANGSMITH_API_KEY=your_key_here to your .env file")
            return False
        
        logger.info(f"✅ LangSmith API key found: {self.api_key[:10]}...")
        logger.info(f"✅ Environment: {self.environment}")
        logger.info(f"✅ Project will be: {self.project_name}")
        return True

    def get_project_url(self) -> Optional[str]:
        """Get the LangSmith project URL for this configuration"""
        if not self.is_enabled:
            return None
        
        # LangSmith project URLs follow this pattern
        return f"https://smith.langchain.com/projects/p/{self.project_name}"

# Environment-aware global configuration instances
def create_service_configs():
    """Create environment-aware service configurations"""
    return {
        "gupshup": LangSmithConfig("fashion-bot", "gupshup-webhook"),
        "whatsapp": LangSmithConfig("fashion-bot", "whatsapp-webhook"),
        "shiprocket": LangSmithConfig("fashion-bot", "shiprocket-webhook"),
        "shopify": LangSmithConfig("fashion-bot", "shopify-webhook"),
        "general": LangSmithConfig("fashion-bot", "general")
    }

# Global configuration instances (will be environment-specific)
_service_configs = create_service_configs()
GUPSHUP_LANGSMITH = _service_configs["gupshup"]
WHATSAPP_LANGSMITH = _service_configs["whatsapp"]
SHIPROCKET_LANGSMITH = _service_configs["shiprocket"]
SHOPIFY_LANGSMITH = _service_configs["shopify"]
GENERAL_LANGSMITH = _service_configs["general"]

def get_langsmith_config(service: str = "general") -> LangSmithConfig:
    """
    Get LangSmith configuration for a specific service
    
    Args:
        service: Service name ('gupshup', 'whatsapp', 'general')
        
    Returns:
        LangSmithConfig: Environment-aware configuration instance
    """
    return _service_configs.get(service, GENERAL_LANGSMITH)

def setup_langsmith_for_service(service: str) -> bool:
    """
    Setup LangSmith tracing for a specific service with environment awareness
    
    Args:
        service: Service name ('gupshup', 'whatsapp', 'general')
        
    Returns:
        bool: True if setup successful, False otherwise
    """
    config = get_langsmith_config(service)
    return config.setup_tracing()

def print_environment_info():
    """Print current environment configuration for debugging"""
    current_env = get_current_environment()
    env_suffix = get_environment_suffix(current_env)
    
    print("🌍 Environment Configuration:")
    print(f"   Current Environment: {current_env}")
    print(f"   Environment Suffix: {env_suffix}")
    print(f"   Environment Variables:")
    for var in ["ENVIRONMENT", "NODE_ENV", "DEPLOY_ENV"]:
        value = get_env(var, "Not set")
        print(f"     {var}: {value}")
    
    print(f"\n📊 Project Names:")
    for service_name, config in _service_configs.items():
        print(f"   {service_name}: {config.project_name}")

# Auto-print environment info when in development
if get_current_environment() == "development":
    print_environment_info() 