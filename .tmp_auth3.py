
import sys
sys.path.insert(0, ".")
from backend.services.paper.reader_service import load_path_stores, merged_page
pid = "attention-is-all-you-need"
a = load_path_stores(pid)[0]
am = str(a.all_pages().get(1, {}).get("original_md") or "")
print("AUTHORS:", repr(am[:520]))
m1 = merged_page(pid, 1) or {}
rows = m1.get("rows") or []
print("rows", len(rows))
for r in rows[:4]:
    print("  ", r.get("id"), "|", str(r.get("original"))[:70].replace("\n"," / "))
