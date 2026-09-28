// background.js - Service worker for the Chrome extension

// Listen for installation
chrome.runtime.onInstalled.addListener(() => {
  console.log('Product Chat Demo extension installed');
  
  // Set default settings
  chrome.storage.sync.set({
    backendUrl: 'http://localhost:8000/demo',
    clientId: '',
    apiKey: '',
    enableWidget: true
  });
});

// Handle messages from content scripts
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.type === 'GET_SETTINGS') {
    chrome.storage.sync.get({
      backendUrl: 'http://localhost:8000/demo',
      clientId: '',
      apiKey: '',
      enableWidget: true
    }, (settings) => {
      sendResponse(settings);
    });
    return true;
  }
  
  if (request.type === 'CHAT_REQUEST') {
    handleChatRequest(request.data)
      .then(response => sendResponse({ success: true, data: response }))
      .catch(error => sendResponse({ success: false, error: error.message }));
    return true;
  }

  if (request.type === 'FETCH_OG_IMAGE') {
    fetchOgImage(request.url)
      .then(data => sendResponse({ success: true, data }))
      .catch(error => sendResponse({ success: false, error: error.message }));
    return true;
  }
});

async function fetchOgImage(url) {
  const resp = await fetch(url, { credentials: 'omit' });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const html = await resp.text();

  // Try og:image first
  let imgMatch = html.match(/<meta[^>]+property=["']og:image["'][^>]+content=["']([^"']+)["']/i)
              || html.match(/<meta[^>]+content=["']([^"']+)["'][^>]+property=["']og:image["']/i);

  // Fallback: twitter:image
  if (!imgMatch) {
    imgMatch = html.match(/<meta[^>]+name=["']twitter:image["'][^>]+content=["']([^"']+)["']/i)
            || html.match(/<meta[^>]+content=["']([^"']+)["'][^>]+name=["']twitter:image["']/i);
  }

  // Fallback: first product image in JSON-LD
  if (!imgMatch) {
    const ldMatch = html.match(/"image"\s*:\s*"(https?:\/\/[^"]+)"/i);
    if (ldMatch) imgMatch = ldMatch;
  }

  // Fallback: first large image on the page (likely the product hero)
  if (!imgMatch) {
    const imgTag = html.match(/<img[^>]+src=["'](https?:\/\/[^"']+(?:product|cdn|image)[^"']*\.(?:jpg|jpeg|png|webp)[^"']*)["']/i);
    if (imgTag) imgMatch = imgTag;
  }

  const ogPrice = html.match(/<meta[^>]+property=["']product:price:amount["'][^>]+content=["']([^"']+)["']/i)
               || html.match(/<meta[^>]+property=["']og:price:amount["'][^>]+content=["']([^"']+)["']/i)
               || html.match(/<meta[^>]+content=["']([^"']+)["'][^>]+property=["']product:price:amount["']/i)
               || html.match(/"price"\s*:\s*"?([\d,.]+)"?/i);

  const ogTitle = html.match(/<meta[^>]+property=["']og:title["'][^>]+content=["']([^"']+)["']/i)
               || html.match(/<meta[^>]+content=["']([^"']+)["'][^>]+property=["']og:title["']/i);

  return {
    image: imgMatch ? imgMatch[1] : null,
    price: ogPrice ? ogPrice[1] : null,
    title: ogTitle ? ogTitle[1] : null,
  };
}

async function handleChatRequest(data) {
  const settings = await chrome.storage.sync.get({
    backendUrl: 'http://localhost:8000/demo',
    apiKey: ''
  });
  
  const response = await fetch(`${settings.backendUrl}/chat`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(settings.apiKey && { 'X-API-Key': settings.apiKey })
    },
    body: JSON.stringify(data)
  });
  
  if (!response.ok) {
    throw new Error(`Backend error: ${response.status}`);
  }
  
  return response.json();
}

