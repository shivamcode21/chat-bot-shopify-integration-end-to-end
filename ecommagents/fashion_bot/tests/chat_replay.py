#!/usr/bin/env python3
"""
Real Chat Replay for Agent Testing.

Loads actual conversation histories from the Postgres `messages` table,
reconstructs them as state fixtures, and replays them through the graph
to verify:
  1. Were the right tools called for this real user interaction?
  2. Did the agent handle the conversation correctly?
  3. Are there regressions compared to what the agent did in production?

Usage:
    # Import and replay a specific conversation
    python -m tests.chat_replay --conversation-id <uuid>
    
    # Import recent conversations for a client and phone
    python -m tests.chat_replay --client-id <uuid> --phone 9876543210 --limit 5
    
    # Export a real conversation as a test fixture
    python -m tests.chat_replay --conversation-id <uuid> --export-fixture
    
    # Replay and verify tool calls
    python -m tests.chat_replay --conversation-id <uuid> --verify-tools
"""
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import AIMessage, HumanMessage

logger = logging.getLogger("chat_replay")

# Path setup
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.env_utils import setup_environment
setup_environment()


# ==================== DB LOADERS ====================

def fetch_conversation_messages(
    conversation_id: str,
) -> List[Dict[str, Any]]:
    """
    Fetch all messages for a conversation from Postgres.
    
    Returns list of message dicts ordered by created_at, each with:
    - message_id, message, message_side, created_at, created_by, langsmith_id
    """
    from fashion_bot.database_manager import get_postgres_connection

    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    message_id::text,
                    message,
                    message_side,
                    phone,
                    created_at,
                    created_by,
                    langsmith_id,
                    message_metadata
                FROM messages
                WHERE conversation_id = %s
                ORDER BY created_at ASC
                """,
                (conversation_id,),
            )
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()

    messages = [dict(zip(columns, row)) for row in rows]
    logger.info(f"📂 Fetched {len(messages)} messages for conversation {conversation_id}")
    return messages


def fetch_recent_conversations(
    client_id: str,
    phone: str = None,
    limit: int = 10,
    channel_type: str = None,
) -> List[Dict[str, Any]]:
    """
    Fetch recent conversations from Postgres for a client (optionally filtered by phone).
    
    Returns list of conversation metadata dicts.
    """
    from fashion_bot.database_manager import get_postgres_connection

    query = """
        SELECT
            c.conversation_id::text,
            c.phone,
            c.channel_type,
            c.status,
            c.first_message,
            c.started_by,
            c.created_at,
            c.updated_at,
            (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.conversation_id) as message_count
        FROM conversations c
        WHERE c.client_id = %s
    """
    params = [client_id]

    if phone:
        query += " AND c.phone = %s"
        params.append(phone)

    if channel_type:
        query += " AND c.channel_type = %s"
        params.append(channel_type)

    query += " ORDER BY c.updated_at DESC LIMIT %s"
    params.append(limit)

    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()

    conversations = [dict(zip(columns, row)) for row in rows]
    logger.info(
        f"📂 Found {len(conversations)} conversations for client {client_id}"
        + (f", phone {phone}" if phone else "")
    )
    return conversations


# ==================== CONVERSATION RECONSTRUCTION ====================

def reconstruct_state_from_messages(
    messages: List[Dict[str, Any]],
    client_id: str,
    phone: str = None,
) -> Dict[str, Any]:
    """
    Reconstruct a SupportState-compatible dict from raw DB messages.
    
    This creates a state that can be directly used with build_test_state
    or fed to the graph for replay.
    
    Args:
        messages: List of message dicts from fetch_conversation_messages()
        client_id: Client UUID
        phone: Customer phone (extracted from messages if not provided)
        
    Returns:
        State dict compatible with SupportState
    """
    langchain_messages = []
    extracted_phone = phone

    for msg in messages:
        side = msg.get("message_side", "").lower()
        content = msg.get("message", "")

        if side in ("customer", "user", "inbound"):
            langchain_messages.append(HumanMessage(content=content))
            # Extract phone from message metadata if not set
            if not extracted_phone and msg.get("phone"):
                extracted_phone = msg["phone"]
        elif side in ("bot", "agent", "support", "outbound"):
            langchain_messages.append(AIMessage(content=content))

    state = {
        "messages": langchain_messages,
        "phone_number": extracted_phone,
        "client_id": client_id,
        "selected_order_id": None,
        "known_orders": None,
        "page_context": None,
        "conversation_context": None,
        "is_frustrated": False,
        "needs_escalation": False,
        "waiting_for_cancellation_reason": False,
        "waiting_for_order_confirmation": False,
    }

    return state


def split_conversation_into_replay_turns(
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Split a real conversation into replay turns.
    
    Groups messages into (customer_message, bot_response) pairs for replay.
    This handles cases where there are multiple consecutive bot messages
    (they get concatenated) or consecutive customer messages.
    
    Returns list of turn dicts:
    [
        {
            "turn": 1,
            "customer_message": "I want to cancel my order",
            "actual_bot_response": "I found your order...",
            "customer_timestamp": "2026-01-15T10:00:00Z",
            "bot_timestamp": "2026-01-15T10:00:05Z",
        },
        ...
    ]
    """
    turns = []
    current_customer_msg = None
    current_customer_ts = None
    current_bot_msgs = []
    current_bot_ts = None

    for msg in messages:
        side = msg.get("message_side", "").lower()
        content = msg.get("message", "")
        ts = msg.get("created_at")

        if side in ("customer", "user", "inbound"):
            # If we have a pending customer+bot pair, save it
            if current_customer_msg and current_bot_msgs:
                turns.append({
                    "turn": len(turns) + 1,
                    "customer_message": current_customer_msg,
                    "actual_bot_response": "\n".join(current_bot_msgs),
                    "customer_timestamp": str(current_customer_ts) if current_customer_ts else None,
                    "bot_timestamp": str(current_bot_ts) if current_bot_ts else None,
                })
                current_bot_msgs = []

            current_customer_msg = content
            current_customer_ts = ts

        elif side in ("bot", "agent", "support", "outbound"):
            current_bot_msgs.append(content)
            current_bot_ts = ts

    # Save last pair
    if current_customer_msg and current_bot_msgs:
        turns.append({
            "turn": len(turns) + 1,
            "customer_message": current_customer_msg,
            "actual_bot_response": "\n".join(current_bot_msgs),
            "customer_timestamp": str(current_customer_ts) if current_customer_ts else None,
            "bot_timestamp": str(current_bot_ts) if current_bot_ts else None,
        })
    elif current_customer_msg:
        # Last message was from customer with no bot response
        turns.append({
            "turn": len(turns) + 1,
            "customer_message": current_customer_msg,
            "actual_bot_response": None,
            "customer_timestamp": str(current_customer_ts) if current_customer_ts else None,
            "bot_timestamp": None,
        })

    return turns


# ==================== EXPORT AS FIXTURE ====================

def export_conversation_as_fixture(
    conversation_id: str,
    client_id: str,
    fixture_name: str = None,
    output_dir: str = None,
) -> str:
    """
    Export a real conversation from DB as a reusable test fixture JSON.
    
    This creates:
    1. A state fixture (tests/fixtures/states/replay_<conv_id>.json)
    2. A scenario file (tests/fixtures/replays/<conv_id>.json)
    
    Args:
        conversation_id: UUID of the conversation
        client_id: Client UUID
        fixture_name: Custom name for the fixture (default: replay_<short_id>)
        output_dir: Directory to save fixtures (default: tests/fixtures)
        
    Returns:
        Path to the saved scenario file
    """
    messages = fetch_conversation_messages(conversation_id)
    if not messages:
        raise ValueError(f"No messages found for conversation {conversation_id}")

    phone = messages[0].get("phone")
    turns = split_conversation_into_replay_turns(messages)

    short_id = conversation_id[:8]
    name = fixture_name or f"replay_{short_id}"

    if output_dir is None:
        output_dir = os.path.join(os.path.dirname(__file__), "fixtures")

    # 1. State fixture (just the initial state before first customer message)
    states_dir = os.path.join(output_dir, "states")
    os.makedirs(states_dir, exist_ok=True)

    state_fixture = {
        "fixture_id": name,
        "description": f"Real conversation replay from {conversation_id}",
        "source": {
            "conversation_id": conversation_id,
            "exported_at": datetime.now().isoformat(),
            "total_messages": len(messages),
            "total_turns": len(turns),
        },
        "state": {
            "messages": [],  # Start fresh — the replay will send messages
            "phone_number": phone,
            "client_id": "{{client_id}}",
            "selected_order_id": None,
            "known_orders": None,
            "page_context": None,
            "conversation_context": None,
            "is_frustrated": False,
            "needs_escalation": False,
        },
    }

    state_path = os.path.join(states_dir, f"{name}.json")
    with open(state_path, "w") as f:
        json.dump(state_fixture, f, indent=4, default=str)

    # 2. Replay scenario
    replays_dir = os.path.join(output_dir, "replays")
    os.makedirs(replays_dir, exist_ok=True)

    # Build conversation turns for the scenario
    scenario_conversation = []
    for turn in turns:
        scenario_conversation.append({
            "role": "customer",
            "message": turn["customer_message"],
        })
        if turn["actual_bot_response"]:
            scenario_conversation.append({
                "role": "bot",
                "actual_production_response": turn["actual_bot_response"],
                "expected_behavior": "Should match or improve on the production response",
                "expected_keywords": [],  # To be filled by user or auto-generated
            })

    scenario = {
        "id": f"replay-{short_id}",
        "description": f"Replay of real conversation {conversation_id}",
        "source_conversation_id": conversation_id,
        "client_name": "casence",  # Will be overridden at runtime
        "initial_state_fixture": name,
        "target_agent": "auto",
        "context_assertions": {
            "expected_behaviors": [
                "Should handle the conversation as well as or better than production",
            ],
        },
        "conversation": scenario_conversation,
        "replay_metadata": {
            "phone": phone,
            "total_turns": len(turns),
            "first_message_at": str(messages[0].get("created_at")),
            "last_message_at": str(messages[-1].get("created_at")),
        },
    }

    scenario_path = os.path.join(replays_dir, f"{name}.json")
    with open(scenario_path, "w") as f:
        json.dump(scenario, f, indent=4, default=str)

    logger.info(f"📁 Exported fixture: {state_path}")
    logger.info(f"📁 Exported scenario: {scenario_path}")

    return scenario_path


# ==================== REPLAY WITH TOOL VERIFICATION ====================

async def replay_conversation_with_verification(
    conversation_id: str,
    client_id: str,
    use_mock: bool = True,
    client_name: str = "casence",
) -> Dict[str, Any]:
    """
    Replay a real conversation through the graph and verify tool calls.
    
    This is the main entry point for real chat testing:
    1. Fetches the real conversation from DB
    2. Replays each customer message through the graph (with mocked tools)
    3. Captures which tools were called for each turn
    4. Compares new bot response with production response
    5. Returns detailed results
    
    Args:
        conversation_id: UUID of the conversation to replay
        client_id: Client UUID
        use_mock: If True, mock external API calls
        client_name: Client profile name for fixture loading
        
    Returns:
        Dict with replay results including tool calls, response comparisons, etc.
    """
    from tests.tool_mocker import (
        mock_tools_for_agent,
        get_tool_call_log,
        get_tool_call_summary,
        reset_tool_call_log,
    )

    messages = fetch_conversation_messages(conversation_id)
    if not messages:
        return {"error": f"No messages found for conversation {conversation_id}"}

    phone = messages[0].get("phone")
    turns = split_conversation_into_replay_turns(messages)

    logger.info(
        f"\n🔄 Replaying conversation {conversation_id} "
        f"({len(turns)} turns, phone={phone})"
    )

    # Build initial state
    state = reconstruct_state_from_messages([], client_id, phone)

    # Import graph
    from fashion_bot.graph_context_meta import graph

    # Set client context
    try:
        from fashion_bot.client_context import set_client_id
        set_client_id(client_id)
    except ImportError:
        pass

    # Patch tool registry at the USAGE SITE (generic_skill_node)
    # Direct imports mean we must patch where the function is referenced, not where it's defined
    original_get_tools = None
    if use_mock:
        from fashion_bot.core import tool_registry
        from fashion_bot.nodes import generic_skill_node

        original_get_tools = generic_skill_node.get_tools_for_agent

        def patched_get_tools(agent_name, st, messages_list, cid, **extra):
            tools = original_get_tools(agent_name, st, messages_list, cid, **extra)
            return mock_tools_for_agent(tools, state=st, use_mock=True, scenario_id=f"replay-{conversation_id[:8]}")

        generic_skill_node.get_tools_for_agent = patched_get_tools
        tool_registry.get_tools_for_agent = patched_get_tools

    replay_results = []

    try:
        for turn in turns:
            reset_tool_call_log()  # Fresh log per turn

            customer_msg = turn["customer_message"]
            actual_bot_response = turn["actual_bot_response"]

            # Add customer message to state
            state["messages"] = state.get("messages", []) + [
                HumanMessage(content=customer_msg)
            ]

            # Invoke graph
            try:
                thread_id = f"replay-{conversation_id[:8]}-{turn['turn']}"
                config = {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": "chat_replay",
                    }
                }

                result = graph.invoke(state, config=config)

            except Exception as e:
                logger.error(f"Graph invoke error at turn {turn['turn']}: {e}")
                replay_results.append({
                    "turn": turn["turn"],
                    "customer_message": customer_msg,
                    "actual_bot_response": actual_bot_response,
                    "replay_bot_response": f"[ERROR: {e}]",
                    "tool_calls": [],
                    "error": str(e),
                })
                continue

            # Extract replay bot response
            replay_response = ""
            if "messages" in result and result["messages"]:
                last_msg = result["messages"][-1]
                replay_response = (
                    last_msg.content if hasattr(last_msg, "content") else str(last_msg)
                )
            elif result.get("customer_message"):
                replay_response = result["customer_message"]

            # Capture tool calls for this turn
            turn_tool_calls = get_tool_call_log()
            turn_summary = get_tool_call_summary()

            # Update running state
            state = result

            replay_results.append({
                "turn": turn["turn"],
                "customer_message": customer_msg,
                "actual_bot_response": actual_bot_response,
                "replay_bot_response": replay_response,
                "tool_calls": turn_tool_calls,
                "tool_summary": turn_summary,
                "customer_timestamp": turn.get("customer_timestamp"),
                "bot_timestamp": turn.get("bot_timestamp"),
            })

    finally:
        # Restore original tool registry
        if use_mock and original_get_tools is not None:
            from fashion_bot.core import tool_registry
            from fashion_bot.nodes import generic_skill_node
            generic_skill_node.get_tools_for_agent = original_get_tools
            tool_registry.get_tools_for_agent = original_get_tools

    return {
        "conversation_id": conversation_id,
        "client_id": client_id,
        "phone": phone,
        "total_turns": len(turns),
        "replay_results": replay_results,
        "all_tools_called": [
            tool
            for r in replay_results
            for tool in r.get("tool_summary", {}).get("tools_called", [])
        ],
    }


# ==================== CLI ====================

async def _main():
    """CLI entry point for chat replay."""
    import argparse

    parser = argparse.ArgumentParser(description="Real Chat Replay for Agent Testing")
    parser.add_argument(
        "--conversation-id", "-c", help="UUID of the conversation to replay"
    )
    parser.add_argument("--client-id", help="Client UUID")
    parser.add_argument("--phone", "-p", help="Phone number to filter conversations")
    parser.add_argument(
        "--limit", "-l", type=int, default=10, help="Max conversations to list"
    )
    parser.add_argument(
        "--export-fixture", action="store_true", help="Export as test fixture"
    )
    parser.add_argument(
        "--verify-tools", action="store_true", help="Replay and verify tool calls"
    )
    parser.add_argument(
        "--list", action="store_true", help="List recent conversations"
    )
    parser.add_argument(
        "--use-real-apis",
        action="store_true",
        help="Use real APIs instead of mocks (default: mock)",
    )

    args = parser.parse_args()

    if args.list:
        if not args.client_id:
            print("❌ --client-id required with --list")
            sys.exit(1)

        conversations = fetch_recent_conversations(
            args.client_id, phone=args.phone, limit=args.limit
        )

        print(f"\n📋 Recent conversations for client {args.client_id}:")
        for conv in conversations:
            print(
                f"   • {conv['conversation_id']} | "
                f"Phone: {conv.get('phone', 'N/A')} | "
                f"Messages: {conv.get('message_count', 0)} | "
                f"Channel: {conv.get('channel_type', 'N/A')} | "
                f"Status: {conv.get('status', 'N/A')} | "
                f"Updated: {conv.get('updated_at', 'N/A')}"
            )
            print(f"     First: {conv.get('first_message', '')[:80]}")
        return

    if not args.conversation_id:
        print("❌ --conversation-id required for replay or export")
        sys.exit(1)

    client_id = args.client_id or "81e80e20-fe91-470a-ab3d-e9dfc2eebf4a"

    if args.export_fixture:
        path = export_conversation_as_fixture(args.conversation_id, client_id)
        print(f"\n✅ Exported to: {path}")
        return

    if args.verify_tools:
        result = await replay_conversation_with_verification(
            args.conversation_id,
            client_id,
            use_mock=not args.use_real_apis,
        )

        print(f"\n🔄 Replay Results for {args.conversation_id}")
        print("=" * 70)
        for r in result.get("replay_results", []):
            print(f"\n  Turn {r['turn']}:")
            print(f"    Customer: {r['customer_message'][:100]}")
            print(f"    Actual:   {(r.get('actual_bot_response') or 'N/A')[:100]}")
            print(f"    Replay:   {r['replay_bot_response'][:100]}")

            tool_summary = r.get("tool_summary", {})
            if tool_summary.get("tools_called"):
                print(f"    Tools:    {tool_summary['tool_sequence']}")
            else:
                print(f"    Tools:    (none)")

        print(f"\n  All tools called across turns: {result.get('all_tools_called', [])}")
        return

    # Default: just show the conversation
    messages = fetch_conversation_messages(args.conversation_id)
    turns = split_conversation_into_replay_turns(messages)

    print(f"\n📋 Conversation {args.conversation_id} ({len(turns)} turns)")
    print("=" * 70)
    for turn in turns:
        print(f"\n  Turn {turn['turn']}:")
        print(f"    Customer: {turn['customer_message'][:200]}")
        if turn["actual_bot_response"]:
            print(f"    Bot:      {turn['actual_bot_response'][:200]}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())

