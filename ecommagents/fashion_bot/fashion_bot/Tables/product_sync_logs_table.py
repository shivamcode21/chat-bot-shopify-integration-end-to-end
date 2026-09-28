def create_product_sync_logs_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS product_sync_logs (
            id SERIAL PRIMARY KEY,
            client_id UUID REFERENCES clients(id) ON DELETE CASCADE,
            sync_source TEXT NOT NULL,
            sync_type TEXT NOT NULL,
            status TEXT NOT NULL,
            products_added INT DEFAULT 0,
            products_updated INT DEFAULT 0,
            products_deleted INT DEFAULT 0,
            products_unchanged INT DEFAULT 0,
            products_failed INT DEFAULT 0,
            product_id TEXT,
            product_title TEXT,
            error_message TEXT,
            duration_seconds DECIMAL,
            trace_id TEXT,
            shopify_webhook_id TEXT,
            shopify_shop_domain TEXT,
            run_id TEXT,
            steps JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            completed_at TIMESTAMPTZ
        );

        CREATE INDEX IF NOT EXISTS idx_product_sync_logs_client_id
            ON product_sync_logs (client_id);
        CREATE INDEX IF NOT EXISTS idx_product_sync_logs_created_at
            ON product_sync_logs (created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_product_sync_logs_client_created
            ON product_sync_logs (client_id, created_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_product_sync_logs_run_id
            ON product_sync_logs (run_id)
            WHERE run_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_product_sync_logs_client_run
            ON product_sync_logs (client_id, run_id)
            WHERE run_id IS NOT NULL;
    """)
