
import sys
sys.path.insert(0, ".")
from backend.services.paper.reader_service import load_path_stores, merged_page
pid = "attention-is-all-you-need"
b = load_path_stores(pid)[1]
bm = str(b.all_pages().get(1, {}).get("translation_md") or "")
print("B head:", repr(bm[:260]))
m1 = merged_page(pid, 1) or {}
for r in (m1.get("rows") or [])[:3]:
    print("row", r.get("id"), "|", str(r.get("original"))[:60].replace("\n"," / "), "||", str(r.get("translation"))[:60].replace("\n"," / "))
