
import sys
sys.path.insert(0, "/root/Cylit")
from backend.services.paper.reader_service import load_path_stores
pid = "attention-cleanroom"
a = load_path_stores(pid)[0]
p = a.all_pages().get(2, {}) or {}
brk = p.get("breaks_a") or p.get("breaks") or []
print("p2 anchors:", len(brk))
for b in brk[:6]:
    print("  ", {k: str(v)[:40] for k, v in b.items()})
