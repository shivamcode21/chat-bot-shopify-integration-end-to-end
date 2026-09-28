#!/usr/bin/env python3
"""
LangSmith integration for Fashion Bot testing
"""
import asyncio
import json
from typing import Dict, List, Any, Optional
from datetime import datetime

# Import environment utilities (this will auto-setup the environment)
from tests.env_utils import get_api_keys, validate_environment

from langsmith import Client, RunTree
from langsmith.run_helpers import traceable
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from tests.test_config import LANGSMITH_CONFIG, custom_evaluation_functions


class LangSmithTestManager:
    def __init__(self, api_key: Optional[str] = None):
        self.client = Client(api_key=api_key or LANGSMITH_CONFIG["api_key"])
        self.project_name = LANGSMITH_CONFIG["project_name"]
        self.dataset_name = LANGSMITH_CONFIG["dataset_name"]
        
    def create_test_dataset(self, test_data: Dict[str, Any]) -> str:
        """Create a dataset in LangSmith from test scenarios"""
        try:
            # Create dataset
            dataset = self.client.create_dataset(
                dataset_name=self.dataset_name,
                description="Fashion Bot multi-turn conversation test scenarios for evaluation"
            )
            
            # Add examples to dataset
            for scenario in test_data["test_scenarios"]:
                # Format conversation for dataset
                conversation_text = ""
                for turn in scenario["conversation"]:
                    if turn["role"] == "customer":
                        conversation_text += f"Customer: {turn['message']}\n"
                    else:
                        conversation_text += f"Bot: [Expected keywords: {', '.join(turn['expected_keywords'])}]\n"
                
                example = self.client.create_example(
                    inputs={
                        "scenario_id": scenario["id"],
                        "description": scenario["description"],
                        "conversation": conversation_text
                    },
                    outputs={
                        "expected_keywords": [kw for turn in scenario["conversation"] if turn["role"] == "bot" for kw in turn["expected_keywords"]],
                        "expected_behaviors": [turn["expected_behavior"] for turn in scenario["conversation"] if turn["role"] == "bot"]
                    },
                    dataset_id=dataset.id
                )
            
            print(f"Created dataset: {dataset.id}")
            return dataset.id
            
        except Exception as e:
            print(f"Error creating dataset: {e}")
            return None
    
    def log_test_run(self, test_result: Dict[str, Any], run_name: str = None) -> str:
        """Log a test run to LangSmith"""
        try:
            run_name = run_name or f"fashion-bot-conversation-test-{datetime.now().isoformat()}"
            
            # Format conversation for logging
            conversation_text = ""
            for turn in test_result["conversation_result"]["conversation_results"]:
                conversation_text += f"Turn {turn['turn']}:\n"
                conversation_text += f"Customer: {turn['customer_message']}\n"
                conversation_text += f"Bot: {turn['bot_response']}\n\n"
            
            # Create run tree
            with RunTree(
                name=run_name,
                run_type="chain",
                inputs={
                    "scenario_id": test_result["scenario"]["id"],
                    "description": test_result["scenario"]["description"],
                    "conversation": conversation_text
                },
                outputs={
                    "conversation_results": test_result["conversation_result"]["conversation_results"],
                    "string_evaluation": test_result["string_evaluation"],
                    "llm_evaluation": test_result["llm_evaluation"],
                    "passed": test_result["passed"]
                },
                project_name=self.project_name
            ) as run:
                # Add child runs for different evaluation methods
                with run.create_child(
                    name="string-matching-evaluation",
                    run_type="tool",
                    inputs={"conversation_results": test_result["conversation_result"]["conversation_results"]},
                    outputs=test_result["string_evaluation"]
                ):
                    pass
                
                with run.create_child(
                    name="llm-judge-evaluation", 
                    run_type="llm",
                    inputs={
                        "conversation_text": conversation_text,
                        "scenario_description": test_result["scenario"]["description"]
                    },
                    outputs=test_result["llm_evaluation"]
                ):
                    pass
                
                return run.id
                
        except Exception as e:
            print(f"Error logging test run: {e}")
            return None
    
    def create_evaluation_dataset(self, test_results: List[Dict[str, Any]]) -> str:
        """Create an evaluation dataset for LLM-as-a-judge"""
        try:
            eval_dataset_name = f"{self.dataset_name}-evaluation"
            dataset = self.client.create_dataset(
                dataset_name=eval_dataset_name,
                description="Fashion Bot multi-turn conversation evaluation examples for LLM judge"
            )
            
            for result in test_results:
                # Format conversation for evaluation
                conversation_text = ""
                for turn in result["conversation_result"]["conversation_results"]:
                    conversation_text += f"Turn {turn['turn']}:\n"
                    conversation_text += f"Customer: {turn['customer_message']}\n"
                    conversation_text += f"Bot: {turn['bot_response']}\n\n"
                
                # Create evaluation example
                example = self.client.create_example(
                    inputs={
                        "scenario_id": result["scenario"]["id"],
                        "conversation_text": conversation_text,
                        "expected_keywords": [kw for turn in result["conversation_result"]["conversation_results"] for kw in turn["expected_keywords"]]
                    },
                    outputs={
                        "logical_correctness_score": result["llm_evaluation"].get("logical_correctness", {}).get("score", 0),
                        "conciseness_score": result["llm_evaluation"].get("conciseness", {}).get("score", 0),
                        "helpfulness_score": result["llm_evaluation"].get("helpfulness", {}).get("score", 0),
                        "tone_score": result["llm_evaluation"].get("tone", {}).get("score", 0),
                        "completeness_score": result["llm_evaluation"].get("completeness", {}).get("score", 0),
                        "context_awareness_score": result["llm_evaluation"].get("context_awareness", {}).get("score", 0),
                        "conversation_flow_score": result["llm_evaluation"].get("conversation_flow", {}).get("score", 0),
                        "memory_retention_score": result["llm_evaluation"].get("memory_retention", {}).get("score", 0),
                        "overall_score": result["llm_evaluation"].get("overall_score", 0)
                    },
                    dataset_id=dataset.id
                )
            
            print(f"Created evaluation dataset: {dataset.id}")
            return dataset.id
            
        except Exception as e:
            print(f"Error creating evaluation dataset: {e}")
            return None
    
    def get_test_metrics(self, run_ids: List[str]) -> Dict[str, Any]:
        """Get aggregated metrics from test runs"""
        try:
            metrics = {
                "total_runs": len(run_ids),
                "passed_runs": 0,
                "failed_runs": 0,
                "average_scores": {
                    "logical_correctness": 0,
                    "conciseness": 0,
                    "helpfulness": 0,
                    "tone": 0,
                    "completeness": 0,
                    "context_awareness": 0,
                    "conversation_flow": 0,
                    "memory_retention": 0,
                    "overall": 0
                },
                "keyword_scores": [],
                "conversation_lengths": [],
                "response_times": []
            }
            
            for run_id in run_ids:
                run = self.client.read_run(run_id)
                outputs = run.outputs or {}
                
                if outputs.get("passed", False):
                    metrics["passed_runs"] += 1
                else:
                    metrics["failed_runs"] += 1
                
                # Aggregate LLM evaluation scores
                llm_eval = outputs.get("llm_evaluation", {})
                for criterion in metrics["average_scores"].keys():
                    if criterion == "overall":
                        score = llm_eval.get("overall_score", 0)
                    else:
                        score = llm_eval.get(criterion, {}).get("score", 0)
                    metrics["average_scores"][criterion] += score
                
                # Aggregate string matching scores
                string_eval = outputs.get("string_evaluation", {})
                metrics["keyword_scores"].append(string_eval.get("overall_keyword_score", 0))
                metrics["conversation_lengths"].append(string_eval.get("conversation_length", 0))
            
            # Calculate averages
            if metrics["total_runs"] > 0:
                for criterion in metrics["average_scores"].keys():
                    metrics["average_scores"][criterion] /= metrics["total_runs"]
                
                metrics["pass_rate"] = (metrics["passed_runs"] / metrics["total_runs"]) * 100
                metrics["average_keyword_score"] = sum(metrics["keyword_scores"]) / len(metrics["keyword_scores"])
                metrics["average_conversation_length"] = sum(metrics["conversation_lengths"]) / len(metrics["conversation_lengths"])
            
            return metrics
            
        except Exception as e:
            print(f"Error getting test metrics: {e}")
            return {}
    
    def create_custom_evaluator(self, evaluation_criteria: Dict[str, Any]) -> ChatPromptTemplate:
        """Create a custom LLM evaluator based on configuration"""
        criteria_text = "\n".join([
            f"{i+1}. {criterion.replace('_', ' ').title()} (1-5): {config['description']}"
            for i, (criterion, config) in enumerate(evaluation_criteria.items())
        ])
        
        prompt = ChatPromptTemplate.from_template("""
You are an expert evaluator for a fashion e-commerce chatbot. Evaluate the following multi-turn conversation based on the given criteria.

Conversation:
{conversation_text}

Evaluation Criteria:
{criteria}

Please provide scores for each criterion and a brief explanation for each score.

Expected Keywords: {expected_keywords}

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
        
        return prompt
    
    async def run_langsmith_evaluation(self, test_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Run comprehensive evaluation using LangSmith"""
        run_ids = []
        
        # Log all test runs
        for result in test_results:
            run_id = self.log_test_run(result)
            if run_id:
                run_ids.append(run_id)
        
        # Create evaluation dataset
        eval_dataset_id = self.create_evaluation_dataset(test_results)
        
        # Get aggregated metrics
        metrics = self.get_test_metrics(run_ids)
        
        return {
            "run_ids": run_ids,
            "evaluation_dataset_id": eval_dataset_id,
            "metrics": metrics,
            "langsmith_url": f"https://smith.langchain.com/projects/{self.project_name}"
        }


# Example usage
async def main():
    """Example usage of LangSmith integration"""
    # Initialize manager
    manager = LangSmithTestManager()
    
    # Load test data
    with open("tests/test_data.json", "r") as f:
        test_data = json.load(f)
    
    # Create test dataset
    dataset_id = manager.create_test_dataset(test_data)
    print(f"Created dataset with ID: {dataset_id}")
    
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
    print(f"Logged test run with ID: {run_id}")


if __name__ == "__main__":
    asyncio.run(main()) 