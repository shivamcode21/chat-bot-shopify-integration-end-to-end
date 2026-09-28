from fashion_bot.database_manager import get_postgres_connection

def get_client_uuid(client_slug: str):
    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT client_id FROM clients WHERE slug = %s", (client_slug,))
            row = cur.fetchone()
            if row:
                print(f"UUID for {client_slug}: {row[0]}")
            else:
                cur.execute("SELECT client_id, slug FROM clients")
                rows = cur.fetchall()
                print(f"Available clients: {rows}")

if __name__ == "__main__":
    import sys
    slug = sys.argv[1] if len(sys.argv) > 1 else "groovee"
    get_client_uuid(slug)
