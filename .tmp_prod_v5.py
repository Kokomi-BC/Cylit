
import sys
sys.path.insert(0, "/root/Cylit")
from backend.services.paper.reader_service import merged_page, load_path_stores
pid = "attention-cleanroom"
m2 = merged_page(pid, 2) or {}
rows = m2.get("rows") or []
print("p2 rows:", len(rows))
for r in rows[:2]:
    print("==", r.get("id"))
    print("  EN:", str(r.get("original"))[:150].replace("\n"," / "))
    print("  ZH:", str(r.get("translation"))[:150].replace("\n"," / "))
m1 = merged_page(pid, 1) or {}
r1 = [r for r in (m1.get("rows") or []) if str(r.get("original") or "").startswith("Recurrent neural")]
print("\np1 intro row EN tail:", str(r1[0].get("original"))[-60:] if r1 else None)
print("p1 intro row ZH:", str(r1[0].get("translation")) if r1 else None)
print("\np1 rows:", len(m1.get("rows") or []))
rules = [r.get("id") for r in (m1.get("rows") or []) if str(r.get("original")).strip() == "---"]
print("p1 --- row:", rules)
