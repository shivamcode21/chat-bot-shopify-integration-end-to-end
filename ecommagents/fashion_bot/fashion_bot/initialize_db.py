from fashion_bot.Tables.client_intent_table import create_client_intents_table
from fashion_bot.Tables.client_llm_prompt_table import create_client_llm_prompts_table
from fashion_bot.Tables.client_order_format_table import create_client_order_formats_table
from fashion_bot.Tables.client_table import create_clients_table
from fashion_bot.Tables.clients_config_table import create_client_configs_table
from fashion_bot.Tables.conversation_mode_human_agent_and_bot import conversation_mode_human_agent_and_bot
from fashion_bot.Tables.attribution_table import initialize_attribution_tables
from fashion_bot.Tables.webhook_verification_tokens_table import create_webhook_verification_tokens_table
from fashion_bot.Tables.product_sync_logs_table import create_product_sync_logs_table
from fashion_bot.Tables.migrate_product_sync_logs_progressive import migrate_product_sync_logs_progressive
from fashion_bot.Tables.product_extracted_attributes_table import create_product_extracted_attributes_table
from fashion_bot.Tables.client_taxonomy_config_table import create_client_taxonomy_config_table
from fashion_bot.Tables.product_image_ocr_cache_table import create_product_image_ocr_cache_table
from fashion_bot.Tables.client_shop_content_cache_table import create_client_shop_content_cache_table
from fashion_bot.Tables.product_ocr_summary_cache_table import create_product_ocr_summary_cache_table
from fashion_bot.Tables.return_prime_webhook_events_table import create_return_prime_webhook_events_table
from fashion_bot.Tables.return_prime_event_notifications_table import create_return_prime_event_notifications_table
from fashion_bot.database_manager import get_postgres_cursor


def initialize_database():
    # Replace with your actual PostgreSQL connection URL
    conn,cursor = get_postgres_cursor()

    # Run all table creation functions
    create_clients_table(cursor)
    create_client_configs_table(cursor)
    create_client_intents_table(cursor)
    create_client_llm_prompts_table(cursor)
    create_client_order_formats_table(cursor)
    conversation_mode_human_agent_and_bot(cursor)
    create_webhook_verification_tokens_table(cursor)
    
    # Attribution tables
    initialize_attribution_tables(cursor)

    # Product ingestion tables
    create_product_sync_logs_table(cursor)
    migrate_product_sync_logs_progressive(cursor)
    create_product_extracted_attributes_table(cursor)
    create_client_taxonomy_config_table(cursor)
    create_product_image_ocr_cache_table(cursor)
    create_client_shop_content_cache_table(cursor)
    create_product_ocr_summary_cache_table(cursor)

    # Return Prime webhook + notification tables
    create_return_prime_webhook_events_table(cursor)
    create_return_prime_event_notifications_table(cursor)

    # Commit and close
    conn.commit()
    cursor.close()
    conn.close()


if __name__ == "__main__":
    initialize_database()
