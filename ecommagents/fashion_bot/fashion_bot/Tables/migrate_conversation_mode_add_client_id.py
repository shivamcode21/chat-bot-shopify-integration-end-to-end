"""
Migration script to add client_id column to conversation_mode_human_agent_and_bot table.

This script:
1. Drops old PRIMARY KEY constraint (to allow phone number normalization)
2. Adds client_id column (if not exists)
3. Populates existing rows with resolved client_id from resolve_client_id()
4. Normalizes phone numbers (removes '+', ensures starts with '91')
5. Handles duplicate phone numbers (keeps most recent for each client_id + phone_number)
6. Makes client_id NOT NULL after population
7. Creates new composite PRIMARY KEY on (client_id, phone_number)

Run this before deploying the updated code.
"""

import os
from dotenv import load_dotenv
from fashion_bot.database_manager import get_postgres_cursor
from fashion_bot.config_manager import resolve_client_id

def migrate_add_client_id():
    """Add client_id column, normalize phones, and update primary key"""
    
    # Load environment variables
    load_dotenv()
    
    # Check if DATABASE_URL is set
    if not os.getenv("DATABASE_URL"):
        raise ValueError("DATABASE_URL environment variable is not set. Please check your .env file.")
    
    resolved_client_id = resolve_client_id()
    print(f"Using resolved client_id: {resolved_client_id}")
    
    conn = None
    try:
        conn, cur = get_postgres_cursor()
        
        print("\n" + "="*80)
        print("Starting migration: Adding client_id to conversation_mode_human_agent_and_bot")
        print("="*80 + "\n")
        
        # Step 1: Drop old primary key constraint FIRST (before normalizing)
        print("Step 1: Dropping old PRIMARY KEY constraint...")
        cur.execute("""
            ALTER TABLE conversation_mode_human_agent_and_bot 
            DROP CONSTRAINT IF EXISTS conversation_mode_human_agent_and_bot_pkey;
        """)
        conn.commit()
        print("✓ Old primary key dropped")
        
        # Step 2: Add client_id column (nullable first to allow existing data)
        print("\nStep 2: Adding client_id column...")
        cur.execute("""
            ALTER TABLE conversation_mode_human_agent_and_bot 
            ADD COLUMN IF NOT EXISTS client_id VARCHAR(100);
        """)
        conn.commit()
        print("✓ Column added")
        
        # Step 3: Populate existing rows with default client_id
        print(f"\nStep 3: Populating existing rows with client_id = '{resolved_client_id}'...")
        cur.execute("""
            UPDATE conversation_mode_human_agent_and_bot 
            SET client_id = %s 
            WHERE client_id IS NULL;
        """, (resolved_client_id,))
        rows_updated = cur.rowcount
        conn.commit()
        print(f"✓ Updated {rows_updated} rows")
        
        # Step 4: Normalize phone numbers (remove '+', ensure starts with '91')
        print("\nStep 4: Normalizing phone numbers...")
        cur.execute("""
            UPDATE conversation_mode_human_agent_and_bot
            SET phone_number = CASE
                -- Remove '+' if present
                WHEN phone_number LIKE '+%' THEN REPLACE(phone_number, '+', '')
                ELSE phone_number
            END;
        """)
        conn.commit()
        
        cur.execute("""
            UPDATE conversation_mode_human_agent_and_bot
            SET phone_number = CASE
                -- Add '91' prefix if missing
                WHEN LENGTH(phone_number) = 10 THEN '91' || phone_number
                ELSE phone_number
            END;
        """)
        rows_normalized = cur.rowcount
        conn.commit()
        print(f"✓ Normalized {rows_normalized} phone numbers")
        
        # Step 5: Handle duplicates - keep the most recent conversation mode for each (client_id, phone_number)
        print("\nStep 5: Handling duplicate phone numbers (keeping most recent)...")
        cur.execute("""
            DELETE FROM conversation_mode_human_agent_and_bot a
            USING conversation_mode_human_agent_and_bot b
            WHERE a.client_id = b.client_id 
              AND a.phone_number = b.phone_number
              AND a.ctid < b.ctid;
        """)
        duplicates_removed = cur.rowcount
        conn.commit()
        print(f"✓ Removed {duplicates_removed} duplicate records")
        
        # Step 6: Make client_id NOT NULL
        print("\nStep 6: Making client_id NOT NULL...")
        cur.execute("""
            ALTER TABLE conversation_mode_human_agent_and_bot 
            ALTER COLUMN client_id SET NOT NULL;
        """)
        conn.commit()
        print("✓ Column set to NOT NULL")
        
        # Step 7: Add new composite primary key
        print("\nStep 7: Creating new composite PRIMARY KEY (client_id, phone_number)...")
        cur.execute("""
            ALTER TABLE conversation_mode_human_agent_and_bot 
            ADD CONSTRAINT conversation_mode_human_agent_and_bot_pkey 
            PRIMARY KEY (client_id, phone_number);
        """)
        conn.commit()
        print("✓ New composite primary key created")
        
        # Verify the migration
        print("\nVerifying migration...")
        cur.execute("""
            SELECT column_name, data_type, is_nullable 
            FROM information_schema.columns 
            WHERE table_name = 'conversation_mode_human_agent_and_bot'
            ORDER BY ordinal_position;
        """)
        columns = cur.fetchall()
        print("\nTable structure:")
        for col in columns:
            print(f"  - {col[0]}: {col[1]} (nullable: {col[2]})")
        
        cur.close()
        conn.close()
        
        print("\n✅ Migration completed successfully!")
        print(f"   All existing records now have client_id = '{resolved_client_id}'")
        print("   Primary key is now (client_id, phone_number)")
        
    except Exception as e:
        print(f"\n❌ Migration failed: {e}")
        if conn:
            conn.rollback()
        raise

if __name__ == "__main__":
    migrate_add_client_id()
