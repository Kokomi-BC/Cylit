
import sys, json
sys.path.insert(0, "/root/Cylit")
from sqlalchemy import create_engine, text
from backend.core.config import get_settings
eng = create_engine(get_settings().database_url)
pid = "attention-cleanroom"
with eng.connect() as c:
    rows = c.execute(text("SELECT id, created_at, citations_json FROM conversation_message WHERE citations_json LIKE '%pids%' ORDER BY created_at DESC LIMIT 3")).fetchall()
    print("messages with pids:", len(rows))
    for mid, at, cj in rows[:1]:
        cites = json.loads(cj or "[]")
        hit = [x for x in cites if x.get("pids")][:3]
        for h in hit:
            print(" ", h.get("ref"), h.get("kind"), "rows:", len(h.get("pids") or []), (h.get("pids") or [])[:4])
