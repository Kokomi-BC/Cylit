
import sys
sys.path.insert(0, "/root/Cylit")
from backend.services.paper.reader_service import load_path_stores, merged_page
pid = "attention-cleanroom"
a, b = load_path_stores(pid)
pa, pb = a.all_pages(), b.all_pages()
am1 = str(pa.get(1, {}).get("original_md") or "")
print("A p1 tail:", repr(am1[-320:]))
bm1 = str(pb.get(1, {}).get("translation_md") or "")
print("B p1 tail:", repr(bm1[-200:]))
pm1 = pb.get(1, {})
print("B p1 carry_md:", repr(pm1.get("carry_md")))
print("B p1 blocks count:", len(pm1.get("blocks") or []))
bl = (pb.get(2, {}) or {}).get("blocks") or []
print("B p2 blocks count:", len(bl), "| first:", str(bl[0])[:80] if bl else None)
print("A p2 blocks(first 120):", str(pa.get(2, {}).get("original_md"))[:120])
m1 = merged_page(pid, 1) or {}
for r in (m1.get("rows") or [])[-3:]:
    print("p1 row", r.get("id"), "EN tail:", str(r.get("original"))[-90:].replace("\n"," / "))
