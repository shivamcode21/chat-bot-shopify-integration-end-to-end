/**
 * Fashion Bot Widget Loader (Production v2)
 * 
 * ULTRA-LIGHTWEIGHT LOADER (~2.5KB gzipped)
 * 
 * Production Best Practices:
 * ✅ Async script load - never blocks page rendering
 * ✅ No global pollution - single minimal namespace (FashionBotWidget)
 * ✅ Error isolation - all errors caught, NEVER crashes host page
 * ✅ Fallback safety - uses configurable stable fallback if anything fails
 * ✅ CDN-friendly - fetches config then loads immutable versioned bundle
 * ✅ No long JS tasks - uses requestIdleCallback/setTimeout
 * ✅ Zero polling - purely event-driven
 * ✅ Memory efficient - minimal footprint until interaction
 * 
 * Usage:
 * <script>
 *   window.FashionBotWidgetConfig = { clientName: 'Store Name' };
 * </script>
 * <script src="https://your-server/static/chat-widget.js" async defer></script>
 */
(function(window, document) {
  'use strict';

  // ============================================================
  // PREVENT DOUBLE LOADING
  // ============================================================
  if (window.__FBW_LOADER_V2__) return;
  window.__FBW_LOADER_V2__ = true;

  // ============================================================
  // CONSTANTS
  // ============================================================
  var DEFAULT_FALLBACK_VERSION = 'v63';
  var CONFIG_TIMEOUT_MS = 3000;
  var BUNDLE_LOAD_TIMEOUT_MS = 10000;
  var FARO_BOOTSTRAP_TIMEOUT_MS = 1800;
  var CONFIG_CACHE_KEY = '__FBW_WIDGET_CONFIG_CACHE__';
  var DEFAULT_VERSION_CAPS = Object.freeze({
    minSupportedVersion: 36,
    phoneNudgeMin: 36,
    themeMin: 36,
    headerAvatarMin: 36,
    variantUiMin: 36,
    faroMin: 38,
    clarityMin: 39
  });

  // ============================================================
  // UTILITIES
  // ============================================================
  
  // Safe console logging (never throws)
  function log(level, msg) {
    try {
      if (typeof console === 'undefined') return;
      var resolvedLevel = typeof level === 'string' ? level.toLowerCase() : 'log';
      var method = (resolvedLevel && typeof console[resolvedLevel] === 'function')
        ? console[resolvedLevel]
        : (typeof console.log === 'function' ? console.log : null);
      if (method) {
        method.call(console, '[FashionBot] ' + msg);
      }
    } catch (e) { /* silence */ }
  }

  // Defer work to idle time (never blocks main thread)
  function defer(fn, timeout) {
    try {
      if (typeof requestIdleCallback === 'function') {
        requestIdleCallback(fn, { timeout: timeout || 2000 });
      } else {
        setTimeout(fn, 1);
      }
    } catch (e) {
      setTimeout(fn, 1);
    }
  }

  // Detect base URL from loader script
  function detectBaseUrl() {
    try {
      var scripts = document.getElementsByTagName('script');
      for (var i = 0; i < scripts.length; i++) {
        var src = scripts[i].src || '';
        // Match chat-widget.js but NOT chat-widget.v1.js
        if (src.indexOf('chat-widget.js') !== -1 && src.indexOf('.v') === -1) {
          var url = new URL(src);
          return url.protocol + '//' + url.host;
        }
      }
    } catch (e) {
      log('warn', 'Base URL detection failed');
    }
    return '';
  }

  function getWidgetVersionNumber(version) {
    var match = /^v(\d+)$/.exec(String(version || ''));
    return match ? parseInt(match[1], 10) : 0;
  }

  function readCachedConfig(cacheKey) {
    try {
      if (!window.localStorage) return null;
      var raw = window.localStorage.getItem(cacheKey || CONFIG_CACHE_KEY);
      if (!raw) return null;
      return JSON.parse(raw);
    } catch (e) {
      return null;
    }
  }

  function writeCachedConfig(config, cacheKey) {
    try {
      if (!config || !window.localStorage) return;
      window.localStorage.setItem(cacheKey || CONFIG_CACHE_KEY, JSON.stringify(config));
    } catch (e) { /* ignore cache write failures */ }
  }

  function normalizeWidgetPosition(value) {
    var position = String(value || '').trim().toLowerCase();
    if (position === 'left') return 'bottom-left';
    if (position === 'right') return 'bottom-right';
    if (position === 'bottom-left' || position === 'bottom-right') return position;
    return null;
  }

  function buildConfigUrl(baseUrl, publicConfig) {
    var configUrl = baseUrl + '/widget/config.json';
    var params = [];
    var clientId = publicConfig && (publicConfig.clientId || publicConfig.encodedClientId);
    var clientName = publicConfig && publicConfig.clientName;
    if (clientId) params.push('client_id=' + encodeURIComponent(clientId));
    if (clientName) params.push('client_name=' + encodeURIComponent(clientName));
    return params.length ? configUrl + '?' + params.join('&') : configUrl;
  }

  function buildConfigCacheKey(publicConfig) {
    var clientId = publicConfig && (publicConfig.clientId || publicConfig.encodedClientId);
    var clientName = publicConfig && publicConfig.clientName;
    var identifier = String(clientId || clientName || 'default').replace(/[^\w.-]+/g, '_');
    return CONFIG_CACHE_KEY + '_' + identifier;
  }

  function applyServerWidgetConfig(config, publicConfig) {
    if (!config || typeof config !== 'object') return;
    var position = normalizeWidgetPosition(config.position || (config.clientConfig && config.clientConfig.position));
    if (!position) return;
    publicConfig.position = position;
    window.FashionBotWidgetConfig = publicConfig;
  }

  function resolveFallbackVersion(config, publicConfig) {
    var fallbackVersion = (config && config.fallbackVersion) || (publicConfig && publicConfig.fallbackVersion) || DEFAULT_FALLBACK_VERSION;
    return /^v\d+$/.test(String(fallbackVersion || '')) ? String(fallbackVersion) : DEFAULT_FALLBACK_VERSION;
  }

  function resolveVersionCaps(config) {
    var source = config && config.versionCaps && typeof config.versionCaps === 'object' ? config.versionCaps : {};
    var resolved = {};
    var key;
    for (key in DEFAULT_VERSION_CAPS) {
      if (!DEFAULT_VERSION_CAPS.hasOwnProperty(key)) continue;
      var raw = source[key];
      var parsed = parseInt(raw, 10);
      resolved[key] = !isNaN(parsed) && isFinite(parsed) && parsed >= 0 ? parsed : DEFAULT_VERSION_CAPS[key];
    }
    return resolved;
  }

  function safeHostnameFromUrl(raw) {
    try {
      if (!raw) return '';
      return new URL(String(raw), window.location.href).hostname.toLowerCase();
    } catch (e) {
      return '';
    }
  }

  function buildFaroAllowedHosts(telemetryScope, faroConfig) {
    var hosts = {};
    var i;
    var list = telemetryScope || {};
    var candidates = [list.scriptBase, list.apiBase, list.staticBase, window.__FBW_BUNDLE__ || ''];
    for (i = 0; i < candidates.length; i++) {
      var host = safeHostnameFromUrl(candidates[i]);
      if (host) hosts[host] = true;
    }
    var fromConfig = faroConfig && Array.isArray(faroConfig.allowedDomains) ? faroConfig.allowedDomains : [];
    for (i = 0; i < fromConfig.length; i++) {
      var raw = String(fromConfig[i] || '').trim().toLowerCase();
      if (!raw) continue;
      raw = raw.replace(/^https?:\/\//, '').replace(/\/.*$/, '');
      if (raw) hosts[raw] = true;
    }
    return hosts;
  }

  function eventFramesFromFaro(event) {
    try {
      var values = event && event.exception && event.exception.values;
      if (values && values[0] && values[0].stacktrace && Array.isArray(values[0].stacktrace.frames)) {
        return values[0].stacktrace.frames;
      }
    } catch (e) {}
    return [];
  }

  function hasWidgetFrameInEvent(event, allowedHosts) {
    var frames = eventFramesFromFaro(event);
    if (!frames.length) return false;
    for (var i = 0; i < frames.length; i++) {
      var filename = frames[i] && frames[i].filename ? frames[i].filename : '';
      var host = safeHostnameFromUrl(filename);
      if (host && allowedHosts[host]) return true;
    }
    return false;
  }

  function hasFaroWidgetContext(event) {
    var candidates = [
      event,
      event && event.payload,
      event && event.payload && event.payload.attributes,
      event && event.data,
      event && event.data && event.data.attributes,
      event && event.attributes,
      event && event.context
    ];
    for (var i = 0; i < candidates.length; i++) {
      var value = candidates[i];
      if (!value || typeof value !== 'object') continue;
      if (String(value.name || value.event || value.type || '').toLowerCase() === 'widget_context') return true;
      if (value.channel === 'web_widget' && value.layer === 'loader') return true;
    }
    return false;
  }

  function shouldSendLoaderFaroEvent(event, allowedHosts) {
    if (!event) return false;
    if (event.exception) return hasWidgetFrameInEvent(event, allowedHosts);
    return hasFaroWidgetContext(event);
  }

  function loadExternalScript(src, onLoad, onError) {
    try {
      var script = document.createElement('script');
      script.src = src;
      script.async = true;
      script.crossOrigin = 'anonymous';
      script.onload = function() {
        onLoad && onLoad();
      };
      script.onerror = function() {
        onError && onError();
      };
      (document.head || document.documentElement).appendChild(script);
    } catch (e) {
      onError && onError(e);
    }
  }

  function applyFaroContext(context, eventKey) {
    try {
      if (!window.GrafanaFaroWebSdk || !window.GrafanaFaroWebSdk.faro || !window.GrafanaFaroWebSdk.faro.api) return;
      var api = window.GrafanaFaroWebSdk.faro.api;
      var attributes = {};
      var key;

      for (key in context) {
        if (!context.hasOwnProperty(key)) continue;
        if (context[key] === undefined || context[key] === null || context[key] === '') continue;
        attributes[key] = context[key];
      }

      if (typeof api.setUser === 'function') {
        api.setUser({
          id: attributes.client_id || attributes.client_name || attributes.client_identifier || attributes.hostname || 'widget-client',
          name: attributes.client_name || undefined,
          attributes: attributes
        });
      }

      if (eventKey && typeof api.pushEvent === 'function' && !window[eventKey]) {
        api.pushEvent('widget_context', attributes);
        window[eventKey] = true;
      }
    } catch (e) {
      log('warn', 'Faro context apply failed');
    }
  }

  function applyClarityContext(context, eventKey) {
    try {
      if (typeof window.clarity !== 'function') return;
      var key;
      for (key in context) {
        if (!context.hasOwnProperty(key)) continue;
        if (context[key] === undefined || context[key] === null || context[key] === '') continue;
        window.clarity('set', key, String(context[key]));
      }
      if (eventKey && !window[eventKey]) {
        window.clarity('event', 'widget_context');
        window[eventKey] = true;
      }
    } catch (e) {
      log('warn', 'Clarity context apply failed');
    }
  }

  function bootstrapClarity(version, widgetConfig) {
    var clarityConfig = widgetConfig && widgetConfig.observability && widgetConfig.observability.clarity;
    var publicConfig = window.FashionBotWidgetConfig || {};
    var versionNumber = getWidgetVersionNumber(version);
    var versionCaps = resolveVersionCaps(widgetConfig);

    if (versionNumber < versionCaps.clarityMin || !clarityConfig || clarityConfig.enabled !== true || !clarityConfig.projectId) {
      return;
    }

    if (!window.__FBW_CLARITY_BOOTSTRAPPED__) {
      (function(c, l, a, r, i, t, y) {
        c[a] = c[a] || function() { (c[a].q = c[a].q || []).push(arguments); };
        t = l.createElement(r);
        t.async = 1;
        t.src = 'https://www.clarity.ms/tag/' + i;
        y = l.getElementsByTagName(r)[0];
        y.parentNode.insertBefore(t, y);
      })(window, document, 'clarity', 'script', clarityConfig.projectId);
      window.__FBW_CLARITY_BOOTSTRAPPED__ = true;
    }

    applyClarityContext({
      layer: 'loader',
      channel: 'web_widget',
      widget_version: version,
      client_identifier: publicConfig.clientId || publicConfig.clientName || publicConfig.encodedClientId || '',
      client_id: publicConfig.clientId || publicConfig.encodedClientId || '',
      client_name: publicConfig.clientName || '',
      hostname: window.location.hostname || '',
      page_url: window.location.href || ''
    }, '__FBW_CLARITY_LOADER_CONTEXT_SENT__');
  }

  function bootstrapFaro(version, widgetConfig, telemetryScope, callback) {
    var done = false;
    var timeoutId = null;
    var faroConfig = widgetConfig && widgetConfig.observability && widgetConfig.observability.faro;
    var publicConfig = window.FashionBotWidgetConfig || {};
    var versionNumber = getWidgetVersionNumber(version);
    var versionCaps = resolveVersionCaps(widgetConfig);

    function finish() {
      if (done) return;
      done = true;
      if (timeoutId) clearTimeout(timeoutId);
      callback && callback();
    }

    if (versionNumber < versionCaps.faroMin || !faroConfig || faroConfig.enabled !== true || !faroConfig.url) {
      finish();
      return;
    }

    if (window.__FBW_FARO_BOOTSTRAPPED__) {
      finish();
      return;
    }

    timeoutId = setTimeout(function() {
      log('warn', 'Faro bootstrap timeout, continuing widget load');
      finish();
    }, FARO_BOOTSTRAP_TIMEOUT_MS);

    loadExternalScript(
      'https://unpkg.com/@grafana/faro-web-sdk@2/dist/bundle/faro-web-sdk.iife.js',
      function() {
        try {
          if (!window.GrafanaFaroWebSdk || typeof window.GrafanaFaroWebSdk.initializeFaro !== 'function') {
            finish();
            return;
          }

          if (!window.__FBW_FARO_INITIALIZED__) {
            var allowedHosts = buildFaroAllowedHosts(telemetryScope, faroConfig);
            window.GrafanaFaroWebSdk.initializeFaro({
              url: faroConfig.url,
              app: {
                name: faroConfig.appName || 'fashion-bot-chat-widget',
                version: version,
                environment: faroConfig.environment || 'production'
              },
              instrumentations: [],
              beforeSend: function(event) {
                return shouldSendLoaderFaroEvent(event, allowedHosts) ? event : null;
              }
            });
            window.__FBW_FARO_INITIALIZED__ = true;
          }

          applyFaroContext({
            layer: 'loader',
            channel: 'web_widget',
            widget_version: version,
            client_identifier: publicConfig.clientId || publicConfig.clientName || publicConfig.encodedClientId || '',
            client_id: publicConfig.clientId || publicConfig.encodedClientId || '',
            client_name: publicConfig.clientName || ''
          }, '__FBW_FARO_LOADER_CONTEXT_SENT__');

          window.__FBW_FARO_BOOTSTRAPPED__ = true;
          finish();
        } catch (e) {
          log('warn', 'Faro init failed');
          finish();
        }
      },
      function() {
        log('warn', 'Faro SDK failed to load');
        finish();
      }
    );
  }

  // ============================================================
  // CONFIG FETCHER (with timeout)
  // ============================================================
  function fetchConfig(baseUrl, publicConfig, callback) {
    var configUrl = buildConfigUrl(baseUrl, publicConfig || {});
    var cacheKey = buildConfigCacheKey(publicConfig || {});
    var completed = false;
    var timeoutId = null;

    // Timeout handler
    timeoutId = setTimeout(function() {
      if (completed) return;
      completed = true;
      log('warn', 'Config timeout after ' + CONFIG_TIMEOUT_MS + 'ms, using fallback');
      callback(readCachedConfig(cacheKey));
    }, CONFIG_TIMEOUT_MS);

    try {
      // Use fetch if available (modern browsers)
      if (typeof fetch === 'function') {
        fetch(configUrl, { 
          method: 'GET',
          cache: 'default',
          credentials: 'omit'
        })
        .then(function(res) {
          if (!res.ok) throw new Error('HTTP ' + res.status);
          return res.json();
        })
        .then(function(config) {
          if (completed) return;
          completed = true;
          clearTimeout(timeoutId);
          writeCachedConfig(config, cacheKey);
          callback(config);
        })
        .catch(function(err) {
          if (completed) return;
          completed = true;
          clearTimeout(timeoutId);
          log('warn', 'Config fetch error: ' + err.message);
          callback(readCachedConfig(cacheKey));
        });
      } else {
        // Fallback to XHR (older browsers)
        var xhr = new XMLHttpRequest();
        xhr.open('GET', configUrl, true);
        xhr.onreadystatechange = function() {
          if (xhr.readyState !== 4 || completed) return;
          completed = true;
          clearTimeout(timeoutId);
          
          if (xhr.status === 200) {
            try {
              var parsedConfig = JSON.parse(xhr.responseText);
              writeCachedConfig(parsedConfig, cacheKey);
              callback(parsedConfig);
            } catch (e) {
              log('warn', 'Config parse error');
              callback(readCachedConfig(cacheKey));
            }
          } else {
            log('warn', 'Config HTTP error: ' + xhr.status);
            callback(readCachedConfig(cacheKey));
          }
        };
        xhr.onerror = function() {
          if (completed) return;
          completed = true;
          clearTimeout(timeoutId);
          log('warn', 'Config network error');
          callback(readCachedConfig(cacheKey));
        };
        xhr.send();
      }
    } catch (e) {
      if (!completed) {
        completed = true;
        clearTimeout(timeoutId);
        log('warn', 'Config exception: ' + e.message);
        callback(readCachedConfig(cacheKey));
      }
    }
  }

  // ============================================================
  // BUNDLE LOADER (async, non-blocking)
  // ============================================================
  function loadBundle(baseUrl, version, onSuccess, onError) {
    try {
      var bundleUrl = baseUrl + '/static/chat-widget.' + version + '.js';
      var script = document.createElement('script');
      var loaded = false;
      var timeoutId = null;

      // Track loaded bundle for debugging
      window.__FBW_BUNDLE__ = bundleUrl;

      // Timeout for bundle load
      timeoutId = setTimeout(function() {
        if (loaded) return;
        loaded = true;
        log('warn', 'Bundle timeout: ' + version);
        onError && onError();
      }, BUNDLE_LOAD_TIMEOUT_MS);

      script.src = bundleUrl;
      script.async = true;
      script.defer = true;
      
      // Accessibility: mark as non-critical
      script.setAttribute('data-widget', 'fashionbot');
      
      script.onload = function() {
        if (loaded) return;
        loaded = true;
        clearTimeout(timeoutId);
        log('info', 'Bundle loaded: ' + version);
        onSuccess && onSuccess();
      };

      script.onerror = function() {
        if (loaded) return;
        loaded = true;
        clearTimeout(timeoutId);
        log('warn', 'Bundle load failed: ' + version);
        onError && onError();
      };

      // Append to head (non-blocking)
      (document.head || document.documentElement).appendChild(script);
      
    } catch (e) {
      log('error', 'Bundle inject error: ' + e.message);
      onError && onError();
    }
  }

  // ============================================================
  // STUB API (queues calls until bundle loads)
  // ============================================================
  function createStubAPI() {
    var queue = [];
    
    // Minimal stub that queues all method calls
    window.FashionBotWidget = {
      _q: queue,
      _v: 'loader',
      init: function(config) { queue.push(['init', config]); return this; },
      open: function() { queue.push(['open']); return this; },
      close: function() { queue.push(['close']); return this; },
      toggle: function() { queue.push(['toggle']); return this; },
      destroy: function() { queue.push(['destroy']); return this; },
      on: function(event, fn) { queue.push(['on', event, fn]); return this; },
      off: function(event, fn) { queue.push(['off', event, fn]); return this; }
    };

    log('info', 'Stub API created');
  }

  // ============================================================
  // MAIN LOADER (called deferred - heavy work only)
  // ============================================================
  function main() {
    try {
      var scriptBase = detectBaseUrl();
      var preCfg = window.FashionBotWidgetConfig || {};
      var apiBase = (preCfg.apiBaseUrl && String(preCfg.apiBaseUrl).trim()) ? String(preCfg.apiBaseUrl).replace(/\/$/, '') : scriptBase;

      if (!apiBase) {
        log('error', 'Could not detect API base URL - widget disabled');
        return;
      }

      log('info', 'Loader API base: ' + apiBase + (scriptBase ? ' (script origin: ' + scriptBase + ')' : ''));

      // Fetch config and load bundle
      fetchConfig(apiBase, preCfg, function(config) {
        var publicConfig = window.FashionBotWidgetConfig || {};
        applyServerWidgetConfig(config, publicConfig);
        publicConfig = window.FashionBotWidgetConfig || publicConfig;
        var fallbackVersion = resolveFallbackVersion(config, publicConfig);
        var version = (config && config.version) ? config.version : fallbackVersion;
        var staticBase = (config && config.staticBaseUrl && String(config.staticBaseUrl).trim())
          ? String(config.staticBaseUrl).replace(/\/$/, '')
          : ((publicConfig.staticBaseUrl && String(publicConfig.staticBaseUrl).trim())
            ? String(publicConfig.staticBaseUrl).replace(/\/$/, '')
            : (scriptBase || apiBase));
        window.__FBW_LOADER_CONFIG__ = config || null;
        bootstrapClarity(version, config);

        bootstrapFaro(version, config, {
          scriptBase: scriptBase,
          apiBase: apiBase,
          staticBase: staticBase
        }, function() {
          log('info', 'Loading bundle version: ' + version);

          loadBundle(staticBase, version,
            // Success
            function() {
              log('info', 'Widget ready (' + version + ')');
            },
            // Error - try fallback
            function() {
              if (version !== fallbackVersion) {
                log('info', 'Trying fallback version: ' + fallbackVersion);
                loadBundle(staticBase, fallbackVersion, null, function() {
                  log('error', 'All bundle versions failed to load');
                });
              } else {
                log('error', 'Fallback bundle also failed');
              }
            }
          );
        });
      });

    } catch (e) {
      log('error', 'Loader error: ' + (e.message || e));
    }
  }

  // ============================================================
  // INITIALIZATION
  // ============================================================
  
  // Create stub API IMMEDIATELY (synchronously) so it's available
  // as soon as the loader script finishes executing
  createStubAPI();

  // Auto-queue init if config was set before script loaded
  if (window.FashionBotWidgetConfig) {
    window.FashionBotWidget.init(window.FashionBotWidgetConfig);
  }

  // Defer the heavy lifting (config fetch + bundle load)
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function() {
      defer(function() { main(); });
    });
  } else {
    defer(function() { main(); });
  }

})(window, document);
