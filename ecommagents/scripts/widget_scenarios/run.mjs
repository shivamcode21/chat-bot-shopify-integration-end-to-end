/**
 * Scenario suite for the chat widget launcher (v78).
 *
 * Faults are injected with Playwright route interception rather than by
 * changing the server, so the same scenarios run unchanged against a local
 * harness or against a live service:
 *
 *   node run.mjs                                  # against harness on :8795
 *   node run.mjs --base-url http://127.0.0.1:4631 # against the live service
 *   node run.mjs --version v77                    # baseline comparison
 *   node run.mjs --only C4                        # single scenario
 *
 * Exit code is non-zero if any scenario fails.
 */
import { chromium } from 'playwright';
import { readFileSync } from 'fs';

const argv = process.argv.slice(2);
const arg = (name, fallback) => {
  const i = argv.indexOf(`--${name}`);
  return i !== -1 && argv[i + 1] ? argv[i + 1] : fallback;
};
const BASE = (arg('base-url', 'http://127.0.0.1:8795')).replace(/\/$/, '');
const PAGE_PATH = arg('page', '/test');
const ONLY = arg('only', null);
const HEADLESS = !argv.includes('--headed');
// Serve a local bundle in place of the deployed one, so the suite can exercise
// a build the host has not deployed yet.
const INJECT = arg('inject-bundle', null);
const INJECT_AS = arg('inject-as', 'v78');
const INJECT_BODY = INJECT ? readFileSync(INJECT, 'utf8') : null;
// The panel lives in its own document; testing frame-side changes needs it too.
const INJECT_FRAME = arg('inject-frame', null);
const INJECT_FRAME_BODY = INJECT_FRAME ? readFileSync(INJECT_FRAME, 'utf8') : null;
// ngrok's free tier interstitial would otherwise replace the page.
const EXTRA_HEADERS = { 'ngrok-skip-browser-warning': 'true' };

const CONFIG_ROUTES = [
  '**/widget/launcher-theme*',
  '**/widget/logo*',
  '**/widget/desktop-widget-text*',
  '**/widget/mobile-widget-text*',
  '**/widget/launcher-hints*',
];

// Network profiles, as Chrome DevTools defines them.
const NET = {
  slow3g: { offline: false, latency: 400, downloadThroughput: (400 * 1024) / 8, uploadThroughput: (400 * 1024) / 8 },
  fast3g: { offline: false, latency: 150, downloadThroughput: (1.6 * 1024 * 1024) / 8, uploadThroughput: (750 * 1024) / 8 },
  offline: { offline: true, latency: 0, downloadThroughput: 0, uploadThroughput: 0 },
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// A stand-in bundle shaped exactly like the backend's: each section carries the
// same envelope its standalone endpoint returns.
const SECTION_PAYLOADS = {
  'launcher-theme': { enabled: true, launcher_theme: { background: '#0f3d2e', text_color: '#ffffff' } },
  'launcher-hints': { enabled: true, launcher_hints: { default: ['Bundled hint'] } },
  'logo': { enabled: true, widget_logo: {} },
  'mobile-widget-text': { enabled: true, mobile_widget_text: { isDisplay: true, textToDisplay: 'Chat with us' } },
  'desktop-widget-text': { enabled: true, desktop_widget_text: { isDisplay: true } },
  'suggestions-config': { enabled: true, suggestions: {} },
  'button-groups': { enabled: true, button_groups: {} },
  'quick-actions': { enabled: true, quick_actions: {} },
  'geolocation-config': { enabled: true, geolocation_config: { enabled: false } },
  'header-text': { enabled: true, widget_header_text: {} },
  'window-theme': { enabled: true, widget_window_theme: { background_color: '#ffffff', apply_background_color: true } },
  'status-text': { enabled: true, widget_status_text: {} },
  'message-theme': { enabled: true, widget_message_theme: {} },
  'send-button-theme': { enabled: true, widget_send_button_theme: {} },
  'chat-input-theme': { enabled: true, widget_chat_input_theme: {} },
  'product-card-action-theme': { enabled: true, widget_product_card_action_theme: {} },
};
const serveBundle = (sections) => ({
  match: '**/widget/bundle-config/**',
  handler: (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    headers: { 'Cache-Control': 'public, max-age=60' },
    body: JSON.stringify({ bundle_version: 1, client_id: 'scenario', incomplete_sections: [],
      sections: Object.fromEntries(Object.entries(SECTION_PAYLOADS).filter(([k]) => !sections || sections.includes(k))) }),
  }),
});
// The widget only asks for a bundle when /widget/config.json advertises one,
// so a backend that serves it must say so.
// Whether the backend serves a bundle decides which code path the widget takes,
// so the suite must set it rather than inherit whatever happens to be deployed.
// Without this, scenarios that inject faults into the per-section endpoints go
// quietly untested the moment the bundle ships.
const denyBundle = {
  match: '**/widget/config.json*',
  handler: async (route) => {
    const res = await route.fetch();
    let body;
    try { body = await res.json(); } catch { body = {}; }
    delete body.bundleConfig;
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  },
};

const advertiseBundle = {
  match: '**/widget/config.json*',
  handler: async (route) => {
    const res = await route.fetch();
    let body;
    try { body = await res.json(); } catch { body = {}; }
    body.bundleConfig = true;
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  },
};

/** Runs one scenario and returns everything observed. */
async function observe(browser, { viewport, throttle, routes = [], hover = false, clickAfterHover = false, watchMs = 20000, bundleAdvertised = false }) {
  const context = await browser.newContext({ viewport: viewport || { width: 1280, height: 900 }, extraHTTPHeaders: EXTRA_HEADERS });
  const page = await context.newPage();

  if (throttle) {
    const cdp = await context.newCDPSession(page);
    await cdp.send('Network.enable');
    await cdp.send('Network.emulateNetworkConditions', { ...NET[throttle] });
  }

  // Stub Faro so pushEvent calls are captured rather than shipped.
  await page.addInitScript(() => {
    window.__faroEvents = [];
    window.GrafanaFaroWebSdk = {
      faro: { api: { pushEvent: (name, attrs) => window.__faroEvents.push({ name, attrs }) } },
    };
  });

  let injectedBundleServed = false;
  if (INJECT_BODY) {
    await page.route(`**/chat-widget.${INJECT_AS}.js*`, (route) => {
      injectedBundleServed = true;
      return route.fulfill({ status: 200, contentType: 'application/javascript', body: INJECT_BODY });
    });
  }
  if (!bundleAdvertised) await page.route(denyBundle.match, denyBundle.handler);
  if (INJECT_FRAME_BODY) {
    await page.route('**/chat-widget-frame.html*', (route) =>
      route.fulfill({ status: 200, contentType: 'text/html', body: INJECT_FRAME_BODY }));
  }
  for (const r of routes) await page.route(r.match, r.handler);

  const t0 = Date.now();
  const net = [];
  const consoleErrors = [];
  const sockets = [];
  page.on('websocket', (ws) => sockets.push({ t: Date.now() - t0, url: ws.url() }));
  page.on('requestfailed', (r) => {
    if (/\/widget\/|frame\.html/.test(r.url()))
      net.push({
        t: Date.now() - t0, kind: 'failed', url: shortUrl(r.url()),
        error: r.failure()?.errorText,
        fromFrame: (r.frame()?.url() || '').includes('frame.html'),
      });
  });
  page.on('request', (r) => {
    if (/\/widget\/|frame\.html/.test(r.url())) net.push({ t: Date.now() - t0, kind: 'request', url: shortUrl(r.url()) });
  });
  page.on('console', (m) => { if (/FashionBot/.test(m.text())) consoleErrors.push(m.text()); });

  await page.goto(BASE + PAGE_PATH, { waitUntil: 'commit' }).catch(() => {});

  // Sample the launcher's visible state densely enough to catch a repaint.
  const samples = [];
  const deadline = Date.now() + watchMs;
  let hovered = false;
  while (Date.now() < deadline) {
    const s = await page.evaluate(() => {
      const b = document.getElementById('fbw-button');
      if (!b) return { present: false };
      const pv = b.querySelector('.fbw-preview');
      const cs = pv ? getComputedStyle(pv) : null;
      return {
        present: true,
        visible: getComputedStyle(b).visibility === 'visible',
        text: (b.querySelector('.fbw-stream-text') || {}).textContent || '',
        iconOnly: b.classList.contains('fbw-icon-only'),
        style: cs ? `${cs.backgroundImage}|${cs.backgroundColor}|${cs.boxShadow}` : '',
      };
    }).catch(() => ({ present: false }));
    samples.push({ t: Date.now() - t0, ...s });

    if (hover && !hovered && s.present && s.visible) {
      hovered = true;
      await page.hover('#fbw-button').catch(() => {});
      samples[samples.length - 1].hoverFired = true;
    }
    await sleep(50);
  }

  if (clickAfterHover) {
    await page.click('#fbw-button').catch(() => {});
    await sleep(6000);
  }
  const faroEvents = await page.evaluate(() => window.__faroEvents || []).catch(() => []);
  const finalState = await page.evaluate(() => {
    const b = document.getElementById('fbw-button');
    const f = document.getElementById('fbw-iframe');
    const w = document.getElementById('fbw-wrapper');
    return {
      launcherVisible: b ? getComputedStyle(b).visibility === 'visible' : false,
      iframeExists: !!f,
      wrapperClass: w ? w.className : '',
    };
  }).catch(() => ({}));

  await context.close();
  return { samples, faroEvents, net, consoleErrors, finalState, sockets, injectedBundleServed, t0 };
}

function shortUrl(u) {
  return u.replace(BASE, '').split('?')[0];
}

/* ---------- assertions over an observation ---------- */

const visibleSamples = (o) => o.samples.filter((s) => s.present && s.visible);
const firstVisibleAt = (o) => (visibleSamples(o)[0] || {}).t ?? null;

/** The invariant behind bug 1: the first frame the shopper sees is the last. */
function stylingRepaints(o) {
  const styles = [...new Set(visibleSamples(o).map((s) => `${s.style}|icon:${s.iconOnly}`))];
  return styles.length ? styles.length - 1 : 0;
}
const everShowedPlaceholder = (o) => visibleSamples(o).some((s) => /Loading/i.test(s.text));
const criticalEvents = (o) => o.faroEvents.filter((e) => e.name === 'widget_launcher_config_failed');
const cancelled = (o) => o.net.filter((n) => n.kind === 'failed');
const panelConfigAborts = (o) => cancelled(o).filter((n) => n.fromFrame);
// An abort that a retry then recovers is the policy working. What matters is a
// section that was abandoned: aborted with no later successful attempt.
const abandonedPanelSections = (o) => {
  const byUrl = new Map();
  for (const n of o.net) {
    if (!/\/widget\//.test(n.url)) continue;
    const rec = byUrl.get(n.url) || { requests: 0, aborts: 0 };
    if (n.kind === 'request') rec.requests += 1;
    if (n.kind === 'failed') rec.aborts += 1;
    byUrl.set(n.url, rec);
  }
  return [...byUrl.entries()].filter(([, r]) => r.aborts > 0 && r.requests <= r.aborts).map(([u]) => u);
};
const socketsBefore = (o, t) => o.sockets.filter((w) => w.t <= t).length;
const requestsTo = (o, re) => o.net.filter((n) => n.kind === 'request' && re.test(n.url)).length;
const SECTION_URLS = /\/widget\/(launcher-theme|launcher-hints|logo|mobile-widget-text|desktop-widget-text|suggestions-config|button-groups|quick-actions|geolocation-config|header-text|window-theme|status-text|message-theme|send-button-theme|chat-input-theme|product-card-action-theme)$/;

/* ---------- fault injectors ---------- */

const abortLauncherConfig = () => CONFIG_ROUTES.map((m) => ({ match: m, handler: (route) => route.abort('failed') }));
const hangOne = (m) => ({ match: m, handler: async (route) => { await sleep(60000); route.abort(); } });
let failOneAttempts = 0;
const failOne = (m, status = 500) => ({
  match: m,
  handler: (route) => { failOneAttempts++; return route.fulfill({ status, contentType: 'application/json', body: '{}' }); },
});
const delayAll = (ms) => CONFIG_ROUTES.map((m) => ({ match: m, handler: async (route) => { await sleep(ms); route.continue(); } }));
/** Fails only the first attempt of each endpoint, lets the retry through. */
function flakyFirstAttempt() {
  const seen = new Set();
  return CONFIG_ROUTES.map((m) => ({
    match: m,
    handler: async (route) => {
      const key = route.request().url().split('?')[0];
      if (!seen.has(key)) { seen.add(key); return route.abort('failed'); }
      return route.continue();
    },
  }));
}

/* ---------- the scenarios ---------- */

const SCENARIOS = [
  {
    id: 'C1', name: 'Healthy config, fast network',
    opts: { watchMs: 8000 },
    expect: (o) => [
      ['launcher becomes visible', o.finalState.launcherVisible],
      ['no "Loading…" ever shown', !everShowedPlaceholder(o)],
      ['zero styling repaints once visible', stylingRepaints(o) === 0],
      ['no critical alert', criticalEvents(o).length === 0],
    ],
  },
  {
    id: 'C2', name: 'Every endpoint 200 {enabled:false} (real default flags)',
    opts: { watchMs: 8000, routes: CONFIG_ROUTES.map((m) => ({ match: m, handler: (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ enabled: false, launcher_hints: {}, launcher_theme: {}, widget_logo: {}, desktop_widget_text: {}, mobile_widget_text: {} }) }) })) },
    expect: (o) => [
      ['launcher still shows (disabled != failed)', o.finalState.launcherVisible],
      ['no critical alert', criticalEvents(o).length === 0],
      ['no "Loading…" ever shown', !everShowedPlaceholder(o)],
    ],
  },
  {
    id: 'C3', name: 'One endpoint returns HTTP 500',
    before: () => { failOneAttempts = 0; },
    opts: { watchMs: 20000, routes: [failOne('**/widget/launcher-theme*')] },
    expect: (o) => [
      ['launcher never shown', !o.finalState.launcherVisible],
      ['exactly one critical alert', criticalEvents(o).length === 1],
      ['alert names launcher-theme', (criticalEvents(o)[0]?.attrs.failed_endpoints || '').includes('launcher-theme')],
      ['alert marks widget_rendered=false', criticalEvents(o)[0]?.attrs.widget_rendered === 'false'],
      ['the 500 was retried once (2 attempts)', failOneAttempts === 2],
    ],
  },
  {
    id: 'C4', name: 'One endpoint hangs forever (5s + 10s retry)',
    opts: { watchMs: 20000, routes: [hangOne('**/widget/launcher-theme*')] },
    expect: (o) => [
      ['launcher never shown', !o.finalState.launcherVisible],
      ['exactly one critical alert', criticalEvents(o).length === 1],
      ['two attempts were made', cancelled(o).filter((c) => c.url.includes('launcher-theme')).length === 2],
      ['gave up at ~15s', Number(criticalEvents(o)[0]?.attrs.max_elapsed_ms || 0) >= 14500 && Number(criticalEvents(o)[0]?.attrs.max_elapsed_ms || 0) <= 16500],
      ['reason recorded as timeout', /timeout/.test(criticalEvents(o)[0]?.attrs.detail || '')],
    ],
  },
  {
    id: 'C5', name: 'First attempt fails, retry succeeds',
    opts: { watchMs: 20000, routes: flakyFirstAttempt() },
    expect: (o) => [
      ['launcher shows via the retry', o.finalState.launcherVisible],
      ['no critical alert', criticalEvents(o).length === 0],
      ['no "Loading…" ever shown', !everShowedPlaceholder(o)],
      ['zero styling repaints once visible', stylingRepaints(o) === 0],
    ],
  },
  {
    id: 'C6', name: 'Every config endpoint dead',
    opts: { watchMs: 22000, routes: abortLauncherConfig() },
    expect: (o) => [
      ['launcher never shown', !o.finalState.launcherVisible],
      ['exactly one critical alert', criticalEvents(o).length === 1],
      ['alert lists more than one endpoint', (criticalEvents(o)[0]?.attrs.failed_endpoints || '').split(',').length > 1],
    ],
  },
  {
    id: 'C7', name: 'Config slow but within budget (3s)',
    opts: { watchMs: 22000, routes: delayAll(3000) },
    expect: (o) => [
      ['launcher shows', o.finalState.launcherVisible],
      ['no critical alert', criticalEvents(o).length === 0],
      ['nothing cancelled', cancelled(o).length === 0],
      ['zero styling repaints once visible', stylingRepaints(o) === 0],
    ],
  },
  {
    id: 'N1', name: 'Slow 3G, healthy config',
    opts: { watchMs: 25000, throttle: 'slow3g' },
    expect: (o) => [
      ['launcher shows within budget', o.finalState.launcherVisible],
      ['no "Loading…" ever shown', !everShowedPlaceholder(o)],
      ['zero styling repaints once visible', stylingRepaints(o) === 0],
    ],
  },
  {
    id: 'N2', name: 'Offline from the start',
    allowFallbackBundle: true,
    opts: { watchMs: 22000, throttle: 'offline' },
    expect: (o) => [
      ['launcher never shown', !o.finalState.launcherVisible],
    ],
  },
  {
    id: 'M1', name: 'Mobile viewport (390x844), healthy config',
    opts: { watchMs: 10000, viewport: { width: 390, height: 844 } },
    expect: (o) => [
      ['launcher shows', o.finalState.launcherVisible],
      ['no "Loading…" ever shown', !everShowedPlaceholder(o)],
      ['zero styling repaints once visible', stylingRepaints(o) === 0],
    ],
  },
  {
    id: 'P1', name: 'Panel prewarm on hover',
    opts: { watchMs: 9000, hover: true },
    expect: (o) => [
      ['iframe created before any click', o.finalState.iframeExists],
      ['wrapper held in prewarm (hidden)', /fbw-prewarm/.test(o.finalState.wrapperClass)],
      ['no critical alert', criticalEvents(o).length === 0],
    ],
  },
  {
    id: 'P2', name: 'Prewarm on slow 3G (watchdog teardown watch)',
    opts: { watchMs: 25000, hover: true, throttle: 'slow3g' },
    expect: (o) => [
      ['frame document not cancelled by the ready-watchdog',
        cancelled(o).filter((c) => /frame\.html/.test(c.url)).length === 0],
    ],
  },
  {
    id: 'F1', name: 'Panel config survives latency (fix 1: 5s + retry in the frame)',
    opts: { watchMs: 22000, hover: true, throttle: 'fast3g' },
    expect: (o) => [
      ['no panel config section abandoned (an abort recovered by its retry is the policy working)',
        abandonedPanelSections(o).length === 0],
    ],
  },
  {
    id: 'F2', name: 'Prewarm does not arm the ready-watchdog (fix 2)',
    opts: { watchMs: 26000, hover: true, throttle: 'slow3g' },
    expect: (o) => [
      ['frame document never torn down', cancelled(o).filter((c) => /frame\.html/.test(c.url)).length === 0],
      ['frame requested exactly once', o.net.filter((n) => n.kind === 'request' && /frame\.html/.test(n.url)).length === 1],
    ],
  },
  {
    id: 'F3', name: 'Hover warms the panel but opens no socket (fix 3)',
    opts: { watchMs: 13000, hover: true, clickAfterHover: true },
    expect: (o) => [
      ['iframe warmed on hover', o.finalState.iframeExists],
      ['no websocket opened while only hovering', socketsBefore(o, 13000) === 0],
      ['websocket opens after the click', o.sockets.length >= 1],
    ],
  },
  {
    id: 'B1', name: 'Backend without the bundle still works (fallback)',
    opts: { watchMs: 14000, hover: true },
    expect: (o) => [
      ['launcher shows', o.finalState.launcherVisible],
      ['no critical alert', criticalEvents(o).length === 0],
      ['no bundle was requested (backend does not advertise one)', requestsTo(o, /bundle-config/) === 0],
      ['used the individual endpoints', requestsTo(o, SECTION_URLS) > 0],
      ['zero styling repaints once visible', stylingRepaints(o) === 0],
    ],
  },
  {
    id: 'B2', name: 'Bundle present: one request replaces sixteen',
    opts: { watchMs: 14000, hover: true, bundleAdvertised: true, routes: [advertiseBundle, serveBundle(null)] },
    expect: (o) => [
      ['launcher shows', o.finalState.launcherVisible],
      ['no individual config endpoint was called', requestsTo(o, SECTION_URLS) === 0],
      ['bundle fetched', requestsTo(o, /bundle-config/) >= 1],
      ['no critical alert', criticalEvents(o).length === 0],
      ['zero styling repaints once visible', stylingRepaints(o) === 0],
    ],
  },
  {
    id: 'B3', name: 'Bundle missing a section: that one falls back alone',
    opts: { watchMs: 16000, hover: true, bundleAdvertised: true, routes: [advertiseBundle, serveBundle(Object.keys(SECTION_PAYLOADS).filter((k) => k !== 'window-theme'))] },
    expect: (o) => [
      ['launcher shows', o.finalState.launcherVisible],
      ['only the absent section was fetched individually',
        requestsTo(o, /\/widget\/window-theme$/) === 1 && requestsTo(o, SECTION_URLS) === 1],
      ['no critical alert', criticalEvents(o).length === 0],
    ],
  },
  {
    id: 'B4', name: 'Bundle endpoint 500s: falls back to the sections, widget still shows',
    opts: { watchMs: 16000, hover: true, bundleAdvertised: true,
            routes: [advertiseBundle, { match: '**/widget/bundle-config/**', handler: (route) => route.fulfill({ status: 500, body: '{}' }) }] },
    expect: (o) => [
      ['launcher shows', o.finalState.launcherVisible],
      ['fell back to the individual endpoints', requestsTo(o, SECTION_URLS) > 0],
      ['no critical alert', criticalEvents(o).length === 0],
      ['zero styling repaints once visible', stylingRepaints(o) === 0],
    ],
  },
  {
    id: 'B5', name: 'Bundle hangs AND the sections are dead: hidden + alerted',
    opts: { watchMs: 26000, bundleAdvertised: true,
            routes: [advertiseBundle, hangOne('**/widget/bundle-config/**'), ...CONFIG_ROUTES.map((m) => ({ match: m, handler: (route) => route.abort('failed') }))] },
    expect: (o) => [
      ['launcher never shown', !o.finalState.launcherVisible],
      ['exactly one critical alert', criticalEvents(o).length === 1],
    ],
  },
];

/* ---------- runner ---------- */

const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium', headless: HEADLESS });
let failures = 0;
console.log(`base-url: ${BASE}${PAGE_PATH}\n`);

// A cold connection makes the loader's 3s config fetch time out, which drops it
// to its fallback bundle. Warm the path first so scenarios test what they mean to.
for (let i = 0; i < 3; i++) {
  try { await fetch(`${BASE}/widget/config.json`, { headers: { 'ngrok-skip-browser-warning': 'true' } }); } catch {}
}

for (const sc of SCENARIOS) {
  if (ONLY && sc.id !== ONLY) continue;
  process.stdout.write(`${sc.id}  ${sc.name}\n`);
  try { await fetch(`${BASE}/widget/config.json`, { headers: { 'ngrok-skip-browser-warning': 'true' } }); } catch {}
  if (sc.before) sc.before();
  let o;
  try {
    o = await observe(browser, sc.opts);
  } catch (e) {
    console.log(`    ERROR ${e.message}\n`);
    failures++;
    continue;
  }
  // The loader falls back to an older bundle when /widget/config.json is slow,
  // which silently turns every assertion below into a test of the wrong code.
  if (INJECT_BODY && !o.injectedBundleServed && !sc.allowFallbackBundle) {
    console.log(`    ERROR the loader never requested chat-widget.${INJECT_AS}.js - it fell back to`);
    console.log(`          another version, so this scenario did not test the injected bundle.\n`);
    failures++;
    continue;
  }
  for (const [what, ok] of sc.expect(o)) {
    console.log(`    ${ok ? 'PASS' : 'FAIL'}  ${what}`);
    if (!ok) failures++;
  }
  const tVis = firstVisibleAt(o);
  console.log(`    ~ first visible: ${tVis === null ? 'never' : tVis + 'ms'}` +
    `, repaints: ${stylingRepaints(o)}` +
    `, cancelled: ${cancelled(o).length}` +
    `, alerts: ${criticalEvents(o).length}`);
  if (process.env.SHOW_CONFIG_REQS) {
    console.log('    ~ config requests: ' + (o.net.filter((n) => n.kind === 'request' && /\/widget\//.test(n.url)).map((n) => n.url).join(', ') || 'none'));
  }
  const alert = criticalEvents(o)[0];
  if (alert) console.log(`    ~ alert: ${alert.attrs.detail}`);
  console.log('');
}

await browser.close();
console.log(failures ? `${failures} check(s) FAILED` : 'all checks passed');
process.exit(failures ? 1 : 0);
