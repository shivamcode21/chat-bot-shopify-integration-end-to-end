/**
 * Fashion Bot Widget Bundle v20
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
  var WIDGET_VERSION = 'v20';
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

  function detectPageContext(platformConfig) {
    var detectedPlatform = detectPlatform();
    var context = {
      url: window.location.href, pathname: window.location.pathname,
      hostname: window.location.hostname, platform: detectedPlatform,
      pageType: 'unknown', productHandle: null, productTitle: null, productPrice: null
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
    this.config = { clientName: null, clientId: null, position: 'bottom-right', theme: 'light', platform: 'auto' };
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
    this.handleMessage = this.handleMessage.bind(this);
    this.onButtonClick = this.onButtonClick.bind(this);
  }

  FashionBotWidgetCore.prototype = {
    init: function(options) {
      if (this.state.initialized) return this;
      try {
        for (var key in options) { if (options.hasOwnProperty(key)) this.config[key] = options[key]; }
        if (!this.config.clientName) return this;
        this.baseUrl = this.detectBaseUrl();
        if (!this.baseUrl) return this;
        var platform = this.config.platform === 'auto' ? detectPlatform() : this.config.platform;
        this.platformConfig = PlatformConfigs[platform] || PlatformConfigs.generic;
        this.state.sessionId = this.getOrCreateSessionId();
        Attribution.init(this.baseUrl, this.config.clientName);
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
      var key = 'fbw_session_' + this.config.clientName.replace(/\s+/g, '_');
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
        '@media(max-width:480px){#fbw-container{right:12px;left:auto;bottom:104px;}#fbw-button .fbw-preview{width:min(240px,calc(100vw - 96px));max-width:calc(100vw - 96px);min-height:52px;padding:10px 12px;gap:10px;border-radius:20px;}#fbw-button .fbw-stream-row{display:flex;}#fbw-button .fbw-stream-text{font-size:13px;line-height:1.3;}#fbw-button .fbw-avatar-wrap{width:44px;height:44px;}#fbw-button .fbw-avatar{width:44px;height:44px;border:2px solid rgba(255,255,255,0.25);}#fbw-wrapper{position:fixed!important;left:0!important;right:0!important;bottom:0!important;width:100%!important;height:90vh!important;border-radius:20px 20px 0 0!important;}}'
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

    createButtonOnly: function() {
      var self = this;
      this.elements.container = document.createElement('div');
      this.elements.container.id = 'fbw-container';
      this.elements.button = document.createElement('button');
      this.elements.button.id = 'fbw-button';
      var basePath = this.baseUrl ? this.baseUrl + '/static' : '/static';
      var avatarImg = basePath + '/an-illustration-of-a-boy-with-a-question-mark-hand-under-his-chin-vector.jpg';
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
        var initialText = this.pickNextLauncherMessage(this.launcherMessagesCache) || 'Ask Bloom';
        this.elements.launcherText.textContent = initialText;
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
      this.loadLauncherHintMessages().then(function(messages) {
        self.launcherMessagesCache = messages;
        if (!self.state.chatOpen) self.startLauncherRotation(messages);
      });
    },

    loadIframeIfNeeded: function() {
      if (this.state.iframeLoaded) return;
      var self = this;
      this.elements.iframe = document.createElement('iframe');
      this.elements.iframe.id = 'fbw-iframe';
      this.elements.iframe.setAttribute('allow', 'clipboard-write');
      var frameUrl = this.baseUrl + '/static/chat-widget-frame.html?clientName=' + encodeURIComponent(this.config.clientName) + '&sessionId=' + encodeURIComponent(this.state.sessionId) + '&v=' + WIDGET_VERSION + '&theme=' + encodeURIComponent(this.config.theme || 'light') + '&position=' + encodeURIComponent(this.config.position || 'bottom-right');
      if (this.config.clientId) frameUrl += '&clientId=' + encodeURIComponent(this.config.clientId);
      if (window.innerWidth <= 768) frameUrl += '&mobile=true';
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
      return WIDGET_POSITION_STORAGE_PREFIX + String(this.config.clientName || 'default').replace(/\s+/g, '_') + '_' + mode;
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

    addToCart: function(variantId, quantity, data) {
      var self = this;
      // For testing purposes, we attach extra data to the fetch options 
      // so the mock in test-v7.html can be dynamic. 
      // This has no effect on real Shopify stores.
      var productPriceText = data && data.productPrice != null ? String(data.productPrice) : '';
      var parsedProductPrice = parseFloat(productPriceText.replace(/[^0-9.]/g, ''));
      var fetchOptions = {
        method: 'POST', 
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items: [{ id: parseInt(variantId), quantity: parseInt(quantity) || 1 }] }),
        _productTitle: data.productTitle,
        _productPrice: (isNaN(parsedProductPrice) ? 0 : parsedProductPrice * 100),
        _productImg: data.productUrl // Simplified for mock
      };

      fetch('/cart/add.js', fetchOptions).then(function(res) {
        if (!res.ok) throw new Error('cart add failed');
        return res.json();
      }).then(function() {
        return fetch('/cart.js', { credentials: 'same-origin' }).then(function(res) {
          if (!res.ok) throw new Error('cart fetch failed');
          return res.json();
        }).then(function(cart) {
          self.syncStorefrontCartUi(cart);
          self.postMessage({ type: 'CART_UPDATED', payload: cart });
          return cart;
        }).catch(function() {
          self.scheduleCartSync(0);
          return null;
        });
      }).then(function() {
        Attribution.trackEvent('add_to_cart', { variant_id: variantId, product_title: data.productTitle || '' });
      }).catch(function(err) {
        log('warn', 'Add to cart failed: ' + err.message);
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
