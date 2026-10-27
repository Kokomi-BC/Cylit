
import sys
sys.path.insert(0, "/root/Cylit")
from backend.services.knowledge import chunking
from backend.services.knowledge.knowledge_service import chunk_params
pid = "attention-cleanroom"
rows, tree = chunking.load(pid)
merged_rows = [r["id"] for r in rows if r.get("semantic_merge_into")]
print("并发行:", merged_rows, "→", [r.get("semantic_merge_into") for r in rows if r.get("semantic_merge_into")])
target, floor, hard = chunk_params()
built = chunking.build(rows, tree, target=target, hard=hard, floor=floor, use_breaks=True, road="a")
print("chunks", len(built["chunks"]), "| merged_chunks:", built["stats"].get("merged_chunks"), "| report ok:", built["report"]["ok"])
for c in built["chunks"]:
    ids = [r["id"] for r in c["rows"]]
    if "p001-05" in ids or "p002-00" in ids:
        print("  chunk", c.get("key"), "rows:", ids[:6], "... len", len(ids))
