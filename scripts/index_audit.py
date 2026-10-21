"""索引健康自检：把"模型输出正确、链路映射出错"在**入库之后立刻**暴露出来。

为什么需要：检索/解读链路里有一类 bug 只有用户点标号才会发现 —— 模型写得没错，
是"行 → 单元 → chunk → 锚点"这段映射把 id 接错了（行没被任何 unit 收进去、
chunk 的 ``row_ids`` 指向不存在的行、公式块落成 ``row-x`` 这种恒等假 id、
解读正文里的短码在清单里找不到）。它们全都可以**纯查库**判定，不必等用户点。

用法::

    python scripts/index_audit.py                 # 审计库里所有有 chunk 的论文
    python scripts/index_audit.py <paper_id> ...  # 只审计指定论文
    python scripts/index_audit.py --json          # 机器可读（stdout 只出 JSON）

退出码：有 error 级发现 → 1；只有 warn 或全通过 → 0；跑不起来（读不到论文列表）→ 2。

四组检查（每条发现都带 level=error/warn/ok）：

1. **行覆盖**（row ⊆ unit ⊆ chunk）：正文行是否都落在某个 unit chunk 的
   ``metadata.row_ids`` 里；unit 的 ``row_ids`` 是否非空、是否都能在页级产物里找到
   （孤儿引用）；``metadata.unit_ids`` 指向的 unit chunk 是否存在。
2. **锚点可解析**：chunk 的 ``block_id`` 必须能被 ``anchor_map`` 解析到行、
   本身就是页级行 id、或是 ``row-<行 id>`` / ``blk-<块 id>``（判定一律走
   ``anchor_map.resolve_anchor``，绝不另写一套口径）；``row-x`` 这类恒等假 id 直接 error。
3. **解读引用的完整性**：``understanding.markdown`` 里的每个 ``[Axxx]`` 短码都要在
   ``refs_json`` 的 ``resolved``（或 ``inventory``）里；``inventory`` 为空而 markdown 非空 → error。
4. **索引卫生**：chunk id 重复（error）、``order_global`` 并列（warn）、
   ``prev/next/parent`` 指针悬空（warn）；``row_ids`` 跨页**仅统计**。

设计约束：**判定逻辑全是模块级纯函数**（``check_*``，入参是普通 dict 列表，返回发现列表），
IO 只在外层 ``_load_*`` 里做 —— 单测喂构造数据即可（``tests/test_index_audit.py``）。
每条检查独立 try/except：一条炸了只记 ``check crashed: <原因>``，不让整篇审计中断。
全程**只读**：不改库、不写文件。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 单条详情截断长度（只 print 发现与统计，不 print 大段内容）
DETAIL_MAX = 200
#: 一条发现里最多列几个 id（其余只给数量）
SAMPLE_MAX = 12

#: 解读正文里的短码，与 ``understanding_service._REF_RE`` 同一口径（``A`` + 2~4 位数字）
_CODE_RE = re.compile(r"\b(A\d{2,4})\b")
#: 解析器给的 markdown 锚点前缀（``anchor_map.PSEUDO_PREFIXES`` 之一）
_BLK_PREFIX = "blk-"
#: 指针类元数据：指向**别的 chunk**
_POINTERS = ("prev_unit_id", "next_unit_id", "parent_chunk_id")
#: 入库行 id 末尾的语言后缀（`knowledge_service._variant_row` 加的），与
#: `rag_service._LANG_SUFFIXES` 同一口径
_LANG_SUFFIXES = (":zh", ":en", ":any")


# --------------------------------------------------------------------------
# 小工具（纯函数）
# --------------------------------------------------------------------------


def _clip(text: object, limit: int = DETAIL_MAX) -> str:
    """压成一行并截断 —— 详情里不放整段内容。"""
    s = " ".join(str(text if text is not None else "").split())
    return s if len(s) <= limit else s[:limit - 1] + "…"


def finding(level: str, check: str, detail: object) -> dict:
    """一条发现：``{"level", "check", "detail"}``（level ∈ error / warn / ok）。"""
    return {"level": level, "check": check, "detail": _clip(detail)}


def _sample(items: list, limit: int = SAMPLE_MAX) -> str:
    """列表 → 摘要串：前 ``limit`` 项 + 省略数量。"""
    seq = [str(x) for x in list(items)]
    shown = seq[:limit]
    rest = len(seq) - len(shown)
    return ", ".join(shown) + (f" …(+{rest})" if rest > 0 else "")


def id_prefix(block_id: object) -> str:
    """块 id 的**形态**前缀：到第一个数字为止。

    ``p001-06`` → ``p``（页级行 id）、``attentionisa-p01-b06`` → ``attentionisa-p``（解析块 id）、
    ``row-x`` → ``row-x``（假 id）。用于汇总"解析不到的都是些什么东西"。
    """
    bid = str(block_id or "")
    if not bid:
        return "∅"
    match = re.match(r"[^0-9]*", bid)
    head = (match.group(0) if match else "") or bid
    return head[:20] or "∅"


def base_id(chunk_id: object) -> str:
    """剥掉入库行 id 末尾的**语言后缀**（``:en`` / ``:zh`` / ``:any``）。

    库里 chunk id 一律带后缀（``_variant_row`` 加的，两路/双语各一行），而元数据里的
    ``unit_ids`` / ``prev/next_unit_id`` / ``parent_chunk_id`` 存的是**不带后缀**的基准 id
    （``rag_service._without_lang`` 读的时候也是先剥再拼）。两边换算后再比，否则指针检查
    会把每一条都报成"指向不存在的 chunk"—— 全是假警，真问题反而被埋掉。
    """
    text = str(chunk_id or "")
    for suffix in _LANG_SUFFIXES:
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def _row_ids_of(chunk: dict) -> list[str]:
    """chunk 元数据里的 ``row_ids``（统一成 str 列表）。"""
    meta = chunk.get("metadata") or {}
    return [str(x) for x in (meta.get("row_ids") or [])]


def _body_rows(rows: list[dict]) -> list[dict]:
    """正文行 = 没被 skip、也不是结构行（图/表/认证过的图注由 figure/table chunk 承载）。"""
    return [r for r in rows if not r.get("skip_reason") and not r.get("structural")]


# --------------------------------------------------------------------------
# 检查 1：行覆盖（row ⊆ unit ⊆ chunk）
# --------------------------------------------------------------------------


def check_row_coverage(rows: list[dict], chunks: list[dict]) -> list[dict]:
    """正文行是否都被 unit chunk 覆盖；unit 的 ``row_ids`` 是否非空且引用真实行。

    ``rows``：``[{"id","page","skip_reason","structural",...}]``（来自 ``chunking.row_units``）；
    ``chunks``：``[{"id","chunk_type","metadata":{...}}]``。
    """
    out: list[dict] = []
    known = {str(r.get("id")) for r in rows if r.get("id")}
    if not known:
        return [finding("error", "row_coverage",
                        "读不到任何页级行（row_units 为空），行覆盖无从校验")]

    unit = [c for c in chunks if c.get("chunk_type") == "unit"]
    if not unit:
        out.append(finding("error", "row_coverage",
                           "没有任何 unit chunk（kb.unit_index 关了？），正文行覆盖无从校验"))
    else:
        covered: set[str] = set()
        for chunk in unit:
            covered.update(_row_ids_of(chunk))
        body = _body_rows(rows)
        lost = [str(r.get("id")) for r in body if str(r.get("id")) not in covered]
        if lost:
            out.append(finding("error", "row_coverage",
                               f"{len(lost)} 个正文行没落在任何 unit chunk 的 row_ids 里"
                               f"（共 {len(body)} 行）: {_sample(lost)}"))

    empty = [str(c.get("id")) for c in unit if not _row_ids_of(c)]
    if empty:
        out.append(finding("error", "row_coverage",
                           f"{len(empty)} 个 unit chunk 的 row_ids 为空: {_sample(empty)}"))

    orphan: dict[str, list[str]] = {}
    for chunk in chunks:
        for rid in _row_ids_of(chunk):
            if rid not in known:
                orphan.setdefault(rid, []).append(str(chunk.get("id")))
    if orphan:
        shown = [f"{rid}(@{', '.join(cids[:2])})"
                 for rid, cids in list(orphan.items())[:SAMPLE_MAX]]
        out.append(finding("error", "row_coverage",
                           f"{len(orphan)} 个孤儿 row_id 在页级产物里找不到: {_sample(shown)}"))
    return out


def check_unit_links(chunks: list[dict]) -> list[dict]:
    """``metadata.unit_ids`` 指向的 unit chunk 必须真的存在（正文/图/表/公式块都挂这个指针）。"""
    known = {base_id(c.get("id")) for c in chunks}
    dangling: list[str] = []
    for chunk in chunks:
        meta = chunk.get("metadata") or {}
        for unit_id in (meta.get("unit_ids") or []):
            if base_id(unit_id) not in known:
                dangling.append(f"{unit_id}(@{chunk.get('id')})")
    if not dangling:
        return []
    return [finding("error", "unit_links",
                    f"{len(dangling)} 个 unit_ids 指向不存在的 chunk: {_sample(dangling)}")]


# --------------------------------------------------------------------------
# 检查 2：锚点可解析
# --------------------------------------------------------------------------


def anchor_status(block_id: str, anchors: dict) -> str:
    """``block_id`` 落到正文行的情况：``ok`` / ``fake`` / ``unknown``。

    解析**全部交给 ``anchor_map``**（``resolve_anchor`` 是"锚点解析的唯一定义"，
    纯函数、不查库；伪前缀 ``row-``/``blk-`` 的剥离也在里面），本函数只把结果归成三档 ——
    规格里允许的四种形态各落在哪一档很清楚：

    - ``block_row``（``build_block_row_map`` 的块 → 行映射）命中 = **真的**落到了正文行；
    - ``resolve_anchor`` 给出 ``kind=row`` 且查得到页 = 页级行 id 或 ``row-<行 id>``；
    - ``blk-<块 id>`` 且块表里有这个块 = 解析器给的 markdown 锚点。

    ``row-<行 id>`` 却查不到任何行（kind=row 但 page 为 None）= **恒等假 id**（``row-x``
    就是这么造出来的），单独报 error；剩下的一律 warn —— 其中最常见的一类是**裸块 id
    （不带 ``blk-``）只解析到页、解析不到行**，用户点标号只能整页闪，正是要暴露的那类问题。
    """
    from backend.services.paper.anchor_map import resolve_anchor

    if block_id in (anchors.get("block_row") or {}):
        return "ok"
    hit = resolve_anchor(block_id,
                         row_pages=anchors.get("row_pages") or {},
                         block_pages=anchors.get("block_pages") or {})
    kind = hit.get("kind")
    if kind == "row":
        return "ok" if hit.get("page") is not None else "fake"
    if kind == "block" and block_id.startswith(_BLK_PREFIX):
        return "ok"
    return "unknown"


def check_anchors(chunks: list[dict], anchors: dict) -> list[dict]:
    """每个 chunk 的 ``block_id`` 能不能落到正文行；``row-x`` 这类恒等假 id 直接 error。

    ``anchors``：``{"row_pages": {行 id: 页}, "block_pages": {块 id: 页},
    "block_row": {block_id: row_id}}``（``_load_anchors`` 装配；判定见 ``anchor_status``）。
    """
    out: list[dict] = []
    row_pages = anchors.get("row_pages") or {}
    fake: list[str] = []
    unknown: list[str] = []
    saved = 0
    for chunk in chunks:
        bid = chunk.get("block_id")
        if not bid:
            unknown.append("∅")
            continue
        bid = str(bid)
        status = anchor_status(bid, anchors)
        if status == "ok":
            continue
        if status == "fake":
            fake.append(bid)
            continue
        unknown.append(bid)
        # 公式条目常带 ``metadata.row_id``（``_row_formula_chunks`` 写的"真正落在哪一行"）：
        # 有的不算真问题（前端拿 row_id 照样跳得到行），所以把条数摆在同一条 warn 里
        meta = chunk.get("metadata") or {}
        fallback = meta.get("row_id") or (meta.get("row_ids") or [None])[0]
        if fallback and str(fallback) in row_pages:
            saved += 1

    if fake:
        out.append(finding("error", "anchor",
                           f"{len(fake)} 条恒等假 id（row-<不存在的行>，用户点了跳不动）: "
                           f"{_sample(sorted(set(fake)))}"))
    if unknown:
        dist = Counter(id_prefix(b) for b in unknown)
        top = " ".join(f"{k}×{v}" for k, v in dist.most_common(6))
        out.append(finding("warn", "anchor",
                           f"{len(unknown)} 条 block_id 解析不到正文行（裸块 id 只能跳到页、"
                           f"或形态根本认不出；其中 {saved} 条有 metadata.row_id 兜底）；"
                           f"形态分布 {top}；样本 {_sample(sorted(set(unknown)))}"))
    return out


def id_form_stats(chunks: list[dict], anchors: dict) -> dict:
    """``block_id`` 形态分布（前缀 → 条数），以及可解析/总条数。"""
    dist: Counter = Counter()
    resolved = 0
    for chunk in chunks:
        bid = str(chunk.get("block_id") or "")
        dist[id_prefix(bid)] += 1
        if bid and anchor_status(bid, anchors) == "ok":
            resolved += 1
    return {"forms": dict(dist.most_common(10)), "resolved": resolved,
            "total": len(chunks)}


# --------------------------------------------------------------------------
# 检查 3：解读引用的完整性
# --------------------------------------------------------------------------


def check_understanding(markdown: str | None, refs: dict | None,
                        inventory: list | None) -> list[dict]:
    """markdown 里的每个 ``[Axxx]`` 都要能在 ``resolved`` / ``inventory`` 里找到。

    ``refs``：``refs_json`` 解析结果（``{"resolved": {code: chunk_id}, ...}``）；
    ``inventory``：``[{"code","chunk_id",...}]``。
    """
    out: list[dict] = []
    md = markdown or ""
    inv = [it for it in (inventory or []) if isinstance(it, dict)]
    if md.strip() and not inv:
        out.append(finding("error", "understanding_refs",
                           f"inventory 为空但 markdown 非空（{len(md)} 字）—— 标号必然点不动"))
    known = {str(code) for code in ((refs or {}).get("resolved") or {})}
    known |= {str(it.get("code")) for it in inv if it.get("code")}
    used = sorted(set(_CODE_RE.findall(md)))
    missing = [code for code in used if code not in known]
    if missing:
        out.append(finding("error", "understanding_refs",
                           f"{len(missing)}/{len(used)} 个短码在 resolved/inventory 里找不到: "
                           f"{_sample(missing)}"))
    return out


# --------------------------------------------------------------------------
# 检查 4：索引卫生（+ 跨页统计）
# --------------------------------------------------------------------------


def check_index_hygiene(chunks: list[dict]) -> list[dict]:
    """chunk id 重复（error）、``order_global`` 并列（warn）、悬空指针（warn）。"""
    out: list[dict] = []
    counts = Counter(str(c.get("id")) for c in chunks)
    dup = sorted(cid for cid, n in counts.items() if n > 1)
    if dup:
        out.append(finding("error", "index_hygiene",
                           f"{len(dup)} 个 chunk id 重复: {_sample(dup)}"))

    orders = Counter(int(c.get("order_global") or 0) for c in chunks)
    ties = sorted(o for o, n in orders.items() if n > 1)
    if ties:
        involved = sum(orders[o] for o in ties)
        out.append(finding("warn", "index_hygiene",
                           f"{len(ties)} 个 order_global 并列（涉及 {involved} 条 chunk；"
                           f"A/B 两路共用行序时是常态，但短码顺序会抖）: {_sample(ties)}"))

    known = {base_id(c.get("id")) for c in chunks}
    dangling: dict[str, list[str]] = {}
    for chunk in chunks:
        meta = chunk.get("metadata") or {}
        for field in _POINTERS:
            target = meta.get(field)
            # 指针存的是**基准 id**（不带 ``:en``/``:zh``），要比就得先剥后缀
            if target and base_id(target) not in known:
                dangling.setdefault(field, []).append(str(target))
    for field in _POINTERS:
        bad = dangling.get(field) or []
        if bad:
            out.append(finding("warn", "index_hygiene",
                               f"{len(bad)} 个 {field} 指向不存在的 chunk: "
                               f"{_sample(sorted(set(bad)))}"))
    return out


def row_span_stats(chunks: list[dict], rows: list[dict]) -> dict:
    """``row_ids`` 跨页的 chunk 数 —— **仅统计，不报错**（先看清现状，再决定要不要管）。"""
    page_of = {str(r.get("id")): int(r.get("page") or 0) for r in rows if r.get("id")}
    cross: list[str] = []
    unknown = 0
    for chunk in chunks:
        ids = _row_ids_of(chunk)
        pages = [page_of[rid] for rid in ids if rid in page_of]
        unknown += len(ids) - len(pages)
        if pages and pages[0] != pages[-1]:
            cross.append(f"{chunk.get('id')}(p{pages[0]}→p{pages[-1]})")
    return {"chunks": len(chunks), "cross_page": len(cross),
            "unresolved_row_ids": unknown, "examples": cross[:SAMPLE_MAX]}


# --------------------------------------------------------------------------
# IO（全部只读；每个 loader 独立 try/except）
# --------------------------------------------------------------------------


def _load_paper_ids() -> list[str]:
    """库里所有有 chunk 的论文（排序稳定，输出可比）。"""
    from backend.repositories.mysql.models import Chunk
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        rows = sess.query(Chunk.paper_id).distinct().order_by(Chunk.paper_id).all()
    return [str(r[0]) for r in rows if r[0]]


def _load_chunks(paper_id: str) -> list[dict]:
    from backend.core.utils import json_loads
    from backend.repositories.mysql.models import Chunk
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        rows = (sess.query(Chunk).filter(Chunk.paper_id == paper_id)
                .order_by(Chunk.order_global.asc()).all())
        return [{"id": str(r.id), "paper_id": str(r.paper_id),
                 "chunk_type": str(r.chunk_type or ""),
                 "block_id": str(r.block_id) if r.block_id else "",
                 "page_no": int(r.page_no or 0),
                 "order_global": int(r.order_global or 0),
                 "metadata": json_loads(r.metadata_json, {}) or {}} for r in rows]


def _load_rows(paper_id: str) -> list[dict]:
    """页级行（**与建索引同一个口径**：``chunking.row_units``，带 skip/structural 标记）。

    结构行判定必须传 ``structural_caption_ids`` —— 它是"IR 认证过的图注行"，
    与 ``knowledge_service.build_chunks_for_paper`` 用的是同一个判据。不传的话图注行会被
    当成正文行，而它们本来就由 figure/table chunk 承载 → 会报一堆假"未覆盖"。
    """
    from backend.services.knowledge import chunking, knowledge_service
    from backend.services.paper import figure_regions, paper_service

    log = logging.getLogger(__name__)
    authors = ""
    try:
        paper = paper_service.get_paper(paper_id)
        authors = str((paper or {}).get("authors") or "")
    except Exception as exc:  # noqa: BLE001 —— 取不到作者只影响前沿 metadata 判定，不致命
        log.debug("取论文元数据失败（%s）：%s", paper_id, exc)
    try:
        regions = figure_regions.all_regions(paper_id)
    except Exception as exc:  # noqa: BLE001 —— 缺 IR 就当"没有图注"，与建索引时一致
        log.debug("读图/表区域失败（%s）：%s", paper_id, exc)
        regions = {}
    caption_ids, _ = knowledge_service._caption_rows(paper_id, regions)
    return chunking.row_units(paper_id, authors=authors,
                              structural_caption_ids=caption_ids)


def _load_anchors(paper_id: str, rows: list[dict]) -> dict:
    """``{"row_pages", "block_pages", "block_row"}``：``anchor_map.resolve_anchor`` 的入参 + 块→行映射。"""
    from backend.repositories.mysql.models import Block
    from backend.repositories.mysql.session import session_scope
    from backend.services.paper import anchor_map, reader_service

    md_map = reader_service.load_markdown_map(paper_id)
    with session_scope() as sess:
        blocks = (sess.query(Block).filter(Block.paper_id == paper_id)
                  .order_by(Block.order_global.asc()).all())
        parse_rows = [{"id": str(b.id), "page": int(b.page_no or 1),
                       "text": ((md_map.get(b.id) or {}).get("text")
                                or b.preview or "")} for b in blocks]
        block_pages = {str(b.id): int(b.page_no or 0) for b in blocks}
    payloads = reader_service.page_translations(paper_id)
    block_row = anchor_map.build_block_row_map(
        parse_rows, anchor_map.rows_by_page(payloads))
    return {"row_pages": {str(r.get("id")): int(r.get("page") or 0)
                          for r in rows if r.get("id")},
            "block_pages": block_pages, "block_row": block_row}


def _load_understanding(paper_id: str) -> list[dict]:
    from backend.core.utils import json_loads
    from backend.repositories.mysql.models import Understanding
    from backend.repositories.mysql.session import session_scope

    with session_scope() as sess:
        rows = (sess.query(Understanding)
                .filter(Understanding.paper_id == paper_id).all())
        return [{"id": str(r.id), "path": str(r.path or ""),
                 "markdown": r.markdown or "",
                 "refs": json_loads(r.refs_json, {}) or {},
                 "inventory": json_loads(r.inventory, []) or []} for r in rows]


def _guard(name: str, fn, findings: list[dict]):
    """跑一个 loader；炸了只记一条发现并返回 ``None``（不让整篇审计中断）。"""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        findings.append(finding("error", name,
                                f"load crashed: {exc.__class__.__name__}: {exc}"))
        return None


def _run(findings: list[dict], name: str, fn, *args) -> None:
    """跑一条检查；炸了只记 ``check crashed``（"一条检查失败不影响其它检查"的实现）。"""
    try:
        findings.extend(fn(*args) or [])
    except Exception as exc:  # noqa: BLE001
        findings.append(finding("error", name,
                                f"check crashed: {exc.__class__.__name__}: {exc}"))


# --------------------------------------------------------------------------
# 单篇审计
# --------------------------------------------------------------------------


def audit_paper(paper_id: str) -> dict:
    """审计一篇论文，返回 ``{"paper_id","chunks","findings","row_span","id_forms"}``。"""
    findings: list[dict] = []
    chunks = _guard("load_chunks", lambda: _load_chunks(paper_id), findings) or []
    rows = _guard("load_rows", lambda: _load_rows(paper_id), findings) or []

    anchors = None
    if not chunks:
        findings.append(finding("warn", "index", "该论文没有 chunk（未建索引）"))
    else:
        if rows:
            _run(findings, "row_coverage", check_row_coverage, rows, chunks)
        else:
            findings.append(finding("error", "row_coverage",
                                    "读不到页级行，行覆盖无法校验"))
        _run(findings, "unit_links", check_unit_links, chunks)

        anchors = _guard("load_anchors", lambda: _load_anchors(paper_id, rows), findings)
        if anchors:
            _run(findings, "anchor", check_anchors, chunks, anchors)
        else:
            findings.append(finding("error", "anchor", "读不到解析块，锚点无从校验"))
        _run(findings, "index_hygiene", check_index_hygiene, chunks)

    for item in _guard("load_understanding",
                       lambda: _load_understanding(paper_id), findings) or []:
        _run(findings, f"understanding_refs[{item['path']}]", check_understanding,
             item["markdown"], item["refs"], item["inventory"])

    return {"paper_id": paper_id, "chunks": len(chunks), "findings": findings,
            "row_span": row_span_stats(chunks, rows),
            "id_forms": id_form_stats(chunks, anchors) if anchors else {}}


def count_levels(findings: list[dict]) -> tuple:
    """``(errors, warns)``；``ok`` 级不计入任何一边。"""
    errors = sum(1 for f in findings if f.get("level") == "error")
    warns = sum(1 for f in findings if f.get("level") == "warn")
    return errors, warns


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------


def print_human(results: list[dict]) -> None:
    total_chunks = errors = warns = 0
    for res in results:
        e, w = count_levels(res["findings"])
        errors += e
        warns += w
        total_chunks += res["chunks"]
        print(f"{res['paper_id']}  chunks={res['chunks']}  errors={e}  warns={w}")
        for item in res["findings"]:
            print(f"[{item['level']}] {item['check']}: {item['detail']}")
        span = res.get("row_span") or {}
        if span.get("cross_page"):
            print(f"[stat] row_span: {span['cross_page']} 个 chunk 的 row_ids 跨页"
                  f"（仅统计，不报错）: {_sample(span.get('examples') or [])}")
        print()
    print(f"== 汇总：papers={len(results)} chunks={total_chunks} "
          f"errors={errors} warns={warns} ==")


def build_payload(results: list[dict]) -> dict:
    errors = sum(count_levels(r["findings"])[0] for r in results)
    warns = sum(count_levels(r["findings"])[1] for r in results)
    return {"papers": results,
            "summary": {"papers": len(results),
                        "chunks": sum(r["chunks"] for r in results),
                        "errors": errors, "warns": warns}}


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="索引健康自检（只读）：行覆盖 / 锚点可解析 / 解读引用 / 索引卫生")
    parser.add_argument("paper_ids", nargs="*", help="只审计这些论文（默认：全库有 chunk 的论文）")
    parser.add_argument("--json", action="store_true", help="机器可读输出（stdout 只出 JSON）")
    args = parser.parse_args(argv)

    logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    try:
        paper_ids = args.paper_ids or _load_paper_ids()
    except Exception as exc:  # noqa: BLE001 —— 连不上库是运行环境问题，不是索引问题
        message = f"读不到论文列表（数据库连不上？）: {exc.__class__.__name__}: {exc}"
        if args.json:
            print(json.dumps({"error": message}, ensure_ascii=False))
        print(message, file=sys.stderr)
        return 2

    results = [audit_paper(pid) for pid in paper_ids]
    if args.json:
        print(json.dumps(build_payload(results), ensure_ascii=False))
    else:
        print_human(results)
    errors = sum(count_levels(r["findings"])[0] for r in results)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
