// content.js - Enhanced Chat Widget with Shopify Runtime Support

(function() {
  'use strict';
  
  // Prevent multiple injections
  if (window.__productChatDemoLoaded) return;
  window.__productChatDemoLoaded = true;
  
  let settings = {
    backendUrl: 'http://localhost:8000/demo',
    clientId: '',
    apiKey: '',
    enableWidget: true
  };
  
  let chatHistory = [];
  let recentProducts = [];
  let pageContext = null;
  let clientLocationPromise = null;
  let publicIpPromise = null;
  const ENABLE_IP_LOCATION_FALLBACK = false;
  let isShopifyStore = false;
  let carouselShownForQuery = false; // Track if carousel was shown for current query type
  let lastCarouselQuery = ''; // Track what query triggered the carousel
  
  // Load settings from storage
  chrome.runtime.sendMessage({ type: 'GET_SETTINGS' }, (response) => {
    if (response) {
      settings = response;
      if (settings.enableWidget) {
        initWidget();
      }
    }
  });
  
  // Listen for settings updates
  chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request.type === 'SETTINGS_UPDATED') {
      settings = request.settings;
      if (settings.enableWidget && !document.getElementById('product-chat-demo-widget')) {
        initWidget();
      } else if (!settings.enableWidget) {
        const widget = document.getElementById('product-chat-demo-widget');
        if (widget && typeof widget._launcherCleanup === 'function') {
          widget._launcherCleanup();
        }
        if (widget) widget.remove();
      }
    }
  });
  
  // ==================== SHOPIFY DETECTION ====================
  
  function detectShopifyStore() {
    // Multiple ways to detect Shopify
    const indicators = [
      () => window.Shopify !== undefined,
      () => document.querySelector('link[href*="cdn.shopify.com"]') !== null,
      () => document.querySelector('script[src*="cdn.shopify.com"]') !== null,
      () => document.querySelector('meta[name="shopify-checkout-api-token"]') !== null,
      () => /\.myshopify\.com/.test(window.location.hostname)
    ];
    
    return indicators.some(check => {
      try { return check(); } catch { return false; }
    });
  }
  
  // ==================== PAGE SCRAPER ====================

  function baseClientLocation(status) {
    return {
      permissionStatus: status,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || null,
      locale: navigator.language || null,
      capturedAt: new Date().toISOString()
    };
  }

  function getPublicIpLocation() {
    if (publicIpPromise) {
      return publicIpPromise;
    }

    publicIpPromise = fetch('https://ipwho.is/')
      .then((response) => response.ok ? response.json() : null)
      .then((data) => {
        if (!data || data.success === false) return null;
        return {
          publicIp: data.ip || null,
          publicCity: data.city || null,
          publicPincode: data.postal || data.zip || null,
          publicRegion: data.region || null,
          publicCountry: data.country || null,
          publicCountryCode: data.country_code || null,
          publicLatitude: data.latitude || null,
          publicLongitude: data.longitude || null,
          publicTimezone: data.timezone && data.timezone.id ? data.timezone.id : data.timezone || null,
          publicIsp: data.connection && data.connection.isp ? data.connection.isp : null
        };
      })
      .catch(() => null);

    return publicIpPromise;
  }

  async function withPublicIpLocation(location) {
    if (!ENABLE_IP_LOCATION_FALLBACK) {
      return location;
    }

    const publicIpLocation = await getPublicIpLocation();
    return publicIpLocation ? { ...location, ...publicIpLocation } : location;
  }

  function getClientLocation() {
    if (clientLocationPromise) {
      return clientLocationPromise;
    }

    clientLocationPromise = new Promise((resolve) => {
      if (!navigator.geolocation) {
        withPublicIpLocation(baseClientLocation('unavailable')).then(resolve);
        return;
      }

      navigator.geolocation.getCurrentPosition(
        (position) => {
          withPublicIpLocation({
            ...baseClientLocation('granted'),
            latitude: position.coords.latitude,
            longitude: position.coords.longitude,
            accuracy: position.coords.accuracy
          }).then(resolve);
        },
        (error) => {
          const status = error && error.code === error.PERMISSION_DENIED ? 'denied' : 'unavailable';
          withPublicIpLocation({
            ...baseClientLocation(status),
            errorCode: error ? error.code : null,
            errorMessage: error ? error.message : null
          }).then(resolve);
        },
        {
          enableHighAccuracy: true,
          timeout: 8000,
          maximumAge: 10 * 60 * 1000
        }
      );
    });

    return clientLocationPromise;
  }
  
  function scrapePageContext() {
    isShopifyStore = detectShopifyStore();
    
    const context = {
      url: window.location.href,
      domain: window.location.hostname,
      title: document.title,
      timestamp: new Date().toISOString(),
      isShopify: isShopifyStore
    };
    
    // Get meta description
    const metaDesc = document.querySelector('meta[name="description"]');
    if (metaDesc) {
      context.metaDescription = metaDesc.content;
    }
    
    // Try to extract product information
    context.product = extractProductInfo();
    
    // Get page text content (limited for context)
    context.pageText = extractPageText();
    
    // Get structured data (JSON-LD)
    context.structuredData = extractStructuredData();
    
    // Get Open Graph data
    context.openGraph = extractOpenGraphData();
    
    // For Shopify stores, try to get additional data
    if (isShopifyStore) {
      context.shopifyData = extractShopifyData();
    }
    
    return context;
  }
  
  function extractShopifyData() {
    const data = {};
    
    // Get product handle from URL
    const urlMatch = window.location.pathname.match(/\/products\/([^\/\?]+)/);
    if (urlMatch) {
      data.productHandle = urlMatch[1];
    }
    
    // Get collection handle from URL
    const collectionMatch = window.location.pathname.match(/\/collections\/([^\/\?]+)/);
    if (collectionMatch) {
      data.collectionHandle = collectionMatch[1];
    }
    
    // Try to get Shopify product JSON if available
    try {
      const productJson = document.querySelector('script[type="application/json"][data-product-json]');
      if (productJson) {
        data.productJson = JSON.parse(productJson.textContent);
      }
    } catch {}
    
    // Try to get from Shopify global
    try {
      if (window.ShopifyAnalytics && window.ShopifyAnalytics.meta && window.ShopifyAnalytics.meta.product) {
        data.analyticsProduct = window.ShopifyAnalytics.meta.product;
      }
    } catch {}
    
    return Object.keys(data).length > 0 ? data : null;
  }
  
  function extractProductInfo() {
    const product = {};
    
    // Common product selectors
    const nameSelectors = [
      'h1.product-title', 'h1.product_title', 'h1.product-name',
      '.product-title h1', '.product_title', '.product-name',
      'h1[itemprop="name"]', '.pdp-title', '.product-info h1',
      '.product-single__title', '.product__title', // Shopify common
      'h1', '.title h1'
    ];
    
    const priceSelectors = [
      '.price', '.product-price', '.pdp-price', '[itemprop="price"]',
      '.price-box', '.offer-price', '.sale-price', '.current-price',
      '.woocommerce-Price-amount', 'span.price', 'ins .amount',
      '.product__price', '.price-item--sale', '.price-item--regular' // Shopify
    ];
    
    const originalPriceSelectors = [
      '.original-price', '.compare-price', '.was-price', 'del .amount',
      '.regular-price', 's .amount', 'del', '.price-item--compare'
    ];
    
    const descriptionSelectors = [
      '.product-description', '.description', '[itemprop="description"]',
      '.product-info', '.pdp-description', '#description', 
      '.woocommerce-product-details__short-description',
      '.product__description', '.product-single__description' // Shopify
    ];
    
    const imageSelectors = [
      '.product-image img', '.pdp-image img', '[itemprop="image"]',
      '.product-gallery img', '.woocommerce-product-gallery img',
      '.product-featured-image img', 'img.wp-post-image',
      '.product__media img', '.product-single__photo img' // Shopify
    ];
    
    // Extract name
    for (const selector of nameSelectors) {
      const el = document.querySelector(selector);
      if (el && el.textContent.trim()) {
        let name = el.textContent.trim().replace(/\s+/g, ' ');
        // Deduplicate: some sites render the name twice inside the h1
        const half = Math.floor(name.length / 2);
        const first = name.substring(0, half).trim();
        const second = name.substring(half).trim();
        if (first && first === second) {
          name = first;
        }
        product.name = name;
        break;
      }
    }
    
    // Extract price
    for (const selector of priceSelectors) {
      const el = document.querySelector(selector);
      if (el && el.textContent.trim()) {
        product.price = el.textContent.trim().replace(/\s+/g, ' ');
        break;
      }
    }
    
    // Extract original price
    for (const selector of originalPriceSelectors) {
      const el = document.querySelector(selector);
      if (el && el.textContent.trim()) {
        product.originalPrice = el.textContent.trim().replace(/\s+/g, ' ');
        break;
      }
    }
    
    // Calculate discount if both prices available
    if (product.price && product.originalPrice) {
      const currentPrice = parseFloat(product.price.replace(/[^0-9.]/g, ''));
      const origPrice = parseFloat(product.originalPrice.replace(/[^0-9.]/g, ''));
      if (origPrice > currentPrice) {
        product.discount = Math.round(((origPrice - currentPrice) / origPrice) * 100) + '% OFF';
      }
    }
    
    // Extract description
    for (const selector of descriptionSelectors) {
      const el = document.querySelector(selector);
      if (el && el.textContent.trim()) {
        product.description = el.textContent.trim().substring(0, 500);
        break;
      }
    }
    
    // Extract image and image metadata
    for (const selector of imageSelectors) {
      const el = document.querySelector(selector);
      if (el && el.src) {
        product.image = el.src;
        // Try to extract additional info from image attributes
        if (el.alt) product.imageAlt = el.alt;
        if (el.title) product.imageTitle = el.title;
        break;
      }
    }
    
    // Extract ALL product images for more context
    const allProductImages = document.querySelectorAll('.product-image img, .product-gallery img, .product-photos img, .product__media img, [data-product-image]');
    const imageDescriptions = [];
    allProductImages.forEach(img => {
      if (img.alt && img.alt.trim()) {
        imageDescriptions.push(img.alt.trim());
      }
      if (img.title && img.title.trim()) {
        imageDescriptions.push(img.title.trim());
      }
      // Check for data attributes that might contain product info
      if (img.dataset.description) imageDescriptions.push(img.dataset.description);
      if (img.dataset.variant) imageDescriptions.push(img.dataset.variant);
    });
    if (imageDescriptions.length > 0) {
      product.imageDescriptions = [...new Set(imageDescriptions)].slice(0, 5); // Unique, max 5
    }
    
    // Extract additional attributes (from tables, lists)
    product.attributes = extractProductAttributes();
    
    return Object.keys(product).length > 0 ? product : null;
  }
  
  function extractProductAttributes() {
    const attributes = {};
    
    // Look for specification tables
    const tables = document.querySelectorAll('table');
    tables.forEach(table => {
      const rows = table.querySelectorAll('tr');
      rows.forEach(row => {
        const cells = row.querySelectorAll('td, th');
        if (cells.length >= 2) {
          const key = cells[0].textContent.trim().toLowerCase();
          const value = cells[1].textContent.trim();
          if (key && value && key.length < 50) {
            attributes[key] = value;
          }
        }
      });
    });
    
    // Look for definition lists
    const dls = document.querySelectorAll('dl');
    dls.forEach(dl => {
      const dts = dl.querySelectorAll('dt');
      const dds = dl.querySelectorAll('dd');
      dts.forEach((dt, i) => {
        if (dds[i]) {
          const key = dt.textContent.trim().toLowerCase();
          const value = dds[i].textContent.trim();
          if (key && value) {
            attributes[key] = value;
          }
        }
      });
    });
    
    // Look for product meta (WooCommerce style)
    const productMeta = document.querySelector('.product_meta, .product-meta');
    if (productMeta) {
      const spans = productMeta.querySelectorAll('span');
      spans.forEach(span => {
        const text = span.textContent.trim();
        if (text.includes(':')) {
          const [key, value] = text.split(':').map(s => s.trim());
          if (key && value) {
            attributes[key.toLowerCase()] = value;
          }
        }
      });
    }
    
    return Object.keys(attributes).length > 0 ? attributes : null;
  }
  
  function extractPageText() {
    console.log('[Demo Chat] 🔍 SIMPLE extraction - getting ALL visible text for LLM...');
    
    // ==================== SUPER SIMPLE APPROACH ====================
    // Just get ALL visible text from the page. Let the LLM figure it out.
    // This works on ANY website without CSS selector games.
    
    // Step 1: Clone the entire document body
    const bodyClone = document.body.cloneNode(true);
    
    // Step 2: Remove elements that don't contain useful product info
    const removeSelectors = [
      'script', 'style', 'svg', 'noscript', 'iframe', 'canvas',
      'nav', 'header', 'footer',
      '[class*="cookie"]', '[class*="popup"]', '[class*="modal"]', '[class*="banner"]',
      '[class*="newsletter"]', '[class*="subscribe"]',
      '[role="navigation"]', '[role="banner"]', '[role="contentinfo"]'
    ];
    
    removeSelectors.forEach(sel => {
      try {
        bodyClone.querySelectorAll(sel).forEach(el => el.remove());
      } catch(e) {}
    });
    
    // Step 3: EXPAND all closed accordions/details BEFORE extracting text
    // This is critical - many sites hide info in accordions
    document.querySelectorAll('details').forEach((details, idx) => {
      const clonedDetails = bodyClone.querySelectorAll('details')[idx];
      if (clonedDetails) {
        clonedDetails.open = true; // Force open in clone
      }
    });
    
    // Step 4: Get ALL visible text
    let allText = bodyClone.innerText || bodyClone.textContent || '';
    
    // Step 5: Clean up whitespace but PRESERVE line breaks (they indicate structure)
    allText = allText
      .split('\n')
      .map(line => line.replace(/\s+/g, ' ').trim())
      .filter(line => line.length > 0)
      .join('\n');
    
    console.log('[Demo Chat] 📄 Raw text length:', allText.length);
    
    // Step 6: Also get structured data from JSON-LD if available (many e-commerce sites have this)
    let structuredJson = '';
    try {
      document.querySelectorAll('script[type="application/ld+json"]').forEach(script => {
        try {
          const data = JSON.parse(script.textContent);
          if (data['@type'] === 'Product' || (data['@graph'] && data['@graph'].some(i => i['@type'] === 'Product'))) {
            structuredJson += '\n=== PRODUCT STRUCTURED DATA (JSON-LD) ===\n';
            structuredJson += JSON.stringify(data, null, 2).substring(0, 3000);
          }
        } catch(e) {}
      });
    } catch(e) {}
    
    // Step 7: Build final result
    let result = '=== PAGE CONTENT (Read this to answer user questions) ===\n\n';
    result += allText.substring(0, 20000);
    
    if (structuredJson) {
      result += '\n\n' + structuredJson;
    }
    
    console.log('[Demo Chat] ✅ Extraction complete. Total:', result.length, 'chars');
    console.log('[Demo Chat] 📝 First 1000 chars:', result.substring(0, 1000));
    console.log('[Demo Chat] 📝 Last 500 chars:', result.substring(Math.max(0, result.length - 500)));
    
    // ==================== PRINT FULL TEXT FOR DEBUGGING ====================
    console.log('\n\n');
    console.log('═══════════════════════════════════════════════════════════════');
    console.log('📤 FULL PAGE TEXT BEING SENT TO LLM:');
    console.log('═══════════════════════════════════════════════════════════════');
    console.log(result);
    console.log('═══════════════════════════════════════════════════════════════');
    console.log('\n\n');
    
    return result;
  }
  
  function extractStructuredData() {
    const scripts = document.querySelectorAll('script[type="application/ld+json"]');
    const data = [];
    
    scripts.forEach(script => {
      try {
        const json = JSON.parse(script.textContent);
        // Look for Product type
        if (json['@type'] === 'Product' || 
            (Array.isArray(json['@graph']) && json['@graph'].some(item => item['@type'] === 'Product'))) {
          data.push(json);
        }
      } catch (e) {
        // Invalid JSON, skip
      }
    });
    
    return data.length > 0 ? data : null;
  }
  
  function extractOpenGraphData() {
    const og = {};
    const metas = document.querySelectorAll('meta[property^="og:"]');
    
    metas.forEach(meta => {
      const property = meta.getAttribute('property').replace('og:', '');
      og[property] = meta.content;
    });
    
    return Object.keys(og).length > 0 ? og : null;
  }
  
  // ==================== CHAT WIDGET ====================

  const LAUNCHER_HINTS_FALLBACK = [
    'Show me denims',
    'What is your latest collection?',
    'I want to go to Goa — suggest something',
    'Do you have white shirts?',
    'Track my order'
  ];

  function selectMessagesFromHintsData(data, pageCtx, cfg) {
    if (!data || typeof data !== 'object') return LAUNCHER_HINTS_FALLBACK.slice();
    try {
      const cid = (cfg.clientId || '').trim();
      if (cid && data.byClientId && Array.isArray(data.byClientId[cid]) && data.byClientId[cid].length) {
        return data.byClientId[cid];
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
      if (Array.isArray(data.default) && data.default.length) {
        return data.default;
      }
    } catch (e) {
      console.warn('[Demo Chat] launcher hints parse error', e);
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
      console.warn('[Demo Chat] launcher-hints.json load failed', e);
      return LAUNCHER_HINTS_FALLBACK.slice();
    }
  }

  function initWidget() {
    // Create widget container
    const widget = document.createElement('div');
    widget.id = 'product-chat-demo-widget';
    widget.innerHTML = `
      <button class="chat-toggle-btn" type="button" aria-label="Open chat">
        <span class="toggle-preview-panel">
          <span class="toggle-stream-row">
            <span class="toggle-stream-text" aria-live="polite"></span><span class="toggle-stream-caret" aria-hidden="true"></span>
          </span>
          <span class="toggle-avatar-wrap">
            <span class="toggle-avatar">
              <img class="toggle-avatar-img" src="" alt="" onerror="this.style.display='none';this.nextElementSibling.style.display='flex';">
              <span class="toggle-avatar-fallback">🧑‍💻</span>
            </span>
            <span class="toggle-dot"></span>
          </span>
        </span>
      </button>
      
      <div class="chat-window">
        <div class="chat-header">
          <div class="chat-header-info">
            <div class="chat-header-avatar">🛒</div>
            <div class="chat-header-text">
              <h3>Shopping Assistant</h3>
              <p><span class="status-dot"></span>Online</p>
            </div>
          </div>
          <div class="chat-header-actions">
            <button class="theme-toggle-btn" id="chat-theme-btn" aria-label="Toggle theme" title="Toggle light/dark">☀️</button>
            <button class="chat-header-btn" id="chat-maximize-btn" aria-label="Maximize chat" title="Maximize">
              <svg viewBox="0 0 24 24"><path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"/></svg>
            </button>
            <button class="chat-header-btn chat-close-btn" aria-label="Close chat" title="Close">
              <svg viewBox="0 0 24 24"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>
            </button>
          </div>
        </div>
        
        <div class="chat-messages" id="chat-messages">
          <!-- Messages will be added here -->
        </div>
        
        <div class="chat-input-area">
          <div class="chat-input-container">
            <input type="text" class="chat-input" id="chat-input" placeholder="Ask about products, prices, sizes..." autocomplete="off">
            <button class="chat-send-btn" id="chat-send-btn" aria-label="Send message">
              <svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
            </button>
          </div>
          <div class="quick-actions" id="quick-actions">
            <!-- Quick actions populated dynamically -->
          </div>
        </div>
        <div class="chat-footer">Powered by Bloomerce</div>
      </div>
    `;
    
    document.body.appendChild(widget);
    
    // Get elements
    const toggleBtn = widget.querySelector('.chat-toggle-btn');
    const closeBtn = widget.querySelector('.chat-close-btn');
    const maximizeBtn = widget.querySelector('#chat-maximize-btn');
    const chatWindow = widget.querySelector('.chat-window');
    const chatInput = widget.querySelector('#chat-input');
    const sendBtn = widget.querySelector('#chat-send-btn');
    const messagesContainer = widget.querySelector('#chat-messages');
    const quickActions = widget.querySelector('#quick-actions');
    
    // Scrape initial context
    pageContext = scrapePageContext();
    
    // Update quick actions based on context
    updateQuickActions();
    
    // Set Bloom avatar image on toggle button
    const toggleImg = widget.querySelector('.toggle-avatar-img');
    if (toggleImg && settings.backendUrl) {
      const baseUrl = settings.backendUrl.replace(/\/demo\/?$/, '');
      toggleImg.src = baseUrl + '/static/an-illustration-of-a-boy-with-a-question-mark-hand-under-his-chin-vector.jpg';
    }

    const streamTextEl = widget.querySelector('.toggle-stream-text');
    const streamCaretEl = widget.querySelector('.toggle-stream-caret');
    let launcherTypeIntervalId = null;
    let launcherPauseTimeoutId = null;
    let launcherAbort = false;
    let lastLauncherMessage = '';
    let launcherMessagesCache = null;

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

    function typewriterShow(fullText, onComplete) {
      if (!streamTextEl) {
        if (onComplete) onComplete();
        return;
      }
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
        toggleBtn.setAttribute('aria-label', 'Open chat — try: ' + fullText);
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

    widget._launcherCleanup = stopLauncherRotation;

    loadLauncherHintMessages(pageContext, settings).then((messages) => {
      launcherMessagesCache = messages;
      startLauncherRotation(messages);
    });

    // Event handlers
    toggleBtn.addEventListener('click', () => {
      stopLauncherRotation();
      chatWindow.classList.add('open');
      toggleBtn.style.display = 'none';
      if (chatHistory.length === 0) {
        sendWelcomeMessage();
      }
      chatInput.focus();
    });

    closeBtn.addEventListener('click', () => {
      chatWindow.classList.remove('open');
      chatWindow.classList.remove('maximized');
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
    
    // Maximize/minimize toggle
    let isMaximized = false;
    maximizeBtn.addEventListener('click', () => {
      isMaximized = !isMaximized;
      chatWindow.classList.toggle('maximized', isMaximized);
      
      if (isMaximized) {
        maximizeBtn.innerHTML = '<svg viewBox="0 0 24 24"><path d="M5 16h3v3h2v-5H5v2zm3-8H5v2h5V5H8v3zm6 11h2v-3h3v-2h-5v5zm2-11V5h-2v5h5V8h-3z"/></svg>';
        maximizeBtn.title = 'Minimize';
      } else {
        maximizeBtn.innerHTML = '<svg viewBox="0 0 24 24"><path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"/></svg>';
        maximizeBtn.title = 'Maximize';
      }
    });
    
    // Theme toggle (dark ↔ light)
    const themeBtn = widget.querySelector('#chat-theme-btn');
    let isLightMode = false;
    themeBtn.addEventListener('click', () => {
      isLightMode = !isLightMode;
      widget.classList.toggle('light-mode', isLightMode);
      themeBtn.textContent = isLightMode ? '🌙' : '☀️';
      themeBtn.title = isLightMode ? 'Switch to dark mode' : 'Switch to light mode';
    });
    
    sendBtn.addEventListener('click', sendMessage);
    
    chatInput.addEventListener('keypress', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    });
    
    quickActions.addEventListener('click', (e) => {
      if (e.target.classList.contains('quick-action-btn')) {
        const message = e.target.dataset.message;
        chatInput.value = message;
        sendMessage();
      }
    });
    
    function updateQuickActions() {
      const actions = [];
      
      if (pageContext.product?.name) {
        // Product page quick actions - include recommendation options
        actions.push({ text: '💰 Price?', message: "What's the price?" });
        actions.push({ text: '📏 Sizes?', message: 'What sizes are available?' });
        actions.push({ text: '🔗 Similar', message: 'Show me similar products' });
        actions.push({ text: '💳 Buy', message: 'I want to order this' });
      } else if (isShopifyStore || pageContext.domain.includes('shopify')) {
        // General Shopify store actions with recommendations
        actions.push({ text: '🔥 Deals', message: 'Show me best deals' });
        actions.push({ text: '✨ New', message: 'Show me new arrivals' });
        actions.push({ text: '👕 All', message: 'Show me all products' });
        actions.push({ text: '📋 Policies', message: 'What are your return and shipping policies?' });
      } else {
        // Generic quick actions
        actions.push({ text: '🛍️ Products', message: 'What products do you have?' });
        actions.push({ text: '🏷️ Offers', message: 'Any discounts or offers?' });
        actions.push({ text: '❓ Help', message: 'How can you help me?' });
      }
      
      quickActions.innerHTML = actions.map(a => 
        `<button class="quick-action-btn" data-message="${a.message}">${a.text}</button>`
      ).join('');
    }
    
    function cleanPrice(raw) {
      if (!raw) return '';
      const m = raw.match(/[₹$€£]?\s?[\d,]+(?:\.\d{1,2})?/);
      return m ? m[0].trim() : '';
    }
    
    function sendWelcomeMessage() {
      const storeName = pageContext.domain.replace('www.', '').split('.')[0];
      const formattedStoreName = storeName.charAt(0).toUpperCase() + storeName.slice(1);
      
      const isProductPage = /\/products?\//.test(pageContext.url);
      if (isProductPage && pageContext.product?.name) {
        addMessage('bot', `👋 Welcome to **${formattedStoreName}**!\nI see you're viewing **${pageContext.product.name}**\n\nHow can I help?`);
        setTimeout(() => {
          addSuggestionChips([
            { text: '📏 Sizes available?', message: 'What sizes are available?' },
            { text: '🔗 Similar products', message: 'Show me similar products' },
            { text: '💰 Price details', message: "What's the price?" },
            { text: '💳 Buy this', message: 'I want to buy this' },
          ]);
        }, 400);
      } else {
        addMessage('bot', `👋 Welcome to **${formattedStoreName}**! I'm your AI shopping assistant. How can I help you today?`);
        setTimeout(() => {
          addSuggestionChips([
            { text: '🛍️ Show best products', message: 'Show best products' },
            { text: '📏 Want to buy', message: 'Want to buy' },
            { text: '🏷️ Any current offers?', message: 'Any current offers?' },
          ]);
        }, 400);
      }
    }
    
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
      text = text.replace(
        /(?<![:\/\/\w"@])([a-zA-Z0-9-]+\.(com|in|co|net|org)\/products?\/[^\s<"]+)(?![^<]*<\/a>)/g,
        '<a href="https://$1" target="_blank" rel="noopener">$1</a>'
      );
      text = text.replace(/(<br>){3,}/g, '<br><br>');
      return text;
    }

    async function sendMessageStreaming(message) {
      const typingId = addTypingIndicator();
      let typingRemoved = false;
      let messageEl = null;
      let fullText = '';

      try {
        const clientLocation = await getClientLocation();
        const response = await fetch(`${settings.backendUrl}/chat/stream`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            ...(settings.apiKey && { 'X-API-Key': settings.apiKey })
          },
          body: JSON.stringify({
            message: message,
            context: pageContext,
            history: chatHistory.slice(-10),
            clientId: settings.clientId || undefined,
            recentProducts: recentProducts.length > 0 ? recentProducts : undefined,
            clientLocation
          })
        });

        if (!response.ok) throw new Error(`Server error: ${response.status}`);

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let doneData = null;

        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          const parts = buffer.split('\n\n');
          buffer = parts.pop();

          for (const part of parts) {
            const lines = part.split('\n');
            let eventType = 'token';
            let dataStr = '';
            for (const line of lines) {
              if (line.startsWith('event: ')) eventType = line.slice(7).trim();
              else if (line.startsWith('data: ')) dataStr = line.slice(6);
            }
            if (!dataStr) continue;

            const payload = JSON.parse(dataStr);

            if (eventType === 'thinking') {
              // Keep the 3-dot typing indicator running during search
              continue;
            }

            if (!typingRemoved) {
              removeTypingIndicator(typingId);
              typingRemoved = true;
            }

            if (eventType === 'token') {
              if (!messageEl) {
                messageEl = document.createElement('div');
                messageEl.className = 'message bot';
                messagesContainer.appendChild(messageEl);
              }
              fullText += payload.content;
              messageEl.innerHTML = formatBotHtml(fullText);
              messagesContainer.scrollTop = messagesContainer.scrollHeight;
            } else if (eventType === 'done') {
              doneData = payload;
            } else if (eventType === 'error') {
              if (!messageEl) {
                messageEl = document.createElement('div');
                messageEl.className = 'message bot';
                messagesContainer.appendChild(messageEl);
              }
              fullText += '\n⚠️ ' + (payload.message || 'Unknown error');
              messageEl.innerHTML = formatBotHtml(fullText);
            }
          }
        }

        if (fullText) {
          chatHistory.push({ role: 'assistant', content: fullText });
        }

        if (doneData) {
          const products = doneData.products;
          if (products && products.length > 0) {
            recentProducts = products.map(p => ({
              title: p.title,
              price: p.price || null,
              url: p.url,
            }));
            addProductCards(products.map(p => ({
              title: p.title,
              price: p.price || null,
              image: p.image || p.image_url || null,
              url: p.url,
            })));
          }
          checkForOrderIntent(message, fullText);
        }

        return true;
      } catch (error) {
        removeTypingIndicator(typingId);
        if (messageEl) {
          messageEl.remove();
        }
        console.warn('Streaming failed, will fall back:', error);
        return false;
      }
    }

    async function sendMessageNonStreaming(message) {
      const typingId = addTypingIndicator();
      try {
        const clientLocation = await getClientLocation();
        const response = await fetch(`${settings.backendUrl}/chat`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            ...(settings.apiKey && { 'X-API-Key': settings.apiKey })
          },
          body: JSON.stringify({
            message: message,
            context: pageContext,
            history: chatHistory.slice(-10),
            clientId: settings.clientId || undefined,
            recentProducts: recentProducts.length > 0 ? recentProducts : undefined,
            clientLocation
          })
        });

        removeTypingIndicator(typingId);

        if (!response.ok) throw new Error(`Server error: ${response.status}`);

        const data = await response.json();
        const hasStructuredProducts = data.products && data.products.length > 0;
        addMessage('bot', data.reply, { skipProductCards: hasStructuredProducts });

        if (hasStructuredProducts) {
          recentProducts = data.products.map(p => ({
            title: p.title,
            price: p.price || null,
            url: p.url,
          }));
          addProductCards(data.products.map(p => ({
            title: p.title,
            price: p.price || null,
            image: p.image || p.image_url || null,
            url: p.url,
          })));
        }

        checkForOrderIntent(message, data.reply);
      } catch (error) {
        removeTypingIndicator(typingId);
        addMessage('bot', `⚠️ Sorry, I couldn't connect to the server. Please make sure the backend is running at ${settings.backendUrl}`);
        console.error('Chat error:', error);
      }
    }

    async function sendMessage() {
      const message = chatInput.value.trim();
      if (!message) return;

      chatInput.value = '';
      addMessage('user', message);

      const streamed = await sendMessageStreaming(message);
      if (!streamed) {
        await sendMessageNonStreaming(message);
      }
    }
    
    function linkProductNamesInText(text) {
      const lines = text.split('\n');
      const output = [];

      for (let i = 0; i < lines.length; i++) {
        const trimmed = lines[i].trim();

        // Detect URL lines: extract a product URL, then check
        // that the non-URL portion is very short (emoji or "URL:" prefix)
        const urlMatch = trimmed.match(/(https?:\/\/[^\s]+\/products?\/[^\s]+)/);
        if (urlMatch) {
          const nonUrl = trimmed.replace(urlMatch[0], '').trim();
          if (nonUrl.length <= 6) {
            // This is a standalone URL line — hyperlink the nearest
            // product name above it and drop this line
            const url = urlMatch[1];
            let linked = false;

            for (let j = output.length - 1; j >= 0; j--) {
              const prev = output[j].trim();
              if (!prev) continue;
              if (/^https?:\/\//.test(prev)) continue;
              if (/^(Price|In Stock|Out of Stock|Sizes?|Available|View Product)/i.test(prev)) continue;
              // Skip lines starting with common detail emojis / bullets
              if (/^(\u{1F4B0}|\u{1F4E6}|\u{1F4CF}|\u{1F517}|\u{1F50D}|\u2022|\u{1F4DD}|\u{1F4CD}|[-•])/u.test(prev)) continue;

              // Found the product name line — wrap it in a markdown link
              const raw = output[j];
              const name = raw.replace(/^\d+\.\s*/, '').replace(/\*\*/g, '').trim();
              if (name.length <= 3 || name.length > 120) continue;

              if (/\*\*/.test(raw)) {
                output[j] = raw.replace(/\*\*([^*]+)\*\*/, `**[${name}](${url})**`);
              } else {
                const stripped = raw.replace(/^\d+\.\s*/, '');
                output[j] = raw.replace(stripped.trim(), `[${stripped.trim()}](${url})`);
              }
              linked = true;
              break;
            }
            // Drop the URL line regardless
            continue;
          }
        }

        output.push(lines[i]);
      }

      text = output.join('\n');

      // Convert markdown-style links [text](url) to HTML anchors
      text = text.replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener">$1</a>');

      return text;
    }
    
    function addMessage(type, text, options = {}) {
      const messageEl = document.createElement('div');
      messageEl.className = `message ${type}`;
      
      // Store plain text for history before formatting
      const plainText = text;
      
      // For bot messages: hyperlink product names to their URLs and
      // remove the separate 🔗 URL lines for a cleaner look
      if (type === 'bot') {
        text = linkProductNamesInText(text);
      }
      
      // Enhanced markdown-like formatting
      text = text
        .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
        .replace(/\*(.*?)\*/g, '<em>$1</em>')
        .replace(/~~(.*?)~~/g, '<s>$1</s>')
        .replace(/`(.*?)`/g, '<code>$1</code>')
        .replace(/\n/g, '<br>');
      
      // Auto-link any remaining bare URLs not already inside <a> tags
      text = text.replace(
        /(?<!href=")(https?:\/\/[^\s<"]+)(?![^<]*<\/a>)/g,
        '<a href="$1" target="_blank" rel="noopener">$1</a>'
      );
      
      // Clean up empty lines left by removed URL lines
      text = text.replace(/(<br>){3,}/g, '<br><br>');
      
      messageEl.innerHTML = text;
      messagesContainer.appendChild(messageEl);
      messagesContainer.scrollTop = messagesContainer.scrollHeight;
      
      // Add to history
      chatHistory.push({ role: type === 'user' ? 'user' : 'assistant', content: plainText });
      
      // For bot messages: extract product URLs from the reply text and
      // show image cards — but only when the caller hasn't already
      // provided structured products (skipProductCards flag).
      if (type === 'bot' && !options.skipProductCards) {
        const productUrls = extractProductUrlsFromText(plainText);
        if (productUrls.length > 0) {
          showProductImageCards(productUrls);
        }
      }
      
      return messageEl;
    }
    
    function addSuggestionChips(suggestions) {
      const container = document.createElement('div');
      container.className = 'suggestion-chips';
      suggestions.forEach(s => {
        const chip = document.createElement('button');
        chip.className = 'suggestion-chip';
        chip.textContent = s.text;
        chip.addEventListener('click', () => {
          container.remove();
          chatInput.value = s.message;
          sendMessage();
        });
        container.appendChild(chip);
      });
      messagesContainer.appendChild(container);
      messagesContainer.scrollTop = messagesContainer.scrollHeight;
    }
    
    function extractProductUrlsFromText(text) {
      const urlPattern = /(https?:\/\/[^\s]+\/products\/([a-z0-9][a-z0-9-]*[a-z0-9])(?:\?[^\s]*)?)/gi;
      const products = [];
      const seen = new Set();
      let match;
      while ((match = urlPattern.exec(text)) !== null) {
        const url = match[1].split('?')[0];
        const handle = match[2].toLowerCase();
        if (!seen.has(handle)) {
          seen.add(handle);
          products.push({ url, handle, title: handle.replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase()) });
        }
      }
      return products;
    }
    
    function showProductImageCards(products) {
      const cardsContainer = document.createElement('div');
      cardsContainer.className = 'product-cards-container';
      
      products.forEach(product => {
        const card = document.createElement('div');
        card.className = 'product-card';
        
        card.innerHTML = `
          <div class="product-card-image"><div class="no-image">⏳</div></div>
          <div class="product-card-content">
            <div class="product-card-title">${product.title}</div>
            <div class="product-card-price"></div>
            <a class="view-product-link" href="${product.url}" target="_blank" rel="noopener">View Product ↗</a>
          </div>
        `;
        cardsContainer.appendChild(card);
        
        fetchProductImage(product.url, card);
      });
      
      messagesContainer.appendChild(cardsContainer);
      messagesContainer.scrollTop = messagesContainer.scrollHeight;
      // Scroll again after layout settles
      setTimeout(() => { messagesContainer.scrollTop = messagesContainer.scrollHeight; }, 100);
      setTimeout(() => { messagesContainer.scrollTop = messagesContainer.scrollHeight; }, 500);
    }
    
    function fetchProductImage(url, cardEl) {
      chrome.runtime.sendMessage({ type: 'FETCH_OG_IMAGE', url }, (response) => {
        const imgEl = cardEl.querySelector('.product-card-image');
        if (!imgEl) return;

        if (response && response.success && response.data && response.data.image) {
          const { image, price, title } = response.data;

          imgEl.innerHTML = `<img src="${image}" alt="${title || 'Product'}" onerror="this.onerror=null;this.parentElement.innerHTML='<div class=no-image>🛍️</div>'">`;

          // Only update title/price if we got a real image (proves the page had proper product data)
          if (title) {
            const titleEl = cardEl.querySelector('.product-card-title');
            if (titleEl) titleEl.textContent = title;
          }

          const priceEl = cardEl.querySelector('.product-card-price');
          if (priceEl && price) {
            const cleaned = price.replace(/[^0-9.,]/g, '');
            const num = parseFloat(cleaned.replace(/,/g, ''));
            if (num > 0 && num < 100000) {
              priceEl.innerHTML = `<span class="current-price">₹${cleaned}</span>`;
            }
          }
        } else {
          const container = cardEl.parentElement;
          cardEl.remove();
          if (container && container.classList.contains('product-cards-container') && container.children.length === 0) {
            container.remove();
          }
        }

        messagesContainer.scrollTop = messagesContainer.scrollHeight;
      });
    }
    
    function addProductCards(products) {
      // Limit to max 10 products
      const limitedProducts = products.slice(0, 10);
      
      if (limitedProducts.length === 0) {
        return;
      }
      
      const cardsContainer = document.createElement('div');
      cardsContainer.className = 'product-cards-container';
      
      limitedProducts.forEach(product => {
        const card = document.createElement('div');
        card.className = 'product-card';
        
        let cardHTML = '';
        
        // Image with placeholder fallback
        cardHTML += `<div class="product-card-image">`;
        if (product.image) {
          cardHTML += `<img src="${product.image}" alt="${product.title || 'Product'}" onerror="this.onerror=null;this.src='';this.parentElement.innerHTML='<div class=no-image>🛍️</div>'">`;
        } else {
          cardHTML += `<div class="no-image">🛍️</div>`;
        }
        cardHTML += `</div>`;
        
        // Content
        cardHTML += `<div class="product-card-content">`;
        cardHTML += `<div class="product-card-title">${product.title || 'Product'}</div>`;
        cardHTML += `<div class="product-card-price">`;
        cardHTML += `<span class="current-price">${product.price || 'N/A'}</span>`;
        if (product.discount) {
          cardHTML += `<span class="discount-badge">${product.discount}</span>`;
        }
        cardHTML += `</div>`;
        
        // Only show stock badge if product is OUT OF STOCK
        // Don't clutter UI with "in stock" badges - assume available unless marked otherwise
        if (product.available === false) {
          cardHTML += `<span class="availability out-stock">✕ Out of Stock</span>`;
        }
        cardHTML += `</div>`;
        
        card.innerHTML = cardHTML;
        
        // Make whole card clickable if URL exists
        if (product.url) {
          card.style.cursor = 'pointer';
          card.onclick = () => window.open(product.url, '_blank');
        }
        
        cardsContainer.appendChild(card);
      });
      
      messagesContainer.appendChild(cardsContainer);
      messagesContainer.scrollTop = messagesContainer.scrollHeight;
    }
    
    // Order form for collecting customer details
    function showOrderForm(productName, productPrice) {
      const formContainer = document.createElement('div');
      formContainer.className = 'order-form-container';
      formContainer.innerHTML = `
        <div class="order-form">
          <div class="order-form-header">
            <span>📝</span> Complete Your Order
          </div>
          <div class="order-form-product">
            <strong>${productName}</strong>
            ${productPrice ? `<span class="order-price">${productPrice}</span>` : ''}
          </div>
          <div class="order-form-fields">
            <input type="text" id="order-name" placeholder="Full Name *" required>
            <input type="tel" id="order-phone" placeholder="Phone (10 digits) *" maxlength="10" pattern="[0-9]{10}" required>
            <textarea id="order-address" placeholder="Delivery Address *" rows="2" required></textarea>
            <div class="order-form-row">
              <input type="text" id="order-city" placeholder="City">
              <input type="text" id="order-pincode" placeholder="Pincode" maxlength="6">
            </div>
          </div>
          <div class="order-form-actions">
            <button class="order-submit-btn" id="order-submit-btn">
              🛒 Place Order
            </button>
            <button class="order-cancel-btn" id="order-cancel-btn">
              Cancel
            </button>
          </div>
        </div>
      `;
      
      messagesContainer.appendChild(formContainer);
      messagesContainer.scrollTop = messagesContainer.scrollHeight;
      
      // Store product info for submission
      formContainer.dataset.productName = productName;
      formContainer.dataset.productPrice = productPrice || '';
      
      // Handle form submission
      const submitBtn = formContainer.querySelector('#order-submit-btn');
      const cancelBtn = formContainer.querySelector('#order-cancel-btn');
      
      submitBtn.addEventListener('click', async () => {
        const name = formContainer.querySelector('#order-name').value.trim();
        const phone = formContainer.querySelector('#order-phone').value.trim();
        const address = formContainer.querySelector('#order-address').value.trim();
        const city = formContainer.querySelector('#order-city').value.trim();
        const pincode = formContainer.querySelector('#order-pincode').value.trim();
        
        // Validation
        if (!name || !phone || !address) {
          addMessage('bot', '⚠️ Please fill in all required fields (Name, Phone, Address)');
          return;
        }
        
        if (phone.length !== 10 || !/^\d+$/.test(phone)) {
          addMessage('bot', '⚠️ Please enter a valid 10-digit phone number');
          return;
        }
        
        // Submit order
        submitBtn.disabled = true;
        submitBtn.textContent = '⏳ Processing...';
        
        try {
          const params = new URLSearchParams({
            product_name: productName,
            product_price: productPrice || '',
            customer_name: name,
            customer_phone: phone,
            customer_address: address,
            customer_city: city,
            customer_pincode: pincode
          });
          
          const response = await fetch(`${settings.backendUrl}/order/submit?${params}`, {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              ...(settings.apiKey && { 'X-API-Key': settings.apiKey })
            }
          });
          
          const data = await response.json();
          
          // Remove form
          formContainer.remove();
          
          if (data.success) {
            const order = data.order;
            addMessage('bot', `🎉 **Order Placed Successfully!**

**Order ID:** \`${order.order_id}\`
**Product:** ${order.product.name}
**Amount:** ${order.product.price || 'As per website'}

📦 **Delivery To:**
${order.customer.name}
${order.customer.address}${order.customer.city ? ', ' + order.customer.city : ''}${order.customer.pincode ? ' - ' + order.customer.pincode : ''}
📞 ${order.customer.phone}

**Expected Delivery:** ${order.estimated_delivery}

💡 Type "track order ${order.order_id}" to check status anytime!

⚠️ *This is a demo order.*`);
          } else {
            addMessage('bot', `❌ Failed to place order: ${data.detail || 'Unknown error'}`);
          }
        } catch (error) {
          formContainer.remove();
          addMessage('bot', `❌ Error placing order. Please try again.`);
        }
      });
      
      cancelBtn.addEventListener('click', () => {
        formContainer.remove();
        addMessage('bot', 'Order cancelled. Let me know if you need anything else! 😊');
      });
    }
    
    // Show order form ONLY when on a product page and reply asks for details
    function checkForOrderIntent(message, reply) {
      // Must be on a product page with an identified product
      if (!pageContext.product?.name || !pageContext.product?.price) {
        return false;
      }
      
      const lowerMessage = message.toLowerCase();
      
      // Skip status queries
      const statusKeywords = ['status', 'track', 'tracking', 'where is', 'my order', 'check order'];
      if (statusKeywords.some(kw => lowerMessage.includes(kw))) {
        return false;
      }
      
      // Skip browsing queries — user wants to see products, not purchase yet
      const browsingKeywords = ['show me', 'find me', 'search', 'any ', 'some ', 'what ', 'which ', 'recommend', 'suggest', 'similar'];
      if (browsingKeywords.some(kw => lowerMessage.includes(kw))) {
        return false;
      }
      
      // Only trigger when the reply explicitly asks for customer details
      if (!(reply.includes('provide your details') || reply.includes('Required Information'))) {
        return false;
      }
      
      const orderKeywords = ['buy', 'purchase', 'want this', 'get this', 'checkout', 'place order', 'order now', 'order this'];
      if (orderKeywords.some(kw => lowerMessage.includes(kw)) ||
          (lowerMessage.includes('order') && !lowerMessage.includes('order status'))) {
        const productName = pageContext.product.name;
        const productPrice = pageContext.product.price;
        setTimeout(() => showOrderForm(productName, productPrice), 500);
        return true;
      }
      
      return false;
    }
    
    
    function addTypingIndicator() {
      const id = 'typing-' + Date.now();
      const typingEl = document.createElement('div');
      typingEl.className = 'message bot typing';
      typingEl.id = id;
      typingEl.innerHTML = '<span></span><span></span><span></span>';
      messagesContainer.appendChild(typingEl);
      messagesContainer.scrollTop = messagesContainer.scrollHeight;
      return id;
    }
    
    function removeTypingIndicator(id) {
      const el = document.getElementById(id);
      if (el) el.remove();
    }
    
    // Re-scrape context on URL changes (SPA support)
    let lastUrl = window.location.href;
    const observer = new MutationObserver(() => {
      if (window.location.href !== lastUrl) {
        lastUrl = window.location.href;
        pageContext = scrapePageContext();
        updateQuickActions();
        
        // Clear carousel tracking when page changes
        lastCarouselQuery = '';
        
        // Silently update — no notification needed
        if (chatWindow.classList.contains('open')) {
          updateQuickActions();
        }
      }
    });
    
    observer.observe(document.body, { childList: true, subtree: true });
  }
})();
