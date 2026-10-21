"""重建索引后把旧解读的短码重绑到新 chunk（capture / apply 两段式）。

为什么要它：重建索引会**删掉全部 chunk 并按新切点重建**，于是 chunk.id 全变，
understanding.refs_json.resolved 里那份"短码 → chunk id"就全成过期引用
（前端只能按 inventory 的页码跳整页）。

桥只能是 **pid**（row_identity 的行身份）：页内行 id p015-06 是位置编号，
重新切分 / 重新翻译就换号；pid 生成一次、重建时按指纹继承，所以
"旧 chunk 的行 pid 集合" 与 "新 chunk 的行 pid 集合" 是唯一跨重建可比的键。

两段式：

1. capture（**重建之前**跑）—— 读旧 chunk 的 row_ids / block_id，用页产物
   （reader_service.page_translations）的行 id → pid 表换成 pids，写到
   <论文目录>/md/ref_pids.json（md/ 是重建过程中唯一不被删的目录）。
   **code 集合 = refs_json.resolved 的 key ∪ inventory 的 code**：后者是"正文写了短码、
   收尾 JSON 漏登记"的那一类（线上 A116 就是这形态，读时靠 understanding_service.to_dict
   从 inventory 就地补齐）。只 capture resolved 等于把它们重新弄钝，所以一并收进来，
   每条记 source = resolved / inventory 供审计；
2. apply（**重建之后**跑）—— 读 ref_pids.json + 新 chunk 的 metadata.pids，按
   "①集合相等 > ②交集最大 > ③本路 road 匹配优先 > ④相同 chunk_type > ⑤相同页 >
   ⑥chunk id 字典序" 选最佳新 chunk，把 refs_json.resolved[code] 与 inventory 里该 code 的
   chunk_id / chunk_type / page **一起**改（只改 resolved 不改 inventory = 跳到新块却显示旧页）。
   inventory-only 的 code 写回后就是正式 resolved 了 —— 两类 code 一视同仁。

边界：本脚本对库的写只有 understanding.refs_json 与 understanding.inventory 两列，
外加那一个中间文件；--dry-run 一个字节都不写。pid 只从页产物取
（reader_service.page_translations 的 row["pid"]），**不调** row_identity.assign、
不自己算 —— 重算出来的 pid 与阅读器下发的对不上，引用一存就指错行。

用法::

    python scripts/remap_understanding_refs.py capture <paper_id> [--root DIR]
    python scripts/remap_understanding_refs.py apply   <paper_id> [--dry-run] [--root DIR]

--root 把"数据根"换掉（只给测试：落点是 <root>/data/papers/<id>/md/），真实调用一律走
paper_service.paper_paths，不在这里自己拼 data/papers/...。

设计约束：判定逻辑全是模块级**纯函数**（choose_chunk / build_ref_doc / plan_apply /
remap_refs），IO 只在 capture / apply 里；单测喂构造数据即可（tests/test_remap_refs.py）。
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

from backend.core.utils import (  # noqa: E402
    json_dumps, json_loads, now_iso, read_json, write_json,
)

#: 中间文件 schema 版本（写入即带；读的时候不校验，未知字段原样保留）
VERSION = 1
#: 中间文件名：与 row_ids.json 同目录（md/），重建索引不会删这一层
FILE_NAME = "ref_pids.json"
#: 打印样例条数（"会怎么改"只看前面几条，其余靠汇总）
SAMPLE_MAX = 5
#: 未映射清单最多列几条（其余只给数量）
UNMAPPED_MAX = 20
#: block_id 里"行 id 的伪前缀"（_row_formula_chunks 写 row-<行 id>）
_ROW_PREFIX = "row-"


# --------------------------------------------------------------------------
# 小工具（纯函数）
# --------------------------------------------------------------------------


def _as_int(value: Any, default: int | None = 0) -> int | None:
    """尽量转 int；转不了给 default（**不猜**页号）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str_list(value: Any) -> list[str]:
    """只留非空字符串，保序去重（pids 不许多、不许重）。"""
    out: list[str] = []
    for raw in value or []:
        text = str(raw or "").strip()
        if text and text not in out:
            out.append(text)
    return out


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------


def ref_path(paper_id: str, *, root: str | Path | None = None) -> Path:
    """中间文件路径：<论文目录>/md/ref_pids.json。

    root 只给单测用：传了就按 <root>/data/papers/<论文id>/md/ 落盘（与其他脚本对
    "数据根"的理解一致），免得测试往真实 data/ 里写；不传就走
    paper_service.paper_paths(paper_id)["md"]。
    """
    if root is not None:
        md_dir = Path(root) / "data" / "papers" / paper_id / "md"
    else:
        from backend.services.paper.paper_service import paper_paths

        md_dir = Path(paper_paths(paper_id)["md"])
    return md_dir / FILE_NAME


def write_ref_doc(paper_id: str, doc: dict, *, root: str | Path | None = None) -> Path:
    """原子写中间文件（复用 core.utils.write_json = 临时文件 + os.replace）。

    幂等：同一篇重复 capture 直接覆盖同名文件，不追加、不合并旧内容。
    """
    path = ref_path(paper_id, root=root)
    write_json(path, doc, indent=2)
    return path


# --------------------------------------------------------------------------
# capture：旧 chunk → pids
# --------------------------------------------------------------------------


def pid_by_row_id(pages: Sequence[dict] | None) -> dict[str, str]:
    """页产物 → {行 id: pid}（与 knowledge_service._pid_by_row_id 同一口径）。

    pid **只从页产物取**（_finalize_page 是唯一写 row["pid"] 的地方），这里不重算、
    不补默认值：读不到产物的行就是没有 pid。
    """
    out: dict[str, str] = {}
    for page in pages or []:
        if not isinstance(page, dict):
            continue
        for row in page.get("rows") or []:
            if not isinstance(row, dict):
                continue
            rid = str(row.get("id") or "").strip()
            pid = str(row.get("pid") or "").strip()
            if rid and pid:
                out[rid] = pid
    return out


def _fallback_row_ids(chunk: dict) -> list[str]:
    """row_ids 之外的锚点：metadata.row_id、block_id。

    为什么要它们：正文 / 单元的 block_id **就是**首行 id（_make_chunk 写的是
    first["id"]），公式块的 block_id 是 row-<行 id>；而公式 chunk 的 row_ids
    常常是空的（锚点没解析出来）。这些都是真实存在的行 id，拿它只是"多问一句"，
    取不到 pid 仍然算没有（见 pids_for_chunk）。
    """
    out: list[str] = []
    row_id = str(chunk.get("row_id") or "").strip()
    if row_id:
        out.append(row_id)
    block_id = str(chunk.get("block_id") or "").strip()
    if block_id:
        out.append(block_id[len(_ROW_PREFIX):]
                   if block_id.startswith(_ROW_PREFIX) and len(block_id) > len(_ROW_PREFIX)
                   else block_id)
    return out


def pids_for_chunk(chunk: dict | None, pid_of: dict[str, str]) -> list[str]:
    """一条旧 chunk → 它的行身份列表；**取不到就是空列表，绝不编造**。

    先按 row_ids 逐个换 pid（与 row_ids 同序、去重）；一条都换不到时才试
    _fallback_row_ids（公式块的 row_id / block_id）。
    """
    if not isinstance(chunk, dict):
        return []
    pids: list[str] = []
    for rid in _as_str_list(chunk.get("row_ids")):
        pid = pid_of.get(rid)
        if pid and pid not in pids:
            pids.append(pid)
    if pids:
        return pids
    for rid in _fallback_row_ids(chunk):
        pid = pid_of.get(rid)
        if pid and pid not in pids:
            pids.append(pid)
    return pids


#: 正文里的短码（与 understanding_service._REF_RE 同口径：A001 / 来源 Chunk: A023 都认）。
#: 这里不 import 那个模块 —— 它会把 sqlalchemy / 服务层整条链拉进来，脚本要保持能单文件编译。
_CITED_RE = re.compile(r"\b([A-Z]\d{2,4})\b")


def _cited_codes(markdown: str) -> list[str]:
    """正文里出现过的短码（按首次出现顺序去重）。"""
    out: list[str] = []
    for match in _CITED_RE.finditer(markdown or ""):
        code = match.group(1).upper()
        if code not in out:
            out.append(code)
    return out


def payloads_from_rows(rows: Iterable[Any]) -> list[dict]:
    """understanding 记录 → [{"path","refs","inventory"}]（ORM 行与 dict 都吃）。"""
    out: list[dict] = []
    for row in rows or []:
        table = row if isinstance(row, dict) else None
        get = (lambda k: table.get(k)) if table is not None else (lambda k: getattr(row, k, None))
        out.append({
            "path": str(get("path") or ""),
            "refs": json_loads(get("refs_json"), {}) or {},
            "inventory": json_loads(get("inventory"), []) or [],
            # 正文也用得上：inventory 是全篇材料（213 条），而"resolved 漏登记"的只有
            # **正文真引用了**的那几条 —— 靠 markdown 才能把范围收窄到实际用到的短码。
            "markdown": str(get("markdown") or ""),
        })
    return out


def build_ref_doc(paper_id: str, understanding: Sequence[dict], chunks: dict[str, dict],
                  pid_of: dict[str, str], *, captured_at: str | None = None) -> dict:
    """组装中间文件内容（纯函数）：每路每 code → 旧 chunk_id / pids / page / chunk_type。

    understanding 是 payloads_from_rows 的结果，chunks 是
    {旧 chunk id: {row_ids, row_id, block_id, page, chunk_type}}。

    code 集合是 **resolved 的 key ∪ inventory 的 code**：后者是"正文写了短码、收尾 JSON
    漏登记"的那一类（线上 A116；读时 understanding_service.to_dict 会从 inventory 就地
    补齐）。只 capture resolved 会把它们重新变成过期引用，所以要一并收进来，
    每条记 source = resolved / inventory（审计"这枚标号当初是怎么来的"）。
    同名的以 resolved 为准（它的 chunk_id 才是当时的解析结果）。

    - 某条 code 找不到旧 chunk：pids 给空列表，page / chunk_type 退回 inventory 里那份
      （那是唯一还剩的定位信息），**不编造** pids；
    - 旧 chunk 的 row_ids 一条都换不到 pid：同样给空列表。
    """
    paths: dict[str, dict] = {}
    for payload in understanding or []:
        if not isinstance(payload, dict):
            continue
        path = str(payload.get("path") or "")
        refs = payload.get("refs") if isinstance(payload.get("refs"), dict) else {}
        resolved = refs.get("resolved") if isinstance(refs.get("resolved"), dict) else {}
        inventory = [it for it in (payload.get("inventory") or []) if isinstance(it, dict)]
        inv_by_code = {str(it.get("code") or ""): it for it in inventory}

        def entry(chunk_id: str, inv: dict, source: str) -> dict:
            """一条 code 的 capture 结果（旧 chunk 表优先，其次 inventory 那点信息）。"""
            old = chunks.get(chunk_id) or {}
            page = _as_int(old.get("page"), None)
            if page is None:
                page = _as_int(inv.get("page"), 0)
            return {
                "chunk_id": chunk_id,
                "pids": pids_for_chunk(old, pid_of),
                "page": page,
                "chunk_type": str(old.get("chunk_type") or inv.get("chunk_type") or ""),
                "source": source,
            }

        codes: dict[str, dict] = {}
        for raw_code, raw_id in resolved.items():
            code = str(raw_code)
            codes[code] = entry(str(raw_id or ""), inv_by_code.get(code) or {}, "resolved")
        # inventory-only：**正文真引用了**、resolved 却漏登记的那一类（线上 A116 / A045）。
        # 范围必须按 markdown 里的短码收窄：inventory 是全篇材料（213 条），把它整个并进来
        # 会把从来没被引用过的短码写成 resolved，等于改掉"resolved = 引用了且能解析"的语义。
        # 放后面（顺序 = resolved 原序 → 正文出现顺序），重复的以 resolved 为准。
        for code in _cited_codes(payload.get("markdown") or ""):
            if code in codes:
                continue
            inv = inv_by_code.get(code)
            if inv is None:
                continue
            codes[code] = entry(str(inv.get("chunk_id") or ""), inv, "inventory")
        paths[path] = {"codes": codes}
    return {"version": VERSION, "paper_id": paper_id,
            "captured_at": captured_at or now_iso(), "paths": paths}


# --------------------------------------------------------------------------
# apply：pids → 新 chunk（纯函数：选择规则）
# --------------------------------------------------------------------------


#: 哪一路看哪些路的 chunk（a 路看 A 路 + 共用）。understanding_service.ROAD_OF_PATH
#: 现在只登记了 a（b 已随「阅读地图」下线），但老库里的 b 行仍要能重映射，所以两条都留。
_ROAD_OF_PATH = {"a": "a", "b": "b"}
#: road 取值里的"共用"：图表公式这类与语言无关的块，两路都算匹配
_SHARED_ROAD = "shared"


def _road_rank(cand: dict, road: str | None) -> int:
    """候选与这一路的 road 匹配度：0 = 本路 / 共用，1 = 明确属于另一路。

    ⚠️ `road` 为**空串**与"没有这个字段"都按**共用**处理（0）：图表公式这类与语言无关的块，
    入库时 metadata.road 常常是 `""` 而不是 `"shared"`。把它们当"不匹配"会踩到一个真实的坑 ——
    公式块与它所在的行**共用同一批 pids**，集合相等时若按 road 先裁决，公式块会被同行的
    unit 块挤掉，于是引用从"精确到这条公式"退回"精确到这一行"（线上 A045 就是这样变钝的）。
    排序里 ③ 只在 ①② 打平时起作用，所以这一修正不会推翻"集合相等优先"。
    """
    if not road:
        return 0                          # 路未知（老中间文件）：不偏袒
    if "road" not in cand:
        return 1                          # 老索引根本没写这个字段 → 未知，排在已知的后面
    cand_road = str(cand.get("road") or "").strip()
    if not cand_road:
        return 0                          # 空串 = 与语言无关的共用块，算本路
    return 0 if cand_road in (road, _SHARED_ROAD) else 1


def _match_key(cand: dict, wanted: set[str], *, chunk_type: str | None,
               page: int | None, road: str | None = None) -> tuple | None:
    """候选的排序键（越小越好）；无交集返回 None。

    顺序即优先级：①集合完全相等 → ②交集最大 → ③road 匹配优先 →
    ④chunk_type 相同 → ⑤页码相同 → ⑥chunk id 字典序（同样的输入永远选出同一条）。

    ⚠️ ① 永远排在 ③ 前面：集合相等就是集合相等，**不会**因为某条"交集更小 / 相等但
    road 更对"而让位（"集合相等优先于更大交集"这条最高优先级没被 road 这档改动）。
    """
    have = set(_as_str_list(cand.get("pids")))
    overlap = len(wanted & have) if wanted and have else 0
    if overlap == 0:
        return None
    exact = have == wanted
    same_type = 0 if (chunk_type and str(cand.get("chunk_type") or "") == chunk_type) else 1
    cand_page = _as_int(cand.get("page"), None)
    same_page = 0 if (page is not None and cand_page == page) else 1
    return (0 if exact else 1, -overlap, _road_rank(cand, road), same_type, same_page,
            str(cand.get("chunk_id") or ""))


def choose_chunk(pids: Sequence[str], candidates: Sequence[dict], *,
                 chunk_type: str | None = None, page: int | None = None,
                 road: str | None = None) -> dict | None:
    """在候选新 chunk 里给这组 pids 选最佳；一条都不沾边就返回 None。

    road 传"这一路的 road"（a / b）：A、B 两路的分块**共用同一批行**，pid 集合因此
    可以完全相同，只有 road 能把它们分开（见 knowledge_service._row_unit_chunks 的
    row_ids）。返回 {"chunk_id","pids","chunk_type","page","road","mode"}，mode 是
    "equal"（集合完全相等）或 "overlap"（按交集映射）—— 汇总是分开计的。
    """
    wanted = set(_as_str_list(pids))
    if not wanted:
        return None                      # 没有身份就没有桥：不猜
    best: dict | None = None
    best_key: tuple | None = None
    for cand in candidates or []:
        if not isinstance(cand, dict):
            continue
        key = _match_key(cand, wanted, chunk_type=chunk_type, page=page, road=road)
        if key is None:
            continue
        if best_key is None or key < best_key:
            best, best_key = cand, key
    if best is None:
        return None
    return {"chunk_id": str(best.get("chunk_id") or ""),
            "pids": _as_str_list(best.get("pids")),
            "chunk_type": str(best.get("chunk_type") or ""),
            "page": _as_int(best.get("page"), 0),
            "road": str(best.get("road") or ""),
            "mode": "equal" if (best_key and best_key[0] == 0) else "overlap"}


def plan_path(codes: dict[str, dict], candidates: Sequence[dict], *,
              road: str | None = None) -> dict:
    """一整路（a / b）的映射计划：每条 code → 新 chunk（或 None = 未映射）。

    road 只用于 ③ 那一档（A/B 两路 pid 集合相同，靠 road 分家）；候选里没有 road
    字段的老索引仍会被选中，只是排在 matching 之后。
    """
    items: list[dict] = []
    for raw_code, info in (codes or {}).items():
        info = info if isinstance(info, dict) else {}
        pids = _as_str_list(info.get("pids"))
        chunk_type = str(info.get("chunk_type") or "") or None
        page = _as_int(info.get("page"), None)
        match = choose_chunk(pids, candidates, chunk_type=chunk_type, page=page, road=road)
        items.append({
            "code": str(raw_code),
            "old_chunk_id": str(info.get("chunk_id") or ""),
            "new_chunk_id": (match["chunk_id"] if match else None),
            "mode": (match["mode"] if match else "none"),
            "pids": pids,
            # source 来自中间文件：resolved（正常登过记）/ inventory（正文写了但漏登记）
            "source": str(info.get("source") or ""),
            # 未映射时保留旧值：写回那一步只认 new_chunk_id，这两项只是报告用
            "chunk_type": (match["chunk_type"] if match else chunk_type),
            "page": (match["page"] if match else page),
        })
    equal = sum(1 for it in items if it["mode"] == "equal")
    overlap = sum(1 for it in items if it["mode"] == "overlap")
    return {"items": items, "total": len(items), "mapped": equal + overlap,
            "equal": equal, "overlap": overlap,
            "unmapped": [it["code"] for it in items if it["mode"] == "none"]}


def plan_apply(ref_doc: dict, candidates: Sequence[dict], *,
               paper_id: str | None = None) -> dict:
    """整份中间文件 + 新 chunk 表 → 完整计划（含汇总与前置样例）。"""
    paths: dict[str, dict] = {}
    raw_paths = (ref_doc or {}).get("paths")
    for path in sorted(raw_paths or {}):
        payload = raw_paths.get(path) or {}
        codes = payload.get("codes") if isinstance(payload.get("codes"), dict) else {}
        # 路号只影响 ③（road 匹配优先）：a 路看 A 路 + 共用，b 路看 B 路 + 共用
        paths[str(path)] = plan_path(codes, candidates, road=_ROAD_OF_PATH.get(str(path)))
    totals = {"total": 0, "mapped": 0, "equal": 0, "overlap": 0}
    unmapped: list[dict] = []
    samples: list[dict] = []
    for path in sorted(paths):
        summary = paths[path]
        for key in totals:
            totals[key] += summary[key]
        for code in summary["unmapped"]:
            unmapped.append({"path": path, "code": code})
        for item in summary["items"]:
            if item["new_chunk_id"] and len(samples) < SAMPLE_MAX:
                samples.append({"path": path, **item})
    return {"paper_id": paper_id or str((ref_doc or {}).get("paper_id") or ""),
            "paths": paths, **totals, "unmapped": unmapped, "samples": samples}


# --------------------------------------------------------------------------
# apply：写回（纯函数：新旧 refs / inventory 的拼装）
# --------------------------------------------------------------------------


def remap_refs(refs: dict, inventory: Sequence[dict], items: Sequence[dict]
               ) -> tuple[dict, list[dict], list[str]]:
    """按计划改一份 refs_json + inventory；返回 (新 refs, 新 inventory, 改了的 code)。

    只动三处：resolved[code]、inventory 里该 code 的 chunk_id / chunk_type / page
    （inventory 决定引用显示哪一页、什么类型，不改就是"跳到新块、显示旧页"），
    以及 refs_json.pids[code]（capture 算好的行身份，落库后读路径首选它认回同一行 ——
    chunk id 下次重建还会变，pid 不会）。pids 取不到就**不写这个键**（与读路径同口径）。
    codes / invalid / cross_road / ok 原样带走；**未映射的 code 一个字段都不碰**
    （旧 chunk id 留着，前端按过期引用处理，至少还能按页跳）。
    """
    new_refs = dict(refs or {})
    resolved = dict(new_refs.get("resolved") or {})
    new_inventory = [dict(it) for it in (inventory or []) if isinstance(it, dict)]
    by_code = {str(it.get("code") or ""): it for it in new_inventory}
    # 行身份一起写回：capture 已经把这组 pids 算好了，写进 refs_json 后读路径就能**首选**它
    # 认回同一行（chunk id 下次重建又会变，pids 不会）。老结构里没有 pids 时这份是空的。
    saved = new_refs.get("pids")
    pids_map = ({str(k): _as_str_list(v) for k, v in saved.items() if isinstance(v, list)}
                if isinstance(saved, dict) else {})
    changed: list[str] = []
    for item in items or []:
        new_id = str(item.get("new_chunk_id") or "")
        if not new_id:
            continue                     # 未映射：旧值不动
        code = str(item.get("code") or "")
        touched = False
        if resolved.get(code) != new_id:
            resolved[code] = new_id
            touched = True
        pids = _as_str_list(item.get("pids"))
        if pids and pids_map.get(code) != pids:
            pids_map[code] = pids
            touched = True
        inv = by_code.get(code)
        if inv is not None:
            for key, value in (("chunk_id", new_id),
                               ("chunk_type", item.get("chunk_type")),
                               ("page", item.get("page"))):
                if value is None:
                    continue             # "能确定才改"：拿不到就不写
                if inv.get(key) != value:
                    inv[key] = value
                    touched = True
        if touched:
            changed.append(code)
    new_refs["resolved"] = resolved
    if pids_map:
        new_refs["pids"] = pids_map
    return new_refs, new_inventory, changed


def apply_plan_to_rows(rows: Iterable[Any], plan: dict, *, dry_run: bool = False) -> dict:
    """把计划写回 understanding 记录：**只写** refs_json / inventory 两列。

    dry_run 时一个字段都不赋值（连"改了但没提交"都不允许），返回的计数照算 ——
    那正是 dry-run 要打印的东西。
    """
    written = 0
    codes = 0
    for row in rows or []:
        is_dict = isinstance(row, dict)
        path = str((row.get("path") if is_dict else getattr(row, "path", "")) or "")
        summary = (plan.get("paths") or {}).get(path)
        if not summary or not summary.get("items"):
            continue
        getter = (lambda k: row.get(k)) if is_dict else (lambda k: getattr(row, k, None))
        refs = json_loads(getter("refs_json"), {}) or {}
        inventory = json_loads(getter("inventory"), []) or []
        new_refs, new_inventory, changed = remap_refs(refs, inventory, summary["items"])
        if not changed:
            continue
        written += 1
        codes += len(changed)
        if dry_run:
            continue
        if is_dict:
            row["refs_json"] = json_dumps(new_refs)
            row["inventory"] = json_dumps(new_inventory)
        else:
            row.refs_json = json_dumps(new_refs)
            row.inventory = json_dumps(new_inventory)
    return {"written": written, "codes": codes, "dry_run": bool(dry_run)}


# --------------------------------------------------------------------------
# DB 读取（唯一碰库的地方，且都是只读）
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


def load_old_chunks(sess: Any, paper_id: str) -> dict[str, dict]:
    """旧 chunk 表 → {id: {row_ids, row_id, block_id, page, chunk_type}}。"""
    from backend.repositories.mysql.models import Chunk

    rows = (sess.query(Chunk.id, Chunk.block_id, Chunk.page_no, Chunk.chunk_type,
                       Chunk.metadata_json)
            .filter(Chunk.paper_id == paper_id).all())
    out: dict[str, dict] = {}
    for cid, block_id, page_no, chunk_type, meta_json in rows:
        meta = json_loads(meta_json, {}) or {}
        out[str(cid)] = {
            "row_ids": _as_str_list(meta.get("row_ids")),
            "row_id": meta.get("row_id"),
            "block_id": block_id,
            "page": _as_int(page_no, 0),
            "chunk_type": str(chunk_type or ""),
        }
    return out


def load_new_chunks(sess: Any, paper_id: str) -> list[dict]:
    """新 chunk 表 → 候选列表（metadata.pids / chunk_type / page_no）。

    没有 pids 的 chunk 不进候选：存量 chunk 本来就没这个字段，它也永远选不上。
    """
    from backend.repositories.mysql.models import Chunk

    rows = (sess.query(Chunk.id, Chunk.chunk_type, Chunk.page_no, Chunk.metadata_json)
            .filter(Chunk.paper_id == paper_id).all())
    out: list[dict] = []
    for cid, chunk_type, page_no, meta_json in rows:
        meta = json_loads(meta_json, {}) or {}
        pids = _as_str_list(meta.get("pids"))
        if not pids:
            continue
        # road 只给选择规则的 ③ 用（A/B 两路 unit 的 pid 集合完全相同）；老索引没有
        # 这个字段 → 空串，仍然留在候选里（见 _road_rank）。
        out.append({"chunk_id": str(cid), "pids": pids,
                    "chunk_type": str(chunk_type or ""), "page": _as_int(page_no, 0),
                    "road": str(meta.get("road") or "")})
    return out


# --------------------------------------------------------------------------
# 两个子命令
# --------------------------------------------------------------------------


def capture(paper_id: str, *, root: str | Path | None = None,
            pages: Sequence[dict] | None = None,
            session_factory: Callable | None = None,
            captured_at: str | None = None) -> dict:
    """重建**之前**跑：把旧 chunk 的行身份落到 md/ref_pids.json。

    pages / session_factory 只为单测注入（生产别传）：真实 pid 一律来自
    reader_service.page_translations(paper_id)。
    """
    factory = session_factory or _session_scope()
    with factory() as sess:
        understanding = payloads_from_rows(load_understanding_rows(sess, paper_id))
        chunks = load_old_chunks(sess, paper_id)
    page_list = pages
    if page_list is None:
        from backend.services.paper import reader_service

        page_list = reader_service.page_translations(paper_id)
    doc = build_ref_doc(paper_id, understanding, chunks, pid_by_row_id(page_list),
                        captured_at=captured_at)
    path = write_ref_doc(paper_id, doc, root=root)
    codes = {p: len(payload.get("codes") or {}) for p, payload in doc["paths"].items()}
    entries = [info for payload in doc["paths"].values()
               for info in (payload.get("codes") or {}).values()]
    return {"doc": doc, "path": path, "codes": codes,
            "total_codes": sum(codes.values()),
            "with_pids": sum(1 for info in entries if info.get("pids")),
            # resolved 漏登记、只有 inventory 有的那些（写回后就是正式 resolved）
            "from_inventory": sum(1 for info in entries
                                  if info.get("source") == "inventory")}


def apply(paper_id: str, *, dry_run: bool = False,
          root: str | Path | None = None,
          session_factory: Callable | None = None) -> dict:
    """重建**之后**跑：按 pids 把旧解读重绑到新 chunk（dry_run 一个字节都不写）。"""
    src = ref_path(paper_id, root=root)
    ref_doc = read_json(src, default=None)
    if not isinstance(ref_doc, dict):
        raise FileNotFoundError(f"没有 capture 产物：{src}（重建之前先跑 capture）")
    factory = session_factory or _session_scope()
    with factory() as sess:
        rows = load_understanding_rows(sess, paper_id)
        candidates = load_new_chunks(sess, paper_id)
        plan = plan_apply(ref_doc, candidates, paper_id=paper_id)
        plan["result"] = apply_plan_to_rows(rows, plan, dry_run=dry_run)
    plan["ref_file"] = str(src)
    return plan


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------


def summary_line(plan: dict) -> str:
    """结尾那一行汇总（格式固定：mapped N/M（集合相等 X / 交集 Y / 未映射 Z））。"""
    unmapped = plan.get("unmapped")
    count = len(unmapped) if isinstance(unmapped, (list, tuple)) else (unmapped or 0)
    return ("mapped {mapped}/{total}（集合相等 {equal} / 交集 {overlap} / "
            "未映射 {unmapped}）").format(
                mapped=plan.get("mapped", 0), total=plan.get("total", 0),
                equal=plan.get("equal", 0), overlap=plan.get("overlap", 0),
                unmapped=count)


def plan_lines(plan: dict, *, dry_run: bool) -> list[str]:
    """"会怎么改"：前置样例 + 未映射清单 + 每路计数（不含写库动作）。"""
    lines = ["（dry-run）只打印，不写库" if dry_run
             else "（apply）写回 understanding.refs_json / inventory"]
    lines.append(f"论文：{plan.get('paper_id') or '?'}　中间文件：{plan.get('ref_file') or '?'}")
    for path in sorted(plan.get("paths") or {}):
        summary = plan["paths"][path]
        lines.append(f"  路 {path}：{summary['mapped']}/{summary['total']} 条可映射"
                     f"（集合相等 {summary['equal']} / 交集 {summary['overlap']}）")
    for sample in plan.get("samples") or []:
        page = sample.get("page")
        origin = f"/{sample['source']}" if sample.get("source") else ""
        lines.append(
            f"  [{sample['path']}] {sample['code']} {sample.get('chunk_type') or '?'}"
            f" p{page if page is not None else '?'}: {sample['old_chunk_id']} → "
            f"{sample['new_chunk_id']}（{sample['mode']}{origin}）")
    unmapped = plan.get("unmapped") or []
    if unmapped:
        shown = ", ".join(f"[{it['path']}]{it['code']}" for it in unmapped[:UNMAPPED_MAX])
        more = f" …（共 {len(unmapped)} 条）" if len(unmapped) > UNMAPPED_MAX else ""
        lines.append(f"  未映射（保留旧 chunk id，按过期引用处理）：{shown}{more}")
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
        prog="remap_understanding_refs.py",
        description="重建索引前后，用行身份（pid）把旧解读的短码重绑到新 chunk。")
    sub = parser.add_subparsers(dest="command", required=True)

    cap = sub.add_parser(
        "capture", help="重建**之前**跑：把旧 chunk 的行身份记到 md/ref_pids.json")
    cap.add_argument("paper_id", help="论文 id")
    cap.add_argument("--root", default=None,
                     help="数据根（只给测试）：落点 <root>/data/papers/<id>/md/")

    app = sub.add_parser(
        "apply", help="重建**之后**跑：按 pids 把旧解读重绑到新 chunk")
    app.add_argument("paper_id", help="论文 id")
    app.add_argument("--dry-run", action="store_true", help="只打印会怎么改，不写库")
    app.add_argument("--root", default=None,
                     help="数据根（只给测试）：读 <root>/data/papers/<id>/md/ref_pids.json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "capture":
        got = capture(args.paper_id, root=args.root)
        print(f"已写 capture：{got['path']}")
        for path in sorted(got["codes"]):
            payload = got["doc"]["paths"][path]
            codes = payload.get("codes") or {}
            n_pids = sum(1 for info in codes.values() if info.get("pids"))
            n_inv = sum(1 for info in codes.values()
                        if info.get("source") == "inventory")
            print(f"  路 {path}：{len(codes)} 条 code（{n_pids} 条带 pids；"
                  f"{n_inv} 条只在 inventory）")
        print(f"合计 {got['total_codes']} 条 code，其中 {got['with_pids']} 条带 pids，"
              f"{got['from_inventory']} 条只在 inventory（resolved 漏登记）")
        return 0

    try:
        plan = apply(args.paper_id, dry_run=bool(args.dry_run), root=args.root)
    except FileNotFoundError as exc:
        print(f"✗ {exc}")
        return 2
    for line in plan_lines(plan, dry_run=bool(args.dry_run)):
        print(line)
    print(summary_line(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
