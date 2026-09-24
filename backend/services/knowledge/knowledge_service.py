"""知识库服务（产线 C：chunk 化 → 向量化 → 入库）。

规划 §11 / §21.6：chunk 化 → Embedding → 写入 MySQL chunk 元数据 → 写 LanceDB。
检索（向量/词法/混合/重排/RAG 问答）在阶段 14–17 的后续轮次实现；
本模块负责 chunk 与索引链路，保证「删除论文 → 清索引」与「重建索引」可用。

**双语 chunk（本轮新增）**：同一个 chunk 出 ``zh`` / ``en`` 两行（chunk id 加 ``:zh`` /
``:en`` 后缀，``lang`` 进 ``metadata_json``）—— **不改表结构**（不动 MySQL 列，
避免 ALTER 迁移）。理由见 ``build_chunks_for_paper`` 的 docstring。
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.core.config import get_settings
from backend.core.errors import NotFoundError, ValidationError
from backend.core.logging import get_logger
from backend.core.utils import json_dumps, new_id, now, sha256_text, truncate
from backend.repositories.mysql.models import Block, Chunk, Paper
from backend.repositories.mysql.session import session_scope
from backend.services.model.model_manager import model_manager
from backend.services.paper import paper_service, reader_service

log = get_logger(__name__)

# 四类 chunk（规划 §10 ⑫ chunk_type）
CHUNK_TYPES = ("text", "formula", "figure", "table")
# 目标 chunk 字符数（段合并上限）
TARGET_CHUNK_CHARS = 900
MIN_CHUNK_CHARS = 40
# 双语：同一 chunk 的两种语言入库行（顺序即入库顺序，zh 在前便于人肉核对）
LANGS = ("zh", "en")
# 中文侧的最小长度门槛按**字**算：中文 1 字的信息量约等于英文 2~3 字符，沿用 40 字符
# 会把大量正常的中文块误杀（实测把 "## B.2 QJL：1 比特内积量化" 这类小节标题全丢了）。
# 门槛只用来兜"取到的是残渣"，不承担质量判断 —— 对不上译文的行压根不会出（见 _lookup_zh）。
MIN_ZH_CHARS = 8

_WS_RE = re.compile(r"\s+")
# 只保留小写字母 / 数字 / 汉字
_COMPACT_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
# 页内对齐的最小可信重叠（签名字符数）
_MIN_ALIGN_CHARS = 12


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


def _chunk_variants(base: list[dict], zh: dict[int, list[tuple[str, str]]],
                    langs: tuple[str, ...]) -> list[dict]:
    """把「单语 chunk」展开成 zh/en 两行。

    译文缺失的处理：正文块 / 图注块**翻译没做就不出 zh 行**（不拿原文冒充译文，
    否则索引里会出现"中文行其实是英文"的脏数据）；公式块的语言无关（只有 LaTeX），
    两侧同文本、照出。
    """
    out: list[dict] = []
    for c in base:
        parts: list[tuple[int, str]] = c.get("_parts") or []
        for lang in langs:
            if lang == "en":
                content = c["content"]
            elif c["chunk_type"] == "formula":
                content = c["content"]            # LaTeX 无语言之分
            else:
                pieces = [_lookup_zh(zh, p, t) for p, t in parts]
                pieces = [p for p in pieces if p]
                # 相邻块共用一个译文行（切分不同时的正常现象）→ 去重，别把同一段中文塞两次
                deduped: list[str] = []
                for piece in pieces:
                    if not deduped or deduped[-1] != piece:
                        deduped.append(piece)
                pieces = deduped
                if not pieces or not parts:
                    continue                      # 还没翻译 → 不出 zh 行
                if len(pieces) < len(parts):
                    log.debug("chunk %s 的 zh 侧只对上 %d/%d 块（其余未翻译或切分不同）",
                              c["id"], len(pieces), len(parts))
                content = "\n\n".join(pieces)
            # 长度门槛只对原文侧用；中文侧按字数另算（MIN_ZH_CHARS）
            floor = MIN_CHUNK_CHARS if lang == "en" else MIN_ZH_CHARS
            if c["chunk_type"] == "text" and len(content) < floor:
                continue
            out.append({
                **{k: v for k, v in c.items() if not k.startswith("_")},
                "id": f"{c['id']}:{lang}",
                "lang": lang,
                "content": content,
                "content_hash": sha256_text(content),
                "content_preview": truncate(content, 200),
                "token_count": _approx_tokens(content),
                "metadata": {**(c.get("metadata") or {}), "lang": lang,
                             "pair_id": c["id"]},
            })
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


def _collect_chunks(paper_id: str) -> list[dict]:
    """单语（原文）chunk：与旧实现一致，只是额外留下 ``_parts`` 供译文对齐。"""
    md_map = reader_service.load_markdown_map(paper_id)
    from backend.parsing.ir.ir_reader import IRReader

    ir = IRReader(paper_service.paper_paths(paper_id)["ir"])
    formulas = {f["block_id"]: f for f in ir.formulas()}
    figures = {f["block_id"]: f for f in ir.figures()}
    tables = {t["block_id"]: t for t in ir.tables()}

    with session_scope() as sess:
        blocks = (sess.query(Block).filter(Block.paper_id == paper_id)
                  .order_by(Block.order_global.asc()).all())
        rows = [_block_row(b, md_map) for b in blocks]

    chunks: list[dict] = []
    buffer: list[dict] = []
    buffer_chars = 0

    def flush_text() -> None:
        nonlocal buffer, buffer_chars
        if not buffer:
            return
        text = "\n\n".join(b["text"] for b in buffer if b["text"])
        if len(text) >= MIN_CHUNK_CHARS:
            chunks.append(_make_chunk(
                paper_id, "text", text, buffer[0], buffer[-1],
                section=buffer[0].get("section") or "",
                parts=[(b["page"], b["text"]) for b in buffer if b["text"]],
                metadata={"block_ids": [b["id"] for b in buffer],
                          "pages": sorted({b["page"] for b in buffer})},
            ))
        buffer = []
        buffer_chars = 0

    for b in rows:
        bid = b["id"]
        if b["type"] == "heading":
            flush_text()
            buffer = [b]
            buffer_chars = len(b["text"])
            continue
        if b["type"] in ("ref",):
            continue
        if bid in formulas:
            flush_text()
            f = formulas[bid]
            latex = f.get("latex_final") or f.get("latex_repaired") or ""
            cands = f.get("latex_candidate") or []
            text = f"公式 {bid}：{latex or (cands[0] if cands else f.get('raw_text') or '')}"
            chunks.append(_make_chunk(
                paper_id, "formula", text, b, b, section=b.get("section") or "",
                metadata={"latex": latex, "grade": f.get("grade"),
                          "raw_text": f.get("raw_text"),
                          "crop": f.get("image_path")},
            ))
            continue
        if bid in figures:
            flush_text()
            f = figures[bid]
            caption = f.get("caption") or ""
            text = f"插图 {bid}：{caption}"
            chunks.append(_make_chunk(
                paper_id, "figure", text, b, b, section=b.get("section") or "",
                # 图注本身是可翻译的正文块 → 拿它的签名去译文索引里取中文图注
                parts=[(b["page"], caption)] if caption else [],
                metadata={"caption": caption, "image_path": f.get("image_path")},
            ))
            continue
        if bid in tables:
            flush_text()
            t = tables[bid]
            caption = t.get("caption") or ""
            text = f"表格 {bid}：{caption}"
            chunks.append(_make_chunk(
                paper_id, "table", text, b, b, section=b.get("section") or "",
                parts=[(b["page"], caption)] if caption else [],
                metadata={"caption": caption, "image_path": t.get("image_path")},
            ))
            continue

        text = b["text"] or ""
        if not text:
            continue
        buffer.append(b)
        buffer_chars += len(text)
        if buffer_chars >= TARGET_CHUNK_CHARS:
            flush_text()
    flush_text()
    return chunks


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
            "section": meta.get("section_title") or b.section_id or ""}


def _make_chunk(paper_id: str, chunk_type: str, content: str, first: dict,
                last: dict, *, section: str = "", metadata: dict | None = None,
                parts: list[tuple[int, str]] | None = None) -> dict:
    cid = f"{paper_id}:{chunk_type}:{first['id']}"
    if chunk_type == "text" and first["id"] != last["id"]:
        cid = f"{paper_id}:text:{first['id']}-{last['id']}"
    return {
        "id": cid, "paper_id": paper_id, "chunk_type": chunk_type,
        "page_no": first["page"], "order_global": first["order"],
        "block_id": first["id"], "end_block_id": last["id"],
        "section": section, "content": content,
        "content_hash": sha256_text(content),
        "content_preview": truncate(content, 200),
        "token_count": _approx_tokens(content),
        "metadata": metadata or {},
        "_parts": parts or [],
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
        # 双语行数按 id 后缀数（`…:zh` / `…:en`）—— 不用解析 metadata_json，
        # 免得依赖 JSON 的序列化空格这种细节（规划 §8：一处事实源）
        by_lang = {
            "zh": int(sess.query(func.count(Chunk.id))
                      .filter(Chunk.id.like("%:zh")).scalar() or 0),
            "en": int(sess.query(func.count(Chunk.id))
                      .filter(Chunk.id.like("%:en")).scalar() or 0),
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
