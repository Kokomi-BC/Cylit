import json, urllib.request, re, sys
BASE = 'http://127.0.0.1:8710/api/v1'
papers = json.load(urllib.request.urlopen(BASE + '/papers?limit=5'))['data']['items']
pid = None
for p in papers:
    hs = json.load(urllib.request.urlopen(BASE + f'/highlights?paper_id={p["id"]}'))['data']['items']
    if hs:
        pid = p['id']; break
if not pid:
    print('no paper with highlights'); sys.exit()
d = json.load(urllib.request.urlopen(BASE + f'/papers/{pid}/reader'))['data']
rows = {}
for page in (d.get('page_translations') or []):
    for r in (page.get('rows') or []):
        rows[r.get('id')] = r
print('paper', pid, 'rows', len(rows), 'highlights', len(d.get('highlights') or []))
def render(md):
    t = md or ''
    t = re.sub(r'\$\$[^$]+\$\$', '\ufffc', t)
    t = re.sub(r'\$[^$]+\$', '\ufffc', t)
    t = re.sub(r'(\*\*|__|\*|_|~~|`)', '', t)
    t = re.sub(r'^\s{0,3}([-*+]\s+|\d+[.)]\s+)', '', t, flags=re.M)
    return t
for h in (d.get('highlights') or []):
    r = rows.get(h['block_id']) or {}
    side = h.get('side') or 'en'
    raw = (r.get('original') if side == 'en' else r.get('translation')) or ''
    dom = render(raw)
    s, e = int(h['start']), int(h['end'])
    want = h.get('text') or ''
    print('---', h['id'][:12], h['block_id'], side, h.get('scope'), 'grp' if h.get('group_id') else '-', 'off', s, e, 'rowlen', len(dom))
    print('   slice:', repr(dom[s:e][:60]))
    print('   want :', repr(want[:60]))
    print('   slice==want', dom[s:e] == want, '| want in dom', want[:20] in dom)