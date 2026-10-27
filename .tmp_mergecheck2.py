
import sys
sys.path.insert(0, "/root/Cylit")
from backend.services.knowledge import chunking
from backend.services.knowledge.knowledge_service import chunk_params
pid = "attention-cleanroom"
rows, tree = chunking.load(pid)
mr = [(r["id"], r.get("semantic_merge_into")) for r in rows if r.get("semantic_merge_into")]
print("并发行:", mr)
target, floor, hard = chunk_params()
built = chunking.build(rows, tree, target=target, hard=hard, floor=floor, use_breaks=True, road="a")
print("chunks", len(built["chunks"]), "| merged_chunks:", built["stats"].get("merged_chunks"), "| ok:", built["report"]["ok"])
for c in built["chunks"]:
    ids = [r["id"] for r in c["rows"]]
    if "p002-00" in ids:
        print("  p002-00 所在块:", ids)
