"""
Migration script to create template_delivery_logs table for tracking template message sends.
"""
import logging
from fashion_bot.database_manager import get_postgres_cursor

logger = logging.getLogger(__name__)


def create_template_delivery_logs_table():
    """Create table to track template message delivery status"""
    try:
        conn, cur = get_postgres_cursor()
        
        logger.info("Creating template_delivery_logs table...")
        
        # Create main table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS template_delivery_logs (
                log_id BIGSERIAL PRIMARY KEY,
                client_id VARCHAR(100) NOT NULL,
                phone_number VARCHAR(20) NOT NULL,
                template_id VARCHAR(255) NOT NULL,
                template_name VARCHAR(255),
                event_key VARCHAR(100),
                channel VARCHAR(20) NOT NULL DEFAULT 'whatsapp',
                success BOOLEAN NOT NULL,
                response_data JSONB,
                error_message TEXT,
                sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)
        logger.info("✅ Created template_delivery_logs table")
        
        # Create indexes for better query performance
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_template_logs_client_phone 
            ON template_delivery_logs (client_id, phone_number, sent_at DESC);
        """)
        logger.info("✅ Created index on client_id and phone_number")
        
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_template_logs_template_id 
            ON template_delivery_logs (template_id, sent_at DESC);
        """)
        logger.info("✅ Created index on template_id")
        
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_template_logs_success 
            ON template_delivery_logs (success, sent_at DESC);
        """)
        logger.info("✅ Created index on success status")
        
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_template_logs_event_key 
            ON template_delivery_logs (event_key, sent_at DESC)
            WHERE event_key IS NOT NULL;
        """)
        logger.info("✅ Created index on event_key")
        
        # Add constraint if not exists
        cur.execute("""
            DO $$ 
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint 
                    WHERE conname = 'ck_template_logs_channel'
                ) THEN
                    ALTER TABLE template_delivery_logs 
                    ADD CONSTRAINT ck_template_logs_channel 
                    CHECK (channel IN ('whatsapp', 'sms', 'email'));
                END IF;
            END $$;
        """)
        logger.info("✅ Added channel constraint")
        
        # Add template_name column if table already exists (for existing deployments)
        cur.execute("""
            ALTER TABLE template_delivery_logs 
            ADD COLUMN IF NOT EXISTS template_name VARCHAR(255);
        """)
        logger.info("✅ Added/verified template_name column")
        
        conn.commit()
        cur.close()
        conn.close()
        
        logger.info("✅ Template delivery logs migration completed successfully")
        return True
        
    except Exception as e:
        logger.error(f"❌ Error creating template_delivery_logs table: {e}")
        return False


if __name__ == "__main__":
    # Run migration
    logging.basicConfig(level=logging.INFO)
    success = create_template_delivery_logs_table()
    print(f"\nMigration {'succeeded' if success else 'failed'}")

