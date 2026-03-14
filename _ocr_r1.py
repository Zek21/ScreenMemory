import mss, json
from rapidocr_onnxruntime import RapidOCR

workers = json.load(open(r'D:\Prospects\ScreenMemory\data\workers.json'))['workers']
sct = mss.mss()
ocr = RapidOCR()

for w in workers:
    name = w['name']
    monitor = {'left': w['x'], 'top': w['y'], 'width': w['w'], 'height': w['h']}
    img = sct.grab(monitor)
    path = f'D:/Prospects/ScreenMemory/screenshots/_r1_{name}.png'
    from PIL import Image
    Image.frombytes('RGB', (img.width, img.height), img.rgb).save(path)
    
    result, _ = ocr(path)
    print(f'=== {name.upper()} ===')
    if result:
        texts = [r[1] for r in result]
        combined = ' '.join(texts)
        # Check for key indicators
        indicators = []
        for kw in ['ROUND 1', 'ROUND1', 'UPGRADE', 'Bus Communication', 'Worker Self-Registration', 
                    'Dashboard', 'Cross-Validation', 'processing', 'reading', 'writing', 
                    'creating', 'Created', 'implement', 'COMPLETE', 'result', 'skynet_bus_persist',
                    'skynet_worker_register', 'skynet_crossval', 'god_console', 'executing',
                    'tool_call', 'create_file', 'replace_string', 'run_in_terminal']:
            if kw.lower() in combined.lower():
                indicators.append(kw)
        if indicators:
            print(f'  FOUND: {", ".join(indicators)}')
        print(f'  LAST 400 chars: ...{combined[-400:]}')
    else:
        print('  (no text detected)')
    print()
