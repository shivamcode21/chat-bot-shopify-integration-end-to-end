import requests
import re
from datetime import datetime

# --- Config ---
SHOPIFY_DOMAIN = '2dce50-98.myshopify.com'
SHOPIFY_TOKEN = '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'
SHIPROCKET_EMAIL = '20cs169.akshat.saxena@gmail.com'
SHIPROCKET_PASSWORD = 'Akshat@1110'
SHIPROCKET_API_BASE = 'https://apiv2.shiprocket.in/v1/external'

# --- Utils ---
def normalize_order_id(input_text):
    match = re.search(r'gv(\d+)', input_text, re.IGNORECASE)
    return f'#gv{match.group(1)}' if match else None

def classify_status(shiprocket_status, cancelled_at, fulfillment_status):
    shiprocket_status = (shiprocket_status or "").upper().strip()
    cancelled_at = (cancelled_at or "").strip()
    fulfillment_status = (fulfillment_status or "").strip()

    if cancelled_at:
        return "Cancelled"
    if shiprocket_status == "DELIVERED":
        return "Delivered"
    if shiprocket_status in {
        "IN TRANSIT", "IN TRANSIT-EN-ROUTE", "OUT FOR DELIVERY", "PICKED UP",
        "MISROUTED", "UNDELIVERED-1ST ATTEMPT", "UNDELIVERED-2ND ATTEMPT",
        "UNDELIVERED-3RD ATTEMPT", "UNDELIVERED", "REACHED AT DESTINATION HUB", "SHIPPED"
    }:
        return "In Transit"
    if re.search(r"RTO|REACHED BACK TO SELLER CITY", shiprocket_status):
        return "RTO"
    if re.search(r"RETURN", shiprocket_status):
        return "Return"
    if shiprocket_status in {"PICKUP SCHEDULED", "PICKUP RESCHEDULED", "PICKUP EXCEPTION", "OUT FOR PICKUP"}:
        return "Not Yet Dispatched"
    if fulfillment_status == "":
        return "Not Yet Dispatched"
    return shiprocket_status

def get_shopify_customer_orders_by_phone(phone):
    url = f'https://{SHOPIFY_DOMAIN}/admin/api/2023-10/customers/search.json?query=phone:{phone}'
    headers = {'X-Shopify-Access-Token': SHOPIFY_TOKEN}
    res = requests.get(url, headers=headers)
    if res.status_code != 200:
        print(f"⚠️ Shopify API error: {res.status_code} - {res.text}")
        return []
    customers = res.json().get('customers', [])
    if not customers:
        return []
    customer_id = customers[0]['id']
    orders_url = f'https://{SHOPIFY_DOMAIN}/admin/api/2023-10/orders.json?customer_id={customer_id}&status=any'
    res = requests.get(orders_url, headers=headers)
    return res.json().get('orders', [])

def get_shopify_order(order_name):
    order_name = order_name.lstrip('#')
    encoded_order_name = f"%23{order_name}"
    url = f'https://{SHOPIFY_DOMAIN}/admin/api/2023-10/orders.json?name={encoded_order_name}&status=any'
    headers = {'X-Shopify-Access-Token': SHOPIFY_TOKEN}
    res = requests.get(url, headers=headers)
    orders = res.json().get('orders', [])
    for order in orders:
        if order.get('name', '').lower().lstrip('#') == order_name.lower():
            return order
    return None

def get_shiprocket_token():
    url = f'{SHIPROCKET_API_BASE}/auth/login'
    res = requests.post(url, json={'email': SHIPROCKET_EMAIL, 'password': SHIPROCKET_PASSWORD})
    return res.json().get('token')

def get_shiprocket_order_id_from_awb(awb, token):
    url = f'{SHIPROCKET_API_BASE}/courier/track/awb/{awb}'
    headers = {'Authorization': f'Bearer {token}'}
    res = requests.get(url, headers=headers)
    try:
        return res.json().get("tracking_data", {}).get("shipment_track", [{}])[0].get("order_id")
    except:
        return None

def get_shiprocket_order_details(order_id, token):
    url = f'{SHIPROCKET_API_BASE}/orders/show/{order_id}'
    headers = {'Authorization': f'Bearer {token}'}
    res = requests.get(url, headers=headers)
    return res.json().get("data", {})

def get_current_location_api(awb, token):
    url = f"{SHIPROCKET_API_BASE}/courier/track/awb/{awb}"
    headers = {"Authorization": f"Bearer {token}"}
    res = requests.get(url, headers=headers)
    if res.status_code != 200:
        return "⚠️ Could not fetch the latest update."
    data = res.json().get("tracking_data", {})
    activities = data.get("shipment_track_activities", [])
    if not activities:
        return "📦 No tracking updates logged yet."
    latest = activities[0]
    activity = latest.get('activity', '').upper()
    location = latest.get('location', '')
    raw_date = latest.get('date', '')
    return f"📍 Last update: {activity} at {location} on {raw_date}"

def format_etd_date(raw_date):
    try:
        dt = datetime.strptime(raw_date, "%d-%m-%Y %H:%M:%S")
        suffix = "th" if 11 <= dt.day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(dt.day % 10, "th")
        hour = dt.strftime("%I").lstrip("0") or "12"
        minute = dt.strftime("%M")
        ampm = dt.strftime("%p")
        return f"{dt.day}{suffix} {dt.strftime('%B, %Y')} around {hour}:{minute} {ampm}"
    except:
        return "soon! 🚚"


def get_order_status_summary(order):
    token = get_shiprocket_token()
    fulfillments = order.get("fulfillments", [])
    awb = fulfillments[0].get("tracking_number") if fulfillments else None
    courier_url = fulfillments[0].get("tracking_urls", [""])[0] if fulfillments else ""
    cancel = order.get("cancelled_at")
    fulfillment_status = order.get("fulfillment_status")
    sr_order_id = get_shiprocket_order_id_from_awb(awb, token)
    sr_data = get_shiprocket_order_details(sr_order_id, token)
    sr_status = sr_data.get("status", "")
    etd = sr_data.get("etd_date", "")
    status = classify_status(sr_status, cancel, fulfillment_status)
    fulfilled_items = []
    pending_items = []
    for item in order.get("line_items", []):
        if item.get("fulfillment_status") == "fulfilled":
            fulfilled_items.append(item["name"])
        else:
            pending_items.append(item["name"])
    shipments = sr_data.get("shipments")
    if isinstance(shipments, list) and shipments:
        courier = shipments[0].get("courier", "N/A")
    else:
        courier = "N/A"
    return {
        "status": status,
        "fulfillment_status": fulfillment_status,
        "fulfilled_items": fulfilled_items,
        "pending_items": pending_items
    }



# --- Chatbot ---
def chatbot():
    print("🤖: Hi! I'm your Groovee Order Assistant 🪩")
    print("Tell me your order ID (e.g. gv1234) or phone number (10 digits) to begin.")

    cached_orders = []
    while True:
        user_input = input("You: ").strip()

        if user_input.lower() in ["clear", "refresh", "restart", "new chat"]:
            print("\n🔄 Starting a new session...\n")
            chatbot()
            return

        if user_input.lower() in ["exit", "quit", "bye"]:
            print("👋 Bye! Ping me anytime for order help.")
            break

        order_name = normalize_order_id(user_input)
        phone_match = re.search(r"(\d{10})", user_input)
        phone = phone_match.group(1) if phone_match else None
        order = None

        if order_name:
            order = get_shopify_order(order_name)
        elif phone:
            orders = get_shopify_customer_orders_by_phone(phone)
            if not orders:
                print("❌ No orders found for this phone number.")
                continue
            cached_orders = orders
            token = get_shiprocket_token()
            if len(orders) > 1:
                print("📦 Found multiple orders:\n")
                for o in orders:
                    line_items = ", ".join([item['name'] for item in o.get('line_items', [])])
                    cancel = o.get('cancelled_at')
                    fulfillment_status = o.get('fulfillment_status')
                    fulfillments = o.get('fulfillments', [])
                    sr_status = ""
                    try:
                        if fulfillments and fulfillments[0].get("tracking_number"):
                            awb = fulfillments[0].get("tracking_number")
                            sr_order_id = get_shiprocket_order_id_from_awb(awb, token)
                            if sr_order_id:
                                sr_data = get_shiprocket_order_details(sr_order_id, token)
                                sr_status = sr_data.get("status", "")
                    except Exception as e:
                        print(f"⚠️ Could not fetch Shiprocket status for {o['name']}: {e}")
                    status = classify_status(sr_status, cancel, fulfillment_status)
                    print(f"• {o['name']} - {line_items} - Status: {status}")
                print("📌 To continue, enter your Order ID like #gv1234")
                continue
            else:
                order = orders[0]
                order_name = order['name']

        if not order:
            print("❌ Could not find your order. Please try again.")
            continue

        print(f"✅ Found order {order['name']} for {order.get('billing_address', {}).get('name', 'Guest')}.")

        fulfillments = order.get("fulfillments", [])
        awb = fulfillments[0].get("tracking_number") if fulfillments else None
        courier_url = fulfillments[0].get("tracking_urls", [""])[0] if fulfillments else ""
        cancel = order.get("cancelled_at")
        fulfillment_status = order.get("fulfillment_status")

        if not awb:
            print("📦 Your order hasn't shipped yet. We'll notify you once it moves!")
            continue

        token = get_shiprocket_token()
        sr_order_id = get_shiprocket_order_id_from_awb(awb, token)
        sr_data = get_shiprocket_order_details(sr_order_id, token)
        sr_status = sr_data.get("status", "")
        status = classify_status(sr_status, cancel, fulfillment_status)
        shipments = sr_data.get("shipments")
        if isinstance(shipments, list) and shipments:
            courier = shipments[0].get("courier", "N/A")
        else:
            courier = "N/A"

        etd = sr_data.get("etd_date", "")

        if status == "Delivered":
            print(f"🎉 Your order was delivered on {format_etd_date(etd)}. Hope you loved it!")
        else:
            print(f"📦 Your order is currently: {status}")
            print(f"🔗 Track here: {courier_url}")

        while True:
            follow = input("You: ").lower()
            if any(word in follow for word in ["where", "track", "status", "current"]):
                print(get_current_location_api(awb, token))
                print(f"🔗 Track here: {courier_url}")
            elif any(word in follow for word in ["when", "delivery", "arrive", "etd"]):
                print(f"🕒 Expected by: {format_etd_date(etd)}")
                print(f"🔗 Track here: {courier_url}")
            elif any(word in follow for word in ["courier", "partner", "shipping"]):
                print(f"🚚 Shipped via: {courier}")
                print(f"🔗 Track here: {courier_url}")
            elif any(word in follow for word in ["awb", "tracking", "track"]):
                print(f"🔗 AWB: {awb}\n🔗 Track here: {courier_url}")
                print(f"🔗 Track here: {courier_url}")
            elif any(word in follow for word in ["total", "price", "amount"]):
                line_items = order.get("line_items", [])
                total = sum((float(i['price']) - sum(float(d.get("amount", 0)) for d in i.get("discount_allocations", []))) * i.get("quantity", 1) for i in line_items)
                print(f"💰 The Order Total is ₹{total:.2f}")
            elif any(word in follow for word in ["items", "products", "what did i order"]):
                for idx, item in enumerate(order.get("line_items", []), 1):
                    print(f"{idx}. {item['name']} (x{item['quantity']})")
            elif follow in ["exit", "thanks", "ok", "bye"]:
                print("✨ Happy to help! Chat again soon 💬")
                break
            else:
                print("🤖 Ask me about delivery, courier, price, or items. Type 'exit' to end chat.")

# --- Run ---
if __name__ == "__main__":
    chatbot()
