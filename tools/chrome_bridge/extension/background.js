/*
 * Chrome Bridge v4.0 — Background Service Worker
 * The ultimate browser automation bridge. 265+ commands.
 * Core: tabs, windows, debugger, scripting, cookies, downloads, tab groups
 * Advanced: network interception, console capture, PDF, emulation, a11y, performance
 * Elite: smart element finding, stealth mode, pre-page injection, workflow engine,
 *        auto-healing selectors, visual regression, session recording, HAR recording,
 *        JS/CSS coverage, DOM snapshots, CDP input, multi-tab orchestration,
 *        WebSocket interception, cookie profiles, navigation history, frame tree,
 *        error auto-screenshot, user agent rotation, response body capture
 * v3.0: Real-time event streaming, smart navigation wait, command pipeline
 * v3.1: Configurable hub endpoint, persistent runtime state, HTTP health probe,
 *        IntersectionObserver, DOM stability wait, element traversal, canvas reading,
 *        XHR interception, fuzzy smart find, POST navigation, wait.function/wait.url,
 *        tabs.createAndWait, merged CDP input handlers, touch swipe support
 * v4.0: Full stealth suite (canvas/WebGL/audio/timing noise, UA client hints),
 *        performance tracing, vision deficiency simulation, dark mode emulation,
 *        CPU throttling, forced colors, animation control, CDP overlays (FPS/paint/layout),
 *        WebAuthn virtual authenticator, network mocking, service worker control,
 *        Cache API access, IndexedDB operations, runtime heap inspection, GC trigger,
 *        tab sort/deduplicate/groupByDomain/suspend, DOM CDP ops (scrollIntoView/focus/search),
 *        natural typing, element hover/focus/blur/closest/style/offset/matches,
 *        form submit, DOM serialize, page freeze/unfreeze, bridge state export
 */

const DEFAULT_HUB_PORT = 7777;
const DEFAULT_HUB_URL = `ws://127.0.0.1:${DEFAULT_HUB_PORT}`;
const HUB_HEALTH_PATH = '/healthz';
const BRIDGE_VERSION = '4.0.0';
const BRIDGE_TRANSPORT = 'service-worker-websocket';
const HUB_RECONNECT_BASE_MS = 1000;
const HUB_RECONNECT_MAX_MS = 30000;
const HUB_PING_INTERVAL_MS = 20000;
const HUB_PROBE_TIMEOUT_MS = 1200;

let profileId = null;
let hubSocket = null;
let hubReconnectTimer = null;
let hubReconnectAttempt = 0;
let hubPingTimer = null;
let hubProbePromise = null;
let hubConnectGeneration = 0;
let suppressHubConfigReconnect = false;
let hubConfig = { url: DEFAULT_HUB_URL };
let bridgeRuntime = {
  connectionState: 'starting',
  lastProbeAt: 0,
  lastProbeOk: null,
  lastConnectedAt: 0,
  lastDisconnectedAt: 0,
  nextReconnectAt: 0
};
const debuggerAttached = new Set();

// ─── Event Streaming ───
const eventSubscriptions = new Set();
const cdpEventTabs = new Map(); // tabId -> Set<domain>

function pushEvent(event, data) {
  if (!eventSubscriptions.size) return;
  if (!eventSubscriptions.has(event) && !eventSubscriptions.has('*')) return;
  sendToHub({ type: 'event', event, data, ts: Date.now() });
}

async function enableCDPEvents(tabId, domains) {
  await ensureDebugger(tabId);
  const enabled = cdpEventTabs.get(tabId) || new Set();
  for (const domain of domains) {
    if (!enabled.has(domain)) {
      await cdpSend(tabId, `${domain}.enable`, {});
      enabled.add(domain);
    }
  }
  cdpEventTabs.set(tabId, enabled);
}

// ─── Performance: Tab State Cache ───
let tabCache = [];
let tabCacheTime = 0;
const TAB_CACHE_TTL = 500; // ms

async function getCachedTabs() {
  if (Date.now() - tabCacheTime < TAB_CACHE_TTL && tabCache.length) return tabCache;
  tabCache = await chrome.tabs.query({});
  tabCacheTime = Date.now();
  return tabCache;
}

function invalidateTabCache() { tabCacheTime = 0; }

// ─── Metrics ───
let metrics = {
  commandsExecuted: 0,
  commandsFailed: 0,
  startTime: Date.now(),
  lastCommandTime: 0,
  transport: BRIDGE_TRANSPORT,
  reconnectAttempt: 0,
  lastError: null,
  lastRegistrationTime: 0
};

function isHubConnected() {
  return hubSocket && hubSocket.readyState === WebSocket.OPEN;
}

function isHubConnecting() {
  return hubSocket && hubSocket.readyState === WebSocket.CONNECTING;
}

function normalizeHubUrl(raw = DEFAULT_HUB_URL) {
  const value = String(raw || '').trim();
  if (!value) return DEFAULT_HUB_URL;
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

function getHubUrl() {
  return hubConfig.url;
}

function getHubHealthUrl(hubUrl = getHubUrl()) {
  const url = new URL(hubUrl);
  url.protocol = url.protocol === 'wss:' ? 'https:' : 'http:';
  url.pathname = HUB_HEALTH_PATH;
  url.search = '';
  url.hash = '';
  return url.toString();
}

function setBridgeConnectionState(state, extra = {}) {
  bridgeRuntime = {
    ...bridgeRuntime,
    connectionState: state,
    ...extra
  };
}

function getReconnectDelay() {
  const base = Math.min(HUB_RECONNECT_BASE_MS * (2 ** hubReconnectAttempt), HUB_RECONNECT_MAX_MS);
  const jitter = Math.round(base * 0.2 * (Math.random() * 2 - 1));
  return Math.max(HUB_RECONNECT_BASE_MS, base + jitter);
}

async function ensureProfileId() {
  const { bridgeProfileId } = await chrome.storage.local.get('bridgeProfileId');
  if (bridgeProfileId) {
    profileId = bridgeProfileId;
    return profileId;
  }
  profileId = crypto.randomUUID();
  await chrome.storage.local.set({ bridgeProfileId: profileId });
  return profileId;
}

async function loadHubConfig() {
  const { bridgeHubUrl } = await chrome.storage.local.get('bridgeHubUrl');
  const normalized = normalizeHubUrl(bridgeHubUrl || DEFAULT_HUB_URL) || DEFAULT_HUB_URL;
  hubConfig.url = normalized;
  if (bridgeHubUrl !== normalized) {
    suppressHubConfigReconnect = true;
    await chrome.storage.local.set({ bridgeHubUrl: normalized });
  }
}

async function hydrateBridgeState() {
  const data = await chrome.storage.session.get(['bridgeMetrics', 'bridgeRuntimeState']);
  if (data.bridgeMetrics && typeof data.bridgeMetrics === 'object') {
    metrics = {
      ...metrics,
      ...data.bridgeMetrics,
      transport: BRIDGE_TRANSPORT
    };
  }
  if (data.bridgeRuntimeState && typeof data.bridgeRuntimeState === 'object') {
    bridgeRuntime = {
      ...bridgeRuntime,
      ...data.bridgeRuntimeState,
      connectionState: 'starting',
      nextReconnectAt: 0
    };
  }
  hubReconnectAttempt = metrics.reconnectAttempt || 0;
}

// ─── Initialization ───

async function bootstrapBridge() {
  await ensureProfileId();
  await loadHubConfig();
  await hydrateBridgeState();
  setBridgeConnectionState('starting', { nextReconnectAt: 0 });
  await syncStateToStorage();
  await setBridgeSessionState();
  void connectHub();
}

chrome.runtime.onInstalled.addListener(() => {
  void bootstrapBridge();
});

chrome.runtime.onStartup.addListener(() => {
  void bootstrapBridge();
});

chrome.runtime.onSuspend?.addListener(() => {
  stopHubPing();
});

void bootstrapBridge();

chrome.alarms.create('bridge-reconnect', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'bridge-reconnect' && !isHubConnected() && !isHubConnecting()) {
    void connectHub();
  }
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== 'local' || !changes.bridgeHubUrl) return;
  const nextHubUrl = normalizeHubUrl(changes.bridgeHubUrl.newValue);
  if (!nextHubUrl) return;

  hubConfig.url = nextHubUrl;
  metrics.lastError = null;
  setBridgeConnectionState('disconnected', { nextReconnectAt: 0 });
  void setBridgeSessionState();

  if (suppressHubConfigReconnect) {
    suppressHubConfigReconnect = false;
    return;
  }

  disconnectHub({ reconnect: true, clearError: true });
});

// ─── Badge / Session State ───

async function setBridgeSessionState(extra = {}) {
  await chrome.storage.session.set({
    bridgeConnected: isHubConnected(),
    bridgeTransport: isHubConnected() ? BRIDGE_TRANSPORT : 'disconnected',
    bridgeMetrics: metrics,
    bridgeHubUrl: getHubUrl(),
    bridgeHealthUrl: getHubHealthUrl(),
    bridgeConnectionState: bridgeRuntime.connectionState,
    bridgeRuntimeState: bridgeRuntime,
    ...extra
  });
}

function setBadge(connected) {
  chrome.action.setBadgeText({ text: connected ? '✓' : '' });
  chrome.action.setBadgeBackgroundColor({ color: connected ? '#22c55e' : '#ef4444' });
  void setBridgeSessionState();
}

// ─── Hub Transport ───

function sendToHub(payload) {
  if (!isHubConnected()) return false;
  hubSocket.send(JSON.stringify(payload));
  return true;
}

function stopHubPing() {
  if (hubPingTimer) {
    clearInterval(hubPingTimer);
    hubPingTimer = null;
  }
}

function startHubPing() {
  stopHubPing();
  hubPingTimer = setInterval(() => {
    sendToHub({ type: 'ping', timestamp: Date.now() });
  }, HUB_PING_INTERVAL_MS);
}

function clearHubReconnect() {
  if (hubReconnectTimer) {
    clearTimeout(hubReconnectTimer);
    hubReconnectTimer = null;
  }
  bridgeRuntime.nextReconnectAt = 0;
  metrics.reconnectAttempt = 0;
}

function disconnectHub({ reconnect = false, clearError = false } = {}) {
  hubConnectGeneration += 1;
  clearHubReconnect();
  stopHubPing();
  if (clearError) metrics.lastError = null;

  const socket = hubSocket;
  hubSocket = null;

  if (socket) {
    socket.onopen = null;
    socket.onmessage = null;
    socket.onclose = null;
    socket.onerror = null;
    try {
      socket.close();
    } catch {}
  }

  setBridgeConnectionState('disconnected', {
    lastDisconnectedAt: Date.now(),
    nextReconnectAt: 0
  });
  setBadge(false);

  if (reconnect) {
    void connectHub();
  }
}

function scheduleHubReconnect() {
  clearHubReconnect();
  const delay = getReconnectDelay();
  hubReconnectAttempt += 1;
  metrics.reconnectAttempt = hubReconnectAttempt;
  setBridgeConnectionState('disconnected', { nextReconnectAt: Date.now() + delay });
  void setBridgeSessionState();
  hubReconnectTimer = setTimeout(() => {
    hubReconnectTimer = null;
    void connectHub();
  }, delay);
}

async function probeHubAvailability() {
  const healthUrl = getHubHealthUrl();
  setBridgeConnectionState('probing', {
    lastProbeAt: Date.now(),
    lastProbeOk: null
  });
  await setBridgeSessionState();

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), HUB_PROBE_TIMEOUT_MS);

  try {
    const response = await fetch(healthUrl, {
      method: 'GET',
      cache: 'no-store',
      signal: controller.signal
    });
    bridgeRuntime.lastProbeOk = response.ok;
    return response.ok;
  } catch (error) {
    bridgeRuntime.lastProbeOk = false;
    return false;
  } finally {
    clearTimeout(timeoutId);
  }
}

async function publishRegistration() {
  const registration = await getRegistration();
  if (sendToHub(registration)) {
    metrics.lastRegistrationTime = Date.now();
    metrics.lastError = null;
    await setBridgeSessionState();
  }
}

async function executeIncomingCommand(command, params = {}) {
  const start = performance.now();
  try {
    const result = await executeCommand(command, params);
    metrics.commandsExecuted += 1;
    metrics.lastCommandTime = performance.now() - start;
    await setBridgeSessionState();
    return { result, _ms: metrics.lastCommandTime };
  } catch (error) {
    metrics.commandsFailed += 1;
    metrics.lastCommandTime = performance.now() - start;
    await setBridgeSessionState();
    return { error: error.message, _ms: metrics.lastCommandTime };
  }
}

async function executeIncomingBatch(commands = []) {
  const results = await Promise.allSettled(
    commands.map(async (cmd) => {
      try {
        return { id: cmd.id, result: await executeCommand(cmd.command, cmd.params || {}) };
      } catch (error) {
        return { id: cmd.id, error: error.message };
      }
    })
  );
  metrics.commandsExecuted += commands.length;
  metrics.lastCommandTime = 0;
  await setBridgeSessionState();
  return results.map((entry) => entry.value || entry.reason);
}

async function connectHub() {
  if (isHubConnected() || isHubConnecting() || hubProbePromise) return;

  const connectGeneration = hubConnectGeneration;
  const probePromise = probeHubAvailability();
  hubProbePromise = probePromise;
  const hubAvailable = await probePromise;
  if (hubProbePromise === probePromise) {
    hubProbePromise = null;
  }
  if (connectGeneration !== hubConnectGeneration) return;

  if (!hubAvailable) {
    metrics.lastError = `Hub unavailable at ${getHubUrl()}`;
    setBadge(false);
    await setBridgeSessionState();
    scheduleHubReconnect();
    return;
  }

  setBridgeConnectionState('connecting', { nextReconnectAt: 0 });
  await setBridgeSessionState();

  let socket;
  try {
    socket = new WebSocket(getHubUrl());
  } catch (error) {
    metrics.lastError = error.message || 'Failed to create WebSocket';
    void setBridgeSessionState();
    scheduleHubReconnect();
    return;
  }

  hubSocket = socket;

  socket.onopen = async () => {
    if (hubSocket !== socket) {
      socket.close();
      return;
    }
    hubReconnectAttempt = 0;
    metrics.reconnectAttempt = 0;
    metrics.lastError = null;
    clearHubReconnect();
    setBridgeConnectionState('connected', {
      lastConnectedAt: Date.now(),
      nextReconnectAt: 0
    });
    setBadge(true);
    startHubPing();
    await publishRegistration();
  };

  socket.onmessage = async (event) => {
    if (hubSocket !== socket) return;

    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch (error) {
      return;
    }

    if (msg.type === 'pong') return;

    if (msg.type === 'batch') {
      const results = await executeIncomingBatch(msg.commands || []);
      sendToHub({ id: msg.id ?? 0, type: 'batchResult', results });
      return;
    }

    if (msg.id !== undefined && msg.command) {
      const response = await executeIncomingCommand(msg.command, msg.params || {});
      sendToHub({ id: msg.id, ...response });
    }
  };

  socket.onclose = () => {
    if (hubSocket === socket) {
      hubSocket = null;
      stopHubPing();
      setBridgeConnectionState('disconnected', {
        lastDisconnectedAt: Date.now()
      });
      setBadge(false);
      scheduleHubReconnect();
    }
  };

  socket.onerror = () => {
    metrics.lastError = `WebSocket error connecting to ${getHubUrl()}`;
    void setBridgeSessionState();
  };
}

// Legacy sendMessage handler
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === 'getRegistration') {
    getRegistration().then(sendResponse);
    return true;
  }
  if (msg.type === 'getMetrics') {
    sendResponse({
      ...metrics,
      uptime: Date.now() - metrics.startTime,
      connected: isHubConnected(),
      transport: isHubConnected() ? BRIDGE_TRANSPORT : 'disconnected'
    });
    return true;
  }
  if (msg.type === 'bridgeReconnect') {
    disconnectHub({ reconnect: true, clearError: true });
    sendResponse({ ok: true, hubUrl: getHubUrl(), healthUrl: getHubHealthUrl() });
    return true;
  }
  if (msg.type === 'bridgeSetHubUrl') {
    const hubUrl = normalizeHubUrl(msg.hubUrl);
    if (!hubUrl) {
      sendResponse({ ok: false, error: 'Invalid WebSocket URL' });
      return true;
    }
    chrome.storage.local.set({ bridgeHubUrl: hubUrl })
      .then(() => sendResponse({ ok: true, hubUrl, healthUrl: getHubHealthUrl(hubUrl) }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }
});

async function getRegistration() {
  const { bridgeProfileId } = await chrome.storage.local.get('bridgeProfileId');
  profileId = bridgeProfileId || profileId;
  const windows = await chrome.windows.getAll({ populate: true });
  const tabs = [];
  for (const w of windows) {
    for (const t of w.tabs || []) {
      tabs.push({ id: t.id, windowId: t.windowId, url: t.url, title: t.title, active: t.active });
    }
  }
  return {
    type: 'register', profileId, email: '', userAgent: navigator.userAgent,
    tabs, windowCount: windows.length, version: BRIDGE_VERSION, transport: BRIDGE_TRANSPORT
  };
}

// ─── Tab/Window Change Notifications ───

async function syncStateToStorage() {
  const windows = await chrome.windows.getAll({ populate: true });
  const tabs = [];
  for (const w of windows) {
    for (const t of (w.tabs || [])) {
      tabs.push({ id: t.id, windowId: t.windowId, url: t.url, title: t.title, active: t.active });
    }
  }
  await setBridgeSessionState({ bridgeTabs: tabs, bridgeWindowCount: windows.length });
  invalidateTabCache();
}

function pushTabUpdate() {
  syncStateToStorage().then(() => {
    if (isHubConnected()) {
      getRegistration().then((reg) => {
        sendToHub(reg);
      });
    }
  });
}

chrome.tabs.onCreated.addListener(pushTabUpdate);
chrome.tabs.onRemoved.addListener(pushTabUpdate);
chrome.tabs.onActivated.addListener(pushTabUpdate);
chrome.tabs.onUpdated.addListener((tabId, change) => {
  if (change.url || change.title || change.status === 'complete') pushTabUpdate();
});
chrome.windows.onFocusChanged.addListener(pushTabUpdate);

// ─── Event Streaming — Chrome API Events ───
// Passive: always listening, only push when subscribed

chrome.webNavigation.onBeforeNavigate.addListener((d) => {
  if (d.frameId === 0) pushEvent('navigation.started', {
    tabId: d.tabId, url: d.url, timeStamp: d.timeStamp
  });
});
chrome.webNavigation.onCommitted.addListener((d) => {
  if (d.frameId === 0) pushEvent('navigation.committed', {
    tabId: d.tabId, url: d.url, transitionType: d.transitionType
  });
});
chrome.webNavigation.onCompleted.addListener((d) => {
  if (d.frameId === 0) pushEvent('navigation.completed', {
    tabId: d.tabId, url: d.url, timeStamp: d.timeStamp
  });
});
chrome.webNavigation.onErrorOccurred?.addListener((d) => {
  if (d.frameId === 0) pushEvent('navigation.error', {
    tabId: d.tabId, url: d.url, error: d.error
  });
});
chrome.downloads?.onCreated?.addListener((item) => {
  pushEvent('download.created', {
    id: item.id, url: item.url, filename: item.filename, state: item.state
  });
});
chrome.downloads?.onChanged?.addListener((delta) => {
  if (delta.state) pushEvent('download.changed', {
    id: delta.id, state: delta.state.current
  });
});

// ─── Event Streaming — CDP Events (for all debugger-attached tabs) ───

chrome.debugger.onEvent.addListener((source, method, params) => {
  if (!eventSubscriptions.size) return;
  const tabId = source.tabId;

  if (method === 'Network.requestWillBeSent') {
    pushEvent('network.request', {
      tabId, requestId: params.requestId,
      url: params.request?.url, method: params.request?.method,
      type: params.type, timestamp: params.timestamp
    });
  } else if (method === 'Network.responseReceived') {
    pushEvent('network.response', {
      tabId, requestId: params.requestId,
      url: params.response?.url, status: params.response?.status,
      mimeType: params.response?.mimeType, timestamp: params.timestamp
    });
  } else if (method === 'Network.loadingFailed') {
    pushEvent('network.failed', {
      tabId, requestId: params.requestId,
      errorText: params.errorText, canceled: params.canceled
    });
  } else if (method === 'Runtime.consoleAPICalled') {
    pushEvent('console.message', {
      tabId, type: params.type,
      text: params.args?.map(a => a.value ?? a.description ?? '').join(' '),
      timestamp: params.timestamp
    });
  } else if (method === 'Page.javascriptDialogOpening') {
    pushEvent('dialog.opened', {
      tabId, type: params.type, message: params.message,
      url: params.url, defaultPrompt: params.defaultPrompt
    });
  } else if (method === 'Runtime.exceptionThrown') {
    pushEvent('runtime.exception', {
      tabId, text: params.exceptionDetails?.text,
      url: params.exceptionDetails?.url,
      line: params.exceptionDetails?.lineNumber
    });
  } else if (method === 'Fetch.requestPaused') {
    pushEvent('fetch.paused', {
      tabId, requestId: params.requestId,
      url: params.request?.url, resourceType: params.resourceType
    });
  }
});

// ─── Command Dispatcher ───

function isTopLevelCommandFailure(result) {
  if (!result || typeof result !== 'object' || Array.isArray(result)) return false;
  if (typeof result.__error === 'string' && result.__error) return true;
  if (typeof result.error !== 'string' || !result.error) return false;
  if (result.ok === false) return false;

  const keys = Object.keys(result);
  return keys.length === 1 || keys.every((key) => ['error', 'code', 'details', 'stack'].includes(key));
}

function normalizeCommandResult(result) {
  if (isTopLevelCommandFailure(result)) {
    throw new Error(result.__error || result.error);
  }
  return result;
}

async function executeCommand(command, params = {}) {
  return normalizeCommandResult(await handleCommand({ command, params }));
}

async function handleCommand(msg) {
  const { command, params = {} } = msg;
  const p = params;

  if (!profileId) {
    const { bridgeProfileId } = await chrome.storage.local.get('bridgeProfileId');
    profileId = bridgeProfileId;
  }

  switch (command) {
    // ════════════════════════════════════════
    // ── Tab Management ──
    // ════════════════════════════════════════
    case 'tabs.list': {
      const tabs = await getCachedTabs();
      return tabs.map(t => ({
        id: t.id, windowId: t.windowId, url: t.url, title: t.title,
        active: t.active, index: t.index, pinned: t.pinned, status: t.status,
        groupId: t.groupId ?? -1
      }));
    }
    case 'tabs.get': {
      const t = await chrome.tabs.get(p.tabId);
      return { id: t.id, windowId: t.windowId, url: t.url, title: t.title, active: t.active, groupId: t.groupId };
    }
    case 'tabs.create': {
      const t = await chrome.tabs.create({ url: p.url, windowId: p.windowId, active: p.active !== false });
      invalidateTabCache();
      return { id: t.id, windowId: t.windowId, url: t.url };
    }
    case 'tabs.navigate': {
      await chrome.tabs.update(p.tabId, { url: p.url });
      if (p.waitUntil) {
        await waitForNavigation(p.tabId, p.timeout || 30000);
      }
      return { ok: true };
    }
    case 'tabs.close': {
      const ids = Array.isArray(p.tabId) ? p.tabId : [p.tabId];
      await chrome.tabs.remove(ids);
      invalidateTabCache();
      return { ok: true, closed: ids.length };
    }
    case 'tabs.activate': {
      await chrome.tabs.update(p.tabId, { active: true });
      if (p.focusWindow) {
        const t = await chrome.tabs.get(p.tabId);
        await chrome.windows.update(t.windowId, { focused: true });
      }
      return { ok: true };
    }
    case 'tabs.reload': {
      await chrome.tabs.reload(p.tabId, { bypassCache: !!p.bypassCache });
      return { ok: true };
    }
    case 'tabs.find': {
      const tabs = await getCachedTabs();
      const urlQ = (p.url || '').toLowerCase();
      const titleQ = (p.title || '').toLowerCase();
      const match = tabs.filter(t => {
        const urlMatch = urlQ ? (t.url || '').toLowerCase().includes(urlQ) : true;
        const titleMatch = titleQ ? (t.title || '').toLowerCase().includes(titleQ) : true;
        return (urlQ || titleQ) && urlMatch && titleMatch;
      });
      return match.map(t => ({
        id: t.id, windowId: t.windowId, url: t.url, title: t.title, active: t.active
      }));
    }
    case 'tabs.duplicate': {
      const t = await chrome.tabs.duplicate(p.tabId);
      invalidateTabCache();
      return { id: t.id, windowId: t.windowId, url: t.url };
    }
    case 'tabs.move': {
      await chrome.tabs.move(p.tabId, { index: p.index ?? -1, windowId: p.windowId });
      return { ok: true };
    }
    case 'tabs.pin': {
      await chrome.tabs.update(p.tabId, { pinned: p.pinned !== false });
      return { ok: true };
    }
    case 'tabs.mute': {
      await chrome.tabs.update(p.tabId, { muted: p.muted !== false });
      return { ok: true };
    }
    case 'tabs.discard': {
      await chrome.tabs.discard(p.tabId);
      return { ok: true };
    }

    // ════════════════════════════════════════
    // ── Tab Groups ──
    // ════════════════════════════════════════
    case 'tabGroups.list': {
      const groups = await chrome.tabGroups.query({});
      return groups.map(g => ({
        id: g.id, title: g.title, color: g.color, collapsed: g.collapsed, windowId: g.windowId
      }));
    }
    case 'tabGroups.create': {
      const tabIds = Array.isArray(p.tabIds) ? p.tabIds : [p.tabIds];
      const groupId = await chrome.tabs.group({ tabIds, createProperties: { windowId: p.windowId } });
      if (p.title || p.color) {
        await chrome.tabGroups.update(groupId, {
          title: p.title || undefined,
          color: p.color || undefined,
          collapsed: p.collapsed || false
        });
      }
      return { groupId };
    }
    case 'tabGroups.update': {
      const updates = {};
      if (p.title !== undefined) updates.title = p.title;
      if (p.color !== undefined) updates.color = p.color;
      if (p.collapsed !== undefined) updates.collapsed = p.collapsed;
      await chrome.tabGroups.update(p.groupId, updates);
      return { ok: true };
    }
    case 'tabGroups.ungroup': {
      const tabIds = Array.isArray(p.tabIds) ? p.tabIds : [p.tabIds];
      await chrome.tabs.ungroup(tabIds);
      return { ok: true };
    }
    case 'tabGroups.addTabs': {
      const tabIds = Array.isArray(p.tabIds) ? p.tabIds : [p.tabIds];
      await chrome.tabs.group({ tabIds, groupId: p.groupId });
      return { ok: true };
    }

    // ════════════════════════════════════════
    // ── Window Management ──
    // ════════════════════════════════════════
    case 'windows.list': {
      const wins = await chrome.windows.getAll({ populate: false });
      return wins.map(w => ({ id: w.id, type: w.type, state: w.state, focused: w.focused,
        width: w.width, height: w.height, top: w.top, left: w.left }));
    }
    case 'windows.focus': {
      await chrome.windows.update(p.windowId, { focused: true });
      return { ok: true };
    }
    case 'windows.create': {
      const w = await chrome.windows.create({ url: p.url, type: p.type || 'normal',
        width: p.width, height: p.height, top: p.top, left: p.left, state: p.state });
      return { id: w.id };
    }
    case 'windows.update': {
      const updates = {};
      if (p.state) updates.state = p.state;
      if (p.width) updates.width = p.width;
      if (p.height) updates.height = p.height;
      if (p.top !== undefined) updates.top = p.top;
      if (p.left !== undefined) updates.left = p.left;
      if (p.focused !== undefined) updates.focused = p.focused;
      await chrome.windows.update(p.windowId, updates);
      return { ok: true };
    }
    case 'windows.close': {
      await chrome.windows.remove(p.windowId);
      return { ok: true };
    }

    // ════════════════════════════════════════
    // ── Script Execution ──
    // ════════════════════════════════════════
    case 'eval': {
      // Default: use scripting API (no debugger banner). Falls back to CDP only if p.useCDP is set.
      if (p.useCDP) {
        await chrome.debugger.attach({ tabId: p.tabId }, '1.3');
        try {
          const res = await chrome.debugger.sendCommand(
            { tabId: p.tabId }, 'Runtime.evaluate',
            { expression: p.expression, returnByValue: true, userGesture: true, awaitPromise: !!p.awaitPromise }
          );
          if (res.exceptionDetails) {
            return { __error: res.exceptionDetails.text || res.exceptionDetails.exception?.description };
          }
          return res.result?.value;
        } finally {
          await chrome.debugger.detach({ tabId: p.tabId }).catch(() => {});
        }
      }
      // Scripting API path — no banner
      const evalResults = await chrome.scripting.executeScript({
        target: { tabId: p.tabId, allFrames: !!p.allFrames },
        func: (expr, awaitP) => {
          try {
            const r = eval(expr);
            if (awaitP && r && typeof r.then === 'function') return r;
            return r;
          } catch(e) { return { __error: e.message }; }
        },
        args: [p.expression, !!p.awaitPromise],
        world: p.isolated ? 'ISOLATED' : 'MAIN'
      });
      return evalResults[0]?.result;
    }
    case 'eval.cdp': {
      // Explicit CDP eval — always uses debugger (shows banner)
      await chrome.debugger.attach({ tabId: p.tabId }, '1.3');
      try {
        const res = await chrome.debugger.sendCommand(
          { tabId: p.tabId }, 'Runtime.evaluate',
          { expression: p.expression, returnByValue: true, userGesture: true, awaitPromise: !!p.awaitPromise }
        );
        if (res.exceptionDetails) {
          return { __error: res.exceptionDetails.text || res.exceptionDetails.exception?.description };
        }
        return res.result?.value;
      } finally {
        await chrome.debugger.detach({ tabId: p.tabId }).catch(() => {});
      }
    }
    case 'eval.safe': {
      // eval via chrome.scripting (no debugger bar, but subject to CSP)
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId, allFrames: !!p.allFrames },
        func: (expr) => { try { return eval(expr); } catch(e) { return { __error: e.message }; } },
        args: [p.expression],
        world: p.isolated ? 'ISOLATED' : 'MAIN'
      });
      return results[0]?.result;
    }

    // ════════════════════════════════════════
    // ── Screenshot ──
    // ════════════════════════════════════════
    case 'screenshot': {
      if (p.activate !== false) {
        const tab = await chrome.tabs.get(p.tabId);
        await chrome.tabs.update(p.tabId, { active: true });
        await chrome.windows.update(tab.windowId, { focused: true });
        await new Promise(r => setTimeout(r, 150));
      }
      const tab = await chrome.tabs.get(p.tabId);
      const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, {
        format: p.format || 'png', quality: p.quality || 80
      });
      return { dataUrl };
    }
    case 'screenshot.full': {
      await ensureDebugger(p.tabId);
      const layout = await cdpSend(p.tabId, 'Page.getLayoutMetrics', {});
      const { width, height } = layout.contentSize || layout.cssContentSize || {};
      await cdpSend(p.tabId, 'Emulation.setDeviceMetricsOverride', {
        width: Math.ceil(width || 1920), height: Math.ceil(height || 1080),
        deviceScaleFactor: 1, mobile: false
      });
      const result = await cdpSend(p.tabId, 'Page.captureScreenshot', {
        format: p.format || 'jpeg', quality: p.quality || 80, captureBeyondViewport: true
      });
      await cdpSend(p.tabId, 'Emulation.clearDeviceMetricsOverride', {});
      return { data: result.data };
    }
    case 'screenshot.element': {
      await ensureDebugger(p.tabId);
      const doc = await cdpSend(p.tabId, 'DOM.getDocument', { depth: 0 });
      const node = await cdpSend(p.tabId, 'DOM.querySelector', {
        nodeId: doc.root.nodeId, selector: p.selector
      });
      if (!node.nodeId) return { __error: 'Element not found: ' + p.selector };
      const box = await cdpSend(p.tabId, 'DOM.getBoxModel', { nodeId: node.nodeId });
      const quad = box.model.border;
      const clip = {
        x: quad[0], y: quad[1],
        width: quad[2] - quad[0], height: quad[5] - quad[1],
        scale: 1
      };
      const result = await cdpSend(p.tabId, 'Page.captureScreenshot', {
        format: p.format || 'png', quality: p.quality || 90, clip
      });
      return { data: result.data };
    }

    // ════════════════════════════════════════
    // ── Click/Type (Lite — via JS injection) ──
    // ════════════════════════════════════════
    case 'click': {
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: (selector, x, y) => {
          if (selector) {
            const el = document.querySelector(selector);
            if (!el) return { error: 'Element not found: ' + selector };
            el.click();
            const r = el.getBoundingClientRect();
            return { clicked: selector, x: r.left + r.width/2, y: r.top + r.height/2 };
          }
          if (x !== undefined && y !== undefined) {
            const el = document.elementFromPoint(x, y);
            if (el) { el.click(); return { clicked: el.tagName, x, y }; }
            return { error: 'No element at ' + x + ',' + y };
          }
          return { error: 'Provide selector or x,y' };
        },
        args: [p.selector, p.x, p.y],
        world: 'MAIN'
      });
      return results[0]?.result;
    }
    case 'click.text': {
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: (text, tag, partial) => {
          const els = document.querySelectorAll(tag || '*');
          for (const el of els) {
            if (!el.innerText || el.offsetParent === null) continue;
            const t = el.innerText.trim();
            if (partial ? t.includes(text) : t === text) {
              el.scrollIntoView({ block: 'center' });
              el.click();
              const r = el.getBoundingClientRect();
              return { clicked: text, x: r.left + r.width/2, y: r.top + r.height/2 };
            }
          }
          return { error: 'Text not found: ' + text };
        },
        args: [p.text, p.tag, p.partial],
        world: 'MAIN'
      });
      return results[0]?.result;
    }
    case 'type': {
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: (selector, text, clear) => {
          const el = selector ? document.querySelector(selector) : document.activeElement;
          if (!el) return { error: 'Element not found' };
          el.focus();
          if (clear) { el.value = ''; el.innerText = ''; }
          if (el.isContentEditable) {
            document.execCommand('insertText', false, text);
          } else {
            el.value += text;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
          }
          return { typed: text.length + ' chars' };
        },
        args: [p.selector, p.text, p.clear],
        world: 'MAIN'
      });
      return results[0]?.result;
    }
    case 'scroll': {
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: (x, y, selector, behavior) => {
          const target = selector ? document.querySelector(selector) : window;
          if (selector && !target) return { error: 'Element not found' };
          if (target === window) {
            window.scrollBy({ left: x || 0, top: y || 0, behavior: behavior || 'auto' });
          } else {
            target.scrollBy({ left: x || 0, top: y || 0, behavior: behavior || 'auto' });
          }
          return { scrolled: true, scrollX: window.scrollX, scrollY: window.scrollY };
        },
        args: [p.deltaX || 0, p.deltaY || p.delta || 0, p.selector, p.behavior],
        world: 'MAIN'
      });
      return results[0]?.result;
    }
    case 'scroll.to': {
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: (selector, x, y, behavior) => {
          if (selector) {
            const el = document.querySelector(selector);
            if (!el) return { error: 'Element not found' };
            el.scrollIntoView({ behavior: behavior || 'smooth', block: 'center' });
            return { scrolledTo: selector };
          }
          window.scrollTo({ left: x || 0, top: y || 0, behavior: behavior || 'smooth' });
          return { scrolledTo: { x: x || 0, y: y || 0 } };
        },
        args: [p.selector, p.x, p.y, p.behavior],
        world: 'MAIN'
      });
      return results[0]?.result;
    }
    case 'select': {
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: (selector) => {
          const el = document.querySelector(selector);
          if (!el) return null;
          const r = el.getBoundingClientRect();
          return { x: r.left + r.width/2, y: r.top + r.height/2,
                   width: r.width, height: r.height, text: el.innerText.substring(0, 200),
                   tag: el.tagName, id: el.id, classes: el.className };
        },
        args: [p.selector],
        world: 'MAIN'
      });
      return results[0]?.result;
    }
    case 'selectAll': {
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: (selector, limit) => {
          const els = document.querySelectorAll(selector);
          return Array.from(els).slice(0, limit || 100).map(el => {
            const r = el.getBoundingClientRect();
            return { x: r.left + r.width/2, y: r.top + r.height/2,
                     width: r.width, height: r.height, text: el.innerText?.substring(0, 200),
                     tag: el.tagName, visible: el.offsetParent !== null };
          });
        },
        args: [p.selector, p.limit],
        world: 'MAIN'
      });
      return results[0]?.result;
    }

    // ════════════════════════════════════════
    // ── CDP Passthrough ──
    // ════════════════════════════════════════
    case 'cdp': {
      await ensureDebugger(p.tabId);
      return await cdpSend(p.tabId, p.method, p.params || {});
    }

    // ════════════════════════════════════════
    // ── CDP Input (mouse/keyboard/touch via debugger) ──
    // ════════════════════════════════════════
    case 'input.mouse': {
      await ensureDebugger(p.tabId);
      const mouseX = p.x || 0, mouseY = p.y || 0;
      const mouseBtn = p.button || 'left';
      const clickCnt = p.clickCount || 1;
      const mods = p.modifiers || 0;
      await cdpSend(p.tabId, 'Input.dispatchMouseEvent', {
        type: 'mouseMoved', x: mouseX, y: mouseY, modifiers: mods
      });
      if (p.type === 'click' || !p.type) {
        await cdpSend(p.tabId, 'Input.dispatchMouseEvent', {
          type: 'mousePressed', x: mouseX, y: mouseY, button: mouseBtn,
          clickCount: clickCnt, modifiers: mods
        });
        await cdpSend(p.tabId, 'Input.dispatchMouseEvent', {
          type: 'mouseReleased', x: mouseX, y: mouseY, button: mouseBtn,
          clickCount: clickCnt, modifiers: mods
        });
      } else if (p.type === 'hover') {
        // mouseMoved already dispatched above
      } else if (p.type === 'dblclick') {
        for (let i = 0; i < 2; i++) {
          await cdpSend(p.tabId, 'Input.dispatchMouseEvent', {
            type: 'mousePressed', x: mouseX, y: mouseY, button: mouseBtn,
            clickCount: i + 1, modifiers: mods
          });
          await cdpSend(p.tabId, 'Input.dispatchMouseEvent', {
            type: 'mouseReleased', x: mouseX, y: mouseY, button: mouseBtn,
            clickCount: i + 1, modifiers: mods
          });
        }
      } else if (p.type === 'contextmenu') {
        await cdpSend(p.tabId, 'Input.dispatchMouseEvent', {
          type: 'mousePressed', x: mouseX, y: mouseY, button: 'right',
          clickCount: 1, modifiers: mods
        });
        await cdpSend(p.tabId, 'Input.dispatchMouseEvent', {
          type: 'mouseReleased', x: mouseX, y: mouseY, button: 'right',
          clickCount: 1, modifiers: mods
        });
      }
      return { ok: true };
    }
    case 'input.key': {
      await ensureDebugger(p.tabId);
      const keyMap = {
        Enter: [13, '\r'], Tab: [9, ''], Backspace: [8, ''], Escape: [27, ''],
        ArrowUp: [38, ''], ArrowDown: [40, ''], ArrowLeft: [37, ''], ArrowRight: [39, ''],
        Delete: [46, ''], Home: [36, ''], End: [35, ''], Space: [32, ' '],
        PageUp: [33, ''], PageDown: [34, ''],
        F1: [112,''], F2: [113,''], F3: [114,''], F4: [115,''],
        F5: [116,''], F6: [117,''], F7: [118,''], F8: [119,''],
        F9: [120,''], F10: [121,''], F11: [122,''], F12: [123,''],
        a: [65,'a'], b: [66,'b'], c: [67,'c'], d: [68,'d'], e: [69,'e'],
        f: [70,'f'], g: [71,'g'], h: [72,'h'], i: [73,'i'], j: [74,'j'],
        k: [75,'k'], l: [76,'l'], m: [77,'m'], n: [78,'n'], o: [79,'o'],
        p: [80,'p'], q: [81,'q'], r: [82,'r'], s: [83,'s'], t: [84,'t'],
        u: [85,'u'], v: [86,'v'], w: [87,'w'], x: [88,'x'], y: [89,'y'], z: [90,'z']
      };
      if (p.text) {
        for (const char of p.text) {
          await cdpSend(p.tabId, 'Input.dispatchKeyEvent', {
            type: 'keyDown', text: char, key: char, unmodifiedText: char
          });
          await cdpSend(p.tabId, 'Input.dispatchKeyEvent', { type: 'keyUp', key: char });
        }
      } else if (p.key) {
        const [code, text] = keyMap[p.key] || [0, ''];
        const params = { type: 'rawKeyDown', key: p.key,
          windowsVirtualKeyCode: code, nativeVirtualKeyCode: code,
          code: p.code || '', modifiers: p.modifiers || 0 };
        if (text) params.text = text;
        await cdpSend(p.tabId, 'Input.dispatchKeyEvent', params);
        await cdpSend(p.tabId, 'Input.dispatchKeyEvent', { ...params, type: 'keyUp' });
      }
      return { ok: true };
    }
    case 'input.insertText': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Input.insertText', { text: p.text });
      return { ok: true };
    }
    case 'input.touch': {
      await ensureDebugger(p.tabId);
      const touchPoints = [{ x: p.x, y: p.y }];
      await cdpSend(p.tabId, 'Input.dispatchTouchEvent', { type: 'touchStart', touchPoints });
      if (p.type === 'swipe' && p.toX !== undefined) {
        const swipeSteps = p.steps || 8;
        for (let si = 1; si <= swipeSteps; si++) {
          const sx = p.x + (p.toX - p.x) * (si / swipeSteps);
          const sy = p.y + ((p.toY ?? p.y) - p.y) * (si / swipeSteps);
          await cdpSend(p.tabId, 'Input.dispatchTouchEvent', {
            type: 'touchMove', touchPoints: [{ x: Math.round(sx), y: Math.round(sy) }]
          });
        }
      }
      await cdpSend(p.tabId, 'Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
      return { ok: true };
    }

    // ════════════════════════════════════════
    // ── Network Interception ──
    // ════════════════════════════════════════
    case 'network.enable': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Network.enable', {});
      return { ok: true };
    }
    case 'network.intercept': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Fetch.enable', {
        patterns: (p.patterns || [{ urlPattern: '*' }]).map(pat => ({
          urlPattern: pat.urlPattern || '*',
          requestStage: pat.stage || 'Request'
        }))
      });
      return { ok: true };
    }
    case 'network.getRequests': {
      // Get recent requests via performance entries
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: (filter) => {
          const entries = performance.getEntriesByType('resource');
          let results = entries.map(e => ({
            url: e.name, type: e.initiatorType, duration: Math.round(e.duration),
            size: e.transferSize, startTime: Math.round(e.startTime)
          }));
          if (filter) {
            const f = filter.toLowerCase();
            results = results.filter(r => r.url.toLowerCase().includes(f));
          }
          return results.slice(-200);
        },
        args: [p.filter],
        world: 'MAIN'
      });
      return results[0]?.result;
    }
    case 'network.block': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Network.setBlockedURLs', { urls: p.urls || [] });
      return { ok: true };
    }
    case 'network.emulateConditions': {
      await ensureDebugger(p.tabId);
      const presets = {
        'slow3g': { offline: false, latency: 2000, downloadThroughput: 50000, uploadThroughput: 50000 },
        'fast3g': { offline: false, latency: 560, downloadThroughput: 188000, uploadThroughput: 86000 },
        'offline': { offline: true, latency: 0, downloadThroughput: 0, uploadThroughput: 0 },
        'none': { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 }
      };
      const condition = presets[p.preset] || p;
      await cdpSend(p.tabId, 'Network.emulateNetworkConditions', condition);
      return { ok: true };
    }

    // ════════════════════════════════════════
    // ── Console Capture ──
    // ════════════════════════════════════════
    case 'console.enable': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Runtime.enable', {});
      return { ok: true };
    }
    case 'console.getLogs': {
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: (level) => {
          // Install log capture if not present
          if (!window.__bridgeLogs) {
            window.__bridgeLogs = [];
            const orig = {};
            ['log', 'warn', 'error', 'info', 'debug'].forEach(m => {
              orig[m] = console[m];
              console[m] = (...args) => {
                window.__bridgeLogs.push({
                  level: m, message: args.map(a => typeof a === 'object' ? JSON.stringify(a) : String(a)).join(' '),
                  timestamp: Date.now()
                });
                if (window.__bridgeLogs.length > 500) window.__bridgeLogs.shift();
                orig[m].apply(console, args);
              };
            });
          }
          let logs = window.__bridgeLogs;
          if (level) logs = logs.filter(l => l.level === level);
          return logs.slice(-100);
        },
        args: [p.level],
        world: 'MAIN'
      });
      return results[0]?.result;
    }
    case 'console.clear': {
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: () => { if (window.__bridgeLogs) window.__bridgeLogs = []; return { ok: true }; },
        world: 'MAIN'
      });
      return results[0]?.result;
    }

    // ════════════════════════════════════════
    // ── Storage Access ──
    // ════════════════════════════════════════
    case 'storage.local.get': {
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: (key) => {
          if (key) return localStorage.getItem(key);
          const data = {};
          for (let i = 0; i < localStorage.length; i++) {
            const k = localStorage.key(i);
            data[k] = localStorage.getItem(k);
          }
          return data;
        },
        args: [p.key],
        world: 'MAIN'
      });
      return results[0]?.result;
    }
    case 'storage.local.set': {
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: (key, value) => { localStorage.setItem(key, value); return { ok: true }; },
        args: [p.key, p.value],
        world: 'MAIN'
      });
      return results[0]?.result;
    }
    case 'storage.local.remove': {
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: (key) => { localStorage.removeItem(key); return { ok: true }; },
        args: [p.key],
        world: 'MAIN'
      });
      return results[0]?.result;
    }
    case 'storage.local.clear': {
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: () => { localStorage.clear(); return { ok: true }; },
        world: 'MAIN'
      });
      return results[0]?.result;
    }
    case 'storage.session.get': {
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: (key) => {
          if (key) return sessionStorage.getItem(key);
          const data = {};
          for (let i = 0; i < sessionStorage.length; i++) {
            const k = sessionStorage.key(i);
            data[k] = sessionStorage.getItem(k);
          }
          return data;
        },
        args: [p.key],
        world: 'MAIN'
      });
      return results[0]?.result;
    }
    case 'storage.session.set': {
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: (key, value) => { sessionStorage.setItem(key, value); return { ok: true }; },
        args: [p.key, p.value],
        world: 'MAIN'
      });
      return results[0]?.result;
    }

    // ════════════════════════════════════════
    // ── CSS Injection ──
    // ════════════════════════════════════════
    case 'css.inject': {
      await chrome.scripting.insertCSS({
        target: { tabId: p.tabId },
        css: p.css,
        origin: p.origin || 'USER'
      });
      return { ok: true };
    }
    case 'css.remove': {
      await chrome.scripting.removeCSS({
        target: { tabId: p.tabId },
        css: p.css,
        origin: p.origin || 'USER'
      });
      return { ok: true };
    }

    // ════════════════════════════════════════
    // ── PDF Generation ──
    // ════════════════════════════════════════
    case 'page.pdf': {
      await ensureDebugger(p.tabId);
      const result = await cdpSend(p.tabId, 'Page.printToPDF', {
        landscape: !!p.landscape,
        printBackground: p.printBackground !== false,
        paperWidth: p.paperWidth || 8.5,
        paperHeight: p.paperHeight || 11,
        marginTop: p.marginTop ?? 0.4,
        marginBottom: p.marginBottom ?? 0.4,
        marginLeft: p.marginLeft ?? 0.4,
        marginRight: p.marginRight ?? 0.4,
        scale: p.scale || 1,
        headerTemplate: p.headerTemplate || '',
        footerTemplate: p.footerTemplate || '',
        displayHeaderFooter: !!p.displayHeaderFooter,
        preferCSSPageSize: !!p.preferCSSPageSize
      });
      return { data: result.data, stream: result.stream };
    }

    // ════════════════════════════════════════
    // ── Device Emulation ──
    // ════════════════════════════════════════
    case 'emulate.device': {
      await ensureDebugger(p.tabId);
      const devices = {
        'iphone14': { width: 390, height: 844, deviceScaleFactor: 3, mobile: true,
          userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1' },
        'ipad': { width: 810, height: 1080, deviceScaleFactor: 2, mobile: true,
          userAgent: 'Mozilla/5.0 (iPad; CPU OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1' },
        'pixel7': { width: 412, height: 915, deviceScaleFactor: 2.625, mobile: true,
          userAgent: 'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36' },
        'desktop1080': { width: 1920, height: 1080, deviceScaleFactor: 1, mobile: false },
        'desktop4k': { width: 3840, height: 2160, deviceScaleFactor: 1, mobile: false }
      };
      const device = devices[p.device] || {
        width: p.width || 1920, height: p.height || 1080,
        deviceScaleFactor: p.deviceScaleFactor || 1, mobile: !!p.mobile
      };
      await cdpSend(p.tabId, 'Emulation.setDeviceMetricsOverride', device);
      if (device.userAgent || p.userAgent) {
        await cdpSend(p.tabId, 'Emulation.setUserAgentOverride', {
          userAgent: p.userAgent || device.userAgent
        });
      }
      return { ok: true, device };
    }
    case 'emulate.clear': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Emulation.clearDeviceMetricsOverride', {});
      return { ok: true };
    }
    case 'emulate.geolocation': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Emulation.setGeolocationOverride', {
        latitude: p.latitude, longitude: p.longitude, accuracy: p.accuracy || 100
      });
      return { ok: true };
    }
    case 'emulate.timezone': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Emulation.setTimezoneOverride', { timezoneId: p.timezoneId });
      return { ok: true };
    }
    case 'emulate.media': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Emulation.setEmulatedMedia', {
        media: p.media || '',
        features: p.features || []
      });
      return { ok: true };
    }

    // ════════════════════════════════════════
    // ── Performance Metrics ──
    // ════════════════════════════════════════
    case 'performance.metrics': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Performance.enable', {});
      const { metrics: m } = await cdpSend(p.tabId, 'Performance.getMetrics', {});
      const result = {};
      for (const { name, value } of m) result[name] = value;
      return result;
    }
    case 'performance.timing': {
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: () => {
          const nav = performance.getEntriesByType('navigation')[0] || {};
          const paint = {};
          performance.getEntriesByType('paint').forEach(e => paint[e.name] = Math.round(e.startTime));
          return {
            dns: Math.round(nav.domainLookupEnd - nav.domainLookupStart),
            tcp: Math.round(nav.connectEnd - nav.connectStart),
            ttfb: Math.round(nav.responseStart - nav.requestStart),
            download: Math.round(nav.responseEnd - nav.responseStart),
            domInteractive: Math.round(nav.domInteractive),
            domComplete: Math.round(nav.domComplete),
            loadEvent: Math.round(nav.loadEventEnd - nav.loadEventStart),
            totalLoad: Math.round(nav.loadEventEnd - nav.startTime),
            transferSize: nav.transferSize,
            encodedBodySize: nav.encodedBodySize,
            decodedBodySize: nav.decodedBodySize,
            ...paint,
            resourceCount: performance.getEntriesByType('resource').length
          };
        },
        world: 'MAIN'
      });
      return results[0]?.result;
    }
    case 'performance.webVitals': {
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: () => {
          const vitals = {};
          // LCP
          const lcp = performance.getEntriesByType('largest-contentful-paint');
          if (lcp.length) vitals.LCP = Math.round(lcp[lcp.length - 1].startTime);
          // FCP
          const fcp = performance.getEntriesByType('paint').find(e => e.name === 'first-contentful-paint');
          if (fcp) vitals.FCP = Math.round(fcp.startTime);
          // FP
          const fp = performance.getEntriesByType('paint').find(e => e.name === 'first-paint');
          if (fp) vitals.FP = Math.round(fp.startTime);
          // CLS
          const cls = performance.getEntriesByType('layout-shift');
          if (cls.length) vitals.CLS = cls.reduce((sum, e) => sum + (e.hadRecentInput ? 0 : e.value), 0);
          // Total Blocking Time estimate
          const longTasks = performance.getEntriesByType('longtask');
          if (longTasks.length) vitals.TBT = Math.round(longTasks.reduce((sum, t) => sum + Math.max(0, t.duration - 50), 0));
          // Resource summary
          const res = performance.getEntriesByType('resource');
          vitals.resources = res.length;
          vitals.totalTransferred = res.reduce((sum, r) => sum + (r.transferSize || 0), 0);
          return vitals;
        },
        world: 'MAIN'
      });
      return results[0]?.result;
    }

    // ════════════════════════════════════════
    // ── Accessibility ──
    // ════════════════════════════════════════
    case 'a11y.snapshot': {
      await ensureDebugger(p.tabId);
      const { nodes } = await cdpSend(p.tabId, 'Accessibility.getFullAXTree', { depth: p.depth || 4 });
      return nodes?.slice(0, p.limit || 500).map(n => ({
        nodeId: n.nodeId, role: n.role?.value, name: n.name?.value,
        value: n.value?.value, description: n.description?.value,
        properties: n.properties?.map(pp => ({ name: pp.name, value: pp.value?.value }))
      }));
    }
    case 'a11y.query': {
      // Use content script for lighter-weight a11y
      return sendContentCommand(p.tabId, 'a11y.snapshot', { root: p.root });
    }

    // ════════════════════════════════════════
    // ── Content Script Commands ──
    // ════════════════════════════════════════
    case 'xpath': return sendContentCommand(p.tabId, 'xpath', p);
    case 'xpath.all': return sendContentCommand(p.tabId, 'xpath.all', p);
    case 'shadow': return sendContentCommand(p.tabId, 'shadow', p);
    case 'highlight': return sendContentCommand(p.tabId, 'highlight', p);
    case 'highlight.clear': return sendContentCommand(p.tabId, 'highlight.clear', p);
    case 'mutation.start': return sendContentCommand(p.tabId, 'mutation.start', p);
    case 'mutation.stop': return sendContentCommand(p.tabId, 'mutation.stop', p);
    case 'mutation.flush': return sendContentCommand(p.tabId, 'mutation.flush', p);
    case 'elements.at': return sendContentCommand(p.tabId, 'elements.at', p);
    case 'element.bounds': return sendContentCommand(p.tabId, 'element.bounds', p);
    case 'element.computed': return sendContentCommand(p.tabId, 'element.computed', p);
    case 'forms.detect': return sendContentCommand(p.tabId, 'forms.detect', p);
    case 'forms.fill': return sendContentCommand(p.tabId, 'forms.fill', p);
    case 'links.extract': return sendContentCommand(p.tabId, 'links.extract', p);
    case 'tables.extract': return sendContentCommand(p.tabId, 'tables.extract', p);
    case 'search.text': return sendContentCommand(p.tabId, 'search.text', p);
    case 'meta.extract': return sendContentCommand(p.tabId, 'meta.extract', p);
    case 'scroll.infinite': return sendContentCommand(p.tabId, 'scroll.infinite', p);

    // Smart element commands (content script)
    case 'smart.find': return sendContentCommand(p.tabId, 'smart.find', p);
    case 'smart.findAll': return sendContentCommand(p.tabId, 'smart.findAll', p);
    case 'smart.click': return sendContentCommand(p.tabId, 'smart.click', p);
    case 'smart.fill': return sendContentCommand(p.tabId, 'smart.fill', p);
    case 'smart.wait': return sendContentCommand(p.tabId, 'smart.wait', p);
    case 'element.interactive': return sendContentCommand(p.tabId, 'element.interactive', p);
    case 'dom.diff': return sendContentCommand(p.tabId, 'dom.diff', p);
    case 'dom.snapshot': return sendContentCommand(p.tabId, 'dom.snapshot', p);
    case 'drag': return sendContentCommand(p.tabId, 'drag', p);
    case 'record.start': return sendContentCommand(p.tabId, 'record.start', p);
    case 'record.stop': return sendContentCommand(p.tabId, 'record.stop', p);
    case 'record.replay': return sendContentCommand(p.tabId, 'record.replay', p);
    case 'page.readiness': return sendContentCommand(p.tabId, 'page.readiness', p);
    case 'stealth.check': return sendContentCommand(p.tabId, 'stealth.check', p);
    case 'iframe.list': return sendContentCommand(p.tabId, 'iframe.list', p);
    case 'iframe.eval': return sendContentCommand(p.tabId, 'iframe.eval', p);

    // Advanced content script commands (v3.1)
    case 'element.waitGone': return sendContentCommand(p.tabId, 'element.waitGone', p);
    case 'dom.waitStable': return sendContentCommand(p.tabId, 'dom.waitStable', p);
    case 'element.attributes': return sendContentCommand(p.tabId, 'element.attributes', p);
    case 'element.setAttribute': return sendContentCommand(p.tabId, 'element.setAttribute', p);
    case 'element.xpath': return sendContentCommand(p.tabId, 'element.xpath', p);
    case 'element.dispatchEvent': return sendContentCommand(p.tabId, 'element.dispatchEvent', p);
    case 'element.parent': return sendContentCommand(p.tabId, 'element.parent', p);
    case 'element.children': return sendContentCommand(p.tabId, 'element.children', p);
    case 'element.siblings': return sendContentCommand(p.tabId, 'element.siblings', p);
    case 'scroll.position': return sendContentCommand(p.tabId, 'scroll.position', p);
    case 'intersection.observe': return sendContentCommand(p.tabId, 'intersection.observe', p);
    case 'intersection.check': return sendContentCommand(p.tabId, 'intersection.check', p);
    case 'intersection.stop': return sendContentCommand(p.tabId, 'intersection.stop', p);
    case 'canvas.readPixels': return sendContentCommand(p.tabId, 'canvas.readPixels', p);
    case 'network.observeXHR': return sendContentCommand(p.tabId, 'network.observeXHR', p);
    case 'network.flushXHR': return sendContentCommand(p.tabId, 'network.flushXHR', p);
    case 'smart.select': return sendContentCommand(p.tabId, 'smart.select', p);
    case 'element.highlight.multiple': return sendContentCommand(p.tabId, 'element.highlight.multiple', p);

    // ════════════════════════════════════════
    // ── Network Interception (Response Bodies) ──
    // ════════════════════════════════════════
    case 'fetch.enable': {
      await ensureDebugger(p.tabId);
      const patterns = p.patterns || [{ urlPattern: '*', requestStage: 'Response' }];
      await cdpSend(p.tabId, 'Fetch.enable', { patterns, handleAuthRequests: !!p.handleAuth });
      return { ok: true };
    }
    case 'fetch.disable': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Fetch.disable', {});
      return { ok: true };
    }
    case 'fetch.getBody': {
      await ensureDebugger(p.tabId);
      const body = await cdpSend(p.tabId, 'Fetch.getResponseBody', { requestId: p.requestId });
      return body;
    }
    case 'fetch.fulfill': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Fetch.fulfillRequest', {
        requestId: p.requestId,
        responseCode: p.responseCode || 200,
        responseHeaders: p.responseHeaders || [],
        body: p.body ? btoa(p.body) : undefined
      });
      return { ok: true };
    }
    case 'fetch.continue': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Fetch.continueRequest', {
        requestId: p.requestId,
        url: p.url,
        method: p.method,
        headers: p.headers
      });
      return { ok: true };
    }
    case 'fetch.fail': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Fetch.failRequest', {
        requestId: p.requestId,
        reason: p.reason || 'Failed'
      });
      return { ok: true };
    }

    // ════════════════════════════════════════
    // ── HAR / Network Recording ──
    // ════════════════════════════════════════
    case 'har.start': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Network.enable', {
        maxTotalBufferSize: p.maxBuffer || 10000000,
        maxResourceBufferSize: p.maxResourceBuffer || 5000000
      });
      // Store recording state
      if (!globalThis._harRecording) globalThis._harRecording = {};
      globalThis._harRecording[p.tabId] = { entries: [], startTime: Date.now() };
      return { ok: true, recording: true };
    }
    case 'har.stop': {
      const rec = globalThis._harRecording?.[p.tabId];
      if (!rec) return { __error: 'No HAR recording for this tab' };
      // Get all network data
      await ensureDebugger(p.tabId);
      const result = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: () => {
          const resources = performance.getEntriesByType('resource');
          return resources.map(r => ({
            name: r.name, type: r.initiatorType,
            startTime: Math.round(r.startTime),
            duration: Math.round(r.duration),
            transferSize: r.transferSize,
            encodedBodySize: r.encodedBodySize,
            decodedBodySize: r.decodedBodySize,
            dns: Math.round(r.domainLookupEnd - r.domainLookupStart),
            tcp: Math.round(r.connectEnd - r.connectStart),
            ttfb: Math.round(r.responseStart - r.requestStart),
            download: Math.round(r.responseEnd - r.responseStart),
            protocol: r.nextHopProtocol,
          }));
        },
        world: 'MAIN'
      });
      delete globalThis._harRecording[p.tabId];
      await cdpSend(p.tabId, 'Network.disable', {});
      return {
        entries: result[0]?.result || [],
        duration: Date.now() - rec.startTime,
        entryCount: (result[0]?.result || []).length
      };
    }

    // ════════════════════════════════════════
    // ── JS/CSS Coverage ──
    // ════════════════════════════════════════
    case 'coverage.startJS': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Profiler.enable', {});
      await cdpSend(p.tabId, 'Profiler.startPreciseCoverage', {
        callCount: true, detailed: !!p.detailed
      });
      return { ok: true };
    }
    case 'coverage.stopJS': {
      await ensureDebugger(p.tabId);
      const coverage = await cdpSend(p.tabId, 'Profiler.takePreciseCoverage', {});
      await cdpSend(p.tabId, 'Profiler.stopPreciseCoverage', {});
      await cdpSend(p.tabId, 'Profiler.disable', {});
      // Summarize coverage
      const scripts = (coverage.result || []).map(s => {
        const totalBytes = s.functions.reduce((sum, f) => {
          return sum + f.ranges.reduce((rs, r) => rs + (r.endOffset - r.startOffset), 0);
        }, 0);
        const usedBytes = s.functions.reduce((sum, f) => {
          return sum + f.ranges.filter(r => r.count > 0).reduce((rs, r) => rs + (r.endOffset - r.startOffset), 0);
        }, 0);
        return {
          url: s.url, totalBytes, usedBytes,
          coveragePercent: totalBytes ? Math.round(usedBytes / totalBytes * 100) : 0
        };
      }).filter(s => s.url && !s.url.startsWith('chrome-extension'));
      return { scripts, totalScripts: scripts.length };
    }
    case 'coverage.startCSS': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'CSS.enable', {});
      await cdpSend(p.tabId, 'CSS.startRuleUsageTracking', {});
      return { ok: true };
    }
    case 'coverage.stopCSS': {
      await ensureDebugger(p.tabId);
      const usage = await cdpSend(p.tabId, 'CSS.stopRuleUsageTracking', {});
      await cdpSend(p.tabId, 'CSS.disable', {});
      const total = (usage.ruleUsage || []).length;
      const used = (usage.ruleUsage || []).filter(r => r.used).length;
      return {
        totalRules: total, usedRules: used,
        coveragePercent: total ? Math.round(used / total * 100) : 0,
        rules: (usage.ruleUsage || []).slice(0, p.limit || 100)
      };
    }

    // ════════════════════════════════════════
    // ── Stealth Mode ──
    // ════════════════════════════════════════
    case 'stealth.enable': {
      // Apply anti-detection measures
      await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: () => {
          // Remove webdriver flag
          Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
          // Fix plugins
          Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5].map(() => ({
              description: 'Portable Document Format',
              filename: 'internal-pdf-viewer',
              name: 'Chrome PDF Plugin'
            }))
          });
          // Fix languages
          Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
          // Remove automation indicators
          delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
          delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
          delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
          // Fix permissions query
          const origQuery = window.Notification?.permission;
          if (origQuery === 'denied') {
            Object.defineProperty(Notification, 'permission', { get: () => 'default' });
          }
          // Fix chrome.runtime to look natural
          window.chrome = window.chrome || {};
          window.chrome.runtime = window.chrome.runtime || {};
          // Canvas fingerprint noise
          const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
          HTMLCanvasElement.prototype.toDataURL = function(type) {
            const ctx = this.getContext('2d');
            if (ctx) {
              const imageData = ctx.getImageData(0, 0, this.width, this.height);
              for (let i = 0; i < imageData.data.length; i += 4) {
                imageData.data[i] ^= 1; // tiny noise
              }
              ctx.putImageData(imageData, 0, 0);
            }
            return origToDataURL.apply(this, arguments);
          };
        },
        world: 'MAIN',
        injectImmediately: true
      });
      return { ok: true, stealth: true };
    }

    // ════════════════════════════════════════
    // ── DOM Snapshot (CDP) ──
    // ════════════════════════════════════════
    case 'dom.cdpSnapshot': {
      await ensureDebugger(p.tabId);
      const snap = await cdpSend(p.tabId, 'DOMSnapshot.captureSnapshot', {
        computedStyles: p.computedStyles || ['display', 'visibility', 'opacity', 'color', 'background-color', 'font-size'],
        includePaintOrder: !!p.includePaintOrder,
        includeDOMRects: p.includeDOMRects !== false
      });
      return {
        documents: snap.documents?.length || 0,
        strings: snap.strings?.length || 0,
        nodeCount: snap.documents?.[0]?.nodes?.nodeName?.length || 0,
        layoutCount: snap.documents?.[0]?.layout?.nodeIndex?.length || 0,
        snapshot: p.full ? snap : undefined
      };
    }

    case 'input.dragCDP': {
      await ensureDebugger(p.tabId);
      // CDP-level drag: mousePressed → mouseMoved → mouseReleased
      await cdpSend(p.tabId, 'Input.dispatchMouseEvent', {
        type: 'mousePressed', x: p.fromX, y: p.fromY, button: 'left', clickCount: 1
      });
      // Interpolate movement
      const steps = p.steps || 10;
      for (let i = 1; i <= steps; i++) {
        const x = p.fromX + (p.toX - p.fromX) * (i / steps);
        const y = p.fromY + (p.toY - p.fromY) * (i / steps);
        await cdpSend(p.tabId, 'Input.dispatchMouseEvent', {
          type: 'mouseMoved', x: Math.round(x), y: Math.round(y), button: 'left'
        });
      }
      await cdpSend(p.tabId, 'Input.dispatchMouseEvent', {
        type: 'mouseReleased', x: p.toX, y: p.toY, button: 'left', clickCount: 1
      });
      return { ok: true };
    }

    // ════════════════════════════════════════
    // ── Memory Profiling ──
    // ════════════════════════════════════════
    case 'memory.info': {
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: () => {
          const m = performance.memory || {};
          return {
            jsHeapSizeLimit: m.jsHeapSizeLimit,
            totalJSHeapSize: m.totalJSHeapSize,
            usedJSHeapSize: m.usedJSHeapSize,
            usagePercent: m.jsHeapSizeLimit ? Math.round(m.usedJSHeapSize / m.jsHeapSizeLimit * 100) : null
          };
        },
        world: 'MAIN'
      });
      return results[0]?.result;
    }

    // ════════════════════════════════════════
    // ── Security Info ──
    // ════════════════════════════════════════
    case 'security.info': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Security.enable', {});
      // Get certificate info
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: () => ({
          protocol: location.protocol,
          isSecure: location.protocol === 'https:',
          host: location.host,
          origin: location.origin,
        }),
        world: 'MAIN'
      });
      return results[0]?.result;
    }

    // ════════════════════════════════════════
    // ── Clipboard ──
    // ════════════════════════════════════════
    case 'clipboard.write': {
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: async (text) => {
          await navigator.clipboard.writeText(text);
          return { ok: true };
        },
        args: [p.text],
        world: 'MAIN'
      });
      return results[0]?.result;
    }
    case 'clipboard.read': {
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: async () => await navigator.clipboard.readText(),
        world: 'MAIN'
      });
      return results[0]?.result;
    }

    // ════════════════════════════════════════
    // ── File Upload ──
    // ════════════════════════════════════════
    case 'file.upload': {
      await ensureDebugger(p.tabId);
      const doc = await cdpSend(p.tabId, 'DOM.getDocument', { depth: 0 });
      const node = await cdpSend(p.tabId, 'DOM.querySelector', {
        nodeId: doc.root.nodeId, selector: p.selector
      });
      const files = Array.isArray(p.files) ? p.files : [p.files];
      await cdpSend(p.tabId, 'DOM.setFileInputFiles', { nodeId: node.nodeId, files });
      return { ok: true, filesSet: files.length };
    }

    // ════════════════════════════════════════
    // ── Cookies ──
    // ════════════════════════════════════════
    case 'cookies.get': {
      const opts = {};
      if (p.url) opts.url = p.url;
      if (p.domain) opts.domain = p.domain;
      if (p.name) opts.name = p.name;
      const cookies = await chrome.cookies.getAll(opts);
      return cookies;
    }
    case 'cookies.set': {
      const cookie = await chrome.cookies.set(p);
      return cookie;
    }
    case 'cookies.remove': {
      await chrome.cookies.remove({ url: p.url, name: p.name });
      return { ok: true };
    }
    case 'cookies.clearAll': {
      const cookies = await chrome.cookies.getAll({ url: p.url });
      for (const c of cookies) {
        await chrome.cookies.remove({ url: p.url, name: c.name });
      }
      return { ok: true, cleared: cookies.length };
    }

    // ════════════════════════════════════════
    // ── Downloads ──
    // ════════════════════════════════════════
    case 'downloads.list': {
      const items = await chrome.downloads.search(p.query || { limit: 20, orderBy: ['-startTime'] });
      return items.map(d => ({ id: d.id, url: d.url, filename: d.filename,
        state: d.state, bytesReceived: d.bytesReceived, totalBytes: d.totalBytes }));
    }
    case 'downloads.start': {
      const id = await chrome.downloads.download({
        url: p.url, filename: p.filename, saveAs: !!p.saveAs
      });
      return { downloadId: id };
    }

    // ════════════════════════════════════════
    // ── Page Info ──
    // ════════════════════════════════════════
    case 'page.info': {
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: () => ({
          title: document.title, url: window.location.href, readyState: document.readyState,
          scrollY: window.scrollY, scrollX: window.scrollX,
          innerWidth: window.innerWidth, innerHeight: window.innerHeight,
          bodyHeight: document.body?.scrollHeight, bodyWidth: document.body?.scrollWidth,
          elementCount: document.querySelectorAll('*').length,
          charset: document.characterSet, lang: document.documentElement.lang
        }),
        world: 'MAIN'
      });
      return results[0]?.result;
    }
    case 'page.html': {
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: (selector) => {
          if (selector) {
            const el = document.querySelector(selector);
            return el ? el.outerHTML : null;
          }
          return document.documentElement.outerHTML;
        },
        args: [p.selector],
        world: 'MAIN'
      });
      return results[0]?.result;
    }
    case 'page.text': {
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: (selector) => {
          const el = selector ? document.querySelector(selector) : document.body;
          return el ? el.innerText : null;
        },
        args: [p.selector],
        world: 'MAIN'
      });
      return results[0]?.result;
    }
    case 'dom.extract': {
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: (selectors) => {
          const data = {};
          for (const [key, sel] of Object.entries(selectors)) {
            const els = document.querySelectorAll(sel);
            if (els.length === 0) data[key] = null;
            else if (els.length === 1) data[key] = els[0].innerText.trim();
            else data[key] = Array.from(els).map(el => el.innerText.trim());
          }
          return data;
        },
        args: [p.selectors || {}],
        world: 'MAIN'
      });
      return results[0]?.result;
    }

    // ════════════════════════════════════════
    // ── History ──
    // ════════════════════════════════════════
    case 'history.search': {
      const results = await chrome.history.search({
        text: p.text || '', maxResults: p.maxResults || 50,
        startTime: p.startTime || 0
      });
      return results.map(h => ({
        url: h.url, title: h.title, visitCount: h.visitCount, lastVisitTime: h.lastVisitTime
      }));
    }
    case 'history.delete': {
      await chrome.history.deleteUrl({ url: p.url });
      return { ok: true };
    }

    // ════════════════════════════════════════
    // ── Bookmarks ──
    // ════════════════════════════════════════
    case 'bookmarks.search': {
      const results = await chrome.bookmarks.search(p.query || '');
      return results.map(b => ({ id: b.id, title: b.title, url: b.url, parentId: b.parentId }));
    }
    case 'bookmarks.create': {
      const bm = await chrome.bookmarks.create({
        title: p.title, url: p.url, parentId: p.parentId
      });
      return { id: bm.id, title: bm.title, url: bm.url };
    }

    // ════════════════════════════════════════
    // ── Notifications ──
    // ════════════════════════════════════════
    case 'notify': {
      const id = await chrome.notifications.create({
        type: 'basic',
        iconUrl: p.iconUrl || 'icons/icon128.png',
        title: p.title || 'Chrome Bridge',
        message: p.message || '',
        priority: p.priority || 0
      });
      return { notificationId: id };
    }

    // ════════════════════════════════════════
    // ── Wait ──
    // ════════════════════════════════════════
    case 'wait.element': {
      const timeout = p.timeout || 10000;
      const start = Date.now();
      while (Date.now() - start < timeout) {
        const results = await chrome.scripting.executeScript({
          target: { tabId: p.tabId },
          func: (sel) => !!document.querySelector(sel),
          args: [p.selector], world: 'MAIN'
        });
        if (results[0]?.result) return { found: true, elapsed: Date.now() - start };
        await new Promise(r => setTimeout(r, 150));
      }
      return { found: false, elapsed: timeout };
    }
    case 'wait.text': {
      const timeout = p.timeout || 10000;
      const start = Date.now();
      while (Date.now() - start < timeout) {
        const results = await chrome.scripting.executeScript({
          target: { tabId: p.tabId },
          func: (text) => document.body.innerText.includes(text),
          args: [p.text], world: 'MAIN'
        });
        if (results[0]?.result) return { found: true, elapsed: Date.now() - start };
        await new Promise(r => setTimeout(r, 150));
      }
      return { found: false, elapsed: timeout };
    }
    case 'wait.navigation': {
      const timeout = p.timeout || 15000;
      return new Promise((resolve) => {
        const timer = setTimeout(() => {
          chrome.tabs.onUpdated.removeListener(listener);
          resolve({ completed: false });
        }, timeout);
        function listener(tabId, change) {
          if (tabId === p.tabId && change.status === 'complete') {
            clearTimeout(timer);
            chrome.tabs.onUpdated.removeListener(listener);
            resolve({ completed: true });
          }
        }
        chrome.tabs.onUpdated.addListener(listener);
      });
    }
    case 'wait.idle': {
      // Wait for network idle (no requests for idleTime ms)
      const timeout = p.timeout || 30000;
      const idleTime = p.idleTime || 2000;
      const start = Date.now();
      let lastActivity = Date.now();

      return new Promise((resolve) => {
        const timer = setTimeout(() => {
          chrome.webNavigation?.onCompleted?.removeListener(navListener);
          resolve({ idle: false, elapsed: timeout });
        }, timeout);

        const checkIdle = setInterval(() => {
          if (Date.now() - lastActivity > idleTime) {
            clearInterval(checkIdle);
            clearTimeout(timer);
            chrome.webNavigation?.onCompleted?.removeListener(navListener);
            resolve({ idle: true, elapsed: Date.now() - start });
          }
        }, 200);

        function navListener(details) {
          if (details.tabId === p.tabId) lastActivity = Date.now();
        }
        chrome.webNavigation?.onCompleted?.addListener(navListener);
      });
    }
    case 'wait.function': {
      // Poll a JS expression until it returns truthy
      const timeout = p.timeout || 15000;
      const interval = p.interval || 200;
      const start = Date.now();
      while (Date.now() - start < timeout) {
        const results = await chrome.scripting.executeScript({
          target: { tabId: p.tabId },
          func: (expr) => { try { return eval(expr); } catch { return false; } },
          args: [p.expression], world: 'MAIN'
        });
        if (results[0]?.result) return { satisfied: true, elapsed: Date.now() - start, value: results[0].result };
        await new Promise(r => setTimeout(r, interval));
      }
      return { satisfied: false, elapsed: timeout };
    }
    case 'wait.url': {
      // Wait for tab URL to match a pattern
      const timeout = p.timeout || 15000;
      const start = Date.now();
      const pattern = (p.pattern || '').toLowerCase();
      while (Date.now() - start < timeout) {
        const tab = await chrome.tabs.get(p.tabId);
        if (tab.url.toLowerCase().includes(pattern)) return { matched: true, url: tab.url, elapsed: Date.now() - start };
        await new Promise(r => setTimeout(r, 200));
      }
      return { matched: false, elapsed: timeout };
    }

    // ════════════════════════════════════════
    // ── Tab Creation with Wait ──
    // ════════════════════════════════════════
    case 'tabs.createAndWait': {
      const t = await chrome.tabs.create({ url: p.url, windowId: p.windowId, active: p.active !== false });
      invalidateTabCache();
      await waitForNavigation(t.id, p.timeout || 30000);
      const loaded = await chrome.tabs.get(t.id);
      return { id: loaded.id, windowId: loaded.windowId, url: loaded.url, title: loaded.title };
    }

    // ════════════════════════════════════════
    // ── Navigate with POST Data (CDP) ──
    // ════════════════════════════════════════
    case 'tabs.navigatePost': {
      await ensureDebugger(p.tabId);
      const navResult = await cdpSend(p.tabId, 'Page.navigate', {
        url: p.url,
        transitionType: p.transitionType || 'typed',
        postData: p.postData
      });
      if (p.waitUntil !== false) {
        await waitForNavigation(p.tabId, p.timeout || 30000);
      }
      return { ok: true, frameId: navResult?.frameId, loaderId: navResult?.loaderId };
    }

    // ════════════════════════════════════════
    // ── Browsing Data ──
    // ════════════════════════════════════════
    case 'browsing.clearCache': {
      await chrome.browsingData.removeCache({});
      return { ok: true };
    }

    // ════════════════════════════════════════
    // ── Pre-Page Script Injection (Stealth on steroids) ──
    // ════════════════════════════════════════
    case 'stealth.inject': {
      // Inject script BEFORE any page JS runs — the nuclear stealth option
      await ensureDebugger(p.tabId);
      const { identifier } = await cdpSend(p.tabId, 'Page.addScriptToEvaluateOnNewDocument', {
        source: p.script || `
          Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
          Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
          Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
          window.chrome = { runtime: {}, loadTimes: () => ({}) };
          delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
          delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
          delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
          const origQuery = window.Notification?.permission;
          if (origQuery === 'denied') Object.defineProperty(Notification, 'permission', { get: () => 'default' });
        `,
        runImmediately: true
      });
      return { ok: true, scriptId: identifier };
    }
    case 'stealth.removeScript': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Page.removeScriptToEvaluateOnNewDocument', { identifier: p.scriptId });
      return { ok: true };
    }

    // ════════════════════════════════════════
    // ── Event Binding (page→extension callbacks) ──
    // ════════════════════════════════════════
    case 'binding.add': {
      // Create a JS binding that the page can call back to the extension
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Runtime.addBinding', { name: p.name || 'bridgeCallback' });
      return { ok: true, binding: p.name || 'bridgeCallback' };
    }

    // ════════════════════════════════════════
    // ── WebSocket Interception ──
    // ════════════════════════════════════════
    case 'ws.enable': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Network.enable', {});
      if (!globalThis._wsFrames) globalThis._wsFrames = {};
      globalThis._wsFrames[p.tabId] = [];
      return { ok: true };
    }
    case 'ws.getFrames': {
      const frames = (globalThis._wsFrames || {})[p.tabId] || [];
      const limit = p.limit || 100;
      return { frames: frames.slice(-limit), total: frames.length };
    }
    case 'ws.clearFrames': {
      if (globalThis._wsFrames) globalThis._wsFrames[p.tabId] = [];
      return { ok: true };
    }

    // ════════════════════════════════════════
    // ── Network Response Body ──
    // ════════════════════════════════════════
    case 'network.getResponseBody': {
      await ensureDebugger(p.tabId);
      const body = await cdpSend(p.tabId, 'Network.getResponseBody', { requestId: p.requestId });
      return body;
    }
    case 'network.getPostData': {
      await ensureDebugger(p.tabId);
      const postData = await cdpSend(p.tabId, 'Network.getRequestPostData', { requestId: p.requestId });
      return postData;
    }
    case 'network.setHeaders': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Network.setExtraHTTPHeaders', { headers: p.headers });
      return { ok: true };
    }
    case 'network.setUserAgent': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Network.setUserAgentOverride', {
        userAgent: p.userAgent,
        acceptLanguage: p.acceptLanguage,
        platform: p.platform
      });
      return { ok: true };
    }
    case 'network.setCacheDisabled': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Network.setCacheDisabled', { cacheDisabled: !!p.disabled });
      return { ok: true };
    }
    case 'network.bypassServiceWorker': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Network.setBypassServiceWorker', { bypass: p.bypass !== false });
      return { ok: true };
    }

    // ════════════════════════════════════════
    // ── Navigation History ──
    // ════════════════════════════════════════
    case 'navigation.history': {
      await ensureDebugger(p.tabId);
      const history = await cdpSend(p.tabId, 'Page.getNavigationHistory', {});
      return history;
    }
    case 'navigation.back': {
      await ensureDebugger(p.tabId);
      const hist = await cdpSend(p.tabId, 'Page.getNavigationHistory', {});
      if (hist.currentIndex > 0) {
        await cdpSend(p.tabId, 'Page.navigateToHistoryEntry', { entryId: hist.entries[hist.currentIndex - 1].id });
        return { ok: true, navigatedTo: hist.entries[hist.currentIndex - 1].url };
      }
      return { ok: false, reason: 'Already at first entry' };
    }
    case 'navigation.forward': {
      await ensureDebugger(p.tabId);
      const hist2 = await cdpSend(p.tabId, 'Page.getNavigationHistory', {});
      if (hist2.currentIndex < hist2.entries.length - 1) {
        await cdpSend(p.tabId, 'Page.navigateToHistoryEntry', { entryId: hist2.entries[hist2.currentIndex + 1].id });
        return { ok: true, navigatedTo: hist2.entries[hist2.currentIndex + 1].url };
      }
      return { ok: false, reason: 'Already at last entry' };
    }

    // ════════════════════════════════════════
    // ── Frame Tree ──
    // ════════════════════════════════════════
    case 'frames.tree': {
      await ensureDebugger(p.tabId);
      const tree = await cdpSend(p.tabId, 'Page.getFrameTree', {});
      function flattenTree(node) {
        const frame = node.frame;
        const result = [{
          frameId: frame.id, parentId: frame.parentId,
          url: frame.url, name: frame.name,
          securityOrigin: frame.securityOrigin,
          mimeType: frame.mimeType
        }];
        for (const child of (node.childFrames || [])) {
          result.push(...flattenTree(child));
        }
        return result;
      }
      return flattenTree(tree.frameTree);
    }
    case 'frames.eval': {
      // Execute JS in a specific frame
      await ensureDebugger(p.tabId);
      const result = await cdpSend(p.tabId, 'Runtime.evaluate', {
        expression: p.expression,
        contextId: p.contextId,
        returnByValue: true,
        awaitPromise: !!p.awaitPromise
      });
      return result?.result?.value ?? result;
    }

    // ════════════════════════════════════════
    // ── Layout Metrics ──
    // ════════════════════════════════════════
    case 'page.layoutMetrics': {
      await ensureDebugger(p.tabId);
      const metrics = await cdpSend(p.tabId, 'Page.getLayoutMetrics', {});
      return {
        viewport: metrics.cssLayoutViewport,
        visual: metrics.cssVisualViewport,
        contentSize: metrics.cssContentSize
      };
    }

    // ════════════════════════════════════════
    // ── Cookie Profiles (export/import) ──
    // ════════════════════════════════════════
    case 'cookies.export': {
      const allCookies = await chrome.cookies.getAll(p.filter || {});
      return { cookies: allCookies, count: allCookies.length };
    }
    case 'cookies.import': {
      let imported = 0;
      for (const c of (p.cookies || [])) {
        try {
          const cookieData = {
            url: c.url || `http${c.secure ? 's' : ''}://${c.domain}${c.path || '/'}`,
            name: c.name, value: c.value,
            domain: c.domain, path: c.path || '/',
            secure: !!c.secure, httpOnly: !!c.httpOnly,
            sameSite: c.sameSite || 'lax'
          };
          if (c.expirationDate) cookieData.expirationDate = c.expirationDate;
          await chrome.cookies.set(cookieData);
          imported++;
        } catch {}
      }
      return { ok: true, imported, total: (p.cookies || []).length };
    }

    // ════════════════════════════════════════
    // ── Multi-Tab Execution ──
    // ════════════════════════════════════════
    case 'multi.eval': {
      // Execute same JS on multiple tabs simultaneously
      const tabIds = p.tabIds || (await getCachedTabs()).map(t => t.id);
      const results = {};
      await Promise.allSettled(tabIds.map(async (tid) => {
        try {
          const r = await chrome.scripting.executeScript({
            target: { tabId: tid },
            func: new Function('return (' + p.expression + ')'),
            world: 'MAIN'
          });
          results[tid] = { result: r[0]?.result };
        } catch (e) {
          results[tid] = { error: e.message };
        }
      }));
      return results;
    }
    case 'multi.screenshot': {
      // Screenshot multiple tabs at once
      const tabIds = p.tabIds || (await getCachedTabs()).filter(t => t.active).map(t => t.id);
      const results = {};
      for (const tid of tabIds) {
        try {
          const dataUrl = await chrome.tabs.captureVisibleTab(
            (await chrome.tabs.get(tid)).windowId,
            { format: p.format || 'jpeg', quality: p.quality || 60 }
          );
          results[tid] = { dataUrl };
        } catch (e) {
          results[tid] = { error: e.message };
        }
      }
      return results;
    }
    case 'multi.navigate': {
      // Navigate multiple tabs to different URLs
      const tasks = p.tasks || []; // [{tabId, url}]
      const results = {};
      await Promise.allSettled(tasks.map(async ({ tabId, url }) => {
        try {
          await chrome.tabs.update(tabId, { url });
          results[tabId] = { ok: true };
        } catch (e) {
          results[tabId] = { error: e.message };
        }
      }));
      invalidateTabCache();
      return results;
    }
    case 'multi.close': {
      // Close tabs matching URL pattern
      const tabs = await getCachedTabs();
      const matching = tabs.filter(t => {
        if (p.urlPattern && !t.url.includes(p.urlPattern)) return false;
        if (p.titlePattern && !t.title.includes(p.titlePattern)) return false;
        return true;
      });
      const ids = matching.map(t => t.id);
      if (ids.length) await chrome.tabs.remove(ids);
      invalidateTabCache();
      return { closed: ids.length };
    }

    // ════════════════════════════════════════
    // ── Workflow Engine ──
    // ════════════════════════════════════════
    case 'workflow.run': {
      // Execute a multi-step workflow defined as JSON
      const steps = p.steps || [];
      const results = [];
      const vars = { ...p.vars }; // user-defined variables

      for (let i = 0; i < steps.length; i++) {
        const step = steps[i];
        const stepParams = { ...step.params };

        // Variable substitution: {{varName}} in param values
        for (const [key, val] of Object.entries(stepParams)) {
          if (typeof val === 'string' && val.startsWith('{{') && val.endsWith('}}')) {
            const varName = val.slice(2, -2).trim();
            stepParams[key] = vars[varName];
          }
        }

        try {
          const result = await executeCommand(step.command, stepParams);
          results.push({ step: i, command: step.command, result });

          // Store result in variable if step has 'as' field
          if (step.as) vars[step.as] = result;

          // Conditional: skip next steps if condition fails
          if (step.condition && !evaluateCondition(step.condition, result, vars)) {
            results.push({ step: i, skipped: true, reason: 'Condition failed' });
            if (step.onFail === 'stop') break;
            continue;
          }

          // Delay between steps
          if (step.delay) await new Promise(r => setTimeout(r, step.delay));
        } catch (e) {
          results.push({ step: i, command: step.command, error: e.message });
          if (step.onError === 'stop' || p.stopOnError) break;
        }
      }
      return { results, vars };
    }

    // ════════════════════════════════════════
    // ── Auto-Healing Selector ──
    // ════════════════════════════════════════
    case 'heal.click': {
      // Try multiple selector strategies to find and click an element
      const results = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: (selector, text, xpath) => {
          let el = null;
          // Strategy 1: CSS selector
          if (selector) el = document.querySelector(selector);
          // Strategy 2: Text content match
          if (!el && text) {
            const candidates = 'a, button, input[type="submit"], [role="button"], [tabindex]';
            for (const c of document.querySelectorAll(candidates)) {
              if ((c.textContent || '').trim().toLowerCase().includes(text.toLowerCase())) {
                el = c; break;
              }
            }
          }
          // Strategy 3: Aria-label
          if (!el && text) el = document.querySelector(`[aria-label*="${text}" i]`);
          // Strategy 4: XPath
          if (!el && xpath) {
            const r = document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
            el = r.singleNodeValue;
          }
          // Strategy 5: Closest visible interactive element
          if (!el && text) {
            for (const c of document.querySelectorAll('*')) {
              if (c.offsetParent !== null && (c.textContent || '').trim().toLowerCase() === text.toLowerCase()) {
                el = c; break;
              }
            }
          }

          if (el) {
            el.scrollIntoView({ block: 'center' });
            el.click();
            return { found: true, tag: el.tagName.toLowerCase(), text: (el.textContent || '').substring(0, 80) };
          }
          return { found: false };
        },
        args: [p.selector, p.text, p.xpath],
        world: 'MAIN'
      });
      return results[0]?.result;
    }

    // ════════════════════════════════════════
    // ── Visual Regression (Screenshot Comparison) ──
    // ════════════════════════════════════════
    case 'visual.capture': {
      // Capture baseline screenshot for comparison
      if (!globalThis._visualBaselines) globalThis._visualBaselines = {};
      const dataUrl = await chrome.tabs.captureVisibleTab(null, {
        format: 'png', quality: 100
      });
      const key = p.name || `tab_${p.tabId}`;
      globalThis._visualBaselines[key] = dataUrl;
      return { ok: true, name: key, captured: true };
    }
    case 'visual.compare': {
      // Compare current screenshot with baseline
      if (!globalThis._visualBaselines) return { __error: 'No baselines captured' };
      const key = p.name || `tab_${p.tabId}`;
      const baseline = globalThis._visualBaselines[key];
      if (!baseline) return { __error: `No baseline found for "${key}"` };

      const current = await chrome.tabs.captureVisibleTab(null, {
        format: 'png', quality: 100
      });
      // Simple string comparison (pixel-perfect check)
      const match = baseline === current;
      return { match, name: key, baselineSize: baseline.length, currentSize: current.length };
    }

    // ════════════════════════════════════════
    // ── User Agent Rotation ──
    // ════════════════════════════════════════
    case 'stealth.rotateUA': {
      const agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3 Safari/605.1.15',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0',
      ];
      const ua = p.userAgent || agents[Math.floor(Math.random() * agents.length)];
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Network.setUserAgentOverride', {
        userAgent: ua,
        platform: ua.includes('Mac') ? 'MacIntel' : ua.includes('Linux') ? 'Linux x86_64' : 'Win32'
      });
      return { ok: true, userAgent: ua };
    }

    // ════════════════════════════════════════
    // ── Page Lifecycle ──
    // ════════════════════════════════════════
    case 'page.stopLoading': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Page.stopLoading', {});
      return { ok: true };
    }
    case 'page.handleDialog': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Page.handleJavaScriptDialog', {
        accept: p.accept !== false,
        promptText: p.promptText
      });
      return { ok: true };
    }
    case 'page.setContent': {
      await ensureDebugger(p.tabId);
      const frameTree = await cdpSend(p.tabId, 'Page.getFrameTree', {});
      const frameId = frameTree.frameTree.frame.id;
      await cdpSend(p.tabId, 'Page.setDocumentContent', {
        frameId, html: p.html
      });
      return { ok: true };
    }

    // ════════════════════════════════════════
    // ── Error Screenshot on Failure ──
    // ════════════════════════════════════════
    case 'try': {
      // Execute a command with auto-screenshot on failure
      try {
        const result = await executeCommand(p.command, p.commandParams || {});
        return { ok: true, result };
      } catch (e) {
        let errorScreenshot = null;
        try {
          const tab = await chrome.tabs.get(p.commandParams?.tabId || p.tabId);
          errorScreenshot = await chrome.tabs.captureVisibleTab(tab.windowId, {
            format: 'jpeg', quality: 50
          });
        } catch {}
        return { ok: false, error: e.message, screenshot: errorScreenshot };
      }
    }

    // ════════════════════════════════════════
    // ── Batch Execution ──
    // ════════════════════════════════════════
    case 'batch': {
      const results = [];
      for (const cmd of (p.commands || [])) {
        try {
          results.push({ id: cmd.id, result: await executeCommand(cmd.command, cmd.params || {}) });
        } catch (e) {
          results.push({ id: cmd.id, error: e.message });
        }
      }
      return results;
    }

    // ════════════════════════════════════════
    // ── Bridge Metrics ──
    // ════════════════════════════════════════
    case 'bridge.metrics': {
      return {
        ...metrics,
        uptime: Date.now() - metrics.startTime,
        version: BRIDGE_VERSION,
        connected: isHubConnected(),
        transport: isHubConnected() ? BRIDGE_TRANSPORT : 'disconnected',
        connectionState: bridgeRuntime.connectionState,
        hubUrl: getHubUrl(),
        healthUrl: getHubHealthUrl()
      };
    }
    case 'bridge.ping': {
      return { pong: true, timestamp: Date.now() };
    }
    case 'bridge.capabilities': {
      return {
        version: BRIDGE_VERSION,
        transport: isHubConnected() ? BRIDGE_TRANSPORT : 'disconnected',
        chromeMinimum: 116,
        features: [
          'batch',
          'event-streaming',
          'content-script-automation',
          'cdp-debugger',
          'workflow-engine',
          'visual-regression',
          'session-recording',
          'network-interception',
          'network-mocking',
          'hub-diagnostics',
          'smart-navigation-wait',
          'pipeline',
          'configurable-hub-endpoint',
          'persistent-runtime-state',
          'http-health-probe',
          'full-stealth-suite',
          'performance-tracing',
          'vision-simulation',
          'animation-control',
          'cdp-overlays',
          'webauthn-virtual-auth',
          'service-worker-control',
          'cache-api-control',
          'indexeddb-access',
          'advanced-emulation',
          'cpu-throttling',
          'tab-organization',
          'dom-cdp-operations',
          'runtime-inspection',
          'bridge-state-export'
        ]
      };
    }
    case 'bridge.configure': {
      const hubUrl = normalizeHubUrl(p.hubUrl);
      if (!hubUrl) throw new Error('Invalid WebSocket URL');
      suppressHubConfigReconnect = true;
      await chrome.storage.local.set({ bridgeHubUrl: hubUrl });
      if (p.applyNow) {
        setTimeout(() => disconnectHub({ reconnect: true, clearError: true }), 50);
      }
      return {
        ok: true,
        hubUrl,
        healthUrl: getHubHealthUrl(hubUrl),
        applyNow: !!p.applyNow
      };
    }
    case 'bridge.reconnect': {
      disconnectHub({ reconnect: true, clearError: true });
      return {
        ok: true,
        hubUrl: getHubUrl(),
        healthUrl: getHubHealthUrl()
      };
    }
    case 'bridge.reload': {
      // Self-reload: detach all debuggers, then reload the extension
      for (const tabId of debuggerAttached) {
        await chrome.debugger.detach({ tabId }).catch(() => {});
      }
      debuggerAttached.clear();
      // Schedule reload after responding
      setTimeout(() => chrome.runtime.reload(), 100);
      return { ok: true, reloading: true };
    }
    case 'bridge.detachAll': {
      // Detach all debuggers to dismiss the banner without reloading
      for (const tabId of debuggerAttached) {
        await chrome.debugger.detach({ tabId }).catch(() => {});
      }
      debuggerAttached.clear();
      return { ok: true, detached: true };
    }
    case 'bridge.status': {
      return {
        connected: isHubConnected(),
        profileId,
        transport: isHubConnected() ? BRIDGE_TRANSPORT : 'disconnected',
        connectionState: bridgeRuntime.connectionState,
        hubUrl: getHubUrl(),
        healthUrl: getHubHealthUrl(),
        version: BRIDGE_VERSION,
        runtime: bridgeRuntime,
        metrics
      };
    }

    // ════════════════════════════════════════
    // ── Event Streaming ──
    // ════════════════════════════════════════
    case 'events.subscribe': {
      const types = Array.isArray(p.events) ? p.events : [p.events];
      for (const t of types) eventSubscriptions.add(t);
      if (p.tabId) {
        const domains = new Set();
        for (const t of types) {
          if (t.startsWith('network.') || t === '*') domains.add('Network');
          if (t.startsWith('console.') || t.startsWith('runtime.') || t === '*') domains.add('Runtime');
          if (t.startsWith('dialog.') || t === '*') domains.add('Page');
        }
        if (domains.size) await enableCDPEvents(p.tabId, domains);
      }
      return { ok: true, subscriptions: [...eventSubscriptions] };
    }
    case 'events.unsubscribe': {
      const types = Array.isArray(p.events) ? p.events : [p.events];
      for (const t of types) eventSubscriptions.delete(t);
      return { ok: true, subscriptions: [...eventSubscriptions] };
    }
    case 'events.list': {
      return { subscriptions: [...eventSubscriptions] };
    }
    case 'events.enableCDP': {
      const domains = Array.isArray(p.domains) ? p.domains : [p.domains || 'Network'];
      await enableCDPEvents(p.tabId, domains);
      return { ok: true, tabId: p.tabId, domains: [...(cdpEventTabs.get(p.tabId) || [])] };
    }

    // ════════════════════════════════════════
    // ── Debugger Management ──
    // ════════════════════════════════════════
    case 'debugger.attach': {
      await ensureDebugger(p.tabId);
      return { ok: true };
    }
    case 'debugger.detach': {
      if (debuggerAttached.has(p.tabId)) {
        await chrome.debugger.detach({ tabId: p.tabId }).catch(() => {});
        debuggerAttached.delete(p.tabId);
      }
      return { ok: true };
    }
    case 'debugger.detachAll': {
      for (const tabId of debuggerAttached) {
        await chrome.debugger.detach({ tabId }).catch(() => {});
      }
      debuggerAttached.clear();
      return { ok: true };
    }

    // ════════════════════════════════════════
    // ── Performance Tracing (v4.0) ──
    // ════════════════════════════════════════
    case 'tracing.start': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Tracing.start', {
        categories: p.categories || '-*,devtools.timeline,v8.execute,disabled-by-default-devtools.timeline,disabled-by-default-devtools.timeline.frame,toplevel,blink.console,disabled-by-default-devtools.timeline.stack',
        traceConfig: p.traceConfig || undefined
      });
      return { ok: true, tracing: true };
    }
    case 'tracing.stop': {
      await ensureDebugger(p.tabId);
      const traceData = await new Promise((resolve) => {
        const chunks = [];
        const traceHandler = (source, method, params) => {
          if (source.tabId !== p.tabId) return;
          if (method === 'Tracing.dataCollected') chunks.push(...(params.value || []));
          if (method === 'Tracing.tracingComplete') {
            chrome.debugger.onEvent.removeListener(traceHandler);
            resolve(chunks);
          }
        };
        chrome.debugger.onEvent.addListener(traceHandler);
        cdpSend(p.tabId, 'Tracing.end', {});
      });
      return { events: traceData.length, trace: p.full ? traceData : traceData.slice(0, p.limit || 500) };
    }

    // ════════════════════════════════════════
    // ── Advanced Emulation (v4.0) ──
    // ════════════════════════════════════════
    case 'emulate.darkMode': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Emulation.setEmulatedMedia', {
        features: [{ name: 'prefers-color-scheme', value: p.dark !== false ? 'dark' : 'light' }]
      });
      return { ok: true, darkMode: p.dark !== false };
    }
    case 'emulate.reducedMotion': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Emulation.setEmulatedMedia', {
        features: [{ name: 'prefers-reduced-motion', value: p.reduce !== false ? 'reduce' : 'no-preference' }]
      });
      return { ok: true, reducedMotion: p.reduce !== false };
    }
    case 'emulate.colorBlind': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Emulation.setEmulatedVisionDeficiency', {
        type: p.type || 'none'
      });
      return { ok: true, visionType: p.type || 'none' };
    }
    case 'emulate.cpuThrottle': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Emulation.setCPUThrottlingRate', { rate: p.rate || 1 });
      return { ok: true, cpuThrottleRate: p.rate || 1 };
    }
    case 'emulate.forcedColors': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Emulation.setForcedColors', {
        forcedColors: p.enabled !== false ? 'active' : 'none'
      });
      return { ok: true, forcedColors: p.enabled !== false };
    }
    case 'emulate.disableJS': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Emulation.setScriptExecutionDisabled', { value: p.disabled !== false });
      return { ok: true, jsDisabled: p.disabled !== false };
    }
    case 'emulate.printMedia': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Emulation.setEmulatedMedia', { media: p.enabled !== false ? 'print' : '' });
      return { ok: true, printMedia: p.enabled !== false };
    }

    // ════════════════════════════════════════
    // ── Runtime / Heap (v4.0) ──
    // ════════════════════════════════════════
    case 'runtime.heapStats': {
      await ensureDebugger(p.tabId);
      return await cdpSend(p.tabId, 'Runtime.getHeapUsage', {});
    }
    case 'runtime.collectGarbage': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'HeapProfiler.collectGarbage', {});
      return { ok: true, collected: true };
    }
    case 'runtime.getProperties': {
      await ensureDebugger(p.tabId);
      const evalObj = await cdpSend(p.tabId, 'Runtime.evaluate', {
        expression: p.expression, returnByValue: false
      });
      if (!evalObj?.result?.objectId) return { __error: 'Expression did not return an object' };
      const props = await cdpSend(p.tabId, 'Runtime.getProperties', {
        objectId: evalObj.result.objectId,
        ownProperties: p.ownProperties !== false,
        generatePreview: true
      });
      return (props.result || []).map(pr => ({
        name: pr.name, value: pr.value?.value, type: pr.value?.type,
        description: pr.value?.description?.substring(0, 200)
      }));
    }
    case 'runtime.queryObjects': {
      await ensureDebugger(p.tabId);
      const proto = await cdpSend(p.tabId, 'Runtime.evaluate', {
        expression: p.constructorName + '.prototype', returnByValue: false
      });
      if (!proto?.result?.objectId) return { __error: 'Constructor not found' };
      const objects = await cdpSend(p.tabId, 'Runtime.queryObjects', {
        prototypeObjectId: proto.result.objectId
      });
      const arr = await cdpSend(p.tabId, 'Runtime.getProperties', {
        objectId: objects.objects.objectId, ownProperties: true
      });
      return { count: arr.result?.length || 0 };
    }

    // ════════════════════════════════════════
    // ── Advanced Tab Operations (v4.0) ──
    // ════════════════════════════════════════
    case 'tabs.sort': {
      const allTabs = await chrome.tabs.query({});
      const sortBy = p.by || 'url';
      const sorted = [...allTabs].sort((a, b) => (a[sortBy] || '').localeCompare(b[sortBy] || ''));
      for (let i = 0; i < sorted.length; i++) {
        await chrome.tabs.move(sorted[i].id, { index: i });
      }
      invalidateTabCache();
      return { ok: true, sorted: sorted.length, by: sortBy };
    }
    case 'tabs.deduplicate': {
      const allTabs = await chrome.tabs.query({});
      const seenUrls = new Map();
      const dupes = [];
      for (const t of allTabs) {
        try {
          const key = new URL(t.url).origin + new URL(t.url).pathname;
          if (seenUrls.has(key)) dupes.push(t.id);
          else seenUrls.set(key, t.id);
        } catch {}
      }
      if (dupes.length) await chrome.tabs.remove(dupes);
      invalidateTabCache();
      return { ok: true, removed: dupes.length, remaining: allTabs.length - dupes.length };
    }
    case 'tabs.groupByDomain': {
      const allTabs = await chrome.tabs.query({});
      const domainMap = new Map();
      for (const t of allTabs) {
        try {
          const domain = new URL(t.url).hostname;
          if (!domainMap.has(domain)) domainMap.set(domain, []);
          domainMap.get(domain).push(t.id);
        } catch {}
      }
      const tabGroups = [];
      const colors = ['blue', 'red', 'yellow', 'green', 'pink', 'purple', 'cyan', 'orange'];
      for (const [domain, tabIds] of domainMap) {
        if (tabIds.length < 2) continue;
        const groupId = await chrome.tabs.group({ tabIds });
        await chrome.tabGroups.update(groupId, {
          title: domain.replace(/^www\./, ''),
          color: colors[tabGroups.length % colors.length]
        });
        tabGroups.push({ domain, groupId, count: tabIds.length });
      }
      return { ok: true, groups: tabGroups };
    }
    case 'tabs.suspendInactive': {
      const inactiveTabs = await chrome.tabs.query({ active: false });
      let suspended = 0;
      for (const t of inactiveTabs) {
        if (t.discarded || t.url.startsWith('chrome://')) continue;
        try { await chrome.tabs.discard(t.id); suspended++; } catch {}
      }
      return { ok: true, suspended };
    }
    case 'tabs.closeByPattern': {
      const allTabs = await chrome.tabs.query({});
      const regex = p.pattern ? new RegExp(p.pattern, 'i') : null;
      const matched = allTabs.filter(t => {
        if (regex) return regex.test(t.url) || regex.test(t.title);
        if (p.urlPattern) return t.url.includes(p.urlPattern);
        return false;
      });
      const ids = matched.map(t => t.id);
      if (ids.length) await chrome.tabs.remove(ids);
      invalidateTabCache();
      return { closed: ids.length };
    }

    // ════════════════════════════════════════
    // ── CDP DOM Operations (v4.0) ──
    // ════════════════════════════════════════
    case 'dom.scrollIntoView': {
      await ensureDebugger(p.tabId);
      const doc4 = await cdpSend(p.tabId, 'DOM.getDocument', { depth: 0 });
      const node4 = await cdpSend(p.tabId, 'DOM.querySelector', {
        nodeId: doc4.root.nodeId, selector: p.selector
      });
      if (!node4.nodeId) return { __error: 'Element not found' };
      await cdpSend(p.tabId, 'DOM.scrollIntoViewIfNeeded', { nodeId: node4.nodeId });
      return { ok: true };
    }
    case 'dom.focus': {
      await ensureDebugger(p.tabId);
      const doc5 = await cdpSend(p.tabId, 'DOM.getDocument', { depth: 0 });
      const node5 = await cdpSend(p.tabId, 'DOM.querySelector', {
        nodeId: doc5.root.nodeId, selector: p.selector
      });
      if (!node5.nodeId) return { __error: 'Element not found' };
      await cdpSend(p.tabId, 'DOM.focus', { nodeId: node5.nodeId });
      return { ok: true };
    }
    case 'dom.search': {
      await ensureDebugger(p.tabId);
      const { searchId: sid, resultCount: rCount } = await cdpSend(p.tabId, 'DOM.performSearch', {
        query: p.query, includeUserAgentShadowDOM: !!p.includeShadow
      });
      const searchResults = await cdpSend(p.tabId, 'DOM.getSearchResults', {
        searchId: sid, fromIndex: 0, toIndex: Math.min(rCount, p.limit || 50)
      });
      await cdpSend(p.tabId, 'DOM.discardSearchResults', { searchId: sid });
      return { count: rCount, nodeIds: searchResults.nodeIds };
    }
    case 'dom.getOuterHTML': {
      await ensureDebugger(p.tabId);
      const doc6 = await cdpSend(p.tabId, 'DOM.getDocument', { depth: 0 });
      const node6 = await cdpSend(p.tabId, 'DOM.querySelector', {
        nodeId: doc6.root.nodeId, selector: p.selector
      });
      if (!node6.nodeId) return { __error: 'Element not found' };
      const { outerHTML } = await cdpSend(p.tabId, 'DOM.getOuterHTML', { nodeId: node6.nodeId });
      return { html: outerHTML };
    }
    case 'page.bringToFront': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Page.bringToFront', {});
      return { ok: true };
    }

    // ════════════════════════════════════════
    // ── Animation Control (v4.0) ──
    // ════════════════════════════════════════
    case 'animation.disable': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Animation.disable', {});
      return { ok: true };
    }
    case 'animation.enable': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Animation.enable', {});
      return { ok: true };
    }
    case 'animation.setSpeed': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Animation.setPlaybackRate', { playbackRate: p.rate || 1 });
      return { ok: true, playbackRate: p.rate || 1 };
    }

    // ════════════════════════════════════════
    // ── Service Worker & Cache (v4.0) ──
    // ════════════════════════════════════════
    case 'serviceWorker.list': {
      const swResults = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: async () => {
          const regs = await navigator.serviceWorker?.getRegistrations() || [];
          return regs.map(r => ({
            scope: r.scope,
            active: r.active?.state || null,
            waiting: r.waiting?.state || null,
            installing: r.installing?.state || null,
            scriptURL: r.active?.scriptURL || r.waiting?.scriptURL || null
          }));
        },
        world: 'MAIN'
      });
      return swResults[0]?.result;
    }
    case 'serviceWorker.unregister': {
      const swUnreg = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: async (scope) => {
          const regs = await navigator.serviceWorker?.getRegistrations() || [];
          let unregistered = 0;
          for (const r of regs) {
            if (!scope || r.scope.includes(scope)) {
              await r.unregister();
              unregistered++;
            }
          }
          return { ok: true, unregistered };
        },
        args: [p.scope],
        world: 'MAIN'
      });
      return swUnreg[0]?.result;
    }
    case 'cache.list': {
      const cacheList = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: async () => {
          const names = await caches?.keys() || [];
          const details = [];
          for (const name of names) {
            const cache = await caches.open(name);
            const keys = await cache.keys();
            details.push({ name, entries: keys.length });
          }
          return details;
        },
        world: 'MAIN'
      });
      return cacheList[0]?.result;
    }
    case 'cache.clear': {
      const cacheClear = await chrome.scripting.executeScript({
        target: { tabId: p.tabId },
        func: async (cacheName) => {
          if (cacheName) return { deleted: await caches.delete(cacheName) };
          const names = await caches?.keys() || [];
          let deleted = 0;
          for (const n of names) { if (await caches.delete(n)) deleted++; }
          return { deleted };
        },
        args: [p.name],
        world: 'MAIN'
      });
      return cacheClear[0]?.result;
    }

    // ════════════════════════════════════════
    // ── IndexedDB (v4.0) ──
    // ════════════════════════════════════════
    case 'indexedDB.list': {
      await ensureDebugger(p.tabId);
      const idbResult = await cdpSend(p.tabId, 'IndexedDB.requestDatabaseNames', {
        securityOrigin: p.origin || undefined,
        storageKey: p.storageKey || undefined
      });
      return { databases: idbResult.databaseNames };
    }
    case 'indexedDB.clear': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'IndexedDB.clearObjectStore', {
        securityOrigin: p.origin,
        databaseName: p.database,
        objectStoreName: p.store
      });
      return { ok: true };
    }

    // ════════════════════════════════════════
    // ── Full Stealth Suite (v4.0) ──
    // ════════════════════════════════════════
    case 'stealth.full': {
      await ensureDebugger(p.tabId);
      // Pre-page script injection — executes before any page JS
      await cdpSend(p.tabId, 'Page.addScriptToEvaluateOnNewDocument', {
        source: `
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
          window.chrome.csi = window.chrome.csi || (() => ({}));
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
          if (typeof WebGL2RenderingContext !== 'undefined') {
            const _origGetParam2 = WebGL2RenderingContext.prototype.getParameter;
            WebGL2RenderingContext.prototype.getParameter = function(param) {
              if (param === 37445) return 'Intel Inc.';
              if (param === 37446) return 'Intel Iris OpenGL Engine';
              return _origGetParam2.call(this, param);
            };
          }
          const _origCreateOsc = AudioContext.prototype.createOscillator;
          AudioContext.prototype.createOscillator = function() {
            const osc = _origCreateOsc.call(this);
            const _origConn = osc.connect.bind(osc);
            osc.connect = function(dest) {
              if (dest instanceof AnalyserNode) {
                const g = osc.context.createGain();
                g.gain.value = 1 + (Math.random() * 0.0001 - 0.00005);
                _origConn(g); g.connect(dest); return dest;
              }
              return _origConn(dest);
            };
            return osc;
          };
          try {
            if (Notification.permission === 'denied')
              Object.defineProperty(Notification, 'permission', { get: () => 'default' });
          } catch {}
          const _origPerfNow = Performance.prototype.now;
          Performance.prototype.now = function() { return _origPerfNow.call(this) + (Math.random() * 0.001); };
        `,
        runImmediately: true
      });
      // Set modern UA with full client hints
      const stealthUA = p.userAgent || 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36';
      await cdpSend(p.tabId, 'Network.setUserAgentOverride', {
        userAgent: stealthUA,
        acceptLanguage: 'en-US,en;q=0.9',
        platform: 'Win32',
        userAgentMetadata: {
          brands: [
            { brand: 'Chromium', version: '134' },
            { brand: 'Google Chrome', version: '134' },
            { brand: 'Not:A-Brand', version: '24' }
          ],
          fullVersion: '134.0.6998.89',
          platform: 'Windows', platformVersion: '15.0.0',
          architecture: 'x86', model: '', mobile: false,
          bitness: '64', wow64: false
        }
      });
      return { ok: true, stealth: 'full', features: [
        'webdriver-hide', 'plugins-spoof', 'languages-fix', 'chrome-runtime-fix',
        'automation-clean', 'canvas-noise', 'webgl-spoof', 'webgl2-spoof',
        'audio-noise', 'notification-fix', 'timing-noise', 'ua-override', 'ua-client-hints'
      ]};
    }

    // ════════════════════════════════════════
    // ── Network Mocking (v4.0) ──
    // ════════════════════════════════════════
    case 'network.mock': {
      await ensureDebugger(p.tabId);
      if (!globalThis._networkMocks) globalThis._networkMocks = new Map();
      const mockPattern = p.urlPattern || '*';
      globalThis._networkMocks.set(p.tabId + ':' + mockPattern, {
        responseCode: p.responseCode || 200,
        responseHeaders: p.responseHeaders || [{ name: 'Content-Type', value: p.contentType || 'application/json' }],
        body: p.body || ''
      });
      await cdpSend(p.tabId, 'Fetch.enable', {
        patterns: [{ urlPattern: mockPattern, requestStage: 'Response' }]
      });
      return { ok: true, mocking: mockPattern };
    }
    case 'network.unmock': {
      await ensureDebugger(p.tabId);
      if (globalThis._networkMocks) globalThis._networkMocks.delete(p.tabId + ':' + (p.urlPattern || '*'));
      if (!globalThis._networkMocks?.size) {
        await cdpSend(p.tabId, 'Fetch.disable', {});
      }
      return { ok: true };
    }

    // ════════════════════════════════════════
    // ── WebAuthn Virtual Authenticator (v4.0) ──
    // ════════════════════════════════════════
    case 'webauthn.addVirtualAuth': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'WebAuthn.enable', {});
      const { authenticatorId: authId } = await cdpSend(p.tabId, 'WebAuthn.addVirtualAuthenticator', {
        options: {
          protocol: p.protocol || 'ctap2',
          transport: p.transport || 'internal',
          hasResidentKey: p.hasResidentKey !== false,
          hasUserVerification: p.hasUserVerification !== false,
          isUserVerified: p.isUserVerified !== false,
          automaticPresenceSimulation: true
        }
      });
      return { ok: true, authenticatorId: authId };
    }
    case 'webauthn.removeVirtualAuth': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'WebAuthn.removeVirtualAuthenticator', {
        authenticatorId: p.authenticatorId
      });
      return { ok: true };
    }

    // ════════════════════════════════════════
    // ── CDP Overlay & Inspection (v4.0) ──
    // ════════════════════════════════════════
    case 'overlay.highlight': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Overlay.enable', {});
      const doc7 = await cdpSend(p.tabId, 'DOM.getDocument', { depth: 0 });
      const node7 = await cdpSend(p.tabId, 'DOM.querySelector', {
        nodeId: doc7.root.nodeId, selector: p.selector
      });
      if (!node7.nodeId) return { __error: 'Element not found' };
      await cdpSend(p.tabId, 'Overlay.highlightNode', {
        highlightConfig: {
          contentColor: { r: 111, g: 168, b: 220, a: 0.66 },
          paddingColor: { r: 147, g: 196, b: 125, a: 0.55 },
          borderColor: { r: 255, g: 229, b: 153, a: 0.66 },
          marginColor: { r: 246, g: 178, b: 107, a: 0.66 },
          showInfo: true,
          ...(p.config || {})
        },
        nodeId: node7.nodeId
      });
      return { ok: true };
    }
    case 'overlay.hide': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Overlay.hideHighlight', {});
      return { ok: true };
    }
    case 'overlay.showFPS': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Overlay.enable', {});
      await cdpSend(p.tabId, 'Overlay.setShowFPSCounter', { show: p.enabled !== false });
      return { ok: true };
    }
    case 'overlay.showPaintRects': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Overlay.enable', {});
      await cdpSend(p.tabId, 'Overlay.setShowPaintRects', { result: p.enabled !== false });
      return { ok: true };
    }
    case 'overlay.showLayoutShifts': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Overlay.enable', {});
      await cdpSend(p.tabId, 'Overlay.setShowLayoutShiftRegions', { result: p.enabled !== false });
      return { ok: true };
    }
    case 'overlay.showScrollSnap': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Overlay.enable', {});
      await cdpSend(p.tabId, 'Overlay.setShowScrollSnapOverlays', {
        scrollSnapHighlightConfigs: p.configs || []
      });
      return { ok: true };
    }

    // ════════════════════════════════════════
    // ── Input: CDP Scroll (v4.0) ──
    // ════════════════════════════════════════
    case 'input.scroll': {
      await ensureDebugger(p.tabId);
      await cdpSend(p.tabId, 'Input.dispatchMouseEvent', {
        type: 'mouseWheel', x: p.x || 0, y: p.y || 0,
        deltaX: p.deltaX || 0, deltaY: p.deltaY || 0,
        modifiers: p.modifiers || 0
      });
      return { ok: true };
    }

    // ════════════════════════════════════════
    // ── Content Script Commands (v4.0) ──
    // ════════════════════════════════════════
    case 'element.focus': return sendContentCommand(p.tabId, 'element.focus', p);
    case 'element.blur': return sendContentCommand(p.tabId, 'element.blur', p);
    case 'element.closest': return sendContentCommand(p.tabId, 'element.closest', p);
    case 'element.style': return sendContentCommand(p.tabId, 'element.style', p);
    case 'element.offset': return sendContentCommand(p.tabId, 'element.offset', p);
    case 'element.matches': return sendContentCommand(p.tabId, 'element.matches', p);
    case 'element.type': return sendContentCommand(p.tabId, 'element.type', p);
    case 'element.hover': return sendContentCommand(p.tabId, 'element.hover', p);
    case 'forms.submit': return sendContentCommand(p.tabId, 'forms.submit', p);
    case 'dom.serialize': return sendContentCommand(p.tabId, 'dom.serialize', p);
    case 'dom.ready': return sendContentCommand(p.tabId, 'dom.ready', p);
    case 'clipboard.copy': return sendContentCommand(p.tabId, 'clipboard.copy', p);
    case 'page.freeze': return sendContentCommand(p.tabId, 'page.freeze', p);
    case 'page.unfreeze': return sendContentCommand(p.tabId, 'page.unfreeze', p);

    // ════════════════════════════════════════
    // ── Bridge State Export (v4.0) ──
    // ════════════════════════════════════════
    case 'bridge.export': {
      const [bridgeLocal, bridgeSession] = await Promise.all([
        chrome.storage.local.get(null),
        chrome.storage.session.get(null)
      ]);
      return { local: bridgeLocal, session: bridgeSession, metrics, runtime: bridgeRuntime, version: BRIDGE_VERSION };
    }

    default:
      throw new Error('Unknown command: ' + command);
  }
}

// ─── Content Script Messaging ───

async function sendContentCommand(tabId, command, params) {
  let response;
  try {
    response = await chrome.tabs.sendMessage(tabId, {
      target: 'content', command, params
    });
  } catch (e) {
    // Content script might not be loaded, inject it first
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ['content.js']
    });
    response = await chrome.tabs.sendMessage(tabId, {
      target: 'content', command, params
    });
  }
  return normalizeCommandResult(response);
}

// ─── CDP Helper ───

async function ensureDebugger(tabId) {
  if (!debuggerAttached.has(tabId)) {
    await chrome.debugger.attach({ tabId }, '1.3');
    debuggerAttached.add(tabId);
  }
}

function cdpSend(tabId, method, params) {
  return new Promise((resolve, reject) => {
    chrome.debugger.sendCommand({ tabId }, method, params, (result) => {
      if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
      else resolve(result);
    });
  });
}

chrome.debugger.onDetach.addListener((source) => {
  if (source.tabId) debuggerAttached.delete(source.tabId);
});

chrome.tabs.onRemoved.addListener((tabId) => {
  debuggerAttached.delete(tabId);
});

// ─── Workflow Condition Evaluator ───

function evaluateCondition(condition, result, vars) {
  try {
    if (typeof condition === 'string') {
      // Simple expression: "result.found === true" or "vars.count > 0"
      return new Function('result', 'vars', `return (${condition})`)(result, vars);
    }
    return !!result;
  } catch {
    return false;
  }
}

// ─── Smart Navigation Wait ───

function waitForNavigation(tabId, timeout = 30000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.webNavigation.onCompleted.removeListener(onComplete);
      chrome.webNavigation.onErrorOccurred?.removeListener(onError);
      resolve(); // don't block forever — resolve on timeout
    }, timeout);

    function onComplete(details) {
      if (details.tabId === tabId && details.frameId === 0) {
        clearTimeout(timer);
        chrome.webNavigation.onCompleted.removeListener(onComplete);
        chrome.webNavigation.onErrorOccurred?.removeListener(onError);
        resolve();
      }
    }
    function onError(details) {
      if (details.tabId === tabId && details.frameId === 0) {
        clearTimeout(timer);
        chrome.webNavigation.onCompleted.removeListener(onComplete);
        chrome.webNavigation.onErrorOccurred?.removeListener(onError);
        reject(new Error('Navigation failed: ' + details.error));
      }
    }

    chrome.webNavigation.onCompleted.addListener(onComplete);
    chrome.webNavigation.onErrorOccurred?.addListener(onError);
  });
}
