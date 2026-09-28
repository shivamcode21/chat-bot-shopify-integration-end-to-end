"""
Configuration file for ShipRocket webhook event handling system.
Modify these settings to customize the system behavior.
"""

import os
from typing import Dict, Any

# Database Configuration
DATABASE_CONFIG = {
    "connection_timeout": int(os.getenv("DB_CONNECTION_TIMEOUT", "30")),
    "max_connections": int(os.getenv("DB_MAX_CONNECTIONS", "20")),
    "cleanup_days": int(os.getenv("EVENT_CLEANUP_DAYS", "90")),
}

# Event Processing Configuration
EVENT_CONFIG = {
    "max_retries": int(os.getenv("EVENT_MAX_RETRIES", "3")),
    "retry_delay": int(os.getenv("EVENT_RETRY_DELAY", "5")),
    "bulk_batch_size": int(os.getenv("BULK_BATCH_SIZE", "100")),
    "enable_async_processing": os.getenv("ENABLE_ASYNC_PROCESSING", "true").lower() == "true",
}

# Notification Configuration
NOTIFICATION_CONFIG = {
    "enable_notifications": os.getenv("ENABLE_NOTIFICATIONS", "true").lower() == "true",
    "notification_timeout": int(os.getenv("NOTIFICATION_TIMEOUT", "30")),
    "max_notification_retries": int(os.getenv("MAX_NOTIFICATION_RETRIES", "3")),
    "notification_delay": {
        "high": int(os.getenv("HIGH_PRIORITY_DELAY", "0")),
        "medium": int(os.getenv("MEDIUM_PRIORITY_DELAY", "1")),
        "low": int(os.getenv("LOW_PRIORITY_DELAY", "5")),
    },
    "channels": {
        "whatsapp": os.getenv("ENABLE_WHATSAPP", "false").lower() == "true",
        "email": os.getenv("ENABLE_EMAIL", "false").lower() == "true",
        "sms": os.getenv("ENABLE_SMS", "false").lower() == "true",
        "push": os.getenv("ENABLE_PUSH", "false").lower() == "true",
    }
}

# Logging Configuration
LOGGING_CONFIG = {
    "level": os.getenv("LOG_LEVEL", "INFO"),
    "format": os.getenv("LOG_FORMAT", "%(asctime)s - %(name)s - %(levelname)s - %(message)s"),
    "enable_file_logging": os.getenv("ENABLE_FILE_LOGGING", "false").lower() == "true",
    "log_file_path": os.getenv("LOG_FILE_PATH", "logs/shiprocket_webhook.log"),
    "max_log_size": int(os.getenv("MAX_LOG_SIZE", "10485760")),  # 10MB
    "backup_count": int(os.getenv("LOG_BACKUP_COUNT", "5")),
}

# Webhook Configuration
WEBHOOK_CONFIG = {
    "max_payload_size": int(os.getenv("MAX_PAYLOAD_SIZE", "1048576")),  # 1MB
    "rate_limit_requests": int(os.getenv("RATE_LIMIT_REQUESTS", "100")),
    "rate_limit_window": int(os.getenv("RATE_LIMIT_WINDOW", "3600")),  # 1 hour
    "enable_webhook_signature_validation": os.getenv("ENABLE_WEBHOOK_SIGNATURE_VALIDATION", "false").lower() == "true",
    "webhook_secret": os.getenv("WEBHOOK_SECRET", ""),
}

# Performance Configuration
PERFORMANCE_CONFIG = {
    "enable_connection_pooling": os.getenv("ENABLE_CONNECTION_POOLING", "true").lower() == "true",
    "pool_size": int(os.getenv("DB_POOL_SIZE", "10")),
    "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "20")),
    "enable_query_caching": os.getenv("ENABLE_QUERY_CACHING", "false").lower() == "true",
    "cache_ttl": int(os.getenv("CACHE_TTL", "300")),  # 5 minutes
}

# Monitoring Configuration
MONITORING_CONFIG = {
    "enable_metrics": os.getenv("ENABLE_METRICS", "true").lower() == "true",
    "metrics_port": int(os.getenv("METRICS_PORT", "9090")),
    "enable_health_checks": os.getenv("ENABLE_HEALTH_CHECKS", "true").lower() == "true",
    "health_check_interval": int(os.getenv("HEALTH_CHECK_INTERVAL", "30")),
    "enable_alerting": os.getenv("ENABLE_ALERTING", "false").lower() == "true",
}

# Custom Event Mappings
# Add your custom event mappings here
CUSTOM_EVENT_MAPPINGS = {
    # Example: Map custom status to existing event
    # "CUSTOM_STATUS": "EXISTING_EVENT_NAME",
    
    # Example: Add new custom events
    # "CUSTOM_EVENT": {
    #     "event_name": "CUSTOM_EVENT_NAME",
    #     "template_id": "custom_event_template",
    #     "message": "Custom event message! 🎉",
    #     "should_notify": True,
    #     "priority": 1
    # }
}

# Custom Notification Templates
# Add your custom notification templates here
CUSTOM_NOTIFICATION_TEMPLATES = {
    # Example: Add new template
    # "custom_event_template": {
    #     "title": "🎉 Custom Event",
    #     "message": "Custom event message!",
    #     "subtitle": "Custom event description",
    #     "action_text": "Take Action",
    #     "priority": "high"
    # }
}

# Environment-specific overrides
def get_config() -> Dict[str, Any]:
    """
    Get configuration based on current environment.
    
    Returns:
        Configuration dictionary
    """
    env = os.getenv("ENVIRONMENT", "development").lower()
    
    config = {
        "database": DATABASE_CONFIG,
        "event": EVENT_CONFIG,
        "notification": NOTIFICATION_CONFIG,
        "logging": LOGGING_CONFIG,
        "webhook": WEBHOOK_CONFIG,
        "performance": PERFORMANCE_CONFIG,
        "monitoring": MONITORING_CONFIG,
        "custom_events": CUSTOM_EVENT_MAPPINGS,
        "custom_templates": CUSTOM_NOTIFICATION_TEMPLATES,
        "environment": env,
    }
    
    # Environment-specific overrides
    if env == "production":
        config["logging"]["level"] = "WARNING"
        config["notification"]["enable_notifications"] = True
        config["monitoring"]["enable_metrics"] = True
        config["monitoring"]["enable_alerting"] = True
    elif env == "testing":
        config["logging"]["level"] = "DEBUG"
        config["notification"]["enable_notifications"] = False
        config["database"]["cleanup_days"] = 1
    
    return config

# Get current configuration
CURRENT_CONFIG = get_config()

def get_setting(category: str, key: str, default: Any = None) -> Any:
    """
    Get a specific configuration setting.
    
    Args:
        category: Configuration category (e.g., 'database', 'notification')
        key: Setting key
        default: Default value if setting not found
        
    Returns:
        Configuration value
    """
    return CURRENT_CONFIG.get(category, {}).get(key, default)

def update_config(category: str, key: str, value: Any) -> None:
    """
    Update a configuration setting at runtime.
    
    Args:
        category: Configuration category
        key: Setting key
        value: New value
    """
    if category not in CURRENT_CONFIG:
        CURRENT_CONFIG[category] = {}
    CURRENT_CONFIG[category][key] = value

def reload_config() -> None:
    """Reload configuration from environment variables"""
    global CURRENT_CONFIG
    CURRENT_CONFIG = get_config() 