#!/usr/bin/env python3
"""
Example script demonstrating how to use configuration files with the Fashion Bot test runner
"""
import asyncio
import json
import os
from run_tests import TestRunner


async def demonstrate_config_usage():
    """Demonstrate different ways to use configuration files"""
    
    print("🎯 Fashion Bot Configuration Examples")
    print("=" * 50)
    
    # Example 1: Using default configuration
    print("\n1️⃣ Example 1: Using Default Configuration")
    print("-" * 40)
    runner1 = TestRunner()
    print("Default configuration loaded:")
    print(f"- Overall pass rate: {runner1._get_config_value('test_thresholds.overall_pass_rate')}%")
    print(f"- LLM model: {runner1._get_config_value('llm_judge.model')}")
    print(f"- Output directory: {runner1._get_config_value('report.output_directory')}")
    
    # Example 2: Using custom configuration file
    print("\n2️⃣ Example 2: Using Custom Configuration File")
    print("-" * 40)
    
    # Create a simple custom config
    custom_config = {
        "test_thresholds": {
            "overall_pass_rate": 90.0,
            "individual_test_pass": {
                "keyword_score": 0.9,
                "llm_overall_score": 4.5
            }
        },
        "evaluation_criteria": {
            "logical_correctness": {
                "weight": 0.40,
                "min_score": 4.5
            },
            "helpfulness": {
                "weight": 0.40,
                "min_score": 4.5
            },
            "tone": {
                "weight": 0.20,
                "min_score": 4.0
            }
        },
        "report": {
            "output_directory": "custom_test_reports"
        }
    }
    
    # Save custom config to file
    with open("custom_example_config.json", "w") as f:
        json.dump(custom_config, f, indent=2)
    
    print("Created custom_example_config.json with:")
    print(f"- Stricter pass rate: {custom_config['test_thresholds']['overall_pass_rate']}%")
    print(f"- Higher keyword score threshold: {custom_config['test_thresholds']['individual_test_pass']['keyword_score']}")
    print(f"- Focused evaluation criteria (logical_correctness + helpfulness)")
    
    # Example 3: Using the strict evaluation config
    print("\n3️⃣ Example 3: Using Strict Evaluation Configuration")
    print("-" * 40)
    
    if os.path.exists("strict_evaluation_config.json"):
        runner3 = TestRunner(config_file="strict_evaluation_config.json")
        print("Strict evaluation configuration loaded:")
        print(f"- Overall pass rate: {runner3._get_config_value('test_thresholds.overall_pass_rate')}%")
        print(f"- Keyword score threshold: {runner3._get_config_value('test_thresholds.individual_test_pass.keyword_score')}")
        print(f"- LLM overall score threshold: {runner3._get_config_value('test_thresholds.individual_test_pass.llm_overall_score')}")
        
        # Show evaluation criteria weights
        criteria = runner3._get_config_value('evaluation_criteria')
        print("\nEvaluation criteria weights:")
        for criterion, config in criteria.items():
            print(f"- {criterion}: {config['weight']} (min score: {config['min_score']})")
    else:
        print("strict_evaluation_config.json not found")
    
    # Example 4: Creating a minimal configuration
    print("\n4️⃣ Example 4: Creating Minimal Configuration")
    print("-" * 40)
    
    minimal_config = {
        "test_thresholds": {
            "overall_pass_rate": 60.0
        },
        "report": {
            "generate_markdown": False,
            "generate_json": True,
            "include_detailed_scores": False,
            "output_directory": "minimal_reports"
        }
    }
    
    with open("minimal_config.json", "w") as f:
        json.dump(minimal_config, f, indent=2)
    
    print("Created minimal_config.json with:")
    print("- Lower pass rate threshold (60%)")
    print("- Minimal reporting (JSON only)")
    print("- No detailed scores")
    
    # Example 5: Configuration validation
    print("\n5️⃣ Example 5: Configuration Validation")
    print("-" * 40)
    
    print("You can validate any configuration using --show-config:")
    print("python run_tests.py --config custom_example_config.json --show-config")
    print("python run_tests.py --config strict_evaluation_config.json --show-config")
    print("python run_tests.py --config minimal_config.json --show-config")
    
    # Clean up example files
    print("\n🧹 Cleaning up example files...")
    for filename in ["custom_example_config.json", "minimal_config.json"]:
        if os.path.exists(filename):
            os.remove(filename)
            print(f"Removed {filename}")
    
    print("\n✅ Configuration examples completed!")
    print("\n📋 Next steps:")
    print("1. Create your own configuration file based on the examples")
    print("2. Run tests with: python run_tests.py --config your_config.json")
    print("3. View the detailed configuration guide: CONFIG_GUIDE.md")


def show_config_comparison():
    """Show comparison between different configuration approaches"""
    
    print("\n📊 Configuration Comparison")
    print("=" * 50)
    
    configs = {
        "Default": "Uses test_config.py defaults",
        "Custom JSON": "External JSON file with your settings",
        "Strict": "Higher thresholds and stricter evaluation",
        "Minimal": "Lower thresholds and minimal reporting"
    }
    
    print("\nConfiguration Types:")
    for name, description in configs.items():
        print(f"- {name}: {description}")
    
    print("\nWhen to use each:")
    print("- Default: Quick testing, getting started")
    print("- Custom JSON: Production testing, team standards")
    print("- Strict: Quality assurance, high standards")
    print("- Minimal: Quick feedback, development testing")


if __name__ == "__main__":
    print("🚀 Fashion Bot Configuration Examples")
    print("This script demonstrates how to use configuration files with the test runner.")
    
    # Show comparison first
    show_config_comparison()
    
    # Run examples
    asyncio.run(demonstrate_config_usage())
    
    print("\n🎉 All examples completed!")
    print("Check CONFIG_GUIDE.md for detailed configuration options.") 