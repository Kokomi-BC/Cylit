
import sys
sys.path.insert(0, "/root/Cylit")
from backend.services.paper.reader_service import load_path_stores
from backend.services.translation.page_translate import split_blocks
pid = "attention-cleanroom"
a, b = load_path_stores(pid)
am = str(a.all_pages().get(7, {}).get("original_md") or "")
bm = str(b.all_pages().get(7, {}).get("translation_md") or "")
ab, bb = split_blocks(am), split_blocks(bm)
print("A blocks", len(ab), "B blocks", len(bb))
for i in range(max(len(ab), len(bb))):
    at = str(ab[i].get("text"))[:46].replace(chr(10), " ") if i < len(ab) else "-"
    bt = str(bb[i].get("text"))[:40].replace(chr(10), " ") if i < len(bb) else "-"
    print(f"{i:2d} EN {at!r} | ZH {bt!r}")
