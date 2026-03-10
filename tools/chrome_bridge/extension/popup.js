let connectedSince = null;
let uptimeInterval = null;

function normalizeHubUrl(raw) {
  const value = String(raw || '').trim();
  if (!value) return null;
  try {
    const candidate = value.includes('://') ? value : `ws://${value}`;
    const url = new URL(candidate);
    if (!['ws:', 'wss:'].includes(url.protocol)) return null;
    url.username = '';
    url.password = '';
    url.hash = '';
    if (url.pathname === '/') url.pathname = '';
    return url.toString().replace(/\/$/, '');
  } catch {
    return null;
  }
}

function describeConnectionState(state, runtime) {
  if (state === 'connected') return 'Connected to hub';
  if (state === 'connecting') return 'Connecting to hub';
  if (state === 'probing') return 'Checking hub health';
  if (runtime?.nextReconnectAt && runtime.nextReconnectAt > Date.now()) {
    return `Waiting to reconnect (${formatCountdown(runtime.nextReconnectAt - Date.now())})`;
  }
  return 'Disconnected';
}

async function update() {
  try {
    const [localData, sessionData] = await Promise.all([
      chrome.storage.local.get(['bridgeProfileId', 'bridgeHubUrl']),
      chrome.storage.session.get([
        'bridgeTabs',
        'bridgeConnected',
        'bridgeTransport',
        'bridgeWindowCount',
        'bridgeMetrics',
        'bridgeHealthUrl',
        'bridgeConnectionState',
        'bridgeRuntimeState'
      ])
    ]);
    const data = { ...localData, ...sessionData };
    const runtime = data.bridgeRuntimeState || {};

    const connected = !!data.bridgeConnected;
    const state = data.bridgeConnectionState || runtime.connectionState || (connected ? 'connected' : 'disconnected');
    document.getElementById('dot').className = 'dot ' + (connected ? 'on' : 'off');
    document.getElementById('statusText').textContent = describeConnectionState(state, runtime);
    document.getElementById('connectionState').textContent = state;

    document.getElementById('profileId').textContent =
      data.bridgeProfileId ? data.bridgeProfileId.substring(0, 12) + '...' : '—';

    const tabs = data.bridgeTabs || [];
    document.getElementById('tabCount').textContent = tabs.length;
    document.getElementById('windowCount').textContent = data.bridgeWindowCount || 0;
    document.getElementById('transport').textContent = data.bridgeTransport || 'disconnected';
    document.getElementById('hubUrlText').textContent = data.bridgeHubUrl || '—';
    document.getElementById('healthUrlText').textContent = data.bridgeHealthUrl || '—';

    const hubUrlInput = document.getElementById('hubUrlInput');
    if (hubUrlInput && document.activeElement !== hubUrlInput) {
      hubUrlInput.value = data.bridgeHubUrl || '';
    }

    // Metrics
    const m = data.bridgeMetrics || {};
    document.getElementById('cmdCount').textContent = m.commandsExecuted || 0;
    document.getElementById('failCount').textContent = m.commandsFailed || 0;
    document.getElementById('cmdBadge').textContent = (m.commandsExecuted || 0) + ' cmds';
    document.getElementById('lastError').textContent = m.lastError || '—';

    if (m.lastCommandTime) {
      document.getElementById('latency').textContent = Math.round(m.lastCommandTime) + 'ms';
      document.getElementById('avgLatency').textContent = Math.round(m.lastCommandTime) + 'ms';
    } else if (!connected && runtime.nextReconnectAt && runtime.nextReconnectAt > Date.now()) {
      const retryIn = formatCountdown(runtime.nextReconnectAt - Date.now());
      document.getElementById('latency').textContent = 'retry ' + retryIn;
      document.getElementById('avgLatency').textContent = retryIn;
    } else {
      document.getElementById('latency').textContent = '—';
      document.getElementById('avgLatency').textContent = '—';
    }

    renderTabs(tabs);

    if (connected) {
      connectedSince = runtime.lastConnectedAt || connectedSince || Date.now();
      startUptimeTimer();
    } else if (!connected) {
      connectedSince = null;
      document.getElementById('uptime').textContent = '—';
      if (uptimeInterval) { clearInterval(uptimeInterval); uptimeInterval = null; }
    }
  } catch (e) {
    document.getElementById('profileId').textContent = 'Error';
  }
}

function renderTabs(tabs) {
  const list = document.getElementById('tabList');
  const section = document.getElementById('tabSection');
  if (!tabs.length) { section.style.display = 'none'; return; }
  section.style.display = '';
  list.innerHTML = tabs.map(t => {
    const dot = t.active ? 'tab-active' : 'tab-inactive';
    const title = (t.title || t.url || '(untitled)').substring(0, 55);
    const url = (t.url || '').replace(/^https?:\/\//, '').substring(0, 40);
    return `<div class="tab-item"><span class="${dot}"></span><span class="tab-title" title="${url}">${title}</span></div>`;
  }).join('');
}

function formatUptime(ms) {
  const s = Math.floor(ms / 1000);
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'm ' + (s % 60) + 's';
  const h = Math.floor(m / 60);
  if (h < 24) return h + 'h ' + (m % 60) + 'm';
  const d = Math.floor(h / 24);
  return d + 'd ' + (h % 24) + 'h';
}

function formatCountdown(ms) {
  const seconds = Math.max(1, Math.ceil(ms / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${seconds % 60}s`;
}

function startUptimeTimer() {
  if (uptimeInterval) clearInterval(uptimeInterval);
  const tick = () => {
    if (connectedSince) {
      document.getElementById('uptime').textContent = formatUptime(Date.now() - connectedSince);
    }
  };
  tick();
  uptimeInterval = setInterval(tick, 1000);
}

// Quick actions
document.getElementById('btnScreenshot')?.addEventListener('click', async () => {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab) {
      const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: 'png' });
      const a = document.createElement('a');
      a.href = dataUrl;
      a.download = 'screenshot-' + Date.now() + '.png';
      a.click();
    }
  } catch (e) { /* ignore */ }
});

document.getElementById('btnPageInfo')?.addEventListener('click', async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab) {
    chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => {
        const info = {
          title: document.title, url: location.href,
          elements: document.querySelectorAll('*').length,
          links: document.querySelectorAll('a').length,
          images: document.querySelectorAll('img').length,
          scripts: document.querySelectorAll('script').length,
          forms: document.querySelectorAll('form').length,
          size: document.documentElement.outerHTML.length
        };
        alert(Object.entries(info).map(([k,v]) => `${k}: ${v}`).join('\n'));
      },
      world: 'MAIN'
    });
  }
});

document.getElementById('btnRefresh')?.addEventListener('click', () => update());

document.getElementById('btnSaveHub')?.addEventListener('click', async () => {
  const input = document.getElementById('hubUrlInput');
  const hubUrl = normalizeHubUrl(input?.value);
  if (!hubUrl) {
    showOutput('Invalid hub URL. Use ws://host:port or wss://host.');
    return;
  }

  try {
    const response = await chrome.runtime.sendMessage({ type: 'bridgeSetHubUrl', hubUrl });
    if (!response?.ok) throw new Error(response?.error || 'Failed to save hub URL');
    showOutput(`Hub updated to ${response.hubUrl}`);
    await update();
  } catch (error) {
    showOutput('❌ ' + error.message);
  }
});

document.getElementById('btnReconnect')?.addEventListener('click', async () => {
  try {
    const response = await chrome.runtime.sendMessage({ type: 'bridgeReconnect' });
    if (!response?.ok) throw new Error(response?.error || 'Reconnect failed');
    showOutput(`Reconnecting to ${response.hubUrl}`);
    await update();
  } catch (error) {
    showOutput('❌ ' + error.message);
  }
});

// ── New elite buttons ──

document.getElementById('btnStealth')?.addEventListener('click', async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab) {
    try {
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => {
          Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
          Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5].map(() => ({
              description: 'Portable Document Format',
              filename: 'internal-pdf-viewer',
              name: 'Chrome PDF Plugin'
            }))
          });
          Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
          delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
          delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
          delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
        },
        world: 'MAIN',
        injectImmediately: true
      });
      showOutput('🥷 Stealth mode enabled on tab ' + tab.id);
    } catch (e) {
      showOutput('❌ ' + e.message);
    }
  }
});

let isRecording = false;
document.getElementById('btnRecord')?.addEventListener('click', async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) return;
  const btn = document.getElementById('btnRecord');
  if (!isRecording) {
    try {
      await chrome.tabs.sendMessage(tab.id, { target: 'content', command: 'record.start', params: {} });
      btn.textContent = '⏹ Stop';
      isRecording = true;
      showOutput('🔴 Recording started on tab ' + tab.id);
    } catch (e) {
      // Inject content script first
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ['content.js'] });
      await chrome.tabs.sendMessage(tab.id, { target: 'content', command: 'record.start', params: {} });
      btn.textContent = '⏹ Stop';
      isRecording = true;
      showOutput('🔴 Recording started on tab ' + tab.id);
    }
  } else {
    const result = await chrome.tabs.sendMessage(tab.id, { target: 'content', command: 'record.stop', params: {} });
    btn.textContent = '🔴 Record';
    isRecording = false;
    showOutput(`⏹ Recording stopped: ${result?.count || 0} events captured`);
  }
});

document.getElementById('btnPerf')?.addEventListener('click', async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab) {
    try {
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => {
          const vitals = {};
          const lcp = performance.getEntriesByType('largest-contentful-paint');
          if (lcp.length) vitals.LCP = Math.round(lcp[lcp.length - 1].startTime) + 'ms';
          const fcp = performance.getEntriesByType('paint').find(e => e.name === 'first-contentful-paint');
          if (fcp) vitals.FCP = Math.round(fcp.startTime) + 'ms';
          const nav = performance.getEntriesByType('navigation')[0] || {};
          vitals.TTFB = Math.round(nav.responseStart - nav.requestStart) + 'ms';
          vitals.Load = Math.round(nav.loadEventEnd - nav.startTime) + 'ms';
          vitals.Resources = performance.getEntriesByType('resource').length;
          vitals.DOM = document.querySelectorAll('*').length + ' elements';
          return vitals;
        },
        world: 'MAIN'
      });
      const v = results[0]?.result || {};
      showOutput('⚡ Web Vitals:\n' + Object.entries(v).map(([k, val]) => `  ${k}: ${val}`).join('\n'));
    } catch (e) {
      showOutput('❌ ' + e.message);
    }
  }
});

function showOutput(text) {
  const el = document.getElementById('output');
  el.style.display = 'block';
  el.textContent = text;
  setTimeout(() => { el.style.display = 'none'; }, 8000);
}

// ── v4.0 God Stealth ──

document.getElementById('btnFullStealth')?.addEventListener('click', async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) return;
  try {
    const response = await chrome.runtime.sendMessage({
      type: 'bridgeExecCommand', command: 'stealth.full', params: { tabId: tab.id }
    });
    // Fallback: try direct scripting if message handler not available
    if (!response) {
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => {
          Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
          delete navigator.__proto__.webdriver;
          Object.defineProperty(navigator, 'plugins', {
            get: () => {
              const p = [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' }
              ]; p.length = 3; return p;
            }
          });
          Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
          window.chrome = window.chrome || {};
          window.chrome.runtime = window.chrome.runtime || { connect: () => {}, sendMessage: () => {} };
          window.chrome.loadTimes = window.chrome.loadTimes || (() => ({}));
          for (const k of Object.keys(window)) {
            if (k.startsWith('cdc_') || k.startsWith('__selenium') || k.startsWith('__driver')) delete window[k];
          }
          const _origToDataURL = HTMLCanvasElement.prototype.toDataURL;
          const _origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
          HTMLCanvasElement.prototype.toDataURL = function() {
            try {
              const ctx = this.getContext('2d');
              if (ctx && this.width > 0 && this.height > 0) {
                const d = _origGetImageData.call(ctx, 0, 0, Math.min(this.width, 2), Math.min(this.height, 2));
                d.data[0] ^= 1; ctx.putImageData(d, 0, 0);
              }
            } catch {}
            return _origToDataURL.apply(this, arguments);
          };
          const _origGetParam = WebGLRenderingContext.prototype.getParameter;
          WebGLRenderingContext.prototype.getParameter = function(param) {
            if (param === 37445) return 'Intel Inc.';
            if (param === 37446) return 'Intel Iris OpenGL Engine';
            return _origGetParam.call(this, param);
          };
        },
        world: 'MAIN',
        injectImmediately: true
      });
    }
    showOutput('🛡️ God Stealth enabled — full fingerprint protection active');
  } catch (e) {
    showOutput('❌ ' + e.message);
  }
});

// ── v4.0 Dark Mode Toggle ──

let darkModeActive = false;
document.getElementById('btnDarkMode')?.addEventListener('click', async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) return;
  darkModeActive = !darkModeActive;
  try {
    await chrome.debugger.attach({ tabId: tab.id }, '1.3').catch(() => {});
    await new Promise((resolve, reject) => {
      chrome.debugger.sendCommand({ tabId: tab.id }, 'Emulation.setEmulatedMedia', {
        features: [{ name: 'prefers-color-scheme', value: darkModeActive ? 'dark' : 'light' }]
      }, (result) => {
        if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
        else resolve(result);
      });
    });
    const btn = document.getElementById('btnDarkMode');
    btn.textContent = darkModeActive ? '☀️ Light' : '🌙 Dark';
    showOutput(darkModeActive ? '🌙 Dark mode emulation enabled' : '☀️ Light mode restored');
  } catch (e) {
    showOutput('❌ ' + e.message);
  }
});

// ── v4.0 FPS Counter ──

let fpsActive = false;
document.getElementById('btnFPS')?.addEventListener('click', async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) return;
  fpsActive = !fpsActive;
  try {
    await chrome.debugger.attach({ tabId: tab.id }, '1.3').catch(() => {});
    await new Promise((resolve, reject) => {
      chrome.debugger.sendCommand({ tabId: tab.id }, 'Overlay.enable', {}, () => {
        chrome.debugger.sendCommand({ tabId: tab.id }, 'Overlay.setShowFPSCounter', {
          show: fpsActive
        }, (result) => {
          if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
          else resolve(result);
        });
      });
    });
    showOutput(fpsActive ? '📊 FPS counter visible' : '📊 FPS counter hidden');
  } catch (e) {
    showOutput('❌ ' + e.message);
  }
});

// ── v4.0 Tab Sort ──

document.getElementById('btnSortTabs')?.addEventListener('click', async () => {
  try {
    const tabs = await chrome.tabs.query({});
    const sorted = [...tabs].sort((a, b) => (a.url || '').localeCompare(b.url || ''));
    for (let i = 0; i < sorted.length; i++) {
      await chrome.tabs.move(sorted[i].id, { index: i });
    }
    showOutput(`🔤 Sorted ${sorted.length} tabs by URL`);
  } catch (e) {
    showOutput('❌ ' + e.message);
  }
});

// ── v4.0 Tab Deduplicate ──

document.getElementById('btnDedupe')?.addEventListener('click', async () => {
  try {
    const tabs = await chrome.tabs.query({});
    const seen = new Map();
    const dupes = [];
    for (const t of tabs) {
      try {
        const key = new URL(t.url).origin + new URL(t.url).pathname;
        if (seen.has(key)) dupes.push(t.id);
        else seen.set(key, t.id);
      } catch {}
    }
    if (dupes.length) await chrome.tabs.remove(dupes);
    showOutput(`🧹 Removed ${dupes.length} duplicate tabs`);
  } catch (e) {
    showOutput('❌ ' + e.message);
  }
});

// ── v4.0 Tab Group by Domain ──

document.getElementById('btnGroupTabs')?.addEventListener('click', async () => {
  try {
    const tabs = await chrome.tabs.query({});
    const domains = new Map();
    for (const t of tabs) {
      try {
        const domain = new URL(t.url).hostname;
        if (!domains.has(domain)) domains.set(domain, []);
        domains.get(domain).push(t.id);
      } catch {}
    }
    const colors = ['blue', 'red', 'yellow', 'green', 'pink', 'purple', 'cyan', 'orange'];
    let grouped = 0;
    let colorIdx = 0;
    for (const [domain, tabIds] of domains) {
      if (tabIds.length < 2) continue;
      const groupId = await chrome.tabs.group({ tabIds });
      await chrome.tabGroups.update(groupId, {
        title: domain.replace(/^www\./, ''),
        color: colors[colorIdx++ % colors.length]
      });
      grouped++;
    }
    showOutput(`📁 Created ${grouped} tab groups by domain`);
  } catch (e) {
    showOutput('❌ ' + e.message);
  }
});

// Live updates
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' || area === 'session') update();
});

update();
