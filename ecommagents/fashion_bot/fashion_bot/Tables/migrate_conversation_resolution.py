"""
Migration script to add conversation resolution tracking fields.
"""
import logging
from fashion_bot.database_manager import get_postgres_cursor

logger = logging.getLogger(__name__)


def add_conversation_resolution_tracking():
    """Add resolution tracking fields to conversations table"""
    try:
        conn, cur = get_postgres_cursor()
        
        logger.info("Adding conversation resolution tracking columns...")
        
        # Add is_resolved column (nullable, default NULL)
        cur.execute("""
            ALTER TABLE conversations 
            ADD COLUMN IF NOT EXISTS is_resolved BOOLEAN DEFAULT NULL;
        """)
        logger.info("✅ Added is_resolved column")
        
        # Add resolved_at column
        cur.execute("""
            ALTER TABLE conversations 
            ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;
        """)
        logger.info("✅ Added resolved_at column")
        
        # Add is_loop column (for loop detection)
        cur.execute("""
            ALTER TABLE conversations 
            ADD COLUMN IF NOT EXISTS is_loop BOOLEAN DEFAULT FALSE;
        """)
        logger.info("✅ Added is_loop column")
        
        # Add unresolved_reason column
        cur.execute("""
            ALTER TABLE conversations 
            ADD COLUMN IF NOT EXISTS unresolved_reason TEXT;
        """)
        logger.info("✅ Added unresolved_reason column")
        
        # Create index for querying resolved/unresolved conversations
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversations_resolution 
            ON conversations (client_id, is_resolved, updated_at)
            WHERE is_resolved IS NOT NULL;
        """)
        logger.info("✅ Created resolution index")
        
        # GIN index for tags (for escalation queries) if not exists
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversations_tags 
            ON conversations USING GIN (tags);
        """)
        logger.info("✅ Created tags GIN index")
        
        conn.commit()
        cur.close()
        conn.close()
        
        logger.info("✅ Conversation resolution tracking migration completed successfully")
        return True
        
    except Exception as e:
        logger.error(f"❌ Error adding conversation resolution tracking: {e}")
        return False


if __name__ == "__main__":
    # Run migration
    logging.basicConfig(level=logging.INFO)
    success = add_conversation_resolution_tracking()
    print(f"\nMigration {'succeeded' if success else 'failed'}")

