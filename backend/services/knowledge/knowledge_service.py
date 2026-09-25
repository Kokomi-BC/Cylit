"""知识库服务（产线 C：chunk 化 → 向量化 → 入库）。

规划 §11 / §21.6：chunk 化 → Embedding → 写入 MySQL chunk 元数据 → 写 LanceDB。
检索（向量/词法/混合/重排/RAG 问答）在阶段 14–17 的后续轮次实现；
本模块负责 chunk 与索引链路，保证「删除论文 → 清索引」与「重建索引」可用。

**双语 chunk**：同一个 chunk 出 ``zh`` / ``en`` 两行（chunk id 加 ``:zh`` / ``:en`` 后缀，
``lang`` 进 ``metadata_json``）—— **不改表结构**（不动 MySQL 列，避免 ALTER 迁移）。
理由见 ``build_chunks_for_paper`` 的 docstring。

**公式的两类索引单元（本轮）**：
- *语义 chunk*（``text``）：正文里**内联保留完整公式**（``$$…$$`` 原样、绝不拆开），
  「公式前的定义/说明 + 公式 + 公式后的 where/解释」落在同一条；
- *公式原子 chunk*（``formula``）：一条公式一条，用于公式专项检索与公式裁图。
两类互为补充、不做去重，靠 ``formula_ids`` / ``text_chunk_id`` 双向指认。
公式正文与语言无关，只出 **1 行**（``lang = any``）—— 旧实现 zh/en 各存一份，
实测 76 组正文完全相同的行（占全库 19%）纯属浪费。

**索引层预处理**（作者/单位/邮箱/arXiv/会议行/脚注过滤、Markdown 表现格式清理、
图注去重、表格数字行不当标题）在 ``backend/services/knowledge/preprocess.py``：
只服务知识库，**不动解析产物、不动阅读器视图**，改完只需重建索引（不重新解析）。
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.core.config import get_settings
from backend.core.errors import NotFoundError, ValidationError
from backend.core.logging import get_logger
from backend.core.utils import json_dumps, new_id, now, sha256_text, truncate
from backend.parsing.formula.noise import is_noise_formula
from backend.repositories.mysql.models import Block, Chunk, Paper
from backend.repositories.mysql.session import session_scope
from backend.services.knowledge.preprocess import (attached_caption_ids,
                                                   first_body_index,
                                                   heading_level, is_front_matter,
                                                   is_table_number_heading,
                                                   split_long_text,
                                                   strip_presentation)
from backend.services.knowledge import formula_source
from backend.services.model.model_manager import model_manager
from backend.services.paper import paper_service, reader_service

log = get_logger(__name__)

# 四类 chunk（规划 §10 ⑫ chunk_type）
CHUNK_TYPES = ("text", "formula", "figure", "table")
# 正文语义 chunk 的目标长度（到点即 flush）
TARGET_CHUNK_CHARS = 1400
# 小正文 chunk 的**合并门槛**（不是丢弃门槛：能和相邻正文块合并就合并，
# 合并不了就照样入库 —— 用户口径"尝试与相邻语义块合并"）
MIN_CHUNK_CHARS = 200
# 单个 chunk 的硬上限：append 之前预检 buffer + block，任何正文 chunk 都不得超过
HARD_CHUNK_CHARS = 2240
# 真正的"残渣"丢弃门槛（英文侧）：低于这个长度的正文块才丢
RESIDUE_CHARS = 40
# 双语：同一 chunk 的两种语言入库行（顺序即入库顺序，zh 在前便于人肉核对）
LANGS = ("zh", "en")
# 语言无关（公式）：只出 1 行、zh/en 检索都要能召回
ANY_LANG = "any"
# 中文侧的最小长度门槛按**字**算：中文 1 字的信息量约等于英文 2~3 字符，沿用 40 字符
# 会把大量正常的中文块误杀（实测把 "## B.2 QJL：1 比特内积量化" 这类小节标题全丢了）。
# 门槛只用来兜"取到的是残渣"，不承担质量判断 —— 对不上译文的行压根不会出（见 _lookup_zh）。
MIN_ZH_CHARS = 8

_WS_RE = re.compile(r"\s+")
# 只保留小写字母 / 数字 / 汉字
_COMPACT_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
# 页内对齐的最小可信重叠（签名字符数）
_MIN_ALIGN_CHARS = 12


def _setting_int(key: str, default: int) -> int:
    """读 setting 表里的整数参数（读不到/坏值一律回退默认）。

    为什么要落成 setting：粒度是**要按真实检索效果反复调**的参数，改一次不该动代码；
    但改完必须**重建索引**才生效（chunk 是建出来的，不是查出来的）。
    """
    try:
        from backend.repositories.mysql.models import Setting

        with session_scope() as sess:
            row = sess.get(Setting, key)
        if row is None or row.value is None:
            return default
        return int(float(str(row.value).strip().strip('"')))
    except Exception:  # noqa: BLE001 —— 参数读不到不影响建索引
        return default


def chunk_params() -> tuple[int, int, int]:
    """``(target, min, hard)``；setting 键：``kb.chunk_target_chars`` 等。"""
    target = max(200, _setting_int("kb.chunk_target_chars", TARGET_CHUNK_CHARS))
    floor = max(RESIDUE_CHARS, _setting_int("kb.chunk_min_chars", MIN_CHUNK_CHARS))
    hard = max(target, _setting_int("kb.chunk_hard_chars", HARD_CHUNK_CHARS))
    return target, floor, hard


def _block_sig(text: str) -> str:
    """块匹配签名：只留小写字母 / 数字 / 汉字。

    为什么这么狠：**入库的原文来自解析产物**（``md/full.md``），**译文来自页级产物**
    （``pages.json`` 的 ``original_md`` / ``translation_md``，由视觉模型按页图转写）——
    同一页的两次转写，标题层级符（``#`` vs ``##``）、冒号后空格、LaTeX 里的空格
    都会有差异。第一版只压空白，在真实论文上正文块只有 **52%** 能对上（实测），
    换成"只留字母数字汉字"后这些差异全部消失，块与块之间仍然区分得开。
    """
    return _COMPACT_RE.sub("", (text or "").lower())


def _zh_index(pages: list[dict]) -> dict[int, list[tuple[str, str]]]:
    """``{页号: [(行签名, 译文), …]}``：页级翻译产物的译文索引（保持行序）。

    为什么不直接建 ``{(页号, 签名): 译文}`` 字典就完事：解析块与译文行的**切分边界
    不一样**（同一个段落，解析切成"…quantizer's output." + "Thus, we aim…"，页级产物
    切成一整行）。实测只按签名精确匹配，正文块覆盖率只有 55% —— 剩下的不是"没翻译"，
    而是**切分不同**。所以索引保留整页的行序列，由 `_lookup_zh` 做页内对齐。
    """
    out: dict[int, list[tuple[str, str]]] = {}
    for page in pages or []:
        try:
            pno = int(page.get("page") or 0)
        except (TypeError, ValueError):
            continue
        rows: list[tuple[str, str]] = []
        for row in page.get("rows") or []:
            src = _block_sig(row.get("original"))
            dst = str(row.get("translation") or "").strip()
            if src and dst:
                rows.append((src, dst))
        if rows:
            out[pno] = rows
    return out


def _overlap(a: str, b: str) -> int:
    """两个签名的最长公共子序列长度（用 difflib，字符级，够用且稳）。"""
    return _overlap_span(a, b)[0]


def _overlap_span(a: str, b: str) -> tuple[int, int, int]:
    """``(公共字符数, 在 b 中的起始位置, 结束位置)``。"""
    from difflib import SequenceMatcher

    matcher = SequenceMatcher(None, a, b, autojunk=False)
    total, lo, hi = 0, -1, -1
    for tag, _, _, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            continue
        total += j2 - j1
        lo = j1 if lo < 0 else min(lo, j1)
        hi = max(hi, j2)
    return total, lo, hi


_SENT_END = "。！？；\n"
_SENT_END_ASCII = ".!?;"


def _snap_sentence(text: str, pos: int, *, forward: bool) -> int:
    """把比例切点吸附到最近的句子边界（找不到就原样返回）。"""
    n = len(text)
    pos = max(0, min(n, pos))
    step = 1 if forward else -1
    i = pos
    while 0 < i < n:
        ch = text[i - 1]
        if ch in _SENT_END or (ch in _SENT_END_ASCII and (i >= n or text[i] in " \n")):
            return i
        i += step
    return pos


def _slice_zh(text: str, lo: float, hi: float) -> str:
    """按比例切一段译文，切点吸附到句子边界。

    为什么需要：解析块比页级行**细**（同一段被切成两三个块，页级产物只有一整行），
    不切的话同一个块会拿到整行译文 —— 实测 turboquant 有 21/56 个中文 chunk 的
    token 数是英文侧的两倍以上、还有 6 条内容完全重复（同一段中文被塞进多个 chunk）。
    译文与原位序一致，所以按"该段在行内的字符占比"切，再吸附到最近句号，
    既不重复也不留空档。
    """
    n = len(text)
    if n <= 1:
        return text
    edge = max(3, int(0.05 * n))
    raw_lo, raw_hi = int(round(lo * n)), int(round(hi * n))
    # 贴边的直接用整段边界：否则会从"向|量化"这种词中间切进去（实测发生过）
    start = 0 if raw_lo <= edge else _snap_sentence(text, raw_lo, forward=False)
    end = n if raw_hi >= n - edge else _snap_sentence(text, raw_hi, forward=True)
    if end <= start:
        start, end = raw_lo, raw_hi
    piece = text[start:end].strip()
    return piece or text.strip()


def _lookup_zh(zh: dict[int, list[tuple[str, str]]], page: int, text: str) -> str | None:
    """取某段原文的中文：先精确签名，再在**本页内**做字符级对齐取重叠最多的行。

    为什么是"本页内 + 取最优行"：跨页对齐毫无必要（同一页的行序天然单调），
    而重叠最多的一行就是这段原文所在的行 —— 即使两侧切分不同也不会配到别的小节去。
    门槛（重叠 ≥ 12 字符且 ≥ 较短一侧的一半）保证**宁可漏配也不错配**；
    命中后若"行比这段大得多"（行粗、块细），按比例切片而不是整行端走。
    """
    rows = zh.get(page)
    sig = _block_sig(text)
    if not rows or not sig:
        return None
    for row_sig, dst in rows:
        if row_sig == sig:
            return dst
    if len(sig) < _MIN_ALIGN_CHARS:
        return None
    best: tuple[int, str, str, int, int] | None = None
    for row_sig, dst in rows:
        hit, j1, j2 = _overlap_span(sig, row_sig)
        if best is None or hit > best[0]:
            best = (hit, dst, row_sig, j1, j2)
    if best is None:
        return None
    hit, dst, row_sig, j1, j2 = best
    if hit < _MIN_ALIGN_CHARS or hit * 2 < min(len(sig), len(row_sig)):
        return None
    if len(row_sig) <= len(sig) * 1.2 or j1 < 0:   # 行不比这段大多少 → 整行端走
        return dst
    return _slice_zh(dst, j1 / len(row_sig), j2 / len(row_sig))


def _zh_content(parts: list[tuple], zh: dict[int, list[tuple[str, str]]]) -> str | None:
    """按块拼中文正文；**公式片段原样插入**（LaTeX 无语言之分，两侧同串）。

    ``parts`` 的元素是 ``(页号, 文本, kind)``（``kind`` 缺省视为 ``text``，兼容旧数据）。
    """
    if not parts:
        return None
    pieces: list[str] = []
    text_parts = 0
    for item in parts:
        page, text = item[0], item[1]
        kind = item[2] if len(item) > 2 else "text"
        if kind == "formula":
            pieces.append(text)          # 公式：不查译文，直接原样保留
            continue
        text_parts += 1
        got = _lookup_zh(zh, page, text)
        if got:
            pieces.append(got)
    # 相邻块共用一个译文行（切分不同时的正常现象）→ 去重，别把同一段中文塞两次
    deduped: list[str] = []
    for piece in pieces:
        if not deduped or deduped[-1] != piece:
            deduped.append(piece)
    if not deduped:
        return None
    if text_parts and len(deduped) < len(parts):
        log.debug("中文侧只对上 %d/%d 个片段（其余未翻译或切分不同）",
                  len(deduped), len(parts))
    return "\n\n".join(deduped)


def _variant_row(c: dict, lang: str, content: str) -> dict:
    """一条入库行（``:zh`` / ``:en`` / ``:any``）。"""
    meta = {**(c.get("metadata") or {}), "lang": lang}
    if lang != ANY_LANG:
        meta["pair_id"] = c["id"]
    return {
        **{k: v for k, v in c.items() if not k.startswith("_")},
        "id": f"{c['id']}:{lang}",
        "lang": lang,
        "content": content,
        "content_hash": sha256_text(content),
        "content_preview": truncate(content, 200),
        "token_count": _approx_tokens(content),
        "metadata": meta,
    }


def _chunk_variants(base: list[dict], zh: dict[int, list[tuple[str, str]]],
                    langs: tuple[str, ...]) -> list[dict]:
    """把「单语 chunk」展开成语言行。

    - **公式原子 chunk 只出 1 行**（``lang = any``）：正文是 LaTeX，与语言无关。
      旧实现 zh/en 各出一行、正文完全相同（实测 76 组冗余行）—— 语言过滤那边
      （``retrieval._filter_rows`` / ``/knowledge/chunks``）把 ``any`` 视为"两种语言都通过"，
      所以中文、英文检索都召得回，中/英文 RAG 上下文也都能直接用这段公式。
    - 正文 / 图注块：译文缺失就**不出 zh 行**（不拿原文冒充译文，否则索引里会出现
      "中文行其实是英文"的脏数据）。
    """
    out: list[dict] = []
    for c in base:
        if c["chunk_type"] == "formula":
            out.append(_variant_row(c, ANY_LANG, c["content"]))
            continue
        parts: list[tuple] = c.get("_parts") or []
        for lang in langs:
            if lang == "en":
                content = c["content"]
            else:
                content = _zh_content(parts, zh)
                if not content:
                    continue                  # 还没翻译 → 不出 zh 行
            # 残渣门槛只对正文用；中文侧按字数另算
            floor = RESIDUE_CHARS if lang == "en" else MIN_ZH_CHARS
            if c["chunk_type"] == "text" and len(content) < floor:
                continue
            out.append(_variant_row(c, lang, content))
    return out


# --------------------------------------------------------------------------
# Chunk 化（规划 §21.6 步骤 1）
# --------------------------------------------------------------------------


def build_chunks_for_paper(paper_id: str, *,
                           langs: tuple[str, ...] = LANGS) -> list[dict]:
    """按 IR + Markdown 生成四类 chunk，并展开成 zh/en 双语行。

    为什么要双语：库内正文的事实源是**页级产物**（``pages.json`` 有原文 + 译文），
    而中文提问与英文原文之间隔着一次跨语检索 —— 模型的跨语能力能用，但实测（§17.33）
    中文问句对中文段落的相似度明显更"实"；反之英文术语（MSE、inner product）在英文行上
    更准。两行都入库、由重排统一裁决，是召回最稳的做法。

    **不改表结构**：chunk id 加 ``:zh`` / ``:en`` 后缀，``lang`` 只写进 ``metadata_json``
    （MySQL ``chunk`` 表不加列，省一次 ALTER 迁移）。
    """
    return _chunk_variants(_collect_chunks(paper_id),
                           _zh_index(reader_service.page_translations(paper_id)),
                           langs)


def _formula_chunk_text(f: dict, *, latex: str | None = None) -> str | None:
    """公式 chunk 的正文；**不是数学的返回 `None`**（那条 chunk 就不该存在）。

    为什么单独判一道：解析阶段把页脚的 `∗†` 认成了公式（小而符号密度高），
    于是知识库里出现 `公式 attentionisa-p01-b06：* †` 这种条目（还带一张裁图）——
    用户报过。解析侧已经修了，这里再兜一层：**旧产物（IR）不用重新解析也能干净**，
    重建索引时这类条目会自然消失（`_collect_chunks` 返回 None 就跳过）。

    ``latex`` 由调用方给出（**换源后是页级视觉转写的完整版**，见 `formula_source`）：
    解析版碎片化严重（p20 一条公式被拆成 5 块），只取 `latex_final` 会只剩一小段。
    """
    body = (latex or formula_source.parsed_latex(f)).strip()
    cands = f.get("latex_candidate") or []
    if not body:
        body = (cands[0] if cands else "") or (f.get("raw_text") or "")
    if is_noise_formula(body) and is_noise_formula(f.get("raw_text")):
        return None
    return f"公式 {f.get('block_id')}：{body}"


def _collect_chunks(paper_id: str) -> list[dict]:
    """单语（原文）chunk 序列：正文语义 chunk + 公式原子 chunk（+ 图/表 chunk）。

    与旧实现的差别（都是"索引层"改动，不重新解析也能生效）：
    1. 前沿元数据（作者/单位/邮箱/arXiv 章/会议行/脚注）不进库；
    2. Markdown 表现格式清理，公式与引用编号保留；
    3. 图注块若已被 figure/table 吸收，不再进正文 buffer（避免同一段图注索引两遍）；
    4. 公式**不再切断正文**：以 ``$$…$$`` 原样进 buffer，同时另出一条公式原子 chunk；
    5. 每个 chunk 带 ``section`` / ``section_path``（从最近的 heading 继承）；
    6. 表格数字行不再当标题；
    7. append 前预检硬上限；超长正文块按句子边界二次切分；
    8. 小于合并门槛的正文 chunk 尝试与相邻正文块合并（不看标题/公式/图/表）。
    """
    md_map = reader_service.load_markdown_map(paper_id)
    from backend.parsing.ir.ir_reader import IRReader

    ir = IRReader(paper_service.paper_paths(paper_id)["ir"])
    formulas = {f["block_id"]: f for f in ir.formulas()}
    figures = {f["block_id"]: f for f in ir.figures()}
    tables = {t["block_id"]: t for t in ir.tables()}
    caption_of = {str(x.get("caption_block_id"))
                  for x in list(figures.values()) + list(tables.values())
                  if x.get("caption_block_id")}

    with session_scope() as sess:
        blocks = (sess.query(Block).filter(Block.paper_id == paper_id)
                  .order_by(Block.order_global.asc()).all())
        rows = [_block_row(b, md_map) for b in blocks]
        paper = sess.get(Paper, paper_id)
        authors = (paper.authors or "") if paper is not None else ""

    target, floor, hard = chunk_params()
    body_start = first_body_index(rows)
    skip_captions = attached_caption_ids(rows, caption_of)
    # 公式 LaTeX 的来源选择（解析产物残缺 → 换成页级视觉转写的完整版）
    f_source = formula_source.assign_formulas(rows, formulas, paper_id)

    chunks: list[dict] = []
    buffer: list[dict] = []          # 语义 chunk 的组成块（正文 + 内联公式）
    buffer_chars = 0
    section_stack: list[tuple[int, str]] = []

    def current_section() -> tuple[str, str]:
        if not section_stack:
            return "", ""
        return section_stack[-1][1], " / ".join(t for _, t in section_stack)

    def entry(kind: str, b: dict, text: str) -> dict:
        sec, path = current_section()
        return {"kind": kind, "id": b["id"], "page": b["page"], "order": b["order"],
                "type": b.get("type") or "", "text": text,
                "section": sec, "section_path": path,
                "section_kind": b.get("section_kind") or "body"}

    def flush_text() -> None:
        nonlocal buffer, buffer_chars
        entries = [e for e in buffer if e.get("text")]
        buffer, buffer_chars = [], 0
        if not entries or not any(e["kind"] == "text" for e in entries):
            # 只有公式的 buffer 不建正文 chunk：公式已有原子 chunk，避免重复行
            return
        text = "\n\n".join(e["text"] for e in entries)
        chunks.append(_make_chunk(
            paper_id, "text", text, entries[0], entries[-1],
            section=entries[0]["section"] or "",
            section_path=entries[0]["section_path"] or "",
            parts=[(e["page"], e["text"], e["kind"]) for e in entries],
            heading_only=all(e["type"] == "heading" for e in entries),
            metadata={"block_ids": [e["id"] for e in entries],
                      "pages": sorted({e["page"] for e in entries}),
                      "section_kind": entries[0]["section_kind"],
                      "formula_ids": [e["id"] for e in entries
                                      if e["kind"] == "formula"]},
        ))

    def push_section(text: str, level: int) -> None:
        lv = level if level > 0 else 2
        while section_stack and section_stack[-1][0] >= lv:
            section_stack.pop()
        section_stack.append((lv, (text or "")[:128]))

    for idx, b in enumerate(rows):
        bid = b["id"]
        raw = b["text"] or ""
        text = strip_presentation(raw)
        if b["type"] == "ref":
            continue
        if is_front_matter(raw, page_no=b["page"], before_body=idx < body_start,
                           authors=authors):
            log.debug("跳过前沿元数据块：%s（%r）", bid, raw[:40])
            continue
        if bid in skip_captions:
            continue

        if b["type"] == "heading" and not is_table_number_heading(text):
            flush_text()
            push_section(text, heading_level(raw))
            if text:
                buffer.append(entry("text", b, text))
                buffer_chars += len(text) + 2
            continue

        if bid in formulas:
            f = formulas[bid]
            src = f_source.get(bid) or {}
            # 完整公式只出一条原子 chunk：同一条页级公式被解析切成 N 个碎片时，
            # 只在**第一个碎片**处出 chunk，其余碎片并进它的 `block_ids`（否则同一段公式
            # 会在库里出现 N 条内容完全一样的行 —— 就是之前 formula zh/en 重复那个毛病）。
            if src.get("group_first") is False:
                continue
            latex = src.get("latex") or ""
            atomic = _formula_chunk_text(f, latex=latex)
            if atomic is None:                    # 纯脚注符号，不是公式 → 整条跳过
                log.info("跳过非数学公式 chunk：%s（%r）", bid, f.get("raw_text"))
                continue
            inline = f"$$ {latex} $$" if latex else \
                strip_presentation(((md_map or {}).get(bid) or {}).get("text") or "")
            if not inline:
                inline = atomic
            # 公式块**不拆**：超上限也只 flush，绝不切公式内部
            if buffer and buffer_chars + len(inline) + 2 > hard:
                flush_text()
            sec, path = current_section()
            buffer.append(entry("formula", b, inline))
            buffer_chars += len(inline) + 2
            parsed = formula_source.parsed_latex(f)
            # 短碎片（`\times 10`、`b\cdot d`、`j=1`…）且没配上页级完整公式：
            # 多半是图内坐标标签或行内小片段，**不单独出"公式"条目**（用户搜"量化公式"
            # 搜出一堆 `\times 10` 就是这种）。上面已经把它内联进正文语义 chunk 了，
            # 所以正文内容一点不丢 —— 只是不再在"公式"分类里占一条。
            if src.get("source") != "page_vision" and formula_source.is_fragment(latex):
                log.debug("短碎片不单独出公式条目：%s（%r）", bid, latex)
                continue
            meta = {"latex": latex or parsed, "grade": f.get("grade"),
                    "raw_text": f.get("raw_text"), "crop": f.get("image_path"),
                    "section_kind": b.get("section_kind") or "body",
                    "latex_source": src.get("source") or "parsed",
                    "latex_parsed": parsed,
                    "latex_candidates": list(f.get("latex_candidate") or []),
                    "needs_review": bool(src.get("needs_review")),
                    # 锚点：解析块本身锚点精确（用户口径第 2 条）
                    "anchor_type": "formula_block", "anchor_precision": "exact",
                    # 编号单独进 metadata（用户口径第 4 条）
                    "formula_number": src.get("formula_number")}
            if src.get("source") == "page_vision":
                meta["align_score"] = src.get("score")
            if src.get("block_ids"):
                meta["block_ids"] = src["block_ids"]
            chunks.append(_make_chunk(
                paper_id, "formula", atomic, b, b, section=sec or "",
                section_path=path or "", metadata=meta,
            ))
            continue

        if bid in figures or bid in tables:
            flush_text()
            source, kind, label = ((figures, "figure", "插图") if bid in figures
                                   else (tables, "table", "表格"))
            item = source[bid]
            caption = item.get("caption") or ""
            sec, path = current_section()
            chunks.append(_make_chunk(
                paper_id, kind, f"{label} {bid}：{caption}", b, b,
                section=sec or "", section_path=path or "",
                # 图注本身是可翻译的正文块 → 拿它的签名去译文索引里取中文图注
                parts=[(b["page"], caption, "text")] if caption else [],
                metadata={"caption": caption, "image_path": item.get("image_path"),
                          "section_kind": b.get("section_kind") or "body"},
            ))
            continue

        if not text:
            continue
        for piece in split_long_text(text, hard):
            if buffer and buffer_chars + len(piece) + 2 > hard:
                flush_text()
            buffer.append(entry("text", b, piece))
            buffer_chars += len(piece) + 2
            if buffer_chars >= target:
                flush_text()
    flush_text()

    # 解析器漏掉的**编号展示公式**：页级 md 里有、解析侧没有对应块 → 补一条合成条目
    # （用户口径第 1/3/4 条：synthetic=true / formula_source=page_md / 同页最近块作锚点）。
    chunks.extend(_synthetic_formula_chunks(paper_id, rows, formulas, f_source))
    chunks.sort(key=lambda c: (c["order_global"],
                               0 if c["chunk_type"] == "text" else 1))
    merged = _merge_small_chunks(chunks, min_chars=floor, hard=hard)
    return _link_formulas(merged)


def _synthetic_formula_chunks(paper_id: str, rows: list[dict], formulas: dict[str, dict],
                              assignments: dict[str, dict]) -> list[dict]:
    """给"页级有、解析侧漏掉"的编号展示公式建公式条目。

    它们**没有解析块 id**，所以：
    - chunk id 用合成前缀 ``{paper}:formula:syn-p{页:02d}-v{序号:02d}``（唯一、可追溯）；
    - ``block_id`` 存**同页最近解析块**的 id —— 知识库页的「查看原文位置」靠它跳转，
      精度由 ``anchor_precision = approximate`` 如实标注（找不到块就退 ``page`` / ``page``）。
    """
    try:
        items = formula_source.synthetic_formulas(rows, formulas, paper_id, assignments)
    except Exception as exc:  # noqa: BLE001 —— 补漏失败不该让整篇建索引挂掉
        log.warning("补漏公式失败（%s）：%s", paper_id, exc)
        return []
    out: list[dict] = []
    for it in items:
        anchor_id = it.get("block_id") or ""
        first = {"id": anchor_id or f"syn-p{it['page']:02d}", "page": it["page"],
                 "order": it.get("order") or 0}
        cid = f"{paper_id}:formula:syn-p{it['page']:02d}-v{it['vision_index']:02d}"
        content = f"公式 {cid.rsplit(':', 1)[-1]}：{it['latex']}"
        out.append(_make_chunk(
            paper_id, "formula", content, first, first,
            section="", section_path="",
            metadata={
                "latex": it["latex"], "latex_parsed": None,
                "latex_source": "page_vision", "formula_source": "page_md",
                "synthetic": True, "formula_number": it.get("formula_number"),
                "page": it["page"], "block_id": anchor_id or None,
                "anchor_type": it["anchor_type"],
                "anchor_precision": it["anchor_precision"],
                "needs_review": False,
                **({"also_on_pages": it["also_on_pages"]} if it.get("also_on_pages") else {}),
            },
            chunk_id=cid))
    if out:
        log.info("补漏公式 %d 条（%s）：%s", len(out), paper_id,
                 [c["id"].rsplit(":", 1)[-1] for c in out])
    return out


def _link_formulas(chunks: list[dict]) -> list[dict]:
    """给公式原子 chunk 补 ``text_chunk_id``：它被内联在哪条语义 chunk 里。

    关系是双向的：语义 chunk 的 ``metadata.formula_ids`` 列出它包含的公式块，
    公式 chunk 反过来指回语义 chunk（合并/切分之后再算，所以不会指错）。
    """
    owner: dict[str, str] = {}
    for c in chunks:
        if c["chunk_type"] != "text":
            continue
        for fid in (c.get("metadata") or {}).get("formula_ids") or []:
            owner.setdefault(fid, c["id"])
    for c in chunks:
        if c["chunk_type"] != "formula":
            continue
        target = owner.get(c["block_id"])
        if target:
            c.setdefault("metadata", {})["text_chunk_id"] = target
    return chunks


def _mergeable(a: dict, b: dict, hard: int) -> bool:
    """两条正文 chunk 能否合并：同小节、都不是纯标题、合起来不超硬上限。"""
    if a.get("chunk_type") != "text" or b.get("chunk_type") != "text":
        return False
    if a.get("_heading_only") or b.get("_heading_only"):
        return False
    if (a.get("_section_path") or "") != (b.get("_section_path") or ""):
        return False
    return len(a["content"]) + len(b["content"]) + 2 <= hard


def _merge_chunks(a: dict, b: dict) -> dict:
    """把两条正文 chunk 合成一条（保持块的原始次序与译文对齐 parts）。"""
    first, last = a["block_id"], b["end_block_id"]
    cid = (f"{a['paper_id']}:text:{first}" if first == last
           else f"{a['paper_id']}:text:{first}-{last}")
    content = a["content"].rstrip() + "\n\n" + b["content"].lstrip()
    meta = {**(a.get("metadata") or {})}
    meta["block_ids"] = list(a["metadata"].get("block_ids") or []) + \
        list(b["metadata"].get("block_ids") or [])
    meta["pages"] = sorted(set(a["metadata"].get("pages") or []) |
                           set(b["metadata"].get("pages") or []))
    meta["formula_ids"] = list(a["metadata"].get("formula_ids") or []) + \
        list(b["metadata"].get("formula_ids") or [])
    return {
        **a, "id": cid, "content": content,
        "end_block_id": b["end_block_id"],
        "order_global": min(a["order_global"], b["order_global"]),
        "content_hash": sha256_text(content),
        "content_preview": truncate(content, 200),
        "token_count": _approx_tokens(content),
        "metadata": meta,
        "_parts": list(a.get("_parts") or []) + list(b.get("_parts") or []),
    }


def _merge_small_chunks(chunks: list[dict], *, min_chars: int, hard: int) -> list[dict]:
    """把不足 ``min_chars`` 的正文 chunk 并进相邻正文块（优先往后并）。

    为什么优先往后：小块多是"小节的第一段/标题后紧跟的一句"，往后并保持阅读顺序；
    往后不行（后一条是公式/图/表或跨小节）才试往前。
    合并不了就原样保留 —— 这是**合并门槛**，不是丢弃门槛（用户口径：尝试合并）。
    """
    out: list[dict] = []
    pending: dict | None = None
    for c in chunks:
        if pending is not None:
            if _mergeable(pending, c, hard):
                out.append(_merge_chunks(pending, c))
                pending = None
                continue
            if out and _mergeable(out[-1], pending, hard):
                out[-1] = _merge_chunks(out[-1], pending)
            else:
                out.append(pending)
            pending = None
        if (c.get("chunk_type") == "text" and not c.get("_heading_only")
                and len(c["content"]) < min_chars):
            pending = c
        else:
            out.append(c)
    if pending is not None:
        if out and _mergeable(out[-1], pending, hard):
            out[-1] = _merge_chunks(out[-1], pending)
        else:
            out.append(pending)
    out.sort(key=lambda c: (c["order_global"],
                            0 if c["chunk_type"] == "text" else 1))
    return out


def _block_row(b: Block, md_map: dict | None = None) -> dict:
    import json

    meta = {}
    try:
        meta = json.loads(b.meta_json or "{}")
    except ValueError:
        meta = {}
    # 正文只在 Markdown（规划 §8 铁律），MySQL 里不存正文
    text = ((md_map or {}).get(b.id, {}) or {}).get("text") or b.preview or ""
    return {"id": b.id, "page": b.page_no, "type": b.type, "order": b.order_global,
            "text": text, "hash": b.content_hash,
            "section": meta.get("section_title") or b.section_id or "",
            # `section_kind` 由 parser 的状态机标注（body / refs / appendix）：
            # 附录内容**要进知识库**，但要能认出来（检索侧将来可降权/过滤）
            "section_kind": meta.get("section_kind") or "body"}


def _make_chunk(paper_id: str, chunk_type: str, content: str, first: dict,
                last: dict, *, section: str = "", section_path: str = "",
                metadata: dict | None = None, parts: list[tuple] | None = None,
                heading_only: bool = False, chunk_id: str | None = None) -> dict:
    cid = chunk_id or f"{paper_id}:{chunk_type}:{first['id']}"
    if not chunk_id and chunk_type == "text" and first["id"] != last["id"]:
        cid = f"{paper_id}:text:{first['id']}-{last['id']}"
    meta = dict(metadata or {})
    if section_path:
        meta["section_path"] = section_path
    if chunk_type == "text" and heading_only:
        meta["heading_only"] = True
    return {
        "id": cid, "paper_id": paper_id, "chunk_type": chunk_type,
        "page_no": first["page"], "order_global": first["order"],
        "block_id": first["id"], "end_block_id": last["id"],
        "section": section, "content": content,
        "content_hash": sha256_text(content),
        "content_preview": truncate(content, 200),
        "token_count": _approx_tokens(content),
        "metadata": meta,
        "_parts": parts or [],
        "_heading_only": heading_only,
        "_section_path": section_path,
    }


def _approx_tokens(text: str) -> int:
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    return int(cjk / 1.5 + (len(text) - cjk) / 4) + 1


# --------------------------------------------------------------------------
# 入库（规划 §21.6 步骤 2–5）
# --------------------------------------------------------------------------


def index_paper(paper_id: str, *, rebuild: bool = False,
                embed: bool = True, progress=None) -> dict:
    """EMBED + INDEX：写 MySQL chunk 元数据 + 生成向量 + 写 LanceDB。"""
    paper_service.get_paper(paper_id)
    chunks = build_chunks_for_paper(paper_id)
    if not chunks:
        raise ValidationError(
            f"没有可用 chunk：{paper_id} 是否尚未解析？")

    cfg = model_manager.get_config(None, "embedding")
    model_id = cfg.get("id")
    dim = (cfg.get("parameters") or {}).get("dim")

    # 索引版本号（换模型即重建，规划 §11.3）
    with session_scope() as sess:
        row = sess.get(Paper, paper_id)
        prev = sess.query(Chunk).filter(Chunk.paper_id == paper_id).first()
        index_version = int(prev.index_version or 0) + 1 if prev else 1
        sess.query(Chunk).filter(Chunk.paper_id == paper_id).delete()
        for c in chunks:
            sess.add(Chunk(
                id=c["id"], paper_id=paper_id, block_id=c["block_id"],
                chunk_type=c["chunk_type"], page_no=c["page_no"],
                order_global=c["order_global"], section=c["section"],
                content_preview=c["content_preview"],
                content_hash=c["content_hash"], token_count=c["token_count"],
                index_version=index_version, embedding_model_id=model_id,
                metadata_json=json_dumps(c["metadata"]),
            ))
        if row is not None:
            # **建索引即纳入知识库**（用户口径：两个动作合并，不再有单独的"纳入"开关）
            row.in_knowledge_base = True

    embedded = 0
    vector_error = None
    if embed:
        try:
            provider = model_manager.get_embedding()
            batch = int((cfg.get("parameters") or {}).get("batch") or 32)
            vectors: list[list[float]] = []
            for i in range(0, len(chunks), batch):
                part = chunks[i:i + batch]
                vectors.extend(provider.embed([c["content"] for c in part]))
                if progress:
                    progress(min(i + batch, len(chunks)), len(chunks))
            dim = len(vectors[0]) if vectors else dim
            model_manager.remember_dim(cfg, dim)
            from backend.repositories.vector.lancedb_store import vector_store

            vector_store.upsert_chunks(chunks, vectors, index_version,
                                       embedding_model_id=model_id)
            embedded = len(vectors)
        except Exception as exc:  # noqa: BLE001
            vector_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            log.warning("向量入库失败（MySQL chunk 元数据已写入）：%s", exc)

    return {
        "paper_id": paper_id, "chunks": len(chunks),
        "embedded": embedded, "index_version": index_version,
        "model_id": model_id, "dim": dim,
        "by_type": _count_by_type(chunks),
        "by_lang": _count_by_lang(chunks),
        "vector_error": vector_error,
        "rebuilt": rebuild,
        "at": now().isoformat(timespec="seconds"),
    }


def _count_by_type(chunks: list[dict]) -> dict:
    out: dict[str, int] = {}
    for c in chunks:
        out[c["chunk_type"]] = out.get(c["chunk_type"], 0) + 1
    return out


def _count_by_lang(chunks: list[dict]) -> dict:
    out: dict[str, int] = {}
    for c in chunks:
        lang = c.get("lang") or "en"
        out[lang] = out.get(lang, 0) + 1
    return out


def overview() -> dict:
    """知识库概览（规划 §20.5）。"""
    from sqlalchemy import func

    with session_scope() as sess:
        papers = sess.query(Paper).filter(Paper.deleted_at.is_(None)).all()
        chunk_count = sess.query(func.count(Chunk.id)).scalar() or 0
        by_type = dict(sess.query(Chunk.chunk_type, func.count(Chunk.id))
                       .group_by(Chunk.chunk_type).all())
        # 语言行数按 id 后缀数（`…:zh` / `…:en` / `…:any`）—— 不用解析 metadata_json，
        # 免得依赖 JSON 的序列化空格这种细节（规划 §8：一处事实源）。
        # `any` 是语言无关的公式原子行（本轮新增）：不统计它，总数就对不上。
        by_lang = {
            "zh": int(sess.query(func.count(Chunk.id))
                      .filter(Chunk.id.like("%:zh")).scalar() or 0),
            "en": int(sess.query(func.count(Chunk.id))
                      .filter(Chunk.id.like("%:en")).scalar() or 0),
            "any": int(sess.query(func.count(Chunk.id))
                       .filter(Chunk.id.like("%:any")).scalar() or 0),
        }
        index_versions = dict(sess.query(Chunk.paper_id, func.max(Chunk.index_version))
                              .group_by(Chunk.paper_id).all())
        model_ids = [m for (m,) in sess.query(Chunk.embedding_model_id).distinct().all()]
    vector = None
    try:
        from backend.repositories.vector.lancedb_store import vector_store

        vector = vector_store.stats()
    except Exception:  # noqa: BLE001
        vector = {"available": False}
    return {
        "papers_total": len(papers),
        "papers_in_kb": sum(1 for p in papers if p.in_knowledge_base),
        "chunks": int(chunk_count),
        "chunks_by_type": by_type,
        "chunks_by_lang": by_lang,
        "embedding_models": _model_labels([m for m in model_ids if m]),
        "index_versions": index_versions,
        "vector_store": vector,
        "items": _overview_items(papers),
    }


def _model_labels(model_ids: list[str]) -> list[dict]:
    """把 ``embedding_model_id`` 换成**人能看的名字**。

    以前这里直接把 id 贴出去，界面上就出现 `embedding-7036089f` 这种看不懂的徽标
    （用户报"模型显示错误"）。id 认不出来时原样回退，不隐藏。
    """
    import json

    from backend.repositories.mysql.models import ModelConfig

    if not model_ids:
        return []
    with session_scope() as sess:
        rows = (sess.query(ModelConfig.id, ModelConfig.name, ModelConfig.model_name,
                           ModelConfig.parameters_json)
                .filter(ModelConfig.id.in_(model_ids)).all())
    known = {}
    for mid, name, model_name, params_json in rows:
        try:
            dim = (json.loads(params_json or "{}") or {}).get("dim")
        except ValueError:
            dim = None
        label = model_name or name or mid
        known[mid] = {"id": mid, "label": f"{label}（{dim} 维）" if dim else label,
                      "model_name": model_name, "dim": dim}
    return [known.get(m, {"id": m, "label": m, "model_name": None, "dim": None})
            for m in model_ids]


def _overview_items(papers: list[Paper]) -> list[dict]:
    from sqlalchemy import func

    with session_scope() as sess:
        counts = dict(sess.query(Chunk.paper_id, func.count(Chunk.id))
                      .group_by(Chunk.paper_id).all())
    return [{"id": p.id, "title": p.title, "status": p.status,
             "in_knowledge_base": bool(p.in_knowledge_base),
             "chunks": int(counts.get(p.id, 0))} for p in papers]


def set_kb_membership(paper_id: str, include: bool) -> dict:
    with session_scope() as sess:
        row = sess.get(Paper, paper_id)
        if row is None or row.deleted_at is not None:
            raise NotFoundError(f"论文不存在：{paper_id}")
        row.in_knowledge_base = include
        sess.flush()
        return {"paper_id": paper_id, "in_knowledge_base": include}


def remove_from_kb(paper_id: str) -> dict:
    """**从知识库移除**：删向量 + 删 chunk 元数据 + 取消"已纳入"标记。

    「纳入 / 移出」原来是只翻一个布尔（索引还留着），用户口径是把它和索引合并：
    纳入 = 建索引；所以"移除"必须真的把索引清掉，否则 MySQL 与向量库会长期不一致。
    论文本体、译文、笔记、高亮**都不动** —— 移除的是知识库（派生数据），不是论文。
    """
    paper_service.get_paper(paper_id)
    from backend.repositories.vector.lancedb_store import vector_store

    vectors = 0
    vector_error = None
    try:
        vectors = vector_store.delete_paper(paper_id)
    except Exception as exc:  # noqa: BLE001
        vector_error = f"{type(exc).__name__}: {str(exc)[:200]}"
        log.warning("移除向量失败（继续清元数据）：%s", exc)
    with session_scope() as sess:
        chunks = (sess.query(Chunk).filter(Chunk.paper_id == paper_id)
                  .delete(synchronize_session=False))
        row = sess.get(Paper, paper_id)
        if row is not None:
            row.in_knowledge_base = False
    return {"paper_id": paper_id, "removed_chunks": int(chunks),
            "removed_vectors": int(vectors), "in_knowledge_base": False,
            "vector_error": vector_error,
            "at": now().isoformat(timespec="seconds")}


def reconcile(*, paper_id: str | None = None, fix: bool = False) -> dict:
    """MySQL chunk 元数据 ↔ 向量库 对账。

    - ``orphans``：MySQL 有、向量库没有 —— 换 embedding 模型时向量表被整体重建，
      旧元数据就留了下来（真实案例：`scanned-demo` 的 10 条，导致概览显示"两侧条数不一致"
      且 embedding 模型徽标里多出一个陈旧的 `mock-embedding`）；
    - ``missing``：向量库有、MySQL 没有（元数据被删但向量还在）。

    ``fix=True`` 时两边都清掉（向量库是派生数据，规划 §6.2）。
    """
    from backend.repositories.vector.lancedb_store import vector_store

    store_ids = set(vector_store.chunk_ids(paper_id=paper_id))
    with session_scope() as sess:
        q = sess.query(Chunk.id)
        if paper_id:
            q = q.filter(Chunk.paper_id == paper_id)
        db_ids = {i for (i,) in q.all()}

    orphans = sorted(db_ids - store_ids)
    missing = sorted(store_ids - db_ids)
    removed_meta = removed_vectors = 0
    if fix:
        if missing:
            removed_vectors = vector_store.delete_chunks(missing)
        for i in range(0, len(orphans), 500):
            part = orphans[i:i + 500]
            with session_scope() as sess:
                removed_meta += int(sess.query(Chunk).filter(Chunk.id.in_(part))
                                    .delete(synchronize_session=False))
    return {
        "paper_id": paper_id,
        "mysql_chunks": len(db_ids), "vector_chunks": len(store_ids),
        "orphans": len(orphans), "missing": len(missing),
        "orphan_sample": orphans[:5], "missing_sample": missing[:5],
        "consistent": not orphans and not missing,
        "fixed": bool(fix), "removed_metadata": removed_meta,
        "removed_vectors": int(removed_vectors),
        "at": now().isoformat(timespec="seconds"),
    }


def index_status(paper_id: str | None = None) -> dict:
    from sqlalchemy import func

    with session_scope() as sess:
        q = sess.query(Chunk)
        if paper_id:
            q = q.filter(Chunk.paper_id == paper_id)
        total = q.count()
        with_hash = q.filter(Chunk.content_hash.isnot(None)).count()
        versions = (sess.query(Chunk.paper_id, func.max(Chunk.index_version))
                    .group_by(Chunk.paper_id).all())
        db_ids = {i for (i,) in sess.query(Chunk.id).all()}
    vector = None
    store_ids: set[str] = set()
    try:
        from backend.repositories.vector.lancedb_store import vector_store

        store_ids = set(vector_store.chunk_ids(paper_id=paper_id))
        vector = {"available": True, "count": len(store_ids)}
    except Exception as exc:  # noqa: BLE001
        vector = {"available": False, "error": str(exc)[:200]}
    orphans = len(db_ids - store_ids) if not paper_id else None
    missing = len(store_ids - db_ids) if not paper_id else None
    return {"paper_id": paper_id, "chunks": total, "chunks_with_hash": with_hash,
            "index_versions": dict(versions), "vector": vector,
            # 只看**全库**对账（单篇视图下这两个数没意义，交给 /knowledge/reconcile）
            "orphan_chunks": orphans, "missing_vectors": missing,
            "consistent": None if paper_id else (not orphans and not missing)}


def new_chunk_id(paper_id: str, chunk_type: str) -> str:
    return f"{paper_id}:{chunk_type}:{new_id('c')[-8:]}"


# --------------------------------------------------------------------------
# 知识条目的图片（插图 / 表格 / 公式的裁图）
#
# 用户口径：**图片也是知识库的一类内容** —— 检索命中插图/表格/公式时要能看到缩略图，
# 浏览"插图"时要能像图库一样翻。所以每个条目都带一个浏览器能直接取的 `image_url`。
#
# 两个来源，优先级不同：
# 1. 插图 / 表格 → **按图注锚定的裁图**（`figure_regions`），与阅读器里显示的是**同一张**
#    （阅读器早就因为"解析阶段的裁图会把相邻的图和小标题一起裁进来"换成了这张）；
# 2. 公式（以及匹配不到区域时的插图/表格）→ **解析阶段已经落盘的裁图**，
#    走 `data/papers/` 的静态挂载（`main._mount_static` 把 `/papers` 指到它）。
#
# 拿不到图就返回 `None`：前端不显示缩略图，**绝不塞一张错图**。
# --------------------------------------------------------------------------

#: 图号识别（`Figure 3` / `Fig. 3` / `Table 1` / `图 2`）—— 与 figure_regions 同一套规则
_FIG_NUMBER_RE = re.compile(r"(?:Figure|Fig\.?|Table|图|表)\s*(\d{1,3})", re.IGNORECASE)


def _static_crop_url(image_path: str | None) -> str | None:
    """解析阶段的裁图文件 → 浏览器可取的 URL（不在论文目录下 / 文件不在 → None）。"""
    if not image_path:
        return None
    try:
        root = Path(get_settings().papers_dir).resolve()
        path = Path(str(image_path)).resolve()
        rel = path.relative_to(root)
    except (ValueError, OSError):        # 不在 data/papers 下（或路径非法）
        return None
    if not path.is_file():
        return None
    return f"/papers/{rel.as_posix()}"


def _figure_region_url(paper_id: str, chunk_type: str, page_no: int,
                       caption: str) -> str | None:
    """插图/表格 → 「按图注锚定的裁图」URL（同页 + 同类 + 图号相同）。

    图号缺失时用图注文本前缀兜底；仍匹配不到返回 `None`（退到解析阶段那张裁图）。
    """
    from backend.services.paper import figure_regions

    if not paper_id or page_no <= 0:
        return None
    try:
        regions = figure_regions.page_regions(paper_id, int(page_no))
    except Exception as exc:  # noqa: BLE001 —— 缺 IR / 读盘失败都不该让检索整条失败
        log.debug("读取插图区域失败（%s p%s）：%s", paper_id, page_no, exc)
        return None
    kind = "figure" if chunk_type == "figure" else "table"
    match = _FIG_NUMBER_RE.search(caption or "")
    number = match.group(1) if match else ""
    head = (caption or "").strip()[:40]
    for i, region in enumerate(regions):
        if region.get("kind") != kind:
            continue
        if number and str(region.get("number") or "") == number:
            return (f"{get_settings().api_prefix}/papers/{paper_id}"
                    f"/figures/region/{int(page_no)}/{i}")
        if not number and head and (region.get("caption") or "").strip()[:40] == head:
            return (f"{get_settings().api_prefix}/papers/{paper_id}"
                    f"/figures/region/{int(page_no)}/{i}")
    return None


def chunk_image_url(paper_id: str, chunk_type: str, page_no: int,
                    metadata: dict | None = None) -> str | None:
    """知识条目的缩略图 URL；正文（text）没有图，返回 `None`。"""
    meta = metadata or {}
    if chunk_type in ("figure", "table"):
        url = _figure_region_url(paper_id, chunk_type, page_no,
                                 str(meta.get("caption") or ""))
        return url or _static_crop_url(meta.get("image_path"))
    if chunk_type == "formula":
        return _static_crop_url(meta.get("crop"))
    return None


def _chunk_row_meta(chunk_id: str) -> dict:
    """MySQL 里那条 chunk 元数据（token 数 / 索引版本）；没有就返回空。"""
    with session_scope() as sess:
        row = sess.get(Chunk, chunk_id)
        if row is None:
            return {}
        return {"tokens": row.token_count, "index_version": row.index_version,
                "content_hash": row.content_hash}


def chunk_detail(chunk_id: str) -> dict:
    """单个 chunk 的**完整正文**（知识库页「展开全文」用，规划 §8：全文只在向量库）。

    为什么单独开一个接口：列表接口按规划 §8 只下发 200 字 preview，而"展开全文"要的正是全文。
    这里只读那一条向量行（顺便把 MySQL 的 token/版本补齐），不触发任何任务、不写任何状态。

    **不下发同 `pair_id` 的对侧语种**（用户口径：展开全文只看当前 chunk，中英对照回阅读器）。
    只给 `pair_id`，需要时前端/后续再取。
    """
    from backend.repositories.vector.lancedb_store import vector_store

    cid = str(chunk_id or "").strip()
    if not cid:
        raise ValidationError("缺少 chunk_id")
    rows = vector_store.get_rows([cid])
    if not rows:
        raise NotFoundError(f"chunk 不存在或还没入库：{cid}")
    row = rows[0]
    meta = row.get("metadata") or {}
    paper_id = str(row.get("paper_id") or "")
    page_no = int(row.get("page_no") or 0)
    chunk_type = str(row.get("chunk_type") or "")
    content = str(row.get("content") or "")
    extra = _chunk_row_meta(cid)
    return {
        "chunk_id": cid,
        "paper_id": paper_id,
        "paper_title": _paper_titles([paper_id]).get(paper_id, paper_id) if paper_id else "",
        "block_id": row.get("block_id"), "page": page_no,
        "section": row.get("section"), "chunk_type": chunk_type,
        "lang": row.get("lang") or "en",
        "pair_id": meta.get("pair_id") or None,
        "content": content, "chars": len(content),
        "preview": truncate(content, 200),
        "tokens": extra.get("tokens"), "index_version": extra.get("index_version"),
        "image_url": chunk_image_url(paper_id, chunk_type, page_no, meta),
        # 公式条目：把"这条 LaTeX 从哪来、可不可信"一并下发 ——
        # 知识库页据此决定要不要渲染 KaTeX / 挂「待修复」徽标 / 展示解析版对照。
        "latex": meta.get("latex") or None,
        "latex_parsed": meta.get("latex_parsed") or None,
        "latex_source": meta.get("latex_source") or None,
        "raw_text": meta.get("raw_text") or None,
        "grade": meta.get("grade") or None,
        "needs_review": bool(meta.get("needs_review")),
    }


# --------------------------------------------------------------------------
# 相似检索（规划 §19.1 / §20.4；任务书功能⑥「跨文献技术比对」）
# --------------------------------------------------------------------------


def similar(payload: dict) -> dict:
    """跨文献技术相似度检索：向量 + 词法 → RRF → 重排。

    查询可以是**一段文本**，也可以是**库里已有的 chunk**（`chunk_id`）——
    后者是"我这篇的这段技术，别的论文里谁在做类似的"这个用法的主路径：
    直接拿该 chunk 的原文当查询，并把它自己排除掉。
    """
    from backend.services.knowledge import retrieval

    query = str(payload.get("query") or "").strip()
    chunk_id = payload.get("chunk_id")
    paper_id = payload.get("paper_id") or None
    source = None
    exclude_chunks: list[str] = []

    if chunk_id:
        from backend.repositories.vector.lancedb_store import vector_store

        rows = vector_store.get_rows([chunk_id])
        if not rows:
            raise NotFoundError(f"chunk 不存在或还没入库：{chunk_id}")
        row = rows[0]
        query = query or str(row.get("content") or "").strip()
        paper_id = paper_id or row.get("paper_id")
        exclude_chunks = [chunk_id]
        source = {"chunk_id": chunk_id, "paper_id": row.get("paper_id"),
                  "block_id": row.get("block_id"), "page": row.get("page_no"),
                  "lang": row.get("lang")}

    if not query:
        raise ValidationError("要么给 query，要么给库里已有的 chunk_id")

    scope = str(payload.get("scope") or ("cross" if paper_id else "all"))
    if scope not in ("all", "cross", "paper"):
        raise ValidationError("scope 只能是 all / cross / paper")

    chunk_types = payload.get("chunk_types") or None
    langs = payload.get("langs") or None
    top_k = payload.get("top_k")
    result = retrieval.search(
        query, mode="similar", scope=scope, paper_id=paper_id,
        chunk_types=chunk_types, langs=langs, exclude_chunks=exclude_chunks,
        top_k=int(top_k) if top_k else None,
    )
    items = [public_item(i) for i in _decorate_items(result["items"])]
    return {
        "implemented": True,
        "query": query,
        "source": source,
        "scope": scope,
        "items": items,
        "retrieval": result["retrieval"],
    }


def _decorate_items(items: list[dict]) -> list[dict]:
    """补上论文标题与展示用摘录（检索层不该知道"标题"这种业务字段）。

    **保留 `content` / `metadata`**：RAG 的 ContextBuilder 要用原文与 `pair_id`
    （踩过一次：装饰时把 `content` 丢了，上下文里一条来源都拼不出来）。
    对外响应在各接口边界上再裁掉这两个字段。
    """
    titles = _paper_titles(sorted({i.get("paper_id") for i in items if i.get("paper_id")}))
    out = []
    for i in items:
        paper_id = str(i.get("paper_id") or "")
        chunk_type = str(i.get("chunk_type") or "")
        page_no = int(i.get("page_no") or 0)
        metadata = i.get("metadata") or {}
        out.append({
            "chunk_id": i.get("chunk_id"), "paper_id": i.get("paper_id"),
            "paper_title": titles.get(i.get("paper_id") or "", ""),
            "block_id": i.get("block_id"), "page": i.get("page_no"),
            "section": i.get("section"), "chunk_type": i.get("chunk_type"),
            "lang": i.get("lang") or "en",
            "langs": i.get("langs") or [i.get("lang") or "en"],
            "snippet": truncate(str(i.get("content") or ""), 260),
            # 图注本身（插图/表格 chunk 的正文就是"插图 <块 id>：<图注>"，
            # 列表里显示图注更干净；正文 chunk 没有这个字段）
            "caption": metadata.get("caption"),
            "content": i.get("content"),
            "metadata": metadata,
            # 插图 / 表格 / 公式的裁图（正文条目为 None）——知识库页靠它出缩略图
            "image_url": chunk_image_url(paper_id, chunk_type, page_no, metadata),
            "scores": {
                "vector": i.get("vector_score"), "lexical": i.get("lexical_score"),
                "rrf": round(float(i.get("rrf") or 0.0), 6),
                "rerank": i.get("rerank_score"),
            },
            "ranks": {"rrf": i.get("rrf_rank"), "rerank": i.get("rerank_rank"),
                      **{f"{k}_rank": v for k, v in (i.get("sources") or {}).items()}},
        })
    return out


#: 只给前端看的字段（`content` / `metadata` / 内部校验用文本都不下发）
PUBLIC_ITEM_FIELDS = ("chunk_id", "paper_id", "paper_title", "block_id", "page",
                      "section", "chunk_type", "lang", "langs", "snippet", "caption",
                      "image_url", "scores", "ranks")


def public_item(item: dict) -> dict:
    return {k: item.get(k) for k in PUBLIC_ITEM_FIELDS if k in item}


def _paper_titles(paper_ids: list[str]) -> dict[str, str]:
    if not paper_ids:
        return {}
    with session_scope() as sess:
        rows = (sess.query(Paper.id, Paper.title)
                .filter(Paper.id.in_(paper_ids)).all())
    return {pid: title or pid for pid, title in rows}


