"""打印某篇论文的目录树（按 Markdown 标题结构），用于人工核对目录质量。

    python scripts/show_outline.py                       # 库内全部论文
    python scripts/show_outline.py turboquant-iclr26     # 指定论文

目录来自 `reader_service.build_outline`：与 Markdown 事实源同源（原生页=解析器标题判定，
扫描页=视觉模型返回的 `##/###`），并已剔除算法伪代码/表格数值这类"假标题"。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def show(nodes, depth: int = 0) -> None:
    for node in nodes:
        pad = "    " * depth
        num = node["number"] or "-"
        print(f"{pad}L{node['level']} [{num:>6}] p{node['page']:<3} {node['text'][:70]}")
        show(node["children"], depth + 1)


def main() -> int:
    from backend.services.paper import paper_service, reader_service

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

    wanted = sys.argv[1:]
    if not wanted:
        wanted = [p["id"] for p in paper_service.list_papers(page_size=50)["items"]]
    for pid in wanted:
        try:
            outline = reader_service.build_outline(pid)
        except Exception as exc:  # noqa: BLE001
            print(f"=== {pid}：读取失败（{exc}）")
            continue
        print(f"=== {pid}：{outline['count']} 条 ===")
        show(outline["tree"])
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
