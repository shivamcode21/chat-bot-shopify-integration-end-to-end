"""
Centralized configuration for load tests.
All values can be overridden via environment variables.
"""
from fashion_bot.env_loader import get_env, get_int

# Server
HOST = get_env("LOAD_TEST_HOST", "localhost")
PORT = get_int("LOAD_TEST_PORT", 8000)
WS_URL = f"ws://{HOST}:{PORT}/ws/chat"
CLIENT_NAME = get_env("LOAD_TEST_CLIENT_NAME", "Concept Groove")

# Load test parameters
USERS = get_int("LOAD_TEST_USERS", 50)
SPAWN_RATE = get_int("LOAD_TEST_SPAWN_RATE", 5)
DURATION = get_env("LOAD_TEST_DURATION", "5m")

# Mock LLM
MOCK_DELAY_MS = get_int("MOCK_LLM_DELAY_MS", 50)

# Message patterns used by virtual users
MESSAGES = [
    "hello",
    "hi there",
    "show me denim jackets",
    "what is your return policy?",
    "where is my order?",
    "do you have size M?",
    "what offers do you have?",
    "what categories do you have?",
    "how long will delivery take?",
    "thanks bye",
]
