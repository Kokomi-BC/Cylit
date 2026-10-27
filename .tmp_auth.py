
import sys
sys.path.insert(0, ".")
from backend.services.paper.reader_service import load_path_stores
pid = "attention-is-all-you-need"
a, b = load_path_stores(pid)
am = str(a.all_pages().get(1, {}).get("original_md") or "")
i = am.find("Vaswani")
print("A p1 authors:", repr(am[i:i+420]))
print("rule lines:", [l for l in am.split("\n") if l.strip() == "---"])
