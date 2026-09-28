#!/usr/bin/env python3
"""
Seed / update the conversation_analytics prompt in agents_config.

Usage:
    python3 scripts/seed_conversation_analytics_prompt.py --client-id <UUID>

Or set CLIENT_ID in .env.

The prompt is imported from the canonical source at
fashion_bot/prompts/conversation_analytics_prompt.py.

Safe to re-run (uses ON CONFLICT DO UPDATE).
"""

import argparse
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()

# ── Resolve project root so fashion_bot is importable ────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _load_prompt() -> str:
    """Import the canonical prompt template from the prompts module."""
    import importlib.util

    prompt_file = os.path.join(
        PROJECT_ROOT, "fashion_bot", "prompts", "conversation_analytics_prompt.py"
    )
    spec = importlib.util.spec_from_file_location("ca_prompt", prompt_file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.USER_PROMPT_TEMPLATE


def upsert_prompt(client_id: str, prompt_text: str) -> None:
    """Insert or update the conversation_analytics prompt in agents_config."""
    import psycopg

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL not set in environment or .env")
        sys.exit(1)

    sql = """
        INSERT INTO agents_config
            (client_id, agent_name, agent_prompt, when_to_route, when_not_to_route, created_by)
        VALUES
            (%s::uuid, %s, %s, %s, %s, %s)
        ON CONFLICT (client_id, agent_name)
        DO UPDATE SET
            agent_prompt = EXCLUDED.agent_prompt,
            updated_at   = NOW()
    """

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (
                client_id,
                "conversation_analytics",
                prompt_text,
                "Background analytics job — not routed by intent",
                "N/A",
                "seed_script",
            ))
        conn.commit()

    print(f"Upserted conversation_analytics prompt for client {client_id} "
          f"({len(prompt_text)} chars)")


def invalidate_cache(client_id: str) -> None:
    """Delete the Redis agents_config cache so the new prompt is picked up."""
    try:
        import redis as _redis
        import certifi

        redis_url = (
            os.getenv("REDIS_URL")
            or os.getenv("REDIS_CONNECTION_STRING")
            or "redis://localhost:6379/0"
        )
        kwargs = {"decode_responses": True}
        if redis_url.lower().startswith("rediss://"):
            kwargs["ssl_ca_certs"] = certifi.where()

        client = _redis.Redis.from_url(redis_url, **kwargs)
        deleted = client.delete(f"agents_config:{client_id}")
        print(f"Redis cache invalidated for client {client_id} (keys deleted: {deleted})")
    except Exception as exc:
        print(f"Warning: could not invalidate Redis cache: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed conversation_analytics prompt into agents_config"
    )
    parser.add_argument(
        "--client-id",
        default=os.getenv("CLIENT_ID", ""),
        help="Client UUID (or set CLIENT_ID env var)",
    )
    args = parser.parse_args()

    client_id = args.client_id.strip()
    if not client_id:
        print("ERROR: Provide --client-id <UUID> or set CLIENT_ID in .env")
        sys.exit(1)

    prompt_text = _load_prompt()
    print(f"Loaded prompt ({len(prompt_text)} chars)")

    upsert_prompt(client_id, prompt_text)
    invalidate_cache(client_id)

    # Verify
    print("\n--- Verification ---")
    print(f"  agent_name:  conversation_analytics")
    print(f"  client_id:   {client_id}")
    print(f"  prompt size: {len(prompt_text)} chars")
    print("Done.")


if __name__ == "__main__":
    main()
