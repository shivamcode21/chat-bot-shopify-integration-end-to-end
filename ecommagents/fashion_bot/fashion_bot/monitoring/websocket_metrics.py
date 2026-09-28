"""
WebSocket connection metrics and monitoring

Real-time tracking of active connections, messages, errors, and session health.
Thread-safe and production-ready.
Emits metrics via OpenTelemetry for Grafana integration.
"""
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, List
from dataclasses import dataclass, asdict
from enum import Enum

# OpenTelemetry imports
from opentelemetry import metrics as otel_metrics

logger = logging.getLogger(__name__)


class ConnectionStatus(str, Enum):
    """Connection status enum"""
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    IDLE = "idle"
    ERROR = "error"


@dataclass
class ConnectionMetrics:
    """Track individual connection metrics"""
    session_id: str
    phone_number: Optional[str]
    connected_at: str  # ISO format timestamp
    last_message_at: str  # ISO format timestamp
    message_count: int = 0
    error_count: int = 0
    status: str = ConnectionStatus.CONNECTED.value
    
    def get_duration_seconds(self) -> float:
        """Get connection duration in seconds"""
        connected = datetime.fromisoformat(self.connected_at)
        return (datetime.now(timezone.utc) - connected).total_seconds()
    
    def is_idle(self, idle_threshold_seconds: int = 300) -> bool:
        """Check if connection is idle (no messages for X seconds)"""
        last_msg = datetime.fromisoformat(self.last_message_at)
        time_since_last_msg = (datetime.now(timezone.utc) - last_msg).total_seconds()
        return time_since_last_msg > idle_threshold_seconds
    
    def mark_disconnected(self):
        """Mark connection as disconnected"""
        self.status = ConnectionStatus.DISCONNECTED.value
    
    def mark_error(self):
        """Mark connection as error"""
        self.status = ConnectionStatus.ERROR.value


class WebSocketMetricsCollector:
    """
    Collect and track WebSocket connection metrics
    Thread-safe singleton for production use
    Emits metrics via OpenTelemetry for Grafana OTLP integration
    """
    
    def __init__(self):
        self.connections: Dict[str, ConnectionMetrics] = {}
        self.total_connections_ever = 0
        self.total_messages_processed = 0
        self.total_errors = 0
        self.start_time = datetime.now(timezone.utc)
        self._lock = threading.RLock()
        
        # Initialize OpenTelemetry Meter for metrics export
        try:
            self.meter = otel_metrics.get_meter(__name__)
            
            # Create OTel counters and gauges for OTLP export
            self._connections_total_counter = self.meter.create_counter(
                "websocket.connections.total",
                description="Total WebSocket connections created",
                unit="1"
            )
            self._messages_total_counter = self.meter.create_counter(
                "websocket.messages.total",
                description="Total WebSocket messages processed",
                unit="1"
            )
            self._errors_total_counter = self.meter.create_counter(
                "websocket.errors.total",
                description="Total WebSocket errors",
                unit="1"
            )
            self._connection_duration = self.meter.create_histogram(
                "websocket.connection.duration_seconds",
                description="WebSocket connection lifetime (accept -> disconnect)",
                unit="s",
            )
            self._accept_duration = self.meter.create_histogram(
                "websocket.connection.accept_duration",
                description="Time spent in websocket.accept() handshake",
                unit="ms",
            )
            
            logger.info("✅ WebSocketMetricsCollector initialized with OTel metrics enabled")
        except Exception as e:
            logger.warning(f"⚠️ OpenTelemetry initialization: {e} (metrics will work locally)")
            self.meter = None
    
    def record_accept_duration(self, duration_ms: float, status: str = "ok") -> None:
        """Record how long the WebSocket handshake took (server-side accept)."""
        if not self.meter:
            return
        try:
            self._accept_duration.record(duration_ms, {"status": status})
        except Exception as e:
            logger.debug(f"OTel accept-duration emit: {e}")

    def track_connection(self, session_id: str, phone_number: Optional[str] = None):
        """Track a new connection and emit OTel metric"""
        with self._lock:
            now = datetime.now(timezone.utc).isoformat()
            self.connections[session_id] = ConnectionMetrics(
                session_id=session_id,
                phone_number=phone_number,
                connected_at=now,
                last_message_at=now
            )
            self.total_connections_ever += 1
            active_count = len(self.connections)
            
            # Emit OTel counter for new connection
            if self.meter:
                try:
                    self._connections_total_counter.add(1, {"status": "new"})
                except Exception as e:
                    logger.debug(f"OTel counter emit: {e}")
            
            logger.info(f"📊 Connection tracked: {session_id} | Active: {active_count}/{self.total_connections_ever}")
    
    def record_message(self, session_id: str):
        """Record a message from the connection and emit OTel metric"""
        with self._lock:
            if session_id in self.connections:
                self.connections[session_id].message_count += 1
                self.connections[session_id].last_message_at = datetime.now(timezone.utc).isoformat()
                self.total_messages_processed += 1
                
                # Emit OTel counter for message
                if self.meter:
                    try:
                        self._messages_total_counter.add(1)
                    except Exception as e:
                        logger.debug(f"OTel counter emit: {e}")
    
    def record_error(self, session_id: str):
        """Record an error on the connection and emit OTel metric"""
        with self._lock:
            if session_id in self.connections:
                self.connections[session_id].error_count += 1
                self.total_errors += 1
            else:
                self.total_errors += 1
            
            # Emit OTel counter for error
            if self.meter:
                try:
                    self._errors_total_counter.add(1, {"session_id": session_id or "unknown"})
                except Exception as e:
                    logger.debug(f"OTel counter emit: {e}")
            
            logger.warning(f"⚠️ Error recorded for {session_id} | Total errors: {self.total_errors}")
    
    def disconnect_connection(self, session_id: str):
        """Mark connection as disconnected and record connection-lifetime histogram."""
        with self._lock:
            conn = self.connections.pop(session_id, None)
            if conn:
                duration_seconds = conn.get_duration_seconds()
                conn.mark_disconnected()
                remaining = len(self.connections)
                logger.info(f"📊 Connection disconnected: {session_id} | Remaining: {remaining}")

                if self.meter:
                    try:
                        self._connection_duration.record(
                            duration_seconds,
                            {"status": "ok" if conn.error_count == 0 else "error"},
                        )
                    except Exception as e:
                        logger.debug(f"OTel histogram emit: {e}")
    
    def get_metrics(self) -> Dict:
        """Get overall metrics"""
        with self._lock:
            active_connections = len(self.connections)
            idle_connections = sum(1 for c in self.connections.values() if c.is_idle())
            total_messages = sum(c.message_count for c in self.connections.values())
            total_errors = sum(c.error_count for c in self.connections.values())
            
            uptime = datetime.now(timezone.utc) - self.start_time
            uptime_seconds = uptime.total_seconds()
            
            # Calculate rates
            avg_messages_per_connection = total_messages / active_connections if active_connections > 0 else 0
            messages_per_second = total_messages / uptime_seconds if uptime_seconds > 0 else 0
            
            return {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "active_connections": active_connections,
                "idle_connections": idle_connections,
                "connected_connections": active_connections - idle_connections,
                "total_connections_ever": self.total_connections_ever,
                "total_messages": total_messages,
                "total_errors": total_errors,
                "uptime_seconds": uptime_seconds,
                "avg_messages_per_connection": round(avg_messages_per_connection, 2),
                "messages_per_second": round(messages_per_second, 2),
                "error_rate": round((total_errors / total_messages * 100) if total_messages > 0 else 0, 2),
                "capacity_usage_percent": round((active_connections / 5000 * 100), 2),  # Assuming 5K per instance
            }
    
    def get_connection_details(self, limit: int = 100) -> List[Dict]:
        """Get detailed info on individual connections"""
        with self._lock:
            details = []
            for conn in list(self.connections.values())[:limit]:
                details.append({
                    "session_id": conn.session_id,
                    "phone_number": conn.phone_number or "N/A",
                    "connected_seconds": round(conn.get_duration_seconds(), 2),
                    "message_count": conn.message_count,
                    "error_count": conn.error_count,
                    "status": conn.status,
                    "is_idle": conn.is_idle(),
                    "last_message_at": conn.last_message_at,
                })
            return details
    
    def get_health_status(self) -> str:
        """Get overall health status"""
        metrics = self.get_metrics()
        
        # Determine health status
        if metrics["capacity_usage_percent"] > 80:
            return "critical"  # Nearing capacity
        elif metrics["capacity_usage_percent"] > 60:
            return "warning"  # Should consider scaling
        elif metrics["error_rate"] > 5:
            return "degraded"  # High error rate
        elif metrics["error_rate"] > 1:
            return "warning"  # Moderate error rate
        else:
            return "healthy"
    
    def reset_metrics(self):
        """Reset all metrics (for testing only)"""
        with self._lock:
            self.connections.clear()
            self.total_messages_processed = 0
            self.total_errors = 0
            logger.warning("🔄 Metrics reset")


# Global metrics collector singleton
_metrics_collector: Optional[WebSocketMetricsCollector] = None
_collector_lock = threading.Lock()


def get_metrics_collector() -> WebSocketMetricsCollector:
    """Get or create global metrics collector (thread-safe singleton)"""
    global _metrics_collector
    
    if _metrics_collector is None:
        with _collector_lock:
            if _metrics_collector is None:
                _metrics_collector = WebSocketMetricsCollector()
    
    return _metrics_collector
