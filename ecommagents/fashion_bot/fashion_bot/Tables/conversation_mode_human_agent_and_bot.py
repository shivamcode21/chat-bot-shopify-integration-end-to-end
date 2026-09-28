def conversation_mode_human_agent_and_bot(cursor):
    cursor.execute("""
       CREATE TABLE IF NOT EXISTS conversation_mode_human_agent_and_bot (
        client_id VARCHAR(100) NOT NULL,
        phone_number VARCHAR(20) NOT NULL,
        mode VARCHAR(10) NOT NULL CHECK (mode IN ('bot', 'agent')),
        last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (client_id, phone_number)
       );
       """)