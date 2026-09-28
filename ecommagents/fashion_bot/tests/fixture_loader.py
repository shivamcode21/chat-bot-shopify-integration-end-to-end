#!/usr/bin/env python3
"""
Fixture Loader for Agent Testing Framework.

Loads client profiles and state fixtures, resolves template variables,
and reconstructs LangChain message objects from JSON fixtures.
"""
import json
import os
import copy
import logging
from typing import Dict, Any, Optional, List

from langchain_core.messages import HumanMessage, AIMessage

logger = logging.getLogger("fixture_loader")

# Base directories
FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
CLIENTS_DIR = os.path.join(FIXTURES_DIR, "clients")
STATES_DIR = os.path.join(FIXTURES_DIR, "states")


# ==================== CLIENT PROFILES ====================

def load_client_profile(client_name: str) -> Dict[str, Any]:
    """
    Load a client profile from fixtures/clients/<client_name>.json
    
    Args:
        client_name: Name of the client (e.g., 'groovee')
        
    Returns:
        Client profile dictionary
        
    Raises:
        FileNotFoundError: If client profile doesn't exist
    """
    profile_path = os.path.join(CLIENTS_DIR, f"{client_name}.json")
    if not os.path.exists(profile_path):
        raise FileNotFoundError(
            f"Client profile not found: {profile_path}. "
            f"Available clients: {list_available_clients()}"
        )
    
    with open(profile_path, "r") as f:
        profile = json.load(f)
    
    # Resolve client_id: env var → fallback UUID → test default
    env_var = profile.get("client_id_env_var")
    fallback = profile.get("client_id_fallback")
    if env_var and os.getenv(env_var):
        profile["client_id"] = os.getenv(env_var)
    elif fallback:
        profile["client_id"] = fallback
    else:
        profile["client_id"] = f"test-{client_name}-default"
    
    logger.info(f"📂 Loaded client profile: {client_name} (client_id={profile.get('client_id', 'N/A')})")
    return profile


def list_available_clients() -> List[str]:
    """List all available client profiles."""
    if not os.path.exists(CLIENTS_DIR):
        return []
    return [
        f.replace(".json", "") 
        for f in os.listdir(CLIENTS_DIR) 
        if f.endswith(".json")
    ]


def get_client_id(client_name: str) -> str:
    """
    Get the client_id for a given client name.
    Resolution order:
      1. TEST_<NAME>_CLIENT_ID env var
      2. Live DB lookup via gupshup_source from client profile
      3. Fallback to test default string
    
    Args:
        client_name: Name of the client
        
    Returns:
        Resolved client_id string
    """
    profile = load_client_profile(client_name)
    
    # Already resolved via env var
    if profile.get("client_id") and not profile["client_id"].startswith("test-"):
        return profile["client_id"]
    
    # Try live DB lookup using gupshup source from profile
    gupshup_source = profile.get("mock_config", {}).get("gupshup_source")
    if gupshup_source:
        try:
            from fashion_bot.config_manager import get_default_client_id
            db_client_id = get_default_client_id(gupshup_source)
            if db_client_id:
                logger.info(f"📂 Resolved client_id from DB for {client_name}: {db_client_id}")
                return db_client_id
        except Exception as e:
            logger.warning(f"Could not resolve client_id from DB: {e}")
    
    return profile.get("client_id", f"test-{client_name}-default")


# ==================== STATE FIXTURES ====================

def load_state_fixture(fixture_id: str, client_id: str = None) -> Dict[str, Any]:
    """
    Load a state fixture and resolve template variables.
    
    Args:
        fixture_id: ID of the state fixture (e.g., 'cold_start', 'mid_cancellation_flow')
        client_id: Client ID to inject into the state (replaces {{client_id}})
        
    Returns:
        State dictionary with resolved templates and reconstructed LangChain messages
        
    Raises:
        FileNotFoundError: If fixture doesn't exist
    """
    fixture_path = os.path.join(STATES_DIR, f"{fixture_id}.json")
    if not os.path.exists(fixture_path):
        raise FileNotFoundError(
            f"State fixture not found: {fixture_path}. "
            f"Available fixtures: {list_available_fixtures()}"
        )
    
    with open(fixture_path, "r") as f:
        fixture_data = json.load(f)
    
    state = copy.deepcopy(fixture_data.get("state", {}))
    
    # Resolve {{client_id}} template variable
    if client_id:
        state = _resolve_templates(state, {"client_id": client_id})
    
    # Reconstruct LangChain message objects from JSON
    if "messages" in state and state["messages"]:
        state["messages"] = _reconstruct_messages(state["messages"])
    
    logger.info(
        f"📂 Loaded state fixture: {fixture_id} "
        f"(messages={len(state.get('messages', []))}, "
        f"phone={state.get('phone_number', 'N/A')})"
    )
    return state


def list_available_fixtures() -> List[str]:
    """List all available state fixtures."""
    if not os.path.exists(STATES_DIR):
        return []
    return [
        f.replace(".json", "") 
        for f in os.listdir(STATES_DIR) 
        if f.endswith(".json")
    ]


def get_fixture_metadata(fixture_id: str) -> Dict[str, str]:
    """Get metadata (id, description) for a fixture without loading full state."""
    fixture_path = os.path.join(STATES_DIR, f"{fixture_id}.json")
    if not os.path.exists(fixture_path):
        return {}
    
    with open(fixture_path, "r") as f:
        data = json.load(f)
    
    return {
        "fixture_id": data.get("fixture_id", fixture_id),
        "description": data.get("description", "No description")
    }


# ==================== TEMPLATE RESOLUTION ====================

def _resolve_templates(obj: Any, variables: Dict[str, str]) -> Any:
    """
    Recursively resolve {{variable}} templates in a dictionary/list/string.
    
    Args:
        obj: The object to resolve (dict, list, or string)
        variables: Dictionary of variable_name -> value
        
    Returns:
        Object with templates resolved
    """
    if isinstance(obj, str):
        for key, value in variables.items():
            obj = obj.replace(f"{{{{{key}}}}}", str(value))
        return obj
    elif isinstance(obj, dict):
        return {k: _resolve_templates(v, variables) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_resolve_templates(item, variables) for item in obj]
    return obj


# ==================== MESSAGE RECONSTRUCTION ====================

def _reconstruct_messages(messages_json: List[Dict]) -> List:
    """
    Convert JSON message dicts to LangChain message objects.
    
    Supports formats:
    - {"role": "human", "content": "..."} -> HumanMessage
    - {"role": "ai", "content": "..."} -> AIMessage
    - {"role": "customer", "content": "..."} -> HumanMessage (alias)
    - {"role": "bot", "content": "..."} -> AIMessage (alias)
    
    Args:
        messages_json: List of message dicts
        
    Returns:
        List of LangChain message objects
    """
    messages = []
    for msg in messages_json:
        role = msg.get("role", "human").lower()
        content = msg.get("content", "")
        
        if role in ("human", "customer", "user"):
            messages.append(HumanMessage(content=content))
        elif role in ("ai", "bot", "assistant", "support"):
            messages.append(AIMessage(content=content))
        else:
            # Default to HumanMessage for unknown roles
            logger.warning(f"Unknown message role '{role}', treating as human message")
            messages.append(HumanMessage(content=content))
    
    return messages


# ==================== SCENARIO HELPERS ====================

def build_test_state(
    scenario: Dict[str, Any],
    default_client: str = "casence"
) -> Dict[str, Any]:
    """
    Build a complete test state from a scenario definition.
    
    This is the main entry point for the test runner to get a ready-to-use state.
    
    Args:
        scenario: Test scenario dict with optional 'client_id', 'initial_state_fixture',
                  and 'initial_state_overrides' fields
        default_client: Default client name if scenario doesn't specify one
        
    Returns:
        Ready-to-use state dictionary
    """
    # 1. Resolve client_id
    client_name = scenario.get("client_name", default_client)
    client_id = scenario.get("client_id")
    
    if not client_id:
        try:
            client_id = get_client_id(client_name)
        except FileNotFoundError:
            client_id = f"test-{client_name}-default"
    
    # 2. Load base state from fixture (or use cold_start)
    fixture_id = scenario.get("initial_state_fixture", "cold_start")
    state = load_state_fixture(fixture_id, client_id=client_id)
    
    # 3. Apply any inline state overrides from the scenario
    overrides = scenario.get("initial_state_overrides", {})
    if overrides:
        for key, value in overrides.items():
            state[key] = value
        logger.info(f"📝 Applied {len(overrides)} state overrides from scenario")
    
    # 4. Ensure client_id is set
    if "client_id" not in state or not state["client_id"]:
        state["client_id"] = client_id
    
    return state


def load_context_assertions(scenario: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Extract context assertions from a scenario for evaluation.
    
    Args:
        scenario: Test scenario dict
        
    Returns:
        Context assertions dict or None if not specified
    """
    return scenario.get("context_assertions")

