from fastapi import APIRouter, Query, Response, Request
from typing import Optional, List, Dict, Any
from functools import lru_cache
import html
import json
import logging
import re
from langchain_core.messages import HumanMessage, AIMessage

from fashion_bot.repository import bot_user_agent_mode
from fashion_bot.state_cache import (
    list_conversations,
    aget_state_by_numbers,
    aupdate_state,
)
from fashion_bot.gupshup_webhook import send_message
from fashion_bot.gupshup_webhook import GUPSHUP_SOURCE
from starlette.responses import RedirectResponse, JSONResponse

router = APIRouter()

# Configure logging
logger = logging.getLogger("dashboard")


@lru_cache(maxsize=1)
def _get_bigquery_fetchers():
    # Lazy import: avoid loading google-cloud-bigquery during app bootstrap.
    from fashion_bot.history.bigquery_logger import (
        fetch_conversations_by_phone,
        fetch_conversations_by_thread,
        fetch_recent_rows,
    )
    return fetch_conversations_by_phone, fetch_conversations_by_thread, fetch_recent_rows

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "groovee"
AUTH_COOKIE_NAME = "dash_auth"

LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Dashboard Login</title>
  <style>
    * {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }}
    
    body {{ 
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
      background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
      display: flex; 
      align-items: center; 
      justify-content: center; 
      min-height: 100vh;
      min-height: -webkit-fill-available;
      padding: 1rem;
      line-height: 1.6;
    }}
    
    .card {{ 
      background: white;
      border: none;
      border-radius: 16px;
      padding: 2rem;
      width: 100%;
      max-width: 400px;
      box-shadow: 0 20px 40px rgba(0,0,0,0.1);
      backdrop-filter: blur(10px);
    }}
    
    .logo {{
      text-align: center;
      margin-bottom: 2rem;
    }}
    
    .logo-icon {{
      font-size: 3rem;
      margin-bottom: 0.5rem;
      display: block;
    }}
    
    h2 {{ 
      margin: 0 0 1.5rem 0;
      text-align: center;
      color: #1e293b;
      font-size: 1.75rem;
      font-weight: 700;
    }}
    
    .subtitle {{
      text-align: center;
      color: #64748b;
      margin-bottom: 2rem;
      font-size: 0.95rem;
    }}
    
    label {{ 
      display: block;
      margin: 1rem 0 0.5rem 0;
      font-size: 0.9rem;
      font-weight: 500;
      color: #374151;
    }}
    
    input {{ 
      width: 100%;
      padding: 0.875rem 1rem;
      border: 2px solid #e2e8f0;
      border-radius: 12px;
      font-size: 1rem;
      transition: all 0.2s ease;
      background: #f8fafc;
    }}
    
    input:focus {{
      outline: none;
      border-color: #667eea;
      background: white;
      box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
    }}
    
    button {{ 
      width: 100%;
      margin-top: 1.5rem;
      padding: 1rem;
      background: linear-gradient(135deg, #667eea, #764ba2);
      color: white;
      border: none;
      border-radius: 12px;
      cursor: pointer;
      font-size: 1rem;
      font-weight: 600;
      transition: all 0.2s ease;
      box-shadow: 0 4px 12px rgba(102, 126, 234, 0.3);
    }}
    
    button:hover {{
      transform: translateY(-2px);
      box-shadow: 0 6px 20px rgba(102, 126, 234, 0.4);
    }}
    
    button:active {{
      transform: translateY(0);
    }}
    
    .error {{ 
      color: #991b1b;
      background: #fef2f2;
      border: 1px solid #fecaca;
      padding: 0.875rem 1rem;
      border-radius: 8px;
      margin-bottom: 1rem;
      font-size: 0.9rem;
      font-weight: 500;
      animation: shake 0.5s ease-in-out;
    }}
    
    @keyframes shake {{
      0%, 100% {{ transform: translateX(0); }}
      25% {{ transform: translateX(-5px); }}
      75% {{ transform: translateX(5px); }}
    }}
    
    .footer {{
      text-align: center;
      margin-top: 2rem;
      color: #64748b;
      font-size: 0.8rem;
    }}
    
    /* Mobile Styles */
    @media (max-width: 480px) {{
      body {{
        padding: 0.5rem;
      }}
      
      .card {{
        padding: 1.5rem;
        border-radius: 12px;
      }}
      
      h2 {{
        font-size: 1.5rem;
      }}
      
      input {{
        font-size: 16px; /* Prevents zoom on iOS */
        padding: 1rem;
      }}
      
      button {{
        padding: 1.125rem;
        font-size: 16px;
      }}
    }}
    
    /* Dark mode support */
    @media (prefers-color-scheme: dark) {{
      .card {{
        background: #1e293b;
        color: #e2e8f0;
      }}
      
      h2 {{
        color: #f1f5f9;
      }}
      
      label {{
        color: #cbd5e1;
      }}
      
      input {{
        background: #334155;
        border-color: #475569;
        color: #e2e8f0;
      }}
      
      input:focus {{
        background: #475569;
        border-color: #667eea;
      }}
      
      .footer {{
        color: #94a3b8;
      }}
    }}
  </style>
</head>
<body>
  <form class="card" method="post" action="/dashboard/login">
    <div class="logo">
      <div class="logo-icon">🛡️</div>
    </div>
    <h2>Support Dashboard</h2>
    <div class="subtitle">Sign in to access the support dashboard</div>
    {error_html}
    <label>Username</label>
    <input name="username" placeholder="Enter your username" required />
    <label>Password</label>
    <input type="password" name="password" placeholder="Enter your password" required />
    <button type="submit">Sign In</button>
    <div class="footer">
      Secure access to conversation management
    </div>
  </form>
</body>
</html>
"""

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
  <title>Support Dashboard</title>
  <style>
    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }
    
    body { 
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
      background: #f8fafc;
      color: #334155;
      line-height: 1.6;
    }
    
    header { 
      background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
      color: white; 
      padding: 1rem;
      box-shadow: 0 2px 10px rgba(0,0,0,0.1);
      position: sticky;
      top: 0;
      z-index: 100;
    }
    
    .header-content {
      display: flex;
      justify-content: space-between;
      align-items: center;
      max-width: 1200px;
      margin: 0 auto;
    }
    
    .header-title {
      font-size: 1.5rem;
      font-weight: 600;
    }
    
    .mobile-menu-btn {
      display: none;
      background: rgba(255,255,255,0.2);
      border: none;
      color: white;
      padding: 0.5rem;
      border-radius: 0.5rem;
      cursor: pointer;
      font-size: 1.2rem;
    }
    
    main { 
      display: flex; 
      min-height: calc(100vh - 80px);
      max-width: 1200px;
      margin: 0 auto;
      gap: 1rem;
      padding: 1rem;
    }
    
    #sidebar { 
      width: 360px;
      background: white;
      border-radius: 12px;
      box-shadow: 0 4px 20px rgba(0,0,0,0.08);
      padding: 1.5rem;
      height: fit-content;
      position: sticky;
      top: 100px;
    }
    
    #content { 
      flex: 1;
      background: white;
      border-radius: 12px;
      box-shadow: 0 4px 20px rgba(0,0,0,0.08);
      padding: 1.5rem;
      display: flex; 
      flex-direction: column;
      height: calc(100vh - 140px);
      max-height: calc(100vh - 140px);
    }
    
    .toolbar { 
      margin-bottom: 1.5rem;
      padding-bottom: 1rem;
      border-bottom: 1px solid #e2e8f0;
    }
    
    .toolbar input[type=text] { 
      width: 100%;
      padding: 0.75rem;
      border: 2px solid #e2e8f0;
      border-radius: 8px;
      font-size: 0.95rem;
      transition: all 0.2s ease;
      margin-bottom: 1rem;
    }
    
    .toolbar input[type=text]:focus {
      outline: none;
      border-color: #667eea;
      box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
    }
    
    .checkbox-label {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      font-size: 0.9rem;
      color: #64748b;
      cursor: pointer;
    }
    
    .checkbox-label input[type=checkbox] {
      width: 1rem;
      height: 1rem;
      accent-color: #667eea;
    }
    
    .conv { 
      padding: 1rem;
      border-radius: 8px;
      margin-bottom: 0.5rem;
      cursor: pointer;
      transition: all 0.2s ease;
      border: 1px solid transparent;
    }
    
    .conv:hover { 
      background: #f1f5f9;
      border-color: #e2e8f0;
      transform: translateY(-1px);
    }
    
    .conv.active {
      background: #eff6ff;
      border-color: #3b82f6;
    }
    
    .conv-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 0.5rem;
    }
    
    .conv-phones {
      font-weight: 600;
      color: #1e293b;
    }
    
    .badge { 
      background: linear-gradient(135deg, #f59e0b, #d97706);
      color: white;
      padding: 0.25rem 0.5rem;
      border-radius: 12px;
      font-size: 0.75rem;
      font-weight: 500;
      box-shadow: 0 2px 4px rgba(245, 158, 11, 0.2);
    }
    
    .conv-meta {
      color: #64748b;
      font-size: 0.8rem;
      margin-bottom: 0.25rem;
    }
    
    .conv-preview {
      color: #64748b;
      font-size: 0.85rem;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }
    
    .thread { 
      flex: 1;
      overflow-y: auto;
      overflow-x: hidden;
      padding-right: 0.5rem;
      margin-bottom: 1rem;
      min-height: 0;
      max-height: 100%;
    }
    
    .thread::-webkit-scrollbar {
      width: 6px;
    }
    
    .thread::-webkit-scrollbar-track {
      background: #f1f5f9;
      border-radius: 3px;
    }
    
    .thread::-webkit-scrollbar-thumb {
      background: #cbd5e1;
      border-radius: 3px;
    }
    
    .thread::-webkit-scrollbar-thumb:hover {
      background: #94a3b8;
    }
    
    .msg { 
      margin: 0.75rem 0;
      padding: 0.875rem 1.125rem;
      border-radius: 16px;
      max-width: 85%;
      box-shadow: 0 2px 8px rgba(0,0,0,0.06);
      animation: fadeInUp 0.3s ease-out;
    }
    
    @keyframes fadeInUp {
      from {
        opacity: 0;
        transform: translateY(10px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }
    
    .human { 
      background: linear-gradient(135deg, #3b82f6, #1d4ed8);
      color: white;
      align-self: flex-start;
      border-bottom-left-radius: 6px;
    }
    
    .ai { 
      background: #f1f5f9;
      color: #334155;
      align-self: flex-end;
      border: 1px solid #e2e8f0;
      border-bottom-right-radius: 6px;
    }
    
    .meta { 
      color: #64748b;
      font-size: 0.85rem;
      margin-bottom: 1rem;
      padding: 0.75rem;
      background: #f8fafc;
      border-radius: 8px;
      border-left: 3px solid #667eea;
    }
    
    .reply-section { 
      border-top: 2px solid #e2e8f0;
      padding-top: 1.5rem;
      margin-top: auto;
      flex-shrink: 0;
      background: white;
    }
    
    .phone-display { 
      background: linear-gradient(135deg, #10b981, #059669);
      color: white;
      padding: 0.75rem 1rem;
      border-radius: 8px;
      margin-bottom: 1rem;
      font-weight: 500;
      font-size: 0.9rem;
    }
    
    .reply-box { 
      display: flex;
      gap: 0.75rem;
      align-items: flex-end;
    }
    
    .reply-input { 
      flex: 1;
      padding: 0.875rem 1rem;
      border: 2px solid #e2e8f0;
      border-radius: 12px;
      font-size: 0.95rem;
      resize: none;
      min-height: 44px;
      max-height: 120px;
      transition: all 0.2s ease;
    }
    
    .reply-input:focus {
      outline: none;
      border-color: #667eea;
      box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
    }
    
    .send-btn { 
      background: linear-gradient(135deg, #667eea, #764ba2);
      color: white;
      border: none;
      border-radius: 12px;
      padding: 0.875rem 1.5rem;
      font-weight: 600;
      font-size: 0.9rem;
      cursor: pointer;
      transition: all 0.2s ease;
      box-shadow: 0 4px 12px rgba(102, 126, 234, 0.3);
      min-width: 80px;
    }
    
    .send-btn:hover:not(:disabled) {
      transform: translateY(-2px);
      box-shadow: 0 6px 20px rgba(102, 126, 234, 0.4);
    }
    
    .send-btn:active {
      transform: translateY(0);
    }
    
    .send-btn:disabled { 
      background: #94a3b8;
      cursor: not-allowed;
      transform: none;
      box-shadow: none;
    }
    
    .status-message { 
      padding: 0.75rem 1rem;
      border-radius: 8px;
      margin-bottom: 1rem;
      font-size: 0.9rem;
      font-weight: 500;
      animation: slideDown 0.3s ease-out;
    }
    
    @keyframes slideDown {
      from {
        opacity: 0;
        transform: translateY(-10px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }
    
    .status-success { 
      background: #dcfce7;
      color: #166534;
      border: 1px solid #bbf7d0;
    }
    
    .status-error { 
      background: #fef2f2;
      color: #991b1b;
      border: 1px solid #fecaca;
    }
    
    .empty-state {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      height: 300px;
      color: #64748b;
      text-align: center;
    }
    
    .empty-state-icon {
      font-size: 3rem;
      margin-bottom: 1rem;
      opacity: 0.5;
    }
    
    .spinner {
      width: 1rem;
      height: 1rem;
      border: 2px solid rgba(255, 255, 255, 0.3);
      border-top: 2px solid white;
      border-radius: 50%;
      animation: spin 1s linear infinite;
    }
    
    @keyframes spin {
      0% { transform: rotate(0deg); }
      100% { transform: rotate(360deg); }
    }
    
    /* Mobile Styles */
    @media (max-width: 768px) {
      body {
        height: 100vh;
        height: -webkit-fill-available;
        overflow: hidden;
      }
      
      .mobile-menu-btn {
        display: block;
      }
      
      header {
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        z-index: 100;
      }
      
      main {
        flex-direction: column;
        padding: 0.5rem;
        gap: 0.5rem;
        height: calc(100vh - 80px);
        height: calc(-webkit-fill-available - 80px);
        margin-top: 80px;
        overflow: hidden;
      }
      
      #sidebar {
        width: 100%;
        position: fixed;
        top: 80px;
        left: -100%;
        height: calc(100vh - 80px);
        height: calc(-webkit-fill-available - 80px);
        z-index: 50;
        transition: left 0.3s ease;
        overflow-y: auto;
        -webkit-overflow-scrolling: touch;
      }
      
      #sidebar.open {
        left: 0;
      }
      
      #content {
        margin-top: 0;
        height: calc(100vh - 100px);
        height: calc(-webkit-fill-available - 100px);
        max-height: calc(100vh - 100px);
        max-height: calc(-webkit-fill-available - 100px);
        overflow: hidden;
        display: flex;
        flex-direction: column;
      }
      
      .thread {
        -webkit-overflow-scrolling: touch;
        touch-action: pan-y;
        height: 100%;
        overflow-y: scroll;
        flex: 1 1 0;
        min-height: 0;
      }
      
      .reply-section {
        position: fixed;
        left: 0;
        right: 0;
        bottom: env(safe-area-inset-bottom, 0);
        background: white;
        padding: 0.75rem 0.75rem calc(0.75rem + env(safe-area-inset-bottom, 0));
        border-top: 2px solid #e2e8f0;
        margin-top: 0;
        flex-shrink: 0;
        box-shadow: 0 -2px 10px rgba(0,0,0,0.1);
        z-index: 150;
      }
      
      .conv {
        padding: 0.75rem;
      }
      
      .msg {
        max-width: 90%;
        padding: 0.75rem 1rem;
      }
      
      .reply-box {
        flex-direction: column;
        gap: 0.5rem;
      }
      
      .reply-input {
        min-height: 50px;
        font-size: 16px; /* Prevents zoom on iOS */
      }
      
      .send-btn {
        width: 100%;
        padding: 1rem;
        font-size: 16px;
      }
      
      .toolbar input[type=text] {
        font-size: 16px; /* Prevents zoom on iOS */
      }
      
      .meta {
        flex-shrink: 0;
        margin-bottom: 0.5rem;
      }
    }
    
    @media (max-width: 480px) {
      main {
        padding: 0.25rem;
      }
      
      #sidebar,
      #content {
        border-radius: 8px;
        padding: 1rem;
      }
      
      .msg {
        max-width: 95%;
        padding: 0.625rem 0.875rem;
      }
    }
    
    /* Dark mode support */
    @media (prefers-color-scheme: dark) {
      body {
        background: #0f172a;
        color: #e2e8f0;
      }
      
      #sidebar,
      #content {
        background: #1e293b;
        box-shadow: 0 4px 20px rgba(0,0,0,0.3);
      }
      
      .conv:hover {
        background: #334155;
      }
      
      .conv.active {
        background: #1e3a8a;
      }
      
      .ai {
        background: #334155;
        color: #e2e8f0;
        border-color: #475569;
      }
      
      .meta {
        background: #334155;
        color: #94a3b8;
      }
      
      .toolbar input[type=text],
      .reply-input {
        background: #334155;
        border-color: #475569;
        color: #e2e8f0;
      }
      
      .toolbar input[type=text]:focus,
      .reply-input:focus {
        border-color: #667eea;
      }
    }
  </style>
</head>
<body>
  <header>
    <div class="header-content">
      <div class="header-title">Support Dashboard <span style="font-size: 0.8rem; opacity: 0.8;">(Recent Conversations)</span></div>
      <div style="display: flex; align-items: center; gap: 1rem;">
        <a href="/dashboard/bq" style="color: rgba(255,255,255,0.9); text-decoration: none; font-size: 0.9rem; padding: 0.5rem 1rem; background: rgba(255,255,255,0.1); border-radius: 6px; transition: all 0.2s ease;">
          🔍 Advanced Search
        </a>
        <button class="mobile-menu-btn" onclick="toggleSidebar()">☰</button>
      </div>
    </div>
  </header>
  <main>
    <section id=\"sidebar\">
      <div class=\"toolbar\">
        <input id=\"phoneFilter\" placeholder=\"🔍 Search phone numbers...\" />
        <label class="checkbox-label">
          <input type=\"checkbox\" id=\"onlyEscalations\" checked />
          Only escalations
        </label>
      </div>
      <div id=\"convList\"></div>
    </section>
    <section id=\"content\">
      <div id=\"convMeta\" class=\"meta\"></div>
      <div id=\"thread\" class=\"thread\">
        <div class="empty-state">
          <div class="empty-state-icon">💬</div>
          <h3>Select a conversation</h3>
          <p>Choose a conversation from the sidebar to view messages and send replies</p>
        </div>
      </div>
      <div id\"replySection\" class=\"reply-section\" style=\"display: none;\">
        <div id\"phoneDisplay\" class=\"phone-display\"></div>
        <div id\"statusMessage\"></div>
        <div class=\"reply-box\">
          <textarea id\"replyInput\" class=\"reply-input\" placeholder=\"Type your reply here...\" rows=\"1\"></textarea>
          <button id\"sendBtn\" class=\"send-btn\">Send</button>
          <button id\"refreshBtn\" class=\"send-btn\" title=\"Refresh conversation\">Refresh</button>
          <button id\"resetBotBtn\" class=\"send-btn\" title=\"Reset to bot mode\">Reset to Bot</button>
        </div>
      </div>
    </section>
  </main>
  <script>
    let currentFromPhone = null;
    let currentToPhone = null;
    let activeConvElement = null;
    
    function toggleSidebar() {
      const sidebar = document.getElementById('sidebar');
      sidebar.classList.toggle('open');
    }
    
    // Close sidebar when clicking outside on mobile
    document.addEventListener('click', function(e) {
      const sidebar = document.getElementById('sidebar');
      const menuBtn = document.querySelector('.mobile-menu-btn');
      
      if (window.innerWidth <= 768 && sidebar.classList.contains('open')) {
        if (!sidebar.contains(e.target) && !menuBtn.contains(e.target)) {
          sidebar.classList.remove('open');
        }
      }
    });
    
    // Auto-resize textarea
    function autoResize(textarea) {
      textarea.style.height = 'auto';
      textarea.style.height = Math.min(textarea.scrollHeight, 120) + 'px';
    }
    
    async function fetchConversations() {
      console.log('🔍 [fetchConversations] Starting escalations fetch');
      const resp = await fetch('/dashboard/api/escalations');
      console.log('📡 [fetchConversations] Response status:', resp.status);
      const data = await resp.json();
      console.log('📋 [fetchConversations] Data received:', data);
      return data;
    }
    async function fetchAll() {
      console.log('🔍 [fetchAll] Starting recent conversations fetch');
      // Use BigQuery data instead of state cache
      const resp = await fetch('/dashboard/api/bq/recent?limit=200');
      console.log('📡 [fetchAll] Response status:', resp.status);
      const data = await resp.json();
      console.log('📋 [fetchAll] Data received:', data);
      console.log('📊 [fetchAll] Items count:', data.items ? data.items.length : 0);
      return data;
    }
    async function fetchConversation(fromPhone, toPhone) {
      console.log('🔍 [fetchConversation] Starting conversation fetch for:', fromPhone);
      // Use BigQuery data for conversation details
      const resp = await fetch(`/dashboard/api/bq/phone?phone=${encodeURIComponent(fromPhone)}&limit=50`);
      console.log('📡 [fetchConversation] Response status:', resp.status);
      const data = await resp.json();
      console.log('📋 [fetchConversation] Data received:', data);
      console.log('📊 [fetchConversation] Items count:', data.items ? data.items.length : 0);
      return data;
    }
    
    async function sendReply(toPhone, message) {
      console.log('📤 [sendReply] Starting send request:', { to: toPhone, messageLength: message.length });
      try {
        const response = await fetch('/dashboard/api/send-reply', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json'
          },
          body: JSON.stringify({
            to: toPhone,
            message: message
          })
        });
        console.log('📡 [sendReply] Response status:', response.status);
        const result = await response.json();
        console.log('📋 [sendReply] Result:', result);
        return result;
      } catch (error) {
        console.error('💥 [sendReply] Error:', error);
        return { success: false, error: error.message };
      }
    }
    
    function showStatusMessage(message, isSuccess = true) {
      const statusDiv = document.getElementById('statusMessage');
      statusDiv.className = `status-message ${isSuccess ? 'status-success' : 'status-error'}`;
      statusDiv.textContent = message;
      statusDiv.style.display = 'block';
      
      // Hide after 3 seconds
      setTimeout(() => {
        statusDiv.style.display = 'none';
      }, 3000);
    }
    
    function showThreadLoading() {
      const thread = document.getElementById('thread');
      thread.innerHTML = `
        <div class="empty-state">
          <div class="empty-state-icon">⏳</div>
          <h3>Loading conversation...</h3>
          <p>Please wait while we fetch the messages</p>
        </div>
      `;
    }
    
    function updateThreadPadding() {
      const reply = document.getElementById('replySection');
      const thread = document.getElementById('thread');
      if (!reply || !thread) {
        console.warn('⚠️ [updateThreadPadding] Required elements not found:', { reply: !!reply, thread: !!thread });
        return;
      }
      const visible = reply.style.display !== 'none';
      const replyHeight = visible ? reply.getBoundingClientRect().height : 0;
      thread.style.paddingBottom = (replyHeight + 12) + 'px';
    }

    function ensureMobileComposer() {
      const reply = document.getElementById('replySection');
      if (!reply) {
        console.warn('⚠️ [ensureMobileComposer] replySection element not found');
        return;
      }
      
      if (window.innerWidth <= 768) {
        reply.style.position = 'fixed';
        reply.style.left = '0';
        reply.style.right = '0';
        reply.style.bottom = '0';
        reply.style.zIndex = '150';
      } else {
        reply.style.position = '';
        reply.style.left = '';
        reply.style.right = '';
        reply.style.bottom = '';
        reply.style.zIndex = '';
      }
      updateThreadPadding();
    }
    
    function renderConversations(convs) {
      console.log('🎨 [renderConversations] Starting render with data:', convs);
      const list = document.getElementById('convList');
      list.innerHTML = '';
      const phoneFilter = document.getElementById('phoneFilter').value.trim();
      const onlyEsc = document.getElementById('onlyEscalations').checked;
      console.log('🔧 [renderConversations] Filters:', { phoneFilter, onlyEsc });
      
      // Build phone list from BigQuery data like in BQ dashboard
      const seen = new Set();
      const conversations = [];
      (convs || []).forEach(r => {
        const phone = r.from_phone || r.phone;
        if (!phone) return;
        if (!seen.has(phone)) {
          seen.add(phone);
          
          // Check if this conversation has escalation indicators
          const bot = (r.bot_reply || "").toLowerCase();
          const meta = r.metadata || {};
          const isEscalation = (
            "transfer you to a team member" in bot ||
            "connecting you with a human" in bot ||
            (typeof meta === 'object' && meta.escalation === true)
          );
          
          conversations.push({
            from_phone: phone,
            to_phone: r.to_phone || 'Support',
            thread_id: r.thread_id || '',
            timestamp: r.timestamp || '',
            last_message: (r.user_question || r.bot_reply || '').slice(0, 120),
            needs_escalation: isEscalation,
            needs_human_agent: isEscalation,
            last_updated: r.timestamp || ''
          });
        }
      });
      
      console.log('📊 [renderConversations] Built conversations:', conversations.length);
      
      const filtered = conversations.filter(c => {
        const matchPhone = !phoneFilter || (c.from_phone && c.from_phone.includes(phoneFilter));
        const esc = (c.needs_escalation || c.needs_human_agent);
        return matchPhone && (!onlyEsc || esc);
      });
      
      console.log('📊 [renderConversations] Filtered conversations:', filtered.length);
      
      if (filtered.length === 0) {
        console.log('📭 [renderConversations] No conversations to show');
        list.innerHTML = '<div class="empty-state"><div class="empty-state-icon">🔍</div><p>No conversations found</p></div>';
        return;
      }
      
      console.log('✅ [renderConversations] Rendering', filtered.length, 'conversations');
      
      filtered.forEach((c, index) => {
        console.log(`🎯 [renderConversations] Rendering conversation ${index + 1}:`, c.from_phone);
        const div = document.createElement('div');
        div.className = 'conv';
        const esc = (c.needs_escalation || c.needs_human_agent);
        
        div.innerHTML = `
          <div class="conv-header">
            <div class="conv-phones"><strong>${c.from_phone || ''}</strong> → ${c.to_phone || ''}</div>
            ${esc ? '<span class="badge">escalation</span>' : ''}
          </div>
          <div class="conv-meta">Updated: ${c.last_updated || ''} | Trace: ${c.thread_id || ''}</div>
          <div class="conv-preview">${c.last_message || ''}</div>
        `;
        
        div.onclick = async () => {
          console.log('🖱️ [renderConversations] Conversation clicked:', c.from_phone);
          
          // Update active state
          if (activeConvElement) {
            activeConvElement.classList.remove('active');
          }
          div.classList.add('active');
          activeConvElement = div;
          
          currentFromPhone = c.from_phone;
          currentToPhone = c.to_phone;
          
          // Close sidebar on mobile
          if (window.innerWidth <= 768) {
            document.getElementById('sidebar').classList.remove('open');
          }
          
          // Show loading state
          console.log('⏳ [renderConversations] Showing loading state');
          showThreadLoading();
          
          try {
            console.log('🔄 [renderConversations] Fetching conversation data for:', c.from_phone);
            const data = await fetchConversation(c.from_phone, c.to_phone);
            
            console.log('📝 [renderConversations] Updating conversation meta');
            document.getElementById('convMeta').innerHTML = `
              <strong>Conversation Details</strong><br>
              From: ${c.from_phone} → ${c.to_phone}<br>
              Trace ID: ${c.thread_id || 'N/A'}<br>
              Messages: ${data.items ? data.items.length : 0}
            `;
            document.getElementById('phoneDisplay').innerHTML = `📱 Reply to: <strong>${c.from_phone}</strong>`;
            
            const thread = document.getElementById('thread');
            thread.innerHTML = '';
            
            if (!data.items || data.items.length === 0) {
              console.log('📭 [renderConversations] No messages found');
              thread.innerHTML = '<div class="empty-state"><div class="empty-state-icon">💭</div><p>No messages found for this phone number</p></div>';
            } else {
              console.log('💬 [renderConversations] Rendering', data.items.length, 'messages');
              // Render BigQuery data (newest first, so reverse)
              const items = data.items.slice().reverse();
              items.forEach((r, msgIndex) => {
                console.log(`📝 [renderConversations] Message ${msgIndex + 1}:`, { user: !!r.user_question, bot: !!r.bot_reply });
                if (r.user_question) {
                  const um = document.createElement('div');
                  um.className = 'msg human';
                  um.textContent = r.user_question;
                  thread.appendChild(um);
                }
                if (r.bot_reply) {
                  const am = document.createElement('div');
                  am.className = 'msg ai';
                  am.textContent = r.bot_reply;
                  thread.appendChild(am);
                }
              });
              thread.scrollTop = thread.scrollHeight;
            }
            
            // Show reply section
            console.log('✅ [renderConversations] Showing reply section');
            const replySection = document.getElementById('replySection');
            if (replySection) {
              replySection.style.display = 'block';
            } else {
              console.warn('⚠️ [renderConversations] replySection element not found');
            }
            
            ensureMobileComposer();
            
            // Focus reply input on desktop
            if (window.innerWidth > 768) {
              setTimeout(() => document.getElementById('replyInput').focus(), 100);
            }
            // Adjust padding for mobile composer overlay
            setTimeout(updateThreadPadding, 50);
          } catch (error) {
            console.error('💥 [renderConversations] Error loading conversation:', error);
            const thread = document.getElementById('thread');
            thread.innerHTML = '<div class="empty-state"><div class="empty-state-icon">❌</div><h3>Failed to load</h3><p>Could not load conversation messages</p></div>';
            showStatusMessage('Failed to load conversation', false);
          }
        };
        list.appendChild(div);
      });
    }
    
    async function handleSendReply() {
      const replyInput = document.getElementById('replyInput');
      const sendBtn = document.getElementById('sendBtn');
      const message = replyInput.value.trim();
      
      if (!message || !currentFromPhone) {
        showStatusMessage('Please enter a message and select a conversation', false);
        return;
      }
      
      // Disable send button and show loading
      sendBtn.disabled = true;
      sendBtn.innerHTML = '<span style="display: inline-flex; align-items: center; gap: 0.5rem;"><span class="spinner"></span>Sending...</span>';
      
      try {
        const result = await sendReply(currentFromPhone, message);
        
        if (result.success) {
          showStatusMessage('✅ Message sent successfully!', true);
          replyInput.value = '';
          autoResize(replyInput);
          
          // Add message to current thread
          const thread = document.getElementById('thread');
          const emptyState = thread.querySelector('.empty-state');
          if (emptyState) {
            emptyState.remove();
          }
          
          const mdiv = document.createElement('div');
          mdiv.className = 'msg ai';
          mdiv.textContent = message;
          thread.appendChild(mdiv);
          thread.scrollTop = thread.scrollHeight;
        } else {
          showStatusMessage(`❌ Failed: ${result.error}`, false);
        }
      } catch (error) {
        showStatusMessage(`❌ Error: ${error.message}`, false);
      } finally {
        sendBtn.disabled = false;
        sendBtn.innerHTML = 'Send';
      }
    }
    
    async function refreshCurrentConversation() {
      if (!currentFromPhone) {
        showStatusMessage('Select a conversation to refresh', false);
        return;
      }
      const btn = document.getElementById('refreshBtn');
      const original = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = '<span style="display: inline-flex; align-items: center; gap: 0.5rem;"><span class="spinner"></span>Refreshing...</span>';
      try {
        showThreadLoading();
        const data = await fetchConversation(currentFromPhone, currentToPhone);
        const metaDiv = document.getElementById('convMeta');
        const thread = document.getElementById('thread');
        const phoneDisplay = document.getElementById('phoneDisplay');
        thread.innerHTML = '';
        const first = (data.items && data.items[0]) || {};
        const traceId = first.thread_id || 'N/A';
        metaDiv.innerHTML = `
          <strong>Conversation Details</strong><br>
          From: ${currentFromPhone} → ${currentToPhone || ''}<br>
          Trace ID: ${traceId}<br>
          Messages: ${data.items ? data.items.length : 0}
        `;
        if (phoneDisplay) {
          phoneDisplay.innerHTML = `📱 Reply to: <strong>${currentFromPhone}</strong>`;
        }
        if (!data.items || data.items.length === 0) {
          thread.innerHTML = '<div class="empty-state"><div class="empty-state-icon">💭</div><p>No messages found for this phone number</p></div>';
        } else {
          const items = data.items.slice().reverse();
          items.forEach(r => {
            if (r.user_question) {
              const um = document.createElement('div');
              um.className = 'msg human';
              um.textContent = r.user_question;
              thread.appendChild(um);
            }
            if (r.bot_reply) {
              const am = document.createElement('div');
              am.className = 'msg ai';
              am.textContent = r.bot_reply;
              thread.appendChild(am);
            }
          });
          thread.scrollTop = thread.scrollHeight;
        }
        ensureMobileComposer();
        setTimeout(updateThreadPadding, 50);
      } catch (e) {
        console.error('💥 [refreshCurrentConversation] Error:', e);
        showStatusMessage('Failed to refresh conversation', false);
      } finally {
        btn.disabled = false;
        btn.innerHTML = 'Refresh';
      }
    }
    
    // Event listeners
    document.getElementById('sendBtn').addEventListener('click', handleSendReply);
    
    const replyInput = document.getElementById('replyInput');
    replyInput.addEventListener('input', () => { autoResize(replyInput); updateThreadPadding(); });
    replyInput.addEventListener('keypress', function(e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleSendReply();
      }
    });
    document.getElementById('refreshBtn').addEventListener('click', refreshCurrentConversation);
    
    async function load() {
      console.log('🚀 [load] Starting dashboard load');
      // Show loading in sidebar
      const convList = document.getElementById('convList');
      convList.innerHTML = '<div class="empty-state"><div class="empty-state-icon">⏳</div><h3>Loading conversations...</h3><p>Fetching recent conversations from BigQuery</p></div>';
      
      try {
        console.log('📡 [load] Fetching all conversations');
        const all = await fetchAll();
        const conversations = all.items || [];
        
        console.log('📊 [load] Processing conversations:', conversations.length);
        
        if (conversations.length === 0) {
          console.log('📭 [load] No conversations found');
          // Show helpful message when no conversations found
          convList.innerHTML = `
            <div class="empty-state">
              <div class="empty-state-icon">📭</div>
              <h3>No Recent Conversations</h3>
              <p>No conversations found in BigQuery database.</p>
              <div style="margin-top: 1rem; padding: 1rem; background: #f0f9ff; border: 1px solid #0891b2; border-radius: 8px; font-size: 0.9rem;">
                <strong>💡 Note:</strong> Conversations appear here after users interact with the WhatsApp bot.
              </div>
            </div>
          `;
          return;
        }
        
        console.log('🎨 [load] Rendering conversations');
        renderConversations(conversations);
        
        console.log('🔧 [load] Setting up event listeners');
        document.getElementById('phoneFilter').addEventListener('input', () => {
          console.log('🔍 [load] Phone filter changed');
          renderConversations(conversations);
        });
        document.getElementById('onlyEscalations').addEventListener('change', () => {
          console.log('🚨 [load] Escalations filter changed');
          renderConversations(conversations);
        });
        
        console.log('✅ [load] Dashboard loaded successfully');
      } catch (error) {
        console.error('💥 [load] Failed to load conversations:', error);
        convList.innerHTML = `
          <div class="empty-state">
            <div class="empty-state-icon">❌</div>
            <h3>Failed to Load</h3>
            <p>Could not fetch conversations from BigQuery.</p>
            <div style="margin-top: 1rem; padding: 1rem; background: #fef2f2; border: 1px solid #f87171; border-radius: 8px; font-size: 0.9rem;">
              <strong>Check:</strong> Ensure BigQuery is properly configured and accessible.
            </div>
          </div>
        `;
        showStatusMessage('Failed to load conversations from BigQuery', false);
      }
    }
    
    // Handle window resize
    window.addEventListener('resize', () => {
      if (window.innerWidth > 768) {
        document.getElementById('sidebar').classList.remove('open');
      }
      ensureMobileComposer();
    });
    
    load();
    // Initial padding compute
    window.addEventListener('load', () => { ensureMobileComposer(); updateThreadPadding(); });
  </script>
</body>
</html>
"""

BQ_DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Support Dashboard - BigQuery</title>
  <style>
    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }
    
    body { 
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
      background: #f0f9ff;
      color: #334155;
      line-height: 1.6;
    }
    
    header { 
      background: linear-gradient(135deg, #0891b2 0%, #0e7490 100%);
      color: white; 
      padding: 1rem;
      box-shadow: 0 2px 10px rgba(0,0,0,0.1);
      position: sticky;
      top: 0;
      z-index: 100;
    }
    
    .header-content {
      display: flex;
      justify-content: space-between;
      align-items: center;
      max-width: 1200px;
      margin: 0 auto;
    }
    
    .header-title {
      font-size: 1.5rem;
      font-weight: 600;
    }
    
    .mobile-menu-btn {
      display: none;
      background: rgba(255,255,255,0.2);
      border: none;
      color: white;
      padding: 0.5rem;
      border-radius: 0.5rem;
      cursor: pointer;
      font-size: 1.2rem;
    }
    
    main { 
      display: flex; 
      min-height: calc(100vh - 80px);
      max-width: 1200px;
      margin: 0 auto;
      gap: 1rem;
      padding: 1rem;
    }
    
    #sidebar { 
      width: 360px;
      background: white;
      border-radius: 12px;
      box-shadow: 0 4px 20px rgba(0,0,0,0.08);
      padding: 1.5rem;
      height: fit-content;
      position: sticky;
      top: 100px;
    }
    
    #content { 
      flex: 1;
      background: white;
      border-radius: 12px;
      box-shadow: 0 4px 20px rgba(0,0,0,0.08);
      padding: 1.5rem;
      display: flex; 
      flex-direction: column;
      height: calc(100vh - 140px);
      max-height: calc(100vh - 140px);
    }
    
    .toolbar { 
      display: flex; 
      flex-direction: column; 
      gap: 1rem; 
      margin-bottom: 1.5rem;
      padding-bottom: 1rem;
      border-bottom: 1px solid #e2e8f0;
    }
    
    .toolbar-section {
      padding: 1rem;
      background: #f8fafc;
      border-radius: 8px;
      border: 1px solid #e2e8f0;
    }
    
    .toolbar-section label {
      display: block;
      margin-bottom: 0.5rem;
      font-weight: 500;
      color: #374151;
      font-size: 0.9rem;
    }
    
    .toolbar input[type=text] { 
      width: 100%;
      padding: 0.75rem;
      border: 2px solid #e2e8f0;
      border-radius: 8px;
      font-size: 0.95rem;
      transition: all 0.2s ease;
      margin-bottom: 0.75rem;
    }
    
    .toolbar input[type=text]:focus {
      outline: none;
      border-color: #0891b2;
      box-shadow: 0 0 0 3px rgba(8, 145, 178, 0.1);
    }
    
    .toolbar button { 
      width: 100%;
      padding: 0.75rem 1rem;
      background: linear-gradient(135deg, #0891b2, #0e7490);
      color: white;
      border: none;
      border-radius: 8px;
      cursor: pointer;
      font-weight: 500;
      font-size: 0.9rem;
      transition: all 0.2s ease;
      box-shadow: 0 2px 8px rgba(8, 145, 178, 0.3);
    }
    
    .toolbar button:hover:not(:disabled) {
      transform: translateY(-1px);
      box-shadow: 0 4px 12px rgba(8, 145, 178, 0.4);
    }
    
    .toolbar button:disabled { 
      background: #94a3b8;
      cursor: not-allowed;
      transform: none;
      box-shadow: none;
    }
    
    .checkbox-label {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      font-size: 0.9rem;
      color: #64748b;
      cursor: pointer;
      margin-bottom: 0.75rem;
    }
    
    .checkbox-label input[type=checkbox] {
      width: 1rem;
      height: 1rem;
      accent-color: #0891b2;
    }
    
    .conv { 
      padding: 1rem;
      border-radius: 8px;
      margin-bottom: 0.5rem;
      cursor: pointer;
      transition: all 0.2s ease;
      border: 1px solid transparent;
    }
    
    .conv:hover { 
      background: #f0f9ff;
      border-color: #e0f2fe;
      transform: translateY(-1px);
    }
    
    .conv.active {
      background: #e0f2fe;
      border-color: #0891b2;
    }
    
    .conv-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 0.5rem;
    }
    
    .conv-phone {
      font-weight: 600;
      color: #1e293b;
    }
    
    .conv-meta {
      color: #64748b;
      font-size: 0.8rem;
      margin-bottom: 0.25rem;
    }
    
    .conv-preview {
      color: #64748b;
      font-size: 0.85rem;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }
    
    .thread { 
      flex: 1;
      overflow-y: auto;
      overflow-x: hidden;
      padding-right: 0.5rem;
      margin-bottom: 1rem;
      min-height: 0;
      max-height: 100%;
    }
    
    .thread::-webkit-scrollbar {
      width: 6px;
    }
    
    .thread::-webkit-scrollbar-track {
      background: #f1f5f9;
      border-radius: 3px;
    }
    
    .thread::-webkit-scrollbar-thumb {
      background: #cbd5e1;
      border-radius: 3px;
    }
    
    .thread::-webkit-scrollbar-thumb:hover {
      background: #94a3b8;
    }
    
    .msg { 
      margin: 0.75rem 0;
      padding: 0.875rem 1.125rem;
      border-radius: 16px;
      max-width: 85%;
      box-shadow: 0 2px 8px rgba(0,0,0,0.06);
      animation: fadeInUp 0.3s ease-out;
    }
    
    @keyframes fadeInUp {
      from {
        opacity: 0;
        transform: translateY(10px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }
    
    .human { 
      background: linear-gradient(135deg, #0891b2, #0e7490);
      color: white;
      align-self: flex-start;
      border-bottom-left-radius: 6px;
    }
    
    .ai { 
      background: #f0f9ff;
      color: #334155;
      align-self: flex-end;
      border: 1px solid #e0f2fe;
      border-bottom-right-radius: 6px;
    }
    
    .meta { 
      color: #64748b;
      font-size: 0.85rem;
      margin-bottom: 1rem;
      padding: 0.75rem;
      background: #f8fafc;
      border-radius: 8px;
      border-left: 3px solid #0891b2;
    }
    
    .reply-section { 
      border-top: 2px solid #e2e8f0;
      padding-top: 1.5rem;
      margin-top: auto;
      flex-shrink: 0;
      background: white;
    }
    
    .phone-display { 
      background: linear-gradient(135deg, #059669, #047857);
      color: white;
      padding: 0.75rem 1rem;
      border-radius: 8px;
      margin-bottom: 1rem;
      font-weight: 500;
      font-size: 0.9rem;
    }
    
    .reply-box { 
      display: flex;
      gap: 0.75rem;
      align-items: flex-end;
    }
    
    .reply-input { 
      flex: 1;
      padding: 0.875rem 1rem;
      border: 2px solid #e2e8f0;
      border-radius: 12px;
      font-size: 0.95rem;
      resize: none;
      min-height: 44px;
      max-height: 120px;
      transition: all 0.2s ease;
    }
    
    .reply-input:focus {
      outline: none;
      border-color: #0891b2;
      box-shadow: 0 0 0 3px rgba(8, 145, 178, 0.1);
    }
    
    .send-btn { 
      background: linear-gradient(135deg, #0891b2, #0e7490);
      color: white;
      border: none;
      border-radius: 12px;
      padding: 0.875rem 1.5rem;
      font-weight: 600;
      font-size: 0.9rem;
      cursor: pointer;
      transition: all 0.2s ease;
      box-shadow: 0 4px 12px rgba(8, 145, 178, 0.3);
      min-width: 80px;
    }
    
    .send-btn:hover:not(:disabled) {
      transform: translateY(-2px);
      box-shadow: 0 6px 20px rgba(8, 145, 178, 0.4);
    }
    
    .send-btn:active {
      transform: translateY(0);
    }
    
    .send-btn:disabled { 
      background: #94a3b8;
      cursor: not-allowed;
      transform: none;
      box-shadow: none;
    }
    
    .status-message { 
      padding: 0.75rem 1rem;
      border-radius: 8px;
      margin-bottom: 1rem;
      font-size: 0.9rem;
      font-weight: 500;
      animation: slideDown 0.3s ease-out;
    }
    
    @keyframes slideDown {
      from {
        opacity: 0;
        transform: translateY(-10px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }
    
    .status-success { 
      background: #dcfce7;
      color: #166534;
      border: 1px solid #bbf7d0;
    }
    
    .status-error { 
      background: #fef2f2;
      color: #991b1b;
      border: 1px solid #fecaca;
    }
    
    .empty-state {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      height: 300px;
      color: #64748b;
      text-align: center;
    }
    
    .empty-state-icon {
      font-size: 3rem;
      margin-bottom: 1rem;
      opacity: 0.5;
    }
    
    .spinner {
      width: 1rem;
      height: 1rem;
      border: 2px solid rgba(255, 255, 255, 0.3);
      border-top: 2px solid white;
      border-radius: 50%;
      animation: spin 1s linear infinite;
    }
    
    @keyframes spin {
      0% { transform: rotate(0deg); }
      100% { transform: rotate(360deg); }
    }
    
    .tip-text {
      color: #64748b;
      font-size: 0.8rem;
      margin-top: 0.5rem;
      padding: 0.5rem;
      background: #f8fafc;
      border-radius: 6px;
      border-left: 3px solid #0891b2;
    }
    
    /* Mobile Styles */
    @media (max-width: 768px) {
      body {
        height: 100vh;
        height: -webkit-fill-available;
        overflow: hidden;
      }
      
      .mobile-menu-btn {
        display: block;
      }
      
      header {
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        z-index: 100;
      }
      
      main {
        flex-direction: column;
        padding: 0.5rem;
        gap: 0.5rem;
        height: calc(100vh - 80px);
        height: calc(-webkit-fill-available - 80px);
        margin-top: 80px;
        overflow: hidden;
      }
      
      #sidebar {
        width: 100%;
        position: fixed;
        top: 80px;
        left: -100%;
        height: calc(100vh - 80px);
        height: calc(-webkit-fill-available - 80px);
        z-index: 50;
        transition: left 0.3s ease;
        overflow-y: auto;
        -webkit-overflow-scrolling: touch;
      }
      
      #sidebar.open {
        left: 0;
      }
      
      #content {
        margin-top: 0;
        height: calc(100vh - 100px);
        height: calc(-webkit-fill-available - 100px);
        max-height: calc(100vh - 100px);
        max-height: calc(-webkit-fill-available - 100px);
        overflow: hidden;
        display: flex;
        flex-direction: column;
      }
      
      .thread {
        -webkit-overflow-scrolling: touch;
        touch-action: pan-y;
        height: 100%;
        overflow-y: scroll;
        flex: 1 1 0;
        min-height: 0;
      }
      
      .reply-section {
        position: fixed;
        left: 0;
        right: 0;
        bottom: env(safe-area-inset-bottom, 0);
        background: white;
        padding: 0.75rem 0.75rem calc(0.75rem + env(safe-area-inset-bottom, 0));
        border-top: 2px solid #e2e8f0;
        margin-top: 0;
        flex-shrink: 0;
        box-shadow: 0 -2px 10px rgba(0,0,0,0.1);
        z-index: 150;
      }
      
      .toolbar {
        gap: 0.75rem;
      }
      
      .toolbar-section {
        padding: 0.75rem;
      }
      
      .conv {
        padding: 0.75rem;
      }
      
      .msg {
        max-width: 90%;
        padding: 0.75rem 1rem;
      }
      
      .reply-box {
        flex-direction: column;
        gap: 0.5rem;
      }
      
      .reply-input {
        min-height: 50px;
        font-size: 16px; /* Prevents zoom on iOS */
      }
      
      .send-btn {
        width: 100%;
        padding: 1rem;
        font-size: 16px;
      }
      
      .toolbar input[type=text] {
        font-size: 16px; /* Prevents zoom on iOS */
      }
      
      .meta {
        flex-shrink: 0;
        margin-bottom: 0.5rem;
      }
    }
    
    @media (max-width: 480px) {
      main {
        padding: 0.25rem;
      }
      
      #sidebar,
      #content {
        border-radius: 8px;
        padding: 1rem;
      }
      
      .msg {
        max-width: 95%;
        padding: 0.625rem 0.875rem;
      }
    }
    
    /* Dark mode support */
    @media (prefers-color-scheme: dark) {
      body {
        background: #0f172a;
        color: #e2e8f0;
      }
      
      #sidebar,
      #content {
        background: #1e293b;
        box-shadow: 0 4px 20px rgba(0,0,0,0.3);
      }
      
      .toolbar-section {
        background: #334155;
        border-color: #475569;
      }
      
      .conv:hover {
        background: #334155;
      }
      
      .conv.active {
        background: #1e3a8a;
      }
      
      .ai {
        background: #334155;
        color: #e2e8f0;
        border-color: #475569;
      }
      
      .meta {
        background: #334155;
        color: #94a3b8;
      }
      
      .toolbar input[type=text],
      .reply-input {
        background: #334155;
        border-color: #475569;
        color: #e2e8f0;
      }
      
      .toolbar input[type=text]:focus,
      .reply-input:focus {
        border-color: #0891b2;
      }
      
      .tip-text {
        background: #334155;
        color: #94a3b8;
      }
    }
  </style>
</head>
<body>
  <header>
          <div class="header-content">
        <div class="header-title">Support Dashboard - BigQuery</div>
        <div style="display: flex; align-items: center; gap: 1rem;">
          <a href="/dashboard" style="color: rgba(255,255,255,0.9); text-decoration: none; font-size: 0.9rem; padding: 0.5rem 1rem; background: rgba(255,255,255,0.1); border-radius: 6px; transition: all 0.2s ease;">
            📋 Recent View
          </a>
          <button class="mobile-menu-btn" onclick="toggleSidebar()">☰</button>
        </div>
      </div>
  </header>
  <main>
    <section id=\"sidebar\">
      <div class=\"toolbar\">
        <div class="toolbar-section">
          <label>🔍 Search by Phone</label>
          <input id=\"bqPhone\" placeholder=\"Enter user phone (e.g. 9198...)\" />
          <button onclick=\"loadByPhone()\">Load by Phone</button>
        </div>
        <div class="toolbar-section">
          <label>🔎 Search by Trace ID</label>
          <input id=\"bqTrace\" placeholder=\"Enter trace id (e.g. a1b2c3d4)\" />
          <button onclick=\"loadByTrace()\">Load by Trace</button>
        </div>
        <div class="toolbar-section">
          <label class=\"checkbox-label\">
            <input type=\"checkbox\" id=\"bqOnlyEsc\" />
            Only escalations
          </label>
          <button onclick=\"loadEscalations()\">Load Escalations</button>
        </div>
        <div class="toolbar-section">
          <label>📞 Filter Numbers</label>
          <input id=\"bqPhoneFilter\" placeholder=\"Type to filter numbers...\" />
          <div class="tip-text">💡 Tip: Use /dashboard/api/bq/phone and /dashboard/api/bq/thread for programmatic access</div>
        </div>
      </div>
      <div id=\"bqConvList\"></div>
    </section>
    <section id=\"content\">
      <div id=\"convMeta\" class=\"meta\"></div>
      <div id=\"thread\" class=\"thread\">
        <div class="empty-state">
          <div class="empty-state-icon">🗃️</div>
          <h3>Load a conversation</h3>
          <p>Use the search options in the sidebar to load conversations from BigQuery</p>
        </div>
      </div>
      <div id\"replySection\" class=\"reply-section\" style=\"display: none;\">
        <div id\"phoneDisplay\" class=\"phone-display\"></div>
        <div id\"statusMessage\"></div>
        <div class=\"reply-box\">
          <textarea id\"replyInput\" class=\"reply-input\" placeholder=\"Type your reply here...\" rows=\"1\"></textarea>
                     <button id\"sendBtn\" class=\"send-btn\">Send</button>
                     <button id\"refreshBtn\" class=\"send-btn\" title=\"Refresh conversation\">Refresh</button>
                     <button id\"resetBotBtn\" class=\"send-btn\" title=\"Reset to bot mode\">Reset to Bot</button>
        </div>
      </div>
    </section>
  </main>
  <script>
    let recentPhones = [];
    let currentReplyPhone = null;
    let activeConvElement = null;
    
    function toggleSidebar() {
      const sidebar = document.getElementById('sidebar');
      sidebar.classList.toggle('open');
    }
    
    // Close sidebar when clicking outside on mobile
    document.addEventListener('click', function(e) {
      const sidebar = document.getElementById('sidebar');
      const menuBtn = document.querySelector('.mobile-menu-btn');
      
      if (window.innerWidth <= 768 && sidebar.classList.contains('open')) {
        if (!sidebar.contains(e.target) && !menuBtn.contains(e.target)) {
          sidebar.classList.remove('open');
        }
      }
    });
    
    // Auto-resize textarea
    function autoResize(textarea) {
      textarea.style.height = 'auto';
      textarea.style.height = Math.min(textarea.scrollHeight, 120) + 'px';
    }

    async function fetchBQPhone(phone, limit=200) {
      console.log('🔍 [BQ fetchBQPhone] Starting phone fetch:', phone);
      const resp = await fetch(`/dashboard/api/bq/phone?phone=${encodeURIComponent(phone)}&limit=${limit}`);
      console.log('📡 [BQ fetchBQPhone] Response status:', resp.status);
      const data = await resp.json();
      console.log('📋 [BQ fetchBQPhone] Data received:', data);
      console.log('📊 [BQ fetchBQPhone] Items count:', data.items ? data.items.length : 0);
      return data;
    }
    async function fetchBQTrace(trace, limit=200) {
      console.log('🔍 [BQ fetchBQTrace] Starting trace fetch:', trace);
      const resp = await fetch(`/dashboard/api/bq/thread?thread_id=${encodeURIComponent(trace)}&limit=${limit}`);
      console.log('📡 [BQ fetchBQTrace] Response status:', resp.status);
      const data = await resp.json();
      console.log('📋 [BQ fetchBQTrace] Data received:', data);
      console.log('📊 [BQ fetchBQTrace] Items count:', data.items ? data.items.length : 0);
      return data;
    }
    async function fetchBQRecent(limit=200) {
      console.log('🔍 [BQ fetchBQRecent] Starting recent fetch, limit:', limit);
      const resp = await fetch(`/dashboard/api/bq/recent?limit=${limit}`);
      console.log('📡 [BQ fetchBQRecent] Response status:', resp.status);
      const data = await resp.json();
      console.log('📋 [BQ fetchBQRecent] Data received:', data);
      console.log('📊 [BQ fetchBQRecent] Items count:', data.items ? data.items.length : 0);
      return data;
    }
    async function fetchBQEscalations(limit=200) {
      console.log('🔍 [BQ fetchBQEscalations] Starting escalations fetch, limit:', limit);
      const resp = await fetch(`/dashboard/api/bq/escalations?limit=${limit}`);
      console.log('📡 [BQ fetchBQEscalations] Response status:', resp.status);
      const data = await resp.json();
      console.log('📋 [BQ fetchBQEscalations] Data received:', data);
      console.log('📊 [BQ fetchBQEscalations] Items count:', data.items ? data.items.length : 0);
      return data;
    }

    async function sendReply(toPhone, message) {
      console.log('📤 [BQ sendReply] Starting send request:', { to: toPhone, messageLength: message.length });
      try {
        const response = await fetch('/dashboard/api/send-reply', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json'
          },
          body: JSON.stringify({
            to: toPhone,
            message: message
          })
        });
        console.log('📡 [BQ sendReply] Response status:', response.status);
        const result = await response.json();
        console.log('📋 [BQ sendReply] Result:', result);
        return result;
      } catch (error) {
        console.error('💥 [BQ sendReply] Error:', error);
        return { success: false, error: error.message };
      }
    }
    
    function showStatusMessage(message, isSuccess = true) {
      const statusDiv = document.getElementById('statusMessage');
      statusDiv.className = `status-message ${isSuccess ? 'status-success' : 'status-error'}`;
      statusDiv.textContent = message;
      statusDiv.style.display = 'block';
      
      // Hide after 3 seconds
      setTimeout(() => {
        statusDiv.style.display = 'none';
      }, 3000);
    }

    function showThreadLoading() {
      const thread = document.getElementById('thread');
      thread.innerHTML = `
        <div class="empty-state">
          <div class="empty-state-icon">⏳</div>
          <h3>Loading conversation...</h3>
          <p>Please wait while we fetch the messages from BigQuery</p>
        </div>
      `;
    }

    function updateThreadPadding() {
      const reply = document.getElementById('replySection');
      const thread = document.getElementById('thread');
      if (!reply || !thread) {
        console.warn('⚠️ [updateThreadPadding] Required elements not found:', { reply: !!reply, thread: !!thread });
        return;
      }
      const visible = reply.style.display !== 'none';
      const replyHeight = visible ? reply.getBoundingClientRect().height : 0;
      thread.style.paddingBottom = (replyHeight + 12) + 'px';
    }

    function ensureMobileComposer() {
      const reply = document.getElementById('replySection');
      if (!reply) {
        console.warn('⚠️ [ensureMobileComposer] replySection element not found');
        return;
      }
      
      if (window.innerWidth <= 768) {
        reply.style.position = 'fixed';
        reply.style.left = '0';
        reply.style.right = '0';
        reply.style.bottom = '0';
        reply.style.zIndex = '150';
      } else {
        reply.style.position = '';
        reply.style.left = '';
        reply.style.right = '';
        reply.style.bottom = '';
        reply.style.zIndex = '';
      }
      updateThreadPadding();
    }

    function buildRecentPhoneList(rows) {
      // rows expected newest first
      const seen = new Set();
      const list = [];
      (rows.items || []).forEach(r => {
        const phone = r.from_phone || r.phone;
        if (!phone) return;
        if (!seen.has(phone)) {
          seen.add(phone);
          list.push({
            phone: phone,
            thread_id: r.thread_id || '',
            timestamp: r.timestamp || '',
            last_preview: (r.user_question || r.bot_reply || '').slice(0, 60),
          });
        }
      });
      return list;
    }

    function renderPhoneList() {
      const container = document.getElementById('bqConvList');
      container.innerHTML = '';
      const filter = (document.getElementById('bqPhoneFilter').value || '').trim();
      const items = recentPhones.filter(it => !filter || (it.phone && it.phone.includes(filter)));
      
      if (items.length === 0) {
        container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📱</div><p>No phone numbers found</p></div>';
        return;
      }
      
      items.forEach(it => {
        const div = document.createElement('div');
        div.className = 'conv';
        div.innerHTML = `
          <div class="conv-header">
            <div class="conv-phone"><strong>${it.phone}</strong></div>
          </div>
          <div class="conv-meta">${it.timestamp || ''}</div>
          <div class="conv-preview">${it.last_preview || ''}</div>
        `;
        div.onclick = async () => {
          // Update active state
          if (activeConvElement) {
            activeConvElement.classList.remove('active');
          }
          div.classList.add('active');
          activeConvElement = div;
          
          document.getElementById('bqPhone').value = it.phone;
          currentReplyPhone = it.phone;
          
          // Close sidebar on mobile
          if (window.innerWidth <= 768) {
            document.getElementById('sidebar').classList.remove('open');
          }
          
          await loadByPhone();
        };
        container.appendChild(div);
      });
    }

    function renderBQRows(rows) {
      const thread = document.getElementById('thread');
      const meta = document.getElementById('convMeta');
      const replySection = document.getElementById('replySection');
      const phoneDisplay = document.getElementById('phoneDisplay');
      
      if (!thread || !meta) {
        console.error('💥 [BQ renderBQRows] Required DOM elements not found:', { thread: !!thread, meta: !!meta });
        return;
      }
      
      thread.innerHTML = '';
      
      if (!rows || !rows.items || rows.items.length === 0) {
        meta.innerHTML = '<strong>No Data Found</strong><br>No conversation data available for the selected criteria.';
        thread.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📭</div><h3>No messages found</h3><p>Try adjusting your search criteria</p></div>';
        
        if (replySection) {
          replySection.style.display = 'none';
        } else {
          console.warn('⚠️ [BQ renderBQRows] replySection element not found');
        }
        
        updateThreadPadding();
        return;
      }
      
      const first = rows.items[0] || {};
      const phone = first.from_phone || first.phone || '';
      currentReplyPhone = phone;
      
      meta.innerHTML = `
        <strong>Conversation Data</strong><br>
        Thread ID: ${first.thread_id || 'N/A'}<br>
        Phone: ${phone}<br>
        Messages: ${rows.items.length}
      `;
      
      // Show reply section if we have a phone number
      if (phone) {
        if (phoneDisplay) {
          phoneDisplay.innerHTML = `📱 Reply to: <strong>${phone}</strong>`;
        } else {
          console.warn('⚠️ [BQ renderBQRows] phoneDisplay element not found');
        }
        
        if (replySection) {
          replySection.style.display = 'block';
        } else {
          console.warn('⚠️ [BQ renderBQRows] replySection element not found');
        }
        
        setTimeout(updateThreadPadding, 50);
      }
      
      // Rows contain user_question and bot_reply per interaction. Render as pairs, newest first.
      const items = rows.items.slice().reverse();
      items.forEach(r => {
        if (r.user_question) {
          const um = document.createElement('div');
          um.className = 'msg human';
          um.textContent = r.user_question;
          thread.appendChild(um);
        }
        if (r.bot_reply) {
          const am = document.createElement('div');
          am.className = 'msg ai';
          am.textContent = r.bot_reply;
          thread.appendChild(am);
        }
      });
      
      // Scroll to bottom
      thread.scrollTop = thread.scrollHeight;
    }

    async function loadByPhone() {
      console.log('🔍 [BQ loadByPhone] Starting load by phone');
      const phone = document.getElementById('bqPhone').value.trim();
      if (!phone) {
        console.log('❌ [BQ loadByPhone] No phone number provided');
        showStatusMessage('Please enter a phone number', false);
        return;
      }
      
      console.log('📞 [BQ loadByPhone] Phone number:', phone);
      
      // Show loading state
      showThreadLoading();
      
      currentReplyPhone = phone;
      const onlyEsc = document.getElementById('bqOnlyEsc').checked;
      console.log('🔧 [BQ loadByPhone] Only escalations:', onlyEsc);
      
      try {
        if (onlyEsc) {
          console.log('🚨 [BQ loadByPhone] Fetching escalations and filtering by phone');
          const rows = await fetchBQEscalations();
          // filter to phone
          rows.items = (rows.items || []).filter(r => (r.from_phone === phone || r.phone === phone));
          console.log('📊 [BQ loadByPhone] Filtered escalations:', rows.items.length);
          renderBQRows(rows);
          return;
        }
        console.log('📡 [BQ loadByPhone] Fetching phone conversations');
        const rows = await fetchBQPhone(phone);
        renderBQRows(rows);
      } catch (error) {
        console.error('💥 [BQ loadByPhone] Error:', error);
        const thread = document.getElementById('thread');
        thread.innerHTML = '<div class="empty-state"><div class="empty-state-icon">❌</div><h3>Failed to load</h3><p>Could not load conversation data from BigQuery</p></div>';
        showStatusMessage('Failed to load conversation data', false);
      }
    }
    
    async function loadByTrace() {
      console.log('🔍 [BQ loadByTrace] Starting load by trace');
      const trace = document.getElementById('bqTrace').value.trim();
      if (!trace) {
        console.log('❌ [BQ loadByTrace] No trace ID provided');
        showStatusMessage('Please enter a trace ID', false);
        return;
      }
      
      console.log('🧵 [BQ loadByTrace] Trace ID:', trace);
      
      // Show loading state
      showThreadLoading();
      
      const onlyEsc = document.getElementById('bqOnlyEsc').checked;
      console.log('🔧 [BQ loadByTrace] Only escalations:', onlyEsc);
      
      try {
        if (onlyEsc) {
          console.log('🚨 [BQ loadByTrace] Fetching escalations and filtering by trace');
          const rows = await fetchBQEscalations();
          rows.items = (rows.items || []).filter(r => (r.thread_id === trace));
          console.log('📊 [BQ loadByTrace] Filtered escalations:', rows.items.length);
          renderBQRows(rows);
          return;
        }
        console.log('📡 [BQ loadByTrace] Fetching trace conversations');
        const rows = await fetchBQTrace(trace);
        renderBQRows(rows);
      } catch (error) {
        console.error('💥 [BQ loadByTrace] Error:', error);
        const thread = document.getElementById('thread');
        thread.innerHTML = '<div class="empty-state"><div class="empty-state-icon">❌</div><h3>Failed to load</h3><p>Could not load conversation data from BigQuery</p></div>';
        showStatusMessage('Failed to load conversation data', false);
      }
    }
    
    async function loadEscalations() {
      console.log('🔍 [BQ loadEscalations] Starting load escalations');
      // Show loading state
      showThreadLoading();
      
      try {
        console.log('📡 [BQ loadEscalations] Fetching escalations');
        const rows = await fetchBQEscalations();
        renderBQRows(rows);
      } catch (error) {
        console.error('💥 [BQ loadEscalations] Error:', error);
        const thread = document.getElementById('thread');
        thread.innerHTML = '<div class="empty-state"><div class="empty-state-icon">❌</div><h3>Failed to load</h3><p>Could not load escalations from BigQuery</p></div>';
        showStatusMessage('Failed to load escalations', false);
      }
    }

    async function initRecentPhones() {
      console.log('🚀 [BQ initRecentPhones] Starting recent phones initialization');
      // Show loading in sidebar
      const container = document.getElementById('bqConvList');
      container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">⏳</div><h3>Loading phone numbers...</h3><p>Fetching recent conversations from BigQuery</p></div>';
      
      try {
        console.log('📡 [BQ initRecentPhones] Fetching recent conversations');
        const recent = await fetchBQRecent(300);
        console.log('📊 [BQ initRecentPhones] Processing phone list');
        recentPhones = buildRecentPhoneList(recent);
        console.log('📱 [BQ initRecentPhones] Built phone list:', recentPhones.length);
        renderPhoneList();
        document.getElementById('bqPhoneFilter').addEventListener('input', renderPhoneList);
        console.log('✅ [BQ initRecentPhones] Initialization complete');
      } catch (error) {
        console.error('💥 [BQ initRecentPhones] Error:', error);
        container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">❌</div><h3>Failed to load</h3><p>Could not fetch recent conversations</p></div>';
        showStatusMessage('Failed to load recent conversations', false);
      }
    }

    async function handleSendReply() {
      const replyInput = document.getElementById('replyInput');
      const sendBtn = document.getElementById('sendBtn');
      const message = replyInput.value.trim();
      
      if (!message || !currentReplyPhone) {
        showStatusMessage('Please enter a message and load a conversation', false);
        return;
      }
      
      // Disable send button and show loading
      sendBtn.disabled = true;
      sendBtn.innerHTML = '<span style="display: inline-flex; align-items: center; gap: 0.5rem;"><span class="spinner"></span>Sending...</span>';
      
      try {
        const result = await sendReply(currentReplyPhone, message);
        
        if (result.success) {
          showStatusMessage('✅ Message sent successfully!', true);
          replyInput.value = '';
          autoResize(replyInput);
          
          // Add message to current thread
          const thread = document.getElementById('thread');
          const emptyState = thread.querySelector('.empty-state');
          if (emptyState) {
            emptyState.remove();
          }
          
          const mdiv = document.createElement('div');
          mdiv.className = 'msg ai';
          mdiv.textContent = message;
          thread.appendChild(mdiv);
          thread.scrollTop = thread.scrollHeight;
        } else {
          showStatusMessage(`❌ Failed: ${result.error}`, false);
        }
      } catch (error) {
        showStatusMessage(`❌ Error: ${error.message}`, false);
      } finally {
        sendBtn.disabled = false;
        sendBtn.innerHTML = 'Send';
      }
    }

    async function refreshCurrentBQConversation() {
      const btn = document.getElementById('refreshBtn');
      const original = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = '<span style="display: inline-flex; align-items: center; gap: 0.5rem;"><span class="spinner"></span>Refreshing...</span>';
      try {
        if (currentReplyPhone) {
          await loadByPhone();
        } else if ((document.getElementById('bqTrace').value || '').trim()) {
          await loadByTrace();
        } else {
          showStatusMessage('Load a conversation first', false);
        }
      } catch (e) {
        console.error('💥 [refreshCurrentBQConversation] Error:', e);
        showStatusMessage('Failed to refresh conversation', false);
      } finally {
        btn.disabled = false;
        btn.innerHTML = 'Refresh';
      }
    }

    // Event listeners for reply functionality
    document.getElementById('sendBtn').addEventListener('click', handleSendReply);
    
    const replyInput = document.getElementById('replyInput');
    replyInput.addEventListener('input', () => { autoResize(replyInput); updateThreadPadding(); });
    replyInput.addEventListener('keypress', function(e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleSendReply();
      }
    });
    document.getElementById('refreshBtn').addEventListener('click', refreshCurrentBQConversation);

    // Handle window resize
    window.addEventListener('resize', () => {
      if (window.innerWidth > 768) {
        document.getElementById('sidebar').classList.remove('open');
      }
      ensureMobileComposer();
    });
    
    // initialize phone list on load
    initRecentPhones();
    window.addEventListener('load', () => { ensureMobileComposer(); updateThreadPadding(); });

    async function resetModeToBotBQ() {\n      if (!currentReplyPhone) {\n        showStatusMessage('Select or load a conversation first', false);\n        return;\n      }\n      try {\n        const resp = await fetch('/dashboard/api/set-mode-bot', {\n          method: 'POST',\n          headers: { 'Content-Type': 'application/json' },\n          body: JSON.stringify({ phone: currentReplyPhone })\n        });\n        const result = await resp.json();\n        if (result.success) {\n          showStatusMessage('Mode reset to bot');\n        } else {\n          showStatusMessage(result.error || 'Failed to reset mode', false);\n        }\n      } catch (e) {\n        showStatusMessage('Network error', false);\n      }\n    }\n

    const resetBtnBQ = document.getElementById('resetBotBtn');
    if (resetBtnBQ) { resetBtnBQ.addEventListener('click', resetModeToBotBQ); }
  </script>
</body>
</html>
"""

def is_authed(request: Request) -> bool:
    cookie_value = request.cookies.get(AUTH_COOKIE_NAME)
    print("cookie received "+ cookie_value)
    logger.debug(f"🔐 Auth check - Cookie '{AUTH_COOKIE_NAME}': {cookie_value}")
    return cookie_value == "1"

@router.get("/dashboard/login")
async def dashboard_login_page(request: Request):
    return Response(content=LOGIN_HTML.format(error_html=""), media_type="text/html")

@router.post("/dashboard/login")
async def dashboard_login(request: Request):
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = (form.get("password") or "").strip()
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        resp = RedirectResponse(url="/dashboard/bq", status_code=303)
        resp.set_cookie(
            AUTH_COOKIE_NAME,
            "1", 
            httponly=True, 
            samesite="lax",
            max_age=3600  # 1 hour expiry
        )
        return resp
    else:
        html_err = "<div class=\"error\">Invalid username or password</div>"
        return Response(content=LOGIN_HTML.format(error_html=html_err), media_type="text/html")

@router.get("/dashboard/logout")
async def dashboard_logout():
    resp = RedirectResponse(url="/dashboard/login", status_code=303)
    resp.delete_cookie(AUTH_COOKIE_NAME)
    return resp

@router.get("/dashboard")
def dashboard_page(request: Request) -> Response:
    if not is_authed(request):
        return RedirectResponse(url="/dashboard/login", status_code=303)
    return Response(content=DASHBOARD_HTML, media_type="text/html")

@router.get("/dashboard/api/escalations")
def list_escalations(request: Request) -> Dict[str, Any]:
    if not is_authed(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    items: List[Dict[str, Any]] = []
    for conv in list_conversations():
        if conv.get("needs_escalation") or conv.get("needs_human_agent"):
            items.append(conv)
    return {"items": items}

@router.get("/dashboard/api/conversations")
def list_all_conversations(request: Request) -> Dict[str, Any]:
    if not is_authed(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return {"items": list_conversations()}

@router.get("/dashboard/api/conversation")
async def get_conversation(request: Request, from_phone: str = Query(...), to_phone: str = Query(...)) -> Dict[str, Any]:
    if not is_authed(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    state = await aget_state_by_numbers(from_phone, to_phone)
    if not state:
        return {"messages": []}
    messages = []
    for msg in state.get("messages", []) or []:
        role = getattr(msg, 'type', '')
        content = getattr(msg, 'content', str(msg))
        if role == 'human':
            messages.append({"role": "user", "content": content})
        elif role == 'ai':
            messages.append({"role": "assistant", "content": content})
        else:
            messages.append({"role": role or "unknown", "content": content})
    return {
        "from_phone": from_phone,
        "to_phone": to_phone,
        "trace_id": state.get("trace_id"),
        "flags": {
            "needs_escalation": state.get("needs_escalation"),
            "needs_human_agent": state.get("needs_human_agent"),
            "is_frustrated": state.get("is_frustrated"),
        },
        "messages": messages,
    }

def normalize_india_phone(phone: str) -> str:
    """Normalize Indian phone numbers to start with 91 and be 12 digits total.
    - Accepts formats like 9876543210, +919876543210, 919876543210
    - Returns 919876543210 when possible; otherwise returns only digits.
    """
    if phone is None:
        return phone
    s = str(phone).strip()
    # Strip spaces and non-digits but keep leading + for normalization step
    s = re.sub(r"^\+91", "91", s)
    digits = re.sub(r"\D", "", s)
    if digits.startswith("91") and len(digits) == 12:
        return digits
    m = re.search(r"(\d{10})$", digits)
    if m:
        return "91" + m.group(1)
    return digits

@router.post("/dashboard/api/set-mode-bot")
async def set_mode_bot(request: Request):
	if not is_authed(request):
		return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
	try:
		data = await request.json()
		phone = (data.get("phone") or "").strip()
		phone = normalize_india_phone(phone)
		client_id = (data.get("client_id") or "").strip() or None
		if not phone:
			return JSONResponse({"success": False, "error": "Missing 'phone'"}, status_code=400)
		await bot_user_agent_mode.aset_conversation_mode(phone, "bot", client_id=client_id)
		logger.info(f"🔄 [Set Mode] Conversation mode reset to bot for {phone}, client_id={client_id}")
		return JSONResponse({"success": True})
	except Exception as e:
		logger.exception(f"💥 [Set Mode] Exception resetting mode for phone: {str(e)}")
		return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@router.post("/dashboard/api/set-mode-agent")
async def set_mode_agent(request: Request):
	if not is_authed(request):
		return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
	try:
		data = await request.json()
		phone = (data.get("phone") or "").strip()
		phone = normalize_india_phone(phone)
		client_id = (data.get("client_id") or "").strip() or None
		if not phone:
			return JSONResponse({"success": False, "error": "Missing 'phone'"}, status_code=400)
		await bot_user_agent_mode.aset_conversation_mode(phone, "agent", client_id=client_id)
		logger.info(f"🔄 [Set Mode] Conversation mode set to agent for {phone}, client_id={client_id}")
		return JSONResponse({"success": True})
	except Exception as e:
		logger.exception(f"💥 [Set Mode] Exception setting mode to agent for phone: {str(e)}")
		return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@router.post("/dashboard/api/send-reply")
async def send_reply(request: Request):
    """Send reply message via Gupshup webhook"""
    logger.info("📤 [Send Reply] Incoming request")
    if not is_authed(request):
        logger.warning("❌ [Send Reply] Unauthorized request")
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
    
    try:
        data = await request.json()
        to_phone = data.get("to")
        to_phone = normalize_india_phone(to_phone)
        message = data.get("message")
        conversation_id = data.get("conversation_id")
        client_id = (data.get("client_id") or "").strip() or None
        logger.info(f"📨 [Send Reply] Request data: to={to_phone}, conv_id={conversation_id}, client_id={client_id}, message_length={len(message) if message else 0}")

        if not to_phone or not message:
            logger.warning(f"❌ [Send Reply] Missing required fields: to={bool(to_phone)}, message={bool(message)}")
            return JSONResponse({"success": False, "error": "Missing 'to' or 'message'"}, status_code=400)

        # Call the send_message function directly
        import uuid
        trace_id = str(uuid.uuid4())[:8]  # Generate a short trace ID
        logger.info(f"📞 [Send Reply] Calling send_message: to={to_phone}, trace_id={trace_id}")
        current_status = await bot_user_agent_mode.aget_conversation_state(to_phone, client_id=client_id)
        current_mode = current_status['mode']

        # If bot mode is enabled, return a descriptive response for the caller
        if current_mode != "agent":
            logger.info(f"🤖 [Send Reply] Bot mode is enabled for {to_phone}; not sending agent reply")
            return JSONResponse({
                "success": False,
                "error": "Bot mode is enabled, can send message only in agent mode",
                "mode": current_mode,
                "bot_mode": True,
            }, status_code=200)

        result = None
        if current_mode == "agent":
            # Agent is allowed to send message to customer
            result = await send_message(to_phone, message, trace_id=trace_id, client_id=client_id)
            logger.info(f"📋 [Send Reply] send_message result: {result}, trace_id={trace_id}")

        if result is not None:
            logger.info(f"✅ [Send Reply] Message sent successfully to {to_phone}")
            
            # Persist agent reply in in-memory state for continuity when bot takes over
            try:
                from fashion_bot.state_cache import timestamped_ai_message
                state = await aget_state_by_numbers(to_phone, client_id)
                if state is None:
                    # Initialize a minimal state without adding a human message
                    state = {"messages": [], "phone_number": to_phone}
                if state.get("messages") is None:
                    state["messages"] = []
                state["messages"].append(timestamped_ai_message(message))
                await aupdate_state(to_phone, client_id, state)
                # Refresh last_activity to keep agent mode active
                await bot_user_agent_mode.aset_conversation_mode(to_phone, "agent", client_id=client_id)
            except Exception as _state_err:
                logger.warning(f"[Send Reply] Failed to persist agent reply to state: {str(_state_err)}")
            
            # Store agent reply in Postgres (messages table) with provided conversation_id if any
            try:
                from fashion_bot.config_manager import aresolve_client_id
                from fashion_bot.history.conversation_handler import astore_conversation_event  # ✅ Auto-converts templates
                # Use provided client_id if available, otherwise fallback
                if not client_id:
                    client_id = await aresolve_client_id(GUPSHUP_SOURCE)
                conv_id = await astore_conversation_event(
                    client_id=client_id,
                    phone=to_phone,
                    sender="support",
                    text=message,
                    channel_type="whatsapp",
                    started_by="agent",
                    conversation_id=conversation_id,
                )
                logger.info(f"🗄️ [Send Reply] Stored agent message in Postgres under conversation_id={conv_id}")
            except Exception as _pg_err:
                logger.warning(f"⚠️ [Send Reply] Postgres conversation store failed: {_pg_err}")
            
            # Publish agent reply to Redis inbound channel for UI
            try:
                from fashion_bot.gupshup_webhook import publish_inbound_to_redis
                await publish_inbound_to_redis(to_phone, message, sender="agent", client_id=client_id)
            except Exception as _redis_err:
                logger.warning(f"⚠️ [Send Reply] Redis publish failed: {_redis_err}")
             
            # DISABLED: BigQuery logging
            # try:
            #     from fashion_bot.history.bigquery_logger import log_conversation_to_bigquery
            #     from fashion_bot.gupshup_webhook import GUPSHUP_SOURCE
            #     
            #     logger.info(f"📊 [Send Reply] Logging to BigQuery: trace_id={trace_id}, to={to_phone}")
            #     
            #     # Schedule async BigQuery insert without blocking the response
            #     import asyncio
            #     asyncio.create_task(
            #         log_conversation_to_bigquery(
            #             user_question="",  # Dashboard replies don't have user questions
            #             bot_reply=message,
            #             thread_id=trace_id,
            #             from_phone=to_phone,  # From the user's phone (for conversation continuity)
            #             to_phone=GUPSHUP_SOURCE,  # To the support system
            #             message_type="dashboard_reply",
            #             session_phone=to_phone,  # User's phone number
            #             extracted_order=None,
            #             product_type=None,
            #             backend_url=None,
            #             backend_status=None,
            #             processing_time_ms=None,
            #             metadata={
            #                 "source": "dashboard",
            #                 "agent_reply": True,
            #                 "trace_id": trace_id,
            #                 "gupshup_response": result,
            #                 "actual_sender": "support_agent",
            #                 "actual_recipient": to_phone
            #             }
            #         )
            #     )
            #     
            #     logger.info(f"📋 [Send Reply] BigQuery logging scheduled: thread_id={trace_id}, from={GUPSHUP_SOURCE}, to={to_phone}")
            #     
            # except Exception as bq_error:
            #     logger.warning(f"⚠️ [Send Reply] BigQuery logging failed (non-critical): {str(bq_error)}")
            #     # Don't fail the request if BigQuery logging fails
            #     pass
            
            return JSONResponse({"success": True, "message": "Message sent successfully", "trace_id": trace_id})
        else:
            logger.error(f"❌ [Send Reply] send_message returned None for {to_phone}")
            return JSONResponse({"success": False, "error": "Failed to send message - check logs for details"})
                
    except Exception as e:
        logger.exception(f"💥 [Send Reply] Exception: {str(e)}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

@router.get("/dashboard/api/bq/phone")
async def bq_by_phone(request: Request, phone: str, limit: int = 100):
    logger.info(f"🔍 [BQ Phone] Request: phone={phone}, limit={limit}")
    if not is_authed(request):
        logger.warning(f"❌ [BQ Phone] Unauthorized request for phone={phone}")
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    
    try:
        logger.info(f"📞 [BQ Phone] Fetching conversations for phone: {phone}")
        fetch_conversations_by_phone, _, _ = _get_bigquery_fetchers()
        rows = await fetch_conversations_by_phone(phone, limit)
        logger.info(f"✅ [BQ Phone] Found {len(rows)} rows for phone: {phone}")
        if len(rows) > 0:
            logger.debug(f"📋 [BQ Phone] Sample row: {rows[0] if rows else 'None'}")
        return {"items": rows}
    except Exception as e:
        logger.exception(f"💥 [BQ Phone] Error fetching data for phone={phone}: {str(e)}")
        return JSONResponse({"error": f"Database error: {str(e)}"}, status_code=500)

@router.get("/dashboard/api/bq/thread")
async def bq_by_thread(request: Request, thread_id: str, limit: int = 200):
    logger.info(f"🔍 [BQ Thread] Request: thread_id={thread_id}, limit={limit}")
    if not is_authed(request):
        logger.warning(f"❌ [BQ Thread] Unauthorized request for thread_id={thread_id}")
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    
    try:
        logger.info(f"🧵 [BQ Thread] Fetching conversations for thread: {thread_id}")
        _, fetch_conversations_by_thread, _ = _get_bigquery_fetchers()
        rows = await fetch_conversations_by_thread(thread_id, limit)
        logger.info(f"✅ [BQ Thread] Found {len(rows)} rows for thread: {thread_id}")
        return {"items": rows}
    except Exception as e:
        logger.exception(f"💥 [BQ Thread] Error fetching data for thread_id={thread_id}: {str(e)}")
        return JSONResponse({"error": f"Database error: {str(e)}"}, status_code=500)

@router.get("/dashboard/api/bq/recent")
async def bq_recent(request: Request, limit: int = 100):
    logger.info(f"🔍 [BQ Recent] Request: limit={limit}")
    if not is_authed(request):
        logger.warning(f"❌ [BQ Recent] Unauthorized request")
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    
    try:
        logger.info(f"📅 [BQ Recent] Fetching recent conversations, limit: {limit}")
        _, _, fetch_recent_rows = _get_bigquery_fetchers()
        rows = await fetch_recent_rows(limit)
        logger.info(f"✅ [BQ Recent] Found {len(rows)} recent rows")
        if len(rows) > 0:
            logger.debug(f"📋 [BQ Recent] Sample row: {rows[0] if rows else 'None'}")
        return {"items": rows}
    except Exception as e:
        logger.exception(f"💥 [BQ Recent] Error fetching recent data: {str(e)}")
        return JSONResponse({"error": f"Database error: {str(e)}"}, status_code=500)

@router.get("/dashboard/api/bq/escalations")
async def bq_escalations(request: Request, limit: int = 200):
    logger.info(f"🔍 [BQ Escalations] Request: limit={limit}")
    if not is_authed(request):
        logger.warning(f"❌ [BQ Escalations] Unauthorized request")
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    
    try:
        logger.info(f"🚨 [BQ Escalations] Fetching escalations, limit: {limit}")
        _, _, fetch_recent_rows = _get_bigquery_fetchers()
        rows = await fetch_recent_rows(limit)
        logger.info(f"📊 [BQ Escalations] Processing {len(rows)} rows for escalation detection")
        
        items = []
        for r in rows:
            bot = (r.get("bot_reply") or "").lower()
            meta = r.get("metadata") or {}
            if isinstance(meta, str):
                # try parse json
                try:
                    import json as _json
                    meta = _json.loads(meta)
                except Exception:
                    meta = {}
            is_escalation = (
                "transfer you to a team member" in bot or
                "connecting you with a human" in bot or
                (isinstance(meta, dict) and meta.get("escalation") is True)
            )
            if is_escalation:
                items.append(r)
        
        logger.info(f"✅ [BQ Escalations] Found {len(items)} escalation conversations")
        return {"items": items}
    except Exception as e:
        logger.exception(f"💥 [BQ Escalations] Error fetching escalations: {str(e)}")
        return JSONResponse({"error": f"Database error: {str(e)}"}, status_code=500)

@router.get("/dashboard/bq")
def bq_dashboard_page(request: Request) -> Response:
    logger.debug(f"🚪 Access attempt to /dashboard/bq - Cookies: {dict(request.cookies)}")
    if not is_authed(request):
        logger.warning("❌ Unauthorized access - redirecting to login")
        return RedirectResponse(url="/dashboard/login", status_code=303)
    logger.info("✅ Authorized access granted")
    return Response(content=BQ_DASHBOARD_HTML, media_type="text/html")

@router.get("/dashboard/api/get-mode")
async def get_mode(request: Request, phone: str = Query(...), client_id: str = Query(None)):
    """Return current conversation mode for the given phone number."""
    logger.info(f"🔎 [Get Mode] Incoming request for phone={phone}, client_id={client_id}")
    if not is_authed(request):
        logger.warning("❌ [Get Mode] Unauthorized request")
        return JSONResponse({"success": False, "error": "Unauthorized"}, status_code=401)
    try:
        norm_phone = normalize_india_phone(phone)
        if not norm_phone:
            logger.warning("⚠️ [Get Mode] Missing or invalid phone parameter")
            return JSONResponse({"success": False, "error": "Missing or invalid 'phone'"}, status_code=400)
        state = await bot_user_agent_mode.aget_conversation_state(norm_phone, client_id=client_id)
        mode = state.get("mode") if isinstance(state, dict) else None
        last_activity = state.get("last_activity") if isinstance(state, dict) else None
        if not mode:
            try:
                await bot_user_agent_mode.aset_conversation_mode(norm_phone, "bot", client_id=client_id)
                state = await bot_user_agent_mode.aget_conversation_state(norm_phone, client_id=client_id)
                mode = state.get("mode") if isinstance(state, dict) else "bot"
                last_activity = state.get("last_activity") if isinstance(state, dict) else None
                logger.info(f"🔄 [Get Mode] Initialized missing mode to 'bot' for {norm_phone}")
            except Exception:
                mode = "bot"
                last_activity = None
        logger.info(f"✅ [Get Mode] phone={norm_phone}, mode={mode}")
        return JSONResponse({
            "success": True,
            "phone": norm_phone,
            "mode": mode
        })
    except Exception as e:
        logger.exception(f"💥 [Get Mode] Exception: {str(e)}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
