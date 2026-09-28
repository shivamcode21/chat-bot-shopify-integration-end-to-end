import json
import os
import base64
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from google.cloud import bigquery
from google.cloud.exceptions import GoogleCloudError
from google.oauth2 import service_account
import logging
from dotenv import load_dotenv
from pathlib import Path

# BigQuery logging - environment variables should be loaded by main entry point
load_dotenv()  # Keep basic load_dotenv for standalone usage

# Set up logging
logger = logging.getLogger(__name__)

class BigQueryLogger:
    def __init__(self):
        self.project_id = os.getenv('BIGQUERY_PROJECT_ID',"groovee-dashboard-new")
        self.dataset_id = os.getenv('BIGQUERY_DATASET_ID', 'ecommagents_history')
        self.table_id = os.getenv('BIGQUERY_TABLE_ID', 'conversation_logs')
        self.service_account_key = os.getenv('BIGQUERY_SERVICE_ACCOUNT_KEY')
        self.service_account_key_base64 = os.getenv('BIGQUERY_SERVICE_ACCOUNT_KEY_BASE64')
        self.service_account_key_file = os.getenv('BIGQUERY_SERVICE_ACCOUNT_KEY_FILE')
        self.client = None
        
        if not self.project_id:
            logger.warning("BIGQUERY_PROJECT_ID not set in environment variables")
    
    def _get_credentials(self):
        """Get Google Cloud credentials from service account key or use default"""
        credentials = None
        
        # Option 1: Base64 encoded JSON string in environment variable (highest priority)
        if self.service_account_key_base64:
            try:
                # Clean up the base64 string - remove whitespace and newlines
                clean_base64 = ''.join(self.service_account_key_base64.split())
                
                # Validate base64 characters
                import string
                valid_base64_chars = string.ascii_letters + string.digits + '+/='
                invalid_chars = [c for c in clean_base64 if c not in valid_base64_chars]
                if invalid_chars:
                    logger.error(f"Invalid characters found in base64 string: {set(invalid_chars)}")
                    raise ValueError(f"Invalid base64 characters: {set(invalid_chars)}")
                
                # Add padding if necessary
                padding_needed = len(clean_base64) % 4
                if padding_needed:
                    clean_base64 += '=' * (4 - padding_needed)
                    logger.info(f"Added {4 - padding_needed} padding characters to base64 string")
                
                logger.info(f"Processing base64 string of length: {len(clean_base64)}")
                
                # Decode base64 encoded service account key
                try:
                    decoded_bytes = base64.b64decode(clean_base64)
                    logger.info(f"Successfully decoded base64 to {len(decoded_bytes)} bytes")
                    
                    # Try to decode as UTF-8 with error handling
                    try:
                        decoded_key = decoded_bytes.decode('utf-8')
                    except UnicodeDecodeError as e:
                        logger.error(f"UTF-8 decoding failed at position {e.start}: {e.reason}")
                        # Try with error handling
                        decoded_key = decoded_bytes.decode('utf-8', errors='replace')
                        logger.warning("Used error replacement for UTF-8 decoding")
                        
                except base64.binascii.Error as e:
                    logger.error(f"Base64 decoding failed: {e}")
                    raise
                
                # Clean the JSON string by removing/replacing invalid control characters
                import re
                # Remove control characters except for valid JSON whitespace (space, tab, newline, carriage return)
                cleaned_json = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', decoded_key)
                
                # Additional cleaning: fix common JSON issues
                cleaned_json = cleaned_json.strip()
                
                logger.info(f"Cleaned JSON string, length: {len(cleaned_json)}")
                
                try:
                    service_account_info = json.loads(cleaned_json)
                except json.JSONDecodeError as e:
                    logger.error(f"JSON parsing failed: {e}")
                    logger.error(f"JSON content around error position {e.pos}:")
                    start = max(0, e.pos - 50)
                    end = min(len(cleaned_json), e.pos + 50)
                    logger.error(f"'{cleaned_json[start:end]}'")
                    
                    # Try to fix common JSON issues
                    logger.info("Attempting to fix common JSON issues...")
                    
                    # Fix escaped newlines in private key
                    fixed_json = cleaned_json.replace('\\n', '\n')
                    
                    try:
                        service_account_info = json.loads(fixed_json)
                        logger.info("Successfully parsed JSON after fixing escaped newlines")
                    except json.JSONDecodeError:
                        # If that doesn't work, try the original error
                        raise e
                
                credentials = service_account.Credentials.from_service_account_info(
                    service_account_info,
                    scopes=['https://www.googleapis.com/auth/bigquery']
                )
                logger.info("Using base64 encoded service account key from BIGQUERY_SERVICE_ACCOUNT_KEY_BASE64")
            except (base64.binascii.Error, UnicodeDecodeError) as e:
                logger.error(f"Invalid base64 encoding in BIGQUERY_SERVICE_ACCOUNT_KEY_BASE64: {e}")
                logger.error(f"Base64 string length: {len(self.service_account_key_base64)}")
                logger.error(f"First 50 chars: {self.service_account_key_base64[:50]}...")
                raise
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON in decoded BIGQUERY_SERVICE_ACCOUNT_KEY_BASE64: {e}")
                logger.error(f"Decoded content first 100 chars: {decoded_key[:100]}...")
                raise
            except Exception as e:
                logger.error(f"Failed to create credentials from base64 encoded service account key: {e}")
                raise
        
        # Option 2: Raw JSON string in environment variable
        elif self.service_account_key:
            try:
                service_account_info = json.loads(self.service_account_key)
                credentials = service_account.Credentials.from_service_account_info(
                    service_account_info,
                    scopes=['https://www.googleapis.com/auth/bigquery']
                )
                logger.info("Using explicit service account key from BIGQUERY_SERVICE_ACCOUNT_KEY")
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON in BIGQUERY_SERVICE_ACCOUNT_KEY: {e}")
                raise
            except Exception as e:
                logger.error(f"Failed to create credentials from service account key: {e}")
                raise
        
        # Option 3: Path to service account key file
        elif self.service_account_key_file:
            try:
                credentials = service_account.Credentials.from_service_account_file(
                    self.service_account_key_file,
                    scopes=['https://www.googleapis.com/auth/bigquery']
                )
                logger.info(f"Using service account key file: {self.service_account_key_file}")
            except Exception as e:
                logger.error(f"Failed to load service account key file {self.service_account_key_file}: {e}")
                raise
        
        # Option 4: Fall back to Application Default Credentials
        else:
            logger.info("Using Application Default Credentials (no explicit service account key provided)")
            
        return credentials
    
    def _get_client(self):
        """Initialize BigQuery client if not already done"""
        if self.client is None:
            try:
                credentials = self._get_credentials()
                if credentials:
                    self.client = bigquery.Client(
                        project=self.project_id, 
                        credentials=credentials
                    )
                else:
                    # Fall back to default credentials
                    self.client = bigquery.Client(project=self.project_id)
            except Exception as e:
                logger.error(f"Failed to initialize BigQuery client: {e}")
                raise
        return self.client
    
    def _ensure_table_exists(self):
        """Create the table if it doesn't exist"""
        try:
            client = self._get_client()
            table_ref = client.dataset(self.dataset_id).table(self.table_id)
            
            try:
                client.get_table(table_ref)
                logger.info(f"Table {self.dataset_id}.{self.table_id} already exists")
            except:
                # Table doesn't exist, create it
                schema = [
                    bigquery.SchemaField("timestamp", "TIMESTAMP", mode="REQUIRED"),
                    bigquery.SchemaField("thread_id", "STRING", mode="REQUIRED"),
                    bigquery.SchemaField("from_phone", "STRING", mode="REQUIRED"),
                    bigquery.SchemaField("to_phone", "STRING", mode="NULLABLE"),
                    bigquery.SchemaField("user_question", "STRING", mode="REQUIRED"),
                    bigquery.SchemaField("bot_reply", "STRING", mode="REQUIRED"),
                    bigquery.SchemaField("session_phone", "STRING", mode="NULLABLE"),
                    bigquery.SchemaField("extracted_order", "STRING", mode="NULLABLE"),
                    bigquery.SchemaField("product_type", "STRING", mode="NULLABLE"),
                    bigquery.SchemaField("backend_url", "STRING", mode="NULLABLE"),
                    bigquery.SchemaField("backend_status", "INTEGER", mode="NULLABLE"),
                    bigquery.SchemaField("processing_time_ms", "FLOAT", mode="NULLABLE"),
                    bigquery.SchemaField("metadata", "JSON", mode="NULLABLE"),
                ]
                
                table = bigquery.Table(table_ref, schema=schema)
                table = client.create_table(table)
                logger.info(f"Created table {self.dataset_id}.{self.table_id}")
                
        except Exception as e:
            logger.error(f"Failed to ensure table exists: {e}")
            raise
    
    async def log_conversation(
        self,
        user_question: str,
        bot_reply: str,
        thread_id: str,
        from_phone: str,
        to_phone: Optional[str] = None,
        message_type: Optional[str] = None,
        session_phone: Optional[str] = None,
        extracted_order: Optional[str] = None,
        product_type: Optional[str] = None,
        backend_url: Optional[str] = None,
        backend_status: Optional[int] = None,
        processing_time_ms: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Log conversation data to BigQuery asynchronously
        
        Args:
            user_question: The user's original question/message
            bot_reply: The bot's response
            thread_id: Conversation thread identifier
            from_phone: Phone number of the user sending the message
            to_phone: Phone number receiving the message (optional)
            message_type: Type of message (e.g., 'user', 'bot', 'system')
            session_phone: Phone number stored in session
            extracted_order: Order number if extracted from message
            product_type: Type of product being discussed
            backend_url: URL of the backend service called
            backend_status: HTTP status code from backend
            processing_time_ms: Time taken to process the request
            metadata: Additional metadata as JSON
            
        Returns:
            bool: True if logging successful, False otherwise
        """
        
        if not self.project_id:
            logger.warning("BigQuery logging skipped - project ID not configured")
            return False
            
        try:
            return self._insert_row(
                user_question,
                bot_reply,
                thread_id,
                from_phone,
                to_phone,
                message_type,
                session_phone,
                extracted_order,
                product_type,
                backend_url,
                backend_status,
                processing_time_ms,
                metadata,
            )
            
        except Exception as e:
            logger.error(f"Failed to log conversation to BigQuery: {e}")
            return False
    
    def _insert_row(
        self,
        user_question: str,
        bot_reply: str,
        thread_id: str,
        from_phone: str,
        to_phone: Optional[str],
        message_type: Optional[str],
        session_phone: Optional[str],
        extracted_order: Optional[str],
        product_type: Optional[str],
        backend_url: Optional[str],
        backend_status: Optional[int],
        processing_time_ms: Optional[float],
        metadata: Optional[Dict[str, Any]]
    ) -> bool:
        """Insert a single row into BigQuery (blocking operation)"""
        
        try:
            self._ensure_table_exists()
            client = self._get_client()
            
            # Prepare the row data
            row_data = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "thread_id": thread_id,
                "from_phone": from_phone,
                "to_phone": to_phone,
                "user_question": user_question,
                "bot_reply": bot_reply,
                "message_type": message_type,
                "session_phone": session_phone,
                "extracted_order": extracted_order,
                "product_type": product_type,
                "backend_url": backend_url,
                "backend_status": backend_status,
                "processing_time_ms": processing_time_ms,
                "metadata": json.dumps(metadata) if metadata else None,
            }
            
            # Insert the row with schema-tolerant filtering
            table_ref = client.dataset(self.dataset_id).table(self.table_id)
            table = client.get_table(table_ref)
            allowed_fields = {f.name for f in table.schema}
            filtered_row = {k: v for k, v in row_data.items() if k in allowed_fields}
            skipped = [k for k in row_data.keys() if k not in allowed_fields]
            if skipped:
                logger.debug(f"BQ insert skipped fields (not in table schema): {skipped}")
            
            errors = client.insert_rows_json(table, [filtered_row])
            
            if errors:
                logger.error(f"BigQuery insert errors: {errors}")
                return False
            else:
                logger.info(f"Successfully logged conversation to BigQuery for thread {thread_id}")
                return True
                
        except GoogleCloudError as e:
            logger.error(f"Google Cloud error during BigQuery insert: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error during BigQuery insert: {e}")
            return False

    def _query_rows_by_phone(self, phone: str, limit: int = 100) -> List[Dict[str, Any]]:
        try:
            self._ensure_table_exists()
            client = self._get_client()
            query = f"""
                SELECT *
                FROM `{self.project_id}.{self.dataset_id}.{self.table_id}`
                WHERE from_phone = @phone
                ORDER BY timestamp DESC
                LIMIT @limit
            """
            job = client.query(query, job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("phone", "STRING", phone),
                    bigquery.ScalarQueryParameter("limit", "INT64", int(limit)),
                ]
            ))
            return [dict(row) for row in job]
        except Exception as e:
            logger.error(f"BQ query by phone failed: {e}")
            return []

    def _query_rows_by_thread(self, thread_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        try:
            self._ensure_table_exists()
            client = self._get_client()
            query = f"""
                SELECT *
                FROM `{self.project_id}.{self.dataset_id}.{self.table_id}`
                WHERE thread_id = @thread_id
                ORDER BY timestamp DESC
                LIMIT @limit
            """
            job = client.query(query, job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("thread_id", "STRING", thread_id),
                    bigquery.ScalarQueryParameter("limit", "INT64", int(limit)),
                ]
            ))
            return [dict(row) for row in job]
        except Exception as e:
            logger.error(f"BQ query by thread_id failed: {e}")
            return []

    async def fetch_by_phone(self, phone: str, limit: int = 100) -> List[Dict[str, Any]]:
        return self._query_rows_by_phone(phone, limit)

    async def fetch_by_thread(self, thread_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        return self._query_rows_by_thread(thread_id, limit)

    def _query_rows_recent(self, limit: int = 200) -> List[Dict[str, Any]]:
        try:
            self._ensure_table_exists()
            client = self._get_client()
            query = f"""
                SELECT *
                FROM `{self.project_id}.{self.dataset_id}.{self.table_id}`
                ORDER BY timestamp DESC
                LIMIT @limit
            """
            job = client.query(query, job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("limit", "INT64", int(limit)),
                ]
            ))
            return [dict(row) for row in job]
        except Exception as e:
            logger.error(f"BQ recent rows query failed: {e}")
            return []

    async def fetch_recent(self, limit: int = 200) -> List[Dict[str, Any]]:
        return self._query_rows_recent(limit)

# Global instance
_bigquery_logger = BigQueryLogger()

async def log_conversation_to_bigquery(
    user_question: str,
    bot_reply: str,
    thread_id: str,
    from_phone: str,
    to_phone: Optional[str] = None,
    message_type: Optional[str] = None,
    session_phone: Optional[str] = None,
    extracted_order: Optional[str] = None,
    product_type: Optional[str] = None,
    backend_url: Optional[str] = None,
    backend_status: Optional[int] = None,
    processing_time_ms: Optional[float] = None,
    metadata: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Convenience function to log conversation data to BigQuery
    
    This is the main function that should be imported and used in the webhook.
    """
    return await _bigquery_logger.log_conversation(
        user_question=user_question,
        bot_reply=bot_reply,
        thread_id=thread_id,
        from_phone=from_phone,
        to_phone=to_phone,
        message_type=message_type,
        session_phone=session_phone,
        extracted_order=extracted_order,
        product_type=product_type,
        backend_url=backend_url,
        backend_status=backend_status,
        processing_time_ms=processing_time_ms,
        metadata=metadata
    ) 

async def fetch_conversations_by_phone(phone: str, limit: int = 100) -> List[Dict[str, Any]]:
    return await _bigquery_logger.fetch_by_phone(phone, limit)

async def fetch_conversations_by_thread(thread_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    return await _bigquery_logger.fetch_by_thread(thread_id, limit) 

async def fetch_recent_rows(limit: int = 200) -> List[Dict[str, Any]]:
    return await _bigquery_logger.fetch_recent(limit) 
