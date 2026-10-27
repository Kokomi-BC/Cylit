import os, sys, json, re
sys.path.insert(0, os.getcwd())
from backend.core.config import settings
import pymysql

PAPER = "turboquant-online-vector-iclr-26"
c = pymysql.connect(host=settings.db_host, port=int(settings.db_port), user=settings.db_user,
                    password=settings.db_password, database=settings.db_name, charset='utf8mb4')
cur = c.cursor()
cur.execute("select id, title, updated_at from conversation where paper_id=%s order by updated_at desc limit 5", (PAPER,))
convs = cur.fetchall()
for r in convs:
    print("CONV", r)
cid = convs[0][0]
cur.execute("select id, role, created_at, length(content), citations_json from conversation_message "
            "where conversation_id=%s order by seq desc limit 4", (cid,))
rows = cur.fetchall()
for mid, role, at, ln, citj in rows:
    n = len(json.loads(citj or '[]'))
    print("MSG", role, at, "chars", ln, "citations", n, mid)
