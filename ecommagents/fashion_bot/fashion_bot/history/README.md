# BigQuery Conversation Logging

This module provides async BigQuery logging functionality for WhatsApp conversations.

## Setup

### 1. Environment Variables

Add these variables to your `.env` file:

```bash
# Required
BIGQUERY_PROJECT_ID=your-gcp-project-id

# Optional (defaults provided)
BIGQUERY_DATASET_ID=whatsapp_conversations
BIGQUERY_TABLE_ID=conversation_logs
```

### 2. Google Cloud Authentication

You now have four options for authentication:

#### Option A: Base64 Encoded Service Account Key (Recommended for production)
Set the base64 encoded service account key JSON as an environment variable:
```bash
BIGQUERY_SERVICE_ACCOUNT_KEY_BASE64='<base64-encoded-json-key>'
```

#### Option B: Explicit Service Account Key (JSON String)
Set the service account key JSON as an environment variable:
```bash
BIGQUERY_SERVICE_ACCOUNT_KEY='{"type": "service_account", "project_id": "your-project-id", ...}'
```

#### Option C: Explicit Service Account Key (File Path)
Set the path to your service account key file:
```bash
BIGQUERY_SERVICE_ACCOUNT_KEY_FILE=/path/to/your/service-account-key.json
```

#### Option D: Application Default Credentials (Fallback)
If no explicit service account key is provided, the system falls back to:
```bash
# For local development
gcloud auth application-default login

# Or set the traditional Google Cloud environment variable
GOOGLE_APPLICATION_CREDENTIALS=/path/to/your/service-account-key.json
```

**Authentication Priority:**
1. `BIGQUERY_SERVICE_ACCOUNT_KEY_BASE64` (Base64 encoded JSON) - **Highest priority**
2. `BIGQUERY_SERVICE_ACCOUNT_KEY` (JSON string) - **High priority**
3. `BIGQUERY_SERVICE_ACCOUNT_KEY_FILE` (file path) - **Medium priority**  
4. Application Default Credentials - **Fallback**

### 3. BigQuery Setup

The module will automatically:
- Create the dataset if it doesn't exist
- Create the table with the proper schema if it doesn't exist

### 4. Required Permissions

Your service account needs these BigQuery permissions:
- `bigquery.datasets.create`
- `bigquery.tables.create`
- `bigquery.tables.updateData`
- `bigquery.jobs.create`

## Table Schema

The conversation logs table has the following schema:

| Field | Type | Mode | Description |
|-------|------|------|-------------|
| timestamp | TIMESTAMP | REQUIRED | When the conversation occurred |
| thread_id | STRING | REQUIRED | Conversation thread identifier |
| from_phone | STRING | REQUIRED | User's phone number |
| to_phone | STRING | NULLABLE | Recipient phone number |
| user_question | STRING | REQUIRED | User's original message |
| bot_reply | STRING | REQUIRED | Bot's response |
| message_type | STRING | NULLABLE | Type of message (e.g., 'user', 'bot', 'system') |
| session_phone | STRING | NULLABLE | Phone number stored in session |
| extracted_order | STRING | NULLABLE | Order number if extracted |
| product_type | STRING | NULLABLE | Type of product discussed |
| backend_url | STRING | NULLABLE | Backend API URL called |
| backend_status | INTEGER | NULLABLE | HTTP status from backend |
| processing_time_ms | FLOAT | NULLABLE | Processing time in milliseconds |
| metadata | JSON | NULLABLE | Additional metadata |

## Usage

The logging is automatically called from the webhook. If you need to use it elsewhere:

```python
from fashion_bot.history import log_conversation_to_bigquery

# Async usage
success = await log_conversation_to_bigquery(
    user_question="Hello",
    bot_reply="Hi there!",
    thread_id="user-thread",
    from_phone="+919876543210",
    message_type="user",  # 'user', 'bot', or 'system'
    # ... other parameters
)
```

## Practical Examples

### Example 1: Using Base64 Encoded Service Account Key (Recommended)

1. **Get your service account key:**
   - Go to Google Cloud Console → IAM & Admin → Service Accounts
   - Create or select a service account
   - Create a new key (JSON format)
   - Download the JSON file

2. **Encode the JSON as base64:**
   ```bash
   # For the JSON file
   cat /path/to/your/service-account-key.json | base64 -w 0
   
   # Or for the JSON string
   echo '{"type": "service_account", ...}' | base64 -w 0
   ```

3. **Set the environment variable:**
   ```bash
   # In your .env file
   BIGQUERY_SERVICE_ACCOUNT_KEY_BASE64='eyJ0eXBlIjogInNlcnZpY2VfYWNjb3VudCIsICJwcm9qZWN0X2lkIjogIi4uLiIsIC4uLn0='
   ```

4. **Test the connection:**
   ```python
   from fashion_bot.history.bigquery_logger import BigQueryLogger
   
   logger = BigQueryLogger()
   client = logger._get_client()
   print("BigQuery authentication successful!")
   ```

### Example 2: Using Service Account Key JSON String

1. **Get your service account key:**
   - Go to Google Cloud Console → IAM & Admin → Service Accounts
   - Create or select a service account
   - Create a new key (JSON format)
   - Copy the entire JSON content

2. **Set the environment variable:**
   ```bash
   # In your .env file
   BIGQUERY_SERVICE_ACCOUNT_KEY='{"type": "service_account", "project_id": "your-project-id", "private_key_id": "...", "private_key": "-----BEGIN PRIVATE KEY-----\n...", "client_email": "...", "client_id": "...", "auth_uri": "...", "token_uri": "...", "auth_provider_x509_cert_url": "...", "client_x509_cert_url": "..."}'
   ```

3. **Test the connection:**
   ```python
   from fashion_bot.history.bigquery_logger import BigQueryLogger
   
   logger = BigQueryLogger()
   client = logger._get_client()
   print("BigQuery authentication successful!")
   ```

### Example 3: Using Service Account Key File

1. **Download your service account key file**
2. **Set the file path:**
   ```bash
   # In your .env file
   BIGQUERY_SERVICE_ACCOUNT_KEY_FILE=/path/to/your/service-account-key.json
   ```

### Example 4: Docker Environment

For Docker deployments, you can use base64 encoded keys (recommended):

```dockerfile
# Dockerfile
ENV BIGQUERY_SERVICE_ACCOUNT_KEY_BASE64='eyJ0eXBlIjogInNlcnZpY2VfYWNjb3VudCIsIC4uLn0='
```

Or pass the JSON as an environment variable:

```dockerfile
# Dockerfile
ENV BIGQUERY_SERVICE_ACCOUNT_KEY='{"type": "service_account", ...}'
```

Or mount the key file:

```yaml
# docker-compose.yml
services:
  fashion-bot:
    environment:
      - BIGQUERY_SERVICE_ACCOUNT_KEY_FILE=/app/secrets/service-account-key.json
    volumes:
      - ./secrets/service-account-key.json:/app/secrets/service-account-key.json:ro
```

## Error Handling

- If BigQuery is not configured, logging is silently skipped
- Logging failures don't affect the webhook response
- All errors are logged to the application logs 