# Gupshup Template Integration - Final Setup Guide

## ✅ What Was Fixed

The system now uses the **correct Gupshup API endpoint**:
```
https://api.gupshup.io/wa/app/{APP_ID}/template/{TEMPLATE_ID}
```

This endpoint:
- ✅ Returns template definition with `data` field containing template text
- ✅ Works with your API key (no 401 error)
- ✅ Provides exact template text for rendering

## 🚀 Quick Setup (3 Steps)

### Step 1: Add APP_ID to Configuration

Run the configuration manager:
```bash
cd /Users/shivammehrotra/git-bot/ecommagents/fashion_bot
python update_gupshup_config.py
```

Choose option 2, then enter your Gupshup APP_ID (e.g., `80c17b30-b4f9-442e-a889-0aa1761ca8e3`)

**Alternative (Direct SQL)**:
```sql
-- View current config
SELECT config_value 
FROM client_configs 
WHERE config_key = 'gupshup_template_details';

-- Update to add APP_ID
UPDATE client_configs
SET config_value = jsonb_set(
    config_value::jsonb,
    '{APP_ID}',
    '"80c17b30-b4f9-442e-a889-0aa1761ca8e3"'::jsonb
)
WHERE config_key = 'gupshup_template_details';
```

### Step 2: Verify Configuration

```sql
SELECT config_value->'APP_ID' as app_id,
       config_value->'GUPSHUP_TEMPLATE_API_KEY' as api_key
FROM client_configs 
WHERE config_key = 'gupshup_template_details';
```

Should show both APP_ID and API key.

### Step 3: Test Template Fetching

Trigger a webhook (Shopify order or Shiprocket shipment update) and check logs:

```bash
tail -f fashion_bot/logs/fashion_bot_meta_graph.log
```

## 📊 What You'll See in Logs

### Successful Template Fetch & Render

```
================================================================================
[SHOPIFY] 📤 Sending WhatsApp template message
[SHOPIFY] To: +919876543210
[SHOPIFY] Template ID: 4ba97662-f301-40c2-833e-98ed3f33a43e
[SHOPIFY] Parameters: ['John Doe', 'ORD12345']
================================================================================

[GUPSHUP_API] 📡 Fetching template from: https://api.gupshup.io/wa/app/80c17b30-.../template/4ba97662...
[GUPSHUP_API] Response status: 200
[GUPSHUP_API] ✅ Template fetched successfully
[GUPSHUP_API] Template Name: concept_groove_order_confirmation
[GUPSHUP_API] Template Text: Concept Groove
Hi {{1}}, your order {{2}} is confirmed. ✨...

[TEMPLATE_RENDER] 🔄 Replacing placeholders...
[TEMPLATE_RENDER] Template: Concept Groove
Hi {{1}}, your order {{2}} is confirmed. ✨
We're packing it up — you'll hear from us once it's on the move.
[TEMPLATE_RENDER] Parameters: ['John Doe', 'ORD12345']
[TEMPLATE_RENDER] Replaced {{1}} → John Doe
[TEMPLATE_RENDER] Replaced {{2}} → ORD12345
[TEMPLATE_RENDER] ✅ Template rendered successfully
[TEMPLATE_RENDER] 📝 Rendered message: Concept Groove
Hi John Doe, your order ORD12345 is confirmed. ✨
We're packing it up — you'll hear from us once it's on the move.

[SHOPIFY] ✅ FINAL MESSAGE TO BE SENT:
────────────────────────────────────────────────────────────────────────────────
Concept Groove
Hi John Doe, your order ORD12345 is confirmed. ✨
We're packing it up — you'll hear from us once it's on the move.
────────────────────────────────────────────────────────────────────────────────
```

### Database Entry

```sql
SELECT 
    template_name,
    template_message,
    template_params,
    success
FROM template_delivery_logs
ORDER BY sent_at DESC
LIMIT 1;
```

**Result**:
| template_name | template_message | template_params | success |
|---------------|------------------|-----------------|---------|
| concept_groove_order_confirmation | Concept Groove\nHi John Doe, your order ORD12345 is confirmed. ✨\nWe're packing it up — you'll hear from us once it's on the move. | {"params": ["John Doe", "ORD12345"], "param_count": 2, "image_url": "..."} | true |

## 🔍 API Response Format

The Gupshup API returns:

```json
{
    "status": "success",
    "template": {
        "id": "5f4138e7-61c1-4210-ad52-771505e33ca9",
        "elementName": "concept_groove_order_confirmation",
        "data": "Concept Groove\nHi {{1}}, your order {{2}} is confirmed. ✨\nWe're packing it up — you'll hear from us once it's on the move.",
        "templateType": "IMAGE",
        "meta": "{...}"
    }
}
```

The system:
1. Extracts `data` field → Contains template text with {{1}}, {{2}} placeholders
2. Replaces {{1}} with first param, {{2}} with second param, etc.
3. Logs complete rendered message to database
4. Sends via WhatsApp

## ✨ Key Features

### 1. Clear Logging
Every template send shows:
- Phone number
- Template ID
- Parameters
- Template text before rendering
- Placeholder replacement process
- **Final rendered message** (exactly what customer receives)

### 2. Database Storage
- `template_message`: Complete rendered message text
- `template_params`: JSON with params array, count, and image URL
- Full audit trail of every message sent

### 3. Caching
- Templates cached for 1 hour
- Reduces API calls
- Improves performance

### 4. Error Handling
- Clear error messages
- Graceful degradation (templates still send even if render fails)
- Detailed logging for debugging

## 🧪 Testing

### Test Template Fetch
```python
from fashion_bot.utils.gupshup_api_client import fetch_template_from_gupshup

template = fetch_template_from_gupshup('4ba97662-f301-40c2-833e-98ed3f33a43e')

if template:
    print("✅ Template fetched!")
    print(f"Name: {template.get('elementName')}")
    print(f"Text: {template.get('data')}")
else:
    print("❌ Failed to fetch template")
```

### Test Rendering
```python
from fashion_bot.utils.gupshup_api_client import render_template_message

template = {'data': 'Hi {{1}}, order {{2}} confirmed!'}
params = ['John', 'ORD123']

message = render_template_message(template, params)
print(f"Rendered: {message}")
# Output: "Hi John, order ORD123 confirmed!"
```

### Test End-to-End
Send a test order or trigger a webhook, then:

```sql
-- Check latest sent template
SELECT 
    phone_number,
    template_name,
    template_message,
    success,
    sent_at
FROM template_delivery_logs
WHERE sent_at > NOW() - INTERVAL '1 hour'
ORDER BY sent_at DESC
LIMIT 5;
```

## ❌ Troubleshooting

### Error: Missing APP_ID
```
[GUPSHUP_API] Missing configuration - API Key: ✓, APP_ID: ✗
```
**Solution**: Run `python update_gupshup_config.py` and add your APP_ID

### Error: 404 Not Found
```
[GUPSHUP_API] ❌ Template not found or APP_ID is incorrect
```
**Solution**: 
- Verify APP_ID is correct in database
- Verify template_id exists in your Gupshup account

### Error: 401 Authentication Failed
```
[GUPSHUP_API] ❌ Authentication failed - check API key
```
**Solution**: Verify GUPSHUP_TEMPLATE_API_KEY in database is correct

### Template not rendering
Check logs for:
```
[TEMPLATE_RENDER] ⚠️  No template text found in 'data' field
```
**Solution**: Template was fetched but doesn't have expected format. Check API response structure.

## 📝 Configuration Checklist

- [ ] APP_ID added to `gupshup_template_details` config
- [ ] API key present and correct
- [ ] Test template fetch successful
- [ ] Webhook triggered and processed
- [ ] Rendered message appears in logs
- [ ] `template_message` column populated in database

## 🎯 What Happens Now

For every Shopify order or Shiprocket shipment update:

1. ✅ Webhook received
2. ✅ Template ID and params determined
3. ✅ API call to `https://api.gupshup.io/wa/app/{APP_ID}/template/{TEMPLATE_ID}`
4. ✅ Template text extracted from `data` field
5. ✅ Placeholders {{1}}, {{2}}, etc. replaced with actual values
6. ✅ Complete message logged clearly in logs
7. ✅ Template sent via WhatsApp
8. ✅ Rendered message + params saved to database

All with **clear, detailed logging** showing exactly what message is sent to each customer!

## 🚀 You're Done!

Run the config script, add your APP_ID, and you'll immediately start seeing:
- ✅ Templates fetching successfully
- ✅ Clear logs showing rendered messages
- ✅ Complete message text in database
- ✅ Full audit trail of customer communications

**No more 401 errors! Everything just works!** 🎉

