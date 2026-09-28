# Configuration Guide for Fashion Bot Testing

This guide explains how to use configuration files to customize the Fashion Bot testing framework.

## Overview

The test runner supports external JSON configuration files that allow you to customize:
- Evaluation criteria and weights
- Test thresholds and pass rates
- String matching parameters
- Report generation options
- Performance metrics
- LangSmith integration settings

## Basic Usage

### 1. Using Default Configuration

Run tests with default settings:
```bash
cd tests
python run_tests.py
```

### 2. Using a Custom Configuration File

Create a custom configuration file and use it:
```bash
python run_tests.py --config my_config.json
```

### 3. Viewing Current Configuration

See what configuration is being used:
```bash
python run_tests.py --show-config
```

### 4. Combining Options

Use multiple options together:
```bash
python run_tests.py \
  --config my_config.json \
  --langsmith-key your_api_key \
  --output-dir custom_reports
```

## Configuration File Structure

### Example Configuration File

```json
{
  "langsmith": {
    "api_key": null,
    "project_name": "my-fashion-bot-tests",
    "dataset_name": "my-test-scenarios"
  },
  "llm_judge": {
    "model": "gpt-4",
    "temperature": 0,
    "max_tokens": 1000
  },
  "evaluation_criteria": {
    "logical_correctness": {
      "weight": 0.25,
      "description": "Do the responses accurately address the user's questions?",
      "min_score": 3.5
    },
    "helpfulness": {
      "weight": 0.25,
      "description": "Do the responses provide actionable information?",
      "min_score": 3.5
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

## Configuration Sections

### 1. LangSmith Configuration

```json
"langsmith": {
  "api_key": "your_api_key_here",
  "project_name": "fashion-bot-testing",
  "dataset_name": "conversation-scenarios"
}
```

**Options:**
- `api_key`: Your LangSmith API key (optional)
- `project_name`: Name of the LangSmith project
- `dataset_name`: Name of the test dataset

### 2. LLM Judge Configuration

```json
"llm_judge": {
  "model": "gpt-4",
  "temperature": 0,
  "max_tokens": 1000
}
```

**Options:**
- `model`: LLM model to use for evaluation
- `temperature`: Model temperature (0 for consistent results)
- `max_tokens`: Maximum tokens for evaluation responses

### 3. Evaluation Criteria

```json
"evaluation_criteria": {
  "logical_correctness": {
    "weight": 0.20,
    "description": "Do the responses accurately address the user's questions?",
    "min_score": 3.0
  },
  "conciseness": {
    "weight": 0.10,
    "description": "Are the responses brief and to the point?",
    "min_score": 3.0
  }
}
```

**Available Criteria:**
- `logical_correctness`: Accuracy of information
- `conciseness`: Brevity and clarity
- `helpfulness`: Actionable information provided
- `tone`: Professional and helpful tone
- `completeness`: Addressing all aspects of questions
- `context_awareness`: Appropriate for conversation context
- `conversation_flow`: Natural conversation progression
- `memory_retention`: Remembering previous information

**For each criterion:**
- `weight`: Importance in overall score (0.0-1.0)
- `description`: What the criterion evaluates
- `min_score`: Minimum acceptable score (1.0-5.0)

### 4. Test Thresholds

```json
"test_thresholds": {
  "overall_pass_rate": 70.0,
  "individual_test_pass": {
    "keyword_score": 0.7,
    "llm_overall_score": 3.5,
    "conversation_length": 2
  },
  "response_quality": {
    "min_length": 10,
    "max_length": 500
  }
}
```

**Options:**
- `overall_pass_rate`: Minimum percentage of tests that must pass
- `individual_test_pass.keyword_score`: Minimum keyword matching score
- `individual_test_pass.llm_overall_score`: Minimum LLM evaluation score
- `response_quality.min_length`: Minimum response length
- `response_quality.max_length`: Maximum response length

### 5. String Matching Configuration

```json
"string_matching": {
  "keyword_score_threshold": 0.7,
  "case_sensitive": false,
  "partial_match": true,
  "synonyms": {
    "sizes": ["size", "sizing", "measurements"],
    "colors": ["color", "colour", "shade"]
  }
}
```

**Options:**
- `keyword_score_threshold`: Minimum score for keyword matching
- `case_sensitive`: Whether to match case exactly
- `partial_match`: Allow partial keyword matches
- `synonyms`: Groups of equivalent keywords

### 6. Report Configuration

```json
"report": {
  "generate_markdown": true,
  "generate_json": true,
  "include_detailed_scores": true,
  "include_keyword_analysis": true,
  "include_performance_metrics": true,
  "include_conversation_flow": true,
  "output_directory": "tests/test_reports"
}
```

**Options:**
- `generate_markdown`: Create markdown report
- `generate_json`: Create JSON results file
- `include_detailed_scores`: Include individual criterion scores
- `include_keyword_analysis`: Include keyword matching details
- `include_performance_metrics`: Include timing and performance data
- `include_conversation_flow`: Include conversation turn details
- `output_directory`: Where to save reports

### 7. Performance Configuration

```json
"performance": {
  "response_time_threshold": 5.0,
  "memory_usage_threshold": 100,
  "concurrent_requests": 10,
  "conversation_length_target": 3,
  "max_conversation_time": 30.0
}
```

**Options:**
- `response_time_threshold`: Maximum seconds per response
- `memory_usage_threshold`: Maximum memory usage in MB
- `concurrent_requests`: Number of concurrent test requests
- `conversation_length_target`: Target conversation length
- `max_conversation_time`: Maximum time per conversation

## Advanced Usage Examples

### 1. Stricter Evaluation

```json
{
  "evaluation_criteria": {
    "logical_correctness": {
      "weight": 0.30,
      "min_score": 4.0
    },
    "helpfulness": {
      "weight": 0.30,
      "min_score": 4.0
    }
  },
  "test_thresholds": {
    "overall_pass_rate": 85.0,
    "individual_test_pass": {
      "llm_overall_score": 4.0
    }
  }
}
```

### 2. Focus on Specific Criteria

```json
{
  "evaluation_criteria": {
    "tone": {
      "weight": 0.40,
      "min_score": 4.5
    },
    "conversation_flow": {
      "weight": 0.40,
      "min_score": 4.0
    },
    "memory_retention": {
      "weight": 0.20,
      "min_score": 3.5
    }
  }
}
```

### 3. Custom String Matching

```json
{
  "string_matching": {
    "keyword_score_threshold": 0.8,
    "case_sensitive": true,
    "synonyms": {
      "fashion": ["style", "clothing", "apparel"],
      "customer_service": ["support", "help", "assistance"],
      "order": ["purchase", "buy", "transaction"]
    }
  }
}
```

### 4. Minimal Reporting

```json
{
  "report": {
    "generate_markdown": false,
    "generate_json": true,
    "include_detailed_scores": false,
    "include_keyword_analysis": false,
    "output_directory": "minimal_reports"
  }
}
```

## Command Line Options

| Option | Description | Example |
|--------|-------------|---------|
| `--config` | Path to JSON configuration file | `--config my_config.json` |
| `--langsmith-key` | LangSmith API key | `--langsmith-key sk-...` |
| `--output-dir` | Custom output directory | `--output-dir custom_reports` |
| `--show-config` | Display current configuration | `--show-config` |

## Best Practices

1. **Start with Defaults**: Begin with the default configuration and customize gradually
2. **Use Version Control**: Keep configuration files in version control
3. **Environment-Specific Configs**: Create different configs for different environments
4. **Document Changes**: Comment your configuration changes
5. **Test Configurations**: Validate your config files before running tests

## Troubleshooting

### Common Issues

1. **Invalid JSON**: Ensure your configuration file is valid JSON
2. **Missing Sections**: The runner will use defaults for missing sections
3. **Invalid Values**: Check that numeric values are within expected ranges
4. **File Not Found**: Verify the path to your configuration file

### Validation

Use the `--show-config` option to verify your configuration is loaded correctly:

```bash
python run_tests.py --config my_config.json --show-config
```

This will display the complete configuration that will be used for testing. 