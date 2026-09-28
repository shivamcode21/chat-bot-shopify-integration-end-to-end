# Widget CDN & Versioning Strategy

> Industry-standard approach for serving chat widget with instant rollouts and CDN caching.

## 📋 Table of Contents

- [Architecture Overview](#architecture-overview)
- [How It Works](#how-it-works)
- [Files Reference](#files-reference)
- [Deployment Workflow](#deployment-workflow)
- [Fallback Behavior](#fallback-behavior)
- [CDN Setup (Cloudflare)](#cdn-setup-cloudflare)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│  CUSTOMER'S WEBSITE (e.g., groovee.in)                                  │
│                                                                          │
│  <script src="https://your-server/static/chat-widget.js" async></script>│
│  <script>                                                                │
│    window.FashionBotWidgetConfig = { clientName: 'Concept Groove' };    │
│  </script>                                                               │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  LOADER: /static/chat-widget.js                                          │
│  ├── Cache: 5 minutes (short - allows fast rollouts)                    │
│  ├── Size: ~2KB (tiny)                                                  │
│  └── Job: Fetch config → Load versioned bundle                          │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  CONFIG ENDPOINT: /widget/config.json                                    │
│  ├── Cache: 1 minute                                                    │
│  ├── Response: { "version": "v1", "bundle": "chat-widget.v1.js" }       │
│  └── Controlled by: WIDGET_VERSION env var                              │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  BUNDLE: /static/chat-widget.v1.js (or v2, v3, etc.)                    │
│  ├── Cache: 1 year, immutable (CDN-friendly)                            │
│  ├── Size: ~25KB (full widget code)                                     │
│  └── Contains: All widget logic, UI, WebSocket, context detection       │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## How It Works

### Loading Sequence

```
1. Customer page loads chat-widget.js (loader)
2. Loader fetches /widget/config.json
3. Config returns: { "version": "v2" }
4. Loader injects <script src="chat-widget.v2.js">
5. Widget initializes with queued config
```

### Cache Strategy

| File | Cache Duration | Why |
|------|---------------|-----|
| `chat-widget.js` (loader) | 5 minutes | Fast rollouts - change version quickly |
| `/widget/config.json` | 1 minute | Near-instant version switches |
| `chat-widget.v1.js` | 1 year, immutable | CDN edge caching, instant delivery |

### Why This Pattern?

✅ **Instant Rollouts**: Change `WIDGET_VERSION` env var → clients get new code in ~1 min  
✅ **CDN-Friendly**: Versioned bundles cached forever at edge  
✅ **No Customer Changes**: They embed `chat-widget.js` once, never change it  
✅ **Fallback Safety**: If anything fails, loads stable v1  
✅ **A/B Testing Ready**: Can serve different versions to different clients  

---

## Files Reference

### Created/Modified Files

| File | Purpose |
|------|---------|
| `static/chat-widget.js` | Tiny loader (~2KB) - fetches config, loads bundle |
| `static/chat-widget.v1.js` | Full widget bundle (versioned) |
| `fashion_bot/widget_config.py` | API endpoint `/widget/config.json` |
| `fashion_bot/cache_static.py` | Custom StaticFiles with cache headers |
| `scripts/bump-widget-version.sh` | Helper script to create new versions |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `WIDGET_VERSION` | `v1` | Current widget version to serve |

---

## Deployment Workflow

### 🚀 Rolling Out a New Widget Version

```bash
# Step 1: Create new version file
cp static/chat-widget.v1.js static/chat-widget.v2.js

# Step 2: Make your changes
# Edit static/chat-widget.v2.js with bug fixes / features

# Step 3: Test locally
WIDGET_VERSION=v2 uvicorn fashion_bot.agent_controller:app --reload

# Step 4: Deploy to production
# Option A: Set env var on Render/Heroku/etc
#   WIDGET_VERSION=v2

# Option B: Edit fashion_bot/widget_config.py
#   WIDGET_VERSION = os.getenv("WIDGET_VERSION", "v2")

# Step 5: Push and deploy
git add .
git commit -m "Release widget v2"
git push
```

### Using the Helper Script

```bash
# Automatically creates new version and updates references
./scripts/bump-widget-version.sh v2

# Output:
# 📦 Current version: v1
# 🆕 New version: v2
# 📋 Copying chat-widget.v1.js → chat-widget.v2.js
# ✅ Done!
```

### Rollback (if v2 has issues)

```bash
# Just change the env var back
WIDGET_VERSION=v1

# Or edit widget_config.py
WIDGET_VERSION = os.getenv("WIDGET_VERSION", "v1")

# Redeploy - clients get v1 within ~1 minute
```

---

## Fallback Behavior

The loader has multiple safety nets:

```
┌─────────────────────────────────────────────────────────────────┐
│  SCENARIO 1: Config fetch fails (server down, network error)    │
│  ─────────────────────────────────────────────────────────────  │
│  fetch("/widget/config.json") → ❌ FAILS                        │
│                      ↓                                           │
│  loadBundle("v1")  ← Fallback to v1                             │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  SCENARIO 2: Config returns v3, but v3.js doesn't exist         │
│  ─────────────────────────────────────────────────────────────  │
│  fetch("/widget/config.json") → {"version": "v3"}               │
│  loadBundle("v3") → ❌ 404 Not Found                            │
│                      ↓                                           │
│  loadBundle("v1")  ← Fallback to v1                             │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  SCENARIO 3: Everything works ✅                                 │
│  ─────────────────────────────────────────────────────────────  │
│  fetch("/widget/config.json") → {"version": "v2"}               │
│  loadBundle("v2") → ✅ Loaded successfully                      │
└─────────────────────────────────────────────────────────────────┘
```

**⚠️ Important**: Always keep `chat-widget.v1.js` as your stable fallback!

---

## CDN Setup (Cloudflare)

### Option 1: Cloudflare in Front of Render (Recommended)

```
# DNS Setup (in Cloudflare dashboard)
Type: CNAME
Name: cdn (or api)
Target: ecommagents.onrender.com
Proxy: ☁️ ON (Orange cloud - IMPORTANT!)
```

### Option 2: Cache Rules (for aggressive caching)

```
# Cloudflare Dashboard → Rules → Cache Rules

Rule 1: Cache Widget Config (short)
- IF: URI Path equals "/widget/config.json"
- THEN: Cache, Edge TTL = 1 minute

Rule 2: Cache Versioned Bundles (long)
- IF: URI Path matches "/static/chat-widget.v*.js"
- THEN: Cache, Edge TTL = 1 year
```

### Verify CDN is Working

```bash
# First request (MISS)
curl -I https://your-domain.com/static/chat-widget.v1.js
# cf-cache-status: MISS

# Second request (HIT)
curl -I https://your-domain.com/static/chat-widget.v1.js
# cf-cache-status: HIT  ← CDN is working!
```

---

## Testing

### Local Testing

```bash
# Start server
cd /path/to/fashion_bot
uvicorn fashion_bot.agent_controller:app --reload --port 8000

# Test config endpoint
curl http://localhost:8000/widget/config.json
# → {"version":"v1","bundle":"chat-widget.v1.js"}

# Test with different version
WIDGET_VERSION=v2 uvicorn fashion_bot.agent_controller:app --reload
curl http://localhost:8000/widget/config.json
# → {"version":"v2","bundle":"chat-widget.v2.js"}

# Open test page
open http://localhost:8000/test
```

### Production Testing

```bash
# Check config endpoint
curl https://ecommagents.onrender.com/widget/config.json

# Check cache headers on loader
curl -I https://ecommagents.onrender.com/static/chat-widget.js
# Should see: cache-control: public, max-age=300

# Check cache headers on bundle
curl -I https://ecommagents.onrender.com/static/chat-widget.v1.js
# Should see: cache-control: public, max-age=31536000, immutable
```

### Browser Console Testing

```javascript
// Check what version loaded
console.log(window.__FashionBotWidgetBundleSrc);
// → "https://your-server/static/chat-widget.v1.js"

// Check if widget initialized
console.log(window.FashionBotWidget);
```

---

## Troubleshooting

### Widget Not Loading

1. **Check browser console** for errors
2. **Verify config endpoint**: `curl https://your-server/widget/config.json`
3. **Check bundle exists**: `curl -I https://your-server/static/chat-widget.v1.js`

### Old Version Still Loading

1. **Clear browser cache** or hard refresh (Cmd+Shift+R)
2. **Check CDN cache**: May take up to 5 min to propagate
3. **Verify env var**: `echo $WIDGET_VERSION` on server

### CORS Errors

Ensure your server has CORS middleware:
```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
```

### Config Returns Wrong Version

Check `fashion_bot/widget_config.py`:
```python
WIDGET_VERSION = os.getenv("WIDGET_VERSION", "v1")  # ← Check default
```

---

## Quick Reference

```bash
# Create new version
./scripts/bump-widget-version.sh v2

# Or manually:
cp static/chat-widget.v1.js static/chat-widget.v2.js
# Edit v2, then set WIDGET_VERSION=v2 and deploy

# Rollback
WIDGET_VERSION=v1  # Set env var and redeploy

# Check current version (production)
curl https://your-server/widget/config.json

# Check cache headers
curl -I https://your-server/static/chat-widget.v1.js
```

---

## Customer Integration (What They Do)

Customers only need to add this to their website **once**:

```html
<!-- Bloomerce Chat Widget -->
<script>
  window.FashionBotWidgetConfig = {
    clientName: 'Their Store Name'  // Must match database
  };
</script>
<script src="https://your-server/static/chat-widget.js" async></script>
```

**They never need to change this code** - you control versions server-side!

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| v1 | 2025-12-17 | Initial release with CDN strategy |

---

*Last updated: December 17, 2025*

