import os, sys
sys.path.insert(0, 'D:\\Prospects\\ScreenMemory')
os.chdir('D:\\Prospects\\ScreenMemory')
from tools.uia_engine import get_engine
e = get_engine()
for name, hwnd in [('Alpha', 264680), ('Beta', 723422), ('Gamma', 1116666), ('Delta', 395780)]:
    s = e.scan(hwnd)
    print(f'{name}: {s.state}')
