import requests
from create_shiprocket_order import create_shiprocket_order

# --- CONFIGURE THESE ---
SHIPROCKET_EMAIL = "puneetjindal@groovee.in"
SHIPROCKET_PASSWORD = "M4r2zDq2^N3%1LQA"
ORDER_ID_TO_CLONE = "909886163"  # Replace with a real Shiprocket order ID

# 1. Authenticate to get token
def get_token():
    url = "https://apiv2.shiprocket.in/v1/external/auth/login"
    resp = requests.post(url, json={"email": SHIPROCKET_EMAIL, "password": SHIPROCKET_PASSWORD})
    return resp.json().get("token")

# 2. Fetch existing order details
def get_order_data(token, order_id):
    url = f"https://apiv2.shiprocket.in/v1/external/orders/show/{order_id}"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers)
    return resp.json().get("data", {})

# 3. Prepare new shipping details (change as needed)
new_shipping = {
    "shipping_customer_name": "Test User",
    "shipping_last_name": "Bot",
    "shipping_address": "123 Test Street",
    "shipping_address_2": "",
    "shipping_city": "TestCity",
    "shipping_pincode": "123456",
    "shipping_country": "India",
    "shipping_state": "TestState",
    "shipping_email": "testuser@example.com",
    "shipping_phone": "9876543210"
}

if __name__ == "__main__":
    token = get_token()
    if not token:
        print("Failed to authenticate with Shiprocket.")
        exit(1)
    existing_order_data = get_order_data(token, ORDER_ID_TO_CLONE)
    if not existing_order_data:
        print("Failed to fetch order data.")
        exit(1)
    result = create_shiprocket_order(token, existing_order_data, new_shipping)
    print("Shiprocket order creation result:")
    print(result)