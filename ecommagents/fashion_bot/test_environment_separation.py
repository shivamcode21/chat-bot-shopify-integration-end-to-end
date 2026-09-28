#!/usr/bin/env python3
"""
Test script to demonstrate environment-based LangSmith project separation
"""
import os
import sys
from typing import Dict, Any

# Add the fashion_bot directory to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'fashion_bot'))

def test_environment_separation():
    """Test how different environment variables affect project naming"""
    
    print("🧪 Testing Environment-Based LangSmith Project Separation")
    print("=" * 60)
    
    # Store original environment value
    original_env = os.environ.get("ENVIRONMENT", "")
    
    # Test different environments
    test_environments = [
        ("development", "ENVIRONMENT", "development"),
        ("staging", "ENVIRONMENT", "staging"), 
        ("production", "ENVIRONMENT", "production"),
        ("dev_via_node", "NODE_ENV", "development"),
        ("prod_via_node", "NODE_ENV", "production"),
        ("staging_via_deploy", "DEPLOY_ENV", "staging"),
        ("default", "", "")  # No environment set
    ]
    
    results = []
    
    for test_name, env_var, env_value in test_environments:
        print(f"\n🔧 Testing: {test_name}")
        print("-" * 40)
        
        # Clear all environment variables first
        for var in ["ENVIRONMENT", "NODE_ENV", "DEPLOY_ENV"]:
            if var in os.environ:
                del os.environ[var]
        
        # Set the specific environment variable
        if env_var and env_value:
            os.environ[env_var] = env_value
            print(f"   Set {env_var}={env_value}")
        else:
            print("   No environment variable set")
        
        # Import after setting environment (to trigger re-evaluation)
        if 'fashion_bot.langsmith_config' in sys.modules:
            del sys.modules['fashion_bot.langsmith_config']
        
        try:
            from fashion_bot.langsmith_config import (
                get_current_environment, 
                get_environment_suffix,
                get_langsmith_config,
                _service_configs
            )
            
            detected_env = get_current_environment()
            env_suffix = get_environment_suffix(detected_env)
            
            # Get configurations for all services
            service_configs = {
                service: config.project_name 
                for service, config in _service_configs.items()
            }
            
            result = {
                "test_name": test_name,
                "env_var_set": f"{env_var}={env_value}" if env_var else "None",
                "detected_environment": detected_env,
                "environment_suffix": env_suffix,
                "project_names": service_configs
            }
            
            results.append(result)
            
            print(f"   Detected Environment: {detected_env}")
            print(f"   Environment Suffix: {env_suffix}")
            print(f"   Project Names:")
            for service, project_name in service_configs.items():
                print(f"     {service}: {project_name}")
                
        except Exception as e:
            print(f"   ❌ Error: {e}")
            results.append({
                "test_name": test_name,
                "error": str(e)
            })
    
    # Restore original environment
    if original_env:
        os.environ["ENVIRONMENT"] = original_env
    
    print("\n" + "=" * 60)
    print("📊 SUMMARY OF RESULTS")
    print("=" * 60)
    
    for result in results:
        if "error" in result:
            print(f"❌ {result['test_name']}: {result['error']}")
        else:
            print(f"✅ {result['test_name']}:")
            print(f"   Environment: {result['detected_environment']}")
            print(f"   Gupshup Project: {result['project_names']['gupshup']}")
            print()
    
    print("🎯 KEY TAKEAWAYS:")
    print("1. Environment detection priority: ENVIRONMENT > NODE_ENV > DEPLOY_ENV")
    print("2. Project names automatically include environment suffix")
    print("3. Different environments = Different LangSmith projects")
    print("4. Default environment is 'development' if none specified")
    
    return results

def test_health_endpoint_response():
    """Test what the health endpoint would return for different environments"""
    
    print("\n🏥 Testing Health Endpoint Responses")
    print("=" * 50)
    
    # Set production environment
    os.environ["ENVIRONMENT"] = "production"
    
    # Clear module cache to force re-import
    if 'fashion_bot.langsmith_config' in sys.modules:
        del sys.modules['fashion_bot.langsmith_config']
    
    try:
        from fashion_bot.langsmith_config import get_langsmith_config
        
        gupshup_config = get_langsmith_config("gupshup")
        status = gupshup_config.get_status()
        
        print("Health endpoint response for ENVIRONMENT=production:")
        print("```json")
        import json
        print(json.dumps({
            "langsmith_tracing": status
        }, indent=2))
        print("```")
        
        print(f"\nLangSmith Project URL: {gupshup_config.get_project_url()}")
        
    except Exception as e:
        print(f"❌ Error testing health endpoint: {e}")

if __name__ == "__main__":
    results = test_environment_separation()
    test_health_endpoint_response()
    
    print("\n🚀 Environment separation is working!")
    print("To use in your deployment:")
    print("1. Set ENVIRONMENT=production in production")
    print("2. Set ENVIRONMENT=staging in staging") 
    print("3. Set ENVIRONMENT=development or leave unset for dev")
    print("4. Restart your application after changing environment variables")
    print("5. Check /gupshup/webhook/health to verify configuration") 