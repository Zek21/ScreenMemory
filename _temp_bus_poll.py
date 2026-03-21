import os, requests, glob

os.chdir('D:\\Prospects\\ScreenMemory')

# 1. Poll bus for recent results
msgs = requests.get('http://localhost:8420/bus/messages?limit=30').json()
for m in msgs:
    s = m.get('sender','?')
    t = m.get('type','?')
    c = m.get('content','')[:120]
    print(f'[{s}] {t}: {c}')

print('\n--- Review files ---')
for f in sorted(glob.glob('data/research_review/*')):
    sz = os.path.getsize(f)
    print(f'  {os.path.basename(f)} ({sz} bytes)')
