def create_product_image_ocr_cache_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS product_image_ocr_cache (
            client_id           UUID REFERENCES clients(id) ON DELETE CASCADE,
            image_url_sha256    TEXT NOT NULL,
            image_url           TEXT NOT NULL,
            source              TEXT NOT NULL DEFAULT 'gallery',
            extracted_text      TEXT NOT NULL DEFAULT '',
            summary_terms       JSONB NOT NULL DEFAULT '[]'::jsonb,
            has_text            BOOLEAN NOT NULL DEFAULT FALSE,
            model               TEXT NOT NULL,
            prompt_version      SMALLINT NOT NULL DEFAULT 1,
            extracted_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (client_id, image_url_sha256)
        );

        CREATE INDEX IF NOT EXISTS idx_product_image_ocr_cache_client_extracted_at
            ON product_image_ocr_cache (client_id, extracted_at);
    """)
