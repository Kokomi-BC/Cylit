"""page 级 golden set 的**生成**侧：把每条查询「应当命中的页」算出来写进 JSON。

为什么真值用「字面出现」而不是人肉标注相关性：
- 它**可复算**：同一份 PDF、同一套分词，谁跑都得到同一张表；人肉标注会随人漂。
- 它答的是一个具体问题：「这个查询该看到的页，检索给了没有」。相关性排序是另一件事，
  由 `golden_retrieval.py` 的 recall@k 去衡量，不要在这一层混。

**期望页 = 「提到它最多的前 N 页」**（N 默认与 k 对齐）：
turboquant 这种词全篇 14 页都有，若把 14 页都当期望，recall@10 的上限只有 10/14 ——
那不是检索差，是标尺选错了。

用法（在仓库根目录，主后端要跑着）：

    python scripts/golden_build.py --paper turboquant-online-vector-iclr-26 --write
    python scripts/golden_build.py --base https://fakenewsdetect.art/cylit-api/api/v1

不给 `--write` 只打印，不落盘。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "tests" / "golden" / "retrieval_pages.json"


def _token(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _get(base: str, token: str, path: str) -> dict:
    req = urllib.request.Request(base.rstrip("/") + path,
                                 headers={"X-Internal-Token": token})
    with urllib.request.urlopen(req, timeout=180) as resp:
        body = json.load(resp)
    return body.get("data") or {}


def page_texts(base: str, token: str, paper_id: str) -> dict[int, str]:
    """页号 → 该页所有块的原文 + 译文（拼在一起，便于按字面判定）。

    **一页一页取**，不用 `pages=all`：那个接口一次最多 20 页（`PAGES_MAX`），22 页的论文
    会被整条请求拒掉（实测 404），而这里要的是「全篇都扫一遍」。
    """
    first = _get(base, token, "/agent/papers/" + paper_id + "/pages?pages=1&lang=both")
    total = int(first.get("page_total") or 0)
    pages_data = list(first.get("pages") or [])
    missing: list[int] = []
    for no in range(2, total + 1):
        try:
            more = _get(base, token,
                        "/agent/papers/%s/pages?pages=%d&lang=both" % (paper_id, no))
        except Exception:            # noqa: BLE001 —— 缺页当作「这页没有产物」，不整条失败
            missing.append(no)
            continue
        pages_data.extend(more.get("pages") or [])
    if missing:
        print("这些页没有产物（跳过）：%s" % missing, file=sys.stderr)
    out: dict[int, str] = {}
    for page in pages_data:
        no = int(page.get("page") or 0)
        chunks: list[str] = []
        for block in page.get("blocks") or []:
            chunks.append(str(block.get("text") or block.get("original") or ""))
            chunks.append(str(block.get("translation") or ""))
        out[no] = "\n".join(chunks).lower()
    return out


def _score(text: str, needle: dict) -> int:
    """这一页命中多少次（0 = 不算期望页）。

    `substring`（中文 / 缩写这类按字面找）与 `all_tokens`（英文词全都要出现）两种写法，
    都返回**出现次数**，供上层排序取前 N 页。
    """
    if "substring" in needle:
        return text.count(str(needle["substring"]).lower())
    tokens = [str(t).lower() for t in needle.get("all_tokens") or [] if str(t).strip()]
    if not tokens or not all(t in text for t in tokens):
        return 0
    return sum(text.count(t) for t in tokens)


def build(case_file: Path, base: str, token: str, *, write: bool) -> int:
    spec = json.loads(case_file.read_text(encoding="utf-8"))
    paper_id = spec["paper_id"]
    texts = page_texts(base, token, paper_id)
    cap = int(spec.get("max_pages") or spec.get("k") or 10)
    print("页数：%d（%s），期望页上限 %d" % (len(texts), paper_id, cap))
    for case in spec["cases"]:
        scored = [(no, _score(text, case["needle"])) for no, text in texts.items()]
        scored = [(no, sc) for no, sc in scored if sc > 0]
        scored.sort(key=lambda item: (-item[1], item[0]))
        pages = sorted(no for no, _ in scored[:cap])
        case["expect_pages"] = pages
        print("  %-26s → %s（命中 %d 页，取前 %d）"
              % (case["query"], pages, len(scored), cap))
    if write:
        spec["built_with"] = "scripts/golden_build.py（字面出现 + 按次数取前 N 页，不是相关性标注）"
        case_file.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        print("已写回 %s" % case_file)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="算 page 级 golden set 的期望页")
    ap.add_argument("--paper", help="论文 id（必须与 JSON 里那篇一致）")
    ap.add_argument("--base", default="http://127.0.0.1:8710/api/v1",
                    help="主后端地址，**含 API 前缀**（/api/v1）")
    ap.add_argument("--token-file", default=str(ROOT / "data" / "secrets" / "agent.token"))
    ap.add_argument("--cases", default=str(CASES))
    ap.add_argument("--write", action="store_true", help="把 expect_pages 写回 JSON")
    args = ap.parse_args(argv)
    case_file = Path(args.cases)
    if args.paper:
        spec = json.loads(case_file.read_text(encoding="utf-8"))
        if spec.get("paper_id") != args.paper:
            print("JSON 里是 %s，与 --paper %s 不一致" % (spec.get("paper_id"), args.paper),
                  file=sys.stderr)
            return 2
    return build(case_file, args.base, _token(Path(args.token_file)), write=args.write)


if __name__ == "__main__":
    raise SystemExit(main())
