import os, sys, json, re
sys.path.insert(0, os.getcwd())
from backend.core.config import settings
import pymysql
from backend.agent.cite_groups import plan_cite_groups, row_pair, pid_set

MID = "ed876cd6-d90b-4b36-994f-b10e2fef80d6"
c = pymysql.connect(host=settings.db_host, port=int(settings.db_port), user=settings.db_user,
                    password=settings.db_password, database=settings.db_name, charset='utf8mb4')
cur = c.cursor()
cur.execute("select content, citations_json, created_at from conversation_message where id=%s", (MID,))
content, citj, at = cur.fetchone()
cites = json.loads(citj or '[]')
print("created", at, "chars", len(content), "citations", len(cites))

TOK = re.compile(r"\[\d{1,3}(?:\s*[,，、]\s*\d{1,3}|\s*[–—~-]\s*\d{1,3})*\]")
marks = TOK.findall(content)
print("labels", len(marks))
print("first 30:", marks[:30])
print("groups in payload (members):", len([x for x in cites if x.get("members")]))
by = {x["ref"]: x for x in cites}

# 相邻机会：连续两个标号（紧挨着写的 run）目标是否相邻
runs, cur_run, gaps = [], [], []
prev = None
for m in TOK.finditer(content):
    if prev is not None:
        gap = content[prev.end():m.start()]
        if re.fullmatch(r"[\s]*", gap):
            cur_run.append(m)
        else:
            runs.append(cur_run); cur_run = [m]
    else:
        cur_run = [m]
    prev = m
if cur_run: runs.append(cur_run)
multi = [r for r in runs if len(r) > 1]
print("runs with >1 label:", len(multi), "max in a run:", max((len(r) for r in runs), default=0))

def nums(tok):
    out = []
    for seg in tok[1:-1].split(","):
        s = seg.strip()
        m = re.fullmatch(r"(\d{1,3})\s*[–—~-]\s*(\d{1,3})", s)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            out += list(range(min(lo,hi), max(lo,hi)+1))
        elif s.isdigit():
            out.append(int(s))
    return out

merged_now = 0
print("\n-- run 内相邻机会（按现在后端规则判） --")
for r in multi[:12]:
    seq = []
    for m in r:
        for n in nums(m.group(0)):
            t = by.get(n)
            seq.append((n, (t or {}).get("page"), (t or {}).get("row_id"), len((t or {}).get("pids") or [])))
    print(" run:", [x[0] for x in seq], [(x[1], x[2]) for x in seq])

plan = plan_cite_groups(content, cites)
new = plan.render(content)
print("\nnew labels", len(TOK.findall(new)), "first 30:", TOK.findall(new)[:30])
print("groups after replan:", len([x for x in plan.payload() if x.get("members")]))
