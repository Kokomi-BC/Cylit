
import sys
sys.path.insert(0, "/root/Cylit")
from backend.services.paper.reader_service import load_path_stores, merged_page
from backend.services.translation.page_translate import split_blocks
pid = "attention-cleanroom"
a, b = load_path_stores(pid)
am = str(a.all_pages().get(3, {}).get("original_md") or "")
bm = str(b.all_pages().get(3, {}).get("translation_md") or "")
print("A blocks", len(split_blocks(am)), "B blocks", len(split_blocks(bm)))
rows = (merged_page(pid, 3) or {}).get("rows") or []
for r in rows[:5]:
    print("row", r.get("id"), "|", str(r.get("original"))[:44].replace(chr(10)," "), "||", str(r.get("translation"))[:30].replace(chr(10)," "))
