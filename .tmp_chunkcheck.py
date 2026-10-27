
import sys
sys.path.insert(0, "/root/Cylit")
from backend.services.knowledge import chunking
from backend.services.knowledge.knowledge_service import chunk_params
pid = "attention-cleanroom"
rows, tree = chunking.load(pid)
foot = [r["id"] for r in rows if r.get("skip_reason") == "footnote"]
print("总行", len(rows), "| 被标脚注的行:", foot)
target, floor, hard = chunk_params()
built = chunking.build(rows, tree, target=target, hard=hard, floor=floor, use_breaks=True, road="a")
ids = {r["id"] for c in built["chunks"] for r in c["rows"]}
print("chunks", len(built["chunks"]), "| 落入块里的脚注行:", [f for f in foot if f in ids])
print("report ok:", built["report"]["ok"], "| merged_chunks:", built["stats"].get("merged_chunks"))
