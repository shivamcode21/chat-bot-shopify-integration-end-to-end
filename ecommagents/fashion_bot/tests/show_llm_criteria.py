#!/usr/bin/env python3
"""
Script to show all available LLM-as-a-judge criteria and evaluation options
"""
import json
from test_config import (
    EVALUATION_CRITERIA, 
    STRING_MATCHING_CONFIG, 
    TEST_THRESHOLDS,
    custom_evaluation_functions
)


def show_default_llm_criteria():
    """Display all default LLM-as-a-judge criteria"""
    print("🤖 Default LLM-as-a-Judge Evaluation Criteria")
    print("=" * 60)
    
    for i, (criterion, config) in enumerate(EVALUATION_CRITERIA.items(), 1):
        print(f"{i}. {criterion.replace('_', ' ').title()}")
        print(f"   Weight: {config['weight']}")
        print(f"   Min Score: {config['min_score']}/5")
        print(f"   Description: {config['description']}")
        print()


def show_string_matching_criteria():
    """Display string matching evaluation criteria"""
    print("🔍 String Matching Evaluation Criteria")
    print("=" * 60)
    
    print(f"Keyword Score Threshold: {STRING_MATCHING_CONFIG['keyword_score_threshold']}")
    print(f"Case Sensitive: {STRING_MATCHING_CONFIG['case_sensitive']}")
    print(f"Partial Match: {STRING_MATCHING_CONFIG['partial_match']}")
    
    print("\nKeyword Synonyms:")
    for category, synonyms in STRING_MATCHING_CONFIG['synonyms'].items():
        print(f"  {category}: {', '.join(synonyms)}")
    print()


def show_custom_evaluation_functions():
    """Display custom evaluation functions"""
    print("🎯 Custom Evaluation Functions")
    print("=" * 60)
    
    custom_funcs = custom_evaluation_functions()
    
    for func_name, func in custom_funcs.items():
        print(f"• {func_name.replace('_', ' ').title()}")
        print(f"  Function: {func.__name__}")
        print(f"  Docstring: {func.__doc__}")
        print()


def show_test_thresholds():
    """Display test thresholds and requirements"""
    print("📊 Test Thresholds and Requirements")
    print("=" * 60)
    
    print(f"Overall Pass Rate: {TEST_THRESHOLDS['overall_pass_rate']}%")
    print(f"Individual Test Pass Requirements:")
    print(f"  - Keyword Score: {TEST_THRESHOLDS['individual_test_pass']['keyword_score']}")
    print(f"  - LLM Overall Score: {TEST_THRESHOLDS['individual_test_pass']['llm_overall_score']}")
    
    print(f"\nResponse Quality Requirements:")
    print(f"  - Min Length: {TEST_THRESHOLDS['response_quality']['min_length']} characters")
    print(f"  - Max Length: {TEST_THRESHOLDS['response_quality']['max_length']} characters")
    print()


def show_evaluation_prompt_template():
    """Show the default LLM evaluation prompt template"""
    print("📝 Default LLM Evaluation Prompt Template")
    print("=" * 60)
    
    prompt_template = """
You are an expert evaluator for a fashion e-commerce chatbot. Evaluate the following response based on the given criteria.

User Question: {user_question}
Product Context: {product_context}
Bot Response: {bot_response}

Evaluation Criteria:
1. Logical Correctness (1-5): Does the response accurately address the user's question and provide correct information?
2. Conciseness (1-5): Is the response brief and to the point without unnecessary verbosity?
3. Helpfulness (1-5): Does the response provide actionable information or clear next steps?
4. Tone (1-5): Does the response maintain a professional and helpful tone?
5. Completeness (1-5): Does the response address all aspects of the user's question from the last message in the conversation?
6. Context Awareness (1-5): Is the response appropriate for the context (order status vs product info vs frustration) judge this based on the last message in the conversation?

Please provide scores for each criterion and a brief explanation for each score.

Expected Keywords: {expected_keywords}

Respond in the following JSON format:
{
    "logical_correctness": {"score": 4, "explanation": "..."},
    "conciseness": {"score": 3, "explanation": "..."},
    "helpfulness": {"score": 4, "explanation": "..."},
    "tone": {"score": 5, "explanation": "..."},
    "completeness": {"score": 4, "explanation": "..."},
    "context_awareness": {"score": 4, "explanation": "..."},
    "overall_score": 4.0,
    "summary": "Overall assessment of the response quality"
}
"""
    
    print(prompt_template)


def show_customization_examples():
    """Show examples of how to customize evaluation criteria"""
    print("⚙️  Customization Examples")
    print("=" * 60)
    
    print("1. Custom Evaluation Criteria:")
    custom_criteria = {
        "accuracy": {
            "weight": 0.4,
            "description": "How accurate is the information provided?",
            "min_score": 3.5
        },
        "clarity": {
            "weight": 0.3,
            "description": "How clear and understandable is the response?",
            "min_score": 3.0
        },
        "speed": {
            "weight": 0.3,
            "description": "How quickly does the response address the question?",
            "min_score": 3.0
        }
    }
    
    for criterion, config in custom_criteria.items():
        print(f"   - {criterion}: {config['description']}")
    
    print("\n2. Custom String Matching:")
    custom_string_config = {
        "keyword_score_threshold": 0.8,
        "case_sensitive": True,
        "synonyms": {
            "product": ["item", "goods", "merchandise"],
            "order": ["purchase", "buy", "transaction"]
        }
    }
    print(f"   - Higher keyword threshold: {custom_string_config['keyword_score_threshold']}")
    print(f"   - Case sensitive: {custom_string_config['case_sensitive']}")
    
    print("\n3. Custom Test Thresholds:")
    custom_thresholds = {
        "overall_pass_rate": 85.0,
        "individual_test_pass": {
            "keyword_score": 0.8,
            "llm_overall_score": 4.0
        }
    }
    print(f"   - Higher pass rate: {custom_thresholds['overall_pass_rate']}%")
    print(f"   - Higher LLM score requirement: {custom_thresholds['individual_test_pass']['llm_overall_score']}")
    print()


def show_available_metrics():
    """Show all available metrics and measurements"""
    print("📈 Available Metrics and Measurements")
    print("=" * 60)
    
    metrics = {
        "Performance Metrics": [
            "Response Time (seconds)",
            "Memory Usage (MB)",
            "Concurrent Request Handling",
            "Throughput (requests/second)"
        ],
        "Quality Metrics": [
            "Keyword Score (0-1)",
            "LLM Judge Scores (1-5 for each criterion)",
            "Overall LLM Score (1-5)",
            "Custom Evaluation Scores (0-1)"
        ],
        "Business Metrics": [
            "Pass/Fail Rate (%)",
            "Category-wise Performance",
            "Error Rate",
            "User Satisfaction Score"
        ],
        "Technical Metrics": [
            "Graph Execution Time",
            "Node Processing Time",
            "Memory Leaks",
            "API Response Times"
        ]
    }
    
    for category, metric_list in metrics.items():
        print(f"\n{category}:")
        for metric in metric_list:
            print(f"  • {metric}")


def main():
    """Display all available criteria and options"""
    print("🎯 Fashion Bot Testing Framework - Available Criteria")
    print("=" * 70)
    
    show_default_llm_criteria()
    show_string_matching_criteria()
    show_custom_evaluation_functions()
    show_test_thresholds()
    show_evaluation_prompt_template()
    show_customization_examples()
    show_available_metrics()
    
    print("\n" + "=" * 70)
    print("💡 To use these criteria, modify the configuration in test_config.py")
    print("📚 For more details, see README_TESTING.md")
    print("🚀 To run tests, use: python run_tests.py")


if __name__ == "__main__":
    main() 