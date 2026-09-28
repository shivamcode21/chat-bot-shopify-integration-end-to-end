#!/usr/bin/env python3
"""
Context-Aware Agent Test Runner.

This is the core engine that:
1. Loads state fixtures and seeds conversation state before running tests
2. Runs multi-turn conversations through the graph with proper client_id
3. Evaluates results using both base criteria AND context-specific criteria
4. Tracks tool calls and state mutations for regression detection

Works with the existing graph infrastructure — no mocking of the graph itself.
"""
import asyncio
import json
import os
import re
import time
import logging
import copy
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime

# Environment setup
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.env_utils import setup_environment
setup_environment()

from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI

from tests.fixture_loader import (
    build_test_state,
    load_client_profile,
    load_context_assertions,
    list_available_clients,
    list_available_fixtures,
)
from tests.test_config import (
    EVALUATION_CRITERIA,
    TEST_THRESHOLDS,
    LLM_JUDGE_CONFIG,
)
from tests.context_test_config import (
    CONTEXT_EVALUATION_CRITERIA,
    CONTEXT_TEST_THRESHOLDS,
)
from tests.tool_mocker import (
    mock_tools_for_agent,
    get_tool_call_log,
    get_tool_call_summary,
    reset_tool_call_log,
    ToolCallVerifier,
    MockToolDataProvider,
)

logger = logging.getLogger("agent_test_runner")


# ==================== LLM JUDGE ====================

def get_llm_judge():
    """Create the LLM judge for evaluation."""
    api_key = os.getenv(LLM_JUDGE_CONFIG.get("api_key_env_var", "GOOGLE_API_KEY"))
    if not api_key:
        raise ValueError(
            f"API key not found. Set {LLM_JUDGE_CONFIG.get('api_key_env_var', 'GOOGLE_API_KEY')} env var."
        )
    return ChatGoogleGenerativeAI(
        model=LLM_JUDGE_CONFIG["model"],
        temperature=LLM_JUDGE_CONFIG["temperature"],
        max_tokens=LLM_JUDGE_CONFIG.get("max_tokens", 2000),
        google_api_key=api_key,
    )


# ==================== CORE TEST RUNNER ====================

class ContextAwareTestRunner:
    """
    Runs agent tests with pre-seeded conversation state and evaluates
    both response quality AND context utilization.
    
    Supports two modes:
    - use_mock=True (default): Tools return deterministic mock data. No real API calls.
      This isolates testing to agent behavior — are the RIGHT tools called?
    - use_mock=False: Tools call real APIs but every call is still intercepted and logged.
    """

    def __init__(self, langsmith_api_key: str = None, use_mock: bool = True):
        self.llm_judge = get_llm_judge()
        self.results: List[Dict[str, Any]] = []
        self.use_mock = use_mock

    # ---------- graph loading ----------
    @staticmethod
    def _get_graph():
        """Import and return the conversation graph."""
        from fashion_bot.graph_context_meta import graph
        return graph

    # ---------- tool interception ----------
    def _patch_tool_registry(self, scenario_id: str = None):
        """
        Monkey-patch get_tools_for_agent at the USAGE SITE (generic_skill_node)
        so the graph's skill nodes use intercepted/mocked tools.
        
        IMPORTANT: generic_skill_node.py does a direct import:
            from fashion_bot.core.tool_registry import get_tools_for_agent
        So patching tool_registry.get_tools_for_agent alone won't work —
        we must patch the reference in the module that actually calls it.
        """
        from fashion_bot.core import tool_registry
        from fashion_bot.nodes import generic_skill_node

        # Save the original function reference from the usage site
        original_get_tools = generic_skill_node.get_tools_for_agent

        def patched_get_tools(agent_name, state, messages_list, client_id, **extra):
            # Get the real tools first
            tools = original_get_tools(agent_name, state, messages_list, client_id, **extra)
            # Wrap with interceptor + mock
            return mock_tools_for_agent(
                tools,
                state=state,
                use_mock=self.use_mock,
                scenario_id=scenario_id,
            )

        # Patch at BOTH locations to be safe
        generic_skill_node.get_tools_for_agent = patched_get_tools
        tool_registry.get_tools_for_agent = patched_get_tools
        return original_get_tools  # Return original for cleanup

    def _restore_tool_registry(self, original_func):
        """Restore the original get_tools_for_agent after test."""
        from fashion_bot.core import tool_registry
        from fashion_bot.nodes import generic_skill_node
        generic_skill_node.get_tools_for_agent = original_func
        tool_registry.get_tools_for_agent = original_func

    # ---------- run a single scenario ----------
    async def run_scenario(self, scenario: Dict[str, Any]) -> Dict[str, Any]:
        """
        Run a single test scenario end-to-end:
        1. Build initial state from fixture
        2. Patch tool registry for interception/mocking
        3. Execute each customer turn through the graph
        4. Collect bot responses + tool calls + state snapshots
        5. Evaluate with LLM judge (base + context criteria)
        6. Verify tool call expectations
        """
        scenario_id = scenario.get("id", "unknown")
        logger.info(f"▶️  Running scenario: {scenario_id}")

        # 1. Build seeded state
        state = build_test_state(scenario)
        initial_state_snapshot = _snapshot_state(state)

        graph = self._get_graph()
        conversation = scenario.get("conversation", [])
        context_assertions = load_context_assertions(scenario)

        conversation_results = []
        all_tool_calls: List[Dict] = []
        state_mutations: List[Dict] = []

        # Set client_id in context var for tools
        try:
            from fashion_bot.client_context import set_client_id
            set_client_id(state.get("client_id"))
        except ImportError:
            pass

        # 2. Patch tool registry for interception
        reset_tool_call_log()
        original_get_tools = self._patch_tool_registry(scenario_id)

        # Set scenario-specific mock overrides if defined
        mock_overrides = scenario.get("mock_overrides", {})
        if mock_overrides:
            MockToolDataProvider.set_scenario_overrides(scenario_id, mock_overrides)

        try:
            # 3. Walk through conversation turns
            conv_start = time.time()
            for i, turn in enumerate(conversation):
                if turn["role"] != "customer":
                    continue

                customer_msg = turn["message"]
                state["messages"] = state.get("messages", []) + [
                    HumanMessage(content=customer_msg)
                ]

                # Reset per-turn log to track tool calls per turn
                pre_turn_log_len = len(get_tool_call_log())

                # Invoke graph
                try:
                    thread_id = f"test-{scenario_id}-{i}"
                    config = {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": "agent_test",
                        }
                    }
                    result = graph.invoke(state, config=config)
                except Exception as e:
                    logger.error(f"Graph invoke error at turn {i}: {e}")
                    conversation_results.append({
                        "turn": len(conversation_results) + 1,
                        "customer_message": customer_msg,
                        "bot_response": f"[ERROR: {e}]",
                        "expected_behavior": _get_expected_behavior(conversation, i),
                        "expected_keywords": _get_expected_keywords(conversation, i),
                        "tool_calls": [],
                    })
                    continue

                # Capture tool calls for this turn
                full_log = get_tool_call_log()
                turn_tool_calls = full_log[pre_turn_log_len:]

                # Extract bot response
                bot_response = ""
                if "messages" in result and result["messages"]:
                    last_msg = result["messages"][-1]
                    bot_response = last_msg.content if hasattr(last_msg, "content") else str(last_msg)
                elif result.get("customer_message"):
                    bot_response = result["customer_message"]

                # Track state mutations
                new_snapshot = _snapshot_state(result)
                mutation = _diff_states(initial_state_snapshot, new_snapshot)
                if mutation:
                    state_mutations.append({"after_turn": i, "changes": mutation})

                # Update running state
                state = result

                conversation_results.append({
                    "turn": len(conversation_results) + 1,
                    "customer_message": customer_msg,
                    "bot_response": bot_response,
                    "expected_behavior": _get_expected_behavior(conversation, i),
                    "expected_keywords": _get_expected_keywords(conversation, i),
                    "context_check": _get_context_check(conversation, i),
                    "tool_calls": [
                        {"tool": tc["tool"], "args": tc.get("args", {})}
                        for tc in turn_tool_calls
                    ],
                })

            conv_time = time.time() - conv_start
            all_tool_calls = get_tool_call_log()

        finally:
            # 4. Restore original tool registry
            self._restore_tool_registry(original_get_tools)
            MockToolDataProvider.clear_scenario_overrides()

        # 5. Evaluate
        eval_start = time.time()

        conversation_result_obj = {
            "scenario_id": scenario_id,
            "conversation_results": conversation_results,
            "final_state": state,
        }

        # Base LLM evaluation
        base_eval = await self._evaluate_base(conversation_result_obj)

        # Context-aware evaluation (only if assertions exist)
        context_eval = {}
        if context_assertions:
            context_eval = await self._evaluate_context(
                conversation_result_obj,
                context_assertions,
                initial_state_snapshot,
                scenario.get("target_agent"),
            )

        eval_time = time.time() - eval_start

        # 6. Tool call verification
        tool_expectations = scenario.get("tool_expectations", {})
        tool_verification = {}
        if tool_expectations:
            tool_verification = ToolCallVerifier.verify(tool_expectations, all_tool_calls)
            if not tool_verification["passed"]:
                logger.warning(
                    f"🔴 Tool verification FAILED for {scenario_id}: "
                    + "; ".join(tool_verification["details"])
                )
        else:
            # Even without explicit expectations, still capture the summary
            tool_verification = {
                "passed": True,
                "total_calls": len(all_tool_calls),
                "tools_called": [tc["tool"] for tc in all_tool_calls],
                "details": [],
            }

        # 7. Pass/fail logic
        base_score = base_eval.get("overall_score", 0)
        context_score = context_eval.get("overall_context_score", 0) if context_eval else None

        if context_score is not None:
            w_base = CONTEXT_TEST_THRESHOLDS["base_criteria_weight"]
            w_ctx = CONTEXT_TEST_THRESHOLDS["context_criteria_weight"]
            combined = (base_score * w_base) + (context_score * w_ctx)
            passed = combined >= CONTEXT_TEST_THRESHOLDS["individual_test_pass"]["combined_weighted_score"]
        else:
            combined = base_score
            passed = base_score >= TEST_THRESHOLDS["individual_test_pass"]["llm_overall_score"]

        # Tool verification can override pass/fail
        if tool_expectations and not tool_verification["passed"]:
            passed = False

        test_result = {
            "scenario": scenario,
            "conversation_result": conversation_result_obj,
            "llm_evaluation": base_eval,
            "context_evaluation": context_eval,
            "tool_verification": tool_verification,
            "tool_calls_log": all_tool_calls,
            "combined_score": round(combined, 2),
            "conversation_length": len(conversation_results),
            "passed": passed,
            "state_mutations": state_mutations,
            "initial_state_fixture": scenario.get("initial_state_fixture", "cold_start"),
            "client_name": scenario.get("client_name", "default"),
            "timing": {
                "conversation_time_seconds": round(conv_time, 2),
                "evaluation_time_seconds": round(eval_time, 2),
                "total_time_seconds": round(conv_time + eval_time, 2),
            },
        }

        self.results.append(test_result)
        return test_result

    # ---------- batch run ----------
    async def run_all_scenarios(
        self,
        scenarios: List[Dict[str, Any]],
        test_id: str = None,
        client_filter: str = None,
        max_workers: int = 1,
    ) -> List[Dict[str, Any]]:
        """
        Run multiple scenarios with optional filters and parallel execution.

        Args:
            scenarios: List of scenario dicts
            test_id: Optional — run only this scenario
            client_filter: Optional — run only scenarios for this client
            max_workers: Number of parallel workers (default: 1 = sequential)
        """
        if test_id:
            scenarios = [s for s in scenarios if s.get("id") == test_id]
        if client_filter:
            scenarios = [s for s in scenarios if s.get("client_name") == client_filter]

        mock_label = "🔒 MOCK" if self.use_mock else "🌐 REAL APIs"
        parallel_label = f" (parallel: {max_workers} workers)" if max_workers > 1 else ""
        print(f"\n🚀 Running {len(scenarios)} context-aware test scenarios ({mock_label}){parallel_label}")
        print("=" * 70)

        if max_workers == 1:
            # Sequential execution (original behavior)
            results = []
            for idx, scenario in enumerate(scenarios, 1):
                result = await self._run_single_scenario_with_output(scenario, idx, len(scenarios))
                results.append(result)
        else:
            # Parallel execution
            semaphore = asyncio.Semaphore(max_workers)
            completed = 0
            total = len(scenarios)
            
            async def run_with_semaphore(scenario, idx):
                nonlocal completed
                async with semaphore:
                    result = await self._run_single_scenario_with_output(scenario, idx, total)
                    completed += 1
                    return result
            
            # Create tasks for all scenarios
            tasks = [
                run_with_semaphore(scenario, idx)
                for idx, scenario in enumerate(scenarios, 1)
            ]
            
            # Run all in parallel (with semaphore limiting concurrency)
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Handle any exceptions
            final_results = []
            for r in results:
                if isinstance(r, Exception):
                    logger.error(f"   💥 Unhandled error: {r}")
                    final_results.append({
                        "scenario": {"id": "?"},
                        "conversation_result": {"conversation_results": []},
                        "llm_evaluation": {"error": str(r)},
                        "context_evaluation": {},
                        "tool_verification": {},
                        "tool_calls_log": [],
                        "combined_score": 0,
                        "conversation_length": 0,
                        "passed": False,
                        "timing": {},
                    })
                else:
                    final_results.append(r)
            results = final_results

        # Summary
        passed = sum(1 for r in results if r["passed"])
        total = len(results)
        rate = (passed / total * 100) if total else 0
        print(f"\n{'='*70}")
        print(f"📊 Results: {passed}/{total} passed ({rate:.1f}%)")
        print(f"{'='*70}")

        return results

    async def _run_single_scenario_with_output(
        self, scenario: Dict[str, Any], idx: int, total: int
    ) -> Dict[str, Any]:
        """Run a single scenario and print output (used by both sequential and parallel modes)."""
        sid = scenario.get("id", "?")
        fixture = scenario.get("initial_state_fixture", "cold_start")
        client = scenario.get("client_name", "default")

        print(f"\n[{idx}/{total}] {sid}")
        print(f"   Client: {client} | Fixture: {fixture}")

        try:
            result = await self.run_scenario(scenario)
            status = "✅ PASS" if result["passed"] else "❌ FAIL"
            print(f"   {status} | Combined={result['combined_score']}/5")
            if result.get("context_evaluation"):
                ctx = result["context_evaluation"]
                print(f"   Context Score: {ctx.get('overall_context_score', 'N/A')}/5")

            # Show tool calls summary
            tool_log = result.get("tool_calls_log", [])
            if tool_log:
                tool_names = [tc["tool"] for tc in tool_log]
                print(f"   🔧 Tools ({len(tool_log)}): {' → '.join(tool_names)}")
            else:
                print(f"   🔧 Tools: (none)")

            # Show tool verification result
            tv = result.get("tool_verification", {})
            if tv.get("forbidden_detected"):
                print(f"   🔴 FORBIDDEN TOOLS CALLED: {tv['forbidden_detected']}")
            if tv.get("expected_missing"):
                print(f"   ⚠️  Expected tools NOT called: {tv['expected_missing']}")

            return result

        except Exception as e:
            logger.error(f"   💥 Error: {e}")
            result = {
                "scenario": scenario,
                "conversation_result": {"conversation_results": []},
                "llm_evaluation": {"error": str(e)},
                "context_evaluation": {},
                "tool_verification": {},
                "tool_calls_log": [],
                "combined_score": 0,
                "conversation_length": 0,
                "passed": False,
                "timing": {},
            }
            self.results.append(result)
            return result

    # ==================== BASE EVALUATION ====================

    async def _evaluate_base(self, conv_result: Dict) -> Dict:
        """Standard LLM-as-judge evaluation (same criteria as existing tests)."""
        if not conv_result.get("conversation_results"):
            return {"error": "No conversation results", "overall_score": 0}

        prompt = ChatPromptTemplate.from_template(
            """You are an expert evaluator for an e-commerce customer support chatbot.
Evaluate the following multi-turn conversation.

Conversation:
{conversation_text}

Expected Behaviors by Turn:
{expected_behaviors}

Evaluation Criteria (score 1-5 each):
1. Logical Correctness: Responses accurately address user's questions with correct info
2. Conciseness: Responses are brief and to the point
3. Helpfulness: Responses provide actionable info or clear next steps
4. Tone: Professional, empathetic, and warm tone maintained
5. Completeness: All aspects of user's questions addressed
6. Context Awareness: Responses appropriate for conversation flow
7. Conversation Flow: Conversation feels natural, builds on previous exchanges
8. Memory Retention: Bot remembers and references info from previous messages

Respond ONLY with valid JSON (no markdown):
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
    "summary": "Overall assessment"
}}"""
        )

        conv_text, behaviors = _format_conversation_for_judge(conv_result)

        try:
            chain = prompt | self.llm_judge
            resp = await chain.ainvoke({
                "conversation_text": conv_text,
                "expected_behaviors": behaviors,
            })
            return _parse_json_response(resp.content)
        except Exception as e:
            logger.error(f"Base evaluation error: {e}")
            return {"error": str(e), "overall_score": 0}

    # ==================== CONTEXT-AWARE EVALUATION ====================

    async def _evaluate_context(
        self,
        conv_result: Dict,
        context_assertions: Dict,
        initial_state_snapshot: Dict,
        target_agent: str = None,
    ) -> Dict:
        """
        Evaluate how well the agent utilized the pre-seeded context.
        Uses context_assertions to check specific behavioral expectations.
        """
        if not conv_result.get("conversation_results"):
            return {"error": "No conversation results", "overall_context_score": 0}

        # Build assertion text for the judge
        assertions_text = _format_context_assertions(context_assertions)
        initial_state_text = _format_state_for_judge(initial_state_snapshot)

        prompt = ChatPromptTemplate.from_template(
            """You are an expert evaluator for an e-commerce chatbot's CONTEXT AWARENESS.

The bot was given PRE-SEEDED conversation state before the customer sent their message.
Your job is to evaluate whether the bot CORRECTLY USED that context.

== PRE-SEEDED STATE (what the bot knew before the message) ==
{initial_state}

== CONVERSATION ==
{conversation_text}

== CONTEXT ASSERTIONS (what SHOULD have happened) ==
{assertions}

== TARGET AGENT ==
The message should have been handled by: {target_agent}

Evaluate on these criteria (score 1-5 each):

1. Context Utilization: Did the bot USE the pre-seeded context instead of asking for info it already had?
2. No Redundant Questions: Did the bot avoid asking for phone/order/product when already in state?
3. Flow Continuity: If waiting_for_* flags were set, did the bot treat the message as a continuation?
4. Correct Routing: Was the message handled by the correct agent/flow?
5. Prompt Compliance: Did the bot follow the specific behavioral rules (e.g., offer fix before cancel)?

Respond ONLY with valid JSON (no markdown):
{{
    "context_utilization": {{"score": 4, "explanation": "..."}},
    "no_redundant_questions": {{"score": 4, "explanation": "..."}},
    "flow_continuity": {{"score": 4, "explanation": "..."}},
    "correct_routing": {{"score": 4, "explanation": "..."}},
    "prompt_compliance": {{"score": 4, "explanation": "..."}},
    "overall_context_score": 4.0,
    "context_summary": "Assessment of context usage"
}}"""
        )

        conv_text, _ = _format_conversation_for_judge(conv_result)

        try:
            chain = prompt | self.llm_judge
            resp = await chain.ainvoke({
                "initial_state": initial_state_text,
                "conversation_text": conv_text,
                "assertions": assertions_text,
                "target_agent": target_agent or "auto-detect",
            })
            return _parse_json_response(resp.content)
        except Exception as e:
            logger.error(f"Context evaluation error: {e}")
            return {"error": str(e), "overall_context_score": 0}


# ==================== PROMPT CONTRACT EXTRACTOR ====================

class PromptContractExtractor:
    """
    Layer 1: Automatically extract behavioral contracts from an agent prompt.
    These contracts become test assertions that can be auto-checked.
    """

    def __init__(self):
        # Use a higher token limit for contract extraction (large outputs)
        api_key = os.getenv(LLM_JUDGE_CONFIG.get("api_key_env_var", "GOOGLE_API_KEY"))
        self.llm = ChatGoogleGenerativeAI(
            model=LLM_JUDGE_CONFIG["model"],
            temperature=0,
            max_tokens=8000,
            google_api_key=api_key,
        )

    async def extract_contracts(self, prompt_text: str, agent_name: str) -> List[Dict]:
        """
        Use an LLM to extract behavioral contracts from a prompt.

        Returns a list of contracts like:
        [
            {"category": "must_do", "rule": "Always ask for cancellation reason before cancelling", "priority": "high"},
            {"category": "must_not_do", "rule": "Never cancel orders directly in Shopify", "priority": "critical"},
            ...
        ]
        """
        extraction_prompt = ChatPromptTemplate.from_template(
            """You are an expert at analyzing chatbot prompts and extracting testable behavioral rules.

Analyze this agent prompt for "{agent_name}" and extract ALL behavioral contracts.

PROMPT:
{prompt_text}

Extract rules in these categories:
- must_do: Things the agent MUST always do
- must_not_do: Things the agent must NEVER do
- conditional_behavior: If X then Y rules
- escalation_rules: When to escalate to human
- data_handling: Rules about what data to collect/auto-fill

For each rule, assign priority: critical, high, medium, low

Respond ONLY with a valid JSON array (no markdown):
[
    {{"category": "must_not_do", "rule": "Never cancel orders directly in Shopify", "priority": "critical", "test_hint": "Send cancellation request and verify escalation instead of direct cancel"}},
    {{"category": "must_do", "rule": "Always collect cancellation reason before proceeding", "priority": "high", "test_hint": "Request cancellation without giving reason first"}}
]"""
        )

        try:
            chain = extraction_prompt | self.llm
            resp = await chain.ainvoke({
                "agent_name": agent_name,
                "prompt_text": prompt_text[:8000],  # Limit prompt size
            })
            contracts = _parse_json_response(resp.content)
            if isinstance(contracts, list):
                logger.info(f"📋 Extracted {len(contracts)} contracts for {agent_name}")
                return contracts
            return []
        except Exception as e:
            logger.error(f"Contract extraction error: {e}")
            return []

    async def generate_test_scenarios_from_contracts(
        self,
        contracts: List[Dict],
        agent_name: str,
        client_name: str = "casence",
    ) -> List[Dict]:
        """
        Generate test scenarios from extracted contracts.
        Processes in batches of critical/high priority contracts to stay within token limits.
        """
        if not contracts:
            return []

        # Focus on critical and high priority contracts first
        priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        sorted_contracts = sorted(
            contracts, key=lambda c: priority_order.get(c.get("priority", "low"), 3)
        )
        # Take top 15 most important contracts
        top_contracts = sorted_contracts[:15]

        gen_prompt = ChatPromptTemplate.from_template(
            """You are a test engineer generating test scenarios for an e-commerce chatbot agent.

Given these top behavioral contracts for the "{agent_name}" agent, generate ONE focused test scenario for each contract.

CONTRACTS:
{contracts}

Available state fixtures:
- cold_start: Empty state, no prior context
- mid_cancellation_flow: Order GV12345 confirmed, waiting for cancellation reason
- identified_user_with_orders: Phone 9876543210 known, 2 orders fetched (GV12345 shipped, GV12346 new)
- product_page_context: User is on product page for Sunfire Denim
- cross_topic_switch: Was in product inquiry, now switching topics

For each contract generate a scenario with id, description, client_name, initial_state_fixture, target_agent, context_assertions, and a short 1-turn conversation.

Respond ONLY with a valid JSON array (no markdown fences). Keep each scenario concise:
[
    {{
        "id": "contract-{agent_name}-1",
        "description": "Tests that agent does not cancel directly",
        "client_name": "{client_name}",
        "initial_state_fixture": "mid_cancellation_flow",
        "target_agent": "{agent_name}",
        "context_assertions": {{
            "expected_behaviors": ["Should escalate instead of cancelling directly"]
        }},
        "conversation": [
            {{"role": "customer", "message": "just cancel it already"}},
            {{"role": "bot", "expected_behavior": "Should escalate, not cancel", "expected_keywords": ["support", "contact"]}}
        ]
    }}
]"""
        )

        try:
            contracts_text = json.dumps(top_contracts, indent=2)
            chain = gen_prompt | self.llm
            resp = await chain.ainvoke({
                "agent_name": agent_name,
                "contracts": contracts_text,
                "client_name": client_name,
            })
            scenarios = _parse_json_response(resp.content)
            if isinstance(scenarios, list):
                logger.info(f"🧪 Generated {len(scenarios)} test scenarios from contracts")
                return scenarios
            return []
        except Exception as e:
            logger.error(f"Scenario generation error: {e}")
            return []


# ==================== REGRESSION SNAPSHOT MANAGER ====================

class RegressionSnapshotManager:
    """
    Layer 4: Record and compare tool call sequences + state mutations
    to detect regressions when code changes.
    """

    def __init__(self, snapshot_dir: str = None):
        self.snapshot_dir = snapshot_dir or os.path.join(
            os.path.dirname(__file__), "regression_snapshots"
        )
        os.makedirs(self.snapshot_dir, exist_ok=True)

    def save_snapshot(self, scenario_id: str, result: Dict[str, Any]):
        """Save a regression snapshot for a scenario."""
        snapshot = {
            "scenario_id": scenario_id,
            "timestamp": datetime.now().isoformat(),
            "conversation_length": result.get("conversation_length", 0),
            "passed": result.get("passed", False),
            "combined_score": result.get("combined_score", 0),
            "bot_responses": [
                turn.get("bot_response", "")
                for turn in result.get("conversation_result", {}).get("conversation_results", [])
            ],
            "state_mutations": result.get("state_mutations", []),
            "tool_calls_log": result.get("tool_calls_log", []),
            "final_state_keys": list(
                result.get("conversation_result", {}).get("final_state", {}).keys()
            ),
        }

        path = os.path.join(self.snapshot_dir, f"{scenario_id}.json")
        with open(path, "w") as f:
            json.dump(snapshot, f, indent=2, default=str)

        logger.info(f"📸 Saved regression snapshot: {path}")

    def compare_with_baseline(self, scenario_id: str, current_result: Dict) -> Dict:
        """
        Compare current result with saved baseline.
        Returns comparison report.
        """
        path = os.path.join(self.snapshot_dir, f"{scenario_id}.json")
        if not os.path.exists(path):
            return {"status": "no_baseline", "message": "No baseline snapshot found. Current run will become baseline."}

        with open(path, "r") as f:
            baseline = json.load(f)

        comparison = {
            "status": "compared",
            "score_change": current_result.get("combined_score", 0) - baseline.get("combined_score", 0),
            "pass_changed": current_result.get("passed") != baseline.get("passed"),
            "conversation_length_changed": (
                current_result.get("conversation_length", 0) != baseline.get("conversation_length", 0)
            ),
            "response_diffs": [],
            "regression_detected": False,
        }

        # Compare bot responses semantically (just check if they changed)
        current_responses = [
            t.get("bot_response", "")
            for t in current_result.get("conversation_result", {}).get("conversation_results", [])
        ]
        baseline_responses = baseline.get("bot_responses", [])

        for idx, (curr, base) in enumerate(
            zip(current_responses, baseline_responses)
        ):
            if curr != base:
                comparison["response_diffs"].append({
                    "turn": idx + 1,
                    "baseline": base[:200],
                    "current": curr[:200],
                })

        # Detect regression: score dropped significantly or pass -> fail
        if comparison["score_change"] < -0.5:
            comparison["regression_detected"] = True
        if baseline.get("passed") and not current_result.get("passed"):
            comparison["regression_detected"] = True

        return comparison

    def list_baselines(self) -> List[str]:
        """List all saved baseline scenario IDs."""
        if not os.path.exists(self.snapshot_dir):
            return []
        return [
            f.replace(".json", "")
            for f in os.listdir(self.snapshot_dir)
            if f.endswith(".json")
        ]


# ==================== HELPER FUNCTIONS ====================

def _format_conversation_for_judge(conv_result: Dict) -> Tuple[str, str]:
    """Format conversation for LLM judge consumption."""
    conv_text = ""
    behaviors = ""
    for turn in conv_result.get("conversation_results", []):
        conv_text += f"Turn {turn.get('turn', '?')}:\n"
        conv_text += f"Customer: {turn.get('customer_message', '')}\n"
        conv_text += f"Bot: {turn.get('bot_response', '')}\n\n"
        if turn.get("expected_behavior"):
            behaviors += f"Turn {turn.get('turn', '?')}: {turn['expected_behavior']}\n"
        if turn.get("context_check"):
            behaviors += f"  Context Check: {turn['context_check']}\n"
    return conv_text, behaviors


def _format_context_assertions(assertions: Dict) -> str:
    """Format context assertions into readable text for the LLM judge."""
    lines = []
    if assertions.get("should_not_ask_for"):
        lines.append(f"MUST NOT ask for: {', '.join(assertions['should_not_ask_for'])}")
    if assertions.get("should_ask_for"):
        lines.append(f"SHOULD ask for: {', '.join(assertions['should_ask_for'])}")
    if assertions.get("should_use_context"):
        lines.append(f"MUST reference/use: {', '.join(assertions['should_use_context'])}")
    if assertions.get("should_not_reroute_to"):
        lines.append(f"MUST NOT route to: {', '.join(assertions['should_not_reroute_to'])}")
    if assertions.get("expected_agent"):
        lines.append(f"Expected handling agent: {assertions['expected_agent']}")
    if assertions.get("expected_behaviors"):
        for b in assertions["expected_behaviors"]:
            lines.append(f"Expected behavior: {b}")
    if assertions.get("expected_state_changes"):
        for k, v in assertions["expected_state_changes"].items():
            lines.append(f"State should change: {k} → {v}")
    return "\n".join(lines) if lines else "No specific assertions."


def _format_state_for_judge(state_snapshot: Dict) -> str:
    """Format state snapshot into readable text for the judge."""
    lines = []
    if state_snapshot.get("phone_number"):
        lines.append(f"Phone: {state_snapshot['phone_number']} (KNOWN)")
    if state_snapshot.get("known_orders"):
        orders = state_snapshot["known_orders"]
        lines.append(f"Known Orders ({len(orders)}):")
        for o in orders[:5]:
            lines.append(f"  - {o.get('order_id', '?')}: {o.get('product', '?')} ({o.get('status', '?')})")
    if state_snapshot.get("selected_order_id"):
        lines.append(f"Selected Order: {state_snapshot['selected_order_id']}")
    if state_snapshot.get("waiting_for_cancellation_reason"):
        lines.append("⚠️ WAITING FOR: Cancellation reason (waiting_for_cancellation_reason=True)")
    if state_snapshot.get("waiting_for_order_confirmation"):
        lines.append("⚠️ WAITING FOR: Order confirmation")
    if state_snapshot.get("page_context"):
        pc = state_snapshot["page_context"]
        lines.append(f"Page Context: {pc.get('pageType', '?')} - {pc.get('productTitle', '?')}")
    if state_snapshot.get("conversation_context"):
        cc = state_snapshot["conversation_context"]
        lines.append(f"Topic: {cc.get('topic', '?')} ({cc.get('topic_status', '?')})")
        if cc.get("focal_entity"):
            lines.append(f"Focal Entity: {cc['focal_entity'].get('type', '?')}={cc['focal_entity'].get('id', '?')}")
    if state_snapshot.get("inquiry_product_info"):
        pi = state_snapshot["inquiry_product_info"]
        lines.append(f"Product in context: {pi.get('title', '?')}")
    msgs = state_snapshot.get("messages", [])
    if msgs:
        lines.append(f"Prior messages: {len(msgs)} messages in history")
    return "\n".join(lines) if lines else "Empty state (cold start)"


def _snapshot_state(state: Dict) -> Dict:
    """Create a serializable snapshot of key state fields."""
    snapshot = {}
    keys = [
        "phone_number", "client_id", "known_orders", "selected_order_id",
        "page_context", "current_page_type", "current_product_handle",
        "current_product_title", "waiting_for_cancellation_reason",
        "waiting_for_order_confirmation", "waiting_for_update_confirmation",
        "active_return_exchange_flow", "conversation_context",
        "inquiry_product_info", "detected_intents", "parent_intent",
        "is_frustrated", "needs_escalation",
    ]
    for k in keys:
        val = state.get(k)
        if val is not None:
            snapshot[k] = val

    # Snapshot message count (not content — too large)
    msgs = state.get("messages", [])
    if msgs:
        snapshot["messages"] = [
            {"role": getattr(m, "type", "unknown"), "content": m.content[:100] if hasattr(m, "content") else str(m)[:100]}
            for m in msgs[-6:]
        ]
    return snapshot


def _diff_states(before: Dict, after: Dict) -> Dict:
    """Find differences between two state snapshots."""
    diff = {}
    all_keys = set(list(before.keys()) + list(after.keys()))
    for k in all_keys:
        if k == "messages":
            continue
        old = before.get(k)
        new = after.get(k)
        if old != new:
            diff[k] = {"before": old, "after": new}
    return diff


def _get_expected_behavior(conversation: List[Dict], customer_turn_idx: int) -> str:
    """Get the expected_behavior from the next bot turn."""
    if customer_turn_idx + 1 < len(conversation):
        next_turn = conversation[customer_turn_idx + 1]
        if next_turn.get("role") == "bot":
            return next_turn.get("expected_behavior", "")
    return ""


def _get_expected_keywords(conversation: List[Dict], customer_turn_idx: int) -> List[str]:
    """Get expected_keywords from the next bot turn."""
    if customer_turn_idx + 1 < len(conversation):
        next_turn = conversation[customer_turn_idx + 1]
        if next_turn.get("role") == "bot":
            return next_turn.get("expected_keywords", [])
    return []


def _get_context_check(conversation: List[Dict], customer_turn_idx: int) -> str:
    """Get context_check from the next bot turn."""
    if customer_turn_idx + 1 < len(conversation):
        next_turn = conversation[customer_turn_idx + 1]
        if next_turn.get("role") == "bot":
            return next_turn.get("context_check", "")
    return ""


def _parse_json_response(text: str) -> Any:
    """Parse JSON from LLM response, handling markdown code blocks."""
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code block
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try finding JSON object or array
    for pattern in [r"\{.*\}", r"\[.*\]"]:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                continue

    logger.warning(f"Could not parse JSON from LLM response: {text[:200]}")
    return {"error": "Could not parse JSON", "overall_score": 0, "overall_context_score": 0}

