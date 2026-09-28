def create_clients_table(cursor):
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS clients (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        name TEXT NOT NULL,
        domain TEXT,
        logo_url TEXT,
        support_email TEXT,
        created_at TIMESTAMP DEFAULT NOW()
    );
    """)
    
    # Add gupshup_source_number column if it doesn't exist
    cursor.execute("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns 
            WHERE table_name='clients' AND column_name='gupshup_source_number'
        ) as column_exists;
    """)
    result = cursor.fetchone()
    gupshup_column_exists = result['column_exists'] if result else False
    
    if not gupshup_column_exists:
        cursor.execute("ALTER TABLE clients ADD COLUMN gupshup_source_number TEXT;")
        print("✓ Added gupshup_source_number column to clients table")
    
    # Add shopify_domain_name column if it doesn't exist
    cursor.execute("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns 
            WHERE table_name='clients' AND column_name='shopify_domain_name'
        ) as column_exists;
    """)
    result = cursor.fetchone()
    shopify_column_exists = result['column_exists'] if result else False
    
    if not shopify_column_exists:
        cursor.execute("ALTER TABLE clients ADD COLUMN shopify_domain_name TEXT;")
        print("✓ Added shopify_domain_name column to clients table")