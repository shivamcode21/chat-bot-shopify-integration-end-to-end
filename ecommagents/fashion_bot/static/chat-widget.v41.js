/**
 * Fashion Bot Widget Bundle v41
 *
 * v8 base + in-chat phone collection when server sends phone_required (guest turn limit).
 *
 * Features:
 * ✅ Premium purple theme matching demo plugin UX
 * ✅ Larger chat window (480×720), dark/light support
 * ✅ Cart & Checkout Integration
 * ✅ Shopify Event Hooking, Attribution, Isolated UI, IndexedDB
 */
(function() {
  'use strict';

  // ============================================================
  // CONSTANTS
  // ============================================================
  var WIDGET_VERSION = 'v41';
  var DB_NAME = 'FashionBotWidgetDB';
  var DB_VERSION = 1;
  var STORE_NAME = 'messages';
  var MESSAGE_TTL = 24 * 60 * 60 * 1000; // 24 hours
  var MAX_STORED_MESSAGES = 100;
  var CART_SYNC_DEBOUNCE_MS = 250;
  var LAUNCHER_CONFIG_TIMEOUT_MS = 1200;
  
  // Attribution Constants
  var ATTRIBUTION_TTL = 72 * 60 * 60 * 1000; // 72 hours (3 days) - for bot_ref
  var ANON_ID_TTL = 90 * 24 * 60 * 60 * 1000; // 90 days - for anon_id (assisted attribution window)
  var ATTRIBUTION_STORAGE_KEY = 'fbw_attribution';
  var ANON_ID_STORAGE_KEY = 'fbw_anon_id';
  var EVENT_BATCH_SIZE = 10;
  var EVENT_FLUSH_INTERVAL = 5000; // 5 seconds

  var PHONE_COLLECTION_STORAGE_KEY = 'fbw_phone_provided';
  var WIDGET_POSITION_STORAGE_PREFIX = 'fbw_position_';

  // ============================================================
  // MOBILE NUMBER MODULE (v11 add-on; pairs with chat-widget-frame v11)
  // ============================================================
  var PhoneCollection = {
    phoneProvided: false,
    phoneNumber: null,
    collectionBlocked: false,

    init: function() {
      this.loadPhoneStatus();
    },

    loadPhoneStatus: function() {
      try {
        var stored = localStorage.getItem(PHONE_COLLECTION_STORAGE_KEY);
        if (stored) {
          var data = JSON.parse(stored);
          this.phoneProvided = data.provided || false;
          this.phoneNumber = data.phone || null;
        }
      } catch (e) {
        log('warn', 'Error loading phone status: ' + e.message);
      }
    },

    storePhoneStatus: function(phoneNumber) {
      try {
        localStorage.setItem(PHONE_COLLECTION_STORAGE_KEY, JSON.stringify({
          provided: true,
          phone: phoneNumber,
          providedAt: new Date().toISOString()
        }));
        this.phoneProvided = true;
        this.phoneNumber = phoneNumber;
      } catch (e) {
        log('warn', 'Error storing phone: ' + e.message);
      }
    },

    validatePhoneNumber: function(phone) {
      var cleaned = phone.replace(/\D/g, '');
      if (cleaned.length === 10 && /^[6-9]/.test(cleaned)) {
        return cleaned;
      }
      return null;
    },

    isPhoneRequired: function() {
      return !this.phoneProvided;
    },

    blockChat: function() {
      this.collectionBlocked = true;
    },

    unblockChat: function() {
      this.collectionBlocked = false;
    }
  };

  // ============================================================
  // ATTRIBUTION MODULE
  // ============================================================
  var Attribution = {
    botRef: null,
    anonId: null,
    clientId: null,
    baseUrl: '',
    eventQueue: [],
    flushTimer: null,
    initialized: false,

    init: function(baseUrl, clientId) {
      if (this.initialized) return;
      
      this.baseUrl = baseUrl;
      this.clientId = clientId;
      this.botRef = this.getOrCreateBotRef();
      this.anonId = this.getOrCreateAnonId();
      this.initialized = true;
      
      var self = this;
      this.flushTimer = setInterval(function() {
        self.flushEvents();
      }, EVENT_FLUSH_INTERVAL);
      
      this.trackEvent('session_started');
      this.checkUrlAndPersist();
    },
    
    checkUrlAndPersist: function() {
      try {
        var urlParams = new URLSearchParams(window.location.search);
        var urlBotRef = urlParams.get('bot_ref');
        
        if (urlBotRef) {
          this.botRef = urlBotRef;
          this.storeAttribution({
            botRef: urlBotRef,
            createdAt: Date.now()
          });
          this.persistToCart();
          this.interceptCheckoutLinks();
        }
      } catch (e) {
        log('warn', 'Error checking URL for bot_ref: ' + e.message);
      }
    },
    
    interceptCheckoutLinks: function() {
      var self = this;
      document.addEventListener('click', function(e) {
        var target = e.target;
        var checkoutLink = null;
        var el = target;
        while (el && el !== document) {
          var href = el.href || el.getAttribute('href') || '';
          var action = el.action || el.getAttribute('action') || '';
          var name = (el.name || '').toLowerCase();
          var id = (el.id || '').toLowerCase();
          var className = (el.className || '').toLowerCase();
          
          if (href.indexOf('/checkout') !== -1 || 
              action.indexOf('/checkout') !== -1 ||
              name.indexOf('checkout') !== -1 ||
              id.indexOf('checkout') !== -1 ||
              className.indexOf('checkout') !== -1 ||
              className.indexOf('buy-now') !== -1 ||
              className.indexOf('buy_now') !== -1) {
            checkoutLink = el;
            break;
          }
          el = el.parentElement;
        }
        
        if (checkoutLink) {
          self.persistToCart();
          if (checkoutLink.href && checkoutLink.href.indexOf('/checkout') !== -1) {
            try {
              var url = new URL(checkoutLink.href);
              if (!url.searchParams.has('bot_ref')) {
                url.searchParams.set('bot_ref', self.botRef);
                url.searchParams.set('anon_id', self.anonId);
                checkoutLink.href = url.toString();
              }
            } catch (err) {}
          }
        }
      }, true);
      
      document.addEventListener('submit', function(e) {
        var form = e.target;
        var action = form.action || '';
        if (action.indexOf('/checkout') !== -1 || action.indexOf('/cart') !== -1) {
          self.persistToCart();
          if (!form.querySelector('input[name="attributes[bot_ref]"]')) {
            var botRefInput = document.createElement('input');
            botRefInput.type = 'hidden';
            botRefInput.name = 'attributes[bot_ref]';
            botRefInput.value = self.botRef;
            form.appendChild(botRefInput);
          }
          if (!form.querySelector('input[name="attributes[anon_id]"]')) {
            var anonIdInput = document.createElement('input');
            anonIdInput.type = 'hidden';
            anonIdInput.name = 'attributes[anon_id]';
            anonIdInput.value = self.anonId;
            form.appendChild(anonIdInput);
          }
        }
      }, true);
    },

    getOrCreateBotRef: function() {
      try {
        var stored = this.getStoredAttribution();
        var now = Date.now();
        if (stored && stored.botRef && stored.createdAt) {
          var age = now - stored.createdAt;
          if (age < ATTRIBUTION_TTL) return stored.botRef;
        }
        var botRef = 'sm_' + Math.random().toString(36).slice(2) + '_' + Date.now().toString(36);
        this.storeAttribution({ botRef: botRef, createdAt: now });
        return botRef;
      } catch (e) {
        return 'sm_' + Math.random().toString(36).slice(2) + '_' + Date.now().toString(36);
      }
    },

    getOrCreateAnonId: function() {
      try {
        var now = Date.now();
        var stored = null;
        try {
          var storedData = localStorage.getItem(ANON_ID_STORAGE_KEY);
          if (storedData) stored = JSON.parse(storedData);
        } catch (e) {
          var oldAnonId = localStorage.getItem(ANON_ID_STORAGE_KEY);
          if (oldAnonId) {
            stored = { anon_id: oldAnonId, created_at: now - (30 * 24 * 60 * 60 * 1000) };
          }
        }
        if (stored && stored.anon_id) {
          var age = now - (stored.created_at || now);
          if (age < ANON_ID_TTL) return stored.anon_id;
        }
        var anonId = 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
          var r = Math.random() * 16 | 0, v = c === 'x' ? r : (r & 0x3 | 0x8);
          return v.toString(16);
        });
        localStorage.setItem(ANON_ID_STORAGE_KEY, JSON.stringify({ anon_id: anonId, created_at: now }));
        return anonId;
      } catch (e) {
        return 'anon_' + Math.random().toString(36).slice(2) + '_' + Date.now().toString(36);
      }
    },

    getStoredAttribution: function() {
      try {
        var data = localStorage.getItem(ATTRIBUTION_STORAGE_KEY);
        return data ? JSON.parse(data) : null;
      } catch (e) { return null; }
    },

    storeAttribution: function(data) {
      try {
        var existing = this.getStoredAttribution() || {};
        var updated = Object.assign({}, existing, data);
        localStorage.setItem(ATTRIBUTION_STORAGE_KEY, JSON.stringify(updated));
      } catch (e) {}
    },

    trackEvent: function(eventType, eventData, context) {
      if (!this.initialized || !this.clientId) return;
      var event = {
        bot_ref: this.botRef,
        anon_id: this.anonId,
        client_id: this.clientId,
        event_type: eventType,
        event_data: eventData || {},
        page_url: window.location.href,
        page_type: context ? context.pageType : null,
        product_handle: context ? context.productHandle : null,
        product_title: context ? context.productTitle : null,
        product_price: context ? context.productPrice : null,
        timestamp: Date.now()
      };
      this.eventQueue.push(event);
      if (eventType === 'add_to_cart' || eventType === 'checkout_started' || eventType === 'order_completed') {
        this.flushEvents();
      }
      if (this.eventQueue.length >= EVENT_BATCH_SIZE) {
        this.flushEvents();
      }
    },

    flushEvents: function() {
      if (this.eventQueue.length === 0 || !this.baseUrl) return;
      var events = this.eventQueue.splice(0, EVENT_BATCH_SIZE);
      var self = this;
      fetch(this.baseUrl + '/api/attribution/events/batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ events: events })
      }).catch(function() {
        self.eventQueue = events.concat(self.eventQueue);
      });
    },

    decorateUrl: function(url) {
      if (!url || !this.botRef) return url;
      try {
        var urlObj = new URL(url, window.location.origin);
        urlObj.searchParams.set('bot_ref', this.botRef);
        return urlObj.toString();
      } catch (e) {
        var separator = url.indexOf('?') === -1 ? '?' : '&';
        return url + separator + 'bot_ref=' + encodeURIComponent(this.botRef);
      }
    },

    persistToCart: function() {
      if (!this.botRef && !this.anonId) return;
      fetch('/cart/update.js', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          attributes: {
            bot_ref: this.botRef,
            anon_id: this.anonId
          }
        })
      }).catch(function() {});
    },

    destroy: function() {
      if (this.flushTimer) clearInterval(this.flushTimer);
      this.flushEvents();
      this.initialized = false;
    }
  };

  // ============================================================
  // UTILITY FUNCTIONS
  // ============================================================
  function log(level, msg) {
    try {
      if (typeof console !== 'undefined' && console[level]) {
        console[level]('[FashionBot] ' + msg);
      }
    } catch (e) {}
  }

  function defer(fn) {
    if (typeof requestIdleCallback === 'function') requestIdleCallback(fn, { timeout: 2000 });
    else setTimeout(fn, 1);
  }

  function safeCall(fn, context) {
    return function() {
      try { return fn.apply(context || null, arguments); }
      catch (e) { log('error', e.message); return null; }
    };
  }

  function generateId() {
    return 'fbw_' + Date.now().toString(36) + Math.random().toString(36).substr(2, 9);
  }

  var LAUNCHER_HINTS_FALLBACK = [
    'Show me denims',
    'What is your latest collection?',
    'I want to go to Goa - suggest something',
    'Do you have white shirts?',
    'Track my order'
  ];

  function selectLauncherMessages(data, pageCtx, cfg) {
    if (!data || typeof data !== 'object') return LAUNCHER_HINTS_FALLBACK.slice();
    try {
      var clientId = (cfg.clientId || '').trim();
      if (clientId && data.byClientId && Array.isArray(data.byClientId[clientId]) && data.byClientId[clientId].length) {
        return data.byClientId[clientId];
      }
      var clientName = (cfg.clientName || '').trim();
      if (clientName && data.byClientName && Array.isArray(data.byClientName[clientName]) && data.byClientName[clientName].length) {
        return data.byClientName[clientName];
      }
      var host = (pageCtx && pageCtx.domain) ? String(pageCtx.domain).toLowerCase() : '';
      if (host && data.byDomainSubstring && typeof data.byDomainSubstring === 'object') {
        for (var sub in data.byDomainSubstring) {
          if (!data.byDomainSubstring.hasOwnProperty(sub) || !sub) continue;
          var list = data.byDomainSubstring[sub];
          if (host.indexOf(String(sub).toLowerCase()) !== -1 && Array.isArray(list) && list.length) {
            return list;
          }
        }
      }
      if (Array.isArray(data.default) && data.default.length) return data.default;
    } catch (e) {
      log('warn', 'launcher hints parse error: ' + e.message);
    }
    return LAUNCHER_HINTS_FALLBACK.slice();
  }

  function buildLauncherHintsEndpoint(baseUrl, cfg) {
    var url = (baseUrl ? baseUrl : '') + '/widget/launcher-hints';
    var params = [];
    if (cfg && cfg.clientId) params.push('client_id=' + encodeURIComponent(cfg.clientId));
    if (cfg && cfg.clientName) params.push('client_name=' + encodeURIComponent(cfg.clientName));
    if (params.length) url += '?' + params.join('&');
    return url;
  }

  function buildLauncherThemeEndpoint(baseUrl, cfg) {
    var url = (baseUrl ? baseUrl : '') + '/widget/launcher-theme';
    var params = [];
    if (cfg && cfg.clientId) params.push('client_id=' + encodeURIComponent(cfg.clientId));
    if (cfg && cfg.clientName) params.push('client_name=' + encodeURIComponent(cfg.clientName));
    if (params.length) url += '?' + params.join('&');
    return url;
  }

  function buildWidgetLogoEndpoint(baseUrl, cfg) {
    var url = (baseUrl ? baseUrl : '') + '/widget/logo';
    var params = [];
    if (cfg && cfg.clientId) params.push('client_id=' + encodeURIComponent(cfg.clientId));
    if (cfg && cfg.clientName) params.push('client_name=' + encodeURIComponent(cfg.clientName));
    if (params.length) url += '?' + params.join('&');
    return url;
  }

  // ============================================================
  // INDEXEDDB STORAGE
  // ============================================================
  var Storage = {
    db: null,
    isSupported: typeof indexedDB !== 'undefined',
    init: function(callback) {
      if (!this.isSupported || this.db) { callback && callback(this.db); return; }
      try {
        var request = indexedDB.open(DB_NAME, DB_VERSION);
        var self = this;
        request.onsuccess = function(e) { self.db = e.target.result; callback && callback(self.db); };
        request.onupgradeneeded = function(e) {
          var db = e.target.result;
          if (!db.objectStoreNames.contains(STORE_NAME)) {
            var store = db.createObjectStore(STORE_NAME, { keyPath: 'id' });
            store.createIndex('sessionId', 'sessionId', { unique: false });
            store.createIndex('timestamp', 'timestamp', { unique: false });
          }
        };
      } catch (e) { callback && callback(null); }
    },
    saveMessages: function(sessionId, messages) {
      if (!this.db) return;
      defer(safeCall(function() {
        var tx = this.db.transaction([STORE_NAME], 'readwrite');
        var store = tx.objectStore(STORE_NAME);
        var now = Date.now();
        messages.forEach(function(msg, idx) {
          store.put({ id: sessionId + '_' + idx, sessionId: sessionId, message: msg, timestamp: now });
        });
      }, this));
    },
    loadMessages: function(sessionId, callback) {
      if (!this.db) { callback([]); return; }
      try {
        var tx = this.db.transaction([STORE_NAME], 'readonly');
        var store = tx.objectStore(STORE_NAME);
        var index = store.index('sessionId');
        index.getAll(IDBKeyRange.only(sessionId)).onsuccess = function(e) {
          var results = e.target.result || [];
          var valid = results.filter(function(r) { return (Date.now() - r.timestamp) < MESSAGE_TTL; });
          valid.sort(function(a, b) { return a.timestamp - b.timestamp; });
          callback(valid.map(function(r) { return r.message; }));
        };
      } catch (e) { callback([]); }
    },
    cleanup: function() {
      if (!this.db) return;
      defer(safeCall(function() {
        var tx = this.db.transaction([STORE_NAME], 'readwrite');
        var index = tx.objectStore(STORE_NAME).index('timestamp');
        index.openCursor(IDBKeyRange.upperBound(Date.now() - MESSAGE_TTL)).onsuccess = function(e) {
          var cursor = e.target.result;
          if (cursor) { cursor.delete(); cursor.continue(); }
        };
      }, this));
    },
    clear: function(sessionId) {
      if (!this.db) return;
      try {
        var tx = this.db.transaction([STORE_NAME], 'readwrite');
        var index = tx.objectStore(STORE_NAME).index('sessionId');
        index.openCursor(IDBKeyRange.only(sessionId)).onsuccess = function(e) {
          var cursor = e.target.result;
          if (cursor) { cursor.delete(); cursor.continue(); }
        };
      } catch (e) {}
    }
  };

  // ============================================================
  // PLATFORM DETECTION
  // ============================================================
  var PlatformConfigs = {
    shopify: {
      productUrlPattern: /\/products\/([^/?#]+)/,
      collectionUrlPattern: /\/collections\/([^/?#]+)/,
      cartUrlPattern: /\/cart/,
      checkoutUrlPattern: /\/checkout/,
      titleSelectors: ['h1.product-title', 'h1.product__title', '.product-single__title', 'h1[itemprop="name"]', 'h1'],
      priceSelectors: ['.product__price', '.price__current', '[data-product-price]', '.product-price']
    },
    generic: {
      productUrlPattern: /\/(?:products?|item|p)\/([^/?#]+)/i,
      collectionUrlPattern: /\/(?:collections?|category|c|shop)\/([^/?#]+)/i,
      cartUrlPattern: /\/cart/i,
      checkoutUrlPattern: /\/checkout/i,
      titleSelectors: ['h1', '.product-title', '[itemprop="name"]'],
      priceSelectors: ['.price', '[data-price]', '.product-price']
    }
  };

  function detectPlatform() {
    if (typeof window.Shopify !== 'undefined') return 'shopify';
    if (document.querySelector('.woocommerce')) return 'woocommerce';
    return 'generic';
  }

  function resolveSelectedVariantId() {
    try {
      var params = new URLSearchParams(window.location.search || '');
      var urlVariant = params.get('variant');
      if (urlVariant && /^\d+$/.test(urlVariant)) return urlVariant;
    } catch (e) {}

    try {
      if (window.ShopifyAnalytics && window.ShopifyAnalytics.meta) {
        var analyticsMeta = window.ShopifyAnalytics.meta;
        if (analyticsMeta.selectedVariantId) return String(analyticsMeta.selectedVariantId);
        if (analyticsMeta.product && analyticsMeta.product.selectedVariantId) return String(analyticsMeta.product.selectedVariantId);
      }
    } catch (e) {}

    try {
      if (window.meta && window.meta.product) {
        if (window.meta.product.selected_variant_id) return String(window.meta.product.selected_variant_id);
        if (window.meta.product.selectedVariantId) return String(window.meta.product.selectedVariantId);
      }
    } catch (e) {}

    try {
      var selectedInput = document.querySelector('form[action*="/cart/add"] [name="id"], form[action^="/cart/add"] [name="id"], input[name="id"]');
      if (selectedInput && selectedInput.value && /^\d+$/.test(String(selectedInput.value))) {
        return String(selectedInput.value);
      }
    } catch (e) {}

    return null;
  }

  function normalizeVariantId(value) {
    var raw = String(value || '').trim();
    if (!raw) return null;
    if (/^\d+$/.test(raw)) return raw;
    var gidMatch = raw.match(/ProductVariant\/(\d+)/i);
    if (gidMatch && gidMatch[1]) return gidMatch[1];
    var digitMatch = raw.match(/(\d{6,})/);
    return digitMatch && digitMatch[1] ? digitMatch[1] : null;
  }

  function detectPageContext(platformConfig) {
    var detectedPlatform = detectPlatform();
    var context = {
      url: window.location.href, pathname: window.location.pathname,
      hostname: window.location.hostname, platform: detectedPlatform,
      pageType: 'unknown', productHandle: null, productTitle: null, productPrice: null, variant: null
    };
    try {
      var cfg = platformConfig || PlatformConfigs[detectedPlatform] || PlatformConfigs.generic;
      var match;
      if (cfg.productUrlPattern && (match = window.location.pathname.match(cfg.productUrlPattern))) {
        context.pageType = 'product'; context.productHandle = match[1];
      } else if (cfg.collectionUrlPattern && (match = window.location.pathname.match(cfg.collectionUrlPattern))) {
        context.pageType = 'collection';
      } else if (cfg.cartUrlPattern && cfg.cartUrlPattern.test(window.location.pathname)) {
        context.pageType = 'cart';
      } else if (cfg.checkoutUrlPattern && cfg.checkoutUrlPattern.test(window.location.pathname)) {
        context.pageType = 'checkout';
      } else if (window.location.pathname === '/' || window.location.pathname === '') {
        context.pageType = 'home';
      }
      if (context.pageType === 'product') {
        context.variant = normalizeVariantId(resolveSelectedVariantId());
        cfg.titleSelectors && cfg.titleSelectors.some(function(sel) {
          var el = document.querySelector(sel);
          if (el && el.textContent.trim()) { context.productTitle = el.textContent.trim(); return true; }
          return false;
        });
        cfg.priceSelectors && cfg.priceSelectors.some(function(sel) {
          var el = document.querySelector(sel);
          if (el && el.textContent.trim()) { context.productPrice = el.textContent.trim(); return true; }
          return false;
        });
      }
    } catch (e) {}
    return context;
  }

  // ============================================================
  // MAIN WIDGET CLASS
  // ============================================================
  function FashionBotWidgetCore() {
    this.config = { clientName: null, clientId: null, encodedClientId: null, position: 'bottom-right', theme: 'light', platform: 'auto' };
    this.state = { initialized: false, buttonInjected: false, iframeLoaded: false, chatOpen: false, sessionId: null, botMessageCount: 0, phoneNudgeShown: false, dragMoved: false };
    this.elements = { container: null, button: null, wrapper: null, iframe: null, launcherText: null, launcherCaret: null, launcherPreview: null, launcherAvatar: null, launcherDot: null };
    this.baseUrl = '';
    this.urlObserver = null;
    this.lastUrl = '';
    this.launcherMessagesCache = [];
    this.lastLauncherMessage = '';
    this.launcherTypeIntervalId = null;
    this.launcherPauseTimeoutId = null;
    this.launcherAbort = false;
    this.launcherBootstrapScheduled = false;
    this.cartListenerInstalled = false;
    this.cartSyncTimer = null;
    this.cartSyncInFlight = false;
    this.cartSyncQueued = false;
    this.widgetLogoUrl = '';
    this.handleMessage = this.handleMessage.bind(this);
    this.onButtonClick = this.onButtonClick.bind(this);
  }

  FashionBotWidgetCore.prototype = {
    init: function(options) {
      if (this.state.initialized) return this;
      try {
        for (var key in options) { if (options.hasOwnProperty(key)) this.config[key] = options[key]; }
        if (!this.config.encodedClientId && this.config.clientId && !this.config.clientName) {
          // New integrations can keep using `clientId` publicly while treating it
          // as an opaque encoded identifier internally.
          this.config.encodedClientId = this.config.clientId;
        }
        if (!this.config.clientName && !this.config.clientId && !this.config.encodedClientId) return this;
        this.baseUrl = this.detectBaseUrl();
        if (!this.baseUrl) return this;
        var platform = this.config.platform === 'auto' ? detectPlatform() : this.config.platform;
        this.platformConfig = PlatformConfigs[platform] || PlatformConfigs.generic;
        this.state.sessionId = this.getOrCreateSessionId();
        Attribution.init(this.baseUrl, this.config.encodedClientId || this.config.clientId || this.config.clientName);
        PhoneCollection.init();
        this.injectButton();
        this.setupCartListener(); // 🛒 New in v7
        this.state.initialized = true;
        return this;
      } catch (e) { log('error', e.message); return this; }
    },

    detectBaseUrl: function() {
      try {
        var scripts = document.getElementsByTagName('script');
        for (var i = 0; i < scripts.length; i++) {
          var src = scripts[i].src || '';
          if (src.indexOf('chat-widget') !== -1) {
            // Use current origin as base for relative URLs to avoid 'Invalid URL' error
            var url;
            try {
              url = new URL(src);
            } catch (e) {
              url = new URL(src, window.location.origin);
            }
            return url.protocol + '//' + url.host;
          }
        }
      } catch (e) {
        log('error', 'Base URL detection failed: ' + e.message);
      }
      return '';
    },

    fetchWithTimeout: function(url, options, timeoutMs) {
      var controller = typeof AbortController === 'function' ? new AbortController() : null;
      var timer = null;
      var nextOptions = options ? Object.assign({}, options) : {};
      if (controller) {
        nextOptions.signal = controller.signal;
        timer = setTimeout(function() { controller.abort(); }, timeoutMs || LAUNCHER_CONFIG_TIMEOUT_MS);
      }
      return fetch(url, nextOptions).finally(function() {
        if (timer) clearTimeout(timer);
      });
    },

    getOrCreateSessionId: function() {
      var identifier = String(this.config.clientName || this.config.clientId || this.config.encodedClientId || 'default');
      var key = 'fbw_session_' + identifier.replace(/\s+/g, '_');
      var sid = sessionStorage.getItem(key);
      if (!sid) { sid = generateId(); sessionStorage.setItem(key, sid); }
      return sid;
    },

    // 🛒 CART LISTENER (v7)
    setupCartListener: function() {
      var self = this;
      if (this.cartListenerInstalled || detectPlatform() !== 'shopify') return;
      this.cartListenerInstalled = true;
      log('info', 'Setting up cart listener');
      document.addEventListener('click', function(e) {
        var target = e.target && e.target.closest ? e.target.closest('[name="add"], .add-to-cart, #add-to-cart, .product-form__submit') : null;
        if (target) {
          self.scheduleCartSync(500);
        }
      }, true);
      document.addEventListener('cart:updated', function() { self.scheduleCartSync(CART_SYNC_DEBOUNCE_MS); });
      document.addEventListener('cart:added', function() { self.scheduleCartSync(CART_SYNC_DEBOUNCE_MS); });
      window.addEventListener('fashionbot:cart-mutated', function() {
        self.scheduleCartSync(CART_SYNC_DEBOUNCE_MS);
      });
      if (!window.__fbwCartFetchPatched) {
        window.__fbwCartFetchPatched = true;
        window.__fbwOriginalFetch = window.fetch;
        window.fetch = function() {
          var arg = arguments[0];
          var url = typeof arg === 'string' ? arg : ((arg && arg.url) || '');
          var res = window.__fbwOriginalFetch.apply(this, arguments);
          if (url && /\/cart\/(add|update|change|clear)\.js(?:[?#]|$)|\/cart\/add\.js(?:[?#]|$)|\/cart\/update\.js(?:[?#]|$)/.test(url)) {
            Promise.resolve(res).then(function() {
              try {
                window.dispatchEvent(new CustomEvent('fashionbot:cart-mutated', {
                  detail: { url: url, source: 'fetch' }
                }));
              } catch (e) {}
            }).catch(function() {});
          }
          return res;
        };
      }
    },

    scheduleCartSync: function(delayMs) {
      var self = this;
      if (this.cartSyncTimer) {
        clearTimeout(this.cartSyncTimer);
      }
      this.cartSyncTimer = setTimeout(function() {
        self.cartSyncTimer = null;
        self.fetchCartAndNotify();
      }, typeof delayMs === 'number' ? delayMs : CART_SYNC_DEBOUNCE_MS);
    },

    fetchCartAndNotify: function() {
      var self = this;
      if (this.cartSyncInFlight) {
        this.cartSyncQueued = true;
        return;
      }
      this.cartSyncInFlight = true;
      fetch('/cart.js', { credentials: 'same-origin' }).then(function(res) { return res.json(); }).then(function(cart) {
        log('info', 'Cart updated');
        self.syncStorefrontCartUi(cart);
        self.postMessage({ type: 'CART_UPDATED', payload: cart });
        if (!self.state.chatOpen && cart.item_count > 0 && self.elements.button) {
          self.elements.button.classList.add('pulse');
        }
      }).catch(function() {}).finally(function() {
        self.cartSyncInFlight = false;
        if (self.cartSyncQueued) {
          self.cartSyncQueued = false;
          self.scheduleCartSync(CART_SYNC_DEBOUNCE_MS);
        }
      });
    },

    syncStorefrontCartUi: function(cart) {
      if (!cart) return;

      var detail = { cart: cart, source: 'fashionbot-widget' };
      var eventNames = ['fashionbot:cart-updated', 'cart:updated', 'cart:added'];

      eventNames.forEach(function(name) {
        try { document.dispatchEvent(new CustomEvent(name, { detail: detail })); } catch (e) {}
        try { window.dispatchEvent(new CustomEvent(name, { detail: detail })); } catch (e) {}
      });

      try {
        if (typeof window.publish === 'function' && window.PUB_SUB_EVENTS && window.PUB_SUB_EVENTS.cartUpdate) {
          window.publish(window.PUB_SUB_EVENTS.cartUpdate, {
            source: 'fashionbot-widget',
            cartData: cart
          });
        }
      } catch (e) {}

      var count = Number(cart.item_count || 0);
      [
        '.cart-count',
        '.cart-item-count',
        '[data-cart-count]',
        '#CartCount',
        '.cart-count-bubble',
        '.cart-count-bubble span',
        '.header__icon .cart-count-bubble',
        '.header__icon .cart-count-bubble span'
      ].forEach(function(sel) {
        try {
          document.querySelectorAll(sel).forEach(function(el) {
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
    },

    injectButton: function() {
      if (this.state.buttonInjected) return;
      var self = this;
      var inject = function() {
        if (!document.body) { setTimeout(inject, 50); return; }
        self.injectStyles();
        self.createButtonOnly();
        self.state.buttonInjected = true;
      };
      if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', inject);
      else setTimeout(inject, 10);
    },

    injectStyles: function() {
      if (document.getElementById('fbw-styles')) return;
      var isRight = this.config.position.indexOf('right') !== -1;
      var css = [
        '#fbw-container{position:fixed;' + (isRight ? 'right:20px;' : 'left:20px;') + 'bottom:20px;z-index:2147483647;display:flex;align-items:flex-end;gap:0;' + (isRight ? 'flex-direction:column;' : 'flex-direction:column;') + ';touch-action:none;-webkit-user-select:none;user-select:none;will-change:left,top;}',
        '#fbw-button{display:flex;align-items:center;gap:0;padding:0;border:none;cursor:pointer;background:transparent;outline:none;position:relative;transition:transform 0.3s;touch-action:none;-webkit-user-select:none;user-select:none;}',
        '#fbw-button:hover{transform:scale(1.04);}',
        '#fbw-button .fbw-preview{width:300px;max-width:min(300px,calc(100vw - 48px));box-sizing:border-box;min-height:56px;display:flex;flex-direction:row;align-items:center;gap:12px;padding:12px 14px;border-radius:22px;background:linear-gradient(145deg,#5340c9 0%,#6C5CE7 48%,#7c6cf0 100%);box-shadow:0 6px 28px rgba(44,36,120,0.45),0 0 0 2px rgba(125,211,252,0.55),0 0 20px rgba(56,189,248,0.25);}',
        '#fbw-button .fbw-stream-row{flex:1;min-width:0;display:flex;align-items:center;gap:1px;}',
        '#fbw-button .fbw-stream-text{flex:1;min-width:0;color:rgba(255,255,255,0.96);font-size:14px;font-weight:600;line-height:1.35;text-align:left;overflow:hidden;white-space:nowrap;text-overflow:clip;}',
        '#fbw-button .fbw-stream-caret{flex-shrink:0;display:inline-block;width:2px;height:1.05em;margin-left:1px;border-radius:1px;background:rgba(255,255,255,0.9);animation:fbw-caret-blink 1s step-end infinite;vertical-align:-0.12em;}',
        '@keyframes fbw-caret-blink{0%,49%{opacity:1;}50%,100%{opacity:0;}}',
        '#fbw-button .fbw-avatar-wrap{position:relative;flex-shrink:0;width:48px;height:48px;}',
        '#fbw-button .fbw-avatar{width:48px;height:48px;border-radius:50%;background:rgba(255,255,255,0.2);box-shadow:0 2px 10px rgba(0,0,0,0.2);display:flex;align-items:center;justify-content:center;overflow:hidden;position:relative;}',
        '#fbw-button .fbw-avatar-icon{width:100%;height:100%;display:flex;align-items:center;justify-content:center;font-size:32px;}',
        '#fbw-button .fbw-avatar-icon img{width:100%;height:100%;object-fit:cover;border-radius:50%;}',
        '#fbw-button .fbw-avatar-fallback{display:none;align-items:center;justify-content:center;font-size:24px;}',
        '#fbw-button .fbw-dot{position:absolute;bottom:0;right:0;width:13px;height:13px;background:#2ecc71;border-radius:50%;border:2px solid #5a4ac9;z-index:2;}',
        '@keyframes fbw-pulse{0%{transform:scale(1);}50%{transform:scale(1.03);}100%{transform:scale(1);}}',
        '#fbw-button.pulse .fbw-preview{animation:fbw-pulse 1.5s ease-in-out infinite;will-change:transform;}',
        '#fbw-wrapper{display:none;position:fixed;' + (isRight ? 'right:20px;' : 'left:20px;') + 'bottom:20px;width:480px;height:min(720px, calc(100vh - 40px));border:none;border-radius:20px;box-shadow:0 12px 48px rgba(0,0,0,0.45);overflow:hidden;background:#1e1e2e;z-index:2147483646;border:1px solid rgba(255,255,255,0.08);}',
        '#fbw-wrapper.open{display:block;animation:fbw-slide 0.3s ease-out;}',
        '@keyframes fbw-slide{from{transform:translateY(24px) scale(0.96);opacity:0;}to{transform:translateY(0) scale(1);opacity:1;}}',
        '#fbw-iframe{width:100%;height:100%;border:none;background:#1e1e2e;}',
        '#fbw-backdrop{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:transparent;z-index:2147483645;}',
        '#fbw-backdrop.visible{display:block;}',
        '#fbw-close{display:none;align-items:center;justify-content:center;cursor:pointer;}',
        '#fbw-container.chat-open #fbw-button{display:none;}',
        '@media(max-width:768px){#fbw-container{right:16px;left:auto;bottom:96px;}#fbw-wrapper{right:0!important;left:0!important;bottom:0!important;width:100%!important;height:85vh!important;max-height:720px!important;border-radius:20px 20px 0 0!important;}}',
        '@media(max-width:480px){#fbw-container{right:12px;left:auto;bottom:112px;}#fbw-button .fbw-preview{position:relative;width:136px;max-width:136px;min-height:40px;padding:0 39px 0 14px;gap:0;border-radius:999px;box-shadow:0 6px 18px rgba(78,63,183,0.24),0 0 0 2px rgba(255,255,255,0.72);}#fbw-button .fbw-stream-row{display:flex;align-items:center;justify-content:flex-start;}#fbw-button .fbw-stream-text{font-size:12px;font-weight:700;line-height:1;white-space:nowrap;overflow:visible;}#fbw-button .fbw-stream-caret{display:none!important;}#fbw-button .fbw-avatar-wrap{position:absolute;right:-7px;top:50%;transform:translateY(-50%);width:44px;height:44px;}#fbw-button .fbw-avatar{width:44px;height:44px;border:3px solid rgba(255,255,255,0.9);background:#ffffff;box-shadow:0 6px 14px rgba(52,40,120,0.18);}#fbw-button .fbw-avatar-icon{font-size:24px;}#fbw-button .fbw-dot{width:10px;height:10px;right:1px;bottom:1px;border:2px solid #ffffff;}#fbw-wrapper{position:fixed!important;left:0!important;right:0!important;bottom:0!important;width:100%!important;height:90vh!important;border-radius:20px 20px 0 0!important;}}'
      ].join('');
      var style = document.createElement('style');
      style.id = 'fbw-styles';
      style.textContent = css;
      document.head.appendChild(style);
    },

    applyLauncherTheme: function(theme) {
      if (!theme || typeof theme !== 'object') return;
      if (this.elements.launcherPreview) {
        if (theme.background) this.elements.launcherPreview.style.background = theme.background;
        if (theme.shadow) this.elements.launcherPreview.style.boxShadow = theme.shadow;
        if (theme.ring_color) {
          this.elements.launcherPreview.style.border = '1px solid ' + theme.ring_color;
        }
      }
      if (this.elements.launcherText && theme.text_color) {
        this.elements.launcherText.style.color = theme.text_color;
      }
      if (this.elements.launcherCaret && theme.caret_color) {
        this.elements.launcherCaret.style.background = theme.caret_color;
      }
      if (this.elements.launcherAvatar) {
        if (theme.avatar_background) this.elements.launcherAvatar.style.background = theme.avatar_background;
        if (theme.ring_color) this.elements.launcherAvatar.style.border = '2px solid ' + theme.ring_color;
      }
      if (this.elements.launcherDot && theme.dot_border_color) {
        this.elements.launcherDot.style.borderColor = theme.dot_border_color;
      }
    },

    loadLauncherTheme: function() {
      var self = this;
      var endpointUrl = buildLauncherThemeEndpoint(this.baseUrl, this.config);
      return this.fetchWithTimeout(endpointUrl, { credentials: 'same-origin' }, LAUNCHER_CONFIG_TIMEOUT_MS).then(function(res) {
        if (!res.ok) throw new Error('launcher theme endpoint unavailable');
        return res.json();
      }).then(function(payload) {
        if (payload && payload.enabled === true && payload.launcher_theme) {
          self.applyLauncherTheme(payload.launcher_theme);
          return payload.launcher_theme;
        }
        throw new Error('launcher theme config disabled');
      }).catch(function() {
        return null;
      });
    },

    applyWidgetLogo: function(logoUrl) {
      var nextLogo = typeof logoUrl === 'string' ? logoUrl.trim() : '';
      if (!nextLogo) return;
      this.widgetLogoUrl = nextLogo;
      var image = this.elements.button ? this.elements.button.querySelector('.fbw-avatar-icon img') : null;
      if (image) {
        image.style.display = '';
        image.src = nextLogo;
      }
      if (this.state.iframeLoaded) {
        this.postMessage({ type: 'widget-logo', logoUrl: nextLogo });
      }
    },

    loadWidgetLogo: function() {
      var self = this;
      var endpointUrl = buildWidgetLogoEndpoint(this.baseUrl, this.config);
      return this.fetchWithTimeout(endpointUrl, { credentials: 'same-origin' }, LAUNCHER_CONFIG_TIMEOUT_MS).then(function(res) {
        if (!res.ok) throw new Error('widget logo endpoint unavailable');
        return res.json();
      }).then(function(payload) {
        var logoUrl = payload && payload.widget_logo && payload.widget_logo.logo_url ? String(payload.widget_logo.logo_url).trim() : '';
        if (payload && payload.enabled === true && logoUrl) {
          self.applyWidgetLogo(logoUrl);
          return logoUrl;
        }
        throw new Error('widget logo config unavailable');
      }).catch(function() {
        return '';
      });
    },

    createButtonOnly: function() {
      var self = this;
      this.elements.container = document.createElement('div');
      this.elements.container.id = 'fbw-container';
      this.elements.button = document.createElement('button');
      this.elements.button.id = 'fbw-button';
      var basePath = this.baseUrl ? this.baseUrl + '/static' : '/static';
      var avatarImg = this.widgetLogoUrl || (basePath + '/groovee_icon_image.jpg');
      this.elements.button.innerHTML = '<span class="fbw-preview"><span class="fbw-stream-row"><span class="fbw-stream-text" aria-live="polite"></span><span class="fbw-stream-caret" aria-hidden="true"></span></span><span class="fbw-avatar-wrap"><span class="fbw-avatar"><span class="fbw-avatar-icon"><img src="' + avatarImg + '" onerror="this.style.display=\'none\';this.parentElement.nextElementSibling.style.display=\'flex\'"></span><span class="fbw-avatar-fallback">🤔</span></span><span class="fbw-dot"></span></span></span>';
      this.elements.button.addEventListener('click', this.onButtonClick);
      this.elements.launcherPreview = this.elements.button.querySelector('.fbw-preview');
      this.elements.launcherText = this.elements.button.querySelector('.fbw-stream-text');
      this.elements.launcherCaret = this.elements.button.querySelector('.fbw-stream-caret');
      this.elements.launcherAvatar = this.elements.button.querySelector('.fbw-avatar');
      this.elements.launcherDot = this.elements.button.querySelector('.fbw-dot');
      this.launcherMessagesCache = LAUNCHER_HINTS_FALLBACK.slice();
      this.lastLauncherMessage = '';
      if (this.elements.launcherText) {
        var initialText = this.getStaticLauncherMessage() || this.pickNextLauncherMessage(this.launcherMessagesCache) || 'Ask Bloom';
        this.elements.launcherText.textContent = initialText;
        if (this.isMobileLauncherMode() && this.elements.launcherCaret) this.elements.launcherCaret.style.display = 'none';
        this.elements.button.setAttribute('aria-label', 'Open chat - try: ' + initialText);
      }
      this.elements.wrapper = document.createElement('div');
      this.elements.wrapper.id = 'fbw-wrapper';
      this.elements.backdrop = document.createElement('div');
      this.elements.backdrop.id = 'fbw-backdrop';
      this.elements.backdrop.addEventListener('click', function() { self.close(); });
      this.elements.closeBtn = document.createElement('button');
      this.elements.closeBtn.id = 'fbw-close';
      this.elements.closeBtn.innerHTML = '✕';
      this.elements.closeBtn.addEventListener('click', function(e) { e.stopPropagation(); self.close(); });
      document.body.appendChild(this.elements.backdrop);
      this.elements.container.appendChild(this.elements.closeBtn);
      this.elements.container.appendChild(this.elements.button);
      this.elements.container.appendChild(this.elements.wrapper);
      document.body.appendChild(this.elements.container);
      this.enableDragging();
      this.restoreLauncherPosition();
      this.scheduleLauncherBootstrap();
    },

    isMobileLauncherMode: function() {
      return window.innerWidth <= 768;
    },

    getStaticLauncherMessage: function() {
      return this.isMobileLauncherMode() ? 'Ask Bloom' : '';
    },

    applyStaticLauncherMessage: function(text) {
      if (!this.elements.launcherText) return;
      this.stopLauncherRotation();
      this.elements.launcherText.textContent = text;
      if (this.elements.launcherCaret) this.elements.launcherCaret.style.display = 'none';
      if (this.elements.button) this.elements.button.setAttribute('aria-label', 'Open chat - try: ' + text);
    },

    loadLauncherHintMessages: function() {
      var self = this;
      var pageCtx = { domain: window.location.hostname || '' };
      var endpointUrl = buildLauncherHintsEndpoint(this.baseUrl, this.config);
      return this.fetchWithTimeout(endpointUrl, { credentials: 'same-origin' }, LAUNCHER_CONFIG_TIMEOUT_MS).then(function(res) {
        if (!res.ok) throw new Error('launcher hints endpoint unavailable');
        return res.json();
      }).then(function(payload) {
        if (payload && payload.enabled === true && payload.launcher_hints) {
          return selectLauncherMessages(payload.launcher_hints, pageCtx, self.config);
        }
        throw new Error('launcher hints config disabled');
      }).catch(function() {
        var staticUrl = (self.baseUrl ? self.baseUrl : '') + '/static/launcher-hints.json?v=' + encodeURIComponent(WIDGET_VERSION);
        return fetch(staticUrl).then(function(res) {
          if (!res.ok) return LAUNCHER_HINTS_FALLBACK.slice();
          return res.json().then(function(data) {
            return selectLauncherMessages(data, pageCtx, self.config);
          });
        }).catch(function() {
          return LAUNCHER_HINTS_FALLBACK.slice();
        });
      });
    },

    scheduleLauncherBootstrap: function() {
      var self = this;
      if (this.launcherBootstrapScheduled) return;
      this.launcherBootstrapScheduled = true;
      defer(function() {
        self.launcherBootstrapScheduled = false;
        self.loadLauncherTheme();
        self.loadWidgetLogo();
        if (self.isMobileLauncherMode()) {
          self.applyStaticLauncherMessage(self.getStaticLauncherMessage());
          return;
        }
        self.refreshLauncherHints();
      });
    },

    stopLauncherRotation: function() {
      this.launcherAbort = true;
      if (this.launcherTypeIntervalId) {
        clearInterval(this.launcherTypeIntervalId);
        this.launcherTypeIntervalId = null;
      }
      if (this.launcherPauseTimeoutId) {
        clearTimeout(this.launcherPauseTimeoutId);
        this.launcherPauseTimeoutId = null;
      }
    },

    pickNextLauncherMessage: function(messages) {
      if (!messages || !messages.length) return '';
      var next = messages[Math.floor(Math.random() * messages.length)];
      var guard = 0;
      while (messages.length > 1 && next === this.lastLauncherMessage && guard++ < 12) {
        next = messages[Math.floor(Math.random() * messages.length)];
      }
      this.lastLauncherMessage = next;
      return next;
    },

    typewriterShow: function(fullText, onComplete) {
      var self = this;
      if (!this.elements.launcherText) {
        if (onComplete) onComplete();
        return;
      }
      this.elements.launcherText.textContent = '';
      if (this.elements.launcherCaret) this.elements.launcherCaret.style.display = '';
      var i = 0;
      var msPerChar = Math.min(52, Math.max(26, Math.floor(900 / Math.max(fullText.length, 1))));
      this.launcherTypeIntervalId = window.setInterval(function() {
        if (self.launcherAbort) {
          clearInterval(self.launcherTypeIntervalId);
          self.launcherTypeIntervalId = null;
          return;
        }
        i += 1;
        self.elements.launcherText.textContent = fullText.slice(0, i);
        self.elements.button.setAttribute('aria-label', 'Open chat - try: ' + fullText);
        if (i >= fullText.length) {
          clearInterval(self.launcherTypeIntervalId);
          self.launcherTypeIntervalId = null;
          if (!self.launcherAbort && onComplete) onComplete();
        }
      }, msPerChar);
    },

    startLauncherRotation: function(messages) {
      var self = this;
      this.stopLauncherRotation();
      this.launcherAbort = false;
      if (!this.elements.launcherText || !messages || !messages.length) return;
      function cycle() {
        if (self.launcherAbort) return;
        var text = self.pickNextLauncherMessage(messages);
        self.typewriterShow(text, function() {
          if (self.launcherAbort) return;
          self.launcherPauseTimeoutId = window.setTimeout(function() {
            if (self.launcherAbort) return;
            cycle();
          }, 2800);
        });
      }
      cycle();
    },

    refreshLauncherHints: function() {
      var self = this;
      if (this.isMobileLauncherMode()) {
        this.applyStaticLauncherMessage(this.getStaticLauncherMessage());
        return;
      }
      this.loadLauncherHintMessages().then(function(messages) {
        self.launcherMessagesCache = messages;
        if (!self.state.chatOpen) self.startLauncherRotation(messages);
      });
    },

    loadIframeIfNeeded: function() {
      if (this.state.iframeLoaded) return;
      var self = this;
      var runtimeConfig = window.__FBW_LOADER_CONFIG__ || null;
      var faroConfig = runtimeConfig && runtimeConfig.observability && runtimeConfig.observability.faro ? runtimeConfig.observability.faro : null;
      var clarityConfig = runtimeConfig && runtimeConfig.observability && runtimeConfig.observability.clarity ? runtimeConfig.observability.clarity : null;
      var versionCaps = runtimeConfig && runtimeConfig.versionCaps ? runtimeConfig.versionCaps : null;
      this.elements.iframe = document.createElement('iframe');
      this.elements.iframe.id = 'fbw-iframe';
      this.elements.iframe.setAttribute('allow', 'clipboard-write');
      var frameUrl = this.baseUrl + '/static/chat-widget-frame.html?sessionId=' + encodeURIComponent(this.state.sessionId) + '&v=' + WIDGET_VERSION + '&theme=' + encodeURIComponent(this.config.theme || 'light') + '&position=' + encodeURIComponent(this.config.position || 'bottom-right');
      // Use encodedClientId for direct routing (bypasses DB lookup)
      if (this.config.encodedClientId) {
        frameUrl += '&encodedClientId=' + encodeURIComponent(this.config.encodedClientId);
      } else if (this.config.clientName) {
        // Fallback to clientName for backward compatibility
        frameUrl += '&clientName=' + encodeURIComponent(this.config.clientName);
      }
      if (this.config.clientId) frameUrl += '&clientId=' + encodeURIComponent(this.config.clientId);
      if (window.innerWidth <= 768) frameUrl += '&mobile=true';
      if (faroConfig && faroConfig.enabled === true && faroConfig.url) {
        frameUrl += '&faroConfig=' + encodeURIComponent(JSON.stringify(faroConfig));
      }
      if (clarityConfig && clarityConfig.enabled === true && clarityConfig.projectId) {
        frameUrl += '&clarityConfig=' + encodeURIComponent(JSON.stringify(clarityConfig));
      }
      if (versionCaps && typeof versionCaps === 'object') {
        frameUrl += '&versionCaps=' + encodeURIComponent(JSON.stringify(versionCaps));
      }
      if (this.widgetLogoUrl) {
        frameUrl += '&logoUrl=' + encodeURIComponent(this.widgetLogoUrl);
      }
      var wsProtocol = this.baseUrl.indexOf('https://') === 0 ? 'wss://' : 'ws://';
      var apiUrl = wsProtocol + this.baseUrl.replace(/^https?:\/\//, '') + '/ws/chat';
      this.elements.iframe.src = frameUrl + '&apiUrl=' + encodeURIComponent(apiUrl);
      this.elements.wrapper.appendChild(this.elements.iframe);
      window.addEventListener('message', this.handleMessage);
      this.state.iframeLoaded = true;
      Storage.init();
    },

    onButtonClick: function() {
      if (this.state.dragMoved) {
        this.state.dragMoved = false;
        return;
      }
      if (this.elements.button) this.elements.button.classList.remove('pulse');
      this.toggle();
    },

    getPositionStorageKey: function() {
      var mode = window.innerWidth <= 768 ? 'mobile' : 'desktop';
      return WIDGET_POSITION_STORAGE_PREFIX + String(this.config.clientName || this.config.clientId || this.config.encodedClientId || 'default').replace(/\s+/g, '_') + '_' + mode;
    },

    getDefaultLauncherPosition: function() {
      var launcherWidth = (this.elements.button && this.elements.button.offsetWidth) || (this.elements.launcherPreview && this.elements.launcherPreview.offsetWidth) || 300;
      var launcherHeight = (this.elements.button && this.elements.button.offsetHeight) || (this.elements.launcherPreview && this.elements.launcherPreview.offsetHeight) || 56;
      if (window.innerWidth <= 480) {
        return this.clampLauncherPosition(window.innerWidth - launcherWidth - 12, window.innerHeight - launcherHeight - 104);
      }
      if (window.innerWidth <= 768) {
        return this.clampLauncherPosition(window.innerWidth - launcherWidth - 16, window.innerHeight - launcherHeight - 96);
      }
      return this.clampLauncherPosition(window.innerWidth - launcherWidth - 20, window.innerHeight - launcherHeight - 20);
    },

    clampLauncherPosition: function(left, top) {
      var minOffset = 16;
      var launcherWidth = (this.elements.button && this.elements.button.offsetWidth) || (this.elements.launcherPreview && this.elements.launcherPreview.offsetWidth) || 300;
      var launcherHeight = (this.elements.button && this.elements.button.offsetHeight) || (this.elements.launcherPreview && this.elements.launcherPreview.offsetHeight) || 56;
      var maxLeft = Math.max(minOffset, window.innerWidth - launcherWidth - minOffset);
      var maxTop = Math.max(minOffset, window.innerHeight - launcherHeight - minOffset);
      return {
        left: Math.max(minOffset, Math.min(left, maxLeft)),
        top: Math.max(minOffset, Math.min(top, maxTop))
      };
    },

    saveLauncherPosition: function(left, top) {
      try {
        localStorage.setItem(this.getPositionStorageKey(), JSON.stringify({ left: left, top: top }));
      } catch (e) {}
    },

    loadLauncherPosition: function() {
      try {
        var raw = localStorage.getItem(this.getPositionStorageKey());
        return raw ? JSON.parse(raw) : null;
      } catch (e) {
        return null;
      }
    },

    applyLauncherPosition: function(left, top) {
      if (!this.elements.container) return;
      var next = this.clampLauncherPosition(left, top);
      this.elements.container.style.left = next.left + 'px';
      this.elements.container.style.top = next.top + 'px';
      this.elements.container.style.right = 'auto';
      this.elements.container.style.bottom = 'auto';
    },

    applyWrapperPosition: function(left, top) {
      if (!this.elements.wrapper || window.innerWidth <= 768) return;
      var minOffset = 16;
      var width = 480;
      var height = Math.min(720, window.innerHeight - 40);
      var launcherWidth = (this.elements.launcherPreview && this.elements.launcherPreview.offsetWidth) || 300;
      var launcherHeight = (this.elements.launcherPreview && this.elements.launcherPreview.offsetHeight) || 56;
      var wrapperLeft = Math.max(minOffset, Math.min(left + Math.max(launcherWidth - width, 0), window.innerWidth - width - minOffset));
      var wrapperTop = Math.max(minOffset, Math.min(top + launcherHeight - height, window.innerHeight - height - minOffset));
      this.elements.wrapper.style.left = wrapperLeft + 'px';
      this.elements.wrapper.style.top = wrapperTop + 'px';
      this.elements.wrapper.style.right = 'auto';
      this.elements.wrapper.style.bottom = 'auto';
    },

    resetWrapperPosition: function() {
      if (!this.elements.wrapper || window.innerWidth <= 768) return;
      this.elements.wrapper.style.left = '';
      this.elements.wrapper.style.top = '';
      this.elements.wrapper.style.right = '20px';
      this.elements.wrapper.style.bottom = '20px';
    },

    restoreLauncherPosition: function() {
      var stored = this.loadLauncherPosition();
      if (stored && typeof stored.left === 'number' && typeof stored.top === 'number') {
        this.applyLauncherPosition(stored.left, stored.top);
        return;
      }
      var defaults = this.getDefaultLauncherPosition();
      this.applyLauncherPosition(defaults.left, defaults.top);
    },

    enableDragging: function() {
      var self = this;
      var dragTarget = this.elements.container || this.elements.button;
      if (!dragTarget) return;

      var dragging = false;
      var moved = false;
      var startX = 0;
      var startY = 0;
      var originLeft = 0;
      var originTop = 0;
      var pendingLeft = 0;
      var pendingTop = 0;
      var dragFrame = null;

      function flushDragPosition() {
        dragFrame = null;
        self.applyLauncherPosition(pendingLeft, pendingTop);
      }

      function beginDrag(clientX, clientY, pointerId) {
        if (self.state.chatOpen) return false;
        dragging = true;
        moved = false;
        startX = clientX;
        startY = clientY;
        var rect = self.elements.container.getBoundingClientRect();
        originLeft = rect.left;
        originTop = rect.top;
        if (pointerId != null && self.elements.button.setPointerCapture) {
          self.elements.button.setPointerCapture(pointerId);
        }
        return true;
      }

      function moveDrag(clientX, clientY) {
        if (!dragging) return;
        var dx = clientX - startX;
        var dy = clientY - startY;
        if (Math.abs(dx) > 3 || Math.abs(dy) > 3) moved = true;
        pendingLeft = originLeft + dx;
        pendingTop = originTop + dy;
        if (!dragFrame) {
          dragFrame = window.requestAnimationFrame(flushDragPosition);
        }
      }

      dragTarget.addEventListener('pointerdown', function(e) {
        beginDrag(e.clientX, e.clientY, e.pointerId);
      });

      window.addEventListener('pointermove', function(e) {
        moveDrag(e.clientX, e.clientY);
      });

      dragTarget.addEventListener('touchstart', function(e) {
        if (!e.touches || !e.touches.length) return;
        beginDrag(e.touches[0].clientX, e.touches[0].clientY, null);
      }, { passive: true });

      dragTarget.addEventListener('mousedown', function(e) {
        beginDrag(e.clientX, e.clientY, null);
      });

      document.addEventListener('touchmove', function(e) {
        if (!dragging || !e.touches || !e.touches.length) return;
        moveDrag(e.touches[0].clientX, e.touches[0].clientY);
        e.preventDefault();
      }, { passive: false });

      window.addEventListener('mousemove', function(e) {
        moveDrag(e.clientX, e.clientY);
      });

      function endDrag() {
        if (!dragging) return;
        dragging = false;
        if (dragFrame) {
          window.cancelAnimationFrame(dragFrame);
          dragFrame = null;
        }
        if (moved) {
          self.applyLauncherPosition(pendingLeft, pendingTop);
        }
        if (!moved) return;
        var rect = self.elements.container.getBoundingClientRect();
        self.saveLauncherPosition(rect.left, rect.top);
        self.state.dragMoved = true;
        setTimeout(function() { self.state.dragMoved = false; }, 0);
      }

      window.addEventListener('pointerup', endDrag);
      window.addEventListener('pointercancel', endDrag);
      window.addEventListener('touchend', endDrag, { passive: true });
      window.addEventListener('touchcancel', endDrag, { passive: true });
      window.addEventListener('mouseup', endDrag);
      window.addEventListener('resize', function() {
        if (self._resizeRaf) return;
        self._resizeRaf = window.requestAnimationFrame(function() {
          self._resizeRaf = null;
          var stored = self.loadLauncherPosition();
          if (stored && typeof stored.left === 'number' && typeof stored.top === 'number') {
            self.applyLauncherPosition(stored.left, stored.top);
            return;
          }
          var defaults = self.getDefaultLauncherPosition();
          self.applyLauncherPosition(defaults.left, defaults.top);
        });
      });
    },

    handleMessage: function(event) {
      if (event.origin !== this.baseUrl) return;
      var data = event.data;
      if (!data) return;
      switch (data.type) {
        case 'fashionbot-ready': this.sendContext(); break;
        case 'fashionbot-close': this.close(); break;
        case 'fashionbot-request-context': this.sendContext(); break;
        case 'fashionbot-add-to-cart': this.addToCart(data.variantId, data.quantity, data); break;
        case 'fashionbot-add-to-cart-bulk': this.addToCartBulk(data.items || []); break;
        case 'fashionbot-remove-from-cart': this.removeFromCart(data); break;
        case 'fashionbot-update-cart-qty': this.updateCartQuantity(data); break;
        case 'fashionbot-remove-sold-out-cart': this.removeSoldOutProductsFromCart(); break;
        case 'fashionbot-get-cart': this.sendCartSnapshot(data); break;
        case 'fashionbot-navigate-checkout': 
          Attribution.trackEvent('checkout_started', {}); 
          Attribution.persistToCart();
          // Use standard Shopify checkout path on the parent window
          setTimeout(function() {
            window.location.href = '/checkout';
          }, 50);
          break;
        case 'fashionbot-get-bot-ref': this.postMessage({ type: 'bot-ref', botRef: Attribution.botRef, anonId: Attribution.anonId }); break;
        case 'fashionbot-bot-message-count': this.state.botMessageCount = data.count || 0; break;
        case 'fashionbot-block-chat': PhoneCollection.blockChat(); break;
        case 'fashionbot-unblock-chat': PhoneCollection.unblockChat(); break;
        case 'fashionbot-submit-phone': if (data.phone) { this.submitPhoneNumber(data.phone); } break;
      }
    },

    showPhoneNudge: function() {
      this.state.phoneNudgeShown = true;
      PhoneCollection.blockChat();
      this.postMessage({ type: 'show-phone-nudge', blocked: true });
      log('info', 'Phone collection nudge triggered');
    },

    submitPhoneNumber: function(phoneNumber) {
      var validated = PhoneCollection.validatePhoneNumber(phoneNumber);
      if (!validated) {
        log('warn', 'Invalid phone number: ' + phoneNumber);
        this.postMessage({ type: 'phone-validation-error', message: 'Invalid phone number. Please enter 10 digits.' });
        return false;
      }
      PhoneCollection.storePhoneStatus(validated);
      PhoneCollection.unblockChat();
      this.postMessage({ type: 'phone-submitted', phoneNumber: validated });
      log('info', 'Phone number submitted: ' + validated);
      Attribution.trackEvent('phone_collected', { phone: validated });
      return true;
    },

    open: function() {
      this.stopLauncherRotation();
      this.loadIframeIfNeeded();
      this.elements.wrapper.classList.add('open');
      this.elements.container.classList.add('chat-open');
      this.elements.backdrop.classList.add('visible');
      this.state.chatOpen = true;
      this.resetWrapperPosition();
      this.sendContext();
      return this;
    },

    close: function() {
      this.elements.wrapper.classList.remove('open');
      this.elements.container.classList.remove('chat-open');
      this.elements.backdrop.classList.remove('visible');
      this.state.chatOpen = false;
      if (this.isMobileLauncherMode()) {
        this.applyStaticLauncherMessage(this.getStaticLauncherMessage());
        return this;
      }
      if (this.launcherMessagesCache && this.launcherMessagesCache.length) this.startLauncherRotation(this.launcherMessagesCache);
      else this.refreshLauncherHints();
      return this;
    },

    toggle: function() { if (this.state.chatOpen) this.close(); else this.open(); },

    postMessage: function(message) {
      if (this.elements.iframe && this.elements.iframe.contentWindow) {
        this.elements.iframe.contentWindow.postMessage(message, this.baseUrl);
      }
    },

    sendContext: function() {
      var context = detectPageContext(this.platformConfig);
      this.postMessage({
        type: 'pageContext',
        context: context,
        phoneRequired: PhoneCollection.isPhoneRequired(),
        phoneBlocked: PhoneCollection.collectionBlocked
      });
    },

    emitCartActionRecord: function(action, items) {
      var normalizedItems = Array.isArray(items) ? items.filter(function(item) { return !!item; }) : [];
      if (!normalizedItems.length) return;
      this.postMessage({
        type: 'cart_action_record',
        action: action,
        items: normalizedItems
      });
    },

    buildCartActionRecord: function(action, data, cartItem, extra) {
      var payload = data || {};
      var item = cartItem || {};
      var meta = extra || {};
      var selectedOptions = payload.selectedOptions || meta.selectedOptions || [];
      var optionSummary = payload.optionSummary || payload.variantTitle || item.variant_title || meta.optionSummary || '';
      return {
        action: action,
        productTitle: payload.productTitle || item.product_title || item.title || '',
        productHandle: payload.handle || meta.handle || '',
        variantId: payload.variantId || item.variant_id || meta.variantId || '',
        variantTitle: payload.variantTitle || item.variant_title || meta.variantTitle || '',
        optionSummary: optionSummary,
        selectedOptions: selectedOptions,
        quantity: meta.quantity != null ? meta.quantity : (payload.quantity != null ? payload.quantity : (item.quantity || 1)),
        cartQuantity: meta.cartQuantity,
        qtyDirection: payload.qtyDirection || meta.qtyDirection || '',
        productUrl: payload.productUrl || meta.productUrl || '',
        productImage: payload.productImage || item.image || meta.productImage || ''
      };
    },

    addToCart: function(variantId, quantity, data) {
      var self = this;
      var normalizedVariantId = normalizeVariantId(variantId);
      if (!normalizedVariantId) {
        log('warn', 'Add to cart failed: invalid variant id');
        this.postMessage({
          type: 'cart_action_feedback',
          level: 'error',
          message: 'Could not determine the selected product variant.'
        });
        return;
      }
      // For testing purposes, we attach extra data to the fetch options 
      // so the mock in test-v7.html can be dynamic. 
      // This has no effect on real Shopify stores.
      var productPriceText = data && data.productPrice != null ? String(data.productPrice) : '';
      var parsedProductPrice = parseFloat(productPriceText.replace(/[^0-9.]/g, ''));
      var fetchOptions = {
        method: 'POST', 
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: parseInt(normalizedVariantId, 10), quantity: parseInt(quantity, 10) || 1 }),
        _productTitle: data.productTitle,
        _productPrice: (isNaN(parsedProductPrice) ? 0 : parsedProductPrice * 100),
        _productImg: data.productUrl // Simplified for mock
      };

      fetch('/cart/add.js', fetchOptions).then(function(res) {
        if (!res.ok) throw new Error('cart add failed');
        return res.json();
      }).then(function(addPayload) {
        return fetch('/cart.js', { credentials: 'same-origin' }).then(function(res) {
          if (!res.ok) throw new Error('cart fetch failed');
          return res.json();
        }).then(function(cart) {
          self.syncStorefrontCartUi(cart);
          self.postMessage({ type: 'CART_UPDATED', payload: cart });
          var cartItems = cart && Array.isArray(cart.items) ? cart.items : [];
          var matchedItem = null;
          for (var i = 0; i < cartItems.length; i += 1) {
            if (parseInt(cartItems[i].variant_id, 10) === parseInt(normalizedVariantId, 10)) {
              matchedItem = cartItems[i];
              break;
            }
          }
          self.emitCartActionRecord('add', [
            self.buildCartActionRecord('add', data, matchedItem || addPayload, {
              quantity: parseInt(quantity, 10) || 1,
              variantId: normalizedVariantId
            })
          ]);
          return cart;
        }).catch(function() {
          self.scheduleCartSync(0);
          return null;
        });
      }).then(function() {
        Attribution.trackEvent('add_to_cart', { variant_id: normalizedVariantId, product_title: data.productTitle || '' });
      }).catch(function(err) {
        log('warn', 'Add to cart failed: ' + err.message);
      });
    },

    addToCartBulk: function(items) {
      var self = this;
      var payloadItems = Array.isArray(items) ? items.map(function(item) {
        var normalizedVariantId = normalizeVariantId(item && item.variantId);
        return {
          id: parseInt(normalizedVariantId, 10),
          quantity: Math.max(1, parseInt(item && item.quantity, 10) || 1)
        };
      }).filter(function(item) { return !!item.id; }) : [];
      if (!payloadItems.length) {
        self.postMessage({
          type: 'cart_action_feedback',
          level: 'error',
          message: 'Could not add selected products right now.'
        });
        return;
      }
      fetch('/cart/add.js', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items: payloadItems })
      }).then(function(res) {
        if (!res.ok) throw new Error('bulk cart add failed');
        return res.json();
      }).then(function() {
        return self._fetchCart();
      }).then(function(cart) {
        self.syncStorefrontCartUi(cart);
        self.postMessage({ type: 'CART_UPDATED', payload: cart, forceRender: true });
        var cartItems = cart && Array.isArray(cart.items) ? cart.items : [];
        var records = [];
        for (var i = 0; i < items.length; i += 1) {
          var sourceItem = items[i] || {};
          var itemVariantId = parseInt(normalizeVariantId(sourceItem.variantId), 10);
          if (!itemVariantId) continue;
          var matchedItem = null;
          for (var j = 0; j < cartItems.length; j += 1) {
            if (parseInt(cartItems[j].variant_id, 10) === itemVariantId) {
              matchedItem = cartItems[j];
              break;
            }
          }
          records.push(self.buildCartActionRecord('add', sourceItem, matchedItem, {
            quantity: Math.max(1, parseInt(sourceItem.quantity, 10) || 1),
            variantId: String(itemVariantId)
          }));
        }
        self.emitCartActionRecord('add', records);
      }).catch(function(err) {
        log('warn', 'Bulk add to cart failed: ' + err.message);
        self.postMessage({
          type: 'cart_action_feedback',
          level: 'error',
          message: 'Could not add selected products right now.'
        });
      });
    },

    _normalizeCartMatchText: function(text) {
      return String(text || '')
        .toLowerCase()
        .replace(/[^a-z0-9\s]/g, ' ')
        .replace(/\b(from|cart|bag|please|remove|delete|product|item|the|my|qty|quantity)\b/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
    },

    _levenshteinDistance: function(a, b) {
      var left = String(a || '');
      var right = String(b || '');
      if (!left) return right.length;
      if (!right) return left.length;
      var prev = [];
      for (var j = 0; j <= right.length; j += 1) prev[j] = j;
      for (var i = 1; i <= left.length; i += 1) {
        var curr = [i];
        for (var k = 1; k <= right.length; k += 1) {
          var cost = left.charAt(i - 1) === right.charAt(k - 1) ? 0 : 1;
          curr[k] = Math.min(curr[k - 1] + 1, prev[k] + 1, prev[k - 1] + cost);
        }
        prev = curr;
      }
      return prev[right.length];
    },

    _cartItemMatchScore: function(item, needle) {
      var target = this._normalizeCartMatchText(item && (item.product_title || item.title || ''));
      var query = this._normalizeCartMatchText(needle);
      if (!target || !query) return 0;
      var targetFlat = target.replace(/\s+/g, '');
      var queryFlat = query.replace(/\s+/g, '');
      if (target === query) return 100;
      if (target.indexOf(query) !== -1 || query.indexOf(target) !== -1) return 90;
      if (targetFlat.indexOf(queryFlat) !== -1 || queryFlat.indexOf(targetFlat) !== -1) return 88;
      var targetTokens = target.split(' ').filter(Boolean);
      var queryTokens = query.split(' ').filter(Boolean);
      var tokenHits = 0;
      for (var i = 0; i < queryTokens.length; i += 1) {
        var q = queryTokens[i];
        var hit = false;
        for (var j = 0; j < targetTokens.length; j += 1) {
          var t = targetTokens[j];
          if (t.indexOf(q) !== -1 || q.indexOf(t) !== -1 || this._levenshteinDistance(q, t) <= 1) {
            hit = true;
            break;
          }
        }
        if (hit) tokenHits += 1;
      }
      if (!queryTokens.length) return 0;
      var ratio = tokenHits / queryTokens.length;
      if (ratio >= 1) return 82;
      if (ratio >= 0.75) return 72;
      if (queryFlat && targetFlat && this._levenshteinDistance(queryFlat, targetFlat) <= 3) return 66;
      if (this._levenshteinDistance(query, target) <= 2) return 68;
      return 0;
    },

    _fetchCart: function() {
      return fetch('/cart.js', { credentials: 'same-origin' }).then(function(res) {
        if (!res.ok) throw new Error('cart fetch failed');
        return res.json();
      });
    },

    sendCartSnapshot: function(data) {
      var self = this;
      var mode = String(data && data.mode || 'show').toLowerCase();
      this._fetchCart().then(function(cart) {
        var hasItems = !!(cart && Array.isArray(cart.items) && cart.items.length > 0);
        if (hasItems) {
          self.syncStorefrontCartUi(cart);
          self.postMessage({ type: 'CART_UPDATED', payload: cart, forceRender: true });
        }
        if (mode === 'total') {
          var total = typeof cart.total_price === 'number' ? (cart.total_price / 100) : 0;
          var formatted = new Intl.NumberFormat('en-IN', { style: 'currency', currency: cart.currency || 'INR', maximumFractionDigits: 0 }).format(total);
          self.postMessage({
            type: 'cart_action_feedback',
            level: 'info',
            message: hasItems ? ('Your cart total is ' + formatted + '.') : 'Your cart is empty right now.'
          });
        } else if (!hasItems) {
          self.postMessage({
            type: 'cart_action_feedback',
            level: 'info',
            message: 'Your cart is empty right now.'
          });
        }
      }).catch(function(err) {
        log('warn', 'Send cart snapshot failed: ' + err.message);
        self.postMessage({
          type: 'cart_action_feedback',
          level: 'error',
          message: mode === 'total' ? 'Could not fetch your cart total right now.' : 'Could not fetch your cart right now.'
        });
      });
    },

    _findCartLineForAction: function(cart, data) {
      if (!cart || !Array.isArray(cart.items)) return null;
      var variantId = parseInt(data && data.variantId, 10);
      var titleNeedle = data && (data.targetText || data.productTitle || '') || '';
      var best = null;
      var bestScore = 0;
      for (var i = 0; i < cart.items.length; i += 1) {
        var item = cart.items[i];
        if (!item) continue;
        if (variantId && parseInt(item.variant_id, 10) === variantId) {
          return { line: i + 1, item: item };
        }
        if (titleNeedle) {
          var score = this._cartItemMatchScore(item, titleNeedle);
          if (score > bestScore) {
            bestScore = score;
            best = { line: i + 1, item: item };
          }
        }
      }
      if (best && bestScore >= 60) return best;
      if (Array.isArray(cart.items) && cart.items.length > 0 && !variantId && !titleNeedle) {
        return { line: 1, item: cart.items[0] };
      }
      return null;
    },

    removeFromCart: function(data) {
      var self = this;
      var removedRecord = null;
      self._fetchCart().then(function(cart) {
        var matched = self._findCartLineForAction(cart, data || {});
        if (!matched) throw new Error('cart line not found');
        removedRecord = self.buildCartActionRecord('remove', data, matched.item, {
          quantity: parseInt(matched.item.quantity, 10) || 1,
          variantId: matched.item.variant_id
        });
        return fetch('/cart/change.js', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ line: matched.line, quantity: 0 })
        });
      }).then(function(res) {
        if (!res.ok) throw new Error('cart remove failed');
        return self._fetchCart();
      }).then(function(cart) {
        self.syncStorefrontCartUi(cart);
        self.postMessage({ type: 'CART_UPDATED', payload: cart });
        self.emitCartActionRecord('remove', removedRecord ? [removedRecord] : []);
      }).catch(function(err) {
        log('warn', 'Remove from cart failed: ' + err.message);
        self.postMessage({
          type: 'cart_action_feedback',
          level: 'error',
          message: 'I could not find that product in your cart.'
        });
      });
    },

    updateCartQuantity: function(data) {
      var self = this;
      var explicitQty = parseInt(data && data.quantity, 10);
      var qtyDirection = String((data && data.qtyDirection) || '').toLowerCase();
      var updateRecord = null;
      self._fetchCart().then(function(cart) {
        var matched = self._findCartLineForAction(cart, data || {});
        if (!matched) throw new Error('cart line not found');
        var desiredQty = Math.max(1, explicitQty || 1);
        if (qtyDirection === 'increase') {
          desiredQty = Math.max(1, (parseInt(matched.item.quantity, 10) || 0) + 1);
        } else if (qtyDirection === 'decrease') {
          desiredQty = Math.max(1, (parseInt(matched.item.quantity, 10) || 1) - 1);
        }
        updateRecord = self.buildCartActionRecord('qty', data, matched.item, {
          quantity: desiredQty,
          cartQuantity: desiredQty,
          qtyDirection: qtyDirection,
          variantId: matched.item.variant_id
        });
        return fetch('/cart/change.js', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ line: matched.line, quantity: desiredQty })
        });
      }).then(function(res) {
        if (!res.ok) throw new Error('cart update failed');
        return self._fetchCart();
      }).then(function(cart) {
        self.syncStorefrontCartUi(cart);
        self.postMessage({ type: 'CART_UPDATED', payload: cart });
        self.emitCartActionRecord('qty', updateRecord ? [updateRecord] : []);
      }).catch(function(err) {
        log('warn', 'Update cart quantity failed: ' + err.message);
        self.postMessage({
          type: 'cart_action_feedback',
          level: 'error',
          message: 'I could not find that product in your cart.'
        });
      });
    },

    _productHandleFromCartItem: function(item) {
      if (!item) return '';
      if (item.handle) return String(item.handle);
      var url = String(item.url || '');
      var m = url.match(/\/products\/([^/?#]+)/);
      return m ? m[1] : '';
    },

    removeSoldOutProductsFromCart: function() {
      var self = this;
      var removedRecords = [];
      fetch('/cart.js', { credentials: 'same-origin' }).then(function(res) {
        if (!res.ok) throw new Error('cart fetch failed');
        return res.json();
      }).then(function(cart) {
        if (!cart || !Array.isArray(cart.items) || cart.items.length === 0) return { cart: cart, soldOutLines: [] };
        var checks = cart.items.map(function(item, idx) {
          var handle = self._productHandleFromCartItem(item);
          if (!handle) return Promise.resolve(null);
          return fetch('/products/' + encodeURIComponent(handle) + '.js', { credentials: 'same-origin' }).then(function(res) {
            if (!res.ok) return null;
            return res.json();
          }).then(function(product) {
            if (!product || !Array.isArray(product.variants)) return null;
            var variantId = parseInt(item.variant_id, 10);
            var matched = null;
            for (var i = 0; i < product.variants.length; i += 1) {
              if (parseInt(product.variants[i].id, 10) === variantId) {
                matched = product.variants[i];
                break;
              }
            }
            if (!matched) return null;
            if (matched.available === false) return idx + 1;
            return null;
          }).catch(function() { return null; });
        });
        return Promise.all(checks).then(function(lines) {
          var soldOutLines = lines.filter(function(x) { return typeof x === 'number'; }).sort(function(a, b) { return b - a; });
          removedRecords = soldOutLines.map(function(lineNo) {
            var item = cart.items[lineNo - 1];
            return self.buildCartActionRecord('remove', {}, item, {
              quantity: item && item.quantity ? item.quantity : 1,
              variantId: item && item.variant_id
            });
          }).filter(Boolean);
          return { cart: cart, soldOutLines: soldOutLines };
        });
      }).then(function(result) {
        var originalCart = result.cart || null;
        var soldOutLines = result.soldOutLines || [];
        if (!soldOutLines.length) {
          self.postMessage({
            type: 'cart_action_feedback',
            level: 'info',
            message: 'No sold out products were found in your cart.'
          });
          return null;
        }
        var removedCount = soldOutLines.length;
        var chain = Promise.resolve();
        soldOutLines.forEach(function(lineNo) {
          chain = chain.then(function() {
            return fetch('/cart/change.js', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ line: lineNo, quantity: 0 })
            }).then(function(res) {
              if (!res.ok) throw new Error('cart change failed');
              return null;
            });
          });
        });
        return chain.then(function() {
          return fetch('/cart.js', { credentials: 'same-origin' }).then(function(res) {
            if (!res.ok) throw new Error('cart fetch failed');
            return res.json();
          }).then(function(cart) {
            self.syncStorefrontCartUi(cart);
            self.postMessage({ type: 'CART_UPDATED', payload: cart });
            var beforeCount = originalCart && typeof originalCart.item_count === 'number' ? originalCart.item_count : null;
            var afterCount = cart && typeof cart.item_count === 'number' ? cart.item_count : null;
            var actualRemoved = (beforeCount != null && afterCount != null) ? Math.max(0, beforeCount - afterCount) : removedCount;
            if (actualRemoved <= 0) {
              self.postMessage({
                type: 'cart_action_feedback',
                level: 'info',
                message: 'No sold out products were found in your cart.'
              });
              return;
            }
            self.postMessage({
              type: 'cart_action_feedback',
              level: 'success',
              message: actualRemoved === 1
                ? 'Removed 1 sold out product from your cart.'
                : ('Removed ' + actualRemoved + ' sold out products from your cart.')
            });
            self.emitCartActionRecord('remove', removedRecords);
          });
        });
      }).catch(function(err) {
        log('warn', 'Remove sold out from cart failed: ' + err.message);
        self.postMessage({
          type: 'cart_action_feedback',
          level: 'error',
          message: 'Could not remove sold out products right now.'
        });
      });
    },

    setupUrlObserver: function() {
      var self = this;
      this.lastUrl = window.location.href;
      var obs = new MutationObserver(function() {
        if (window.location.href !== self.lastUrl) {
          self.lastUrl = window.location.href;
          self.sendContext();
        }
      });
      var title = document.querySelector('title');
      if (title) obs.observe(title, { childList: true, subtree: true, characterData: true });
      window.addEventListener('popstate', function() {
        if (window.location.href !== self.lastUrl) {
          self.lastUrl = window.location.href;
          self.sendContext();
        }
      });
    }
  };

  var widget = new FashionBotWidgetCore();
  window.FashionBotWidget = {
    init: function(config) { return widget.init(config); },
    open: function() { return widget.open(); },
    close: function() { return widget.close(); },
    version: WIDGET_VERSION,
    submitPhone: function(phone) { return widget.submitPhoneNumber(phone); },
    attribution: {
      getBotRef: function() { return Attribution.botRef; },
      persistToCart: function() { return Attribution.persistToCart(); }
    }
  };

  if (window.FashionBotWidgetConfig) widget.init(window.FashionBotWidgetConfig);
})();
