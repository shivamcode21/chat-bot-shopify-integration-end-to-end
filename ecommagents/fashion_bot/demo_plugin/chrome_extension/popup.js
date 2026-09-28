// popup.js - Settings management for the demo plugin

document.addEventListener('DOMContentLoaded', async () => {
  // Load saved settings
  const settings = await chrome.storage.sync.get({
    backendUrl: 'http://localhost:8000/demo',
    clientId: '',
    apiKey: '',
    enableWidget: true
  });
  
  document.getElementById('backendUrl').value = settings.backendUrl;
  document.getElementById('clientId').value = settings.clientId;
  document.getElementById('apiKey').value = settings.apiKey;
  document.getElementById('enableWidget').checked = settings.enableWidget;
});

// Save settings
document.getElementById('saveBtn').addEventListener('click', async () => {
  const settings = {
    backendUrl: document.getElementById('backendUrl').value.trim() || 'http://localhost:8000/demo',
    clientId: document.getElementById('clientId').value.trim(),
    apiKey: document.getElementById('apiKey').value.trim(),
    enableWidget: document.getElementById('enableWidget').checked
  };
  
  await chrome.storage.sync.set(settings);
  
  // Notify content scripts
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (tabs[0]) {
      chrome.tabs.sendMessage(tabs[0].id, { type: 'SETTINGS_UPDATED', settings });
    }
  });
  
  showStatus('Settings saved!', 'success');
});

// Test connection
document.getElementById('testBtn').addEventListener('click', async () => {
  const backendUrl = document.getElementById('backendUrl').value.trim() || 'http://localhost:8000/demo';
  
  try {
    const response = await fetch(`${backendUrl}/health`);
    if (response.ok) {
      showStatus('✓ Backend connected successfully!', 'success');
    } else {
      showStatus('✗ Backend returned error: ' + response.status, 'error');
    }
  } catch (err) {
    showStatus('✗ Cannot connect to backend. Is it running?', 'error');
  }
});

function showStatus(message, type) {
  const status = document.getElementById('status');
  status.textContent = message;
  status.className = 'status ' + type;
  status.style.display = 'block';
  
  setTimeout(() => {
    status.style.display = 'none';
  }, 3000);
}

