#!/usr/bin/env python3
"""
Tool Mocker & Interceptor for Agent Testing.

This module provides:
1. ToolInterceptor — wraps every tool to log calls (name, args, result) without
   modifying behavior. Works with real APIs OR mock data.
2. MockToolDataProvider — returns deterministic fake data for each tool so tests
   don't hit Shopify/Shiprocket/Gupshup APIs.
3. mock_tools_for_agent() — one-call function that patches a tool list with
   intercepted + mocked versions.

The interceptor records every invocation into a shared list that the test runner
inspects after the conversation to verify:
  - Which tools were called (expected_tools, forbidden_tools)
  - What arguments were passed (expected_tool_args)
  - Whether the call sequence matches the baseline (regression check)

Usage:
    from tests.tool_mocker import mock_tools_for_agent, get_tool_call_log, reset_tool_call_log

    # Wrap tools before passing to AgentExecutor
    mocked_tools = mock_tools_for_agent(original_tools, state)
    agent_executor = AgentExecutor(agent=agent, tools=mocked_tools, ...)

    # After conversation, inspect what was called
    log = get_tool_call_log()
    # [{"tool": "get_recent_orders", "args": {...}, "result": {...}, "timestamp": ...}, ...]
"""
import copy
import json
import logging
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from functools import wraps

from langchain_core.tools import BaseTool, StructuredTool

logger = logging.getLogger("tool_mocker")

# ==================== GLOBAL TOOL CALL LOG ====================
# Shared across all intercepted tools within a single test run.
# Must be reset between scenarios.

_TOOL_CALL_LOG: List[Dict[str, Any]] = []


def get_tool_call_log() -> List[Dict[str, Any]]:
    """Return the current tool call log."""
    return list(_TOOL_CALL_LOG)


def reset_tool_call_log():
    """Clear the tool call log (call before each scenario)."""
    _TOOL_CALL_LOG.clear()


def get_tool_call_summary() -> Dict[str, Any]:
    """Return a summary of tool calls for quick assertion."""
    log = get_tool_call_log()
    tool_names = [entry["tool"] for entry in log]
    return {
        "total_calls": len(log),
        "tools_called": tool_names,
        "unique_tools": list(dict.fromkeys(tool_names)),  # Preserves order
        "tool_sequence": " → ".join(tool_names),
        "calls_by_tool": {
            name: [e for e in log if e["tool"] == name]
            for name in dict.fromkeys(tool_names)
        },
    }


# ==================== MOCK DATA PROVIDER ====================

class MockToolDataProvider:
    """
    Provides deterministic mock responses for each tool.
    
    Mock data is keyed by tool name and optionally by specific argument values.
    This allows testing the full agent flow without hitting real APIs.
    
    The mock data is designed to be realistic — matching the actual API response
    formats from Shopify, Shiprocket, and internal tools.
    """

    # Default mock data per tool
    MOCK_RESPONSES: Dict[str, Any] = {
        # ── Order lookup ──
        "get_recent_orders": {
            "default": {
                "total_orders_found": 2,
                "orders": [
                    {
                        "order_id": "GV12345",
                        "order_number": "GV12345",
                        "order_name": "#GV12345",
                        "status": "New",
                        "product": "Sunfire Denim",
                        "size": "32",
                        "amount": 2999,
                        "currency": "INR",
                        "created_at": "2026-01-10T10:00:00Z",
                        "customer_name": "Test User",
                        "customer_email": "test@example.com",
                        "customer_phone": "9876543210",
                        "shipping_address": {
                            "address1": "123 Main St",
                            "city": "Mumbai",
                            "state": "Maharashtra",
                            "zip": "400001",
                        },
                        "can_cancel": True,
                        "phone_validated": True,
                    },
                    {
                        "order_id": "GV12346",
                        "order_number": "GV12346",
                        "order_name": "#GV12346",
                        "status": "Processing",
                        "product": "Evolve Shacket",
                        "size": "L",
                        "amount": 1999,
                        "currency": "INR",
                        "created_at": "2026-01-12T14:30:00Z",
                        "customer_name": "Test User",
                        "customer_email": "test@example.com",
                        "customer_phone": "9876543210",
                        "can_cancel": True,
                        "phone_validated": True,
                    },
                ],
                "phone_validated": True,
            },
        },

        # ── Order status ──
        "get_order_status_details": {
            "default": {
                "total_orders_found": 1,
                "orders": [
                    {
                        "order_id": "GV12345",
                        "order_number": "GV12345",
                        "status": "New",
                        "product": "Sunfire Denim",
                        "size": "32",
                        "amount": 2999,
                        "tracking_number": None,
                        "expected_delivery": None,
                        "shipping_status": "Pending",
                    }
                ],
                "phone_validated": True,
                "escalation": {
                    "needs_escalation": False,
                    "escalation_type": None,
                    "reason": None,
                },
            },
            "access_denied": {
                "error": "access_denied",
                "message": "Customer phone number not available. Please ask customer for their phone number.",
                "phone_validated": False,
            },
        },

        "get_order_details": {
            "default": {
                "success": True,
                "order_id": "GV12345",
                "shopify_order_id": "GV12345",
                "status": "paid",
                "fulfillment_status": "unfulfilled",
                "total_price": "2999",
                "customer": {"name": "Test User", "email": "test@example.com", "phone": "9876543210"},
                "shipping_address": {},
                "line_items": [{"name": "Sunfire Denim - 32", "quantity": 1, "price": "2999", "sku": None, "variant_title": "32"}],
                "tags": "",
                "note": None,
                "tracking": {"awb": None, "courier": None, "expected_delivery": None, "current_location": None},
                "escalation": {"needs_escalation": False, "escalation_type": None, "reason": None},
                "phone_validated": True,
            },
        },

        # ── Order updates ──
        "update_order_address": {
            "default": {
                "success": True,
                "message": "Address updated successfully for order GV12345.",
                "phone_validated": True,
            },
        },

        "update_order_size_tool": {
            "default": {
                "success": True,
                "message": "Order variant updated successfully to {new_variant} for order GV12345.",
                "phone_validated": True,
            },
        },

        "update_order_phone_number_tool": {
            "default": {
                "success": True,
                "message": "Phone number updated successfully for order GV12345.",
                "phone_validated": True,
            },
        },

        "update_order_email_tool": {
            "default": {
                "success": True,
                "message": "Email updated successfully for order GV12345.",
                "phone_validated": True,
            },
        },

        "annotate_order": {
            "default": {
                "success": True,
                "phone_validated": True,
                "note_result": {"success": True, "message": "Note added to order GV12345."},
                "tags_result": {"success": True, "message": "Tags added to order GV12345."},
            },
        },

        # ── Cancellation ──
        "cancel_order_tool": {
            "default": {
                "success": True,
                "message": "Order GV12345 cancelled successfully in Shopify.",
                "phone_validated": True,
            },
        },

        "escalate_to_agent": {
            "default": {
                "success": True,
                "message": "Escalation triggered. A support representative will contact the customer.",
            },
        },

        # ── Product change ──
        "change_order_product_tool": {
            "default": {
                "success": True,
                "old_order_id": "GV12345",
                "new_order_id": "GV12350",
                "differential_amount": 0,
                "message": "Product changed successfully. Old order GV12345 cancelled, new order GV12350 created.",
                "phone_validated": True,
            },
            "differential": {
                "success": True,
                "old_order_id": "GV12345",
                "new_order_id": "GV12350",
                "differential_amount": 300,
                "message": "Product change has a price difference of ₹300.",
                "phone_validated": True,
            },
        },

        "search_products_by_name": {
            "default": {
                "products_found": 2,
                "products": [
                    {
                        "title": "Silicone Case - iPhone 17 Pro Max - Black",
                        "handle": "silicone-case-iphone-17-pro-max-black",
                        "url": "https://example.com/products/silicone-case-iphone-17-pro-max-black",
                        "price": 999,
                    },
                    {
                        "title": "Silicone Case - iPhone 17 Pro Max - Blue",
                        "handle": "silicone-case-iphone-17-pro-max-blue",
                        "url": "https://example.com/products/silicone-case-iphone-17-pro-max-blue",
                        "price": 999,
                    },
                ],
            },
        },

        # ── Return/Exchange ──
        "get_final_return_exchange_message": {
            "default": {
                "message": (
                    "To initiate your return, please visit: https://example.com/returns\n"
                    "Or contact us at +91 8882174388 (Mon-Sat, 11:30 AM to 6:30 PM)\n"
                    "Email: support@example.com"
                ),
            },
        },

        # ── Product details (for product_details agent) ──
        "search_product_by_name": {
            "default": {
                "products_found": 1,
                "products": [
                    {
                        "title": "Sunfire Denim",
                        "handle": "sunfire-denim",
                        "price": 2999,
                        "available_sizes": ["28", "30", "32", "34"],
                        "description": "Premium denim with comfort fit.",
                    }
                ],
            },
        },

        "get_product_details": {
            "default": {
                "title": "Sunfire Denim",
                "price": 2999,
                "available_sizes": ["28", "30", "32", "34"],
                "available_colors": ["blue", "black"],
                "description": "Premium denim with comfort fit.",
                "in_stock": True,
            },
        },

        # ── Shipping update (shared between agents) ──
        "update_shipping_order": {
            "default": {
                "success": True,
                "message": "Shipping order updated successfully.",
            },
        },
    }

    # Per-scenario overrides (scenario_id → tool_name → mock_variant)
    _scenario_overrides: Dict[str, Dict[str, str]] = {}

    @classmethod
    def set_scenario_overrides(cls, scenario_id: str, overrides: Dict[str, str]):
        """
        Set per-scenario mock variants.
        
        Example:
            MockToolDataProvider.set_scenario_overrides("ctx-1", {
                "get_order_details": "access_denied",
            })
        """
        cls._scenario_overrides[scenario_id] = overrides

    @classmethod
    def clear_scenario_overrides(cls):
        cls._scenario_overrides.clear()

    @classmethod
    def get_mock_response(
        cls,
        tool_name: str,
        args: Dict[str, Any] = None,
        scenario_id: str = None,
    ) -> Any:
        """
        Get mock response for a tool call.
        
        Resolution order:
        1. Per-scenario override variant
        2. Arg-based variant matching
        3. Default response
        
        If tool not in MOCK_RESPONSES, returns a generic success.
        """
        tool_mocks = cls.MOCK_RESPONSES.get(tool_name, {})

        # 1. Scenario override
        if scenario_id and scenario_id in cls._scenario_overrides:
            variant = cls._scenario_overrides[scenario_id].get(tool_name)
            if variant and variant in tool_mocks:
                response = copy.deepcopy(tool_mocks[variant])
                return cls._interpolate_response(response, args or {})

        # 2. Check if phone_number is missing → use access_denied variant
        if args and not args.get("phone") and "access_denied" in tool_mocks:
            # Only for order tools that do phone validation
            pass  # Keep it simple — let the mock return default

        # 3. Default
        if "default" in tool_mocks:
            response = copy.deepcopy(tool_mocks["default"])
            return cls._interpolate_response(response, args or {})

        # Fallback: if tool has direct value (not dict with variants)
        if tool_mocks and not isinstance(tool_mocks, dict):
            return copy.deepcopy(tool_mocks)

        # Unknown tool — return generic success
        return {"success": True, "message": f"Mock: {tool_name} executed successfully."}

    @classmethod
    def _interpolate_response(cls, response: Any, args: Dict[str, Any]) -> Any:
        """Replace {arg_name} placeholders in mock responses with actual args."""
        if isinstance(response, str):
            for key, val in args.items():
                response = response.replace(f"{{{key}}}", str(val))
            return response
        elif isinstance(response, dict):
            return {k: cls._interpolate_response(v, args) for k, v in response.items()}
        elif isinstance(response, list):
            return [cls._interpolate_response(item, args) for item in response]
        return response


# ==================== TOOL INTERCEPTOR ====================

def _create_intercepted_func(
    original_func: Callable,
    tool_name: str,
    use_mock: bool = True,
    scenario_id: str = None,
) -> Callable:
    """
    Create an intercepted version of a tool function.
    
    - Logs every call (name, args, result, timing) to _TOOL_CALL_LOG
    - If use_mock=True, returns mock data instead of calling the real function
    - If use_mock=False, calls the real function but still logs the call
    """

    @wraps(original_func)
    def intercepted(*args, **kwargs):
        call_entry = {
            "tool": tool_name,
            "args": _serialize_args(kwargs if kwargs else (args[0] if args else {})),
            "timestamp": datetime.now().isoformat(),
            "mock": use_mock,
        }

        start = time.time()
        try:
            if use_mock:
                result = MockToolDataProvider.get_mock_response(
                    tool_name, kwargs or {}, scenario_id
                )
            else:
                result = original_func(*args, **kwargs)

            call_entry["result"] = _safe_serialize(result)
            call_entry["success"] = True
            call_entry["duration_ms"] = round((time.time() - start) * 1000, 2)
        except Exception as e:
            call_entry["result"] = str(e)
            call_entry["success"] = False
            call_entry["error"] = str(e)
            call_entry["duration_ms"] = round((time.time() - start) * 1000, 2)
            raise
        finally:
            _TOOL_CALL_LOG.append(call_entry)
            logger.info(
                f"🔧 [{tool_name}] args={json.dumps(call_entry['args'], default=str)[:200]} "
                f"mock={use_mock} success={call_entry.get('success', False)}"
            )

        return result

    return intercepted


def _serialize_args(args: Any) -> Any:
    """Serialize args for logging (handles non-serializable types)."""
    if isinstance(args, dict):
        return {k: _safe_serialize(v) for k, v in args.items()}
    return _safe_serialize(args)


def _safe_serialize(obj: Any) -> Any:
    """Safely serialize an object for JSON storage."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: _safe_serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_safe_serialize(item) for item in obj]
    # For complex objects, convert to string (truncated)
    s = str(obj)
    return s[:500] if len(s) > 500 else s


# ==================== PUBLIC API ====================

def mock_tools_for_agent(
    tools: List[BaseTool],
    state: Dict[str, Any] = None,
    use_mock: bool = True,
    scenario_id: str = None,
) -> List[BaseTool]:
    """
    Wrap a list of LangChain tools with intercepted+mocked versions.
    
    This is the main entry point. Call this before passing tools to AgentExecutor.
    
    Args:
        tools: Original list of LangChain tools from the tool factory
        state: Current conversation state (for context in mock responses)
        use_mock: If True, tools return mock data. If False, calls real APIs but logs calls.
        scenario_id: Optional scenario ID for per-scenario mock overrides
        
    Returns:
        New list of tools with intercepted functions
    """
    intercepted_tools = []

    for tool in tools:
        tool_name = tool.name
        original_func = tool.func if hasattr(tool, "func") else tool._run

        intercepted_func = _create_intercepted_func(
            original_func, tool_name, use_mock=use_mock, scenario_id=scenario_id
        )

        # Create a new StructuredTool that preserves the original's metadata
        new_tool = StructuredTool(
            name=tool.name,
            description=tool.description,
            func=intercepted_func,
            args_schema=getattr(tool, "args_schema", None),
        )
        intercepted_tools.append(new_tool)

    logger.info(
        f"🔧 Intercepted {len(intercepted_tools)} tools "
        f"(mock={'ON' if use_mock else 'OFF'}, scenario={scenario_id or 'N/A'})"
    )
    return intercepted_tools


# ==================== TOOL CALL VERIFICATION ====================

class ToolCallVerifier:
    """
    Verify that the agent called the correct tools with correct arguments.
    
    This replaces the state-inspection-based verifier with a direct call-log
    based approach — much more reliable since it sees EVERY tool invocation.
    """

    @staticmethod
    def verify(
        expectations: Dict[str, Any],
        log: List[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Verify tool call expectations against the actual call log.
        
        Args:
            expectations: {
                "expected_tools": ["get_recent_orders", "escalate_to_agent"],
                "forbidden_tools": ["cancel_order_tool"],
                "expected_tool_args": {
                    "cancel_order_tool": {"cancellation_reason": "wrong_size"}
                },
                "expected_tool_sequence": ["extract_phone_from_message", "get_recent_orders"],
                "min_tool_calls": 1,
                "max_tool_calls": 10,
            }
            log: Tool call log (defaults to global log)
            
        Returns:
            {
                "passed": bool,
                "expected_found": [...],
                "expected_missing": [...],
                "forbidden_detected": [...],
                "arg_mismatches": [...],
                "sequence_match": bool,
                "total_calls": int,
                "details": "..."
            }
        """
        if log is None:
            log = get_tool_call_log()

        tools_called = [entry["tool"] for entry in log]
        unique_tools = list(dict.fromkeys(tools_called))

        result = {
            "passed": True,
            "total_calls": len(log),
            "tools_called": tools_called,
            "unique_tools": unique_tools,
            "expected_found": [],
            "expected_missing": [],
            "forbidden_detected": [],
            "arg_mismatches": [],
            "sequence_match": True,
            "details": [],
        }

        # 1. Check expected tools
        for expected_tool in expectations.get("expected_tools", []):
            if expected_tool in tools_called:
                result["expected_found"].append(expected_tool)
            else:
                result["expected_missing"].append(expected_tool)
                result["passed"] = False
                result["details"].append(f"❌ Expected tool '{expected_tool}' was NOT called")

        # 2. Check forbidden tools
        for forbidden_tool in expectations.get("forbidden_tools", []):
            if forbidden_tool in tools_called:
                result["forbidden_detected"].append(forbidden_tool)
                result["passed"] = False
                result["details"].append(
                    f"🔴 FORBIDDEN tool '{forbidden_tool}' was called! "
                    f"(called {tools_called.count(forbidden_tool)} time(s))"
                )

        # 3. Check tool arguments
        expected_args = expectations.get("expected_tool_args", {})
        for tool_name, expected_arg_vals in expected_args.items():
            matching_calls = [e for e in log if e["tool"] == tool_name]
            if not matching_calls:
                result["arg_mismatches"].append({
                    "tool": tool_name,
                    "expected_args": expected_arg_vals,
                    "actual": "Tool not called",
                })
                result["passed"] = False
                continue

            # Check last call's args
            actual_args = matching_calls[-1].get("args", {})
            for arg_name, expected_val in expected_arg_vals.items():
                actual_val = actual_args.get(arg_name)
                if actual_val != expected_val:
                    result["arg_mismatches"].append({
                        "tool": tool_name,
                        "arg": arg_name,
                        "expected": expected_val,
                        "actual": actual_val,
                    })
                    # Don't fail on arg mismatches — they're informational
                    result["details"].append(
                        f"⚠️ Arg mismatch in '{tool_name}': "
                        f"{arg_name}={actual_val} (expected: {expected_val})"
                    )

        # 4. Check tool call sequence (if specified)
        expected_sequence = expectations.get("expected_tool_sequence", [])
        if expected_sequence:
            # Check if expected sequence appears as a subsequence
            seq_idx = 0
            for called in tools_called:
                if seq_idx < len(expected_sequence) and called == expected_sequence[seq_idx]:
                    seq_idx += 1
            if seq_idx < len(expected_sequence):
                result["sequence_match"] = False
                result["details"].append(
                    f"⚠️ Expected sequence {expected_sequence} not found in {tools_called}"
                )

        # 5. Check call count bounds
        min_calls = expectations.get("min_tool_calls")
        max_calls = expectations.get("max_tool_calls")
        if min_calls is not None and len(log) < min_calls:
            result["details"].append(f"⚠️ Too few tool calls: {len(log)} < {min_calls}")
        if max_calls is not None and len(log) > max_calls:
            result["details"].append(f"⚠️ Too many tool calls: {len(log)} > {max_calls}")

        return result


# ==================== HELPER: DESCRIBE AVAILABLE MOCKS ====================

def list_available_mocks() -> List[str]:
    """Return list of tool names that have mock data defined."""
    return list(MockToolDataProvider.MOCK_RESPONSES.keys())


def get_mock_variants(tool_name: str) -> List[str]:
    """Return list of available variants for a given tool mock."""
    mocks = MockToolDataProvider.MOCK_RESPONSES.get(tool_name, {})
    if isinstance(mocks, dict):
        return list(mocks.keys())
    return ["default"]

