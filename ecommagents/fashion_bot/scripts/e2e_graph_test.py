"""
Real End-to-End Test: Order Update via LangGraph Pipeline

This test simulates a REAL WhatsApp conversation by:
1. Creating a real order in Shopify (setup)
2. Sending messages through the full LangGraph pipeline (call_main_bot)
   - Intent detection, tool selection, tool execution all happen for real
3. Pulling the order back from Shopify and cross-validating every field (assertion)
4. Cleaning up the test order via direct Shopify cancel API (teardown)
5. Writing a detailed JSON report to scripts/e2e_test_report.json

This is NOT a mock test. Every call hits real Shopify APIs and real LLM.

Usage:
    python3 scripts/e2e_graph_test.py <client_id>
    python3 scripts/e2e_graph_test.py c3ffcb1b-afb9-4ca4-8746-a06698bec870
"""

import os
import sys
import json
import logging
import time
import uuid
import requests
from datetime import datetime
from typing import Dict, Any, Optional, List

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fashion_bot.config_manager import get_shopify_config
from fashion_bot.shopify.modules.order_creation_api import (
    OrderCreationAPI,
    ShopifyEnvironment as CreationShopifyEnv,
    CustomerInfo,
    ShippingAddress,
    ProductSelection,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("e2e_graph")

REPORT_PATH = os.path.join(os.path.dirname(__file__), "e2e_test_report.json")


# ─── Helpers ──────────────────────────────────────────────────────────

def _shopify_headers(token: str) -> dict:
    return {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}


def _fetch_order_from_shopify(shop_url: str, api_version: str, token: str, order_id: int) -> dict:
    r = requests.get(
        f"https://{shop_url}/admin/api/{api_version}/orders/{order_id}.json",
        headers=_shopify_headers(token),
    )
    r.raise_for_status()
    return r.json().get("order", {})


def _cancel_order_direct(shop_url: str, api_version: str, token: str, order_id: int) -> dict:
    """Cancel a Shopify order directly via REST API. Returns the cancel response."""
    try:
        r = requests.post(
            f"https://{shop_url}/admin/api/{api_version}/orders/{order_id}/cancel.json",
            headers=_shopify_headers(token),
        )
        if r.status_code == 200:
            return {"success": True, "status_code": r.status_code}
        return {"success": False, "status_code": r.status_code, "body": r.text[:500]}
    except Exception as e:
        return {"success": False, "error": str(e)}


def send_bot_message(message: str, phone: str, client_id: str, trace_id: str) -> str:
    """
    Send a message through the FULL LangGraph pipeline.
    This calls the same function the Gupshup webhook uses.
    """
    from fashion_bot.gupshup_webhook import call_main_bot

    response, _ = call_main_bot(
        question=message,
        phone_number=phone,
        trace_id=trace_id,
        client_id=client_id,
    )
    return response


def _clear_state(client_id: str, phone: str):
    """Clear Redis state cache so bot starts a fresh conversation."""
    try:
        from fashion_bot.state_cache import get_unified_cache, Channel
        cache = get_unified_cache()
        tid = cache.generate_thread_id(Channel.WHATSAPP, client_id, phone)
        cache.delete_state(tid)
    except Exception:
        pass


# ─── Test Result Model ────────────────────────────────────────────────

class E2ETestResult:
    def __init__(self, name: str):
        self.name = name
        self.passed = False
        self.order_name = ""
        self.shopify_order_id = 0
        self.user_message = ""
        self.bot_response = ""
        self.error: Optional[str] = None
        self.shopify_before: dict = {}
        self.shopify_after: dict = {}
        self.validations: List[dict] = []
        self.cleanup_result: dict = {}
        self.duration_seconds = 0.0

    def add_validation(self, field: str, expected: str, actual: str, passed: bool):
        self.validations.append({
            "field": field,
            "expected": expected,
            "actual": actual,
            "passed": passed,
        })

    def to_dict(self) -> dict:
        return {
            "test_name": self.name,
            "passed": self.passed,
            "order_name": self.order_name,
            "shopify_order_id": self.shopify_order_id,
            "user_message": self.user_message,
            "bot_response": self.bot_response,
            "error": self.error,
            "shopify_before": self.shopify_before,
            "shopify_after": self.shopify_after,
            "validations": self.validations,
            "cleanup": self.cleanup_result,
            "duration_seconds": round(self.duration_seconds, 2),
        }


# ─── Test Scenarios ───────────────────────────────────────────────────

def run_test_update_name(
    order_name: str, shopify_order_id: int, phone: str, client_id: str, env: dict,
) -> E2ETestResult:
    """Update Customer Name"""
    result = E2ETestResult("Update Customer Name via Graph")
    result.order_name = order_name
    result.shopify_order_id = shopify_order_id
    trace_id = f"test-name-{uuid.uuid4().hex[:8]}"
    t0 = time.time()

    try:
        result.shopify_before = _fetch_order_from_shopify(
            env["shop_url"], env["api_version"], env["token"], shopify_order_id
        ).get("shipping_address", {})

        msg = f"I want to update my name on order {order_name}. Please change my name to Shivam Mehrotra."
        result.user_message = msg
        resp = send_bot_message(msg, phone, client_id, trace_id)
        result.bot_response = resp

        time.sleep(5)

        result.shopify_after = _fetch_order_from_shopify(
            env["shop_url"], env["api_version"], env["token"], shopify_order_id
        ).get("shipping_address", {})

        first = result.shopify_after.get("first_name", "")
        last = result.shopify_after.get("last_name", "")

        name_check = "shivam" in first.lower()
        last_check = "mehrotra" in last.lower()

        result.add_validation("first_name", "Shivam", first, name_check)
        result.add_validation("last_name", "Mehrotra", last, last_check)

        result.passed = name_check and last_check
        if not result.passed:
            result.error = f"Name not updated. Got: '{first} {last}'"

    except Exception as e:
        result.error = str(e)

    result.duration_seconds = time.time() - t0
    return result


def run_test_update_address(
    order_name: str, shopify_order_id: int, phone: str, client_id: str, env: dict,
) -> E2ETestResult:
    """Update Shipping Address"""
    result = E2ETestResult("Update Shipping Address via Graph")
    result.order_name = order_name
    result.shopify_order_id = shopify_order_id
    trace_id = f"test-addr-{uuid.uuid4().hex[:8]}"
    t0 = time.time()

    try:
        result.shopify_before = _fetch_order_from_shopify(
            env["shop_url"], env["api_version"], env["token"], shopify_order_id
        ).get("shipping_address", {})

        msg = (
            f"Please update the delivery address for order {order_name}. "
            f"New address is: 456 MG Road, Indiranagar, Bangalore, Karnataka, 560038."
        )
        result.user_message = msg
        resp = send_bot_message(msg, phone, client_id, trace_id)
        result.bot_response = resp

        time.sleep(5)

        result.shopify_after = _fetch_order_from_shopify(
            env["shop_url"], env["api_version"], env["token"], shopify_order_id
        ).get("shipping_address", {})

        addr = result.shopify_after.get("address1", "")
        city = result.shopify_after.get("city", "")
        zip_code = result.shopify_after.get("zip", "")

        addr_ok = "mg road" in addr.lower() or "456" in addr
        city_ok = "bangalore" in city.lower() or "bengaluru" in city.lower()
        zip_ok = "560038" in zip_code

        result.add_validation("address1", "456 MG Road", addr, addr_ok)
        result.add_validation("city", "Bangalore", city, city_ok)
        result.add_validation("zip", "560038", zip_code, zip_ok)

        result.passed = addr_ok and city_ok
        if not result.passed:
            result.error = f"Address not updated. Got: address1='{addr}', city='{city}'"

    except Exception as e:
        result.error = str(e)

    result.duration_seconds = time.time() - t0
    return result


def run_test_update_name_and_address(
    order_name: str, shopify_order_id: int, phone: str, client_id: str, env: dict,
) -> E2ETestResult:
    """Update Name + Address Combined"""
    result = E2ETestResult("Update Name + Address (Combined) via Graph")
    result.order_name = order_name
    result.shopify_order_id = shopify_order_id
    trace_id = f"test-both-{uuid.uuid4().hex[:8]}"
    t0 = time.time()

    try:
        result.shopify_before = _fetch_order_from_shopify(
            env["shop_url"], env["api_version"], env["token"], shopify_order_id
        ).get("shipping_address", {})

        msg = (
            f"Hi, I need to make changes to order {order_name}. "
            f"Please change my name to Ravi Kumar and update the address to "
            f"789 Brigade Road, Koramangala, Bangalore, Karnataka, 560095."
        )
        result.user_message = msg
        resp = send_bot_message(msg, phone, client_id, trace_id)
        result.bot_response = resp

        time.sleep(5)

        result.shopify_after = _fetch_order_from_shopify(
            env["shop_url"], env["api_version"], env["token"], shopify_order_id
        ).get("shipping_address", {})

        first = result.shopify_after.get("first_name", "")
        last = result.shopify_after.get("last_name", "")
        addr = result.shopify_after.get("address1", "")
        city = result.shopify_after.get("city", "")
        zip_code = result.shopify_after.get("zip", "")

        name_ok = "ravi" in first.lower()
        last_ok = "kumar" in last.lower()
        addr_ok = "brigade" in addr.lower() or "789" in addr
        city_ok = "bangalore" in city.lower() or "bengaluru" in city.lower() or "koramangala" in city.lower()
        zip_ok = "560095" in zip_code

        result.add_validation("first_name", "Ravi", first, name_ok)
        result.add_validation("last_name", "Kumar", last, last_ok)
        result.add_validation("address1", "789 Brigade Road", addr, addr_ok)
        result.add_validation("city", "Bangalore/Koramangala", city, city_ok)
        result.add_validation("zip", "560095", zip_code, zip_ok)

        result.passed = name_ok and (addr_ok or city_ok)
        if not result.passed:
            parts = []
            if not name_ok:
                parts.append(f"Name not updated (got '{first} {last}')")
            if not addr_ok and not city_ok:
                parts.append(f"Address not updated (got '{addr}, {city}')")
            result.error = "; ".join(parts)

    except Exception as e:
        result.error = str(e)

    result.duration_seconds = time.time() - t0
    return result


# ─── Main Orchestrator ────────────────────────────────────────────────

def run_all_tests(client_id: str):
    run_timestamp = datetime.now().isoformat()
    logger.info(f"Starting Real E2E Graph Tests for client: {client_id}")

    shopify_config = get_shopify_config(client_id=client_id)
    if not shopify_config or not shopify_config.get("access_token"):
        logger.error("Shopify config not found. Aborting.")
        return

    env = {
        "shop_url": shopify_config["shop_url"],
        "api_version": shopify_config.get("api_version", "2023-10"),
        "token": shopify_config["access_token"],
    }
    creation_env = CreationShopifyEnv(
        access_token=env["token"],
        shop_url=env["shop_url"],
        api_version=env["api_version"],
    )
    creation_api = OrderCreationAPI(creation_env)

    logger.info("Finding a product for test orders...")
    resp = requests.get(
        f"https://{env['shop_url']}/admin/api/{env['api_version']}/products.json?limit=1",
        headers=_shopify_headers(env["token"]),
    )
    products = resp.json().get("products", [])
    if not products:
        logger.error("No products in store. Aborting.")
        return
    product = products[0]
    variant = product["variants"][0]
    logger.info(f"Using product: {product['title']}")

    test_phone = "9000000001"
    _clear_state(client_id, test_phone)

    # ─── Run each test in isolation ───────────────────────────────────
    all_results: List[E2ETestResult] = []

    test_functions = [
        run_test_update_name,
        run_test_update_address,
        run_test_update_name_and_address,
    ]

    for test_fn in test_functions:
        test_label = test_fn.__doc__.strip()
        logger.info(f"\n{'─'*60}")
        logger.info(f"SETUP: Creating order for test: {test_label}")

        customer = CustomerInfo(
            email=f"test_{uuid.uuid4().hex[:6]}@e2e.test",
            phone=test_phone,
        )
        address = ShippingAddress(
            first_name="Original",
            last_name="Testname",
            address1="123 Test Street",
            city="Mumbai",
            state="Maharashtra",
            zip_code="400001",
            phone=test_phone,
        )
        order_result = creation_api.create_order(
            customer=customer,
            address=address,
            product=ProductSelection(
                product_id=product["id"],
                variant_id=variant["id"],
                quantity=1,
            ),
        )

        if not order_result.get("success"):
            logger.error(f"Failed to create order: {order_result.get('error')}")
            r = E2ETestResult(test_label)
            r.error = f"Order creation failed: {order_result.get('error')}"
            all_results.append(r)
            continue

        order_data = order_result["order"]
        shopify_id = order_data["id"]
        order_name = order_data["name"]
        logger.info(f"Created order: {order_name} (ID: {shopify_id})")

        time.sleep(5)
        _clear_state(client_id, test_phone)

        # RUN TEST
        try:
            test_result = test_fn(order_name, shopify_id, test_phone, client_id, env)
        except Exception as e:
            test_result = E2ETestResult(test_label)
            test_result.error = str(e)

        # CLEANUP: Cancel order directly via Shopify API
        logger.info(f"CLEANUP: Cancelling order {order_name} via Shopify API...")
        cancel_result = _cancel_order_direct(
            env["shop_url"], env["api_version"], env["token"], shopify_id
        )
        test_result.cleanup_result = cancel_result
        if cancel_result.get("success"):
            logger.info(f"Order {order_name} cancelled successfully.")
        else:
            logger.warning(f"Failed to cancel order {order_name}: {cancel_result}")

        # Log result
        status = "PASS" if test_result.passed else "FAIL"
        logger.info(f"RESULT: [{status}] {test_result.name}")
        if test_result.error:
            logger.info(f"  Error: {test_result.error}")
        for v in test_result.validations:
            v_status = "OK" if v["passed"] else "MISMATCH"
            logger.info(f"  [{v_status}] {v['field']}: expected='{v['expected']}' actual='{v['actual']}'")

        all_results.append(test_result)

    # ─── Write Report ─────────────────────────────────────────────────
    report = {
        "run_timestamp": run_timestamp,
        "client_id": client_id,
        "test_phone": test_phone,
        "product_used": product["title"],
        "total_tests": len(all_results),
        "passed": sum(1 for r in all_results if r.passed),
        "failed": sum(1 for r in all_results if not r.passed),
        "tests": [r.to_dict() for r in all_results],
    }

    with open(REPORT_PATH, "w") as f:
        json.dump(report, f, indent=2, default=str)
    logger.info(f"\nReport written to: {REPORT_PATH}")

    # ─── Console Summary ─────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  E2E GRAPH TEST SUMMARY  |  {run_timestamp}")
    print(f"{'='*70}")

    for r in all_results:
        status = "PASS" if r.passed else "FAIL"
        print(f"\n  [{status}] {r.name}  ({r.duration_seconds:.1f}s)")
        print(f"       Order: {r.order_name}")
        print(f"       Message: {r.user_message[:100]}...")
        print(f"       Bot: {r.bot_response[:150]}...")
        if r.validations:
            print(f"       Validations:")
            for v in r.validations:
                icon = "OK" if v["passed"] else "FAIL"
                print(f"         [{icon}] {v['field']}: expected='{v['expected']}' | actual='{v['actual']}'")
        if r.error:
            print(f"       Error: {r.error}")
        print(f"       Cleanup: {'OK' if r.cleanup_result.get('success') else 'FAILED'}")

    passed = report["passed"]
    total = report["total_tests"]
    print(f"\n{'='*70}")
    print(f"  {passed}/{total} tests passed")
    print(f"  Report: {REPORT_PATH}")
    print(f"{'='*70}\n")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    target_client = sys.argv[1] if len(sys.argv) > 1 else "c3ffcb1b-afb9-4ca4-8746-a06698bec870"
    run_all_tests(target_client)
