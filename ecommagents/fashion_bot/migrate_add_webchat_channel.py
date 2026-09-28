"""
Migration script to add 'web-chat' to channel_type constraint

Run this once to update your database schema to support web-chat conversations
"""
import sys
sys.path.insert(0, '/Users/shivammehrotra/git-bot/ecommagents/fashion_bot')

from fashion_bot.database_manager import get_postgres_connection

def migrate_channel_type_constraint():
    """
    Add 'web-chat' to the channel_type constraint for both conversations and messages tables
    Also expand phone column to support longer session IDs
    """
    
    print("="*60)
    print("Migration: Add 'web-chat' Support")
    print("="*60)
    
    try:
        with get_postgres_connection() as conn:
            with conn.cursor() as cur:
                # First, expand phone column to support session IDs
                print("\n0. Expanding phone column to support session IDs...")
                print("   conversations.phone: VARCHAR(20) → VARCHAR(255)")
                cur.execute("""
                    ALTER TABLE conversations 
                    ALTER COLUMN phone TYPE VARCHAR(255);
                """)
                
                print("   messages.phone: VARCHAR(20) → VARCHAR(255)")
                cur.execute("""
                    ALTER TABLE messages 
                    ALTER COLUMN phone TYPE VARCHAR(255);
                """)
                print("   ✅ Phone columns expanded")
                # Check if conversations table has a constraint
                print("\n1. Checking conversations table constraint...")
                cur.execute("""
                    SELECT constraint_name 
                    FROM information_schema.table_constraints 
                    WHERE table_name = 'conversations' 
                    AND constraint_type = 'CHECK'
                    AND constraint_name LIKE '%channel_type%';
                """)
                conv_constraint = cur.fetchone()
                
                if conv_constraint:
                    print(f"   Found constraint: {conv_constraint[0]}")
                    # Drop old constraint on conversations
                    print("2. Dropping old constraint on conversations...")
                    cur.execute(f"""
                        ALTER TABLE conversations 
                        DROP CONSTRAINT IF EXISTS {conv_constraint[0]};
                    """)
                else:
                    print("   No constraint found (will add new one)")
                
                # Add new constraint with web-chat to conversations
                print("3. Adding new constraint to conversations with 'web-chat'...")
                cur.execute("""
                    ALTER TABLE conversations
                    DROP CONSTRAINT IF EXISTS ck_conversation_channel_type;
                """)
                cur.execute("""
                    ALTER TABLE conversations
                    ADD CONSTRAINT ck_conversation_channel_type 
                    CHECK (channel_type IN ('whatsapp', 'instagram', 'email', 'web-chat'));
                """)
                
                # Drop old constraint on messages
                print("4. Dropping old constraint on messages...")
                cur.execute("""
                    ALTER TABLE messages 
                    DROP CONSTRAINT IF EXISTS ck_message_channel_type;
                """)
                
                # Add new constraint with web-chat to messages
                print("5. Adding new constraint to messages with 'web-chat'...")
                cur.execute("""
                    ALTER TABLE messages
                    ADD CONSTRAINT ck_message_channel_type 
                    CHECK (channel_type IN ('whatsapp', 'instagram', 'email', 'web-chat'));
                """)
                
                conn.commit()
                
                print("\n" + "="*60)
                print("✅ Migration completed successfully!")
                print("="*60)
                print("\nUpdated tables:")
                print("  ✅ conversations (constraint: ck_conversation_channel_type)")
                print("  ✅ messages (constraint: ck_message_channel_type)")
                print("\nSupported channel types:")
                print("  - whatsapp")
                print("  - instagram")
                print("  - email")
                print("  - web-chat (NEW!)")
                print("="*60)
                
    except Exception as e:
        print(f"\n❌ Migration failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    migrate_channel_type_constraint()

