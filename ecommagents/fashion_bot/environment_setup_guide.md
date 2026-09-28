# Environment-Specific LangSmith Configuration Guide

This guide explains how to separate dev, staging, and production logs into different LangSmith projects automatically.

## Overview

Our LangSmith integration now automatically creates environment-specific project names:

- **Development**: `fashion-bot-gupshup-webhook-dev`
- **Staging**: `fashion-bot-gupshup-webhook-staging`  
- **Production**: `fashion-bot-gupshup-webhook-prod`

## Environment Detection

The system detects environment from these variables (in priority order):

1. `ENVIRONMENT` (recommended)
2. `NODE_ENV` (Node.js compatibility)
3. `DEPLOY_ENV` (alternative)

If none are set, it defaults to `development`.

## Configuration for Different Environments

### 1. Development Environment

```bash
# .env file for development
ENVIRONMENT=development
LANGSMITH_API_KEY=your_langsmith_api_key_here
OPENAI_API_KEY=your_openai_key_here
```

**Result**: Traces go to `fashion-bot-gupshup-webhook-dev`

### 2. Staging Environment

```bash
# .env.staging or environment variables
ENVIRONMENT=staging
LANGSMITH_API_KEY=your_langsmith_api_key_here
OPENAI_API_KEY=your_openai_key_here
```

**Result**: Traces go to `fashion-bot-gupshup-webhook-staging`

### 3. Production Environment

```bash
# Production environment variables
ENVIRONMENT=production
LANGSMITH_API_KEY=your_langsmith_api_key_here
OPENAI_API_KEY=your_openai_key_here
```

**Result**: Traces go to `fashion-bot-gupshup-webhook-prod`

## Docker/Container Setup

### Development
```dockerfile
# Dockerfile.dev
ENV ENVIRONMENT=development
ENV LANGSMITH_API_KEY=your_key
```

### Staging
```dockerfile
# Dockerfile.staging
ENV ENVIRONMENT=staging
ENV LANGSMITH_API_KEY=your_key
```

### Production
```dockerfile
# Dockerfile.prod
ENV ENVIRONMENT=production
ENV LANGSMITH_API_KEY=your_key
```

## Kubernetes/Cloud Deployment

### Development Namespace
```yaml
# k8s-dev.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: fashion-bot-dev
  namespace: development
spec:
  template:
    spec:
      containers:
      - name: fashion-bot
        env:
        - name: ENVIRONMENT
          value: "development"
        - name: LANGSMITH_API_KEY
          valueFrom:
            secretKeyRef:
              name: langsmith-secret
              key: api-key
```

### Production Namespace
```yaml
# k8s-prod.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: fashion-bot-prod
  namespace: production
spec:
  template:
    spec:
      containers:
      - name: fashion-bot
        env:
        - name: ENVIRONMENT
          value: "production"
        - name: LANGSMITH_API_KEY
          valueFrom:
            secretKeyRef:
              name: langsmith-secret
              key: api-key
```

## Verification

### Check Current Configuration

```python
# test_environment.py
from fashion_bot.langsmith_config import get_langsmith_config, print_environment_info

# Print current environment setup
print_environment_info()

# Check specific service config
gupshup_config = get_langsmith_config("gupshup")
print(f"Project Name: {gupshup_config.project_name}")
print(f"Environment: {gupshup_config.environment}")
print(f"LangSmith URL: {gupshup_config.get_project_url()}")
```

### Health Check Endpoint

The health endpoint now shows environment information:

```bash
curl http://localhost:8000/gupshup/webhook/health
```

Response includes:
```json
{
  "langsmith_tracing": {
    "enabled": true,
    "environment": "development", 
    "project_name": "fashion-bot-gupshup-webhook-dev",
    "environment_suffix": "dev"
  }
}
```

## Troubleshooting

### 1. Traces Going to Wrong Project

**Problem**: All traces going to development project

**Solution**: Check environment variable is set correctly
```bash
echo $ENVIRONMENT
# Should output: production, staging, or development
```

### 2. No Environment Separation

**Problem**: All environments using same project name

**Solution**: Restart application after setting environment variables

### 3. Environment Variable Not Detected

**Problem**: System defaulting to 'development'

**Solution**: Use one of the supported environment variable names:
- `ENVIRONMENT=production`
- `NODE_ENV=production` 
- `DEPLOY_ENV=production`

## Best Practices

### 1. Use Different API Keys (Optional)
While not required, you can use different LangSmith API keys for each environment:

```bash
# Development
LANGSMITH_API_KEY=dev_key_here

# Production  
LANGSMITH_API_KEY=prod_key_here
```

### 2. Environment-Specific Tags
The system automatically adds tags like:
- `environment:production`
- `env:prod`

These help filter traces in LangSmith UI.

### 3. Project Naming Convention
Projects follow this pattern:
`{base_name}-{service}-{environment}`

Examples:
- `fashion-bot-gupshup-webhook-dev`
- `fashion-bot-whatsapp-webhook-prod`
- `fashion-bot-general-staging`

## LangSmith UI Organization

With this setup, your LangSmith dashboard will show:

```
📁 fashion-bot-gupshup-webhook-dev     (Development traces)
📁 fashion-bot-gupshup-webhook-staging (Staging traces)  
📁 fashion-bot-gupshup-webhook-prod    (Production traces)
```

This provides clean separation and makes it easy to:
- Debug development issues without production noise
- Monitor production performance separately
- Test changes in staging environment
- Set up environment-specific alerts and monitoring

## Quick Commands

```bash
# Check current environment
python -c "from fashion_bot.langsmith_config import get_current_environment; print(f'Environment: {get_current_environment()}')"

# List all project names
python -c "from fashion_bot.langsmith_config import print_environment_info; print_environment_info()"

# Test tracing for specific service
python -c "from fashion_bot.langsmith_config import setup_langsmith_for_service; setup_langsmith_for_service('gupshup')"
```

## Environment Access Policy

- Runtime modules must not read environment variables directly via `os.getenv(...)` or `os.environ.get(...)`.
- Runtime modules must not call `load_dotenv(...)` directly.
- Use the shared loader only: `fashion_bot.env_loader`.
- Development mode can read local `.env` fallback.
- Staging/production mode ignores `.env` and uses deployment environment variables only.
- Policy check command:

```bash
./scripts/check_env_access.sh
```
