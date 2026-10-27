
import sys
sys.path.insert(0, "/root/Cylit")
from backend.services.paper.reader_service import load_path_stores, merged_page
from backend.services.translation.page_translate import split_blocks
pid = "attention-cleanroom"
b = load_path_stores(pid)[1]
bm = str(b.all_pages().get(1, {}).get("translation_md") or "")
print("B p1 blocks:", len(split_blocks(bm)))
m1 = merged_page(pid, 1) or {}
print("rows", len(m1.get("rows") or []))
for r in (m1.get("rows") or []):
    print("row", r.get("id"), "|", str(r.get("original"))[:40].replace(chr(10)," "), "||", str(r.get("translation"))[:30].replace(chr(10)," "))
