# Fashion Bot Multi-Turn Conversation Testing Framework

A comprehensive testing framework for the Fashion Bot using LangSmith, LLM-as-a-judge evaluation, and string matching techniques for **multi-turn conversations**.

## Features

- **Multi-Turn Conversation Testing**: Tests complete conversation flows with 2-3 message exchanges
- **Integration Testing**: Tests the complete graph workflow across multiple turns
- **String Matching**: Keyword-based evaluation with configurable thresholds per turn
- **LLM-as-a-Judge**: GPT-4 powered evaluation with conversation-specific criteria
- **LangSmith Integration**: Dataset creation and test run tracking for conversations
- **Custom Evaluations**: Domain-specific evaluation functions for conversation flow
- **Comprehensive Reporting**: Markdown and JSON reports with detailed conversation metrics
- **Configurable Criteria**: Easy customization of evaluation parameters for conversations
- **External Configuration Files**: JSON-based configuration for easy customization

## Directory Structure

```
fashion_bot/
├── tests/
│   ├── test_fashion_bot.py          # Main testing framework
│   ├── test_data.json               # Multi-turn conversation scenarios
│   ├── test_config.py               # Configuration and evaluation criteria
│   ├── langsmith_integration.py     # LangSmith integration
│   ├── run_tests.py                 # Main test runner
│   ├── example_usage.py             # Usage examples
│   ├── show_llm_criteria.py         # Display available criteria
│   ├── pytest.ini                   # Pytest configuration
│   ├── README_TESTING.md            # This file
│   └── test_reports/                # Generated reports (auto-created)
└── fashion_bot/                     # Main application code
```

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Set up Environment Variables

```bash
export OPENAI_API_KEY="your-openai-api-key"
export LANGSMITH_API_KEY="your-langsmith-api-key"  # Optional
```

### 3. Run Tests

```bash
# Run all conversation tests
cd tests
python run_tests.py

# Run with LangSmith integration
python run_tests.py --langsmith-key your-langsmith-key

# Run with custom output directory
python run_tests.py --output-dir my_test_results
```

## Multi-Turn Conversation Test Data Structure

The testing framework uses `tests/test_data.json` to define multi-turn conversation scenarios:

```json
{
  "test_scenarios": [
    {
      "id": "conversation_name",
      "description": "Description of the conversation scenario",
      "conversation": [
        {
          "role": "customer",
          "message": "First customer message"
        },
        {
          "role": "bot",
          "expected_keywords": ["keyword1", "keyword2"],
          "expected_behavior": "Expected behavior description"
        },
        {
          "role": "customer",
          "message": "Second customer message"
        },
        {
          "role": "bot",
          "expected_keywords": ["keyword3", "keyword4"],
          "expected_behavior": "Expected behavior for second response"
        }
      ]
    }
  ]
}
```

## Evaluation Criteria

The framework evaluates multi-turn conversations using multiple criteria:

### LLM Judge Criteria (1-5 scale)

1. **Logical Correctness**: Do the responses accurately address the user's questions?
2. **Conciseness**: Are the responses brief and to the point?
3. **Helpfulness**: Do the responses provide actionable information?
4. **Tone**: Do the responses maintain a professional tone throughout?
5. **Completeness**: Do the responses address all aspects of the questions?
6. **Context Awareness**: Are the responses appropriate for the context?
7. **Conversation Flow**: Does the conversation feel natural and build on previous exchanges?
8. **Memory Retention**: Does the bot remember and reference information from previous messages?

### String Matching Criteria

- **Keyword Score per Turn**: Percentage of expected keywords found in each response
- **Overall Keyword Score**: Average keyword score across all turns
- **Product Information**: Checks for size and color information across conversation
- **Response Length**: Validates response length constraints per turn

### Custom Evaluations

- **Product Info Completeness**: Checks if all product details are mentioned
- **Order Status Appropriateness**: Validates order-related responses
- **Frustration Handling**: Evaluates emotional response handling
- **Conversation Flow**: Evaluates natural conversation progression
- **Memory Retention**: Checks if bot references previous information

## Configuration

### Test Configuration (`tests/test_config.py`)

```python
# Evaluation criteria weights for conversations
EVALUATION_CRITERIA = {
    "logical_correctness": {"weight": 0.20, "min_score": 3.0},
    "conciseness": {"weight": 0.10, "min_score": 3.0},
    "helpfulness": {"weight": 0.20, "min_score": 3.5},
    "tone": {"weight": 0.10, "min_score": 3.5},
    "completeness": {"weight": 0.10, "min_score": 3.0},
    "context_awareness": {"weight": 0.10, "min_score": 3.5},
    "conversation_flow": {"weight": 0.10, "min_score": 3.5},
    "memory_retention": {"weight": 0.10, "min_score": 3.5}
}

# Test thresholds for conversations
TEST_THRESHOLDS = {
    "overall_pass_rate": 70.0,
    "individual_test_pass": {
        "keyword_score": 0.7,
        "llm_overall_score": 3.5,
        "conversation_length": 2  # Minimum conversation length
    }
}
```

### LangSmith Configuration

```python
LANGSMITH_CONFIG = {
    "api_key": "your-api-key",
    "project_name": "fashion-bot-conversation-testing",
    "dataset_name": "fashion-bot-conversation-test-scenarios"
}
```

### External Configuration Files

The test runner supports external JSON configuration files for easy customization. This allows you to modify evaluation criteria, thresholds, and other settings without changing the code.

#### Basic Usage

```bash
# Use default configuration
python run_tests.py

# Use custom configuration file
python run_tests.py --config my_config.json

# View current configuration
python run_tests.py --show-config
```

#### Configuration File Structure

```json
{
  "langsmith": {
    "api_key": null,
    "project_name": "my-fashion-bot-tests",
    "dataset_name": "my-test-scenarios"
  },
  "evaluation_criteria": {
    "logical_correctness": {
      "weight": 0.25,
      "min_score": 4.0
    },
    "helpfulness": {
      "weight": 0.25,
      "min_score": 4.0
    }
  },
  "test_thresholds": {
    "overall_pass_rate": 80.0,
    "individual_test_pass": {
      "keyword_score": 0.8,
      "llm_overall_score": 4.0
    }
  },
  "report": {
    "generate_markdown": true,
    "generate_json": true,
    "output_directory": "my_test_reports"
  }
}
```

#### Example Configuration Files

- `example_config.json` - Basic configuration template
- `strict_evaluation_config.json` - Stricter evaluation criteria

#### Command Line Options

| Option | Description | Example |
|--------|-------------|---------|
| `--config` | Path to JSON configuration file | `--config my_config.json` |
| `--langsmith-key` | LangSmith API key | `--langsmith-key sk-...` |
| `--output-dir` | Custom output directory | `--output-dir custom_reports` |
| `--show-config` | Display current configuration | `--show-config` |

For detailed configuration options, see [CONFIG_GUIDE.md](CONFIG_GUIDE.md).

## Running Different Test Types

### 1. Basic Conversation Tests

```python
from tests.test_fashion_bot import FashionBotTester

tester = FashionBotTester()
results = await tester.run_all_tests()
```

### 2. Individual Conversation Scenarios

```python
# Run specific conversation scenario
scenario = tester.test_data["test_scenarios"][0]
result = await tester.run_comprehensive_test(scenario)
```

### 3. Custom Evaluations for Conversations

```python
from tests.test_config import custom_evaluation_functions

custom_funcs = custom_evaluation_functions()
flow_score = custom_funcs["conversation_flow"](conversation_turns)
memory_score = custom_funcs["memory_retention"](conversation_turns)
```

### 4. LangSmith Integration

```python
from tests.langsmith_integration import LangSmithTestManager

manager = LangSmithTestManager(api_key="your-key")
dataset_id = manager.create_test_dataset(test_data)
run_id = manager.log_test_run(conversation_result)
```

## Test Reports

The framework generates comprehensive reports for multi-turn conversations:

### 1. Markdown Report (`tests/test_reports/test_report.md`)

- Executive summary with pass/fail status
- Conversation statistics (total turns, average turns per conversation)
- Performance metrics by conversation category
- Detailed results for each conversation scenario
- Turn-by-turn analysis with keyword scores
- LLM judge scores and explanations
- Conversation flow analysis

### 2. JSON Results (`tests/test_reports/detailed_results.json`)

- Complete conversation results with all evaluations
- Raw conversation data with turn-by-turn details
- Evaluation scores and metadata per turn

### 3. Summary (`tests/test_reports/summary.json`)

- High-level conversation statistics
- Pass rates and timestamps
- Conversation scenario list
- Average conversation metrics

## Adding New Conversation Scenarios

### 1. Add to JSON File

```json
{
  "id": "new_conversation_scenario",
  "description": "Test new conversation flow",
  "conversation": [
    {
      "role": "customer",
      "message": "First customer message"
    },
    {
      "role": "bot",
      "expected_keywords": ["expected", "keywords"],
      "expected_behavior": "Should handle first message appropriately"
    },
    {
      "role": "customer",
      "message": "Follow-up question"
    },
    {
      "role": "bot",
      "expected_keywords": ["follow", "up", "keywords"],
      "expected_behavior": "Should build on previous context"
    }
  ]
}
```

### 2. Add Custom Evaluation (Optional)

```python
def check_new_conversation_aspect(conversation_turns: List[Dict]) -> float:
    """Custom evaluation for new conversation aspect"""
    # Your evaluation logic here
    return score

# Add to custom_evaluation_functions in test_config.py
```

## LangSmith Integration

### 1. Dataset Creation

The framework automatically creates datasets in LangSmith:

- **Test Dataset**: Contains all conversation scenarios
- **Evaluation Dataset**: Contains LLM judge evaluations for conversations

### 2. Run Tracking

Each conversation test run is logged with:

- Complete conversation flow
- Turn-by-turn evaluation results
- Performance metrics
- Child runs for different evaluation methods

### 3. Metrics Aggregation

LangSmith provides:

- Pass/fail rates for conversations
- Average scores by criterion across turns
- Conversation performance trends
- Comparative analysis of conversation types

## Performance Monitoring

### Response Time Tracking

```python
import time

start_time = time.time()
result = await tester.run_comprehensive_test(scenario)
total_time = time.time() - start_time
avg_time_per_turn = total_time / result["string_evaluation"]["conversation_length"]
```

### Memory Usage Monitoring

```python
import psutil

process = psutil.Process()
memory_usage = process.memory_info().rss / 1024 / 1024  # MB
```

## Troubleshooting

### Common Issues

1. **LLM Evaluation Errors**
   - Check OpenAI API key
   - Verify model availability
   - Check response format for conversations

2. **LangSmith Integration Issues**
   - Verify API key
   - Check network connectivity
   - Validate project permissions

3. **Conversation Test Failures**
   - Review expected keywords per turn
   - Check conversation flow logic
   - Verify graph state initialization across turns

### Debug Mode

```python
# Enable debug logging
import logging
logging.basicConfig(level=logging.DEBUG)

# Run with verbose output
python run_tests.py --verbose
```

## Best Practices

### 1. Conversation Scenario Design

- Use realistic multi-turn conversations
- Include natural conversation flow
- Test different conversation patterns
- Maintain consistent expected outputs per turn

### 2. Evaluation Criteria

- Balance between strict and flexible criteria
- Consider conversation-specific requirements
- Regular calibration of LLM judge prompts
- Monitor conversation flow quality

### 3. Continuous Integration

```yaml
# GitHub Actions example
- name: Run Fashion Bot Conversation Tests
  run: |
    cd tests
    python run_tests.py --langsmith-key ${{ secrets.LANGSMITH_API_KEY }}
    python -m pytest test_fashion_bot.py -v
```

### 4. Performance Optimization

- Cache LLM evaluations when possible
- Use async operations for concurrent testing
- Optimize conversation processing
- Monitor memory usage across turns

## Advanced Usage

### Custom Evaluation Prompts

```python
from tests.langsmith_integration import LangSmithTestManager

manager = LangSmithTestManager()
custom_prompt = manager.create_custom_evaluator(my_criteria)
```

### Batch Testing

```python
# Run conversation tests in batches
batch_size = 5
for i in range(0, len(scenarios), batch_size):
    batch = scenarios[i:i+batch_size]
    results = await run_batch_conversation_tests(batch)
```

### A/B Testing

```python
# Compare different conversation configurations
config_a = {"model": "gpt-4o", "temperature": 0}
config_b = {"model": "gpt-3.5-turbo", "temperature": 0.1}

results_a = await test_conversations_with_config(config_a)
results_b = await test_conversations_with_config(config_b)
```

## Contributing

1. Add new conversation scenarios to `tests/test_data.json`
2. Update evaluation criteria in `tests/test_config.py`
3. Add custom evaluation functions as needed
4. Update documentation
5. Run full conversation test suite before submitting

## License

This testing framework is part of the Fashion Bot project and follows the same license terms. 