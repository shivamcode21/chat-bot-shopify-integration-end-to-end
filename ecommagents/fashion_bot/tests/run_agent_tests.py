#!/usr/bin/env python3
"""
Agent Test Suite — Entry Point CLI

Runs the full multi-layer testing framework:
  Layer 0: Fixtures & client profiles (auto-loaded)
  Layer 1: Prompt contract extraction & test generation
  Layer 2: Tool call verification
  Layer 3: Context-aware semantic tests (LLM-as-Judge)
  Layer 4: Regression snapshot comparison
  Layer 5: Pre-deployment gate (pass/fail verdict)

Usage:
  # Run all context-aware tests
  python -m tests.run_agent_tests

  # Run a specific test by ID
  python -m tests.run_agent_tests --test-id ctx-2-cancellation-mid-flow-reason

  # Run tests for a specific client
  python -m tests.run_agent_tests --client groovee

  # Extract prompt contracts and generate tests
  python -m tests.run_agent_tests --extract-contracts --agent cancellation_handler

  # Run with regression comparison
  python -m tests.run_agent_tests --regression

  # List available fixtures and tests
  python -m tests.run_agent_tests --list

  # Pre-deployment gate check
  python -m tests.run_agent_tests --gate
"""
import asyncio
import argparse
import json
import os
import sys
from datetime import datetime
from typing import Dict, List, Any, Optional

# Path setup
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.env_utils import get_api_keys, validate_environment, print_environment_status
from tests.agent_test_runner import (
    ContextAwareTestRunner,
    PromptContractExtractor,
    RegressionSnapshotManager,
)
from tests.tool_mocker import ToolCallVerifier
from tests.fixture_loader import (
    list_available_clients,
    list_available_fixtures,
    load_client_profile,
    get_fixture_metadata,
)
from tests.context_test_config import CONTEXT_TEST_THRESHOLDS


# ==================== REPORT GENERATOR ====================

def generate_report(results: List[Dict], output_dir: str = None) -> str:
    """Generate a comprehensive test report."""
    if output_dir is None:
        output_dir = "tests/agent_test_reports"
    os.makedirs(output_dir, exist_ok=True)

    total = len(results)
    passed = sum(1 for r in results if r.get("passed"))
    rate = (passed / total * 100) if total else 0

    # Scores
    base_scores = [
        r.get("llm_evaluation", {}).get("overall_score", 0)
        for r in results
        if "error" not in r.get("llm_evaluation", {})
    ]
    ctx_scores = [
        r.get("context_evaluation", {}).get("overall_context_score", 0)
        for r in results
        if r.get("context_evaluation") and "error" not in r.get("context_evaluation", {})
    ]
    combined_scores = [r.get("combined_score", 0) for r in results]

    avg_base = sum(base_scores) / len(base_scores) if base_scores else 0
    avg_ctx = sum(ctx_scores) / len(ctx_scores) if ctx_scores else 0
    avg_combined = sum(combined_scores) / len(combined_scores) if combined_scores else 0

    # Console report
    report = f"""
╔══════════════════════════════════════════════════════════════════════════════╗
║              Agent Test Suite — Context-Aware Report                        ║
╚══════════════════════════════════════════════════════════════════════════════╝

Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

📊 SUMMARY
═══════════════════════════════════════════════════════════════════════════════
• Total Tests:    {total}
• Passed:         {passed}
• Failed:         {total - passed}
• Pass Rate:      {rate:.1f}%
• Status:         {'✅ PASS' if rate >= CONTEXT_TEST_THRESHOLDS['overall_pass_rate'] else '❌ FAIL'}

🎯 SCORES (Average)
═══════════════════════════════════════════════════════════════════════════════
• Base LLM Score:     {avg_base:.2f}/5.0
• Context Score:      {avg_ctx:.2f}/5.0
• Combined Score:     {avg_combined:.2f}/5.0

🔍 PER-TEST DETAILS
═══════════════════════════════════════════════════════════════════════════════
"""
    for i, result in enumerate(results, 1):
        scenario = result.get("scenario", {})
        sid = scenario.get("id", "?")
        desc = scenario.get("description", "")
        fixture = result.get("initial_state_fixture", "cold_start")
        client = result.get("client_name", "?")
        status = "✅ PASS" if result.get("passed") else "❌ FAIL"
        combined = result.get("combined_score", 0)

        report += f"""
{i}. {sid} {status}
{'─' * 75}
   Description:  {desc}
   Client:       {client}
   Fixture:      {fixture}
   Combined:     {combined}/5.0
"""
        # Base eval
        base_eval = result.get("llm_evaluation", {})
        if "error" not in base_eval:
            report += f"   Base Score:   {base_eval.get('overall_score', 'N/A')}/5.0\n"
        else:
            report += f"   Base Score:   ERROR - {base_eval.get('error', '')}\n"

        # Context eval
        ctx_eval = result.get("context_evaluation", {})
        if ctx_eval and "error" not in ctx_eval:
            report += f"   Context Score: {ctx_eval.get('overall_context_score', 'N/A')}/5.0\n"
            for crit in ["context_utilization", "no_redundant_questions", "flow_continuity", "correct_routing", "prompt_compliance"]:
                crit_data = ctx_eval.get(crit, {})
                if isinstance(crit_data, dict):
                    report += f"     • {crit}: {crit_data.get('score', 'N/A')}/5 — {crit_data.get('explanation', '')[:80]}\n"

        # Tool verification
        tv = result.get("tool_verification", {})
        if tv:
            tools_called = tv.get("tools_called", [])
            if tools_called:
                report += f"\n   🔧 Tool Calls ({tv.get('total_calls', 0)}): {' → '.join(tools_called)}\n"
            if tv.get("forbidden_detected"):
                report += f"   🔴 FORBIDDEN: {', '.join(tv['forbidden_detected'])}\n"
            if tv.get("expected_missing"):
                report += f"   ⚠️  MISSING: {', '.join(tv['expected_missing'])}\n"

        # Conversation
        conv_results = result.get("conversation_result", {}).get("conversation_results", [])
        if conv_results:
            report += "\n   Conversation:\n"
            for turn in conv_results:
                report += f"     Customer: {turn.get('customer_message', '')}\n"
                bot_resp = turn.get("bot_response", "")
                report += f"     Bot:      {bot_resp[:200]}{'...' if len(bot_resp) > 200 else ''}\n"
                turn_tools = turn.get("tool_calls", [])
                if turn_tools:
                    tool_names = [tc["tool"] for tc in turn_tools]
                    report += f"     Tools:    {' → '.join(tool_names)}\n"
                report += "\n"

        # Timing
        timing = result.get("timing", {})
        if timing:
            report += f"   Timing: conv={timing.get('conversation_time_seconds', 0):.1f}s eval={timing.get('evaluation_time_seconds', 0):.1f}s\n"

    # Save
    with open(os.path.join(output_dir, "agent_test_report.txt"), "w", encoding="utf-8") as f:
        f.write(report)

    with open(os.path.join(output_dir, "agent_test_results.json"), "w") as f:
        json.dump(results, f, indent=2, default=str)

    # Summary JSON
    summary = {
        "timestamp": datetime.now().isoformat(),
        "total": total,
        "passed": passed,
        "pass_rate": rate,
        "avg_base_score": round(avg_base, 2),
        "avg_context_score": round(avg_ctx, 2),
        "avg_combined_score": round(avg_combined, 2),
        "tests": [
            {
                "id": r.get("scenario", {}).get("id"),
                "passed": r.get("passed"),
                "combined_score": r.get("combined_score"),
            }
            for r in results
        ],
    }
    with open(os.path.join(output_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    print(report)
    print(f"\n📁 Reports saved to {output_dir}/")
    return report


# ==================== PRE-DEPLOYMENT GATE ====================

def pre_deployment_gate(results: List[Dict]) -> bool:
    """
    Pre-deployment gate check.
    Returns True if all thresholds are met, False otherwise.
    """
    total = len(results)
    passed = sum(1 for r in results if r.get("passed"))
    rate = (passed / total * 100) if total else 0

    threshold = CONTEXT_TEST_THRESHOLDS["overall_pass_rate"]

    print("\n" + "=" * 70)
    print("🚦 PRE-DEPLOYMENT GATE CHECK")
    print("=" * 70)
    print(f"   Pass Rate:     {rate:.1f}% (threshold: {threshold}%)")
    print(f"   Tests Passed:  {passed}/{total}")

    # Check for critical failures (context score = 0 or errors)
    critical_failures = [
        r for r in results
        if r.get("context_evaluation", {}).get("error")
        or r.get("llm_evaluation", {}).get("error")
    ]
    if critical_failures:
        print(f"   ⚠️  Critical Failures: {len(critical_failures)} tests had errors")

    # Check regression
    regressions = [
        r for r in results
        if r.get("regression", {}).get("regression_detected")
    ]
    if regressions:
        print(f"   🔴 Regressions Detected: {len(regressions)} tests regressed")

    gate_passed = rate >= threshold and not regressions
    print(f"\n   {'✅ GATE PASSED — Safe to deploy' if gate_passed else '❌ GATE FAILED — Do NOT deploy'}")
    print("=" * 70)

    return gate_passed


# ==================== MAIN ====================

async def main():
    parser = argparse.ArgumentParser(description="Agent Test Suite — Context-Aware Testing")

    parser.add_argument("--test-id", help="Run only a specific test by ID")
    parser.add_argument("--client", help="Run tests for a specific client only")
    parser.add_argument("--list", action="store_true", help="List available tests, fixtures, and clients")
    parser.add_argument("--extract-contracts", action="store_true",
                        help="Extract prompt contracts and generate test scenarios")
    parser.add_argument("--agent", help="Agent name for contract extraction (e.g., cancellation_handler)")
    parser.add_argument("--prompt-file", help="Path to prompt text file for contract extraction")
    parser.add_argument("--regression", action="store_true", help="Run with regression snapshot comparison")
    parser.add_argument("--save-baseline", action="store_true", help="Save current results as regression baseline")
    parser.add_argument("--gate", action="store_true", help="Run pre-deployment gate check")
    parser.add_argument("--output-dir", help="Output directory for reports")
    parser.add_argument("--scenarios-file", help="Path to custom scenarios JSON (default: context_test_scenarios.json)")
    parser.add_argument("--use-real-apis", action="store_true",
                        help="Use real APIs instead of mocks (default: mock mode)")
    parser.add_argument("--enable-tracing", action="store_true",
                        help="Enable LangSmith tracing for this test run")
    parser.add_argument("--replay", type=str,
                        help="Replay a real conversation from DB by conversation_id UUID")
    parser.add_argument("--replay-list", action="store_true",
                        help="List recent conversations available for replay")
    parser.add_argument("--phone", type=str, help="Filter conversations by phone (used with --replay-list)")
    parser.add_argument("--replay-export", type=str,
                        help="Export a real conversation as a test fixture")
    parser.add_argument("--list-agents", action="store_true",
                        help="List all agents available in the tool registry")
    parser.add_argument("--parallel", type=int, default=1, metavar="N",
                        help="Run tests in parallel with N workers (default: 1 = sequential)")

    args = parser.parse_args()

    # Validate environment
    openai_key, langsmith_key = get_api_keys()
    if not openai_key:
        print("❌ OPENAI_API_KEY not found. Set it in .env or environment.")
        sys.exit(1)
    print(f"✅ OpenAI API key found")
    print_environment_status()

    # ---- LIST AGENTS MODE ----
    if args.list_agents:
        from fashion_bot.core.tool_registry import TOOL_REGISTRY, list_available_agents
        print("\n📋 Available Agents in Tool Registry:")
        print("=" * 60)
        for agent_name in list_available_agents():
            entry = TOOL_REGISTRY[agent_name]
            factory = entry.get("factory", "?")
            topic = entry.get("topic", "?")
            prompt = entry.get("prompt_name", "?")
            is_no_tools = factory == "no_tools"
            icon = "📝" if is_no_tools else "🔧"
            print(f"   {icon} {agent_name}")
            print(f"      Topic: {topic} | Prompt: {prompt}")
            if is_no_tools:
                print(f"      Tools: (LLM-only, no tools)")
            else:
                print(f"      Factory: {factory}")
        print(f"\nTotal: {len(list_available_agents())} agents")
        return

    # ---- LIST MODE ----
    if args.list:
        print("\n📋 Available Clients:")
        for c in list_available_clients():
            profile = load_client_profile(c)
            print(f"   • {c} ({len(profile.get('test_agents', []))} agents)")

        print("\n📋 Available State Fixtures:")
        for f in list_available_fixtures():
            meta = get_fixture_metadata(f)
            print(f"   • {f}: {meta.get('description', 'N/A')}")

        scenarios_file = args.scenarios_file or os.path.join(
            os.path.dirname(__file__), "context_test_scenarios.json"
        )
        if os.path.exists(scenarios_file):
            with open(scenarios_file) as fh:
                data = json.load(fh)
            scenarios = data.get("test_scenarios", [])
            print(f"\n📋 Available Test Scenarios ({len(scenarios)}):")
            for s in scenarios:
                print(f"   • {s['id']}: {s.get('description', '')}")
                print(f"     Client: {s.get('client_name', 'default')} | Fixture: {s.get('initial_state_fixture', 'cold_start')}")
        return

    # ---- REPLAY MODES ----
    if args.replay_list:
        from tests.chat_replay import fetch_recent_conversations
        # Use casence client_id by default
        try:
            profile = load_client_profile(args.client or "casence")
            cid = profile.get("client_id") or profile.get("client_id_fallback")
        except FileNotFoundError:
            cid = "81e80e20-fe91-470a-ab3d-e9dfc2eebf4a"

        convs = fetch_recent_conversations(cid, phone=args.phone, limit=20)
        print(f"\n📋 Recent conversations for client {cid}:")
        for conv in convs:
            print(
                f"   • {conv['conversation_id']} | "
                f"Phone: {conv.get('phone', 'N/A')} | "
                f"Msgs: {conv.get('message_count', 0)} | "
                f"Channel: {conv.get('channel_type', 'N/A')} | "
                f"Updated: {conv.get('updated_at', 'N/A')}"
            )
            print(f"     First: {conv.get('first_message', '')[:80]}")
        return

    if args.replay_export:
        from tests.chat_replay import export_conversation_as_fixture
        try:
            profile = load_client_profile(args.client or "casence")
            cid = profile.get("client_id") or profile.get("client_id_fallback")
        except FileNotFoundError:
            cid = "81e80e20-fe91-470a-ab3d-e9dfc2eebf4a"

        path = export_conversation_as_fixture(args.replay_export, cid)
        print(f"\n✅ Exported fixture: {path}")
        return

    if args.replay:
        from tests.chat_replay import replay_conversation_with_verification
        try:
            profile = load_client_profile(args.client or "casence")
            cid = profile.get("client_id") or profile.get("client_id_fallback")
        except FileNotFoundError:
            cid = "81e80e20-fe91-470a-ab3d-e9dfc2eebf4a"

        result = await replay_conversation_with_verification(
            args.replay,
            cid,
            use_mock=not args.use_real_apis,
            client_name=args.client or "casence",
        )

        print(f"\n🔄 Replay Results for {args.replay}")
        print("=" * 70)
        for r in result.get("replay_results", []):
            print(f"\n  Turn {r['turn']}:")
            print(f"    Customer: {r['customer_message'][:100]}")
            print(f"    Actual:   {(r.get('actual_bot_response') or 'N/A')[:100]}")
            print(f"    Replay:   {r['replay_bot_response'][:100]}")

            tool_summary = r.get("tool_summary", {})
            if tool_summary.get("tools_called"):
                print(f"    🔧 Tools: {tool_summary['tool_sequence']}")
            else:
                print(f"    🔧 Tools: (none)")

        all_tools = result.get("all_tools_called", [])
        print(f"\n  📊 All tools called: {all_tools}")
        print(f"  📊 Unique tools: {list(dict.fromkeys(all_tools))}")

        # Save replay result
        output_dir = args.output_dir or "tests/agent_test_reports"
        os.makedirs(output_dir, exist_ok=True)
        replay_file = os.path.join(output_dir, f"replay_{args.replay[:8]}.json")
        with open(replay_file, "w") as f:
            json.dump(result, f, indent=2, default=str)
        print(f"\n📁 Replay saved to {replay_file}")
        return

    # ---- CONTRACT EXTRACTION MODE ----
    if args.extract_contracts:
        if not args.agent:
            print("❌ --agent is required with --extract-contracts (use 'all' for every agent)")
            sys.exit(1)

        # Resolve client_id from profile
        client_name = args.client
        if not client_name:
            print("❌ --client is required with --extract-contracts (e.g. --client casence)")
            sys.exit(1)

        try:
            profile = load_client_profile(client_name)
            client_id = profile.get("client_id")
        except FileNotFoundError:
            print(f"❌ Client profile '{client_name}' not found in tests/fixtures/clients/")
            sys.exit(1)

        if not client_id:
            print(f"❌ No client_id found in profile '{client_name}'")
            sys.exit(1)

        print(f"📎 Client: {client_name} (client_id: {client_id})")

        # ---- Build list of agents to process ----
        agents_to_process = []  # list of {"agent_arg": str, "prompt_name": str, "prompt_text": str}

        if args.agent.lower() == "all":
            # Pull ALL agent prompts from agents_config table
            print(f"\n🔄 Loading ALL agent prompts from agents_config table...")
            try:
                import asyncio
                from fashion_bot.utils.utils import aget_agents_config_from_db
                all_agents = asyncio.run(aget_agents_config_from_db(client_id))
                if not all_agents:
                    print(f"❌ No agents found in agents_config for client_id={client_id}")
                    sys.exit(1)
                print(f"✅ Found {len(all_agents)} agents in DB:")
                for ag in all_agents:
                    aname = ag["agent_name"]
                    prompt = ag.get("agent_prompt", "") or ""
                    print(f"   • {aname:40s} ({len(prompt):>6} chars)")
                    if prompt:
                        agents_to_process.append({
                            "agent_arg": aname,
                            "prompt_name": aname,
                            "prompt_text": prompt,
                        })
                    else:
                        print(f"     ⚠️  Skipping {aname} — empty prompt")
            except Exception as e:
                print(f"❌ Could not load agents from DB: {e}")
                sys.exit(1)
        else:
            # Single agent mode
            agent_arg = args.agent
            prompt_name_for_db = agent_arg  # default: use as-is
            try:
                from fashion_bot.core.tool_registry import get_agent_config, TOOL_REGISTRY
                if agent_arg in TOOL_REGISTRY:
                    cfg = get_agent_config(agent_arg)
                    prompt_name_for_db = cfg.get("prompt_name", f"{agent_arg}_handler")
                    print(f"🔍 Resolved agent '{agent_arg}' → prompt_name '{prompt_name_for_db}' in agents_config")
                else:
                    for aname, entry in TOOL_REGISTRY.items():
                        if entry.get("prompt_name") == agent_arg:
                            print(f"🔍 '{agent_arg}' is a prompt_name (agent: {aname})")
                            break
                    else:
                        print(f"⚠️  '{agent_arg}' not found in tool registry — using as-is for DB lookup")
            except Exception as e:
                print(f"⚠️  Could not resolve agent name via registry: {e}")

            prompt_text = ""
            if args.prompt_file:
                with open(args.prompt_file) as f:
                    prompt_text = f.read()
                print(f"📄 Loaded prompt from file: {args.prompt_file}")
            else:
                print(f"🔄 Loading prompt '{prompt_name_for_db}' from agents_config table...")
                try:
                    import asyncio
                    from fashion_bot.utils.utils import aget_agent_prompt_with_caching
                    prompt_text = asyncio.run(aget_agent_prompt_with_caching(client_id, prompt_name_for_db)) or ""
                    if prompt_text:
                        print(f"✅ Loaded prompt from DB ({len(prompt_text)} chars)")
                    else:
                        print(f"⚠️  No prompt returned for '{prompt_name_for_db}'")
                        try:
                            from fashion_bot.utils.utils import aget_agents_config_from_db
                            all_agents = asyncio.run(aget_agents_config_from_db(client_id))
                            if all_agents:
                                available = [a["agent_name"] for a in all_agents]
                                print(f"   Available agents in DB: {available}")
                        except Exception:
                            pass
                except Exception as e:
                    print(f"⚠️  Could not load prompt from DB: {e}")

            if not prompt_text:
                print("❌ No prompt text found. Provide --prompt-file or ensure DB access (DATABASE_URL).")
                sys.exit(1)

            agents_to_process.append({
                "agent_arg": agent_arg,
                "prompt_name": prompt_name_for_db,
                "prompt_text": prompt_text,
            })

        # ---- Process each agent ----
        output_dir = args.output_dir or "tests/agent_test_reports"
        os.makedirs(output_dir, exist_ok=True)
        extractor = PromptContractExtractor()

        all_generated_scenarios = []
        summary_rows = []

        for idx, agent_info in enumerate(agents_to_process, 1):
            agent_arg = agent_info["agent_arg"]
            prompt_name = agent_info["prompt_name"]
            prompt_text = agent_info["prompt_text"]

            print(f"\n{'='*75}")
            print(f"  [{idx}/{len(agents_to_process)}] 📋 {agent_arg} ({len(prompt_text)} chars)")
            print(f"{'='*75}")

            try:
                contracts = await extractor.extract_contracts(prompt_text, agent_arg)
                print(f"  ✅ Extracted {len(contracts)} contracts")

                critical = sum(1 for c in contracts if c.get("priority") == "critical")
                high = sum(1 for c in contracts if c.get("priority") == "high")
                print(f"     🔴 {critical} critical  🟡 {high} high  ⚪ {len(contracts) - critical - high} normal")

                # Generate test scenarios
                scenarios = await extractor.generate_test_scenarios_from_contracts(
                    contracts, agent_arg, client_name
                )
                print(f"  🧪 Generated {len(scenarios)} test scenarios")
                all_generated_scenarios.extend(scenarios)

                # Save per-agent files
                contracts_file = os.path.join(output_dir, f"{agent_arg}_contracts.json")
                scenarios_file = os.path.join(output_dir, f"{agent_arg}_generated_scenarios.json")

                with open(contracts_file, "w") as f:
                    json.dump(contracts, f, indent=2)
                with open(scenarios_file, "w") as f:
                    json.dump({"test_scenarios": scenarios}, f, indent=2)

                summary_rows.append({
                    "agent": agent_arg,
                    "contracts": len(contracts),
                    "critical": critical,
                    "scenarios": len(scenarios),
                    "status": "✅",
                })

            except Exception as e:
                print(f"  ❌ Failed: {e}")
                summary_rows.append({
                    "agent": agent_arg,
                    "contracts": 0,
                    "critical": 0,
                    "scenarios": 0,
                    "status": f"❌ {str(e)[:60]}",
                })

        # ---- Combined output for --agent all ----
        if len(agents_to_process) > 1:
            combined_file = os.path.join(output_dir, "all_agents_generated_scenarios.json")
            with open(combined_file, "w") as f:
                json.dump({"test_scenarios": all_generated_scenarios}, f, indent=2)

            print(f"\n{'='*75}")
            print(f"  📊 SUMMARY — {len(agents_to_process)} agents processed")
            print(f"{'='*75}")
            print(f"  {'Agent':<40s} {'Contracts':>9s} {'Critical':>8s} {'Scenarios':>9s}  Status")
            print(f"  {'─'*40} {'─'*9} {'─'*8} {'─'*9}  {'─'*8}")
            for row in summary_rows:
                print(f"  {row['agent']:<40s} {row['contracts']:>9d} {row['critical']:>8d} {row['scenarios']:>9d}  {row['status']}")
            total_contracts = sum(r["contracts"] for r in summary_rows)
            total_scenarios = sum(r["scenarios"] for r in summary_rows)
            print(f"  {'─'*40} {'─'*9} {'─'*8} {'─'*9}")
            print(f"  {'TOTAL':<40s} {total_contracts:>9d} {'':>8s} {total_scenarios:>9d}")
            print(f"\n📁 Per-agent files saved to {output_dir}/")
            print(f"📁 Combined scenarios: {combined_file} ({total_scenarios} scenarios)")
        else:
            print(f"\n📁 Saved to {output_dir}/")

        return

    # ---- TEST EXECUTION MODE ----
    scenarios_file = args.scenarios_file or os.path.join(
        os.path.dirname(__file__), "context_test_scenarios.json"
    )
    if not os.path.exists(scenarios_file):
        print(f"❌ Scenarios file not found: {scenarios_file}")
        sys.exit(1)

    with open(scenarios_file) as f:
        data = json.load(f)
    scenarios = data.get("test_scenarios", [])

    print(f"\n📂 Loaded {len(scenarios)} scenarios from {os.path.basename(scenarios_file)}")

    # Initialize runner
    use_mock = not args.use_real_apis
    
    if args.enable_tracing:
        print("🔗 Enabling LangSmith tracing...")
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGSMITH_TRACING_V2"] = "true"
        if langsmith_key:
            os.environ["LANGSMITH_API_KEY"] = langsmith_key
    
    runner = ContextAwareTestRunner(langsmith_api_key=langsmith_key, use_mock=use_mock)
    regression_mgr = RegressionSnapshotManager() if (args.regression or args.save_baseline) else None

    # Run tests
    max_workers = args.parallel if hasattr(args, 'parallel') else 1
    results = await runner.run_all_scenarios(
        scenarios,
        test_id=args.test_id,
        client_filter=args.client,
        max_workers=max_workers,
    )

    # Regression comparison
    if regression_mgr and results:
        print("\n📸 Regression Analysis:")
        for result in results:
            sid = result.get("scenario", {}).get("id", "?")
            if args.regression:
                comparison = regression_mgr.compare_with_baseline(sid, result)
                result["regression"] = comparison
                if comparison["status"] == "no_baseline":
                    print(f"   {sid}: No baseline (will save as new baseline)")
                elif comparison.get("regression_detected"):
                    print(f"   {sid}: 🔴 REGRESSION DETECTED (score change: {comparison['score_change']:+.2f})")
                else:
                    print(f"   {sid}: ✅ No regression (score change: {comparison.get('score_change', 0):+.2f})")

            if args.save_baseline or (args.regression and result.get("regression", {}).get("status") == "no_baseline"):
                regression_mgr.save_snapshot(sid, result)

    # Generate report
    output_dir = args.output_dir or "tests/agent_test_reports"
    generate_report(results, output_dir)

    # Gate check
    if args.gate:
        gate_passed = pre_deployment_gate(results)
        sys.exit(0 if gate_passed else 1)
    else:
        total = len(results)
        passed_count = sum(1 for r in results if r.get("passed"))
        rate = (passed_count / total * 100) if total else 0
        sys.exit(0 if rate >= CONTEXT_TEST_THRESHOLDS["overall_pass_rate"] else 1)


if __name__ == "__main__":
    asyncio.run(main())

