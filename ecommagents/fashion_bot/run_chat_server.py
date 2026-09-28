"""
Launcher script for Fashion Bot Server with Chat Widget Support

Usage:
    python run_chat_server.py
    
Then open: http://localhost:8000/test

Endpoints:
    - WebSocket Chat: ws://localhost:8000/ws/chat/{client_id}/{session_id}
    - Test Page: http://localhost:8000/test
    - Health: http://localhost:8000/health
    - WhatsApp Webhook: http://localhost:8000/whatsapp/webhook
    - Gupshup Webhook: http://localhost:8000/gupshup/webhook
"""
import uvicorn
import logging

logging.basicConfig(level=logging.INFO)

if __name__ == "__main__":
    print("\n" + "="*60)
    print("🚀 Starting Fashion Bot Server with Chat Widget")
    print("="*60)
    print("\n📍 Server: http://localhost:8000")
    print("🧪 Test widget: http://localhost:8000/test")
    print("💚 Health: http://localhost:8000/health")
    print("🔌 WebSocket: ws://localhost:8000/ws/chat/{client_id}/{session_id}")
    print("\n📋 Try asking the bot:")
    print("   - Where is my order GV10455?")
    print("   - What are your return policies?")
    print("   - Show me hoodies collection")
    print("\n" + "="*60 + "\n")
    
    uvicorn.run(
        "fashion_bot.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )

