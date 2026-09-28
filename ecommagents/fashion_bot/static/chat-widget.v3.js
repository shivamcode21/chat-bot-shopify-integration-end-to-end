/**
 * Fashion Bot Widget Bundle v1 (Production)
 * 
 * Full-featured chat widget with maximum isolation and performance.
 * 
 * Production Features:
 * ✅ Async/lazy - Button only until clicked
 * ✅ No global pollution - IIFE with single namespace
 * ✅ No long JS tasks - Uses requestIdleCallback
 * ✅ No polling - Event-driven with MutationObserver
 * ✅ WebSocket only after interaction
 * ✅ Error isolation - Never crashes host page
 * ✅ Memory cleanup - Proper disposal
 * ✅ IndexedDB + TTL - Offline message cache
 * ✅ iframe isolation - CSS/JS sandboxed
 */
(function() {
  'use strict';

  // ============================================================
  // CONSTANTS
  // ============================================================
  var WIDGET_VERSION = 'v3';
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

    /**
     * Initialize attribution tracking
     */
    init: function(baseUrl, clientId) {
      if (this.initialized) return;
      
      this.baseUrl = baseUrl;
      this.clientId = clientId;
      this.botRef = this.getOrCreateBotRef();
      this.anonId = this.getOrCreateAnonId();
      this.initialized = true;
      
      // Start event flush timer
      var self = this;
      this.flushTimer = setInterval(function() {
        self.flushEvents();
      }, EVENT_FLUSH_INTERVAL);
      
      // Track session start
      this.trackEvent('session_started');
      
      // Check if there's a bot_ref in the URL (user came from chat link)
      // If yes, persist attribution tokens to cart
      this.checkUrlAndPersist();
      
      log('info', 'Attribution initialized: bot_ref=' + this.botRef.substring(0, 12) + '...');
    },
    
    /**
     * Check URL for bot_ref param and persist to cart if present
     * This is called when widget loads on a page (e.g., product page opened from chat)
     */
    checkUrlAndPersist: function() {
      try {
        var urlParams = new URLSearchParams(window.location.search);
        var urlBotRef = urlParams.get('bot_ref');
        
        if (urlBotRef) {
          log('info', 'bot_ref found in URL: ' + urlBotRef.substring(0, 12) + '... - persisting to cart');
          
          // Use the bot_ref from URL (it's from the chat session that sent user here)
          this.botRef = urlBotRef;
          
          // Store it locally too
          this.storeAttribution({
            botRef: urlBotRef,
            createdAt: Date.now()
          });
          
          // Persist to cart - user came from chat, so we should track this
          this.persistToCart();
          
          // Also intercept checkout links to ensure attribution flows through
          this.interceptCheckoutLinks();
        }
      } catch (e) {
        log('warn', 'Error checking URL for bot_ref: ' + e.message);
      }
    },
    
    /**
     * Intercept checkout links/buttons to add attribution params
     * Handles cases where user goes directly to checkout (Buy Now, etc.)
     */
    interceptCheckoutLinks: function() {
      var self = this;
      
      // Intercept all clicks that lead to checkout
      document.addEventListener('click', function(e) {
        var target = e.target;
        
        // Check if clicked element or its parents are checkout-related
        var checkoutLink = null;
        var el = target;
        while (el && el !== document) {
          // Check for checkout links/buttons
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
        
        // If checkout link found, ensure cart has attribution before proceeding
        if (checkoutLink) {
          log('info', 'Checkout link clicked - ensuring attribution is persisted');
          
          // Persist to cart synchronously if possible
          self.persistToCart();
          
          // If it's an anchor with href, add bot_ref to URL
          if (checkoutLink.href && checkoutLink.href.indexOf('/checkout') !== -1) {
            try {
              var url = new URL(checkoutLink.href);
              if (!url.searchParams.has('bot_ref')) {
                url.searchParams.set('bot_ref', self.botRef);
                url.searchParams.set('anon_id', self.anonId);
                checkoutLink.href = url.toString();
                log('info', 'Added attribution params to checkout URL');
              }
            } catch (err) {
              log('warn', 'Could not modify checkout URL: ' + err.message);
            }
          }
        }
      }, true); // Use capture phase to intercept early
      
      // Also intercept form submissions to checkout
      document.addEventListener('submit', function(e) {
        var form = e.target;
        var action = form.action || '';
        
        if (action.indexOf('/checkout') !== -1 || action.indexOf('/cart') !== -1) {
          log('info', 'Checkout form submitted - ensuring attribution is persisted');
          
          // Persist to cart
          self.persistToCart();
          
          // Add hidden fields for attribution if not already present
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
          
          log('info', 'Added attribution fields to checkout form');
        }
      }, true);
      
      log('info', 'Checkout interception enabled');
    },

    /**
     * Generate or retrieve bot_ref (unique per chat session)
     */
    getOrCreateBotRef: function() {
      try {
        var stored = this.getStoredAttribution();
        var now = Date.now();
        
        // Check if existing bot_ref is still valid (within TTL)
        if (stored && stored.botRef && stored.createdAt) {
          var age = now - stored.createdAt;
          if (age < ATTRIBUTION_TTL) {
            return stored.botRef;
          }
        }
        
        // Generate new bot_ref
        var botRef = 'sm_' + Math.random().toString(36).slice(2) + '_' + Date.now().toString(36);
        
        // Store it
        this.storeAttribution({
          botRef: botRef,
          createdAt: now
        });
        
        return botRef;
      } catch (e) {
        return 'sm_' + Math.random().toString(36).slice(2) + '_' + Date.now().toString(36);
      }
    },

    /**
     * Generate or retrieve anon_id (persistent across sessions with TTL)
     */
    getOrCreateAnonId: function() {
      try {
        var now = Date.now();
        var stored = null;
        
        // Try to get stored anon_id with timestamp
        try {
          var storedData = localStorage.getItem(ANON_ID_STORAGE_KEY);
          if (storedData) {
            stored = JSON.parse(storedData);
          }
        } catch (e) {
          // Fallback: try old format (just the ID string)
          var oldAnonId = localStorage.getItem(ANON_ID_STORAGE_KEY);
          if (oldAnonId && typeof oldAnonId === 'string' && oldAnonId.indexOf('{') === -1) {
            // Old format - migrate to new format with timestamp
            stored = {
              anon_id: oldAnonId,
              created_at: now - (30 * 24 * 60 * 60 * 1000) // Assume 30 days old for migration
            };
          }
        }
        
        // Check if stored anon_id exists and is still valid (within TTL)
        if (stored && stored.anon_id) {
          var age = now - (stored.created_at || now);
          if (age < ANON_ID_TTL) {
            // Still valid, return it
            return stored.anon_id;
          } else {
            // Expired, remove it
            localStorage.removeItem(ANON_ID_STORAGE_KEY);
            log('info', 'anon_id expired, generating new one');
          }
        }
        
        // Generate new UUID-like identifier
        var anonId = 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
          var r = Math.random() * 16 | 0;
          var v = c === 'x' ? r : (r & 0x3 | 0x8);
          return v.toString(16);
        });
        
        // Store with timestamp
        localStorage.setItem(ANON_ID_STORAGE_KEY, JSON.stringify({
          anon_id: anonId,
          created_at: now
        }));
        
        return anonId;
      } catch (e) {
        // Fallback if localStorage fails
        return 'anon_' + Math.random().toString(36).slice(2) + '_' + Date.now().toString(36);
      }
    },

    /**
     * Get stored attribution data
     */
    getStoredAttribution: function() {
      try {
        var data = localStorage.getItem(ATTRIBUTION_STORAGE_KEY);
        return data ? JSON.parse(data) : null;
      } catch (e) {
        return null;
      }
    },

    /**
     * Store attribution data
     */
    storeAttribution: function(data) {
      try {
        var existing = this.getStoredAttribution() || {};
        var updated = Object.assign({}, existing, data);
        localStorage.setItem(ATTRIBUTION_STORAGE_KEY, JSON.stringify(updated));
      } catch (e) { /* ignore */ }
    },

    /**
     * Track an attribution event
     */
    trackEvent: function(eventType, eventData, context) {
      if (!this.initialized) return;
      
      // Validate client_id is present
      if (!this.clientId || !this.clientId.trim()) {
        log('warn', 'Cannot track event: client_id is missing');
        return;
      }
      
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
      
      // Flush immediately for important events
      if (eventType === 'add_to_cart' || eventType === 'checkout_started' || eventType === 'order_completed') {
        this.flushEvents();
      }
      
      // Flush if queue is full
      if (this.eventQueue.length >= EVENT_BATCH_SIZE) {
        this.flushEvents();
      }
      
      log('info', 'Attribution event: ' + eventType);
    },

    /**
     * Flush queued events to backend
     */
    flushEvents: function() {
      if (this.eventQueue.length === 0 || !this.baseUrl) return;
      
      var events = this.eventQueue.splice(0, EVENT_BATCH_SIZE);
      var self = this;
      
      fetch(this.baseUrl + '/api/attribution/events/batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ events: events })
      })
      .then(function(res) {
        if (!res.ok) {
          // Re-queue events on failure
          self.eventQueue = events.concat(self.eventQueue);
        }
      })
      .catch(function() {
        // Re-queue events on error
        self.eventQueue = events.concat(self.eventQueue);
      });
    },

    /**
     * Decorate a URL with bot_ref parameter
     */
    decorateUrl: function(url) {
      if (!url || !this.botRef) return url;
      
      try {
        var urlObj = new URL(url, window.location.origin);
        urlObj.searchParams.set('bot_ref', this.botRef);
        return urlObj.toString();
      } catch (e) {
        // Fallback for relative URLs
        var separator = url.indexOf('?') === -1 ? '?' : '&';
        return url + separator + 'bot_ref=' + encodeURIComponent(this.botRef);
      }
    },

    /**
     * Persist attribution tokens to Shopify cart
     * Works on Shopify stores - silently fails on non-Shopify (which is fine)
     */
    persistToCart: function(retryCount) {
      retryCount = retryCount || 0;
      var self = this;
      var maxRetries = 2;
      
      // Don't require window.Shopify - just try the endpoint
      // It will work on Shopify stores and fail gracefully elsewhere
      log('info', 'Persisting attribution to cart (attempt ' + (retryCount + 1) + ')...');
      log('info', '  bot_ref: ' + (this.botRef ? this.botRef.substring(0, 12) + '...' : 'null'));
      log('info', '  anon_id: ' + (this.anonId ? this.anonId.substring(0, 12) + '...' : 'null'));
      
      if (!this.botRef && !this.anonId) {
        log('warn', 'No attribution tokens to persist');
        return;
      }
      
      fetch('/cart/update.js', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          attributes: {
            bot_ref: this.botRef,
            anon_id: this.anonId,
            chat_session_id: this.clientId + '_' + this.botRef
          }
        })
      })
      .then(function(res) {
        if (res.ok) {
          return res.json().then(function(data) {
            log('info', '✅ Attribution persisted to cart successfully');
            log('info', '  Cart token: ' + (data.token || 'unknown'));
            // Verify attributes were set
            if (data.attributes && data.attributes.bot_ref) {
              log('info', '  Verified bot_ref in cart: ' + data.attributes.bot_ref.substring(0, 12) + '...');
            }
          });
        } else {
          // Non-Shopify store or error - log and retry if needed
          log('warn', 'Cart update returned ' + res.status);
          if (retryCount < maxRetries) {
            setTimeout(function() {
              self.persistToCart(retryCount + 1);
            }, 1000);
          }
        }
      })
      .catch(function(err) {
        // This is expected on non-Shopify stores
        log('warn', 'Failed to persist attribution to cart: ' + err.message);
        if (retryCount < maxRetries && err.message.indexOf('NetworkError') === -1) {
          setTimeout(function() {
            self.persistToCart(retryCount + 1);
          }, 1000);
        }
      });
    },

    /**
     * Clean up on destroy
     */
    destroy: function() {
      if (this.flushTimer) {
        clearInterval(this.flushTimer);
        this.flushTimer = null;
      }
      
      // Final flush
      this.flushEvents();
      
      // Track session end
      this.trackEvent('session_ended');
      
      this.initialized = false;
    }
  };

  // ============================================================
  // UTILITY FUNCTIONS
  // ============================================================
  
  // Safe console logging
  function log(level, msg) {
    try {
      if (typeof console !== 'undefined' && console[level]) {
        console[level]('[FashionBot] ' + msg);
      }
    } catch (e) { /* ignore */ }
  }

  // Defer non-critical work
  function defer(fn) {
    if (typeof requestIdleCallback === 'function') {
      requestIdleCallback(fn, { timeout: 2000 });
    } else {
      setTimeout(fn, 1);
    }
  }

  // Safe error handler
  function safeCall(fn, context) {
    return function() {
      try {
        return fn.apply(context || null, arguments);
      } catch (e) {
        log('error', 'Error: ' + (e.message || e));
        return null;
      }
    };
  }

  // Generate unique ID
  function generateId() {
    return 'fbw_' + Date.now().toString(36) + Math.random().toString(36).substr(2, 9);
  }

  // ============================================================
  // INDEXEDDB STORAGE (Lazy loaded)
  // ============================================================
  var Storage = {
    db: null,
    isSupported: typeof indexedDB !== 'undefined',

    init: function(callback) {
      if (!this.isSupported || this.db) {
        callback && callback(this.db);
        return;
      }

      try {
        var request = indexedDB.open(DB_NAME, DB_VERSION);
        var self = this;

        request.onerror = function() {
          log('warn', 'IndexedDB not available');
          callback && callback(null);
        };

        request.onsuccess = function(e) {
          self.db = e.target.result;
          log('info', 'IndexedDB initialized');
          callback && callback(self.db);
        };

        request.onupgradeneeded = function(e) {
          var db = e.target.result;
          if (!db.objectStoreNames.contains(STORE_NAME)) {
            var store = db.createObjectStore(STORE_NAME, { keyPath: 'id' });
            store.createIndex('sessionId', 'sessionId', { unique: false });
            store.createIndex('timestamp', 'timestamp', { unique: false });
          }
        };
      } catch (e) {
        log('warn', 'IndexedDB init failed: ' + e.message);
        callback && callback(null);
      }
    },

    saveMessages: function(sessionId, messages) {
      if (!this.db) return;
      
      defer(safeCall(function() {
        var tx = this.db.transaction([STORE_NAME], 'readwrite');
        var store = tx.objectStore(STORE_NAME);
        var now = Date.now();

        messages.forEach(function(msg, idx) {
          store.put({
            id: sessionId + '_' + idx,
            sessionId: sessionId,
            message: msg,
            timestamp: now
          });
        });
      }, this));
    },

    loadMessages: function(sessionId, callback) {
      if (!this.db) {
        callback([]);
        return;
      }

      try {
        var tx = this.db.transaction([STORE_NAME], 'readonly');
        var store = tx.objectStore(STORE_NAME);
        var index = store.index('sessionId');
        var request = index.getAll(IDBKeyRange.only(sessionId));
        var now = Date.now();

        request.onsuccess = function(e) {
          var results = e.target.result || [];
          // Filter out expired messages
          var valid = results.filter(function(r) {
            return (now - r.timestamp) < MESSAGE_TTL;
          });
          // Sort by timestamp
          valid.sort(function(a, b) { return a.timestamp - b.timestamp; });
          callback(valid.map(function(r) { return r.message; }));
        };

        request.onerror = function() {
          callback([]);
        };
      } catch (e) {
        callback([]);
      }
    },

    cleanup: function() {
      if (!this.db) return;

      defer(safeCall(function() {
        var tx = this.db.transaction([STORE_NAME], 'readwrite');
        var store = tx.objectStore(STORE_NAME);
        var index = store.index('timestamp');
        var cutoff = Date.now() - MESSAGE_TTL;
        var range = IDBKeyRange.upperBound(cutoff);
        
        index.openCursor(range).onsuccess = function(e) {
          var cursor = e.target.result;
          if (cursor) {
            cursor.delete();
            cursor.continue();
          }
        };
      }, this));
    },

    clear: function(sessionId) {
      if (!this.db) return;

      try {
        var tx = this.db.transaction([STORE_NAME], 'readwrite');
        var store = tx.objectStore(STORE_NAME);
        var index = store.index('sessionId');
        
        index.openCursor(IDBKeyRange.only(sessionId)).onsuccess = function(e) {
          var cursor = e.target.result;
          if (cursor) {
            cursor.delete();
            cursor.continue();
          }
        };
      } catch (e) { /* ignore */ }
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
    woocommerce: {
      productUrlPattern: /\/product\/([^/?#]+)/,
      collectionUrlPattern: /\/product-category\/([^/?#]+)/,
      cartUrlPattern: /\/cart/,
      checkoutUrlPattern: /\/checkout/,
      titleSelectors: ['.product_title', 'h1.entry-title', 'h1'],
      priceSelectors: ['.woocommerce-Price-amount', '.price .amount']
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
    // Detect platform first for the context
    var detectedPlatform = detectPlatform();
    
    var context = {
      url: window.location.href,
      pathname: window.location.pathname,
      hostname: window.location.hostname,  // Include hostname for iframe API calls
      platform: detectedPlatform,           // Include platform for Add to Cart
      pageType: 'unknown',
      productHandle: null,
      productTitle: null,
      productPrice: null
    };

    try {
      var cfg = platformConfig || PlatformConfigs[detectedPlatform] || PlatformConfigs.generic;
      var pathname = window.location.pathname;

      // Detect page type
      var match;
      if (cfg.productUrlPattern && (match = pathname.match(cfg.productUrlPattern))) {
        context.pageType = 'product';
        context.productHandle = match[1];
      } else if (cfg.collectionUrlPattern && (match = pathname.match(cfg.collectionUrlPattern))) {
        context.pageType = 'collection';
      } else if (cfg.cartUrlPattern && cfg.cartUrlPattern.test(pathname)) {
        context.pageType = 'cart';
      } else if (cfg.checkoutUrlPattern && cfg.checkoutUrlPattern.test(pathname)) {
        context.pageType = 'checkout';
      } else if (pathname === '/' || pathname === '') {
        context.pageType = 'home';
      }

      // Extract product details
      if (context.pageType === 'product') {
        cfg.titleSelectors && cfg.titleSelectors.some(function(sel) {
          var el = document.querySelector(sel);
          if (el && el.textContent.trim()) {
            context.productTitle = el.textContent.trim();
            return true;
          }
          return false;
        });

        cfg.priceSelectors && cfg.priceSelectors.some(function(sel) {
          var el = document.querySelector(sel);
          if (el && el.textContent.trim()) {
            context.productPrice = el.textContent.trim();
            return true;
          }
          return false;
        });
      }
    } catch (e) {
      log('warn', 'Context detection error: ' + e.message);
    }

    return context;
  }

  // ============================================================
  // MAIN WIDGET CLASS
  // ============================================================
  function FashionBotWidgetCore() {
    this.config = {
      clientName: null,
      position: 'bottom-right',
      theme: 'light',
      platform: 'auto'
    };

    this.state = {
      initialized: false,
      buttonInjected: false,
      iframeLoaded: false,
      chatOpen: false,
      wsConnected: false,
      sessionId: null
    };

    this.elements = {
      container: null,
      button: null,
      wrapper: null,
      iframe: null
    };

    this.ws = null;
    this.baseUrl = '';
    this.platformConfig = null;
    this.reconnectAttempts = 0;
    this.urlObserver = null;
    this.lastUrl = '';

    // Bind methods
    this.handleMessage = this.handleMessage.bind(this);
    this.onButtonClick = this.onButtonClick.bind(this);
  }

  FashionBotWidgetCore.prototype = {
    // ----------------------------------------
    // INITIALIZATION
    // ----------------------------------------
    init: function(options) {
      if (this.state.initialized) {
        log('warn', 'Already initialized');
        return this;
      }

      try {
        // Merge config
        for (var key in options) {
          if (options.hasOwnProperty(key)) {
            this.config[key] = options[key];
          }
        }

        if (!this.config.clientName) {
          log('error', 'clientName is required');
          return this;
        }

        // Detect base URL
        this.baseUrl = this.detectBaseUrl();
        if (!this.baseUrl) {
          log('error', 'Could not detect base URL');
          return this;
        }

        // Detect platform
        var platform = this.config.platform === 'auto' ? detectPlatform() : this.config.platform;
        this.platformConfig = PlatformConfigs[platform] || PlatformConfigs.generic;

        // Generate session ID
        this.state.sessionId = this.getOrCreateSessionId();

        log('info', 'Initializing widget v' + WIDGET_VERSION + ' for ' + this.config.clientName);

        // Initialize attribution tracking
        Attribution.init(this.baseUrl, this.config.clientName);

        // Inject button (lazy - iframe loads on click)
        this.injectButton();

        this.state.initialized = true;
        return this;

      } catch (e) {
        log('error', 'Init failed: ' + e.message);
        return this;
      }
    },

    detectBaseUrl: function() {
      try {
        var scripts = document.getElementsByTagName('script');
        for (var i = 0; i < scripts.length; i++) {
          var src = scripts[i].src || '';
          if (src.indexOf('chat-widget') !== -1) {
            var url = new URL(src);
            return url.protocol + '//' + url.host;
          }
        }
      } catch (e) { /* ignore */ }
      return '';
    },

    getOrCreateSessionId: function() {
      var key = 'fbw_session_' + this.config.clientName.replace(/\s+/g, '_');
      var sid = null;
      
      try {
        sid = sessionStorage.getItem(key);
        if (!sid) {
          sid = generateId();
          sessionStorage.setItem(key, sid);
        }
      } catch (e) {
        sid = generateId();
      }
      
      return sid;
    },

    // ----------------------------------------
    // UI INJECTION (Lazy)
    // ----------------------------------------
    injectButton: function() {
      if (this.state.buttonInjected) return;

      var self = this;

      // Inject function - creates button and styles
      var inject = function() {
        try {
          if (!document.body) {
            setTimeout(inject, 50);
            return;
          }

          self.injectStyles();
          self.createButtonOnly();
          self.state.buttonInjected = true;
          log('info', 'Button injected successfully');
        } catch (e) {
          log('error', 'Button injection failed: ' + e.message);
          console.error('[FashionBot] Injection error:', e);
        }
      };

      // Run immediately or wait for DOM
      if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', inject);
      } else {
        // Use simple setTimeout instead of requestIdleCallback (more reliable)
        setTimeout(inject, 10);
      }
    },

    injectStyles: function() {
      if (document.getElementById('fbw-styles')) return;

      var isRight = this.config.position.indexOf('right') !== -1;
      var css = [
        // ═══════════════════════════════════════════════════════════════
        // CONTAINER - Fixed position with label support
        // ═══════════════════════════════════════════════════════════════
        '#fbw-container{',
          'position:fixed;',
          isRight ? 'right:24px;' : 'left:24px;',
          'bottom:90px;',
          'z-index:2147483647;',
          'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;',
          'display:flex;',
          'align-items:center;',
          'gap:12px;',
          isRight ? 'flex-direction:row-reverse;' : 'flex-direction:row;',
        '}',
        
        // ═══════════════════════════════════════════════════════════════
        // CHAT BUTTON - Pill shape with avatar (like "Ask Cupid" style)
        // ═══════════════════════════════════════════════════════════════
        '#fbw-button{',
          'display:flex;',
          'align-items:center;',
          'gap:0;',
          'padding:0;',
          'border:none;',
          'cursor:pointer;',
          'background:transparent;',
          'outline:none;',
          '-webkit-tap-highlight-color:transparent;',
          'position:relative;',
          'transition:transform 0.25s ease;',
        '}',
        
        // Pill-shaped text area
        '#fbw-button .fbw-pill{',
          'background:linear-gradient(135deg,#6366f1 0%,#8b5cf6 50%,#a855f7 100%);',
          'color:#ffffff;',
          'font-size:15px;',
          'font-weight:600;',
          'padding:14px 24px;',
          'padding-right:36px;',
          'border-radius:28px;',
          'white-space:nowrap;',
          'box-shadow:0 8px 32px rgba(99,102,241,0.4),0 4px 12px rgba(0,0,0,0.15);',
          'transition:all 0.25s ease;',
          'letter-spacing:0.3px;',
        '}',
        
        // Avatar circle (overlaps the pill) - white background for image
        '#fbw-button .fbw-avatar{',
          'width:56px;',
          'height:56px;',
          'border-radius:50%;',
          'background:#ffffff;',
          'border:3px solid #ffffff;',
          'box-shadow:0 4px 20px rgba(0,0,0,0.15);',
          'margin-left:-20px;',
          'display:flex;',
          'align-items:center;',
          'justify-content:center;',
          'overflow:hidden;',
          'position:relative;',
        '}',
        
        // Avatar content (emoji, icon, or image)
        '#fbw-button .fbw-avatar-icon{',
          'width:100%;',
          'height:100%;',
          'display:flex;',
          'align-items:center;',
          'justify-content:center;',
          'font-size:32px;',
          'line-height:1;',
          'user-select:none;',
        '}',
        '#fbw-button .fbw-avatar-icon svg{',
          'width:28px;',
          'height:28px;',
        '}',
        '#fbw-button .fbw-avatar-icon img{',
          'width:100%;',
          'height:100%;',
          'object-fit:cover;',
          'border-radius:50%;',
        '}',
        
        // Hover effect - lift up
        '#fbw-button:hover{',
          'transform:translateY(-3px);',
        '}',
        '#fbw-button:hover .fbw-pill{',
          'box-shadow:0 12px 40px rgba(99,102,241,0.5),0 6px 16px rgba(0,0,0,0.2);',
        '}',
        
        // Active press
        '#fbw-button:active{transform:scale(0.97);}',
        
        // ═══════════════════════════════════════════════════════════════
        // ONLINE DOT - Shows bot is available
        // ═══════════════════════════════════════════════════════════════
        '#fbw-button .fbw-dot{',
          'position:absolute;',
          'bottom:2px;',
          'right:2px;',
          'width:14px;',
          'height:14px;',
          'background:#22c55e;',
          'border-radius:50%;',
          'border:2.5px solid #fff;',
          'box-shadow:0 2px 8px rgba(34,197,94,0.6);',
          'animation:fbw-dot-pulse 2s ease-in-out infinite;',
          'z-index:2;',
        '}',
        '@keyframes fbw-dot-pulse{',
          '0%,100%{opacity:1;transform:scale(1);}',
          '50%{opacity:0.85;transform:scale(1.15);}',
        '}',
        
        // ═══════════════════════════════════════════════════════════════
        // PULSE ANIMATION - Attention grab (runs twice)
        // ═══════════════════════════════════════════════════════════════
        '@keyframes fbw-pulse{',
          '0%{box-shadow:0 8px 32px rgba(99,102,241,0.4),0 0 0 0 rgba(99,102,241,0.5);}',
          '50%{box-shadow:0 8px 32px rgba(99,102,241,0.4),0 0 0 16px rgba(99,102,241,0);}',
          '100%{box-shadow:0 8px 32px rgba(99,102,241,0.4),0 0 0 0 rgba(99,102,241,0);}',
        '}',
        '#fbw-button.pulse .fbw-pill{animation:fbw-pulse 2s ease-out 2;}',
        
        // ═══════════════════════════════════════════════════════════════
        // CLOSE BUTTON - X button when chat is open (hidden by default)
        // ═══════════════════════════════════════════════════════════════
        '#fbw-close{',
          'display:none;',
          'position:absolute;',
          'top:-8px;',
          'left:-8px;',
          'width:28px;',
          'height:28px;',
          'border-radius:50%;',
          'background:#ffffff;',
          'border:none;',
          'cursor:pointer;',
          'box-shadow:0 2px 8px rgba(0,0,0,0.15);',
          'align-items:center;',
          'justify-content:center;',
          'font-size:16px;',
          'color:#666;',
          'transition:all 0.2s;',
          'z-index:3;',
        '}',
        '#fbw-close:hover{background:#f3f4f6;color:#333;}',
        '#fbw-container.chat-open #fbw-close{display:flex;}',
        
        // Hide label when using pill design (text is in the button itself)
        '#fbw-label{display:none!important;}',
        
        // ═══════════════════════════════════════════════════════════════
        // CHAT WRAPPER - Desktop view
        // ═══════════════════════════════════════════════════════════════
        '#fbw-wrapper{',
          'display:none;',
          'position:fixed;',
          isRight ? 'right:20px;' : 'left:20px;',
          'bottom:170px;',
          'width:390px;',
          'height:620px;',
          'max-height:calc(100vh - 120px);',
          'border:none;',
          'border-radius:20px;',
          'box-shadow:0 20px 60px rgba(0,0,0,0.4),0 0 0 1px rgba(255,255,255,0.1);',
          'overflow:hidden;',
          'background:#1a1a2e;',
        '}',
        '#fbw-wrapper.open{display:block;animation:fbw-slide 0.35s cubic-bezier(0.34,1.56,0.64,1);}',
        '#fbw-wrapper.maximized{width:90vw!important;height:90vh!important;max-height:90vh!important;bottom:50%!important;' + (isRight ? 'right:50%!important;transform:translate(50%,50%)!important;' : 'left:50%!important;transform:translate(-50%,50%)!important;') + '}',
        '#fbw-iframe{width:100%;height:100%;border:none;background:#1a1a2e;}',
        '@keyframes fbw-slide{from{transform:translateY(30px) scale(0.95);opacity:0;}to{transform:translateY(0) scale(1);opacity:1;}}',
        
        // Mobile backdrop overlay
        '#fbw-backdrop{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.6);z-index:2147483646;backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);}',
        '#fbw-backdrop.visible{display:block;animation:fbw-fade 0.2s ease-out;}',
        '@keyframes fbw-fade{from{opacity:0;}to{opacity:1;}}',
        
        // ═══════════════════════════════════════════════════════════════
        // MOBILE STYLES - Higher position to avoid thumb + browser UI
        // ═══════════════════════════════════════════════════════════════
        '@media(max-width:768px){',
          '#fbw-container{',
            'right:12px;',
            'left:auto;',
            'bottom:80px;',
          '}',
          '#fbw-button .fbw-pill{font-size:14px;padding:12px 20px;padding-right:32px;}',
          '#fbw-button .fbw-avatar{width:50px;height:50px;margin-left:-18px;}',
          '#fbw-button .fbw-avatar-icon{font-size:24px;}',
          '#fbw-close{width:26px;height:26px;font-size:14px;top:-6px;left:-6px;}',
          
          // Full-screen chat on mobile
          '#fbw-wrapper{',
            'position:fixed!important;',
            'top:0!important;',
            'left:0!important;',
            'right:0!important;',
            'bottom:0!important;',
            'width:100%!important;',
            'height:100%!important;',
            'max-height:100%!important;',
            'border-radius:0!important;',
            'z-index:2147483647!important;',
          '}',
          '#fbw-wrapper.open{animation:fbw-slideup 0.3s ease-out;}',
          '@keyframes fbw-slideup{from{transform:translateY(100%);}to{transform:translateY(0);}}',
        '}',
        
        // Small mobile (iPhone SE, etc) - show icon only
        '@media(max-width:400px){',
          '#fbw-container{right:10px;bottom:80px;}',
          '#fbw-button .fbw-pill{display:none;}',
          '#fbw-button .fbw-avatar{width:56px;height:56px;margin-left:0;border:3px solid #6366f1;box-shadow:0 6px 24px rgba(99,102,241,0.4);}',
          '#fbw-button .fbw-avatar-icon{font-size:26px;}',
        '}'
      ].join('');

      var style = document.createElement('style');
      style.id = 'fbw-styles';
      style.textContent = css;
      document.head.appendChild(style);
    },

    createButtonOnly: function() {
      var self = this;
      
      // Create container
      this.elements.container = document.createElement('div');
      this.elements.container.id = 'fbw-container';

      // Create button with pill + avatar design (like "Ask Cupid")
      this.elements.button = document.createElement('button');
      this.elements.button.id = 'fbw-button';
      this.elements.button.setAttribute('aria-label', 'Open chat');
      
      // Build the pill + avatar structure
      var buttonText = this.getButtonText();
      var avatarIcon = this.getAvatarIcon();
      this.elements.button.innerHTML = 
        '<span class="fbw-pill">' + buttonText + '</span>' +
        '<span class="fbw-avatar"><span class="fbw-avatar-icon">' + avatarIcon + '</span></span>' +
        '<span class="fbw-dot"></span>';
      
      this.elements.button.addEventListener('click', this.onButtonClick);

      // Create close button (X) - shown when chat is open
      this.elements.closeBtn = document.createElement('button');
      this.elements.closeBtn.id = 'fbw-close';
      this.elements.closeBtn.setAttribute('aria-label', 'Close chat');
      this.elements.closeBtn.innerHTML = '✕';
      this.elements.closeBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        self.close();
      });

      // Create wrapper (empty, iframe loaded on click)
      this.elements.wrapper = document.createElement('div');
      this.elements.wrapper.id = 'fbw-wrapper';

      this.elements.container.appendChild(this.elements.closeBtn);
      this.elements.container.appendChild(this.elements.button);
      this.elements.container.appendChild(this.elements.wrapper);
      document.body.appendChild(this.elements.container);
      
      // ═══════════════════════════════════════════════════════════════
      // ATTENTION ANIMATION (one-time pulse)
      // ═══════════════════════════════════════════════════════════════
      setTimeout(function() {
        if (self.elements.button && !self.state.chatOpen) {
          self.elements.button.classList.add('pulse');
        }
      }, 2500);
    },
    
    // Get button text - "Ask Bloom"
    getButtonText: function() {
      return 'Ask Bloom';
    },
    
    // Get avatar - cartoon boy with question mark image
    getAvatarIcon: function() {
      // Try to use custom image URL from config first
      if (this.config.avatarImageUrl) {
        var imageUrl = this.config.avatarImageUrl;
        return '<img src="' + imageUrl + '" alt="Chat Support" onerror="this.onerror=null; this.parentElement.innerHTML=\'🤔\';" style="width:100%;height:100%;object-fit:cover;border-radius:50%;">';
      }
      
      // Use local image from static folder
      var basePath = this.baseUrl ? this.baseUrl + '/static' : '/static';
      var imageUrl = basePath + '/an-illustration-of-a-boy-with-a-question-mark-hand-under-his-chin-vector.jpg';
      
      // Return image with emoji fallback (🤔 = thinking face with hand on chin)
      return '<img src="' + imageUrl + '" alt="Chat Support" onerror="this.onerror=null; this.style.display=\'none\'; this.parentElement.innerHTML=\'🤔\';" style="width:100%;height:100%;object-fit:cover;border-radius:50%;">';
    },

    // ----------------------------------------
    // LAZY IFRAME LOADING
    // ----------------------------------------
    loadIframeIfNeeded: function() {
      if (this.state.iframeLoaded) return;

      var self = this;

      // Create iframe
      this.elements.iframe = document.createElement('iframe');
      this.elements.iframe.id = 'fbw-iframe';
      this.elements.iframe.setAttribute('title', 'Chat Widget');
      this.elements.iframe.setAttribute('loading', 'lazy');
      this.elements.iframe.setAttribute('allow', 'clipboard-write');

      // Build iframe URL
      var frameUrl = this.baseUrl + '/static/chat-widget-frame.html';
      frameUrl += '?clientName=' + encodeURIComponent(this.config.clientName);
      frameUrl += '&theme=' + encodeURIComponent(this.config.theme);
      frameUrl += '&sessionId=' + encodeURIComponent(this.state.sessionId);
      frameUrl += '&v=' + WIDGET_VERSION;
      
      // Build WebSocket API URL (ws:// or wss://)
      var wsProtocol = this.baseUrl.indexOf('https://') === 0 ? 'wss://' : 'ws://';
      var wsHost = this.baseUrl.replace(/^https?:\/\//, '');
      var apiUrl = wsProtocol + wsHost + '/ws/chat';
      frameUrl += '&apiUrl=' + encodeURIComponent(apiUrl);

      this.elements.iframe.src = frameUrl;
      this.elements.wrapper.appendChild(this.elements.iframe);

      // Listen for iframe messages
      window.addEventListener('message', this.handleMessage);

      // Setup URL change detection (for SPAs)
      this.setupUrlObserver();

      this.state.iframeLoaded = true;

      // Initialize IndexedDB lazily
      defer(function() {
        Storage.init(function() {
          Storage.cleanup(); // Cleanup old messages
        });
      });

      log('info', 'Iframe loaded');
    },

    // ----------------------------------------
    // EVENT HANDLERS
    // ----------------------------------------
    onButtonClick: function() {
      // Clear attention animations on click
      if (this.elements.button) {
        this.elements.button.classList.remove('pulse');
      }
      if (this.elements.label) {
        this.elements.label.classList.remove('show');
      }
      this.toggle();
    },

    handleMessage: function(event) {
      // Verify origin
      if (event.origin !== this.baseUrl) return;
      
      var data = event.data;
      if (!data || typeof data !== 'object') return;

      switch (data.type) {
        case 'fashionbot-ready':
          log('info', 'Frame ready');
          this.sendContext();
          this.loadCachedMessages();
          break;

        case 'fashionbot-close':
          this.close();
          break;

        case 'fashionbot-maximize':
          this.toggleMaximize();
          break;

        case 'fashionbot-request-context':
          this.sendContext();
          break;

        case 'fashionbot-save-messages':
          if (data.messages && data.messages.length) {
            Storage.saveMessages(this.state.sessionId, data.messages);
          }
          break;

        case 'fashionbot-clear-messages':
          Storage.clear(this.state.sessionId);
          break;

        case 'fashionbot-add-to-cart':
          this.addToCart(data.variantId, data.quantity, data);
          break;
        
        // Attribution event tracking from iframe
        case 'fashionbot-message-sent':
          Attribution.trackEvent('message_sent', {
            message_length: data.messageLength || 0
          }, detectPageContext(this.platformConfig));
          break;
        
        case 'fashionbot-message-received':
          Attribution.trackEvent('message_received', {
            has_products: data.hasProducts || false,
            response_time_ms: data.responseTimeMs || 0
          });
          break;
        
        case 'fashionbot-product-clicked':
          Attribution.trackEvent('product_clicked', {
            product_title: data.productTitle,
            product_handle: data.productHandle,
            product_price: data.productPrice,
            product_url: data.productUrl
          });
          // Just open the decorated URL - don't touch cart on view
          // Widget on product page will detect bot_ref in URL and persist to cart
          if (data.productUrl) {
            window.open(data.productUrl, '_blank');
          }
          break;
        
        case 'fashionbot-link-clicked':
          Attribution.trackEvent('link_clicked', {
            link_url: data.linkUrl,
            link_text: data.linkText
          });
          break;
        
        case 'fashionbot-checkout-started':
          Attribution.trackEvent('checkout_started', {});
          Attribution.persistToCart();
          break;
        
        case 'fashionbot-get-bot-ref':
          // Send bot_ref back to iframe for URL decoration
          this.postMessage({
            type: 'bot-ref',
            botRef: Attribution.botRef,
            anonId: Attribution.anonId
          });
          break;
      }
    },

    // ----------------------------------------
    // PUBLIC API
    // ----------------------------------------
    open: function() {
      if (!this.state.initialized) return this;
      
      // Load iframe on first open (lazy)
      this.loadIframeIfNeeded();
      
      this.elements.wrapper.classList.add('open');
      this.elements.container.classList.add('chat-open');
      this.state.chatOpen = true;
      
      // Remove pulse animation when opened
      if (this.elements.button) {
        this.elements.button.classList.remove('pulse');
      }
      
      // Send opened event with context
      this.sendContext();
      this.postMessage({ type: 'opened' });
      
      // Track attribution event
      var context = detectPageContext(this.platformConfig);
      Attribution.trackEvent('chat_opened', {}, context);
      
      log('info', 'Chat opened');
      return this;
    },

    close: function() {
      if (!this.state.initialized) return this;
      
      this.elements.wrapper.classList.remove('open', 'maximized');
      this.elements.container.classList.remove('chat-open');
      this.state.chatOpen = false;
      
      // Track attribution event
      Attribution.trackEvent('chat_closed');
      
      log('info', 'Chat closed');
      return this;
    },

    toggle: function() {
      if (this.state.chatOpen) {
        this.close();
      } else {
        this.open();
      }
      return this;
    },

    toggleMaximize: function() {
      this.elements.wrapper.classList.toggle('maximized');
      var isMax = this.elements.wrapper.classList.contains('maximized');
      this.postMessage({ type: 'maximized', value: isMax });
      return this;
    },

    destroy: function() {
      try {
        // Cleanup attribution
        Attribution.destroy();
        
        // Remove event listeners
        window.removeEventListener('message', this.handleMessage);
        
        if (this.elements.button) {
          this.elements.button.removeEventListener('click', this.onButtonClick);
        }

        // Disconnect URL observer
        if (this.urlObserver) {
          this.urlObserver.disconnect();
          this.urlObserver = null;
        }

        // Remove DOM elements
        if (this.elements.container && this.elements.container.parentNode) {
          this.elements.container.parentNode.removeChild(this.elements.container);
        }

        // Remove styles
        var styles = document.getElementById('fbw-styles');
        if (styles) styles.parentNode.removeChild(styles);

        // Reset state
        this.state = {
          initialized: false,
          buttonInjected: false,
          iframeLoaded: false,
          chatOpen: false,
          wsConnected: false,
          sessionId: null
        };

        this.elements = {};

        log('info', 'Widget destroyed');

      } catch (e) {
        log('error', 'Destroy error: ' + e.message);
      }

      return this;
    },

    // ----------------------------------------
    // INTERNAL METHODS
    // ----------------------------------------
    postMessage: function(message) {
      if (this.elements.iframe && this.elements.iframe.contentWindow) {
        try {
          // Verify iframe is loaded and has correct src before posting
          var iframeSrc = this.elements.iframe.src || '';
          if (iframeSrc.indexOf(this.baseUrl) === 0) {
            this.elements.iframe.contentWindow.postMessage(message, this.baseUrl);
          } else {
            log('warn', 'Iframe src mismatch, skipping postMessage');
          }
        } catch (e) { 
          // Silently ignore cross-origin errors
          log('warn', 'postMessage error (likely cross-origin): ' + e.message);
        }
      }
    },

    sendContext: function() {
      var context = detectPageContext(this.platformConfig);
      this.postMessage({ type: 'pageContext', context: context });
    },

    loadCachedMessages: function() {
      var self = this;
      Storage.loadMessages(this.state.sessionId, function(messages) {
        if (messages && messages.length > 0) {
          self.postMessage({ type: 'cached-messages', messages: messages });
          log('info', 'Loaded ' + messages.length + ' cached messages');
        }
      });
    },

    setupUrlObserver: function() {
      var self = this;
      this.lastUrl = window.location.href;

      // Use MutationObserver on <title> as proxy for SPA navigation
      // This avoids polling and catches most SPA route changes
      try {
        this.urlObserver = new MutationObserver(function() {
          if (window.location.href !== self.lastUrl) {
            self.lastUrl = window.location.href;
            log('info', 'URL changed, updating context');
            self.sendContext();
          }
        });

        // Observe title changes (common in SPAs)
        var title = document.querySelector('title');
        if (title) {
          this.urlObserver.observe(title, { childList: true, subtree: true, characterData: true });
        }

        // Also listen to popstate for back/forward navigation
        window.addEventListener('popstate', function() {
          if (window.location.href !== self.lastUrl) {
            self.lastUrl = window.location.href;
            self.sendContext();
          }
        });

      } catch (e) {
        log('warn', 'URL observer setup failed: ' + e.message);
      }
    },

    addToCart: function(variantId, quantity, eventData) {
      if (!variantId) return;
      
      var self = this;
      var context = detectPageContext(this.platformConfig);
      
      fetch('/cart/add.js', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          items: [{ id: parseInt(variantId), quantity: quantity || 1 }]
        })
      })
      .then(function(res) {
        if (!res.ok) throw new Error('Add to cart failed');
        return res.json();
      })
      .then(function(data) {
        log('info', 'Added to cart');
        self.postMessage({ type: 'cart-add-success', data: data });
        self.updateCartCount();
        
        // Track add to cart event with product details from iframe or cart response
        Attribution.trackEvent('add_to_cart', {
          variant_id: variantId,
          quantity: quantity || 1,
          product_title: eventData?.productTitle || data.title || context.productTitle || '',
          product_price: eventData?.productPrice || data.price || context.productPrice || '',
          product_url: eventData?.productUrl || context.productHandle ? ('/products/' + context.productHandle) : '',
          product_handle: eventData?.handle || context.productHandle || ''
        }, context);
        
        // Persist attribution tokens to cart
        Attribution.persistToCart();
      })
      .catch(function(err) {
        log('error', 'Add to cart error: ' + err.message);
        self.postMessage({ type: 'cart-add-error', error: err.message });
      });
    },

    updateCartCount: function() {
      window.dispatchEvent(new CustomEvent('fashionbot:cart-updated'));
      
      if (typeof window.Shopify !== 'undefined') {
        fetch('/cart.js')
          .then(function(res) { return res.json(); })
          .then(function(cart) {
            var count = cart.item_count;
            ['.cart-count', '.cart-item-count', '[data-cart-count]', '#CartCount'].forEach(function(sel) {
              var el = document.querySelector(sel);
              if (el) {
                el.textContent = count;
                el.style.display = count > 0 ? '' : 'none';
              }
            });
          })
          .catch(function() { /* ignore */ });
      }
    }
  };

  // ============================================================
  // EXPOSE API & PROCESS QUEUE
  // ============================================================
  var widget = new FashionBotWidgetCore();

  // Process queued calls from loader
  var queue = (window.FashionBotWidget && window.FashionBotWidget._q) || [];

  // Expose clean API
  window.FashionBotWidget = {
    init: function(config) { return widget.init(config); },
    open: function() { return widget.open(); },
    close: function() { return widget.close(); },
    toggle: function() { return widget.toggle(); },
    destroy: function() { return widget.destroy(); },
    version: WIDGET_VERSION,
    
    // Attribution API
    attribution: {
      getBotRef: function() { return Attribution.botRef; },
      getAnonId: function() { return Attribution.anonId; },
      trackEvent: function(eventType, eventData) { return Attribution.trackEvent(eventType, eventData); },
      decorateUrl: function(url) { return Attribution.decorateUrl(url); },
      persistToCart: function() { return Attribution.persistToCart(); }
    }
  };

  // Process queued calls
  queue.forEach(function(item) {
    try {
      if (item && item[0] && typeof window.FashionBotWidget[item[0]] === 'function') {
        window.FashionBotWidget[item[0]](item[1]);
      }
    } catch (e) { /* ignore */ }
  });

  // ============================================================
  // AUTO-INITIALIZATION
  // ============================================================
  // Check for FashionBotWidgetConfig and auto-init
  if (window.FashionBotWidgetConfig) {
    log('info', 'Auto-initializing with config: ' + JSON.stringify(window.FashionBotWidgetConfig));
    widget.init(window.FashionBotWidgetConfig);
  } else {
    log('warn', 'No FashionBotWidgetConfig found - widget will not initialize automatically');
  }

  log('info', 'Widget bundle v' + WIDGET_VERSION + ' loaded');

})();

