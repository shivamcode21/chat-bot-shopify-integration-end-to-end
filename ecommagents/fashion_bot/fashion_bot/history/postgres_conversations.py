import os
import json
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, Any, Dict, List
import logging

from fashion_bot.database_manager import (
    awith_retry,
    get_async_postgres_connection,
    is_connection_error,
)

_TABLES_ENSURED = False
logger = logging.getLogger(__name__)


def _ensure_tables_exist(conn) -> None:
    global _TABLES_ENSURED
    if _TABLES_ENSURED:
        return
    with conn.cursor() as cur:
        # Conversations: metadata only, one row per conversation_id
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id UUID PRIMARY KEY,
                client_id UUID NOT NULL,
                customer_id VARCHAR(255),
                channel_type VARCHAR(20) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'active',
                tags TEXT[],
                first_message TEXT NOT NULL,
                started_by VARCHAR(20) NOT NULL,
                phone VARCHAR(20),
                customer_info JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        # Ensure commonly used columns on conversations
        cur.execute(
            """
            ALTER TABLE conversations
            ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
            """
        )
        cur.execute(
            """
            ALTER TABLE conversations
            ADD COLUMN IF NOT EXISTS created_by VARCHAR(20) NOT NULL DEFAULT 'system';
            """
        )
        # New messages table to store every message event
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                message_id UUID PRIMARY KEY,
                client_id UUID NOT NULL,
                conversation_id UUID NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
                customer_id VARCHAR(255),
                channel_type VARCHAR(20) NOT NULL,
                tags TEXT[],
                message TEXT NOT NULL,
                message_side VARCHAR(20) NOT NULL,
                phone VARCHAR(20),
                customer_info JSONB,
                message_metadata JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        # Ensure updated_at and created_by exist on messages as well
        cur.execute(
            """
            ALTER TABLE messages
            ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
            """
        )
        cur.execute(
            """
            ALTER TABLE messages
            ADD COLUMN IF NOT EXISTS created_by VARCHAR(20) NOT NULL DEFAULT 'user';
            """
        )
        # Ensure langsmith_id column exists for tracing
        cur.execute(
            """
            ALTER TABLE messages
            ADD COLUMN IF NOT EXISTS langsmith_id VARCHAR(100);
            """
        )
        # Constraints for messages
        cur.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'ck_message_channel_type'
                ) THEN
                    ALTER TABLE messages
                    ADD CONSTRAINT ck_message_channel_type CHECK (channel_type IN ('whatsapp', 'instagram', 'email'));
                END IF;
            END$$;
            """
        )
        cur.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'ck_message_side'
                ) THEN
                    ALTER TABLE messages
                    ADD CONSTRAINT ck_message_side CHECK (message_side IN ('user_to_system', 'system_to_user'));
                END IF;
            END$$;
            """
        )
        # Helpful indexes for messages
        cur.execute("CREATE INDEX IF NOT EXISTS ix_message_conversation_created ON messages (conversation_id, created_at);")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_message_client_created ON messages (client_id, created_at);")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_message_customer_created ON messages (customer_id, created_at);")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_message_side_created ON messages (message_side, created_at);")
        # Helpful index for conversations lookup
        cur.execute("CREATE INDEX IF NOT EXISTS idx_conversations_client_phone_channel_status ON conversations (client_id, phone, channel_type, status);")
        conn.commit()
    _TABLES_ENSURED = True


async def _aensure_tables_exist(conn) -> None:
    global _TABLES_ENSURED
    if _TABLES_ENSURED:
        return
    async with conn.cursor() as cur:
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id UUID PRIMARY KEY,
                client_id UUID NOT NULL,
                customer_id VARCHAR(255),
                channel_type VARCHAR(20) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'active',
                tags TEXT[],
                first_message TEXT NOT NULL,
                started_by VARCHAR(20) NOT NULL,
                phone VARCHAR(20),
                customer_info JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        await cur.execute(
            """
            ALTER TABLE conversations
            ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
            """
        )
        await cur.execute(
            """
            ALTER TABLE conversations
            ADD COLUMN IF NOT EXISTS created_by VARCHAR(20) NOT NULL DEFAULT 'system';
            """
        )
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                message_id UUID PRIMARY KEY,
                client_id UUID NOT NULL,
                conversation_id UUID NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
                customer_id VARCHAR(255),
                channel_type VARCHAR(20) NOT NULL,
                tags TEXT[],
                message TEXT NOT NULL,
                message_side VARCHAR(20) NOT NULL,
                phone VARCHAR(20),
                customer_info JSONB,
                message_metadata JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        await cur.execute(
            """
            ALTER TABLE messages
            ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
            """
        )
        await cur.execute(
            """
            ALTER TABLE messages
            ADD COLUMN IF NOT EXISTS created_by VARCHAR(20) NOT NULL DEFAULT 'user';
            """
        )
        await cur.execute(
            """
            ALTER TABLE messages
            ADD COLUMN IF NOT EXISTS langsmith_id VARCHAR(100);
            """
        )
        await cur.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'ck_message_channel_type'
                ) THEN
                    ALTER TABLE messages
                    ADD CONSTRAINT ck_message_channel_type CHECK (channel_type IN ('whatsapp', 'instagram', 'email'));
                END IF;
            END$$;
            """
        )
        await cur.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'ck_message_side'
                ) THEN
                    ALTER TABLE messages
                    ADD CONSTRAINT ck_message_side CHECK (message_side IN ('user_to_system', 'system_to_user'));
                END IF;
            END$$;
            """
        )
        await cur.execute("CREATE INDEX IF NOT EXISTS ix_message_conversation_created ON messages (conversation_id, created_at);")
        await cur.execute("CREATE INDEX IF NOT EXISTS ix_message_client_created ON messages (client_id, created_at);")
        await cur.execute("CREATE INDEX IF NOT EXISTS ix_message_customer_created ON messages (customer_id, created_at);")
        await cur.execute("CREATE INDEX IF NOT EXISTS ix_message_side_created ON messages (message_side, created_at);")
        await cur.execute("CREATE INDEX IF NOT EXISTS idx_conversations_client_phone_channel_status ON conversations (client_id, phone, channel_type, status);")
    _TABLES_ENSURED = True


def _find_latest_active_conversation(conn, client_id: str, phone: str, channel_type: str) -> Optional[dict]:
    with conn.cursor() as cur:
        # Determine the most recent active conversation for this client/phone/channel by last message time
        cur.execute(
            """
            SELECT c.conversation_id, COALESCE(MAX(m.created_at), c.updated_at) AS last_ts
            FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.conversation_id
            WHERE c.client_id = %s AND c.phone = %s AND c.channel_type = %s AND c.status = 'active'
            GROUP BY c.conversation_id, c.updated_at
            ORDER BY last_ts DESC
            LIMIT 1;
            """,
            (client_id, phone, channel_type),
        )
        row = cur.fetchone()
        if not row:
            return None
        # Handle both dict rows and tuple rows
        if isinstance(row, dict):
            conversation_id = row.get('conversation_id')
            last_ts = row.get('last_ts')
        else:
            conversation_id, last_ts = row[0], row[1]
        return {"conversation_id": str(conversation_id), "last_activity": last_ts}


async def _afind_latest_active_conversation(conn, client_id: str, phone: str, channel_type: str) -> Optional[dict]:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT c.conversation_id, COALESCE(MAX(m.created_at), c.updated_at) AS last_ts
            FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.conversation_id
            WHERE c.client_id = %s AND c.phone = %s AND c.channel_type = %s AND c.status = 'active'
            GROUP BY c.conversation_id, c.updated_at
            ORDER BY last_ts DESC
            LIMIT 1;
            """,
            (client_id, phone, channel_type),
        )
        row = await cur.fetchone()
        if not row:
            return None
        if isinstance(row, dict):
            conversation_id = row.get('conversation_id')
            last_ts = row.get('last_ts')
        else:
            conversation_id, last_ts = row[0], row[1]
        return {"conversation_id": str(conversation_id), "last_activity": last_ts}


def _normalize_started_by(started_by: Optional[str], sender: str) -> str:
    val = (started_by or sender or "system").lower()
    if val in ("customer", "user", "agent"):
        return "user"
    return "system"


def _conversation_exists(conn, conversation_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM conversations WHERE conversation_id = %s LIMIT 1;", (conversation_id,))
        return cur.fetchone() is not None


async def _aconversation_exists(conn, conversation_id: str) -> bool:
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM conversations WHERE conversation_id = %s LIMIT 1;", (conversation_id,))
        return await cur.fetchone() is not None


def _map_message_side(sender: str) -> str:
    s = (sender or "").lower()
    if s in ("customer", "user"):
        return "user_to_system"
    return "system_to_user"


def _map_created_by(sender: str) -> str:
    s = (sender or "").lower()
    if s == "bot":
        return "bot"
    if s == "support":
        return "support"
    if s in ("customer", "user", "agent"):
        return "user"
    # default to user
    return "user"


@awith_retry
async def astore_message_event_with_conversation_resolution(
    *,
    client_id: str,
    phone: str,
    sender: str,
    text: str,
    channel_type: str,
    started_by: Optional[str] = None,
    tags: Optional[list[str]] = None,
    customer_id: Optional[str] = None,
    customer_info: Optional[Dict[str, Any]] = None,
    conversation_id: Optional[str] = None,
    langsmith_id: Optional[str] = None,
) -> str:
    """
    Async variant of store_message_event_with_conversation_resolution.
    """
    if not client_id or not phone or not channel_type:
        raise ValueError("client_id, phone, and channel_type are required")

    return await _astore_conversation_event_internal(
        client_id=client_id,
        phone=phone,
        sender=sender,
        text=text,
        channel_type=channel_type,
        started_by=started_by,
        tags=tags,
        customer_id=customer_id,
        customer_info=customer_info,
        conversation_id=conversation_id,
        langsmith_id=langsmith_id,
    )


async def astore_conversation_event(
    *,
    client_id: str,
    phone: str,
    sender: str,
    text: str,
    channel_type: str,
    started_by: Optional[str] = None,
    tags: Optional[list[str]] = None,
    customer_id: Optional[str] = None,
    customer_info: Optional[Dict[str, Any]] = None,
    conversation_id: Optional[str] = None,
    langsmith_id: Optional[str] = None,
) -> str:
    """
    Async backward-compatible alias with exponential backoff retry.
    """
    from fashion_bot.database_manager import async_db_execute_with_retry

    return await async_db_execute_with_retry(lambda: astore_message_event_with_conversation_resolution(
        client_id=client_id,
        phone=phone,
        sender=sender,
        text=text,
        channel_type=channel_type,
        started_by=started_by,
        tags=tags,
        customer_id=customer_id,
        customer_info=customer_info,
        conversation_id=conversation_id,
        langsmith_id=langsmith_id,
    ))


async def _astore_conversation_event_internal(
    *,
    client_id: str,
    phone: str,
    sender: str,
    text: str,
    channel_type: str,
    started_by: Optional[str] = None,
    tags: Optional[list[str]] = None,
    customer_id: Optional[str] = None,
    customer_info: Optional[Dict[str, Any]] = None,
    conversation_id: Optional[str] = None,
    langsmith_id: Optional[str] = None,
) -> str:
    """Async implementation of store_conversation_event."""
    now = datetime.now(timezone.utc)
    created_new_conversation = False
    async with get_async_postgres_connection() as conn:
        await _aensure_tables_exist(conn)
        conv_id: Optional[str] = None
        last_ts: Optional[datetime] = None

        if conversation_id and await _aconversation_exists(conn, conversation_id):
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT COALESCE(MAX(m.created_at), c.updated_at) AS last_ts
                    FROM conversations c
                    LEFT JOIN messages m ON m.conversation_id = c.conversation_id
                    WHERE c.conversation_id = %s
                    GROUP BY c.conversation_id, c.updated_at
                    """,
                    (conversation_id,),
                )
                row = await cur.fetchone()
                if row:
                    last_ts = row.get('last_ts') if isinstance(row, dict) else row[0]
                    if isinstance(last_ts, datetime) and last_ts.tzinfo is None:
                        last_ts = last_ts.replace(tzinfo=timezone.utc)

                    if last_ts and (now - last_ts) <= timedelta(minutes=90):
                        conv_id = conversation_id
                    else:
                        logger.debug(f"Conversation {conversation_id} expired (>90 min), creating new")
                        conv_id = None
                        last_ts = None
                else:
                    conv_id = None
                    last_ts = None

        if not conv_id:
            found = await _afind_latest_active_conversation(conn, client_id, phone, channel_type)
            if found:
                conv_id = found["conversation_id"]
                last_ts = found["last_activity"]
                if isinstance(last_ts, datetime) and last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=timezone.utc)

            create_new = False
            if not conv_id:
                create_new = True
            else:
                try:
                    if last_ts is None or (now - last_ts) > timedelta(minutes=90):
                        create_new = True
                except Exception:
                    create_new = False
            if create_new:
                conv_id = str(uuid.uuid4())
                created_new_conversation = True
                logger.info(f"[DB] New conversation {conv_id[:8]} for phone ***{str(phone)[-4:]}")
                created_by_conv = "system"
                started_by_value = _normalize_started_by(started_by, sender)
                async with conn.cursor() as cur:
                    sql_conv = (
                        """
                        INSERT INTO conversations (
                            conversation_id, client_id, customer_id, channel_type, status, tags, first_message,
                            started_by, phone, customer_info, created_at, updated_at, created_by
                        ) VALUES (
                            %s, %s, %s, %s, 'active', %s, %s, %s, %s, %s, %s, %s, %s
                        );
                        """
                    )
                    conv_params = (
                        conv_id,
                        client_id,
                        customer_id,
                        channel_type,
                        tags,
                        text,
                        started_by_value,
                        phone,
                        customer_info,
                        now,
                        now,
                        created_by_conv,
                    )
                    _t0 = time.monotonic()
                    await cur.execute(sql_conv, conv_params)
                    logger.info(f"[DB] INSERT conversation elapsed_ms={int((time.monotonic() - _t0) * 1000)}")

        async with conn.cursor() as cur:
            sql_msg = (
                """
                INSERT INTO messages (
                    message_id, client_id, conversation_id, customer_id, channel_type, tags, message, message_side,
                    phone, customer_info, message_metadata, created_at, updated_at, created_by, langsmith_id
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                );
                """
            )
            message_id = str(uuid.uuid4())
            message_side = _map_message_side(sender)
            created_by_msg = _map_created_by(sender)
            msg_params = (
                message_id,
                client_id,
                conv_id,
                customer_id,
                channel_type,
                tags,
                text,
                message_side,
                phone,
                customer_info,
                None,
                now,
                now,
                created_by_msg,
                langsmith_id,
            )
            _t0 = time.monotonic()
            await cur.execute(sql_msg, msg_params)
            await cur.execute(
                """
                UPDATE conversations SET updated_at = %s WHERE conversation_id = %s;
                """,
                (now, conv_id),
            )
            logger.info(f"[DB] INSERT message + UPDATE conv elapsed_ms={int((time.monotonic() - _t0) * 1000)} conv={conv_id[:8]} side={message_side}")

        # conversation_created_event disabled — only conversation_inactivity_event
        # is enqueued (via the inactivity scheduler) for now.
        # if created_new_conversation and channel_type == "whatsapp":
        #     from fashion_bot.workers.event_publishers import (
        #         fire_and_forget,
        #         publish_conversation_created_event,
        #     )
        #
        #     fire_and_forget(
        #         publish_conversation_created_event(
        #             {
        #                 "conversation_id": conv_id,
        #                 "client_id": client_id,
        #                 "phone": phone,
        #                 "channel_type": channel_type,
        #                 "first_message": text,
        #                 "created_at": now.isoformat(),
        #             }
        #         ),
        #         label="conversation_created_event",
        #     )

        return conv_id


@awith_retry
async def amigrate_webchat_guest_to_phone(
    client_id: str,
    session_id: str,
    new_phone: str,
    conversation_id: Optional[str] = None,
) -> Dict[str, int]:
    """
    After a web-chat guest submits their real phone, backfill `phone` and `customer_id`
    on existing `conversations` and `messages` rows that were stored under the guest
    identifier (session id), so dashboards show the number instead of "Guest ...".

    Prefer `conversation_id` from the current session when available (exact match).
    Otherwise match rows where `phone` equals the session id or its VARCHAR(20) prefix.
    """
    if not client_id or not session_id or not new_phone:
        raise ValueError("client_id, session_id, and new_phone are required")

    patch = json.dumps(
        {
            "is_identified": True,
            "source": "web-widget",
            "web_guest_session_id": session_id,
        }
    )

    guest_keys: List[str] = [session_id]
    if len(session_id) > 20:
        guest_keys.append(session_id[:20])

    stats = {"conversations_updated": 0, "messages_updated": 0, "escalations_updated": 0}

    async with get_async_postgres_connection() as conn:
        await _aensure_tables_exist(conn)
        async with conn.cursor() as cur:
            if conversation_id:
                try:
                    await cur.execute(
                        """
                        UPDATE conversations
                        SET phone = %s,
                            customer_id = %s,
                            updated_at = NOW(),
                            customer_info = COALESCE(customer_info, '{}'::jsonb) || %s::jsonb
                        WHERE conversation_id = %s::uuid
                          AND client_id = %s::uuid
                          AND channel_type = 'web-chat'
                        """,
                        (new_phone, new_phone, patch, conversation_id, client_id),
                    )
                    stats["conversations_updated"] = cur.rowcount

                    await cur.execute(
                        """
                        UPDATE messages
                        SET phone = %s,
                            customer_id = %s,
                            updated_at = NOW()
                        WHERE conversation_id = %s::uuid
                          AND client_id = %s::uuid
                          AND channel_type = 'web-chat'
                        """,
                        (new_phone, new_phone, conversation_id, client_id),
                    )
                    stats["messages_updated"] = cur.rowcount
                except Exception as e:
                    logger.warning(
                        "amigrate_webchat_guest_to_phone: conversation_id path failed (%s), falling back to guest keys",
                        e,
                    )

            if stats["conversations_updated"] == 0:
                in_ph = ",".join(["%s"] * len(guest_keys))
                await cur.execute(
                    f"""
                    UPDATE conversations
                    SET phone = %s,
                        customer_id = %s,
                        updated_at = NOW(),
                        customer_info = COALESCE(customer_info, '{{}}'::jsonb) || %s::jsonb
                    WHERE client_id = %s::uuid
                      AND channel_type = 'web-chat'
                      AND phone IN ({in_ph})
                    """,
                    (new_phone, new_phone, patch, client_id, *guest_keys),
                )
                stats["conversations_updated"] += cur.rowcount

            if stats["messages_updated"] == 0:
                in_ph = ",".join(["%s"] * len(guest_keys))
                await cur.execute(
                    f"""
                    UPDATE messages
                    SET phone = %s,
                        customer_id = %s,
                        updated_at = NOW()
                    WHERE client_id = %s::uuid
                      AND channel_type = 'web-chat'
                      AND phone IN ({in_ph})
                    """,
                    (new_phone, new_phone, client_id, *guest_keys),
                )
                stats["messages_updated"] += cur.rowcount

            try:
                in_ph = ",".join(["%s"] * len(guest_keys))
                await cur.execute(
                    f"""
                    UPDATE escalations
                    SET customer_phone = %s,
                        updated_at = NOW()
                    WHERE client_id = %s::uuid
                      AND customer_phone IN ({in_ph})
                    """,
                    (new_phone, client_id, *guest_keys),
                )
                stats["escalations_updated"] = cur.rowcount
            except Exception as esc_err:
                logger.warning(
                    "amigrate_webchat_guest_to_phone: escalation backfill failed (%s)",
                    esc_err,
                )
                stats["escalations_updated"] = 0

            await conn.commit()

    # The escalation snapshot is cached per identity, and this migration just
    # moved rows from the guest id to the phone. Both cache entries are now
    # wrong: the guest key still lists escalations that have moved away, and
    # the phone key may hold a long-lived "no escalations" answer that would
    # hide them. Bust both so the next turn reads the truth.
    try:
        from fashion_bot.utils.escalation_context import abust_escalation_snapshot

        await abust_escalation_snapshot(client_id, session_id)
        await abust_escalation_snapshot(client_id, new_phone)
    except Exception as _bust_err:  # best-effort; never fail the migration
        logger.debug("amigrate_webchat_guest_to_phone: escalation cache bust skipped: %s", _bust_err)

    logger.info(
        "amigrate_webchat_guest_to_phone: client=%s session=%s new_phone=%s stats=%s",
        client_id[:8] if client_id else "",
        session_id[:12] if session_id else "",
        new_phone,
        stats,
    )
    return stats


@awith_retry
async def aupdate_conversation_tags(conversation_id: str, tags: list[str]) -> bool:
    """Async variant of update_conversation_tags."""
    try:
        if not tags:
            return True
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT tags FROM conversations WHERE conversation_id = %s",
                    (conversation_id,)
                )
                row = await cur.fetchone()
                if not row:
                    logger.warning("Conversation %s not found for tag update", conversation_id)
                    return False
                existing_tags = row.get('tags') if isinstance(row, dict) else row[0]
                existing_tags = existing_tags if existing_tags else []
                all_tags = list(set(existing_tags + tags))
                _t0 = time.monotonic()
                await cur.execute(
                    """
                    UPDATE conversations
                    SET tags = %s, updated_at = NOW()
                    WHERE conversation_id = %s
                """,
                    (all_tags, conversation_id),
                )
                rows_affected = cur.rowcount
                await conn.commit()
                logger.info(
                    "[DB] UPDATE conv tags elapsed_ms=%d conv=%s tags=%d",
                    int((time.monotonic() - _t0) * 1000), conversation_id[:8], len(all_tags),
                )
                return rows_affected > 0
    except Exception as e:
        if is_connection_error(e):
            raise
        logger.error("Failed to update conversation tags for %s: %s", conversation_id, e)
        return False


@awith_retry
async def aupdate_message_tags(conversation_id: str, sender: str, tags: list[str]) -> bool:
    """Async variant of update_message_tags."""
    try:
        if not tags:
            return True
        message_side = _map_message_side(sender)
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT message_id, tags
                    FROM messages
                    WHERE conversation_id = %s AND message_side = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                """,
                    (conversation_id, message_side),
                )
                result = await cur.fetchone()
                if not result:
                    logger.warning("No %s message (side: %s) found for conversation %s", sender, message_side, conversation_id)
                    return False
                if isinstance(result, dict):
                    message_id = result.get('message_id')
                    existing_tags = result.get('tags')
                else:
                    message_id, existing_tags = result
                _t0 = time.monotonic()
                await cur.execute(
                    """
                    UPDATE messages
                    SET tags = %s, updated_at = NOW()
                    WHERE message_id = %s
                """,
                    (tags, message_id),
                )
                rows_affected = cur.rowcount
                await conn.commit()
                logger.info(
                    "[DB] UPDATE msg tags elapsed_ms=%d msg=%s tags=%s",
                    int((time.monotonic() - _t0) * 1000), str(message_id)[:8], tags,
                )
                return rows_affected > 0
    except Exception as e:
        if is_connection_error(e):
            raise
        logger.error("Failed to update message tags for conversation %s, sender %s: %s", conversation_id, sender, e)
        return False


@awith_retry
async def aupdate_message_tags_by_id(message_id: str, tags: list[str]) -> bool:
    """Merge tags into a specific message row."""
    try:
        if not tags:
            return True
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT tags FROM messages WHERE message_id = %s::uuid",
                    (message_id,),
                )
                row = await cur.fetchone()
                if not row:
                    logger.warning("Message %s not found for tag update", message_id)
                    return False
                existing_tags = row.get("tags") if isinstance(row, dict) else row[0]
                existing_tags = existing_tags if existing_tags else []
                merged_tags = list(set(existing_tags + tags))
                _t0 = time.monotonic()
                await cur.execute(
                    """
                    UPDATE messages
                    SET tags = %s, updated_at = NOW()
                    WHERE message_id = %s::uuid
                    """,
                    (merged_tags, message_id),
                )
                rows_affected = cur.rowcount
                await conn.commit()
                logger.info(
                    "[DB] UPDATE msg tags by id elapsed_ms=%d msg=%s tags=%s",
                    int((time.monotonic() - _t0) * 1000),
                    str(message_id)[:8],
                    merged_tags,
                )
                return rows_affected > 0
    except Exception as e:
        if is_connection_error(e):
            raise
        logger.error("Failed to update message tags for message %s: %s", message_id, e)
        return False


@awith_retry
async def aupdate_message_metadata(
    conversation_id: str, sender: str, metadata: dict
) -> bool:
    """Merge metadata into the message_metadata JSONB column of the latest message for a given conversation/sender."""
    try:
        if not metadata:
            return True
        message_side = _map_message_side(sender)
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT message_id, message_metadata
                    FROM messages
                    WHERE conversation_id = %s AND message_side = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (conversation_id, message_side),
                )
                result = await cur.fetchone()
                if not result:
                    logger.warning(
                        "No %s message (side: %s) found for conversation %s to update metadata",
                        sender, message_side, conversation_id,
                    )
                    return False
                if isinstance(result, dict):
                    message_id = result.get("message_id")
                    existing_metadata = result.get("message_metadata")
                else:
                    message_id, existing_metadata = result
                merged = existing_metadata or {}
                merged.update(metadata)
                _t0 = time.monotonic()
                await cur.execute(
                    """
                    UPDATE messages
                    SET message_metadata = %s::jsonb, updated_at = NOW()
                    WHERE message_id = %s
                    """,
                    (json.dumps(merged), message_id),
                )
                rows_affected = cur.rowcount
                await conn.commit()
                logger.info(
                    "[DB] UPDATE msg metadata elapsed_ms=%d msg=%s keys=%s",
                    int((time.monotonic() - _t0) * 1000),
                    str(message_id)[:8],
                    list(metadata.keys()),
                )
                return rows_affected > 0
    except Exception as e:
        if is_connection_error(e):
            raise
        logger.error(
            "Failed to update message metadata for conversation %s, sender %s: %s",
            conversation_id, sender, e,
        )
        return False


@awith_retry
async def aattach_media_to_message(
    conversation_id: str,
    sender: str,
    expected_text: str,
    new_text: str,
    metadata: dict,
) -> bool:
    """Backfill a stored-media link onto an already-inserted message row.

    Used when the media upload finishes after the transcript row was written
    (the agent-mode path acknowledges the webhook first, then uploads). The row
    is matched on conversation + side + the exact placeholder text we wrote, so
    a message that arrived in the meantime is never rewritten — if the text no
    longer matches, this is a no-op and the caller keeps the placeholder.

    Rows that already carry a media descriptor are excluded. Two media messages
    in one conversation share a placeholder ("[Video message]: Customer sent
    video"), so without that guard both backfills would land on the newest row
    and the older one would never get its link. The guard checks for the
    ``media`` key specifically rather than for NULL metadata, because the async
    tag generator also writes to this column.
    """
    if not conversation_id or not new_text:
        return False
    try:
        message_side = _map_message_side(sender)
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT message_id, message_metadata
                    FROM messages
                    WHERE conversation_id = %s AND message_side = %s AND message = %s
                      AND (message_metadata -> 'media') IS NULL
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (conversation_id, message_side, expected_text),
                )
                result = await cur.fetchone()
                if not result:
                    logger.warning(
                        "No matching %s message in conversation %s to attach media to",
                        sender, conversation_id,
                    )
                    return False
                if isinstance(result, dict):
                    message_id = result.get("message_id")
                    existing_metadata = result.get("message_metadata")
                else:
                    message_id, existing_metadata = result

                merged = existing_metadata or {}
                merged.update(metadata or {})
                _t0 = time.monotonic()
                await cur.execute(
                    """
                    UPDATE messages
                    SET message = %s, message_metadata = %s::jsonb, updated_at = NOW()
                    WHERE message_id = %s
                    """,
                    (new_text, json.dumps(merged), message_id),
                )
                rows_affected = cur.rowcount
                await conn.commit()
                logger.info(
                    "[DB] UPDATE msg media elapsed_ms=%d msg=%s",
                    int((time.monotonic() - _t0) * 1000),
                    str(message_id)[:8],
                )
                return rows_affected > 0
    except Exception as e:
        if is_connection_error(e):
            raise
        logger.error(
            "Failed to attach media to message for conversation %s: %s",
            conversation_id, e,
        )
        return False
