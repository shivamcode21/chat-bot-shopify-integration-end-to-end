#!/usr/bin/env python3
"""
Multi-session Interactive Bot
Demonstrates the state management capabilities of the new /chat endpoint
"""

import asyncio
import uuid

import httpx

API_URL = "http://localhost:8000/chat"


def print_banner():
    print("🤖 Multi-Session Interactive Bot")
    print("=" * 50)
    print("This bot demonstrates state management across multiple sessions")
    print("Each session maintains separate conversation context")
    print("=" * 50)


def print_help():
    print("\n📋 Available Commands:")
    print("-" * 30)
    print("help                    - Show this help")
    print("quit/exit               - Exit the bot")
    print("switch <session_id>     - Switch to a different session")
    print("reset                   - Reset current session")
    print("state                   - Show current session state")
    print("list                    - List all active sessions")
    print("new                     - Create a new session")
    print("\n💡 Example conversation flow:")
    print("-" * 30)
    print("1. 'Hello, I want to check my order'")
    print("2. 'My order ID is GV1234'")
    print("3. 'What's the delivery status?'")
    print("4. 'reset' (to start fresh)")
    print("5. 'switch session2' (to switch sessions)")


async def send_message(message, session_id):
    """Send a message to the API."""
    try:
        payload = {"message": message, "session_id": session_id}
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(API_URL, json=payload)

        if response.status_code == 200:
            data = response.json()
            return data.get("response", "No response"), data.get("session_id", session_id)
        return f"❌ API Error: {response.status_code} - {response.text}", session_id
    except httpx.ConnectError:
        return "❌ Could not connect to server. Make sure the FastAPI server is running on port 8000.", session_id
    except Exception as e:
        return f"❌ Error: {e}", session_id


async def reset_session(session_id):
    """Reset a session."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"http://localhost:8000/session/reset?session_id={session_id}")
        if response.status_code == 200:
            return f"🔄 Session {session_id} reset successfully!"
        return f"❌ Failed to reset session: {response.status_code}"
    except Exception as e:
        return f"❌ Error resetting session: {e}"


async def get_session_state(session_id):
    """Get session state."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"http://localhost:8000/session/{session_id}/state")
        if response.status_code == 200:
            state_data = response.json()
            return f"""📊 Session {session_id} State:
   Messages: {state_data.get('messages_count', 0)}
   Phone: {state_data.get('phone_number', 'None')}
   Order ID: {state_data.get('selected_order_id', 'None')}
   Frustrated: {state_data.get('is_frustrated', 'None')}
   Needs Escalation: {state_data.get('needs_escalation', 'None')}
   Trace ID: {state_data.get('trace_id', 'None')}"""
        return f"❌ Failed to get session state: {response.status_code}"
    except Exception as e:
        return f"❌ Error getting session state: {e}"


async def amain():
    print_banner()

    current_session = str(uuid.uuid4())[:8]
    sessions = {current_session: "Active"}

    print(f"📱 Current Session: {current_session}")
    print_help()

    while True:
        try:
            user_input = input(f"\n👤 [{current_session}] You: ").strip()

            if user_input.lower() in ["quit", "exit"]:
                print("👋 Goodbye!")
                break
            if user_input.lower() == "help":
                print_help()
                continue
            if user_input.lower() == "reset":
                print(await reset_session(current_session))
                continue
            if user_input.lower() == "state":
                print(await get_session_state(current_session))
                continue
            if user_input.lower() == "list":
                print("📋 Active Sessions:")
                for session_id, status in sessions.items():
                    marker = "🟢" if session_id == current_session else "⚪"
                    print(f"   {marker} {session_id}: {status}")
                continue
            if user_input.lower() == "new":
                new_session = str(uuid.uuid4())[:8]
                sessions[new_session] = "Active"
                current_session = new_session
                print(f"🆕 Created new session: {current_session}")
                continue
            if user_input.lower().startswith("switch "):
                session_id = user_input[7:].strip()
                if session_id in sessions:
                    current_session = session_id
                    print(f"🔄 Switched to session: {current_session}")
                else:
                    print(f"❌ Session {session_id} not found. Available sessions: {list(sessions.keys())}")
                continue

            if not user_input:
                continue

            response, response_session = await send_message(user_input, current_session)
            print(f"🤖 Bot: {response}")

            if response_session != current_session:
                sessions[response_session] = "Active"
                current_session = response_session
                print(f"📱 Switched to session: {current_session}")
        except KeyboardInterrupt:
            print("\n👋 Goodbye!")
            break


def main():
    asyncio.run(amain())


if __name__ == "__main__":
    main()
