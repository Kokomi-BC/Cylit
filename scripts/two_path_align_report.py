r"""两路对齐报告：拆成 A/B 两路之后，**行对齐还准不准**。

## 为什么需要这个脚本

页级翻译原本是**同一次调用**里先转录原文、再给块对块对应的译文（旧 `page_translate.SYSTEM_PROMPT`）。
所以两侧块数天然相等，``pair_rows`` 永远走 ``len(left) == len(right)`` 那条分支 ——
实测两篇论文 ``align`` 是 **exact 22/22 + 15/15**。

两路拆开之后（``TRANSCRIBE_SYSTEM`` / ``TRANSLATE_SYSTEM``）这个构造性保证没有了：
两侧只共享一条"按页图分段"的指令，块数可能不等，``pair_rows`` 会退回 ``_sequence_pairs``
结构对齐，并把多出来的一侧保留成**单侧行**（另一侧为空 → 阅读器显示「未翻译」）。
这本身是设计内的降级，但必须**量出来**，因为它正是用户报过的
「每行配的是邻行译文 / 最后一行空成未翻译」那类缺陷的来源。

## 判据（三条，前两条是门限，第三条是**诊断**）

1. **单侧行占比**：单侧行多的页说明两侧分段差异大；
2. **「段落中间的单侧行」**：``type == "para"`` 的单侧行，前后紧邻也都是有内容的正文行。
   页首/页尾的单侧行是**良性**的（两侧各自多出开头/结尾一段很正常，不影响后面任何行）；
   只有落在连续正文段中间的那种才会把后续行**整体错位**。这一条才是真正的门限。
3. **内容对应（错位诊断）**：两侧都必须保留的"标记"（公式片段 / 引用 `Author (2023)` /
   数字）在**成对行**里应当对得上。如果"错开 k 行再配"比"按原顺序配"对上的更多，
   就说明这一页**整体错位了 k 行** —— 这是单侧行判据**看不到**的失败模式（实测 turbo p1：
   A 把摘要里相邻两句并成一块、B 拆成两块，于是从第 4 行起英中错位一行，
   而页面上只有 1 个页尾单侧行）。

门限（P4 验收）：单侧行 ≤ 全库行数的 2%、「段落中间的单侧行」为 0、**错位页为 0**。

## 两种数据形态

- ``legacy``：只有 ``md/pages.json``（合并产物），直接读它存下来的 ``align`` —— 这是**基线**；
- ``paths``：有 ``md/path_a.json`` + ``md/path_b.json``，**现算** ``pair_rows(original_md, translation_md)``
  的质量（这才是拆分后真实会发生的事）。

默认按文件存在性自动判断；两者都没有就报错退出。

用法::

    python scripts/two_path_align_report.py                      # 两篇论文，自动判断形态
    python scripts/two_path_align_report.py --mode legacy        # 强制读 pages.json
    python scripts/two_path_align_report.py --tag p4-gate        # 报告落盘 data/logs/snapshot/two-path-align-<tag>.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PAPERS = ["turboquant-online-vector-iclr-26", "attention-is-all-you-need"]
SNAPSHOT_DIR = ROOT / "data" / "logs" / "snapshot"

#: 单侧行占比门限（P4 验收）
ONE_SIDED_MAX_RATIO = 0.02


# --------------------------------------------------------------------------
# 读盘
# --------------------------------------------------------------------------


def paper_md_dir(paper_id: str) -> Path:
    return ROOT / "data" / "papers" / paper_id / "md"


def load_pages(path: Path) -> dict[int, dict]:
    """读 ``PageStore`` 格式的文件（``pages`` 键是页号的字符串形式）。"""
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    pages = data.get("pages") if isinstance(data.get("pages"), dict) else {}
    return {int(k): v for k, v in pages.items() if isinstance(v, dict)}


def detect_mode(paper_id: str) -> str:
    md = paper_md_dir(paper_id)
    if (md / "path_a.json").is_file() and (md / "path_b.json").is_file():
        return "paths"
    if (md / "pages.json").is_file():
        return "legacy"
    return "missing"


# --------------------------------------------------------------------------
# 指标
# --------------------------------------------------------------------------


def _has(row: dict, side: str) -> bool:
    return bool(str(row.get(side) or "").strip())


def one_sided_rows(rows: list[dict]) -> list[dict]:
    """恰好一侧有内容的行（另一侧空）—— 拆路后 ``pair_rows`` 的正常降级产物。"""
    return [r for r in rows
            if _has(r, "original") != _has(r, "translation")]


def mid_prose_one_sided(rows: list[dict]) -> list[dict]:
    """落在**连续正文段中间**的单侧行 —— 会让后续行整体错位的那种。

    判据（保守，宁可漏报也不误报）：本行 ``para``、前后紧邻都存在且都是 ``para``，
    并且本行**有内容的那一侧**是真文字（有侧别之分才有错位可言）。
    """
    out: list[dict] = []
    for i, row in enumerate(rows):
        if (row.get("type") or "para") != "para":
            continue
        if _has(row, "original") == _has(row, "translation"):
            continue                      # 两侧都有内容（或都空）→ 不是单侧行
        prev = rows[i - 1] if i > 0 else None
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        def is_para(r: dict | None) -> bool:
            return bool(r) and (r.get("type") or "para") == "para"
        if is_para(prev) and is_para(nxt):
            out.append(row)
    return out


def _row_of(page: int, index: int, row: dict, *, why: str) -> dict:
    text = (row.get("original") or row.get("translation") or "").strip()
    return {"page": page, "index": index, "row_id": row.get("id"), "why": why,
            "type": row.get("type"), "chars": len(text), "head": text[:70]}


# --------------------------------------------------------------------------
# 内容对应（错位诊断）：两侧都必须保留的"标记"对不对得上
# --------------------------------------------------------------------------

_MATH_RE = re.compile(r"\$([^$]+)\$", re.DOTALL)
#: `Shannon (1948)` / `Achiam et al. (2023)` / `Dubey et al. (2024)` 这类引用：
#: 两侧都会原样保留英文作者名与年份（提示词就是这么要求的），所以是**跨语言可靠的对位标记**。
_CITE_RE = re.compile(r"[A-Z][A-Za-z\-]+(?:\s+et\s+al\.?)?\s*\((\d{4}[a-z]?)\)")
#: 纯数字（≥2 位）：两侧数值必须一致
_NUM_RE = re.compile(r"\b\d[\d.,]{1,}\b")


def marker_tokens(text: str) -> set[str]:
    """这一块正文里的**跨语言标记**：公式片段 + 引用 + 数字。

    为什么用它们做对位判据：中英正文永远不可能字面相同，但这三样在两侧**必须一致**
    （提示词硬要求 LaTeX 逐字符保留、引用原样保留、数字不改）。于是"成对的行有没有共同标记"
    就成了一个**不需要翻译模型**的对位证据。
    """
    text = text or ""
    tokens: set[str] = set()
    for m in _MATH_RE.finditer(text):
        body = re.sub(r"\s+", "", m.group(1))
        if body:
            tokens.add(body[:60])
    for m in _CITE_RE.finditer(text):
        tokens.add(m.group(1))
    for m in _NUM_RE.finditer(text):
        tokens.add(m.group(0).rstrip(".,"))
    return tokens


def page_alignment(page: int, rows: list[dict], *, max_shift: int = 4) -> dict:
    """这一页**是不是整体错位了**：用"跨语言相同的标记"反推行偏移。

    做法：把两侧所有标记按行号记下来（同一标记取首次出现的行），对**两侧都出现**的标记算
    `delta = 该标记在 B 的行号 − 在 A 的行号`。整页若整体错位 k 行，则几乎所有共享标记的
    delta 都等于 k —— 取众数即可判。

    为什么不用"成对行有没有共同标记"那种更直观的口径：实测 turbo p1 的摘要里只有 1 个
    成对行同时带标记（`Shannon (1948)`），"没有证据"会被误读成"没问题" —— 而那一页
    从第 4 行起正好错位一行（实测 EN 第 5 行的 `Shannon (1948)` 出现在 ZH 第 6 行）。
    **证据少必须如实上报**（`evidence`），不能让沉默看起来像健康。

    返回 ``{"evidence": 共享标记数, "shift": 众数偏移, "agree": 该偏移的票数,
    "pairs": 两侧都有标记的行数, "identity_hits": 原序成对命中数, "shifted": bool}``。
    """
    tokens_a: dict[str, int] = {}
    tokens_b: dict[str, int] = {}
    for i, r in enumerate(rows):
        for t in marker_tokens(r.get("original")):
            tokens_a.setdefault(t, i)
        for t in marker_tokens(r.get("translation")):
            tokens_b.setdefault(t, i)
    shared = set(tokens_a) & set(tokens_b)
    deltas: dict[int, int] = {}
    for token in shared:
        delta = tokens_b[token] - tokens_a[token]
        deltas[delta] = deltas.get(delta, 0) + 1
    identity_hits = sum(1 for i in range(len(rows))
                        if marker_tokens(rows[i].get("original"))
                        & marker_tokens(rows[i].get("translation")))
    pairs = sum(1 for r in rows
                if marker_tokens(r.get("original")) and marker_tokens(r.get("translation")))
    shift, agree = (0, deltas.get(0, 0))
    for delta, count in sorted(deltas.items(), key=lambda kv: -kv[1]):
        if delta != 0 and abs(delta) <= max_shift and count > agree:
            shift, agree = delta, count
    # 判定门槛：非零偏移要**压倒性地**解释全部证据（≥2 票，或占共享标记的多数）
    decisive = shift != 0 and (agree >= 2 or (agree >= 1 and agree * 2 > len(shared)))
    return {"evidence": len(shared), "shift": shift, "agree": agree, "pairs": pairs,
            "identity_hits": identity_hits, "shifted": bool(decisive),
            "deltas": dict(sorted(deltas.items()))}


def analyse_paper(paper_id: str, mode: str) -> dict:
    from backend.services.translation.page_translate import pair_rows

    md = paper_md_dir(paper_id)
    if mode == "legacy":
        pages = load_pages(md / "pages.json")
        # 基线：``pages.json`` 已存了当时的 align 质量，直接读（不重算 —— 重算会掩盖历史差异）
        per_page = []
        total = one_sided = mid = 0
        align_counts: dict[str, int] = {}
        for page, payload in sorted(pages.items()):
            rows = list(payload.get("rows") or [])
            quality = str(payload.get("align") or "?")
            align_counts[quality] = align_counts.get(quality, 0) + 1
            os_ = one_sided_rows(rows)
            mp = mid_prose_one_sided(rows)
            total += len(rows)
            one_sided += len(os_)
            mid += len(mp)
            per_page.append({
                "page": page, "align": quality, "rows": len(rows),
                "one_sided": [_row_of(page, i, r, why="one_sided") for i, r in enumerate(os_)],
                "mid_prose": [_row_of(page, i, r, why="mid_prose") for i, r in enumerate(mp)],
            })
        return {
            "paper_id": paper_id, "mode": mode, "pages": len(pages),
            "rows": total, "align": align_counts,
            "one_sided": one_sided, "mid_prose": mid, "per_page": per_page,
        }

    # paths：两路各自落盘，**现算** pair_rows 的质量（这就是拆分后真实发生的事）
    a_pages = load_pages(md / "path_a.json")
    b_pages = load_pages(md / "path_b.json")
    page_numbers = sorted(set(a_pages) | set(b_pages))
    per_page = []
    total = one_sided = mid = shifted_pages = 0
    no_evidence_pages: list[int] = []
    align_counts = {}
    for page in page_numbers:
        original = str((a_pages.get(page) or {}).get("original_md") or "")
        translation = str((b_pages.get(page) or {}).get("translation_md") or "")
        rows, quality = pair_rows(original, translation, page)
        align_counts[quality] = align_counts.get(quality, 0) + 1
        os_ = one_sided_rows(rows)
        mp = mid_prose_one_sided(rows)
        total += len(rows)
        one_sided += len(os_)
        mid += len(mp)
        if not mp:
            # 只有"没有段落中间单侧行"的页才值得看内容对应：否则那页本来就已知有错位
            pass
        offset = page_alignment(page, rows)
        if offset["shifted"]:
            shifted_pages += 1
        if offset["evidence"] == 0 and len(rows) > 1:
            no_evidence_pages.append(page)
        item = {"page": page, "align": quality, "rows": len(rows),
                "one_sided": [_row_of(page, i, r, why="one_sided")
                              for i, r in enumerate(os_)],
                "mid_prose": [_row_of(page, i, r, why="mid_prose")
                              for i, r in enumerate(mp)],
                "offset": offset}
        if page not in a_pages or page not in b_pages:
            item["missing_side"] = ("a" if page not in a_pages else "b")
        per_page.append(item)
    return {
        "paper_id": paper_id, "mode": mode, "pages": len(page_numbers),
        "rows": total, "align": align_counts,
        "one_sided": one_sided, "mid_prose": mid,
        "shifted_pages": shifted_pages, "shifted_list": [
            {"page": p["page"], "shift": (p.get("offset") or {}).get("shift"),
             "agree": (p.get("offset") or {}).get("agree"),
             "evidence": (p.get("offset") or {}).get("evidence")}
            for p in per_page if (p.get("offset") or {}).get("shifted")],
        "no_evidence_pages": no_evidence_pages,
        "per_page": per_page,
    }


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper", action="append", default=None,
                    help="论文 id（可重复）；默认两篇演示论文")
    ap.add_argument("--mode", choices=("auto", "legacy", "paths"), default="auto")
    ap.add_argument("--tag", default="", help="报告文件名后缀：two-path-align-<tag>.json")
    args = ap.parse_args()

    papers = args.paper or PAPERS
    reports: list[dict] = []
    for pid in papers:
        mode = detect_mode(pid) if args.mode == "auto" else args.mode
        if mode == "missing":
            print(f"！{pid}：既没有 pages.json 也没有 path_a/path_b.json，跳过")
            continue
        reports.append(analyse_paper(pid, mode))

    print("=" * 96)
    print(f"两路对齐报告（门限：单侧行 ≤ {ONE_SIDED_MAX_RATIO:.0%}、段落中间单侧行 = 0、错位页 = 0）")
    grand_rows = grand_one_sided = grand_mid = grand_shifted = 0
    for rep in reports:
        grand_rows += rep["rows"]
        grand_one_sided += rep["one_sided"]
        grand_mid += rep["mid_prose"]
        grand_shifted += rep.get("shifted_pages", 0)
        ratio = rep["one_sided"] / max(1, rep["rows"])
        print("-" * 96)
        print(f"# {rep['paper_id']}  [{rep['mode']}]  页 {rep['pages']}  行 {rep['rows']}")
        print(f"  align 分布：{rep['align']}")
        print(f"  单侧行 {rep['one_sided']}（{ratio:.2%}）  段落中间单侧行 {rep['mid_prose']}"
              f"  **错位页 {rep.get('shifted_pages', 0)}**")
        if rep.get("no_evidence_pages"):
            print(f"  无标记可判（证据不足，不算通过也不算失败）："
                  f"{rep['no_evidence_pages']}")
        for item in rep.get("shifted_list") or []:
            print(f"    p{item['page']}: 整体错位 {item['shift']:+d} 行"
                  f"（{item['agree']}/{item['evidence']} 个共享标记支持）")
        for page in rep["per_page"]:
            off = page.get("offset") or {}
            if not page["one_sided"] and not page["mid_prose"] and not off.get("shifted"):
                continue
            flag = ""
            if page["mid_prose"]:
                flag += "  ← 段落中间错位"
            if off.get("shifted"):
                flag += f"  ← 整体错位 {off['shift']:+d} 行（{off['agree']}/{off['evidence']} 标记支持）"
            if page["one_sided"] or page["mid_prose"]:
                print(f"    p{page['page']:>3}  align={page['align']:<9} 行 {page['rows']:>3}"
                      f"  单侧 {len(page['one_sided'])}  中间 {len(page['mid_prose'])}"
                      f"  标记证据 {off.get('evidence', 0)}{flag}")
            elif off.get("shifted"):
                print(f"    p{page['page']:>3}  align={page['align']:<9} 行 {page['rows']:>3}"
                      f"  单侧 0  标记证据 {off['evidence']}"
                      f"（众数偏移 {off['shift']:+d}，{off['agree']} 票）{flag}")
            for item in page["mid_prose"]:
                print(f"        · [{item['row_id']}] {item['type']} {item['chars']}字：{item['head']}")

    ratio = grand_one_sided / max(1, grand_rows)
    ok = (ratio <= ONE_SIDED_MAX_RATIO) and grand_mid == 0 and grand_shifted == 0
    print("=" * 96)
    print(f"合计：行 {grand_rows}，单侧行 {grand_one_sided}（{ratio:.2%}），"
          f"段落中间单侧行 {grand_mid}，错位页 {grand_shifted}")
    print(f"结论：{'通过' if ok else '不通过 —— 需要 P4b 共享坐标契约'}")

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    name = f"two-path-align-{args.tag}.json" if args.tag else "two-path-align.json"
    out = SNAPSHOT_DIR / name
    out.write_text(json.dumps({
        "generated_by": "scripts/two_path_align_report.py",
        "threshold": {"one_sided_max_ratio": ONE_SIDED_MAX_RATIO, "mid_prose": 0,
                      "shifted_pages": 0},
        "totals": {"rows": grand_rows, "one_sided": grand_one_sided,
                   "one_sided_ratio": round(ratio, 4), "mid_prose": grand_mid,
                   "shifted_pages": grand_shifted, "passed": ok},
        "papers": reports,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"报告 → {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
