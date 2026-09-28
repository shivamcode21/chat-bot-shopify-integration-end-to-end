"""
Context-Aware Test Configuration.
Extends the base test_config.py with context-specific evaluation criteria.
"""
from typing import Dict, Any

# ==================== CONTEXT-AWARE EVALUATION CRITERIA ====================
# These are ADDITIONAL criteria used when a test scenario has context assertions

CONTEXT_EVALUATION_CRITERIA = {
    "context_utilization": {
        "weight": 0.20,
        "description": (
            "Did the agent USE pre-seeded context (page_context, known_orders, phone_number) "
            "instead of asking for information that was already available?"
        ),
        "min_score": 3.5
    },
    "no_redundant_questions": {
        "weight": 0.20,
        "description": (
            "Did the agent avoid asking for information already available in the conversation state? "
            "For example: not asking for phone when phone_number is already set, "
            "not asking for order ID when selected_order_id is set."
        ),
        "min_score": 3.5
    },
    "flow_continuity": {
        "weight": 0.20,
        "description": (
            "If a waiting_for_* flag was set (e.g., waiting_for_cancellation_reason=True), "
            "did the agent correctly treat the user's next message as a continuation of that flow "
            "rather than starting a new intent detection?"
        ),
        "min_score": 3.5
    },
    "correct_routing": {
        "weight": 0.20,
        "description": (
            "Was the message routed to the correct agent given the conversation context? "
            "For example, if the expected_agent is 'cancel_or_update_order', "
            "was cancellation logic invoked and not product details?"
        ),
        "min_score": 3.5
    },
    "prompt_compliance": {
        "weight": 0.20,
        "description": (
            "Did the agent follow the specific behavioral rules defined in the prompt? "
            "For example: offering size change for size issues before cancellation, "
            "never asking for name during address updates, always escalating instead of cancelling."
        ),
        "min_score": 3.5
    }
}

# ==================== CLIENT-LEVEL TEST CONFIG ====================

CLIENT_TEST_CONFIG = {
    "default_client": "casence",
    "test_matrix": {
        "description": "Which agents to test per client. '*' means all agents from client profile.",
        "casence": "*"
    }
}

# ==================== PASS/FAIL THRESHOLDS FOR CONTEXT TESTS ====================

CONTEXT_TEST_THRESHOLDS = {
    "overall_pass_rate": 60.0,
    "individual_test_pass": {
        "base_llm_score": 2.5,
        "context_score": 3.0,
        "combined_weighted_score": 3.0
    },
    "base_criteria_weight": 0.5,
    "context_criteria_weight": 0.5
}

# ==================== PROMPT CONTRACT EXTRACTION CONFIG ====================

PROMPT_CONTRACT_CONFIG = {
    "llm_model": "gpt-4o-mini",
    "extraction_temperature": 0,
    "max_contracts_per_prompt": 20,
    "contract_categories": [
        "must_do",
        "must_not_do", 
        "conditional_behavior",
        "escalation_rules",
        "data_handling"
    ]
}

# ==================== REGRESSION SNAPSHOT CONFIG ====================

REGRESSION_CONFIG = {
    "snapshot_dir": "tests/regression_snapshots",
    "compare_fields": [
        "tool_calls_sequence",
        "final_agent_used",
        "state_mutations"
    ],
    "tolerance": {
        "tool_call_order_strict": False,
        "allow_extra_tool_calls": True,
        "max_extra_calls": 2
    }
}

