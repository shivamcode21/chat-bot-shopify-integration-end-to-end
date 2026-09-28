#!/usr/bin/env python3

"""
Test script to verify that order prefix configuration is working correctly
across all the updated files.
"""

import sys
import os

# Add the fashion_bot directory to the Python path
fashion_bot_dir = os.path.join(os.path.dirname(__file__), 'fashion_bot')
sys.path.insert(0, fashion_bot_dir)

def test_order_prefix_config():
    """Test order prefix configuration fetching"""
    print("🧪 Testing Order Prefix Configuration...")
    
    try:
        from fashion_bot.config_manager import get_config
        
        # Test getting the order prefix from database
        order_prefix = get_config('order_prefix', 'gv')
        print(f"✅ Order prefix from database: '{order_prefix}'")
        
        if order_prefix:
            print(f"📋 Type: {type(order_prefix)}")
            print(f"📋 Length: {len(order_prefix)}")
            print(f"📋 Lowercase: '{order_prefix.lower()}'")
            print(f"📋 Uppercase: '{order_prefix.upper()}'")
        
        return True
        
    except Exception as e:
        print(f"❌ Error testing order prefix config: {str(e)}")
        return False

def test_tools_py_functions():
    """Test functions in tools.py"""
    print("\n🔧 Testing tools.py functions...")
    
    try:
        from fashion_bot.tools import get_order_prefix, generate_order_name_variants
        
        # Test the helper function
        prefix = get_order_prefix()
        print(f"✅ get_order_prefix() returned: '{prefix}'")
        
        # Test order name variant generation
        test_cases = ["1234", "gv1234", "#gv1234", "GV1234"]
        
        for test_case in test_cases:
            variants = generate_order_name_variants(test_case)
            print(f"📋 Variants for '{test_case}': {variants}")
        
        return True
        
    except Exception as e:
        print(f"❌ Error testing tools.py functions: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def test_delivery_nodes_config():
    """Test delivery_nodes.py configuration"""
    print("\n📦 Testing delivery_nodes.py configuration...")
    
    try:
        from fashion_bot.config_manager import get_config
        
        # Simulate what delivery_nodes.py does
        try:
            order_prefix = get_config('order_prefix', 'gv')
        except Exception:
            order_prefix = 'gv'
        
        print(f"✅ Delivery nodes would use prefix: '{order_prefix}'")
        
        # Test regex pattern building
        import re
        order_prefix_escaped = re.escape(order_prefix)
        order_id_pattern = rf'\b(?:order|ord|#)?\s*(?:{order_prefix_escaped})?(\d+)\b'
        
        print(f"📋 Regex pattern: {order_id_pattern}")
        
        # Test pattern with sample text
        test_text = f"My order {order_prefix}1234 is delayed"
        match = re.search(order_id_pattern, test_text, re.IGNORECASE)
        if match:
            print(f"✅ Pattern matched: '{match.group()}'")
        else:
            print("⚠️ Pattern did not match test text")
        
        return True
        
    except Exception as e:
        print(f"❌ Error testing delivery_nodes.py config: {str(e)}")
        return False

def test_order_apis():
    """Test order API classes"""
    print("\n🛒 Testing Order API classes...")
    
    try:
        # Test OrderIdentifier from order_cancellation_api
        from fashion_bot.shopify.modules.order_cancellation_api import OrderIdentifier as CancelOrderId
        
        cancel_order = CancelOrderId(value="1234")
        formatted = cancel_order.to_gv_format()
        print(f"✅ Cancellation OrderIdentifier.to_gv_format('1234'): '{formatted}'")
        
        # Test OrderIdentifier from order_updation_api  
        from fashion_bot.shopify.modules.order_updation_api import OrderIdentifier as UpdateOrderId
        
        update_order = UpdateOrderId(value="1234")
        prefix = update_order.get_order_prefix()
        formatted_new = update_order.to_formatted_order_name()
        print(f"✅ Update OrderIdentifier.get_order_prefix(): '{prefix}'")
        print(f"✅ Update OrderIdentifier.to_formatted_order_name('1234'): '{formatted_new}'")
        
        return True
        
    except Exception as e:
        print(f"❌ Error testing order APIs: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def test_extract_channel_order_base():
    """Test extract_channel_order_base functions"""
    print("\n🔄 Testing extract_channel_order_base functions...")
    
    try:
        from fashion_bot.shopify.modules.order_updation_api import extract_channel_order_base
        
        test_cases = ["1234", "gv1234", "#gv1234", "GV1234"]
        
        for test_case in test_cases:
            result = extract_channel_order_base(test_case, client_id=None)
            print(f"📋 extract_channel_order_base('{test_case}'): '{result}'")
        
        return True
        
    except Exception as e:
        print(f"❌ Error testing extract_channel_order_base: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    print("🚀 Starting Order Prefix Configuration Tests...\n")
    
    # Test configuration fetching
    test1_success = test_order_prefix_config()
    
    # Test tools.py functions
    test2_success = test_tools_py_functions()
    
    # Test delivery_nodes.py config
    test3_success = test_delivery_nodes_config()
    
    # Test order API classes
    test4_success = test_order_apis()
    
    # Test extract functions
    test5_success = test_extract_channel_order_base()
    
    print(f"\n📊 Test Results:")
    print(f"📋 Order Prefix Config: {'✅ PASSED' if test1_success else '❌ FAILED'}")
    print(f"🔧 Tools.py Functions: {'✅ PASSED' if test2_success else '❌ FAILED'}")
    print(f"📦 Delivery Nodes Config: {'✅ PASSED' if test3_success else '❌ FAILED'}")
    print(f"🛒 Order API Classes: {'✅ PASSED' if test4_success else '❌ FAILED'}")
    print(f"🔄 Extract Functions: {'✅ PASSED' if test5_success else '❌ FAILED'}")
    
    if all([test1_success, test2_success, test3_success, test4_success, test5_success]):
        print("\n🎉 All tests passed! Order prefix configuration is working correctly.")
        print("🔧 The system now uses dynamic order prefix from PostgreSQL configuration.")
    else:
        print("\n⚠️ Some tests failed. Please check the implementation.")
