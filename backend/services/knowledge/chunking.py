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
        "semantic_break_sides": list(r.get("semantic_break_sides") or []),
        "semantic_type": str(r.get("semantic_type") or ""),
        # v3「并入」：这一行要并进的那个单元的**开头原文片段**（跨页 / 不连续就靠它；
        # 解析成行 id 要等全文行都到齐，见 apply_row_merges）
        "semantic_merge_text": str(r.get("semantic_merge_text") or ""),
    } for i, r in enumerate(raw_rows)]

    body_start = first_body_index([{"type": r["type"], "text": r["original"]} for r in rows])
    verified = {str(x) for x in (structural_caption_ids or set())}
    for i, row in enumerate(rows):
        row["skip_reason"] = _skip_reason(row, i, body_start, authors)
        # 结构行 = 表体/图本身，或**IR 认证过的**图注行；其余 `caption` 行是正文（判据见 load 的说明）
        row["structural"] = (row["type"] in ("table", "figure")
                             or (row["type"] == "caption" and row["id"] in verified))
    # 脚注行（页内 `---` 之后）**不进正文块**：先标，后面所有"该进正文的行"都按 skip_reason 过滤
    mark_footnote_rows(rows)
    _assign_sections(rows, section_tree(paper_id, rows))
    # 参考文献**整节**不入库：turboquant 的条目行是 `para`（不是 `ref`），只按行类型判会漏掉
    # 整节参考文献（用户口径 §11：REFERENCES 子树不进普通 RAG chunk，不因为语义锚点而变）。
    for row in rows:
        if not row["skip_reason"] and _REF_SECTION.match(
                strip_presentation(str(row.get("section_title") or "")).strip()):
            row["skip_reason"] = "ref_section"
    return rows


#: 脚注行标记（页内 `---` 之后的行，由 `mark_footnote_rows` 打上）：
#: 论文底部那几条批注不进正文 chunk（用户口径 2026-12-27「支持去除页面底部的批注」）。
FOOTNOTE_SKIP = "footnote"


def mark_footnote_rows(rows: list[dict]) -> int:
    """把**页内 `---` 之后、下一个标题之前**的行标成 `skip_reason=footnote`。

    为什么要它：脚注（`*Equal contribution…` / `†Work performed…` / 会议行）混进正文块，
    检索时会以"正文证据"的身份被召回 —— 它们既不是论点也不是方法，只是版面补充。
    判据只认转录里那条 `---`（模型只在脚注分隔线处写它），不靠"以星号开头"这种启发式；
    与前端 `.row-pair.is-footnote` 用的是同一套规则（读模型里那行就是脚注）。
    返回标了多少行，供索引报告用。
    """
    marked = 0
    after: dict[int, bool] = {}
    for row in rows:
        page = int(row.get("page") or 0)
        text = str(row.get("original") or "").strip()
        if re.fullmatch(r"-{3,}", text):
            after[page] = True
            continue
        if not after.get(page):
            continue
        if row.get("type") in ("heading", "formula"):
            after[page] = False
            continue
        if not row.get("skip_reason"):
            row["skip_reason"] = FOOTNOTE_SKIP
            marked += 1
    return marked


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
    # `semantic_break_sides` 是"两路都标了"时的完整集合（见 reader_service 的合成）；
    # 没有这个字段的旧产物退回单值口径 —— 一条行两路都标时单值只能记住一个，
    # 实测 turboquant 的 A 路因此从 63 个边界掉到 38 个（B 路那次覆盖写把它抹了）。
    sides = row.get("semantic_break_sides")
    if sides:
        return SIDE_OF_ROAD[road] in sides
    return row.get("semantic_break_side") == SIDE_OF_ROAD[road]


def spans_break(items: list[dict], road: str | None = None) -> bool:
    """这段行**内部**（不含首行）有没有本路断点 —— 有就说明它不能整段成一块。"""
    return any(is_break(row, road) for row in items[1:])


def split_at_breaks(items: list[dict], road: str | None = None) -> list[list[dict]]:
    """按**本路**断点把一段连续行切开：断点所在行恒为下一片的开头。

    没有断点时原样返回一片 —— 「模型没给锚点」的论文（以及 "use_breaks=False" 的对照臂）
    行为与改造前**逐字一致**，这条兜底是 "break_walls" 能默认打开的前提。
    """
    pieces: list[list[dict]] = []
    cur: list[dict] = []
    for row in items:
        if cur and is_break(row, road):
            pieces.append(cur)
            cur = []
        cur.append(row)
    if cur:
        pieces.append(cur)
    return pieces


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
             use_breaks: bool = True, break_walls: bool = True,
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

    外加一条**硬约束**（``break_walls``，默认开）：**块的边界必须是本路的语义断点** ——
    任何一条 chunk 内部（首行之外）都不许出现断点行。没有这条约束，"语义分块"只是一句
    口号：实测同一份文本只切 ``use_breaks`` 开/关，四组块集合**一字不差**（锚点只把
    单元切细了 2~2.6 倍，随后又被 target 贪心合并回去）。模型没给锚点时 ``spans_break``
    恒为 False，装配路径与改造前逐字一致。
    """
    # 断点硬边界实际生效的开关：没有锚点的对照臂（use_breaks=False）不许被它改写行为
    walls = bool(break_walls and use_breaks)
    text_rows = [r for r in rows if not r.get("skip_reason") and not _is_structural(r)]
    # 行归属必须在装配前定好（`load()` 已经做过一次）；这里再调一次是幂等的，
    # 保证"装配看到的 section_path"与"校验看到的"是同一份。
    _assign_sections(rows, tree)
    if tree and text_rows and not _locates_rows(tree, text_rows):
        # 树在、但**一个节点都对不上正文行**（目录退回了解析块 id，或老产物缺 heading 行）：
        # 这时每个节点的 row_index 都是 None，_own_rows 一律返回空，pack_node 的
        # 「not subtree → 只递归子节点」会把整篇静默跳完，正文块层**整层为空**，
        # 而调用方只打一行 ERROR 就照常入库（审计 2026-12-04 的 P0）。
        # 处理与"没有目录"完全一致：整篇按一个合成节点切，宁可没有小节边界，也不能没有块。
        log.warning("目录树 %d 个节点没有一个对得上正文行 → 整篇按一个合成节点切", len(_walk(tree)))
        tree = []
    if not tree and text_rows:
        # 没有目录（未翻译 / 缺索引）→ 整篇当一个合成节点，只靠 target/hard 切。
        # 这是**降级**而不是失败：内容与顺序不变，只是没有小节边界。
        tree = _synthetic_tree(text_rows)
    all_nodes = _walk(tree)
    unit_partition = semantic_units(text_rows, use_breaks=use_breaks,
                                    max_chars=unit_max_chars, road=road)
    unit_of_row = {r["index"]: u for u in unit_partition for r in u["rows"]}
    stats = {"nodes": 0, "whole_subtree": 0, "own_chunk": 0, "intro_merged": 0,
             "intro_alone": 0, "break_cuts": 0,
             "unit_split": 0, "row_fallback": 0, "chunks": 0, "units": len(unit_partition),
             # cap 额外切出来的单元数 = 有 cap 与无 cap 的单元数之差（确定性，不必埋计数器）
             "unit_capped": (len(unit_partition)
                             - len(semantic_units(text_rows, use_breaks=use_breaks,
                                                  road=road)))}
    chunks: list[dict] = []

    def chars(items: list[dict]) -> int:
        # 长度判据按**本路**的文本算（B 路看译文字数）
        return sum(len(road_text(r, road)) for r in items)

    def hard_len(items: list[dict]) -> int:
        """按**最终文本**（行之间用空行连接）量长度 —— 与 `validate` 的 `hard` 判据同口径。

        为什么不能复用 `chars`：它只把各行长度相加，**少了 n-1 个 "

"**。实测 17 行的块
        装配算出 2215 ≤ hard(2240)、校验按真文本算出 2247 > hard —— 于是"通过装配、复核超标"
        （turboquant 路 b p017-00，7 个字）。这两个数必须是同一把尺子。
        """
        return len("\n\n".join(road_text(r, road) for r in items))

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
        # 断点是硬边界：整棵子树即使装得进 target，也不能跨过它内部的断点（见 docstring）
        if chars(subtree) <= target and not (walls and spans_break(subtree, road)):
            stats["whole_subtree"] += 1
            emit(subtree, node)
            return

        intro_merged = False
        if own:
            # 节点自己的行（标题 + 引言）内部也可能有断点 → 先按断点切片，每片独立成块。
            # 只有一片时才谈"并进首个子块"：多片意味着这段行本身就是两个语义段。
            pieces = split_at_breaks(own, road) if walls else [own]
            if len(pieces) > 1:
                stats["break_cuts"] += len(pieces) - 1
            if len(pieces) == 1 and chars(own) < floor and node["children"]:
                # 只是"打算并进首个子块"：能不能成还要过下面的硬边界与 hard 两道闸，
                # 所以计数放在真正合上的那一刻（否则统计会把"被拒的合并"也算成合并）。
                intro_merged = True
            else:
                stats["own_chunk"] += len(pieces)
                for piece in pieces:
                    emit(piece, node)
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
            # 按**行序**取第一个候选，且**只取这一个**：紧邻引言的那个块装不下时就放弃合并，
            # 绝不许跳到更远的块 —— 中间那些行在各自己的块里，跳过去合并会造出
            # 「205,206 + 229..242」这种中间有洞的块，校验层报 order_between_chunks
            # （实测 turbo p018，B 路；最终整篇退回不用锚点）。宁可引言单独成块。
            adjacent = sorted((c for c in chunks[mark:] if c["rows"]),
                              key=lambda c: c["rows"][0]["index"])
            for chunk in adjacent:
                first = chunk["rows"][0]["index"]
                if first <= tail:
                    continue
                if walls and is_break(chunk["rows"][0], road):
                    # 合并会把一个断点挤到块内部（块的首行变成引言）→ 宁可引言单独成块
                    break
                if hard_len(own + chunk["rows"]) <= hard:
                    best = (first, chunk)
                break
            if best is not None:
                stats["intro_merged"] += 1
                best[1]["rows"] = own + best[1]["rows"]
            else:
                stats["intro_alone"] += 1
                # 合不进任何子块 → 引言单独成块。这时**追加**即可：`assemble` 最后会按
                # 首行下标整体排序，位置自然正确（以前插在 `mark` 处，在 TOC 顺序 ≠ 行序时是错的）。
                stats["chunks"] += 1
                chunks.append({"rows": own, "node": node})

    for node in tree:
        pack_node(node)

    # 超过 hard 的块按**单元边界**二次切分（单元自身超 hard 才退回行边界）
    out: list[dict] = []
    for chunk in chunks:
        if hard_len(chunk["rows"]) <= hard:
            out.append(chunk)
            continue
        stats["unit_split"] += 1
        pieces, used_rows = pack_units(units_of(chunk["rows"]), target=target, hard=hard,
                                      road=road, break_walls=walls)
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
    if not out and text_rows:
        # **正文块层整层为空必须出声**：有正文行却一个块都没装配出来，等于拿一张空索引去
        # 入库（调用方以前只打一行 ERROR 就照常写库 —— 库里 359 条元数据、检索永远为空的
        # 那种半截状态）。这里直接失败，让建索引任务在任务面板里失败，而不是静默写空索引。
        # 论文本来就没有正文行（纯图片页）时不抛：那种情况 text_rows 是空的。
        raise ValueError(
            f"正文块装配结果为空：正文行 {len(text_rows)} 条，但一个 chunk 都没生成"
            f"（目录节点 {len(_walk(tree))} 个）")
    return out, stats


def pack_units(units: list[dict], *, target: int, hard: int, road: str | None = None,
               break_walls: bool = True) -> tuple[list[list[dict]], bool]:
    """单元序列 → 块的划分（贪心）：累加到 ``target`` 之上即封口，``+下一个 > hard`` 必停。

    返回 ``(块的列表, 是否用到行边界兜底)``。单元自身 > ``hard`` 时按**行边界**兜底
    —— 单行不可拆，所以这条路径只在"某一行本身就超 hard"时才会走到。

    字数一律按**本路**的文本算（`road_text`）：中英字符数差异很大，混用会让其中一路的
    chunk 长度系统性偏大或偏小。

    ``break_walls`` 与 `assemble` 同义：**以断点开头的单元不许并进上一块**（这一层是
    超 hard 的二次切分，正常不会碰到断点；留着是防"某片自身超 hard 后又被重新合并）。
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
        if buf and break_walls and is_break(unit["rows"][0], road):
            flush()                     # 硬边界：断点行恒为块的起点
        # 与 validate 同口径：行之间要算上 "\n\n"（见 assemble.hard_len 的说明）
        unit_size = len("\n\n".join(road_text(r, road) for r in unit["rows"]))
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


def _synthetic_tree(text_rows: list[dict]) -> list[dict]:
    """整篇当一个合成节点（没有可用目录时的降级树：内容与顺序不变，只是没有小节边界）。"""
    return [{"key": "", "number": "", "title": "", "title_zh": "", "level": 1,
             "page": text_rows[0]["page"], "row_id": text_rows[0]["id"],
             "children": [], "parent": None, "path": "",
             "row_index": text_rows[0]["index"]}]


def _locates_rows(tree: list[dict], text_rows: list[dict]) -> bool:
    """这棵树能不能认出行：**至少一个节点**的 row_index 落在正文行的下标里。

    目录有可能是退回解析块 id 得来的（reader_service 里的 _outline_from_index），
    那些 id 与页级产物的行 id 不同源，映射过来全是 None —— 这种树一行都认领不到。
    """
    known = {r["index"] for r in text_rows}
    return any(node.get("row_index") in known for node in _walk(tree))


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


#: 「标题样」文本的长度上限（只有标题、没有正文的那种块）。
HEADING_TEXT_MAX = 120


def is_heading_like(row: dict) -> bool:
    """这一行是不是「标题样」—— 只有标题、没有正文。

    ⚠️ **不能只看 `type == "heading"`**：实测线上还有一批 `## 0.2 PRIOR WORK` 这种行，
    解析侧标的是 ``para``（它们确实是 Markdown 标题，但行类型没跟上），只按类型判会漏掉一半。

    文字的判据同样要小心：**不能用「整段没有句号」**——章节号自带点（`## 0.2`）会被判成正文。
    这里看的是去掉 ``#`` 之后的尾巴：不以句末标点收尾，也不含句号加空格（`… end. Next`）。
    """
    if row.get("type") == "heading":
        return True
    text = road_text(row, None).strip()
    if not text.startswith("#") or len(text) > HEADING_TEXT_MAX:
        return False
    rest = text.lstrip("#").strip()
    if not rest:
        return True
    if "。" in rest or ". " in rest:
        return False
    return rest[-1] not in ".!?！？:：;…"



#: 「标题续行」允许出现在哪些行类型上：只有正文类行才可能是标题被切开的后半截。
#: 表格 / 公式 / 图注**绝不能**因为"短"被当成标题吞掉。
_HEADING_CONT_TYPES = ("para", "text", "list", "title", "heading")


def _same_page(a: dict, b: dict) -> bool:
    try:
        return int(a.get("page") or 0) == int(b.get("page") or 0)
    except (TypeError, ValueError):
        return False


def _heading_tail(text: str) -> bool:
    """这行文字像不像**标题的后半截**：没有句号、没有句间空格、不以句末标点收尾。"""
    rest = text.lstrip("#").strip()
    if not rest or "。" in rest or ". " in rest:
        return False
    return rest[-1] not in ".!?！？:：;…"


def heading_like_ids(rows: list[dict]) -> set[str]:
    """这批行里哪些**只有标题** —— 含"标题被排版切成两块"的**续行**。

    为什么要有这一层：`is_heading_like` 只看一行，判不出这种 ——
    `## 2.2 NEEDLE-IN-A-HAYSTACK` 是一行、后面半截 `（Needle-in-a-Haystack）` 是另一行，
    第二行不带 `#`、也短，单看它是正文；但两条合起来仍然只有一个章节名。
    实测这两条正是线上"点开引用只有一行标题"的残余（用户口径 2026-12-05）。

    判据只加一条：**紧挨着一个标题行**、同一页、**短**、**不像句子**，且行类型是正文类
    （公式 / 表格 / 图注不参与，见 `_HEADING_CONT_TYPES`）。
    行序按传入顺序（调用方给的就是阅读顺序），因此只看前一行。
    """
    out: set[str] = set()
    prev: dict | None = None
    for row in rows or []:
        rid = str(row.get("id") or "")
        if not rid:
            prev = row
            continue
        if is_heading_like(row):
            out.add(rid)
            prev = row
            continue
        prev_head = prev is not None and str(prev.get("id") or "") in out
        if (prev_head and _same_page(prev, row)
                and str(row.get("type") or "") in _HEADING_CONT_TYPES):
            text = road_text(row, None).strip()
            if text and len(text) <= HEADING_TEXT_MAX and _heading_tail(text):
                out.add(rid)
        prev = row
    return out

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


def _merge_key(text: str) -> str:
    """并入锚点的规范化：只留字母数字与汉字（与 `anchor_key` 同一口径，但这里不依赖翻译侧模块）。"""
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(text or "")).lower()


def apply_row_merges(chunks: list[dict], rows: list[dict]) -> int:
    """把 `semantic_merge_into` 指定的两块**并成一个 chunk**（用户口径 2026-12-27）。

    语义：某一行标了 `semantic_merge_into = <另一行的 id>`，表示"这两块属于同一个单元"。
    模型可以指向**前一页**、也可以指向**不连续**的块（两路调用都带完整前文，看得见）。
    做法：按行序把被连起来的 chunk 头尾接成一串（行集合取并集、按行序排列），
    **不跨小节**（TOC 子树不同就不并，记一条告警）—— 与其余不变量保持一致。

    返回合并了多少次，写进索引报告。没有这样的行时一个都不动（存量产物行为不变）。
    """
    if not chunks:
        return 0
    by_row: dict[str, dict] = {}
    by_start: list[tuple[int, str, dict]] = []      # (行下标, 规范化后的一侧文本, 行) 供锚点匹配
    for chunk in chunks:
        for row in chunk.get("rows") or []:
            by_row[str(row.get("id") or "")] = chunk
    for row in rows:
        for side in ("original", "translation"):
            key = _merge_key(str(row.get(side) or ""))
            if key:
                by_start.append((int(row.get("index") or 0), key, row))
    merged = 0
    for row in rows:
        target_text = str(row.get("semantic_merge_text") or "").strip()
        if not target_text:
            continue
        want = _merge_key(target_text)
        hits = [r for i, key, r in by_start
                if key.startswith(want) and i < int(row.get("index") or 0)]
        if len(hits) != 1:                                  # 0 条 = 没定位到；多条 = 太普适，都不猜
            log.info("并入跳过（%s）：%s", "未定位" if not hits else "锚点不唯一", target_text[:40])
            continue
        target_id = str(hits[0].get("id") or "")
        here = by_row.get(str(row.get("id") or ""))
        there = by_row.get(target_id)
        if not here or not there or here is there:
            continue
        if str(here.get("section_key") or "") != str(there.get("section_key") or ""):
            log.info("并入跳过（跨小节）：%s → %s", row.get("id"), target_id)
            continue
        first, second = sorted((here, there), key=lambda c: (c["rows"][0]["index"] if c.get("rows") else 0))
        first["rows"] = sorted(list(first.get("rows") or []) + list(second.get("rows") or []),
                               key=lambda r: r["index"])
        first["key"] = str(first.get("key") or "") + "+" + str(second.get("key") or "")
        if "chars" in first or "chars" in second:
            first["chars"] = int(first.get("chars") or 0) + int(second.get("chars") or 0)
        for r in first["rows"]:
            by_row[str(r.get("id") or "")] = first
        chunks.remove(second)
        merged += 1
    return merged


def build(rows: list[dict], tree: list[dict], *, target: int, hard: int, floor: int,
          use_breaks: bool = True, break_walls: bool = True,
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
                             use_breaks=use_breaks, break_walls=break_walls,
                             unit_max_chars=unit_max_chars, road=road)
    # 模型标的"并入"（跨页 / 不连续）在这里落地：并完再算单元，于是单元也自然跟着合并
    merge_count = apply_row_merges(chunks, rows)
    units = unit_records(rows, chunks, use_breaks=use_breaks,
                         unit_max_chars=unit_max_chars, road=road)
    by_parent: dict[str, list[str]] = {}
    for unit in units:
        by_parent.setdefault(str(unit["parent_chunk_id"]), []).append(unit["unit_id"])
    for chunk in chunks:
        chunk["unit_ids"] = by_parent.get(str(chunk.get("key")), [])
    report = validate(chunks, rows, hard=hard, units=units, road=road)
    stats["merged_chunks"] = merge_count
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
