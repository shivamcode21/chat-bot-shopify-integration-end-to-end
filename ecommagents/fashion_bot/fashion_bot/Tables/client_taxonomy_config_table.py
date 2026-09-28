def create_client_taxonomy_config_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS client_taxonomy_config (
            id SERIAL PRIMARY KEY,
            client_id UUID REFERENCES clients(id) ON DELETE CASCADE,
            categories JSONB NOT NULL DEFAULT '[]',
            subcategory_mapping JSONB NOT NULL DEFAULT '{}',
            attribute_schema JSONB NOT NULL DEFAULT '{}',
            occasions JSONB NOT NULL DEFAULT '[]',
            styles JSONB NOT NULL DEFAULT '[]',
            vibes JSONB NOT NULL DEFAULT '[]',
            segments JSONB NOT NULL DEFAULT '[]',
            color_families JSONB NOT NULL DEFAULT '[]',
            patterns JSONB NOT NULL DEFAULT '[]',
            fits JSONB NOT NULL DEFAULT '[]',
            pairing_tags JSONB NOT NULL DEFAULT '[]',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (client_id)
        );
    """)
    for col in ("fits", "pairing_tags"):
        cursor.execute(f"""
            ALTER TABLE client_taxonomy_config
            ADD COLUMN IF NOT EXISTS {col} JSONB NOT NULL DEFAULT '[]';
        """)
