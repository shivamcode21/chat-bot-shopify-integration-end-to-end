#!/usr/bin/env python3
"""
Generate widget embed snippet.

Uses ``clientId`` in ``FashionBotWidgetConfig`` with the **opaque encoded**
tenant token (same string ``encode_client_id`` produces). The widget bundle
mirrors that into ``encodedClientId`` for WebSocket routing when appropriate.

Usage:
    python generate_widget_code.py --client-name "Groovee"
    python generate_widget_code.py --client-id "abc-123-uuid"
    python generate_widget_code.py --list-all
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fashion_bot"))

from fashion_bot.utils.client_id_utils import encode_client_id


def get_client_from_db(client_name=None, client_id=None):
    """Get client info from database."""
    from fashion_bot.env_loader import bootstrap_environment

    bootstrap_environment()

    from fashion_bot.database_manager import get_postgres_connection

    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            if client_id:
                cur.execute(
                    "SELECT id, name FROM clients WHERE id = %s",
                    (client_id,),
                )
            elif client_name:
                cur.execute(
                    "SELECT id, name FROM clients WHERE LOWER(name) = LOWER(%s)",
                    (client_name,),
                )
            else:
                return None

            result = cur.fetchone()
            if result:
                return {"client_id": result[0], "client_name": result[1]}
            return None


def get_all_clients():
    """Get all clients from database."""
    from fashion_bot.env_loader import bootstrap_environment

    bootstrap_environment()

    from fashion_bot.database_manager import get_postgres_connection

    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM clients ORDER BY name")
            results = cur.fetchall()
            return [{"client_id": r[0], "client_name": r[1]} for r in results]


def generate_widget_code(client_id, client_name, base_url):
    """Emit embed using ``clientId`` with encoded (opaque) value."""
    encoded = encode_client_id(client_id)
    client_id_js = json.dumps(encoded)
    client_name_js = json.dumps(str(client_name))

    return f"""<!-- Fashion Bot Widget — clientId is the opaque encoded tenant token -->
<script>
  window.FashionBotWidgetConfig = {{
    clientId: {client_id_js}
  }};
</script>
<script src="{base_url}/static/chat-widget.js" async defer></script>

<!-- With optional display name (local storage hints) -->
<script>
  window.FashionBotWidgetConfig = {{
    clientId: {client_id_js},
    clientName: {client_name_js},
    position: "bottom-right",
    theme: "light"
  }};
</script>
<script src="{base_url}/static/chat-widget.js" async defer></script>

<!-- Raw UUID (internal): {client_id} -->
"""


def main():
    parser = argparse.ArgumentParser(description="Generate widget embed (clientId = encoded token)")
    parser.add_argument("--client-name", "-n", help="Client name to look up")
    parser.add_argument("--client-id", "-i", help="Direct client UUID to encode into clientId")
    parser.add_argument("--base-url", "-b", default="https://your-domain.com", help="Base URL for widget scripts")
    parser.add_argument("--list-all", "-l", action="store_true", help="List all clients (UUID + encoded token)")

    args = parser.parse_args()

    if args.list_all:
        print("\n📋 All clients (UUID + opaque clientId value):\n")
        print(f"{'Client Name':<25} {'Client UUID':<40} {'clientId (encoded)':<36}")
        print("-" * 105)

        clients = get_all_clients()
        for client in clients:
            enc = encode_client_id(client["client_id"])
            print(f"{str(client['client_name']):<25} {str(client['client_id']):<40} {enc:<36}")

        print(f"\nTotal: {len(clients)} clients")
        return

    if args.client_name:
        print(f"\n🔍 Looking up client: {args.client_name}")
        client = get_client_from_db(client_name=args.client_name)
        if client:
            print(f"✅ Found: {client['client_name']} (ID: {client['client_id']})")
        else:
            print(f"❌ Client not found: {args.client_name}")
            return
    elif args.client_id:
        client = {"client_id": args.client_id, "client_name": "(direct)"}
        print(f"\n📝 Using client_id: {args.client_id}")
    else:
        print("❌ Please specify --client-name or --client-id")
        print("   Or use --list-all to see all clients")
        return

    print("\n" + "=" * 60)
    print("📋 Widget code (copy to your site):")
    print("=" * 60 + "\n")

    print(generate_widget_code(client["client_id"], client["client_name"], args.base_url))

    enc = encode_client_id(client["client_id"])
    enc_js = json.dumps(enc)
    print("\n" + "=" * 60)
    print("🔗 One-liner:")
    print("=" * 60)
    print(f"<script>window.FashionBotWidgetConfig={{clientId:{enc_js}}};</script>")
    print(f'<script src="{args.base_url}/static/chat-widget.js" async defer></script>')


if __name__ == "__main__":
    main()
