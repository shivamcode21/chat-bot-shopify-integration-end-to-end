#!/usr/bin/env python3
"""
Main script to run Fashion Bot tests with comprehensive evaluation
"""
import asyncio
import json
import os
import sys
import random
from typing import Dict, List, Any
from datetime import datetime

# Add parent directory to path to allow running from fashion_bot directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import environment utilities (this will auto-setup the environment)
from tests.env_utils import get_api_keys, validate_environment, print_environment_status

from tests.test_fashion_bot import FashionBotTester
from tests.langsmith_integration import LangSmithTestManager
from tests.test_config import (
    EVALUATION_CRITERIA, 
    TEST_THRESHOLDS, 
    REPORT_CONFIG,
    LANGSMITH_CONFIG,
    LLM_JUDGE_CONFIG,
    PERFORMANCE_METRICS,
    TEST_CATEGORIES,
    custom_evaluation_functions
)


class TestRunner:
    def __init__(self, langsmith_api_key: str = None):
        self.tester = FashionBotTester(langsmith_api_key)
        self.langsmith_manager = LangSmithTestManager(langsmith_api_key) if langsmith_api_key else None
        self.results = []
        self.config = self._get_config()
        
    def _get_config(self) -> Dict[str, Any]:
        """Get configuration from test_config.py"""
        return {
            "langsmith": LANGSMITH_CONFIG,
            "llm_judge": LLM_JUDGE_CONFIG,
            "evaluation_criteria": EVALUATION_CRITERIA,
            "test_thresholds": TEST_THRESHOLDS,
            "report": REPORT_CONFIG,
            "performance": PERFORMANCE_METRICS,
            "test_categories": TEST_CATEGORIES
        }
    
    def _get_config_value(self, key_path: str, default=None):
        """Get a configuration value using dot notation (e.g., 'test_thresholds.overall_pass_rate')"""
        keys = key_path.split('.')
        value = self.config
        
        try:
            for key in keys:
                value = value[key]
            return value
        except (KeyError, TypeError):
            return default
    
    async def run_basic_tests(self, test_id: str = None, top_x: int = None, random_x: int = None) -> List[Dict[str, Any]]:
        """Run basic test scenarios"""
        print("🚀 Running basic conversation test scenarios...")
        
        # Get all test scenarios
        scenarios = self.tester.test_data.get("test_scenarios", [])
        
        # Filter by test ID if specified
        if test_id:
            scenarios = [s for s in scenarios if s.get("id") == test_id]
            if not scenarios:
                print(f"❌ ERROR: No test found with ID '{test_id}'")
                print("📋 Available test IDs:")
                all_scenarios = self.tester.test_data.get("test_scenarios", [])
                for s in all_scenarios:
                    print(f"   - {s.get('id')}: {s.get('description', 'No description')}")
                return []
            print(f"🎯 Running single test: {test_id}")
        
        # Select random X tests if specified
        if random_x:
            if random_x > len(scenarios):
                print(f"⚠️  WARNING: Requested {random_x} random tests but only {len(scenarios)} available")
                random_x = len(scenarios)
            scenarios = random.sample(scenarios, random_x)
            print(f"🎲 Running {random_x} random tests:")
            for i, scenario in enumerate(scenarios, 1):
                print(f"   {i}. {scenario.get('id')}: {scenario.get('description', 'No description')}")
            print()
        # Limit to top X tests if specified
        elif top_x:
            if top_x > len(scenarios):
                print(f"⚠️  WARNING: Requested {top_x} tests but only {len(scenarios)} available")
                top_x = len(scenarios)
            scenarios = scenarios[:top_x]
            print(f"🎯 Running top {top_x} tests:")
            for i, scenario in enumerate(scenarios, 1):
                print(f"   {i}. {scenario.get('id')}: {scenario.get('description', 'No description')}")
            print()
        
        total_scenarios = len(scenarios)
        
        print(f"📋 Found {total_scenarios} test scenarios to run")
        print("=" * 60)
        
        results = []
        completed = 0
        passed = 0
        
        for i, scenario in enumerate(scenarios, 1):
            print(f"\n🔄 Running test {i}/{total_scenarios}: {scenario['id']}")
            print(f"   📝 Description: {scenario['description']}")
            
            try:
                # Run the test
                result = await self.tester.run_comprehensive_test(scenario)
                results.append(result)
                
                # Update counters
                completed += 1
                if result["passed"]:
                    passed += 1
                    status = "✅ PASS"
                else:
                    status = "❌ FAIL"
                
                # Calculate progress
                pending = total_scenarios - completed
                progress_pct = (completed / total_scenarios) * 100 if total_scenarios > 0 else 0
                
                # Display progress
                print(f"   {status} - Test {i} completed")
                print(f"   📊 Progress: {completed}/{total_scenarios} ({progress_pct:.1f}%) - {pending} pending")
                
                # Show quick summary - LLM score only
                if "llm_evaluation" in result and "error" not in result["llm_evaluation"]:
                    overall_score = result["llm_evaluation"].get("overall_score", "N/A")
                    print(f"   🎯 LLM Score: {overall_score}/5")
                
            except Exception as e:
                print(f"   💥 ERROR: {str(e)}")
                # Add error result
                error_result = {
                    "scenario": scenario,
                    "conversation_result": {"conversation_results": []},
                    "llm_evaluation": {"error": str(e)},
                    "conversation_length": 0,
                    "passed": False
                }
                results.append(error_result)
                completed += 1
                pending = total_scenarios - completed
                progress_pct = (completed / total_scenarios) * 100 if total_scenarios > 0 else 0
                print(f"   ❌ FAIL - Test {i} completed with error")
                print(f"   📊 Progress: {completed}/{total_scenarios} ({progress_pct:.1f}%) - {pending} pending")
        
        # Final summary
        print("\n" + "=" * 60)
        print("🎉 BASIC TESTS COMPLETED")
        print("=" * 60)
        pass_rate = (passed/completed)*100 if completed > 0 else 0
        print(f"📊 Results: {passed}/{completed} tests passed ({pass_rate:.1f}%)")
        print(f"📋 Total scenarios: {total_scenarios}")
        print("=" * 60)
        
        self.results.extend(results)
        return results
    
    async def run_custom_evaluations(self) -> List[Dict[str, Any]]:
        """Run custom evaluation functions"""
        print("🔍 Running custom evaluations...")
        
        total_results = len(self.results)
        print(f"📋 Processing {total_results} test results for custom evaluations")
        print("=" * 60)
        
        custom_results = []
        custom_funcs = custom_evaluation_functions()
        
        for i, result in enumerate(self.results, 1):
            print(f"\n🔄 Processing custom evaluations {i}/{total_results}: {result['scenario']['id']}")
            
            conversation_result = result["conversation_result"]
            all_responses = [turn["bot_response"] for turn in conversation_result["conversation_results"]]
            
            custom_scores = {}
            
            # Product info completeness
            if "product_info_completeness" in custom_funcs:
                print(f"   📦 Evaluating product info completeness...")
                custom_scores["product_info_completeness"] = custom_funcs["product_info_completeness"](
                    " ".join(all_responses), self.tester.test_data
                )
            
            # Order status appropriateness
            if "order_status_appropriateness" in custom_funcs:
                print(f"   📋 Evaluating order status appropriateness...")
                has_order_number = any(char.isdigit() for char in " ".join([turn["customer_message"] for turn in conversation_result["conversation_results"]]))
                custom_scores["order_status_appropriateness"] = custom_funcs["order_status_appropriateness"](
                    " ".join(all_responses), has_order_number
                )
            
            # Frustration handling
            if "frustration_handling" in custom_funcs:
                print(f"   😤 Evaluating frustration handling...")
                custom_scores["frustration_handling"] = custom_funcs["frustration_handling"](" ".join(all_responses))
            
            result["custom_evaluation"] = custom_scores
            custom_results.append(result)
            
            # Show progress
            progress_pct = (i / total_results) * 100 if total_results > 0 else 0
            pending = total_results - i
            print(f"   ✅ Custom evaluations {i} completed")
            print(f"   📊 Progress: {i}/{total_results} ({progress_pct:.1f}%) - {pending} pending")
            
            # Show custom scores
            for eval_name, score in custom_scores.items():
                print(f"   🎯 {eval_name.replace('_', ' ').title()}: {score:.2f}")
        
        print("\n" + "=" * 60)
        print("🎉 CUSTOM EVALUATIONS COMPLETED")
        print("=" * 60)
        print(f"📊 Processed {total_results} test results")
        print("=" * 60)
        
        return custom_results
    
    async def run_langsmith_integration(self) -> Dict[str, Any]:
        """Run LangSmith integration if available"""
        if not self.langsmith_manager:
            print("⚠️  LangSmith integration not available (no API key provided)")
            return {}
        
        print("📊 Running LangSmith integration...")
        
        # Create test dataset
        dataset_id = self.langsmith_manager.create_test_dataset(self.tester.test_data)
        
        # Run comprehensive evaluation
        langsmith_results = await self.langsmith_manager.run_langsmith_evaluation(self.results)
        
        return {
            "dataset_id": dataset_id,
            "langsmith_results": langsmith_results
        }
    
    def generate_comprehensive_report(self) -> str:
        """Generate a comprehensive test report"""
        print("📝 Generating comprehensive report...")
        
        total_tests = len(self.results)
        passed_tests = sum(1 for r in self.results if r["passed"])
        pass_rate = (passed_tests / total_tests) * 100 if total_tests > 0 else 0
        
        # Calculate average scores
        avg_scores = {
            "logical_correctness": 0,
            "conciseness": 0,
            "helpfulness": 0,
            "tone": 0,
            "completeness": 0,
            "context_awareness": 0,
            "conversation_flow": 0,
            "memory_retention": 0,
            "overall": 0
        }
        
        valid_llm_results = [r for r in self.results if "llm_evaluation" in r and "error" not in r["llm_evaluation"]]
        
        if valid_llm_results:
            for criterion in avg_scores.keys():
                if criterion == "overall":
                    scores = [r["llm_evaluation"].get("overall_score", 0) for r in valid_llm_results]
                else:
                    scores = [r["llm_evaluation"].get(criterion, {}).get("score", 0) for r in valid_llm_results]
                avg_scores[criterion] = sum(scores) / len(scores) if scores else 0
        
        # Calculate conversation statistics
        total_turns = sum(r.get("conversation_length", len(r.get("conversation_result", {}).get("conversation_results", []))) for r in self.results)
        avg_turns_per_conversation = total_turns / total_tests if total_tests > 0 else 0
        
        report = f"""# Fashion Bot Multi-Turn Conversation Test Report

Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## Executive Summary

- **Total Tests**: {total_tests}
- **Passed**: {passed_tests}
- **Failed**: {total_tests - passed_tests}
- **Pass Rate**: {pass_rate:.1f}%
- **Status**: {'✅ PASS' if pass_rate >= self._get_config_value('test_thresholds.overall_pass_rate') else '❌ FAIL'}

## Conversation Statistics

- **Total Turns**: {total_turns}
- **Average Turns per Conversation**: {avg_turns_per_conversation:.1f}

## Performance Metrics

### LLM Judge Scores (Average)

- **Logical Correctness**: {avg_scores['logical_correctness']:.2f}/5.0
- **Conciseness**: {avg_scores['conciseness']:.2f}/5.0
- **Helpfulness**: {avg_scores['helpfulness']:.2f}/5.0
- **Tone**: {avg_scores['tone']:.2f}/5.0
- **Completeness**: {avg_scores['completeness']:.2f}/5.0
- **Context Awareness**: {avg_scores['context_awareness']:.2f}/5.0
- **Conversation Flow**: {avg_scores['conversation_flow']:.2f}/5.0
- **Memory Retention**: {avg_scores['memory_retention']:.2f}/5.0
- **Overall Score**: {avg_scores['overall']:.2f}/5.0

## Test Categories Performance

### Conversation Types

"""
        
        # Category breakdown
        categories = self._get_config_value('test_categories')
        
        for category_name, scenario_ids in categories.items():
            category_results = [r for r in self.results if r["scenario"]["id"] in scenario_ids]
            if category_results:
                category_passed = sum(1 for r in category_results if r["passed"])
                category_pass_rate = (category_passed / len(category_results)) * 100
                avg_turns = sum(r.get("conversation_length", len(r.get("conversation_result", {}).get("conversation_results", []))) for r in category_results) / len(category_results)
                report += f"- **{category_name}**: {category_pass_rate:.1f}% ({category_passed}/{len(category_results)}) - Avg {avg_turns:.1f} turns\n"
        
        report += "\n## Detailed Results\n"
        
        # Detailed results for each test
        for result in self.results:
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
            
            # Ensure we have conversation results to display
            if conversation_result.get("conversation_results"):
                for i, turn in enumerate(conversation_result["conversation_results"], 1):
                    report += f"""
**Turn {i}:**

- Customer: {turn['customer_message']}
- Bot: {turn['bot_response']}
"""
            else:
                report += "No conversation turns recorded.\n"
            
            report += f"""
**LLM Judge Scores:**

"""
            
            if "error" not in llm_eval:
                for criterion, config in self._get_config_value('evaluation_criteria').items():
                    score = llm_eval.get(criterion, {}).get("score", "N/A")
                    explanation = llm_eval.get(criterion, {}).get("explanation", "")
                    report += f"- **{criterion.replace('_', ' ').title()}**: {score}/5 - {explanation}\n"
                
                report += f"- **Overall Score**: {llm_eval.get('overall_score', 'N/A')}/5\n"
            else:
                report += f"- **Error**: {llm_eval['error']}\n"
            
            # Custom evaluations
            if "custom_evaluation" in result:
                report += "\n**Custom Evaluations:**\n"
                for eval_name, score in result["custom_evaluation"].items():
                    report += f"- **{eval_name.replace('_', ' ').title()}**: {score:.2f}\n"
            
            report += "\n---\n"
        
        return report
    
    def generate_console_report(self) -> str:
        """Generate a console-friendly test report for terminal display"""
        print("📝 Generating console-friendly report...")
        
        total_tests = len(self.results)
        passed_tests = sum(1 for r in self.results if r["passed"])
        pass_rate = (passed_tests / total_tests) * 100 if total_tests > 0 else 0
        
        # Calculate average scores
        avg_scores = {
            "logical_correctness": 0,
            "conciseness": 0,
            "helpfulness": 0,
            "tone": 0,
            "completeness": 0,
            "context_awareness": 0,
            "conversation_flow": 0,
            "memory_retention": 0,
            "overall": 0
        }
        
        valid_llm_results = [r for r in self.results if "llm_evaluation" in r and "error" not in r["llm_evaluation"]]
        
        if valid_llm_results:
            for criterion in avg_scores.keys():
                if criterion == "overall":
                    scores = [r["llm_evaluation"].get("overall_score", 0) for r in valid_llm_results]
                else:
                    scores = [r["llm_evaluation"].get(criterion, {}).get("score", 0) for r in valid_llm_results]
                avg_scores[criterion] = sum(scores) / len(scores) if scores else 0
        
        # Calculate conversation statistics
        total_turns = sum(r.get("conversation_length", len(r.get("conversation_result", {}).get("conversation_results", []))) for r in self.results)
        avg_turns_per_conversation = total_turns / total_tests if total_tests > 0 else 0
        
        report = f"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                    Fashion Bot Multi-Turn Conversation Test Report           ║
╚══════════════════════════════════════════════════════════════════════════════╝

Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

📊 EXECUTIVE SUMMARY
═══════════════════════════════════════════════════════════════════════════════

• Total Tests: {total_tests}
• Passed: {passed_tests}
• Failed: {total_tests - passed_tests}
• Pass Rate: {pass_rate:.1f}%
• Status: {'✅ PASS' if pass_rate >= self._get_config_value('test_thresholds.overall_pass_rate') else '❌ FAIL'}

💬 CONVERSATION STATISTICS
═══════════════════════════════════════════════════════════════════════════════

• Total Turns: {total_turns}
• Average Turns per Conversation: {avg_turns_per_conversation:.1f}

🎯 PERFORMANCE METRICS (LLM-as-a-Judge)
═══════════════════════════════════════════════════════════════════════════════

LLM Judge Scores (Average):
• Logical Correctness: {avg_scores['logical_correctness']:.2f}/5.0
• Conciseness: {avg_scores['conciseness']:.2f}/5.0
• Helpfulness: {avg_scores['helpfulness']:.2f}/5.0
• Tone: {avg_scores['tone']:.2f}/5.0
• Completeness: {avg_scores['completeness']:.2f}/5.0
• Context Awareness: {avg_scores['context_awareness']:.2f}/5.0
• Conversation Flow: {avg_scores['conversation_flow']:.2f}/5.0
• Memory Retention: {avg_scores['memory_retention']:.2f}/5.0
• Overall Score: {avg_scores['overall']:.2f}/5.0

📋 TEST CATEGORIES PERFORMANCE
═══════════════════════════════════════════════════════════════════════════════

"""
        
        # Category breakdown
        categories = self._get_config_value('test_categories')
        
        for category_name, scenario_ids in categories.items():
            category_results = [r for r in self.results if r["scenario"]["id"] in scenario_ids]
            if category_results:
                category_passed = sum(1 for r in category_results if r["passed"])
                category_pass_rate = (category_passed / len(category_results)) * 100
                avg_turns = sum(r.get("conversation_length", len(r.get("conversation_result", {}).get("conversation_results", []))) for r in category_results) / len(category_results)
                report += f"• {category_name}: {category_pass_rate:.1f}% ({category_passed}/{len(category_results)}) - Avg {avg_turns:.1f} turns\n"
        
        report += "\n🔍 DETAILED RESULTS\n"
        report += "═══════════════════════════════════════════════════════════════════════════════\n"
        
        # Detailed results for each test
        for i, result in enumerate(self.results, 1):
            scenario = result["scenario"]
            conversation_result = result["conversation_result"]
            llm_eval = result["llm_evaluation"]
            conversation_length = result.get("conversation_length", len(conversation_result.get("conversation_results", [])))
            
            status = "✅ PASS" if result["passed"] else "❌ FAIL"
            
            report += f"""
{i}. {scenario['id']}: {scenario['description']} {status}
{'─' * 80}

Conversation Summary:
• Total Turns: {conversation_length}
• LLM Overall Score: {llm_eval.get('overall_score', 'N/A')}/5.0

Conversation Flow:
"""
            
            # Ensure we have conversation results to display
            if conversation_result.get("conversation_results"):
                for j, turn in enumerate(conversation_result["conversation_results"], 1):
                    report += f"""
Turn {j}:
• Customer: {turn['customer_message']}
• Bot: {turn['bot_response']}
"""
            else:
                report += "No conversation turns recorded.\n"
            
            report += f"""
LLM Judge Scores:
"""
            
            if "error" not in llm_eval:
                for criterion, config in self._get_config_value('evaluation_criteria').items():
                    score = llm_eval.get(criterion, {}).get("score", "N/A")
                    explanation = llm_eval.get(criterion, {}).get("explanation", "")
                    report += f"• {criterion.replace('_', ' ').title()}: {score}/5 - {explanation}\n"
                
                report += f"• Overall Score: {llm_eval.get('overall_score', 'N/A')}/5\n"
            else:
                report += f"• Error: {llm_eval['error']}\n"
            
            # Custom evaluations
            if "custom_evaluation" in result:
                report += "\nCustom Evaluations:\n"
                for eval_name, score in result["custom_evaluation"].items():
                    report += f"• {eval_name.replace('_', ' ').title()}: {score:.2f}\n"
            
            report += "\n" + "─" * 80 + "\n"
        
        return report
    
    def save_results(self, output_dir: str = None):
        """Save all results to files"""
        if output_dir is None:
            output_dir = self._get_config_value('report.output_directory', "tests/test_reports")
        
        os.makedirs(output_dir, exist_ok=True)
        
        # Save detailed results
        if self._get_config_value('report.generate_json'):
            with open(f"{output_dir}/detailed_results.json", "w") as f:
                json.dump(self.results, f, indent=2, default=str)
        
        # Save markdown report
        if self._get_config_value('report.generate_markdown'):
            report = self.generate_comprehensive_report()
            with open(f"{output_dir}/test_report.md", "w", encoding='utf-8') as f:
                f.write(report)
        
        # Save console-friendly report
        console_report = self.generate_console_report()
        with open(f"{output_dir}/test_report_console.txt", "w", encoding='utf-8') as f:
            f.write(console_report)
        
        # Save summary
        summary = {
            "timestamp": datetime.now().isoformat(),
            "total_tests": len(self.results),
            "passed_tests": sum(1 for r in self.results if r["passed"]),
            "pass_rate": (sum(1 for r in self.results if r["passed"]) / len(self.results)) * 100 if self.results else 0,
            "total_turns": sum(r.get("conversation_length", len(r.get("conversation_result", {}).get("conversation_results", []))) for r in self.results),
            "avg_turns_per_conversation": sum(r.get("conversation_length", len(r.get("conversation_result", {}).get("conversation_results", []))) for r in self.results) / len(self.results) if self.results else 0,
            "test_scenarios": [r["scenario"]["id"] for r in self.results]
        }
        
        with open(f"{output_dir}/summary.json", "w") as f:
            json.dump(summary, f, indent=2)
        
        print(f"📁 Results saved to {output_dir}/")
        print(f"📄 Markdown report: {output_dir}/test_report.md")
        print(f"📄 Console report: {output_dir}/test_report_console.txt")
    
    async def run_all(self, test_id: str = None, top_x: int = None, random_x: int = None) -> Dict[str, Any]:
        """Run all tests and evaluations"""
        print("🎯 Starting comprehensive Fashion Bot conversation testing...")
        
        # Run basic tests
        basic_results = await self.run_basic_tests(test_id, top_x, random_x)
        
        # Run custom evaluations
        custom_results = await self.run_custom_evaluations()
        
        # Run LangSmith integration
        langsmith_results = await self.run_langsmith_integration()
        
        # Generate and save reports
        self.save_results()
        
        # Print summary
        total_tests = len(self.results)
        passed_tests = sum(1 for r in self.results if r["passed"])
        pass_rate = (passed_tests / total_tests) * 100 if total_tests > 0 else 0
        total_turns = sum(r.get("conversation_length", len(r.get("conversation_result", {}).get("conversation_results", []))) for r in self.results)
        avg_turns = total_turns / total_tests if total_tests > 0 else 0
        
        print(f"\n🎉 Testing completed!")
        print(f"📊 Results: {passed_tests}/{total_tests} tests passed ({pass_rate:.1f}%)")
        print(f"💬 Total conversation turns: {total_turns}")
        print(f"🔄 Average turns per conversation: {avg_turns:.1f}")
        
        if langsmith_results.get("langsmith_results"):
            print(f"🔗 LangSmith URL: {langsmith_results['langsmith_results']['langsmith_url']}")
        
        # Display console report
        print("\n" + "=" * 80)
        print("📋 CONSOLE REPORT")
        print("=" * 80)
        console_report = self.generate_console_report()
        print(console_report)
        
        return {
            "total_tests": total_tests,
            "passed_tests": passed_tests,
            "pass_rate": pass_rate,
            "total_turns": total_turns,
            "langsmith_results": langsmith_results
        }


async def main():
    """Main function to run tests"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Run Fashion Bot conversation tests")
    parser.add_argument("--langsmith-key", help="LangSmith API key")
    parser.add_argument("--output-dir", help="Output directory for reports")
    parser.add_argument("--show-config", action="store_true", help="Show current configuration and exit")
    parser.add_argument("--test-id", help="Run only a specific test by its ID")
    parser.add_argument("--top-x", type=int, help="Run only the first X tests")
    parser.add_argument("--random-x", type=int, help="Run X random tests")
    parser.add_argument("--show-report", action="store_true", help="Display console report from existing results without running tests")
    
    args = parser.parse_args()
    
    # Check for required environment variables
    openai_key, langsmith_key = get_api_keys()
    langsmith_key = args.langsmith_key or langsmith_key
    
    if not openai_key:
        print("❌ ERROR: OPENAI_API_KEY not found in environment variables")
        print("   Make sure your .env file contains: OPENAI_API_KEY=your_key_here")
        sys.exit(1)
    else:
        print(f"✅ OpenAI API key found: {openai_key[:10]}...")
    
    if not langsmith_key:
        print("⚠️  WARNING: LANGSMITH_API_KEY not found. LangSmith integration will be disabled.")
        print("   To enable LangSmith, add LANGSMITH_API_KEY=your_key_here to your .env file")
    else:
        print(f"✅ LangSmith API key found: {langsmith_key[:10]}...")
    
    # Set environment variable for LangSmith if provided via command line
    if args.langsmith_key:
        os.environ["LANGSMITH_API_KEY"] = args.langsmith_key
    
    # Validate arguments
    if args.test_id and (args.top_x or args.random_x):
        print("❌ ERROR: Cannot use --test-id with --top-x or --random-x. Use one or the other.")
        sys.exit(1)
    
    if args.top_x and args.random_x:
        print("❌ ERROR: Cannot use --top-x and --random-x together. Use one or the other.")
        sys.exit(1)
    
    # Initialize test runner
    runner = TestRunner(
        langsmith_api_key=langsmith_key
    )
    
    # Show configuration if requested
    if args.show_config:
        print("📋 Current Configuration:")
        print(json.dumps(runner.config, indent=2))
        return
    
    # Show report from existing results if requested
    if args.show_report:
        output_dir = args.output_dir or runner._get_config_value('report.output_directory', "tests/test_reports")
        detailed_results_file = f"{output_dir}/detailed_results.json"
        
        if os.path.exists(detailed_results_file):
            try:
                with open(detailed_results_file, 'r') as f:
                    runner.results = json.load(f)
                console_report = runner.generate_console_report()
                print(console_report)
                return
            except Exception as e:
                print(f"❌ ERROR: Could not load existing results: {e}")
                sys.exit(1)
        else:
            print(f"❌ ERROR: No existing results found at {detailed_results_file}")
            print("   Run tests first to generate results, or specify a different output directory with --output-dir")
            sys.exit(1)
    
    # Override output directory if specified
    if args.output_dir:
        runner.config["report"]["output_directory"] = args.output_dir
    
    # Run tests
    results = await runner.run_all(args.test_id, args.top_x, args.random_x)
    
    # Exit with appropriate code
    if results["pass_rate"] >= runner._get_config_value('test_thresholds.overall_pass_rate', 70.0):
        print("✅ All tests passed threshold requirements")
        sys.exit(0)
    else:
        print("❌ Tests failed to meet threshold requirements")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main()) 