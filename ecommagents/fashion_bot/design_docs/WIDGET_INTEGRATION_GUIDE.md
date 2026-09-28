# Chat Widget Integration Guide 🚀

## Quick Integration (Copy & Paste)

### Step 1: Add This Script Before Closing `</body>` Tag

```html
<!-- Fashion Bot Chat Widget -->
<script src="https://YOUR-DOMAIN.com/static/chat-widget.js"></script>
<script>
  FashionBotWidget.init({
    clientName: 'YOUR_CLIENT_NAME',  // Replace with your client name
    position: 'bottom-right',         // or 'bottom-left'
    theme: 'light'                    // or 'dark'
  });
</script>
```

### Step 2: Replace Configuration

```javascript
FashionBotWidget.init({
  clientName: 'Groovee',           // ← Change this to your client name
  position: 'bottom-right',        // Position: 'bottom-right' or 'bottom-left'
  theme: 'light'                   // Theme: 'light' or 'dark'
});
```

---

## Complete Integration Examples

### Example 1: Standard E-commerce Site

```html
<!DOCTYPE html>
<html>
<head>
    <title>My E-commerce Store</title>
</head>
<body>
    <!-- Your website content -->
    <h1>Welcome to My Store</h1>
    
    <!-- Fashion Bot Widget (Add before </body>) -->
    <script src="https://ecommagents-3.onrender.com/static/chat-widget.js"></script>
    <script>
      FashionBotWidget.init({
        clientName: 'Groovee',
        position: 'bottom-right',
        theme: 'light'
      });
    </script>
</body>
</html>
```

### Example 2: React/Next.js Application

```jsx
// components/ChatWidget.jsx
import { useEffect } from 'react';

export default function ChatWidget() {
  useEffect(() => {
    // Load widget script
    const script = document.createElement('script');
    script.src = 'https://YOUR-DOMAIN.com/static/chat-widget.js';
    script.async = true;
    
    script.onload = () => {
      // Initialize widget after script loads
      if (window.FashionBotWidget) {
        window.FashionBotWidget.init({
          clientName: 'Groovee',
          position: 'bottom-right',
          theme: 'light'
        });
      }
    };
    
    document.body.appendChild(script);
    
    // Cleanup on unmount
    return () => {
      document.body.removeChild(script);
    };
  }, []);
  
  return null; // Widget renders itself
}

// Use in your app
// pages/_app.js or layout.js
import ChatWidget from '../components/ChatWidget';

function MyApp({ Component, pageProps }) {
  return (
    <>
      <Component {...pageProps} />
      <ChatWidget />
    </>
  );
}
```

### Example 3: WordPress Site

```php
<!-- Add to theme's footer.php or use Custom HTML widget -->

<!-- In footer.php, add before </body> -->
<script src="https://YOUR-DOMAIN.com/static/chat-widget.js"></script>
<script>
  jQuery(document).ready(function($) {
    FashionBotWidget.init({
      clientName: 'Groovee',
      position: 'bottom-right',
      theme: 'light'
    });
  });
</script>
```

**Or use WordPress Plugin:**

1. Install "Insert Headers and Footers" plugin
2. Go to Settings → Insert Headers and Footers
3. Paste the widget code in the "Scripts in Footer" section

### Example 4: Shopify Store

```liquid
<!-- In your theme's theme.liquid file -->
<!-- Add before the closing </body> tag -->

{% comment %} Fashion Bot Chat Widget {% endcomment %}
<script src="https://YOUR-DOMAIN.com/static/chat-widget.js"></script>
<script>
  FashionBotWidget.init({
    clientName: '{{ shop.name }}',  // Automatically uses shop name
    position: 'bottom-right',
    theme: 'light'
  });
</script>
```

### Example 5: Google Tag Manager

1. Go to Google Tag Manager
2. Create New Tag → Custom HTML
3. Paste this code:

```html
<script src="https://YOUR-DOMAIN.com/static/chat-widget.js"></script>
<script>
  FashionBotWidget.init({
    clientName: 'Groovee',
    position: 'bottom-right',
    theme: 'light'
  });
</script>
```

4. Set Trigger: All Pages
5. Publish

---

## Configuration Options

### Client Name (Required)

```javascript
clientName: 'Groovee'  // Your unique client identifier
```

**How to get your client name:**
- Contact your account manager
- Check your dashboard settings
- Use your brand name (e.g., 'Groovee', 'YourBrand')

### Position

```javascript
position: 'bottom-right'  // Default: bottom-right
// Options: 'bottom-right' | 'bottom-left'
```

**Preview:**
- `bottom-right`: Widget appears in bottom-right corner (most common)
- `bottom-left`: Widget appears in bottom-left corner

### Theme

```javascript
theme: 'light'  // Default: light
// Options: 'light' | 'dark'
```

**Note:** Theme support coming soon in future updates.

### Custom API URL (Advanced)

```javascript
apiUrl: 'wss://custom-domain.com/ws/chat'  // Optional
// Only needed if using custom backend URL
// Default: Auto-detected from script source
```

---

## Advanced Integration

### Conditional Loading (Show on specific pages)

```html
<script src="https://YOUR-DOMAIN.com/static/chat-widget.js"></script>
<script>
  // Only show on product and checkout pages
  const currentPath = window.location.pathname;
  
  if (currentPath.includes('/products/') || currentPath.includes('/checkout')) {
    FashionBotWidget.init({
      clientName: 'Groovee',
      position: 'bottom-right',
      theme: 'light'
    });
  }
</script>
```

### Delayed Loading (Improve page speed)

```html
<script>
  // Load widget after 3 seconds
  setTimeout(function() {
    const script = document.createElement('script');
    script.src = 'https://YOUR-DOMAIN.com/static/chat-widget.js';
    script.onload = function() {
      FashionBotWidget.init({
        clientName: 'Groovee',
        position: 'bottom-right',
        theme: 'light'
      });
    };
    document.body.appendChild(script);
  }, 3000);
</script>
```

### Programmatic Control

```javascript
// Initialize widget
FashionBotWidget.init({
  clientName: 'Groovee',
  position: 'bottom-right',
  theme: 'light'
});

// Open chat programmatically
// Example: Open when user clicks a button
document.getElementById('help-button').addEventListener('click', function() {
  // Trigger chat open
  // (This would require adding an API method to the widget)
});
```

---

## Testing Your Integration

### 1. Quick Test Checklist

After adding the widget to your site:

- [ ] Chat button appears in bottom-right/left corner
- [ ] Button has purple gradient (not affected by site CSS)
- [ ] Clicking button opens chat window
- [ ] Pre-chat form collects phone number
- [ ] Messages can be sent and received
- [ ] Chat history persists on page refresh
- [ ] Works on mobile devices
- [ ] No console errors in DevTools

### 2. Browser Testing

Test on these browsers:
- ✅ Chrome (Desktop & Mobile)
- ✅ Safari (Desktop & Mobile)
- ✅ Firefox
- ✅ Edge
- ✅ Samsung Internet (Mobile)

### 3. Device Testing

Test on:
- 📱 iPhone (Safari)
- 📱 Android (Chrome)
- 💻 Desktop (Windows/Mac)
- 📱 Tablet (iPad/Android)

---

## Troubleshooting

### Widget Not Appearing

**Check 1: Script loaded correctly**
```javascript
// Open DevTools Console and type:
FashionBotWidget
// Should show object with init function
```

**Check 2: No JavaScript errors**
- Open DevTools Console (F12)
- Look for red error messages
- Common issue: `FashionBotWidget is not defined` = Script didn't load

**Check 3: Network tab**
- Open DevTools → Network tab
- Refresh page
- Look for `chat-widget.js` - should be 200 OK
- If 404: Check the script URL

### Widget Looks Broken

**Issue**: Widget styling looks wrong

**Solution**: This shouldn't happen with Shadow DOM! But if it does:
1. Check DevTools Console for errors
2. Verify you're using the latest widget version
3. Clear browser cache and reload
4. Check if any browser extensions are interfering

### WebSocket Connection Issues

**Symptoms**: Messages not sending, "Connection error" message

**Solutions**:
1. **Check backend is running**
   ```bash
   curl https://YOUR-DOMAIN.com/health
   # Should return 200 OK
   ```

2. **Verify WebSocket URL**
   - Check DevTools Console
   - Look for "Connecting to: wss://..."
   - Verify URL is correct

3. **Check firewall/proxy**
   - Some corporate networks block WebSockets
   - Test on mobile data to confirm

### Phone Number Not Being Collected

**Issue**: Phone form doesn't appear or doesn't work

**Check**:
1. localStorage is enabled
2. No browser extensions blocking it
3. Check DevTools Console for errors

---

## Performance Optimization

### Lazy Loading

```html
<!-- Load widget only when needed -->
<script>
  // Load on scroll or user interaction
  let widgetLoaded = false;
  
  function loadWidget() {
    if (widgetLoaded) return;
    widgetLoaded = true;
    
    const script = document.createElement('script');
    script.src = 'https://YOUR-DOMAIN.com/static/chat-widget.js';
    script.onload = function() {
      FashionBotWidget.init({
        clientName: 'Groovee',
        position: 'bottom-right'
      });
    };
    document.body.appendChild(script);
  }
  
  // Load on scroll
  window.addEventListener('scroll', loadWidget, { once: true });
  
  // Or load after 5 seconds
  setTimeout(loadWidget, 5000);
</script>
```

### Async Loading

```html
<!-- Add async attribute for non-blocking load -->
<script async src="https://YOUR-DOMAIN.com/static/chat-widget.js" 
        onload="FashionBotWidget.init({clientName: 'Groovee', position: 'bottom-right'})">
</script>
```

---

## Security & Privacy

### HTTPS Required

Always use HTTPS for production:
```html
<!-- ✅ Good -->
<script src="https://YOUR-DOMAIN.com/static/chat-widget.js"></script>

<!-- ❌ Bad (insecure) -->
<script src="http://YOUR-DOMAIN.com/static/chat-widget.js"></script>
```

### Content Security Policy (CSP)

If your site uses CSP, add these directives:

```html
<meta http-equiv="Content-Security-Policy" content="
  script-src 'self' https://YOUR-DOMAIN.com;
  connect-src 'self' wss://YOUR-DOMAIN.com;
  style-src 'self' 'unsafe-inline';
">
```

### Privacy Compliance

The widget:
- ✅ Stores chat history in localStorage (client-side only)
- ✅ Phone number stored locally and sent to server only when provided
- ✅ Uses WebSocket for real-time communication
- ✅ Complies with GDPR (user controls their data)
- ✅ No third-party tracking cookies

**For GDPR compliance**, add a notice:
```html
<p style="font-size: 11px; color: #666; margin-top: 10px;">
  By using this chat, you agree to our 
  <a href="/privacy">Privacy Policy</a> and 
  <a href="/terms">Terms of Service</a>.
</p>
```

---

## Support & Contact

### Need Help?

- 📧 **Email**: support@your-domain.com
- 📞 **Phone**: +1-XXX-XXX-XXXX
- 💬 **Chat**: Use the widget on our site!
- 📚 **Docs**: https://docs.your-domain.com

### Reporting Issues

If you encounter issues:
1. Open DevTools Console (F12)
2. Copy any error messages
3. Include browser/device information
4. Send screenshot if applicable
5. Contact support with details

---

## Pricing & Plans

Contact sales for pricing:
- Starter: Up to 1,000 conversations/month
- Professional: Up to 10,000 conversations/month
- Enterprise: Unlimited + custom features

---

## What's Next?

After integration:
1. ✅ Test thoroughly on staging environment
2. ✅ Deploy to production
3. ✅ Monitor widget analytics in dashboard
4. ✅ Train your team on using the dashboard
5. ✅ Customize automated responses
6. ✅ Set up notifications

---

**Version**: 2.0 (Shadow DOM)  
**Last Updated**: December 2024  
**Browser Support**: 95%+ global coverage

