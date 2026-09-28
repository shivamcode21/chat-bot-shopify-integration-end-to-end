ORDERS_BY_PHONE = {
    "9999999999": [
        {"order_id": "ORD001", "status": "Shipped"},
        {"order_id": "ORD002", "status": "Delivered"}
    ]
}

ORDER_STATUS = {
    "ORD001": "Shipped",
    "ORD002": "Delivered"
}

def get_orders_by_phone(phone: str):
    return ORDERS_BY_PHONE.get(phone, [])

def get_order_status(order_id: str):
    return ORDER_STATUS.get(order_id, "Unknown")