"""把正文短码的行身份（pid）补进解读的 refs_json.pids —— 阶段 5 的执行器。

为什么要它：阶段 3 起，解读引用"跳到原文哪一行"**首选** refs_json.pids（读路径见
understanding_service.split_refs_by_pids），chunk id 只是兜底 —— chunk id 每重建一次
索引就全变，pid 不会（生成一次、重建时按指纹继承，见 docs/身份模型.md）。
但阶段 2 之前生成的解读 refs_json 里根本没有 pids。

**不重建索引**怎么拿到 pid：pid 的唯一来源是页产物（reader_service._finalize_page 是
唯一写 row["pid"] 的地方），而 remap_understanding_refs.py 的 **capture** 阶段（重建
**不需要**发生）就能把"旧 chunk 的行 id 集合"经页产物换成 pids，落在
<论文目录>/md/ref_pids.json。存量论文（chunk 表还没有 metadata.pids）就靠它 ——
不该为了补 pids 去重建索引（要花 embedding，阶段 3 之后也不再必要）。

取值顺序（每条 code 逐级回落，**取不到就跳过并计数，绝不编造**）：

1. <论文目录>/md/ref_pids.json 里该路该 code 的 pids（capture 产物，与 chunk 表无关）；
2. refs_json.resolved[code] 那条 chunk 的 metadata.pids；
3. inventory 里该 code 的 chunk_id 那条 chunk 的 metadata.pids（resolved 漏登记 / 那条
   chunk 已不在时，这是唯一还剩的定位信息）；
4. 都没有 → 跳过并计数。

写库边界：**只写** understanding.refs_json 这一列的 pids 键（codes / resolved /
invalid / cross_road / ok 一个都不动，inventory 一列不碰）；--dry-run 一个字节都不写；
幂等 —— 已经有 pids 的 code 不重写（backfill 只补缺口，不覆盖读路径正在用的那份），
所以第二次运行 changed == []。

用法::

    python scripts/backfill_ref_pids.py <paper_id> [--dry-run] [--root DIR]

--root 把"数据根"换掉（只给测试）：capture 产物读 <root>/data/papers/<id>/md/ref_pids.json，
路径定义**复用** remap_understanding_refs.ref_path，不在这里另拼一份。

退出码：有跳过（缺口）→ 0（打印汇总）；读不到该论文的 understanding → 2。

设计约束：判定全是模块级**纯函数**（cited_codes / resolve_pids / plan_backfill /
backfilled_refs），IO 只在 backfill 里；单测喂构造数据即可（tests/test_backfill_ref_pids.py）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.utils import json_dumps, json_loads, read_json  # noqa: E402

#: 打印"补了什么"的样例条数（其余只看汇总）
SAMPLE_MAX = 5
#: 正文里的短码（与 understanding_service._REF_RE / index_audit._CODE_RE 同口径）。
#: 只认 A + 2~4 位数字：inventory 的 code 就是这个形状，放宽只会把普通词算成"缺口"。
_CODE_RE = re.compile(r"\b(A\d{2,4})\b")


# --------------------------------------------------------------------------
# 小工具（纯函数）
# --------------------------------------------------------------------------


def _as_str_list(value: Any) -> list[str]:
    """只留非空字符串，保序去重（pids 不许多、不许重）。"""
    out: list[str] = []
    for raw in value or []:
        text = str(raw or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def cited_codes(markdown: str | None) -> list[str]:
    """正文里出现过的短码（按首次出现顺序去重）—— 回填的范围只到这里。"""
    out: list[str] = []
    for match in _CODE_RE.finditer(markdown or ""):
        code = match.group(1)
        if code not in out:
            out.append(code)
    return out


# --------------------------------------------------------------------------
# 取值：一条 code → pids（三级回落）
# --------------------------------------------------------------------------


def capture_codes(capture_doc: dict | None, path: str) -> dict[str, dict]:
    """ref_pids.json → 该路（a / b）的 ``{code: {"chunk_id", "pids", ...}}``。

    缺失 / 损坏 / 没有这一路 / 形状不对，一律当"没有这一级"（返回空 dict），**不抛** ——
    capture 产物只是加速器，没有它下面还有 chunk 表那两级。
    """
    paths = (capture_doc or {}).get("paths")
    payload = (paths or {}).get(path) if isinstance(paths, dict) else None
    codes = (payload or {}).get("codes") if isinstance(payload, dict) else None
    return {str(code): info for code, info in (codes or {}).items()
            if isinstance(info, dict)}


def resolve_pids(code: str, *, resolved: dict, inv_by_code: dict,
                 captured: dict, chunk_pids: dict) -> tuple[list[str], str]:
    """一条 code 的行身份 → ``(pids, 来源)``；取不到给 ``([], "")``。

    来源：``capture``（ref_pids.json）/ ``chunk``（resolved 那条 chunk 的 metadata.pids）/
    ``inventory``（inventory 里 chunk_id 那条 chunk 的 metadata.pids）。
    三级全空 = 没有身份，绝不拿"猜出来的行"顶替。
    """
    got = _as_str_list((captured.get(code) or {}).get("pids"))
    if got:
        return got, "capture"
    for source, chunk_id in (("chunk", resolved.get(code)),
                             ("inventory", (inv_by_code.get(code) or {}).get("chunk_id"))):
        pids = _as_str_list(chunk_pids.get(str(chunk_id or "").strip()))
        if pids:
            return pids, source
    return [], ""


# --------------------------------------------------------------------------
# 计划（纯函数）
# --------------------------------------------------------------------------


def plan_path(payload: dict, captured: dict, chunk_pids: dict) -> dict:
    """一路（a / b）的计划：正文里的每条短码 → 可补 / 已有 / 跳过。

    payload 是 ``remap_understanding_refs.payloads_from_rows`` 的形状
    （``path`` / ``markdown`` / ``refs`` / ``inventory``）。``已有`` = refs_json.pids
    里已经有这一条：**不重写**（幂等，也不覆盖读路径正在用的那份）。
    """
    refs = payload.get("refs") if isinstance(payload.get("refs"), dict) else {}
    resolved = refs.get("resolved") if isinstance(refs.get("resolved"), dict) else {}
    saved = refs.get("pids") if isinstance(refs.get("pids"), dict) else {}
    inventory = [it for it in (payload.get("inventory") or []) if isinstance(it, dict)]
    inv_by_code = {str(it.get("code") or ""): it for it in inventory}

    items: list[dict] = []
    for code in cited_codes(payload.get("markdown")):
        have = _as_str_list(saved.get(code))
        if have:
            items.append({"code": code, "pids": have, "source": "existing",
                          "action": "keep"})
            continue
        pids, source = resolve_pids(code, resolved=resolved, inv_by_code=inv_by_code,
                                    captured=captured, chunk_pids=chunk_pids)
        items.append({"code": code, "pids": pids, "source": source,
                      "action": "fill" if pids else "skip"})
    return {
        "items": items,
        "total": len(items),
        "fillable": sum(1 for it in items if it["action"] == "fill"),
        "existing": sum(1 for it in items if it["action"] == "keep"),
        "skipped": [it["code"] for it in items if it["action"] == "skip"],
        "by_source": {name: sum(1 for it in items if it["source"] == name)
                      for name in ("capture", "chunk", "inventory")},
    }


def plan_backfill(paper_id: str, payloads: Sequence[dict], chunk_pids: dict,
                  capture_doc: dict | None) -> dict:
    """整篇的计划（含逐路计数与汇总）。``chunk_pids`` = ``{chunk id: [pids]}``。"""
    paths: dict[str, dict] = {}
    for payload in payloads or []:
        if not isinstance(payload, dict):
            continue
        path = str(payload.get("path") or "")
        paths[path] = plan_path(payload, capture_codes(capture_doc, path), chunk_pids)
    totals = {"total": 0, "fillable": 0, "existing": 0}
    by_source = {name: 0 for name in ("capture", "chunk", "inventory")}
    skipped: list[dict] = []
    for path in sorted(paths):
        summary = paths[path]
        for key in totals:
            totals[key] += summary[key]
        for name in by_source:
            by_source[name] += summary["by_source"][name]
        skipped += [{"path": path, "code": code} for code in summary["skipped"]]
    return {"paper_id": paper_id, "paths": paths, **totals,
            "by_source": by_source, "skipped": skipped}


# --------------------------------------------------------------------------
# 写回（纯函数：新 refs 的拼装）
# --------------------------------------------------------------------------


def backfilled_refs(refs: dict, items: Sequence[dict]) -> tuple[dict, list[str]]:
    """按计划补一份 refs_json；返回 ``(新 refs, 真写进去的 code 列表)``。

    只碰 ``pids`` 一个键：其它键原样带走（codes / resolved / invalid / cross_road / ok
    一个都不动）。``keep``（已有）与 ``skip``（取不到）都不写 —— 前者是幂等的来源，
    后者是"不编造"的落点。
    """
    new_refs = dict(refs or {})
    saved = new_refs.get("pids")
    pids_map = dict(saved) if isinstance(saved, dict) else {}
    filled: list[str] = []
    for item in items or []:
        if item.get("action") != "fill":
            continue
        pids = _as_str_list(item.get("pids"))
        if not pids:
            continue
        code = str(item.get("code") or "")
        pids_map[code] = pids
        filled.append(code)
    if filled:
        new_refs["pids"] = pids_map
    return new_refs, filled


def apply_plan_to_rows(rows: Iterable[Any], plan: dict, *, dry_run: bool = False) -> dict:
    """把计划写回 understanding 记录：**只写** refs_json 一列。

    dry_run 时一个字段都不赋值（连"改了但没提交"都不允许），返回的计数照算 ——
    那正是 dry-run 要打印的东西。
    """
    written = 0
    codes = 0
    for row in rows or []:
        is_dict = isinstance(row, dict)
        getter = (lambda k: row.get(k)) if is_dict else (lambda k: getattr(row, k, None))
        summary = (plan.get("paths") or {}).get(str(getter("path") or ""))
        if not summary or not summary.get("items"):
            continue
        refs = json_loads(getter("refs_json"), {}) or {}
        new_refs, filled = backfilled_refs(refs, summary["items"])
        if not filled:
            continue
        written += 1
        codes += len(filled)
        if dry_run:
            continue
        if is_dict:
            row["refs_json"] = json_dumps(new_refs)
        else:
            row.refs_json = json_dumps(new_refs)
    return {"written": written, "codes": codes, "dry_run": bool(dry_run)}


# --------------------------------------------------------------------------
# DB / 文件读取（唯一碰外部的地方）
# --------------------------------------------------------------------------


def _session_scope() -> Callable:
    """默认 session 工厂（延迟导入：单测注入假 session 时不碰库配置）。"""
    from backend.repositories.mysql.session import session_scope

    return session_scope


def load_understanding_rows(sess: Any, paper_id: str) -> list[Any]:
    """该论文的全部 understanding 记录（a / b 两路）。"""
    from backend.repositories.mysql.models import Understanding

    return list(sess.query(Understanding)
                .filter(Understanding.paper_id == paper_id).all())


def load_chunk_pids(sess: Any, paper_id: str) -> dict[str, list[str]]:
    """chunk 表 → ``{chunk id: metadata.pids}``；没有 pids 的 chunk 不进这张表。

    存量论文的 chunk 表还没有这个字段（没重建过索引）→ 这张表是空的，
    那种情况只能靠 capture 产物（见模块说明的三级回落）。
    """
    from backend.repositories.mysql.models import Chunk

    out: dict[str, list[str]] = {}
    rows = (sess.query(Chunk.id, Chunk.metadata_json)
            .filter(Chunk.paper_id == paper_id).all())
    for cid, meta_json in rows:
        meta = json_loads(meta_json, {}) or {}
        pids = _as_str_list(meta.get("pids"))
        if pids:
            out[str(cid)] = pids
    return out


def capture_path(paper_id: str, *, root: str | Path | None = None) -> Path:
    """capture 产物路径 —— **复用** remap_understanding_refs.ref_path（同一处定义）。"""
    from scripts.remap_understanding_refs import ref_path

    return ref_path(paper_id, root=root)


def load_capture_doc(paper_id: str, *, root: str | Path | None = None) -> dict | None:
    """读 capture 产物；缺失 / 损坏都当"没有这一级"（返回 None），**绝不抛**。"""
    data = read_json(capture_path(paper_id, root=root), default=None)
    return data if isinstance(data, dict) else None


class PaperNotFound(LookupError):
    """读不到论文（该论文没有 understanding 记录）：CLI 退出码 2。"""


def backfill(paper_id: str, *, dry_run: bool = False, root: str | Path | None = None,
             session_factory: Callable | None = None) -> dict:
    """补一篇论文全部解读的行身份（dry_run 一个字节都不写）。"""
    from scripts.remap_understanding_refs import payloads_from_rows

    factory = session_factory or _session_scope()
    with factory() as sess:
        rows = load_understanding_rows(sess, paper_id)
        if not rows:
            raise PaperNotFound(
                f"读不到论文 {paper_id} 的 understanding（论文不存在，或还没生成解读）")
        chunk_pids = load_chunk_pids(sess, paper_id)
        capture_doc = load_capture_doc(paper_id, root=root)
        plan = plan_backfill(paper_id, payloads_from_rows(rows), chunk_pids, capture_doc)
        plan["result"] = apply_plan_to_rows(rows, plan, dry_run=dry_run)
    plan["capture_file"] = str(capture_path(paper_id, root=root))
    plan["capture_used"] = bool(capture_doc)
    return plan


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------


def summary_line(plan: dict) -> str:
    """结尾汇总（格式固定：可补 / 已有 / 跳过 + 三个来源计数）。"""
    by_source = plan.get("by_source") or {}
    return (f"合计 {plan.get('total', 0)} 条正文短码：可补 {plan.get('fillable', 0)}"
            f" / 已有 {plan.get('existing', 0)} / 跳过 {len(plan.get('skipped') or [])}"
            f"（来自 capture {by_source.get('capture', 0)}"
            f" / 来自 chunk {by_source.get('chunk', 0)}"
            f" / 来自 inventory {by_source.get('inventory', 0)}）")


def plan_lines(plan: dict, *, dry_run: bool) -> list[str]:
    """逐路计数 + 几条"补了什么"的样例（dry-run 与写库的差别只在这一行）。"""
    lines = ["（dry-run）只打印，不写库" if dry_run
             else "（backfill）只写 understanding.refs_json 的 pids 键"]
    source = "capture 产物：" + str(plan.get("capture_file") or "?")
    lines.append(f"论文：{plan.get('paper_id') or '?'}　{source}"
                 + ("" if plan.get("capture_used") else "（缺失/损坏 → 跳过这一级）"))
    samples = 0
    for path in sorted(plan.get("paths") or {}):
        summary = plan["paths"][path]
        by_source = summary["by_source"]
        lines.append(f"  路 {path}：{summary['total']} 条短码 —— 可补 {summary['fillable']}"
                     f" / 跳过 {len(summary['skipped'])} / 已有 {summary['existing']}"
                     f"（capture {by_source['capture']} / chunk {by_source['chunk']}"
                     f" / inventory {by_source['inventory']}）")
        for item in summary["items"]:
            if item["action"] != "fill" or samples >= SAMPLE_MAX:
                continue
            samples += 1
            lines.append(f"  [{path}] {item['code']} ← {'+'.join(item['pids'])}"
                         f"（{item['source']}）")
    if plan.get("skipped"):
        shown = ", ".join(f"[{it['path']}]{it['code']}" for it in plan["skipped"][:SAMPLE_MAX])
        lines.append(f"  跳过（三级都取不到，不编造）：{shown}")
    result = plan.get("result") or {}
    if result:
        verb = "会改" if result.get("dry_run") else "已改"
        lines.append(f"  {verb} {result.get('written', 0)} 条 understanding"
                     f"（{result.get('codes', 0)} 个 code）")
    return lines


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backfill_ref_pids.py",
        description="给存量解读补行身份（refs_json.pids）—— 不重建索引。"
                    "取值顺序：capture 产物 → resolved 的 chunk → inventory 的 chunk → 跳过。")
    parser.add_argument("paper_id", help="论文 id")
    parser.add_argument("--dry-run", action="store_true", help="只打印可补/跳过/已有，不写库")
    parser.add_argument("--root", default=None,
                        help="数据根（只给测试）：读 <root>/data/papers/<id>/md/ref_pids.json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        plan = backfill(args.paper_id, dry_run=bool(args.dry_run), root=args.root)
    except PaperNotFound as exc:
        print(f"✗ {exc}")
        return 2
    for line in plan_lines(plan, dry_run=bool(args.dry_run)):
        print(line)
    print(summary_line(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
