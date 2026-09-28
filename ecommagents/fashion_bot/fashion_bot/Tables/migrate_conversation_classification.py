"""
Migration script to add conversation classification fields for template-initiated vs user-initiated conversations.
This supports the "Focused/All" conversation classification system.

Run this migration to add:
- conversation_type (user_initiated, template_initiated, template_converted)
- is_billable (boolean flag for billing)
- first_customer_message_at (timestamp when customer first engaged)
- template_send_count (counter for template sends)
- conversion_date (when template converted to engaged conversation)
- message_direction (inbound/outbound on messages table)
- message_type (user_message, template, agent_message, system_message)
"""

import logging
from fashion_bot.database_manager import get_postgres_cursor

logger = logging.getLogger(__name__)


def migrate_conversation_classification():
    """Add conversation classification fields to conversations and messages tables"""
    try:
        conn, cur = get_postgres_cursor()
        
        logger.info("="*80)
        logger.info("🚀 Starting Conversation Classification Migration")
        logger.info("="*80)
        
        # =====================================================================
        # PHASE 1: UPDATE CONVERSATIONS TABLE
        # =====================================================================
        logger.info("\n📊 Phase 1: Updating conversations table...")
        
        # 1. Add conversation_type column
        logger.info("  Adding conversation_type column...")
        cur.execute("""
            ALTER TABLE conversations 
            ADD COLUMN IF NOT EXISTS conversation_type VARCHAR(30) DEFAULT 'user_initiated';
        """)
        logger.info("  ✅ conversation_type column added")
        
        # 2. Add constraint for conversation_type
        logger.info("  Adding conversation_type constraint...")
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint 
                    WHERE conname = 'conversation_type_check'
                ) THEN
                    ALTER TABLE conversations 
                    ADD CONSTRAINT conversation_type_check 
                    CHECK (conversation_type IN ('user_initiated', 'template_initiated', 'template_converted'));
                END IF;
            END$$;
        """)
        logger.info("  ✅ conversation_type constraint added")
        
        # 3. Add is_billable column
        logger.info("  Adding is_billable column...")
        cur.execute("""
            ALTER TABLE conversations 
            ADD COLUMN IF NOT EXISTS is_billable BOOLEAN DEFAULT TRUE;
        """)
        logger.info("  ✅ is_billable column added")
        
        # 4. Add first_customer_message_at column
        logger.info("  Adding first_customer_message_at column...")
        cur.execute("""
            ALTER TABLE conversations 
            ADD COLUMN IF NOT EXISTS first_customer_message_at TIMESTAMPTZ NULL;
        """)
        logger.info("  ✅ first_customer_message_at column added")
        
        # 5. Add template_send_count column
        logger.info("  Adding template_send_count column...")
        cur.execute("""
            ALTER TABLE conversations 
            ADD COLUMN IF NOT EXISTS template_send_count INTEGER DEFAULT 0;
        """)
        logger.info("  ✅ template_send_count column added")
        
        # 6. Add conversion_date column
        logger.info("  Adding conversion_date column...")
        cur.execute("""
            ALTER TABLE conversations 
            ADD COLUMN IF NOT EXISTS conversion_date TIMESTAMPTZ NULL;
        """)
        logger.info("  ✅ conversion_date column added")
        
        # =====================================================================
        # PHASE 2: UPDATE MESSAGES TABLE
        # =====================================================================
        logger.info("\n📨 Phase 2: Updating messages table...")
        
        # 1. Add message_direction column
        logger.info("  Adding message_direction column...")
        cur.execute("""
            ALTER TABLE messages 
            ADD COLUMN IF NOT EXISTS message_direction VARCHAR(20) DEFAULT 'inbound';
        """)
        logger.info("  ✅ message_direction column added")
        
        # 2. Add constraint for message_direction
        logger.info("  Adding message_direction constraint...")
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint 
                    WHERE conname = 'message_direction_check'
                ) THEN
                    ALTER TABLE messages 
                    ADD CONSTRAINT message_direction_check 
                    CHECK (message_direction IN ('inbound', 'outbound'));
                END IF;
            END$$;
        """)
        logger.info("  ✅ message_direction constraint added")
        
        # 3. Add message_type column
        logger.info("  Adding message_type column...")
        cur.execute("""
            ALTER TABLE messages 
            ADD COLUMN IF NOT EXISTS message_type VARCHAR(30) DEFAULT 'user_message';
        """)
        logger.info("  ✅ message_type column added")
        
        # 4. Add constraint for message_type
        logger.info("  Adding message_type constraint...")
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint 
                    WHERE conname = 'message_type_check'
                ) THEN
                    ALTER TABLE messages 
                    ADD CONSTRAINT message_type_check 
                    CHECK (message_type IN ('user_message', 'template', 'agent_message', 'system_message'));
                END IF;
            END$$;
        """)
        logger.info("  ✅ message_type constraint added")
        
        # =====================================================================
        # PHASE 3: CREATE INDEXES FOR PERFORMANCE
        # =====================================================================
        logger.info("\n🔍 Phase 3: Creating indexes for performance...")
        
        # 1. Index for filtering by conversation type and billable status
        logger.info("  Creating idx_conversations_type_billable...")
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversations_type_billable 
            ON conversations(conversation_type, is_billable);
        """)
        logger.info("  ✅ idx_conversations_type_billable created")
        
        # 2. Index for focused view queries (client + type + updated_at)
        logger.info("  Creating idx_conversations_client_type_updated...")
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversations_client_type_updated 
            ON conversations(client_id, conversation_type, updated_at DESC);
        """)
        logger.info("  ✅ idx_conversations_client_type_updated created")
        
        # 3. Index for billable conversation queries
        logger.info("  Creating idx_conversations_client_billable...")
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversations_client_billable 
            ON conversations(client_id, is_billable, created_at DESC) 
            WHERE is_billable = TRUE;
        """)
        logger.info("  ✅ idx_conversations_client_billable created")
        
        # 4. Index for template conversion tracking
        logger.info("  Creating idx_conversations_template_conversion...")
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversations_template_conversion 
            ON conversations(conversation_type, conversion_date) 
            WHERE conversation_type IN ('template_initiated', 'template_converted');
        """)
        logger.info("  ✅ idx_conversations_template_conversion created")
        
        # 5. Index for message direction filtering
        logger.info("  Creating idx_messages_direction_type...")
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_direction_type 
            ON messages(message_direction, message_type, created_at DESC);
        """)
        logger.info("  ✅ idx_messages_direction_type created")
        
        # =====================================================================
        # PHASE 4: BACKFILL EXISTING DATA
        # =====================================================================
        logger.info("\n🔄 Phase 4: Backfilling existing data...")
        
        # Backfill message_direction based on message_side
        logger.info("  Backfilling message_direction from message_side...")
        cur.execute("""
            UPDATE messages
            SET message_direction = CASE
                WHEN message_side = 'user_to_system' THEN 'inbound'
                WHEN message_side = 'system_to_user' THEN 'outbound'
                ELSE 'inbound'
            END
            WHERE message_direction = 'inbound';  -- Only update default values
        """)
        rows_updated = cur.rowcount
        logger.info(f"  ✅ Backfilled message_direction for {rows_updated} messages")
        
        # Backfill message_type based on created_by
        logger.info("  Backfilling message_type from created_by...")
        cur.execute("""
            UPDATE messages
            SET message_type = CASE
                WHEN created_by = 'user' THEN 'user_message'
                WHEN created_by = 'bot' THEN 'system_message'
                WHEN created_by = 'support' THEN 'agent_message'
                ELSE 'user_message'
            END
            WHERE message_type = 'user_message';  -- Only update default values
        """)
        rows_updated = cur.rowcount
        logger.info(f"  ✅ Backfilled message_type for {rows_updated} messages")
        
        # Mark all existing conversations as user_initiated and billable
        logger.info("  Marking existing conversations as user_initiated...")
        cur.execute("""
            UPDATE conversations
            SET conversation_type = 'user_initiated',
                is_billable = TRUE
            WHERE conversation_type IS NULL OR conversation_type = 'user_initiated';
        """)
        rows_updated = cur.rowcount
        logger.info(f"  ✅ Marked {rows_updated} existing conversations as user_initiated")
        
        # Set first_customer_message_at for existing conversations
        logger.info("  Setting first_customer_message_at for existing conversations...")
        cur.execute("""
            UPDATE conversations c
            SET first_customer_message_at = (
                SELECT MIN(m.created_at)
                FROM messages m
                WHERE m.conversation_id = c.conversation_id
                AND m.message_side = 'user_to_system'
            )
            WHERE first_customer_message_at IS NULL
            AND EXISTS (
                SELECT 1 FROM messages m
                WHERE m.conversation_id = c.conversation_id
                AND m.message_side = 'user_to_system'
            );
        """)
        rows_updated = cur.rowcount
        logger.info(f"  ✅ Set first_customer_message_at for {rows_updated} conversations")
        
        # =====================================================================
        # COMMIT AND FINISH
        # =====================================================================
        conn.commit()
        logger.info("\n" + "="*80)
        logger.info("✅ Conversation Classification Migration Completed Successfully!")
        logger.info("="*80)
        logger.info("\n📊 Summary:")
        logger.info("  ✓ Added 6 new fields to conversations table")
        logger.info("  ✓ Added 2 new fields to messages table")
        logger.info("  ✓ Created 5 performance indexes")
        logger.info("  ✓ Backfilled existing data")
        logger.info("\n🎯 Next Steps:")
        logger.info("  1. Update template sending code to mark conversations as template_initiated")
        logger.info("  2. Add conversion logic when customers reply to templates")
        logger.info("  3. Update billing calculations to use is_billable flag")
        logger.info("  4. Update UI to show Focused/All views")
        logger.info("="*80 + "\n")
        
        cur.close()
        conn.close()
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Error during conversation classification migration: {str(e)}")
        logger.error(f"   Error details: {e}", exc_info=True)
        if 'conn' in locals():
            conn.rollback()
            conn.close()
        return False


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    success = migrate_conversation_classification()
    exit(0 if success else 1)

