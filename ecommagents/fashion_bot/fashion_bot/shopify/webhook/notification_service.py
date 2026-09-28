# """
# Notification service for Shopify webhook events.
# """
# import logging
# from typing import Dict, Any, Optional
# from datetime import datetime
#
#
# logger = logging.getLogger(__name__)
#
# class ShopifyNotificationService:
#     def __init__(self):
#         self.templates = {
#             "paid_unfulfilled": {
#                 "title": "Order Paid, Not Fulfilled",
#                 "message": "Your order is paid but not yet fulfilled.",
#                 "priority": "high"
#             },
#             "partially_paid_unfulfilled": {
#                 "title": "Order Partially Paid, Not Fulfilled",
#                 "message": "Your order is partially paid and unfulfilled.",
#                 "priority": "medium"
#             },
#             "payment_pending_unfulfilled": {
#                 "title": "Order Payment Pending, Not Fulfilled",
#                 "message": "Your order payment is pending and unfulfilled.",
#                 "priority": "medium"
#             },
#             "fulfilled": {
#                 "title": "Order Fulfilled",
#                 "message": "Your order has been fulfilled!",
#                 "priority": "high"
#             },
#             "voided": {
#                 "title": "Order Cancelled",
#                 "message": "Your order has been cancelled. We're bummed it didn’t work out — check our latest drops for fresh styles.",
#                 "priority": "high"
#             },
#         }
#     async def send_notification(self, event_template: ShopifyEventTemplate, order_data: Dict[str, Any], user_id: Optional[str] = None) -> bool:
#         try:
#             template_id = event_template.template_id
#             if template_id not in self.templates:
#                 logger.warning(f"Template {template_id} not found")
#                 return False
#             template = self.templates[template_id]
#             notification_data = self._prepare_notification_data(template, event_template, order_data)
#             logger.info(f"NOTIFICATION SENT: {notification_data['title']} - {notification_data['message']}")
#             logger.info(f"Order: {order_data.get('id')}")
#             logger.info(f"User ID: {user_id or 'Unknown'}")
#             return True
#         except Exception as e:
#             logger.error(f"Error sending Shopify notification: {e}")
#             return False
#     def _prepare_notification_data(self, template: Dict[str, Any], event_template: ShopifyEventTemplate, order_data: Dict[str, Any]) -> Dict[str, Any]:
#         notification_data = template.copy()
#         notification_data.update({
#             'order_id': order_data.get('id', ''),
#             'financial_status': order_data.get('financial_status', ''),
#             'fulfillment_status': order_data.get('fulfillment_status', ''),
#             'updated_at': order_data.get('updated_at', ''),
#             'custom_message': event_template.message
#         })
#         return notification_data