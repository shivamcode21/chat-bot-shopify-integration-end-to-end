# Backup root code for order apis now split into 3 files order_creation.py, cancel_order.py, update_order.py

# import requests
# from typing import Dict, Any, Optional
# import re
# import json
# import os
# import random
# import string

# def create_shopify_order(
#     order_data: Dict[str, Any],
#     access_token: str,
#     shop_url: str,
#     api_version: str = "2025-07"
# ) -> Dict[str, Any]:
#     """
#     Creates an order in Shopify.
#     Args:
#         order_data (dict): The order payload as per Shopify API.
#         access_token (str): Shopify access token.
#         shop_url (str): Your shop's myshopify.com URL (e.g., 'your-store.myshopify.com').
#         api_version (str): Shopify API version (default: '2025-07').
#     Returns:
#         dict: { 'success': bool, 'order': dict (if success), 'error': str (if failure), 'details': dict (optional) }
#     """
#     url = f"https://{shop_url}/admin/api/{api_version}/orders.json"
#     headers = {
#         "X-Shopify-Access-Token": access_token,
#         "Content-Type": "application/json"
#     }
#     # Debug: print order_data before sending
#     print("[DEBUG] Shopify order payload:", json.dumps(order_data, indent=2))
#     try:
#         response = requests.post(url, json={"order": order_data}, headers=headers, timeout=15)
#         if response.status_code == 201:
#             return {"success": True, "order": response.json().get("order", {}), "details": response.json()}
#         else:
#             # Shopify returns error details in JSON
#             try:
#                 error_json = response.json()
#             except Exception:
#                 error_json = {}
#             return {
#                 "success": False,
#                 "error": f"Shopify API error: {response.status_code}",
#                 "details": error_json or response.text
#             }
#     except requests.Timeout:
#         return {"success": False, "error": "Request timed out while creating order."}
#     except requests.ConnectionError:
#         return {"success": False, "error": "Network connection error while creating order."}
#     except Exception as e:
#         return {"success": False, "error": f"Unexpected error: {str(e)}"}


# def cancel_shopify_order(
#     order_id: str,
#     access_token: str,
#     shop_url: str,
#     api_version: str = "2025-07",
#     **kwargs
# ) -> Dict[str, Any]:
#     """
#     Cancels an order in Shopify.
#     Args:
#         order_id (str or int): The Shopify order ID.
#         access_token (str): Shopify access token.
#         shop_url (str): Your shop's myshopify.com URL.
#         api_version (str): Shopify API version (default: '2025-07').
#         kwargs: Optional parameters (amount, currency, email, reason, refund, restock).
#     Returns:
#         dict: { 'success': bool, 'order': dict (if success), 'error': str (if failure), 'details': dict (optional) }
#     """
#     url = f"https://{shop_url}/admin/api/{api_version}/orders/{order_id}/cancel.json"
#     headers = {
#         "X-Shopify-Access-Token": access_token,
#         "Content-Type": "application/json"
#     }
#     payload = {}
#     allowed_fields = ["amount", "currency", "email", "reason", "refund", "restock"]
#     for field in allowed_fields:
#         if field in kwargs:
#             payload[field] = kwargs[field]
#     try:
#         response = requests.post(url, json=payload, headers=headers, timeout=15)
#         if response.status_code == 200:
#             return {"success": True, "order": response.json().get("order", {}), "details": response.json()}
#         else:
#             try:
#                 error_json = response.json()
#             except Exception:
#                 error_json = {}
#             return {
#                 "success": False,
#                 "error": f"Shopify API error: {response.status_code}",
#                 "details": error_json or response.text
#             }
#     except requests.Timeout:
#         return {"success": False, "error": "Request timed out while canceling order."}
#     except requests.ConnectionError:
#         return {"success": False, "error": "Network connection error while canceling order."}
#     except Exception as e:
#         return {"success": False, "error": f"Unexpected error: {str(e)}"}

# def is_valid_email(email: str) -> bool:
#     """Validate email address format."""
#     return re.match(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", email) is not None

# def is_valid_phone(phone: str) -> bool:
#     """Validate Indian phone number (10 digits, optional +91/91)."""
#     return re.match(r"^(\+91|91)?\d{10}$", phone) is not None

# def collect_address_from_data(data: dict) -> dict:
#     """
#     Stepwise validation for address fields. State must always match a known Indian state (abbreviation or full name).
#     Uses only the online API for PIN/city/state validation.
#     """
#     state_map = {
#         "up": "Uttar Pradesh", "dl": "Delhi", "mh": "Maharashtra", "gj": "Gujarat", "rj": "Rajasthan",
#         "mp": "Madhya Pradesh", "pb": "Punjab", "hr": "Haryana", "ka": "Karnataka", "tn": "Tamil Nadu",
#         "wb": "West Bengal", "br": "Bihar", "ap": "Andhra Pradesh", "tg": "Telangana", "kl": "Kerala",
#         "jk": "Jammu and Kashmir", "cg": "Chhattisgarh", "or": "Odisha", "as": "Assam", "jh": "Jharkhand",
#         "ch": "Chandigarh", "ga": "Goa", "tr": "Tripura", "ml": "Meghalaya", "mn": "Manipur", "ar": "Arunachal Pradesh",
#         "mz": "Mizoram", "sk": "Sikkim", "nl": "Nagaland", "py": "Puducherry", "ld": "Lakshadweep", "an": "Andaman and Nicobar Islands"
#     }
#     if not data.get("first_name"):
#         raise ValueError("First name is required.")
#     if not data.get("last_name"):
#         raise ValueError("Last name is required.")
#     if not data.get("address1"):
#         raise ValueError("Address Line 1 is required.")
#     if len(data["address1"]) < 10 or not any(c.isdigit() for c in data["address1"]) or not any(c.isalpha() for c in data["address1"]):
#         raise ValueError("Address must be at least 10 characters and contain both letters and numbers.")
#     # City and state are now filled by pincode, so skip their validation
#     province = data.get("state", "") or data.get("province", "")
#     if not data.get("zip"):
#         raise ValueError("PIN/ZIP Code is required.")
#     # --- PIN code validation with city/state using only the online API ---
#     pin_info = lookup_pincode_online(str(data["zip"]))
#     if pin_info and pin_info.get("city") and pin_info.get("state"):
#         city_match = data["city"].strip().lower() == pin_info["city"].strip().lower()
#         province_str = province.strip().lower() if province else ""
#         state_match = province_str == pin_info["state"].strip().lower()
#         if not (city_match and state_match):
#             raise ValueError(f"PIN {data['zip']} does not match city/state (expected {pin_info['city']}, {pin_info['state']}).")
#     # If not found in API, accept as-is
#     if not data.get("phone"):
#         raise ValueError("Phone number is required.")
#     if not is_valid_phone(data["phone"]):
#         raise ValueError("Invalid phone number.")
#     return {
#         "first_name": data["first_name"],
#         "last_name": data["last_name"],
#         "address1": data["address1"],
#         "address2": data.get("address2", ""),
#         "city": data["city"].title(),
#         "province": province,
#         "country": "India",
#         "zip": data["zip"],
#         "phone": data["phone"]
#     }

# def assemble_order_data(customer_email, customer_phone, product, variant, quantity, address) -> dict:
#     """
#     Assemble Shopify order data dict for order creation.
#     """
#     line_item = {
#         "title": product.get("title", "Unknown"),
#         "price": float(variant.get("price", 0)) if variant else float(product.get("price", 0)),
#         "quantity": quantity
#     }
#     if variant and variant.get("id"):
#         line_item["variant_id"] = variant["id"]
#     order_data = {
#         "line_items": [line_item],
#         "currency": "INR",
#         "email": customer_email,
#         "send_receipt": False,
#         "send_fulfillment_receipt": False,
#         "financial_status": "pending",
#         "payment_gateway_names": ["Cash on Delivery (COD)"],
#         "shipping_address": {
#             "first_name": address.get("first_name", ""),
#             "last_name": address.get("last_name", ""),
#             "address1": address.get("address1", ""),
#             "address2": address.get("address2", ""),
#             "city": address.get("city", ""),
#             "province": address.get("province", ""),
#             "zip": address.get("zip", ""),
#             "country": address.get("country", "India"),
#             "phone": address.get("phone", "")
#         },
#     }
#     return order_data

# def create_order(
#     customer_email: str,
#     customer_phone: str,
#     address_data: dict,
#     product_query: str,
#     quantity: int,
#     shopify_env: dict
# ) -> dict:
#     """
#     Modular API to create a Shopify order.
#     Args:
#         customer_email: Customer's email address.
#         customer_phone: Customer's phone number.
#         address_data: Dict with address fields (first_name, last_name, address1, city, state, zip, phone, [address2]).
#         product_query: Product name or keyword to search.
#         quantity: Quantity to order.
#         shopify_env: Dict with access_token, shop_url, api_version.
#     Returns:
#         dict: { 'success': bool, 'order': dict (if success), 'error': str (if failure), 'details': dict (optional) }
#     """
#     from shopify.modules.product_handlers import update_product_cache, load_product_cache, fuzzy_search_products
#     # Validate email
#     if not customer_email:
#         # Use phone number if available, else random digits
#         if customer_phone and customer_phone.isdigit():
#             unique_part = customer_phone
#         else:
#             unique_part = ''.join(random.choices(string.digits, k=10))
#         customer_email = f"dummy{unique_part}@ecommbot.com"
#     if not is_valid_email(customer_email):
#         return {"success": False, "error": "Invalid email address."}
#     if not is_valid_phone(customer_phone):
#         return {"success": False, "error": "Invalid phone number."}
#     try:
#         # Ensure address dict uses 'province' and includes all required fields
#         address = dict(address_data)
#         if "province" not in address and "state" in address:
#             address["province"] = address["state"]
#         address = collect_address_from_data(address)
#     except Exception as e:
#         return {"success": False, "error": f"Invalid address: {str(e)}"}
#     # Update and load product cache
#     cache_result = update_product_cache(shopify_env["access_token"], shopify_env["shop_url"], shopify_env["api_version"])
#     if not (isinstance(cache_result, dict) and cache_result.get("success", True)):
#         return {"success": False, "error": f"Error updating product cache: {cache_result.get('error', 'Unknown error')}", "details": cache_result}
#     products = load_product_cache()
#     if isinstance(products, dict) and not products.get("success", True):
#         return {"success": False, "error": f"Error loading product cache: {products.get('error', 'Unknown error')}", "details": products}
#     if not isinstance(products, list):
#         return {"success": False, "error": "Product cache is not a valid list.", "details": products}
#     # Fuzzy search for product
#     matches = fuzzy_search_products(product_query, products)
#     if not matches:
#         return {"success": False, "error": "No matching products found for query.", "details": {"query": product_query}}
#     if len(matches) > 1:
#         return {"success": False, "error": "Multiple products found. Please refine your query.", "details": {"matches": [p.get('title') for p in matches]}}
#     product = matches[0]
#     variants = product.get('variants', [])
#     if not variants:
#         return {"success": False, "error": "No variants available for selected product.", "details": {"product": product.get('title')}}
#     variant = variants[0]  # Default to first variant; can be extended to select by size, etc.
#     order_data = assemble_order_data(customer_email, customer_phone, product, variant, quantity, address)
#     result = create_shopify_order(
#         order_data,
#         shopify_env["access_token"],
#         shopify_env["shop_url"],
#         shopify_env["api_version"]
#     )
#     if not result.get("success", False):
#         return {"success": False, "error": result.get("error", "Order creation failed."), "details": result.get("details")}
#     return {"success": True, "order": result.get("order"), "details": result.get("details")}

# def cancel_order(
#     order_identifier: str,
#     reason: str,
#     shopify_env: dict
# ) -> dict:
#     """
#     Modular API to cancel a Shopify order.
#     Args:
#         order_identifier: Shopify order ID (numeric) or order name (e.g., #GV7162 or 7162).
#         reason: Reason for cancellation.
#         shopify_env: Dict with access_token, shop_url, api_version.
#     Returns:
#         dict: { 'success': bool, 'order': dict (if success), 'error': str (if failure), 'details': dict (opt) }
#     """
#     def get_order_id_by_name(order_name, access_token, shop_url, api_version="2025-07"):
#         import requests
#         order_name = order_name.lstrip('#').upper()
#         url = f"https://{shop_url}/admin/api/{api_version}/orders.json?name={order_name}&status=any"
#         headers = {
#             "X-Shopify-Access-Token": access_token,
#             "Content-Type": "application/json"
#         }
#         try:
#             response = requests.get(url, headers=headers)
#             if response.status_code == 200:
#                 orders = response.json().get("orders", [])
#                 for order in orders:
#                     if order.get("name", "").replace('#', '').upper() == order_name:
#                         return order.get("id")
#         except Exception as e:
#             return None
#         return None
#     import requests
#     order_id = None
#     # Try as numeric ID first if all digits
#     if order_identifier.isdigit():
#         # Try to look up by name with and without GV prefix
#         possible_names = [f"GV{order_identifier}", f"{order_identifier}"]
#         for name in possible_names:
#             found_id = get_order_id_by_name(name, shopify_env["access_token"], shopify_env["shop_url"], shopify_env["api_version"])
#             if found_id:
#                 order_id = found_id
#                 break
#         if not order_id:
#             # Fallback: try as direct numeric ID
#             order_id = order_identifier
#     else:
#         # Try to look up by name
#         found_id = get_order_id_by_name(order_identifier, shopify_env["access_token"], shopify_env["shop_url"], shopify_env["api_version"])
#         if found_id:
#             order_id = found_id
#     if not order_id:
#         return {"success": False, "error": "Order not found for cancellation.", "details": {"identifier": order_identifier}}
#     # --- Check fulfillment status ---
#     # Get order details
#     order_url = f"https://{shopify_env['shop_url']}/admin/api/{shopify_env.get('api_version', '2025-07')}/orders/{order_id}.json"
#     headers = {
#         "X-Shopify-Access-Token": shopify_env["access_token"],
#         "Content-Type": "application/json"
#     }
#     try:
#         resp = requests.get(order_url, headers=headers, timeout=15)
#         if resp.status_code != 200:
#             return {"success": False, "error": f"Failed to fetch Shopify order: {resp.text}"}
#         order_data = resp.json().get("order", {})
#     except Exception as e:
#         return {"success": False, "error": f"Exception fetching Shopify order: {str(e)}"}
#     fulfillments = order_data.get("fulfillments", [])
#     is_fulfilled = any(f.get("status", "") == "success" and f.get("tracking_number") for f in fulfillments)
#     # --- Shiprocket Cancel (if fulfilled) ---
#     SHIPROCKET_EMAIL = "puneetjindal@groovee.in"
#     SHIPROCKET_PASSWORD = "M4r2zDq2^N3%1LQA"
#     SHIPROCKET_BASE = "https://apiv2.shiprocket.in/v1/external"
#     def shiprocket_auth():
#         url = f"{SHIPROCKET_BASE}/auth/login"
#         resp = requests.post(url, json={"email": SHIPROCKET_EMAIL, "password": SHIPROCKET_PASSWORD}, timeout=10)
#         if resp.status_code == 200 and resp.json().get("token"):
#             return resp.json()["token"]
#         raise Exception(f"Shiprocket auth failed: {resp.text}")
#     def shiprocket_get_order_id(tracking_number, token):
#         url = f"{SHIPROCKET_BASE}/courier/track/awb/{tracking_number}"
#         headers = {"Authorization": f"Bearer {token}"}
#         resp = requests.get(url, headers=headers, timeout=10)
#         if resp.status_code == 200:
#             data = resp.json()
#             try:
#                 order_id = data["tracking_data"]["shipment_track"][0]["order_id"]
#                 return order_id
#             except Exception:
#                 raise Exception(f"Could not extract order_id from Shiprocket tracking response: {data}")
#         raise Exception(f"Shiprocket tracking API failed: {resp.text}")
#     if is_fulfilled:
#         try:
#             tracking_number = None
#             for f in order_data.get("fulfillments", []):
#                 if f.get("tracking_number"):
#                     tracking_number = f["tracking_number"]
#                     break
#             if not tracking_number:
#                 raise Exception("No tracking number found for fulfilled order.")
#             token = shiprocket_auth()
#             shiprocket_order_id = shiprocket_get_order_id(tracking_number, token)
#             if not shiprocket_order_id:
#                 raise Exception(f"Could not resolve Shiprocket order_id for tracking_number {tracking_number}")
#             # Shiprocket cancel API
#             url = f"{SHIPROCKET_BASE}/orders/cancel"
#             headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
#             payload = {"ids": [int(shiprocket_order_id)]}
#             print("[DEBUG] Shiprocket cancel payload:", payload)
#             resp = requests.post(url, json=payload, headers=headers, timeout=15)
#             print("[DEBUG] Shiprocket cancel response status:", resp.status_code)
#             print("[DEBUG] Shiprocket cancel response text:", resp.text)
#             try:
#                 cancel_json = resp.json()
#             except Exception:
#                 cancel_json = {}
#             if resp.status_code != 200 or not cancel_json.get("status"):
#                 return {
#                     "success": False,
#                     "error": f"Shiprocket cancel failed: {resp.text}",
#                     "details": cancel_json
#                 }
#         except Exception as e:
#             return {"success": False, "error": f"Shiprocket cancel failed: {str(e)}"}
#     # --- Shopify Cancel ---
#     # Normalize order_id to #GVxxxx format for Shopify
#     # Use numeric order_id for API calls; do not prefix with #GV or #gv
#     order_id_str = str(order_id)
#     # Add cancellation reason as order note before cancelling
#     update_result = update_order(order_id_str, note=f"Cancellation reason: {reason}", shopify_env=shopify_env)
#     # (Optional: handle update_result if needed)
#     kwargs = {"reason": reason, "email": True, "restock": True, "currency": "INR"}
#     result = cancel_shopify_order(
#         order_id_str,
#         shopify_env["access_token"],
#         shopify_env["shop_url"],
#         shopify_env["api_version"],
#         **kwargs
#     )
#     if not result.get("success", False):
#         return {"success": False, "error": result.get("error", "Order cancellation failed."), "details": result.get("details")}
#     return {"success": True, "order": result.get("order"), "details": result.get("details")}

# import requests

# def lookup_pincode_online(pin_code):
#     try:
#         url = f"https://api.postalpincode.in/pincode/{pin_code}"
#         headers = {
#             "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
#         }
#         resp = requests.get(url, headers=headers, timeout=10)
#         data = resp.json()

#         if (isinstance(data, list) and data and
#             data[0].get("Status") == "Success" and
#             "PostOffice" in data[0] and data[0]["PostOffice"]):

#             po = data[0]["PostOffice"][0]  # Pick first one by default
#             return {
#                 "city": po.get("District", ""),
#                 "state": po.get("State", "")
#             }

#         else:
#             print(f"[DEBUG] Invalid or no data returned for PIN {pin_code}: {data}")
#             return None

#     except Exception as e:
#         print(f"[DEBUG] Exception during PIN lookup: {e}")
#         return None

# def prompt_and_validate_address(phone=None):
#     """
#     Stepwise, interactive address input and validation. Returns a valid address_data dict or '__BACK__' if user wants to go back.
#     Now allows user to go back at any step by typing 'back'.
#     Accepts phone as an argument; if provided, validates and uses it, otherwise prompts for phone.
#     """
#     state_map = {
#         "up": "Uttar Pradesh", "dl": "Delhi", "mh": "Maharashtra", "gj": "Gujarat", "rj": "Rajasthan",
#         "mp": "Madhya Pradesh", "pb": "Punjab", "hr": "Haryana", "ka": "Karnataka", "tn": "Tamil Nadu",
#         "wb": "West Bengal", "br": "Bihar", "ap": "Andhra Pradesh", "tg": "Telangana", "kl": "Kerala",
#         "jk": "Jammu and Kashmir", "cg": "Chhattisgarh", "or": "Odisha", "as": "Assam", "jh": "Jharkhand",
#         "ch": "Chandigarh", "ga": "Goa", "tr": "Tripura", "ml": "Meghalaya", "mn": "Manipur", "ar": "Arunachal Pradesh",
#         "mz": "Mizoram", "sk": "Sikkim", "nl": "Nagaland", "py": "Puducherry", "ld": "Lakshadweep", "an": "Andaman and Nicobar Islands"
#     }
#     # Use a step index to allow going back
#     steps = ["first_name", "last_name", "address1", "address2", "zip", "phone"]
#     data = {"address2": ""}
#     idx = 0
#     while idx < len(steps):
#         step = steps[idx]
#         if step == "first_name":
#             val = input("First Name: ").strip()
#             if val.lower() == "back":
#                 if idx == 0:
#                     return "__BACK__"
#                 idx -= 1
#                 continue
#             if not val:
#                 print("First name is required.")
#                 continue
#             data["first_name"] = val
#         elif step == "last_name":
#             val = input("Last Name: ").strip()
#             if val.lower() == "back":
#                 idx -= 1
#                 continue
#             if not val:
#                 print("Last name is required.")
#                 continue
#             data["last_name"] = val
#         elif step == "address1":
#             val = input("Address Line 1: ").strip()
#             if val.lower() == "back":
#                 idx -= 1
#                 continue
#             if len(val) < 10 or not any(c.isdigit() for c in val) or not any(c.isalpha() for c in val):
#                 print("Address must be at least 10 characters and contain both letters and numbers.")
#                 continue
#             data["address1"] = val
#         elif step == "address2":
#             val = input("Address Line 2 (optional): ").strip()
#             if val.lower() == "back":
#                 idx -= 1
#                 continue
#             data["address2"] = val
#         elif step == "zip":
#             val = input("PIN/ZIP Code: ").strip()
#             if val.lower() == "back":
#                 idx -= 1
#                 continue
#             if not val:
#                 print("PIN/ZIP Code is required.")
#                 continue
#             pin_info = lookup_pincode_online(val)
#             if pin_info:
#                 print(f"PIN {val} found. City: {pin_info['city']}, State: {pin_info['state']}.")
#                 while True:
#                     confirm = input(f"Is this correct? (y/n or back): ").strip().lower()
#                     if confirm == 'back':
#                         idx -= 1
#                         break
#                     if confirm == 'y':
#                         data["city"] = pin_info["city"]
#                         data["state"] = pin_info["state"]
#                         data["zip"] = val
#                         print(f"City/State confirmed: {data['city']}, {data['state']}.")
#                         break
#                     elif confirm == 'n':
#                         while True:
#                             new_city = input("Enter new city: ").strip()
#                             if new_city.lower() == "back":
#                                 idx -= 1
#                                 break
#                             if new_city:
#                                 data["city"] = new_city
#                                 break
#                             else:
#                                 print("City is required.")
#                         if new_city.lower() == "back":
#                             continue
#                         while True:
#                             new_state = input("Enter new state: ").strip()
#                             if new_state.lower() == "back":
#                                 idx -= 1
#                                 break
#                             if new_state:
#                                 data["state"] = new_state
#                                 break
#                             else:
#                                 print("State is required.")
#                         if new_state.lower() == "back":
#                             continue
#                         data["zip"] = val
#                         break
#                     else:
#                         print("Invalid choice. Please enter y, n, or back.")
#                 if confirm == 'back':
#                     continue
#                 if confirm == 'y' or confirm == 'n':
#                     break
#             else:
#                 print(f"PIN {val} not found in our database. Please enter city and state manually.")
#                 while True:
#                     new_city = input("Enter new city: ").strip()
#                     if new_city.lower() == "back":
#                         idx -= 1
#                         break
#                     if new_city:
#                         data["city"] = new_city
#                         break
#                     else:
#                         print("City is required.")
#                 if new_city.lower() == "back":
#                     continue
#                 while True:
#                     new_state = input("Enter new state: ").strip()
#                     if new_state.lower() == "back":
#                         idx -= 1
#                         break
#                     if new_state:
#                         data["state"] = new_state
#                         break
#                     else:
#                         print("State is required.")
#                 if new_state.lower() == "back":
#                     continue
#                 data["zip"] = val
#                 break
#         elif step == "phone":
#             # If phone is provided and valid, use it
#             if phone and is_valid_phone(phone):
#                 data["phone"] = phone
#                 idx += 1
#                 continue
#             val = input("Phone number: ").strip()
#             if val.lower() == "back":
#                 idx -= 1
#                 continue
#             if not val:
#                 print("Phone number is required.")
#                 continue
#             if not is_valid_phone(val):
#                 print("Invalid phone number. Please enter a valid 10-digit Indian phone number.")
#                 continue
#             data["phone"] = val
#         idx += 1
#     return data

# def create_order_cli():
#     from shopify.modules.product_handlers import update_product_cache, load_product_cache, fuzzy_search_products
#     import os
#     env = {
#         "access_token": os.environ.get("SHOPIFY_ACCESS_TOKEN") or "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",
#         "shop_url": os.environ.get("SHOPIFY_SHOP_URL") or "2dce50-98.myshopify.com",
#         "api_version": os.environ.get("SHOPIFY_API_VERSION", "2025-07")
#     }
#     print("\n--- Create Shopify Order ---")
#     print(f"Using shop: {env['shop_url']} (API version: {env['api_version']})")
#     cache_result = update_product_cache(env["access_token"], env["shop_url"], env["api_version"])
#     if not (isinstance(cache_result, dict) and cache_result.get("success", True)):
#         print(f"Error updating product cache: {cache_result.get('error', 'Unknown error')}")
#         return None
#     products = load_product_cache()
#     if isinstance(products, dict) and not products.get("success", True):
#         print(f"Error loading product cache: {products.get('error', 'Unknown error')}")
#         return None
#     if not isinstance(products, list):
#         print("Product cache is not a valid list. Cannot proceed.")
#         return None
#     # Step 1: Email
#     while True:
#         customer_email = input("Customer email (optional, press Enter to skip): ").strip()
#         if customer_email.lower() == "back":
#             print("Returning to main menu.")
#             return None
#         if customer_email and not is_valid_email(customer_email):
#             print("Please enter a valid email address (e.g., user@example.com) or leave blank.")
#             continue
#         break
#     # Step 2: Phone
#     while True:
#         customer_phone = input("Customer phone number: ").strip()
#         if customer_phone.lower() == "back":
#             # Go back to email
#             return create_order_cli()
#         if not is_valid_phone(customer_phone):
#             print("Please enter a valid 10-digit phone number (with or without country code).")
#             continue
#         break
#     # If email is blank, generate unique dummy email using phone
#     if not customer_email:
#         if customer_phone and customer_phone.isdigit():
#             unique_part = customer_phone
#         else:
#             unique_part = ''.join(random.choices(string.digits, k=10))
#         customer_email = f"dummy{unique_part}@ecommbot.com"
#     # Step 3: Cart
#     cart = []
#     while True:
#         # Product selection logic
#         print("\nWhat do you want today? (e.g., jeans, shacket, hoodie, oversized t-shirt)")
#         query = input("Product or category (partial or full): ").strip()
#         # Remove discount/coupon logic
#         if query.lower() == "back":
#             # Go back to phone
#             return create_order_cli()
#         matches = fuzzy_search_products(query, products)
#         # If fewer than 5 matches, supplement with more products from the full list
#         if len(matches) < 5:
#             seen_ids = set(id(p) for p in matches)
#             for p in products:
#                 if id(p) not in seen_ids:
#                     matches.append(p)
#                     seen_ids.add(id(p))
#                 if len(matches) >= 6:
#                     break
#         product = None
#         if not matches:
#             print("No available products in store. Please enter details manually.")
#             while True:
#                 title = input("Product name: ").strip()
#                 if title.lower() == "back":
#                     break
#                 variant_id = input("Variant/size (optional): ").strip()
#                 if variant_id.lower() == "back":
#                     continue
#                 price = input("Product price: ").strip()
#                 if price.lower() == "back":
#                     continue
#                 try:
#                     price = float(price)
#                 except Exception:
#                     print("Invalid price.")
#                     continue
#                 quantity = input("Quantity: ").strip()
#                 if quantity.lower() == "back":
#                     continue
#                 try:
#                     quantity = int(quantity)
#                 except Exception:
#                     print("Invalid quantity.")
#                     continue
#                 total_price = price * quantity
#                 print(f"Total price for {title} x {quantity}: ₹{total_price:.2f}")
#                 confirm = input(f"Add to cart? (y/n): ").strip().lower()
#                 if confirm == 'y':
#                     cart.append({"product": {"title": title}, "variant": {"id": variant_id, "price": price}, "quantity": quantity, "price": price})
#                     break
#                 elif confirm == 'back':
#                     break
#                 else:
#                     print("Not added. Please enter product details again.")
#         else:
#             # Sort products by total inventory (descending) and show only the top 6
#             def total_inventory(product):
#                 variants = product.get('variants', [])
#                 total = 0
#                 for v in variants:
#                     if isinstance(v, dict) and 'inventory_quantity' in v:
#                         try:
#                             total += int(v['inventory_quantity'])
#                         except Exception:
#                             continue
#                 return total
#             sorted_matches = sorted(matches, key=total_inventory, reverse=True)
#             top_matches = sorted_matches[:6]
#             print("Available products:")
#             for idx, p in enumerate(top_matches, 1):
#                 if not isinstance(p, dict):
#                     continue
#                 variants = [v for v in p.get('variants', []) if isinstance(v, dict) and 'title' in v and 'price' in v]
#                 available_sizes = [v['title'] for v in variants]
#                 sizes_str = ', '.join(available_sizes) if available_sizes else 'None'
#                 prices = set(float(v['price']) for v in variants if 'price' in v)
#                 if len(prices) == 1 and prices:
#                     price_val = prices.pop()
#                     print(f"{idx}. {p.get('title', 'Unknown')} (Type: {p.get('product_type', '-')}) - Sizes: {sizes_str} - Price: ₹{price_val:.2f}")
#                 else:
#                     variant_prices = [f"{v['title']}: ₹{float(v['price']):.2f}" for v in variants]
#                     price_str = '; '.join(variant_prices) if variant_prices else 'No price info'
#                     print(f"{idx}. {p.get('title', 'Unknown')} (Type: {p.get('product_type', '-')}) - Sizes: {sizes_str} | Prices: {price_str}")
#             while True:
#                 sel = input(f"Select product (1-{len(top_matches)}), or type 'more' to see more products, or type 'back' to update something before: ").strip()
#                 if sel.lower() == "back":
#                     # Ask what to update from already completed steps
#                     update_options = []
#                     if customer_email:
#                         update_options.append('email')
#                     if customer_phone:
#                         update_options.append('phone')
#                     if cart:
#                         update_options.append('cart')
#                     if not update_options:
#                         print("Nothing to update yet. Returning to previous step.")
#                         return create_order_cli()
#                     print(f"What do you want to update? ({', '.join(update_options)}): ")
#                     what = input().strip().lower()
#                     if what == 'email':
#                         return create_order_cli()
#                     elif what == 'phone':
#                         while True:
#                             customer_phone = input("Customer phone number: ").strip()
#                             if customer_phone.lower() == "back":
#                                 return create_order_cli()
#                             if not is_valid_phone(customer_phone):
#                                 print("Please enter a valid 10-digit phone number (with or without country code).")
#                                 continue
#                             break
#                         continue
#                     elif what == 'cart':
#                         print("Current cart:")
#                         for idx, item in enumerate(cart, 1):
#                             variant = item.get('variant')
#                             variant_title = variant.get('title', '') if variant else ''
#                             print(f"{idx}. {item['product'].get('title', 'Unknown')} {variant_title} x {item['quantity']}")
#                         remove_idx = input("Enter the number of the item to remove, or press Enter to keep all: ").strip()
#                         if remove_idx.isdigit():
#                             remove_idx = int(remove_idx) - 1
#                             if 0 <= remove_idx < len(cart):
#                                 cart.pop(remove_idx)
#                                 print("Item removed.")
#                         continue
#                     else:
#                         print("Unknown update option. Returning to product selection.")
#                         continue
#                 if sel.lower() == "more":
#                     print("All products in store:")
#                     for idxg, pg in enumerate(products, 1):
#                         variants_g = [v for v in pg.get('variants', []) if isinstance(v, dict) and 'title' in v and 'price' in v]
#                         sizes_g = ', '.join([v['title'] for v in variants_g]) if variants_g else 'None'
#                         prices_g = set(float(v['price']) for v in variants_g if 'price' in v)
#                         if len(prices_g) == 1 and prices_g:
#                             price_val = prices_g.pop()
#                             print(f"{idxg}. {pg.get('title', 'Unknown')} (Type: {pg.get('product_type', '-')}) - Sizes: {sizes_g} - Price: ₹{price_val:.2f}")
#                         else:
#                             variant_prices_g = [f"{v['title']}: ₹{float(v['price']):.2f}" for v in variants_g]
#                             price_str_g = '; '.join(variant_prices_g) if variant_prices_g else 'No price info'
#                             print(f"{idxg}. {pg.get('title', 'Unknown')} (Type: {pg.get('product_type', '-')}) - Sizes: {sizes_g} | Prices: {price_str_g}")
#                     matches = products
#                     continue
#                 if sel.isdigit():
#                     sel_idx = int(sel) - 1
#                     if 0 <= sel_idx < len(top_matches) and isinstance(top_matches[sel_idx], dict):
#                         product = top_matches[sel_idx]
#                         print(f"Auto-selected: {product.get('title', 'Unknown')}. Type 'back' to change product.")
#                         variants = product.get('variants', [])
#                         variant = None
#                         if len(variants) > 1:
#                             print("Available sizes:")
#                             for idx, v in enumerate(variants, 1):
#                                 if isinstance(v, dict) and 'title' in v:
#                                     orig = float(v.get('price', 0))
#                                     print(f"{idx}. {v['title']} - ₹{orig:.2f}")
#                             while True:
#                                 vsel = input(f"Select size (1-{len(variants)}), or press Enter for 1, or type 'back' to return to product selection: ").strip()
#                                 if vsel.lower() == "back":
#                                     break
#                                 if vsel.lower() == "show more":
#                                     print("All products in this collection:")
#                                     for idx2, p2 in enumerate(matches, 1):
#                                         sizes2 = [vv['title'] for vv in p2.get('variants', []) if isinstance(vv, dict) and 'title' in vv]
#                                         sizes_str2 = ', '.join(sizes2) if sizes2 else 'None'
#                                         print(f"{idx2}. {p2.get('title', 'Unknown')} (Type: {p2.get('product_type', '-')}) - Sizes: {sizes_str2}")
#                                     show_more_again = input("Type 'show more' again to see all products in store, or press Enter to continue: ").strip().lower()
#                                     if show_more_again == 'show more':
#                                         print("All products in store:")
#                                         for idx3, p3 in enumerate(products, 1):
#                                             sizes3 = [vv['title'] for vv in p3.get('variants', []) if isinstance(vv, dict) and 'title' in vv]
#                                             sizes_str3 = ', '.join(sizes3) if sizes3 else 'None'
#                                             print(f"{idx3}. {p3.get('title', 'Unknown')} (Type: {p3.get('product_type', '-')}) - Sizes: {sizes_str3}")
#                                     continue
#                                 if re.search(r'discount|coupon|off', vsel, re.IGNORECASE):
#                                     print("Please select a size first. You can request a discount after selecting size and entering quantity.")
#                                     continue
#                                 try:
#                                     vsel_idx = int(vsel) - 1 if vsel else 0
#                                     variant = variants[vsel_idx] if 0 <= vsel_idx < len(variants) and isinstance(variants[vsel_idx], dict) else variants[0]
#                                 except Exception:
#                                     print("Invalid selection, please enter a valid number.")
#                                     continue
#                                 if not variant:
#                                     print("No valid size selected.")
#                                     continue
#                                 orig = float(variant.get('price', 0))
#                                 while True:
#                                     quantity = input("Quantity: ").strip()
#                                     if quantity.lower() == "back":
#                                         break
#                                     discounted = False
#                                     if re.search(r'discount|coupon|off', quantity, re.IGNORECASE):
#                                         discounted = True
#                                         quantity = input("Quantity: ").strip()
#                                         if quantity.lower() == "back":
#                                             break
#                                     if not quantity.isdigit():
#                                         print("Invalid quantity.")
#                                         continue
#                                     quantity = int(quantity)
#                                     total_price = orig * quantity
#                                     variant_name = variant['title'] if variant and 'title' in variant else product.get('title', 'Unknown')
#                                     print(f"Total price for {variant_name} x {quantity}: ₹{total_price:.2f}")
#                                     cart.append({"product": product, "variant": variant, "quantity": quantity, "price": orig})
#                                     break
#                                 break
#                         elif variants:
#                             variant = variants[0] if isinstance(variants[0], dict) else None
#                             orig = float(variant.get('price', 0)) if variant else 0
#                             print(f"Selected: {variant['title'] if variant else product.get('title', 'Unknown')} - ₹{orig:.2f}")
#                             quantity = input("Quantity: ").strip()
#                             if quantity.lower() == "back":
#                                 break
#                             discounted = False
#                             if re.search(r'discount|coupon|off', quantity, re.IGNORECASE):
#                                 discounted = True
#                                 quantity = input("Quantity: ").strip()
#                                 if quantity.lower() == "back":
#                                     break
#                             if not quantity.isdigit():
#                                 print("Invalid quantity.")
#                                 continue
#                             quantity = int(quantity)
#                             total_price = orig * quantity
#                             print(f"Total price for {variant['title'] if variant else product.get('title', 'Unknown')} x {quantity}: ₹{total_price:.2f}")
#                             cart.append({"product": product, "variant": variant, "quantity": quantity, "price": orig})
#                             break
#                         else:
#                             variant = None
#                             orig = float(product.get('price', 0))
#                             print(f"Selected: {product.get('title', 'Unknown')} - ₹{orig:.2f}")
#                             quantity = input("Quantity: ").strip()
#                             if quantity.lower() == "back":
#                                 break
#                             discounted = False
#                             if re.search(r'discount|coupon|off', quantity, re.IGNORECASE):
#                                 discounted = True
#                                 quantity = input("Quantity: ").strip()
#                                 if quantity.lower() == "back":
#                                     break
#                             if not quantity.isdigit():
#                                 print("Invalid quantity.")
#                                 continue
#                             quantity = int(quantity)
#                             total_price = orig * quantity
#                             print(f"Total price for {product.get('title', 'Unknown')} x {quantity}: ₹{total_price:.2f}")
#                             cart.append({"product": product, "variant": variant, "quantity": quantity, "price": orig})
#                             break
#                     break
#                 else:
#                     new_query = sel.strip()
#                     new_matches = fuzzy_search_products(new_query, products)
#                     if not new_matches:
#                         print(f"No products found for '{new_query}'. Please try another search term or type 'back'.")
#                         continue
#                     sorted_new_matches = sorted(new_matches, key=total_inventory, reverse=True)
#                     top_new_matches = sorted_new_matches[:6]
#                     print(f"Available products for '{new_query}':")
#                     for idx, p in enumerate(top_new_matches, 1):
#                         if not isinstance(p, dict):
#                             continue
#                         variants = [v for v in p.get('variants', []) if isinstance(v, dict) and 'title' in v and 'price' in v]
#                         available_sizes = [v['title'] for v in variants]
#                         sizes_str = ', '.join(available_sizes) if available_sizes else 'None'
#                         prices = set(float(v['price']) for v in variants if 'price' in v)
#                         if len(prices) == 1 and prices:
#                             price_val = prices.pop()
#                             print(f"{idx}. {p.get('title', 'Unknown')} (Type: {p.get('product_type', '-')}) - Sizes: {sizes_str} - Price: ₹{price_val:.2f}")
#                         else:
#                             variant_prices = [f"{v['title']}: ₹{float(v['price']):.2f}" for v in variants]
#                             price_str = '; '.join(variant_prices) if variant_prices else 'No price info'
#                             print(f"{idx}. {p.get('title', 'Unknown')} (Type: {p.get('product_type', '-')}) - Sizes: {sizes_str} | Prices: {price_str}")
#                     matches = new_matches
#                     top_matches = top_new_matches
#                     continue
#         # After adding a product, ask if user wants to add more
#         while True:
#             if len(cart) == 1:
#                 more = input("Want something more? (y/n): ").strip().lower()
#             else:
#                 more = input("Add another product? (y/n): ").strip().lower()
#             if more == 'y':
#                 break  # Go back to product selection
#             elif more == 'n':
#                 # Show summary and proceed to address
#                 subtotal = sum(item["price"] * item["quantity"] for item in cart)
#                 total = subtotal
#                 print("\nOrder Summary:")
#                 for item in cart:
#                     orig = float(item["price"])
#                     variant = item.get('variant')
#                     variant_title = ''
#                     if variant and isinstance(variant, dict):
#                         variant_title = variant.get('title', '')
#                     line_item_name = item['product'].get('title', 'Unknown')
#                     if variant_title:
#                         line_item_name += f" - {variant_title}"
#                     print(f"{line_item_name} x {item['quantity']} - Price: ₹{orig:.2f}, Subtotal: ₹{orig * item['quantity']:.2f}")
#                 print(f"Subtotal: ₹{subtotal:.2f}")
#                 print(f"Total: ₹{total:.2f}")
#                 break  # Exit cart loop to address entry
#             elif more == 'back':
#                 if cart:
#                     cart.pop()
#                 break  # Go back to product selection
#             else:
#                 print("Please enter 'y' or 'n'.")
#         if more == 'n':
#             break  # Exit cart loop to address entry

#     # Step 4: Address
#     while True:
#         print("\nEnter shipping address details:")
#         pin_code = input("Enter PIN/ZIP Code: ").strip()
#         if pin_code.lower() == "back":
#             # Go back to cart
#             if cart:
#                 cart.pop()
#             continue
#         pin_info = lookup_pincode_online(pin_code)
#         if pin_info:
#             city_match = True # Assume correct for now, user can override
#             province_str = pin_info["state"].strip().lower()
#             state_match = True # Assume correct for now, user can override
#             print(f"Please confirm the city and state for {pin_code} City/State: {pin_info['city']}, {pin_info['state']}.")
#             while True:
#                 confirm = input(f"Is this correct? (y/n or back): ").strip().lower()
#                 if confirm == 'back':
#                     continue # Go back to PIN input
#                 if confirm == 'y':
#                     city_match = True # Confirm correct
#                     state_match = True # Confirm correct
#                     # Prompt for remaining address fields
#                     first_name = input("First Name: ").strip()
#                     last_name = input("Last Name: ").strip()
#                     address1 = input("Address Line 1: ").strip()
#                     address2 = input("Address Line 2 (optional): ").strip()
#                     address = {
#                         "first_name": first_name,
#                         "last_name": last_name,
#                         "address1": address1,
#                         "address2": address2,
#                         "city": pin_info["city"],
#                         "state": pin_info["state"],
#                         "zip": pin_code,
#                         "phone": customer_phone
#                     }
#                     break
#                 elif confirm == 'n':
#                     # Prompt for new city and state as before
#                     while True:
#                         new_city = input("Enter new city: ").strip()
#                         if new_city.lower() == "back":
#                             continue # Go back to PIN input
#                         if new_city:
#                             break
#                         else:
#                             print("City is required.")
#                     while True:
#                         new_state = input("Enter new state: ").strip()
#                         if new_state.lower() == "back":
#                             continue # Go back to PIN input
#                         if new_state:
#                             break
#                         else:
#                             print("State is required.")
#                     # Prompt for remaining address fields
#                     first_name = input("First Name: ").strip()
#                     last_name = input("Last Name: ").strip()
#                     address1 = input("Address Line 1: ").strip()
#                     address2 = input("Address Line 2 (optional): ").strip()
#                     address = {
#                         "first_name": first_name,
#                         "last_name": last_name,
#                         "address1": address1,
#                         "address2": address2,
#                         "city": new_city,
#                         "state": new_state,
#                         "zip": pin_code,
#                         "phone": customer_phone
#                     }
#                     break
#                 else:
#                     print("Invalid choice. Please enter y, n, or back.")
#             break # Exit PIN input loop
#         else:
#             print(f"PIN {pin_code} not found in our database. Please enter city and state manually.")
#             while True:
#                 new_city = input("Enter new city: ").strip()
#                 if new_city.lower() == "back":
#                     continue # Go back to PIN input
#                 if new_city:
#                     break
#                 else:
#                     print("City is required.")
#             while True:
#                 new_state = input("Enter new state: ").strip()
#                 if new_state.lower() == "back":
#                     continue # Go back to PIN input
#                 if new_state:
#                     break
#                 else:
#                     print("State is required.")
#             # Prompt for remaining address fields
#             first_name = input("First Name: ").strip()
#             last_name = input("Last Name: ").strip()
#             address1 = input("Address Line 1: ").strip()
#             address2 = input("Address Line 2 (optional): ").strip()
#             address = {
#                 "first_name": first_name,
#                 "last_name": last_name,
#                 "address1": address1,
#                 "address2": address2,
#                 "city": new_city,
#                 "state": new_state,
#                 "zip": pin_code,
#                 "phone": customer_phone
#             }
#             break # Exit PIN input loop

#         # No further address prompts or validation here; use the address as collected above
#         break
#     # Step 5: Discount logic and summary/update loop
#     while True:
#         subtotal = sum(item["price"] * item["quantity"] for item in cart)
#         total = subtotal
#         # Step 6: Show summary
#         print("\nOrder Summary:")
#         for item in cart:
#             orig = float(item["price"])
#             variant = item.get('variant')
#             variant_title = ''
#             if variant and isinstance(variant, dict):
#                 variant_title = variant.get('title', '')
#             # Use product name + variant title for line item name
#             line_item_name = item['product'].get('title', 'Unknown')
#             if variant_title:
#                 line_item_name += f" - {variant_title}"
#             print(f"{line_item_name} x {item['quantity']} - Price: ₹{orig:.2f}, Subtotal: ₹{orig * item['quantity']:.2f}")
#         print(f"Subtotal: ₹{subtotal:.2f}")
#         print(f"Total: ₹{total:.2f}")
#         print("Shipping Address:")
#         for k, v in address.items():
#             print(f"  {k.title()}: {v}")
#         confirm = input("Type 'yes' to confirm order, or 'update' to change something: ").strip().lower()
#         if confirm == "yes":
#             break
#         elif confirm == "update":
#             what = input("What do you want to update? (product, quantity, address): ").strip().lower()
#             if what == "product":
#                 # Remove last product and re-enter
#                 if cart:
#                     cart.pop()
#                 continue
#             elif what == "quantity":
#                 for idx, item in enumerate(cart, 1):
#                     variant = item.get('variant')
#                     variant_title = ''
#                     if variant and isinstance(variant, dict):
#                         variant_title = variant.get('title', '')
#                     line_item_name = item['product'].get('title', 'Unknown')
#                     if variant_title:
#                         line_item_name += f" - {variant_title}"
#                     print(f"{idx}. {line_item_name} (Current qty: {item['quantity']})")
#                 sel = input("Select product to update quantity (number): ").strip()
#                 try:
#                     sel_idx = int(sel) - 1
#                     if 0 <= sel_idx < len(cart):
#                         new_qty = input("Enter new quantity: ").strip()
#                         cart[sel_idx]["quantity"] = int(new_qty)
#                 except Exception:
#                     print("Invalid selection or quantity.")
#                 continue
#             elif what == "address":
#                 while True:
#                     print("Update shipping address:")
#                     address_data = prompt_and_validate_address(customer_phone)
#                     if address_data == "__BACK__":
#                         break
#                     try:
#                         address = collect_address_from_data(address_data)
#                         break
#                     except ValueError as ve:
#                         print(f"Address error: {ve}")
#                         continue
#                 continue
#             else:
#                 print("Unknown update option.")
#                 continue
#         else:
#             print("Order cancelled.")
#             return None
#     # Step 7: Assemble line_items for Shopify order
#     line_items = []
#     for item in cart:
#         variant = item.get('variant')
#         variant_title = ''
#         if variant and isinstance(variant, dict):
#             variant_title = variant.get('title', '')
#         line_item_name = item['product'].get('title', 'Unknown')
#         if variant_title:
#             line_item_name += f" - {variant_title}"
#         line_item = {
#             "title": line_item_name,
#             "price": item["price"],
#             "quantity": item["quantity"]
#         }
#         if variant and variant.get("id"):
#             line_item["variant_id"] = variant["id"]
#         line_items.append(line_item)
#     order_data = {
#         "line_items": line_items,
#         "currency": "INR",
#         "email": customer_email,
#         "send_receipt": False,
#         "send_fulfillment_receipt": False,
#         "financial_status": "pending",
#         "payment_gateway_names": ["Cash on Delivery (COD)"],
#         "shipping_address": {
#             "first_name": address.get("first_name", ""),
#             "last_name": address.get("last_name", ""),
#             "address1": address.get("address1", ""),
#             "address2": address.get("address2", ""),
#             "city": address.get("city", ""),
#             "province": address.get("state", ""),
#             "zip": address.get("zip", ""),
#             "country": "India",
#             "phone": address.get("phone", "")
#         },
#         "discount_codes": []
#     }
#     print("\nCreating order...")
#     result = create_shopify_order(
#         order_data,
#         env["access_token"],
#         env["shop_url"],
#         env["api_version"]
#     )
#     if not result.get("success", False):
#         print(f"Order creation failed: {result.get('error', 'Unknown error')}")
#         if result.get("details"):
#             print(f"Details: {result['details']}")
#         return result
#     print("\nShopify API Response:")
#     print(result)
#     return result

# def cancel_order_cli():
#     import os
#     env = {
#         "access_token": os.environ.get("SHOPIFY_ACCESS_TOKEN") or "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",
#         "shop_url": os.environ.get("SHOPIFY_SHOP_URL") or "2dce50-98.myshopify.com",
#         "api_version": os.environ.get("SHOPIFY_API_VERSION", "2025-07")
#     }
#     print("\n--- Cancel Shopify Order ---")
#     print(f"Using shop: {env['shop_url']} (API version: {env['api_version']})")
#     def get_order_id_by_name(order_name, access_token, shop_url, api_version="2025-07"):
#         import requests
#         order_name = order_name.lstrip('#').upper()
#         url = f"https://{shop_url}/admin/api/{api_version}/orders.json?name={order_name}&status=any"
#         headers = {
#             "X-Shopify-Access-Token": access_token,
#             "Content-Type": "application/json"
#         }
#         try:
#             response = requests.get(url, headers=headers)
#             if response.status_code == 200:
#                 orders = response.json().get("orders", [])
#                 for order in orders:
#                     if order.get("name", "").replace('#', '').upper() == order_name:
#                         return order.get("id")
#             else:
#                 print(f"Shopify API error: {response.status_code} {response.text}")
#         except Exception as e:
#             print(f"Exception querying Shopify API for order: {e}")
#         return None
#     # Step 1: Order ID
#     while True:
#         order_id_input = input("Enter the Shopify Order ID or order name (e.g., #GV7091 or 7091): ").strip()
#         if order_id_input.lower() == "back":
#             print("Exiting order cancellation.")
#             return None
#         order_id = None
#         # Try as numeric ID first if all digits
#         if order_id_input.isdigit():
#             # Try to look up by name with and without #GV prefix
#             possible_names = [f"GV{order_id_input}", f"{order_id_input}"]
#             for name in possible_names:
#                 found_id = get_order_id_by_name(name, env["access_token"], env["shop_url"], env["api_version"])
#                 if found_id:
#                     order_id = found_id
#                     break
#             if not order_id:
#                 # Fallback: try as direct numeric ID
#                 order_id = order_id_input
#         else:
#             # Try to look up by name
#             found_id = get_order_id_by_name(order_id_input, env["access_token"], env["shop_url"], env["api_version"])
#             if found_id:
#                 order_id = found_id
#         if order_id:
#             break
#         print("Could not find order with that name or ID. Please try again.")
#     email = True
#     restock = True
#     # Step 2: Reason
#     while True:
#         reason = input("Reason for cancellation (customer, inventory, fraud, declined, other): ").strip()
#         if reason.lower() == "back":
#             # Go back to order ID
#             return cancel_order_cli()
#         if reason:
#             break
#         print("Reason for cancellation is required.")
#     # Normalize order_id to #GVxxxx format for Shopify
#     # Use numeric order_id for API calls; do not prefix with #GV or #gv
#     order_id_str = str(order_id)
#     # Update order note with reason before cancellation
#     update_result = update_order(order_id_str, note=f"Cancellation reason: {reason}", shopify_env=env)
#     if not update_result.get("success"):
#         print(f"Failed to update order note: {update_result.get('error')}")
#         if update_result.get("details"):
#             print(f"Details: {update_result['details']}")
#         # Continue with cancellation anyway
#     kwargs = {"reason": reason, "email": email, "restock": restock, "currency": "INR"}
#     print("\nCanceling order...")
#     result = cancel_shopify_order(
#         order_id_str,
#         env["access_token"],
#         env["shop_url"],
#         env["api_version"],
#         **kwargs
#     )
#     if not result.get("success", False):
#         print(f"Order cancellation failed: {result.get('error', 'Unknown error')}")
#         if result.get("details"):
#             print(f"Details: {result['details']}")
#         return result
#     print("\nShopify API Response:")
#     print(result)
#     print("If a refund is due, it will be processed within 7-10 business days.")
#     return result

# def update_order(
#     order_id: str,
#     shipping_address: Optional[dict] = None,
#     note: Optional[str] = None,
#     shopify_env: Optional[dict] = None
# ) -> dict:
#     """
#     Update an order's shipping address and/or note using Shopify's orderUpdate GraphQL mutation.
#     If the order is fulfilled, also update the address in Shiprocket before updating Shopify.
#     Args:
#         order_id: Shopify order GID (e.g., 'gid://shopify/Order/123456789') or numeric ID (will be converted).
#         shipping_address: dict with address fields (address1, city, province, zip, country, ...)
#         note: Optional order note.
#         shopify_env: Dict with access_token, shop_url, api_version.
#     Returns:
#         dict: { 'success': bool, 'order': dict (if success), 'error': str (if failure), 'details': dict (optional), 'userErrors': list }
#     """
#     import requests
#     import time
#     # --- Shiprocket helpers ---
#     SHIPROCKET_EMAIL = "puneetjindal@groovee.in"
#     SHIPROCKET_PASSWORD = "M4r2zDq2^N3%1LQA"
#     SHIPROCKET_BASE = "https://apiv2.shiprocket.in/v1/external"

#     def shiprocket_auth():
#         url = f"{SHIPROCKET_BASE}/auth/login"
#         resp = requests.post(url, json={"email": SHIPROCKET_EMAIL, "password": SHIPROCKET_PASSWORD}, timeout=10)
#         if resp.status_code == 200 and resp.json().get("token"):
#             return resp.json()["token"]
#         raise Exception(f"Shiprocket auth failed: {resp.text}")

#     def shiprocket_get_order_id(tracking_number, token):
#         url = f"{SHIPROCKET_BASE}/courier/track/awb/{tracking_number}"
#         headers = {"Authorization": f"Bearer {token}"}
#         resp = requests.get(url, headers=headers, timeout=10)
#         if resp.status_code == 200:
#             data = resp.json()
#             try:
#                 order_id = data["tracking_data"]["shipment_track"][0]["order_id"]
#                 return order_id
#             except Exception:
#                 raise Exception(f"Could not extract order_id from Shiprocket tracking response: {data}")
#         raise Exception(f"Shiprocket tracking API failed: {resp.text}")

#     def shiprocket_update_address(order_id, address, token):
#         url = f"{SHIPROCKET_BASE}/orders/address/update"
#         headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
#         shipping_customer_name = f"{address.get('first_name', '')} {address.get('last_name', '')}".strip()
#         shipping_phone = address.get("phone", "").strip()
#         if not shipping_customer_name or not shipping_phone:
#             raise Exception(f"Missing required fields for Shiprocket: shipping_customer_name ('{shipping_customer_name}') or shipping_phone ('{shipping_phone}')")
#         payload = {
#             "order_id": int(order_id),
#             "shipping_customer_name": shipping_customer_name,
#             "shipping_phone": shipping_phone,
#             "shipping_address": address.get("address1", ""),
#             "shipping_address_2": address.get("address2", ""),
#             "shipping_city": address.get("city", ""),
#             "shipping_state": address.get("province", address.get("state", "")),
#             "shipping_country": address.get("country", "India"),
#             "shipping_pincode": int(address.get("zip", 0)),
#         }
#         # Optional fields
#         if address.get("email"): payload["shipping_email"] = address["email"]
#         if address.get("alternate_phone"): payload["billing_alternate_phone"] = address["alternate_phone"]
#         print("[DEBUG] Shiprocket address payload:", payload)
#         resp = requests.post(url, json=payload, headers=headers, timeout=15)
#         if resp.status_code in [200, 202]:
#             # Optionally check for status in JSON if present, but 202 is success for Shiprocket
#             return resp.json() if resp.text else {"success": True, "message": "Accepted by Shiprocket"}
#         print("[DEBUG] Shiprocket response status:", resp.status_code)
#         print("[DEBUG] Shiprocket response text:", resp.text)
#         raise Exception(f"Shiprocket address update failed: {resp.text}")

#     def resolve_order_gid(order_id_input, shopify_env):
#         if str(order_id_input).startswith("gid://shopify/Order/"):
#             return order_id_input
#         if str(order_id_input).isdigit():
#             return f"gid://shopify/Order/{order_id_input}"
#         order_name = str(order_id_input).lstrip('#').upper()
#         url = f"https://{shopify_env['shop_url']}/admin/api/{shopify_env.get('api_version', '2025-07')}/orders.json?name={order_name}&status=any"
#         headers = {
#             "X-Shopify-Access-Token": shopify_env["access_token"],
#             "Content-Type": "application/json"
#         }
#         try:
#             resp = requests.get(url, headers=headers, timeout=10)
#             if resp.status_code == 200:
#                 orders = resp.json().get("orders", [])
#                 for order in orders:
#                     if order.get("name", "").replace('#', '').upper() == order_name:
#                         return f"gid://shopify/Order/{order.get('id')}"
#         except Exception:
#             pass
#         return order_id_input

#     if not shopify_env or not shopify_env.get("access_token") or not shopify_env.get("shop_url"):
#         return {"success": False, "error": "Missing Shopify environment details."}
#     if shipping_address:
#         try:
#             _ = collect_address_from_data({
#                 "first_name": shipping_address.get("first_name", "Test"),
#                 "last_name": shipping_address.get("last_name", "Test"),
#                 "address1": shipping_address.get("address1", ""),
#                 "address2": shipping_address.get("address2", ""),
#                 "city": shipping_address.get("city", ""),
#                 "state": shipping_address.get("province", ""),
#                 "zip": shipping_address.get("zip", ""),
#                 "phone": shipping_address.get("phone", "1234567890")
#             })
#         except Exception as e:
#             return {"success": False, "error": f"Invalid shipping address: {str(e)}"}
#     # --- Fetch Shopify order details (REST) ---
#     order_numeric_id = None
    
#     # Helper function to try different order name formats for #gv7198 format
#     def try_order_lookup(order_input, shopify_env):
#         possible_names = []
        
#         # If it's all digits, try different formats
#         if str(order_input).isdigit():
#             possible_names = [
#                 f"#gv{str(order_input)}",  # #gv7198
#                 f"gv{str(order_input)}",   # gv7198
#                 str(order_input),          # 7198
#                 f"#{str(order_input)}"    # #7198
#             ]
#         else:
#             # If it starts with #, try different formats
#             clean_name = str(order_input).lstrip('#')
#             possible_names = [
#                 str(order_input),          # #gv7198 (original)
#                 clean_name,                # gv7198
#                 f"#{clean_name}",         # #gv7198
#             ]
        
#         headers = {
#             "X-Shopify-Access-Token": shopify_env["access_token"],
#             "Content-Type": "application/json"
#         }
        
#         for name in possible_names:
#             url = f"https://{shopify_env['shop_url']}/admin/api/{shopify_env.get('api_version', '2025-07')}/orders.json?name={name}&status=any"
#             print(f"[DEBUG] Trying order lookup with name: {name}")
#             print(f"[DEBUG] Shopify order lookup URL: {url}")
#             try:
#                 resp = requests.get(url, headers=headers, timeout=10)
#                 print(f"[DEBUG] Shopify order lookup response: {resp.status_code} {resp.text}")
#                 if resp.status_code == 200:
#                     orders = resp.json().get("orders", [])
#                     for order in orders:
#                         order_name = order.get("name", "")
#                         # Try exact match first
#                         if order_name == name:
#                             return str(order.get('id'))
#                         # Try case-insensitive match
#                         if order_name.upper() == name.upper():
#                             return str(order.get('id'))
#                         # Try without hash
#                         if order_name.replace('#', '') == name.replace('#', ''):
#                             return str(order.get('id'))
#                         # Try case-insensitive without hash
#                         if order_name.replace('#', '').upper() == name.replace('#', '').upper():
#                             return str(order.get('id'))
#             except Exception as e:
#                 print(f"[DEBUG] Exception during Shopify order lookup for {name}: {e}")
#                 continue
#         return None
    
#     # Try to resolve order ID
#     if str(order_id).startswith("gid://shopify/Order/"):
#         order_numeric_id = str(order_id).split("/")[-1]
#     else:
#         order_numeric_id = try_order_lookup(order_id, shopify_env)
#     # Fallback: try direct numeric ID fetch if not found
#     if not order_numeric_id and str(order_id).isdigit():
#         headers = {
#             "X-Shopify-Access-Token": shopify_env["access_token"],
#             "Content-Type": "application/json"
#         }
#         test_url = f"https://{shopify_env['shop_url']}/admin/api/{shopify_env.get('api_version', '2025-07')}/orders/{str(order_id)}.json"
#         print(f"[DEBUG] Shopify direct numeric ID fetch URL: {test_url}")
#         try:
#             resp = requests.get(test_url, headers=headers, timeout=10)
#             print(f"[DEBUG] Shopify direct numeric ID fetch response: {resp.status_code} {resp.text}")
#             if resp.status_code == 200:
#                 order_numeric_id = str(order_id)
#         except Exception as e:
#             print(f"[DEBUG] Exception during Shopify direct numeric ID fetch: {e}")
#             pass
#     if not order_numeric_id:
#         return {"success": False, "error": "Could not resolve Shopify order ID."}
#     # Get order details
#     order_url = f"https://{shopify_env['shop_url']}/admin/api/{shopify_env.get('api_version', '2025-07')}/orders/{order_numeric_id}.json"
#     headers = {
#         "X-Shopify-Access-Token": shopify_env["access_token"],
#         "Content-Type": "application/json"
#     }
#     try:
#         resp = requests.get(order_url, headers=headers, timeout=15)
#         if resp.status_code != 200:
#             return {"success": False, "error": f"Failed to fetch Shopify order: {resp.text}"}
#         order_data = resp.json().get("order", {})
#     except Exception as e:
#         return {"success": False, "error": f"Exception fetching Shopify order: {str(e)}"}
#     fulfillments = order_data.get("fulfillments", [])
#     is_fulfilled = any(f.get("status", "") == "success" and f.get("tracking_number") for f in fulfillments)
#     # --- If NOT fulfilled: update Shopify only ---
#     if not is_fulfilled:
#         # Add note about address update
#         note_to_add = (note or "") + "\nOrder address updated by chatbot"
#         # Use GraphQL mutation as before
#         gid = resolve_order_gid(order_id, shopify_env)
#         mutation = '''
#         mutation OrderUpdate($input: OrderInput!) {
#           orderUpdate(input: $input) {
#             order {
#               id
#               note
#               shippingAddress {
#                 address1
#                 city
#                 province
#                 zip
#                 country
#               }
#             }
#             userErrors {
#               field
#               message
#             }
#           }
#         }
#         '''
#         input_obj: dict[str, Any] = {"id": gid}
#         if shipping_address:
#             input_obj["shippingAddress"] = {k: str(v) for k, v in shipping_address.items() if k in ["address1", "address2", "city", "province", "zip", "country"] and v is not None}
#         input_obj["note"] = note_to_add.strip()
#         variables = {"input": input_obj}
#         url = f"https://{shopify_env['shop_url']}/admin/api/{shopify_env.get('api_version', '2025-07')}/graphql.json"
#         headers = {
#             "X-Shopify-Access-Token": shopify_env["access_token"],
#             "Content-Type": "application/json"
#         }
#         try:
#             response = requests.post(url, json={"query": mutation, "variables": variables}, headers=headers, timeout=15)
#             resp_json = response.json()
#             if "errors" in resp_json:
#                 return {"success": False, "error": "GraphQL error", "details": resp_json["errors"]}
#             order_update = resp_json.get("data", {}).get("orderUpdate", {})
#             user_errors = order_update.get("userErrors", [])
#             if user_errors:
#                 return {"success": False, "error": "User errors in order update", "userErrors": user_errors, "details": order_update}
#             return {"success": True, "order": order_update.get("order"), "details": order_update}
#         except Exception as e:
#             return {"success": False, "error": f"Exception during order update: {str(e)}"}
#     # --- If fulfilled: update Shiprocket, then Shopify ---
#     # Get tracking_number from first successful fulfillment
#     tracking_number = None
#     for f in fulfillments:
#         if f.get("status", "") == "success" and f.get("tracking_number"):
#             tracking_number = f["tracking_number"]
#             break
#     if not tracking_number:
#         return {"success": False, "error": "Order is fulfilled but no tracking number found."}
#     # Shiprocket auth and update
#     try:
#         shiprocket_token = shiprocket_auth()
#     except Exception as e:
#         return {"success": False, "error": f"Shiprocket auth failed: {str(e)}"}
#     try:
#         shiprocket_order_id = shiprocket_get_order_id(tracking_number, shiprocket_token)
#     except Exception as e:
#         return {"success": False, "error": f"Shiprocket tracking lookup failed: {str(e)}"}
#     try:
#         shiprocket_update_address(shiprocket_order_id, shipping_address, shiprocket_token)
#     except Exception as e:
#         return {"success": False, "error": f"Shiprocket address update failed: {str(e)}"}
#     # After Shiprocket update, update Shopify
#     note_to_add = (note or "") + "\nOrder address updated by chatbot (after fulfillment, Shiprocket updated)"
#     gid = resolve_order_gid(order_id, shopify_env)
#     mutation = '''
#     mutation OrderUpdate($input: OrderInput!) {
#       orderUpdate(input: $input) {
#         order {
#           id
#           note
#           shippingAddress {
#             address1
#             city
#             province
#             zip
#             country
#           }
#         }
#         userErrors {
#           field
#           message
#         }
#       }
#     }
#     '''
#     input_obj_fulfilled: dict[str, Any] = {"id": gid}
#     if shipping_address:
#         input_obj_fulfilled["shippingAddress"] = {k: str(v) for k, v in shipping_address.items() if k in ["address1", "address2", "city", "province", "zip", "country"] and v is not None}
#     input_obj_fulfilled["note"] = note_to_add.strip()
#     variables = {"input": input_obj_fulfilled}
#     url = f"https://{shopify_env['shop_url']}/admin/api/{shopify_env.get('api_version', '2025-07')}/graphql.json"
#     headers = {
#         "X-Shopify-Access-Token": shopify_env["access_token"],
#         "Content-Type": "application/json"
#     }
#     try:
#         response = requests.post(url, json={"query": mutation, "variables": variables}, headers=headers, timeout=15)
#         resp_json = response.json()
#         if "errors" in resp_json:
#             return {"success": False, "error": "GraphQL error", "details": resp_json["errors"]}
#         order_update = resp_json.get("data", {}).get("orderUpdate", {})
#         user_errors = order_update.get("userErrors", [])
#         if user_errors:
#             return {"success": False, "error": "User errors in order update", "userErrors": user_errors, "details": order_update}
#         return {"success": True, "order": order_update.get("order"), "details": order_update}
#     except Exception as e:
#         return {"success": False, "error": f"Exception during order update: {str(e)}"}


# def update_order_cli():
#     import os
#     env = {
#         "access_token": os.environ.get("SHOPIFY_ACCESS_TOKEN") or "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",
#         "shop_url": os.environ.get("SHOPIFY_SHOP_URL") or "2dce50-98.myshopify.com",
#         "api_version": os.environ.get("SHOPIFY_API_VERSION", "2025-07")
#     }
#     print("\n--- Update Shopify Order ---")
#     # Step 1: Order ID
#     while True:
#         order_id = input("Enter the Shopify Order ID (gv1234 or #gv1234): ").strip()
#         if order_id.lower() == "back":
#             print("Exiting order update.")
#             return None
#         if order_id:
#             break
#     # Always resolve to GID before update
#     def resolve_order_gid(order_id_input, env):
#         if str(order_id_input).startswith("gid://shopify/Order/"):
#             return order_id_input
#         if str(order_id_input).isdigit():
#             return f"gid://shopify/Order/{order_id_input}"
        
#         # Try different order name formats for #gv7198 format
#         possible_names = []
#         clean_name = str(order_id_input).lstrip('#')
#         possible_names = [
#             str(order_id_input),          # #gv7198 (original)
#             clean_name,                   # gv7198
#             f"#{clean_name}",            # #gv7198
#         ]
        
#         headers = {
#             "X-Shopify-Access-Token": env["access_token"],
#             "Content-Type": "application/json"
#         }
        
#         for name in possible_names:
#             url = f"https://{env['shop_url']}/admin/api/{env.get('api_version', '2025-07')}/orders.json?name={name}&status=any"
#             try:
#                 resp = requests.get(url, headers=headers, timeout=10)
#                 if resp.status_code == 200:
#                     orders = resp.json().get("orders", [])
#                     for order in orders:
#                         order_name = order.get("name", "")
#                         # Try exact match first
#                         if order_name == name:
#                             return f"gid://shopify/Order/{order.get('id')}"
#                         # Try case-insensitive match
#                         if order_name.upper() == name.upper():
#                             return f"gid://shopify/Order/{order.get('id')}"
#                         # Try without hash
#                         if order_name.replace('#', '') == name.replace('#', ''):
#                             return f"gid://shopify/Order/{order.get('id')}"
#                         # Try case-insensitive without hash
#                         if order_name.replace('#', '').upper() == name.replace('#', '').upper():
#                             return f"gid://shopify/Order/{order.get('id')}"
#             except Exception:
#                 continue
#         return order_id_input
#     order_id_gid = resolve_order_gid(order_id, env)
#     # Step 2: Update shipping address?
#     while True:
#         update_shipping = input("Do you want to update the shipping address? (y/n): ").strip().lower()
#         if update_shipping == "back":
#             # Go back to order ID
#             return update_order_cli()
#         if update_shipping in ['y', 'n']:
#             break
#     shipping_address = None
#     if update_shipping == 'y':
#         while True:
#             print("Enter new shipping address details:")
#             address_data = prompt_and_validate_address()
#             if address_data == "__BACK__":
#                 # Go back to update_shipping
#                 break
#             # Ensure phone is present and valid
#             if "phone" not in address_data or not address_data["phone"] or not is_valid_phone(address_data["phone"]):
#                 while True:
#                     phone = input("Phone number: ").strip()
#                     if not phone:
#                         print("Phone number is required.")
#                         continue
#                     if not is_valid_phone(phone):
#                         print("Invalid phone number. Please enter a valid 10-digit Indian phone number.")
#                         continue
#                     address_data["phone"] = phone
#                     break
#             # Map to Shopify's expected keys and include required Shiprocket fields
#             shipping_address = {
#                 "first_name": address_data["first_name"],
#                 "last_name": address_data["last_name"],
#                 "phone": address_data["phone"],
#                 "address1": address_data["address1"],
#                 "address2": address_data.get("address2", ""),
#                 "city": address_data["city"],
#                 "province": address_data["state"],
#                 "zip": address_data["zip"],
#                 "country": "India"
#             }
#             break
#         if address_data == "__BACK__":
#             return update_order_cli()
#     # Step 3: Note
#     while True:
#         note = input("Enter a note for the order (or leave blank): ").strip()
#         if note.lower() == "back":
#             # Go back to shipping address
#             if update_shipping == 'y':
#                 return update_order_cli()
#             else:
#                 return update_order_cli()
#         break
#     result = update_order(order_id_gid, shipping_address, note or None, env)
#     if result.get("success"):
#         print("Order updated successfully!")
#         print(result.get("order"))
#     else:
#         print(f"Order update failed: {result.get('error')}")
#         if result.get("userErrors"):
#             for err in result["userErrors"]:
#                 print(f"Field: {err.get('field')} - Message: {err.get('message')}")
#         if result.get("details"):
#             print(f"Details: {result['details']}") 