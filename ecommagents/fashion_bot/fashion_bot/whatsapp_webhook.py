from fastapi import APIRouter, Request
import re
import os
import logging
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage

from fashion_bot.state_cache import aget_or_create_state, aupdate_state, get_cache_stats, cleanup_expired_states
from fashion_bot.utils.http_client import get_shared_async_http_client

#load_dotenv()

router = APIRouter()

INSTANCE_ID = os.getenv("ULTRAMSG_INSTANCE_ID")
TOKEN = os.getenv("ULTRAMSG_TOKEN")
ULTRAMSG_URL = f"https://api.ultramsg.com/{INSTANCE_ID}/messages/chat"

# In-memory session for user phone numbers
user_sessions = {}

async def send_message(to, message):
    payload = {
        "token": TOKEN,
        "to": to,
        "body": message
    }
    client = await get_shared_async_http_client()
    await client.post(ULTRAMSG_URL, data=payload, timeout=30)

# From test bot
async def call_main_bot(question, phone_number):
    """
    Call the main_meta.py graph directly with the user's question and phone number as trace_id.
    
    Args:
        question (str): The user's message/question
        phone_number (str): The user's phone number to use as trace_id
    
    Returns:
        str: The agent's response
    """
    try:
        # Get or create state using the state cache module
        # phone_number is the sender (from), INSTANCE_ID is the business identifier (to)
        state, is_new_state = await aget_or_create_state(phone_number, INSTANCE_ID, question, None)
        
        # Configure graph execution with phone number as thread_id
        config = {
            "configurable": {
                "thread_id": phone_number,  # Use phone number as thread_id for conversation persistence
                "checkpoint_ns": "whatsapp_support"
            }
        }
        
        # Invoke the graph (lazy import to avoid circular dependency)
        from fashion_bot.graph_context_meta import graph
        result = await graph.ainvoke(state, config=config)
        
        # Update the cached state with the result
        if result:
            await aupdate_state(phone_number, INSTANCE_ID, result)

        # Extract the final response from the result
        if "messages" in result and result["messages"]:
            final_message = result["messages"][-1]
            if hasattr(final_message, 'content'):
                return final_message.content
            else:
                return str(final_message)
        elif "customer_message" in result and result["customer_message"]:
            return result["customer_message"]
        else:
            return "I apologize, but I couldn't generate a response. Please try again."
            
    except Exception as e:
        logging.error(f"Error in call_main_bot: {str(e)}")
        return f"Sorry, there was an error processing your request: {str(e)}"

def extract_and_normalize_phone(msg):
    # Extract 10 digit number, with or without +91/91
    match = re.search(r"(\+91|91)?(\d{10})", msg)
    if match:
        number = match.group(2)
        return f"+91{number}"
    return None

def extract_and_normalize_order(msg):
    # Extract order number in form gvXXXX or #gvXXXX (case-insensitive)
    match = re.search(r"#?gv([a-zA-Z0-9]+)", msg, re.IGNORECASE)
    if match:
        return f"#gv{match.group(1)}"
    return None

def extract_10_digit_phone(phone):
    # Extracts the last 10 digits from a phone number string
    match = re.search(r'(\d{10})$', phone)
    return match.group(1) if match else phone

@router.post("/webhook")
async def webhook(request: Request):
    # Periodic cleanup of expired states (every 100th request approximately)
    import random
    if random.randint(1, 100) == 1:
        cleanup_expired_states()
    
    data = await request.json()
    print("Received:", data)

    # Handle different webhook formats
    message = ""
    from_number = ""
    
    if "data" in data and isinstance(data["data"], dict):
        # UltraMsg format
        inner = data["data"]
        message = inner.get("body", "").lower()
        from_number_full = inner.get("from", "")  # e.g., '91xxxxxxxxxx@c.us'
        from_number = from_number_full.split("@")[0]  # extract '91xxxxxxxxx'
    elif "message" in data:
        # Simple format for testing
        message = data["message"].lower()
        from_number = data.get("from", "919876543210")  # Default test number
    elif "body" in data:
        # Direct format
        message = data["body"].lower()
        from_number = data.get("from", "919876543210")
    else:
        return {"error": "Invalid webhook format. Expected 'data' field or 'message' field"}

    if not message:
        return {"error": "No message found in webhook data"}

    # number vs in memory state management 

    print(f"Customer ({from_number}) wrote: {message}")

    phone = extract_and_normalize_phone(message)
    order = extract_and_normalize_order(message)

    # Session management: store phone number if found
    if phone:
        user_sessions[from_number] = phone

    # Use stored phone if not present in message
    session_phone = user_sessions.get(from_number)

    # If no phone in message or session, use WhatsApp sender's number
    auto_phone = None
    if not phone and not session_phone:
        if len(from_number) == 12 and from_number.startswith('91'):
            auto_phone = f'{from_number}'
        elif len(from_number) == 10 and from_number.isdigit():
            auto_phone = f'{from_number}'
        if auto_phone:
            session_phone = auto_phone
            user_sessions[from_number] = auto_phone

    # Mediator variable for phone or order
    num_or_order = None
    if order:
        num_or_order = order
    elif phone:
        num_or_order = extract_10_digit_phone(phone)
    elif session_phone:
        num_or_order = extract_10_digit_phone(session_phone)

    # just for testing phone or order captured by the bot
    # feedback_msgs = []
    # if phone:
    #     feedback_msgs.append(f"Fetching Details for {phone}")
    # if order:
    #     feedback_msgs.append(f"Fetching your Order {order}")
    # if auto_phone:
    #     feedback_msgs.append(f"Got your phone number {auto_phone}")

    # for msg in feedback_msgs:
    #     send_message(from_number, msg)

    #Sending the same payload as in interactive_bot.py
    product = "Fashion Product"
    thread_id = "user-thread"
    if order:
        question = f"{message} {order}"
    elif phone and ('order' in message or 'status' in message or 'track' in message):
        # Only append phone for order-related queries
        question = f"{message} {phone}"
    else:
        question = message
    
    # Use the extracted phone number or session phone as trace_id
    final_phone = phone or session_phone or from_number
    
    payload = {
        "product": product,
        "question": question,
        "thread_id": thread_id,
        "phone_number": final_phone
    }
    print(f"Sending to main_meta graph: {payload}")
    reply = await call_main_bot(question, final_phone)
    await send_message(from_number, reply)

    return {"status": "ok"}

@router.get("/")
async def root():
    return {"status": "WhatsApp Webhook API is running", "message": "Use POST /webhook to receive messages"}

@router.get("/health")
async def health_check():
    cache_stats = get_cache_stats()
    return {
        "status": "healthy", 
        "service": "whatsapp-webhook",
        "cache_stats": cache_stats
    }

def your_bot_logic(msg, phone):
    if "order" in msg:
        return f"📦 Checking orders for {phone}..."
    return "Sorry, I didn't understand that. Try typing 'order' or 'help'."

def is_phone_number(msg):
    return re.match(r"^(\+91)?\d{10}$", msg.strip()) is not None
