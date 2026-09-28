def create_client_intents_table(cursor):
 cursor.execute("""
 CREATE TABLE IF NOT EXISTS client_intents (
 id SERIAL PRIMARY KEY,
 client_id UUID REFERENCES clients(id) ON DELETE CASCADE,
 intent_key TEXT NOT NULL,
 enabled BOOLEAN DEFAULT TRUE
 );
 """)