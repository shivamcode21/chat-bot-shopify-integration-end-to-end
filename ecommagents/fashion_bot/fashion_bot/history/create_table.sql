-- BigQuery CREATE TABLE statement for WhatsApp conversation logs
-- This matches the schema defined in bigquery_logger.py

CREATE TABLE IF NOT EXISTS `your-project-id.whatsapp_conversations.conversation_logs` (
  timestamp TIMESTAMP NOT NULL,
  thread_id STRING NOT NULL,
  from_phone STRING NOT NULL,
  to_phone STRING,
  user_question STRING NOT NULL,
  bot_reply STRING NOT NULL,
  message_type STRING,
  session_phone STRING,
  extracted_order STRING,
  product_type STRING,
  backend_url STRING,
  backend_status INT64,
  processing_time_ms FLOAT64,
  metadata JSON
)
PARTITION BY DATE(timestamp)
CLUSTER BY thread_id, from_phone
OPTIONS (
  description = "WhatsApp conversation logs with bot interactions",
  labels = [("environment", "production"), ("service", "whatsapp-bot")]
);

-- Alternative version without partitioning and clustering (simpler)
-- CREATE TABLE IF NOT EXISTS `your-project-id.whatsapp_conversations.conversation_logs` (
--   timestamp TIMESTAMP NOT NULL,
--   thread_id STRING NOT NULL,
--   from_phone STRING NOT NULL,
--   to_phone STRING,
--   user_question STRING NOT NULL,
--   bot_reply STRING NOT NULL,
--   message_type STRING,
--   session_phone STRING,
--   extracted_order STRING,
--   product_type STRING,
--   backend_url STRING,
--   backend_status INT64,
--   processing_time_ms FLOAT64,
--   metadata JSON
-- );

-- Sample queries for analysis:

-- 1. Get conversation history for a specific phone number
-- SELECT 
--   timestamp,
--   user_question,
--   bot_reply,
--   processing_time_ms
-- FROM `your-project-id.whatsapp_conversations.conversation_logs`
-- WHERE from_phone = '+919876543210'
-- ORDER BY timestamp DESC
-- LIMIT 50;

-- 2. Average response time by day
-- SELECT 
--   DATE(timestamp) as conversation_date,
--   COUNT(*) as total_conversations,
--   AVG(processing_time_ms) as avg_response_time_ms,
--   MAX(processing_time_ms) as max_response_time_ms
-- FROM `your-project-id.whatsapp_conversations.conversation_logs`
-- WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
-- GROUP BY DATE(timestamp)
-- ORDER BY conversation_date DESC;

-- 3. Most common user questions
-- SELECT 
--   user_question,
--   COUNT(*) as frequency
-- FROM `your-project-id.whatsapp_conversations.conversation_logs`
-- WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
-- GROUP BY user_question
-- ORDER BY frequency DESC
-- LIMIT 20;

-- 4. Backend API performance analysis
-- SELECT 
--   backend_url,
--   backend_status,
--   COUNT(*) as request_count,
--   AVG(processing_time_ms) as avg_processing_time
-- FROM `your-project-id.whatsapp_conversations.conversation_logs`
-- WHERE backend_status IS NOT NULL
-- GROUP BY backend_url, backend_status
-- ORDER BY request_count DESC; 