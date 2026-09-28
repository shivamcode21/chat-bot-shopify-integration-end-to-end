// popup.js — Settings management for the client-configured plugin

document.addEventListener('DOMContentLoaded', async () => {
  const settings = await chrome.storage.sync.get({
    backendUrl: 'https://ecommagents.onrender.com',
    clientName: 'Underneat',
    allowedDomain: 'underneat.in',
    enableWidget: true
  });

  document.getElementById('backendUrl').value = settings.backendUrl;
  document.getElementById('clientName').value = settings.clientName;
  if (settings.allowedDomain) {
    document.getElementById('allowedDomain').value = settings.allowedDomain;
  } else {
    const activeDomain = await getActiveTabDomain();
    if (activeDomain) {
      document.getElementById('allowedDomain').value = activeDomain;
    }
  }
  document.getElementById('enableWidget').checked = settings.enableWidget;
});

document.getElementById('saveBtn').addEventListener('click', async () => {
  const clientName = document.getElementById('clientName').value.trim();
  let allowedDomain = normalizeDomain(document.getElementById('allowedDomain').value.trim());

  if (!clientName) {
    showStatus('Client Name is required', 'error');
    return;
  }

  if (!allowedDomain) {
    allowedDomain = await getActiveTabDomain();
    if (!allowedDomain) {
      showStatus('Allowed Website Domain is required', 'error');
      return;
    }
    document.getElementById('allowedDomain').value = allowedDomain;
  }

  const settings = {
    backendUrl: document.getElementById('backendUrl').value.trim() || 'https://ecommagents.onrender.com',
    clientName: clientName,
    allowedDomain: allowedDomain,
    enableWidget: document.getElementById('enableWidget').checked
  };

  await chrome.storage.sync.set(settings);

  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (tabs[0]) {
      chrome.tabs.sendMessage(tabs[0].id, { type: 'SETTINGS_UPDATED', settings });
    }
  });

  showStatus('Settings saved!', 'success');
});

document.getElementById('testBtn').addEventListener('click', async () => {
  const backendUrl = document.getElementById('backendUrl').value.trim() || 'https://ecommagents.onrender.com';
  const clientName = document.getElementById('clientName').value.trim();

  if (!clientName) {
    showStatus('Enter a Client Name first', 'error');
    return;
  }

  try {
    const wsProtocol = backendUrl.startsWith('https') ? 'wss' : 'ws';
    const host = backendUrl.replace(/^https?:\/\//, '').replace(/\/$/, '');
    const wsUrl = `${wsProtocol}://${host}/ws/chat/${encodeURIComponent(clientName)}/test_ping_${Date.now()}`;

    const ws = new WebSocket(wsUrl);
    const timeout = setTimeout(() => {
      ws.close();
      showStatus('Connection timed out', 'error');
    }, 5000);

    ws.onopen = () => {
      clearTimeout(timeout);
      showStatus(`Connected to ${clientName}!`, 'success');
      ws.close();
    };

    ws.onerror = () => {
      clearTimeout(timeout);
      showStatus('Cannot connect. Is backend running?', 'error');
    };
  } catch (err) {
    showStatus('Connection error: ' + err.message, 'error');
  }
});

function showStatus(message, type) {
  const status = document.getElementById('status');
  status.textContent = message;
  status.className = 'status ' + type;
  status.style.display = 'block';
  setTimeout(() => { status.style.display = 'none'; }, 4000);
}

function normalizeDomain(domainInput) {
  if (!domainInput) return '';
  let d = domainInput.toLowerCase().trim();
  d = d.replace(/^https?:\/\//, '');
  d = d.replace(/^www\./, '');
  d = d.split('/')[0];
  d = d.split('?')[0];
  return d;
}

async function getActiveTabDomain() {
  return new Promise((resolve) => {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      try {
        const tab = tabs && tabs[0];
        if (!tab || !tab.url) return resolve('');
        const u = new URL(tab.url);
        resolve(normalizeDomain(u.hostname));
      } catch (_e) {
        resolve('');
      }
    });
  });
}
