"""
Migration script to add gupshup_source_number and shopify_domain_name columns to clients table.

This script:
1. Adds gupshup_source_number column (if not exists)
2. Adds shopify_domain_name column (if not exists)

Run this before deploying the updated code.
"""

from dotenv import load_dotenv
from fashion_bot.database_manager import get_postgres_cursor

def migrate_add_shopify_gupshup_columns():
    """Add gupshup_source_number and shopify_domain_name columns to clients table"""
    
    # Load environment variables
    load_dotenv()
    
    conn = None
    try:
        conn, cur = get_postgres_cursor()
        
        print("\n" + "="*80)
        print("Starting migration: Adding shopify_domain_name and gupshup_source_number to clients")
        print("="*80 + "\n")
        
        # Step 1: Add gupshup_source_number column if it doesn't exist
        print("Step 1: Checking and adding gupshup_source_number column...")
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='clients' AND column_name='gupshup_source_number'
            ) as column_exists;
        """)
        result = cur.fetchone()
        gupshup_column_exists = result['column_exists'] if result else False
        
        if not gupshup_column_exists:
            cur.execute("ALTER TABLE clients ADD COLUMN gupshup_source_number TEXT;")
            conn.commit()
            print("✓ Added gupshup_source_number column")
        else:
            print("✓ gupshup_source_number column already exists")
        
        # Step 2: Add shopify_domain_name column if it doesn't exist
        print("\nStep 2: Checking and adding shopify_domain_name column...")
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='clients' AND column_name='shopify_domain_name'
            ) as column_exists;
        """)
        result = cur.fetchone()
        shopify_column_exists = result['column_exists'] if result else False
        
        if not shopify_column_exists:
            cur.execute("ALTER TABLE clients ADD COLUMN shopify_domain_name TEXT;")
            conn.commit()
            print("✓ Added shopify_domain_name column")
        else:
            print("✓ shopify_domain_name column already exists")
        
        # Verify the migration
        print("\nVerifying migration...")
        cur.execute("""
            SELECT column_name, data_type, is_nullable 
            FROM information_schema.columns 
            WHERE table_name = 'clients'
            ORDER BY ordinal_position;
        """)
        columns = cur.fetchall()
        print("\nClients table structure:")
        for col in columns:
            print(f"  - {col['column_name']}: {col['data_type']} (nullable: {col['is_nullable']})")
        
        cur.close()
        conn.close()
        
        print("\n✅ Migration completed successfully!")
        print("   New columns added to clients table:")
        print("   - gupshup_source_number (TEXT, nullable)")
        print("   - shopify_domain_name (TEXT, nullable)")
        
    except Exception as e:
        print(f"\n❌ Migration failed: {e}")
        if conn:
            conn.rollback()
        raise

if __name__ == "__main__":
    migrate_add_shopify_gupshup_columns()

