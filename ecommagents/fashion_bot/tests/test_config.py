"""
Configuration file for Fashion Bot testing framework
"""
from typing import Dict, List, Any
import os

# LangSmith Configuration
LANGSMITH_CONFIG = {
    "api_key": os.getenv("LANGSMITH_API_KEY"),  # Get from environment variable
    "project_name": "fashion-bot-conversation-testing",
    "dataset_name": "fashion-bot-conversation-test-scenarios"
}

# LLM Judge Configuration - Using Gemini 2.5 Flash Lite for fast, cost-effective evaluation
LLM_JUDGE_CONFIG = {
    "provider": "gemini",
    "model": "gemini-2.5-flash-lite",
    "temperature": 0,
    "max_tokens": 2000,
    "api_key_env_var": "GOOGLE_API_KEY"
}

# Evaluation Criteria Configuration
EVALUATION_CRITERIA = {
    "logical_correctness": {
        "weight": 0.20,
        "description": "Do the responses accurately address the user's questions and provide correct information?",
        "min_score": 3.0
    },
    "conciseness": {
        "weight": 0.10,
        "description": "Are the responses brief and to the point without unnecessary verbosity?",
        "min_score": 3.0
    },
    "helpfulness": {
        "weight": 0.20,
        "description": "Do the responses provide actionable information or clear next steps?",
        "min_score": 3.5
    },
    "tone": {
        "weight": 0.10,
        "description": "Do the responses maintain a professional and helpful tone throughout?",
        "min_score": 3.5
    },
    "completeness": {
        "weight": 0.10,
        "description": "Do the responses address all aspects of the user's questions?",
        "min_score": 3.0
    },
    "context_awareness": {
        "weight": 0.10,
        "description": "Are the responses appropriate for the context and conversation flow?",
        "min_score": 3.5
    },
    "conversation_flow": {
        "weight": 0.10,
        "description": "Does the conversation feel natural and build on previous exchanges?",
        "min_score": 3.5
    },
    "memory_retention": {
        "weight": 0.10,
        "description": "Does the bot remember and reference information from previous messages?",
        "min_score": 3.5
    }
}

# Test Thresholds - LLM-only evaluation (keyword scoring removed)
TEST_THRESHOLDS = {
    "overall_pass_rate": 65.0,  # Minimum pass rate percentage
    "individual_test_pass": {
        "llm_overall_score": 2.5,  # Minimum LLM judge score (1-5 scale)
        "conversation_length": 2   # Minimum conversation length
    },
    "response_quality": {
        "min_length": 10,
        "max_length": 500
    },
    "conversation_quality": {
        "min_turns": 2,
        "max_turns": 10,
        "turn_response_time": 5.0  # seconds per turn
    }
}

# Product Data Validation
PRODUCT_DATA_VALIDATION = {
    "required_sizes": ["M", "L", "XL"],
    "required_colors": ["Black", "White"],
    "required_fields": ["sizes_available", "colors_available", "in_stock"]
}

# Order Status Test Configuration
ORDER_STATUS_CONFIG = {
    "mock_orders": {
        "12345": {
            "status": "shipped",
            "tracking_number": "TRK123456",
            "estimated_delivery": "2024-01-15"
        },
        "67890": {
            "status": "processing",
            "estimated_delivery": "2024-01-20"
        }
    },
    "phone_numbers": {
        "12345": "+1234567890",
        "67890": "+0987654321"
    }
}

# Frustration Detection Configuration
FRUSTRATION_CONFIG = {
    "frustration_keywords": [
        "angry", "frustrated", "upset", "annoyed", "disappointed",
        "terrible", "awful", "horrible", "bad", "poor"
    ],
    "escalation_triggers": [
        "manager", "supervisor", "escalate", "complain", "complaint"
    ],
    "response_requirements": [
        "apologize", "acknowledge", "help", "assist", "resolve"
    ]
}

# Conversation Flow Configuration
CONVERSATION_FLOW_CONFIG = {
    "greeting_patterns": [
        "hello", "hi", "hey", "good morning", "good afternoon"
    ],
    "transition_phrases": [
        "now", "next", "also", "additionally", "furthermore"
    ],
    "closing_patterns": [
        "thank you", "thanks", "goodbye", "bye", "have a great day"
    ],
    "context_indicators": [
        "as I mentioned", "as we discussed", "earlier", "previously"
    ]
}

# Test Data Categories
TEST_CATEGORIES = {
    "product_queries": [
        "basic_product_query_conversation",
        "product_selection_conversation", 
        "stock_inquiry_conversation"
    ],
    "order_queries": [
        "order_status_conversation",
        "order_without_number_conversation"
    ],
    "customer_service": [
        "frustration_escalation_conversation",
        "return_refund_conversation"
    ],
    "general_inquiries": [
        "general_inquiry_conversation"
    ]
}

# Performance Metrics
PERFORMANCE_METRICS = {
    "response_time_threshold": 5.0,  # seconds per turn
    "memory_usage_threshold": 100,   # MB
    "concurrent_requests": 10,
    "conversation_length_target": 3,  # target turns per conversation
    "max_conversation_time": 30.0     # seconds per full conversation
}

# Report Configuration
REPORT_CONFIG = {
    "generate_markdown": True,
    "generate_json": True,
    "include_detailed_scores": True,
    "include_performance_metrics": True,
    "include_conversation_flow": True,
    "output_directory": "tests/test_reports"
}

# Custom Evaluation Functions
def custom_evaluation_functions() -> Dict[str, callable]:
    """
    Define custom evaluation functions for specific scenarios
    """
    def check_product_info_completeness(response: str, product_data: Dict) -> float:
        """Check if response contains complete product information"""
        score = 0.0
        if any(size in response for size in product_data.get("sizes_available", [])):
            score += 0.5
        if any(color in response for color in product_data.get("colors_available", [])):
            score += 0.5
        return score
    
    def check_order_status_appropriateness(response: str, has_order_number: bool) -> float:
        """Check if order status response is appropriate"""
        if has_order_number:
            return 1.0 if "order" in response.lower() else 0.0
        else:
            return 1.0 if any(word in response.lower() for word in ["order number", "phone number"]) else 0.0
    
    def check_frustration_handling(response: str) -> float:
        """Check if frustration is handled appropriately"""
        frustration_indicators = ["apologize", "sorry", "understand", "help", "escalate"]
        return sum(1 for indicator in frustration_indicators if indicator in response.lower()) / len(frustration_indicators)
    
    def check_conversation_flow(conversation_turns: List[Dict]) -> float:
        """Check if conversation flows naturally"""
        if len(conversation_turns) < 2:
            return 0.0
        
        flow_score = 0.0
        for i in range(1, len(conversation_turns)):
            current_turn = conversation_turns[i]
            previous_turn = conversation_turns[i-1]
            
            # Check if current response relates to previous message
            if any(word in current_turn["bot_response"].lower() for word in previous_turn["customer_message"].lower().split()):
                flow_score += 1.0
        
        return flow_score / (len(conversation_turns) - 1)
    
    def check_memory_retention(conversation_turns: List[Dict]) -> float:
        """Check if bot remembers information from previous turns"""
        if len(conversation_turns) < 2:
            return 0.0
        
        memory_score = 0.0
        for i in range(1, len(conversation_turns)):
            current_turn = conversation_turns[i]
            previous_turns = conversation_turns[:i]
            
            # Check if current response references previous information
            previous_info = " ".join([turn["customer_message"] for turn in previous_turns])
            current_response = current_turn["bot_response"]
            
            # Simple check for common words between previous info and current response
            previous_words = set(previous_info.lower().split())
            current_words = set(current_response.lower().split())
            common_words = previous_words.intersection(current_words)
            
            if len(common_words) > 2:  # At least 2 common words
                memory_score += 1.0
        
        return memory_score / (len(conversation_turns) - 1)
    
    return {
        "product_info_completeness": check_product_info_completeness,
        "order_status_appropriateness": check_order_status_appropriateness,
        "frustration_handling": check_frustration_handling,
        "conversation_flow": check_conversation_flow,
        "memory_retention": check_memory_retention
    }

# Export all configurations
__all__ = [
    "LANGSMITH_CONFIG",
    "LLM_JUDGE_CONFIG", 
    "EVALUATION_CRITERIA",
    "TEST_THRESHOLDS",
    "PRODUCT_DATA_VALIDATION",
    "ORDER_STATUS_CONFIG",
    "FRUSTRATION_CONFIG",
    "CONVERSATION_FLOW_CONFIG",
    "TEST_CATEGORIES",
    "PERFORMANCE_METRICS",
    "REPORT_CONFIG",
    "custom_evaluation_functions"
] 