#!/usr/bin/env python3
"""
LangSmith-traced testing framework for Fashion Bot with real-time tracing
"""
import asyncio
import json
from typing import Dict, List, Any, Optional
from datetime import datetime
import os

# Import environment utilities
from tests.env_utils import get_api_keys, validate_environment

# Import test configuration
from tests.test_config import TEST_THRESHOLDS, LLM_JUDGE_CONFIG, LANGSMITH_CONFIG

from fashion_bot.graph_context_meta import graph
from fashion_bot.product_data import PRODUCT_DATA
from langchain_core.messages import HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langsmith import Client, RunTree
from langsmith.run_helpers import traceable

from fashion_bot.main import handle, Query


class LangSmithTracedTester:
    """
    Fashion Bot tester with real-time LangSmith tracing during test execution.
    
    This approach provides:
    - Real-time tracing of conversation flow
    - Detailed LLM call traces
    - Graph execution traces (LangGraph node-by-node)
    - Performance metrics during execution
    - Error tracking with full context
    """
    
    def __init__(self, langsmith_api_key: str = None):
        openai_key, langsmith_key = get_api_keys()
        
        # Initialize LangSmith client with proper configuration
        if langsmith_api_key or langsmith_key:
            self.langsmith_client = Client(api_key=langsmith_api_key or langsmith_key)
            # Set the project name for all traces
            import os
            os.environ["LANGCHAIN_PROJECT"] = LANGSMITH_CONFIG["project_name"]
            os.environ["LANGSMITH_PROJECT"] = LANGSMITH_CONFIG["project_name"]
            os.environ["LANGSMITH_TRACING_V2"] = "true"
            os.environ["LANGCHAIN_TRACING_V2"] = "true"
            print(f"🔗 LangSmith tracing enabled for project: {LANGSMITH_CONFIG['project_name']}")
            print(f"🔑 Using API key: {langsmith_api_key or langsmith_key[:10]}...")
            print(f"🔧 Tracing V2 enabled: {os.getenv('LANGSMITH_TRACING_V2')}")
        else:
            self.langsmith_client = None
            print("⚠️  No LangSmith API key found. Tracing will be limited.")
        
        # Initialize LLM judge with tracing
        self.llm_judge = ChatOpenAI(
            model=LLM_JUDGE_CONFIG["model"], 
            temperature=LLM_JUDGE_CONFIG["temperature"],
            openai_api_key=openai_key
        )
        
        self.test_data = self._load_test_data()
        self.project_name = LANGSMITH_CONFIG["project_name"]
        
        # Test LangSmith connection if available
        if self.langsmith_client:
            try:
                # Debug: Show current LangSmith configuration
                import os
                print(f"🔧 LangSmith Configuration:")
                print(f"   API Key: {'✅ Set' if os.getenv('LANGSMITH_API_KEY') else '❌ Not set'}")
                print(f"   Project: {os.getenv('LANGCHAIN_PROJECT', 'Not set')}")
                print(f"   Endpoint: {os.getenv('LANGCHAIN_ENDPOINT', 'Default')}")
                print(f"   Tracing V2: {os.getenv('LANGSMITH_TRACING_V2', 'Not set')}")
                print(f"   LangChain Tracing V2: {os.getenv('LANGCHAIN_TRACING_V2', 'Not set')}")
                
                # Create a simple test trace to verify connection
                from langsmith import traceable
                @traceable(name="langsmith-connection-test", project_name=self.project_name)
                def test_connection():
                    return {"status": "connected", "project": self.project_name}
                
                test_result = test_connection()
                print(f"✅ LangSmith connection test successful: {test_result}")
                print(f"🔍 Check LangSmith dashboard for trace: 'langsmith-connection-test'")
            except Exception as e:
                print(f"❌ LangSmith connection test failed: {e}")
        
    def _load_test_data(self) -> Dict[str, Any]:
        """Load test scenarios from JSON file"""
        with open("tests/test_data.json", "r") as f:
            return json.load(f)
    
    @traceable(name="fashion-bot-conversation-test", project_name=LANGSMITH_CONFIG["project_name"])
    async def run_traced_conversation_test(self, scenario: Dict[str, Any]) -> Dict[str, Any]:
        """
        Run a conversation test with full LangSmith tracing.
        
        This method is decorated with @traceable to enable real-time tracing
        of the entire conversation flow, including:
        - Each conversation turn
        - LLM calls and responses
        - Graph execution steps
        - Evaluation processes
        """
        conversation = scenario["conversation"]
        thread_id = f"traced-test-thread-{scenario['id']}"
        
        # Debug: Print tracing information
        print(f"🔍 Starting traced conversation test for scenario: {scenario['id']}")
        print(f"📝 Thread ID: {thread_id}")
        print(f"🏷️  Project: {self.project_name}")
        
        # Get initial state from scenario or use defaults
        initial_state = scenario.get("initial_state", {})
        
        # Initialize state for the conversation
        state = {
            "messages": [],
            "product_info": f"Fashion T-Shirt - Sizes: {', '.join(PRODUCT_DATA['sizes_available'])}, Colors: {', '.join(PRODUCT_DATA['colors_available'])}",
            "phone_number": initial_state.get("phone_number"),
            "selected_order_id": initial_state.get("selected_order_id"),
            "known_orders": initial_state.get("known_orders"),
            "order_status_by_id": initial_state.get("order_status_by_id", {})
        }
        
        # Add any additional initial state fields
        for key, value in initial_state.items():
            if key not in state:
                state[key] = value
        
        conversation_results = []
        
        # Process each turn in the conversation with tracing
        for i, turn in enumerate(conversation):
            if turn["role"] == "customer":
                # Add customer message to state
                customer_message = HumanMessage(content=turn["message"])
                state["messages"].append(customer_message)
                
                # Run the graph with tracing enabled
                # This will automatically trace all LangGraph execution
                result = await self._run_graph_with_tracing(
                    state, 
                    thread_id,
                    turn_number=i + 1,
                    customer_message=turn["message"]
                )
                
                # Get bot response
                bot_response = result["messages"][-1].content
                
                # Update state with bot response
                state = result
                
                conversation_results.append({
                    "turn": i + 1,
                    "customer_message": turn["message"],
                    "bot_response": bot_response,
                    "expected_keywords": conversation[i + 1]["expected_keywords"] if i + 1 < len(conversation) else [],
                    "expected_behavior": conversation[i + 1]["expected_behavior"] if i + 1 < len(conversation) else ""
                })
        
        return {
            "scenario_id": scenario["id"],
            "conversation_results": conversation_results,
            "final_state": state
        }
    
    @traceable(name="fashion-bot-graph-execution", project_name=LANGSMITH_CONFIG["project_name"])
    async def _run_graph_with_tracing(self, state: Dict, thread_id: str, turn_number: int, customer_message: str) -> Dict:
        """
        Run the LangGraph with tracing enabled.
        
        This method traces the graph execution step-by-step, showing:
        - Which nodes are executed
        - Input/output for each node
        - Decision points and routing
        - Performance metrics
        """
        # Configure tracing for this specific graph execution
        config = {
            "configurable": {"thread_id": thread_id},
            "metadata": {
                "turn_number": turn_number,
                "customer_message": customer_message,
                "test_type": "conversation_test"
            },
            "tags": ["fashion-bot", "conversation-test", f"turn-{turn_number}"]
        }
        
        # Execute graph with tracing
        result = graph.invoke(state, config=config)
        
        return result
    
    @traceable(name="fashion-bot-string-evaluation", project_name=LANGSMITH_CONFIG["project_name"])
    def string_matching_evaluation(self, conversation_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Evaluate conversation using string matching with tracing.
        
        This traces the evaluation process, showing:
        - Keyword matching logic
        - Score calculations
        - Evaluation criteria
        """
        # Evaluate each turn ensuring the turn numbers match those stored in
        # `conversation_results` so that downstream reporting logic can find the
        # correct evaluation for every turn.

        turn_evaluations: List[Dict[str, Any]] = []
        overall_keyword_score = 0.0

        for turn_dict in conversation_result["conversation_results"]:
            turn_number = turn_dict["turn"]
            response = turn_dict["bot_response"]
            expected_keywords = turn_dict["expected_keywords"]

            response_lower = response.lower()

            # Check for keyword presence with detailed tracing
            found_keywords: List[str] = []
            missing_keywords: List[str] = []

            for keyword in expected_keywords:
                if keyword.lower() in response_lower:
                    found_keywords.append(keyword)
                else:
                    missing_keywords.append(keyword)

            keyword_score = (
                len(found_keywords) / len(expected_keywords) if expected_keywords else 1.0
            )
            overall_keyword_score += keyword_score

            turn_evaluations.append(
                {
                    "turn": turn_number,
                    "keyword_score": keyword_score,
                    "found_keywords": found_keywords,
                    "missing_keywords": missing_keywords,
                    "response_length": len(response),
                }
            )

        # Calculate average keyword score
        avg_keyword_score = (
            overall_keyword_score / len(turn_evaluations) if turn_evaluations else 0.0
        )

        all_responses = [td["bot_response"] for td in conversation_result["conversation_results"]]

        return {
            "overall_keyword_score": avg_keyword_score,
            "turn_evaluations": turn_evaluations,
            "conversation_length": len(conversation_result["conversation_results"]),
            "has_product_info": any(
                size in " ".join(all_responses) for size in PRODUCT_DATA["sizes_available"]
            ),
            "has_color_info": any(
                color in " ".join(all_responses) for color in PRODUCT_DATA["colors_available"]
            ),
        }
    
    @traceable(name="fashion-bot-llm-evaluation", project_name=LANGSMITH_CONFIG["project_name"])
    async def llm_judge_evaluation(self, conversation_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Evaluate conversation using LLM as a judge with tracing.
        
        This traces the LLM evaluation process, showing:
        - Prompt construction
        - LLM response
        - Score parsing
        - Evaluation reasoning
        """
        
        # Create evaluation prompt
        evaluation_prompt = ChatPromptTemplate.from_template("""
You are an expert evaluator for a fashion e-commerce chatbot. Evaluate the following multi-turn conversation based on the given criteria.

Conversation:
{conversation_text}

Evaluation Criteria:
1. Logical Correctness (1-5): Do the responses accurately address the user's questions and provide correct information?
2. Conciseness (1-5): Are the responses brief and to the point without unnecessary verbosity?
3. Helpfulness (1-5): Do the responses provide actionable information or clear next steps?
4. Tone (1-5): Do the responses maintain a professional and helpful tone throughout?
5. Completeness (1-5): Do the responses address all aspects of the user's questions?
6. Context Awareness (1-5): Are the responses appropriate for the context and conversation flow?
7. Conversation Flow (1-5): Does the conversation feel natural and build on previous exchanges?
8. Memory Retention (1-5): Does the bot remember and reference information from previous messages?

Please provide scores for each criterion and a brief explanation for each score.

Expected Keywords by Turn: {expected_keywords_by_turn}

Respond in the following JSON format:
{{
    "logical_correctness": {{"score": 4, "explanation": "..."}},
    "conciseness": {{"score": 3, "explanation": "..."}},
    "helpfulness": {{"score": 4, "explanation": "..."}},
    "tone": {{"score": 5, "explanation": "..."}},
    "completeness": {{"score": 4, "explanation": "..."}},
    "context_awareness": {{"score": 4, "explanation": "..."}},
    "conversation_flow": {{"score": 4, "explanation": "..."}},
    "memory_retention": {{"score": 4, "explanation": "..."}},
    "overall_score": 4.0,
    "summary": "Overall assessment of the conversation quality"
}}
""")
        
        # Format conversation text
        conversation_text = ""
        expected_keywords_by_turn = []
        
        for i, turn in enumerate(conversation_result["conversation_results"]):
            conversation_text += f"Turn {i+1}:\n"
            conversation_text += f"Customer: {turn['customer_message']}\n"
            conversation_text += f"Bot: {turn['bot_response']}\n\n"
            expected_keywords_by_turn.append(f"Turn {i+1}: {', '.join(turn['expected_keywords'])}")
        
        # Prepare evaluation data
        eval_data = {
            "conversation_text": conversation_text,
            "expected_keywords_by_turn": "\n".join(expected_keywords_by_turn)
        }
        
        # Get LLM evaluation with tracing
        chain = evaluation_prompt | self.llm_judge
        evaluation_result = await chain.ainvoke(eval_data)
        
        try:
            # Parse JSON response
            import re
            json_match = re.search(r'\{.*\}', evaluation_result.content, re.DOTALL)
            if json_match:
                evaluation_json = json.loads(json_match.group())
                return evaluation_json
            else:
                return {"error": "Could not parse LLM evaluation response"}
        except json.JSONDecodeError:
            return {"error": "Invalid JSON in LLM evaluation response"}
    
    @traceable(name="fashion-bot-comprehensive-test", project_name=LANGSMITH_CONFIG["project_name"])
    async def run_comprehensive_traced_test(self, scenario: Dict[str, Any]) -> Dict[str, Any]:
        """
        Run a comprehensive test with full tracing enabled.
        
        This method traces the entire test process:
        - Conversation execution
        - String matching evaluation
        - LLM judge evaluation
        - Overall test assessment
        """
        # Run the conversation test with tracing
        conversation_result = await self.run_traced_conversation_test(scenario)
        
        # String matching evaluation with tracing
        string_eval = self.string_matching_evaluation(conversation_result)
        
        # LLM judge evaluation with tracing
        llm_eval = await self.llm_judge_evaluation(conversation_result)
        
        return {
            "scenario": scenario,
            "conversation_result": conversation_result,
            "string_evaluation": string_eval,
            "llm_evaluation": llm_eval,
            "passed": string_eval["overall_keyword_score"] >= TEST_THRESHOLDS["individual_test_pass"]["keyword_score"] and llm_eval.get("overall_score", 0) >= TEST_THRESHOLDS["individual_test_pass"]["llm_overall_score"]
        }
    
    @traceable(name="fashion-bot-batch-testing", project_name=LANGSMITH_CONFIG["project_name"])
    async def run_all_traced_tests(self, max_tests: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Run test scenarios with comprehensive tracing.
        
        Args:
            max_tests: Maximum number of tests to run. If None, runs all tests.
        
        This traces the entire batch testing process, showing:
        - Test selection and execution order
        - Individual test results
        - Batch performance metrics
        - Overall test suite assessment
        """
        results = []
        all_scenarios = self.test_data["test_scenarios"]
        total_scenarios = len(all_scenarios)
        
        # Determine how many tests to run
        if max_tests is not None:
            scenarios_to_run = all_scenarios[:max_tests]
            actual_tests = min(max_tests, total_scenarios)
            print(f"Starting to run {actual_tests}/{total_scenarios} test scenarios with LangSmith tracing...")
        else:
            scenarios_to_run = all_scenarios
            print(f"Starting to run {total_scenarios} test scenarios with LangSmith tracing...")
        
        for i, scenario in enumerate(scenarios_to_run, 1):
            print(f"Running traced test {i}/{len(scenarios_to_run)}: {scenario['id']} - {scenario['description']}")
            
            result = await self.run_comprehensive_traced_test(scenario)
            results.append(result)
            
            # Print completion status
            status = "✅ PASSED" if result["passed"] else "❌ FAILED"
            keyword_score = result["string_evaluation"]["overall_keyword_score"]
            llm_score = result["llm_evaluation"].get("overall_score", "N/A")
            print(f"Completed traced test {i}/{len(scenarios_to_run)}: {scenario['id']} - {status} (Keyword: {keyword_score:.2f}, LLM: {llm_score})")
        
        print(f"All {len(scenarios_to_run)} traced tests completed!")
        return results


# Example usage
async def main():
    """Example usage of LangSmith-traced testing"""
    import sys
    import argparse
    
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Run Fashion Bot tests with LangSmith tracing")
    parser.add_argument("-top-x", type=int, metavar="N", 
                       help="Run only the top N tests (e.g., -top-x 5 runs 5 tests)")
    
    args = parser.parse_args()
    max_tests = args.top_x
    
    # Validate the argument
    if max_tests is not None and max_tests <= 0:
        print("❌ Error: Number of tests must be positive")
        return
    
    # Initialize traced tester
    langsmith_key = os.getenv("LANGSMITH_API_KEY")
    if not langsmith_key:
        print("⚠️  LANGSMITH_API_KEY not found. Tracing will be limited.")
        print("   Set LANGSMITH_API_KEY to enable full tracing capabilities.")
    
    tester = LangSmithTracedTester(langsmith_api_key=langsmith_key)
    
    # Run tests with tracing
    if max_tests:
        print(f"🎯 Running {max_tests} test(s) as requested...")
        results = await tester.run_all_traced_tests(max_tests=max_tests)
    else:
        print("🎯 Running all available tests...")
        results = await tester.run_all_traced_tests()
    
    # Generate summary
    total_tests = len(results)
    passed_tests = sum(1 for r in results if r["passed"])
    pass_rate = (passed_tests / total_tests) * 100 if total_tests > 0 else 0
    
    print(f"\n🎉 Traced testing completed!")
    print(f"📊 Results: {passed_tests}/{total_tests} tests passed ({pass_rate:.1f}%)")
    
    if langsmith_key:
        print(f"🔗 View detailed traces in LangSmith: https://smith.langchain.com/")
        print(f"📁 Project: {tester.project_name}")
        
        # Create a final test trace to verify tracing is working
        try:
            from langsmith import traceable
            @traceable(name="test-completion-verification", project_name=tester.project_name)
            def verify_tracing():
                return {"message": "Tracing verification complete", "timestamp": datetime.now().isoformat()}
            
            verify_tracing()
            print(f"✅ Final tracing verification completed - check LangSmith for 'test-completion-verification' trace")
        except Exception as e:
            print(f"❌ Final tracing verification failed: {e}")
    
    return results


if __name__ == "__main__":
    asyncio.run(main()) 