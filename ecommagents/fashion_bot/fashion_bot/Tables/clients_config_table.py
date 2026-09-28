def create_client_configs_table(cursor):
    cursor.execute("""
       CREATE TABLE IF NOT EXISTS client_configs (
        id SERIAL PRIMARY KEY,
        client_id UUID REFERENCES clients(id) ON DELETE CASCADE,
        config_key TEXT NOT NULL,
        config_value JSONB NOT NULL,
        UNIQUE (client_id, config_key)
       );
       """)
   
