
import sys, json
sys.path.insert(0, "/root/Cylit")
from backend.services.paper.reader_service import merged_page
pid = "attention-cleanroom"
m2 = merged_page(pid, 2) or {}
rows = m2.get("rows") or []
print("rows", len(rows))
for r in rows[:3]:
    print("==", r.get("id"), r.get("type"), "continues=", r.get("continues"))
    print("  EN:", str(r.get("original"))[:220].replace("\n"," / "))
    print("  ZH:", str(r.get("translation"))[:200].replace("\n"," / "))
print("align:", m2.get("align"))
