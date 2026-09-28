"""
Migration script to rename tenant_id to client_id in gupshup template tables.

This script:
1. Renames tenant_id to client_id in gupshup_templates table
2. Renames tenant_id to client_id in gupshup_template_catalog table
3. Updates all constraints and indexes

Run this before deploying the updated code.
"""

from dotenv import load_dotenv
from fashion_bot.database_manager import get_postgres_cursor

def migrate_tenant_id_to_client_id():
    """Rename tenant_id to client_id in gupshup template tables"""
    
    # Load environment variables
    load_dotenv()
    
    conn = None
    try:
        conn, cur = get_postgres_cursor()
        
        print("\n" + "="*80)
        print("Starting migration: Renaming tenant_id to client_id in gupshup templates")
        print("="*80 + "\n")
        
        # ========================================================================
        # TABLE 1: gupshup_templates
        # ========================================================================
        print("Step 1: Migrating gupshup_templates table...")
        
        # Check if tenant_id column exists
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='gupshup_templates' AND column_name='tenant_id'
            ) as column_exists;
        """)
        result = cur.fetchone()
        tenant_id_exists = result['column_exists'] if result else False
        
        # Check if client_id column already exists
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='gupshup_templates' AND column_name='client_id'
            ) as column_exists;
        """)
        result = cur.fetchone()
        client_id_exists = result['column_exists'] if result else False
        
        if tenant_id_exists and not client_id_exists:
            print("  - Renaming tenant_id to client_id in gupshup_templates...")
            cur.execute("ALTER TABLE gupshup_templates RENAME COLUMN tenant_id TO client_id;")
            conn.commit()
            print("  ✓ Renamed tenant_id to client_id")
        elif client_id_exists:
            print("  ✓ client_id column already exists")
        else:
            print("  ⚠ No tenant_id or client_id column found")
        
        # Update unique constraint if it exists
        print("  - Updating constraints and indexes...")
        
        # Drop old constraint if exists (Tables version)
        cur.execute("""
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'gupshup_templates_tenant_id_event_key_key') THEN
                    ALTER TABLE gupshup_templates DROP CONSTRAINT gupshup_templates_tenant_id_event_key_key;
                END IF;
            END $$;
        """)
        
        # Drop old index if exists (Shopify version)
        cur.execute("DROP INDEX IF EXISTS gupshup_templates_tenant_channel_event_key_idx;")
        
        # Create new unique constraint/index with client_id
        # Check if channel column exists to determine which constraint to create
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='gupshup_templates' AND column_name='channel'
            ) as column_exists;
        """)
        result = cur.fetchone()
        has_channel = result['column_exists'] if result else False
        
        if has_channel:
            # Shopify version with channel
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS gupshup_templates_client_channel_event_key_idx 
                ON gupshup_templates(client_id, channel, event_key);
            """)
            print("  ✓ Created unique index on (client_id, channel, event_key)")
        else:
            # Tables version without channel
            cur.execute("""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'gupshup_templates_client_id_event_key_key') THEN
                        ALTER TABLE gupshup_templates 
                        ADD CONSTRAINT gupshup_templates_client_id_event_key_key 
                        UNIQUE (client_id, event_key);
                    END IF;
                END $$;
            """)
            print("  ✓ Created unique constraint on (client_id, event_key)")
        
        conn.commit()
        print("  ✓ gupshup_templates table migrated successfully\n")
        
        # ========================================================================
        # TABLE 2: gupshup_template_catalog
        # ========================================================================
        print("Step 2: Migrating gupshup_template_catalog table...")
        
        # Check if tenant_id column exists
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='gupshup_template_catalog' AND column_name='tenant_id'
            ) as column_exists;
        """)
        result = cur.fetchone()
        tenant_id_exists = result['column_exists'] if result else False
        
        # Check if client_id column already exists
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='gupshup_template_catalog' AND column_name='client_id'
            ) as column_exists;
        """)
        result = cur.fetchone()
        client_id_exists = result['column_exists'] if result else False
        
        if tenant_id_exists and not client_id_exists:
            print("  - Renaming tenant_id to client_id in gupshup_template_catalog...")
            cur.execute("ALTER TABLE gupshup_template_catalog RENAME COLUMN tenant_id TO client_id;")
            conn.commit()
            print("  ✓ Renamed tenant_id to client_id")
        elif client_id_exists:
            print("  ✓ client_id column already exists")
        else:
            print("  ⚠ No tenant_id or client_id column found")
        
        # Update unique constraint
        cur.execute("""
            DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'gupshup_template_catalog_tenant_id_name_language_code_key') THEN
                    ALTER TABLE gupshup_template_catalog 
                    DROP CONSTRAINT gupshup_template_catalog_tenant_id_name_language_code_key;
                END IF;
            END $$;
        """)
        
        cur.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'gupshup_template_catalog_client_id_name_language_code_key') THEN
                    ALTER TABLE gupshup_template_catalog 
                    ADD CONSTRAINT gupshup_template_catalog_client_id_name_language_code_key 
                    UNIQUE (client_id, name, language_code);
                END IF;
            END $$;
        """)
        
        conn.commit()
        print("  ✓ gupshup_template_catalog table migrated successfully\n")
        
        # Verify the migration
        print("Verifying migration...")
        
        print("\ngupshup_templates table structure:")
        cur.execute("""
            SELECT column_name, data_type, is_nullable 
            FROM information_schema.columns 
            WHERE table_name = 'gupshup_templates'
            ORDER BY ordinal_position;
        """)
        columns = cur.fetchall()
        for col in columns:
            print(f"  - {col['column_name']}: {col['data_type']} (nullable: {col['is_nullable']})")
        
        print("\ngupshup_template_catalog table structure:")
        cur.execute("""
            SELECT column_name, data_type, is_nullable 
            FROM information_schema.columns 
            WHERE table_name = 'gupshup_template_catalog'
            ORDER BY ordinal_position;
        """)
        columns = cur.fetchall()
        for col in columns:
            print(f"  - {col['column_name']}: {col['data_type']} (nullable: {col['is_nullable']})")
        
        cur.close()
        conn.close()
        
        print("\n✅ Migration completed successfully!")
        print("   - tenant_id → client_id in gupshup_templates")
        print("   - tenant_id → client_id in gupshup_template_catalog")
        print("   - All constraints and indexes updated")
        
    except Exception as e:
        print(f"\n❌ Migration failed: {e}")
        if conn:
            conn.rollback()
        raise

if __name__ == "__main__":
    migrate_tenant_id_to_client_id()

