
import sys
sys.path.insert(0, "/root/Cylit")
from backend.services.paper.reader_service import load_path_stores, merged_page
pid = "attention-cleanroom"
a, b = load_path_stores(pid)
pa, pb = a.all_pages(), b.all_pages()
am1 = str(pa.get(1, {}).get("original_md") or "")
am2 = str(pa.get(2, {}).get("original_md") or "")
bm1 = str(pb.get(1, {}).get("translation_md") or "")
bm2 = str(pb.get(2, {}).get("translation_md") or "")
print("A p1 arXiv:", "arXiv" in am1, "| rule lines:", [l for l in am1.split("\n") if l.strip() in ("---",)])
print("B p1 arXiv:", "arXiv" in bm1, "| rule lines:", [l for l in bm1.split("\n") if l.strip() in ("---",)])
print("A p2 head:", repr(am2[:110]))
print("B p2 head:", repr(bm2[:90]))
print("B p1 carry_md:", repr(pb.get(1, {}).get("carry_md"))[:120])
i = bm1.find("循环神经网络")
print("B p1 intro:", bm1[i:i+140].replace("\n", " / "))
m1 = merged_page(pid, 1) or {}
rows = m1.get("rows") or []
print("merged p1 rows:", len(rows))
for r in rows[6:11]:
    print("  ", r.get("id"), r.get("type"), "|", str(r.get("original"))[:32].replace("\n"," "), "||", str(r.get("translation"))[:26].replace("\n"," "))
