/*
 * Chrome Bridge — Content Script
 * Injected into all pages for advanced DOM operations:
 * XPath queries, shadow DOM traversal, element highlighting, accessibility snapshots,
 * mutation observation, intersection observation, fuzzy smart finding,
 * DOM stability detection, element traversal, canvas reading, XHR interception,
 * session recording, drag-and-drop, and custom event dispatch.
 */

(() => {
  // Avoid double-injection
  if (window.__chromeBridge) return;
  window.__chromeBridge = true;

  let highlightOverlay = null;
  let mutationObserver = null;
  let mutationBuffer = [];
  const MAX_MUTATION_BUFFER = 200;

  // ─── Message Handler ───

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.target !== 'content') return false;
    handleContentCommand(msg).then(sendResponse).catch(e => sendResponse({ __error: e.message }));
    return true; // async
  });

  async function handleContentCommand(msg) {
    const p = msg.params || {};
    switch (msg.command) {
      case 'xpath': return queryXPath(p.expression, p.contextNode);
      case 'xpath.all': return queryXPathAll(p.expression, p.limit);
      case 'shadow': return queryShadowDOM(p.selector, p.hostSelector);
      case 'highlight': return highlightElement(p.selector, p.color, p.duration);
      case 'highlight.clear': return clearHighlight();
      case 'a11y.snapshot': return getAccessibilitySnapshot(p.root);
      case 'mutation.start': return startMutationObserver(p.selector, p.config);
      case 'mutation.stop': return stopMutationObserver();
      case 'mutation.flush': return flushMutations();
      case 'elements.at': return elementsAtPoint(p.x, p.y);
      case 'element.bounds': return getElementBounds(p.selector);
      case 'element.computed': return getComputedStyles(p.selector, p.properties);
      case 'forms.detect': return detectForms();
      case 'forms.fill': return smartFillForm(p.data, p.formIndex);
      case 'links.extract': return extractLinks(p.filter);
      case 'tables.extract': return extractTables(p.selector);
      case 'search.text': return searchText(p.query, p.highlight);
      case 'meta.extract': return extractMeta();
      case 'scroll.infinite': return infiniteScroll(p.maxScrolls, p.delay);
      case 'smart.find': return smartFind(p.description, p.action);
      case 'smart.findAll': return smartFindAll(p.description, p.limit);
      case 'smart.click': return smartClick(p.description);
      case 'smart.fill': return smartFill(p.description, p.value);
      case 'smart.wait': return smartWait(p.description, p.timeout);
      case 'element.interactive': return waitInteractive(p.selector, p.timeout);
      case 'dom.diff': return domDiff(p.snapshotId);
      case 'dom.snapshot': return domSnapshot(p.snapshotId);
      case 'drag': return dragAndDrop(p.from, p.to);
      case 'record.start': return recordStart();
      case 'record.stop': return recordStop();
      case 'record.replay': return recordReplay(p.events, p.speed);
      case 'page.readiness': return pageReadiness();
      case 'stealth.check': return stealthCheck();
      case 'iframe.list': return listIframes();
      case 'iframe.eval': return iframeEval(p.index, p.expression);
      case 'element.screenshot': return elementScreenshot(p.selector);
      case 'element.waitGone': return waitForElementGone(p.selector, p.timeout);
      case 'dom.waitStable': return waitForDOMStable(p.timeout, p.idleTime);
      case 'element.attributes': return getElementAttributes(p.selector);
      case 'element.setAttribute': return setElementAttribute(p.selector, p.attribute, p.value);
      case 'element.xpath': return getElementXPath(p.selector);
      case 'element.dispatchEvent': return dispatchCustomEvent(p.selector, p.event, p.detail);
      case 'element.parent': return getElementParent(p.selector, p.levels);
      case 'element.children': return getElementChildren(p.selector, p.limit);
      case 'element.siblings': return getElementSiblings(p.selector);
      case 'scroll.position': return getScrollPosition();
      case 'intersection.observe': return intersectionObserve(p.selector, p.threshold);
      case 'intersection.check': return intersectionCheck(p.selector);
      case 'intersection.stop': return intersectionStop();
      case 'canvas.readPixels': return canvasReadPixels(p.selector, p.x, p.y, p.width, p.height);
      case 'network.observeXHR': return networkObserveXHR(p.filter);
      case 'network.flushXHR': return networkFlushXHR();
      case 'smart.select': return smartSelect(p.description, p.value);
      case 'element.highlight.multiple': return highlightMultiple(p.selectors, p.color, p.duration);
      // v4.0 content commands
      case 'element.focus': return elementFocus(p.selector);
      case 'element.blur': return elementBlur(p.selector);
      case 'element.closest': return elementClosest(p.selector, p.ancestor);
      case 'element.style': return elementStyle(p.selector, p.styles);
      case 'element.offset': return elementOffset(p.selector);
      case 'element.matches': return elementMatches(p.selector, p.test);
      case 'element.type': return elementNaturalType(p.selector, p.text, p.delay);
      case 'element.hover': return elementHover(p.selector);
      case 'forms.submit': return formsSubmit(p.selector, p.formIndex);
      case 'dom.serialize': return domSerialize(p.selector, p.includeStyles);
      case 'dom.ready': return domReady(p.timeout);
      case 'clipboard.copy': return clipboardCopy(p.text);
      case 'page.freeze': return pageFreeze();
      case 'page.unfreeze': return pageUnfreeze();
      default: return { __error: `Unknown content command: ${msg.command}` };
    }
  }

  // ─── Smart Element Finder (AI-like matching) ───

  function smartFind(description, action) {
    if (!description) return { __error: 'description required' };
    const el = findByDescription(description);
    if (!el) return { __error: `No element found matching: "${description}"` };

    if (action === 'click') {
      el.scrollIntoView({ block: 'center' });
      el.click();
      return { action: 'clicked', ...nodeToInfo(el) };
    }
    return nodeToInfo(el);
  }

  function smartFindAll(description, limit = 20) {
    if (!description) return [];
    return findAllByDescription(description, limit).map(nodeToInfo);
  }

  function smartClick(description) {
    const el = findByDescription(description);
    if (!el) return { __error: `Cannot find: "${description}"` };
    el.scrollIntoView({ block: 'center' });
    el.focus();
    el.click();
    return { clicked: true, ...nodeToInfo(el) };
  }

  function smartFill(description, value) {
    const el = findByDescription(description);
    if (!el) return { __error: `Cannot find: "${description}"` };
    el.scrollIntoView({ block: 'center' });
    el.focus();
    if (el.isContentEditable) {
      el.innerText = '';
      document.execCommand('insertText', false, value);
    } else {
      el.value = value;
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    }
    return { filled: true, ...nodeToInfo(el) };
  }

  function findByDescription(desc) {
    const d = desc.toLowerCase().trim();

    // 1. Exact match strategies
    // By aria-label
    let el = document.querySelector(`[aria-label="${desc}" i]`);
    if (el && isInteractable(el)) return el;

    // By placeholder
    el = document.querySelector(`[placeholder="${desc}" i]`);
    if (el && isInteractable(el)) return el;

    // By title
    el = document.querySelector(`[title="${desc}" i]`);
    if (el && isInteractable(el)) return el;

    // By name
    el = document.querySelector(`[name="${desc}" i]`);
    if (el && isInteractable(el)) return el;

    // By id
    el = document.getElementById(desc);
    if (el && isInteractable(el)) return el;

    // By exact text match on interactive elements
    const interactive = 'a, button, input, select, textarea, [role="button"], [role="link"], [role="tab"], [tabindex]';
    for (const candidate of document.querySelectorAll(interactive)) {
      const text = getElementLabel(candidate);
      if (text === d) return candidate;
    }

    // 2. Fuzzy/contains match
    // By aria-label contains
    for (const candidate of document.querySelectorAll('[aria-label]')) {
      if (candidate.getAttribute('aria-label').toLowerCase().includes(d) && isInteractable(candidate))
        return candidate;
    }

    // By label text
    for (const label of document.querySelectorAll('label')) {
      if (label.textContent.toLowerCase().trim().includes(d)) {
        const input = label.htmlFor ? document.getElementById(label.htmlFor) : label.querySelector('input,select,textarea');
        if (input) return input;
      }
    }

    // By text content on interactive elements
    for (const candidate of document.querySelectorAll(interactive)) {
      const text = getElementLabel(candidate);
      if (text.includes(d)) return candidate;
    }

    // 3. Broader text search on all visible elements
    for (const candidate of document.querySelectorAll('*')) {
      if (!isInteractable(candidate)) continue;
      const text = (candidate.innerText || candidate.textContent || '').toLowerCase().trim();
      if (text === d || (text.length < 200 && text.includes(d))) return candidate;
    }

    // 4. Role-based matching (e.g. "search box", "submit button", "login")
    const roleKeywords = {
      'search': ['search', 'searchbox', 'q'],
      'submit': ['submit', 'go', 'send', 'ok', 'done', 'apply'],
      'login': ['login', 'signin', 'sign in', 'log in'],
      'signup': ['signup', 'sign up', 'register', 'create account'],
      'email': ['email', 'e-mail', 'mail'],
      'password': ['password', 'passwd', 'pass'],
      'username': ['username', 'user', 'login'],
      'close': ['close', 'dismiss', 'x', '×'],
      'menu': ['menu', 'hamburger', 'nav'],
      'next': ['next', 'continue', 'forward'],
      'back': ['back', 'previous', 'return'],
      'cancel': ['cancel', 'abort', 'nevermind'],
      'save': ['save', 'store', 'keep'],
      'delete': ['delete', 'remove', 'trash'],
      'edit': ['edit', 'modify', 'change'],
      'add': ['add', 'new', 'create', 'plus', '+'],
    };

    for (const [category, keywords] of Object.entries(roleKeywords)) {
      if (keywords.some(kw => d.includes(kw) || kw.includes(d))) {
        // Search by input type
        if (['email', 'password', 'search', 'username'].includes(category)) {
          el = document.querySelector(`input[type="${category}"]`) ||
               document.querySelector(`input[name*="${category}" i]`) ||
               document.querySelector(`input[placeholder*="${category}" i]`);
          if (el) return el;
        }
        // Search by button type
        if (['submit', 'login', 'signup'].includes(category)) {
          el = document.querySelector(`button[type="submit"]`) ||
               document.querySelector(`input[type="submit"]`);
          if (el) return el;
        }
      }
    }

    // 5. Fuzzy scoring fallback (Levenshtein-like)
    const scoredCandidates = [];
    const allInteractive = document.querySelectorAll(interactive);
    for (const candidate of allInteractive) {
      const label = getElementLabel(candidate);
      if (!label || label.length > 200) continue;
      const score = fuzzyScore(d, label);
      if (score > 0.4) scoredCandidates.push({ el: candidate, score });
    }
    if (scoredCandidates.length) {
      scoredCandidates.sort((a, b) => b.score - a.score);
      return scoredCandidates[0].el;
    }

    // 6. Shadow DOM traversal
    for (const host of document.querySelectorAll('*')) {
      if (!host.shadowRoot) continue;
      const shadowEl = findInShadow(host.shadowRoot, d);
      if (shadowEl) return shadowEl;
    }

    return null;
  }

  function fuzzyScore(query, text) {
    if (text === query) return 1;
    if (text.includes(query)) return 0.9;
    if (query.includes(text)) return 0.7;
    // Simple bigram overlap score
    const qBigrams = new Set();
    for (let i = 0; i < query.length - 1; i++) qBigrams.add(query.slice(i, i + 2));
    const tBigrams = new Set();
    for (let i = 0; i < text.length - 1; i++) tBigrams.add(text.slice(i, i + 2));
    if (!qBigrams.size || !tBigrams.size) return 0;
    let shared = 0;
    for (const bg of qBigrams) { if (tBigrams.has(bg)) shared++; }
    return (2 * shared) / (qBigrams.size + tBigrams.size);
  }

  function findInShadow(root, desc) {
    for (const el of root.querySelectorAll('a, button, input, select, textarea, [role="button"], [role="link"]')) {
      const label = getElementLabel(el);
      if (label.includes(desc) || desc.includes(label)) return el;
    }
    for (const child of root.querySelectorAll('*')) {
      if (child.shadowRoot) {
        const found = findInShadow(child.shadowRoot, desc);
        if (found) return found;
      }
    }
    return null;
  }

  function findAllByDescription(desc, limit = 20) {
    const d = desc.toLowerCase().trim();
    const results = [];
    const seen = new Set();

    for (const el of document.querySelectorAll('*')) {
      if (results.length >= limit) break;
      if (!isInteractable(el)) continue;
      if (seen.has(el)) continue;

      const label = getElementLabel(el);
      const text = (el.innerText || el.textContent || '').toLowerCase().trim();

      if (label.includes(d) || (text.length < 300 && text.includes(d))) {
        seen.add(el);
        results.push(el);
      }
    }
    return results;
  }

  function getElementLabel(el) {
    return (
      el.getAttribute('aria-label') ||
      el.getAttribute('placeholder') ||
      el.getAttribute('title') ||
      el.getAttribute('alt') ||
      el.getAttribute('name') ||
      (el.labels && el.labels[0]?.textContent) ||
      el.textContent || ''
    ).toLowerCase().trim();
  }

  function isInteractable(el) {
    if (!el || !el.tagName) return false;
    if (el.offsetParent === null && el.tagName !== 'BODY') return false;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || parseFloat(style.opacity) === 0) return false;
    return true;
  }

  // ─── Smart Wait (Interactive) ───

  async function smartWait(description, timeout = 10000) {
    const start = Date.now();
    while (Date.now() - start < timeout) {
      const el = findByDescription(description);
      if (el) return { found: true, elapsed: Date.now() - start, ...nodeToInfo(el) };
      await new Promise(r => setTimeout(r, 200));
    }
    return { found: false, elapsed: timeout };
  }

  async function waitInteractive(selector, timeout = 10000) {
    const start = Date.now();
    while (Date.now() - start < timeout) {
      const el = document.querySelector(selector);
      if (el && isInteractable(el)) {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0 && !el.disabled) {
          return { interactive: true, elapsed: Date.now() - start };
        }
      }
      await new Promise(r => setTimeout(r, 150));
    }
    return { interactive: false, elapsed: timeout };
  }

  // ─── DOM Snapshot & Diff ───

  const snapshots = {};

  function domSnapshot(snapshotId) {
    const id = snapshotId || 'default';
    const snapshot = captureSnapshot();
    snapshots[id] = snapshot;
    return { snapshotId: id, elements: snapshot.length };
  }

  function domDiff(snapshotId) {
    const id = snapshotId || 'default';
    const prev = snapshots[id];
    if (!prev) return { __error: 'No previous snapshot. Call dom.snapshot first.' };
    const current = captureSnapshot();
    const added = [], removed = [], changed = [];
    const prevMap = new Map(prev.map(e => [e.path, e]));
    const currMap = new Map(current.map(e => [e.path, e]));

    for (const [path, el] of currMap) {
      if (!prevMap.has(path)) added.push(el);
      else {
        const old = prevMap.get(path);
        if (old.text !== el.text || old.tag !== el.tag) changed.push({ before: old, after: el });
      }
    }
    for (const [path] of prevMap) {
      if (!currMap.has(path)) removed.push(prevMap.get(path));
    }

    snapshots[id] = current;
    return { added: added.slice(0, 50), removed: removed.slice(0, 50), changed: changed.slice(0, 50) };
  }

  function captureSnapshot() {
    const els = [];
    const all = document.querySelectorAll('body *');
    for (let i = 0; i < Math.min(all.length, 2000); i++) {
      const el = all[i];
      if (el.tagName === 'SCRIPT' || el.tagName === 'STYLE' || el.tagName === 'NOSCRIPT') continue;
      els.push({
        path: buildSelector(el),
        tag: el.tagName.toLowerCase(),
        text: (el.innerText || '').substring(0, 80).trim(),
        visible: el.offsetParent !== null,
      });
    }
    return els;
  }

  // ─── Drag and Drop ───

  async function dragAndDrop(fromSelector, toSelector) {
    const from = typeof fromSelector === 'string' ? document.querySelector(fromSelector) : null;
    const to = typeof toSelector === 'string' ? document.querySelector(toSelector) : null;
    if (!from) return { __error: 'Source not found: ' + fromSelector };
    if (!to) return { __error: 'Target not found: ' + toSelector };

    const fromRect = from.getBoundingClientRect();
    const toRect = to.getBoundingClientRect();
    const fx = fromRect.left + fromRect.width / 2;
    const fy = fromRect.top + fromRect.height / 2;
    const tx = toRect.left + toRect.width / 2;
    const ty = toRect.top + toRect.height / 2;

    // HTML5 Drag and Drop API
    const dataTransfer = new DataTransfer();
    from.dispatchEvent(new DragEvent('dragstart', { bubbles: true, clientX: fx, clientY: fy, dataTransfer }));
    await new Promise(r => setTimeout(r, 50));
    to.dispatchEvent(new DragEvent('dragenter', { bubbles: true, clientX: tx, clientY: ty, dataTransfer }));
    to.dispatchEvent(new DragEvent('dragover', { bubbles: true, clientX: tx, clientY: ty, dataTransfer }));
    await new Promise(r => setTimeout(r, 50));
    to.dispatchEvent(new DragEvent('drop', { bubbles: true, clientX: tx, clientY: ty, dataTransfer }));
    from.dispatchEvent(new DragEvent('dragend', { bubbles: true, clientX: tx, clientY: ty, dataTransfer }));

    return { dragged: true, from: fromSelector, to: toSelector };
  }

  // ─── Session Recording ───

  let recording = false;
  let recordedEvents = [];

  function recordStart() {
    recording = true;
    recordedEvents = [];

    const handler = (e) => {
      if (!recording) return;
      const entry = { time: Date.now(), type: e.type };
      if (e.target && e.target.tagName) {
        entry.selector = buildSelector(e.target);
        entry.tag = e.target.tagName.toLowerCase();
      }
      if (e.type === 'click' || e.type === 'mousedown') {
        entry.x = e.clientX; entry.y = e.clientY;
      }
      if (e.type === 'input' || e.type === 'change') {
        entry.value = e.target.value?.substring(0, 500);
      }
      if (e.type === 'keydown') {
        entry.key = e.key; entry.code = e.code;
      }
      if (e.type === 'scroll') {
        entry.scrollX = window.scrollX; entry.scrollY = window.scrollY;
      }
      recordedEvents.push(entry);
      if (recordedEvents.length > 5000) recordedEvents.shift();
    };

    ['click', 'input', 'change', 'keydown', 'scroll'].forEach(
      evt => document.addEventListener(evt, handler, { capture: true })
    );
    window.__bridgeRecordHandler = handler;
    return { recording: true };
  }

  function recordStop() {
    recording = false;
    if (window.__bridgeRecordHandler) {
      ['click', 'input', 'change', 'keydown', 'scroll'].forEach(
        evt => document.removeEventListener(evt, window.__bridgeRecordHandler, { capture: true })
      );
    }
    const events = [...recordedEvents];
    // Normalize timestamps to relative
    if (events.length) {
      const baseTime = events[0].time;
      events.forEach(e => e.time -= baseTime);
    }
    return { events, count: events.length };
  }

  async function recordReplay(events, speed = 1) {
    if (!events || !events.length) return { __error: 'No events to replay' };
    let replayed = 0;

    for (let i = 0; i < events.length; i++) {
      const evt = events[i];
      const delay = i > 0 ? (evt.time - events[i-1].time) / speed : 0;
      if (delay > 0) await new Promise(r => setTimeout(r, Math.min(delay, 3000)));

      const el = evt.selector ? document.querySelector(evt.selector) : null;

      if (evt.type === 'click' && el) {
        el.scrollIntoView({ block: 'center' });
        el.click();
        replayed++;
      } else if ((evt.type === 'input' || evt.type === 'change') && el && evt.value !== undefined) {
        el.focus();
        el.value = evt.value;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        replayed++;
      } else if (evt.type === 'keydown' && evt.key) {
        const target = el || document.activeElement;
        target.dispatchEvent(new KeyboardEvent('keydown', { key: evt.key, code: evt.code, bubbles: true }));
        replayed++;
      } else if (evt.type === 'scroll') {
        window.scrollTo(evt.scrollX || 0, evt.scrollY || 0);
        replayed++;
      }
    }
    return { replayed, total: events.length };
  }

  // ─── Page Readiness Check ───

  function pageReadiness() {
    const pending = performance.getEntriesByType('resource').filter(r => r.responseEnd === 0).length;
    const images = Array.from(document.images);
    const loadedImages = images.filter(img => img.complete && img.naturalWidth > 0).length;
    const fonts = document.fonts ? document.fonts.status : 'unknown';

    return {
      readyState: document.readyState,
      domComplete: document.readyState === 'complete',
      pendingResources: pending,
      images: { total: images.length, loaded: loadedImages },
      fonts: fonts,
      interactive: document.readyState !== 'loading',
      scripts: {
        total: document.scripts.length,
        async: Array.from(document.scripts).filter(s => s.async).length,
        defer: Array.from(document.scripts).filter(s => s.defer).length,
      },
      stylesheets: document.styleSheets.length,
    };
  }

  // ─── Stealth Check ───

  function stealthCheck() {
    const checks = {};
    checks.webdriver = navigator.webdriver;
    checks.languages = navigator.languages?.length > 0;
    checks.plugins = navigator.plugins?.length > 0;
    checks.chrome = !!window.chrome;
    checks.permissions = !!navigator.permissions;
    checks.webgl = (() => {
      try {
        const c = document.createElement('canvas');
        return !!(c.getContext('webgl') || c.getContext('experimental-webgl'));
      } catch { return false; }
    })();
    checks.canvas = (() => {
      try {
        const c = document.createElement('canvas');
        return !!c.getContext('2d');
      } catch { return false; }
    })();
    checks.audioContext = typeof AudioContext !== 'undefined' || typeof webkitAudioContext !== 'undefined';
    checks.notifications = 'Notification' in window;
    checks.hardwareConcurrency = navigator.hardwareConcurrency;
    checks.deviceMemory = navigator.deviceMemory;
    checks.platform = navigator.platform;
    checks.userAgent = navigator.userAgent;
    checks.screenRes = `${screen.width}x${screen.height}`;
    checks.colorDepth = screen.colorDepth;
    checks.touchPoints = navigator.maxTouchPoints;

    // Detection flags
    checks.detected = {
      webdriverFlag: !!navigator.webdriver,
      automationControlled: !!window.cdc_adoQpoasnfa76pfcZLmcfl_Array,
      phantomjs: !!window._phantom || !!window.__phantomas,
      headless: /HeadlessChrome/.test(navigator.userAgent),
      seleniumIDE: !!document.__selenium_evaluate || !!document.__selenium_unwrapped,
    };

    return checks;
  }

  // ─── Iframe Traversal ───

  function listIframes() {
    const iframes = document.querySelectorAll('iframe');
    return Array.from(iframes).map((iframe, i) => {
      let accessible = false;
      try { accessible = !!iframe.contentDocument; } catch {}
      return {
        index: i, src: iframe.src, name: iframe.name || null,
        id: iframe.id || null, width: iframe.width, height: iframe.height,
        accessible,
      };
    });
  }

  function iframeEval(index, expression) {
    const iframes = document.querySelectorAll('iframe');
    const iframe = iframes[index];
    if (!iframe) return { __error: `Iframe index ${index} not found` };
    try {
      const result = iframe.contentWindow.eval(expression);
      return { result: typeof result === 'object' ? JSON.parse(JSON.stringify(result)) : result };
    } catch (e) {
      return { __error: e.message };
    }
  }

  // ─── Element Screenshot (as data URL) ───

  async function elementScreenshot(selector) {
    const el = document.querySelector(selector);
    if (!el) return { __error: 'Element not found' };
    el.scrollIntoView({ block: 'center' });
    const rect = el.getBoundingClientRect();
    return { bounds: { x: rect.x, y: rect.y, width: rect.width, height: rect.height } };
  }

  // ─── XPath ───

  function queryXPath(expression, contextNode) {
    const ctx = contextNode ? document.querySelector(contextNode) : document;
    if (!ctx) return { __error: 'Context node not found' };
    const result = document.evaluate(expression, ctx, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
    const node = result.singleNodeValue;
    if (!node) return null;
    return nodeToInfo(node);
  }

  function queryXPathAll(expression, limit = 50) {
    const result = document.evaluate(expression, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
    const nodes = [];
    const max = Math.min(result.snapshotLength, limit);
    for (let i = 0; i < max; i++) {
      nodes.push(nodeToInfo(result.snapshotItem(i)));
    }
    return nodes;
  }

  // ─── Shadow DOM ───

  function queryShadowDOM(selector, hostSelector) {
    const host = hostSelector ? document.querySelector(hostSelector) : document.body;
    if (!host) return { __error: 'Host not found' };
    const results = [];
    traverseShadowDOM(host, selector, results, 10);
    return results;
  }

  function traverseShadowDOM(node, selector, results, depth) {
    if (depth <= 0 || results.length >= 50) return;
    if (node.shadowRoot) {
      const found = node.shadowRoot.querySelectorAll(selector);
      found.forEach(el => results.push(nodeToInfo(el)));
      node.shadowRoot.querySelectorAll('*').forEach(child => {
        if (child.shadowRoot) traverseShadowDOM(child, selector, results, depth - 1);
      });
    }
    node.querySelectorAll('*').forEach(child => {
      if (child.shadowRoot) traverseShadowDOM(child, selector, results, depth - 1);
    });
  }

  // ─── Element Highlighting ───

  function highlightElement(selector, color = 'rgba(99, 102, 241, 0.3)', duration = 3000) {
    clearHighlight();
    const el = document.querySelector(selector);
    if (!el) return { __error: 'Element not found' };
    const rect = el.getBoundingClientRect();
    highlightOverlay = document.createElement('div');
    Object.assign(highlightOverlay.style, {
      position: 'fixed', zIndex: '2147483647', pointerEvents: 'none',
      left: rect.left + 'px', top: rect.top + 'px',
      width: rect.width + 'px', height: rect.height + 'px',
      background: color, border: '2px solid rgba(99, 102, 241, 0.8)',
      borderRadius: '3px', transition: 'opacity 0.3s'
    });
    document.body.appendChild(highlightOverlay);
    if (duration > 0) {
      setTimeout(() => clearHighlight(), duration);
    }
    return { highlighted: selector, bounds: { x: rect.left, y: rect.top, w: rect.width, h: rect.height } };
  }

  function clearHighlight() {
    if (highlightOverlay && highlightOverlay.parentNode) {
      highlightOverlay.parentNode.removeChild(highlightOverlay);
    }
    highlightOverlay = null;
    return { ok: true };
  }

  // ─── Accessibility Snapshot ───

  function getAccessibilitySnapshot(rootSelector) {
    const root = rootSelector ? document.querySelector(rootSelector) : document.body;
    if (!root) return { __error: 'Root not found' };
    return buildA11yTree(root, 0, 5);
  }

  function buildA11yTree(el, depth, maxDepth) {
    if (depth > maxDepth) return null;
    const role = el.getAttribute('role') || getImplicitRole(el);
    const name = getAccessibleName(el);
    const node = {
      tag: el.tagName?.toLowerCase(),
      role: role,
      name: name,
      value: el.value || null,
      checked: el.checked ?? null,
      disabled: el.disabled ?? null,
      expanded: el.getAttribute('aria-expanded'),
      hidden: el.hidden || el.getAttribute('aria-hidden') === 'true',
    };

    // Remove null fields
    Object.keys(node).forEach(k => { if (node[k] === null || node[k] === undefined) delete node[k]; });

    const children = [];
    for (const child of el.children) {
      if (child.tagName === 'SCRIPT' || child.tagName === 'STYLE' || child.tagName === 'NOSCRIPT') continue;
      const childNode = buildA11yTree(child, depth + 1, maxDepth);
      if (childNode) children.push(childNode);
    }
    if (children.length) node.children = children;
    return node;
  }

  function getImplicitRole(el) {
    const tag = el.tagName?.toLowerCase();
    const roleMap = {
      a: el.href ? 'link' : null, button: 'button', input: getInputRole(el),
      select: 'combobox', textarea: 'textbox', img: 'img', nav: 'navigation',
      main: 'main', header: 'banner', footer: 'contentinfo', aside: 'complementary',
      form: 'form', table: 'table', ul: 'list', ol: 'list', li: 'listitem',
      h1: 'heading', h2: 'heading', h3: 'heading', h4: 'heading', h5: 'heading', h6: 'heading',
      dialog: 'dialog', article: 'article', section: 'region',
    };
    return roleMap[tag] || null;
  }

  function getInputRole(el) {
    const type = (el.type || 'text').toLowerCase();
    const map = { checkbox: 'checkbox', radio: 'radio', range: 'slider', search: 'searchbox' };
    return map[type] || 'textbox';
  }

  function getAccessibleName(el) {
    return el.getAttribute('aria-label')
      || el.getAttribute('aria-labelledby') && document.getElementById(el.getAttribute('aria-labelledby'))?.textContent
      || el.getAttribute('alt')
      || el.getAttribute('title')
      || el.getAttribute('placeholder')
      || (el.labels && el.labels[0]?.textContent)
      || (el.tagName === 'IMG' ? el.alt : null)
      || (el.innerText && el.children.length === 0 ? el.innerText.trim().substring(0, 100) : null)
      || null;
  }

  // ─── Mutation Observer ───

  function startMutationObserver(selector, config = {}) {
    stopMutationObserver();
    const target = selector ? document.querySelector(selector) : document.body;
    if (!target) return { __error: 'Target not found' };
    mutationBuffer = [];
    mutationObserver = new MutationObserver((mutations) => {
      for (const m of mutations) {
        if (mutationBuffer.length >= MAX_MUTATION_BUFFER) break;
        mutationBuffer.push({
          type: m.type,
          target: m.target.tagName?.toLowerCase() + (m.target.id ? '#' + m.target.id : ''),
          addedNodes: m.addedNodes.length,
          removedNodes: m.removedNodes.length,
          attributeName: m.attributeName,
          oldValue: m.oldValue?.substring(0, 100),
        });
      }
    });
    mutationObserver.observe(target, {
      childList: config.childList !== false,
      attributes: config.attributes !== false,
      subtree: config.subtree !== false,
      characterData: !!config.characterData,
      attributeOldValue: !!config.attributeOldValue,
    });
    return { observing: true };
  }

  function stopMutationObserver() {
    if (mutationObserver) {
      mutationObserver.disconnect();
      mutationObserver = null;
    }
    return { stopped: true };
  }

  function flushMutations() {
    const data = [...mutationBuffer];
    mutationBuffer = [];
    return data;
  }

  // ─── Element Queries ───

  function elementsAtPoint(x, y) {
    const els = document.elementsFromPoint(x, y);
    return els.slice(0, 10).map(nodeToInfo);
  }

  function getElementBounds(selector) {
    const el = document.querySelector(selector);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { x: r.x, y: r.y, width: r.width, height: r.height,
      top: r.top, right: r.right, bottom: r.bottom, left: r.left,
      visible: el.offsetParent !== null, inViewport: isInViewport(r) };
  }

  function getComputedStyles(selector, properties) {
    const el = document.querySelector(selector);
    if (!el) return null;
    const styles = window.getComputedStyle(el);
    if (properties && Array.isArray(properties)) {
      const result = {};
      properties.forEach(p => result[p] = styles.getPropertyValue(p));
      return result;
    }
    // Return common properties
    return {
      display: styles.display, position: styles.position, visibility: styles.visibility,
      opacity: styles.opacity, color: styles.color, backgroundColor: styles.backgroundColor,
      fontSize: styles.fontSize, fontFamily: styles.fontFamily, fontWeight: styles.fontWeight,
      width: styles.width, height: styles.height, margin: styles.margin, padding: styles.padding,
      border: styles.border, overflow: styles.overflow, zIndex: styles.zIndex,
    };
  }

  // ─── Smart Form Detection & Filling ───

  function detectForms() {
    const forms = document.querySelectorAll('form');
    if (!forms.length) {
      // Try to find form-like containers
      const inputs = document.querySelectorAll('input, select, textarea');
      if (!inputs.length) return [];
      return [{ index: 0, formless: true, fields: Array.from(inputs).slice(0, 50).map(fieldInfo) }];
    }
    return Array.from(forms).slice(0, 10).map((form, i) => ({
      index: i,
      id: form.id || null,
      action: form.action || null,
      method: form.method || 'get',
      fields: Array.from(form.querySelectorAll('input, select, textarea')).map(fieldInfo),
    }));
  }

  function fieldInfo(el) {
    const label = el.labels?.[0]?.textContent?.trim()
      || el.getAttribute('aria-label')
      || el.getAttribute('placeholder')
      || el.name || el.id || null;
    return {
      tag: el.tagName.toLowerCase(),
      type: el.type || null,
      name: el.name || null,
      id: el.id || null,
      label: label,
      value: el.value || null,
      required: el.required || false,
      selector: buildSelector(el),
    };
  }

  function smartFillForm(data, formIndex = 0) {
    const forms = detectForms();
    if (!forms.length) return { __error: 'No forms found' };
    const form = forms[formIndex];
    if (!form) return { __error: `Form index ${formIndex} not found` };

    let filled = 0;
    for (const [key, value] of Object.entries(data)) {
      // Match by name, id, label, or type
      const field = form.fields.find(f =>
        f.name === key || f.id === key ||
        (f.label && f.label.toLowerCase().includes(key.toLowerCase())) ||
        f.type === key
      );
      if (field && field.selector) {
        const el = document.querySelector(field.selector);
        if (el) {
          if (el.type === 'checkbox' || el.type === 'radio') {
            el.checked = !!value;
          } else if (el.tagName === 'SELECT') {
            el.value = value;
          } else {
            el.value = value;
          }
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
          filled++;
        }
      }
    }
    return { filled, total: Object.keys(data).length };
  }

  // ─── Link Extraction ───

  function extractLinks(filter) {
    const links = document.querySelectorAll('a[href]');
    let results = Array.from(links).map(a => ({
      href: a.href,
      text: a.innerText?.trim().substring(0, 200) || '',
      title: a.title || null,
      rel: a.rel || null,
      target: a.target || null,
      visible: a.offsetParent !== null,
    }));

    if (filter) {
      const f = filter.toLowerCase();
      results = results.filter(l =>
        l.href.toLowerCase().includes(f) || l.text.toLowerCase().includes(f)
      );
    }

    // Deduplicate by href
    const seen = new Set();
    results = results.filter(l => {
      if (seen.has(l.href)) return false;
      seen.add(l.href);
      return true;
    });

    return results.slice(0, 500);
  }

  // ─── Table Extraction ───

  function extractTables(selector) {
    const tables = selector
      ? document.querySelectorAll(selector)
      : document.querySelectorAll('table');

    return Array.from(tables).slice(0, 10).map((table, i) => {
      const headers = Array.from(table.querySelectorAll('thead th, thead td, tr:first-child th'))
        .map(th => th.innerText.trim());

      const rows = [];
      table.querySelectorAll('tbody tr, tr').forEach((tr, ri) => {
        if (ri === 0 && headers.length > 0) return; // Skip header row
        const cells = Array.from(tr.querySelectorAll('td, th')).map(td => td.innerText.trim());
        if (cells.length) rows.push(cells);
      });

      return { index: i, headers, rows: rows.slice(0, 200), rowCount: rows.length };
    });
  }

  // ─── Text Search ───

  function searchText(query, highlight = false) {
    if (!query) return { matches: 0 };
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const matches = [];
    const q = query.toLowerCase();

    while (walker.nextNode()) {
      const text = walker.currentNode.textContent;
      if (text.toLowerCase().includes(q)) {
        const parent = walker.currentNode.parentElement;
        if (parent && parent.offsetParent !== null) {
          const rect = parent.getBoundingClientRect();
          matches.push({
            text: text.trim().substring(0, 200),
            selector: buildSelector(parent),
            x: rect.left + rect.width / 2,
            y: rect.top + rect.height / 2,
          });
        }
      }
    }

    if (highlight && matches.length) {
      highlightElement(matches[0].selector, 'rgba(255, 255, 0, 0.3)', 5000);
    }

    return { matches: matches.length, results: matches.slice(0, 50) };
  }

  // ─── Meta Extraction ───

  function extractMeta() {
    const meta = {};
    meta.title = document.title;
    meta.description = document.querySelector('meta[name="description"]')?.content || null;
    meta.canonical = document.querySelector('link[rel="canonical"]')?.href || null;
    meta.favicon = document.querySelector('link[rel="icon"], link[rel="shortcut icon"]')?.href || null;
    meta.charset = document.characterSet;
    meta.language = document.documentElement.lang || null;
    meta.viewport = document.querySelector('meta[name="viewport"]')?.content || null;

    // Open Graph
    meta.og = {};
    document.querySelectorAll('meta[property^="og:"]').forEach(el => {
      meta.og[el.getAttribute('property').replace('og:', '')] = el.content;
    });
    if (!Object.keys(meta.og).length) delete meta.og;

    // Twitter Card
    meta.twitter = {};
    document.querySelectorAll('meta[name^="twitter:"]').forEach(el => {
      meta.twitter[el.getAttribute('name').replace('twitter:', '')] = el.content;
    });
    if (!Object.keys(meta.twitter).length) delete meta.twitter;

    // JSON-LD structured data
    const jsonLd = document.querySelectorAll('script[type="application/ld+json"]');
    if (jsonLd.length) {
      meta.structuredData = Array.from(jsonLd).map(s => {
        try { return JSON.parse(s.textContent); } catch { return null; }
      }).filter(Boolean);
    }

    // All meta tags
    meta.all = {};
    document.querySelectorAll('meta[name], meta[property]').forEach(el => {
      const key = el.getAttribute('name') || el.getAttribute('property');
      meta.all[key] = el.content;
    });

    return meta;
  }

  // ─── Infinite Scroll ───

  async function infiniteScroll(maxScrolls = 10, delay = 1500) {
    let prevHeight = document.body.scrollHeight;
    let scrollCount = 0;

    for (let i = 0; i < maxScrolls; i++) {
      window.scrollTo(0, document.body.scrollHeight);
      await new Promise(r => setTimeout(r, delay));
      const newHeight = document.body.scrollHeight;
      scrollCount++;
      if (newHeight === prevHeight) break;
      prevHeight = newHeight;
    }

    return { scrolls: scrollCount, finalHeight: document.body.scrollHeight };
  }

  // ─── Helpers ───

  function nodeToInfo(el) {
    if (!el || !el.tagName) return null;
    const r = el.getBoundingClientRect();
    return {
      tag: el.tagName.toLowerCase(),
      id: el.id || null,
      classes: el.className || null,
      text: el.innerText?.trim().substring(0, 200) || null,
      href: el.href || null,
      src: el.src || null,
      value: el.value || null,
      x: r.left + r.width / 2,
      y: r.top + r.height / 2,
      width: r.width,
      height: r.height,
      visible: el.offsetParent !== null,
      selector: buildSelector(el),
    };
  }

  function buildSelector(el) {
    if (el.id) return '#' + CSS.escape(el.id);
    // Prefer data-testid / data-cy / data-test for stable selectors
    for (const attr of ['data-testid', 'data-cy', 'data-test', 'data-automation-id']) {
      const val = el.getAttribute(attr);
      if (val) return `[${attr}="${CSS.escape(val)}"]`;
    }
    const parts = [];
    let current = el;
    while (current && current !== document.body && parts.length < 4) {
      let part = current.tagName.toLowerCase();
      if (current.id) { parts.unshift('#' + CSS.escape(current.id)); break; }
      // Check for stable attributes on ancestors
      for (const attr of ['data-testid', 'data-cy', 'data-test']) {
        const val = current.getAttribute(attr);
        if (val) { parts.unshift(`[${attr}="${CSS.escape(val)}"]`); current = null; break; }
      }
      if (!current) break;
      if (current.className && typeof current.className === 'string') {
        const cls = current.className.trim().split(/\s+/).slice(0, 2)
          .map(c => '.' + CSS.escape(c)).join('');
        part += cls;
      }
      const parent = current.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter(c => c.tagName === current.tagName);
        if (siblings.length > 1) {
          const idx = siblings.indexOf(current) + 1;
          part += `:nth-of-type(${idx})`;
        }
      }
      parts.unshift(part);
      current = current.parentElement;
    }
    return parts.join(' > ');
  }

  function isInViewport(rect) {
    return rect.top >= 0 && rect.left >= 0 &&
      rect.bottom <= (window.innerHeight || document.documentElement.clientHeight) &&
      rect.right <= (window.innerWidth || document.documentElement.clientWidth);
  }

  // ─── Wait for Element to Disappear ───

  async function waitForElementGone(selector, timeout = 10000) {
    const start = Date.now();
    while (Date.now() - start < timeout) {
      const el = document.querySelector(selector);
      if (!el || el.offsetParent === null) return { gone: true, elapsed: Date.now() - start };
      await new Promise(r => setTimeout(r, 150));
    }
    return { gone: false, elapsed: timeout };
  }

  // ─── Wait for DOM to Stabilize ───

  async function waitForDOMStable(timeout = 10000, idleTime = 500) {
    return new Promise((resolve) => {
      let lastMutationAt = Date.now();
      const observer = new MutationObserver(() => { lastMutationAt = Date.now(); });
      observer.observe(document.body, { childList: true, subtree: true, attributes: true });

      const deadline = Date.now() + timeout;
      const check = setInterval(() => {
        if (Date.now() - lastMutationAt >= idleTime || Date.now() >= deadline) {
          clearInterval(check);
          observer.disconnect();
          resolve({
            stable: Date.now() - lastMutationAt >= idleTime,
            elapsed: Date.now() - (deadline - timeout)
          });
        }
      }, 100);
    });
  }

  // ─── Element Attributes ───

  function getElementAttributes(selector) {
    const el = document.querySelector(selector);
    if (!el) return { __error: 'Element not found' };
    const attrs = {};
    for (const a of el.attributes) attrs[a.name] = a.value;
    return { tag: el.tagName.toLowerCase(), attributes: attrs, attributeCount: el.attributes.length };
  }

  function setElementAttribute(selector, attribute, value) {
    const el = document.querySelector(selector);
    if (!el) return { __error: 'Element not found' };
    if (value === null || value === undefined) {
      el.removeAttribute(attribute);
      return { ok: true, removed: attribute };
    }
    el.setAttribute(attribute, value);
    return { ok: true, set: attribute, value };
  }

  // ─── Generate XPath for Element ───

  function getElementXPath(selector) {
    const el = document.querySelector(selector);
    if (!el) return { __error: 'Element not found' };
    return { xpath: buildXPath(el), selector, ...nodeToInfo(el) };
  }

  function buildXPath(el) {
    if (el.id) return `//*[@id="${el.id}"]`;
    const parts = [];
    let current = el;
    while (current && current.nodeType === Node.ELEMENT_NODE) {
      let index = 0;
      let sibling = current.previousSibling;
      while (sibling) {
        if (sibling.nodeType === Node.ELEMENT_NODE && sibling.tagName === current.tagName) index++;
        sibling = sibling.previousSibling;
      }
      const tag = current.tagName.toLowerCase();
      parts.unshift(index > 0 ? `${tag}[${index + 1}]` : tag);
      current = current.parentNode;
    }
    return '/' + parts.join('/');
  }

  // ─── Custom Event Dispatch ───

  function dispatchCustomEvent(selector, eventName, detail) {
    const el = selector ? document.querySelector(selector) : document;
    if (!el) return { __error: 'Element not found' };
    const event = new CustomEvent(eventName, { bubbles: true, cancelable: true, detail });
    const dispatched = el.dispatchEvent(event);
    return { dispatched, event: eventName, defaultPrevented: !dispatched };
  }

  // ─── Element Traversal ───

  function getElementParent(selector, levels = 1) {
    let el = document.querySelector(selector);
    if (!el) return { __error: 'Element not found' };
    for (let i = 0; i < levels && el.parentElement; i++) el = el.parentElement;
    return nodeToInfo(el);
  }

  function getElementChildren(selector, limit = 50) {
    const el = document.querySelector(selector);
    if (!el) return { __error: 'Element not found' };
    return Array.from(el.children).slice(0, limit).map(nodeToInfo);
  }

  function getElementSiblings(selector) {
    const el = document.querySelector(selector);
    if (!el || !el.parentElement) return { __error: 'Element not found' };
    return Array.from(el.parentElement.children)
      .filter(c => c !== el)
      .slice(0, 30)
      .map(nodeToInfo);
  }

  // ─── Scroll Position ───

  function getScrollPosition() {
    return {
      scrollX: window.scrollX,
      scrollY: window.scrollY,
      scrollWidth: document.documentElement.scrollWidth,
      scrollHeight: document.documentElement.scrollHeight,
      clientWidth: document.documentElement.clientWidth,
      clientHeight: document.documentElement.clientHeight,
      atTop: window.scrollY === 0,
      atBottom: Math.abs(window.scrollY + window.innerHeight - document.documentElement.scrollHeight) < 2,
      percentY: Math.round(window.scrollY / Math.max(1, document.documentElement.scrollHeight - window.innerHeight) * 100)
    };
  }

  // ─── IntersectionObserver (Visibility Tracking) ───

  let intersectionObservers = new Map();
  let intersectionResults = new Map();

  function intersectionObserve(selector, threshold = 0) {
    const els = document.querySelectorAll(selector);
    if (!els.length) return { __error: 'No elements found' };

    intersectionStop(); // clear previous

    const thresholds = Array.isArray(threshold) ? threshold : [threshold];
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        const key = buildSelector(entry.target);
        intersectionResults.set(key, {
          selector: key,
          isIntersecting: entry.isIntersecting,
          intersectionRatio: Math.round(entry.intersectionRatio * 100) / 100,
          boundingRect: {
            x: entry.boundingClientRect.x, y: entry.boundingClientRect.y,
            width: entry.boundingClientRect.width, height: entry.boundingClientRect.height
          },
          time: entry.time
        });
      }
    }, { threshold: thresholds });

    els.forEach(el => observer.observe(el));
    intersectionObservers.set(selector, observer);
    return { observing: true, elements: els.length, threshold: thresholds };
  }

  function intersectionCheck(selector) {
    if (selector) {
      const result = intersectionResults.get(selector);
      if (result) return result;
      // Try to check directly without observer
      const el = document.querySelector(selector);
      if (!el) return { __error: 'Element not found' };
      const r = el.getBoundingClientRect();
      return {
        selector,
        isIntersecting: isInViewport(r),
        boundingRect: { x: r.x, y: r.y, width: r.width, height: r.height }
      };
    }
    return Array.from(intersectionResults.values());
  }

  function intersectionStop() {
    for (const [, observer] of intersectionObservers) observer.disconnect();
    intersectionObservers.clear();
    intersectionResults.clear();
    return { stopped: true };
  }

  // ─── Canvas Pixel Reading ───

  function canvasReadPixels(selector, x = 0, y = 0, width = 1, height = 1) {
    const canvas = document.querySelector(selector || 'canvas');
    if (!canvas || canvas.tagName !== 'CANVAS') return { __error: 'Canvas not found' };
    try {
      const ctx = canvas.getContext('2d');
      if (!ctx) return { __error: 'Cannot get 2D context' };
      const imageData = ctx.getImageData(x, y, Math.min(width, 100), Math.min(height, 100));
      // Return as compact array of [r,g,b,a] per pixel, limited
      const pixels = [];
      const limit = Math.min(imageData.data.length / 4, 1000);
      for (let i = 0; i < limit; i++) {
        pixels.push([
          imageData.data[i * 4], imageData.data[i * 4 + 1],
          imageData.data[i * 4 + 2], imageData.data[i * 4 + 3]
        ]);
      }
      return {
        width: imageData.width, height: imageData.height,
        pixels, canvasWidth: canvas.width, canvasHeight: canvas.height
      };
    } catch (e) {
      return { __error: 'Canvas read failed (possibly tainted): ' + e.message };
    }
  }

  // ─── Network XHR/Fetch Observer ───

  let xhrLog = [];
  let xhrObserving = false;
  const MAX_XHR_LOG = 300;

  function networkObserveXHR(filter) {
    if (xhrObserving) return { observing: true, entries: xhrLog.length };
    xhrObserving = true;
    xhrLog = [];

    // Intercept fetch
    const origFetch = window.fetch;
    window.fetch = async function(...args) {
      const url = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';
      const method = args[1]?.method || 'GET';
      const entry = { type: 'fetch', url, method, startTime: Date.now(), status: null, duration: null };
      try {
        const response = await origFetch.apply(this, args);
        entry.status = response.status;
        entry.duration = Date.now() - entry.startTime;
        pushXHRLog(entry, filter);
        return response;
      } catch (e) {
        entry.error = e.message;
        entry.duration = Date.now() - entry.startTime;
        pushXHRLog(entry, filter);
        throw e;
      }
    };

    // Intercept XMLHttpRequest
    const origOpen = XMLHttpRequest.prototype.open;
    const origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url) {
      this.__bridgeXHR = { type: 'xhr', method, url: String(url), startTime: null };
      return origOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function() {
      if (this.__bridgeXHR) {
        this.__bridgeXHR.startTime = Date.now();
        this.addEventListener('loadend', () => {
          const entry = this.__bridgeXHR;
          entry.status = this.status;
          entry.duration = Date.now() - entry.startTime;
          pushXHRLog(entry, filter);
        });
      }
      return origSend.apply(this, arguments);
    };

    window.__bridgeXHRCleanup = { origFetch, origOpen, origSend };
    return { observing: true };
  }

  function pushXHRLog(entry, filter) {
    if (filter && !entry.url.toLowerCase().includes(filter.toLowerCase())) return;
    xhrLog.push(entry);
    if (xhrLog.length > MAX_XHR_LOG) xhrLog.shift();
  }

  function networkFlushXHR() {
    const data = [...xhrLog];
    xhrLog = [];
    return { entries: data, count: data.length };
  }

  // ─── Smart Select (dropdown/combobox) ───

  function smartSelect(description, value) {
    const el = findByDescription(description);
    if (!el) return { __error: `Cannot find: "${description}"` };
    if (el.tagName !== 'SELECT') return { __error: 'Element is not a <select>' };
    el.scrollIntoView({ block: 'center' });
    // Find option by value or text
    let matched = false;
    for (const opt of el.options) {
      if (opt.value === value || opt.textContent.trim().toLowerCase() === String(value).toLowerCase()) {
        el.value = opt.value;
        matched = true;
        break;
      }
    }
    if (!matched) return { __error: `Option "${value}" not found in select` };
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return { selected: true, value: el.value, ...nodeToInfo(el) };
  }

  // ─── Highlight Multiple Elements ───

  function highlightMultiple(selectors, color = 'rgba(99, 102, 241, 0.3)', duration = 3000) {
    clearHighlight();
    if (!selectors || !selectors.length) return { __error: 'No selectors provided' };
    const results = [];
    const overlays = [];
    for (const sel of selectors.slice(0, 20)) {
      const el = document.querySelector(sel);
      if (!el) { results.push({ selector: sel, highlighted: false }); continue; }
      const rect = el.getBoundingClientRect();
      const overlay = document.createElement('div');
      Object.assign(overlay.style, {
        position: 'fixed', zIndex: '2147483647', pointerEvents: 'none',
        left: rect.left + 'px', top: rect.top + 'px',
        width: rect.width + 'px', height: rect.height + 'px',
        background: color, border: '2px solid rgba(99, 102, 241, 0.8)',
        borderRadius: '3px', transition: 'opacity 0.3s'
      });
      document.body.appendChild(overlay);
      overlays.push(overlay);
      results.push({ selector: sel, highlighted: true });
    }
    if (duration > 0) {
      setTimeout(() => {
        overlays.forEach(o => o.parentNode?.removeChild(o));
      }, duration);
    }
    return { highlighted: results.filter(r => r.highlighted).length, results };
  }

  // ─── v4.0: Element Focus/Blur ───

  function elementFocus(selector) {
    const el = selector ? document.querySelector(selector) : null;
    if (!el) return { __error: 'Element not found' };
    el.scrollIntoView({ block: 'center' });
    el.focus();
    return { focused: true, ...nodeToInfo(el) };
  }

  function elementBlur(selector) {
    const el = selector ? document.querySelector(selector) : document.activeElement;
    if (!el) return { __error: 'No element to blur' };
    el.blur();
    return { blurred: true, tag: el.tagName?.toLowerCase() };
  }

  // ─── v4.0: Element Closest Ancestor ───

  function elementClosest(selector, ancestorSelector) {
    const el = document.querySelector(selector);
    if (!el) return { __error: 'Element not found' };
    const ancestor = el.closest(ancestorSelector);
    if (!ancestor) return { __error: `No ancestor matching "${ancestorSelector}"` };
    return nodeToInfo(ancestor);
  }

  // ─── v4.0: Element Inline Styles ───

  function elementStyle(selector, styles) {
    const el = document.querySelector(selector);
    if (!el) return { __error: 'Element not found' };
    if (styles && typeof styles === 'object') {
      Object.assign(el.style, styles);
      return { ok: true, applied: Object.keys(styles).length };
    }
    // Read mode — return computed + inline styles
    const computed = window.getComputedStyle(el);
    const inline = {};
    for (let i = 0; i < el.style.length; i++) {
      const prop = el.style[i];
      inline[prop] = el.style.getPropertyValue(prop);
    }
    return {
      inline,
      computed: {
        display: computed.display, position: computed.position, visibility: computed.visibility,
        opacity: computed.opacity, color: computed.color, backgroundColor: computed.backgroundColor,
        fontSize: computed.fontSize, fontWeight: computed.fontWeight, width: computed.width,
        height: computed.height, margin: computed.margin, padding: computed.padding,
        transform: computed.transform, transition: computed.transition, zIndex: computed.zIndex,
        overflow: computed.overflow, cursor: computed.cursor, pointerEvents: computed.pointerEvents
      }
    };
  }

  // ─── v4.0: Element Offset ───

  function elementOffset(selector) {
    const el = document.querySelector(selector);
    if (!el) return { __error: 'Element not found' };
    return {
      offsetTop: el.offsetTop, offsetLeft: el.offsetLeft,
      offsetWidth: el.offsetWidth, offsetHeight: el.offsetHeight,
      scrollTop: el.scrollTop, scrollLeft: el.scrollLeft,
      scrollWidth: el.scrollWidth, scrollHeight: el.scrollHeight,
      clientTop: el.clientTop, clientLeft: el.clientLeft,
      clientWidth: el.clientWidth, clientHeight: el.clientHeight
    };
  }

  // ─── v4.0: Element Matches Selector ───

  function elementMatches(selector, testSelector) {
    const el = document.querySelector(selector);
    if (!el) return { __error: 'Element not found' };
    return { matches: el.matches(testSelector), selector, test: testSelector };
  }

  // ─── v4.0: Natural Typing (key-by-key with delay) ───

  async function elementNaturalType(selector, text, delay = 50) {
    const el = selector ? document.querySelector(selector) : document.activeElement;
    if (!el) return { __error: 'Element not found' };
    el.scrollIntoView({ block: 'center' });
    el.focus();

    for (const char of text) {
      el.dispatchEvent(new KeyboardEvent('keydown', { key: char, bubbles: true }));
      if (el.isContentEditable) {
        document.execCommand('insertText', false, char);
      } else {
        el.value += char;
        el.dispatchEvent(new Event('input', { bubbles: true }));
      }
      el.dispatchEvent(new KeyboardEvent('keyup', { key: char, bubbles: true }));
      if (delay > 0) await new Promise(r => setTimeout(r, delay + Math.random() * delay * 0.5));
    }
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return { typed: text.length, naturalDelay: delay };
  }

  // ─── v4.0: Element Hover ───

  function elementHover(selector) {
    const el = document.querySelector(selector);
    if (!el) return { __error: 'Element not found' };
    el.scrollIntoView({ block: 'center' });
    const rect = el.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    el.dispatchEvent(new MouseEvent('mouseenter', { bubbles: true, clientX: x, clientY: y }));
    el.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, clientX: x, clientY: y }));
    el.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, clientX: x, clientY: y }));
    return { hovered: true, ...nodeToInfo(el) };
  }

  // ─── v4.0: Form Submit ───

  function formsSubmit(selector, formIndex = 0) {
    let form;
    if (selector) {
      form = document.querySelector(selector);
    } else {
      const forms = document.querySelectorAll('form');
      form = forms[formIndex];
    }
    if (!form) return { __error: 'Form not found' };
    if (form.tagName !== 'FORM') {
      form = form.closest('form');
      if (!form) return { __error: 'Element is not inside a form' };
    }
    // Try submit button first, then form.submit()
    const submitBtn = form.querySelector('[type="submit"], button:not([type])');
    if (submitBtn) {
      submitBtn.click();
      return { submitted: true, via: 'button', action: form.action || null };
    }
    form.requestSubmit ? form.requestSubmit() : form.submit();
    return { submitted: true, via: 'requestSubmit', action: form.action || null };
  }

  // ─── v4.0: DOM Serialize ───

  function domSerialize(selector, includeStyles = false) {
    const root = selector ? document.querySelector(selector) : document.documentElement;
    if (!root) return { __error: 'Element not found' };

    if (!includeStyles) {
      return { html: root.outerHTML, length: root.outerHTML.length };
    }

    // Serialize with computed styles inlined
    const clone = root.cloneNode(true);
    const origElements = root.querySelectorAll('*');
    const cloneElements = clone.querySelectorAll('*');
    for (let i = 0; i < Math.min(origElements.length, 500); i++) {
      const computed = window.getComputedStyle(origElements[i]);
      const important = ['display', 'position', 'color', 'background', 'font-size', 'margin', 'padding', 'border', 'width', 'height', 'visibility', 'opacity'];
      for (const prop of important) {
        cloneElements[i].style.setProperty(prop, computed.getPropertyValue(prop));
      }
    }
    const serialized = clone.outerHTML;
    return { html: serialized, length: serialized.length, elementsStyled: Math.min(origElements.length, 500) };
  }

  // ─── v4.0: DOM Ready ───

  async function domReady(timeout = 10000) {
    if (document.readyState === 'complete') return { ready: true, state: 'complete', elapsed: 0 };
    if (document.readyState === 'interactive') return { ready: true, state: 'interactive', elapsed: 0 };
    const start = Date.now();
    return new Promise(resolve => {
      const timer = setTimeout(() => {
        resolve({ ready: document.readyState !== 'loading', state: document.readyState, elapsed: timeout });
      }, timeout);
      const onReady = () => {
        clearTimeout(timer);
        document.removeEventListener('DOMContentLoaded', onReady);
        resolve({ ready: true, state: document.readyState, elapsed: Date.now() - start });
      };
      document.addEventListener('DOMContentLoaded', onReady);
    });
  }

  // ─── v4.0: Clipboard Copy ───

  async function clipboardCopy(text) {
    if (!text) return { __error: 'No text provided' };
    try {
      await navigator.clipboard.writeText(text);
      return { ok: true, copied: text.length };
    } catch {
      // Fallback: execCommand
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed;opacity:0;';
      document.body.appendChild(ta);
      ta.select();
      const success = document.execCommand('copy');
      document.body.removeChild(ta);
      return { ok: success, copied: text.length, fallback: true };
    }
  }

  // ─── v4.0: Page Freeze/Unfreeze ───

  let _frozenTimers = null;

  function pageFreeze() {
    if (_frozenTimers) return { __error: 'Already frozen' };
    _frozenTimers = {
      origSetTimeout: window.setTimeout,
      origSetInterval: window.setInterval,
      origRAF: window.requestAnimationFrame,
      ids: []
    };
    window.setTimeout = (fn, ...args) => { const id = _frozenTimers.origSetTimeout.call(window, () => {}, 999999); _frozenTimers.ids.push(id); return id; };
    window.setInterval = (fn, ...args) => { const id = _frozenTimers.origSetInterval.call(window, () => {}, 999999); _frozenTimers.ids.push(id); return id; };
    window.requestAnimationFrame = () => 0;
    // Pause CSS animations
    document.documentElement.style.setProperty('--bridge-anim-state', 'paused');
    const style = document.createElement('style');
    style.id = '__bridge_freeze_style';
    style.textContent = '*, *::before, *::after { animation-play-state: paused !important; transition: none !important; }';
    document.head.appendChild(style);
    return { frozen: true };
  }

  function pageUnfreeze() {
    if (!_frozenTimers) return { __error: 'Not frozen' };
    window.setTimeout = _frozenTimers.origSetTimeout;
    window.setInterval = _frozenTimers.origSetInterval;
    window.requestAnimationFrame = _frozenTimers.origRAF;
    _frozenTimers = null;
    const style = document.getElementById('__bridge_freeze_style');
    if (style) style.remove();
    return { unfrozen: true };
  }
})();
