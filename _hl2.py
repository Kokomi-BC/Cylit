import json, urllib.request
pid = 'turboquant-online-vector-iclr-26'
d = json.load(urllib.request.urlopen(f'http://127.0.0.1:8710/api/v1/papers/{pid}/reader', timeout=30))['data']
br = d.get('block_rows')
print('block_rows type:', type(br).__name__)
sample = br if not isinstance(br, dict) else list(br.values())[0]
print('sample type:', type(sample).__name__, 'keys/len:', (list(sample.keys())[:6] if isinstance(sample, dict) else len(sample)))
rows = {}
if isinstance(br, dict):
    for v in br.values():
        seq = v if isinstance(v, list) else (v.get('rows') or [])
        for r in seq:
            rows[r.get('id')] = r
for h in (d.get('highlights') or []):
    r = rows.get(h['block_id'])
    if not r:
        print(h['id'], 'NO ROW for', h['block_id']); continue
    raw = (r.get('original') if (h.get('side') or 'en') == 'en' else r.get('translation')) or ''
    want = h.get('text') or ''
    seg = raw[int(h['start']):int(h['end'])]
    print(h['id'], 'block', h['block_id'], '| slice==text:', seg == want, '| text in raw:', want[:24] in raw)
    if seg != want:
        print('   slice:', repr(seg[:60]))
        print('   want :', repr(want[:60]))