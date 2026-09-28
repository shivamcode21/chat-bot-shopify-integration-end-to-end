// content.js — Client-configured Chat Widget (WebSocket-based)
// Mirrors the web chat widget: same /ws/chat/{clientName}/{sessionId} backend,
// same message format, same product card UX, mandatory phone collection.

(function() {
  'use strict';

  if (window.__clientChatPluginLoaded) return;
  window.__clientChatPluginLoaded = true;

  let settings = {
    backendUrl: 'http://localhost:8000',
    clientName: '',
    allowedDomain: '',
    enableWidget: true
  };

  let ws = null;
  let sessionId = null;
  let userPhone = null;
  let chatHistory = [];
  let pageContext = null;
  let isShopifyStore = false;
  let reconnectAttempts = 0;
  const MAX_RECONNECT = 5;
  const RECONNECT_BASE_MS = 2000;
  let currentStreamEl = null;
  let currentStreamText = '';
  let pingInterval = null;

  const PHONE_STORAGE_KEY = 'ecomm_plugin_user_phone';
  const SESSION_STORAGE_KEY = 'ecomm_plugin_session_id';

  function normalizeDomain(domainInput) {
    if (!domainInput) return '';
    let d = domainInput.toLowerCase().trim();
    d = d.replace(/^https?:\/\//, '');
    d = d.replace(/^www\./, '');
    d = d.split('/')[0];
    d = d.split('?')[0];
    return d;
  }

  function isCurrentSiteAllowed() {
    const allowed = normalizeDomain(settings.allowedDomain);
    // Backward compatibility for older installs where allowedDomain was not saved yet.
    // Once configured, strict domain lock is enforced.
    if (!allowed) return true;
    const current = window.location.hostname.toLowerCase().replace(/^www\./, '');
    return current === allowed || current.endsWith('.' + allowed);
  }

  function removeWidgetFromPage() {
    disconnectWs();
    const widget = document.getElementById('product-chat-demo-widget');
    if (widget) widget.remove();
  }

  // Load settings
  chrome.runtime.sendMessage({ type: 'GET_SETTINGS' }, (response) => {
    if (response) {
      settings = response;
      if (settings.enableWidget && settings.clientName && isCurrentSiteAllowed()) {
        initWidget();
      }
    }
  });

  chrome.runtime.onMessage.addListener((request) => {
    if (request.type === 'SETTINGS_UPDATED') {
      const oldClient = settings.clientName;
      settings = request.settings;

      if (!(settings.enableWidget && settings.clientName && isCurrentSiteAllowed())) {
        removeWidgetFromPage();
        return;
      }

      if (settings.enableWidget && settings.clientName && isCurrentSiteAllowed()) {
        if (!document.getElementById('product-chat-demo-widget')) {
          initWidget();
        } else if (oldClient !== settings.clientName) {
          disconnectWs();
          connectWs();
        }
      }
    }
  });

  // ===================== SESSION & PHONE =====================

  function getSessionId() {
    let sid = localStorage.getItem(SESSION_STORAGE_KEY);
    if (!sid) {
      sid = 'ext_' + crypto.randomUUID();
      localStorage.setItem(SESSION_STORAGE_KEY, sid);
    }
    return sid;
  }

  function getSavedPhone() {
    return localStorage.getItem(PHONE_STORAGE_KEY) || null;
  }

  function savePhone(phone) {
    localStorage.setItem(PHONE_STORAGE_KEY, phone);
    userPhone = phone;
  }

  // ===================== WEBSOCKET =====================

  function buildWsUrl() {
    const base = settings.backendUrl.replace(/\/$/, '');
    const protocol = base.startsWith('https') ? 'wss' : 'ws';
    const host = base.replace(/^https?:\/\//, '');
    return `${protocol}://${host}/ws/chat/${encodeURIComponent(settings.clientName)}/${sessionId}`;
  }

  function connectWs() {
    if (!settings.clientName) return;
    sessionId = getSessionId();
    const url = buildWsUrl();
    console.log('[ClientPlugin] Connecting:', url);

    try {
      ws = new WebSocket(url);
    } catch (e) {
      console.error('[ClientPlugin] WebSocket creation failed:', e);
      return;
    }

    ws.onopen = () => {
      console.log('[ClientPlugin] WebSocket connected');
      reconnectAttempts = 0;
      updateConnectionStatus(true);
      startPing();

      // Send phone_update if we have one (same as web chat widget)
      if (userPhone) {
        ws.send(JSON.stringify({ type: 'phone_update', phone: userPhone }));
      }
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        handleWsMessage(data);
      } catch (e) {
        console.warn('[ClientPlugin] Bad WS message:', e);
      }
    };

    ws.onclose = (event) => {
      console.log('[ClientPlugin] WebSocket closed:', event.code, event.reason);
      stopPing();
      updateConnectionStatus(false);
      if (event.code !== 1000 && reconnectAttempts < MAX_RECONNECT) {
        const delay = RECONNECT_BASE_MS * Math.pow(2, reconnectAttempts);
        reconnectAttempts++;
        console.log(`[ClientPlugin] Reconnecting in ${delay}ms (attempt ${reconnectAttempts})`);
        setTimeout(connectWs, delay);
      }
    };

    ws.onerror = (err) => {
      console.error('[ClientPlugin] WebSocket error:', err);
    };
  }

  function disconnectWs() {
    stopPing();
    if (ws) {
      ws.onclose = null;
      ws.close();
      ws = null;
    }
  }

  function startPing() {
    stopPing();
    pingInterval = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'ping', timestamp: new Date().toISOString() }));
      }
    }, 30000);
  }

  function stopPing() {
    if (pingInterval) { clearInterval(pingInterval); pingInterval = null; }
  }

  function updateConnectionStatus(connected) {
    const statusText = document.querySelector('#ws-status-text');
    const statusDot = document.querySelector('#product-chat-demo-widget .status-dot');
    if (statusText) statusText.textContent = connected ? 'Online' : 'Connecting...';
    if (statusDot) statusDot.style.background = connected ? '#2ecc71' : '#f39c12';
  }

  function sendWsMessage(text) {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      addMessage('bot', 'Connection lost. Reconnecting...');
      connectWs();
      return;
    }

    const payload = {
      type: 'message',
      message: text,
      phone: userPhone || null,
      streaming: true,
      timestamp: new Date().toISOString(),
      pageContext: pageContext ? {
        url: pageContext.url,
        pageType: pageContext.product?.name ? 'product' : (isShopifyStore ? 'home' : 'other'),
        productHandle: pageContext.shopifyData?.productHandle || null,
        productTitle: pageContext.product?.name || null,
        productPrice: pageContext.product?.price || null,
        hostname: pageContext.domain || null,
      } : undefined
    };

    ws.send(JSON.stringify(payload));
  }

  // ===================== WS MESSAGE HANDLER =====================
  // Matches the exact event types sent by websocket_chat.py

  function handleWsMessage(data) {
    const messagesContainer = document.querySelector('#chat-messages');
    if (!messagesContainer) return;

    switch (data.type) {
      case 'typing':
        if (data.is_typing !== false && !currentStreamEl) {
          removeAllTypingIndicators();
          addTypingIndicator();
        } else if (data.is_typing === false) {
          removeAllTypingIndicators();
        }
        break;

      case 'stream':
        // Streaming token — backend sends { type: 'stream', token: '...' }
        removeAllTypingIndicators();
        if (!currentStreamEl) {
          currentStreamEl = document.createElement('div');
          currentStreamEl.className = 'message bot';
          messagesContainer.appendChild(currentStreamEl);
          currentStreamText = '';
        }
        currentStreamText += data.token || '';
        currentStreamEl.innerHTML = formatBotHtml(currentStreamText);
        messagesContainer.scrollTop = messagesContainer.scrollHeight;
        break;

      case 'stream_end':
        // Streaming complete
        removeAllTypingIndicators();
        if (currentStreamText) {
          chatHistory.push({ role: 'assistant', content: currentStreamText });
          // Check for product URLs in the streamed response
          const urls = extractProductUrlsFromText(currentStreamText);
          if (urls.length > 0) showProductImageCards(urls);
        }
        currentStreamEl = null;
        currentStreamText = '';
        break;

      case 'end':
        // End of graph execution — backend sends { type: 'end', full_response: '...' }
        removeAllTypingIndicators();
        if (data.full_response && !currentStreamEl) {
          // Non-streamed or final full response
          addMessage('bot', data.full_response);
        } else if (currentStreamEl && currentStreamText) {
          chatHistory.push({ role: 'assistant', content: currentStreamText });
        }
        currentStreamEl = null;
        currentStreamText = '';
        break;

      case 'message':
        // Non-streaming response — backend sends { type: 'message', message: '...' }
        removeAllTypingIndicators();
        if (data.message) {
          addMessage('bot', data.message);
        }
        // Inline products with the message event
        if (data.products && data.products.length > 0) {
          addProductCards(data.products);
        }
        currentStreamEl = null;
        currentStreamText = '';
        break;

      case 'products':
        // Async product carousel — backend sends { type: 'products', products: [...] }
        if (data.products && data.products.length > 0) {
          addProductCards(data.products);
        }
        break;

      case 'queued':
        break;

      case 'system':
        console.log('[ClientPlugin] System:', data.message);
        break;

      case 'pong':
        break;

      case 'error':
        removeAllTypingIndicators();
        addMessage('bot', data.message || 'Something went wrong');
        currentStreamEl = null;
        currentStreamText = '';
        break;

      default:
        console.log('[ClientPlugin] Unknown WS event:', data.type, data);
    }
  }

  // ===================== SHOPIFY DETECTION =====================

  function detectShopifyStore() {
    const indicators = [
      () => window.Shopify !== undefined,
      () => document.querySelector('link[href*="cdn.shopify.com"]') !== null,
      () => document.querySelector('script[src*="cdn.shopify.com"]') !== null,
      () => document.querySelector('meta[name="shopify-checkout-api-token"]') !== null,
      () => /\.myshopify\.com/.test(window.location.hostname)
    ];
    return indicators.some(check => { try { return check(); } catch { return false; } });
  }

  // ===================== PAGE SCRAPER =====================

  function scrapePageContext() {
    isShopifyStore = detectShopifyStore();
    const context = {
      url: window.location.href,
      domain: window.location.hostname,
      title: document.title,
      timestamp: new Date().toISOString(),
      isShopify: isShopifyStore
    };
    const metaDesc = document.querySelector('meta[name="description"]');
    if (metaDesc) context.metaDescription = metaDesc.content;
    context.product = extractProductInfo();
    context.openGraph = extractOpenGraphData();
    if (isShopifyStore) context.shopifyData = extractShopifyData();
    return context;
  }

  function extractShopifyData() {
    const data = {};
    const urlMatch = window.location.pathname.match(/\/products\/([^\/\?]+)/);
    if (urlMatch) data.productHandle = urlMatch[1];
    const collMatch = window.location.pathname.match(/\/collections\/([^\/\?]+)/);
    if (collMatch) data.collectionHandle = collMatch[1];
    try {
      if (window.ShopifyAnalytics && window.ShopifyAnalytics.meta && window.ShopifyAnalytics.meta.product) {
        data.analyticsProduct = window.ShopifyAnalytics.meta.product;
      }
    } catch {}
    return Object.keys(data).length > 0 ? data : null;
  }

  function extractProductInfo() {
    const product = {};
    const nameSelectors = [
      'h1.product-title', 'h1.product_title', 'h1.product-name',
      '.product-title h1', '.product_title', '.product-name',
      'h1[itemprop="name"]', '.pdp-title', '.product-info h1',
      '.product-single__title', '.product__title', 'h1'
    ];
    const priceSelectors = [
      '.price', '.product-price', '.pdp-price', '[itemprop="price"]',
      '.price-box', '.offer-price', '.sale-price', '.current-price',
      '.product__price', '.price-item--sale', '.price-item--regular'
    ];
    for (const sel of nameSelectors) {
      const el = document.querySelector(sel);
      if (el && el.textContent.trim()) {
        let name = el.textContent.trim().replace(/\s+/g, ' ');
        const half = Math.floor(name.length / 2);
        if (name.substring(0, half).trim() === name.substring(half).trim()) name = name.substring(0, half).trim();
        product.name = name;
        break;
      }
    }
    for (const sel of priceSelectors) {
      const el = document.querySelector(sel);
      if (el && el.textContent.trim()) {
        product.price = el.textContent.trim().replace(/\s+/g, ' ');
        break;
      }
    }
    return Object.keys(product).length > 0 ? product : null;
  }

  function extractOpenGraphData() {
    const og = {};
    document.querySelectorAll('meta[property^="og:"]').forEach(meta => {
      og[meta.getAttribute('property').replace('og:', '')] = meta.content;
    });
    return Object.keys(og).length > 0 ? og : null;
  }

  // ===================== ADD TO CART (Shopify) =====================

  function addToCartShopify(handle, variantId) {
    if (variantId) {
      doShopifyAddToCart(variantId);
      return;
    }
    // Fetch variant from Shopify product JSON
    const storeUrl = window.location.origin;
    fetch(`${storeUrl}/products/${handle}.json`)
      .then(r => r.json())
      .then(data => {
        const variant = data.product && data.product.variants && data.product.variants[0];
        if (variant) {
          doShopifyAddToCart(variant.id);
        } else {
          alert('Could not find product variant. Please add from the product page.');
        }
      })
      .catch(() => {
        window.open(`${storeUrl}/products/${handle}`, '_blank');
      });
  }

  function doShopifyAddToCart(variantId) {
    fetch('/cart/add.js', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items: [{ id: variantId, quantity: 1 }] })
    })
    .then(r => {
      if (r.ok) {
        addMessage('bot', 'Added to cart! You can proceed to checkout.');
      } else {
        r.json().then(d => {
          addMessage('bot', 'Could not add to cart: ' + (d.description || d.message || 'Unknown error'));
        }).catch(() => {
          addMessage('bot', 'Could not add to cart. Please try from the product page.');
        });
      }
    })
    .catch(() => {
      addMessage('bot', 'Could not add to cart. Please try from the product page.');
    });
  }

  // ===================== CHAT WIDGET UI =====================

  function initWidget() {
    userPhone = getSavedPhone();

    const widget = document.createElement('div');
    widget.id = 'product-chat-demo-widget';
    widget.innerHTML = `
      <button class="chat-toggle-btn" aria-label="Open chat">
        <span class="toggle-pill">Chat with us</span>
        <span class="toggle-avatar">
          <span class="toggle-avatar-fallback" style="display:flex">💬</span>
        </span>
        <span class="toggle-dot"></span>
      </button>

      <div class="chat-window">
        <div class="chat-header">
          <div class="chat-header-info">
            <div class="chat-header-avatar">💬</div>
            <div class="chat-header-text">
              <h3>Shopping Assistant</h3>
              <p><span class="status-dot"></span><span id="ws-status-text">Connecting...</span></p>
            </div>
          </div>
          <div class="chat-header-actions">
            <button class="theme-toggle-btn" id="chat-theme-btn" aria-label="Toggle theme" title="Toggle light/dark">☀️</button>
            <button class="chat-header-btn chat-close-btn" aria-label="Close chat" title="Close">
              <svg viewBox="0 0 24 24"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>
            </button>
          </div>
        </div>

        <!-- PRE-CHAT PHONE FORM -->
        <div class="prechat-form" id="prechat-form" style="display:none;">
          <div class="prechat-inner">
            <div class="prechat-icon">👋</div>
            <h3 class="prechat-title">Welcome!</h3>
            <p class="prechat-subtitle">Please share your phone number to get started</p>
            <input type="tel" id="phone-input" class="prechat-phone-input" placeholder="Enter 10-digit phone number" maxlength="15" autocomplete="tel">
            <div id="phone-error" class="prechat-error" style="display:none;"></div>
            <button class="prechat-start-btn" id="phone-submit-btn">Start Chat</button>
          </div>
        </div>

        <!-- CHAT INTERFACE (hidden until phone collected) -->
        <div class="chat-body" id="chat-body" style="display:none;">
          <div class="chat-messages" id="chat-messages"></div>

          <div class="chat-input-area">
            <div class="chat-input-container">
              <input type="text" class="chat-input" id="chat-input" placeholder="Ask about products, orders, sizes..." autocomplete="off">
              <button class="chat-send-btn" id="chat-send-btn" aria-label="Send message">
                <svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
              </button>
            </div>
            <div class="quick-actions" id="quick-actions"></div>
          </div>
          <div class="chat-footer">Powered by Bloom AI</div>
        </div>
      </div>
    `;

    document.body.appendChild(widget);

    const toggleBtn = widget.querySelector('.chat-toggle-btn');
    const closeBtn = widget.querySelector('.chat-close-btn');
    const chatWindow = widget.querySelector('.chat-window');
    const prechatForm = widget.querySelector('#prechat-form');
    const chatBody = widget.querySelector('#chat-body');
    const phoneInput = widget.querySelector('#phone-input');
    const phoneSubmitBtn = widget.querySelector('#phone-submit-btn');
    const phoneError = widget.querySelector('#phone-error');
    const chatInput = widget.querySelector('#chat-input');
    const sendBtn = widget.querySelector('#chat-send-btn');
    const quickActions = widget.querySelector('#quick-actions');

    pageContext = scrapePageContext();

    // ---- Phone form logic ----
    function showPhoneError(msg) {
      phoneError.textContent = msg;
      phoneError.style.display = 'block';
    }

    phoneSubmitBtn.addEventListener('click', () => {
      const raw = phoneInput.value.replace(/[\s\-\(\)\+]/g, '');
      if (raw.length < 10 || raw.length > 15 || !/^\d+$/.test(raw)) {
        showPhoneError('Please enter a valid phone number (10-15 digits)');
        return;
      }
      phoneError.style.display = 'none';
      savePhone(raw);
      showChatInterface();
    });

    phoneInput.addEventListener('keypress', (e) => {
      if (e.key === 'Enter') phoneSubmitBtn.click();
    });

    function showChatInterface() {
      prechatForm.style.display = 'none';
      chatBody.style.display = 'flex';
      updateQuickActions();

      connectWs();

      if (chatHistory.length === 0) sendWelcomeMessage();
      chatInput.focus();
    }

    // ---- Toggle chat open ----
    toggleBtn.addEventListener('click', () => {
      chatWindow.classList.add('open');
      toggleBtn.style.display = 'none';

      if (userPhone) {
        // Phone already collected — go straight to chat
        showChatInterface();
      } else {
        // Show phone form first
        prechatForm.style.display = 'flex';
        chatBody.style.display = 'none';
        setTimeout(() => phoneInput.focus(), 200);
      }
    });

    closeBtn.addEventListener('click', () => {
      chatWindow.classList.remove('open');
      toggleBtn.style.display = 'flex';
    });

    // Theme toggle
    const themeBtn = widget.querySelector('#chat-theme-btn');
    let isLightMode = false;
    themeBtn.addEventListener('click', () => {
      isLightMode = !isLightMode;
      widget.classList.toggle('light-mode', isLightMode);
      themeBtn.textContent = isLightMode ? '🌙' : '☀️';
    });

    // Send
    sendBtn.addEventListener('click', handleSend);
    chatInput.addEventListener('keypress', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); }
    });

    // Quick actions
    quickActions.addEventListener('click', (e) => {
      if (e.target.classList.contains('quick-action-btn')) {
        chatInput.value = e.target.dataset.message;
        handleSend();
      }
    });

    function handleSend() {
      const msg = chatInput.value.trim();
      if (!msg) return;
      chatInput.value = '';
      addMessage('user', msg);
      addTypingIndicator();
      sendWsMessage(msg);
    }

    function updateQuickActions() {
      const actions = [];
      if (pageContext && pageContext.product?.name) {
        actions.push({ text: '💰 Price?', message: "What's the price?" });
        actions.push({ text: '📏 Sizes?', message: 'What sizes are available?' });
        actions.push({ text: '🔗 Similar', message: 'Show me similar products' });
        actions.push({ text: '📦 Track order', message: 'Track my order' });
      } else {
        actions.push({ text: '🛍️ Products', message: 'What products do you have?' });
        actions.push({ text: '📦 Track order', message: 'Track my order' });
        actions.push({ text: '🏷️ Offers', message: 'Any discounts or offers?' });
        actions.push({ text: '❓ Help', message: 'How can you help me?' });
      }
      quickActions.innerHTML = actions.map(a =>
        `<button class="quick-action-btn" data-message="${a.message}">${a.text}</button>`
      ).join('');
    }

    function sendWelcomeMessage() {
      const storeName = settings.clientName || pageContext.domain.replace('www.', '').split('.')[0];
      const formattedName = storeName.charAt(0).toUpperCase() + storeName.slice(1);

      const isProductPage = /\/products?\//.test(pageContext.url);
      if (isProductPage && pageContext.product?.name) {
        addMessage('bot', `Hi! Welcome to **${formattedName}**.\nI see you're viewing **${pageContext.product.name}**.\n\nHow can I help?`);
        setTimeout(() => {
          addSuggestionChips([
            { text: '📏 Sizes available?', message: 'What sizes are available?' },
            { text: '🔗 Similar products', message: 'Show me similar products' },
            { text: '💰 Price details', message: "What's the price?" },
            { text: '📦 Track my order', message: 'Track my order' },
          ]);
        }, 400);
      } else {
        addMessage('bot', `Hi! Welcome to **${formattedName}**! I'm your AI shopping assistant. How can I help you today?`);
        setTimeout(() => {
          addSuggestionChips([
            { text: '🛍️ Show me products', message: 'Show me all products' },
            { text: '📦 Track my order', message: 'Track my order' },
            { text: '🏷️ Any current offers?', message: 'Any current offers?' },
          ]);
        }, 400);
      }
    }

    // SPA URL change observer
    let lastUrl = window.location.href;
    new MutationObserver(() => {
      if (window.location.href !== lastUrl) {
        lastUrl = window.location.href;
        pageContext = scrapePageContext();
        updateQuickActions();
      }
    }).observe(document.body, { childList: true, subtree: true });
  }

  // ===================== UI HELPERS =====================

  function formatBotHtml(text) {
    text = linkProductNamesInText(text);
    text = text
      .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*(.*?)\*/g, '<em>$1</em>')
      .replace(/~~(.*?)~~/g, '<s>$1</s>')
      .replace(/`(.*?)`/g, '<code>$1</code>')
      .replace(/\n/g, '<br>');
    text = text.replace(
      /(?<!href=")(https?:\/\/[^\s<"]+)(?![^<]*<\/a>)/g,
      '<a href="$1" target="_blank" rel="noopener">$1</a>'
    );
    text = text.replace(/(<br>){3,}/g, '<br><br>');
    return text;
  }

  function linkProductNamesInText(text) {
    const lines = text.split('\n');
    const output = [];
    for (let i = 0; i < lines.length; i++) {
      const trimmed = lines[i].trim();
      const urlMatch = trimmed.match(/(https?:\/\/[^\s]+\/products?\/[^\s]+)/);
      if (urlMatch) {
        const nonUrl = trimmed.replace(urlMatch[0], '').trim();
        if (nonUrl.length <= 6) {
          const url = urlMatch[1];
          for (let j = output.length - 1; j >= 0; j--) {
            const prev = output[j].trim();
            if (!prev || /^https?:\/\//.test(prev)) continue;
            if (/^(Price|In Stock|Out of Stock|Sizes?|Available|View Product)/i.test(prev)) continue;
            if (/^(\u{1F4B0}|\u{1F4E6}|\u{1F4CF}|\u{1F517}|\u{1F50D}|\u2022|\u{1F4DD}|\u{1F4CD}|[-•])/u.test(prev)) continue;
            const raw = output[j];
            const name = raw.replace(/^\d+\.\s*/, '').replace(/\*\*/g, '').trim();
            if (name.length <= 3) continue;
            if (/\*\*/.test(raw)) {
              output[j] = raw.replace(/\*\*([^*]+)\*\*/, `**[${name}](${url})**`);
            } else {
              const stripped = raw.replace(/^\d+\.\s*/, '');
              output[j] = raw.replace(stripped.trim(), `[${stripped.trim()}](${url})`);
            }
            break;
          }
          continue;
        }
      }
      output.push(lines[i]);
    }
    let result = output.join('\n');
    result = result.replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
    return result;
  }

  function addMessage(type, text) {
    const messagesContainer = document.querySelector('#chat-messages');
    if (!messagesContainer) return;
    const el = document.createElement('div');
    el.className = `message ${type}`;
    const plain = text;

    if (type === 'bot') {
      text = formatBotHtml(text);
    } else {
      text = text.replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\n/g, '<br>');
    }

    el.innerHTML = text;
    messagesContainer.appendChild(el);
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
    chatHistory.push({ role: type === 'user' ? 'user' : 'assistant', content: plain });

    if (type === 'bot') {
      const urls = extractProductUrlsFromText(plain);
      if (urls.length > 0) showProductImageCards(urls);
    }
    return el;
  }

  function addSuggestionChips(suggestions) {
    const messagesContainer = document.querySelector('#chat-messages');
    if (!messagesContainer) return;
    const container = document.createElement('div');
    container.className = 'suggestion-chips';
    suggestions.forEach(s => {
      const chip = document.createElement('button');
      chip.className = 'suggestion-chip';
      chip.textContent = s.text;
      chip.addEventListener('click', () => {
        container.remove();
        const input = document.querySelector('#chat-input');
        if (input) { input.value = s.message; }
        const sendBtn = document.querySelector('#chat-send-btn');
        if (sendBtn) sendBtn.click();
      });
      container.appendChild(chip);
    });
    messagesContainer.appendChild(container);
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
  }

  function addTypingIndicator() {
    const messagesContainer = document.querySelector('#chat-messages');
    if (!messagesContainer) return;
    const el = document.createElement('div');
    el.className = 'message bot typing';
    el.id = 'typing-' + Date.now();
    el.innerHTML = '<span></span><span></span><span></span>';
    messagesContainer.appendChild(el);
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
  }

  function removeAllTypingIndicators() {
    document.querySelectorAll('#product-chat-demo-widget .message.typing').forEach(el => el.remove());
  }

  function extractProductUrlsFromText(text) {
    const pattern = /(https?:\/\/[^\s]+\/products\/([a-z0-9][a-z0-9-]*[a-z0-9])(?:\?[^\s]*)?)/gi;
    const products = [];
    const seen = new Set();
    let match;
    while ((match = pattern.exec(text)) !== null) {
      const url = match[1].split('?')[0];
      const handle = match[2].toLowerCase();
      if (!seen.has(handle)) {
        seen.add(handle);
        products.push({ url, handle, title: handle.replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase()) });
      }
    }
    return products;
  }

  // Product image cards (from URLs in bot text)
  function showProductImageCards(products) {
    const messagesContainer = document.querySelector('#chat-messages');
    if (!messagesContainer) return;
    const container = document.createElement('div');
    container.className = 'product-cards-container';
    products.forEach(product => {
      const card = document.createElement('div');
      card.className = 'product-card';
      card.dataset.productHandle = product.handle;
      card.dataset.productUrl = product.url;
      card.innerHTML = `
        <div class="product-card-image"><div class="no-image">⏳</div></div>
        <div class="product-card-content">
          <div class="product-card-title">${product.title}</div>
          <div class="product-card-price"></div>
          <div class="product-card-actions">
            <a class="product-card-btn view" href="${product.url}" target="_blank" rel="noopener">View</a>
            <button class="product-card-btn add-cart" data-handle="${product.handle}">Add to Cart</button>
          </div>
        </div>
      `;
      // Add to Cart click
      card.querySelector('.add-cart').addEventListener('click', (e) => {
        e.stopPropagation();
        addToCartShopify(product.handle);
      });
      container.appendChild(card);
      fetchProductImage(product.url, card);
    });
    messagesContainer.appendChild(container);
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
    setTimeout(() => { messagesContainer.scrollTop = messagesContainer.scrollHeight; }, 200);
  }

  function fetchProductImage(url, cardEl) {
    chrome.runtime.sendMessage({ type: 'FETCH_OG_IMAGE', url }, (response) => {
      const imgEl = cardEl.querySelector('.product-card-image');
      if (!imgEl) return;
      if (response && response.success && response.data && response.data.image) {
        const { image, price, title } = response.data;
        imgEl.innerHTML = `<img src="${image}" alt="${title || 'Product'}" onerror="this.onerror=null;this.parentElement.innerHTML='<div class=no-image>🛍️</div>'">`;
        if (title) {
          const titleEl = cardEl.querySelector('.product-card-title');
          if (titleEl) titleEl.textContent = title;
        }
        const priceEl = cardEl.querySelector('.product-card-price');
        if (priceEl && price) {
          const cleaned = price.replace(/[^0-9.,]/g, '');
          const num = parseFloat(cleaned.replace(/,/g, ''));
          if (num > 0 && num < 100000) {
            priceEl.innerHTML = `<span class="current-price">${cleaned}</span>`;
          }
        }
      } else {
        const parent = cardEl.parentElement;
        cardEl.remove();
        if (parent && parent.classList.contains('product-cards-container') && parent.children.length === 0) {
          parent.remove();
        }
      }
      const mc = document.querySelector('#chat-messages');
      if (mc) mc.scrollTop = mc.scrollHeight;
    });
  }

  // Product cards from backend /products event (structured data)
  function addProductCards(products) {
    const messagesContainer = document.querySelector('#chat-messages');
    if (!messagesContainer) return;
    const limited = products.slice(0, 10);
    if (limited.length === 0) return;
    const container = document.createElement('div');
    container.className = 'product-cards-container';

    limited.forEach(product => {
      const card = document.createElement('div');
      card.className = 'product-card';
      const imgSrc = product.image || product.image_url || '';
      const title = product.title || 'Product';
      const price = product.price || '';
      const url = product.url || '#';
      const handle = product.handle || (url.match(/\/products\/([^\/\?]+)/) || [])[1] || '';

      let html = '<div class="product-card-image">';
      if (imgSrc) {
        html += `<img src="${imgSrc}" alt="${title}" onerror="this.onerror=null;this.src='';this.parentElement.innerHTML='<div class=no-image>🛍️</div>'">`;
      } else {
        html += '<div class="no-image">🛍️</div>';
      }
      html += '</div>';
      html += '<div class="product-card-content">';
      html += `<div class="product-card-title">${title}</div>`;
      if (price) {
        html += `<div class="product-card-price"><span class="current-price">${price}</span></div>`;
      }
      html += '<div class="product-card-actions">';
      html += `<a class="product-card-btn view" href="${url}" target="_blank" rel="noopener">View</a>`;
      if (handle) {
        html += `<button class="product-card-btn add-cart" data-handle="${handle}">Add to Cart</button>`;
      }
      html += '</div></div>';

      card.innerHTML = html;

      // Add to Cart click
      const cartBtn = card.querySelector('.add-cart');
      if (cartBtn) {
        cartBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          addToCartShopify(handle);
        });
      }

      container.appendChild(card);
    });

    messagesContainer.appendChild(container);
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
  }
})();
