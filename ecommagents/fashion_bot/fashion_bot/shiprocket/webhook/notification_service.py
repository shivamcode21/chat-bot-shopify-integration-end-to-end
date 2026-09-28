"""
Notification service for ShipRocket webhook events.
Handles sending templates to users based on shipment events.
"""

import logging
import asyncio
from typing import Dict, Any, Optional
from datetime import datetime
from fashion_bot.shiprocket.webhook.event_config import EventTemplate

logger = logging.getLogger(__name__)

class NotificationService:
    """Service for sending notifications to users"""
    
    def __init__(self):
        self.template_cache = {}
        self._load_templates()
    
    def _load_templates(self):
        """Load notification templates"""
        self.templates = {
            "out_for_delivery": {
                "title": "🚚 Out for Delivery",
                "message": "Your order is out for delivery!",
                "subtitle": "Your package is on its way to you",
                "action_text": "Track Package",
                "priority": "high"
            },
            "delivery_completed": {
                "title": "✅ Delivery Completed",
                "message": "Your order has been delivered successfully!",
                "subtitle": "Thank you for shopping with us",
                "action_text": "Rate Order",
                "priority": "high"
            },
            "delivery_attempted": {
                "title": "🔄 Delivery Attempted",
                "message": "Delivery was attempted but you weren't available",
                "subtitle": "We'll try again soon",
                "action_text": "Reschedule",
                "priority": "medium"
            },
            "order_returned": {
                "title": "📦 Order Returned",
                "message": "Your order has been returned to our facility",
                "subtitle": "Please contact support for assistance",
                "action_text": "Contact Support",
                "priority": "medium"
            },
            "order_cancelled": {
                "title": "💔 Order Cancelled",
                "message": "Your order has been cancelled",
                "subtitle": "Refund will be processed within 5-7 days",
                "action_text": "View Refund Status",
                "priority": "medium"
            },
            "in_transit": {
                "title": "🚛 In Transit",
                "message": "Your order is in transit and on its way to you!",
                "subtitle": "Expected delivery: {etd}",
                "action_text": "Track Package",
                "priority": "low"
            },
            "picked_up": {
                "title": "📦 Picked Up",
                "message": "Your order has been picked up and is on its way!",
                "subtitle": "Package is now in transit",
                "action_text": "Track Package",
                "priority": "medium"
            },
            "manifest_generated": {
                "title": "📋 Manifest Generated",
                "message": "Your order has been processed and is ready for shipping!",
                "subtitle": "Package will be picked up soon",
                "action_text": "Track Package",
                "priority": "low"
            },
            "shipped": {
                "title": "🚢 Order Shipped",
                "message": "Your order has been shipped!",
                "subtitle": "Package is now in transit",
                "action_text": "Track Package",
                "priority": "medium"
            },
            "cod_order_done": {
                "title": "💰 COD Order Completed",
                "message": "Your COD order has been completed successfully!",
                "subtitle": "Payment received and order confirmed",
                "action_text": "View Order",
                "priority": "high"
            },
            "delivered": {
                "title": "Order Delivered",
                "message": "Your order has been delivered successfully! ✅",
                "priority": "high",
                "subtitle": "Your package is delivered",
                "action_text": "Delivered Package",
            }
        }
    
    async def send_notification(self, event_template: EventTemplate, 
                              order_data: Dict[str, Any], 
                              user_id: Optional[str] = None) -> bool:
        """
        Send notification to user based on event template.
        
        Args:
            event_template: The event template to use
            order_data: The order data from webhook
            user_id: Optional user ID for targeted notifications
            
        Returns:
            True if notification sent successfully, False otherwise
        """
        try:
            template_id = event_template.template_id
            
            if template_id not in self.templates:
                logger.warning(f"Template {template_id} not found")
                return False
            
            template = self.templates[template_id]
            
            # Prepare notification data
            notification_data = self._prepare_notification_data(template, event_template, order_data)
            
            # Send notification based on priority and type
            success = await self._send_notification_by_priority(
                notification_data, 
                template.get('priority', 'medium'),
                user_id
            )
            
            if success:
                logger.info(f"Notification sent successfully for {template_id} - Order: {order_data.get('order_id')}")
            else:
                logger.error(f"Failed to send notification for {template_id} - Order: {order_data.get('order_id')}")
            
            return success
            
        except Exception as e:
            logger.error(f"Error sending notification: {e}")
            return False
    
    def _prepare_notification_data(self, template: Dict[str, Any], 
                                 event_template: EventTemplate, 
                                 order_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Prepare notification data by filling template variables.
        
        Args:
            template: The template dictionary
            event_template: The event template
            order_data: The order data
            
        Returns:
            Prepared notification data
        """
        notification_data = template.copy()
        
        # Replace template variables
        if 'etd' in order_data:
            etd = order_data['etd']
            if etd:
                try:
                    # Parse and format ETD
                    if " " in etd:
                        parsed_etd = datetime.strptime(etd, "%Y-%m-%d %H:%M:%S")
                    else:
                        parsed_etd = datetime.fromisoformat(etd.replace('Z', '+00:00'))
                    
                    formatted_etd = parsed_etd.strftime("%B %d, %Y")
                    notification_data['subtitle'] = notification_data['subtitle'].replace('{etd}', formatted_etd)
                except:
                    notification_data['subtitle'] = notification_data['subtitle'].replace('{etd}', 'soon')
        
        # Add order-specific information
        notification_data.update({
            'order_id': order_data.get('order_id', ''),
            'awb': order_data.get('awb', ''),
            'courier_name': order_data.get('courier_name', ''),
            'current_status': order_data.get('current_status', ''),
            'shipment_status': order_data.get('shipment_status', ''),
            'timestamp': order_data.get('current_timestamp', ''),
            'custom_message': event_template.message
        })
        
        return notification_data
    
    async def _send_notification_by_priority(self, notification_data: Dict[str, Any], 
                                           priority: str, user_id: Optional[str] = None) -> bool:
        """
        Send notification based on priority level.
        
        Args:
            notification_data: The notification data
            priority: The priority level (high, medium, low)
            user_id: Optional user ID
            
        Returns:
            True if sent successfully, False otherwise
        """
        try:
            # For high priority notifications, send immediately
            if priority == "high":
                return await self._send_immediate_notification(notification_data, user_id)
            
            # For medium priority, send with slight delay
            elif priority == "medium":
                await asyncio.sleep(1)  # Small delay
                return await self._send_immediate_notification(notification_data, user_id)
            
            # For low priority, send with longer delay
            else:
                await asyncio.sleep(5)  # Longer delay for low priority
                return await self._send_immediate_notification(notification_data, user_id)
                
        except Exception as e:
            logger.error(f"Error in priority-based notification: {e}")
            return False
    
    async def _send_immediate_notification(self, notification_data: Dict[str, Any], 
                                         user_id: Optional[str] = None) -> bool:
        """
        Send immediate notification to user.
        
        Args:
            notification_data: The notification data
            user_id: Optional user ID
            
        Returns:
            True if sent successfully, False otherwise
        """
        try:
            # Here you would integrate with your actual notification system
            # This could be WhatsApp, email, push notification, etc.
            
            # For now, we'll log the notification
            logger.info(f"NOTIFICATION SENT: {notification_data['title']} - {notification_data['message']}")
            logger.info(f"Order: {notification_data['order_id']}, AWB: {notification_data['awb']}")
            logger.info(f"User ID: {user_id or 'Unknown'}")
            
            # TODO: Integrate with actual notification channels:
            # - WhatsApp API
            # - Email service
            # - Push notifications
            # - SMS service
            
            # Example integration points:
            # await self._send_whatsapp_notification(notification_data, user_id)
            # await self._send_email_notification(notification_data, user_id)
            # await self._send_push_notification(notification_data, user_id)
            
            return True
            
        except Exception as e:
            logger.error(f"Error sending immediate notification: {e}")
            return False
    
    async def send_bulk_notifications(self, notifications: list) -> Dict[str, int]:
        """
        Send multiple notifications in bulk.
        
        Args:
            notifications: List of notification data
            
        Returns:
            Dictionary with success/failure counts
        """
        results = {
            'success': 0,
            'failed': 0,
            'total': len(notifications)
        }
        
        for notification in notifications:
            try:
                success = await self.send_notification(
                    notification['event_template'],
                    notification['order_data'],
                    notification.get('user_id')
                )
                
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
                    
            except Exception as e:
                logger.error(f"Error in bulk notification: {e}")
                results['failed'] += 1
        
        return results
    
    def get_template_preview(self, template_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a preview of a template.
        
        Args:
            template_id: The template ID
            
        Returns:
            Template preview data or None
        """
        if template_id in self.templates:
            return self.templates[template_id].copy()
        return None
    
    def update_template(self, template_id: str, template_data: Dict[str, Any]) -> bool:
        """
        Update an existing template.
        
        Args:
            template_id: The template ID
            template_data: The new template data
            
        Returns:
            True if updated successfully, False otherwise
        """
        try:
            if template_id in self.templates:
                self.templates[template_id].update(template_data)
                logger.info(f"Template {template_id} updated successfully")
                return True
            else:
                logger.warning(f"Template {template_id} not found for update")
                return False
                
        except Exception as e:
            logger.error(f"Error updating template {template_id}: {e}")
            return False 