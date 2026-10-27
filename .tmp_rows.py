
import sys, json
sys.path.insert(0, "/root/Cylit")
from backend.services.paper import reader_service
pid = "attention-cleanroom"
pages = reader_service.page_translations(pid)
p2 = next(p for p in pages if int(p.get("page") or 0) == 2)
rows = p2.get("rows") or []
print("p2 rows:", len(rows))
print("row0 keys:", sorted(rows[0].keys()))
print("row0 merge:", rows[0].get("semantic_merge_into"), "| break:", rows[0].get("semantic_break"))
print("breaks_a:", json.dumps(p2.get("breaks_a") or [], ensure_ascii=False)[:400])
