
import sys
sys.path.insert(0, "/root/Cylit")
from backend.services.paper.reader_service import load_path_stores, merged_page
from backend.services.translation.page_translate import split_blocks
pid = "attention-cleanroom"
a, b = load_path_stores(pid)
am = str(a.all_pages().get(1, {}).get("original_md") or "")
print("A p1 blocks:", len(split_blocks(am)))
for blk in split_blocks(am):
    print("  ", repr(str(blk.get("text"))[:60]))
m1 = merged_page(pid, 1) or {}
print("rows", len(m1.get("rows") or []))
for r in (m1.get("rows") or []):
    print("row", r.get("id"), "|", str(r.get("original"))[:44].replace(chr(10)," "), "||", str(r.get("translation"))[:26].replace(chr(10)," "))
