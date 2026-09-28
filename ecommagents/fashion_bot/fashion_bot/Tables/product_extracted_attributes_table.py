def create_product_extracted_attributes_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS product_extracted_attributes (
            id SERIAL PRIMARY KEY,
            client_id UUID REFERENCES clients(id) ON DELETE CASCADE,
            product_id TEXT NOT NULL,
            product_title TEXT,
            category TEXT,
            subcategory TEXT,
            base_product_name TEXT,
            product_line TEXT,
            color TEXT,
            material TEXT,
            occasion JSONB DEFAULT '[]',
            style JSONB DEFAULT '[]',
            vibe JSONB DEFAULT '[]',
            pairing_tags JSONB DEFAULT '[]',
            segment TEXT,
            color_family TEXT,
            pattern TEXT,
            fit TEXT,
            is_manually_edited BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (client_id, product_id)
        );

        CREATE INDEX IF NOT EXISTS idx_product_extracted_attrs_client
            ON product_extracted_attributes (client_id);
        CREATE INDEX IF NOT EXISTS idx_product_extracted_attrs_manual
            ON product_extracted_attributes (client_id, is_manually_edited)
            WHERE is_manually_edited = TRUE;
    """)
