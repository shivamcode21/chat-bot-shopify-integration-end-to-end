#!/usr/bin/env python3
"""
Quick verification script to check if chat widget is ready
"""
import os
import sys

def check_file_exists(path, description):
    """Check if file exists"""
    if os.path.exists(path):
        print(f"✅ {description}: {path}")
        return True
    else:
        print(f"❌ {description} MISSING: {path}")
        return False

def main():
    print("\n" + "="*60)
    print("🔍 Verifying Chat Widget Installation")
    print("="*60 + "\n")
    
    all_good = True
    
    # Check backend files
    print("📦 Backend Files:")
    all_good &= check_file_exists("fashion_bot/websocket_chat.py", "WebSocket handler")
    all_good &= check_file_exists("fashion_bot/main.py", "FastAPI server")
    all_good &= check_file_exists("run_chat_server.py", "Server launcher")
    
    print("\n🎨 Frontend Files:")
    all_good &= check_file_exists("static/chat-widget.js", "Widget JavaScript")
    all_good &= check_file_exists("static/test-chat-widget.html", "Test website")
    
    print("\n📚 Documentation:")
    all_good &= check_file_exists("WEB_CHAT_WIDGET_README.md", "Integration guide")
    all_good &= check_file_exists("WEB_CHAT_IMPLEMENTATION_SUMMARY.md", "Implementation summary")
    
    print("\n" + "="*60)
    
    if all_good:
        print("✅ ALL FILES PRESENT!")
        print("\n🚀 Next Steps:")
        print("   1. Run: python run_chat_server.py")
        print("   2. Open: http://localhost:8000/test")
        print("   3. Click the chat button in bottom-right corner")
        print("   4. Try: 'Where is my order GV10455?'")
    else:
        print("❌ Some files are missing!")
        sys.exit(1)
    
    print("="*60 + "\n")

if __name__ == "__main__":
    main()

