def create_client_order_formats_table(cursor):
  cursor.execute("""
  CREATE TABLE IF NOT EXISTS client_order_formats (
  id SERIAL PRIMARY KEY,
  client_id UUID REFERENCES clients(id) ON DELETE CASCADE,
  order_prefix TEXT,
  validation_regex TEXT,
  display_format TEXT
  );
  """)