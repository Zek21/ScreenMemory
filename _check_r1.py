import urllib.request, json

resp = urllib.request.urlopen('http://localhost:8420/bus/messages?limit=50')
msgs = json.loads(resp.read().decode())
print(f'=== BUS MESSAGES (last 50): {len(msgs)} total ===')

# Look for ROUND1 results
r1 = [m for m in msgs if 'ROUND1' in str(m.get('content',''))]
print(f'\nROUND1 results found: {len(r1)}')
for m in r1:
    s = m.get('sender', '?')
    c = str(m.get('content', ''))[:200]
    print(f'  {s}: {c}')

# Look for any worker results
results = [m for m in msgs if m.get('type') == 'result']
print(f'\nTotal result messages: {len(results)}')
for m in results:
    s = m.get('sender', '?')
    c = str(m.get('content', ''))[:200]
    print(f'  {s}: {c}')

# Worker identity acks
acks = [m for m in msgs if m.get('type') == 'identity_ack']
print(f'\nIdentity ACKs: {len(acks)}')
for m in acks:
    s = m.get('sender', '?')
    c = str(m.get('content', ''))[:100]
    print(f'  {s}: {c}')

# All recent messages from workers
worker_msgs = [m for m in msgs if m.get('sender') in ('alpha','beta','gamma','delta')]
print(f'\nAll worker messages: {len(worker_msgs)}')
for m in worker_msgs:
    s = m.get('sender', '?')
    t = m.get('type', '?')
    c = str(m.get('content', ''))[:150]
    print(f'  [{s}] type={t}: {c}')
