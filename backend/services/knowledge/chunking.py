"""正文 chunk 的分块（**TOC 层级 + 页级行原子 + LLM 语义锚点**，三个输入一个输出）。

设计口径（见 docs/分块方案-语义单元.md）：

```
pages.json 的行（原子，en=original / zh=translation）
      +  阅读器目录（宏观层级边界：TOC 节点之间不许因为长度合适而合并）
      +  LLM 语义锚点（节点内部的语义单元边界，`breaks`，见 page_translate.apply_semantic_breaks）
   ↓ 确定性组装（target 软阈值 / hard 硬上限 / 父节引言并入首个子节）
   ↓ 校验层（覆盖一次、顺序不变、不跨节、≤ hard、公式表格完整；不合法就退化成行边界切分）
final chunk（正文从行**重新拼装**得到，绝不重新生成正文）
```

为什么不在这里碰公式 / 图 / 表：它们的原子 chunk 由 `knowledge_service` 独立生成
（公式来自 canonical 清单、图表来自 IR 裁图），本模块只负责**正文**。
正文装配里遇到这几类行时会跳过（结构行由各自的 chunk 承载），所以正文 chunk 里
永远不会出现表格串行、图内文字这类东西。

三个不变量（`validate` 逐条断言，失败即回退）：

1. **覆盖**：每个"该进正文的行"恰好出现一次；
2. **顺序**：chunk 的先后 = 行的先后（不重排、不丢行）；
3. **不跨节**：一个 chunk 的行必须落在同一个 TOC 子树里（祖先链上的关系不算跨界）。
"""

from __future__ import annotations

import re
from typing import Iterable

from backend.core.logging import get_logger
from backend.services.knowledge.preprocess import (
    first_body_index, is_front_matter, strip_presentation)
from backend.services.paper import reader_service

log = get_logger(__name__)

#: 结构行类型（由图/表 chunk 承载，不进正文块）
STRUCT_TYPES = ("caption", "table", "figure")
#: 语义单元的**长度兜底**（字符）：某节里模型没给锚点时，一段长正文不能整节变成一个单元。
#: 700 ≈ 2~3 个段落 —— 与实测的"单元中位 2 行"同量级（见 docs/语义单元索引-设计.md §2）。
UNIT_MAX_CHARS = 700
#: 参考文献小节名（整节不入库 —— 与改造前一致：解析侧 `type == "ref"` 的块被跳过）
_REF_SECTION = re.compile(r"^\s*(references?|bibliography|参考文献)\s*$", re.I)


# --------------------------------------------------------------------------
# 1. TOC：宏观边界
# --------------------------------------------------------------------------


def section_tree(paper_id: str, rows: list[dict] | None = None) -> list[dict]:
    """阅读器目录 → 分块用的节点树（节点带 ``row_index`` = 它的标题行在行序里的下标）。

    为什么用目录而不是解析侧 Markdown 标题（实测，两篇论文）：

    - 解析侧**漏标题**：turboquant 漏 `2.4 NEAR NEIGHBOUR SEARCH EXPERIMENTS`，
      attention 漏 `3.4 Embeddings and Softmax` 与 `6.2 Model Variations` —— 漏掉的节
      会被静默并进邻节，`section_path` 也就跟着错（旧索引里 `q13` 命中的小节名是 2.3、`q23` 是 6.1）；
    - 解析侧**多假标题**：算法框行 `6: r ←x −DEQUANT(idx)`、图内文字 `*PyramidKV Score: 0.981*`、
      表行 `GNMT + RL [38]` 都被标成 heading，进了旧索引的 `section_path`；
    - 目录是**页级标题行 + 章节编号**推出来的（`reader_service.build_outline` 里
      `_apply_semantic_levels` 按编号定层级），24 / 27 个节点全部能定位到行，且与阅读器左侧目录一致。

    没有页级产物时 `build_outline` 自己会退回解析索引（`_outline_from_index`）；连索引都没有
    （老产物 / 未解析）就返回空树，`assemble` 会退化成"整篇一个合成节点"。
    ``rows`` 传进来时顺带把节点的 ``row_index`` 定好（不传就留给调用方自己定）。
    """
    try:
        outline = reader_service.build_outline(paper_id) or {}
    except Exception as exc:  # noqa: BLE001 —— 目录读不到不该让建索引挂掉（退化成无层级）
        log.debug("读取目录失败（%s）：%s", paper_id, exc)
        outline = {}
    by_key: dict[str, dict] = {}

    def make(node: dict) -> dict:
        number = str(node.get("number") or "").strip()
        key = number or f"@{node.get('page')}:{node.get('block_id')}"
        item = {
            "key": key,
            "number": number,
            "title": (node.get("text") or "").strip(),
            "title_zh": (node.get("text_zh") or "").strip(),
            "level": int(node.get("level") or 1),
            "page": int(node.get("page") or 0),
            "row_id": node.get("block_id"),
            "children": [],
            "parent": None,
        }
        by_key[key] = item
        item["children"] = [make(c) for c in (node.get("children") or [])]
        for child in item["children"]:
            child["parent"] = item
        return item

    tree = [make(n) for n in (outline.get("tree") or [])]
    index_of_row = {r["id"]: r["index"] for r in (rows or [])}
    for item in _walk(tree):
        item["path"] = _path_of(item)
        item["row_index"] = index_of_row.get(str(item.get("row_id")))
    return tree


def load(paper_id: str, *, authors: str = "",
         structural_caption_ids: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    """``(行, TOC 树)`` 一起取，并**用同一棵树**做行归属（节点 ``row_index`` 必须来自同一批行）。

    ``structural_caption_ids``：IR 认证过的图注行 id。**不传就等于"没有图注"** ——
    页级行里以 Figure/Table 开头的行不都是图注（attention 的
    `Table 2 summarizes our results…` 是正文），认错了会从正文里删掉一段真内容。
    """
    rows = row_units(paper_id, authors=authors,
                     structural_caption_ids=structural_caption_ids)
    tree = section_tree(paper_id, rows)
    _assign_sections(rows, tree)
    return rows, tree


def _is_structural(row: dict) -> bool:
    """这一行是不是"结构行"（由图/表 chunk 承载，不进正文块）。判据在 `row_units` 里定。"""
    if "structural" in row:
        return bool(row["structural"])
    return row.get("type") in STRUCT_TYPES


def _walk(tree: Iterable[dict]) -> list[dict]:
    out: list[dict] = []
    for node in tree:
        out.append(node)
        out.extend(_walk(node["children"]))
    return out


def _path_of(node: dict) -> str:
    parts, cur = [], node
    while cur is not None:
        parts.append(cur["title"])
        cur = cur["parent"]
    return " / ".join(reversed([p for p in parts if p]))


# --------------------------------------------------------------------------
# 2. 行原子
# --------------------------------------------------------------------------


def row_units(paper_id: str, *, authors: str = "",
              structural_caption_ids: set[str] | None = None) -> list[dict]:
    """页级行 → 归一化行对象；**行是唯一的内容来源**。

    每个行对象：``{id, page, index, type, level, number, original, translation,
    section_key, section_path, section_title, semantic_break, skip_reason}``。

    跳过（与改造前口径一致）：
    - 参考文献（行类型 ``ref``，或整节标题是 REFERENCES —— turboquant 的参考文献行是 ``para``）；
    - 前沿元数据（作者/单位/邮箱/arXiv/会议行，复用 `preprocess.is_front_matter`）；
    - 空行。

    没有页级产物（未翻译论文）时用**解析块合成行**：一块一行、``translation=""``、
    ``id = block_id``（与前端 `usePageRows.rowFromBlock` 同一语义），这样下游只有一条代码路径。
    """
    pages = reader_service.page_translations(paper_id)
    raw_rows: list[dict] = []
    for payload in pages:
        page = int(payload.get("page") or 0)
        for row in payload.get("rows") or []:
            raw_rows.append({**row, "page": page})
    if not raw_rows:
        raw_rows = _synthetic_rows(paper_id)
        if not raw_rows:
            return []

    rows = [{
        "id": str(r.get("id") or ""),
        "page": int(r.get("page") or 0),
        "index": i,
        "type": str(r.get("type") or "para"),
        "level": int(r.get("level") or 0),
        "number": str(r.get("number") or ""),
        "original": str(r.get("original") or ""),
        "translation": str(r.get("translation") or ""),
        # 语义边界：`semantic_break` 是"有边界"，`semantic_break_side` 是"**哪一路**标的"
        # （原文侧匹配= 英文锚点=A 路；译文侧= 中文锚点=B 路）。按路分块靠后者区分。
        "semantic_break": bool(r.get("semantic_break")),
        "semantic_break_side": str(r.get("semantic_break_side") or ""),
        "semantic_type": str(r.get("semantic_type") or ""),
    } for i, r in enumerate(raw_rows)]

    body_start = first_body_index([{"type": r["type"], "text": r["original"]} for r in rows])
    verified = {str(x) for x in (structural_caption_ids or set())}
    for i, row in enumerate(rows):
        row["skip_reason"] = _skip_reason(row, i, body_start, authors)
        # 结构行 = 表体/图本身，或**IR 认证过的**图注行；其余 `caption` 行是正文（判据见 load 的说明）
        row["structural"] = (row["type"] in ("table", "figure")
                             or (row["type"] == "caption" and row["id"] in verified))
    _assign_sections(rows, section_tree(paper_id, rows))
    # 参考文献**整节**不入库：turboquant 的条目行是 `para`（不是 `ref`），只按行类型判会漏掉
    # 整节参考文献（用户口径 §11：REFERENCES 子树不进普通 RAG chunk，不因为语义锚点而变）。
    for row in rows:
        if not row["skip_reason"] and _REF_SECTION.match(
                strip_presentation(str(row.get("section_title") or "")).strip()):
            row["skip_reason"] = "ref_section"
    return rows


def _skip_reason(row: dict, index: int, body_start: int, authors: str) -> str:
    if not (row["original"] or row["translation"]).strip():
        return "empty"
    if row["type"] == "ref":
        return "ref"
    if _REF_SECTION.match(strip_presentation(row["original"]).strip()):
        return "ref_section"
    if is_front_matter(row["original"], page_no=row["page"],
                       before_body=index < body_start, authors=authors):
        return "front_matter"
    return ""


def _synthetic_rows(paper_id: str) -> list[dict]:
    """未翻译论文的兜底：用解析块合成行（一块一行，译文为空）。"""
    from backend.parsing.ir.ir_reader import IRReader
    from backend.repositories.mysql.models import Block
    from backend.repositories.mysql.session import session_scope
    from backend.services.paper import paper_service

    md_map = reader_service.load_markdown_map(paper_id)
    ir = IRReader(paper_service.paper_paths(paper_id)["ir"])
    formulas = {f["block_id"] for f in ir.formulas()}
    figures = {f["block_id"] for f in ir.figures()}
    tables = {t["block_id"] for t in ir.tables()}
    with session_scope() as sess:
        blocks = (sess.query(Block).filter(Block.paper_id == paper_id)
                  .order_by(Block.order_global.asc()).all())
    out: list[dict] = []
    for b in blocks:
        text = ((md_map.get(b.id) or {}).get("text") or b.preview or "").strip()
        if not text:
            continue
        btype = ("formula" if b.id in formulas else
                 "table" if b.id in tables else
                 "figure" if b.id in figures else
                 "heading" if b.type == "heading" else "para")
        out.append({"id": b.id, "page": int(b.page_no or 0), "type": btype,
                    "level": 0, "number": "", "original": text, "translation": "",
                    "semantic_break": False})
    return out


def _assign_sections(rows: list[dict], tree: list[dict]) -> None:
    """把每个行归到"标题行在它之前、层级最深"的 TOC 节点（行序即文档序）。

    节点自身的行（标题行）也归自己 —— 标题是它那一块的**第一行**，不是上一块的尾巴。
    """
    nodes = _walk(tree)
    located = sorted([n for n in nodes if n.get("row_index") is not None],
                     key=lambda n: n["row_index"])
    # 参考文献之后的节点都算"后置内容"（附录 / 可视化页）—— `Attention Visualizations`
    # 这种既没有编号、标题也不以单个大写字母开头的附录，只能靠"排在 REFERENCES 之后"认出来。
    back_matter = False
    for node in located:
        if _REF_SECTION.match(strip_presentation(str(node.get("title") or "")).strip()):
            back_matter = True
        node["_back_matter"] = back_matter

    current: dict | None = None
    cursor = 0
    for row in rows:
        while cursor < len(located) and located[cursor]["row_index"] <= row["index"]:
            current = located[cursor]
            cursor += 1
        row["section_key"] = (current or {}).get("key") or ""
        row["section_path"] = (current or {}).get("path") or ""
        row["section_title"] = (current or {}).get("title") or ""
        row["section_kind"] = ("appendix"
                               if _is_appendix(current) or (current or {}).get("_back_matter")
                               else "body")


def _is_appendix(node: dict | None) -> bool:
    """这一节是不是附录内容（检索侧可以用来降权/过滤，也是回归报告里的 appendix 列）。

    两条判据，都用得上：
    - 编号首字符是字母（`B`、`B.1`、`C.2`）—— 目录里带子节的附录大标题会拿到字母编号；
    - 编号抽不出来时看**标题本身**：`A EXTENDED PRIOR WORK` / `E IMPLEMENTATION` 这类
      **单个大写字母 + 空白**开头的标题就是附录大标题（实测目录里 E / D 没有编号，
      只看编号会把它们误判成正文 —— 旧索引里这一列是 100%，改造后掉到 88% 就是这个原因）。
      `Attention Visualizations` 这种"首字母恰好是 A 的单词标题"不会命中（A 后面不是空白）。
    """
    if node is None:
        return False
    number = str(node.get("number") or "").strip()
    if number and number[:1].isalpha():
        return True
    return bool(re.match(r"^[A-Z]\s+\S", str(node.get("title") or "").strip()))


# --------------------------------------------------------------------------
# 3. 语义单元
# --------------------------------------------------------------------------


#: 路号 → 该路的锚点落在**哪一侧**（`row["semantic_break_side"]`，见 `page_translate`）。
#: A 路（英文转录）的锚点是英文片段、匹配原文侧；B 路（中文翻译）的锚点是中文片段、匹配译文侧。
SIDE_OF_ROAD = {"a": "original", "b": "translation"}


def road_text(row: dict, road: str | None) -> str:
    """按路取这一行的正文：``"b"`` → 译文（缺译文时退回原文），其余（含并集口径）→ 原文。

    长度兜底、`chars()` 统计都要按**同一侧**算：中英字符数差异很大，
    拿英文长度去卡中文单元会把中文切得过碎（反之亦然）。
    """
    if road == "b":
        return str(row.get("translation") or row.get("original") or "")
    return str(row.get("original") or "")


def is_break(row: dict, road: str | None = None) -> bool:
    """这一行是不是**该路**的语义边界。

    `road=None` → 两路锚点的**并集**（旧行为：一条 chunk 同时服务中英两侧时的口径）；
    `"a"`/`"b"` → 只有那一路标的边界算数。两路的边界不同是**允许并且预期**的
    （用户口径：两个模型对语义边界的理解可能不同，不要强制它们一致），所以
    按路分块时各用各的边界，`chunk = 整数个 unit` 的约束在**每一路内部**成立。
    """
    if not row.get("semantic_break"):
        return False
    if road is None:
        return True
    return row.get("semantic_break_side") == SIDE_OF_ROAD[road]


def semantic_units(rows: list[dict], *, use_breaks: bool = True,
                   max_chars: int = 0, road: str | None = None) -> list[dict]:
    """正文行 → 语义单元（连续行的一段），边界 = TOC 节点边界 ∪ LLM 锚点 ∪（可选）长度上限。

    结构行（图/表）不参与：它们由各自的 chunk 承载，且**天然是单元的边界**
    （所以"表注与表体之间"永远不会被锚点切开）。

    - ``use_breaks=False`` 就是 arm 2（只有 TOC，没有 LLM 锚点），用来量锚点到底带来多少收益；
    - ``max_chars>0`` 是**代码侧的兜底**：某节里模型没给锚点时，一段长正文不能整节变成一个单元。
      到上限就在**行边界**切（行 = 段落，所以永远不会切进公式 / 表格 / 一句话中间）；
      只由一行组成的段不切（单行不可拆）；
    - ``road`` = ``"a"``/``"b"`` 时只用那一路的锚点（见 `is_break`），``None`` = 并集。

    长度兜底用**哪一侧**的字数：给了 `road` 就用那一侧（中文与英文的字符数差异很大，
    拿英文长度去卡中文单元会切得过碎）。`road=None`（并集口径）沿用英文字数，与改造前一致。
    """
    units: list[dict] = []
    cur: list[dict] = []
    size = 0

    def flush() -> None:
        nonlocal cur, size
        if cur:
            units.append({"rows": cur})
        cur, size = [], 0

    for row in rows:
        if row.get("skip_reason") or _is_structural(row):
            flush()
            continue
        if cur and (row["section_key"] != cur[-1]["section_key"]
                    or (use_breaks and is_break(row, road))):
            flush()
        elif (cur and max_chars
              and size + len(road_text(row, road)) > max_chars):
            flush()                     # 长度兜底：在行边界切
        cur.append(row)
        size += len(road_text(row, road))
    flush()
    return units


# --------------------------------------------------------------------------
# 4. 确定性组装
# --------------------------------------------------------------------------


def assemble(rows: list[dict], tree: list[dict], *, target: int, hard: int, floor: int,
             use_breaks: bool = True,
             unit_max_chars: int = UNIT_MAX_CHARS,
             road: str | None = None) -> tuple[list[dict], dict]:
    """行 + TOC + 语义单元 → 正文 chunk 列表（外加一份装配统计）。

    **chunk 由整数个 unit 组成**（unit 是分块层的原子，见 docs/语义单元索引-设计.md §1）：
    这样 `row ⊆ unit ⊆ chunk` 三层关系都是确定的，每个 unit 恰好有一个父 chunk，
    small-to-big 往上取上下文才有唯一答案。

    规则（自上而下）：

    1. 一个 TOC 节点的**整棵子树**能装进 ``target`` → 整棵成一块（主题完整，如 `2.2 Needle`、
       `D Additional Experiment`）；否则递归到子节点；
    2. 节点自己的行（标题 + 引言）单独成一块；短于 ``floor`` 且有子节点时**并进首个子块**
       （祖先→后代，不是兄弟跨越 —— 唯一允许的"跨界"）；
    3. 同一节点内按单元**贪心累加**到 ``target`` 之上即封口，``当前 + 下一个 > hard`` 必须停；
    4. 单元自身 > ``hard``（只可能是"单行就超长"）→ 退回**行边界**兜底（实测单行最长 1487 <
       hard=2240，正常语料不会发生）。
    """
    text_rows = [r for r in rows if not r.get("skip_reason") and not _is_structural(r)]
    # 行归属必须在装配前定好（`load()` 已经做过一次）；这里再调一次是幂等的，
    # 保证"装配看到的 section_path"与"校验看到的"是同一份。
    _assign_sections(rows, tree)
    if not tree and text_rows:
        # 没有目录（未翻译 / 缺索引）→ 整篇当一个合成节点，只靠 target/hard 切。
        # 这是**降级**而不是失败：内容与顺序不变，只是没有小节边界。
        tree = [{"key": "", "number": "", "title": "", "title_zh": "", "level": 1,
                 "page": text_rows[0]["page"], "row_id": text_rows[0]["id"],
                 "children": [], "parent": None, "path": "",
                 "row_index": text_rows[0]["index"]}]
    all_nodes = _walk(tree)
    unit_partition = semantic_units(text_rows, use_breaks=use_breaks,
                                    max_chars=unit_max_chars, road=road)
    unit_of_row = {r["index"]: u for u in unit_partition for r in u["rows"]}
    stats = {"nodes": 0, "whole_subtree": 0, "own_chunk": 0, "intro_merged": 0,
             "unit_split": 0, "row_fallback": 0, "chunks": 0, "units": len(unit_partition),
             # cap 额外切出来的单元数 = 有 cap 与无 cap 的单元数之差（确定性，不必埋计数器）
             "unit_capped": (len(unit_partition)
                             - len(semantic_units(text_rows, use_breaks=use_breaks,
                                                  road=road)))}
    chunks: list[dict] = []

    def chars(items: list[dict]) -> int:
        # 长度判据按**本路**的文本算（B 路看译文字数）
        return sum(len(road_text(r, road)) for r in items)

    def emit(items: list[dict], node: dict | None) -> None:
        if not items:
            return
        stats["chunks"] += 1
        chunks.append({"rows": items, "node": node})

    def units_of(items: list[dict]) -> list[dict]:
        """这段行覆盖了哪些单元（按行序去重；调用方保证传入的是整单元边界）。"""
        seen: list[dict] = []
        for row in items:
            unit = unit_of_row.get(row["index"])
            if unit is not None and (not seen or seen[-1] is not unit):
                seen.append(unit)
        return seen

    def pack_node(node: dict) -> None:
        stats["nodes"] += 1
        own = _own_rows(node, rows, text_rows, all_nodes)
        subtree = _subtree_rows(node, rows, text_rows, all_nodes)
        if not subtree:
            for child in node["children"]:
                pack_node(child)
            return
        if chars(subtree) <= target:
            stats["whole_subtree"] += 1
            emit(subtree, node)
            return

        intro_merged = False
        if own:
            if chars(own) < floor and node["children"]:
                intro_merged = True
                stats["intro_merged"] += 1
            else:
                stats["own_chunk"] += 1
                emit(own, node)
        mark = len(chunks)
        for child in node["children"]:
            pack_node(child)
        # 引言并进"本节点子树里第一个 chunk"：拼在它的行前面（祖先→后代，不是兄弟跨越）。
        # ⚠️ 合并后**不得超过 hard**：这一步是唯一允许的"跨界"，早先没检查长度，实测
        # 中文侧出现过 2241 字（比 hard 多 1 个字）的块 —— 校验层于是报 `over_hard`，
        # 而调用方对"校验不过"的反应是"整篇退回不用锚点"，代价与收益完全不成比例。
        # 找不到合得下的兄弟块时就把引言单独成块（它本来就是一个独立的小节引言）。
        if intro_merged and own:
            tail = own[-1]["index"]
            own_size = chars(own)
            # 目标 = **行序上紧接着引言的那个块**（首行下标最小、且全部行 > tail），
            # 不是"列表里的第一个块" —— `chunks` 此刻还是**发块顺序**，而发块顺序由 TOC
            # 节点顺序决定，可能和行序不一致。实测 turbo p017（B 路）：先发出的块从 229 开始，
            # 紧接着引言的块 207..228 排在它后面；挑错目标会合出**中间有洞**的块
            # （205,206,229..241），校验层报 `order_between_chunks`+`coverage`，
            # 调用方于是整篇退回"只按目录层级"（实测就发生了这一次）。
            best: tuple[int, dict] | None = None
            for chunk in chunks[mark:]:
                if not chunk["rows"]:
                    continue
                first = chunk["rows"][0]["index"]
                if first <= tail:
                    continue
                if own_size + chars(chunk["rows"]) > hard:
                    continue
                if best is None or first < best[0]:
                    best = (first, chunk)
            if best is not None:
                best[1]["rows"] = own + best[1]["rows"]
            else:
                # 合不进任何子块 → 引言单独成块。这时**追加**即可：`assemble` 最后会按
                # 首行下标整体排序，位置自然正确（以前插在 `mark` 处，在 TOC 顺序 ≠ 行序时是错的）。
                stats["chunks"] += 1
                chunks.append({"rows": own, "node": node})

    for node in tree:
        pack_node(node)

    # 超过 hard 的块按**单元边界**二次切分（单元自身超 hard 才退回行边界）
    out: list[dict] = []
    for chunk in chunks:
        if chars(chunk["rows"]) <= hard:
            out.append(chunk)
            continue
        stats["unit_split"] += 1
        pieces, used_rows = pack_units(units_of(chunk["rows"]), target=target, hard=hard,
                                      road=road)
        if used_rows:
            stats["row_fallback"] += 1
        for piece in pieces:
            out.append({"rows": [r for u in piece for r in u["rows"]],
                        "units": piece, "node": chunk["node"]})
    out.sort(key=lambda c: c["rows"][0]["index"])
    for chunk in out:
        # 块的稳定键 = 起止行 id（块是行的划分，所以唯一）。索引层据此把它换成真正的 chunk id，
        # 单元再通过这个键挂到父块上（`unit_records` → `build` → 索引层重写一次）。
        chunk["key"] = f"{chunk['rows'][0]['id']}-{chunk['rows'][-1]['id']}"
    stats["chunks"] = len(out)
    return out, stats


def pack_units(units: list[dict], *, target: int, hard: int, road: str | None = None
               ) -> tuple[list[list[dict]], bool]:
    """单元序列 → 块的划分（贪心）：累加到 ``target`` 之上即封口，``+下一个 > hard`` 必停。

    返回 ``(块的列表, 是否用到行边界兜底)``。单元自身 > ``hard`` 时按**行边界**兜底
    —— 单行不可拆，所以这条路径只在"某一行本身就超 hard"时才会走到。

    字数一律按**本路**的文本算（`road_text`）：中英字符数差异很大，混用会让其中一路的
    chunk 长度系统性偏大或偏小。
    """
    out: list[list[dict]] = []
    buf: list[dict] = []
    size = 0
    used_rows = False

    def flush() -> None:
        nonlocal buf, size
        if buf:
            out.append(buf)
        buf, size = [], 0

    for unit in units:
        unit_size = sum(len(road_text(r, road)) for r in unit["rows"])
        if unit_size > hard:
            used_rows = True
            flush()
            row_buf: list[dict] = []
            row_size = 0
            for row in unit["rows"]:
                if row_buf and row_size + len(road_text(row, road)) > hard:
                    out.append([{"rows": row_buf}])
                    row_buf, row_size = [], 0
                row_buf.append(row)
                row_size += len(road_text(row, road))
                if row_size >= target:
                    out.append([{"rows": row_buf}])
                    row_buf, row_size = [], 0
            if row_buf:
                out.append([{"rows": row_buf}])
            continue
        if buf and size + unit_size > hard:
            flush()
        buf.append(unit)
        size += unit_size
        if size >= target:
            flush()
    flush()
    return out, used_rows


def _own_rows(node: dict, rows: list[dict], text_rows: list[dict],
              all_nodes: list[dict]) -> list[dict]:
    """节点自己的行：从它的标题行起，到第一个子孙标题行（没有子孙就到下一个非子孙标题行）之前。"""
    start = node.get("row_index")
    if start is None:
        return []
    inner = [c["row_index"] for c in _walk(node["children"]) if c.get("row_index") is not None]
    if inner:
        end = min(inner)
    else:
        end = len(rows)
        for other in all_nodes:
            if other is node or _is_descendant(other, node):
                continue
            if other.get("row_index") is not None and other["row_index"] > start:
                end = min(end, other["row_index"])
    return [r for r in text_rows if start <= r["index"] < end]


def _subtree_rows(node: dict, rows: list[dict], text_rows: list[dict],
                  all_nodes: list[dict]) -> list[dict]:
    own = _own_rows(node, rows, text_rows, all_nodes)
    for child in _walk(node["children"]):
        own.extend(_own_rows(child, rows, text_rows, all_nodes))
    own.sort(key=lambda r: r["index"])
    return own


def _is_descendant(node: dict, ancestor: dict) -> bool:
    cur = node.get("parent")
    while cur is not None:
        if cur is ancestor:
            return True
        cur = cur.get("parent")
    return False


# --------------------------------------------------------------------------
# 5. 校验层
# --------------------------------------------------------------------------


def unit_records(rows: list[dict], chunks: list[dict], *, use_breaks: bool = True,
                 unit_max_chars: int = UNIT_MAX_CHARS,
                 road: str | None = None) -> list[dict]:
    """行 + chunk 划分 → **单元记录**（索引层直接用它建 `chunk_type="unit"` 的行）。

    这里**不生成任何文字**：`en` / `zh` 都是把行的 `original` / `translation` 按序拼起来
    （用户口径：LLM 负责语义分组，代码负责取原文）。

    每条记录带 `parent_chunk_id`（它属于哪条正文 chunk）与 `prev` / `next`（±1 邻居，
    都在同一父 chunk 内）—— small-to-big 直接用这两个指针，不必在检索时现算。

    `road` 给定时，`en`/`zh` **只填本路那一侧**（另一侧留空）：按路分块之后每条单元只有
    一个语言面，索引层据此只出一行（A 路 `:en`、B 路 `:zh`）。`road=None`（并集口径）
    两侧都填，与改造前一致。
    """
    text_rows = [r for r in rows if not r.get("skip_reason") and not _is_structural(r)]
    partition = semantic_units(text_rows, use_breaks=use_breaks, max_chars=unit_max_chars,
                               road=road)
    owner: dict[int, dict] = {}
    for chunk in chunks:
        for row in chunk["rows"]:
            owner[row["index"]] = chunk
    out: list[dict] = []
    for i, unit in enumerate(partition):
        items = unit["rows"]
        first, last = items[0], items[-1]
        parent = owner.get(first["index"])
        en = ("" if road == "b"
              else "\n\n".join(r["original"] for r in items))
        zh = ("" if road == "a"
              else "\n\n".join(r["translation"].strip() for r in items
                               if (r["translation"] or "").strip()))
        out.append({
            "unit_id": f"{first['id']}-{last['id']}",
            "row_ids": [r["id"] for r in items],
            "first_row": first, "last_row": last,
            "en": en, "zh": zh,
            "section_path": first.get("section_path") or "",
            "section_title": first.get("section_title") or "",
            "section_kind": first.get("section_kind") or "body",
            "pages": sorted({r["page"] for r in items}),
            "chars": sum(len(road_text(r, road)) for r in items),
            # 这条单元的左边界是**本路**标的锚点吗（A/B 的边界不同，所以必须按路判断）
            "anchor_used": bool(is_break(first, road)),
            "parent_chunk_id": (parent or {}).get("key"),
            "parent_key": (parent or {}).get("key"),
            "prev": None, "next": None,
            "_index": i,
        })
    # 邻居：同一父 chunk 内左右相邻
    by_parent: dict[str, list[dict]] = {}
    for record in out:
        by_parent.setdefault(str(record["parent_chunk_id"]), []).append(record)
    for group in by_parent.values():
        for left, right in zip(group, group[1:]):
            left["next"] = right["unit_id"]
            right["prev"] = left["unit_id"]
    return out


def build(rows: list[dict], tree: list[dict], *, target: int, hard: int, floor: int,
          use_breaks: bool = True,
          unit_max_chars: int = UNIT_MAX_CHARS,
          road: str | None = None) -> dict:
    """一次产出三层：``{chunks, units, stats, report}``（索引层的唯一入口）。

    - `chunks`：正文块（每个带 `unit_ids`）—— 上下文单位；
    - `units`：语义单元（带 `parent_chunk_id` / `prev` / `next`）—— 检索单位；
    - `report`：`validate` 的结果（行覆盖 / 顺序 / 不跨节 / 长度 / 结构 + 单元不变量）。

    ``road`` = ``"a"``/``"b"`` 时只用那一路的锚点、并按那一路的文本算长度
    （**两路的划分允许不同**：这是用户口径"两个模型对语义边界的理解可能不同"的落地点）。
    ``road=None`` 是并集口径，行为与改造前逐字一致。
    """
    chunks, stats = assemble(rows, tree, target=target, hard=hard, floor=floor,
                             use_breaks=use_breaks, unit_max_chars=unit_max_chars,
                             road=road)
    units = unit_records(rows, chunks, use_breaks=use_breaks,
                         unit_max_chars=unit_max_chars, road=road)
    by_parent: dict[str, list[str]] = {}
    for unit in units:
        by_parent.setdefault(str(unit["parent_chunk_id"]), []).append(unit["unit_id"])
    for chunk in chunks:
        chunk["unit_ids"] = by_parent.get(str(chunk.get("key")), [])
    report = validate(chunks, rows, hard=hard, units=units, road=road)
    return {"chunks": chunks, "units": units, "stats": stats, "report": report}


def validate(chunks: list[dict], rows: list[dict], *, hard: int,
             units: list[dict] | None = None,
             unit_max_chars: int = UNIT_MAX_CHARS,
             road: str | None = None) -> dict:
    """校验装配结果；任何一条不满足都记下来（调用方据此回退到确定性行边界切分）。

    校验项：
    - **覆盖**：该进正文的行恰好出现一次；
    - **顺序**：chunk 内的行下标递增，chunk 之间不交叉、不重排；
    - **不跨节**：一个 chunk 的行不落在两个不可比的 TOC 节点里（祖先/后代关系不算跨界）；
    - **长度**：本路文本 ≤ hard（一行不可拆，所以单行超限时只告警不失败）；
    - **结构完整**：正文块里不出现表格行 / 图注行 / 参考文献行（这些由图/表 chunk 承载）；
    - **单元不变量**（传了 `units` 才查）：单元按行序恰好一次覆盖同一批行、每个单元恰属于
      一个 chunk、邻居指针同父相邻、单元 ≤ `unit_max_chars`（单行单元除外）。
    """
    errors: list[dict] = []
    expected = [r for r in rows
                if not r.get("skip_reason") and not _is_structural(r)]
    seen: list[int] = []
    prev_index = -1
    for chunk in chunks:
        items = chunk["rows"]
        indices = [r["index"] for r in items]
        if indices != sorted(indices):
            errors.append({"kind": "order_in_chunk", "chunk_first": items[0]["id"]})
        if indices and indices[0] <= prev_index:
            errors.append({"kind": "order_between_chunks", "chunk_first": items[0]["id"]})
        prev_index = indices[-1] if indices else prev_index
        seen.extend(indices)
        content = "\n\n".join(road_text(r, road) for r in items)
        if len(content) > hard and len(items) > 1:
            errors.append({"kind": "over_hard", "chunk_first": items[0]["id"],
                           "chars": len(content)})
        for row in items:
            if _is_structural(row):
                errors.append({"kind": "structure_in_text", "row": row["id"]})
            if row.get("skip_reason"):
                errors.append({"kind": "skipped_row_in_text", "row": row["id"]})
        nodes = {r.get("section_path") or "" for r in items}
        nodes.discard("")
        if len(nodes) > 1 and not _paths_comparable(nodes):
            errors.append({"kind": "cross_section", "chunk_first": items[0]["id"],
                           "paths": sorted(nodes)[:3]})
    if seen != [r["index"] for r in expected]:
        missing = sorted(set(r["index"] for r in expected) - set(seen))
        dup = sorted(i for i in set(seen) if seen.count(i) > 1)
        errors.append({"kind": "coverage", "missing": missing[:8], "duplicate": dup[:8]})
    if units is not None:
        errors.extend(_validate_units(units, chunks, unit_max_chars=unit_max_chars))
    return {"ok": not errors, "errors": errors,
            "chunks": len(chunks), "rows": len(expected),
            "units": len(units) if units is not None else None,
            "covered": len(seen)}


def _validate_units(units: list[dict], chunks: list[dict], *,
                    unit_max_chars: int) -> list[dict]:
    """单元不变量（见 `validate` 的说明）。"""
    errors: list[dict] = []
    chunk_of_unit: dict[str, dict] = {}
    for chunk in chunks:
        for unit_id in chunk.get("unit_ids") or []:
            chunk_of_unit[unit_id] = chunk
    seen: list[str] = []
    prev_last = -1
    for unit in units:
        rows_span = unit.get("row_ids") or []
        if not rows_span:
            errors.append({"kind": "empty_unit", "unit": unit.get("unit_id")})
            continue
        parent = chunk_of_unit.get(str(unit["unit_id"]))
        if parent is None:
            errors.append({"kind": "unit_without_parent", "unit": unit["unit_id"]})
            continue
        index_of = {r["id"]: r["index"] for r in parent["rows"]}
        if not all(rid in index_of for rid in rows_span):
            errors.append({"kind": "unit_split_across_chunks", "unit": unit["unit_id"],
                           "parent": parent.get("id")})
        first = index_of.get(rows_span[0], -1)
        last = index_of.get(rows_span[-1], -1)
        if first <= prev_last:
            errors.append({"kind": "unit_order", "unit": unit["unit_id"]})
        prev_last = last if last >= 0 else prev_last
        if unit.get("chars", 0) > unit_max_chars and len(rows_span) > 1:
            errors.append({"kind": "unit_over_max", "unit": unit["unit_id"],
                           "chars": unit.get("chars")})
        seen.append(str(unit["unit_id"]))
    if len(seen) != len(set(seen)):
        dup = sorted({u for u in seen if seen.count(u) > 1})
        errors.append({"kind": "unit_duplicate", "units": dup[:8]})
    return errors


def _paths_comparable(paths: set[str]) -> bool:
    """这些 section_path 是不是同一条祖先链上的（`A / B` 与 `A / B / C` 可比，`A / B` 与 `A / D` 不可比）。"""
    split = [p.split(" / ") for p in paths]
    base = min(split, key=len)
    return all(s[:len(base)] == base or base[:len(s)] == s for s in split)
