#!/usr/bin/env python3
"""
Example usage of the Fashion Bot testing framework
"""
import asyncio
import json
from test_fashion_bot import FashionBotTester
from langsmith_integration import LangSmithTestManager
from test_config import EVALUATION_CRITERIA, TEST_THRESHOLDS


async def example_basic_testing():
    """Example of basic testing without LangSmith"""
    print("🔍 Example 1: Basic Multi-Turn Conversation Testing")
    print("=" * 60)
    
    # Initialize tester
    tester = FashionBotTester()
    
    # Run a single conversation scenario
    scenario = tester.test_data["test_scenarios"][0]  # First scenario
    result = await tester.run_comprehensive_test(scenario)
    
    print(f"Test: {scenario['id']}")
    print(f"Description: {scenario['description']}")
    print(f"Total Turns: {result['string_evaluation']['conversation_length']}")
    print(f"Overall Keyword Score: {result['string_evaluation']['overall_keyword_score']:.2f}")
    print(f"LLM Overall Score: {result['llm_evaluation'].get('overall_score', 'N/A')}")
    print(f"Passed: {result['passed']}")
    
    print("\nConversation Flow:")
    for turn in result["conversation_result"]["conversation_results"]:
        print(f"  Turn {turn['turn']}:")
        print(f"    Customer: {turn['customer_message']}")
        print(f"    Bot: {turn['bot_response']}")
        print(f"    Keyword Score: {turn['keyword_score']:.2f}")
    print()


async def example_langsmith_integration():
    """Example of LangSmith integration"""
    print("📊 Example 2: LangSmith Integration")
    print("=" * 50)
    
    # Initialize LangSmith manager (requires API key)
    langsmith_key = "your-langsmith-api-key"  # Replace with actual key
    manager = LangSmithTestManager(langsmith_key)
    
    # Load test data
    with open("tests/test_data.json", "r") as f:
        test_data = json.load(f)
    
    # Create dataset
    dataset_id = manager.create_test_dataset(test_data)
    print(f"Created dataset: {dataset_id}")
    
    # Example conversation result
    example_result = {
        "scenario": {
            "id": "basic_product_query_conversation",
            "description": "Multi-turn conversation about product information"
        },
        "conversation_result": {
            "conversation_results": [
                {
                    "turn": 1,
                    "customer_message": "Hi, I'm looking for a t-shirt",
                    "bot_response": "Hello! I'd be happy to help you find a t-shirt. What specific information are you looking for?",
                    "expected_keywords": ["t-shirt", "help", "available"],
                    "expected_behavior": "Should greet and ask for more details about the t-shirt"
                },
                {
                    "turn": 2,
                    "customer_message": "What sizes do you have available?",
                    "bot_response": "We have sizes M, L, and XL available for our t-shirts.",
                    "expected_keywords": ["M", "L", "XL", "sizes", "available"],
                    "expected_behavior": "Should list available sizes from product data"
                }
            ]
        },
        "string_evaluation": {
            "overall_keyword_score": 0.8,
            "conversation_length": 2
        },
        "llm_evaluation": {
            "logical_correctness": {"score": 4, "explanation": "Correctly addresses questions"},
            "conciseness": {"score": 4, "explanation": "Brief and direct"},
            "helpfulness": {"score": 4, "explanation": "Provides useful information"},
            "conversation_flow": {"score": 4, "explanation": "Natural flow"},
            "memory_retention": {"score": 4, "explanation": "Maintains context"},
            "overall_score": 4.0
        },
        "passed": True
    }
    
    # Log test run
    run_id = manager.log_test_run(example_result)
    print(f"Logged test run: {run_id}")
    print()


async def example_custom_evaluations():
    """Example of custom evaluation functions"""
    print("🎯 Example 3: Custom Evaluations for Multi-Turn Conversations")
    print("=" * 70)
    
    from test_config import custom_evaluation_functions
    
    # Get custom evaluation functions
    custom_funcs = custom_evaluation_functions()
    
    # Test conversation responses
    responses = [
        "Hello! I'd be happy to help you find a t-shirt. What specific information are you looking for?",
        "We have sizes M, L, and XL available for our t-shirts.",
        "We also have colors Black and White available."
    ]
    combined_response = " ".join(responses)
    
    product_data = {
        "sizes_available": ["M", "L", "XL"],
        "colors_available": ["Black", "White"]
    }
    
    # Run custom evaluations
    product_score = custom_funcs["product_info_completeness"](combined_response, product_data)
    order_score = custom_funcs["order_status_appropriateness"](combined_response, has_order_number=False)
    frustration_score = custom_funcs["frustration_handling"](combined_response)
    
    print(f"Combined Responses: {combined_response}")
    print(f"Product Info Completeness: {product_score:.2f}")
    print(f"Order Status Appropriateness: {order_score:.2f}")
    print(f"Frustration Handling: {frustration_score:.2f}")
    print()


async def example_configuration_customization():
    """Example of customizing evaluation criteria"""
    print("⚙️  Example 4: Configuration Customization for Conversations")
    print("=" * 70)
    
    # Custom evaluation criteria for multi-turn conversations
    custom_criteria = {
        "conversation_flow": {
            "weight": 0.25,
            "description": "How natural and coherent is the conversation flow?",
            "min_score": 3.5
        },
        "memory_retention": {
            "weight": 0.25,
            "description": "Does the bot remember and reference previous information?",
            "min_score": 3.5
        },
        "context_awareness": {
            "weight": 0.20,
            "description": "Is the bot aware of the conversation context?",
            "min_score": 3.0
        },
        "response_quality": {
            "weight": 0.30,
            "description": "Overall quality of individual responses",
            "min_score": 3.0
        }
    }
    
    # Custom thresholds for conversations
    custom_thresholds = {
        "overall_pass_rate": 80.0,  # Higher threshold
        "individual_test_pass": {
            "keyword_score": 0.8,  # Higher keyword requirement
            "llm_overall_score": 4.0,  # Higher LLM score requirement
            "conversation_length": 3  # Minimum conversation length
        }
    }
    
    print("Custom Evaluation Criteria for Conversations:")
    for criterion, config in custom_criteria.items():
        print(f"  - {criterion}: {config['description']} (min: {config['min_score']})")
    
    print("\nCustom Thresholds:")
    print(f"  - Overall Pass Rate: {custom_thresholds['overall_pass_rate']}%")
    print(f"  - Keyword Score: {custom_thresholds['individual_test_pass']['keyword_score']}")
    print(f"  - LLM Score: {custom_thresholds['individual_test_pass']['llm_overall_score']}")
    print(f"  - Min Conversation Length: {custom_thresholds['individual_test_pass']['conversation_length']} turns")
    print()


async def example_batch_testing():
    """Example of batch testing multiple conversation scenarios"""
    print("📦 Example 5: Batch Testing Multi-Turn Conversations")
    print("=" * 60)
    
    tester = FashionBotTester()
    
    # Select specific conversation categories
    product_scenarios = [
        s for s in tester.test_data["test_scenarios"] 
        if s["id"] in ["basic_product_query_conversation", "product_selection_conversation", "stock_inquiry_conversation"]
    ]
    
    print(f"Running {len(product_scenarios)} product-related conversation tests...")
    
    results = []
    for scenario in product_scenarios:
        result = await tester.run_comprehensive_test(scenario)
        results.append(result)
        print(f"  {scenario['id']}: {'✅' if result['passed'] else '❌'} ({result['string_evaluation']['conversation_length']} turns)")
    
    # Calculate batch statistics
    passed = sum(1 for r in results if r["passed"])
    pass_rate = (passed / len(results)) * 100
    total_turns = sum(r["string_evaluation"]["conversation_length"] for r in results)
    avg_turns = total_turns / len(results)
    
    print(f"\nBatch Results: {passed}/{len(results)} passed ({pass_rate:.1f}%)")
    print(f"Total Turns: {total_turns}, Average: {avg_turns:.1f} turns per conversation")
    print()


async def example_performance_monitoring():
    """Example of performance monitoring for conversations"""
    print("⏱️  Example 6: Performance Monitoring for Multi-Turn Conversations")
    print("=" * 70)
    
    import time
    import psutil
    
    tester = FashionBotTester()
    scenario = tester.test_data["test_scenarios"][0]
    
    # Monitor performance
    process = psutil.Process()
    start_memory = process.memory_info().rss / 1024 / 1024  # MB
    
    start_time = time.time()
    result = await tester.run_comprehensive_test(scenario)
    end_time = time.time()
    
    end_memory = process.memory_info().rss / 1024 / 1024  # MB
    
    response_time = end_time - start_time
    memory_used = end_memory - start_memory
    conversation_length = result["string_evaluation"]["conversation_length"]
    
    print(f"Test: {scenario['id']}")
    print(f"Conversation Length: {conversation_length} turns")
    print(f"Total Response Time: {response_time:.2f} seconds")
    print(f"Average Time per Turn: {response_time/conversation_length:.2f} seconds")
    print(f"Memory Used: {memory_used:.2f} MB")
    print(f"Performance: {'✅ Good' if response_time < 10.0 else '⚠️  Slow'}")
    print()


async def example_conversation_analysis():
    """Example of analyzing conversation patterns"""
    print("📊 Example 7: Conversation Pattern Analysis")
    print("=" * 50)
    
    tester = FashionBotTester()
    
    # Analyze all conversation scenarios
    all_results = await tester.run_all_tests()
    
    # Calculate conversation statistics
    conversation_lengths = [r["string_evaluation"]["conversation_length"] for r in all_results]
    keyword_scores = [r["string_evaluation"]["overall_keyword_score"] for r in all_results]
    llm_scores = [r["llm_evaluation"].get("overall_score", 0) for r in all_results]
    
    print(f"Total Conversations: {len(all_results)}")
    print(f"Average Conversation Length: {sum(conversation_lengths)/len(conversation_lengths):.1f} turns")
    print(f"Average Keyword Score: {sum(keyword_scores)/len(keyword_scores):.2f}")
    print(f"Average LLM Score: {sum(llm_scores)/len(llm_scores):.2f}")
    
    # Analyze by conversation type
    conversation_types = {
        "Product Queries": ["basic_product_query_conversation", "product_selection_conversation", "stock_inquiry_conversation"],
        "Order Queries": ["order_status_conversation", "order_without_number_conversation"],
        "Customer Service": ["frustration_escalation_conversation", "return_refund_conversation"],
        "General Inquiries": ["general_inquiry_conversation"]
    }
    
    print("\nPerformance by Conversation Type:")
    for conv_type, scenario_ids in conversation_types.items():
        type_results = [r for r in all_results if r["scenario"]["id"] in scenario_ids]
        if type_results:
            avg_length = sum(r["string_evaluation"]["conversation_length"] for r in type_results) / len(type_results)
            avg_keyword = sum(r["string_evaluation"]["overall_keyword_score"] for r in type_results) / len(type_results)
            avg_llm = sum(r["llm_evaluation"].get("overall_score", 0) for r in type_results) / len(type_results)
            print(f"  {conv_type}: {len(type_results)} conversations, avg {avg_length:.1f} turns, keyword {avg_keyword:.2f}, LLM {avg_llm:.2f}")
    print()


async def main():
    """Run all examples"""
    print("🚀 Fashion Bot Multi-Turn Conversation Testing Framework - Examples")
    print("=" * 80)
    
    try:
        await example_basic_testing()
        await example_custom_evaluations()
        await example_configuration_customization()
        await example_batch_testing()
        await example_performance_monitoring()
        await example_conversation_analysis()
        
        # LangSmith example (commented out as it requires API key)
        # await example_langsmith_integration()
        
        print("✅ All examples completed successfully!")
        
    except Exception as e:
        print(f"❌ Error running examples: {e}")
        print("Make sure you have set up your environment variables correctly.")


if __name__ == "__main__":
    asyncio.run(main()) 