// background.js — Service worker for the locked client plugin

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
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

  let imgMatch = html.match(/<meta[^>]+property=["']og:image["'][^>]+content=["']([^"']+)["']/i)
              || html.match(/<meta[^>]+content=["']([^"']+)["'][^>]+property=["']og:image["']/i);

  if (!imgMatch) {
    imgMatch = html.match(/<meta[^>]+name=["']twitter:image["'][^>]+content=["']([^"']+)["']/i)
            || html.match(/<meta[^>]+content=["']([^"']+)["'][^>]+name=["']twitter:image["']/i);
  }

  if (!imgMatch) {
    const ldMatch = html.match(/"image"\s*:\s*"(https?:\/\/[^"]+)"/i);
    if (ldMatch) imgMatch = ldMatch;
  }

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
