
import sys
sys.path.insert(0, "/root/Cylit")
from backend.services.paper.reader_service import merged_page
pid = "attention-cleanroom"
m1 = merged_page(pid, 1) or {}
for r in (m1.get("rows") or []):
    o = str(r.get("original") or "")
    if o.startswith("Recurrent neural networks"):
        print("row", r.get("id"))
        print("EN:", o)
        print("ZH:", r.get("translation"))
m2 = merged_page(pid, 2) or {}
rows2 = m2.get("rows") or []
print("p2 row0 ZH:", str(rows2[0].get("translation"))[:60] if rows2 else None)
print("p2 row0 EN:", str(rows2[0].get("original"))[:60] if rows2 else None)
