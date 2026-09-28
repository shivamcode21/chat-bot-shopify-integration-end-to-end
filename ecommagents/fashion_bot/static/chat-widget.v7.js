/**
 * Fashion Bot Widget Bundle v7
 * 
 * Features:
 * ✅ Cart & Checkout Integration - Instantly show cart in chat on Add to Cart
 * ✅ Shopify Event Hooking
 * ✅ All v6 features (Attribution, Isolated UI, IndexedDB, etc.)
 */
(function() {
  'use strict';

  // ============================================================
  // CONSTANTS
  // ============================================================
  var WIDGET_VERSION = 'v7';
  var DB_NAME = 'FashionBotWidgetDB';
  var DB_VERSION = 1;
  var STORE_NAME = 'messages';
  var MESSAGE_TTL = 24 * 60 * 60 * 1000; // 24 hours
  var MAX_STORED_MESSAGES = 100;
  var RECONNECT_DELAY = 3000;
  var MAX_RECONNECT_ATTEMPTS = 5;
  
  // Attribution Constants
  var ATTRIBUTION_TTL = 72 * 60 * 60 * 1000; // 72 hours (3 days) - for bot_ref
  var ANON_ID_TTL = 90 * 24 * 60 * 60 * 1000; // 90 days - for anon_id (assisted attribution window)
  var ATTRIBUTION_STORAGE_KEY = 'fbw_attribution';
  var ANON_ID_STORAGE_KEY = 'fbw_anon_id';
  var EVENT_BATCH_SIZE = 10;
  var EVENT_FLUSH_INTERVAL = 5000; // 5 seconds

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
    this.config = { clientName: null, position: 'bottom-right', theme: 'light', platform: 'auto' };
    this.state = { initialized: false, buttonInjected: false, iframeLoaded: false, chatOpen: false, sessionId: null };
    this.elements = { container: null, button: null, wrapper: null, iframe: null };
    this.baseUrl = '';
    this.reconnectAttempts = 0;
    this.urlObserver = null;
    this.lastUrl = '';
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

    getOrCreateSessionId: function() {
      var key = 'fbw_session_' + this.config.clientName.replace(/\s+/g, '_');
      var sid = sessionStorage.getItem(key);
      if (!sid) { sid = generateId(); sessionStorage.setItem(key, sid); }
      return sid;
    },

    // 🛒 CART LISTENER (v7)
    setupCartListener: function() {
      var self = this;
      if (detectPlatform() !== 'shopify') return;
      log('info', 'Setting up cart listener');
      document.addEventListener('click', function(e) {
        if (e.target.matches('[name="add"], .add-to-cart, #add-to-cart, .product-form__submit')) {
          setTimeout(function() { self.fetchCartAndNotify(); }, 500);
        }
      }, true);
      document.addEventListener('cart:updated', function() { self.fetchCartAndNotify(); });
      document.addEventListener('cart:added', function() { self.fetchCartAndNotify(); });
      var origFetch = window.fetch;
      window.fetch = function() {
        var arg = arguments[0];
        var url = typeof arg === 'string' ? arg : (arg.url || '');
        var res = origFetch.apply(this, arguments);
        if (url.indexOf('/cart/add.js') !== -1 || url.indexOf('/cart/update.js') !== -1) {
          res.then(function() { self.fetchCartAndNotify(); });
        }
        return res;
      };
    },

    fetchCartAndNotify: function() {
      var self = this;
      fetch('/cart.js').then(function(res) { return res.json(); }).then(function(cart) {
        log('info', 'Cart updated');
        self.postMessage({ type: 'CART_UPDATED', payload: cart });
        if (!self.state.chatOpen && cart.item_count > 0 && self.elements.button) {
          self.elements.button.classList.add('pulse');
        }
      }).catch(function() {});
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
        '#fbw-container{position:fixed;' + (isRight ? 'right:24px;' : 'left:24px;') + 'bottom:90px;z-index:2147483647;display:flex;align-items:center;gap:12px;' + (isRight ? 'flex-direction:row-reverse;' : 'flex-direction:row;') + '}',
        '#fbw-button{display:flex;align-items:center;gap:0;padding:0;border:none;cursor:pointer;background:transparent;outline:none;position:relative;transition:transform 0.25s ease;}',
        '#fbw-button .fbw-pill{background:linear-gradient(135deg,#6366f1 0%,#8b5cf6 50%,#a855f7 100%);color:#ffffff;font-size:15px;font-weight:600;padding:14px 24px;padding-right:36px;border-radius:28px;box-shadow:0 8px 32px rgba(99,102,241,0.4);transition:all 0.25s ease;}',
        '#fbw-button .fbw-avatar{width:56px;height:56px;border-radius:50%;background:#ffffff;border:3px solid #ffffff;box-shadow:0 4px 20px rgba(0,0,0,0.15);margin-left:-20px;display:flex;align-items:center;justify-content:center;overflow:hidden;position:relative;}',
        '#fbw-button .fbw-avatar-icon{width:100%;height:100%;display:flex;align-items:center;justify-content:center;font-size:32px;}',
        '#fbw-button .fbw-avatar-icon img{width:100%;height:100%;object-fit:cover;border-radius:50%;}',
        '#fbw-button:hover{transform:translateY(-3px);}',
        '#fbw-button .fbw-dot{position:absolute;bottom:2px;right:2px;width:14px;height:14px;background:#22c55e;border-radius:50%;border:2.5px solid #fff;animation:fbw-dot-pulse 2s infinite;z-index:2;}',
        '@keyframes fbw-dot-pulse{0%,100%{transform:scale(1);}50%{transform:scale(1.15);}}',
        '@keyframes fbw-pulse{0%{box-shadow:0 0 0 0 rgba(99,102,241,0.5);}100%{box-shadow:0 0 0 20px rgba(99,102,241,0);}}',
        '#fbw-button.pulse .fbw-pill{animation:fbw-pulse 1.5s infinite;}',
        '#fbw-wrapper{display:none;position:fixed;' + (isRight ? 'right:20px;' : 'left:20px;') + 'bottom:90px;width:380px;height:min(580px, calc(100vh - 140px));border:none;border-radius:16px;box-shadow:0 10px 40px rgba(0,0,0,0.25);overflow:hidden;background:#1a1a2e;z-index:2147483646;}',
        '#fbw-wrapper.open{display:block;animation:fbw-slide 0.3s ease-out;}',
        '@keyframes fbw-slide{from{transform:translateY(20px);opacity:0;}to{transform:translateY(0);opacity:1;}}',
        '#fbw-iframe{width:100%;height:100%;border:none;background:#1a1a2e;}',
        '#fbw-backdrop{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.3);z-index:2147483645;backdrop-filter:blur(2px);}',
        '#fbw-backdrop.visible{display:block;}',
        '#fbw-close{display:none;align-items:center;justify-content:center;cursor:pointer;}',
        '#fbw-container.chat-open #fbw-button{display:none;}',
        '@media(max-width:768px){#fbw-container{right:16px;left:auto;bottom:160px;}#fbw-wrapper{right:16px!important;left:auto!important;bottom:160px!important;width:calc(100vw - 32px)!important;max-width:400px!important;height:min(500px, calc(100vh - 230px))!important;}}',
        '@media(max-width:480px){#fbw-container{right:12px;bottom:160px;}#fbw-button .fbw-pill{display:none;}#fbw-button .fbw-avatar{width:54px;height:54px;margin-left:0;border:3px solid #6366f1;}#fbw-wrapper{position:fixed!important;left:0!important;right:0!important;bottom:0!important;width:100%!important;height:85vh!important;border-radius:20px 20px 0 0!important;}}'
      ].join('');
      var style = document.createElement('style');
      style.id = 'fbw-styles';
      style.textContent = css;
      document.head.appendChild(style);
    },

    createButtonOnly: function() {
      var self = this;
      this.elements.container = document.createElement('div');
      this.elements.container.id = 'fbw-container';
      this.elements.button = document.createElement('button');
      this.elements.button.id = 'fbw-button';
      var basePath = this.baseUrl ? this.baseUrl + '/static' : '/static';
      var avatarImg = basePath + '/an-illustration-of-a-boy-with-a-question-mark-hand-under-his-chin-vector.jpg';
      this.elements.button.innerHTML = '<span class="fbw-pill">Ask Bloom</span><span class="fbw-avatar"><span class="fbw-avatar-icon"><img src="' + avatarImg + '" onerror="this.parentElement.innerHTML=\'🤔\'"></span></span><span class="fbw-dot"></span>';
      this.elements.button.addEventListener('click', this.onButtonClick);
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
    },

    loadIframeIfNeeded: function() {
      if (this.state.iframeLoaded) return;
      var self = this;
      this.elements.iframe = document.createElement('iframe');
      this.elements.iframe.id = 'fbw-iframe';
      this.elements.iframe.setAttribute('allow', 'clipboard-write');
      var frameUrl = this.baseUrl + '/static/chat-widget-frame.html?clientName=' + encodeURIComponent(this.config.clientName) + '&sessionId=' + encodeURIComponent(this.state.sessionId) + '&v=' + WIDGET_VERSION;
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
      if (this.elements.button) this.elements.button.classList.remove('pulse');
      this.toggle();
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
      }
    },

    open: function() {
      this.loadIframeIfNeeded();
      this.elements.wrapper.classList.add('open');
      this.elements.container.classList.add('chat-open');
      this.elements.backdrop.classList.add('visible');
      this.state.chatOpen = true;
      this.sendContext();
      return this;
    },

    close: function() {
      this.elements.wrapper.classList.remove('open');
      this.elements.container.classList.remove('chat-open');
      this.elements.backdrop.classList.remove('visible');
      this.state.chatOpen = false;
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
      this.postMessage({ type: 'pageContext', context: context });
    },

    addToCart: function(variantId, quantity, data) {
      var self = this;
      // For testing purposes, we attach extra data to the fetch options 
      // so the mock in test-v7.html can be dynamic. 
      // This has no effect on real Shopify stores.
      var fetchOptions = {
        method: 'POST', 
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items: [{ id: parseInt(variantId), quantity: parseInt(quantity) || 1 }] }),
        _productTitle: data.productTitle,
        _productPrice: parseFloat(data.productPrice.replace(/[^0-9.]/g, '')) * 100,
        _productImg: data.productUrl // Simplified for mock
      };

      fetch('/cart/add.js', fetchOptions).then(function(res) { return res.json(); }).then(function(cart) {
        self.fetchCartAndNotify();
        Attribution.trackEvent('add_to_cart', { variant_id: variantId, product_title: data.productTitle || '' });
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
    attribution: {
      getBotRef: function() { return Attribution.botRef; },
      persistToCart: function() { return Attribution.persistToCart(); }
    }
  };

  if (window.FashionBotWidgetConfig) widget.init(window.FashionBotWidgetConfig);
})();
