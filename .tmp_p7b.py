
import sys
sys.path.insert(0, "/root/Cylit")
from backend.services.paper.reader_service import load_path_stores
from backend.services.translation.page_translate import split_blocks
pid = "attention-cleanroom"
a, b = load_path_stores(pid)
print("p7 A", len(split_blocks(str(a.all_pages().get(7, {}).get("original_md") or ""))),
      "B", len(split_blocks(str(b.all_pages().get(7, {}).get("translation_md") or ""))))
