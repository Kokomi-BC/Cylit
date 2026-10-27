
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

pid = "attention-cleanroom"
for path in ("a", "b"):
    ctx = CliCtx(task_id="manual", task_type="TRANSLATE_MD", queue="md",
                 paper_id=pid, session=None,
                 params={"mode": "retranslate", "pages": [1, 2], "brief": True})
    t0 = time.time()
    out = run_path_task(ctx, path)
    print("PATH", path, "took", round(time.time() - t0, 1), "s")
    print("   result:", str(out)[:300])
