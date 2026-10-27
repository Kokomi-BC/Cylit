
import sys, time
sys.path.insert(0, "/root/Cylit")
from backend.services.translation.translation_service import run_path_task
from backend.services.task.task_service import TaskContext

class CliCtx(TaskContext):
    def _flush(self, **kw) -> None: pass
    def progress(self, done, total, message=None, force=False, **kw) -> None: pass

for path in ("a", "b"):
    ctx = CliCtx(task_id="manual", task_type="TRANSLATE_MD", queue="md",
                 paper_id="attention-cleanroom", session=None,
                 params={"mode": "retranslate", "pages": [1], "brief": True})
    t0 = time.time()
    out = run_path_task(ctx, path)
    print(path, "took", round(time.time()-t0,1), "s |", str(out.get("message"))[:60])

from backend.services.paper.reader_service import load_path_stores, merged_page
a, b = load_path_stores("attention-cleanroom")
am = str(a.all_pages().get(1, {}).get("original_md") or "")
bm = str(b.all_pages().get(1, {}).get("translation_md") or "")
print("A head:", repr(am[:200]))
print("A rule:", [l for l in am.split("\n") if l.strip() == "---"])
print("B head:", repr(bm[:160]))
m1 = merged_page("attention-cleanroom", 1) or {}
print("rows:", len(m1.get("rows") or []))
