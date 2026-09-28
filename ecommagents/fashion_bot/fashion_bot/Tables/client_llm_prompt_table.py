def create_client_llm_prompts_table(cursor):
   cursor.execute("""
   CREATE TABLE IF NOT EXISTS client_llm_prompts (
   id SERIAL PRIMARY KEY,
   client_id UUID REFERENCES clients(id) ON DELETE CASCADE,
   prompt_type TEXT NOT NULL,
   content TEXT NOT NULL
   );
   """)