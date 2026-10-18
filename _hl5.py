import json, urllib.request
pid = 'turboquant-online-vector-iclr-26'
d = json.load(urllib.request.urlopen(f'http://127.0.0.1:8710/api/v1/papers/{pid}/reader', timeout=30))['data']
p0 = (d.get('pages') or [])[0]
b0 = (p0.get('blocks') or [])[0]
print('block keys:', list(b0.keys())[:10])
print('block rows len:', len(b0.get('rows') or []))
r0 = (b0.get('rows') or [{}])[0]
print('row keys:', list(r0.keys())[:12], '| id:', r0.get('id'))
rows = {}
for p in (d.get('pages') or []):
    for b in (p.get('blocks') or []):
        for r in (b.get('rows') or []):
            rows[r.get('id')] = r
print('rows total', len(rows))
for h in (d.get('highlights') or []):
    r = rows.get(h['block_id'])
    if not r:
        print(h['id'], 'NO ROW', h['block_id']); continue
    raw = (r.get('original') if (h.get('side') or 'en') == 'en' else r.get('translation')) or ''
    want = h.get('text') or ''
    seg = raw[int(h['start']):int(h['end'])]
    print(h['id'], h['block_id'], h.get('side'), h.get('scope'), h.get('color'), '| slice==text:', seg == want, '| in raw:', want[:20] in raw)