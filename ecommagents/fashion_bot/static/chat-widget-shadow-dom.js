/**
 * Embeddable Chat Widget for Fashion Bot - Shadow DOM Version
 * 
 * This version uses Shadow DOM for complete CSS isolation.
 * Parent page styles CANNOT affect this widget.
 * 
 * ✅ RECOMMENDED for most use cases!
 * 
 * Usage:
 * <script src="https://your-domain.com/static/chat-widget-shadow-dom.js"></script>
 * <script>
 *   FashionBotWidget.init({
 *     clientName: 'Concept Groove',  // Your client name (not UUID)
 *     position: 'bottom-right', // or 'bottom-left'
 *     theme: 'light' // or 'dark'
 *   });
 * </script>
 */

(function() {
    'use strict';

    const FashionBotWidget = {
        config: {
            clientName: null,  // Human-readable name (e.g., 'Concept Groove')
            apiUrl: null,      // Auto-detected if not provided
            position: 'bottom-right',
            theme: 'light'
        },
        
        sessionId: null,
        ws: null,
        isOpen: false,
        isMaximized: false,
        messageQueue: [],
        userPhone: null,
        phoneCollected: false,
        heartbeatInterval: null,
        historyRestored: false,
        shadowRoot: null,  // Shadow DOM root
        streamingMessageDiv: null,  // Current message being streamed
        streamingContent: '',       // Accumulated streaming content

        detectApiUrl: function() {
            try {
                const scripts = document.getElementsByTagName('script');
                for (let script of scripts) {
                    if (script.src && script.src.includes('chat-widget-shadow-dom.js')) {
                        const url = new URL(script.src);
                        const protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
                        const host = url.host;
                        const apiUrl = `${protocol}//${host}/ws/chat`;
                        console.log('FashionBotWidget [Shadow DOM]: Auto-detected API URL:', apiUrl);
                        return apiUrl;
                    }
                }
            } catch (e) {
                console.error('FashionBotWidget [Shadow DOM]: Failed to auto-detect API URL:', e);
            }
            return null;
        },

        init: function(options) {
            this.config = { ...this.config, ...options };
            
            if (!this.config.apiUrl) {
                this.config.apiUrl = this.detectApiUrl();
                if (!this.config.apiUrl) {
                    console.error('FashionBotWidget [Shadow DOM]: Could not auto-detect apiUrl. Please provide it explicitly.');
                    return;
                }
            }
            
            if (!this.config.clientName) {
                console.error('FashionBotWidget [Shadow DOM]: clientName is required');
                return;
            }
            
            console.log('FashionBotWidget [Shadow DOM]: Initialized with config:', {
                clientName: this.config.clientName,
                apiUrl: this.config.apiUrl,
                position: this.config.position,
                theme: this.config.theme
            });

            this.sessionId = this.getSessionId();
            this.createWidgetWithShadowDOM();
            this.bindEvents();
            
            console.log('FashionBotWidget [Shadow DOM]: Ready (Shadow DOM version - CSS isolated)');
        },

        getSessionId: function() {
            let sessionId = localStorage.getItem('fashionbot_session_id');
            if (!sessionId) {
                sessionId = 'web_' + this.generateUUID();
                localStorage.setItem('fashionbot_session_id', sessionId);
            }
            return sessionId;
        },

        saveMessageToStorage: function(text, sender, timestamp) {
            try {
                const storageKey = `fashionbot_messages_${this.sessionId}`;
                let messages = JSON.parse(localStorage.getItem(storageKey) || '[]');
                const sanitizedText = this.sanitizeForStorage(text);
                
                messages.push({
                    text: sanitizedText,
                    sender: sender,
                    timestamp: timestamp || new Date().toISOString()
                });
                
                if (messages.length > 100) {
                    messages = messages.slice(-100);
                }
                
                localStorage.setItem(storageKey, JSON.stringify(messages));
            } catch (e) {
                console.error('Failed to save message to storage:', e);
            }
        },

        sanitizeForStorage: function(text) {
            if (typeof text !== 'string') return '';
            return text
                .replace(/<script[^>]*>.*?<\/script>/gi, '')
                .replace(/<iframe[^>]*>.*?<\/iframe>/gi, '')
                .replace(/javascript:/gi, '')
                .replace(/on\w+\s*=/gi, '');
        },

        loadMessagesFromStorage: function() {
            try {
                const storageKey = `fashionbot_messages_${this.sessionId}`;
                const messages = JSON.parse(localStorage.getItem(storageKey) || '[]');
                const fortyEightHoursAgo = new Date(Date.now() - 48 * 60 * 60 * 1000);
                const recentMessages = messages.filter(msg => new Date(msg.timestamp) > fortyEightHoursAgo);
                
                if (recentMessages.length < messages.length) {
                    localStorage.setItem(storageKey, JSON.stringify(recentMessages));
                }
                
                return recentMessages;
            } catch (e) {
                console.error('Failed to load messages from storage:', e);
                return [];
            }
        },

        restoreChatHistory: function() {
            if (this.historyRestored) return;
            
            const messages = this.loadMessagesFromStorage();
            
            if (messages.length > 0) {
                console.log(`Restoring ${messages.length} messages from history`);
                
                messages.forEach(msg => {
                    this.addMessageToUI(msg.text, msg.sender, false);
                });
                
                const messagesDiv = this.shadowRoot.getElementById('fashionbot-messages');
                const oldestMessage = messages[0];
                const timeAgo = this.getTimeAgo(new Date(oldestMessage.timestamp));
                
                const separator = document.createElement('div');
                separator.style.cssText = 'text-align: center; color: #999; font-size: 11px; margin: 10px 0; padding: 5px 0; border-top: 1px solid #e0e0e0;';
                separator.innerHTML = `— Previous conversation (${timeAgo}) —`;
                messagesDiv.insertBefore(separator, messagesDiv.firstChild);
                
                this.historyRestored = true;
            }
        },

        getTimeAgo: function(date) {
            const seconds = Math.floor((new Date() - date) / 1000);
            if (seconds < 60) return 'just now';
            if (seconds < 3600) return `${Math.floor(seconds / 60)} minutes ago`;
            if (seconds < 86400) return `${Math.floor(seconds / 3600)} hours ago`;
            return `${Math.floor(seconds / 86400)} days ago`;
        },

        generateUUID: function() {
            return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
                const r = Math.random() * 16 | 0;
                const v = c === 'x' ? r : (r & 0x3 | 0x8);
                return v.toString(16);
            });
        },

        getStyles: function() {
            return `
                :host {
                    all: initial;
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
                }

                * {
                    box-sizing: border-box;
                }

                #fashionbot-widget-container {
                    position: fixed;
                    ${this.config.position.includes('right') ? 'right: 20px;' : 'left: 20px;'}
                    bottom: 20px;
                    z-index: 999999;
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
                }

                #fashionbot-chat-button {
                    width: 60px;
                    height: 60px;
                    border-radius: 50%;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    border: none;
                    cursor: pointer;
                    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    transition: transform 0.2s;
                }

                #fashionbot-chat-button:hover {
                    transform: scale(1.05);
                }

                #fashionbot-chat-button svg {
                    width: 30px;
                    height: 30px;
                    fill: white;
                }

                #fashionbot-chat-window {
                    display: none;
                    position: fixed;
                    ${this.config.position.includes('right') ? 'right: 20px;' : 'left: 20px;'}
                    bottom: 90px;
                    width: 380px;
                    height: 600px;
                    max-height: 80vh;
                    background: white;
                    border-radius: 16px;
                    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.12);
                    flex-direction: column;
                    overflow: hidden;
                }

                #fashionbot-chat-window.open {
                    display: flex;
                    animation: slideUp 0.3s ease-out;
                }

                #fashionbot-chat-window.maximized {
                    width: 90vw !important;
                    height: 90vh !important;
                    max-height: 90vh !important;
                    bottom: 50% !important;
                    ${this.config.position.includes('right') ? 'right: 50% !important;' : 'left: 50% !important;'}
                    transform: translate(${this.config.position.includes('right') ? '50%' : '-50%'}, 50%) !important;
                }

                @keyframes slideUp {
                    from { transform: translateY(20px); opacity: 0; }
                    to { transform: translateY(0); opacity: 1; }
                }

                #fashionbot-chat-header {
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    color: white;
                    padding: 20px;
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                }

                #fashionbot-chat-header h3 {
                    margin: 0;
                    font-size: 18px;
                    font-weight: 600;
                    flex: 1;
                }

                #fashionbot-header-buttons {
                    display: flex;
                    gap: 8px;
                }

                #fashionbot-maximize-button,
                #fashionbot-close-button {
                    background: none;
                    border: none;
                    color: white;
                    font-size: 24px;
                    cursor: pointer;
                    padding: 0;
                    width: 30px;
                    height: 30px;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    opacity: 0.9;
                    transition: opacity 0.2s;
                }

                #fashionbot-maximize-button:hover,
                #fashionbot-close-button:hover {
                    opacity: 1;
                }

                #fashionbot-prechat-form {
                    flex: 1;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    background: #f7f8fa;
                }

                #fashionbot-messages {
                    flex: 1;
                    overflow-y: auto;
                    padding: 20px;
                    background: #f7f8fa;
                }

                .fashionbot-message {
                    margin-bottom: 16px;
                    display: flex;
                    animation: fadeIn 0.3s;
                }

                @keyframes fadeIn {
                    from { opacity: 0; transform: translateY(10px); }
                    to { opacity: 1; transform: translateY(0); }
                }

                .fashionbot-message.user {
                    justify-content: flex-end;
                }

                .fashionbot-message-content {
                    max-width: 75%;
                    padding: 12px 16px;
                    border-radius: 18px;
                    word-wrap: break-word;
                    line-height: 1.5;
                }

                .fashionbot-message.bot .fashionbot-message-content {
                    background: linear-gradient(135deg, #fef3e2 0%, #fff0e6 100%);
                    color: #2d3748;
                    border-bottom-left-radius: 4px;
                    border: 1px solid #ffd7a8;
                    box-shadow: 0 2px 4px rgba(255, 152, 0, 0.08);
                }

                .fashionbot-message.user .fashionbot-message-content {
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    color: white;
                    border-bottom-right-radius: 4px;
                }

                .fashionbot-message-content strong { font-weight: 600; color: inherit; }
                .fashionbot-message-content em { font-style: italic; }
                
                .fashionbot-message-content code {
                    background: rgba(255, 152, 0, 0.1);
                    border: 1px solid rgba(255, 152, 0, 0.2);
                    padding: 2px 6px;
                    border-radius: 4px;
                    font-family: 'Monaco', 'Consolas', 'Courier New', monospace;
                    font-size: 0.9em;
                    color: #e65100;
                    font-weight: 500;
                }

                .fashionbot-message.user .fashionbot-message-content code {
                    background: rgba(255, 255, 255, 0.2);
                    border-color: rgba(255, 255, 255, 0.3);
                    color: white;
                }

                .fashionbot-message-content ul {
                    margin: 8px 0;
                    padding-left: 20px;
                    list-style-type: none;
                }

                .fashionbot-message-content li {
                    margin: 4px 0;
                    padding-left: 8px;
                }

                .fashionbot-message-content li::before {
                    content: "•";
                    color: #ff9800;
                    font-weight: bold;
                    display: inline-block;
                    width: 1em;
                    margin-left: -1em;
                }

                .fashionbot-message-content a {
                    color: #667eea !important;
                    text-decoration: underline !important;
                    text-decoration-color: rgba(102, 126, 234, 0.4) !important;
                    font-weight: 500;
                    word-break: break-word;
                }

                .fashionbot-message-content a:hover {
                    color: #764ba2 !important;
                    text-decoration-color: #764ba2 !important;
                }

                .fashionbot-message.user .fashionbot-message-content a {
                    color: white !important;
                    text-decoration-color: rgba(255, 255, 255, 0.6) !important;
                }

                .fashionbot-typing {
                    display: flex;
                    gap: 4px;
                    padding: 8px 12px;
                    background: linear-gradient(135deg, #fef3e2 0%, #fff0e6 100%);
                    border: 1px solid #ffd7a8;
                    border-radius: 18px;
                    width: fit-content;
                }

                .fashionbot-typing span {
                    width: 8px;
                    height: 8px;
                    background: #ff9800;
                    border-radius: 50%;
                    animation: typing 1.4s infinite;
                }

                .fashionbot-typing span:nth-child(2) { animation-delay: 0.2s; }
                .fashionbot-typing span:nth-child(3) { animation-delay: 0.4s; }

                @keyframes typing {
                    0%, 60%, 100% { transform: translateY(0); }
                    30% { transform: translateY(-10px); }
                }

                /* Streaming cursor animation */
                .streaming-cursor {
                    display: inline-block;
                    color: #667eea;
                    animation: cursor-blink 0.7s infinite;
                    margin-left: 2px;
                }

                @keyframes cursor-blink {
                    0%, 50% { opacity: 1; }
                    51%, 100% { opacity: 0; }
                }

                .fashionbot-message.streaming .fashionbot-message-content {
                    min-height: 20px;
                }

                #fashionbot-input-area {
                    padding: 16px;
                    background: white;
                    border-top: 1px solid #e0e0e0;
                }

                #fashionbot-input-form {
                    display: flex;
                    gap: 8px;
                }

                #fashionbot-input {
                    flex: 1;
                    padding: 12px 16px;
                    border: 1px solid #e0e0e0;
                    border-radius: 24px;
                    font-size: 14px;
                    outline: none;
                    font-family: inherit;
                }

                #fashionbot-input:focus {
                    border-color: #667eea;
                }

                #fashionbot-send-button {
                    width: 44px;
                    height: 44px;
                    border-radius: 50%;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    border: none;
                    color: white;
                    cursor: pointer;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                }

                #fashionbot-send-button:hover {
                    opacity: 0.9;
                }

                .prechat-container {
                    padding: 30px;
                    max-width: 300px;
                    width: 100%;
                }

                .prechat-title {
                    margin: 0 0 10px 0;
                    color: #333;
                    font-size: 18px;
                    font-weight: 600;
                }

                .prechat-subtitle {
                    margin: 0 0 20px 0;
                    color: #666;
                    font-size: 14px;
                    line-height: 1.5;
                }

                .prechat-input {
                    width: 100%;
                    padding: 12px;
                    border: 1px solid #ddd;
                    border-radius: 8px;
                    font-size: 14px;
                    margin-bottom: 12px;
                    box-sizing: border-box;
                    font-family: inherit;
                }

                .prechat-btn-primary {
                    width: 100%;
                    padding: 12px;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    color: white;
                    border: none;
                    border-radius: 8px;
                    font-size: 14px;
                    cursor: pointer;
                    font-weight: 600;
                    transition: opacity 0.2s;
                }

                .prechat-btn-primary:hover {
                    opacity: 0.9;
                }

                .prechat-btn-secondary {
                    width: 100%;
                    padding: 12px;
                    background: transparent;
                    color: #667eea;
                    border: none;
                    font-size: 13px;
                    cursor: pointer;
                    margin-top: 8px;
                }

                .prechat-btn-secondary:hover {
                    opacity: 0.7;
                }

                @media (max-width: 480px) {
                    #fashionbot-chat-window {
                        width: calc(100vw - 40px);
                        height: calc(100vh - 100px);
                        max-height: none;
                    }
                }
            `;
        },

        getWidgetHTML: function() {
            return `
                <div id="fashionbot-widget-container">
                    <button id="fashionbot-chat-button" aria-label="Open chat">
                        <svg viewBox="0 0 24 24">
                            <path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z"/>
                        </svg>
                    </button>
                    
                    <div id="fashionbot-chat-window">
                        <div id="fashionbot-chat-header">
                            <h3>💬 Support Chat</h3>
                            <div id="fashionbot-header-buttons">
                                <button id="fashionbot-maximize-button" aria-label="Maximize chat" title="Maximize">
                                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                        <path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/>
                                    </svg>
                                </button>
                                <button id="fashionbot-close-button" aria-label="Close chat" title="Close">×</button>
                            </div>
                        </div>
                        
                        <div id="fashionbot-prechat-form">
                            <div class="prechat-container">
                                <h4 class="prechat-title">Welcome! 👋</h4>
                                <p class="prechat-subtitle">
                                    Please share your phone number for a better experience (optional)
                                </p>
                                <input type="tel" id="fashionbot-phone-input" class="prechat-input" placeholder="Enter your phone number"/>
                                <button id="fashionbot-start-chat" class="prechat-btn-primary">Start Chat</button>
                                <button id="fashionbot-skip-phone" class="prechat-btn-secondary">Skip for now</button>
                            </div>
                        </div>
                        
                        <div id="fashionbot-messages"></div>
                        
                        <div id="fashionbot-input-area">
                            <form id="fashionbot-input-form">
                                <input type="text" id="fashionbot-input" placeholder="Type your message..." autocomplete="off"/>
                                <button type="submit" id="fashionbot-send-button" aria-label="Send message">
                                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor">
                                        <path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z"/>
                                    </svg>
                                </button>
                            </form>
                        </div>
                    </div>
                </div>
            `;
        },

        createWidgetWithShadowDOM: function() {
            // Create host element
            const host = document.createElement('div');
            host.id = 'fashionbot-widget-host';
            document.body.appendChild(host);
            
            // Attach shadow root
            this.shadowRoot = host.attachShadow({ mode: 'open' });
            
            // Inject styles into shadow root
            const style = document.createElement('style');
            style.textContent = this.getStyles();
            this.shadowRoot.appendChild(style);
            
            // Create widget HTML inside shadow root
            const wrapper = document.createElement('div');
            wrapper.innerHTML = this.getWidgetHTML();
            this.shadowRoot.appendChild(wrapper.firstElementChild);
        },

        bindEvents: function() {
            this.shadowRoot.getElementById('fashionbot-chat-button').addEventListener('click', () => this.toggleChat());
            this.shadowRoot.getElementById('fashionbot-close-button').addEventListener('click', () => this.closeChat());
            this.shadowRoot.getElementById('fashionbot-maximize-button').addEventListener('click', () => this.toggleMaximize());
            this.shadowRoot.getElementById('fashionbot-input-form').addEventListener('submit', (e) => {
                e.preventDefault();
                this.sendMessage();
            });
            this.shadowRoot.getElementById('fashionbot-start-chat').addEventListener('click', () => this.startChat());
            this.shadowRoot.getElementById('fashionbot-skip-phone').addEventListener('click', () => this.skipPhone());
        },

        toggleChat: function() {
            const chatWindow = this.shadowRoot.getElementById('fashionbot-chat-window');
            const preChatForm = this.shadowRoot.getElementById('fashionbot-prechat-form');
            const messagesDiv = this.shadowRoot.getElementById('fashionbot-messages');
            const inputArea = this.shadowRoot.getElementById('fashionbot-input-area');
            
            this.isOpen = !this.isOpen;
            
            if (this.isOpen) {
                chatWindow.classList.add('open');
                
                const phoneStored = localStorage.getItem('fashionbot_user_phone');
                if (phoneStored) {
                    this.userPhone = phoneStored;
                    this.phoneCollected = true;
                    preChatForm.style.display = 'none';
                    messagesDiv.style.display = 'block';
                    inputArea.style.display = 'block';
                    this.restoreChatHistory();
                    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
                        this.connect();
                    }
                    this.shadowRoot.getElementById('fashionbot-input').focus();
                } else if (!this.phoneCollected) {
                    preChatForm.style.display = 'flex';
                    messagesDiv.style.display = 'none';
                    inputArea.style.display = 'none';
                    setTimeout(() => this.shadowRoot.getElementById('fashionbot-phone-input').focus(), 100);
                } else {
                    preChatForm.style.display = 'none';
                    messagesDiv.style.display = 'block';
                    inputArea.style.display = 'block';
                    this.restoreChatHistory();
                    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
                        this.connect();
                    }
                    this.shadowRoot.getElementById('fashionbot-input').focus();
                }
            } else {
                chatWindow.classList.remove('open');
            }
        },

        startChat: function() {
            const phone = this.shadowRoot.getElementById('fashionbot-phone-input').value.trim();
            
            if (phone) {
                if (!/^[0-9]{10,15}$/.test(phone)) {
                    alert('Please enter a valid phone number (10-15 digits)');
                    return;
                }
                this.userPhone = phone;
                this.phoneCollected = true;
                localStorage.setItem('fashionbot_user_phone', phone);
            }
            
            this.showChatInterface();
        },

        skipPhone: function() {
            this.phoneCollected = true;
            this.userPhone = null;
            this.showChatInterface();
        },

        showChatInterface: function() {
            this.shadowRoot.getElementById('fashionbot-prechat-form').style.display = 'none';
            this.shadowRoot.getElementById('fashionbot-messages').style.display = 'block';
            this.shadowRoot.getElementById('fashionbot-input-area').style.display = 'block';
            
            this.restoreChatHistory();
            
            if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
                this.connect();
            }
            
            if (this.userPhone) {
                const sendPhone = () => {
                    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                        this.ws.send(JSON.stringify({ type: 'phone_update', phone: this.userPhone }));
                    } else {
                        setTimeout(sendPhone, 100);
                    }
                };
                sendPhone();
            }
            
            setTimeout(() => this.shadowRoot.getElementById('fashionbot-input').focus(), 100);
        },

        closeChat: function() {
            this.isOpen = false;
            this.shadowRoot.getElementById('fashionbot-chat-window').classList.remove('open');
            if (this.isMaximized) this.toggleMaximize();
            this.stopHeartbeat();
        },

        toggleMaximize: function() {
            this.isMaximized = !this.isMaximized;
            const chatWindow = this.shadowRoot.getElementById('fashionbot-chat-window');
            const btn = this.shadowRoot.getElementById('fashionbot-maximize-button');
            
            if (this.isMaximized) {
                chatWindow.classList.add('maximized');
                btn.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 3v3a2 2 0 0 1-2 2H3m18 0h-3a2 2 0 0 1-2-2V3m0 18v-3a2 2 0 0 1 2-2h3M3 16h3a2 2 0 0 1 2 2v3"/></svg>';
            } else {
                chatWindow.classList.remove('maximized');
                btn.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/></svg>';
            }
        },

        startHeartbeat: function() {
            this.stopHeartbeat();
            this.heartbeatInterval = setInterval(() => {
                if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                    this.ws.send(JSON.stringify({ type: 'ping', timestamp: new Date().toISOString() }));
                }
            }, 5 * 60 * 1000);
        },

        stopHeartbeat: function() {
            if (this.heartbeatInterval) {
                clearInterval(this.heartbeatInterval);
                this.heartbeatInterval = null;
            }
        },

        connect: function() {
            const wsUrl = `${this.config.apiUrl}/${this.config.clientName}/${this.sessionId}`;
            console.log('Connecting to:', wsUrl);
            
            this.ws = new WebSocket(wsUrl);
            
            this.ws.onopen = () => {
                console.log('WebSocket connected');
                this.processMessageQueue();
                this.startHeartbeat();
            };
            
            this.ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                this.handleMessage(data);
            };
            
            this.ws.onerror = (error) => {
                console.error('WebSocket error:', error);
                this.addMessage('Connection error. Please try again.', 'bot');
            };
            
            this.ws.onclose = () => {
                console.log('WebSocket closed');
                this.stopHeartbeat();
            };
        },

        handleMessage: function(data) {
            if (data.type === 'message') {
                this.addMessage(data.message, 'bot');
            } else if (data.type === 'stream') {
                this.handleStreamToken(data.token);
            } else if (data.type === 'stream_end') {
                this.handleStreamEnd(data);
            } else if (data.type === 'typing') {
                this.showTyping(data.is_typing);
            } else if (data.type === 'error') {
                this.addMessage(data.message, 'bot');
            }
        },

        handleStreamToken: function(token) {
            const messagesDiv = this.shadowRoot.getElementById('fashionbot-messages');
            
            // Hide typing indicator when streaming starts
            const typingIndicator = messagesDiv.querySelector('.fashionbot-typing');
            if (typingIndicator) {
                typingIndicator.parentElement.remove();
            }
            
            // Create streaming message div if not exists
            if (!this.streamingMessageDiv) {
                this.streamingMessageDiv = document.createElement('div');
                this.streamingMessageDiv.className = 'fashionbot-message bot streaming';
                
                const contentDiv = document.createElement('div');
                contentDiv.className = 'fashionbot-message-content';
                contentDiv.innerHTML = '<span class="streaming-cursor">▋</span>';
                
                this.streamingMessageDiv.appendChild(contentDiv);
                messagesDiv.appendChild(this.streamingMessageDiv);
                this.streamingContent = '';
            }
            
            // Accumulate content
            this.streamingContent = (this.streamingContent || '') + token;
            
            // Update the message content with formatted text
            const contentDiv = this.streamingMessageDiv.querySelector('.fashionbot-message-content');
            contentDiv.innerHTML = this.formatText(this.streamingContent) + '<span class="streaming-cursor">▋</span>';
            
            // Auto-scroll to bottom
            messagesDiv.scrollTop = messagesDiv.scrollHeight;
        },

        handleStreamEnd: function(data) {
            if (this.streamingMessageDiv) {
                var authoritative = (data.full_response && typeof data.full_response === 'string')
                    ? data.full_response : this.streamingContent;
                // Finalize message with authoritative text
                const contentDiv = this.streamingMessageDiv.querySelector('.fashionbot-message-content');
                contentDiv.innerHTML = this.formatText(authoritative);
                
                // Remove streaming class
                this.streamingMessageDiv.classList.remove('streaming');
                
                // Reset streaming state
                this.streamingMessageDiv = null;
                this.streamingContent = '';
            }
        },

        sendMessage: function() {
            const input = this.shadowRoot.getElementById('fashionbot-input');
            const message = input.value.trim();
            
            if (!message) return;
            
            this.addMessage(message, 'user');
            input.value = '';
            this.showTyping(true);
            
            const payload = {
                type: 'message',
                message: message,
                phone: this.userPhone,
                streaming: true,  // Enable streaming responses
                timestamp: new Date().toISOString()
            };
            
            if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                this.ws.send(JSON.stringify(payload));
            } else {
                this.messageQueue.push(payload);
                this.connect();
            }
        },

        processMessageQueue: function() {
            while (this.messageQueue.length > 0) {
                this.ws.send(JSON.stringify(this.messageQueue.shift()));
            }
        },

        formatText: function(text) {
            const escapeHtml = (unsafe) => unsafe
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/"/g, "&quot;")
                .replace(/'/g, "&#039;");
            
            let formatted = escapeHtml(text);
            formatted = formatted.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
            formatted = formatted.replace(/__(.+?)__/g, '<strong>$1</strong>');
            formatted = formatted.replace(/\*(.+?)\*/g, '<em>$1</em>');
            formatted = formatted.replace(/_(.+?)_/g, '<em>$1</em>');
            formatted = formatted.replace(/`(.+?)`/g, '<code>$1</code>');
            formatted = formatted.replace(/\n/g, '<br>');
            formatted = formatted.replace(/^[-*]\s+(.+?)(<br>|$)/gm, '<li>$1</li>');
            formatted = formatted.replace(/(<li>.*?<\/li>)+/gs, '<ul>$&</ul>');
            formatted = formatted.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
            formatted = formatted.replace(/(?<!href="|">)(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>');
            
            return formatted;
        },

        addMessage: function(text, sender, saveToStorage = true) {
            this.addMessageToUI(text, sender, saveToStorage);
        },

        addMessageToUI: function(text, sender, saveToStorage = true) {
            const messagesDiv = this.shadowRoot.getElementById('fashionbot-messages');
            const messageDiv = document.createElement('div');
            messageDiv.className = `fashionbot-message ${sender}`;
            
            const contentDiv = document.createElement('div');
            contentDiv.className = 'fashionbot-message-content';
            contentDiv.innerHTML = this.formatText(text);
            
            messageDiv.appendChild(contentDiv);
            messagesDiv.appendChild(messageDiv);
            
            if (sender === 'bot') {
                const typingIndicator = messagesDiv.querySelector('.fashionbot-typing');
                if (typingIndicator) typingIndicator.parentElement.remove();
            }
            
            if (saveToStorage) {
                this.saveMessageToStorage(text, sender);
            }
            
            messagesDiv.scrollTop = messagesDiv.scrollHeight;
        },

        showTyping: function(show) {
            const messagesDiv = this.shadowRoot.getElementById('fashionbot-messages');
            let typingIndicator = messagesDiv.querySelector('.fashionbot-typing');
            
            if (show && !typingIndicator) {
                const messageDiv = document.createElement('div');
                messageDiv.className = 'fashionbot-message bot';
                typingIndicator = document.createElement('div');
                typingIndicator.className = 'fashionbot-typing';
                typingIndicator.innerHTML = '<span></span><span></span><span></span>';
                messageDiv.appendChild(typingIndicator);
                messagesDiv.appendChild(messageDiv);
                messagesDiv.scrollTop = messagesDiv.scrollHeight;
            } else if (!show && typingIndicator) {
                typingIndicator.parentElement.remove();
            }
        }
    };

    window.FashionBotWidget = FashionBotWidget;
})();

