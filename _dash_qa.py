import re, json, socket, time

def g(port, path):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(15)
    s.connect(('127.0.0.1', port))
    s.sendall(('GET %s HTTP/1.0\r\nHost: localhost\r\n\r\n' % path).encode())
    c = []
    while True:
        try:
            d = s.recv(16384)
            if not d: break
            c.append(d)
        except: break
    s.close()
    raw = b''.join(c).decode('utf-8', 'ignore')
    hdr, _, body = raw.partition('\r\n\r\n')
    return body

# Load dashboard HTML
html = g(8421, '/dashboard')
print('=== DASHBOARD QA (Beta redesign) ===')
print('HTML size: %d bytes' % len(html))

# Element IDs
ids = set(re.findall(r'id=["\'](\w[\w-]*)', html))
print('Element IDs: %d' % len(ids))

# Check critical IDs
critical = {
    'IQ Gauge': ['iqGauge', 'iqArc', 'iqTrend', 'iqFill'],
    'Self Panel': ['selfHealth', 'selfIQ', 'selfAssessText', 'selfBadge'],
    'Workers': ['card-alpha', 'card-beta', 'card-gamma', 'card-delta'],
    'Engines': ['engineGrid', 'engineCount'],
    'Bus': ['busFeed', 'busDepth'],
    'Tasks': ['taskQueueBody', 'taskQueueCount'],
    'Bus Health': ['bhMsgCount', 'bhThroughput', 'bhLastMsg'],
    'Todos': ['todoBody', 'todoCount'],
    'Clock': ['clock'],
}

print('\n=== PANEL ELEMENT CHECK ===')
all_ok = True
for panel, needed in critical.items():
    found = [i for i in needed if i in ids]
    missing = [i for i in needed if i not in ids]
    status = 'OK' if not missing else 'MISSING'
    if missing:
        all_ok = False
    print('  %-15s %d/%d %s %s' % (panel, len(found), len(needed), status, missing if missing else ''))

# Check fetch endpoints in JS
fetches = re.findall(r'fetch\(["\']([^"\']+)', html)
smarts = re.findall(r'smartFetch\(["\']([^"\']+)', html)
all_endpoints = set(fetches + smarts)
print('\n=== FETCH ENDPOINTS (%d unique) ===' % len(all_endpoints))
for ep in sorted(all_endpoints):
    print('  ' + ep)

# Check setIntervals
intervals = re.findall(r'setInterval\((\w+),\s*(\d+)', html)
print('\n=== POLLING INTERVALS ===')
for fn, ms in intervals:
    print('  %s every %sms' % (fn, ms))

# Check JS balance
js_blocks = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
print('\n=== JS SYNTAX CHECK ===')
for i, block in enumerate(js_blocks):
    braces_o = block.count('{')
    braces_c = block.count('}')
    parens_o = block.count('(')
    parens_c = block.count(')')
    issues = []
    if braces_o != braces_c:
        issues.append('braces %d/%d' % (braces_o, braces_c))
    if parens_o != parens_c:
        issues.append('parens %d/%d' % (parens_o, parens_c))
    if issues:
        print('  Block %d: ISSUES: %s' % (i, ', '.join(issues)))
    else:
        print('  Block %d: OK (braces=%d parens=%d)' % (i, braces_o, parens_o))

# Verify data endpoints return real data
print('\n=== DATA ENDPOINT VERIFICATION ===')
endpoints = {
    '/skynet/self/pulse': ['intelligence_score'],
    '/engines': ['engines'],
    '/status': ['agents'],
    '/bus/messages?limit=3': None,  # list
}
for ep, required_keys in endpoints.items():
    t = time.time()
    try:
        body = g(8421, ep)
        ms = (time.time() - t) * 1000
        d = json.loads(body)
        if required_keys:
            has_keys = all(k in d for k in required_keys)
            vals = {k: type(d.get(k)).__name__ for k in required_keys}
            print('  %s: %.0fms OK keys=%s' % (ep, ms, vals))
        else:
            print('  %s: %.0fms OK items=%d' % (ep, ms, len(d) if isinstance(d, list) else 1))
    except Exception as e:
        ms = (time.time() - t) * 1000
        print('  %s: %.0fms FAIL %s' % (ep, ms, str(e)[:50]))

# IQ value check
try:
    pulse = json.loads(g(8421, '/skynet/self/pulse'))
    iq = pulse.get('intelligence_score', 'MISSING')
    trend = pulse.get('iq_trend', 'MISSING')
    print('\n=== IQ VERIFICATION ===')
    print('  IQ score: %s (type: %s)' % (iq, type(iq).__name__))
    print('  Trend: %s' % trend)
    print('  REAL DATA: %s' % ('YES' if isinstance(iq, (int, float)) and iq > 0 else 'NO'))
except Exception as e:
    print('\n  IQ CHECK FAILED: %s' % e)

# Engine count check
try:
    eng_data = json.loads(g(8421, '/engines'))
    engines = eng_data.get('engines', {})
    online = sum(1 for v in engines.values() if v.get('status') == 'online')
    print('\n=== ENGINE VERIFICATION ===')
    print('  Total engines: %d' % len(engines))
    print('  Online: %d' % online)
    print('  Expected 18: %s' % ('YES' if len(engines) >= 18 else 'NO'))
except Exception as e:
    print('\n  ENGINE CHECK FAILED: %s' % e)

print('\n=== FINAL VERDICT ===')
print('  All panels have IDs: %s' % ('YES' if all_ok else 'NO'))
