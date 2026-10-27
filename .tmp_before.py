
import sys
sys.path.insert(0, "/root/Cylit")
from sqlalchemy import create_engine, text
from backend.core.config import get_settings
eng = create_engine(get_settings().database_url)
pid = "attention-cleanroom"
with eng.connect() as c:
    total = c.execute(text("SELECT COUNT(*) FROM chunk WHERE paper_id=:p"), {"p": pid}).scalar()
    foot = c.execute(text("SELECT COUNT(*) FROM chunk WHERE paper_id=:p AND content_preview LIKE :q"), {"p": pid, "q": "%Equal contribution%"}).scalar()
    print("chunks before:", total, "| 含脚注的块:", foot)
