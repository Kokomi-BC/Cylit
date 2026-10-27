
import sys, time
sys.path.insert(0, "/root/Cylit")
from backend.services.translation.translation_service import run_path_task
from backend.services.task.task_service import TaskContext

class CliCtx(TaskContext):
    def _flush(self, **kw) -> None:
        pass
    def progress(self, done, total, message=None, force=False, **kw) -> None:
        if force:
            print(f"    {done}/{total} {message or ''}", flush=True)

ctx = CliCtx(task_id="manual", task_type="TRANSLATE_MD", queue="md",
             paper_id="attention-cleanroom", session=None,
             params={"mode": "retranslate", "pages": [2], "brief": True})
t0 = time.time()
out = run_path_task(ctx, "b")
print("B page2 took", round(time.time() - t0, 1), "s")
print(str(out)[:260])
from backend.services.paper.reader_service import load_path_stores
pb = load_path_stores("attention-cleanroom")[1].all_pages()
bl = (pb.get(2, {}) or {}).get("blocks") or []
print("p2 blocks:", len(bl), "| first:", repr(str(bl[0])[:90]) if bl else None)
