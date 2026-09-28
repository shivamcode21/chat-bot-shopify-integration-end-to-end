def create_client_shop_content_cache_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS client_shop_content_cache (
            client_id          UUID PRIMARY KEY REFERENCES clients(id) ON DELETE CASCADE,
            promo_terms        JSONB NOT NULL DEFAULT '[]'::jsonb,
            shop_image_terms   JSONB NOT NULL DEFAULT '[]'::jsonb,
            content_hash       TEXT NOT NULL,
            source_url         TEXT,
            extracted_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        -- Forward-compatible: add column on already-deployed instances
        ALTER TABLE client_shop_content_cache
            ADD COLUMN IF NOT EXISTS shop_image_terms JSONB NOT NULL DEFAULT '[]'::jsonb;
    """)
