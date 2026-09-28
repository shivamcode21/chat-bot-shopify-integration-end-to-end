import asyncio
import re
import os
from dotenv import load_dotenv
from fastapi import APIRouter, Request
from fashion_bot.utils.http_client import get_shared_async_http_client

#load_dotenv()

router = APIRouter()

# WhatsApp configuration
INSTANCE_ID = os.getenv("ULTRAMSG_INSTANCE_ID")
TOKEN = os.getenv("ULTRAMSG_TOKEN")
ULTRAMSG_URL = f"https://api.ultramsg.com/{INSTANCE_ID}/messages/chat" if INSTANCE_ID else None

# In-memory session for user phone numbers
user_sessions = {}

async def send_message(to, message):
    """Send message via WhatsApp UltraMsg API"""
    if not ULTRAMSG_URL or not TOKEN:
        print(f"⚠️ WhatsApp not configured. Would send to {to}: {message}")
        return

    payload = {
        "token": TOKEN,
        "to": to,
        "body": message
    }
    try:
        client = await get_shared_async_http_client()
        response = await client.post(ULTRAMSG_URL, data=payload, timeout=30)
        print(f"📱 WhatsApp response: {response.status_code}")
    except Exception as e:
        print(f"❌ WhatsApp error: {e}")

def extract_and_normalize_phone(msg):
    """Extract and normalize phone number from message"""
    match = re.search(r"(\+91|91)?(\d{10})", msg)
    if match:
        number = match.group(2)
        return f"+91{number}"
    return None

def extract_and_normalize_order(msg):
    """Extract order number from message"""
    match = re.search(r"#?gv([a-zA-Z0-9]+)", msg, re.IGNORECASE)
    if match:
        return f"#gv{match.group(1)}"
    return None

def extract_10_digit_phone(phone):
    """Extract last 10 digits from phone number"""
    match = re.search(r'(\d{10})$', phone)
    return match.group(1) if match else phone

@router.post("/webhook")
async def webhook(request: Request, process_message_func):
    """WhatsApp webhook endpoint"""
    try:
        data = await request.json()
        print("📥 Received webhook:", data)

        if not data or "data" not in data:
            return {"error": "Invalid or missing JSON"}

        inner = data["data"]
        message = inner.get("body", "").lower()
        from_number_full = inner.get("from", "")
        from_number = from_number_full.split("@")[0]

        print(f"📱 Customer ({from_number}) wrote: {message}")

        # Extract phone and order
        phone = extract_and_normalize_phone(message)
        order = extract_and_normalize_order(message)

        # Session management
        if phone:
            user_sessions[from_number] = phone

        session_phone = user_sessions.get(from_number)

        # Auto-detect phone if not found
        auto_phone = None
        if not phone and not session_phone:
            if len(from_number) == 12 and from_number.startswith('91'):
                auto_phone = f'{from_number}'
            elif len(from_number) == 10 and from_number.isdigit():
                auto_phone = f'{from_number}'
            if auto_phone:
                session_phone = auto_phone
                user_sessions[from_number] = auto_phone

        # Prepare question for bot
        if order:
            question = f"{message} {order}"
        elif phone and ('order' in message or 'status' in message or 'track' in message):
            question = f"{message} {phone}"
        else:
            question = message

        print(f"🤖 Processing: {question}")

        # Get bot response using the provided function
        reply = process_message_func(question)
        if asyncio.iscoroutine(reply):
            reply = await reply

        # Send response back to WhatsApp
        await send_message(from_number, reply)

        return {"status": "ok", "response": reply}

    except Exception as e:
        print(f"❌ Webhook error: {e}")
        return {"error": str(e)}

@router.get("/webhook/health")
async def webhook_health():
    """WhatsApp webhook health check"""
    return {"status": "ok", "service": "whatsapp-webhook"} 
