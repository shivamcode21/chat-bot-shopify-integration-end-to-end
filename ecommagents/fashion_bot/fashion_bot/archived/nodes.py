import re
from langchain_core.messages import AIMessage
from langchain_core.prompts import PromptTemplate

from fashion_bot.product_data import PRODUCT_DATA
from fashion_bot.order_api import get_orders_by_phone, get_order_status
from fashion_bot.llm_config import llm
from fashion_bot.order_manager import normalize_order_id, get_shopify_order, get_shopify_customer_orders_by_phone, \
    get_shiprocket_token, get_shiprocket_order_details, get_shiprocket_order_id_from_awb, get_current_location_api, \
    classify_status, get_order_status_summary


# 🏗️ Fetch product details
def fetch_product(_):
    sizes = ", ".join(PRODUCT_DATA["sizes_available"])
    colors = ", ".join(PRODUCT_DATA["colors_available"])
    stock = "Yes" if PRODUCT_DATA["in_stock"] else "No"
    return {
        "product_info": f"Sizes: {sizes}. Colors: {colors}. In stock: {stock}."
    }


# 🧠 Build an empathetic LLM prompt
def build_smart_prompt(state) -> str:
    history = "\n".join([
        f"User: {m.content}" if m.type == "human" else f"Bot: {m.content}"
        for m in state.get("messages", [])[-5:]
    ])

    order_info = ""
    if state.get("selected_order_id"):
        order_id = state["selected_order_id"]
        status = state.get("order_status_by_id", {}).get(order_id, "Unknown")
        order_info = f"Order {order_id} is {status}."

    return f"""
You are an intelligent and empathetic support assistant for an online fashion store.

Responsibilities:
- Answer product availability, sizing, fabric, customization, discounts, delivery, and order-related queries.
- Detect customer frustration and respond with empathy.
- If a shipment is delayed 5+ days, apologize sincerely and inform the user you are escalating it.
- If a user requests a delayed delivery (custom order), acknowledge and inform the operations team will help.
- If a customer expresses anger like "you are fooling me" or "you are not genuine", respond with politeness, empathy, and reassurance.

Product Info:
{state.get("product_info", "Product info not available.")}

{order_info}

Conversation History:
{history}

Respond like a professional human support agent: empathetic, helpful, and conversational.
"""

def detect_frustration_llm(state):
    history = "\n".join([m.content for m in state.get("messages", [])[-5:]])

    prompt_text = f"""
You are an AI support agent for an online fashion store.

Check if the customer sounds frustrated, angry, upset, or disappointed.

Conversation:
{history}
Note: Do NOT consider simple questions like "Where is my order?" or "When will it be delivered?" as frustration unless the user uses explicit frustration words like angry, disappointed, or bad service.

Reply only in JSON format like this:
{{"is_frustrated": true/false, "needs_escalation": true/false}}
"""


    output = llm.invoke(prompt_text)

    import json
    try:
        result = json.loads(output.content)
        return {
            "is_frustrated": result.get("is_frustrated", False),
            "needs_escalation": result.get("needs_escalation", False)
        }
    except Exception:
        return {
            "is_frustrated": False,
            "needs_escalation": False
        }

def frustration_handler(state):
    return {
        "messages": state["messages"]
        + [
            AIMessage(
                content="I'm really sorry for the inconvenience caused. I truly understand your frustration. Our team is working to resolve this. Is there anything else I can assist you with right now?"
            )
        ]
    }

def detect_handoff_intent(state):
    history = "\n".join([m.content for m in state.get("messages", [])[-5:]])

    prompt = PromptTemplate.from_template(
        """
You are an intelligent assistant for an online fashion store.

Check whether the customer wants to chat to a human support agent.

Examples of handoff requests:
- "I want to talk to a human"
- "Connect me to support"
- "Escalate this"
- "Transfer to manager"

Do NOT consider normal queries like:
- "Where is my order"
- "Track my order"
- "What is the delivery time"
- "are you guys genuine or not"
- "text starting in pattern like gv1234 or containing such order patterns"
Conversation:
{history}

If the customer is asking to speak to a human, support team, manager respond with:
True

Otherwise, respond with:
False
"""
    )

    chain = prompt | llm
    result = chain.invoke({"history": history}).content.strip().lower()

    is_handoff = result in ["true", "yes"]
    return {"needs_human_agent": is_handoff}



def handoff_node(state):
    return {
        "messages": state["messages"] + [
            AIMessage(
                content="Sure. Connecting you to a human support agent. Please wait..."
            )
        ]
    }

# 🤖 Assistant Node (LLM powered)
def assistant_node(state):
    prompt = PromptTemplate.from_template(build_smart_prompt(state))
    chain = prompt | llm
    output = chain.invoke({"context": ""})
    response_text = output.content
    if state.get("is_frustrated"):
        response_text = "I truly apologize for the inconvenience. " + response_text
    return {
        "messages": state["messages"] + [AIMessage(content=output.content)]
    }


# 📦 Order Status Handler Node
def order_status_node(state):
    last_msg = state["messages"][-1].content

    order_name = normalize_order_id(last_msg)
    phone_match = re.search(r"(\d{10})", last_msg)
    phone = phone_match.group(1) if phone_match else None
    order = None

    if order_name:
        order = get_shopify_order(order_name)
        if not order:
            return {
                "messages": state["messages"] + [
                    AIMessage(content=f"No order with {order} id exists in our system, please check the order id and try again")
                ]
            }
        else:
            order_status = get_order_status_summary(order)
            fulfillment_status = order_status['fulfillment_status'] if order_status['fulfillment_status'] is not None else "Unknown"
            return {
                "selected_order_id": order,
                "order_status_by_id": order,
                "messages": state["messages"] + [
                    AIMessage(content=f"Your order {order['name']} is currently: {order_status['status']} with fulfillment status as : {fulfillment_status}")
                ]
            }
    elif phone:
        orders = get_shopify_customer_orders_by_phone(phone)
        if not orders:
            return {
                "messages": state["messages"] + [
                    AIMessage(content=f"No order with {orders} id exists in our system, please check the order id and try again")
                ]
            }
        else:
            return {
                "selected_order_id": order,
                "order_status_by_id": order,
                "messages": state["messages"] + [
                    AIMessage(content=f"Your order {orders[0]['name']} is currently: shipped.")
                ]
            }

    # Check for order ID

    match_order = re.search(r"\bgv\d{2,}\b", last_msg, re.IGNORECASE)
    if match_order:
        order_id = match_order.group(0)
        status = get_order_status(order_id)

        order_status_by_id = state.get("order_status_by_id", {})
        order_status_by_id[order_id] = status

        return {
            "selected_order_id": order_id,
            "order_status_by_id": order_status_by_id,
            "messages": state["messages"] + [
                AIMessage(content=f"Your order {order_id} is currently: {status}.")
            ]
        }

    # Check for phone number
    match_phone = re.search(r"\b\d{10}\b", last_msg)
    if match_phone:
        phone = match_phone.group(0)
        orders = get_orders_by_phone(phone)

        if not orders:
            return {
                "phone_number": phone,
                "messages": state["messages"] + [
                    AIMessage(content="No orders found for this phone number.")
                ]
            }

        order_summary = "\n".join([
            f"{o['order_id']}: {o['status']}" for o in orders
        ])

        return {
            "phone_number": phone,
            "known_orders": orders,
            "messages": state["messages"] + [
                AIMessage(content=f"Here are your orders:\n{order_summary}")
            ]
        }

    # Check if there is already a selected order ID in memory
    if state.get("selected_order_id"):
        order_id = state["selected_order_id"]
        status = get_order_status(order_id)

        order_status_by_id = state.get("order_status_by_id", {})
        order_status_by_id[order_id] = status

        return {
            "order_status_by_id": order_status_by_id,
            "messages": state["messages"] + [
                AIMessage(content=f"Your order {order_id} is currently: {status}.")
            ]
        }

    return {
        "messages": state["messages"] + [
            AIMessage(content="Please provide your 10-digit phone number or order ID to check your order status.")
        ]
    }


# 🔍 Intent Detection using Boolean flag (for Graph Routing)
def detect_order_intent(state):
    last_msg = state["messages"][-1].content.lower()

    order_id_match = re.search(r"\b(?:ord|gv)\d+\b", last_msg, re.IGNORECASE)
    phone_match = re.search(r"\b\d{10}\b", last_msg)
    keywords = ["order", "shipment", "shipped", "delivered", "where is my order", "status", "track", "tracking"]

    is_order_query = bool(
        order_id_match or phone_match or any(kw in last_msg for kw in keywords)
    )

    # Detect frustration
    frustration_result = detect_frustration_llm(state)
    is_frustrated = frustration_result.get("is_frustrated", False)

    return {
        "is_order_query": is_order_query,
        "is_frustrated": is_frustrated,
    }
