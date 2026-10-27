
import sys
sys.path.insert(0, ".")
from backend.services.paper.reader_service import load_path_stores, merged_page
pid = "attention-is-all-you-need"
a, b = load_path_stores(pid)
am = str(a.all_pages().get(1, {}).get("original_md") or "")
bm = str(b.all_pages().get(1, {}).get("translation_md") or "")
print("A AUTHORS:", repr(am[:300]))
print("B head:", repr(bm[:220]))
m1 = merged_page(pid, 1) or {}
print("rows", len(m1.get("rows") or []))
