"""只重跑**某一路**的页级翻译（不碰另一路的产物）。

为什么需要它：`cli/pr.py translate` 跑的是**融合**路（写 `md/pages.json`，两侧一起重来），
要单独补中文侧（B 路）就会把 A 路的转录与锚点一起覆盖掉。产线里这件事的正路是
`translation_service.start_translation(paper_id, paths=["b"], mode="retranslate")` + cylit-agent 执行；
这里复用**同一个** `run_path_task`，只把 ctx 换成本地 shim，于是可以脚本化、可看日志。

⚠️ 它**不建任务记录**（任务面板看不见这次运行），跑完记得补一次进度回写：

    python cli/pr.py refresh-progress <paper_id>

用法::

    python scripts/retranslate_path.py <paper_id> b                 # 只补 B 路（中文）
    python scripts/retranslate_path.py <paper_id> a --pages 3,4     # 只重跑 A 路指定页
    python scripts/retranslate_path.py <paper_id> b --no-force      # 只翻还没翻的页

`--mode retranslate` 是默认值：整路重来（`force=True`，忽略已有产物）。
B 路默认走**块模式**（A 定块、B 照译），要求 A 路领先；A 路缺页时那一页会等，等不到才失败。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(ROOT))


class CliCtx:
    """`run_path_task` 只用到 paper_id / params / cursor / task_id / progress / set_cursor / check_control。"""

    def __init__(self, paper_id: str, params: dict) -> None:
        self.paper_id = paper_id
        self.params = params
        self.cursor: dict = {}
        self.task_id = "cli"
        self.done_items = 0
        self.total_items = 0
        self._t0 = time.time()

    def progress(self, done, total, message=None, force=False, **kw):
        print(f"  进度 {done}/{total} {message or ''}", flush=True)

    def set_cursor(self, **kw):
        self.cursor.update(kw)

    def check_control(self):
        return None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="只重跑某一路的页级翻译")
    ap.add_argument("paper_id")
    ap.add_argument("path", choices=["a", "b"])
    ap.add_argument("--pages", default="", help="只翻指定页，逗号分隔（如 3,4,7）")
    ap.add_argument("--mode", default="retranslate", choices=["resume", "retranslate"])
    ap.add_argument("--no-force", action="store_true", help="不强制：只翻还没翻的页")
    ap.add_argument("--no-brief", action="store_true", help="不生成文档简报（少一次调用）")
    args = ap.parse_args(argv)

    from backend.services.paper import paper_service
    from backend.services.translation import translation_service

    paper_service.get_paper(args.paper_id)
    params = {"mode": "resume" if args.no_force else args.mode,
              "force": not args.no_force,
              "brief": not args.no_brief}
    if args.pages:
        params["pages"] = [int(p) for p in args.pages.split(",") if p.strip()]
    print(f"== {args.paper_id} 路 {args.path}：{params}", flush=True)
    result = translation_service.run_path_task(CliCtx(args.paper_id, params), path=args.path)
    print(f"== 完成：{result}", flush=True)
    print("提醒：进度回写请跑 python cli/pr.py refresh-progress " + args.paper_id, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
