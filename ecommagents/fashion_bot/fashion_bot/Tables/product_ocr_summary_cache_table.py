def create_product_ocr_summary_cache_table(cursor):
    """Per-product OCR summary cache. One row per (client_id, product_id).

    Hit criterion: the row's ``url_set_sha256`` matches the SHA256 of the
    product's current sorted image-URL set. On hit we reuse ``summary``
    verbatim; on miss we re-run the combined OCR LLM call with whatever
    per-image text we already have cached (in product_image_ocr_cache).
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS product_ocr_summary_cache (
            client_id        UUID REFERENCES clients(id) ON DELETE CASCADE,
            product_id       TEXT NOT NULL,
            url_set_sha256   TEXT NOT NULL,
            summary          TEXT NOT NULL,
            per_image_texts  JSONB NOT NULL DEFAULT '{}'::jsonb,
            model            TEXT NOT NULL,
            extracted_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (client_id, product_id)
        );

        CREATE INDEX IF NOT EXISTS idx_product_ocr_summary_cache_url_set
            ON product_ocr_summary_cache (client_id, url_set_sha256);
    """)
