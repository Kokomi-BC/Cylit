"""真实模型小样本试译（用于人工评估页级翻译的质量与成本）。

只翻**指定的一页**（默认第 2 页），打印该页的原文转录、译文与逐块对齐结果，
以及 token 成本与对齐质量。

用法：
    python scripts/try_translate_page.py --paper-id turboquant-iclr26 --page 2
    python scripts/try_translate_page.py --page 2 --no-brief      # 更省一次调用

页级调用把**页图 + 解析器抽出的页内文本**一起交给模型，模型一次返回
``original_md``（忠实转录）与 ``translation_md``（逐块对应的中文译文），
脚本按行打印对照，便于人工核对公式是否被改写、段落是否一一对应。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.paper import paper_service  # noqa: E402
from backend.services.task.task_service import TaskContext  # noqa: E402
from backend.services.translation import translation_service  # noqa: E402


class CliCtx(TaskContext):
    def _flush(self, **kw) -> None:
        pass

    def progress(self, done, total, message=None, force=False, **kw) -> None:
        if force:
            print(f"    {done}/{total} {message or ''}", flush=True)

    def set_cursor(self, **kw) -> None:
        self.cursor.update(kw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper-id", default="turboquant-iclr26")
    ap.add_argument("--page", type=int, default=2)
    ap.add_argument("--no-brief", action="store_true")
    args = ap.parse_args()

    paper_service.get_paper(args.paper_id)
    ctx = CliCtx(task_id="try", task_type="TRANSLATE_MD", queue="md",
                 paper_id=args.paper_id, session=None,
                 params={"mode": "retranslate", "pages": [args.page],
                         "brief": not args.no_brief})
    ctx._cursor = {}
    t0 = time.time()
    result = translation_service.run_translate_task(ctx)
    wall = round(time.time() - t0, 1)

    usage = result.get("usage") or {}
    print(f"\n耗时 {wall}s | 页 {result.get('done')}/{result.get('pages')} "
          f"| 失败 {result.get('failed')} | 行 {result.get('rows')} "
          f"| 结构对齐页 {result.get('align_structure_pages')}")
    print(f"token: prompt={usage.get('prompt_tokens')} "
          f"completion={usage.get('completion_tokens')}"
          f"（推理 {usage.get('reasoning_tokens')}）"
          f" | max_tokens={result.get('max_tokens_final')}"
          f"（升档 {result.get('max_tokens_escalations')} 次）")
    brief = result.get("brief") or {}
    print(f"简报领域：{brief.get('domain')} | 统一术语 {len(brief.get('glossary') or {})} 条")

    payload = translation_service.page_store(args.paper_id).page(args.page) or {}
    print(f"\n对齐质量：{payload.get('align')}（模型 {payload.get('model')}）")
    print("=" * 78)
    for row in payload.get("rows") or []:
        en = (row.get("original") or "").replace("\n", " ").strip()
        zh = (row.get("translation") or "").replace("\n", " ").strip()
        print(f"\n[{row['id']}] {row['type']}"
              + (f" L{row['level']}" if row.get("level") else "")
              + (f" {row['number']}" if row.get("number") else ""))
        print(f"  EN: {en[:240]}")
        print(f"  ZH: {zh[:260] if zh else '（无译文）'}")
    print("\n" + "=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
