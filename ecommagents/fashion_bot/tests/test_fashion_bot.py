#!/usr/bin/env python3
"""
Comprehensive testing framework for Fashion Bot with multi-turn conversation support
Uses LLM-as-a-judge evaluation with Gemini 2.5 Flash Lite
"""
import asyncio
import json
import os
from typing import Dict, List, Any, Optional
from datetime import datetime

# Import environment utilities (this will auto-setup the environment)
from tests.env_utils import get_api_keys, validate_environment

# Import test configuration
from tests.test_config import TEST_THRESHOLDS, LLM_JUDGE_CONFIG

from fashion_bot.graph_context_meta import graph
from fashion_bot.product_data import PRODUCT_DATA
from langchain_core.messages import HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from langsmith import Client

from fashion_bot.main import handle, Query

# Optional pytest import for when running as pytest
try:
    import pytest
    PYTEST_AVAILABLE = True
except ImportError:
    PYTEST_AVAILABLE = False

if PYTEST_AVAILABLE:
    pytestmark = pytest.mark.skipif(
        not os.getenv(LLM_JUDGE_CONFIG.get("api_key_env_var", "GOOGLE_API_KEY")),
        reason=f"Missing {LLM_JUDGE_CONFIG.get('api_key_env_var', 'GOOGLE_API_KEY')} for LLM-judge integration tests",
    )


def get_llm_judge():
    """
    Create and return the LLM judge instance based on configuration.
    Uses Gemini 2.5 Flash Lite for fast, cost-effective evaluation.
    """
    api_key = os.getenv(LLM_JUDGE_CONFIG.get("api_key_env_var", "GOOGLE_API_KEY"))
    if not api_key:
        raise ValueError(f"API key not found. Set {LLM_JUDGE_CONFIG.get('api_key_env_var', 'GOOGLE_API_KEY')} environment variable.")
    
    return ChatGoogleGenerativeAI(
        model=LLM_JUDGE_CONFIG["model"],
        temperature=LLM_JUDGE_CONFIG["temperature"],
        max_tokens=LLM_JUDGE_CONFIG.get("max_tokens", 2000),
        google_api_key=api_key
    )


class FashionBotTester:
    def __init__(self, langsmith_api_key: str = None):
        self.langsmith_client = Client(api_key=langsmith_api_key) if langsmith_api_key else None
        self.llm_judge = get_llm_judge()
        self.test_data = self._load_test_data()
        
    def _load_test_data(self) -> Dict[str, Any]:
        """Load test scenarios from JSON file"""
        with open("tests/test_data.json", "r") as f:
            return json.load(f)
    
    async def run_conversation_test(self, scenario: Dict[str, Any]) -> Dict[str, Any]:
        """Run a multi-turn conversation test scenario"""
        try:
            conversation = scenario["conversation"]
            thread_id = f"test-thread-{scenario['id']}"
            
            # Initialize state for the conversation
            state = {
                "messages": [],
                "product_info": f"Fashion T-Shirt - Sizes: {', '.join(PRODUCT_DATA['sizes_available'])}, Colors: {', '.join(PRODUCT_DATA['colors_available'])}",
                "phone_number": None,
                "selected_order_id": None,
                "known_orders": None,
                "order_status_by_id": {}
            }
            
            conversation_results = []
            
            # Process each turn in the conversation
            for i, turn in enumerate(conversation):
                if turn["role"] == "customer":
                    # Add customer message to state
                    customer_message = HumanMessage(content=turn["message"])
                    state["messages"].append(customer_message)
                    
                    # Run the graph with current state
                    result = graph.invoke(
                        state, 
                        config={"configurable": {"thread_id": thread_id}}
                    )
                    
                    # Get bot response
                    bot_response = result["messages"][-1].content
                    
                    # Update state with bot response
                    state = result
                    
                    # Find the corresponding bot message with expected keywords
                    expected_keywords = []
                    expected_behavior = ""
                    
                    # Look for the next bot role in the conversation
                    if i + 1 < len(conversation) and conversation[i + 1]["role"] == "bot":
                        expected_keywords = conversation[i + 1].get("expected_keywords", [])
                        expected_behavior = conversation[i + 1].get("expected_behavior", "")
                    
                    conversation_results.append({
                        "turn": len(conversation_results) + 1,
                        "customer_message": turn["message"],
                        "bot_response": bot_response,
                        "expected_keywords": expected_keywords,
                        "expected_behavior": expected_behavior
                    })
            
            return {
                "scenario_id": scenario["id"],
                "conversation_results": conversation_results,
                "final_state": state
            }
        except Exception as e:
            print(f"   ⚠️ Error in conversation test for {scenario.get('id', 'unknown')}: {str(e)}")
            return {
                "scenario_id": scenario.get("id", "unknown"),
                "conversation_results": [],
                "final_state": {},
                "error": str(e)
            }
    
    async def llm_judge_evaluation(self, conversation_result: Dict[str, Any]) -> Dict[str, Any]:
        """Evaluate conversation using LLM as a judge"""
        
        # Check if conversation has results
        if not conversation_result.get("conversation_results"):
            return {"error": "No conversation results to evaluate"}
        
        # Create evaluation prompt for multi-turn conversations
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
        
        try:
            for i, turn in enumerate(conversation_result["conversation_results"]):
                conversation_text += f"Turn {i+1}:\n"
                conversation_text += f"Customer: {turn['customer_message']}\n"
                conversation_text += f"Bot: {turn['bot_response']}\n\n"
                expected_keywords_by_turn.append(f"Turn {i+1}: {', '.join(turn.get('expected_keywords', []))}")
            
            # Prepare evaluation data
            eval_data = {
                "conversation_text": conversation_text,
                "expected_keywords_by_turn": "\n".join(expected_keywords_by_turn)
            }
            
            # Get LLM evaluation
            chain = evaluation_prompt | self.llm_judge
            evaluation_result = await chain.ainvoke(eval_data)
            
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
        except Exception as e:
            return {"error": f"LLM evaluation failed: {str(e)}"}
    
    async def run_comprehensive_test(self, scenario: Dict[str, Any]) -> Dict[str, Any]:
        """Run a comprehensive test with LLM-as-a-judge evaluation"""
        import time
        
        # Track conversation time (model response time only)
        conversation_start = time.time()
        
        # Run the conversation test
        conversation_result = await self.run_conversation_test(scenario)
        
        conversation_end = time.time()
        conversation_time = conversation_end - conversation_start
        
        # Track evaluation time separately
        eval_start = time.time()
        
        # LLM judge evaluation (only evaluation method)
        llm_eval = await self.llm_judge_evaluation(conversation_result)
        
        eval_end = time.time()
        evaluation_time = eval_end - eval_start
        
        # Calculate conversation length for reporting
        conversation_length = len(conversation_result.get("conversation_results", []))
        
        # Pass/fail based solely on LLM judge score
        llm_score = llm_eval.get("overall_score", 0)
        passed = llm_score >= TEST_THRESHOLDS["individual_test_pass"]["llm_overall_score"]
        
        return {
            "scenario": scenario,
            "conversation_result": conversation_result,
            "llm_evaluation": llm_eval,
            "conversation_length": conversation_length,
            "passed": passed,
            "timing": {
                "conversation_time_seconds": conversation_time,
                "evaluation_time_seconds": evaluation_time,
                "total_time_seconds": conversation_time + evaluation_time
            }
        }
    
    async def run_all_tests(self) -> List[Dict[str, Any]]:
        """Run all test scenarios"""
        results = []
        for scenario in self.test_data["test_scenarios"]:
            result = await self.run_comprehensive_test(scenario)
            results.append(result)
        return results
    
    def generate_test_report(self, results: List[Dict[str, Any]]) -> str:
        """Generate a comprehensive test report using LLM-as-a-judge evaluation"""
        total_tests = len(results)
        passed_tests = sum(1 for r in results if r["passed"])
        pass_rate = (passed_tests / total_tests) * 100 if total_tests > 0 else 0
        
        # Calculate average LLM scores
        valid_llm_results = [r for r in results if "llm_evaluation" in r and "error" not in r["llm_evaluation"]]
        avg_llm_score = sum(r["llm_evaluation"].get("overall_score", 0) for r in valid_llm_results) / len(valid_llm_results) if valid_llm_results else 0
        
        report = f"""
# Fashion Bot Multi-Turn Conversation Test Report

## Summary
- Total Tests: {total_tests}
- Passed: {passed_tests}
- Failed: {total_tests - passed_tests}
- Pass Rate: {pass_rate:.1f}%
- Average LLM Score: {avg_llm_score:.2f}/5.0

## Detailed Results
"""
        
        for result in results:
            scenario = result["scenario"]
            conversation_result = result["conversation_result"]
            llm_eval = result["llm_evaluation"]
            conversation_length = result.get("conversation_length", len(conversation_result.get("conversation_results", [])))
            
            status = "✅ PASS" if result["passed"] else "❌ FAIL"
            
            report += f"""
### {scenario['id']}: {scenario['description']} {status}

**Conversation Summary:**
- Total Turns: {conversation_length}
- LLM Overall Score: {llm_eval.get('overall_score', 'N/A')}/5.0

**Conversation Flow:**
"""
            
            for turn in conversation_result.get("conversation_results", []):
                report += f"""
**Turn {turn['turn']}:**
- Customer: {turn['customer_message']}
- Bot: {turn['bot_response']}
"""
            
            report += f"""
**LLM Judge Results:**
- Overall Score: {llm_eval.get('overall_score', 'N/A')}
- Logical Correctness: {llm_eval.get('logical_correctness', {}).get('score', 'N/A')}
- Conciseness: {llm_eval.get('conciseness', {}).get('score', 'N/A')}
- Helpfulness: {llm_eval.get('helpfulness', {}).get('score', 'N/A')}
- Tone: {llm_eval.get('tone', {}).get('score', 'N/A')}
- Completeness: {llm_eval.get('completeness', {}).get('score', 'N/A')}
- Context Awareness: {llm_eval.get('context_awareness', {}).get('score', 'N/A')}
- Conversation Flow: {llm_eval.get('conversation_flow', {}).get('score', 'N/A')}
- Memory Retention: {llm_eval.get('memory_retention', {}).get('score', 'N/A')}

---
"""
        
        return report


# Pytest test functions (only if pytest is available)
if PYTEST_AVAILABLE:
    @pytest.fixture
    def tester():
        """Create a test instance"""
        return FashionBotTester()

    @pytest.mark.asyncio
    async def test_basic_product_query_conversation(tester):
        """Test basic product query conversation scenario"""
        scenario = next(s for s in tester.test_data["test_scenarios"] if s["id"] == "basic_product_query_conversation")
        result = await tester.run_comprehensive_test(scenario)
        
        assert result["passed"], f"Test failed: LLM score = {result['llm_evaluation'].get('overall_score', 'N/A')}"

    @pytest.mark.asyncio
    async def test_order_status_conversation(tester):
        """Test order status conversation scenario"""
        scenario = next(s for s in tester.test_data["test_scenarios"] if s["id"] == "order_status_conversation")
        result = await tester.run_comprehensive_test(scenario)
        
        # Order status conversations should handle multiple turns appropriately
        assert result["conversation_length"] >= TEST_THRESHOLDS["individual_test_pass"]["conversation_length"]

    @pytest.mark.asyncio
    async def test_frustration_escalation_conversation(tester):
        """Test frustration escalation conversation scenario"""
        scenario = next(s for s in tester.test_data["test_scenarios"] if s["id"] == "frustration_escalation_conversation")
        result = await tester.run_comprehensive_test(scenario)
        
        # Check if frustration was handled appropriately using LLM judge
        assert result["passed"], f"Test failed: LLM score = {result['llm_evaluation'].get('overall_score', 'N/A')}"

    @pytest.mark.asyncio
    async def test_all_conversations(tester):
        """Run all conversation test scenarios"""
        results = await tester.run_all_tests()
        
        # Generate and save report
        report = tester.generate_test_report(results)
        with open("tests/test_report.md", "w") as f:
            f.write(report)
        
        # Assert overall pass rate
        pass_rate = (sum(1 for r in results if r["passed"]) / len(results)) * 100
        assert pass_rate >= TEST_THRESHOLDS["overall_pass_rate"], f"Overall pass rate {pass_rate:.1f}% is below {TEST_THRESHOLDS['overall_pass_rate']}%"

    # Integration test for the FastAPI endpoint
    @pytest.mark.asyncio
    async def test_fastapi_endpoint():
        """Test the FastAPI endpoint directly"""
        from fastapi.testclient import TestClient
        from fashion_bot.main import app
        
        client = TestClient(app)
        
        # Test basic endpoint
        response = client.get("/")
        assert response.status_code == 200
        payload = response.json()
        assert payload.get("status") == "Fashion bot is live"
        assert "endpoints" in payload
        
        # Test support-response endpoint
        test_query = {
            "product": "Fashion T-Shirt",
            "question": "What sizes are available?",
            "thread_id": "test-thread-api"
        }
        
        response = client.post("/support-response", json=test_query)
        assert response.status_code == 200
        assert "response" in response.json()
        assert len(response.json()["response"]) > 0


if __name__ == "__main__":
    # Run tests manually
    async def main():
        tester = FashionBotTester()
        results = await tester.run_all_tests()
        report = tester.generate_test_report(results)
        print(report)
        
        # Save detailed results
        with open("tests/detailed_test_results.json", "w") as f:
            json.dump(results, f, indent=2, default=str)
    
    asyncio.run(main()) 
