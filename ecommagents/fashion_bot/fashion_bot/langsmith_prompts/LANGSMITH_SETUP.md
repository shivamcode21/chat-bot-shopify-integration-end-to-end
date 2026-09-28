# LangSmith Prompt Setup Guide

This guide explains how to set up and use LangSmith cloud prompts with your Fashion Bot using the client API.

## Overview

The Fashion Bot now supports fetching prompts from LangSmith cloud, which provides:
- **Centralized prompt management**
- **Version control for prompts**
- **A/B testing capabilities**
- **Performance monitoring**
- **Easy prompt updates without code changes**
- **Environment-based tag selection**

## Quick Setup

### 1. Get LangSmith API Key

1. Go to [LangSmith](https://smith.langchain.com/)
2. Sign up or log in
3. Navigate to your API keys section
4. Copy your API key

### 2. Set Environment Variable

```bash
export LANGSMITH_API_KEY="your-api-key-here"
```

Or add to your `.env` file:
```
LANGSMITH_API_KEY=your-api-key-here
```

### 3. Set Environment (Optional)

Set the environment to control which tag is used:

```bash
# For development
export ENVIRONMENT=development

# For staging
export ENVIRONMENT=staging

# For production (default)
export ENVIRONMENT=production
```

### 4. Run Setup Script

```bash
cd fashion_bot
python setup_langsmith_prompts.py
```

This will show you the prompt template to create in LangSmith.

### 5. Create Prompt in LangSmith

1. Go to [LangSmith Prompts](https://smith.langchain.com/prompts)
2. Click "Create Prompt"
3. Use the template shown by the setup script
4. Name it `fashion_bot_support_prompt`
5. Add appropriate tags:
   - `dev` for development
   - `staging` for staging
   - `prod` for production
6. Save the prompt

## How It Works

### Before (Local Prompts)
```python
def build_smart_prompt(state) -> str:
    return f"""
    You are an intelligent and empathetic support assistant...
    {state.get("product_info")}
    ...
    """
```

### After (LangSmith Prompts)
```python
def build_smart_prompt(state) -> str:
    # Fetch prompt template from LangSmith
    base_prompt_template = fetch_langsmith_prompt("fashion_bot_support_prompt")
    
    # Format with context
    return base_prompt_template.format(
        product_info=state.get("product_info"),
        order_info=order_info,
        history=history
    )
```

## Environment-Based Tag Selection

The system automatically selects the appropriate tag based on your environment:

| Environment | Tag Used |
|-------------|----------|
| `development` or `dev` | `dev` |
| `staging` | `staging` |
| `production` or `prod` | `prod` |

### Example Usage

```python
# Set environment
os.environ["ENVIRONMENT"] = "development"

# This will automatically fetch the prompt with "dev" tag
prompt = fetch_langsmith_prompt("fashion_bot_support_prompt")

# Or explicitly specify a tag
dev_prompt = fetch_langsmith_prompt("fashion_bot_support_prompt", tag="dev")
staging_prompt = fetch_langsmith_prompt("fashion_bot_support_prompt", tag="staging")
prod_prompt = fetch_langsmith_prompt("fashion_bot_support_prompt", tag="prod")
```

## Prompt Template

The fashion bot uses this prompt template:

```
You are an intelligent and empathetic support assistant for an online fashion store.

Responsibilities:
- Answer product availability, sizing, fabric, customization, discounts, delivery, and order-related queries.
- Detect customer frustration and respond with empathy.
- If a shipment is delayed 5+ days, apologize sincerely and inform the user you are escalating it.
- If a user requests a delayed delivery (custom order), acknowledge and inform the operations team will help.
- If a customer expresses anger like "you are fooling me" or "you are not genuine", respond with politeness, empathy, and reassurance.

Product Info:
{product_info}

{order_info}

Conversation History:
{history}

Respond like a professional human support agent: empathetic, helpful, and conversational.
```

## Advanced Usage

### Multiple Prompts

You can create multiple prompts in LangSmith for different scenarios:

```python
# Fetch different prompts for different use cases
frustration_prompt = fetch_langsmith_prompt("fashion_bot_frustration_prompt", tag="dev")
order_prompt = fetch_langsmith_prompt("fashion_bot_order_prompt", tag="staging")
general_prompt = fetch_langsmith_prompt("fashion_bot_support_prompt", tag="prod")
```

### Prompt Versioning

LangSmith supports prompt versioning. You can:
- Create multiple versions of the same prompt with different tags
- A/B test different prompt versions
- Roll back to previous versions if needed

### Monitoring and Analytics

With LangSmith prompts, you get:
- **Performance metrics** for each prompt version
- **Cost tracking** per prompt
- **Latency monitoring**
- **Success rate analysis**

## Troubleshooting

### Prompt Not Found
If you get "Prompt not found" errors:
1. Check that the prompt name matches exactly
2. Verify the prompt is published in LangSmith with the correct tag
3. Ensure your API key has access to the prompt

### Fallback Behavior
If LangSmith is unavailable, the system automatically falls back to the local prompt template.

### API Key Issues
If you get authentication errors:
1. Verify your `LANGSMITH_API_KEY` is set correctly
2. Check that the API key is valid and active
3. Ensure you have the necessary permissions

### Environment Issues
If the wrong tag is being used:
1. Check your `ENVIRONMENT` variable is set correctly
2. Verify the environment mapping in the code
3. Try explicitly specifying the tag parameter

## Benefits

### For Developers
- **No code changes needed** to update prompts
- **Version control** for prompt changes
- **Easy rollback** if issues arise
- **Centralized management** of all prompts
- **Environment-specific** prompt versions

### For Business Users
- **Non-technical prompt updates**
- **A/B testing** of different prompt versions
- **Performance insights** and analytics
- **Cost optimization** through monitoring

### For Operations
- **Real-time prompt updates**
- **Consistent prompt management** across environments
- **Audit trail** of prompt changes
- **Performance monitoring** and alerting

## Next Steps

1. **Set up your first prompt** using the setup script
2. **Test the integration** with your fashion bot
3. **Create additional prompts** for different scenarios
4. **Monitor performance** in LangSmith dashboard
5. **Optimize prompts** based on analytics

## Support

If you encounter issues:
1. Check the [LangSmith documentation](https://docs.smith.langchain.com/)
2. Review the troubleshooting section above
3. Check your LangSmith dashboard for error details
4. Ensure your API key has the necessary permissions
5. Verify your environment variables are set correctly 