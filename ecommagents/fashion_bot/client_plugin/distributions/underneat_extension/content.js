// content.js — Client-configured Chat Widget (WebSocket-based)
// Mirrors the web chat widget: same /ws/chat/{clientName}/{sessionId} backend,
// same message format, same product card UX, mandatory phone collection.

(function() {
  'use strict';

  if (window.__clientChatPluginLoaded) return;
  window.__clientChatPluginLoaded = true;

  let settings = {
    backendUrl: 'https://ecommagents.onrender.com',
    clientName: 'Underneat',
    allowedDomain: 'underneat.in',
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
  let launcherMessagesCache = [];
  let phoneCollectionBlocked = false;
  let typingLabelTimeouts = [];

  const PHONE_STORAGE_KEY = 'ecomm_plugin_user_phone';
  const SESSION_STORAGE_KEY = 'ecomm_plugin_session_id';
  const LAUNCHER_HINTS_FALLBACK = [
    'high waist tummy tuckers',
    'shaping shorts',
    'slimming body shaper',
    'shapewear for post pregnancy',
    'padded bras'
  ];
  const BRAND_ICON_URL = chrome.runtime.getURL('icons/icon128.png');

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

  function selectMessagesFromHintsData(data, pageCtx, cfg) {
    if (!data || typeof data !== 'object') return LAUNCHER_HINTS_FALLBACK.slice();
    try {
      const clientName = (cfg.clientName || '').trim();
      if (clientName && data.byClientName && Array.isArray(data.byClientName[clientName]) && data.byClientName[clientName].length) {
        return data.byClientName[clientName];
      }
      const host = (pageCtx && pageCtx.domain) ? String(pageCtx.domain).toLowerCase() : '';
      if (host && data.byDomainSubstring && typeof data.byDomainSubstring === 'object') {
        for (const sub of Object.keys(data.byDomainSubstring)) {
          if (!sub) continue;
          const list = data.byDomainSubstring[sub];
          if (host.includes(sub.toLowerCase()) && Array.isArray(list) && list.length) {
            return list;
          }
        }
      }
      if (Array.isArray(data.default) && data.default.length) return data.default;
    } catch (e) {
      console.warn('[ClientPlugin] launcher hints parse error', e);
    }
    return LAUNCHER_HINTS_FALLBACK.slice();
  }

  async function loadLauncherHintMessages(pageCtx, cfg) {
    try {
      const url = chrome.runtime.getURL('launcher-hints.json');
      const r = await fetch(url);
      if (!r.ok) return LAUNCHER_HINTS_FALLBACK.slice();
      const data = await r.json();
      return selectMessagesFromHintsData(data, pageCtx, cfg);
    } catch (e) {
      console.warn('[ClientPlugin] launcher-hints.json load failed', e);
      return LAUNCHER_HINTS_FALLBACK.slice();
    }
  }

  function removeWidgetFromPage() {
    disconnectWs();
    const widget = document.getElementById('product-chat-demo-widget');
    if (widget) widget.remove();
  }

  function getMessagesContainer() {
    return document.querySelector('#product-chat-demo-widget #chat-messages');
  }

  if (settings.enableWidget && settings.clientName && isCurrentSiteAllowed()) {
    initWidget();
  } else {
    removeWidgetFromPage();
  }

  // ===================== CHAT HISTORY PERSISTENCE =====================

  const CHAT_HISTORY_SAVE_CAP = 100;
  const CHAT_HISTORY_MAX_AGE_MS = 48 * 60 * 60 * 1000;
  const CHAT_HISTORY_FIRST_PAINT = 20;
  const CHAT_HISTORY_PRELOAD = 15;
  const CHAT_HISTORY_RECENT_WINDOW = CHAT_HISTORY_FIRST_PAINT + CHAT_HISTORY_PRELOAD;
  const CHAT_HISTORY_LOAD_CHUNK = 15;
  const CHAT_HISTORY_RESTORE_BATCH = 5;
  let cachedHistoryBlocks = [];
  let historyRestored = false;
  let historyWindowStart = 0;
  let historyFirstMessageNode = null;
  let historyLoadEarlierBtnEl = null;

  function getChatBlocksStorageKey() {
    return 'ecomm_plugin_blocks_' + (sessionId || getSessionId());
  }

  function loadMessagesFromStorage() {
    try {
      const key = getChatBlocksStorageKey();
      const raw = JSON.parse(localStorage.getItem(key) || '[]');
      const cutoff = new Date(Date.now() - CHAT_HISTORY_MAX_AGE_MS);
      let recent = raw.filter(function (msg) { return new Date(msg.timestamp) > cutoff; });
      if (recent.length > CHAT_HISTORY_SAVE_CAP) {
        recent = recent.slice(-CHAT_HISTORY_SAVE_CAP);
      }
      if (recent.length !== raw.length) {
        localStorage.setItem(key, JSON.stringify(recent));
      }
      cachedHistoryBlocks = recent;
      return recent;
    } catch (e) {
      console.warn('[ClientPlugin] loadMessagesFromStorage:', e);
      cachedHistoryBlocks = [];
      return [];
    }
  }

  function saveMessagesArray(messages) {
    try { localStorage.setItem(getChatBlocksStorageKey(), JSON.stringify(messages)); } catch (e) {}
  }

  function pushMessageBlock(block) {
    var messages = [];
    try { messages = JSON.parse(localStorage.getItem(getChatBlocksStorageKey()) || '[]'); } catch (e) {}
    messages.push(block);
    if (messages.length > CHAT_HISTORY_SAVE_CAP) messages = messages.slice(-CHAT_HISTORY_SAVE_CAP);
    saveMessagesArray(messages);
  }

  function persistBotMessageBlock(text) {
    if (!text || !String(text).trim()) return;
    pushMessageBlock({ type: 'message', sender: 'bot', text: String(text), timestamp: new Date().toISOString() });
  }

  function restoreChatHistory() {
    if (historyRestored) return;

    var all = loadMessagesFromStorage();
    if (!all || all.length === 0) return;

    historyWindowStart = Math.max(0, all.length - CHAT_HISTORY_RECENT_WINDOW);
    var slice = all.slice(historyWindowStart);
    var messagesContainer = getMessagesContainer();
    if (!messagesContainer) return;

    var tailLen = Math.min(CHAT_HISTORY_FIRST_PAINT, slice.length);
    var tail = slice.slice(-tailLen);
    var head = slice.length > tailLen ? slice.slice(0, slice.length - tailLen) : [];
    var tailIdx = 0;
    var tailFirstNode = null;

    function finalizeRestore(topMessageNode) {
      historyFirstMessageNode = topMessageNode || messagesContainer.firstChild;

      var oldestItem = all[historyWindowStart];
      if (oldestItem && oldestItem.timestamp) {
        var separator = document.createElement('div');
        separator.className = 'history-separator';
        var timeAgo = getTimeAgo(new Date(oldestItem.timestamp));
        var truncated = all.length > CHAT_HISTORY_RECENT_WINDOW;
        separator.innerHTML = truncated
          ? 'Previous conversation (' + timeAgo + ') <span style="opacity:.8">(showing recent messages)</span>'
          : 'Previous conversation (' + timeAgo + ')';
        messagesContainer.insertBefore(separator, messagesContainer.firstChild);

        if (historyWindowStart > 0) {
          var btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'history-load-earlier';
          btn.textContent = 'Load earlier messages';
          btn.addEventListener('click', onLoadEarlierHistoryClick);
          messagesContainer.insertBefore(btn, separator);
          historyLoadEarlierBtnEl = btn;
        }
      }

      messagesContainer.scrollTop = messagesContainer.scrollHeight;
      historyRestored = true;
    }

    function pumpTail() {
      var end = Math.min(tailIdx + CHAT_HISTORY_RESTORE_BATCH, tail.length);
      for (; tailIdx < end; tailIdx++) {
        var node = appendHistoryItemToMessages(tail[tailIdx], null);
        if (tailIdx === 0 && node) tailFirstNode = node;
      }
      if (tailIdx < tail.length) {
        requestAnimationFrame(pumpTail);
        return;
      }
      if (head.length === 0) {
        finalizeRestore(tailFirstNode);
        return;
      }

      var schedulePreload = typeof window.requestIdleCallback === 'function'
        ? function(cb) { window.requestIdleCallback(cb, { timeout: 400 }); }
        : function(cb) { setTimeout(cb, 0); };

      schedulePreload(function() {
        var headIdx = 0;
        var headFirstNode = null;
        var anchor = tailFirstNode;

        function pumpHead() {
          var headEnd = Math.min(headIdx + CHAT_HISTORY_RESTORE_BATCH, head.length);
          for (; headIdx < headEnd; headIdx++) {
            var node = appendHistoryItemToMessages(head[headIdx], anchor);
            if (headIdx === 0 && node) headFirstNode = node;
          }
          if (headIdx < head.length) {
            requestAnimationFrame(pumpHead);
            return;
          }
          finalizeRestore(headFirstNode || tailFirstNode);
        }

        requestAnimationFrame(pumpHead);
      });
    }

    requestAnimationFrame(pumpTail);
  }

  function appendHistoryItemToMessages(item, insertBeforeNode) {
    var messagesContainer = getMessagesContainer();
    if (!messagesContainer || !item || item.type !== 'message' || !item.sender || !item.text) return null;

    var el = document.createElement('div');
    el.className = 'message ' + (item.sender === 'bot' ? 'bot' : 'user');
    if (item.sender === 'bot') {
      el.innerHTML = formatBotHtml(item.text);
    } else {
      el.innerHTML = escapeHtml(item.text).replace(/\n/g, '<br>');
    }

    if (insertBeforeNode) messagesContainer.insertBefore(el, insertBeforeNode);
    else messagesContainer.appendChild(el);
    return el;
  }

  function onLoadEarlierHistoryClick() {
    var btn = historyLoadEarlierBtnEl;
    if (!btn || btn.disabled) return;
    var all = cachedHistoryBlocks;
    if (historyWindowStart <= 0 || !all || all.length === 0) return;

    var anchor = historyFirstMessageNode;
    if (!anchor) return;

    var messagesContainer = getMessagesContainer();
    if (!messagesContainer) return;

    var scrollHeightBefore = messagesContainer.scrollHeight;
    var scrollTopBefore = messagesContainer.scrollTop;
    btn.disabled = true;

    var newStart = Math.max(0, historyWindowStart - CHAT_HISTORY_LOAD_CHUNK);
    var chunk = all.slice(newStart, historyWindowStart);
    var i = 0;
    var newFirstRoot = null;

    function pumpEarlier() {
      var end = Math.min(i + CHAT_HISTORY_RESTORE_BATCH, chunk.length);
      for (; i < end; i++) {
        var node = appendHistoryItemToMessages(chunk[i], anchor);
        if (i === 0 && node) newFirstRoot = node;
      }
      if (i < chunk.length) {
        requestAnimationFrame(pumpEarlier);
        return;
      }
      if (newFirstRoot) historyFirstMessageNode = newFirstRoot;
      historyWindowStart = newStart;
      if (historyWindowStart <= 0 && historyLoadEarlierBtnEl) {
        historyLoadEarlierBtnEl.remove();
        historyLoadEarlierBtnEl = null;
      } else {
        btn.disabled = false;
      }
      messagesContainer.scrollTop = scrollTopBefore + (messagesContainer.scrollHeight - scrollHeightBefore);
    }

    requestAnimationFrame(pumpEarlier);
  }

  function getTimeAgo(date) {
    var seconds = Math.floor((new Date() - date) / 1000);
    if (seconds < 60) return 'just now';
    if (seconds < 3600) return Math.floor(seconds / 60) + ' minutes ago';
    if (seconds < 86400) return Math.floor(seconds / 3600) + ' hours ago';
    return Math.floor(seconds / 86400) + ' days ago';
  }

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

  function normalizePhoneNumber(phoneInput) {
    const digits = String(phoneInput || '').replace(/\D/g, '');
    if (digits.length === 10 && /^[6-9]/.test(digits)) return digits;
    return null;
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
        removeAllTypingIndicators();
        if (currentStreamText && currentStreamText.trim()) {
          chatHistory.push({ role: 'assistant', content: currentStreamText });
          persistBotMessageBlock(currentStreamText);
          const urls = extractProductUrlsFromText(currentStreamText);
          if (urls.length > 0) showProductImageCards(urls);
        } else if (currentStreamEl) {
          currentStreamEl.remove();
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
          persistBotMessageBlock(currentStreamText);
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

      case 'phone_required':
        removeAllTypingIndicators();
        showPhoneNudge(true, data.message || 'We need your 10-digit mobile number to continue. Please provide it below.');
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
        fetch('/cart.js')
          .then(cartRes => cartRes.json())
          .then(cart => {
            syncStorefrontCartUi(cart);
            addMessage('bot', 'Added to cart! You can proceed to checkout.');
          })
          .catch(() => {
            addMessage('bot', 'Added to cart! You can proceed to checkout.');
          });
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

  function syncStorefrontCartUi(cart) {
    if (!cart) return;

    const detail = { cart, source: 'underneat-extension' };
    ['fashionbot:cart-updated', 'cart:updated', 'cart:added'].forEach((name) => {
      try { document.dispatchEvent(new CustomEvent(name, { detail })); } catch (e) {}
      try { window.dispatchEvent(new CustomEvent(name, { detail })); } catch (e) {}
    });

    try {
      if (typeof window.publish === 'function' && window.PUB_SUB_EVENTS && window.PUB_SUB_EVENTS.cartUpdate) {
        window.publish(window.PUB_SUB_EVENTS.cartUpdate, {
          source: 'underneat-extension',
          cartData: cart
        });
      }
    } catch (e) {}

    const count = Number(cart.item_count || 0);
    [
      '.cart-count',
      '.cart-item-count',
      '[data-cart-count]',
      '#CartCount',
      '.cart-count-bubble',
      '.cart-count-bubble span',
      '.header__icon .cart-count-bubble',
      '.header__icon .cart-count-bubble span'
    ].forEach((sel) => {
      try {
        document.querySelectorAll(sel).forEach((el) => {
          if (!el) return;
          if (el.matches('.cart-count-bubble') && el.querySelector('span')) {
            el.querySelector('span').textContent = String(count);
          } else {
            el.textContent = String(count);
          }
          el.style.display = count > 0 ? '' : 'none';
        });
      } catch (e) {}
    });
  }

  // ===================== CHAT WIDGET UI =====================

  function initWidget() {
    userPhone = getSavedPhone();

    const widget = document.createElement('div');
    widget.id = 'product-chat-demo-widget';
    widget.innerHTML = `
      <button class="chat-toggle-btn" aria-label="Open chat">
        <span class="toggle-preview-panel">
          <span class="toggle-stream-row">
            <span class="toggle-stream-text" aria-live="polite"></span><span class="toggle-stream-caret" aria-hidden="true"></span>
          </span>
          <span class="toggle-avatar-wrap">
            <span class="toggle-avatar">
              <img class="toggle-avatar-img" src="${BRAND_ICON_URL}" alt="Underneat" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
              <span class="toggle-avatar-fallback" style="display:none">u</span>
            </span>
            <span class="toggle-dot"></span>
          </span>
        </span>
      </button>

        <div class="chat-window">
        <div class="chat-header">
          <div class="chat-header-info">
            <div class="chat-header-avatar">
              <img class="chat-header-avatar-img" src="${BRAND_ICON_URL}" alt="Underneat" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
              <span class="chat-header-avatar-fallback" style="display:none">u</span>
            </div>
            <div class="chat-header-text">
              <h3>Underneat Assistant</h3>
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

        <div class="chat-body" id="chat-body" style="display:none;">
          <div class="chat-messages" id="chat-messages"></div>
          <div class="phone-nudge" id="phone-nudge" style="display:none;">
            <p class="phone-nudge-title" id="phone-nudge-title">We need your 10-digit mobile number to continue. Please provide it below.</p>
            <div class="phone-nudge-row">
              <input type="tel" id="phone-nudge-input" class="phone-nudge-input" placeholder="10-digit mobile" maxlength="15" autocomplete="tel">
              <button type="button" id="phone-nudge-submit" class="phone-nudge-submit">Submit</button>
            </div>
            <p id="phone-nudge-error" class="phone-nudge-error" style="display:none;"></p>
          </div>

          <div class="chat-input-area">
            <div class="chat-input-container">
              <input type="text" class="chat-input" id="chat-input" placeholder="Ask about products, orders, sizes..." autocomplete="off">
              <button class="chat-send-btn" id="chat-send-btn" aria-label="Send message">
                <svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
              </button>
            </div>
            <div class="quick-actions-title" id="quick-actions-title" style="display:none;">Quick Actions</div>
            <div class="quick-actions" id="quick-actions"></div>
          </div>
          <div class="chat-footer">Powered by Bloomerce</div>
        </div>
      </div>
    `;

    document.body.appendChild(widget);

    const toggleBtn = widget.querySelector('.chat-toggle-btn');
    const closeBtn = widget.querySelector('.chat-close-btn');
    const chatWindow = widget.querySelector('.chat-window');
    const chatBody = widget.querySelector('#chat-body');
    const phoneNudge = widget.querySelector('#phone-nudge');
    const phoneNudgeTitle = widget.querySelector('#phone-nudge-title');
    const phoneNudgeInput = widget.querySelector('#phone-nudge-input');
    const phoneNudgeSubmit = widget.querySelector('#phone-nudge-submit');
    const phoneNudgeError = widget.querySelector('#phone-nudge-error');
    const chatInput = widget.querySelector('#chat-input');
    const sendBtn = widget.querySelector('#chat-send-btn');
    const quickActionsTitle = widget.querySelector('#quick-actions-title');
    const quickActions = widget.querySelector('#quick-actions');
    const streamTextEl = widget.querySelector('.toggle-stream-text');
    const streamCaretEl = widget.querySelector('.toggle-stream-caret');

    pageContext = scrapePageContext();

    let launcherAbort = false;
    let launcherTypeIntervalId = null;
    let launcherPauseTimeoutId = null;
    let lastLauncherMessage = '';

    function stopLauncherRotation() {
      launcherAbort = true;
      if (launcherTypeIntervalId) {
        clearInterval(launcherTypeIntervalId);
        launcherTypeIntervalId = null;
      }
      if (launcherPauseTimeoutId) {
        clearTimeout(launcherPauseTimeoutId);
        launcherPauseTimeoutId = null;
      }
    }

    function pickNextLauncherMessage(messages) {
      if (!messages || !messages.length) return '';
      let next = messages[Math.floor(Math.random() * messages.length)];
      let guard = 0;
      while (messages.length > 1 && next === lastLauncherMessage && guard++ < 12) {
        next = messages[Math.floor(Math.random() * messages.length)];
      }
      lastLauncherMessage = next;
      return next;
    }

    function formatLauncherMessage(text) {
      let formatted = String(text || '').trim();
      if (!formatted) return '';
      formatted = formatted.charAt(0).toUpperCase() + formatted.slice(1);
      if (!/[.!?]$/.test(formatted)) formatted += '?';
      return formatted;
    }

    function typewriterShow(fullText, onComplete) {
      if (!streamTextEl) {
        if (onComplete) onComplete();
        return;
      }
      fullText = formatLauncherMessage(fullText);
      streamTextEl.textContent = '';
      if (streamCaretEl) streamCaretEl.style.display = '';
      let i = 0;
      const msPerChar = Math.min(52, Math.max(26, Math.floor(900 / Math.max(fullText.length, 1))));
      launcherTypeIntervalId = window.setInterval(() => {
        if (launcherAbort) {
          clearInterval(launcherTypeIntervalId);
          launcherTypeIntervalId = null;
          return;
        }
        i += 1;
        streamTextEl.textContent = fullText.slice(0, i);
        toggleBtn.setAttribute('aria-label', `Open chat - try: ${fullText}`);
        if (i >= fullText.length) {
          clearInterval(launcherTypeIntervalId);
          launcherTypeIntervalId = null;
          if (!launcherAbort && onComplete) onComplete();
        }
      }, msPerChar);
    }

    function startLauncherRotation(messages) {
      stopLauncherRotation();
      launcherAbort = false;
      if (!streamTextEl || !messages || !messages.length) return;
      function cycle() {
        if (launcherAbort) return;
        const text = pickNextLauncherMessage(messages);
        typewriterShow(text, () => {
          if (launcherAbort) return;
          launcherPauseTimeoutId = window.setTimeout(() => {
            if (launcherAbort) return;
            cycle();
          }, 2800);
        });
      }
      cycle();
    }

    loadLauncherHintMessages(pageContext, settings).then((messages) => {
      launcherMessagesCache = messages;
      startLauncherRotation(messages);
    });

    function showPhoneNudge(show, message) {
      phoneCollectionBlocked = !!show;
      if (phoneNudge) phoneNudge.style.display = show ? 'block' : 'none';
      if (phoneNudgeTitle && message) phoneNudgeTitle.textContent = message;
      if (!show) setPhoneNudgeError('');
      if (chatInput) chatInput.disabled = phoneCollectionBlocked;
      if (sendBtn) sendBtn.disabled = phoneCollectionBlocked;
      if (show) {
        window.setTimeout(() => {
          if (phoneNudgeInput) phoneNudgeInput.focus();
        }, 50);
      }
    }

    function setPhoneNudgeError(message) {
      if (!phoneNudgeError) return;
      if (message) {
        phoneNudgeError.textContent = message;
        phoneNudgeError.style.display = 'block';
      } else {
        phoneNudgeError.textContent = '';
        phoneNudgeError.style.display = 'none';
      }
    }

    function sendPhoneUpdateToServer() {
      if (!userPhone) return;
      const trySend = () => {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'phone_update', phone: userPhone }));
        } else {
          window.setTimeout(trySend, 100);
        }
      };
      trySend();
    }

    function submitPhoneFromNudge() {
      const validated = normalizePhoneNumber(phoneNudgeInput ? phoneNudgeInput.value : '');
      if (!validated) {
        setPhoneNudgeError('Please enter a valid 10-digit mobile number.');
        return;
      }
      savePhone(validated);
      setPhoneNudgeError('');
      if (phoneNudgeInput) phoneNudgeInput.value = '';
      showPhoneNudge(false);
      sendPhoneUpdateToServer();
      if (chatInput) chatInput.focus();
    }

    if (phoneNudgeSubmit) {
      phoneNudgeSubmit.addEventListener('click', submitPhoneFromNudge);
    }
    if (phoneNudgeInput) {
      phoneNudgeInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          submitPhoneFromNudge();
        }
      });
    }

    function showChatInterface() {
      chatBody.style.display = 'flex';
      updateQuickActions();

      sessionId = getSessionId();
      restoreChatHistory();

      if (!ws || ws.readyState === WebSocket.CLOSED) {
        connectWs();
      }

      var mc = document.getElementById('chat-messages');
      var hasMsgs = mc && mc.querySelector('.message');
      if (!hasMsgs) {
        sendWelcomeMessage();
      }
      chatInput.focus();
    }

    // ---- Toggle chat open ----
    toggleBtn.addEventListener('click', () => {
      stopLauncherRotation();
      chatWindow.classList.add('open');
      toggleBtn.style.display = 'none';
      showChatInterface();
    });

    closeBtn.addEventListener('click', () => {
      chatWindow.classList.remove('open');
      toggleBtn.style.display = 'flex';
      if (launcherMessagesCache && launcherMessagesCache.length) {
        startLauncherRotation(launcherMessagesCache);
      } else {
        loadLauncherHintMessages(pageContext, settings).then((messages) => {
          launcherMessagesCache = messages;
          startLauncherRotation(messages);
        });
      }
    });

    // Theme toggle
    const themeBtn = widget.querySelector('#chat-theme-btn');
    let isLightMode = true;
    widget.classList.add('light-mode');
    themeBtn.textContent = '🌙';
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
      const button = e.target.closest('.quick-action-btn');
      if (button) {
        chatInput.value = button.dataset.message;
        handleSend();
      }
    });

    function handleSend() {
      if (phoneCollectionBlocked) {
        if (phoneNudgeInput) phoneNudgeInput.focus();
        return;
      }
      const msg = chatInput.value.trim();
      if (!msg) return;
      chatInput.value = '';
      addMessage('user', msg);
      addTypingIndicator();
      sendWsMessage(msg);
    }

    function updateQuickActions() {
      const actions = [
        { text: '📦 Track order', message: 'Track my order' },
        { text: '🔄 Return & Exchange', message: 'Tell me about returns and exchanges' }
      ];
      const hasCards = actions.some(a => inferQuickActionKind(a));
      if (quickActionsTitle) quickActionsTitle.style.display = hasCards ? 'block' : 'none';
      quickActions.innerHTML = actions.map(a => {
        const actionKind = inferQuickActionKind(a);
        const cardClass = actionKind ? ' quick-action-btn--card' : '';
        const iconHtml = actionKind
          ? `<span class="quick-action-card-badge">${quickActionIconSvg(actionKind)}</span>`
          : '';
        const label = actionKind ? cleanQuickActionLabel(a.text) : a.text;
        return `<button class="quick-action-btn${cardClass}" data-message="${a.message}">${iconHtml}<span class="quick-action-label">${label}</span></button>`;
      }).join('');
    }

    function cleanQuickActionLabel(text) {
      return String(text || '').replace(/^[^\p{L}\p{N}]+/u, '').trim();
    }

    function inferQuickActionKind(action) {
      const haystack = `${action?.text || ''} ${action?.message || ''}`.toLowerCase();
      if (haystack.includes('track') && haystack.includes('order')) return 'track_order';
      if (haystack.includes('return') || haystack.includes('exchange')) return 'return_exchange';
      if (haystack.includes('contact support') || haystack.includes('support') || haystack.includes('help')) return 'contact_support';
      return null;
    }

    function quickActionIconSvg(kind) {
      if (kind === 'track_order') {
        return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7h11v10H3z"/><path d="M14 10h3l4 4v3h-7z"/><circle cx="8" cy="18" r="1.5"/><circle cx="18" cy="18" r="1.5"/></svg>';
      }
      if (kind === 'return_exchange') {
        return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7h11"/><path d="M15 4l3 3-3 3"/><path d="M17 17H6"/><path d="M9 14l-3 3 3 3"/></svg>';
      }
      if (kind === 'contact_support') {
        return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12a8 8 0 0 1 16 0"/><path d="M5 14v2a2 2 0 0 0 2 2h1v-6H7a2 2 0 0 0-2 2Z"/><path d="M19 14v2a2 2 0 0 1-2 2h-1v-6h1a2 2 0 0 1 2 2Z"/><path d="M12 19v1"/></svg>';
      }
      return '';
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
        if (!chatWindow.classList.contains('open')) {
          loadLauncherHintMessages(pageContext, settings).then((messages) => {
            launcherMessagesCache = messages;
            startLauncherRotation(messages);
          });
        }
      }
    }).observe(document.body, { childList: true, subtree: true });
  }

  // ===================== UI HELPERS =====================

  function escapeHtml(unsafe) {
    return String(unsafe)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function formatBotHtml(text) {
    text = linkProductNamesInText(text);
    text = escapeHtml(text);
    text = text
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/__(.+?)__/g, '<strong>$1</strong>')
      .replace(/\*(.+?)\*/g, '<em>$1</em>')
      .replace(/_(.+?)_/g, '<em>$1</em>')
      .replace(/~~(.*?)~~/g, '<s>$1</s>')
      .replace(/`(.*?)`/g, '<code>$1</code>')
      .replace(/\n/g, '<br>');
    text = text.replace(/^[-*]\s+(.+?)(<br>|$)/gm, '<li>$1</li>');
    text = text.replace(/(<li>.*?<\/li>)+/gs, '<ul>$&</ul>');
    text = text.replace(
      /\[([^\]]+)\]\(([^)]+)\)/g,
      function (_m, label, href) {
        return '<a href="' + href.replace(/&amp;/g, '&') + '" target="_blank" rel="noopener">' + label + '</a>';
      }
    );
    text = text.replace(/(?:https?:\/\/[^\s<"]+)/g, function (match, offset, str) {
      if (offset >= 6 && str.slice(offset - 6, offset) === 'href="') return match;
      if (offset >= 5 && str.slice(offset - 5, offset) === 'src="') return match;
      if (offset >= 2 && str.slice(offset - 2, offset) === '">') return match;
      var href = match.replace(/&amp;/g, '&');
      return '<a href="' + href + '" target="_blank" rel="noopener">' + match + '</a>';
    });
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
    return output.join('\n');
  }

  function addMessage(type, text, options) {
    const messagesContainer = getMessagesContainer();
    if (!messagesContainer) return;
    const skipStorage = options && options.skipStorage;
    const el = document.createElement('div');
    el.className = `message ${type}`;
    const plain = text;

    if (type === 'bot') {
      text = formatBotHtml(text);
    } else {
      text = escapeHtml(text).replace(/\n/g, '<br>');
    }

    el.innerHTML = text;
    messagesContainer.appendChild(el);
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
    chatHistory.push({ role: type === 'user' ? 'user' : 'assistant', content: plain });

    if (type === 'bot' && !skipStorage) {
      persistBotMessageBlock(plain);
    }
    if (type === 'user' && !skipStorage) {
      pushMessageBlock({ type: 'message', sender: 'user', text: plain, timestamp: new Date().toISOString() });
    }

    if (type === 'bot') {
      const urls = extractProductUrlsFromText(plain);
      if (urls.length > 0) showProductImageCards(urls);
    }
    return el;
  }

  function addSuggestionChips(suggestions) {
    const messagesContainer = getMessagesContainer();
    if (!messagesContainer) return;
    const container = document.createElement('div');
    container.className = 'suggestion-chips';
    suggestions.forEach(s => {
      const chip = document.createElement('button');
      chip.className = 'suggestion-chip';
      chip.textContent = s.text;
      chip.addEventListener('click', () => {
        container.remove();
        const input = document.querySelector('#product-chat-demo-widget #chat-input');
        if (input) { input.value = s.message; }
        const sendBtn = document.querySelector('#product-chat-demo-widget #chat-send-btn');
        if (sendBtn) sendBtn.click();
      });
      container.appendChild(chip);
    });
    messagesContainer.appendChild(container);
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
  }

  function addTypingIndicator() {
    const messagesContainer = getMessagesContainer();
    if (!messagesContainer) return;
    const el = document.createElement('div');
    el.className = 'message bot typing';
    el.id = 'typing-' + Date.now();
    el.innerHTML = '<span class="typing-dots" aria-hidden="true"><span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span></span><span class="typing-label">Thinking</span>';
    messagesContainer.appendChild(el);
    const typingLabel = el.querySelector('.typing-label');
    if (typingLabelTimeouts.length) {
      typingLabelTimeouts.forEach(clearTimeout);
      typingLabelTimeouts = [];
    }
    typingLabelTimeouts.push(setTimeout(() => {
      if (!typingLabel || !typingLabel.isConnected) return;
      typingLabel.textContent = 'Looking for the answer';
    }, 2000));
    typingLabelTimeouts.push(setTimeout(() => {
      if (!typingLabel || !typingLabel.isConnected) return;
      typingLabel.textContent = 'Typing your response';
      typingLabelTimeouts = [];
    }, 4000));
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
  }

  function removeAllTypingIndicators() {
    if (typingLabelTimeouts.length) {
      typingLabelTimeouts.forEach(clearTimeout);
      typingLabelTimeouts = [];
    }
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
    const messagesContainer = getMessagesContainer();
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
      const mc = getMessagesContainer();
      if (mc) mc.scrollTop = mc.scrollHeight;
    });
  }

  // Product cards from backend /products event (structured data)
  function addProductCards(products) {
    const messagesContainer = getMessagesContainer();
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
